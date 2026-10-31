"""app.py - Flask app: pages, authentication, staff, catalog (products/plans/taxes/discounts/templates)."""
import os
import secrets
import sqlite3
import json

from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from billing import bp as billing_bp, seed_demo
from core import (PASSWORD_RULE, close_db, init_db, one, roles_required, rows, run,
                  send_email, valid_password)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.teardown_appcontext(close_db)
app.register_blueprint(billing_bp)
DEMO_MODE = os.environ.get("DEMO_MODE", "1") == "1"   # shows the reset link on screen as well as in the email

TABLES = {  # url name -> (table, allowed columns)
    "products": ("products", ["name", "type", "sales_price", "cost_price", "notes"]),
    "variants": ("variants", ["product_id", "attribute", "value", "extra_price"]),
    "plans": ("plans", ["name", "price", "billing_period", "min_qty", "start_date", "end_date",
                        "auto_close", "allow_early_close", "allow_pause", "allow_renew"]),
    "taxes": ("taxes", ["name", "rate_percent"]),
    "discounts": ("discounts", ["name", "kind", "value", "min_amount", "min_qty", "start_date",
                                "end_date", "usage_limit", "product_id"]),
    "templates": ("quote_templates", ["name", "plan_id", "validity_days", "lines_json"]),


}
# where each item can be "in use" (used to block deleting)
USAGE = {
    "products": [
        ("SELECT COUNT(*) AS n FROM subscription_lines WHERE product_id=?",
         "it is used in a subscription"),
        ("SELECT COUNT(*) AS n FROM subscription_lines WHERE variant_id IN "
         "(SELECT id FROM variants WHERE product_id=?)", "one of its variants is used in a subscription"),
        ("SELECT COUNT(*) AS n FROM discounts WHERE product_id=?",
         "a discount rule uses it"),
    ],
    "variants": [
        ("SELECT COUNT(*) AS n FROM subscription_lines WHERE variant_id=?",
         "it is used in a subscription"),
    ],
    "plans": [
        ("SELECT COUNT(*) AS n FROM subscriptions WHERE plan_id=?",
         "it is used by a subscription"),
        ("SELECT COUNT(*) AS n FROM quote_templates WHERE plan_id=?",
         "a quotation template uses it"),
    ],
    "taxes": [
        ("SELECT COUNT(*) AS n FROM subscription_lines WHERE tax_id=?",
         "it is used in a subscription"),
    ],
    "discounts": [
        ("SELECT COUNT(*) AS n FROM discounts WHERE id=? AND used_count>0",
         "it has already been applied to invoices"),
    ],
    "templates": [],
}

NUMBER_FIELDS = ["sales_price", "cost_price", "extra_price", "price", "rate_percent",
                 "value", "min_amount", "min_qty", "validity_days", "usage_limit"]


def check_numbers(name, d):
    """Return an error message if a number field is wrong, otherwise None."""
    for f in NUMBER_FIELDS:
        if d.get(f) not in (None, ""):
            try:
                n = float(d[f])
            except (TypeError, ValueError):
                return f"{f} must be a number"
            if n < 0:
                return f"{f} cannot be negative"
            if f == "rate_percent" and n > 100:
                return "rate_percent cannot be more than 100"
            if f == "value" and name == "discounts" and d.get("kind") == "percent" and n > 100:
                return "A percent discount cannot be more than 100"
    return None


def used_in_templates(key, item_id):
    """Quote templates keep product/variant ids inside lines_json, so check there too."""
    for t in rows("SELECT lines_json FROM quote_templates"):
        try:
            lines = json.loads(t["lines_json"])
        except ValueError:
            continue
        if any(isinstance(l, dict) and l.get(key) == item_id for l in lines):
            return True
    return False


def why_in_use(name, item_id):
    """Plain-English reason the item cannot be deleted, or None if it is free."""
    for sql, reason in USAGE.get(name, []):
        if one(sql, (item_id,))["n"] > 0:
            return reason
    if name == "products" and used_in_templates("product_id", item_id):
        return "a quotation template uses it"
    if name == "variants" and used_in_templates("variant_id", item_id):
        return "a quotation template uses it"
    return None


def user_json(u):
    return {"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"]}


def login_user(u):
    session.clear()
    session.update(uid=u["id"], role=u["role"], name=u["name"])

@app.before_request
def drop_deactivated_sessions():
    """If a logged-in user was deactivated, log them out on their next request."""
    uid = session.get("uid")
    if uid and request.path.startswith("/api/"):
        u = one("SELECT active FROM users WHERE id=?", (uid,))
        if not u or not u["active"]:
            session.clear()


@app.route("/")
def home():
    return render_template("index.html")


# ---------------- authentication ----------------
@app.post("/api/signup")
def signup():
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip()
    email = (d.get("email") or "").strip().lower()
    pw = d.get("password") or ""
    if not name or "@" not in email:
        return jsonify(error="Please enter your name and a valid email"), 400
    if not valid_password(pw):
        return jsonify(error=PASSWORD_RULE), 400
    try:
        uid = run("INSERT INTO users(name,email,password_hash,role,created) VALUES(?,?,?,'customer',?)",
                  (name, email, generate_password_hash(pw), datetime.now().strftime("%Y-%m-%d %H:%M")))
    except sqlite3.IntegrityError:
        return jsonify(error="This email is already registered"), 409
    u = one("SELECT * FROM users WHERE id=?", (uid,))
    login_user(u)
    return jsonify(user=user_json(u)), 201


@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}
    u = one("SELECT * FROM users WHERE email=?", ((d.get("email") or "").strip().lower(),))
    if not u or not check_password_hash(u["password_hash"], d.get("password") or ""):
        return jsonify(error="Wrong email or password"), 401
    if not u["active"]:
        return jsonify(error="This account has been deactivated. Please contact the admin."), 403
    login_user(u)
    return jsonify(user=user_json(u))


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def me():
    if "uid" not in session:
        return jsonify(user=None)
    u = one("SELECT * FROM users WHERE id=?", (session["uid"],))
    return jsonify(user=user_json(u) if u else None)


@app.post("/api/forgot")
def forgot():
    email = ((request.get_json(silent=True) or {}).get("email") or "").strip().lower()
    u = one("SELECT * FROM users WHERE email=?", (email,))
    resp = {"message": "If that email is registered, a reset link has been sent."}
    if u:
        token = secrets.token_urlsafe(24)
        run("INSERT INTO reset_tokens(token,user_id,expires) VALUES(?,?,?)",
            (token, u["id"], (datetime.now() + timedelta(minutes=30)).isoformat()))
        link = request.host_url + "#reset=" + token
        send_email(u["email"], "Reset your Brightloop password",
                   f"Hi {u['name']},\n\nUse this link to set a new password (valid 30 minutes):\n{link}\n")
        if DEMO_MODE:
            resp["dev_link"] = link
    return jsonify(resp)


@app.post("/api/reset")
def reset():
    d = request.get_json(silent=True) or {}
    t = one("SELECT * FROM reset_tokens WHERE token=?", (d.get("token") or "",))
    if not t or t["expires"] < datetime.now().isoformat():
        return jsonify(error="This reset link is invalid or has expired"), 400
    if not valid_password(d.get("password") or ""):
        return jsonify(error=PASSWORD_RULE), 400
    run("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(d["password"]), t["user_id"]))
    run("DELETE FROM reset_tokens WHERE token=?", (t["token"],))
    return jsonify(ok=True)


# ---------------- staff & customers ----------------
@app.get("/api/staff")
@roles_required("admin")
def staff_list():
    return jsonify(items=rows(
        "SELECT id,name,email,role,created,active FROM users WHERE role IN ('admin','staff') ORDER BY id"))


def set_staff_active(uid, value):
    u = one("SELECT * FROM users WHERE id=?", (uid,))
    if not u or u["role"] != "staff":
        return jsonify(error="Staff member not found"), 404
    run("UPDATE users SET active=? WHERE id=?", (value, uid))
    return jsonify(ok=True, active=value)


@app.post("/api/staff/<int:uid>/deactivate")
@roles_required("admin")
def staff_deactivate(uid):
    return set_staff_active(uid, 0)


@app.post("/api/staff/<int:uid>/activate")
@roles_required("admin")
def staff_activate(uid):
    return set_staff_active(uid, 1)


@app.post("/api/staff")
@roles_required("admin")  # only the admin can add staff
def staff_add():
    d = request.get_json(silent=True) or {}
    name, email, pw = (d.get("name") or "").strip(), (d.get("email") or "").strip().lower(), d.get("password") or ""
    if not name or "@" not in email:
        return jsonify(error="Name and valid email are required"), 400
    if not valid_password(pw):
        return jsonify(error=PASSWORD_RULE), 400
    try:
        uid = run("INSERT INTO users(name,email,password_hash,role,created) VALUES(?,?,?,'staff',?)",
                  (name, email, generate_password_hash(pw), datetime.now().strftime("%Y-%m-%d %H:%M")))
    except sqlite3.IntegrityError:
        return jsonify(error="This email is already registered"), 409
    return jsonify(id=uid), 201


@app.get("/api/customers")
@roles_required("admin", "staff")
def customers():
    return jsonify(items=rows("SELECT id,name,email FROM users WHERE role='customer' ORDER BY name"))


# ---------------- catalog (admin writes, staff reads) ----------------
@app.get("/api/catalog/<name>")
@roles_required("admin", "staff")
def catalog_list(name):
    if name not in TABLES:
        return jsonify(error="Unknown list"), 404
    data = rows(f"SELECT * FROM {TABLES[name][0]} ORDER BY id")
    if name == "products":
        vs = rows("SELECT * FROM variants ORDER BY id")
        for p in data:
            p["variants"] = [v for v in vs if v["product_id"] == p["id"]]
    return jsonify(items=data)


@app.post("/api/catalog/<name>")
@roles_required("admin")
def catalog_add(name):
    if name not in TABLES:
        return jsonify(error="Unknown list"), 404
    table, fields = TABLES[name]
    d = request.get_json(silent=True) or {}
    cols = [f for f in fields if d.get(f) not in (None, "")]
    if not cols:
        return jsonify(error="Nothing to save"), 400
    problem = check_numbers(name, d)                 # NEW
    if problem:                                      # NEW
        return jsonify(error=problem), 400           # NEW
    try:
        new_id = run(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                     [d[c] for c in cols])
    except sqlite3.Error as e:
        return jsonify(error=f"Could not save: {e}"), 400
    return jsonify(id=new_id), 201

@app.put("/api/catalog/<name>/<int:item_id>")
@roles_required("admin")
def catalog_update(name, item_id):
    if name not in TABLES:
        return jsonify(error="Unknown list"), 404
    table, fields = TABLES[name]
    if not one(f"SELECT id FROM {table} WHERE id=?", (item_id,)):
        return jsonify(error="Item not found"), 404
    d = request.get_json(silent=True) or {}
    cols = [f for f in fields if f in d]
    if not cols:
        return jsonify(error="Nothing to update"), 400
    problem = check_numbers(name, d)
    if problem:
        return jsonify(error=problem), 400
    values = [None if d[c] == "" else d[c] for c in cols]
    try:
        run(f"UPDATE {table} SET {','.join(c + '=?' for c in cols)} WHERE id=?", values + [item_id])
    except sqlite3.Error as e:
        return jsonify(error=f"Could not save: {e}"), 400
    return jsonify(ok=True)


@app.delete("/api/catalog/<name>/<int:item_id>")
@roles_required("admin")
def catalog_delete(name, item_id):
    if name not in TABLES:
        return jsonify(error="Unknown list"), 404
    table = TABLES[name][0]
    if not one(f"SELECT id FROM {table} WHERE id=?", (item_id,)):
        return jsonify(error="Item not found"), 404
    reason = why_in_use(name, item_id)
    if reason:
        return jsonify(error=f"Cannot delete this item because {reason}."), 409
    try:
        if name == "products":
            run("DELETE FROM variants WHERE product_id=?", (item_id,))
        run(f"DELETE FROM {table} WHERE id=?", (item_id,))
    except sqlite3.Error as e:
        return jsonify(error=f"Could not delete: {e}"), 409
    return jsonify(ok=True)




if __name__ == "__main__":
    init_db()
    seed_demo()
    app.run(debug=os.environ.get("FLASK_DEBUG", "1") == "1", port=5000)
