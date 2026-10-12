from flask import Flask, render_template, request, session, redirect, url_for
from flask_socketio import SocketIO, emit
import os
import sqlite3
from datetime import datetime
from contextlib import contextmanager

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-secret-key-change-in-production')
socketio = SocketIO(app, cors_allowed_origins="*")

# Database path - use persistent storage if available
DB_PATH = os.path.join(os.path.expanduser('~'), '.chat_app', 'chat.db')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# Admin credentials
ADMIN_ANSWER = os.getenv('ADMIN_ANSWER', 'secret123')  # Change this in production
ADMIN_QUESTION = "What is the secret admin password?"

# In-memory storage of connected users
connected_users = {}  # session_id -> username

@contextmanager
def get_db():
    """Context manager for database connections"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    """Initialize the database with required tables"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Messages table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                sender TEXT NOT NULL,
                content TEXT NOT NULL,
                recipient TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Unread messages table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS unread_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                read BOOLEAN DEFAULT 0,
                FOREIGN KEY (message_id) REFERENCES messages(id)
            )
        ''')
        
        # User activity log
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
    """Load all users from database"""
    users = {}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT username, password FROM users')
        for row in cursor.fetchall():
            users[row['username']] = row['password']
    return users

def user_exists(username):
    """Check if user exists"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM users WHERE username = ?', (username,))
        return cursor.fetchone() is not None

def create_user(username, password):
    """Create a new user"""
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute('INSERT INTO users (username, password) VALUES (?, ?)', (username, password))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def load_chat_history():
    """Load all messages from database"""
    messages = []
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, type, sender, content, recipient, timestamp 
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
                'timestamp': row['timestamp']
            })
    return messages

def save_message(msg_type, sender, content, recipient=None):
    """Save message to database and mark as unread for relevant users"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO messages (type, sender, content, recipient)
            VALUES (?, ?, ?, ?)
        ''', (msg_type, sender, content, recipient))
        conn.commit()
        
        message_id = cursor.lastrowid
        
        # Mark message as unread for all users except sender
        if msg_type == 'public':
            # All users except sender should see this as unread
            cursor.execute('SELECT username FROM users WHERE username != ?', (sender,))
            for row in cursor.fetchall():
                cursor.execute('''
                    INSERT INTO unread_messages (username, message_id, read)
                    VALUES (?, ?, 0)
                ''', (row['username'], message_id))
        elif msg_type == 'private':
            # Only recipient sees this as unread
            cursor.execute('''
                INSERT INTO unread_messages (username, message_id, read)
                VALUES (?, ?, 0)
            ''', (recipient, message_id))
        
        conn.commit()

def get_unread_count(username):
    """Get unread message count for a user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) as count FROM unread_messages 
            WHERE username = ? AND read = 0
        ''', (username,))
        row = cursor.fetchone()
        return row['count'] if row else 0

def mark_messages_read(username):
    """Mark all messages as read for a user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE unread_messages SET read = 1
            WHERE username = ? AND read = 0
        ''', (username,))
        conn.commit()

def log_activity(username, action):
    """Log user activity"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO activity_log (username, action)
            VALUES (?, ?)
        ''', (username, action))
        conn.commit()

def get_all_users():
    """Get all users with their stats"""
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
    """Get all messages with details"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, type, sender, content, recipient, timestamp
            FROM messages
            ORDER BY timestamp DESC
            LIMIT 1000
        ''')
        return [dict(row) for row in cursor.fetchall()]

def get_activity_log():
    """Get activity log"""
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

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    """Admin dashboard - requires security question"""
    if request.method == 'POST':
        answer = request.form.get('answer', '').strip()
        if answer == ADMIN_ANSWER:
            session['admin_authenticated'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            return render_template('admin_login.html', 
                                 question=ADMIN_QUESTION, 
                                 error='Incorrect answer')
    
    if session.get('admin_authenticated'):
        return redirect(url_for('admin_dashboard'))
    
    return render_template('admin_login.html', question=ADMIN_QUESTION)

@app.route('/admin/dashboard')
def admin_dashboard():
    """Admin dashboard view"""
    if not session.get('admin_authenticated'):
        return redirect(url_for('admin'))
    
    users = get_all_users()
    messages = get_all_messages()
    activity = get_activity_log()
    
    connected_count = len(connected_users)
    total_users = len(users)
    total_messages = len(messages)
    
    stats = {
        'connected_users': connected_count,
        'total_users': total_users,
        'total_messages': total_messages,
        'online_now': list(connected_users.values())
    }
    
    return render_template('admin_dashboard.html', 
                         users=users,
                         messages=messages,
                         activity=activity,
                         stats=stats)

@app.route('/admin/logout')
def admin_logout():
    """Logout from admin"""
    session.pop('admin_authenticated', None)
    return redirect(url_for('admin'))

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

    # Check if user exists
    if username in users:
        if users[username] != password:
            emit('login_response', {'success': False, 'message': 'Incorrect password'})
            return
    else:
        # Create new user
        if not create_user(username, password):
            emit('login_response', {'success': False, 'message': 'Username already taken'})
            return

    # Store user session
    session['username'] = username
    connected_users[request.sid] = username
    
    # Log activity
    log_activity(username, 'login')

    print(f"[SERVER] {username} logged in")
    
    # Get unread count
    unread_count = get_unread_count(username)
    
    emit('login_response', {
        'success': True, 
        'message': f'Welcome {username}!',
        'unread_count': unread_count
    })
    
    # Send chat history to the user
    chat_history = load_chat_history()
    emit('chat_history', {'messages': chat_history, 'unread_count': unread_count})
    
    # Mark messages as read
    mark_messages_read(username)
    
    # Notify all users
    socketio.emit('user_joined', {
        'username': username,
        'message': f'{username} joined the chat'
    })
    save_message('system', 'server', f'{username} joined the chat')

@socketio.on('message')
def handle_message(data):
    if request.sid not in connected_users:
        emit('error', {'message': 'Not logged in'})
        return

    username = connected_users[request.sid]
    msg = data.get('message', '').strip()

    if not msg:
        return

    timestamp = datetime.now().strftime('%H:%M:%S')

    # Check for private message format: @username: message
    if msg.startswith("@"):
        try:
            recipient, content = msg[1:].split(":", 1)
            recipient = recipient.strip()
            content = content.strip()

            # Find recipient's session ID
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
                
                emit('private_message_sent', {
                    'to': recipient,
                    'message': content,
                    'timestamp': timestamp
                })
                
                save_message('private', username, content, recipient)
                log_activity(username, f'sent private message to {recipient}')
            else:
                emit('error', {'message': f'User {recipient} not found or offline'})
        except ValueError:
            emit('error', {'message': 'Invalid format. Use @username: message'})
    else:
        # Broadcast message
        socketio.emit('message', {
            'username': username,
            'message': msg,
            'timestamp': timestamp
        })
        save_message('public', username, msg)
        log_activity(username, 'sent public message')

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in connected_users:
        username = connected_users[request.sid]
        del connected_users[request.sid]
        print(f"[SERVER] {username} disconnected")
        socketio.emit('user_left', {
            'username': username,
            'message': f'{username} left the chat'
        })
        save_message('system', 'server', f'{username} left the chat')
        log_activity(username, 'logout')

if __name__ == '__main__':
    # Initialize database on startup
    init_db()
    
    port = int(os.getenv('PORT', 10000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
