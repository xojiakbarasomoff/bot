# Qabullar kalendari

Ma'lumot bitta joyda — `Qabullar` varag'ida, kelish tartibida. Sana bo'yicha
bo'lish varaqlarda emas, ustida turgan interfeysda bo'ladi.

Ikkita variant bor. **Birinchisi ishlab turibdi va hech qanday deploy talab
qilmaydi.**

---

## 1. `Kalendar` varag'i — jadval ichida (FAOL)

Google Sheet'ni ochasiz, birinchi varaq — `Kalendar`. Oylik kalendardan kunni
bosasiz, pastda o'sha kundagi bemorlar chiqadi.

| Element | Joyi | Nima qiladi |
|---|---|---|
| Sana katakchasi | `B3` | Hamma narsa shunga bog'langan |
| Hisoblagichlar | `E3`, `G3` | O'sha kundagi jami / tasdiqlangan |
| Oy nomi | `J3` | O'zbekcha, `B3` dagi oyga qarab |
| Kalendar to'ri | `J6:P11` | Bosilsa `B3` ga sana yoziladi |
| Bemorlar ro'yxati | `A6` | `FILTER` + `SORT`, vaqt bo'yicha |

Ranglar: **yashil** — tanlangan kun, **qalin ko'k** — o'sha kunda qabul bor,
kulrang — qo'shni oyning kunlari. Ro'yxatda yashil qator — tasdiqlangan,
qizil va chizilgan — bekor qilingan.

Kun bo'sh bo'lsa: *"Ushbu sanada bemorlar qabuli mavjud emas"*.

### Fayllar

- `Kalendar.gs` — jadvalga ulangan skript (`onSelectionChange` + `onOpen`).
  Sheet → **Kengaytmalar → Apps Script** ichida turibdi, nomi *Klinika kalendar*.
- `build_calendar_tab.py` — varaqni noldan quradigan skript. Layout buzilsa
  qayta ishga tushirilsa, `Kalendar` varag'i to'liq tiklanadi.

### Nima uchun formulalar, kod emas

`FILTER` sana o'zgarishi bilan **darhol** ishlaydi — skript chaqirilmaydi,
kutish yo'q, kvota sarflanmaydi. Skript faqat bitta ish qiladi: bosilgan
katakchadagi sanani `B3` ga ko'chiradi.

---

## 2. Web App — alohida sahifa (TAYYOR, lekin deploy qilinmagan)

Agar administrator jadvalni umuman ko'rmasin desangiz: `Code.gs`, `Index.html`,
`css.html`, `js.html` — Tailwind'li alohida veb-sahifa, o'sha `Qabullar`
varag'ini o'qiydi.

**O'rnatish:** Sheet → Kengaytmalar → Apps Script → fayllarni aynan shu nomlar
bilan yarating → **Deploy → New deployment → Web app** → *Execute as:* **Me**,
*Who has access:* **Anyone with Google account** (anonim qilmang — bemor
ma'lumotlari). Kod o'zgarsa: **Manage deployments → ✏️ → New version**.

> Diqqat: bu variant 1-variant bilan bitta Apps Script loyihasida turolmaydi —
> `Code.gs` nomi to'qnashadi. Kerak bo'lsa alohida loyiha yarating.

## Sana formati haqida

`Qabul_Sanasi` — haqiqiy sana (Date), ko'rinishi `2026-09-02` bo'lsa ham.
Shuning uchun:

- `onOpen` `B3` ga **vaqtsiz** sana yozadi — vaqti bor sana hech qachon teng
  chiqmaydi va ro'yxat bo'sh ko'rinardi;
- Web App tarafda `normaliseDate_()` `Date`, `"02.09.2026"` va seriya raqamini
  bir xil ISO satrga keltiradi.
