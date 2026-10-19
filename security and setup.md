# Messaging app security update

## What changed
- Passwords are stored using Werkzeug password hashes; existing SQLite plaintext passwords are upgraded on initialization.
- Removed insecure default production secret key and default admin password. Production startup fails unless strong secrets are configured.
- Admin now uses a username/password, generic login errors, rate limiting, CSRF tokens, and POST-only logout.
- Replaced unsafe chat `innerHTML` insertion with DOM text nodes and `textContent`.
- Media upload requires a short-lived token issued after chat login; uploads are limited to 8 MiB, checked against allowed extension/MIME/signature, and rate limited.
- Socket.IO is configured for same-origin connections rather than wildcard CORS.
- Added username and message length checks, safer session cookie settings, security headers, and removed unused Firebase Analytics code.
- Excluded the legacy `users.txt` and `chat_history.txt` files from this package because they may contain sensitive user data and are not used by the current SQLite-backed server.

## Configure and run
1. Back up your existing SQLite database before deploying this version. The app will migrate legacy plaintext passwords in that database to hashes on startup.
2. Install dependencies: `pip install -r requirements.txt`
3. Configure environment variables. For production, set `APP_ENV=production`, `SECRET_KEY` to a cryptographically random secret, `ADMIN_USERNAME`, `ADMIN_PASSWORD` (at least 14 characters), and the Cloudinary credentials if uploads are needed.
4. Generate a secret with: `python -c "import secrets; print(secrets.token_urlsafe(48))"`
5. Run locally with `python server.py`. For production, use a production Flask-SocketIO deployment (eventlet or gevent), TLS/reverse proxy, persistent storage, and `APP_ENV=production`. Do not expose the development Werkzeug server directly to the internet.

## Important notes
- The app retains its existing first-login account-creation behavior. For a public-facing service, add a distinct registration flow with email verification or another account ownership check before launch.
- Rate limits are in-memory and apply per process. Use a shared limiter (for example Redis) for multi-worker production deployments.
- SQLite remains suitable for small single-instance deployments; move to PostgreSQL for multi-instance/high-write production use.
- Set `SESSION_COOKIE_SECURE` in production only when the site is served over HTTPS.
- Configure backups and a password-reset process before public launch.
