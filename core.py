"""core.py - database, schema, seed data, auth helpers, small utilities."""
import calendar
import json
import os
import re
import smtplib
import sqlite3
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from flask import g, jsonify, session
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")
PASSWORD_RULE = ("Password must be more than 8 characters and include an uppercase letter, "
                 "a lowercase letter and a special character.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','staff','customer')),
  created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reset_tokens(
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, type TEXT NOT NULL DEFAULT 'service',
  sales_price REAL NOT NULL, cost_price REAL NOT NULL DEFAULT 0, notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS variants(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL REFERENCES products(id),
  attribute TEXT NOT NULL, value TEXT NOT NULL, extra_price REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS plans(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, price REAL NOT NULL,
  billing_period TEXT NOT NULL CHECK(billing_period IN ('daily','weekly','monthly','yearly')),
  min_qty INTEGER NOT NULL DEFAULT 1, start_date TEXT, end_date TEXT,
  auto_close INTEGER NOT NULL DEFAULT 0, allow_early_close INTEGER NOT NULL DEFAULT 1,
  allow_pause INTEGER NOT NULL DEFAULT 1, allow_renew INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS taxes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, rate_percent REAL NOT NULL);
CREATE TABLE IF NOT EXISTS discounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('fixed','percent')), value REAL NOT NULL,
  min_amount REAL DEFAULT 0, min_qty INTEGER DEFAULT 0, start_date TEXT, end_date TEXT,
  usage_limit INTEGER, used_count INTEGER NOT NULL DEFAULT 0,
  product_id INTEGER);                       -- NULL = applies to the whole subscription
CREATE TABLE IF NOT EXISTS quote_templates(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  plan_id INTEGER NOT NULL REFERENCES plans(id), validity_days INTEGER NOT NULL DEFAULT 30,
  lines_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS subscriptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  number TEXT NOT NULL UNIQUE,
  customer_id INTEGER NOT NULL REFERENCES users(id),
  plan_id INTEGER NOT NULL REFERENCES plans(id),
  start_date TEXT NOT NULL, expiry_date TEXT NOT NULL,
  payment_terms TEXT NOT NULL DEFAULT 'Net 15',
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK(status IN ('draft','quotation','confirmed','active','paused','closed')),
  next_invoice_date TEXT, quote_valid_until TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subscription_lines(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
  product_id INTEGER, variant_id INTEGER, description TEXT NOT NULL,
  qty INTEGER NOT NULL DEFAULT 1, unit_price REAL NOT NULL, tax_id INTEGER);
CREATE TABLE IF NOT EXISTS invoices(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  number TEXT NOT NULL UNIQUE,
  subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
  customer_id INTEGER NOT NULL REFERENCES users(id),
  period_start TEXT NOT NULL, issue_date TEXT NOT NULL, due_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','confirmed','paid','cancelled')),
  subtotal REAL NOT NULL, discount_total REAL NOT NULL, tax_total REAL NOT NULL, total REAL NOT NULL,
  amount_paid REAL NOT NULL DEFAULT 0, sent_at TEXT,
  UNIQUE(subscription_id, period_start));    -- guarantees nothing is billed twice
CREATE TABLE IF NOT EXISTS invoice_lines(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  description TEXT NOT NULL, qty INTEGER NOT NULL, unit_price REAL NOT NULL,
  discount REAL NOT NULL DEFAULT 0, tax_rate REAL NOT NULL DEFAULT 0, amount REAL NOT NULL);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  method TEXT NOT NULL, amount REAL NOT NULL, pay_date TEXT NOT NULL, note TEXT DEFAULT '',
  recorded_by INTEGER);
CREATE INDEX IF NOT EXISTS ix_sub_status ON subscriptions(status);
CREATE INDEX IF NOT EXISTS ix_sub_cust ON subscriptions(customer_id);
CREATE INDEX IF NOT EXISTS ix_sub_next ON subscriptions(next_invoice_date);
CREATE INDEX IF NOT EXISTS ix_subl_sub ON subscription_lines(subscription_id);
CREATE INDEX IF NOT EXISTS ix_inv_status ON invoices(status);
CREATE INDEX IF NOT EXISTS ix_inv_cust ON invoices(customer_id);
CREATE INDEX IF NOT EXISTS ix_inv_due ON invoices(due_date);
CREATE INDEX IF NOT EXISTS ix_invl_inv ON invoice_lines(invoice_id);
CREATE INDEX IF NOT EXISTS ix_pay_inv ON payments(invoice_id);
CREATE INDEX IF NOT EXISTS ix_pay_date ON payments(pay_date);
"""


# ---------- database helpers ----------
def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def q(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def q1(db, sql, args=()):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else None


def rows(sql, args=()):
    return q(get_db(), sql, args)


def one(sql, args=()):
    return q1(get_db(), sql, args)


def run(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def paged(select, frm, args, order, req):
    """Pagination keeps every list fast, even with thousands of rows."""
    page = max(1, req.args.get("page", 1, type=int))
    per = min(100, max(1, req.args.get("per", 15, type=int)))
    db = get_db()
    total = q1(db, "SELECT COUNT(*) AS n " + frm, args)["n"]
    items = q(db, f"{select} {frm} ORDER BY {order} LIMIT ? OFFSET ?", list(args) + [per, (page - 1) * per])
    return {"items": items, "page": page, "pages": max(1, -(-total // per)), "total": total}


# ---------- auth helpers ----------
def valid_password(p):
    return bool(len(p) > 8 and re.search(r"[A-Z]", p) and re.search(r"[a-z]", p)
                and re.search(r"[^A-Za-z0-9]", p))


def roles_required(*roles):
    """No roles = any logged-in user."""
    def deco(f):
        @wraps(f)
        def wrapper(*a, **k):
            if "uid" not in session:
                return jsonify(error="Please log in"), 401
            if roles and session.get("role") not in roles:
                return jsonify(error="You do not have access to this"), 403
            return f(*a, **k)
        return wrapper
    return deco


# ---------- dates ----------
def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def add_period(iso, period):
    d = date.fromisoformat(iso)
    if period == "daily":
        d += timedelta(days=1)
    elif period == "weekly":
        d += timedelta(days=7)
    elif period == "monthly":
        d = add_months(d, 1)
    else:
        d = add_months(d, 12)
    return d.isoformat()


# ---------- email (real SMTP if configured, otherwise printed to console) ----------
OUTBOX = []


def send_email(to, subject, body):
    host = os.environ.get("SMTP_HOST")
    if host:
        try:
            msg = EmailMessage()
            msg["From"] = os.environ.get("SMTP_FROM", "noreply@brightloop.local")
            msg["To"], msg["Subject"] = to, subject
            msg.set_content(body)
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=5) as s:
                s.starttls()
                s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                s.send_message(msg)
            return True
        except Exception as e:  # never let email failure break the demo
            print("EMAIL FAILED, printing instead:", e)
    print(f"\n--- EMAIL to {to} ---\n{subject}\n{body}\n---------------------")
    OUTBOX.append({"to": to, "subject": subject, "body": body})
    return False


# ---------- create tables + seed ----------
def init_db():
    db = connect()
    db.executescript(SCHEMA)
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        t = date.today()
        users = [("Admin", "admin@brightloop.com", "Admin@12345", "admin"),
                 ("Sam Staff", "staff@brightloop.com", "Staff@12345", "staff"),
                 ("Acme Retail", "customer@acme.com", "Customer@123", "customer"),
                 ("Globex Corp", "billing@globex.com", "Customer@123", "customer"),
                 ("Initech Ltd", "accounts@initech.com", "Customer@123", "customer")]
        for n, e, p, r in users:
            db.execute("INSERT INTO users(name,email,password_hash,role,created) VALUES(?,?,?,?,?)",
                       (n, e, generate_password_hash(p), r, now))
        db.executemany("INSERT INTO taxes(name,rate_percent) VALUES(?,?)",
                       [("GST 18%", 18), ("GST 5%", 5), ("Exempt", 0)])
        db.executemany("INSERT INTO products(name,type,sales_price,cost_price,notes) VALUES(?,?,?,?,?)", [
            ("Brightloop CRM", "software", 1200, 400, "Sales pipeline and contacts"),
            ("Brightloop Accounting", "software", 1500, 500, "Books, invoices, reports"),
            ("Extra Seat Add-on", "add-on", 150, 20, "Per additional user"),
            ("Priority Support Pack", "service", 2500, 900, "24x7 priority support")])
        db.executemany("INSERT INTO variants(product_id,attribute,value,extra_price) VALUES(?,?,?,?)", [
            (1, "Brand", "Brightloop", 0), (1, "Brand", "Odoo", 560), (1, "Brand", "Zoho", 300),
            (2, "Edition", "Standard", 0), (2, "Edition", "Pro", 400)])
        db.executemany("""INSERT INTO plans(name,price,billing_period,min_qty,start_date,end_date,
                          auto_close,allow_early_close,allow_pause,allow_renew) VALUES(?,?,?,?,?,?,?,?,?,?)""", [
            ("Monthly Basic", 999, "monthly", 1, None, None, 0, 1, 1, 1),
            ("Yearly Business", 9999, "yearly", 5, None, None, 0, 1, 1, 1),
            ("Weekly Starter", 199, "weekly", 1, None, None, 0, 1, 1, 1),
            # closes by itself, cannot be paused or closed early (rules are set plan by plan)
            ("Seasonal Promo", 799, "monthly", 1, t.isoformat(), (t + timedelta(days=45)).isoformat(), 1, 0, 0, 1)])
        db.executemany("""INSERT INTO discounts(name,kind,value,min_amount,min_qty,start_date,end_date,
                          usage_limit,product_id) VALUES(?,?,?,?,?,?,?,?,?)""", [
            ("Volume 10% (10+ units)", "percent", 10, 0, 10, None, None, None, None),
            ("Support Pack Rs.500 off", "fixed", 500, 2000, 1, None, (t + timedelta(days=90)).isoformat(), 20, 4)])
        db.execute("INSERT INTO quote_templates(name,plan_id,validity_days,lines_json) VALUES(?,?,?,?)",
                   ("Enterprise Starter", 2, 30, json.dumps([
                       {"product_id": 1, "variant_id": 2, "qty": 5}, {"product_id": 3, "variant_id": None, "qty": 10}])))
    db.commit()
    db.close()
