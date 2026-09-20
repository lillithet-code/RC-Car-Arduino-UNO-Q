"""Email verification and single-use password recovery for website accounts."""
import hashlib
import hmac
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urlparse

from flask import abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


MAIL_DEFAULTS = {
    'ACCOUNT_BASE_URL': 'https://drive.kbob.org', 'SMTP_HOST': '', 'SMTP_PORT': '587',
    'SMTP_USERNAME': '', 'SMTP_PASSWORD': '', 'SMTP_FROM': '', 'SMTP_SECURITY': 'starttls',
}


def email_address(value):
    value = value.strip().lower()
    return value if len(value) <= 254 and re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', value) else None


class AccountMailError(Exception):
    pass


class AccountMail:
    def __init__(self, app):
        self.app = app

    def send(self, recipient, subject, body):
        message = EmailMessage()
        message['From'] = self.app.config['SMTP_FROM']
        message['To'] = recipient
        message['Subject'] = subject
        message.set_content(body)
        # Only tests can intercept mail. Production never logs passwords or links.
        if self.app.testing and self.app.config.get('ACCOUNT_MAIL_SENDER'):
            self.app.config['ACCOUNT_MAIL_SENDER'](message)
            return
        c = self.app.config
        if not c['SMTP_HOST'] or not email_address(c['SMTP_FROM']):
            raise AccountMailError('Mail is not configured')
        if c['SMTP_SECURITY'] not in ('starttls', 'ssl'):
            raise AccountMailError('SMTP_SECURITY must be starttls or ssl')
        try:
            context = ssl.create_default_context()
            smtp_type = smtplib.SMTP_SSL if c['SMTP_SECURITY'] == 'ssl' else smtplib.SMTP
            options = {'context': context} if c['SMTP_SECURITY'] == 'ssl' else {}
            with smtp_type(c['SMTP_HOST'], int(c['SMTP_PORT']), timeout=10, **options) as smtp:
                if c['SMTP_SECURITY'] == 'starttls':
                    smtp.starttls(context=context)
                if c['SMTP_USERNAME']:
                    smtp.login(c['SMTP_USERNAME'], c['SMTP_PASSWORD'])
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            raise AccountMailError('Mail delivery failed') from exc


def init_accounts(app, get_db, release_control):
    for key, default in MAIL_DEFAULTS.items():
        app.config.setdefault(key, os.environ.get(key, default))
    mail = AccountMail(app)
    app.extensions['account_mail'] = mail
    with app.app_context():
        db = get_db()
        db.executescript('''
            CREATE TABLE IF NOT EXISTS account_tokens (
                token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                purpose TEXT NOT NULL, expires_at INTEGER NOT NULL,
                auth_version INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS account_tokens_user ON account_tokens(user_id);
            CREATE TABLE IF NOT EXISTS account_mail_limits (
                bucket TEXT PRIMARY KEY, window INTEGER NOT NULL, attempts INTEGER NOT NULL
            );
        ''')
        db.commit()

    def csrf_token():
        if 'account_csrf' not in session:
            session['account_csrf'] = secrets.token_hex(32)
        return session['account_csrf']

    def check_csrf():
        if not hmac.compare_digest(request.form.get('csrf_token', ''), session.get('account_csrf', '') or '!'):
            abort(403)

    app.jinja_env.globals['account_csrf'] = csrf_token

    def page(kind, **kwargs):
        return render_template('account_action.html', kind=kind, **kwargs)

    def limited(action, email):
        # Database-backed limits work across gunicorn threads/workers and restarts.
        window = int(time.time()) // 3600
        keys = [(action + ':ip:' + (request.remote_addr or 'unknown'), 20),
                (action + ':email:' + email, 5)]
        db = get_db()
        with db:
            db.execute('DELETE FROM account_mail_limits WHERE window < ?', (window - 1,))
            blocked = False
            for key, maximum in keys:
                bucket = hashlib.sha256(key.encode()).hexdigest()
                db.execute('''INSERT INTO account_mail_limits VALUES (?, ?, 1)
                    ON CONFLICT(bucket) DO UPDATE SET
                    attempts = CASE WHEN window = excluded.window THEN attempts + 1 ELSE 1 END,
                    window = excluded.window''', (bucket, window))
                attempts = db.execute('SELECT attempts FROM account_mail_limits WHERE bucket=?', (bucket,)).fetchone()[0]
                blocked = blocked or attempts > maximum
        return blocked

    def send_link(user, purpose):
        base = app.config['ACCOUNT_BASE_URL'].strip().rstrip('/')
        parsed = urlparse(base)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
            raise AccountMailError('ACCOUNT_BASE_URL must be a public HTTPS URL')
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        ttl = 86400 if purpose == 'verify' else 1800
        db = get_db()
        with db:
            db.execute('DELETE FROM account_tokens WHERE expires_at <= ?', (int(time.time()),))
            db.execute('INSERT INTO account_tokens VALUES (?, ?, ?, ?, ?)',
                       (digest, user['id'], purpose, int(time.time()) + ttl, user['auth_version']))
        route = 'confirm_email' if purpose == 'verify' else 'reset_password'
        link = base + url_for(route, token=token)
        if purpose == 'verify':
            subject = 'Confirm your RC Car email address'
            body = f'Confirm your email address to enable your RC Car account:\n\n{link}\n\nThis link expires in 24 hours.\n\nIf you did not create this account, ignore this email.'
        else:
            subject = 'Reset your RC Car password'
            body = f'Choose a new password for your RC Car account:\n\n{link}\n\nThis link expires in 30 minutes and can be used once.\n\nIf you did not request this, ignore this email. Your password has not changed.'
        try:
            mail.send(user['email'], subject, body)
        except (AccountMailError, ValueError):
            with db:
                db.execute('DELETE FROM account_tokens WHERE token_hash=?', (digest,))
            app.logger.warning('Account email delivery failed (%s); check SMTP configuration.', purpose)
            raise AccountMailError('Mail delivery failed')

    def find_token(token, purpose):
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return None
        return get_db().execute('''SELECT t.*, u.email_verified FROM account_tokens t
            JOIN users u ON u.id=t.user_id AND u.auth_version=t.auth_version
            WHERE t.token_hash=? AND t.purpose=? AND t.expires_at>?''',
            (hashlib.sha256(token.encode()).hexdigest(), purpose, int(time.time()))).fetchone()

    @app.before_request
    def enforce_account_state():
        if request.endpoint == 'payments.stripe_webhook':
            return None
        if session.get('user_id'):
            user = get_db().execute('SELECT email_verified, auth_version FROM users WHERE id=?',
                                    (session['user_id'],)).fetchone()
            if not user or not user['email_verified'] or session.get('auth_version', 0) != user['auth_version']:
                session.clear()
                if request.endpoint in ('register', 'login', 'forgot_password', 'resend_confirmation', 'confirm_email', 'reset_password'):
                    return None
                if request.path.startswith('/api/'):
                    abort(401)
                return redirect(url_for('login'))

    @app.after_request
    def protect_account_pages(response):
        if request.endpoint in ('register', 'login', 'forgot_password', 'resend_confirmation', 'confirm_email', 'reset_password'):
            response.headers['Cache-Control'] = 'no-store'
            response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            check_csrf()
            username = request.form.get('username', '').strip()
            email = email_address(request.form.get('email', ''))
            password = request.form.get('password', '')
            if not username or len(username) > 80 or not email or not 8 <= len(password) <= 128:
                return render_template('register.html', error='Enter a username, valid email, and a password of 8–128 characters.'), 400
            if limited('verify', email):
                return render_template('register.html', error='Too many requests. Please try again in an hour.'), 429
            password_hash = generate_password_hash(password)
            db = get_db()
            try:
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    if db.execute('SELECT 1 FROM users WHERE lower(email)=?', (email,)).fetchone():
                        raise sqlite3.IntegrityError()
                    first_user = db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0
                    cursor = db.execute('''INSERT INTO users
                        (username,email,password_hash,balance,is_admin,email_verified,auth_version)
                        VALUES (?, ?, ?, 0, ?, 0, 0)''', (username, email, password_hash, int(first_user)))
                user = db.execute('SELECT * FROM users WHERE id=?', (cursor.lastrowid,)).fetchone()
            except sqlite3.IntegrityError:
                return render_template('register.html', error='Username or email already exists. Try signing in or requesting a new confirmation email.'), 400
            try:
                send_link(user, 'verify')
            except AccountMailError:
                return page('notice', message='Your account was created, but the confirmation email could not be sent. Your account remains disabled. Use Resend confirmation once email delivery is available.'), 503
            flash('Confirm your email to enable your account.', 'registration')
            return redirect(url_for('login'), code=303)
        return render_template('register.html')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            check_csrf()
            user = get_db().execute('SELECT * FROM users WHERE username=?',
                                    (request.form.get('username', '').strip(),)).fetchone()
            password = request.form.get('password', '')
            if user and len(password) <= 128 and check_password_hash(user['password_hash'], password):
                if not user['email_verified']:
                    return render_template('login.html', error='Confirm your email before signing in. Use Resend confirmation if you need a new link.'), 403
                session.clear()
                session['user_id'] = user['id']
                session['auth_version'] = user['auth_version']
                session['last_activity'] = datetime.now(timezone.utc).isoformat()
                return redirect(url_for('index'))
            return render_template('login.html', error='Invalid username or password')
        return render_template('login.html')

    def request_email(purpose):
        kind = 'forgot' if purpose == 'reset' else 'resend'
        if request.method == 'POST':
            check_csrf()
            email = email_address(request.form.get('email', ''))
            if email and not limited(purpose, email):
                user = get_db().execute('SELECT * FROM users WHERE lower(email)=?', (email,)).fetchone()
                if user and (purpose == 'reset' or not user['email_verified']):
                    try:
                        send_link(user, purpose)
                    except AccountMailError:
                        pass
            # Same response for unknown addresses, rate limits and delivery failures.
            return page(kind, message='If an eligible account matches that email, we will send a link. Check your inbox and spam folder. If it does not arrive, try again later or contact the administrator.')
        return page(kind)

    @app.route('/forgot-password', methods=['GET', 'POST'])
    def forgot_password():
        return request_email('reset')

    @app.route('/resend-confirmation', methods=['GET', 'POST'])
    def resend_confirmation():
        return request_email('verify')

    @app.route('/confirm-email/<token>', methods=['GET', 'POST'])
    def confirm_email(token):
        db = get_db()
        if request.method == 'POST':
            check_csrf()
            with db:
                db.execute('BEGIN IMMEDIATE')
                record = find_token(token, 'verify')
                if not record or record['email_verified']:
                    return page('invalid', message='This confirmation link is invalid, expired, or already used.'), 400
                db.execute('UPDATE users SET email_verified=1 WHERE id=?', (record['user_id'],))
                db.execute("DELETE FROM account_tokens WHERE user_id=? AND purpose='verify'", (record['user_id'],))
            return page('notice', message='Email confirmed. Your account is enabled. You can now sign in.')
        if not find_token(token, 'verify'):
            return page('invalid', message='This confirmation link is invalid, expired, or already used.'), 400
        return page('confirm')

    @app.route('/reset-password/<token>', methods=['GET', 'POST'])
    def reset_password(token):
        if request.method == 'POST':
            check_csrf()
        if not find_token(token, 'reset'):
            return page('invalid', message='This reset link is invalid, expired, or already used.'), 400
        if request.method == 'POST':
            password = request.form.get('password', '')
            if not 8 <= len(password) <= 128 or password != request.form.get('password_confirm'):
                return page('reset', error='Enter matching passwords of 8–128 characters.'), 400
            password_hash = generate_password_hash(password)
            db = get_db()
            with db:
                db.execute('BEGIN IMMEDIATE')
                record = find_token(token, 'reset')
                if not record:
                    return page('invalid', message='This reset link is invalid, expired, or already used.'), 400
                db.execute('UPDATE users SET password_hash=?, auth_version=auth_version+1 WHERE id=?',
                           (password_hash, record['user_id']))
                db.execute('DELETE FROM account_tokens WHERE user_id=?', (record['user_id'],))
            release_control(record['user_id'], new_status='password_reset')
            session.clear()
            return page('notice', message='Your password has been changed. Sign in with your new password. Email confirmation is still required if you have not confirmed your account yet.')
        return page('reset')
