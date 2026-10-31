# SubSync: Subscription Management System

Built for the Odoo Hackathon for **Brightloop Software**.

## Problem
Brightloop earns recurring revenue, but renewals get missed, invoices are created by hand, and nobody has a clear view of recurring revenue or overdue payments.

## Solution
SubSync is one platform for the whole billing cycle. It covers the product catalog, plans, quotation templates, the subscription lifecycle, automatic invoicing with taxes and discounts, payments, a customer portal and a finance dashboard.

## Features
- **Accounts:** sign-up with a unique email, password rules (more than 8 characters, with upper case, lower case and a special character), and a password reset link by email.
- **Three roles:** Admin (everything), Staff (limited, read-only catalog), Customer (own data only). Only the Admin can add staff and create discount rules.
- **Catalog:** products with variants, plans (daily, weekly, monthly, yearly), taxes, discounts and quotation templates.
- **Subscriptions:** Draft > Quotation > Confirmed > Active > Closed, plus pause, resume and renew with per-plan rules.
- **Billing engine:** one click creates every due invoice, with taxes and automatic discounts. A period can never be billed twice (database UNIQUE constraint).
- **Invoices and payments:** Draft > Confirmed > Paid, with confirm, cancel, send, print, and partial payments.
- **Finance dashboard:** active subscriptions, MRR, revenue, outstanding and overdue amounts.
- **Performance:** database indexes and pagination keep pages fast with thousands of subscriptions.

## Tech stack
Python 3.14, Flask, SQLite, HTML + Tailwind CSS (CDN), vanilla JavaScript.

## Run locally
```powershell
git clone https://github.com/linnet515/SubSync.git
cd SubSync
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000. Demo data is created automatically.

## Demo logins
| Role | Email | Password |
|------|-------|----------|
| Admin | admin@brightloop.com | Admin@12345 |
| Staff | staff@brightloop.com | Staff@12345 |
| Customer | customer@acme.com | Customer@123 |

## Project structure
| File | Purpose |
|------|---------|
| `core.py` | Database schema, seed data, helpers, email |
| `app.py` | Login, staff and catalog endpoints |
| `billing.py` | Subscriptions, invoicing, payments, dashboard |
| `templates/index.html` | Single-page frontend |
| `static/styles.css` | Custom styles |

## Main API endpoints
`/api/signup`, `/api/login`, `/api/logout`, `/api/me`, `/api/forgot`, `/api/reset`, `/api/staff`, `/api/customers`, `/api/catalog/<name>`, `/api/subscriptions`, `/api/billing/run`, `/api/invoices`, `/api/payments`, `/api/dashboard`

## Assumptions
- A plan's price is treated as a base fee and appears as the first invoice line. Product lines are billed alongside it.
- Emails print to the terminal unless SMTP environment variables are set.

## Demo video
[..........]

## Team
- [Linnet Jijo], Team Leader
- [Gayathri Vimaldev], Backend
- [Akshita Sivasankaran], Frontend
- [Arya KA], Support