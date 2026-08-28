"""Mirroring leads into the clinic's own spreadsheet.

No test here talks to Google. The transport is an httpx.MockTransport, so
what is checked is what this code sends and how it behaves when Google says
no -- which is the part that decides whether a patient's reply survives a
broken sheet.
"""

import base64
import json
from datetime import date
from typing import Any

import httpx
import pytest

from app.services.sheets import (
    DAYS_AHEAD,
    DAYS_BACK,
    HEADER,
    MAX_COMMENT,
    LeadRow,
    SheetsError,
    SheetsMirror,
    _load_credentials,
    _style_requests,
    ensure_day_tabs,
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


# The tab a lead recorded today belongs on.
TAB = "2026-08-28"


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
        await mirror.ensure_header(client, TAB, 0)
        await mirror.append(client, TAB, LeadRow("N", "998901234567", "telegram", "tish"))

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
        await mirror.ensure_header(client, TAB, 0)

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
        found = await mirror.find_row(client, TAB, "998901234567")
        assert found == 2
        await mirror.update_row(
            client, TAB, found, LeadRow("Nodira", "998901234567", "telegram", "x")
        )

    assert written and written[0].startswith("PUT")


async def test_a_number_that_is_not_there_yet_is_not_matched() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"values": [["Telefon", "998900000000"]]})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await mirror.find_row(client, TAB, "998901234567") is None
        # A lead with no number at all must never match the header row.
        assert await mirror.find_row(client, TAB, "") is None


async def test_googles_refusal_is_raised_with_enough_to_act_on() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, text='{"error":{"message":"The caller does not have permission"}}'
        )

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SheetsError, match="403"):
            await mirror.ensure_header(client, TAB, 0)


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
    # Column C — the date pushed it one to the right.
    assert formats[0]["range"]["startColumnIndex"] == HEADER.index("Telefon")
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
            await mirror.ensure_header(client, TAB, 0)
        await mirror.append(client, TAB, LeadRow("N", "1", "telegram", "x"))

    assert "sheets_styling_skipped" in caplog.text
    assert calls[-1] == "POST"  # the row still went in


def test_styling_does_not_try_to_delete_columns_that_were_never_created() -> None:
    """A tab is created exactly as wide as the header, so there is nothing to
    delete. Asking anyway fails the whole batch — which is how a day's tab
    once ended up created but unformatted.
    """
    assert not [r for r in _style_requests(0) if "deleteDimension" in r]


# --- a tab per day ----------------------------------------------------------


async def test_a_day_with_no_tab_yet_gets_one_named_after_it() -> None:
    """The whole point of the change: the owner clicks the 29th and sees the
    people who wrote in on the 29th, instead of scrolling three weeks of a
    single list.
    """
    created: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and "fields=sheets.properties" in url:
            return httpx.Response(200, json={"sheets": []})
        if url.endswith(":batchUpdate"):
            body = json.loads(request.content)
            add = body["requests"][0].get("addSheet")
            if add is not None:
                created.append(add["properties"]["title"])
                return httpx.Response(
                    200, json={"replies": [{"addSheet": {"properties": {"sheetId": 7}}}]}
                )
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        title = await mirror.worksheet_for(client, date(2026, 8, 29))

    assert title == "2026-08-29"
    assert created == ["2026-08-29"]


async def test_a_day_that_already_has_a_tab_is_not_created_again() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(f"{request.method} {url}")
        if request.method == "GET" and "fields=sheets.properties" in url:
            return httpx.Response(
                200, json={"sheets": [{"properties": {"sheetId": 3, "title": "2026-08-29"}}]}
            )
        if request.method == "GET":
            return httpx.Response(200, json={"values": [list(HEADER)]})
        return httpx.Response(200, json={})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await mirror.worksheet_for(client, date(2026, 8, 29))

    assert not any(":batchUpdate" in call for call in calls)


async def test_a_busy_day_reads_the_tab_list_once() -> None:
    """A worker answering forty messages on a Tuesday should not ask Google
    forty times which tabs exist.
    """
    listings = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal listings
        url = str(request.url)
        if request.method == "GET" and "fields=sheets.properties" in url:
            listings += 1
            return httpx.Response(
                200, json={"sheets": [{"properties": {"sheetId": 3, "title": "2026-08-29"}}]}
            )
        if request.method == "GET":
            return httpx.Response(200, json={"values": [list(HEADER)]})
        return httpx.Response(200, json={})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(5):
            await mirror.worksheet_for(client, date(2026, 8, 29))

    assert listings == 1


async def test_tomorrow_is_a_different_tab() -> None:
    """The cache is keyed by date, so midnight is not something anybody has
    to remember to handle.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and "fields=sheets.properties" in url:
            return httpx.Response(
                200,
                json={
                    "sheets": [
                        {"properties": {"sheetId": 3, "title": "2026-08-29"}},
                        {"properties": {"sheetId": 4, "title": "2026-08-30"}},
                    ]
                },
            )
        if request.method == "GET":
            return httpx.Response(200, json={"values": [list(HEADER)]})
        return httpx.Response(200, json={})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        seen.append(await mirror.worksheet_for(client, date(2026, 8, 29)))
        seen.append(await mirror.worksheet_for(client, date(2026, 8, 30)))

    assert seen == ["2026-08-29", "2026-08-30"]


async def test_a_new_tab_is_created_only_as_wide_as_the_columns_it_needs() -> None:
    """Otherwise every day would arrive 26 columns wide and the styling pass
    would have to delete 21 of them each morning.
    """
    widths: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and "fields=sheets.properties" in url:
            return httpx.Response(200, json={"sheets": []})
        if url.endswith(":batchUpdate"):
            body = json.loads(request.content)
            add = body["requests"][0].get("addSheet")
            if add is not None:
                widths.append(add["properties"]["gridProperties"]["columnCount"])
                return httpx.Response(
                    200, json={"replies": [{"addSheet": {"properties": {"sheetId": 7}}}]}
                )
        return httpx.Response(200, json={})

    mirror, _ = _mirror(handler)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await mirror.worksheet_for(client, date(2026, 8, 29))

    assert widths == [len(HEADER)]


# --- keeping the dates on the tab strip -------------------------------------


async def test_the_days_around_today_are_created_so_there_are_dates_to_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A day only exists once somebody writes in on it, so the strip is full
    of holes where the quiet days were, and tomorrow is never on it at all.
    """
    created: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and "fields=sheets.properties" in url:
            return httpx.Response(200, json={"sheets": []})
        if url.endswith(":batchUpdate"):
            body = json.loads(request.content)
            add = body["requests"][0].get("addSheet")
            if add is not None:
                created.append(add["properties"]["title"])
                return httpx.Response(
                    200, json={"replies": [{"addSheet": {"properties": {"sheetId": 1}}}]}
                )
        return httpx.Response(200, json={})

    _install_mirror(monkeypatch, handler)

    made = await ensure_day_tabs(today=date(2026, 8, 28))

    assert made == DAYS_BACK + DAYS_AHEAD + 1
    # A week either side, and today itself.
    assert created[0] == "2026-08-21"
    assert "2026-08-28" in created
    assert created[-1] == "2026-09-04"


async def test_days_that_already_exist_are_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cron runs every night, so it must be cheap and idempotent."""
    titles = [f"2026-08-{day:02d}" for day in range(21, 32)] + [
        f"2026-09-{day:02d}" for day in range(1, 5)
    ]
    batches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal batches
        url = str(request.url)
        if request.method == "GET" and "fields=sheets.properties" in url:
            return httpx.Response(
                200,
                json={
                    "sheets": [
                        {"properties": {"sheetId": n, "title": title}}
                        for n, title in enumerate(titles)
                    ]
                },
            )
        if url.endswith(":batchUpdate"):
            batches += 1
        return httpx.Response(200, json={})

    _install_mirror(monkeypatch, handler)

    made = await ensure_day_tabs(today=date(2026, 8, 28))

    assert made == 0
    assert batches == 0


async def test_a_spreadsheet_that_refuses_never_takes_the_worker_down(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """This is housekeeping on a spreadsheet. A worker that dies of it stops
    answering patients, which is a far worse outcome than a missing tab.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Google is having a day")

    _install_mirror(monkeypatch, handler)

    with caplog.at_level("ERROR", logger="app"):
        made = await ensure_day_tabs(today=date(2026, 8, 28))

    assert made == 0
    assert "sheets_day_tabs_failed" in caplog.text


async def test_no_sheet_configured_is_nothing_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.sheets.get_mirror", lambda: None)

    assert await ensure_day_tabs(today=date(2026, 8, 28)) == 0
