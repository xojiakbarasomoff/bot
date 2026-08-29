# -*- coding: utf-8 -*-
"""A calendar the admin uses inside the spreadsheet itself.

No web app, no deploy: one "Kalendar" tab holding a date cell, a month grid
that marks the days with bookings, and a list that follows the chosen day.
The data still lives once, in Qabullar -- this tab only looks at it.
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
    return {
        "red": int(h[0:2], 16) / 255,
        "green": int(h[2:4], 16) / 255,
        "blue": int(h[4:6], 16) / 255,
    }


def stage(name, requests):
    """One batch, named, so a refusal says which part failed.

    batchUpdate is atomic -- a single bad request would otherwise take the
    whole design with it, which is how formats have been lost here before.
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
meta = CLIENT.get(BASE, params={"fields": "sheets.properties"}).json()
tabs = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}

if TAB not in tabs:
    r = CLIENT.post(
        f"{BASE}:batchUpdate",
        json={"requests": [{"addSheet": {"properties": {
            "title": TAB, "index": 0,
            "gridProperties": {"rowCount": 200, "columnCount": 20, "frozenRowCount": 0},
        }}}]},
    )
    r.raise_for_status()
    SHEET_ID = r.json()["replies"][0]["addSheet"]["properties"]["sheetId"]
    print(f"  [OK] '{TAB}' varag'i yaratildi")
else:
    SHEET_ID = tabs[TAB]
    print(f"  [i] '{TAB}' varag'i bor, qayta quriladi")
    # Wipe it rather than patch it: a leftover cell from an older layout is
    # how a stale header ended up shifting a real patient's row once.
    stage("tozalash", [{"updateCells": {
        "range": {"sheetId": SHEET_ID},
        "fields": "userEnteredValue,userEnteredFormat,dataValidation",
    }}])

# ------------------------------------------------------------- formulas
MONTHS = ("Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun",
          "Iyul", "Avgust", "Sentyabr", "Oktyabr", "Noyabr", "Dekabr")
month_label = '=CHOOSE(MONTH($B$3),' + ",".join(f'"{m}"' for m in MONTHS) + ')&" "&YEAR($B$3)'

# The Monday before (or on) the first of the chosen month. WEEKDAY type 3
# counts Monday as 0, which is the week the clinic actually works.
GRID_ORIGIN = ("DATE(YEAR($B$3),MONTH($B$3),1)"
               "-WEEKDAY(DATE(YEAR($B$3),MONTH($B$3),1),3)")
# One formula for all 42 cells: ROW/COLUMN place each one itself.
grid_cell = f"={GRID_ORIGIN}+(ROW()-6)*7+(COLUMN()-10)"

# The list. Eight columns out of Qabullar, the day taken from B3, sorted by
# time -- the order the front desk works in.
patient_list = (
    '=IFERROR('
    'SORT('
    'FILTER({Qabullar!F2:F,Qabullar!C2:C,Qabullar!D2:D,Qabullar!G2:G,'
    'Qabullar!H2:H,Qabullar!I2:I,Qabullar!M2:M,Qabullar!K2:K},'
    'Qabullar!E2:E=$B$3,Qabullar!A2:A<>"")'
    ',1,TRUE)'
    ',"Ushbu sanada bemorlar qabuli mavjud emas")'
)

rows = {
    "A1": [["📅  QABULLAR KALENDARI"]],
    "A3": [["Sanani tanlang:"]],
    "B3": [["=TODAY()"]],
    "D3": [["Jami qabul:"]],
    "E3": [['=COUNTIF(Qabullar!E:E,$B$3)']],
    "F3": [["Tasdiqlangan:"]],
    "G3": [['=COUNTIFS(Qabullar!E:E,$B$3,Qabullar!M:M,"Tasdiqlandi")']],
    "J3": [[month_label]],
    "J4": [["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"]],
    "A5": [["Vaqt", "Bemor F.I.Sh", "Telefon", "Shifokor",
            "Mutaxassislik", "Xizmat", "Status", "Kanal"]],
    "A6": [[patient_list]],
    "J12": [["Qalin ko'k = qabul bor kun   •   Yashil = tanlangan kun"]],
}
grid_values = [[grid_cell] * 7 for _ in range(6)]

data = [{"range": f"{TAB}!{cell}", "values": values} for cell, values in rows.items()]
data.append({"range": f"{TAB}!J6:P11", "values": grid_values})

r = CLIENT.post(
    f"{BASE}/values:batchUpdate",
    json={"valueInputOption": "USER_ENTERED", "data": data},
)
print("  [OK] formulalar" if r.status_code == 200 else f"  [XATO] formulalar: {r.text[:300]}")


# -------------------------------------------------------------- styling
def span(r1, r2, c1, c2):
    return {"sheetId": SHEET_ID, "startRowIndex": r1, "endRowIndex": r2,
            "startColumnIndex": c1, "endColumnIndex": c2}


def fmt(rng, cell, fields):
    return {"repeatCell": {"range": rng, "cell": {"userEnteredFormat": cell}, "fields": fields}}


white = rgb("#FFFFFF")
requests = [
    # the whole sheet: no gridlines, plain white
    {"updateSheetProperties": {
        "properties": {"sheetId": SHEET_ID, "hidden": False,
                       "gridProperties": {"hideGridlines": True}},
        "fields": "hidden,gridProperties.hideGridlines"}},
    fmt(span(0, 200, 0, 20), {"backgroundColor": white}, "userEnteredFormat.backgroundColor"),

    # title
    {"mergeCells": {"range": span(0, 1, 0, 16), "mergeType": "MERGE_ALL"}},
    fmt(span(0, 1, 0, 16), {
        "backgroundColor": rgb("#1155CC"),
        "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE",
        "padding": {"left": 12},
        "textFormat": {"bold": True, "fontSize": 14, "foregroundColor": white},
    }, "userEnteredFormat"),

    # the date cell -- the one thing the admin touches
    fmt(span(2, 3, 1, 2), {
        "backgroundColor": rgb("#FFF2CC"),
        "numberFormat": {"type": "DATE", "pattern": "dd.MM.yyyy"},
        "horizontalAlignment": "CENTER",
        "borders": {side: {"style": "SOLID_MEDIUM", "color": rgb("#F1C232")}
                    for side in ("top", "bottom", "left", "right")},
        "textFormat": {"bold": True, "fontSize": 12},
    }, "userEnteredFormat"),
    fmt(span(2, 3, 0, 1), {"horizontalAlignment": "RIGHT",
                           "textFormat": {"bold": True}}, "userEnteredFormat"),
    {"setDataValidation": {
        "range": span(2, 3, 1, 2),
        "rule": {"condition": {"type": "DATE_IS_VALID"},
                 "inputMessage": "Sanani tanlang (ikki marta bosing)",
                 "strict": True, "showCustomUi": True}}},

    # the two counters
    fmt(span(2, 3, 3, 7), {"textFormat": {"bold": True},
                           "horizontalAlignment": "CENTER"}, "userEnteredFormat"),
    fmt(span(2, 3, 4, 5), {"backgroundColor": rgb("#CFE2F3")},
        "userEnteredFormat.backgroundColor"),
    fmt(span(2, 3, 6, 7), {"backgroundColor": rgb("#D9EAD3")},
        "userEnteredFormat.backgroundColor"),

    # month label + weekday strip
    {"mergeCells": {"range": span(2, 3, 9, 16), "mergeType": "MERGE_ALL"}},
    fmt(span(2, 3, 9, 16), {
        "backgroundColor": rgb("#1155CC"), "horizontalAlignment": "CENTER",
        "textFormat": {"bold": True, "fontSize": 11, "foregroundColor": white},
    }, "userEnteredFormat"),
    fmt(span(3, 4, 9, 16), {
        "backgroundColor": rgb("#CFE2F3"), "horizontalAlignment": "CENTER",
        "textFormat": {"bold": True, "fontSize": 9},
    }, "userEnteredFormat"),

    # the grid: day numbers only, big enough to read at a glance
    fmt(span(5, 11, 9, 16), {
        "numberFormat": {"type": "DATE", "pattern": "d"},
        "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
        "textFormat": {"fontSize": 11},
        "borders": {side: {"style": "SOLID", "color": rgb("#E0E0E0")}
                    for side in ("top", "bottom", "left", "right")},
    }, "userEnteredFormat"),
    fmt(span(11, 12, 9, 16), {"textFormat": {"fontSize": 8,
                                             "foregroundColor": rgb("#666666")}},
        "userEnteredFormat"),

    # list header
    fmt(span(4, 5, 0, 8), {
        "backgroundColor": rgb("#1155CC"), "horizontalAlignment": "CENTER",
        "textFormat": {"bold": True, "fontSize": 10, "foregroundColor": white},
    }, "userEnteredFormat"),
    # the list body, formatted well past the last booking so a row added
    # tomorrow is already dressed when it appears
    fmt(span(5, 200, 0, 1), {
        "numberFormat": {"type": "DATE_TIME", "pattern": "HH:mm"},
        "horizontalAlignment": "CENTER",
        "textFormat": {"bold": True},
    }, "userEnteredFormat"),
    fmt(span(5, 200, 6, 7), {"horizontalAlignment": "CENTER"}, "userEnteredFormat"),

    {"updateSheetProperties": {
        "properties": {"sheetId": SHEET_ID,
                       "gridProperties": {"frozenRowCount": 5}},
        "fields": "gridProperties.frozenRowCount"}},
]

widths = [(0, 1, 70), (1, 2, 190), (2, 3, 130), (3, 4, 150), (4, 5, 130),
          (5, 6, 150), (6, 7, 110), (7, 8, 90), (8, 9, 30)]
for c1, c2, px in widths:
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": SHEET_ID, "dimension": "COLUMNS",
                  "startIndex": c1, "endIndex": c2},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}})
requests.append({"updateDimensionProperties": {
    "range": {"sheetId": SHEET_ID, "dimension": "COLUMNS",
              "startIndex": 9, "endIndex": 16},
    "properties": {"pixelSize": 46}, "fields": "pixelSize"}})
requests.append({"updateDimensionProperties": {
    "range": {"sheetId": SHEET_ID, "dimension": "ROWS",
              "startIndex": 5, "endIndex": 11},
    "properties": {"pixelSize": 34}, "fields": "pixelSize"}})
requests.append({"updateDimensionProperties": {
    "range": {"sheetId": SHEET_ID, "dimension": "ROWS",
              "startIndex": 0, "endIndex": 1},
    "properties": {"pixelSize": 40}, "fields": "pixelSize"}})

stage("dizayn", requests)

# --------------------------------------------------- conditional formats
# Order is load-bearing: Sheets applies the first rule that matches, so the
# chosen day must be tested before "this day has bookings".
GRID = span(5, 11, 9, 16)
cf = [
    ("=J6=$B$3", rgb("#34A853"), {"bold": True, "foregroundColor": white}),
    ('=COUNTIF(INDIRECT("Qabullar!$E:$E"),J6)>0', rgb("#CFE2F3"),
     {"bold": True, "foregroundColor": rgb("#1155CC")}),
    ("=MONTH(J6)<>MONTH($B$3)", white, {"foregroundColor": rgb("#CCCCCC")}),
]
existing = CLIENT.get(
    BASE, params={"fields": "sheets(properties.sheetId,conditionalFormats)"}
).json()
requests = []
for s in existing.get("sheets", []):
    if s["properties"]["sheetId"] != SHEET_ID:
        continue
    for i in range(len(s.get("conditionalFormats", [])) - 1, -1, -1):
        requests.append({"deleteConditionalFormatRule": {"sheetId": SHEET_ID, "index": i}})
offset = len(requests)
for index, (formula, background, text) in enumerate(cf):
    requests.append({"addConditionalFormatRule": {"index": offset + index, "rule": {
        "ranges": [GRID],
        "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA",
                          "values": [{"userEnteredValue": formula}]},
            "format": {"backgroundColor": background, "textFormat": text},
        }}}})
# and one on the list: a cancelled booking should not read like a live one
requests.append({"addConditionalFormatRule": {"index": offset + len(cf), "rule": {
    "ranges": [span(5, 200, 0, 8)],
    "booleanRule": {
        "condition": {"type": "CUSTOM_FORMULA",
                      "values": [{"userEnteredValue": '=$G6="Bekor qilindi"'}]},
        "format": {"backgroundColor": rgb("#F4CCCC"),
                   "textFormat": {"strikethrough": True}},
    }}}})
requests.append({"addConditionalFormatRule": {"index": offset + len(cf) + 1, "rule": {
    "ranges": [span(5, 200, 0, 8)],
    "booleanRule": {
        "condition": {"type": "CUSTOM_FORMULA",
                      "values": [{"userEnteredValue": '=$G6="Tasdiqlandi"'}]},
        "format": {"backgroundColor": rgb("#D9EAD3")},
    }}}})
stage("rang qoidalari", requests)

# ------------------------------------------------------------- verify
check = CLIENT.get(
    f"{BASE}/values/{TAB}!A1:P20",
    params={"valueRenderOption": "FORMATTED_VALUE"},
).json()
print("\n--- Kalendar (ko'rinishi) ---")
for line in check.get("values", []):
    print(" | ".join(str(cell) for cell in line[:10]))
