# -*- coding: utf-8 -*-
"""Build the "Kalendar" tab: a day picker and that day's patients.

One control at the very top -- a dropdown of days -- and the list of that
day's bookings underneath it. Nothing else: no month grid, no masthead, no
empty rows above the thing the clinic actually touches.

Design notes:

* Restraint. One accent (#1A73E8) and four greys. Colour is spent on the two
  things worth reading: the chosen day and a booking's status.
* Structure without boxes. No filled header bars -- a rule under the header
  and hairlines between rows carry the table instead.
* The date lives in a named range, not a cell address, so the layout can move
  again without breaking the bound script that writes into it.
"""
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
LINE = "#E8EAED"     # rules
HAIR = "#F1F3F4"     # row hairlines
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


def text(size=10, color=INK, bold=False):
    return {"fontFamily": FONT, "fontSize": size, "bold": bold,
            "foregroundColor": rgb(color)}


def chip(color, bold=True, strikethrough=False):
    """A conditional format carries no font or size -- only weight, slant,
    strikethrough and the two colours. Anything else is refused outright."""
    return {"bold": bold, "strikethrough": strikethrough,
            "foregroundColor": rgb(color)}


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
# new ones -- greying rows that are not grey and painting bands that are gone.
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
DAYS = ("Yakshanba", "Dushanba", "Seshanba", "Chorshanba",
        "Payshanba", "Juma", "Shanba")
weekday_name = "=CHOOSE(WEEKDAY($A$2)," + ",".join(f'"{d}"' for d in DAYS) + ")"

# Eight columns out of Qabullar, filtered to the chosen day, earliest first.
patient_list = (
    '=IFERROR(SORT(FILTER('
    '{Qabullar!F2:F,Qabullar!C2:C,Qabullar!D2:D,Qabullar!G2:G,'
    'Qabullar!H2:H,Qabullar!I2:I,Qabullar!M2:M,Qabullar!K2:K},'
    'Qabullar!E2:E=$A$2,Qabullar!A2:A<>"")'
    ',1,TRUE),"Ushbu sanada bemorlar qabuli mavjud emas")'
)

values = {
    "A1": [["TANLANGAN SANA"]],
    "A2": [["=TODAY()"]],
    "C2": [[weekday_name]],
    "D1": [["JAMI QABUL"]],
    "D2": [['=COUNTIF(Qabullar!E:E,$A$2)']],
    "F1": [["TASDIQLANDI"]],
    "F2": [['=COUNTIFS(Qabullar!E:E,$A$2,Qabullar!M:M,"Tasdiqlandi")']],
    "A4": [["VAQT", "BEMOR", "TELEFON", "SHIFOKOR",
            "MUTAXASSISLIK", "XIZMAT", "STATUS", "KANAL"]],
    "A5": [[patient_list]],
    # R is hidden -- it exists only to be the dropdown's source. A week back
    # to a month ahead, plus every day that already has a booking.
    "R1": [["_sanalar"]],
    "R2": [["=IFERROR(SORT(UNIQUE({TODAY()+SEQUENCE(38,1,-7);"
            'FILTER(Qabullar!E2:E,Qabullar!E2:E<>"")})),"")']],
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
HAIRLINE = {"bottom": {"style": "SOLID", "color": rgb(HAIR)}}

requests = [
    {"updateSheetProperties": {
        "properties": {"sheetId": SHEET_ID, "gridProperties": {"hideGridlines": True}},
        "fields": "gridProperties.hideGridlines"}},

    # a clean white ground; everything below adds to this, nothing fights it
    fmt(span(0, 200, 0, 20),
        {"backgroundColor": rgb(WHITE), "textFormat": text(10, MUTED),
         "verticalAlignment": "MIDDLE", "horizontalAlignment": "LEFT"}),

    # the three labels sit on the baseline above their values
    fmt(span(0, 1, 0, 1), LABEL),
    fmt(span(0, 1, 3, 4), LABEL),
    fmt(span(0, 1, 5, 6), LABEL),

    # the date -- the only control on the sheet, and the first thing on it
    {"mergeCells": {"range": span(1, 2, 0, 2), "mergeType": "MERGE_ALL"}},
    fmt(span(1, 2, 0, 2), {
        "backgroundColor": rgb(ACCENT_BG),
        "numberFormat": {"type": "DATE", "pattern": "dd.MM.yyyy"},
        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
        "textFormat": text(13, ACCENT, bold=True),
        "borders": {side: {"style": "SOLID", "color": rgb(ACCENT_LINE)}
                    for side in ("top", "bottom", "left", "right")},
    }),
    # A list of days, not a free date field: clicking it should offer the
    # days, not open another calendar. strict is off so a day outside the
    # window is still allowed rather than refused.
    {"setDataValidation": {"range": span(1, 2, 0, 2), "rule": {
        "condition": {"type": "ONE_OF_RANGE",
                      "values": [{"userEnteredValue": f"={TAB}!$R$2:$R$200"}]},
        "inputMessage": "Sanani ro'yxatdan tanlang",
        "strict": False, "showCustomUi": True}}},
    fmt(span(1, 2, 2, 3), {"textFormat": text(9, FAINT), "padding": {"left": 8}}),

    # the two counters
    fmt(span(1, 2, 3, 4), {"textFormat": text(16, INK, bold=True)}),
    fmt(span(1, 2, 5, 6), {"textFormat": text(16, GREEN, bold=True)}),

    # list header: no fill, one rule underneath -- a bar of colour here would
    # compete with the status chips, which are the only colour worth reading
    fmt(span(3, 4, 0, 8), {
        "textFormat": text(8, FAINT, bold=True), "verticalAlignment": "BOTTOM",
        "borders": {"bottom": {"style": "SOLID_MEDIUM", "color": rgb(LINE)}},
    }),

    # list body, dressed far past the last booking so tomorrow's row is
    # already formatted when it appears
    fmt(span(4, 200, 0, 8),
        {"textFormat": text(10, MUTED), "verticalAlignment": "MIDDLE",
         "borders": HAIRLINE}),
    # LEFT is spelled out: a time is a number, and a number left to itself
    # right-aligns away from the column heading above it.
    fmt(span(4, 200, 0, 1),
        {"numberFormat": {"type": "DATE_TIME", "pattern": "HH:mm"},
         "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE",
         "textFormat": text(10, ACCENT, bold=True), "borders": HAIRLINE}),
    fmt(span(4, 200, 1, 2),
        {"textFormat": text(10, INK, bold=True), "borders": HAIRLINE}),
    fmt(span(4, 200, 6, 7),
        {"horizontalAlignment": "CENTER", "textFormat": text(9, MUTED),
         "borders": HAIRLINE}),

    # the dropdown shows its source cells as they are formatted
    fmt(span(1, 200, 17, 18),
        {"numberFormat": {"type": "DATE", "pattern": "dd.MM.yyyy"},
         "textFormat": text(10, MUTED)}),

    # the header stays put while a long day is scrolled
    {"updateSheetProperties": {
        "properties": {"sheetId": SHEET_ID, "gridProperties": {"frozenRowCount": 4}},
        "fields": "gridProperties.frozenRowCount"}},
]

# Rows: a tight label/value pair, one thin gap, then the table.
for start, end, px in [(0, 1, 18), (1, 2, 36), (2, 3, 14), (3, 200, 30)]:
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": SHEET_ID, "dimension": "ROWS",
                  "startIndex": start, "endIndex": end},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}})

# With the calendar gone the table has the width to itself.
for c1, c2, px in [(0, 1, 90), (1, 2, 200), (2, 3, 140), (3, 4, 170),
                   (4, 5, 140), (5, 6, 150), (6, 7, 120), (7, 8, 100)]:
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": SHEET_ID, "dimension": "COLUMNS",
                  "startIndex": c1, "endIndex": c2},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}})
requests.append({"updateDimensionProperties": {
    "range": {"sheetId": SHEET_ID, "dimension": "COLUMNS", "startIndex": 17, "endIndex": 18},
    "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})

stage("dizayn", requests)

# ------------------------------------------------------------ named range
existing = {n["name"]: n["namedRangeId"] for n in meta.get("namedRanges", [])}
named = [{"deleteNamedRange": {"namedRangeId": existing[NAMED]}}] if NAMED in existing else []
named.append({"addNamedRange": {"namedRange": {"name": NAMED, "range": span(1, 2, 0, 2)}}})
stage("nomlangan diapazon", named)

# ---------------------------------------------------- conditional formats
# Sheets applies the first matching rule, so a cancelled row is dimmed
# everywhere except its status cell, which keeps its own colour: two rules
# must not fight over the same cell.
STATUS = span(4, 200, 6, 7)
ROW_NO_STATUS = [span(4, 200, 0, 6), span(4, 200, 7, 8)]

rules = [
    ([STATUS], '=$G5="Tasdiqlandi"', GREEN_BG, chip(GREEN)),
    ([STATUS], '=$G5="Bekor qilindi"', RED_BG, chip(RED)),
    ([STATUS], '=$G5="Kutilmoqda"', AMBER_BG, chip(AMBER)),
    ([STATUS], '=$G5="Yakunlandi"', ACCENT_BG, chip(ACCENT)),
    (ROW_NO_STATUS, '=$G5="Bekor qilindi"', WHITE,
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
check = CLIENT.get(f"{BASE}/values/{TAB}!A1:H8",
                   params={"valueRenderOption": "FORMATTED_VALUE"}).json()
print("\n--- Kalendar ---")
for line in check.get("values", []):
    print(" | ".join(str(cell) for cell in line))
