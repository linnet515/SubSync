# Brightloop Subscription Manager

> One platform to run plans, subscriptions, invoices, payments and recurring-revenue reports, replacing spreadsheets and hand-typed invoices.

## The problem
Brightloop Software sells everything on subscription, but renewals are remembered by whoever notices first, invoices are typed by hand, payments are matched by scrolling bank statements, and nobody can say how much recurring revenue exists.

## Our solution
A single web app covering the whole billing month: sign-up and roles, product / plan / tax / discount setup, quotation templates, a subscription lifecycle, one-click billing-day invoicing with automatic tax and discounts, payment tracking, a customer portal, and a finance dashboard.

## Features (mapped to the brief)
| Scene | What we built |
|---|---|
| New customer signs up | Sign-up with unique email, password rules (>8 chars, upper, lower, special), password-reset link by email, roles: **admin / staff / customer**, only admin can add staff |
| Launching a plan | Products with variants (e.g. Brand: Odoo +560), plans with period (daily/weekly/monthly/yearly), min qty, availability dates, per-plan rules: auto-close, early close, pause, renew |
| Quote to subscription | Quotation templates with validity days; numbered subscriptions; Draft → Quotation → Confirmed → Active → Closed (plus Pause / Renew) |
| Billing day | **Run billing** creates every due invoice from its subscription (lines, tax, totals pre-filled). `UNIQUE(subscription, period)` means nothing is billed twice. Invoice flow Draft → Confirmed → Paid; confirm / cancel / send / print |
| Better price | Admin-only discount rules (fixed or %), min amount / qty, dates, usage limit, per product or whole subscription; applied **automatically** on the next invoice and stops on expiry or limit |
| Money comes in | Payments with method, amount, date, note; partial payments; paid vs outstanding vs overdue visible everywhere |
| Finance lead | Dashboard: active subscriptions, MRR, revenue (date filter), amount owed, overdue list, revenue by month |
| Speed | Indexed queries and pagination. Tested with 3,000+ subscriptions: pages respond in milliseconds, billing run for 3,000 subscriptions ≈ 1.5 s |

## Tech stack
Python 3.14 · Flask · SQLite · HTML · Tailwind CSS (CDN) · vanilla JavaScript

## Run locally
```bash
git clone <repo-url>
cd <repo-folder>
python -m venv venv
venv\Scripts\activate          # Mac/Linux: source venv/bin/activate
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000. The database and demo data are created automatically on first run (delete `app.db` to reset).

## Demo logins
| Role | Email | Password |
|---|---|---|
| Admin | admin@brightloop.com | Admin@12345 |
| Staff | staff@brightloop.com | Staff@12345 |
| Customer | customer@acme.com | Customer@123 |

## Project structure
```
app.py        pages, authentication, staff, catalog
billing.py    subscriptions, invoice engine (tax + discounts), payments, dashboard
core.py       database schema, seed data, helpers
templates/index.html   single-page UI
static/styles.css
```

## Assumptions
- A plan's price is its base fee and appears as the first invoice line; product lines are the items or add-ons billed with it.
- Password-reset and invoice emails use SMTP when `SMTP_HOST/SMTP_USER/SMTP_PASS` are set; otherwise they print to the console and the reset link is also shown on screen (`DEMO_MODE=1`).
- Amounts are in INR.

## Team
[Name 1 (Leader)] · [Name 2] · [Name 3] · [Name 4]

Demo video: [link]
