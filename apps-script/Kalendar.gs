/**
 * "Kalendar" varag'ini boshqaradigan skript.
 *
 * Sana katakchasi manzil bilan emas, "TanlanganSana" nomi bilan topiladi:
 * dizayn o'zgarib, katakcha joyi ko'chsa ham skript ishlayveradi.
 *
 * Sanani tanlash uchun skript kerak emas — katakchaning o'zi ochiladigan
 * ro'yxat. Bu yerda faqat bitta ish qoldi: jadval ochilganda bugunga qaytish.
 */

var CELL = 'TanlanganSana';


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
