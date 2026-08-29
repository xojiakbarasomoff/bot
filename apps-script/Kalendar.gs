/**
 * "Kalendar" varag'ini boshqaradigan skript.
 *
 * Sana katakchasi manzil bilan emas, "TanlanganSana" nomi bilan topiladi:
 * dizayn o'zgarib, katakcha joyi ko'chsa ham skript ishlayveradi.
 */

var TAB = 'Kalendar';
var CELL = 'TanlanganSana';

// Kalendar to'ri: J..P ustunlar (10..16), 9-qatordan 14-gacha.
var GRID = {top: 9, bottom: 14, left: 10, right: 16};


/**
 * Kalendardan kunni bosib tanlash.
 *
 * Bosilgan katakchadagi sana nomlangan katakchaga ko'chiriladi. Ro'yxat,
 * hisoblagichlar va ranglar o'shanga bog'langan, shuning uchun hammasi
 * o'zi yangilanadi.
 */
function onSelectionChange(e) {
  var range = e.range;
  if (range.getSheet().getName() !== TAB) return;

  var row = range.getRow();
  var col = range.getColumn();
  if (row < GRID.top || row > GRID.bottom) return;
  if (col < GRID.left || col > GRID.right) return;

  var day = range.getValue();
  if (!(day instanceof Date)) return;

  var target = SpreadsheetApp.getActive().getRangeByName(CELL);
  if (!target) return;

  // Bir xil kunni qayta yozmaymiz: har bir yozuv butun varaqni qayta
  // hisoblaydi va bosish sekinlashadi.
  var current = target.getValue();
  if (current instanceof Date && current.getTime() === day.getTime()) return;

  target.setValue(day);
}


/**
 * Jadval ochilganda kalendar bugungi kunga qaytadi.
 *
 * Vaqtsiz sana yoziladi: Qabul_Sanasi ustuni ham toza sana, vaqti bor sana
 * esa unga hech qachon teng chiqmaydi va ro'yxat bo'sh ko'rinib qolardi.
 */
function onOpen() {
  var target = SpreadsheetApp.getActive().getRangeByName(CELL);
  if (!target) return;
  var now = new Date();
  target.setValue(new Date(now.getFullYear(), now.getMonth(), now.getDate()));
}
