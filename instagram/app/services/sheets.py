"""Mirroring leads into the clinic's own Google Sheet.

The people who own these clinics do not open dashboards. They open the
spreadsheet they already keep, and a lead that is only in a dashboard is,
to them, a lead nobody told them about. So every patient the bots talk to
lands in a sheet with four columns the owner asked for:

    ism familiya | telefon | manba | bemor haqida qisqacha

A mirror, never a source. Nothing is read back into the application, and a
deleted, renamed or broken sheet loses nothing the database does not still
hold -- which is what makes it safe for every failure here to be swallowed
rather than retried into the patient's face.

One tab for both channels, with the source column saying which. Two tabs
was the other reading of the request, and it makes "how many leads this
week" a sum the owner has to do by hand.

Google's REST API directly, over the httpx client this project already
uses, with google-auth for the token. gspread and google-api-python-client
each bring a stack of transitive dependencies to wrap two endpoints
(values.append and values.update), and one of those endpoints is the whole
integration.
"""

import base64
import binascii
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any, cast

import google.auth.transport.requests as google_requests
import httpx
from google.oauth2 import service_account

from app.core.config import Settings, get_settings
from app.services.appointment import CLINIC_TIMEZONE
from app.services.conversation_signals import looks_like_a_phone_number

logger = logging.getLogger(__name__)

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)

# Two sheets, and only one of them is for reading.
#
# A tab per day put the date in the tab strip but made "everyone who wrote in
# this week" impossible to see at all, and left the strip growing by one every
# morning. One sheet with a date picker answers both: the owner opens Lidlar,
# chooses a day, and sees that day.
#
# It needs two, because a formula cannot write into the same cells a bot is
# appending to. VIEW_SHEET is what the clinic looks at -- a dropdown and a
# FILTER over the other one. DATA_SHEET is where the leads actually land, and
# is hidden, because a spreadsheet with two lists on it is a spreadsheet where
# somebody edits the wrong one.
VIEW_SHEET = "Lidlar"
DATA_SHEET = "Baza"

# The hidden sheet keeps the date; the view does not, since the date is the
# thing you just chose at the top of it.
DATA_HEADER = ("Sana", "Ism familiya", "Telefon", "Manba", "Bemor haqida qisqacha")
HEADER = DATA_HEADER[1:]

_DATA_LAST_COLUMN = "E"
_DATE_COLUMN = "A"
_PHONE_COLUMN = "C"
# Where the hidden sheet keeps the sorted list of days that actually have
# somebody on them, which is what the dropdown offers.
_DAYS_COLUMN = "G"

# Row 1 is the picker, row 2 is blank, row 3 is the header, and the filter
# fills from row 4 down.
_PICKER_CELL = "B1"
# The same cell written absolutely, for use inside a formula that must not
# shift when rows are inserted above it.
_PICKER_REF = "$B$1"
_VIEW_HEADER_ROW = 3

# Enough for a sentence about a toothache, short enough that the column
# stays readable in a spreadsheet without anyone widening it.
MAX_COMMENT = 180

# Bounded so a slow Google never becomes a slow reply. This runs after the
# patient has already been answered, but it still holds a worker slot.
TIMEOUT_SECONDS = 20.0


# The look of a freshly created tab. Colours chosen for a document somebody
# reads on a phone between patients: one dark header that survives scrolling,
# quiet banding so the eye keeps its place across four columns, and the
# story column wide enough to hold a sentence.
_HEADER_COLOUR = "#0F766E"
_BAND_COLOUR = "#F1F5F9"
_COLUMN_WIDTHS = (220, 170, 130, 560)


def _rgb(value: str) -> dict[str, float]:
    raw = value.lstrip("#")
    return {
        "red": int(raw[0:2], 16) / 255,
        "green": int(raw[2:4], 16) / 255,
        "blue": int(raw[4:6], 16) / 255,
    }


def _style_requests(sheet_id: int, header: Sequence[str]) -> list[dict[str, Any]]:
    white = {"red": 1.0, "green": 1.0, "blue": 1.0}
    ink = _rgb("#1F2937")
    full = {"sheetId": sheet_id, "startColumnIndex": 0, "endColumnIndex": len(header)}
    requests: list[dict[str, Any]] = [
        # The header stays in view through a year of leads.
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "repeatCell": {
                "range": {**full, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _rgb(_HEADER_COLOUR),
                        "textFormat": {"foregroundColor": white, "bold": True, "fontSize": 11},
                        "verticalAlignment": "MIDDLE",
                        "horizontalAlignment": "LEFT",
                        "padding": {"left": 10, "right": 10, "top": 6, "bottom": 6},
                    }
                },
                "fields": (
                    "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,"
                    "horizontalAlignment,padding)"
                ),
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 40},
                "fields": "pixelSize",
            }
        },
        {
            "repeatCell": {
                "range": {**full, "startRowIndex": 1},
                "cell": {
                    "userEnteredFormat": {
                        # Without this the story runs on under the next
                        # column and the owner reads half a sentence.
                        "wrapStrategy": "WRAP",
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {"foregroundColor": ink, "fontSize": 10},
                        "padding": {"left": 10, "right": 10, "top": 6, "bottom": 6},
                    }
                },
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment,textFormat,padding)",
            }
        },
        {
            "repeatCell": {
                # A phone number is a label, not a quantity. Left as a number
                # it loses a leading zero and renders as 9.989E+11.
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": header.index("Telefon"),
                    "endColumnIndex": header.index("Telefon") + 1,
                },
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        {
            "addBanding": {
                "bandedRange": {
                    "range": {**full, "startRowIndex": 1},
                    "rowProperties": {
                        "firstBandColor": white,
                        "secondBandColor": _rgb(_BAND_COLOUR),
                    },
                }
            }
        },
        # So the owner can keep only this week's Telegram leads without
        # asking anyone how.
        {"setBasicFilter": {"filter": {"range": {**full, "startRowIndex": 0}}}},
    ]
    requests.extend(
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": index,
                    "endIndex": index + 1,
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        }
        for index, width in enumerate(_COLUMN_WIDTHS)
    )
    return requests


class SheetsError(Exception):
    """Raised when the sheet cannot be written. Never reaches a patient."""


def _view_requests(sheet_id: int) -> list[dict[str, Any]]:
    """The picker row, the header, and a dropdown of the days on record."""
    white = {"red": 1.0, "green": 1.0, "blue": 1.0}
    header_row = _VIEW_HEADER_ROW - 1
    requests: list[dict[str, Any]] = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": _VIEW_HEADER_ROW},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        },
        # "Sana:" and the chosen day, big enough to read as a control rather
        # than as data.
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": 2,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True, "fontSize": 12},
                        "verticalAlignment": "MIDDLE",
                        "padding": {"left": 10, "right": 10, "top": 6, "bottom": 6},
                    }
                },
                "fields": "userEnteredFormat(textFormat,verticalAlignment,padding)",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 36},
                "fields": "pixelSize",
            }
        },
        # The picker holds a date as text, because that is what the hidden
        # sheet stores and what the FILTER compares against. Left as a normal
        # cell, choosing a day turns it into a real date value and the
        # comparison quietly matches nothing -- the table just says nobody
        # wrote in.
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 1,
                    "endColumnIndex": 2,
                },
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        # The dropdown itself, over the days the hidden sheet has on record.
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 1,
                    "endColumnIndex": 2,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_RANGE",
                        "values": [
                            {
                                "userEnteredValue": (
                                    f"='{DATA_SHEET}'!${_DAYS_COLUMN}$1:${_DAYS_COLUMN}"
                                )
                            }
                        ],
                    },
                    "showCustomUi": True,
                    # Not strict: a day with nobody on it is a legitimate thing
                    # to type, and being refused for it is confusing.
                    "strict": False,
                },
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": header_row,
                    "endRowIndex": header_row + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(HEADER),
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _rgb(_HEADER_COLOUR),
                        "textFormat": {"foregroundColor": white, "bold": True, "fontSize": 11},
                        "verticalAlignment": "MIDDLE",
                        "padding": {"left": 10, "right": 10, "top": 6, "bottom": 6},
                    }
                },
                "fields": (
                    "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,padding)"
                ),
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": header_row + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(HEADER),
                },
                "cell": {
                    "userEnteredFormat": {
                        "wrapStrategy": "WRAP",
                        "verticalAlignment": "MIDDLE",
                        "textFormat": {"fontSize": 10},
                        "padding": {"left": 10, "right": 10, "top": 6, "bottom": 6},
                    }
                },
                "fields": "userEnteredFormat(wrapStrategy,verticalAlignment,textFormat,padding)",
            }
        },
    ]
    requests.extend(
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": index,
                    "endIndex": index + 1,
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        }
        for index, width in enumerate(_COLUMN_WIDTHS)
    )
    return requests


@dataclass(frozen=True)
class LeadRow:
    """One patient, as the clinic's owner reads them."""

    name: str | None
    phone: str | None
    source: str
    comment: str | None

    def as_cells(self) -> list[str]:
        """The row as the clinic reads it."""
        comment = (self.comment or "").strip().replace(chr(10), " ")
        if len(comment) > MAX_COMMENT:
            comment = comment[: MAX_COMMENT - 1].rstrip() + "…"
        return [self.name or "", self.phone or "", self.source, comment]


def _load_credentials(raw: str) -> service_account.Credentials:
    """The service-account key, given verbatim or base64-encoded.

    Both, because the key's private_key field contains newlines and whether
    a host can carry those in an environment variable varies -- Railway can,
    a .env file read line by line cannot. Guessing between the two costs one
    try/except and removes a class of "it works locally" bug.
    """
    text = raw.strip()
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise SheetsError("GOOGLE_SERVICE_ACCOUNT_JSON is neither JSON nor base64") from exc
    try:
        info = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SheetsError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}") from exc
    try:
        # google-auth ships without complete annotations, so this reads as
        # an untyped call under --strict. Silenced at the call rather than
        # for the module, so the rest of this file stays fully checked.
        credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            info, scopes=list(SCOPES)
        )
        return cast("service_account.Credentials", credentials)
    except Exception as exc:  # noqa: BLE001 - google's message names the missing field
        raise SheetsError(f"Not a usable service account key: {exc}") from exc


class SheetsMirror:
    """Appends leads to one worksheet of one spreadsheet."""

    def __init__(self, settings: Settings | None = None) -> None:
        resolved = settings or get_settings()
        if resolved.google_service_account_json is None or (
            resolved.google_sheets_spreadsheet_id is None
        ):
            raise SheetsError(
                "GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEETS_SPREADSHEET_ID are both required"
            )
        self._credentials = _load_credentials(resolved.google_service_account_json)
        self._spreadsheet_id = resolved.google_sheets_spreadsheet_id
        # Whether the two sheets have been checked this process. A flag
        # rather than a request, because this runs before every lead.
        self._ready = False

    def _token(self) -> str:
        # google-auth refreshes only when the current token is close to
        # expiry, so this is a cheap call on all but the first use.
        if not self._credentials.valid:
            self._credentials.refresh(google_requests.Request())  # type: ignore[no-untyped-call]
        token = self._credentials.token
        if not isinstance(token, str):  # pragma: no cover - defensive
            raise SheetsError("Google returned no access token")
        return token

    async def _call(
        self, client: httpx.AsyncClient, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        response = await client.request(
            method,
            f"{SHEETS_API}/{self._spreadsheet_id}{path}",
            headers={"Authorization": f"Bearer {self._token()}"},
            **kwargs,
        )
        if response.is_error:
            # The body names the tab or the id that was wrong, which is the
            # whole diagnostic value; it carries no credential.
            raise SheetsError(f"Sheets API {response.status_code}: {response.text[:300]}")
        body: dict[str, Any] = response.json()
        return body

    async def _ensure_sheets(self, client: httpx.AsyncClient) -> None:
        """Create and lay out the two sheets, once.

        Cheap after the first call in a process: a flag rather than a request,
        because this runs before every single lead.
        """
        if self._ready:
            return
        sheets = await self.worksheet_ids(client)
        if DATA_SHEET not in sheets:
            sheets[DATA_SHEET] = await self._create_worksheet(
                client, DATA_SHEET, len(DATA_HEADER) + 2
            )
            await self._write_row(client, f"{DATA_SHEET}!A1", list(DATA_HEADER))
            await self._style(client, sheets[DATA_SHEET], DATA_HEADER)
            await self._hide(client, sheets[DATA_SHEET])
        if VIEW_SHEET not in sheets:
            sheets[VIEW_SHEET] = await self._create_worksheet(client, VIEW_SHEET, len(HEADER))
        # Both of these write formulas, and both run every start rather than
        # only on creation: a formula somebody cleared is a sheet that
        # silently shows nothing, with no way for the owner to tell why.
        await self._install_day_list(client)
        await self._install_view(client, sheets[VIEW_SHEET])
        self._ready = True

    async def worksheet_ids(self, client: httpx.AsyncClient) -> dict[str, int]:
        body = await self._call(
            client, "GET", "", params={"fields": "sheets.properties(sheetId,title)"}
        )
        return {
            sheet["properties"]["title"]: sheet["properties"]["sheetId"]
            for sheet in body.get("sheets", [])
            if "properties" in sheet
        }

    async def _create_worksheet(self, client: httpx.AsyncClient, title: str, columns: int) -> int:
        body = await self._call(
            client,
            "POST",
            ":batchUpdate",
            json={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": title,
                                "gridProperties": {"columnCount": columns},
                            }
                        }
                    }
                ]
            },
        )
        sheet_id: int = body["replies"][0]["addSheet"]["properties"]["sheetId"]
        logger.warning("sheets_created title=%s", title)
        return sheet_id

    async def _write_row(self, client: httpx.AsyncClient, cell: str, values: list[str]) -> None:
        await self._call(
            client,
            "PUT",
            f"/values/{cell}",
            params={"valueInputOption": "USER_ENTERED"},
            json={"values": [values]},
        )

    async def _install_day_list(self, client: httpx.AsyncClient) -> None:
        """The days that actually have somebody on them, newest first.

        A formula rather than a list we maintain: it cannot fall out of step
        with the rows, and a day nobody wrote in on never appears as an option
        that shows an empty table.
        """
        await self._write_row(
            client,
            f"{DATA_SHEET}!{_DAYS_COLUMN}1",
            [
                # Absolute references throughout. Appending a lead inserts a
                # row, and Sheets rewrites every relative reference pointing
                # past it -- a relative A2:A here silently became A5:A after
                # three leads, and the dropdown emptied.
                "=IFERROR(SORT(UNIQUE(FILTER("
                f"${_DATE_COLUMN}$2:${_DATE_COLUMN}, "
                f'${_DATE_COLUMN}$2:${_DATE_COLUMN}<>""))'
                ', 1, FALSE), "")'
            ],
        )

    async def _install_view(self, client: httpx.AsyncClient, gid: int) -> None:
        """The sheet the clinic opens: a date picker over a filtered table.

        Rewritten every start rather than only on creation, unlike the styling
        elsewhere in this module. These cells are the mechanism, not a
        preference -- a cleared formula is a sheet that silently shows nothing,
        and the owner has no way to know why.
        """
        newest = f"'{DATA_SHEET}'!${_DAYS_COLUMN}$1"
        await self._write_row(client, f"{VIEW_SHEET}!A1", ["Sana:", f'=IFERROR({newest}, "")'])
        await self._write_row(client, f"{VIEW_SHEET}!A{_VIEW_HEADER_ROW}", list(HEADER))
        await self._write_row(
            client,
            f"{VIEW_SHEET}!A{_VIEW_HEADER_ROW + 1}",
            [
                "=IFERROR(FILTER("
                f"'{DATA_SHEET}'!$B$2:${_DATA_LAST_COLUMN}, "
                f"'{DATA_SHEET}'!${_DATE_COLUMN}$2:${_DATE_COLUMN}={_PICKER_REF})"
                ', "Bu kunda hech kim yozmagan")'
            ],
        )
        try:
            await self._call(
                client,
                "POST",
                ":batchUpdate",
                json={"requests": _view_requests(gid)},
            )
        except SheetsError as exc:
            logger.warning("sheets_styling_skipped error=%s", exc)

    async def _style(self, client: httpx.AsyncClient, gid: int, header: Sequence[str]) -> None:
        try:
            await self._call(
                client, "POST", ":batchUpdate", json={"requests": _style_requests(gid, header)}
            )
        except SheetsError as exc:
            logger.warning("sheets_styling_skipped error=%s", exc)

    async def _hide(self, client: httpx.AsyncClient, gid: int) -> None:
        """Hide the raw sheet.

        Two visible lists of the same leads is a spreadsheet where somebody
        edits the wrong one, and edits to this one are the ones the view
        silently disagrees with.
        """
        await self._call(
            client,
            "POST",
            ":batchUpdate",
            json={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": gid, "hidden": True},
                            "fields": "hidden",
                        }
                    }
                ]
            },
        )

    async def append(self, client: httpx.AsyncClient, row: LeadRow, day: date) -> None:
        await self._call(
            client,
            "POST",
            f"/values/{DATA_SHEET}!A:{_DATA_LAST_COLUMN}:append",
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json={"values": [[f"{day:%Y-%m-%d}", *row.as_cells()]]},
        )

    async def update_row(self, client: httpx.AsyncClient, row_number: int, row: LeadRow) -> None:
        """Rewrite one row in place, for a patient already on today's list.

        Starts at column B so the date stays: it says which day this lead
        belongs to, and that is what the picker filters on.
        """
        await self._call(
            client,
            "PUT",
            f"/values/{DATA_SHEET}!B{row_number}:{_DATA_LAST_COLUMN}{row_number}",
            params={"valueInputOption": "RAW"},
            json={"values": [row.as_cells()]},
        )

    async def find_row(self, client: httpx.AsyncClient, phone: str, day: date) -> int | None:
        """The row holding this phone on this day, if any.

        Matched on both, because the same patient writing again next week is a
        separate lead on a separate day -- which is the whole point of being
        able to pick a date.
        """
        if not phone:
            return None
        body = await self._call(
            client,
            "GET",
            f"/values/{DATA_SHEET}!{_DATE_COLUMN}:{_PHONE_COLUMN}",
            params={"majorDimension": "ROWS"},
        )
        wanted = f"{day:%Y-%m-%d}"
        for index, values in enumerate(body.get("values") or [], start=1):
            cells = [*values, "", ""]
            if cells[0].strip() == wanted and cells[2].strip() == phone:
                return index
        return None

    async def upsert(self, row: LeadRow) -> None:
        """Put this lead on the list, once."""
        day = datetime.now(UTC).astimezone(CLINIC_TIMEZONE).date()
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            await self._ensure_sheets(client)
            existing = await self.find_row(client, row.phone or "", day)
            if existing is not None:
                await self.update_row(client, existing, row)
                return
            await self.append(client, row, day)


@lru_cache
def get_mirror() -> SheetsMirror | None:
    """The configured mirror, or None when the clinic has no sheet.

    Cached because building it parses the key and sets up a credentials
    object; the token inside refreshes itself.
    """
    settings = get_settings()
    if settings.google_sheets_spreadsheet_id is None:
        return None
    try:
        return SheetsMirror(settings)
    except SheetsError:
        logger.exception("sheets_disabled_bad_configuration")
        return None


async def mirror_lead(row: LeadRow) -> None:
    """Send one lead to the clinic's sheet, if there is one.

    Never raises. The patient has already been answered by the time this
    runs, and the row is already in the database — a spreadsheet that is
    briefly behind is not worth failing a job over, and a retry would
    duplicate the reply, not the row.
    """
    mirror = get_mirror()
    if mirror is None:
        return
    try:
        await mirror.upsert(row)
    except SheetsError as exc:
        logger.error("sheets_mirror_failed error=%s", exc)
    except Exception:
        logger.exception("sheets_mirror_failed")


# Openings, acknowledgements and bare numbers — the messages that say
# nothing about why the patient wrote. Matched as whole words against a
# stripped message, so "salom" is skipped and "salom, tishim og'riyapti" is
# not.
_SMALL_TALK = frozenset(
    {
        "salom",
        "assalom",
        "assalomu",
        "alaykum",
        "assalomu alaykum",
        "assalom alaykum",
        "ассалому алайкум",
        "салом",
        "здравствуйте",
        "привет",
        "hi",
        "hello",
        "ok",
        "ha",
        "yoq",
        "rahmat",
        "спасибо",
        "/start",
    }
)


def summarise_problem(patient_messages: Sequence[str]) -> str:
    """One line saying what this patient wrote in about.

    Their own words, not a generated summary. A summary would mean another
    model call per lead, and this deployment's daily allowance is better
    spent answering patients than describing them — and the patient's own
    "tishim og'riyapti, chap tomonda" is more useful to a clinic than
    anything a paraphrase would produce.

    The first message that is not a greeting or a phone number, because that
    is the one that says why they wrote.

    It was the longest such message until a real conversation showed why
    that fails: "ha, shu vaqt to'g'ri keladi" is longer than "implant
    qancha turadi?", so the clinic's column filled with the patient
    agreeing to a time instead of the reason they came. Confirmations and
    logistics are reliably wordier than the question underneath them.

    The first one cannot have that problem. Nothing has been offered yet
    when it arrives, so it cannot be agreement to anything -- it is the
    reason, every time. It sometimes costs a detail the patient added a
    message later ("chap tomonda, sovuqdan achishadi"), and that is the
    cheaper mistake: a column saying "implant qancha turadi?" is useful to
    whoever rings them, and one saying "ha, shu vaqt to'g'ri keladi" is not.
    """
    candidates = [
        text.strip()
        for text in patient_messages
        if text.strip()
        and text.strip().lower().strip("!?.,") not in _SMALL_TALK
        and not looks_like_a_phone_number(text)
    ]
    return candidates[0] if candidates else ""
