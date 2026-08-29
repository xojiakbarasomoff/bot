# Qabullar kalendari

Ma'lumot bitta joyda — `Qabullar` varag'ida, kelish tartibida. Sana bo'yicha
bo'lish varaqlarda emas, ustida turgan interfeysda bo'ladi.

Ikkita variant bor. **Birinchisi ishlab turibdi va hech qanday deploy talab
qilmaydi.**

---

## 1. `Kalendar` varag'i — jadval ichida (FAOL)

Google Sheet'ni ochasiz, birinchi varaq — `Kalendar`. Eng tepada sana turadi:
bosasiz, ro'yxat ochiladi, kunni tanlaysiz — pastda o'sha kundagi bemorlar.

| Element | Joyi | Nima qiladi |
|---|---|---|
| Sana | `A2` (`TanlanganSana`) | Ochiladigan ro'yxat — hamma narsa shunga bog'langan |
| Hisoblagichlar | `D2`, `F2` | O'sha kundagi jami / tasdiqlangan |
| Bemorlar ro'yxati | `A5` | `FILTER` + `SORT`, vaqt bo'yicha |
| Ro'yxat manbasi | `R` (yashirin) | Bir hafta oldin — bir oy keyin + band kunlar |

Statuslar rangda: **yashil** tasdiqlangan, **sariq** kutilmoqda, **qizil**
bekor qilingan (qatori xiralashib chiziladi), **ko'k** yakunlangan.

Kun bo'sh bo'lsa: *"Ushbu sanada bemorlar qabuli mavjud emas"*.

### Fayllar

- `Kalendar.gs` — jadvalga ulangan skript (`onOpen`: ochilganda bugunga qaytadi).
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
