/**
 * Kalendarda kunni bosib tanlash.
 *
 * "Kalendar" varag'idagi J6:P11 katakchalari — sanalar. Foydalanuvchi
 * shulardan birini bossa, sana B3 ga yoziladi. Ro'yxat, hisoblagichlar va
 * ranglar B3 ga bog'langan, shuning uchun hammasi o'zi yangilanadi.
 */
function onSelectionChange(e) {
  var range = e.range;
  var sheet = range.getSheet();
  if (sheet.getName() !== 'Kalendar') return;

  var row = range.getRow();
  var col = range.getColumn();
  // J..P = 10..16, qatorlar 6..11. Boshqa katakchalarga tegilmaydi.
  if (row < 6 || row > 11 || col < 10 || col > 16) return;

  var day = range.getValue();
  if (!(day instanceof Date)) return;

  var target = sheet.getRange('B3');
  var current = target.getValue();
  // Bir xil kunni qayta yozmaymiz: har bir yozuv butun varaqni qayta
  // hisoblaydi va bosish sekinlashadi.
  if (current instanceof Date && current.getTime() === day.getTime()) return;

  target.setValue(day);
}


/**
 * Har safar jadval ochilganda kalendar bugungi kunga qaytadi.
 *
 * Vaqtsiz sana yoziladi: Qabul_Sanasi ustuni ham toza sana, vaqti bor sana
 * esa hech qachon unga teng chiqmaydi va ro'yxat bo'sh ko'rinib qoladi.
 */
function onOpen() {
  var sheet = SpreadsheetApp.getActive().getSheetByName('Kalendar');
  if (!sheet) return;
  var now = new Date();
  sheet.getRange('B3').setValue(new Date(now.getFullYear(), now.getMonth(), now.getDate()));
}
