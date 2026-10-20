import os
import re
import hmac
import secrets
import sqlite3
import time
import mimetypes
from collections import defaultdict, deque
from datetime import datetime
from contextlib import contextmanager
from functools import wraps
from urllib.parse import urlparse

from flask import Flask, render_template, request, session, redirect, jsonify
from flask_socketio import SocketIO, emit
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import cloudinary
import cloudinary.uploader

APP_ENV = os.getenv("APP_ENV", "development").lower()
IS_PRODUCTION = APP_ENV == "production"
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY or SECRET_KEY == "your-secret-key-change-in-production":
    if IS_PRODUCTION:
        raise RuntimeError("Set a strong random SECRET_KEY environment variable in production.")
    SECRET_KEY = secrets.token_urlsafe(48)
    print("[SECURITY] Using a temporary development SECRET_KEY; sessions will reset on restart.")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if IS_PRODUCTION and (not ADMIN_PASSWORD or len(ADMIN_PASSWORD) < 14):
    raise RuntimeError("Set ADMIN_PASSWORD to a unique password of at least 14 characters in production.")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,  # 10 MiB request limit
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
)
socketio = SocketIO(app, cors_allowed_origins=None, max_http_buffer_size=1_000_000)

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True,
)

DB_PATH = os.getenv(
    "CHAT_DB_PATH",
    os.path.join(os.path.expanduser("~"), ".chat_app", "chat.db"),
)
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

connected_users = {}
upload_tokens = {}  # socket session ID -> short-lived upload token
login_attempts = defaultdict(deque)
admin_attempts = defaultdict(deque)
upload_attempts = defaultdict(deque)
RATE_WINDOW = 300
MAX_LOGIN_ATTEMPTS = 10
MAX_ADMIN_ATTEMPTS = 5
MAX_UPLOADS_PER_WINDOW = 20
MAX_MESSAGE_LENGTH = 2000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm", ".mov"}
ALLOWED_MIME = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "video/mp4", "video/webm", "video/quicktime",
}
MAX_FILE_SIZE = 8 * 1024 * 1024

def _rate_limited(store, key, limit):
    now = time.time()
    q = store[key]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False

def _client_key():
    # Do not trust X-Forwarded-For unless the reverse proxy is configured to sanitize it.
    return request.remote_addr or "unknown"

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                sender TEXT NOT NULL,
                content TEXT NOT NULL,
                recipient TEXT,
                media_url TEXT,
                media_type TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS unread_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                read BOOLEAN DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        # Upgrade legacy plaintext passwords to Werkzeug hashes. Existing accounts
        # can still log in once; the plaintext is immediately replaced on success.
        rows = conn.execute("SELECT username, password FROM users").fetchall()
        for row in rows:
            password = row["password"] or ""
            if not password.startswith(("scrypt:", "pbkdf2:")):
                conn.execute(
                    "UPDATE users SET password = ? WHERE username = ?",
                    (generate_password_hash(password), row["username"]),
                )
        conn.commit()

# Ensure schema and legacy password migration run under both direct execution and WSGI.
init_db()

def load_users():
    with get_db() as conn:
        return {row["username"]: row["password"] for row in conn.execute(
            "SELECT username, password FROM users"
        ).fetchall()}

def create_user(username, password):
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO users (username, password) VALUES (?, ?)",
                (username, generate_password_hash(password)),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def save_message(msg_type, sender, content, recipient=None, media_url=None, media_type=None):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO messages (type, sender, content, recipient, media_url, media_type)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (msg_type, sender, content, recipient, media_url, media_type),
        )
        message_id = cursor.lastrowid
        if msg_type == "public":
            rows = cursor.execute("SELECT username FROM users WHERE username != ?", (sender,)).fetchall()
            cursor.executemany(
                "INSERT INTO unread_messages (username, message_id, read) VALUES (?, ?, 0)",
                [(row["username"], message_id) for row in rows],
            )
        elif msg_type == "private" and recipient:
            cursor.execute(
                "INSERT INTO unread_messages (username, message_id, read) VALUES (?, ?, 0)",
                (recipient, message_id),
            )
        conn.commit()

def load_chat_history():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, type, sender, content, recipient, media_url, media_type, timestamp
            FROM messages ORDER BY timestamp ASC LIMIT 1000
        """).fetchall()
        return [dict(row) for row in rows]

def get_unread_count(username):
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM unread_messages WHERE username = ? AND read = 0",
            (username,),
        ).fetchone()
        return row["count"] if row else 0

def mark_messages_read(username):
    with get_db() as conn:
        conn.execute("UPDATE unread_messages SET read = 1 WHERE username = ? AND read = 0", (username,))
        conn.commit()

def log_activity(username, action):
    with get_db() as conn:
        conn.execute("INSERT INTO activity_log (username, action) VALUES (?, ?)", (username, action))
        conn.commit()

def get_all_users():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT u.username, u.created_at,
                   COUNT(DISTINCT m.id) AS message_count,
                   COUNT(DISTINCT a.id) AS activity_count
            FROM users u
            LEFT JOIN messages m ON u.username = m.sender
            LEFT JOIN activity_log a ON u.username = a.username
            GROUP BY u.username ORDER BY u.created_at DESC
        """).fetchall()
        return [dict(row) for row in rows]

def get_all_messages():
    with get_db() as conn:
        return [dict(row) for row in conn.execute("""
            SELECT id, type, sender, content, recipient, media_url, media_type, timestamp
            FROM messages ORDER BY timestamp DESC LIMIT 1000
        """).fetchall()]

def get_activity_log():
    with get_db() as conn:
        return [dict(row) for row in conn.execute("""
            SELECT username, action, timestamp FROM activity_log
            ORDER BY timestamp DESC LIMIT 500
        """).fetchall()]

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect("/admin")
        return view(*args, **kwargs)
    return wrapped

def admin_csrf_valid():
    supplied = request.form.get("csrf_token", "")
    expected = session.get("admin_csrf_token", "")
    return bool(supplied and expected and hmac.compare_digest(supplied, expected))

@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload_media", methods=["POST"])
def upload_media():
    token = request.headers.get("X-Chat-Token", "")
    username = request.headers.get("X-Chat-Username", "")
    valid_token = any(
        connected_users.get(sid) == username and hmac.compare_digest(token, value)
        for sid, value in upload_tokens.items()
    )
    if not username or not token or not valid_token:
        return jsonify(error="Please log in again before uploading."), 401
    if _rate_limited(upload_attempts, username, MAX_UPLOADS_PER_WINDOW):
        return jsonify(error="Upload limit reached. Try again later."), 429
    if "file" not in request.files:
        return jsonify(error="No file uploaded."), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify(error="No file selected."), 400
    filename = secure_filename(file.filename)
    ext = os.path.splitext(filename)[1].lower()
    mime = (file.mimetype or "").lower()
    if ext not in ALLOWED_EXTENSIONS or mime not in ALLOWED_MIME:
        return jsonify(error="Only supported image and video files are allowed."), 415
    raw = file.stream.read(MAX_FILE_SIZE + 1)
    if not raw or len(raw) > MAX_FILE_SIZE:
        return jsonify(error="File must be smaller than 8 MB."), 413
    # Basic file-signature checks help reject renamed scripts and arbitrary data.
    signatures = {
        ".png": lambda b: b.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": lambda b: b.startswith(b"\xff\xd8\xff"),
        ".jpeg": lambda b: b.startswith(b"\xff\xd8\xff"),
        ".gif": lambda b: b.startswith((b"GIF87a", b"GIF89a")),
        ".webp": lambda b: len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP",
        ".mp4": lambda b: len(b) >= 12 and b[4:8] == b"ftyp",
        ".mov": lambda b: len(b) >= 12 and b[4:8] == b"ftyp",
        ".webm": lambda b: b.startswith(b"\x1a\x45\xdf\xa3"),
    }
    if not signatures[ext](raw):
        return jsonify(error="The file contents do not match the selected media type."), 415
    if not all((os.getenv("CLOUDINARY_CLOUD_NAME"), os.getenv("CLOUDINARY_API_KEY"), os.getenv("CLOUDINARY_API_SECRET"))):
        return jsonify(error="Media uploads are not configured on this server."), 503
    try:
        from io import BytesIO
        result = cloudinary.uploader.upload(
            BytesIO(raw), resource_type="video" if mime.startswith("video/") else "image",
            folder="chat_uploads", overwrite=False,
        )
        return jsonify(
            url=result.get("secure_url"),
            resource_type=result.get("resource_type", "image"),
            public_id=result.get("public_id"),
        ), 200
    except Exception:
        app.logger.exception("Media upload failed")
        return jsonify(error="Upload failed. Please try again later."), 502

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        if not admin_csrf_valid():
            if not session.get("admin_csrf_token"):
                session["admin_csrf_token"] = secrets.token_urlsafe(32)
            return render_template("admin_login.html", csrf_token=session["admin_csrf_token"],
                                   error="Session expired. Please try again."), 400
        key = _client_key()
        if _rate_limited(admin_attempts, key, MAX_ADMIN_ATTEMPTS):
            return render_template("admin_login.html", csrf_token=session["admin_csrf_token"],
                                   error="Too many attempts. Try again in five minutes."), 429
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if ADMIN_PASSWORD and hmac.compare_digest(username, ADMIN_USERNAME) and hmac.compare_digest(password, ADMIN_PASSWORD):
            session.clear()
            session.permanent = True
            session["admin_authenticated"] = True
            session["admin_csrf_token"] = secrets.token_urlsafe(32)
            return redirect("/admin/dashboard")
        return render_template("admin_login.html", csrf_token=session["admin_csrf_token"],
                               error="Invalid username or password."), 401

    if not session.get("admin_csrf_token"):
        session["admin_csrf_token"] = secrets.token_urlsafe(32)
    if session.get("admin_authenticated"):
        return redirect("/admin/dashboard")
    return render_template("admin_login.html", csrf_token=session["admin_csrf_token"])

@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    users = get_all_users()
    messages = get_all_messages()
    return render_template(
        "admin_dashboard.html",
        users=users, messages=messages, activity=get_activity_log(),
        stats={
            "connected_users": len(connected_users),
            "total_users": len(users),
            "total_messages": len(messages),
            "online_now": list(connected_users.values()),
        },
    )

@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    if not admin_csrf_valid():
        return "Invalid CSRF token", 400
    session.clear()
    return redirect("/admin")

@socketio.on("connect")
def handle_connect(auth=None):
    emit("response", {"data": "Connected to server"})

@socketio.on("login")
def handle_login(data):
    data = data if isinstance(data, dict) else {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    key = request.remote_addr or "unknown"
    if _rate_limited(login_attempts, key, MAX_LOGIN_ATTEMPTS):
        emit("login_response", {"success": False, "message": "Too many attempts. Try again in five minutes."})
        return
    if not USERNAME_RE.fullmatch(username) or not (8 <= len(password) <= 128):
        emit("login_response", {"success": False, "message": "Use a 3–24 character username (letters, numbers, underscores) and an 8–128 character password."})
        return

    with get_db() as conn:
        row = conn.execute("SELECT password FROM users WHERE username = ?", (username,)).fetchone()
        if row:
            stored = row["password"] or ""
            valid = False
            try:
                if stored.startswith(("scrypt:", "pbkdf2:")):
                    valid = check_password_hash(stored, password)
                else:  # Legacy plaintext compatibility for databases not yet initialized/migrated.
                    valid = hmac.compare_digest(stored, password)
                    if valid:
                        conn.execute("UPDATE users SET password = ? WHERE username = ?",
                                     (generate_password_hash(password), username))
                        conn.commit()
            except (ValueError, TypeError):
                valid = False
            if not valid:
                emit("login_response", {"success": False, "message": "Invalid username or password."})
                return
        else:
            conn.execute(
                "INSERT INTO users (username, password) VALUES (?, ?)",
                (username, generate_password_hash(password)),
            )
            conn.commit()

    session.clear()  # rotate session state before establishing the authenticated identity
    session["username"] = username
    connected_users[request.sid] = username
    token = secrets.token_urlsafe(32)
    upload_tokens[request.sid] = token
    log_activity(username, "login")
    unread_count = get_unread_count(username)
    emit("login_response", {"success": True, "message": "Welcome!", "unread_count": unread_count, "upload_token": token, "username": username})
    # Only send public messages and this user's own direct messages.
    history = [
        m for m in load_chat_history()
        if m["type"] != "private" or m["sender"] == username or m["recipient"] == username
    ]
    emit("chat_history", {"messages": history, "unread_count": unread_count})
    mark_messages_read(username)
    socketio.emit("user_joined", {"username": username, "message": f"{username} joined the chat"})
    save_message("system", "server", f"{username} joined the chat")

@socketio.on("message")
def handle_message(data):
    if request.sid not in connected_users:
        emit("error", {"message": "Not logged in"})
        return
    data = data if isinstance(data, dict) else {}
    username = connected_users[request.sid]
    msg = str(data.get("message", "")).strip()
    media_url = data.get("media_url")
    media_type = data.get("media_type")
    timestamp = datetime.now().strftime("%H:%M:%S")

    if media_url:
        # Clients cannot make the server relay arbitrary third-party media URLs.
        parsed = urlparse(str(media_url))
        if parsed.scheme != "https" or not parsed.hostname or not (parsed.hostname == "cloudinary.com" or parsed.hostname.endswith(".cloudinary.com")):
            emit("error", {"message": "Invalid media URL."})
            return
        if media_type not in ("image", "video"):
            emit("error", {"message": "Invalid media type."})
            return
        socketio.emit("message", {"username": username, "message": "", "media_url": media_url,
                                  "media_type": media_type, "timestamp": timestamp})
        save_message("public", username, "", None, media_url, media_type)
        log_activity(username, "sent media message")
        return

    if not msg:
        return
    if len(msg) > MAX_MESSAGE_LENGTH:
        emit("error", {"message": f"Messages must be {MAX_MESSAGE_LENGTH} characters or fewer."})
        return
    if msg.startswith("@"):
        try:
            recipient, content = msg[1:].split(":", 1)
            recipient, content = recipient.strip(), content.strip()
            if not USERNAME_RE.fullmatch(recipient) or not content or len(content) > MAX_MESSAGE_LENGTH:
                emit("error", {"message": "Invalid private message. Use @username: message (max 2000 characters)."})
                return
            recipient_sid = next((sid for sid, uname in connected_users.items() if uname == recipient), None)
            if not recipient_sid:
                emit("error", {"message": "User not found or offline."})
                return
            socketio.emit("private_message", {"from": username, "message": content, "timestamp": timestamp}, to=recipient_sid)
            emit("private_message_sent", {"to": recipient, "message": content, "timestamp": timestamp})
            save_message("private", username, content, recipient)
            log_activity(username, "sent private message")
        except ValueError:
            emit("error", {"message": "Invalid format. Use @username: message"})
    else:
        socketio.emit("message", {"username": username, "message": msg, "timestamp": timestamp})
        save_message("public", username, msg)
        log_activity(username, "sent public message")

@socketio.on("disconnect")
def handle_disconnect():
    username = connected_users.pop(request.sid, None)
    upload_tokens.pop(request.sid, None)
    if username:
        socketio.emit("user_left", {"username": username, "message": f"{username} left the chat"})
        save_message("system", "server", f"{username} left the chat")
        log_activity(username, "logout")

if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", "10000"))
    # Production deployments should run behind a TLS reverse proxy with a production
    # WSGI server compatible with Flask-SocketIO; do not enable debug or unsafe Werkzeug.
    if IS_PRODUCTION:
        print("[SECURITY] Production mode: deploy with eventlet/gevent and a TLS reverse proxy.")
    socketio.run(
    app,
    host="0.0.0.0",
    port=port,
    debug=False,
    allow_unsafe_werkzeug=True
)
