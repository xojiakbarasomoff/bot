# Qabullar kalendari — Google Apps Script Web App

Bitta `Qabullar` varag'i o'zgarmaydi. Filtrlash serverda (`getAppointmentsByDate`),
ko'rsatish brauzerda — sahifa qayta yuklanmaydi.

## Fayllar

| Fayl | Vazifasi |
|---|---|
| `Code.gs` | `doGet`, `getAppointmentsByDate(dateString)`, `getMonthSummary(year, month)`, `refreshCache`, sana/vaqt normallashtirish |
| `Index.html` | Sahifa skeleti (Tailwind CDN) |
| `css.html` | Kalendar katakchalari va yuklanish animatsiyasi |
| `js.html` | Kalendar, `google.script.run`, holatlar (loading / empty / error) |
| `appsscript.json` | Manifest — vaqt mintaqasi `Asia/Tashkent` |

## O'rnatish (5 daqiqa)

1. Google Sheet (`Klinika — Lidlar`) ni oching → **Kengaytmalar → Apps Script**.
2. Chapdagi `+` orqali fayllarni yarating va shu papkadagi mazmunni ko'chiring:
   - `Code.gs` (mavjud `Code.gs` ustiga yozing)
   - `Index` → **HTML** fayl
   - `css` → **HTML** fayl
   - `js` → **HTML** fayl
   > Fayl nomlari aynan shunday bo'lishi shart — `include('css')` shu nomlarni chaqiradi.
3. ⚙️ **Loyiha sozlamalari** → *"appsscript.json manifest faylini ko'rsatish"* ni yoqing,
   `appsscript.json` mazmunini almashtiring (vaqt mintaqasi `Asia/Tashkent`).
4. **Deploy → New deployment → Web app**:
   - *Execute as*: **Me** (skript sizning nomingizdan varaqni o'qiydi)
   - *Who has access*: **Anyone with Google account** — bemor ma'lumotlari,
     shuning uchun `Anyone` (anonim) qilmang.
5. **Authorize access** → hisobingizni tanlang → *Advanced → Go to project (unsafe)* → **Allow**.
6. Chiqqan `https://script.google.com/macros/s/.../exec` havolasini administratorga bering.

## Kodni o'zgartirgandan keyin

**Deploy → Manage deployments → ✏️ → Version: New version → Deploy.**
Aks holda eski versiya ochilib turaveradi.

## Eslatmalar

- `Qabul_Sanasi` varaqda haqiqiy sana (Date) — server uni `YYYY-MM-DD` ga
  keltiradi, shuning uchun `02.09.2026`, `2026-09-02` va formatlanmagan
  seriya raqami ham bir xil ishlaydi.
- Ma'lumotlar 20 soniya keshlanadi; **Yangilash** tugmasi keshni tozalaydi.
