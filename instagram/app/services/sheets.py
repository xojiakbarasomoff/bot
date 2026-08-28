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
from datetime import UTC, date, datetime, timedelta
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

# No date and no time: the date is the tab's own name, and a clock column
# was answering a question nobody asked of a day's list. The name is what
# the owner is looking for when they open it.
HEADER = ("Ism familiya", "Telefon", "Manba", "Bemor haqida qisqacha")

# The A1 column letters for the shape above, stated once so no range in
# this module can drift from the header.
_LAST_COLUMN = "D"
# Where the phone lives, which is how a returning patient finds their row.
_PHONE_COLUMN = "B"

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

# How many rows a day's tab is created with.
_ROWS_PER_DAY = 200


def _rgb(value: str) -> dict[str, float]:
    raw = value.lstrip("#")
    return {
        "red": int(raw[0:2], 16) / 255,
        "green": int(raw[2:4], 16) / 255,
        "blue": int(raw[4:6], 16) / 255,
    }


def _style_requests(sheet_id: int) -> list[dict[str, Any]]:
    white = {"red": 1.0, "green": 1.0, "blue": 1.0}
    ink = _rgb("#1F2937")
    full = {"sheetId": sheet_id, "startColumnIndex": 0, "endColumnIndex": len(HEADER)}
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
                    "startColumnIndex": HEADER.index("Telefon"),
                    "endColumnIndex": HEADER.index("Telefon") + 1,
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
        # Tab titles already created this process, so a busy day does
        # not re-read the spreadsheet's tab list for every message.
        self._known_worksheets: set[str] = set()

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

    async def worksheet_for(self, client: httpx.AsyncClient, day: date) -> str:
        """The tab for one day, created and laid out if it is not there yet.

        A tab per day rather than one long list, because that is the question
        the owner actually asks: not "every lead we have ever had" but "who
        wrote in today". With a single sheet they were scrolling past three
        weeks of other people to find this morning; now the day is a click at
        the bottom of the window.

        Cached per title, so a worker answering forty messages on a Tuesday
        reads the tab list once rather than forty times. The cache is keyed
        by date, so tomorrow simply misses it.
        """
        title = f"{day:%Y-%m-%d}"
        if title in self._known_worksheets:
            return title
        sheets = await self.worksheet_ids(client)
        if title not in sheets:
            sheets[title] = await self._create_worksheet(client, title)
        await self.ensure_header(client, title, sheets[title])
        self._known_worksheets.add(title)
        return title

    async def worksheet_ids(self, client: httpx.AsyncClient) -> dict[str, int]:
        body = await self._call(
            client, "GET", "", params={"fields": "sheets.properties(sheetId,title)"}
        )
        return {
            sheet["properties"]["title"]: sheet["properties"]["sheetId"]
            for sheet in body.get("sheets", [])
            if "properties" in sheet
        }

    async def _create_worksheet(self, client: httpx.AsyncClient, title: str) -> int:
        """Add the day's tab at the end, so the tab strip reads in order."""
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
                                # Exactly as wide as the columns that carry
                                # something. A default tab is 26 columns, and
                                # the five with content then sit against a
                                # grey field three times their width; created
                                # at the right size, there is nothing to
                                # delete afterwards.
                                "gridProperties": {
                                    "columnCount": len(HEADER),
                                    # A busy clinic sees tens of people a
                                    # day, not a thousand. Left at the
                                    # default a year of tabs would be
                                    # millions of empty cells against the
                                    # spreadsheet's own limit.
                                    "rowCount": _ROWS_PER_DAY,
                                },
                            }
                        }
                    }
                ]
            },
        )
        sheet_id: int = body["replies"][0]["addSheet"]["properties"]["sheetId"]
        logger.warning("sheets_day_created title=%s", title)
        return sheet_id

    async def ensure_header(self, client: httpx.AsyncClient, worksheet: str, gid: int) -> None:
        """Write the column names, but only into an empty tab.

        Checked rather than assumed: rewriting row 1 on every deploy would
        undo an owner who renamed a column, and they renamed it because the
        sheet is theirs.
        """
        existing = await self._call(
            client,
            "GET",
            f"/values/{worksheet}!A1:{_LAST_COLUMN}1",
            params={"majorDimension": "ROWS"},
        )
        if existing.get("values"):
            return
        await self._call(
            client,
            "PUT",
            f"/values/{worksheet}!A1:{_LAST_COLUMN}1",
            params={"valueInputOption": "RAW"},
            json={"values": [list(HEADER)]},
        )
        await self._style_new_sheet(client, gid)

    async def _style_new_sheet(self, client: httpx.AsyncClient, gid: int) -> None:
        """Make the tab readable, once, at the moment it is created.

        A default Sheets tab is unlabelled columns of grey -- the owner opens
        it, cannot tell the phone from the source at a glance, and goes back
        to asking somebody. Since this sheet exists precisely because they
        will not use a dashboard, looking like one thing they will use is not
        decoration.

        Failures here are swallowed. A tab that works but looks plain is a
        far better outcome than a lead that was not written because styling
        it went wrong.
        """
        try:
            await self._call(
                client, "POST", ":batchUpdate", json={"requests": _style_requests(gid)}
            )
        except SheetsError as exc:
            logger.warning("sheets_styling_skipped error=%s", exc)

    async def append(self, client: httpx.AsyncClient, worksheet: str, row: LeadRow) -> None:
        await self._call(
            client,
            "POST",
            f"/values/{worksheet}!A:{_LAST_COLUMN}:append",
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
            json={"values": [row.as_cells()]},
        )

    async def update_row(
        self, client: httpx.AsyncClient, worksheet: str, row_number: int, row: LeadRow
    ) -> None:
        """Rewrite one row in place, for a patient already on this day's tab.

        A name or a phone usually arrives a few turns after the first
        message. Appending again would give the owner the same person twice,
        once without their number.

        """
        await self._call(
            client,
            "PUT",
            f"/values/{worksheet}!A{row_number}:{_LAST_COLUMN}{row_number}",
            params={"valueInputOption": "RAW"},
            json={"values": [row.as_cells()]},
        )

    async def find_row(self, client: httpx.AsyncClient, worksheet: str, key: str) -> int | None:
        """The 1-based row holding `key` in the phone column, if any.

        Only within this day's tab. A patient who writes again next week is a
        new line on next week's day, which is what "who wrote in on the 29th"
        means; matching across tabs would quietly move them off the day they
        actually wrote.

        Reading the column rather than remembering a row number, because the
        sheet is the owner's: they sort it, they insert a line, they delete
        somebody. A row number stored on our side would be pointing at a
        stranger by Thursday.
        """
        if not key:
            return None
        body = await self._call(
            client,
            "GET",
            f"/values/{worksheet}!{_PHONE_COLUMN}:{_PHONE_COLUMN}",
            params={"majorDimension": "COLUMNS"},
        )
        columns = body.get("values") or [[]]
        for index, value in enumerate(columns[0], start=1):
            if str(value).strip() == key:
                return index
        return None

    async def upsert(self, row: LeadRow) -> None:
        """Put this lead on today's tab, once."""
        day = datetime.now(UTC).astimezone(CLINIC_TIMEZONE).date()
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            worksheet = await self.worksheet_for(client, day)
            existing = await self.find_row(client, worksheet, row.phone or "")
            if existing is not None:
                await self.update_row(client, worksheet, existing, row)
                return
            await self.append(client, worksheet, row)


# How far either side of today the tab strip is kept populated. A week back
# covers "what came in last Tuesday" without scrolling into last month, and a
# week forward means the owner can open a day before anybody has written on
# it -- which is the point of having the dates there to click at all.
DAYS_BACK = 7
DAYS_AHEAD = 7


async def ensure_day_tabs(*, today: date | None = None) -> int:
    """Make sure the tab strip has the days around today on it.

    Without this a day only exists once somebody has written in on it, so
    the strip is full of holes and tomorrow is never there. Called from a
    daily cron; safe to call as often as you like, since worksheet_for only
    creates what is missing.

    Returns how many tabs it had to create, for the log line. Never raises:
    this is housekeeping on a spreadsheet, and it must not be able to take
    a worker down.
    """
    mirror = get_mirror()
    if mirror is None:
        return 0

    day = today or datetime.now(UTC).astimezone(CLINIC_TIMEZONE).date()
    created = 0
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            known = set(await mirror.worksheet_ids(client))
            for offset in range(-DAYS_BACK, DAYS_AHEAD + 1):
                title = f"{day + timedelta(days=offset):%Y-%m-%d}"
                if title in known:
                    continue
                await mirror.worksheet_for(client, day + timedelta(days=offset))
                created += 1
    except SheetsError as exc:
        logger.error("sheets_day_tabs_failed error=%s", exc)
    except Exception:
        logger.exception("sheets_day_tabs_failed")
    return created


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
