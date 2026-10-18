# Paprika Self-Service Kiosk

Touch-friendly ordering kiosk with a Python/FastAPI backend, local SQLite order
storage, server-side price validation, idempotent order submission, a protected
staff panel, and direct printing to a network thermal printer. The application
operates independently and has no external cash-register or restaurant-system
integration. Orders are saved locally and printed; staff use the printed
number to fulfill them and collect payment.

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
order the kiosk returns to Russian for the next customer. Product names and
ingredients come from the catalog and stay as entered; the staff panel and the
kitchen receipt are in Russian.

Order numbers shown to customers and printed for the kitchen run from 1 to 50
(`KIOSK_ORDER_NUMBER_MAX`) and then start again from 1. Each order also keeps a
unique internal reference (`P-00051`) used in the staff panel, reprints and CSV
exports.

Kiosk orders are limited to 1000 somoni (`KIOSK_MAX_ORDER_TOTAL`). Above the
limit the cart asks the customer to order at the cash desk and the server
rejects the order.

Keep `tovar.xlsx` and `logo.jpg` in the project root when deploying. The server
validates the workbook during startup; a missing or invalid catalog disables
the menu and order endpoints rather than falling back to placeholder prices.

## Run locally

Requires Python 3.10 or newer.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
$env:KIOSK_ADMIN_TOKEN = "replace-with-a-long-random-staff-token"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --env-file .env
```

Open `http://127.0.0.1:8000`. Keep the kiosk on a trusted local network; do not
expose the admin endpoints or development server directly to the internet.

For production, put the application behind a managed service/reverse proxy,
use a dedicated kiosk account and operating-system kiosk/assigned-access mode,
provide HTTPS where traffic leaves the device, and store the staff token in the
service environment rather than in the browser.

## Printer

The kitchen printer is configured in the staff panel (**Для сотрудников** →
**Принтер и чек для кухни**): IP address, port, line width (48 characters for
most 80 mm printers, 42 for 512-dot models), encoding/code page and timeout.
The same section designs the kitchen receipt: header, order-number size,
date/time, item list (weights/amounts of weighted portions, options), prices,
total, customer comment, footer, number of copies, paper feed and cut. A live
preview shows a sample receipt; **Проверить связь** tests the TCP connection
and **Печать пробного чека** prints the sample with the form's current
(unsaved) values. **Сохранить** stores the settings in SQLite; they are used
for every new order and for reprints.

The printer must support ESC/POS over raw TCP (usually port 9100). Reserve its
IP address in the router. CP866 usually corresponds to code page 17 and
Windows-1251 to 46; check the printer manual. Characters the encoding cannot
represent are printed as `?`.

Until settings are saved in the panel, the environment variables
`KIOSK_PRINTER_HOST`, `KIOSK_PRINTER_PORT`, `KIOSK_PRINTER_TIMEOUT`,
`KIOSK_PRINTER_CODEPAGE` and `KIOSK_PRINTER_ENCODING` are used as defaults.

Most network printers do not report paper-out status over raw TCP. A failed
print does not undo or recreate an order; staff can reprint an existing order
from the panel. An idempotent replay will not reprint.

## Administrator report

Open **Для сотрудников** and enter `KIOSK_ADMIN_TOKEN`. The product section
lets staff add products and edit names, prices, categories, availability,
ingredients, and photos. Pizzas, burgers, twisters and quesadillas start with a
typical composition (`PRODUCT_INGREDIENT_RULES` in `app/main.py`); replace it
with the cafe's actual recipe in the panel. Ingredients are shown on the
product card and in the product window.

Each product can have a priority from 1 to 999: products with a priority are
listed first (1 at the top), both in every category and in «Всё меню»;
products without a priority follow in the workbook order. Images may be JPEG, PNG, or WebP up to 5 MB. Edits and new products
are saved in the local SQLite database and override/add to the Excel catalog;
they survive application restarts without modifying `tovar.xlsx`.

On the order confirmation screen the customer must choose «В зале» (dine in)
or «С собой» (takeaway). The choice is stored with the order, printed large on
the kitchen receipt (can be turned off in the receipt settings), shown on the
success screen and in the staff panel, and exported to CSV.

The order confirmation screen accepts an optional comment of up to 100
characters. It is typed on an on-screen keyboard (Russian/English
layouts, plus Tajik letters in the Tajik interface); Tajik letters are printed
on the kitchen receipt as the closest Russian letters (ғ→г, қ→к, ҳ→х, ҷ→ч,
ӣ→и, ӯ→у) because receipt code pages do not contain them. It is stored with the order, included in CSV exports, and printed
below the order number. A comment is not part of the price calculation.

The report defaults to the server's current local date and can be filtered by
start/end date. It summarizes order count, total items, total amount, average
order, and a per-product quantity/revenue breakdown. **Скачать CSV** exports
order lines with date/time, product/options, quantity, unit price, total and
comment. The report includes all persisted orders in the selected period, not
just the 50 recent orders listed below it.

## API

- `GET /api/health` — process health.
- `GET /api/menu` — menu from the local workbook and stored product settings.
- `POST /api/orders` — accepts catalog IDs, quantities and selections;
  recomputes prices on the server, accepts an optional 100-character comment,
  saves the order locally, prints it if configured, and requires a UUID
  `Idempotency-Key`.
- `GET /api/admin/products` — protected catalog for staff editing.
- `POST /api/admin/products` and `PUT /api/admin/products/{product_id}` —
  protected product creation and edits.
- `POST /api/admin/products/{product_id}/image` — protected photo upload.
- `GET /api/admin/status` — protected printer status.
- `GET /api/admin/orders` — last 50 locally persisted orders (staff only).
- `GET /api/admin/report?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` — protected
  period summary and product breakdown.
- `GET /api/admin/report.csv?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` —
  protected CSV export for the selected period.
- `POST /api/admin/print` — reprints the kitchen receipt of an existing order
  by its internal reference (`order_ref`).
- `GET /api/admin/printer` and `PUT /api/admin/printer` — protected printer and
  receipt settings.
- `POST /api/admin/printer/preview`, `/check`, `/test` — protected receipt
  preview, connection check and sample print for the submitted settings.

Staff endpoints require an authorization token; the secret is never returned
by the API. Orders are persisted in SQLite at `KIOSK_DB_PATH`; use an
access-controlled directory and include it in the restaurant's backup/retention
policy.

## Validation

Install test dependencies and run:

```powershell
python -m pip install pytest httpx
python -m pytest
```
