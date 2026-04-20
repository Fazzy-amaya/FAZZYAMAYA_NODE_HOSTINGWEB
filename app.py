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
import zipfile
import io
from pathlib import Path
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    session, send_file, jsonify
)
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import create_engine, select, inspect, text
from sqlalchemy.orm import sessionmaker, scoped_session

from models import Base, User, Bot, KeyValue, Payment

# ------------------ Config ------------------

DATA_DIR = Path("./data").resolve()
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
            # wheel columns
            if 'last_spin_date' not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN last_spin_date DATE"))
            if 'spin_remaining' not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN spin_remaining INTEGER DEFAULT 1"))
            if 'free_deploy_available' not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN free_deploy_available BOOLEAN DEFAULT 0"))
            if 'multiplier_active' not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN multiplier_active BOOLEAN DEFAULT 0"))
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

        # env_vars column to bots table
        if 'bots' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('bots')]
            if 'env_vars' not in columns:
                with ENGINE.connect() as conn2:
                    conn2.execute(text("ALTER TABLE bots ADD COLUMN env_vars TEXT DEFAULT '{}'"))
                    conn2.commit()

Base.metadata.create_all(ENGINE)
migrate_database()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_change_me")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

# ------------------ Configuration ------------------
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf'}
ALLOWED_ARCHIVE_EXTENSIONS = {'zip'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def allowed_archive(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_ARCHIVE_EXTENSIONS

OWNER_USERNAME = "FAZZYAMAYA"
OWNER_PASSWORD = "DANGER73"
COIN_COST = 1000
COIN_PACKAGES = [
    {"name": "Starter", "coins": 1000, "price": 1000},
    {"name": "Popular", "coins": 5000, "price": 4500},
    {"name": "Pro", "coins": 10000, "price": 8500},
    {"name": "Ultimate", "coins": 25000, "price": 20000},
]

RESTART_TRACK_FILE = Path("/tmp/app_restart_tracker.json")
RESTART_WINDOW_SEC = 5 * 60
RESTART_LIMIT = 12

def _register_start_and_maybe_exit():
    try:
        now = int(time.time())
        data = {"starts": []}
        if RESTART_TRACK_FILE.exists():
            data = json.loads(RESTART_TRACK_FILE.read_text())
        data["starts"] = [t for t in data["starts"] if now - t < RESTART_WINDOW_SEC]
        data["starts"].append(now)
        RESTART_TRACK_FILE.write_text(json.dumps(data))
        if len(data["starts"]) > RESTART_LIMIT:
            print("Too many restarts in short time. Exiting.")
            raise SystemExit(1)
    except Exception as e:
        print(f"Restart tracker error: {e} (continuing anyway)")

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
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if u.role == "owner":
            return fn(*a, **kw)
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
                trial_used=False
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

# ------------------ Health & Test ------------------
@app.route('/health')
def health():
    return 'OK', 200

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
                proc = bot_processes.get(bot.id)
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                elif bot.pid:
                    try:
                        os.kill(bot.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                bot.pid = None
                bot.status = "stopped"
                bot.expires_at = None
                db.commit()
                bot_processes.pop(bot.id, None)
        except Exception as e:
            print(f"Error in expiry checker: {e}")
        finally:
            db.close()
        time.sleep(60)

expiry_thread = threading.Thread(target=stop_expired_bots, daemon=True)
expiry_thread.start()

# --------------- Auth Routes (no email) ---------------
@app.route("/ping")
def ping():
    return {"status": "ok", "message": "Server is awake"}, 200

@app.get("/")
def index():
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
        confirm_password = request.form.get("confirm_password","")
        
        if not username or not email or not password:
            flash("All fields are required.", "error")
            return redirect(url_for("register"))
        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register"))
        exists = db.execute(select(User).where(
            (User.username == username) | (User.email == email)
        )).scalar_one_or_none()
        if exists:
            flash("Username or email already taken.", "error")
            return redirect(url_for("register"))
        
        u = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            role="user",
            approved=True,
            expiry=None,
            coins=0,
            trial_used=False
        )
        db.add(u)
        db.commit()
        flash("Registration successful! You can now log in and deploy bots.", "success")
        return redirect(url_for("login"))
    finally:
        db.close()

# ------------ Dashboard ------------
@app.get("/dashboard")
@login_required
def dashboard():
    db = get_db()
    try:
        u = current_user()
        today = date.today()
        if u.last_spin_date != today:
            u.last_spin_date = today
            u.spin_remaining = 1
            db.commit()
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

# ------------------ Bot Management (Node.js) ------------------
bot_processes = {}

def find_node_entry_point(bot_dir):
    """Find the main JS file to run. Case‑insensitive."""
    # Common entry names (case‑insensitive)
    common_names = [
        'index.js', 'Index.js', 'INDEX.js',
        'server.js', 'Server.js', 'SERVER.js',
        'app.js', 'App.js', 'APP.js',
        'main.js', 'Main.js', 'MAIN.js',
        'bot.js', 'Bot.js', 'BOT.js',
        'start.js', 'Start.js', 'START.js'
    ]
    # First check common names case‑insensitively
    for name in common_names:
        # Try exact name
        if (bot_dir / name).exists():
            return bot_dir / name
        # Try lowercased version (already lower in list, but ensure)
        lower_name = name.lower()
        if lower_name != name and (bot_dir / lower_name).exists():
            return bot_dir / lower_name
    # Check package.json main field
    pkg_json = bot_dir / "package.json"
    if pkg_json.exists():
        try:
            with open(pkg_json, 'r') as f:
                pkg = json.load(f)
                main = pkg.get('main')
                if main:
                    candidate = bot_dir / main
                    if candidate.exists():
                        return candidate
        except:
            pass
    # Fallback: any .js file (first one found)
    js_files = list(bot_dir.glob("*.js"))
    if js_files:
        return js_files[0]
    return None

def run_npm_install(bot_dir, log_file):
    """Run npm install in bot directory, logging output."""
    try:
        with open(log_file, "a", buffering=1) as lf:
            lf.write(f"\n=== npm install {datetime.utcnow().isoformat()}Z ===\n")
            env = os.environ.copy()
            proc = subprocess.Popen(
                ["npm", "install"],
                cwd=str(bot_dir),
                stdout=lf,
                stderr=lf,
                text=True,
                env=env
            )
            proc.wait(timeout=120)  # 2 minutes max
            if proc.returncode != 0:
                lf.write(f"npm install failed with code {proc.returncode}\n")
    except subprocess.TimeoutExpired:
        with open(log_file, "a") as lf:
            lf.write("npm install timed out after 120 seconds\n")
    except Exception as e:
        with open(log_file, "a") as lf:
            lf.write(f"npm install error: {e}\n")

def _start_node_process(cmd, log_file, env_override=None, bot_dir=None):
    with open(log_file, "a", buffering=1) as lf:
        lf.write(f"\n=== START {datetime.utcnow().isoformat()}Z ===\n")
        env = os.environ.copy()
        if env_override:
            env.update(env_override)
        if bot_dir:
            env['NODE_PATH'] = str(bot_dir) + ":" + env.get('NODE_PATH', '')
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=lf,
            stderr=lf,
            text=True,
            env=env,
            cwd=str(bot_dir) if bot_dir else None
        )
        return proc

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
            if u.free_deploy_available:
                u.free_deploy_available = False
                db.commit()
                bot.expires_at = now + timedelta(hours=24)
                flash("Free 24h deployment used! Your bot will run for 24 hours.", "success")
            elif not u.trial_used:
                u.trial_used = True
                bot.expires_at = now + timedelta(minutes=5)
                flash("You have used your 5‑minute trial. Next starts require coins.", "info")
            else:
                if u.coins < COIN_COST:
                    return jsonify(ok=False, msg=f"Insufficient coins. You need {COIN_COST} coins for 24h deployment.")
                u.coins -= COIN_COST
                bot.expires_at = now + timedelta(hours=24)
            db.commit()
        env_vars = {}
        if bot.env_vars:
            try:
                env_vars = json.loads(bot.env_vars)
            except Exception as e:
                print(f"Failed to parse env_vars for bot {bot.id}: {e}")
        
        # Get bot directory
        bot_dir = path.parent
        # Run npm install if package.json exists
        pkg_json = bot_dir / "package.json"
        if pkg_json.exists():
            print(f"Running npm install for bot {bot.id}")
            run_npm_install(bot_dir, log_file)
        
        # Determine entry point (should be the same as stored filepath, but re-evaluate)
        entry = find_node_entry_point(bot_dir)
        if not entry:
            return jsonify(ok=False, msg="No JavaScript entry file found (index.js, server.js, app.js, main.js, etc.)")
        
        # Update bot.filepath to the actual entry point (in case it changed)
        bot.filepath = str(entry)
        db.commit()
        
        proc = _start_node_process(["node", str(entry)], log_file, env_override=env_vars, bot_dir=str(bot_dir))
        bot.pid = proc.pid
        bot.status = "running"
        db.commit()
        bot_processes[bot.id] = proc
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
            bot_processes.pop(bot.id, None)
            return jsonify(ok=True)
        proc = bot_processes.get(bot.id)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
        else:
            try:
                os.kill(bot.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        bot.pid = None
        bot.status = "stopped"
        bot.expires_at = None
        db.commit()
        bot_processes.pop(bot.id, None)
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
        proc = bot_processes.get(bot.id)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except:
                    pass
        elif bot.pid:
            try:
                os.kill(bot.pid, signal.SIGTERM)
                time.sleep(1)
                try:
                    os.kill(bot.pid, signal.SIGKILL)
                except:
                    pass
            except ProcessLookupError:
                pass
        bot_processes.pop(bot.id, None)
        try:
            if bot.filepath and Path(bot.filepath).exists():
                Path(bot.filepath).unlink(missing_ok=True)
        except Exception as e:
            print(f"Error deleting file {bot.filepath}: {e}")
        try:
            if bot.logpath and Path(bot.logpath).exists():
                Path(bot.logpath).unlink(missing_ok=True)
        except Exception as e:
            print(f"Error deleting log {bot.logpath}: {e}")
        bot_dir = Path(bot.filepath).parent
        if bot_dir.exists() and bot_dir != UPLOAD_DIR:
            try:
                import shutil
                shutil.rmtree(bot_dir)
            except Exception as e:
                print(f"Error removing bot directory {bot_dir}: {e}")
        db.delete(bot)
        db.commit()
        return jsonify(ok=True)
    finally:
        db.close()

# ---------------- Download Bot Folder ----------------
@app.get("/download/<int:bot_id>")
@login_required
def download_bot(bot_id: int):
    db = get_db()
    try:
        u = current_user()
        bot = db.get(Bot, bot_id)
        if not bot or bot.owner_id != u.id:
            flash("Bot not found.", "error")
            return redirect(url_for("dashboard"))
        bot_dir = Path(bot.filepath).parent
        if not bot_dir.exists():
            flash("Bot directory not found.", "error")
            return redirect(url_for("dashboard"))
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for file_path in bot_dir.rglob('*'):
                if file_path.is_file():
                    arcname = file_path.relative_to(bot_dir)
                    zf.write(file_path, arcname)
        memory_file.seek(0)
        return send_file(
            memory_file,
            as_attachment=True,
            download_name=f"{bot.filename}_backup.zip",
            mimetype='application/zip'
        )
    except Exception as e:
        flash(f"Error creating zip: {e}", "error")
        return redirect(url_for("dashboard"))
    finally:
        db.close()

# ---------------- Bot Terminal ----------------
@app.get("/terminal/<int:bot_id>")
@login_required
def bot_terminal(bot_id):
    db = get_db()
    try:
        u = current_user()
        bot = db.get(Bot, bot_id)
        if not bot or bot.owner_id != u.id:
            flash("Bot not found or you don't own it.", "error")
            return redirect(url_for("dashboard"))
        return render_template("terminal_bot.html", bot=bot)
    finally:
        db.close()

def tail_file(filepath, n=100):
    if not filepath.exists():
        return []
    try:
        with open(filepath, 'r', errors='ignore') as f:
            f.seek(0, 2)
            file_size = f.tell()
            block_size = 1024
            lines = []
            pos = file_size
            while pos > 0 and len(lines) < n:
                block_start = max(0, pos - block_size)
                f.seek(block_start)
                block = f.read(pos - block_start)
                block_lines = block.splitlines()
                lines = block_lines + lines
                pos = block_start
            if len(lines) > n:
                lines = lines[-n:]
            return lines
    except Exception as e:
        print(f"Error tailing file {filepath}: {e}")
        return []

tail_threads = {}

@socketio.on('bot_connect')
def handle_bot_connect(data):
    bot_id = data.get('bot_id')
    if not bot_id:
        return
    sid = request.sid
    room = f"bot_{bot_id}"
    join_room(room)
    emit('bot_connected', {'msg': f'Connected to bot {bot_id}'}, room=room)

    db = get_db()
    try:
        bot = db.get(Bot, bot_id)
        if bot and bot.logpath:
            log_path = Path(bot.logpath)
            if log_path.exists():
                last_lines = tail_file(log_path, 100)
                for line in last_lines:
                    socketio.emit('bot_output', line + '\n', room=room)
    except Exception as e:
        print(f"Error sending initial logs for bot {bot_id}: {e}")
    finally:
        db.close()

    def tail_log():
        if not bot or not bot.logpath:
            return
        log_path = Path(bot.logpath)
        try:
            with open(log_path, 'r', errors='ignore') as f:
                f.seek(0, 2)
                while True:
                    if sid not in tail_threads:
                        break
                    line = f.readline()
                    if line:
                        socketio.emit('bot_output', line, room=room)
                    else:
                        time.sleep(0.5)
        except Exception as e:
            print(f"Error tailing log for bot {bot_id}: {e}")

    tail_threads[sid] = threading.Thread(target=tail_log, daemon=True)
    tail_threads[sid].start()

@socketio.on('bot_disconnect')
def handle_bot_disconnect(data):
    sid = request.sid
    if sid in tail_threads:
        del tail_threads[sid]

@socketio.on('bot_command')
def handle_bot_command(data):
    bot_id = data.get('bot_id')
    command = data.get('command', '').strip()
    if not bot_id or not command:
        return
    room = f"bot_{bot_id}"
    # Allow npm install and npm uninstall
    if command.startswith(('npm install', 'npm uninstall', 'npm i')):
        dangerous = ['&', '|', ';', '>', '<', '$', '`', '\\', '(', ')']
        if any(c in command for c in dangerous):
            emit('bot_output', '❌ Command contains unsafe characters.\r\n', room=room)
            return
        db = get_db()
        try:
            bot = db.get(Bot, bot_id)
            if not bot:
                emit('bot_output', '❌ Bot not found.\r\n', room=room)
                return
            bot_dir = Path(bot.filepath).parent
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(bot_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=os.environ.copy()
            )
            def read_output():
                for line in proc.stdout:
                    socketio.emit('bot_output', line, room=room)
                proc.wait()
                socketio.emit('bot_output', f'\r\n✅ Command completed with exit code {proc.returncode}\r\n', room=room)
            socketio.start_background_task(read_output)
        except Exception as e:
            emit('bot_output', f'❌ Failed to start command: {e}\r\n', room=room)
        finally:
            db.close()
        return
    # Otherwise, send command to bot's stdin
    proc = bot_processes.get(int(bot_id))
    if proc and proc.poll() is None:
        try:
            proc.stdin.write(command + '\n')
            proc.stdin.flush()
            emit('bot_output', f'$ {command}\n', room=room)
        except Exception as e:
            emit('bot_output', f'❌ Failed to write to bot stdin: {e}\r\n', room=room)
    else:
        emit('bot_output', '❌ Bot is not running. Start it first.\r\n', room=room)

# ---------------- Upload Route (supports .js and .zip) ----------------
def find_main_file(bot_dir):
    """Find the main JS entry file (case‑insensitive)."""
    # Priority list (case‑insensitive)
    priority_names = [
        'index.js', 'Index.js', 'INDEX.js',
        'server.js', 'Server.js', 'SERVER.js',
        'app.js', 'App.js', 'APP.js',
        'main.js', 'Main.js', 'MAIN.js',
        'bot.js', 'Bot.js', 'BOT.js',
        'start.js', 'Start.js', 'START.js'
    ]
    for name in priority_names:
        if (bot_dir / name).exists():
            return bot_dir / name
        # also check lowercased
        lower_name = name.lower()
        if lower_name != name and (bot_dir / lower_name).exists():
            return bot_dir / lower_name
    # Check package.json main
    pkg_json = bot_dir / "package.json"
    if pkg_json.exists():
        try:
            with open(pkg_json, 'r') as f:
                pkg = json.load(f)
                main = pkg.get('main')
                if main:
                    candidate = bot_dir / main
                    if candidate.exists():
                        return candidate
        except:
            pass
    # Fallback: any .js file
    js_files = list(bot_dir.glob("*.js"))
    if js_files:
        return js_files[0]
    return None

@app.get("/upload", endpoint="upload")
@login_required
def upload_page():
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
        bot_dir = user_dir / str(uuid.uuid4())
        bot_dir.mkdir(parents=True, exist_ok=True)

        target_path = bot_dir / filename

        if allowed_archive(filename):
            # It's a zip file – extract it
            zip_path = target_path
            f.save(zip_path)
            try:
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(bot_dir)
                # Remove the zip file after extraction
                zip_path.unlink()
                # Find the main entry file
                main_file = find_main_file(bot_dir)
                if not main_file:
                    raise Exception("No JavaScript entry file found in the zip archive.")
                filename = main_file.name
                target_path = main_file
            except Exception as e:
                flash(f"Failed to extract zip: {e}", "error")
                import shutil
                shutil.rmtree(bot_dir)
                return redirect(url_for("upload"))
        else:
            # Single file upload – must be .js
            f.save(target_path)
            if not filename.endswith('.js'):
                flash("Only JavaScript (.js) or zip files are allowed.", "error")
                target_path.unlink()
                bot_dir.rmdir()
                return redirect(url_for("upload"))

        uid = strong_uid()
        logpath = (LOG_DIR / f"{uid}.log").as_posix()

        status = "stopped"
        if u.role != "owner" and user_file_limit_reached(db, u):
            status = "pending"

        bot = Bot(
            uid=uid,
            filename=filename,
            filepath=str(target_path),
            owner_id=u.id,
            status=status,
            pid=None,
            token=None,
            auto_restart=False,
            logpath=logpath,
            expires_at=None,
            env_vars="{}"
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
    except Exception as e:
        flash(f"Error: {e}", "error")
        return redirect(url_for("upload"))
    finally:
        db.close()

# ---------------- File Editor ----------------
@app.get("/edit/<int:bot_id>")
@login_required
def edit_file(bot_id):
    db = get_db()
    try:
        u = current_user()
        bot = db.get(Bot, bot_id)
        if not bot or bot.owner_id != u.id:
            flash("Bot not found.", "error")
            return redirect(url_for("dashboard"))
        file_path = Path(bot.filepath)
        if not file_path.exists():
            flash("File not found.", "error")
            return redirect(url_for("dashboard"))
        content = file_path.read_text(encoding='utf-8', errors='ignore')
        return render_template("edit.html", bot=bot, content=content)
    finally:
        db.close()

@app.post("/edit/<int:bot_id>")
@login_required
def edit_file_save(bot_id):
    db = get_db()
    try:
        u = current_user()
        bot = db.get(Bot, bot_id)
        if not bot or bot.owner_id != u.id:
            flash("Bot not found.", "error")
            return redirect(url_for("dashboard"))
        new_content = request.form.get("content", "")
        file_path = Path(bot.filepath)
        if not file_path.exists():
            flash("File not found.", "error")
            return redirect(url_for("dashboard"))
        file_path.write_text(new_content, encoding='utf-8')
        flash("File saved successfully.", "success")
        if bot.status == "running":
            flash("Bot is running. Changes will take effect after restart.", "warning")
        return redirect(url_for("dashboard"))
    except Exception as e:
        flash(f"Error saving file: {e}", "error")
        return redirect(url_for("dashboard"))
    finally:
        db.close()

# ---------------- Clear Logs ----------------
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
        return redirect(url_for("bot_terminal", bot_id=bot.id))
    finally:
        db.close()

# ---------------- Environment Variables ----------------
@app.get("/bot/<int:bot_id>/env")
@login_required
def bot_env(bot_id):
    db = get_db()
    try:
        u = current_user()
        bot = db.get(Bot, bot_id)
        if not bot or bot.owner_id != u.id:
            flash("Bot not found.", "error")
            return redirect(url_for("dashboard"))
        env_vars = {}
        if bot.env_vars:
            try:
                env_vars = json.loads(bot.env_vars)
            except:
                pass
        return render_template("bot_env.html", bot=bot, env_vars=env_vars)
    finally:
        db.close()

@app.post("/bot/<int:bot_id>/env")
@login_required
def bot_env_post(bot_id):
    db = get_db()
    try:
        u = current_user()
        bot = db.get(Bot, bot_id)
        if not bot or bot.owner_id != u.id:
            flash("Bot not found.", "error")
            return redirect(url_for("dashboard"))
        raw = request.form.get("env_vars", "")
        env_dict = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, val = line.split('=', 1)
                env_dict[key.strip()] = val.strip()
        bot.env_vars = json.dumps(env_dict)
        db.commit()
        flash("Environment variables saved.", "success")
        return redirect(url_for("dashboard"))
    finally:
        db.close()

# ---------------- Payment ----------------
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

# ---------------- Admin ----------------
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
            trial_used=False
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

# ---------------- Admin Terminal ----------------
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
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            shell=True,
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

@app.get("/terminal")
@login_required
@owner_required
def terminal():
    return render_template("terminal.html")

# ---------------- Fortune Wheel ----------------
@app.get("/wheel")
@login_required
def fortune_wheel():
    u = current_user()
    return render_template("wheel.html", user=u)

@app.post("/spin")
@login_required
def spin():
    db = get_db()
    try:
        u = current_user()
        today = date.today()
        if u.last_spin_date != today:
            u.last_spin_date = today
            u.spin_remaining = 1
            db.commit()

        if u.spin_remaining <= 0:
            return jsonify({"error": "No spins remaining today. Come back tomorrow!"}), 400

        u.spin_remaining -= 1
        db.commit()

        segments = [
            {"name": "10 Coins", "type": "coins", "value": 10, "weight": 2},
            {"name": "50 Coins", "type": "coins", "value": 50, "weight": 2},
            {"name": "100 Coins", "type": "coins", "value": 100, "weight": 1},
            {"name": "500 Coins", "type": "coins", "value": 500, "weight": 1},
            {"name": "1000 Coins", "type": "coins", "value": 1000, "weight": 1},
            {"name": "Free 24h", "type": "free_deploy", "value": 1, "weight": 1},
            {"name": "2x Coins", "type": "multiplier", "value": 1, "weight": 1},
            {"name": "Free Spin", "type": "free_spin", "value": 1, "weight": 1},
            {"name": "No Win", "type": "none", "value": 0, "weight": 3},
        ]
        weighted = []
        for s in segments:
            weighted.extend([s] * s["weight"])
        result = random.choice(weighted)

        message = ""
        coins_gained = 0
        multiplier_used = False

        if result["type"] == "coins":
            coins_gained = result["value"]
            if u.multiplier_active:
                coins_gained *= 2
                u.multiplier_active = False
                multiplier_used = True
                message = f"🎉 2x Multiplier applied! You won **{coins_gained} coins**!"
            else:
                message = f"🎉 You won **{coins_gained} coins**!"
            u.coins += coins_gained
            db.commit()
        elif result["type"] == "free_deploy":
            u.free_deploy_available = True
            db.commit()
            message = "🎉 Congratulations! You won a **Free 24h Deployment**! Your next bot start will be free."
        elif result["type"] == "multiplier":
            u.multiplier_active = True
            db.commit()
            message = "🎉 2x Multiplier activated! Your next coin reward will be doubled!"
        elif result["type"] == "free_spin":
            u.spin_remaining += 1
            db.commit()
            message = "🎉 Free Spin! You get one extra spin today."
        else:
            message = "😞 No win this time. Better luck next spin!"

        return jsonify({
            "success": True,
            "segment": result["name"],
            "message": message,
            "coins_gained": coins_gained,
            "multiplier_used": multiplier_used,
            "remaining_spins": u.spin_remaining,
            "new_balance": u.coins,
            "free_deploy_available": u.free_deploy_available,
            "multiplier_active": u.multiplier_active
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        db.close()

# ------------------- Main --------------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
