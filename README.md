# Paprika Self-Service Kiosk

Touch-friendly ordering kiosk, cash register and queue screen for several sales
points (stores), with a Python/FastAPI backend, SQLite storage, server-side
price validation, idempotent order submission and an admin panel. One server
(normally in the cloud) serves every store: each store has its own registers,
kiosks, queue screen, cashiers, product changes, printer and order numbers.
Receipts are printed on each store's network thermal printer by the cash
register program in that store. There is no external cash-register,
restaurant-system or fiscal integration.

The menu is read from `tovar.xlsx` in the project root. The `logo.jpg` file in
the project root is served as the cafe logo. Each product gets a photo matching
its name from `static/images/products/` (rules in `PRODUCT_PHOTO_RULES` in
`app/main.py`); products without a matching rule fall back to the generic
category photos in `static/images/`. A photo uploaded by staff always takes
precedence. Product photos are openly licensed (CC0 / CC BY / CC BY-SA) and
credited in `static/images/products/CREDITS.md`; keep that file when
deploying. The generic category photos are from Unsplash and are governed by
the [Unsplash license](https://unsplash.com/license). None of these are
official product images of the cafe.

Strips and wings sold by weight are priced at 95 somoni per kilogram. In the
product window the customer taps a quick weight (0.5 / 1 / 1.5 / 2 kg, 1 kg is
preselected) or enters their own amount from 20 to 300 somoni on an on-screen
numeric keypad and sees the approximate weight. The server validates the limits
and recalculates the price.

The categories «Горячие напитки», «Прочие», «Прочее» and «Мороженое» from
`tovar.xlsx` are hidden from the kiosk menu (`EXCLUDED_CATEGORIES` in
`app/main.py`).

The customer screens are available in Russian, Tajik and English (switch
RU / TJ / EN in the header; texts are in `static/i18n.js`). After each finished
order the kiosk returns to Russian for the next customer. When an order is placed,
the kiosk plays a soft airport-style chime and says «Ваш заказ принят. Номер
заказа N» in the customer's language (`static/voice.js`). It prefers Microsoft's
natural neural voices available in Edge — «Светлана» for Russian, «Jenny» for
English (they need internet) — and otherwise the best installed voice; there is
no Tajik voice, so the Tajik phrase is read by the Russian voice. The queue
screen announces ready orders the same way. Product names and
ingredients come from the catalog and stay as entered; the admin panel and the
kitchen receipt are in Russian.

Order numbers shown to customers and printed for the kitchen run from 1 to 50
(`KIOSK_ORDER_NUMBER_MAX`) in each store and then start again from 1. Each order
also keeps a unique internal reference (`P-00051`) used in the admin panel,
reprints and CSV exports.

Kiosk orders are limited to 1000 somoni (`KIOSK_MAX_ORDER_TOTAL`). Above the
limit the cart asks the customer to order at the cash desk and the server
rejects the order.

Keep `tovar.xlsx` and `logo.jpg` in the project root when deploying. The server
validates the workbook during startup; a missing or invalid catalog disables
the menu and order endpoints rather than falling back to placeholder prices.

## Run

Requires Python 3.10 or newer.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# set KIOSK_OWNER_LOGIN and KIOSK_OWNER_PASSWORD (8+ characters) in .env
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --env-file .env
```

Open the admin panel at `http://<server>:8000/admin` and log in as the owner.
The first owner is created at startup from `KIOSK_OWNER_LOGIN` /
`KIOSK_OWNER_PASSWORD` when no owner exists yet.

In production run the server behind a reverse proxy with HTTPS (kiosks,
registers and the admin panel connect over the internet), keep `.env` and the
database directory access-controlled, and back up `KIOSK_DB_PATH`. Use a
dedicated kiosk account and the operating system's kiosk/assigned-access mode
on the kiosk devices.

## Stores, devices and the admin panel

The admin panel (`/admin`) is used by the **owner** and by **managers**:

- The owner sees every store, creates stores, and creates managers and other
  owners (**Управляющие**), giving each manager access to selected stores.
- A manager sees only their stores: devices, cashiers, products, printer,
  orders, reports and shifts. Access changes take effect immediately.

For each store:

- **Устройства** — registers, kiosks and queue screens. Each device gets a
  secret key and an activation link (`…/#activate=<key>`); open the link once on
  the device and it works for that store from then on. The key is shown only
  when the device is added or a new key is issued (**Новый ключ**); issuing a
  new key or disabling a device stops the old one. The list shows when each
  device was last online.
- **Сотрудники** — cashiers with a 4–6 digit PIN (unique within the store) and
  **shift access** (open/close shifts, X/Z reports).
- **Товары** — the shared menu from `tovar.xlsx` with the store's own changes:
  price, availability, photo, ingredients, priority, and products of its own.
  Pizzas, burgers, twisters and quesadillas start with a typical composition
  (`PRODUCT_INGREDIENT_RULES` in `app/main.py`). Products with a priority from
  1 to 999 are listed first. Images may be JPEG, PNG or WebP up to 5 MB.
- **Принтер** — the store's printer and kitchen receipt design (see below).
- **Заказы** — the latest 50 orders with a kitchen reprint.

**Отчёты** filter by store (or all stores for the owner), period, register,
shift and cashier: order count and total, paid / cash / card / unpaid,
discounts, refunds, average order, breakdowns by store, register, cashier and
product, and a CSV export. Unpaid kiosk orders count in the order total;
cancelled and refunded orders do not. **Смены** lists register shifts with
their Z report (or the current X report of an open shift) and links to the
sales of a shift.

A database from the single-store version is migrated automatically on start:
everything is moved to the store «Paprika» with the devices «Касса 1»,
«Киоск 1» and «Экран очереди» (issue their keys in the admin panel), order
numbering continues, and cashiers keep their shift access.

## Cash register (POS)

The register page is `https://<server>/pos`; in a store it runs in the Windows
program `PaprikaPOS.exe` (see `desktop/README.md`), which also prints the
store's receipts. The register is activated with its key; cashiers of that
store log in by PIN. Ten wrong PINs within five minutes block logins on that
register for a while.

- **Shift**: each register has its own shift — open with the cash in the
  drawer, X report at any time, close with the counted cash (Z report: sales by
  cash/card, discounts, returns, expected cash and the difference). Only
  cashiers with shift access see the **Смена** tab; a cashier without access
  can sell once a shift is open.
- **Sale**: categories, search, product tiles, quantities, weighted portions
  (weight or amount), discount in percent or somoni, eat-in/takeaway. Payment
  by cash (amount received and change) or card. The kitchen ticket and a
  customer receipt are printed.
- **Kiosk orders** of the store waiting for payment (refreshed every 15
  seconds); the cashier takes the payment or cancels the order.
- **Очередь**: orders being prepared; **Готово** shows them as ready on the
  queue screen.
- **Receipts**: the store's latest 15 receipts; reprint a receipt or make a
  full return; returns are counted in the current shift of the register.

The kiosk limit of 1000 somoni does not apply at the register. **Receipts are
not fiscal**: the register is not connected to a fiscal cash device (ККМ) or the
tax authority.

## Register without internet

The register keeps working when the connection to the server is lost:

- While online it keeps a copy of the store's menu, cashiers (their PIN hashes),
  printer and receipt settings and the current shift
  (`GET /api/pos/offline-data`), and a service worker (`/pos-sw.js`) keeps the
  register page, its scripts and product photos. The page then opens without
  internet too — after at least one online start on that computer.
- Without connection a cashier logs in by PIN (checked locally), may open a
  shift, and sells; each sale is saved on the computer, gets an offline number
  `О1`…`О50`, and its kitchen ticket and customer receipt are printed directly
  by `PaprikaPOS.exe` on the store printer. The header shows «Нет связи» and the
  number of sales not sent yet; they are listed on the **Чеки** tab.
- Every 10 seconds the register checks the connection; when it is back, the
  queue is sent to `POST /api/pos/offline-sync` with the register key. Shifts
  and sales keep the register's time (unless its clock is off by more than 10
  minutes ahead or 30 days back), go into reports, and are recorded once even if
  sent again. Offline sales are not shown on the queue screen; the admin order
  card shows their offline number.
- Paying kiosk orders, cancelling, returns, «Готово» on the queue tab, X/Z
  reports and closing the shift need the connection; the shift can be closed
  after all offline sales are sent.

## Printing

The server cannot reach printers in the stores' local networks, so documents
are put into the store's print queue. The cash register program
`PaprikaPOS.exe` in the store takes the jobs every 2 seconds with its register
key and sends them to the printer over the local network (raw TCP, usually
port 9100). At least one register program of the store must be running; jobs
that nobody prints within 15 minutes are marked as not printed instead of being
printed late, and a job taken by a program that stopped is offered again after
a minute.

The printer is configured per store in the admin panel (**Принтер**): IP
address in the store's network, port, line width (48 characters for most 80 mm
printers, 42 for 512-dot models), encoding/code page and timeout, and the
kitchen receipt design: header, order-number size, date/time, item list,
prices, total, customer comment, footer, number of copies, paper feed and cut.
A live preview shows a sample receipt; **Печать пробного чека** queues the
sample with the form's current values. The page shows which register programs
are printing and the latest print jobs with their result.

The printer must support ESC/POS over raw TCP. Reserve its IP address in the
store's router. CP866 usually corresponds to code page 17 and Windows-1251 to
46; check the printer manual. Characters the encoding cannot represent are
printed as `?`. Until a store's printer is saved in the panel, the environment
variables `KIOSK_PRINTER_HOST`, `KIOSK_PRINTER_PORT`, `KIOSK_PRINTER_TIMEOUT`,
`KIOSK_PRINTER_CODEPAGE` and `KIOSK_PRINTER_ENCODING` are used as defaults.

A failed print does not undo or recreate an order; staff can reprint an order
from the admin panel. An idempotent replay will not reprint.

## Queue screen

A customer-facing screen (a TV in the hall) shows the store's order numbers in
two columns, «Готовится» and «Готово» (with Tajik and English captions). It is
served on a separate port by the same server (`KIOSK_QUEUE_PORT`, default 8001;
`0` disables it) and activated with the key of a queue screen device; behind a
reverse proxy set `KIOSK_QUEUE_PUBLIC_URL` so the admin panel builds the right
activation link. Open it in full-screen mode (F11).

- A kiosk order appears as «Готовится» as soon as it is placed; a register sale
  appears there when it is sent to the kitchen.
- The cashier marks an order ready on the **Очередь** tab of the register
  (**Готово**); a mistaken tap is undone with **Вернуть**.
- A ready number is highlighted, announced with a chime and «Заказ номер N
  готов», and disappears from the screen after 5 minutes
  (`KIOSK_QUEUE_READY_SECONDS`). Browsers allow sound only after one tap on the
  screen («Включить звук»).
- Cancelled and refunded orders are removed from the screen; orders never
  marked ready drop off after 12 hours.

The queue port serves only the page, the logo and order numbers.

## Kiosk

The kiosk page is `https://<server>/`, activated with a kiosk key. On the order
confirmation screen the customer must choose «В зале» (dine in) or «С собой»
(takeaway); the choice is stored with the order, printed large on the kitchen
receipt (can be turned off in the receipt settings) and exported to CSV.

The confirmation screen accepts an optional comment of up to 100 characters,
typed on an on-screen keyboard (Russian/English layouts, plus Tajik letters in
the Tajik interface). Tajik letters are printed on the kitchen receipt as the
closest Russian letters (ғ→г, қ→к, ҳ→х, ҷ→ч, ӣ→и, ӯ→у) because receipt code
pages do not contain them. A comment is not part of the price calculation.

## API

Devices send their key in the `X-Device-Key` header; the admin panel uses
`Authorization: Bearer <session token>` from `POST /api/admin/login`.

- `GET /api/health` — process health.
- `GET /api/device` — the store and device of the key.
- `GET /api/menu`, `POST /api/orders` (kiosk) — the store's menu; orders with
  server-side prices, an optional 100-character comment and a UUID
  `Idempotency-Key`.
- `/api/pos/*` (register) — PIN login, shifts, sales, payments, returns, queue
  status.
- `GET /api/agent/print-jobs`, `POST /api/agent/print-jobs/{id}` (register
  program) — take the store's print jobs and report the result.
- `GET /api/queue` (queue screen port) — order numbers of the store.
- `/api/admin/login`, `/logout`, `/me`, `/users` — admin sessions and users.
- `/api/admin/stores`, `/api/admin/stores/{id}/devices|cashiers|products|printer|orders|print`
  — stores and their settings.
- `GET /api/admin/report`, `/report.csv`, `/report-filters`, `/shifts` —
  reports with `store_id`, `start_date`, `end_date`, `register_id`,
  `shift_id`, `cashier_id` filters.

Device keys and admin sessions are stored only as hashes. Orders are persisted
in SQLite at `KIOSK_DB_PATH`; use an access-controlled directory and include it
in the restaurant's backup/retention policy.

## Validation

Install test dependencies and run:

```powershell
python -m pip install pytest httpx
python -m pytest
```
