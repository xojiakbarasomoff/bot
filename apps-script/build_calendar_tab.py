# -*- coding: utf-8 -*-
"""Build the "Kalendar" tab: a day picker the admin uses inside the sheet.

Design notes, because a spreadsheet fights you on all three:

* Rhythm. Row height is shared across the whole row, so the calendar on the
  right and the patient list on the left cannot each have their own. Every
  row below the spacer is therefore the same 30px, and the layout is built
  to look deliberate at that height rather than fighting it.
* Restraint. One accent (#1A73E8) and four greys. Colour is reserved for
  the two things worth looking at: the chosen day and a booking's status.
* Structure without boxes. No filled header bars and no cell borders on the
  calendar -- hairline rules under the list rows carry the table instead.

The date lives in a named range, not a cell address, so this layout can move
again without breaking the bound script that writes into it.
"""
import json
import sys

import google.auth.transport.requests as gt
import httpx
from google.oauth2 import service_account

KEY = r"C:\Users\Xojiakbar\Downloads\endless-orb-417118-5f147678cb2f.json"
SID = "1GnX6KUhjYY085Hz1Vjyr7UQtOJM4vh6tZ5iTv-YRBJc"
BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{SID}"
TAB = "Kalendar"
NAMED = "TanlanganSana"

# --------------------------------------------------------------- palette
INK = "#202124"      # primary text
MUTED = "#5F6368"    # secondary text
FAINT = "#9AA0A6"    # labels
LINE = "#E8EAED"     # rules and separators
HAIR = "#F1F3F4"     # row hairlines / today's tint
ACCENT = "#1A73E8"
ACCENT_BG = "#E8F0FE"
ACCENT_LINE = "#D2E3FC"
GREEN, GREEN_BG = "#137333", "#E6F4EA"
RED, RED_BG = "#C5221F", "#FCE8E6"
AMBER, AMBER_BG = "#B06000", "#FEF7E0"
WHITE = "#FFFFFF"

FONT = "Roboto"

creds = service_account.Credentials.from_service_account_file(
    KEY, scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
creds.refresh(gt.Request())
CLIENT = httpx.Client(
    timeout=120,
    headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
)


def rgb(h):
    h = h.lstrip("#")
    return {"red": int(h[0:2], 16) / 255,
            "green": int(h[2:4], 16) / 255,
            "blue": int(h[4:6], 16) / 255}


def text(size=10, color=INK, bold=False, italic=False):
    return {"fontFamily": FONT, "fontSize": size, "bold": bold,
            "italic": italic, "foregroundColor": rgb(color)}


def stage(name, requests):
    """One batch, named, so a refusal says which part failed.

    batchUpdate is atomic: a single bad request would otherwise take the
    whole design down with it, which is how formats were lost here before.
    """
    if not requests:
        return True
    r = CLIENT.post(f"{BASE}:batchUpdate", json={"requests": requests})
    if r.status_code != 200:
        print(f"  [XATO] {name}: {r.json().get('error', {}).get('message', r.text)[:300]}")
        return False
    print(f"  [OK] {name}")
    return True


# ------------------------------------------------------------------ tab
# conditionalFormats must be asked for by name: leave it out of the mask and
# the old rules come back empty, survive the rebuild, and quietly fight the
# new ones -- greying days that are not grey and painting rows that are gone.
meta = CLIENT.get(BASE, params={
    "fields": "namedRanges,sheets(properties,conditionalFormats)"}).json()
tabs = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}

if TAB not in tabs:
    r = CLIENT.post(f"{BASE}:batchUpdate", json={"requests": [{"addSheet": {"properties": {
        "title": TAB, "index": 0,
        "gridProperties": {"rowCount": 200, "columnCount": 20},
    }}}]})
    r.raise_for_status()
    SHEET_ID = r.json()["replies"][0]["addSheet"]["properties"]["sheetId"]
    print(f"  [OK] '{TAB}' varag'i yaratildi")
else:
    SHEET_ID = tabs[TAB]
    reset = [
        {"updateCells": {"range": {"sheetId": SHEET_ID},
                         "fields": "userEnteredValue,userEnteredFormat,dataValidation"}},
        {"unmergeCells": {"range": {"sheetId": SHEET_ID}}},
    ]
    # Old rules and old merges would survive a value wipe and quietly fight
    # the new layout, so they go first.
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == SHEET_ID:
            for i in range(len(s.get("conditionalFormats", [])) - 1, -1, -1):
                reset.append({"deleteConditionalFormatRule": {"sheetId": SHEET_ID, "index": i}})
    stage("eskisini tozalash", reset)
    print(f"  [i] '{TAB}' qayta quriladi")


def span(r1, r2, c1, c2):
    return {"sheetId": SHEET_ID, "startRowIndex": r1, "endRowIndex": r2,
            "startColumnIndex": c1, "endColumnIndex": c2}


# ------------------------------------------------------------- formulas
MONTHS = ("Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun",
          "Iyul", "Avgust", "Sentyabr", "Oktyabr", "Noyabr", "Dekabr")
DAYS = ("Yakshanba", "Dushanba", "Seshanba", "Chorshanba",
        "Payshanba", "Juma", "Shanba")

month_label = "=CHOOSE(MONTH($A$6)," + ",".join(f'"{m}"' for m in MONTHS) + ')&" "&YEAR($A$6)'
weekday_name = "=CHOOSE(WEEKDAY($A$6)," + ",".join(f'"{d}"' for d in DAYS) + ")"

# The Monday on or before the first of the chosen month. WEEKDAY type 3
# counts Monday as 0, which is the week the clinic actually works.
ORIGIN = "DATE(YEAR($A$6),MONTH($A$6),1)-WEEKDAY(DATE(YEAR($A$6),MONTH($A$6),1),3)"
# One formula for all 42 cells: ROW and COLUMN place each one itself.
grid_cell = f"={ORIGIN}+(ROW()-9)*7+(COLUMN()-10)"

# Eight columns out of Qabullar, filtered to the chosen day, earliest first.
patient_list = (
    '=IFERROR(SORT(FILTER('
    '{Qabullar!F2:F,Qabullar!C2:C,Qabullar!D2:D,Qabullar!G2:G,'
    'Qabullar!H2:H,Qabullar!I2:I,Qabullar!M2:M,Qabullar!K2:K},'
    'Qabullar!E2:E=$A$6,Qabullar!A2:A<>"")'
    ',1,TRUE),"Ushbu sanada bemorlar qabuli mavjud emas")'
)

values = {
    "A2": [["Qabullar"]],
    "A3": [["Kunlik qabullar ro'yxati"]],
    "A5": [["TANLANGAN SANA"]],
    "A6": [["=TODAY()"]],
    "C6": [[weekday_name]],
    "D5": [["JAMI QABUL"]],
    "D6": [['=COUNTIF(Qabullar!E:E,$A$6)']],
    "F5": [["TASDIQLANDI"]],
    "F6": [['=COUNTIFS(Qabullar!E:E,$A$6,Qabullar!M:M,"Tasdiqlandi")']],
    "J7": [[month_label]],
    "J8": [["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"]],
    "J9:P14": [[grid_cell] * 7 for _ in range(6)],
    "J15": [["Ko'k — qabul bor kun.  Kunni bosing."]],
    "A8": [["VAQT", "BEMOR", "TELEFON", "SHIFOKOR",
            "MUTAXASSISLIK", "XIZMAT", "STATUS", "KANAL"]],
    "A9": [[patient_list]],
    # R is hidden -- it exists only to be the dropdown's source.
    "R1": [["_sanalar"]],
    "R2": [[
        # A week back to a month ahead, plus every day that already has a
        # booking. The window is there so the calendar can hand this cell an
        # empty day without Sheets flagging it as invalid data.
        "=IFERROR(SORT(UNIQUE({TODAY()+SEQUENCE(38,1,-7);"
        'FILTER(Qabullar!E2:E,Qabullar!E2:E<>"")})),"")'
    ]],
}
r = CLIENT.post(f"{BASE}/values:batchUpdate", json={
    "valueInputOption": "USER_ENTERED",
    "data": [{"range": f"{TAB}!{cell}", "values": rows} for cell, rows in values.items()],
})
print("  [OK] formulalar" if r.status_code == 200 else f"  [XATO] formulalar: {r.text[:300]}")


# --------------------------------------------------------------- layout
def fmt(rng, cell, fields="userEnteredFormat"):
    return {"repeatCell": {"range": rng, "cell": {"userEnteredFormat": cell}, "fields": fields}}


LABEL = {"textFormat": text(8, FAINT, bold=True), "verticalAlignment": "BOTTOM"}
requests = [
    {"updateSheetProperties": {
        "properties": {"sheetId": SHEET_ID, "gridProperties": {"hideGridlines": True}},
        "fields": "gridProperties.hideGridlines"}},

    # a clean white ground; everything below adds to this, nothing fights it
    fmt(span(0, 200, 0, 20),
        {"backgroundColor": rgb(WHITE), "textFormat": text(10, MUTED),
         "verticalAlignment": "MIDDLE", "horizontalAlignment": "LEFT"}),

    # masthead
    {"mergeCells": {"range": span(1, 2, 0, 6), "mergeType": "MERGE_ALL"}},
    fmt(span(1, 2, 0, 6), {"textFormat": text(16, INK, bold=True),
                           "verticalAlignment": "MIDDLE"}),
    {"mergeCells": {"range": span(2, 3, 0, 6), "mergeType": "MERGE_ALL"}},
    fmt(span(2, 3, 0, 6), {"textFormat": text(9, FAINT), "verticalAlignment": "TOP"}),

    # the three labels sit on the baseline above their values
    fmt(span(4, 5, 0, 1), LABEL),
    fmt(span(4, 5, 3, 4), LABEL),
    fmt(span(4, 5, 5, 6), LABEL),

    # the date -- the only cell anyone types into
    {"mergeCells": {"range": span(5, 6, 0, 2), "mergeType": "MERGE_ALL"}},
    fmt(span(5, 6, 0, 2), {
        "backgroundColor": rgb(ACCENT_BG),
        "numberFormat": {"type": "DATE", "pattern": "dd.MM.yyyy"},
        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
        "textFormat": text(13, ACCENT, bold=True),
        "borders": {side: {"style": "SOLID", "color": rgb(ACCENT_LINE)}
                    for side in ("top", "bottom", "left", "right")},
    }),
    # A list of the days that have patients, not a free date field. strict is
    # off on purpose: the calendar can still hand it a day nobody has booked
    # yet, and a strict rule would flag that as an error.
    {"setDataValidation": {"range": span(5, 6, 0, 2), "rule": {
        "condition": {"type": "ONE_OF_RANGE",
                      "values": [{"userEnteredValue": f"={TAB}!$R$2:$R$200"}]},
        "inputMessage": "Sanani ro'yxatdan tanlang yoki kalendardan bosing",
        "strict": False, "showCustomUi": True}}},

    # the dropdown shows the source cells as they are formatted
    fmt(span(1, 200, 17, 18),
        {"numberFormat": {"type": "DATE", "pattern": "dd.MM.yyyy"},
         "textFormat": text(10, MUTED)}),
    fmt(span(5, 6, 2, 3), {"textFormat": text(9, FAINT), "padding": {"left": 8}}),

    # the two counters
    fmt(span(5, 6, 3, 4), {"textFormat": text(16, INK, bold=True)}),
    fmt(span(5, 6, 5, 6), {"textFormat": text(16, GREEN, bold=True)}),

    # list header: no fill, one rule underneath -- a bar of colour here would
    # compete with the status chips, which are the only colour worth reading
    fmt(span(7, 8, 0, 8), {
        "textFormat": text(8, FAINT, bold=True), "verticalAlignment": "BOTTOM",
        "borders": {"bottom": {"style": "SOLID_MEDIUM", "color": rgb(LINE)}},
    }),

    # list body, dressed far past the last booking so tomorrow's row is
    # already formatted when it appears
    fmt(span(8, 200, 0, 8), {
        "textFormat": text(10, MUTED), "verticalAlignment": "MIDDLE",
        "borders": {"bottom": {"style": "SOLID", "color": rgb(HAIR)}},
    }),
    fmt(span(8, 200, 0, 1), {
        "numberFormat": {"type": "DATE_TIME", "pattern": "HH:mm"},
        "horizontalAlignment": "LEFT", "textFormat": text(10, ACCENT, bold=True),
        "borders": {"bottom": {"style": "SOLID", "color": rgb(HAIR)}}}),
    fmt(span(8, 200, 1, 2), {
        "textFormat": text(10, INK, bold=True),
        "borders": {"bottom": {"style": "SOLID", "color": rgb(HAIR)}}}),
    fmt(span(8, 200, 6, 7), {
        "horizontalAlignment": "CENTER", "textFormat": text(9, MUTED),
        "borders": {"bottom": {"style": "SOLID", "color": rgb(HAIR)}}}),

    # the calendar: month, weekdays, days, legend
    {"mergeCells": {"range": span(6, 7, 9, 16), "mergeType": "MERGE_ALL"}},
    fmt(span(6, 7, 9, 16), {"horizontalAlignment": "CENTER",
                            "textFormat": text(12, INK, bold=True)}),
    fmt(span(7, 8, 9, 16), {"horizontalAlignment": "CENTER",
                            "textFormat": text(8, FAINT, bold=True)}),
    fmt(span(8, 14, 9, 16), {
        "numberFormat": {"type": "DATE", "pattern": "d"},
        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
        "textFormat": text(11, INK)}),
    {"mergeCells": {"range": span(14, 15, 9, 16), "mergeType": "MERGE_ALL"}},
    fmt(span(14, 15, 9, 16), {"horizontalAlignment": "CENTER",
                             "textFormat": text(8, FAINT), "verticalAlignment": "TOP"}),
]

# One height for every row: the two halves share rows, so a rhythm is the
# only thing that keeps both readable.
requests.append({"updateDimensionProperties": {
    "range": {"sheetId": SHEET_ID, "dimension": "ROWS", "startIndex": 1, "endIndex": 200},
    "properties": {"pixelSize": 30}, "fields": "pixelSize"}})
requests.append({"updateDimensionProperties": {
    "range": {"sheetId": SHEET_ID, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
    "properties": {"pixelSize": 16}, "fields": "pixelSize"}})

# ~1230px in total, so a laptop shows the picker and the day's patients at once.
for c1, c2, px in [(0, 1, 100), (1, 2, 165), (2, 3, 115), (3, 4, 125), (4, 5, 105),
                   (5, 6, 115), (6, 7, 95), (7, 8, 80), (8, 9, 22)]:
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": SHEET_ID, "dimension": "COLUMNS", "startIndex": c1, "endIndex": c2},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}})
requests.append({"updateDimensionProperties": {
    "range": {"sheetId": SHEET_ID, "dimension": "COLUMNS", "startIndex": 9, "endIndex": 16},
    "properties": {"pixelSize": 44}, "fields": "pixelSize"}})
requests.append({"updateDimensionProperties": {
    "range": {"sheetId": SHEET_ID, "dimension": "COLUMNS", "startIndex": 17, "endIndex": 18},
    "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})

stage("dizayn", requests)

# ------------------------------------------------------------ named range
existing = {n["name"]: n["namedRangeId"] for n in meta.get("namedRanges", [])}
named = [{"deleteNamedRange": {"namedRangeId": existing[NAMED]}}] if NAMED in existing else []
named.append({"addNamedRange": {"namedRange": {
    "name": NAMED, "range": span(5, 6, 0, 2)}}})
stage("nomlangan diapazon", named)

# ---------------------------------------------------- conditional formats
# Sheets applies the first matching rule, so the order below is the design:
# the chosen day outranks everything, a neighbouring month is always muted,
# and only then does a day get marked as busy.
GRID = span(8, 14, 9, 16)
LIST = span(8, 200, 0, 8)
STATUS = span(8, 200, 6, 7)
# The cancelled row is dimmed everywhere except its status cell, which keeps
# its own colour -- two rules must not fight over the same cell.
ROW_NO_STATUS = [span(8, 200, 0, 6), span(8, 200, 7, 8)]




def chip(color, bold=True, strikethrough=False):
    """A conditional format carries no font or size -- only weight, slant,
    strikethrough and the two colours. Anything else is refused outright."""
    return {"bold": bold, "strikethrough": strikethrough,
            "foregroundColor": rgb(color)}


rules = [
    ([GRID], "=J9=$A$6", ACCENT, chip(WHITE)),
    ([GRID], "=MONTH(J9)<>MONTH($A$6)", WHITE, chip("#DADCE0", bold=False)),
    ([GRID], '=COUNTIF(INDIRECT("Qabullar!$E:$E"),J9)>0', WHITE, chip(ACCENT)),
    ([GRID], "=J9=TODAY()", HAIR, chip(INK, bold=False)),
    ([STATUS], '=$G9="Tasdiqlandi"', GREEN_BG, chip(GREEN)),
    ([STATUS], '=$G9="Bekor qilindi"', RED_BG, chip(RED)),
    ([STATUS], '=$G9="Kutilmoqda"', AMBER_BG, chip(AMBER)),
    ([STATUS], '=$G9="Yakunlandi"', ACCENT_BG, chip(ACCENT)),
    (ROW_NO_STATUS, '=$G9="Bekor qilindi"', WHITE,
     chip("#BDC1C6", bold=False, strikethrough=True)),
]
requests = []
for index, (ranges, formula, background, style) in enumerate(rules):
    requests.append({"addConditionalFormatRule": {"index": index, "rule": {
        "ranges": ranges,
        "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA",
                          "values": [{"userEnteredValue": formula}]},
            "format": {"backgroundColor": rgb(background), "textFormat": style},
        }}}})
stage("rang qoidalari", requests)

# ------------------------------------------------------------- verify
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
check = CLIENT.get(f"{BASE}/values/{TAB}!A1:P12",
                   params={"valueRenderOption": "FORMATTED_VALUE"}).json()
print("\n--- Kalendar ---")
for line in check.get("values", []):
    print(" | ".join(str(cell) for cell in line))
