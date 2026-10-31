"""billing.py - subscriptions, invoice engine (taxes + discounts), payments, dashboard."""
import json
import re
import secrets
import sqlite3
from datetime import date, timedelta

from flask import Blueprint, jsonify, request, session

from core import add_period, connect, get_db, paged, q, q1, roles_required, send_email

bp = Blueprint("billing", __name__)


def today():
    return date.today().isoformat()


def r2(x):
    return round(x + 1e-9, 2)


# =============== discount + tax engine ===============
def eligible(d, qty, amount, day):
    return ((not d["start_date"] or d["start_date"] <= day)
            and (not d["end_date"] or day <= d["end_date"])
            and (d["usage_limit"] is None or d["used_count"] < d["usage_limit"])
            and qty >= (d["min_qty"] or 0) and amount >= (d["min_amount"] or 0))


def disc_amount(d, base):
    v = d["value"] if d["kind"] == "fixed" else base * d["value"] / 100
    return min(base, v)


def calc_invoice(db, sub_id, day):
    """Price one billing period. Discounts are applied automatically, taxes come from the admin's rules."""
    lines = q(db, """SELECT sl.*, COALESCE(t.rate_percent,0) AS rate FROM subscription_lines sl
                     LEFT JOIN taxes t ON t.id = sl.tax_id WHERE sl.subscription_id=?""", (sub_id,))
    discs = q(db, "SELECT * FROM discounts")
    used, out = [], []
    for l in lines:
        base = l["qty"] * l["unit_price"]
        cands = [d for d in discs if l["product_id"] and d["product_id"] == l["product_id"]
                 and eligible(d, l["qty"], base, day)]
        best = max(cands, key=lambda d: disc_amount(d, base), default=None)
        ld = disc_amount(best, base) if best else 0
        if best:
            used.append(best["id"])
        out.append({"description": l["description"], "qty": l["qty"], "unit_price": l["unit_price"],
                    "base": base, "line_disc": ld, "rate": l["rate"]})
    net = sum(o["base"] - o["line_disc"] for o in out)
    total_qty = sum(l["qty"] for l in lines if l["product_id"])
    cands = [d for d in discs if d["product_id"] is None and eligible(d, total_qty, net, day)]
    best = max(cands, key=lambda d: disc_amount(d, net), default=None)
    sub_disc = disc_amount(best, net) if best and net > 0 else 0
    if best and sub_disc > 0:
        used.append(best["id"])
    factor = (net - sub_disc) / net if net else 1
    tax = 0.0
    for o in out:
        taxable = (o["base"] - o["line_disc"]) * factor
        o["discount"] = r2(o["base"] - taxable)
        o["amount"] = r2(taxable)
        tax += taxable * o["rate"] / 100
    subtotal = sum(o["base"] for o in out)
    disc_total = sum(o["discount"] for o in out)
    return {"lines": out, "subtotal": r2(subtotal), "discount_total": r2(disc_total), "tax_total": r2(tax),
            "total": r2(subtotal - disc_total + tax), "used": used}


def create_invoice(db, sub, period_start, confirm=False):
    """Returns the new invoice id, or None if this period was already billed."""
    c = calc_invoice(db, sub["id"], period_start)
    m = re.search(r"\d+", sub["payment_terms"] or "")
    due = (date.fromisoformat(period_start) + timedelta(days=int(m.group()) if m else 0)).isoformat()
    try:
        cur = db.execute("""INSERT INTO invoices(number,subscription_id,customer_id,period_start,issue_date,due_date,
                            status,subtotal,discount_total,tax_total,total) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                         ("TMP" + secrets.token_hex(4), sub["id"], sub["customer_id"], period_start, period_start, due,
                          "confirmed" if confirm else "draft", c["subtotal"], c["discount_total"], c["tax_total"], c["total"]))
    except sqlite3.IntegrityError:
        return None
    iid = cur.lastrowid
    db.execute("UPDATE invoices SET number=? WHERE id=?", (f"INV-{iid:05d}", iid))
    for o in c["lines"]:
        db.execute("""INSERT INTO invoice_lines(invoice_id,description,qty,unit_price,discount,tax_rate,amount)
                      VALUES(?,?,?,?,?,?,?)""", (iid, o["description"], o["qty"], o["unit_price"], o["discount"], o["rate"], o["amount"]))
    for did in c["used"]:
        db.execute("UPDATE discounts SET used_count = used_count + 1 WHERE id=?", (did,))
    return iid


def bill_due(db, sub, upto, confirm):
    """Create every invoice that is due up to a date, then move next_invoice_date forward."""
    made, nxt = 0, sub["next_invoice_date"]
    while nxt and nxt <= upto and nxt <= sub["expiry_date"] and made < 36:
        if create_invoice(db, sub, nxt, confirm):
            made += 1
        nxt = add_period(nxt, sub["billing_period"])
    db.execute("UPDATE subscriptions SET next_invoice_date=? WHERE id=?", (nxt, sub["id"]))
    return made


def add_payment(db, inv, amount, method, day, note, uid):
    balance = r2(inv["total"] - inv["amount_paid"])
    if inv["status"] != "confirmed":
        raise ValueError("Only confirmed invoices can receive payments")
    if amount <= 0 or amount > balance + 0.005:
        raise ValueError(f"Amount must be between 0.01 and {balance}")
    db.execute("INSERT INTO payments(invoice_id,method,amount,pay_date,note,recorded_by) VALUES(?,?,?,?,?,?)",
               (inv["id"], method, amount, day, note, uid))
    paid = r2(inv["amount_paid"] + amount)
    db.execute("UPDATE invoices SET amount_paid=?, status=? WHERE id=?",
               (paid, "paid" if paid >= inv["total"] - 0.005 else "confirmed", inv["id"]))


# =============== subscriptions ===============
def build_subscription(db, d, status="draft", valid_days=30):
    plan = q1(db, "SELECT * FROM plans WHERE id=?", (d.get("plan_id"),))
    if not plan:
        raise ValueError("Choose a valid plan")
    if not q1(db, "SELECT id FROM users WHERE id=? AND role='customer'", (d.get("customer_id"),)):
        raise ValueError("Choose a valid customer")
    start = d.get("start_date") or today()
    if plan["start_date"] and start < plan["start_date"]:
        raise ValueError(f"This plan is only available from {plan['start_date']}")
    if plan["end_date"] and start > plan["end_date"]:
        raise ValueError("This plan is closed for new subscriptions")
    items = d.get("lines") or []
    if sum(int(i.get("qty") or 1) for i in items) < plan["min_qty"]:
        raise ValueError(f"This plan needs a minimum quantity of {plan['min_qty']}")
    expiry = d.get("expiry_date") or add_period(start, "yearly")
    if plan["end_date"] and expiry > plan["end_date"]:
        expiry = plan["end_date"]
    if expiry <= start:
        raise ValueError("Expiry date must be after the start date")
    tax = q1(db, "SELECT id FROM taxes ORDER BY id LIMIT 1")
    default_tax = tax["id"] if tax else None
    cur = db.execute("""INSERT INTO subscriptions(number,customer_id,plan_id,start_date,expiry_date,payment_terms,
                        status,quote_valid_until,created) VALUES(?,?,?,?,?,?,?,?,?)""",
                     ("TMP" + secrets.token_hex(4), d["customer_id"], plan["id"], start, expiry,
                      d.get("payment_terms") or "Net 15", status,
                      (date.today() + timedelta(days=valid_days)).isoformat(), today()))
    sid = cur.lastrowid
    db.execute("UPDATE subscriptions SET number=? WHERE id=?", (f"SUB-{sid:05d}", sid))
    # the plan's own fee is always the first line, then the product lines
    db.execute("""INSERT INTO subscription_lines(subscription_id,product_id,variant_id,description,qty,unit_price,tax_id)
                  VALUES(?,?,?,?,?,?,?)""", (sid, None, None, f"Plan: {plan['name']}", 1, plan["price"], default_tax))
    for i in items:
        p = q1(db, "SELECT * FROM products WHERE id=?", (i.get("product_id"),))
        if not p:
            raise ValueError("Unknown product")
        price, label = p["sales_price"], p["name"]
        if i.get("variant_id"):
            v = q1(db, "SELECT * FROM variants WHERE id=? AND product_id=?", (i["variant_id"], p["id"]))
            if not v:
                raise ValueError("That variant does not belong to the chosen product")
            price += v["extra_price"]
            label += f" - {v['value']}"
        db.execute("""INSERT INTO subscription_lines(subscription_id,product_id,variant_id,description,qty,unit_price,tax_id)
                      VALUES(?,?,?,?,?,?,?)""", (sid, p["id"], i.get("variant_id") or None, label,
                                                 int(i.get("qty") or 1), price, i.get("tax_id") or default_tax))
    return sid


def can_see_sub(s):
    return s and (session["role"] != "customer" or s["customer_id"] == session["uid"])


@bp.get("/api/subscriptions")
@roles_required()
def list_subs():
    a, where, args = request.args, ["1=1"], []
    if session["role"] == "customer":
        where.append("s.customer_id=?")
        args.append(session["uid"])
    if a.get("status"):
        where.append("s.status=?")
        args.append(a["status"])
    if a.get("q"):
        where.append("(s.number LIKE ? OR u.name LIKE ?)")
        args += [f"%{a['q']}%"] * 2
    frm = f"FROM subscriptions s JOIN users u ON u.id=s.customer_id JOIN plans p ON p.id=s.plan_id WHERE {' AND '.join(where)}"
    select = """SELECT s.*, u.name AS customer, p.name AS plan, p.billing_period,
                (SELECT COALESCE(SUM(qty*unit_price),0) FROM subscription_lines WHERE subscription_id=s.id) AS amount"""
    return jsonify(paged(select, frm, args, "s.id DESC", request))


@bp.get("/api/subscriptions/<int:sid>")
@roles_required()
def sub_detail(sid):
    db = get_db()
    s = q1(db, """SELECT s.*, u.name AS customer, p.name AS plan, p.billing_period FROM subscriptions s
                  JOIN users u ON u.id=s.customer_id JOIN plans p ON p.id=s.plan_id WHERE s.id=?""", (sid,))
    if not can_see_sub(s):
        return jsonify(error="Not found"), 404
    return jsonify(subscription=s, lines=q(db, "SELECT * FROM subscription_lines WHERE subscription_id=?", (sid,)),
                   invoices=q(db, "SELECT id,number,issue_date,total,status FROM invoices WHERE subscription_id=? ORDER BY id DESC", (sid,)))


@bp.post("/api/subscriptions")
@roles_required("admin", "staff")
def create_sub():
    db = get_db()
    try:
        sid = build_subscription(db, request.get_json(silent=True) or {})
        db.commit()
    except (ValueError, KeyError, sqlite3.Error) as e:
        db.rollback()
        return jsonify(error=str(e)), 400
    return jsonify(id=sid), 201

@bp.put("/api/subscriptions/<int:sid>")
@roles_required("admin", "staff")
def edit_draft(sid):
    """Edit a subscription while it is still a Draft. Send only the fields you want to change."""
    db, d = get_db(), request.get_json(silent=True) or {}
    s = q1(db, "SELECT * FROM subscriptions WHERE id=?", (sid,))
    if not s:
        return jsonify(error="Not found"), 404
    if s["status"] != "draft":
        return jsonify(error="Only Draft subscriptions can be edited"), 400
    try:
        plan = q1(db, "SELECT * FROM plans WHERE id=?", (d.get("plan_id") or s["plan_id"],))
        if not plan:
            raise ValueError("Choose a valid plan")
        start = d.get("start_date") or s["start_date"]
        expiry = d.get("expiry_date") or s["expiry_date"]
        terms = d.get("payment_terms") or s["payment_terms"]
        if plan["start_date"] and start < plan["start_date"]:
            raise ValueError(f"This plan is only available from {plan['start_date']}")
        if plan["end_date"] and start > plan["end_date"]:
            raise ValueError("This plan is closed for new subscriptions")
        if plan["end_date"] and expiry > plan["end_date"]:
            expiry = plan["end_date"]
        if expiry <= start:
            raise ValueError("Expiry date must be after the start date")
        items = d.get("lines")
        if items is None:
            qty = q1(db, """SELECT COALESCE(SUM(qty),0) AS n FROM subscription_lines
                            WHERE subscription_id=? AND product_id IS NOT NULL""", (sid,))["n"]
        else:
            qty = sum(int(i.get("qty") or 1) for i in items)
        if qty < plan["min_qty"]:
            raise ValueError(f"This plan needs a minimum quantity of {plan['min_qty']}")
        if items is not None:  # replace the product lines, keep the plan fee line
            db.execute("DELETE FROM subscription_lines WHERE subscription_id=? AND product_id IS NOT NULL", (sid,))
            tax = q1(db, "SELECT id FROM taxes ORDER BY id LIMIT 1")
            default_tax = tax["id"] if tax else None
            for i in items:
                p = q1(db, "SELECT * FROM products WHERE id=?", (i.get("product_id"),))
                if not p:
                    raise ValueError("Unknown product")
                price, label = p["sales_price"], p["name"]
                if i.get("variant_id"):
                    v = q1(db, "SELECT * FROM variants WHERE id=? AND product_id=?", (i["variant_id"], p["id"]))
                    if not v:
                        raise ValueError("That variant does not belong to the chosen product")
                    price += v["extra_price"]
                    label += f" - {v['value']}"
                db.execute("""INSERT INTO subscription_lines(subscription_id,product_id,variant_id,description,qty,unit_price,tax_id)
                              VALUES(?,?,?,?,?,?,?)""", (sid, p["id"], i.get("variant_id") or None, label,
                                                         int(i.get("qty") or 1), price, i.get("tax_id") or default_tax))
        db.execute("UPDATE subscription_lines SET description=?, unit_price=? WHERE subscription_id=? AND product_id IS NULL",
                   (f"Plan: {plan['name']}", plan["price"], sid))
        db.execute("UPDATE subscriptions SET plan_id=?, start_date=?, expiry_date=?, payment_terms=? WHERE id=?",
                   (plan["id"], start, expiry, terms, sid))
        db.commit()
    except (ValueError, KeyError, sqlite3.Error) as e:
        db.rollback()
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)
@bp.post("/api/subscriptions/from-template")
@roles_required("admin", "staff")
def sub_from_template():
    db, d = get_db(), request.get_json(silent=True) or {}
    t = q1(db, "SELECT * FROM quote_templates WHERE id=?", (d.get("template_id"),))
    if not t:
        return jsonify(error="Choose a template"), 400
    try:
        sid = build_subscription(db, {"customer_id": d.get("customer_id"), "plan_id": t["plan_id"],
                                      "start_date": d.get("start_date"), "lines": json.loads(t["lines_json"] or "[]")},
                                 valid_days=t["validity_days"])
        db.commit()
    except (ValueError, KeyError, sqlite3.Error) as e:
        db.rollback()
        return jsonify(error=str(e)), 400
    return jsonify(id=sid), 201


# action -> (allowed current statuses, new status, plan flag that must be on)
ACTIONS = {
    "quote": (("draft",), "quotation", None),
    "confirm": (("quotation",), "confirmed", None),
    "activate": (("confirmed",), "active", None),
    "pause": (("active",), "paused", "allow_pause"),
    "resume": (("paused",), "active", None),
    "close": (("active", "paused"), "closed", "allow_early_close"),
    "renew": (("active", "closed"), "active", "allow_renew"),
}
CUSTOMER_ACTIONS = {"pause", "resume", "close", "renew"}


@bp.post("/api/subscriptions/<int:sid>/action")
@roles_required()
def sub_action(sid):
    db = get_db()
    act = (request.get_json(silent=True) or {}).get("action")
    s = q1(db, "SELECT * FROM subscriptions WHERE id=?", (sid,))
    if not can_see_sub(s):
        return jsonify(error="Not found"), 404
    if act not in ACTIONS:
        return jsonify(error="Unknown action"), 400
    if session["role"] == "customer" and act not in CUSTOMER_ACTIONS:
        return jsonify(error="Only our team can do that"), 403
    plan = q1(db, "SELECT * FROM plans WHERE id=?", (s["plan_id"],))
    allowed, new, flag = ACTIONS[act]
    if s["status"] not in allowed:
        return jsonify(error=f"Cannot {act} a subscription that is {s['status']}"), 400
    if flag and not plan[flag]:
        return jsonify(error=f"The plan '{plan['name']}' does not allow this"), 400
    if act == "confirm" and s["quote_valid_until"] and s["quote_valid_until"] < today():
        return jsonify(error="This quotation has expired"), 400
    sets = {"status": new}
    if act == "activate":
        sets["next_invoice_date"] = s["start_date"]
    if act == "resume":  # do not bill the paused time
        sets["next_invoice_date"] = max(s["next_invoice_date"] or today(), today())
    if act == "renew":
        new_exp = add_period(max(s["expiry_date"], today()), plan["billing_period"])
        if plan["end_date"] and new_exp > plan["end_date"]:
            return jsonify(error=f"The plan ends on {plan['end_date']}, so it cannot be renewed beyond that"), 400
        sets["expiry_date"] = new_exp
        if s["status"] == "closed":
            sets["next_invoice_date"] = today()
    db.execute("UPDATE subscriptions SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?", [*sets.values(), sid])
    db.commit()
    return jsonify(ok=True, status=new)


@bp.post("/api/billing/run")
@roles_required("admin", "staff")
def billing_run():
    """Billing day: invoice every subscription that is due. Safe to click twice, nothing is billed twice."""
    db = get_db()
    confirm = bool((request.get_json(silent=True) or {}).get("auto_confirm", True))
    subs = q(db, """SELECT s.*, p.billing_period FROM subscriptions s JOIN plans p ON p.id=s.plan_id
                    WHERE s.status='active' AND s.next_invoice_date <= ?""", (today(),))
    made = sum(bill_due(db, s, today(), confirm) for s in subs)
    closed = db.execute("""UPDATE subscriptions SET status='closed' WHERE status='active' AND expiry_date < ?
                           AND plan_id IN (SELECT id FROM plans WHERE auto_close=1)""", (today(),)).rowcount
    db.commit()
    return jsonify(subscriptions_checked=len(subs), invoices_created=made, auto_closed=closed)


# =============== invoices ===============
def load_invoice(iid):
    i = q1(get_db(), """SELECT i.*, u.name AS customer, u.email AS customer_email, s.number AS sub_number
                        FROM invoices i JOIN users u ON u.id=i.customer_id
                        JOIN subscriptions s ON s.id=i.subscription_id WHERE i.id=?""", (iid,))
    if not i:
        return None
    if session["role"] == "customer" and (i["customer_id"] != session["uid"] or i["status"] in ("draft", "cancelled")):
        return None
    return i


@bp.get("/api/invoices")
@roles_required()
def list_invoices():
    a, where, args = request.args, ["1=1"], []
    if session["role"] == "customer":
        where.append("i.customer_id=? AND i.status NOT IN ('draft','cancelled')")
        args.append(session["uid"])
    if a.get("status"):
        where.append("i.status=?")
        args.append(a["status"])
    if a.get("overdue") == "1":
        where.append("i.status='confirmed' AND i.due_date < ?")
        args.append(today())
    if a.get("from"):
        where.append("i.issue_date >= ?")
        args.append(a["from"])
    if a.get("to"):
        where.append("i.issue_date <= ?")
        args.append(a["to"])
    if a.get("q"):
        where.append("(i.number LIKE ? OR u.name LIKE ?)")
        args += [f"%{a['q']}%"] * 2
    frm = f"FROM invoices i JOIN users u ON u.id=i.customer_id WHERE {' AND '.join(where)}"
    select = f"""SELECT i.id,i.number,i.issue_date,i.due_date,i.status,i.total,i.amount_paid,u.name AS customer,
                 i.total - i.amount_paid AS balance,
                 CASE WHEN i.status='confirmed' AND i.due_date < '{today()}' THEN 1 ELSE 0 END AS overdue"""
    return jsonify(paged(select, frm, args, "i.id DESC", request))


@bp.get("/api/invoices/<int:iid>")
@roles_required()
def invoice_detail(iid):
    i = load_invoice(iid)
    if not i:
        return jsonify(error="Not found"), 404
    db = get_db()
    i["balance"] = r2(i["total"] - i["amount_paid"])
    i["overdue"] = int(i["status"] == "confirmed" and i["due_date"] < today())
    return jsonify(invoice=i, lines=q(db, "SELECT * FROM invoice_lines WHERE invoice_id=?", (iid,)),
                   payments=q(db, "SELECT * FROM payments WHERE invoice_id=? ORDER BY id", (iid,)))


@bp.post("/api/invoices/<int:iid>/<action>")
@roles_required("admin", "staff")
def invoice_action(iid, action):
    i, db = load_invoice(iid), get_db()
    if not i:
        return jsonify(error="Not found"), 404
    if action == "confirm":
        if i["status"] != "draft":
            return jsonify(error="Only draft invoices can be confirmed"), 400
        db.execute("UPDATE invoices SET status='confirmed' WHERE id=?", (iid,))
    elif action == "cancel":
        if i["status"] not in ("draft", "confirmed") or i["amount_paid"] > 0:
            return jsonify(error="This invoice cannot be cancelled (paid or already has payments)"), 400
        db.execute("UPDATE invoices SET status='cancelled' WHERE id=?", (iid,))
    elif action == "send":
        if i["status"] in ("draft", "cancelled"):
            return jsonify(error="Confirm the invoice before sending it"), 400
        send_email(i["customer_email"], f"Invoice {i['number']} from Brightloop",
                   f"Hi {i['customer']},\n\nInvoice {i['number']} for Rs.{i['total']} is due on {i['due_date']}.\n"
                   f"You can view it any time in your customer portal.\n")
        db.execute("UPDATE invoices SET sent_at=? WHERE id=?", (today(), iid))
    else:
        return jsonify(error="Unknown action"), 404
    db.commit()
    return jsonify(ok=True)


@bp.post("/api/invoices/<int:iid>/payments")
@roles_required("admin", "staff")
def record_payment(iid):
    i, db, d = load_invoice(iid), get_db(), request.get_json(silent=True) or {}
    if not i:
        return jsonify(error="Not found"), 404
    try:
        add_payment(db, i, float(d.get("amount") or 0), (d.get("method") or "").strip() or "Bank transfer",
                    d.get("pay_date") or today(), d.get("note") or "", session["uid"])
        db.commit()
    except (ValueError, sqlite3.Error) as e:
        db.rollback()
        return jsonify(error=str(e)), 400
    return jsonify(ok=True), 201


@bp.get("/api/payments")
@roles_required("admin", "staff")
def list_payments():
    a, where, args = request.args, ["1=1"], []
    if a.get("method"):
        where.append("p.method=?")
        args.append(a["method"])
    frm = f"""FROM payments p JOIN invoices i ON i.id=p.invoice_id JOIN users u ON u.id=i.customer_id
              WHERE {' AND '.join(where)}"""
    return jsonify(paged("SELECT p.*, i.number AS invoice, u.name AS customer", frm, args, "p.id DESC", request))


# =============== finance dashboard ===============
@bp.get("/api/dashboard")
@roles_required("admin", "staff")
def dashboard():
    db, a, t = get_db(), request.args, today()
    f, to = a.get("from") or "0000-01-01", a.get("to") or "9999-12-31"

    def n(sql, args=()):
        return q1(db, sql, args)["v"]
    mrr = n("""SELECT COALESCE(SUM(sl.qty*sl.unit_price*CASE p.billing_period WHEN 'daily' THEN 30
               WHEN 'weekly' THEN 4.345 WHEN 'monthly' THEN 1 ELSE 1.0/12 END),0) v
               FROM subscription_lines sl JOIN subscriptions s ON s.id=sl.subscription_id
               JOIN plans p ON p.id=s.plan_id WHERE s.status='active'""")
    return jsonify(
        active_subscriptions=n("SELECT COUNT(*) v FROM subscriptions WHERE status='active'"),
        total_subscriptions=n("SELECT COUNT(*) v FROM subscriptions"),
        mrr=r2(mrr),
        revenue=r2(n("SELECT COALESCE(SUM(amount),0) v FROM payments WHERE pay_date BETWEEN ? AND ?", (f, to))),
        outstanding=r2(n("SELECT COALESCE(SUM(total-amount_paid),0) v FROM invoices WHERE status='confirmed'")),
        overdue_count=n("SELECT COUNT(*) v FROM invoices WHERE status='confirmed' AND due_date<?", (t,)),
        overdue_amount=r2(n("SELECT COALESCE(SUM(total-amount_paid),0) v FROM invoices WHERE status='confirmed' AND due_date<?", (t,))),
        invoice_status={r["status"]: r["c"] for r in q(db, "SELECT status, COUNT(*) c FROM invoices GROUP BY status")},
        revenue_by_month=q(db, """SELECT strftime('%Y-%m', pay_date) AS month, SUM(amount) AS amt FROM payments
                                  WHERE pay_date BETWEEN ? AND ? GROUP BY month ORDER BY month""", (f, to)),
        overdue=q(db, """SELECT i.id, i.number, u.name AS customer, i.due_date, i.total - i.amount_paid AS balance
                         FROM invoices i JOIN users u ON u.id=i.customer_id
                         WHERE i.status='confirmed' AND i.due_date<? ORDER BY i.due_date LIMIT 10""", (t,)))
# =============== renewals ===============
@bp.get("/api/renewals")
@roles_required("admin", "staff")
def renewals():
    """Active subscriptions that expire soon (or already expired but still active), so renewals are not missed."""
    days = max(1, min(365, request.args.get("days", 30, type=int)))
    limit = (date.today() + timedelta(days=days)).isoformat()
    items = q(get_db(), """SELECT s.id, s.number, u.name AS customer, p.name AS plan, s.expiry_date,
                           CAST(julianday(s.expiry_date) - julianday(?) AS INTEGER) AS days_left,
                           p.allow_renew, p.auto_close
                           FROM subscriptions s JOIN users u ON u.id = s.customer_id
                           JOIN plans p ON p.id = s.plan_id
                           WHERE s.status = 'active' AND s.expiry_date <= ?
                           ORDER BY s.expiry_date""", (today(), limit))
    return jsonify(days=days, items=items)

# =============== demo data (relative to today so the dashboard always looks alive) ===============

def seed_demo():
    db = connect()
    try:
        if q1(db, "SELECT id FROM subscriptions LIMIT 1"):
            return
        t = date.today()

        def ago(n):
            return (t - timedelta(days=n)).isoformat()
        c = {u["email"]: u["id"] for u in q(db, "SELECT id,email FROM users WHERE role='customer'")}
        acme, globex, initech = c["customer@acme.com"], c["billing@globex.com"], c["accounts@initech.com"]
        demo = [  # customer, plan, start, terms, status, lines
            (acme, 1, ago(70), "Net 15", "active", [{"product_id": 1, "variant_id": 2, "qty": 3}]),
            (globex, 2, ago(200), "Net 30", "active", [{"product_id": 1, "variant_id": 1, "qty": 5}, {"product_id": 3, "qty": 10}]),
            (initech, 1, ago(60), "Net 15", "active", [{"product_id": 2, "variant_id": 5, "qty": 2}, {"product_id": 4, "qty": 1}]),
            (acme, 3, ago(20), "Immediate", "paused", [{"product_id": 3, "qty": 2}]),
            (globex, 1, t.isoformat(), "Net 15", "quotation", [{"product_id": 2, "qty": 1}]),
            (initech, 3, ago(120), "Immediate", "closed", [{"product_id": 3, "qty": 1}]),
        ]
        for cust, plan_id, start, terms, status, lines in demo:
            sid = build_subscription(db, {"customer_id": cust, "plan_id": plan_id, "start_date": start,
                                          "payment_terms": terms, "lines": lines})
            db.execute("UPDATE subscriptions SET status=?, next_invoice_date=? WHERE id=?",
                       (status, start if status in ("active", "paused", "closed") else None, sid))
            if status == "active":
                sub = q1(db, """SELECT s.*, p.billing_period FROM subscriptions s JOIN plans p ON p.id=s.plan_id
                                WHERE s.id=?""", (sid,))
                bill_due(db, sub, t.isoformat(), True)
        db.execute("UPDATE subscriptions SET expiry_date=? WHERE number='SUB-00001'", ((t + timedelta(days=12)).isoformat(),))
        db.execute("UPDATE subscriptions SET expiry_date=? WHERE number='SUB-00003'", ((t + timedelta(days=25)).isoformat(),))
        methods = ["Bank transfer", "Card", "UPI"]
        for k, inv in enumerate(q(db, "SELECT * FROM invoices WHERE status='confirmed' ORDER BY id")):
            if inv["issue_date"] < ago(35):  # old invoices are paid; newer ones stay outstanding / overdue
                add_payment(db, inv, inv["total"], methods[k % 3],
                            (date.fromisoformat(inv["issue_date"]) + timedelta(days=5)).isoformat(), "Matched to bank statement", None)
        db.commit()
    finally:
        db.close()
