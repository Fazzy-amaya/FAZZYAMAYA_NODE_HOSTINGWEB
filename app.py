#!/usr/bin/env python3
import os
import json
import time
import uuid
import random
import string
import subprocess
import signal
import sys
import threading
import shlex
import traceback
from pathlib import Path
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    session, send_file, jsonify
)
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import create_engine, select, inspect, text
from sqlalchemy.orm import sessionmaker, scoped_session

from models import Base, User, Bot, KeyValue, Payment

# ------------------ Config ------------------

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
LOG_DIR = DATA_DIR / "logs"
RUN_DIR = DATA_DIR / "run"
QR_UPLOAD_DIR = Path("static/uploads/qr")
RECEIPT_UPLOAD_DIR = UPLOAD_DIR / "receipts"

for d in (DATA_DIR, UPLOAD_DIR, LOG_DIR, RUN_DIR, QR_UPLOAD_DIR, RECEIPT_UPLOAD_DIR):
    d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "app.db"
ENGINE = create_engine(f"sqlite:///{DB_PATH}", echo=False, future=True)
SessionLocal = scoped_session(sessionmaker(bind=ENGINE, expire_on_commit=False))

# ------------------ Database Migration ------------------
def migrate_database():
    inspector = inspect(ENGINE)
    with ENGINE.connect() as conn:
        if 'users' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('users')]
            if 'coins' not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN coins INTEGER DEFAULT 0"))
            if 'trial_used' not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN trial_used BOOLEAN DEFAULT 0"))
            if 'email' not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(120)"))
            if 'email_verified' not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT 0"))
            if 'verification_code' not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN verification_code VARCHAR(6)"))
            conn.commit()
        else:
            print("Users table doesn't exist yet – skipping migration.")

        if 'payments' not in inspector.get_table_names():
            conn.execute(text("""
                CREATE TABLE payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    package VARCHAR(50) NOT NULL,
                    coins INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    receipt_path VARCHAR(255),
                    status VARCHAR(20) DEFAULT 'pending',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            """))
            conn.commit()
        else:
            columns = [col['name'] for col in inspector.get_columns('payments')]
            with ENGINE.connect() as conn2:
                if 'receipt_path' not in columns:
                    conn2.execute(text("ALTER TABLE payments ADD COLUMN receipt_path VARCHAR(255)"))
                    conn2.commit()

Base.metadata.create_all(ENGINE)
migrate_database()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_change_me")
socketio = SocketIO(app, cors_allowed_origins="*")

# ------------------ Email Configuration (SendGrid API via curl) ------------------
SENDGRID_API_KEY = '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'
SENDER_EMAIL = 'freefireelshadai@gmail.com'   # Must be verified in SendGrid

# Allowed extensions for receipt uploads
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Owner fixed credentials
OWNER_USERNAME = "FAZZYAMAYA"
OWNER_PASSWORD = "DANGER73"

# Coin cost for 24h deployment
COIN_COST = 1000

# Predefined coin packages
COIN_PACKAGES = [
    {"name": "Starter", "coins": 1000, "price": 1000},
    {"name": "Popular", "coins": 5000, "price": 4500},
    {"name": "Pro", "coins": 10000, "price": 8500},
    {"name": "Ultimate", "coins": 25000, "price": 20000},
]

# Restart throttle
RESTART_TRACK_FILE = Path("/tmp/app_restart_tracker.json")
RESTART_WINDOW_SEC = 5 * 60
RESTART_LIMIT = 6

def _register_start_and_maybe_exit():
    now = int(time.time())
    data = {"starts": []}
    try:
        if RESTART_TRACK_FILE.exists():
            data = json.loads(RESTART_TRACK_FILE.read_text())
    except Exception:
        data = {"starts": []}
    data["starts"] = [t for t in data["starts"] if now - t < RESTART_WINDOW_SEC]
    data["starts"].append(now)
    RESTART_TRACK_FILE.write_text(json.dumps(data))
    if len(data["starts"]) > RESTART_LIMIT:
        print("Too many restarts in short time. Exiting.")
        raise SystemExit(1)

_register_start_and_maybe_exit()

# ------------------ Helpers ------------------

def get_db():
    return SessionLocal()

def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    db = get_db()
    return db.get(User, uid)

def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper

def owner_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u or u.role != "owner":
            flash("Permission denied. Owner only.", "error")
            return redirect(url_for("dashboard"))
        return fn(*a, **kw)
    return wrapper

def approved_required(fn):
    """All users are automatically approved after email verification."""
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if u.role == "owner":
            return fn(*a, **kw)
        # All regular users are approved by default
        return fn(*a, **kw)
    return wrapper

def strong_uid():
    part1 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
    part2 = datetime.utcnow().year
    part3 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"HOST-{part1}-{part2}-{part3}"

def ensure_owner_exists():
    db = get_db()
    try:
        user = db.execute(select(User).where(User.username == OWNER_USERNAME)).scalar_one_or_none()
        if not user:
            user = User(
                username=OWNER_USERNAME,
                email="owner@example.com",
                password_hash=generate_password_hash(OWNER_PASSWORD),
                role="owner",
                approved=True,
                expiry=None,
                coins=0,
                trial_used=False,
                email_verified=True
            )
            db.add(user)
            db.commit()
        else:
            changed = False
            if user.role != "owner":
                user.role = "owner"; changed = True
            if not user.approved:
                user.approved = True; changed = True
            if user.expiry is not None:
                user.expiry = None; changed = True
            if changed:
                db.commit()
    finally:
        db.close()

ensure_owner_exists()

def get_contact_link(db):
    kv = db.execute(select(KeyValue).where(KeyValue.k == "CONTACT_LINK")).scalar_one_or_none()
    return kv.v if kv else ""

def set_contact_link(db, value: str):
    kv = db.execute(select(KeyValue).where(KeyValue.k == "CONTACT_LINK")).scalar_one_or_none()
    if not kv:
        kv = KeyValue(k="CONTACT_LINK", v=value or "")
        db.add(kv)
    else:
        kv.v = value or ""
    db.commit()

def user_active_bot_count(db, user: User) -> int:
    q = db.query(Bot).filter(Bot.owner_id == user.id)
    try:
        q = q.filter(Bot.status != "deleted")
    except Exception:
        pass
    return q.count()

def user_file_limit_reached(db, user: User) -> bool:
    if user.role == "owner":
        return False
    return user_active_bot_count(db, user) >= 4

# ------------------ Email Functions (SendGrid API via curl) ------------------
def send_email_sync(to, subject, body):
    """Send email via SendGrid API using curl – avoids Python SSL bug."""
    # Build the JSON payload
    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": SENDER_EMAIL},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}]
    }
    payload_json = json.dumps(payload)

    # Build the curl command
    cmd = [
        "curl", "-X", "POST", "https://api.sendgrid.com/v3/mail/send",
        "-H", f"Authorization: Bearer {SENDGRID_API_KEY}",
        "-H", "Content-Type: application/json",
        "-d", payload_json,
        "--max-time", "30", "--fail", "--silent", "--show-error"
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print(f"Email sent successfully to {to}")
            return True, None
        else:
            error = f"curl error (code {result.returncode}): {result.stderr}"
            print(error)
            return False, error
    except Exception as e:
        error = f"Failed to run curl: {e}"
        print(error)
        traceback.print_exc()
        return False, error

def send_email_async(to, subject, body):
    """Send email in background thread – logs errors but doesn't block."""
    def _send():
        with app.app_context():
            success, error = send_email_sync(to, subject, body)
            if not success:
                print(f"Background email error: {error}")
    thread = threading.Thread(target=_send)
    thread.daemon = True
    thread.start()

def send_verification_code(email, code):
    send_email_async(email, "Your Verification Code",
                     f"Your verification code is: {code}\n\nPlease enter this code to complete your registration.")

def notify_bot_expired(user, bot):
    send_email_async(user.email, "Your Bot Has Expired",
                     f"Hello {user.username},\n\nYour bot '{bot.filename}' has expired and has been stopped. To continue, please start it again (coins will be deducted).")

def notify_payment_status(user, payment, status):
    send_email_async(user.email, f"Payment {status.capitalize()}",
                     f"Hello {user.username},\n\nYour payment for {payment.coins} coins (package: {payment.package}) has been {status}.")

def notify_bot_crashed(user, bot):
    send_email_async(user.email, "Your Bot Crashed",
                     f"Hello {user.username},\n\nYour bot '{bot.filename}' has crashed unexpectedly. Please check the logs and restart if needed.")

def notify_low_coins(user):
    send_email_async(user.email, "Low Coin Balance",
                     f"Hello {user.username},\n\nYour coin balance is {user.coins}. You need at least {COIN_COST} coins to start a bot. Please purchase more coins.")

# ------------------ Test Email Route (Synchronous, shows errors) ------------------
@app.route("/test-email")
def test_email():
    """Send a test email synchronously and display the result."""
    try:
        success, error = send_email_sync(SENDER_EMAIL, "Test Email", "If you receive this, email is working!")
        if success:
            return "✅ Test email sent! Check your inbox (and spam folder)."
        else:
            return f"❌ Test email failed: {error}"
    except Exception as e:
        return f"❌ Test email exception: {traceback.format_exc()}"

# ------------------ Background Expiry Checker ------------------
def stop_expired_bots():
    print("Starting expiry checker thread...")
    while True:
        db = get_db()
        try:
            now = datetime.utcnow()
            expired_bots = db.query(Bot).filter(
                Bot.status == "running",
                Bot.expires_at != None,
                Bot.expires_at < now
            ).all()
            for bot in expired_bots:
                print(f"Stopping expired bot {bot.id} (expired at {bot.expires_at})")
                if bot.pid:
                    try:
                        os.kill(bot.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                bot.pid = None
                bot.status = "stopped"
                bot.expires_at = None
                db.commit()
                user = bot.owner
                notify_bot_expired(user, bot)
        except Exception as e:
            print(f"Error in expiry checker: {e}")
        finally:
            db.close()
        time.sleep(60)

expiry_thread = threading.Thread(target=stop_expired_bots, daemon=True)
expiry_thread.start()

# --------------- Auth Routes (with email verification) ------------------

@app.route("/ping")
def ping():
    return {"status": "ok", "message": "Server is awake"}, 200

# ---------- NEW SPLASH PAGE ROUTE ----------
@app.get("/")
def index():
    """Show splash page if not logged in, else dashboard."""
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("splash.html")

@app.get("/login")
def login():
    return render_template("login.html")

@app.post("/login")
def login_post():
    db = get_db()
    try:
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        u = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if not u:
            flash("Invalid username or password", "error")
            return redirect(url_for("login"))

        if u.is_expired():
            flash("Account expired. Please contact owner.", "error")
            return redirect(url_for("login"))

        if not check_password_hash(u.password_hash, password):
            flash("Invalid username or password", "error")
            return redirect(url_for("login"))

        session["uid"] = u.id
        session["role"] = u.role
        flash("Welcome back!", "success")
        return redirect(url_for("dashboard"))
    finally:
        db.close()

@app.get("/logout")
def logout():
    session.pop("uid", None)
    session.pop("role", None)
    return redirect(url_for("login"))

@app.get("/register")
def register():
    return render_template("register.html")

@app.post("/register")
def register_post():
    db = get_db()
    try:
        username = request.form.get("username","").strip()
        email = request.form.get("email","").strip()
        password = request.form.get("password","")

        if not username or not email or not password:
            flash("All fields are required.", "error")
            return redirect(url_for("register"))

        # Check if username or email already exists
        exists = db.execute(select(User).where(
            (User.username == username) | (User.email == email)
        )).scalar_one_or_none()
        if exists:
            flash("Username or email already taken.", "error")
            return redirect(url_for("register"))

        # Generate verification code
        code = ''.join(random.choices(string.digits, k=6))

        # Send email asynchronously – no blocking
        send_verification_code(email, code)

        # Store temp data in session
        session['reg_username'] = username
        session['reg_email'] = email
        session['reg_password'] = password
        session['reg_code'] = code
        flash("Verification code sent to your email. If you don't see it, check spam.", "info")
        return redirect(url_for("verify"))
    finally:
        db.close()

@app.get("/verify")
def verify():
    return render_template("verify.html")

@app.post("/verify")
def verify_post():
    code = request.form.get("code", "").strip()
    if code == session.get('reg_code'):
        db = get_db()
        try:
            u = User(
                username=session['reg_username'],
                email=session['reg_email'],
                password_hash=generate_password_hash(session['reg_password']),
                role="user",
                approved=True,   # automatically approved
                expiry=None,
                coins=0,
                trial_used=False,
                email_verified=True
            )
            db.add(u)
            db.commit()
            session.pop('reg_username', None)
            session.pop('reg_email', None)
            session.pop('reg_password', None)
            session.pop('reg_code', None)
            flash("Registration successful! You can now log in and deploy bots.", "success")
            return redirect(url_for("login"))
        finally:
            db.close()
    else:
        flash("Invalid verification code.", "error")
        return redirect(url_for("verify"))

# ------------ Dashboard & Views -------------

@app.get("/dashboard")
@login_required
def dashboard():
    db = get_db()
    try:
        u = current_user()
        bots = db.query(Bot).filter(Bot.owner_id == u.id).order_by(Bot.created_at.desc()).all()
        running = [b for b in bots if (b.status or "").lower() == "running"]
        stopped = [b for b in bots if (b.status or "").lower() == "stopped"]
        pending = [b for b in bots if (b.status or "").lower() == "pending"]
        contact_link = get_contact_link(db)
        reached = user_file_limit_reached(db, u)
        now = datetime.utcnow()
        for b in running:
            if b.expires_at:
                remaining = (b.expires_at - now).total_seconds()
                b.remaining_seconds = max(0, int(remaining))
            else:
                b.remaining_seconds = None
        return render_template(
            "dashboard.html",
            user=u, bots=bots, running=running, stopped=stopped, pending=pending,
            contact_link=contact_link, file_limit_reached=reached,
            coin_cost=COIN_COST
        )
    finally:
        db.close()

@app.get("/logs/<int:bot_id>")
@login_required
def logs(bot_id: int):
    db = get_db()
    try:
        u = current_user()
        bot = db.get(Bot, bot_id)
        if not bot or bot.owner_id != u.id:
            flash("Not found.", "error")
            return redirect(url_for("dashboard"))
        logs = ""
        if bot.logpath and Path(bot.logpath).exists():
            try:
                logs = Path(bot.logpath).read_text(errors="ignore")[-200000:]
            except Exception:
                logs = "(unable to read logs)"
        return render_template("logs.html", bot=bot, logs=logs)
    finally:
        db.close()

@app.get("/download_logs/<int:bot_id>")
@login_required
def download_logs(bot_id: int):
    db = get_db()
    try:
        u = current_user()
        bot = db.get(Bot, bot_id)
        if not bot or bot.owner_id != u.id:
            flash("Not found.", "error")
            return redirect(url_for("dashboard"))
        if bot.logpath and Path(bot.logpath).exists():
            return send_file(bot.logpath, as_attachment=True, download_name=f"{bot.filename}.log")
        flash("No logs.", "error")
        return redirect(url_for("logs", bot_id=bot.id))
    finally:
        db.close()

@app.post("/clear_logs/<int:bot_id>")
@login_required
def clear_logs(bot_id: int):
    db = get_db()
    try:
        u = current_user()
        bot = db.get(Bot, bot_id)
        if not bot or bot.owner_id != u.id:
            flash("Not found.", "error")
            return redirect(url_for("dashboard"))
        if bot.logpath and Path(bot.logpath).exists():
            open(bot.logpath, 'w').close()
            flash("Logs cleared.", "success")
        else:
            flash("No log file.", "error")
        return redirect(url_for("logs", bot_id=bot.id))
    finally:
        db.close()

# ---------------- Upload/Hosting ------------

@app.get("/upload")
@login_required
def upload():
    u = current_user()
    return render_template("upload.html", user=u)

@app.post("/upload")
@login_required
@approved_required
def upload_post():
    db = get_db()
    try:
        u = current_user()

        if 'file' not in request.files:
            flash("No file.", "error"); return redirect(url_for("upload"))
        f = request.files.get('file')
        if not f or f.filename.strip() == "":
            flash("No file selected.", "error"); return redirect(url_for("upload"))

        filename = os.path.basename(f.filename)
        user_dir = UPLOAD_DIR / f"user_{u.id}"
        user_dir.mkdir(parents=True, exist_ok=True)

        target = user_dir / filename
        f.save(target)

        uid = strong_uid()
        logpath = (LOG_DIR / f"{uid}.log").as_posix()

        status = "stopped"
        if u.role != "owner" and user_file_limit_reached(db, u):
            status = "pending"

        bot = Bot(
            uid=uid,
            filename=filename,
            filepath=str(target),
            owner_id=u.id,
            status=status,
            pid=None,
            token=None,
            auto_restart=False,
            logpath=logpath,
            expires_at=None
        )
        db.add(bot)
        db.commit()

        if status == "pending":
            contact = get_contact_link(db) or "https://t.me/preciouslovesedit"
            flash(
                "⚠️ Limit 4 reached. Request saved as pending. "
                f"<a href='{contact}' target='_blank' class='btn-neon ml-2'>Contact Owner</a>",
                "warning",
            )
        else:
            flash(f"Uploaded: {filename}", "success")
        return redirect(url_for("dashboard"))
    finally:
        db.close()

def _start_process_and_log(cmd, log_file):
    with open(log_file, "a", buffering=1) as lf:
        lf.write(f"\n=== START {datetime.utcnow().isoformat()}Z ===\n")
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        proc = subprocess.Popen(
            cmd, 
            stdout=lf, 
            stderr=lf, 
            text=True, 
            env=env,
            cwd=str(Path(cmd[0]).parent) if Path(cmd[0]).exists() else None
        )
        return proc.pid

@app.post("/start/<int:bot_id>")
@login_required
@approved_required
def start_bot(bot_id: int):
    db = get_db()
    try:
        u = current_user()
        bot = db.get(Bot, bot_id)
        if not bot or bot.owner_id != u.id:
            return jsonify(ok=False, msg="Not found.")
        if (bot.status or "").lower() == "pending":
            return jsonify(ok=False, msg="Pending approval by Owner.")
        if bot.status == "running":
            return jsonify(ok=True, msg="Already running.")
        path = Path(bot.filepath)
        if not path.exists():
            return jsonify(ok=False, msg="File missing on server.")
        log_file = bot.logpath or (LOG_DIR / f"{bot.uid}.log").as_posix()
        bot.logpath = log_file

        now = datetime.utcnow()
        if u.role == "owner":
            bot.expires_at = None
        else:
            if not u.trial_used:
                u.trial_used = True
                bot.expires_at = now + timedelta(minutes=5)
                flash("You have used your 5‑minute trial. Next starts require coins.", "info")
            else:
                if u.coins < COIN_COST:
                    return jsonify(ok=False, msg=f"Insufficient coins. You need {COIN_COST} coins for 24h deployment.")
                u.coins -= COIN_COST
                bot.expires_at = now + timedelta(hours=24)
            db.commit()

        try:
            network_check = subprocess.run([
                sys.executable, 
                str(Path(__file__).parent / "network_check.py")
            ], capture_output=True, text=True, timeout=30)
            if network_check.returncode == 1:
                return jsonify(ok=False, msg="Network issue: No internet connection detected.")
            elif network_check.returncode == 2:
                flash("Warning: Telegram API is currently unreachable. Bot may not work properly.", "warning")
        except subprocess.TimeoutExpired:
            return jsonify(ok=False, msg="Network check timed out. Please try again.")
        except Exception as e:
            print(f"Network check failed: {e}")
        
        pid = _start_process_and_log(["python3", str(path)], log_file)
        bot.pid = pid
        bot.status = "running"
        db.commit()
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, msg=str(e))
    finally:
        db.close()

@app.post("/stop/<int:bot_id>")
@login_required
@approved_required
def stop_bot(bot_id: int):
    db = get_db()
    try:
        u = current_user()
        bot = db.get(Bot, bot_id)
        if not bot or bot.owner_id != u.id:
            return jsonify(ok=False, msg="Not found.")
        if (bot.status or "").lower() == "pending":
            return jsonify(ok=False, msg="Pending bot cannot be stopped.")
        if not bot.pid:
            bot.status = "stopped"
            bot.expires_at = None
            db.commit()
            return jsonify(ok=True)
        try:
            os.kill(bot.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        bot.pid = None
        bot.status = "stopped"
        bot.expires_at = None
        db.commit()
        return jsonify(ok=True)
    finally:
        db.close()

@app.post("/delete/<int:bot_id>")
@login_required
@approved_required
def delete_bot(bot_id: int):
    db = get_db()
    try:
        u = current_user()
        bot = db.get(Bot, bot_id)
        if not bot or bot.owner_id != u.id:
            return jsonify(ok=False, msg="Not found.")

        if bot.pid:
            try: os.kill(bot.pid, signal.SIGTERM)
            except ProcessLookupError: pass

        try:
            if bot.filepath and Path(bot.filepath).exists():
                Path(bot.filepath).unlink(missing_ok=True)
        except Exception: pass
        try:
            if bot.logpath and Path(bot.logpath).exists():
                Path(bot.logpath).unlink(missing_ok=True)
        except Exception: pass

        db.delete(bot)
        db.commit()
        return jsonify(ok=True)
    finally:
        db.close()

# ---------------- Payment System ----------------

@app.get("/payment")
@login_required
def payment():
    qr_path = None
    if (QR_UPLOAD_DIR / "smartcash_qr.png").exists():
        qr_path = url_for('static', filename='uploads/qr/smartcash_qr.png')
    return render_template("payment.html", qr_path=qr_path, packages=COIN_PACKAGES)

@app.post("/payment/submit")
@login_required
def payment_submit():
    db = get_db()
    try:
        u = current_user()
        package_name = request.form.get("package_name")
        coins = request.form.get("coins")
        amount = request.form.get("amount")

        if 'receipt' not in request.files:
            flash("Please upload a payment receipt.", "error")
            return redirect(url_for("payment"))
        file = request.files['receipt']
        if file.filename == '':
            flash("No receipt file selected.", "error")
            return redirect(url_for("payment"))
        if not allowed_file(file.filename):
            flash("Invalid file type. Allowed: png, jpg, jpeg, gif, pdf", "error")
            return redirect(url_for("payment"))

        if not package_name or not coins or not amount:
            flash("Invalid package selection.", "error")
            return redirect(url_for("payment"))

        filename = secure_filename(f"{u.id}_{int(time.time())}_{file.filename}")
        receipt_path = RECEIPT_UPLOAD_DIR / filename
        file.save(receipt_path)

        payment = Payment(
            user_id=u.id,
            package=package_name,
            coins=int(coins),
            amount=int(amount),
            receipt_path=str(receipt_path),
            status="pending"
        )
        db.add(payment)
        db.commit()
        flash("Payment notification sent with receipt. Awaiting approval.", "success")
        return redirect(url_for("dashboard"))
    except Exception as e:
        flash(f"Error: {e}", "error")
        return redirect(url_for("payment"))
    finally:
        db.close()

@app.get("/receipt/<int:payment_id>")
@login_required
@owner_required
def view_receipt(payment_id):
    db = get_db()
    try:
        payment = db.get(Payment, payment_id)
        if not payment or not payment.receipt_path:
            flash("Receipt not found.", "error")
            return redirect(url_for("admin_panel"))
        return send_file(payment.receipt_path)
    finally:
        db.close()

# ---------------- Admin Panel ----------------

@app.get("/admin")
@login_required
@owner_required
def admin_panel():
    db = get_db()
    try:
        ulist = db.query(User).order_by(User.created_at.desc()).all()
        pending_bots = db.query(Bot).filter((Bot.status == "pending")).order_by(Bot.created_at.desc()).all()
        pending_payments = db.query(Payment).filter(Payment.status == "pending").order_by(Payment.created_at.desc()).all()
        contact_link = get_contact_link(db)
        return render_template("admin.html", users=ulist, pending_bots=pending_bots,
                               pending_payments=pending_payments, contact_link=contact_link)
    finally:
        db.close()

@app.post("/admin/set_contact_link")
@login_required
@owner_required
def admin_set_contact_link():
    link = request.form.get("contact_link","").strip()
    db = get_db()
    try:
        set_contact_link(db, link)
        flash("Contact Owner link updated.", "success")
        return redirect(url_for("admin_panel"))
    finally:
        db.close()

@app.post("/admin/create_user")
@login_required
@owner_required
def admin_create_user():
    username = request.form.get("username","").strip()
    password = request.form.get("password","").strip()
    expiry = request.form.get("expiry","").strip()
    approved = bool(request.form.get("approved"))
    coins = request.form.get("coins","0").strip()
    db = get_db()
    try:
        if not username or not password:
            flash("Username & password required.", "error")
            return redirect(url_for("admin_panel"))
        exists = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if exists:
            flash("Username already exists.", "error")
            return redirect(url_for("admin_panel"))
        exp = None
        if expiry:
            try:
                y,m,d = expiry.split("-")
                exp = date(int(y), int(m), int(d))
            except Exception:
                flash("Invalid expiry date.", "error")
                return redirect(url_for("admin_panel"))

        dummy_email = f"{username}@placeholder.com"

        u = User(
            username=username,
            email=dummy_email,
            password_hash=generate_password_hash(password),
            role="user",
            approved=approved,
            expiry=exp,
            coins=int(coins) if coins.isdigit() else 0,
            trial_used=False,
            email_verified=True
        )
        db.add(u)
        db.commit()
        flash("User created.", "success")
        return redirect(url_for("admin_panel"))
    finally:
        db.close()

@app.post("/admin/set_user_status/<int:user_id>")
@login_required
@owner_required
def admin_set_user_status(user_id: int):
    action = request.form.get("action","")
    expiry = request.form.get("expiry","").strip()
    coins = request.form.get("coins","").strip()
    db = get_db()
    try:
        u = db.get(User, user_id)
        if not u:
            flash("User not found.", "error")
            return redirect(url_for("admin_panel"))
        if u.username == OWNER_USERNAME:
            flash("Cannot modify owner.", "error")
            return redirect(url_for("admin_panel"))

        if action == "approve":
            u.approved = True
        elif action == "deny":
            u.approved = False
        elif action == "delete":
            db.delete(u)
            db.commit()
            flash("User deleted.", "success")
            return redirect(url_for("admin_panel"))

        if expiry:
            try:
                y,m,d = expiry.split("-")
                u.expiry = date(int(y), int(m), int(d))
            except Exception:
                flash("Invalid expiry date.", "error")
                return redirect(url_for("admin_panel"))
        if coins:
            try:
                u.coins = int(coins)
            except:
                flash("Invalid coin amount.", "error")
                return redirect(url_for("admin_panel"))
        db.commit()
        flash("Updated.", "success")
        return redirect(url_for("admin_panel"))
    finally:
        db.close()

@app.post("/admin/payment/approve/<int:payment_id>")
@login_required
@owner_required
def approve_payment(payment_id):
    db = get_db()
    try:
        payment = db.get(Payment, payment_id)
        if not payment:
            flash("Payment not found.", "error")
            return redirect(url_for("admin_panel"))

        user = payment.user
        user.coins += payment.coins
        payment.status = "approved"
        db.commit()
        notify_payment_status(user, payment, "approved")
        flash(f"Payment approved. {payment.coins} coins added to {user.username}.", "success")
        return redirect(url_for("admin_panel"))
    finally:
        db.close()

@app.post("/admin/payment/reject/<int:payment_id>")
@login_required
@owner_required
def reject_payment(payment_id):
    db = get_db()
    try:
        payment = db.get(Payment, payment_id)
        if not payment:
            flash("Payment not found.", "error")
            return redirect(url_for("admin_panel"))
        payment.status = "rejected"
        db.commit()
        notify_payment_status(payment.user, payment, "rejected")
        flash("Payment rejected.", "success")
        return redirect(url_for("admin_panel"))
    finally:
        db.close()

@app.post("/admin/upload_qr")
@login_required
@owner_required
def upload_qr():
    if 'qr_file' not in request.files:
        flash("No file.", "error")
        return redirect(url_for("admin_panel"))
    f = request.files['qr_file']
    if f.filename == '':
        flash("No file selected.", "error")
        return redirect(url_for("admin_panel"))
    f.save(QR_UPLOAD_DIR / "smartcash_qr.png")
    flash("QR code uploaded.", "success")
    return redirect(url_for("admin_panel"))

@app.post("/admin/approve_bot/<int:bot_id>")
@login_required
@owner_required
def approve_bot(bot_id: int):
    db = get_db()
    try:
        bot = db.get(Bot, bot_id)
        if not bot:
            flash("Bot not found.", "error")
            return redirect(url_for("admin_panel"))
        bot.status = "stopped"
        db.commit()
        flash("Bot approved.", "success")
        return redirect(url_for("admin_panel"))
    finally:
        db.close()

@app.post("/admin/reject_bot/<int:bot_id>")
@login_required
@owner_required
def reject_bot(bot_id: int):
    db = get_db()
    try:
        bot = db.get(Bot, bot_id)
        if not bot:
            flash("Bot not found.", "error")
            return redirect(url_for("admin_panel"))
        bot.status = "rejected"
        db.commit()
        flash("Bot rejected.", "success")
        return redirect(url_for("admin_panel"))
    finally:
        db.close()

# ---------------- Terminal (restricted) ----------------

active_processes = {}

@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    proc = active_processes.pop(sid, None)
    if proc:
        try:
            proc.terminate()
        except:
            pass

@socketio.on('command')
def handle_command(command):
    sid = request.sid
    if sid in active_processes:
        emit('pty_output', '⚠️ Another command is already running. Please wait.\r\n', room=sid)
        return

    command = command.strip()
    if not command:
        return

    if command.startswith('pip install'):
        dangerous = ['&', '|', ';', '>', '<', '$', '`', '\\', '(', ')']
        if any(c in command for c in dangerous):
            emit('pty_output', '❌ Command contains unsafe characters.\r\n', room=sid)
            return

        try:
            args = shlex.split(command)
        except Exception as e:
            emit('pty_output', f'❌ Failed to parse command: {e}\r\n', room=sid)
            return

        if len(args) < 2 or args[1] != 'install':
            emit('pty_output', '❌ Only "pip install" is allowed.\r\n', room=sid)
            return

        cmd = [sys.executable, '-m'] + args

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=os.environ.copy()
            )
            active_processes[sid] = proc

            def read_output():
                for line in proc.stdout:
                    socketio.emit('pty_output', line, room=sid)
                proc.wait()
                socketio.emit('pty_output', f'\r\n✅ Command completed with exit code {proc.returncode}\r\n', room=sid)
                active_processes.pop(sid, None)

            socketio.start_background_task(read_output)

        except Exception as e:
            emit('pty_output', f'❌ Failed to start command: {e}\r\n', room=sid)
            active_processes.pop(sid, None)

    elif command == 'pkg update':
        emit('pty_output', 'ℹ️ "pkg update" is not available on this server. Use "pip install" instead.\r\n', room=sid)
    else:
        emit('pty_output', f'❌ Command not allowed. Only "pip install ..." is permitted.\r\n', room=sid)

@app.get("/terminal")
@login_required
@owner_required
def terminal():
    return render_template("terminal.html")

# ------------------- Main --------------------

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
