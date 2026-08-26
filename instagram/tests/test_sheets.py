"""Mirroring leads into the clinic's own spreadsheet.

No test here talks to Google. The transport is an httpx.MockTransport, so
what is checked is what this code sends and how it behaves when Google says
no -- which is the part that decides whether a patient's reply survives a
broken sheet.
"""

import base64
import json
from typing import Any

import httpx
import pytest

from app.services.sheets import (
    HEADER,
    MAX_COMMENT,
    LeadRow,
    SheetsError,
    SheetsMirror,
    _load_credentials,
    _style_requests,
    mirror_lead,
    summarise_problem,
)
from tests.conftest import isolated_settings

# A structurally valid key with a real (throwaway) RSA private key would be
# needed to build Credentials, so the credential path is tested through
# _load_credentials' failure modes instead, and the request-shaping tests
# drive SheetsMirror with its credentials replaced.
NOT_A_KEY = json.dumps({"type": "service_account", "project_id": "p"})


class _FakeCredentials:
    valid = True
    token = "test-token"

    def refresh(self, request: Any) -> None:  # pragma: no cover - never called
        raise AssertionError("a valid credential should not be refreshed")


def _mirror(handler: Any) -> tuple[SheetsMirror, list[httpx.Request]]:
    """A mirror whose credentials are fake and whose transport is recorded."""
    settings = isolated_settings(
        google_service_account_json=NOT_A_KEY,
        google_sheets_spreadsheet_id="sheet-1",
        google_sheets_worksheet="Lidlar",
    )
    mirror = SheetsMirror.__new__(SheetsMirror)
    mirror._credentials = _FakeCredentials()  # type: ignore[assignment]
    mirror._spreadsheet_id = settings.google_sheets_spreadsheet_id  # type: ignore[assignment]
    mirror._worksheet = settings.google_sheets_worksheet
    return mirror, []


# --- the row the clinic reads ----------------------------------------------


def test_the_columns_are_the_ones_the_clinic_asked_for() -> None:
    row = LeadRow(name="Nodira Karimova", phone="998901234567", source="telegram", comment="tish")

    assert row.as_cells() == ["Nodira Karimova", "998901234567", "telegram", "tish"]
    assert HEADER == ("Ism familiya", "Telefon", "Manba", "Bemor haqida qisqacha")


def test_a_missing_name_or_number_is_an_empty_cell_not_the_word_none() -> None:
    """A lead usually arrives before the name does. "None" in a spreadsheet
    column reads as a person called None.
    """
    assert LeadRow(name=None, phone=None, source="instagram", comment=None).as_cells() == [
        "",
        "",
        "instagram",
        "",
    ]


def test_a_long_story_is_trimmed_so_the_column_stays_readable() -> None:
    row = LeadRow(name=None, phone="1", source="telegram", comment="a" * 500)

    cell = row.as_cells()[3]

    assert len(cell) == MAX_COMMENT
    assert cell.endswith("…")


def test_newlines_are_flattened_so_one_lead_stays_one_row() -> None:
    row = LeadRow(name=None, phone="1", source="telegram", comment="tishim\nog'riyapti")

    assert row.as_cells()[3] == "tishim og'riyapti"


# --- what the comment says --------------------------------------------------


def test_the_comment_is_what_the_patient_actually_wrote() -> None:
    """Their own words rather than a generated summary: another model call
    per lead is an allowance better spent answering patients, and "chap
    tomonda tishim og'riyapti" is more use to a clinic than a paraphrase.
    """
    said = ["Salom", "chap tomonda tishim og'riyapti, sovuqdan achishadi", "ok"]

    assert summarise_problem(said) == "chap tomonda tishim og'riyapti, sovuqdan achishadi"


@pytest.mark.parametrize(
    "said",
    [
        ["Salom", "assalomu alaykum", "Здравствуйте", "/start", "ok", "rahmat"],
        ["+998 90 123 45 67"],
        [],
        ["   "],
    ],
)
def test_greetings_and_bare_numbers_say_nothing_about_the_problem(said: list[str]) -> None:
    assert summarise_problem(said) == ""


def test_a_greeting_with_a_question_attached_is_not_small_talk() -> None:
    assert summarise_problem(["salom, implant qancha turadi?"]) == "salom, implant qancha turadi?"


# --- what is sent to Google -------------------------------------------------


async def test_a_new_lead_is_appended_under_a_header_written_once() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET" and "!A1:D1" in str(request.url):
            return httpx.Response(200, json={})  # empty tab
        if request.method == "GET":
            return httpx.Response(200, json={"values": [[]]})
        return httpx.Response(200, json={})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await mirror.ensure_header(client)
        await mirror.append(client, LeadRow("N", "998901234567", "telegram", "tish"))

    methods = [method for method, _ in seen]
    # GET the header row, PUT it, GET the sheet ids, POST the styling,
    # POST the row itself.
    assert methods[:2] == ["GET", "PUT"]
    assert methods[-1] == "POST"


async def test_a_tab_that_already_has_a_header_is_left_alone() -> None:
    """An owner who renamed a column renamed it on purpose."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={"values": [["Ism", "Tel", "Manba", "Izoh"]]})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await mirror.ensure_header(client)

    assert calls == ["GET"]


async def test_a_number_already_in_the_sheet_updates_that_row(monkeypatch: Any) -> None:
    """The name arrives a few turns after the number. Appending again would
    give the owner the same person twice, once without their name.
    """
    written: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and "!A1:D1" in url:
            return httpx.Response(200, json={"values": [list(HEADER)]})
        if request.method == "GET" and "!B:B" in url:
            return httpx.Response(200, json={"values": [["Telefon", "998901234567"]]})
        written.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await mirror.find_row(client, "998901234567")
        assert found == 2
        await mirror.update_row(client, found, LeadRow("Nodira", "998901234567", "telegram", "x"))

    assert written and written[0].startswith("PUT")


async def test_a_number_that_is_not_there_yet_is_not_matched() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"values": [["Telefon", "998900000000"]]})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await mirror.find_row(client, "998901234567") is None
        # A lead with no number at all must never match the header row.
        assert await mirror.find_row(client, "") is None


async def test_googles_refusal_is_raised_with_enough_to_act_on() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, text='{"error":{"message":"The caller does not have permission"}}'
        )

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SheetsError, match="403"):
            await mirror.ensure_header(client)


# --- never at the patient's expense -----------------------------------------


async def test_a_broken_sheet_never_reaches_the_patient(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The reply has already gone out and the row is already in the
    database. A spreadsheet that is briefly behind costs nothing; a job that
    fails here would be retried and send the reply twice.
    """

    class _Exploding:
        async def upsert(self, row: LeadRow) -> None:
            raise SheetsError("Sheets API 404: no such tab")

    monkeypatch.setattr("app.services.sheets.get_mirror", lambda: _Exploding())

    with caplog.at_level("ERROR", logger="app"):
        await mirror_lead(LeadRow("N", "1", "telegram", "x"))

    assert "sheets_mirror_failed" in caplog.text


async def test_no_sheet_configured_is_silence_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.sheets.get_mirror", lambda: None)

    await mirror_lead(LeadRow("N", "1", "telegram", "x"))


# --- the key ----------------------------------------------------------------


def test_a_base64_key_is_accepted_because_a_dotenv_cannot_hold_newlines() -> None:
    """private_key contains newlines. Railway can carry those in a variable
    and a line-by-line .env cannot, so both spellings are accepted rather
    than making it work on one host and not the other.
    """
    encoded = base64.b64encode(NOT_A_KEY.encode()).decode()

    with pytest.raises(SheetsError, match="Not a usable service account key"):
        _load_credentials(encoded)  # decoded fine, then rejected for its contents


@pytest.mark.parametrize("raw", ["not json at all", "!!!!", "{oops}"])
def test_a_key_that_is_neither_json_nor_base64_says_so(raw: str) -> None:
    with pytest.raises(SheetsError):
        _load_credentials(raw)


# --- how a new tab is laid out ----------------------------------------------


def test_a_new_tab_is_made_readable_rather_than_left_as_grey_columns() -> None:
    """This sheet exists because the owner will not open a dashboard, so
    looking like something they will read is not decoration.
    """
    kinds = [next(iter(request)) for request in _style_requests(0)]

    assert "updateSheetProperties" in kinds  # frozen header
    assert "addBanding" in kinds
    assert "setBasicFilter" in kinds
    assert kinds.count("updateDimensionProperties") == 1 + len(HEADER)  # row + columns


def test_the_phone_column_is_text_so_it_is_not_shown_as_9_989e_11() -> None:
    formats = [
        request["repeatCell"]
        for request in _style_requests(0)
        if "repeatCell" in request and "numberFormat" in request["repeatCell"]["fields"]
    ]

    assert len(formats) == 1
    assert formats[0]["range"]["startColumnIndex"] == 1
    assert formats[0]["cell"]["userEnteredFormat"]["numberFormat"]["type"] == "TEXT"


def test_the_story_column_wraps_instead_of_running_under_the_next_one() -> None:
    wrapping = [
        request["repeatCell"]
        for request in _style_requests(0)
        if "repeatCell" in request
        and request["repeatCell"]["cell"]["userEnteredFormat"].get("wrapStrategy") == "WRAP"
    ]

    assert wrapping


async def test_styling_a_tab_never_costs_the_clinic_a_lead(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tab that works but looks plain is a far better outcome than a lead
    that was not written because the decoration failed.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET" and "!A1:D1" in str(request.url):
            return httpx.Response(200, json={})
        if request.method == "GET":
            return httpx.Response(
                200, json={"sheets": [{"properties": {"sheetId": 0, "title": "Lidlar"}}]}
            )
        if str(request.url).endswith(":batchUpdate"):
            return httpx.Response(500, text="styling exploded")
        return httpx.Response(200, json={})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with caplog.at_level("WARNING", logger="app"):
            await mirror.ensure_header(client)
        await mirror.append(client, LeadRow("N", "1", "telegram", "x"))

    assert "sheets_styling_skipped" in caplog.text
    assert calls[-1] == "POST"  # the row still went in
