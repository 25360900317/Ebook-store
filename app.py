import os, json, hmac, hashlib, secrets, sqlite3, smtplib, io, base64, urllib.parse
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, abort, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import qrcode

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "store.db"))
BOOK_DIR = Path(os.getenv("BOOK_DIR", str(BASE_DIR / "private_books")))
BOOK_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
UPI_ID = os.getenv("UPI_ID", "saxenajiiiii@axl").strip()
UPI_DISPLAY_NAME = os.getenv("UPI_DISPLAY_NAME", "PromptShelf AI").strip()
SITE_URL = os.getenv("SITE_URL", "http://127.0.0.1:5000").rstrip("/")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
TOKEN_HOURS = int(os.getenv("DOWNLOAD_TOKEN_HOURS", "72"))

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            price_paise INTEGER NOT NULL CHECK(price_paise > 0),
            filename TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            email TEXT NOT NULL,
            amount_paise INTEGER NOT NULL,
            gateway_order_id TEXT UNIQUE,
            gateway_payment_id TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'created',
            download_token_hash TEXT,
            token_expires_at TEXT,
            email_sent_at TEXT,
            manual_payment_ref TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS webhook_events (
            event_id TEXT PRIMARY KEY,
            received_at TEXT NOT NULL
        );
        """)
        # Lightweight migration for databases created by an earlier starter version.
        columns = {row[1] for row in con.execute("PRAGMA table_info(orders)").fetchall()}
        if "manual_payment_ref" not in columns:
            con.execute("ALTER TABLE orders ADD COLUMN manual_payment_ref TEXT")
        count = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if count == 0:
            con.execute(
                "INSERT INTO products(title, description, price_paise, filename, active) VALUES(?,?,?,?,1)",
                ("AI Productivity Toolkit for Beginners",
                 "A practical starter guide to ChatGPT workflows, prompt writing and productivity. Replace this sample listing with your finished eBook.",
                 19900, "sample-ebook.pdf")
            )

init_db()

def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped

def send_download_email(email, product_title, download_url):
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM", user or "")
    if not all([host, user, password, sender]):
        app.logger.warning("SMTP is not configured; email not sent for %s", email)
        return False
    msg = EmailMessage()
    msg["Subject"] = f"Your eBook is ready: {product_title}"
    msg["From"] = sender
    msg["To"] = email
    msg.set_content(
        f"Thanks for your purchase!\n\nYour eBook: {product_title}\n"
        f"Secure download link (expires in {TOKEN_HOURS} hours):\n{download_url}\n\n"
        "If you did not make this purchase, please contact the store owner."
    )
    with smtplib.SMTP(host, port, timeout=20) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
    return True

def issue_download_token(order_id):
    raw_token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    expires = (datetime.now(timezone.utc) + timedelta(hours=TOKEN_HOURS)).isoformat()
    with db() as con:
        con.execute("UPDATE orders SET download_token_hash=?, token_expires_at=? WHERE id=?",
                    (digest, expires, order_id))
    return raw_token

@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store" if request.path.startswith("/download") else "public, max-age=60"
    return response

def make_upi_qr(amount_paise, order_id):
    """Create a personal-UPI QR for manual transfer; it is NOT payment proof."""
    params = {
        "pa": UPI_ID,
        "pn": UPI_DISPLAY_NAME,
        "am": f"{amount_paise / 100:.2f}",
        "cu": "INR",
        "tn": f"eBook order {order_id}",
    }
    uri = "upi://pay?" + urllib.parse.urlencode(params)
    qr = qrcode.QRCode(box_size=7, border=3, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(uri)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

@app.route("/")
def index():
    with db() as con:
        products = con.execute("SELECT * FROM products WHERE active=1 ORDER BY id DESC").fetchall()
    return render_template("index.html", products=products, key_id=RAZORPAY_KEY_ID)

@app.route("/checkout/<int:product_id>", methods=["POST"])
def checkout(product_id):
    email = request.form.get("email", "").strip().lower()
    if len(email) > 254 or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        flash("Please enter a valid email address.")
        return redirect(url_for("index"))
    with db() as con:
        product = con.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
        if not product:
            abort(404)
        cur = con.execute(
            "INSERT INTO orders(product_id,email,amount_paise,status,created_at) VALUES(?,?,?,?,?)",
            (product_id, email, product["price_paise"], "created", datetime.now(timezone.utc).isoformat())
        )
        local_order_id = cur.lastrowid

    gateway_order_id = ""
    gateway_available = bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)
    if gateway_available:
        try:
            import razorpay
            client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
            gateway_order = client.order.create({
                "amount": product["price_paise"],
                "currency": "INR",
                "receipt": f"ebook_{local_order_id}",
                "notes": {"local_order_id": str(local_order_id), "product_id": str(product_id)}
            })
            gateway_order_id = gateway_order["id"]
            with db() as con:
                con.execute("UPDATE orders SET gateway_order_id=? WHERE id=?",
                            (gateway_order_id, local_order_id))
        except Exception:
            app.logger.exception("Razorpay order creation failed; personal UPI option remains available")
            gateway_available = False
            flash("Razorpay checkout could not start. You can still use the personal UPI QR below; delivery requires admin verification.")
    else:
        flash("Razorpay is not configured yet. The personal UPI QR is available below, with manual verification required.")

    return render_template("checkout.html", product=product, email=email,
                           order_id=gateway_order_id, key_id=RAZORPAY_KEY_ID,
                           amount=product["price_paise"], local_order_id=local_order_id,
                           gateway_available=gateway_available,
                           upi_id=UPI_ID, upi_display_name=UPI_DISPLAY_NAME,
                           upi_qr=make_upi_qr(product["price_paise"], local_order_id))

@app.route("/payment/manual-reference/<int:order_id>", methods=["POST"])
def manual_upi_reference(order_id):
    """Record a claimed personal-UPI transfer; never mark it paid here."""
    reference = request.form.get("payment_reference", "").strip()
    if not reference or len(reference) < 6 or len(reference) > 80:
        flash("Enter the UPI transaction reference/UTR (6–80 characters).")
        return redirect(url_for("index"))
    with db() as con:
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            abort(404)
        if order["status"] == "paid":
            flash("This order is already marked paid.")
            return redirect(url_for("index"))
        con.execute("UPDATE orders SET status='manual_pending', manual_payment_ref=? WHERE id=?",
                    (reference, order_id))
    flash("UPI reference submitted. Your order stays pending until the store admin verifies the actual bank/UPI transaction.")
    return redirect(url_for("payment_success"))

@app.route("/payment/verify", methods=["POST"])
def payment_verify():
    # Client callback is a UX convenience only; webhook remains the source of truth.
    # Never unlock download or send the book from this endpoint.
    if not RAZORPAY_KEY_SECRET:
        return {"ok": False, "message": "Payment gateway is not configured"}, 503
    payload = request.get_json(silent=True) or {}
    order_id = str(payload.get("razorpay_order_id", ""))
    payment_id = str(payload.get("razorpay_payment_id", ""))
    signature = str(payload.get("razorpay_signature", ""))
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256
    ).hexdigest()
    if not (order_id and payment_id and signature and hmac.compare_digest(expected, signature)):
        return {"ok": False, "message": "Invalid signature"}, 400
    # Do not mark paid here; webhook confirmation will do that.
    return {"ok": True, "message": "Signature verified; awaiting server webhook confirmation"}

@app.route("/payment/success")
def payment_success():
    return render_template("success.html")

@app.route("/webhooks/razorpay", methods=["POST"])
def razorpay_webhook():
    body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")
    event_id = request.headers.get("X-Razorpay-Event-Id", "")
    if not RAZORPAY_WEBHOOK_SECRET:
        return "Webhook secret not configured", 503
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        return "Invalid webhook signature", 400
    try:
        event = json.loads(body)
    except ValueError:
        return "Invalid JSON", 400

    # Deduplicate webhook delivery. Only payment.captured grants access.
    with db() as con:
        if event_id:
            try:
                con.execute("INSERT INTO webhook_events(event_id, received_at) VALUES(?,?)",
                            (event_id, datetime.now(timezone.utc).isoformat()))
            except sqlite3.IntegrityError:
                return "Duplicate event", 200

    if event.get("event") != "payment.captured":
        return "Ignored", 200

    payment = event.get("payload", {}).get("payment", {}).get("entity", {})
    gateway_order_id = payment.get("order_id")
    gateway_payment_id = payment.get("id")
    amount = payment.get("amount")
    if not gateway_order_id or not gateway_payment_id:
        return "Missing payment fields", 400

    with db() as con:
        order = con.execute("""
            SELECT orders.*, products.title AS product_title
            FROM orders JOIN products ON products.id=orders.product_id
            WHERE gateway_order_id=?
        """, (gateway_order_id,)).fetchone()
        if not order:
            app.logger.error("Captured payment for unknown order %s", gateway_order_id)
            return "Unknown order", 200
        if int(amount or -1) != order["amount_paise"]:
            app.logger.error("Amount mismatch for local order %s", order["id"])
            return "Amount mismatch", 400
        if order["status"] == "paid":
            return "Already processed", 200
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires = (datetime.now(timezone.utc) + timedelta(hours=TOKEN_HOURS)).isoformat()
        con.execute("""
            UPDATE orders SET status='paid', gateway_payment_id=?,
            download_token_hash=?, token_expires_at=? WHERE id=?
        """, (gateway_payment_id, token_hash, expires, order["id"]))
        local_order_id = order["id"]
        customer_email = order["email"]
        product_title = order["product_title"]

    # Email failure must not undo payment; log and allow admin/manual retry.
    try:
        sent = send_download_email(customer_email, product_title,
                                   f"{SITE_URL}/download/{token}")
        if sent:
            with db() as con:
                con.execute("UPDATE orders SET email_sent_at=? WHERE id=?",
                            (datetime.now(timezone.utc).isoformat(), local_order_id))
    except Exception:
        app.logger.exception("Email delivery failed for order %s", local_order_id)
    return "OK", 200

@app.route("/download/<token>")
def download(token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    with db() as con:
        order = con.execute("""
            SELECT orders.*, products.filename, products.title
            FROM orders JOIN products ON products.id=orders.product_id
            WHERE download_token_hash=? AND status='paid'
        """, (digest,)).fetchone()
    if not order:
        abort(404)
    expiry = datetime.fromisoformat(order["token_expires_at"])
    if datetime.now(timezone.utc) > expiry:
        abort(410, "This download link has expired. Contact the store owner.")
    path = (BOOK_DIR / secure_filename(order["filename"])).resolve()
    if BOOK_DIR.resolve() not in path.parents or not path.is_file():
        abort(404, "The eBook file is not available yet.")
    return send_file(path, as_attachment=True, download_name=path.name, mimetype="application/pdf")

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and ADMIN_PASSWORD_HASH and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session.clear()
            session["is_admin"] = True
            return redirect(request.args.get("next") or url_for("admin_dashboard"))
        flash("Invalid credentials. Configure ADMIN_PASSWORD_HASH in .env.")
    return render_template("admin_login.html")

@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/admin")
@admin_required
def admin_dashboard():
    with db() as con:
        products = con.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
        orders = con.execute("""
            SELECT orders.*, products.title AS product_title
            FROM orders JOIN products ON products.id=orders.product_id
            ORDER BY orders.id DESC LIMIT 100
        """).fetchall()
    return render_template("admin.html", products=products, orders=orders)

@app.route("/admin/products", methods=["POST"])
@admin_required
def admin_add_product():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    try:
        price_paise = int(round(float(request.form.get("price", "0")) * 100))
    except ValueError:
        price_paise = 0
    uploaded = request.files.get("ebook")
    if not title or not description or price_paise <= 0 or not uploaded or not uploaded.filename.lower().endswith(".pdf"):
        flash("Provide title, description, positive price and a PDF file.")
        return redirect(url_for("admin_dashboard"))
    safe_name = secure_filename(uploaded.filename)
    filename = f"{secrets.token_hex(8)}_{safe_name}"
    uploaded.save(BOOK_DIR / filename)
    with db() as con:
        con.execute("INSERT INTO products(title,description,price_paise,filename,active) VALUES(?,?,?,?,1)",
                    (title, description, price_paise, filename))
    flash("Product added.")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/products/<int:product_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_product(product_id):
    with db() as con:
        con.execute("UPDATE products SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (product_id,))
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/orders/<int:order_id>/verify-manual-upi", methods=["POST"])
@admin_required
def admin_verify_manual_upi(order_id):
    """Admin must compare the reference and amount with their actual UPI/bank account."""
    with db() as con:
        order = con.execute("""
            SELECT orders.*, products.title AS product_title
            FROM orders JOIN products ON products.id=orders.product_id WHERE orders.id=?
        """, (order_id,)).fetchone()
        if not order or order["status"] != "manual_pending" or not order["manual_payment_ref"]:
            flash("Only pending personal-UPI orders with a reference can be reviewed.")
            return redirect(url_for("admin_dashboard"))
        # This button is the admin's explicit confirmation after checking their bank/UPI app.
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        expires = (datetime.now(timezone.utc) + timedelta(hours=TOKEN_HOURS)).isoformat()
        con.execute("UPDATE orders SET status='paid', download_token_hash=?, token_expires_at=? WHERE id=? AND status='manual_pending'",
                    (digest, expires, order_id))
        email = order["email"]
        title = order["product_title"]
    try:
        sent = send_download_email(email, title, f"{SITE_URL}/download/{token}")
        if sent:
            with db() as con:
                con.execute("UPDATE orders SET email_sent_at=? WHERE id=?",
                            (datetime.now(timezone.utc).isoformat(), order_id))
            flash("Marked paid after admin verification; secure link emailed.")
        else:
            flash("Marked paid after admin verification, but SMTP is not configured. Configure email and use Resend link.")
    except Exception:
        app.logger.exception("Manual UPI order email failed for order %s", order_id)
        flash("Marked paid after admin verification, but email failed. Fix SMTP and use Resend link.")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/orders/<int:order_id>/resend", methods=["POST"])
@admin_required
def admin_resend(order_id):
    with db() as con:
        order = con.execute("""
            SELECT orders.*, products.title AS product_title
            FROM orders JOIN products ON products.id=orders.product_id WHERE orders.id=?
        """, (order_id,)).fetchone()
    if not order or order["status"] != "paid":
        flash("Only paid orders can receive a download link.")
        return redirect(url_for("admin_dashboard"))
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    expires = (datetime.now(timezone.utc) + timedelta(hours=TOKEN_HOURS)).isoformat()
    with db() as con:
        con.execute("UPDATE orders SET download_token_hash=?,token_expires_at=? WHERE id=?",
                    (digest, expires, order_id))
    try:
        send_download_email(order["email"], order["product_title"], f"{SITE_URL}/download/{token}")
        with db() as con:
            con.execute("UPDATE orders SET email_sent_at=? WHERE id=?",
                        (datetime.now(timezone.utc).isoformat(), order_id))
        flash("New download link emailed.")
    except Exception:
        app.logger.exception("Resend failed")
        flash("Email failed. Check SMTP configuration and server logs.")
    return redirect(url_for("admin_dashboard"))

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1")
