"""app.py - Flask app: pages, authentication, staff, catalog (products/plans/taxes/discounts/templates)."""
import os
import secrets
import sqlite3
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from billing import bp as billing_bp, seed_demo
from core import (PASSWORD_RULE, close_db, init_db, one, roles_required, rows, run,
                  send_email, valid_password)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
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


def user_json(u):
    return {"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"]}


def login_user(u):
    session.clear()
    session.update(uid=u["id"], role=u["role"], name=u["name"])


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
    return jsonify(items=rows("SELECT id,name,email,role,created FROM users WHERE role IN ('admin','staff') ORDER BY id"))


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
    try:
        new_id = run(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                     [d[c] for c in cols])
    except sqlite3.Error as e:
        return jsonify(error=f"Could not save: {e}"), 400
    return jsonify(id=new_id), 201


if __name__ == "__main__":
    init_db()
    seed_demo()
    app.run(debug=True, port=5000)
