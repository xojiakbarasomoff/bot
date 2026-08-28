"""Mirroring leads into the clinic's own spreadsheet.

No test here talks to Google. The transport is an httpx.MockTransport, so
what is checked is what this code sends and how it behaves when Google says
no -- which is the part that decides whether a patient's reply survives a
broken sheet.
"""

import base64
import json
import re
from datetime import date
from typing import Any

import httpx
import pytest

from app.services.sheets import (
    DATA_HEADER,
    DATA_SHEET,
    HEADER,
    MAX_COMMENT,
    LeadRow,
    SheetsError,
    SheetsMirror,
    _load_credentials,
    _style_requests,
    _view_requests,
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
    )
    mirror = SheetsMirror.__new__(SheetsMirror)
    mirror._credentials = _FakeCredentials()  # type: ignore[assignment]
    mirror._spreadsheet_id = settings.google_sheets_spreadsheet_id  # type: ignore[assignment]
    mirror._known_worksheets = set()  # type: ignore[assignment]
    return mirror, []


def _install_mirror(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    """Point the module-level mirror and its HTTP client at a fake transport.

    The real AsyncClient is captured before the patch: building one inside
    the replacement would call the replacement again, forever.
    """
    real_client = httpx.AsyncClient
    mirror, _ = _mirror(handler)
    monkeypatch.setattr("app.services.sheets.get_mirror", lambda: mirror)
    monkeypatch.setattr(
        "app.services.sheets.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler)),
    )


# --- the row the clinic reads ----------------------------------------------


TODAY = date(2026, 8, 28)


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


def test_agreeing_to_a_time_is_not_the_reason_they_wrote() -> None:
    """From a real conversation: taking the longest message filled the
    clinic's column with "ha, shu vaqt to'g'ri keladi", because a
    confirmation is wordier than the question under it. The first
    substantive message cannot be agreement — nothing has been offered when
    it arrives.
    """
    said = [
        "Assalomu alaykum",
        "implant qancha turadi?",
        "qimmat ekan",
        "mayli, qabulga yozing",
        "ha, shu vaqt to'g'ri keladi",
    ]

    assert summarise_problem(said) == "implant qancha turadi?"


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


async def test_a_lead_is_written_to_the_hidden_sheet_with_its_date() -> None:
    """The date is what the picker filters on, so it goes in with the row
    rather than being worked out later.
    """
    written: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and ":append" in str(request.url):
            written.append(json.loads(request.content)["values"][0])
        return httpx.Response(200, json={})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await mirror.append(client, LeadRow("N", "998901234567", "telegram", "tish"), TODAY)

    assert written == [["2026-08-28", "N", "998901234567", "telegram", "tish"]]


async def test_the_same_patient_on_the_same_day_is_one_row() -> None:
    """Their name usually arrives a few turns after their number. Appending
    again would give the owner the same person twice.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "!A:C" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "values": [
                        ["Sana", "Ism familiya", "Telefon"],
                        ["2026-08-28", "", "998901234567"],
                    ]
                },
            )
        return httpx.Response(200, json={})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await mirror.find_row(client, "998901234567", TODAY) == 2


async def test_the_same_patient_on_a_different_day_is_a_new_row() -> None:
    """Somebody writing again next week is a separate lead on a separate day,
    which is the whole point of being able to pick a date.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "!A:C" in str(request.url):
            return httpx.Response(
                200,
                json={"values": [["Sana", "Ism", "Telefon"], ["2026-08-20", "", "998901234567"]]},
            )
        return httpx.Response(200, json={})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await mirror.find_row(client, "998901234567", TODAY) is None


async def test_a_lead_with_no_number_never_matches_an_existing_row() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"values": [["Sana", "Ism", "Telefon"]]})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await mirror.find_row(client, "", TODAY) is None


async def test_googles_refusal_is_raised_with_enough_to_act_on() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, text='{"error":{"message":"The caller does not have permission"}}'
        )

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SheetsError, match="403"):
            await mirror._install_view(client, 0)


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
    kinds = [next(iter(request)) for request in _style_requests(0, DATA_HEADER)]

    assert "updateSheetProperties" in kinds  # frozen header
    assert "addBanding" in kinds
    assert "setBasicFilter" in kinds
    assert kinds.count("updateDimensionProperties") == 1 + len(HEADER)  # row + columns


def test_the_phone_column_is_text_so_it_is_not_shown_as_9_989e_11() -> None:
    formats = [
        request["repeatCell"]
        for request in _style_requests(0, DATA_HEADER)
        if "repeatCell" in request and "numberFormat" in request["repeatCell"]["fields"]
    ]

    assert len(formats) == 1
    assert formats[0]["range"]["startColumnIndex"] == DATA_HEADER.index("Telefon")
    assert formats[0]["cell"]["userEnteredFormat"]["numberFormat"]["type"] == "TEXT"


def test_the_story_column_wraps_instead_of_running_under_the_next_one() -> None:
    wrapping = [
        request["repeatCell"]
        for request in _style_requests(0, DATA_HEADER)
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
            await mirror._install_view(client, 0)
        await mirror.append(client, LeadRow("N", "1", "telegram", "x"), TODAY)

    assert "sheets_styling_skipped" in caplog.text
    assert calls[-1] == "POST"  # the row still went in


def test_styling_does_not_try_to_delete_columns_that_were_never_created() -> None:
    """A tab is created exactly as wide as the header, so there is nothing to
    delete. Asking anyway fails the whole batch — which is how a day's tab
    once ended up created but unformatted.
    """
    assert not [r for r in _style_requests(0, DATA_HEADER) if "deleteDimension" in r]


# --- the sheet the clinic actually opens ------------------------------------


def test_the_picker_holds_its_date_as_text() -> None:
    """Left as an ordinary cell, choosing a day turns it into a real date
    value while the hidden sheet stores text, so the comparison matches
    nothing and the table just says nobody wrote in. Found live, not in
    review.
    """
    formats = [
        r["repeatCell"]
        for r in _view_requests(0)
        if "repeatCell" in r and "numberFormat" in r["repeatCell"]["fields"]
    ]

    assert len(formats) == 1
    assert formats[0]["range"]["startRowIndex"] == 0
    assert formats[0]["cell"]["userEnteredFormat"]["numberFormat"]["type"] == "TEXT"


def test_the_dropdown_offers_the_days_on_record() -> None:
    [validation] = [r["setDataValidation"] for r in _view_requests(0) if "setDataValidation" in r]

    condition = validation["rule"]["condition"]
    assert condition["type"] == "ONE_OF_RANGE"
    assert DATA_SHEET in condition["values"][0]["userEnteredValue"]
    assert validation["rule"]["showCustomUi"] is True


def test_the_dropdown_is_not_strict() -> None:
    """A day nobody wrote in on is a reasonable thing to type, and being
    refused for it is confusing.
    """
    [validation] = [r["setDataValidation"] for r in _view_requests(0) if "setDataValidation" in r]

    assert validation["rule"]["strict"] is False


def test_the_picker_and_the_header_stay_in_view() -> None:
    [frozen] = [
        r["updateSheetProperties"]
        for r in _view_requests(0)
        if "updateSheetProperties" in r and "frozenRowCount" in r["updateSheetProperties"]["fields"]
    ]

    rows = frozen["properties"]["gridProperties"]["frozenRowCount"]
    assert rows == 3  # the picker, the blank line, the header


async def test_the_view_formulas_are_rewritten_every_start() -> None:
    """A cleared formula is a sheet that silently shows nothing, with no way
    for the owner to tell why — so these are not treated like the styling,
    which is left alone once applied.
    """
    written: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and "/values/" in str(request.url):
            cell = str(request.url).split("/values/")[1].split("?")[0]
            written[cell] = str(json.loads(request.content)["values"][0][0])
        return httpx.Response(200, json={})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await mirror._install_view(client, 0)

    picker = next(v for k, v in written.items() if k.endswith("A1"))
    table = next(v for k, v in written.items() if k.endswith("A4"))
    assert picker == "Sana:"
    assert table.startswith("=IFERROR(FILTER(")
    assert DATA_SHEET in table


async def test_every_formula_reference_is_absolute() -> None:
    """Appending a lead inserts a row, and Sheets rewrites relative
    references pointing past it. Live, a relative A2:A silently became A5:A
    after three leads and the dropdown emptied.
    """
    written: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and "/values/" in str(request.url):
            written.append(str(json.loads(request.content)["values"][0][0]))
        return httpx.Response(200, json={})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await mirror._install_view(client, 0)
        await mirror._install_day_list(client)

    formulas = [value for value in written if value.startswith("=")]
    assert formulas
    for formula in formulas:
        # No bare column-and-row reference: every one carries its dollars.
        assert not re.search(r"(?<![$\w])[A-Z]\d", formula), formula
