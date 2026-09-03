from flask import Flask, render_template, request, session
from flask_socketio import SocketIO, emit, join_room, leave_room
import os
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-secret-key-change-in-production')
socketio = SocketIO(app, cors_allowed_origins="*")

# File paths
user_file = "users.txt"
chat_log = "chat_history.txt"

# In-memory storage of connected users
connected_users = {}  # session_id -> username

# Ensure files exist
if not os.path.exists(user_file):
    open(user_file, 'w').close()

if not os.path.exists(chat_log):
    open(chat_log, 'w').close()

def load_users():
    users = {}
    with open(user_file, 'r') as f:
        for line in f:
            if ":" in line:
                uname, pwd = line.strip().split(":", 1)
                users[uname] = pwd
    return users

def log_message(msg):
    with open(chat_log, 'a') as f:
        f.write(msg + '\n')

@app.route('/')
def index():
    return render_template('index.html')

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
        with open(user_file, 'a') as f:
            f.write(f"{username}:{password}\n")

    # Store user session
    session['username'] = username
    connected_users[request.sid] = username

    print(f"[SERVER] {username} logged in")
    emit('login_response', {'success': True, 'message': f'Welcome {username}!'})
    
    # Notify all users
    socketio.emit('user_joined', {
        'username': username,
        'message': f'{username} joined the chat'
    })

@socketio.on('message')
def handle_message(data):
    if request.sid not in connected_users:
        emit('error', {'message': 'Not logged in'})
        return

    username = connected_users[request.sid]
    msg = data.get('message', '').strip()

    if not msg:
        return

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
                    'message': content
                }, to=recipient_sid)
                
                emit('private_message_sent', {
                    'to': recipient,
                    'message': content
                })
                
                log_message(f"[DM] {username} -> {recipient}: {content}")
            else:
                emit('error', {'message': f'User {recipient} not found or offline'})
        except ValueError:
            emit('error', {'message': 'Invalid format. Use @username: message'})
    else:
        # Broadcast message
        full_msg = f"{username}: {msg}"
        socketio.emit('message', {
            'username': username,
            'message': msg,
            'timestamp': datetime.now().strftime('%H:%M:%S')
        })
        log_message(full_msg)

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

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
