/**
 * Klinika — Qabullar kalendari (Google Apps Script Web App).
 *
 * The sheet stays exactly as it is: one "Qabullar" tab, every booking in it,
 * in the order they arrived. Splitting it per day would scatter the data and
 * break every formula on the Dashboard. The filtering belongs here, in the
 * app, where it costs nothing and can change without touching the archive.
 */

var SHEET_NAME = 'Qabullar';

// Cheap protection against a receptionist clicking through a month at speed:
// the whole tab is read once and reused for a few seconds.
var CACHE_SECONDS = 20;
var CACHE_KEY = 'qabullar_rows_v1';

/** The web app itself. */
function doGet() {
  return HtmlService.createTemplateFromFile('Index')
    .evaluate()
    .setTitle('Klinika — Qabullar')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

/** So Index.html can pull in css.html and js.html. */
function include(filename) {
  return HtmlService.createHtmlOutputFromFile(filename).getContent();
}

/* ------------------------------------------------------------------ API */

/**
 * Every booking whose Qabul_Sanasi is exactly this day.
 *
 * @param {string} dateString  "YYYY-MM-DD", as the calendar emits it.
 * @return {{ok: boolean, date: string, count: number,
 *           appointments: Object[], error: (string|undefined)}}
 */
function getAppointmentsByDate(dateString) {
  try {
    var wanted = normaliseDate_(dateString);
    if (!wanted) {
      throw new Error('Sana formati notanish: ' + dateString);
    }

    var rows = readRows_();
    var found = [];
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].Qabul_Sanasi === wanted) {
        found.push(rows[i]);
      }
    }

    // Earliest slot first — the order the front desk actually works in.
    found.sort(function (a, b) {
      return String(a.Qabul_Vaqti).localeCompare(String(b.Qabul_Vaqti));
    });

    return {ok: true, date: wanted, count: found.length, appointments: found};
  } catch (err) {
    // The client shows this text, so it has to read like a sentence and not
    // like a stack trace.
    return {
      ok: false,
      date: String(dateString || ''),
      count: 0,
      appointments: [],
      error: err && err.message ? err.message : String(err)
    };
  }
}

/**
 * How many bookings each day of a month holds, so the calendar can mark the
 * busy days before anything is clicked.
 *
 * @param {number} year   e.g. 2026
 * @param {number} month  1-12
 * @return {{ok: boolean, counts: Object, error: (string|undefined)}}
 */
function getMonthSummary(year, month) {
  try {
    var prefix = pad_(year, 4) + '-' + pad_(month, 2) + '-';
    var rows = readRows_();
    var counts = {};
    for (var i = 0; i < rows.length; i++) {
      var day = rows[i].Qabul_Sanasi;
      if (day && day.indexOf(prefix) === 0) {
        counts[day] = (counts[day] || 0) + 1;
      }
    }
    return {ok: true, counts: counts};
  } catch (err) {
    return {ok: false, counts: {}, error: err && err.message ? err.message : String(err)};
  }
}

/** Drop the cache, so "Yangilash" really re-reads the sheet. */
function refreshCache() {
  CacheService.getScriptCache().remove(CACHE_KEY);
  return true;
}

/* -------------------------------------------------------------- reading */

/**
 * The whole tab as plain objects keyed by the header row.
 *
 * Header-driven rather than by column letter: a column inserted in the sheet
 * tomorrow must not silently start showing phone numbers under "Shifokor".
 */
function readRows_() {
  var cache = CacheService.getScriptCache();
  var cached = cache.get(CACHE_KEY);
  if (cached) {
    try {
      return JSON.parse(cached);
    } catch (ignored) {
      cache.remove(CACHE_KEY);   // a truncated entry is worse than none
    }
  }

  var sheet = SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
  if (!sheet) {
    throw new Error('"' + SHEET_NAME + '" nomli varaq topilmadi.');
  }

  var lastRow = sheet.getLastRow();
  var lastColumn = sheet.getLastColumn();
  if (lastRow < 2 || lastColumn < 1) {
    return [];
  }

  var tz = SpreadsheetApp.getActive().getSpreadsheetTimeZone();
  var values = sheet.getRange(1, 1, lastRow, lastColumn).getValues();
  var header = values[0].map(function (cell) {
    return String(cell).trim();
  });

  var rows = [];
  for (var r = 1; r < values.length; r++) {
    var raw = values[r];
    if (isBlankRow_(raw)) {
      continue;
    }
    var row = {_row: r + 1};
    for (var c = 0; c < header.length; c++) {
      var key = header[c];
      if (!key) {
        continue;
      }
      row[key] = formatCell_(key, raw[c], tz);
    }
    rows.push(row);
  }

  // CacheService refuses anything over 100KB. Once the clinic has a few
  // thousand bookings the cache simply stops applying — which costs a second,
  // where a thrown exception would cost the whole page.
  var payload = JSON.stringify(rows);
  if (payload.length < 95000) {
    try {
      cache.put(CACHE_KEY, payload, CACHE_SECONDS);
    } catch (ignored) {}
  }
  return rows;
}

function isBlankRow_(raw) {
  for (var i = 0; i < raw.length; i++) {
    if (String(raw[i]).trim() !== '') {
      return false;
    }
  }
  return true;
}

/**
 * One cell, in the shape the client can compare and print.
 *
 * The date and time columns are real date/time values in the sheet, so they
 * arrive here as Date objects — never as the "2026-09-02" the eye sees. They
 * are normalised to ISO strings so a plain string compare is enough on both
 * sides of the wire.
 */
function formatCell_(key, value, tz) {
  if (key === 'Qabul_Sanasi') {
    return normaliseDate_(value, tz);
  }
  if (key === 'Qabul_Vaqti') {
    return normaliseTime_(value, tz);
  }
  if (key === 'Yozilgan_Vaqti') {
    if (value instanceof Date) {
      return Utilities.formatDate(value, tz, 'yyyy-MM-dd HH:mm');
    }
    return String(value == null ? '' : value).trim();
  }
  return String(value == null ? '' : value).trim();
}

/**
 * Anything that means a day, as "YYYY-MM-DD".
 *
 * Accepts what the calendar sends ("2026-09-02"), what a date-formatted cell
 * returns (a Date), what a hand-typed cell holds ("02.09.2026") and what an
 * unformatted cell holds (a serial counted from 1899-12-30). Anything else
 * is "" — an unparsable date must never quietly match today.
 */
function normaliseDate_(value, tz) {
  if (value === null || value === undefined || value === '') {
    return '';
  }
  tz = tz || SpreadsheetApp.getActive().getSpreadsheetTimeZone();

  if (value instanceof Date) {
    return isNaN(value.getTime()) ? '' : Utilities.formatDate(value, tz, 'yyyy-MM-dd');
  }

  if (typeof value === 'number') {
    var epoch = new Date(1899, 11, 30);
    epoch.setDate(epoch.getDate() + Math.floor(value));
    return Utilities.formatDate(epoch, tz, 'yyyy-MM-dd');
  }

  var text = String(value).trim();
  if (!text) {
    return '';
  }

  var iso = text.match(/^(\d{4})[-\/.](\d{1,2})[-\/.](\d{1,2})/);
  if (iso) {
    return pad_(iso[1], 4) + '-' + pad_(iso[2], 2) + '-' + pad_(iso[3], 2);
  }

  var dmy = text.match(/^(\d{1,2})[-\/.](\d{1,2})[-\/.](\d{4})$/);
  if (dmy) {
    return pad_(dmy[3], 4) + '-' + pad_(dmy[2], 2) + '-' + pad_(dmy[1], 2);
  }

  return '';
}

/** Anything that means a clock time, as "HH:mm". */
function normaliseTime_(value, tz) {
  if (value === null || value === undefined || value === '') {
    return '';
  }
  tz = tz || SpreadsheetApp.getActive().getSpreadsheetTimeZone();

  if (value instanceof Date) {
    return isNaN(value.getTime()) ? '' : Utilities.formatDate(value, tz, 'HH:mm');
  }

  // A time-formatted cell read raw is a fraction of a day: 0.5 is 12:00.
  if (typeof value === 'number') {
    var minutes = Math.round(value * 24 * 60);
    return pad_(Math.floor(minutes / 60) % 24, 2) + ':' + pad_(minutes % 60, 2);
  }

  var match = String(value).trim().match(/^(\d{1,2})[:.](\d{2})/);
  return match ? pad_(match[1], 2) + ':' + match[2] : String(value).trim();
}

function pad_(value, width) {
  var text = String(value);
  while (text.length < width) {
    text = '0' + text;
  }
  return text;
}
