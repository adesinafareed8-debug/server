from flask import Flask, render_template, request, session
from flask_socketio import SocketIO, emit
import os
import sqlite3
from datetime import datetime
from contextlib import contextmanager
import cloudinary
import cloudinary.uploader

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-secret-key-change-in-production')
socketio = SocketIO(app, cors_allowed_origins="*")

# Cloudinary config
cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),
    secure=True
)

# Database path - use persistent storage if available
DB_PATH = os.path.join(os.path.expanduser('~'), '.chat_app', 'chat.db')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# Admin credentials
ADMIN_ANSWER = os.getenv('ADMIN_ANSWER', 'secret123')
ADMIN_QUESTION = "What is the secret admin password?"

# In-memory storage of connected users
connected_users = {}  # session_id -> username

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
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
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS unread_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                read BOOLEAN DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

def load_users():
    users = {}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT username, password FROM users')
        for row in cursor.fetchall():
            users[row['username']] = row['password']
    return users

def create_user(username, password):
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute('INSERT INTO users (username, password) VALUES (?, ?)', (username, password))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def load_chat_history():
    messages = []
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, type, sender, content, recipient, media_url, media_type, timestamp
            FROM messages
            ORDER BY timestamp ASC
        ''')
        for row in cursor.fetchall():
            messages.append({
                'id': row['id'],
                'type': row['type'],
                'sender': row['sender'],
                'content': row['content'],
                'recipient': row['recipient'],
                'media_url': row['media_url'],
                'media_type': row['media_type'],
                'timestamp': row['timestamp']
            })
    return messages

def save_message(msg_type, sender, content, recipient=None, media_url=None, media_type=None):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO messages (type, sender, content, recipient, media_url, media_type)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (msg_type, sender, content, recipient, media_url, media_type))
        conn.commit()
        message_id = cursor.lastrowid

        if msg_type == 'public':
            cursor.execute('SELECT username FROM users WHERE username != ?', (sender,))
            for row in cursor.fetchall():
                cursor.execute('''INSERT INTO unread_messages (username, message_id, read) VALUES (?, ?, 0)''', (row['username'], message_id))
        elif msg_type == 'private':
            cursor.execute('''INSERT INTO unread_messages (username, message_id, read) VALUES (?, ?, 0)''', (recipient, message_id))
        conn.commit()

def get_unread_count(username):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) as count FROM unread_messages WHERE username = ? AND read = 0', (username,))
        row = cursor.fetchone()
        return row['count'] if row else 0

def mark_messages_read(username):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE unread_messages SET read = 1 WHERE username = ? AND read = 0', (username,))
        conn.commit()

def log_activity(username, action):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO activity_log (username, action) VALUES (?, ?)', (username, action))
        conn.commit()

def get_all_users():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT u.username, u.created_at,
                   COUNT(DISTINCT m.id) as message_count,
                   COUNT(DISTINCT a.id) as activity_count
            FROM users u
            LEFT JOIN messages m ON u.username = m.sender
            LEFT JOIN activity_log a ON u.username = a.username
            GROUP BY u.username
            ORDER BY u.created_at DESC
        ''')
        return [dict(row) for row in cursor.fetchall()]

def get_all_messages():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, type, sender, content, recipient, media_url, media_type, timestamp
            FROM messages
            ORDER BY timestamp DESC
            LIMIT 1000
        ''')
        return [dict(row) for row in cursor.fetchall()]

def get_activity_log():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT username, action, timestamp
            FROM activity_log
            ORDER BY timestamp DESC
            LIMIT 500
        ''')
        return [dict(row) for row in cursor.fetchall()]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload_media', methods=['POST'])
def upload_media():
    if 'file' not in request.files:
        return {'error': 'No file uploaded'}, 400

    uploaded_file = request.files['file']
    if uploaded_file.filename == '':
        return {'error': 'No file selected'}, 400

    try:
        result = cloudinary.uploader.upload(uploaded_file, resource_type='auto')
        return {
            'url': result.get('secure_url'),
            'resource_type': result.get('resource_type', 'image'),
            'public_id': result.get('public_id')
        }, 200
    except Exception as e:
        return {'error': str(e)}, 500

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if request.method == 'POST':
        answer = request.form.get('answer', '').strip()
        if answer == ADMIN_ANSWER:
            session['admin_authenticated'] = True
            return render_template('admin_dashboard.html', users=get_all_users(), messages=get_all_messages(), activity=get_activity_log(), stats={
                'connected_users': len(connected_users),
                'total_users': len(get_all_users()),
                'total_messages': len(get_all_messages()),
                'online_now': list(connected_users.values())
            })
        return render_template('admin_login.html', question=ADMIN_QUESTION, error='Incorrect answer')

    if session.get('admin_authenticated'):
        return render_template('admin_dashboard.html', users=get_all_users(), messages=get_all_messages(), activity=get_activity_log(), stats={
            'connected_users': len(connected_users),
            'total_users': len(get_all_users()),
            'total_messages': len(get_all_messages()),
            'online_now': list(connected_users.values())
        })

    return render_template('admin_login.html', question=ADMIN_QUESTION)

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_authenticated', None)
    return render_template('admin_login.html', question=ADMIN_QUESTION)

@socketio.on('connect')
def handle_connect():
    print(f"[SERVER] Client connected: {request.sid}")
    emit('response', {'data': 'Connected to server'})

@socketio.on('login')
def handle_login(data):
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    if not username or not password:
        emit('login_response', {'success': False, 'message': 'Username and password required'})
        return

    users = load_users()

    if username in users:
        if users[username] != password:
            emit('login_response', {'success': False, 'message': 'Incorrect password'})
            return
    else:
        if not create_user(username, password):
            emit('login_response', {'success': False, 'message': 'Username already taken'})
            return

    session['username'] = username
    connected_users[request.sid] = username
    log_activity(username, 'login')

    unread_count = get_unread_count(username)
    emit('login_response', {'success': True, 'message': f'Welcome {username}!', 'unread_count': unread_count})
    emit('chat_history', {'messages': load_chat_history(), 'unread_count': unread_count})
    mark_messages_read(username)

    socketio.emit('user_joined', {'username': username, 'message': f'{username} joined the chat'})
    save_message('system', 'server', f'{username} joined the chat')

@socketio.on('message')
def handle_message(data):
    if request.sid not in connected_users:
        emit('error', {'message': 'Not logged in'})
        return

    username = connected_users[request.sid]
    msg = data.get('message', '').strip()
    media_url = data.get('media_url')
    media_type = data.get('media_type')

    timestamp = datetime.now().strftime('%H:%M:%S')

    if media_url:
        socketio.emit('message', {
            'username': username,
            'message': '',
            'media_url': media_url,
            'media_type': media_type,
            'timestamp': timestamp
        })
        save_message('public', username, '', None, media_url, media_type)
        log_activity(username, 'sent media message')
        return

    if not msg:
        return

    if msg.startswith('@'):
        try:
            recipient, content = msg[1:].split(':', 1)
            recipient = recipient.strip()
            content = content.strip()
            recipient_sid = None
            for sid, uname in connected_users.items():
                if uname == recipient:
                    recipient_sid = sid
                    break

            if recipient_sid:
                socketio.emit('private_message', {
                    'from': username,
                    'message': content,
                    'timestamp': timestamp
                }, to=recipient_sid)
                emit('private_message_sent', {'to': recipient, 'message': content, 'timestamp': timestamp})
                save_message('private', username, content, recipient)
                log_activity(username, f'sent private message to {recipient}')
            else:
                emit('error', {'message': f'User {recipient} not found or offline'})
        except ValueError:
            emit('error', {'message': 'Invalid format. Use @username: message'})
    else:
        socketio.emit('message', {'username': username, 'message': msg, 'timestamp': timestamp})
        save_message('public', username, msg)
        log_activity(username, 'sent public message')

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in connected_users:
        username = connected_users[request.sid]
        del connected_users[request.sid]
        print(f"[SERVER] {username} disconnected")
        socketio.emit('user_left', {'username': username, 'message': f'{username} left the chat'})
        save_message('system', 'server', f'{username} left the chat')
        log_activity(username, 'logout')

if __name__ == '__main__':
    init_db()
    port = int(os.getenv('PORT', 10000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
