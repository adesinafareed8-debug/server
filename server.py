import socket
import threading
import os

# Use Render's PORT environment variable, default to 10000
PORT = int(os.getenv('PORT', 10000))
HOST = '0.0.0.0'  # Listen on all interfaces

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Allow port reuse
server.bind((HOST, PORT))
server.listen()

clients = {}  # socket -> username
user_file = "users.txt"
chat_log = "chat_history.txt"

# Ensure users.txt exists
if not os.path.exists(user_file):
    open(user_file, 'w').close()

# Ensure chat_history.txt exists
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

def private_message(sender, recipient, message):
    for client, uname in clients.items():
        if uname == recipient:
            try:
                client.send(f"[DM from {sender}]: {message}".encode('utf-8'))
                return True
            except:
                return False
    return False

def broadcast(message, exclude=None):
    for client in clients:
        if client != exclude:
            try:
                client.send(message.encode('utf-8'))
            except:
                client.close()
                if client in clients:
                    del clients[client]

def handle_client(client):
    try:
        client.send("Username: ".encode('utf-8'))
        username = client.recv(1024).decode('utf-8').strip()

        client.send("Password: ".encode('utf-8'))
        password = client.recv(1024).decode('utf-8').strip()

        users = load_users()

        if username in users:
            if users[username] != password:
                client.send("Incorrect password.\n".encode('utf-8'))
                client.close()
                return
        else:
            with open(user_file, 'a') as f:
                f.write(f"{username}:{password}\n")

        clients[client] = username
        print(f"[SERVER] {username} connected.")
        broadcast(f"[SERVER] {username} joined the chat.", client)
        client.send("Connected! Type @user: message for private messages.\n".encode('utf-8'))

        while True:
            msg = client.recv(1024).decode('utf-8').strip()
            if msg == "":
                continue

            if msg.startswith("@"):
                try:
                    recipient, content = msg[1:].split(":", 1)
                    success = private_message(username, recipient.strip(), content.strip())
                    if not success:
                        client.send("User not found or offline.\n".encode('utf-8'))
                    else:
                        log_message(f"[DM] {username} -> {recipient.strip()}: {content.strip()}")
                except ValueError:
                    client.send("Invalid format. Use @username: message\n".encode('utf-8'))
            else:
                full_msg = f"{username}: {msg}"
                broadcast(full_msg, client)
                log_message(full_msg)

    except:
        pass
    finally:
        if client in clients:
            uname = clients[client]
            print(f"[SERVER] {uname} disconnected.")
            broadcast(f"[SERVER] {uname} has left.")
            del clients[client]
            client.close()

def start():
    print(f"[SERVER] Listening on {HOST}:{PORT}...")
    while True:
        client, addr = server.accept()
        threading.Thread(target=handle_client, args=(client,), daemon=True).start()

if __name__ == "__main__":
    start()
