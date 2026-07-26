import os
from datetime import datetime, timedelta, timezone
from queue import Empty, Queue
from sqlite3 import dbapi2 as sqlite3

from flask import Flask, g, redirect, render_template, request, session, url_for, jsonify, Response
from werkzeug.security import generate_password_hash, check_password_hash


def create_app(test_config=None):
    app = Flask(__name__)
    app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key')
    app.config['DATABASE_URL'] = os.environ.get('DATABASE_URL', 'sqlite:///app.db')
    app.config['STREAM_MAX_QUEUE'] = int(os.environ.get('STREAM_MAX_QUEUE', '8'))
    app.config['BOARD_TOKEN'] = os.environ.get('BOARD_TOKEN', 'dev-board-token')

    if test_config:
        app.config.update(test_config)

    def get_db():
        if 'db' not in g:
            database_url = app.config.get('DATABASE_URL')
            if database_url.startswith('sqlite:///'):
                path = database_url.replace('sqlite:///', '', 1)
                conn = sqlite3.connect(path)
            else:
                raise ValueError('Only SQLite is supported in this prototype')
            conn.row_factory = sqlite3.Row
            g.db = conn
        return g.db

    @app.teardown_appcontext
    def close_db(error):
        db = g.pop('db', None)
        if db is not None:
            db.close()

    def ensure_column(table_name, column_name, definition):
        db = get_db()
        columns = [row['name'] for row in db.execute(f'PRAGMA table_info({table_name})')]
        if column_name not in columns:
            db.execute(f'ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}')
            db.commit()

    def init_db():
        with app.app_context():
            db = get_db()
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    balance INTEGER DEFAULT 0,
                    is_admin INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL,
                    location TEXT NOT NULL,
                    status TEXT DEFAULT 'available',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    device_id INTEGER NOT NULL,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TEXT NOT NULL,
                    status TEXT DEFAULT 'active'
                );
            ''')
            ensure_column('users', 'is_admin', 'INTEGER DEFAULT 0')
            ensure_column('users', 'balance', 'INTEGER DEFAULT 0')
            db.commit()

    def refresh_sessions():
        db = get_db()
        now = datetime.now(timezone.utc)
        rows = db.execute("SELECT id, device_id FROM sessions WHERE status = 'active' AND expires_at <= ?", (now.strftime('%Y-%m-%d %H:%M:%S'),)).fetchall()
        for row in rows:
            db.execute("UPDATE sessions SET status = 'expired' WHERE id = ?", (row['id'],))
            db.execute("UPDATE devices SET status = 'available' WHERE id = ?", (row['device_id'],))
        if rows:
            db.commit()

    def format_seconds(total_seconds):
        total_seconds = max(0, int(total_seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f'{hours:02d}:{minutes:02d}:{seconds:02d}'
        return f'{minutes:02d}:{seconds:02d}'

    def get_user_view(user_id):
        db = get_db()
        user = db.execute('SELECT id, username, balance, is_admin FROM users WHERE id = ?', (user_id,)).fetchone()
        return user

    def get_active_session(user_id):
        db = get_db()
        return db.execute('''
            SELECT s.id, s.device_id, s.expires_at, d.name
            FROM sessions s
            JOIN devices d ON d.id = s.device_id
            WHERE s.user_id = ? AND s.status = 'active'
            ORDER BY s.id DESC LIMIT 1
        ''', (user_id,)).fetchone()

    def get_remaining_seconds(user_id, active_session=None):
        if active_session is None:
            active_session = get_active_session(user_id)
        if active_session is not None:
            expires_at = datetime.strptime(active_session['expires_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return max(0, int((expires_at - now).total_seconds()))
        user = get_user_view(user_id)
        return max(0, int(user['balance'])) if user else 0

    init_db()

    stream_queue = Queue(maxsize=app.config['STREAM_MAX_QUEUE'])
    latest_stream_chunk = None
    pending_commands = Queue()
    pipeline_frame_counter = 0

    @app.before_request
    def ensure_session_state():
        if 'user_id' in session:
            refresh_sessions()

            timeout_seconds = app.config.get('SESSION_TIMEOUT_SECONDS', 1800)
            now = datetime.now(timezone.utc)
            last_activity = session.get('last_activity')

            if last_activity is None:
                session['last_activity'] = now.isoformat()
                return None

            try:
                if isinstance(last_activity, str):
                    last_activity_dt = datetime.fromisoformat(last_activity.replace('Z', '+00:00'))
                else:
                    last_activity_dt = datetime.fromtimestamp(float(last_activity), tz=timezone.utc)
                if last_activity_dt.tzinfo is None:
                    last_activity_dt = last_activity_dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                session['last_activity'] = now.isoformat()
                return None

            if (now - last_activity_dt).total_seconds() > timeout_seconds:
                session.clear()
                if request.path not in ('/login', '/register', '/logout') and not request.path.startswith('/static'):
                    return redirect(url_for('login'))

            session['last_activity'] = now.isoformat()

    @app.route('/')
    def index():
        if 'user_id' in session:
            db = get_db()
            user = get_user_view(session['user_id'])
            active_session = get_active_session(session['user_id'])
            remaining_seconds = get_remaining_seconds(session['user_id'], active_session)
            return render_template('dashboard.html', user=user, active_session=active_session, remaining_seconds=remaining_seconds)
        return redirect(url_for('login'))

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            username = request.form['username']
            email = request.form['email']
            password = request.form['password']
            db = get_db()
            try:
                user_count = db.execute('SELECT COUNT(*) as count FROM users').fetchone()['count']
                is_admin = 1 if user_count == 0 else 0
                db.execute(
                    'INSERT INTO users (username, email, password_hash, balance, is_admin) VALUES (?, ?, ?, ?, ?)',
                    (username, email, generate_password_hash(password), 600, is_admin)
                )
                db.commit()
                user = db.execute('SELECT id, username FROM users WHERE username = ?', (username,)).fetchone()
                session['user_id'] = user['id']
                session['last_activity'] = datetime.now(timezone.utc).isoformat()
                return redirect(url_for('index'))
            except sqlite3.IntegrityError:
                return render_template('register.html', error='Username or email already exists')
        return render_template('register.html')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            username = request.form['username']
            password = request.form['password']
            db = get_db()
            user = db.execute('SELECT id, username, password_hash FROM users WHERE username = ?', (username,)).fetchone()
            if user and check_password_hash(user['password_hash'], password):
                session['user_id'] = user['id']
                session['last_activity'] = datetime.now(timezone.utc).isoformat()
                return redirect(url_for('index'))
            return render_template('login.html', error='Invalid username or password')
        return render_template('login.html')

    @app.route('/logout')
    def logout():
        session.pop('user_id', None)
        session.pop('last_activity', None)
        return redirect(url_for('login'))

    @app.route('/request_car', methods=['POST'])
    def request_car():
        if 'user_id' not in session:
            return redirect(url_for('login'))

        db = get_db()
        user = get_user_view(session['user_id'])
        duration_seconds = 300
        active_session = get_active_session(session['user_id'])
        remaining_seconds = get_remaining_seconds(session['user_id'], active_session)
        if active_session:
            return render_template(
                'dashboard.html',
                user=user,
                error='You already have an active session',
                active_session=active_session,
                remaining_seconds=remaining_seconds,
            )
        if user['balance'] < duration_seconds:
            return render_template('dashboard.html', user=user, error='Not enough credit', active_session=active_session, remaining_seconds=remaining_seconds)

        device = db.execute('SELECT id, name FROM devices WHERE status = ? ORDER BY id LIMIT 1', ('available',)).fetchone()
        if not device:
            return render_template('dashboard.html', user=user, error='No cars available right now', active_session=active_session, remaining_seconds=remaining_seconds)

        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
        db.execute(
            'INSERT INTO sessions (user_id, device_id, expires_at) VALUES (?, ?, ?)',
            (user['id'], device['id'], expires_at)
        )
        db.execute('UPDATE devices SET status = ? WHERE id = ?', ('busy', device['id']))
        db.execute('UPDATE users SET balance = balance - ? WHERE id = ?', (duration_seconds, user['id']))
        db.commit()
        user = get_user_view(session['user_id'])
        active_session = get_active_session(session['user_id'])
        remaining_seconds = get_remaining_seconds(session['user_id'], active_session)
        return render_template('dashboard.html', user=user, success='Control session started', active_session=active_session, remaining_seconds=remaining_seconds)

    @app.route('/control')
    def control():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        db = get_db()
        session_row = get_active_session(session['user_id'])
        return render_template('control.html', session=session_row)

    @app.route('/release_car', methods=['POST'])
    def release_car():
        if 'user_id' not in session:
            return redirect(url_for('login'))

        db = get_db()
        active_session = get_active_session(session['user_id'])
        if active_session:
            now = datetime.now(timezone.utc)
            expires_at = datetime.strptime(active_session['expires_at'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            refundable_seconds = max(0, int((expires_at - now).total_seconds()))
            db.execute("UPDATE sessions SET status = 'released' WHERE id = ?", (active_session['id'],))
            db.execute("UPDATE devices SET status = 'available' WHERE id = ?", (active_session['device_id'],))
            if refundable_seconds > 0:
                db.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (refundable_seconds, session['user_id']))
            db.commit()
        return redirect(url_for('index'))

    @app.route('/admin', methods=['GET', 'POST'])
    def admin():
        if 'user_id' not in session:
            return redirect(url_for('login'))

        db = get_db()
        requester = get_user_view(session['user_id'])
        if not requester or not requester['is_admin']:
            return redirect(url_for('index'))

        if request.method == 'POST':
            action = request.form.get('action')
            target_user_id = request.form.get('user_id')
            target_user = db.execute('SELECT id, username, is_admin FROM users WHERE id = ?', (target_user_id,)).fetchone()
            if action == 'set_admin':
                if target_user and target_user['username'] != 'admin':
                    is_admin = 1 if request.form.get('is_admin') == '1' else 0
                    db.execute('UPDATE users SET is_admin = ? WHERE id = ?', (is_admin, target_user_id))
            elif action == 'adjust_time':
                delta_seconds = int(request.form.get('seconds', 0))
                if request.form.get('direction') == 'add':
                    db.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (delta_seconds, target_user_id))
                else:
                    db.execute('UPDATE users SET balance = balance - ? WHERE id = ?', (delta_seconds, target_user_id))
            db.commit()

        users = db.execute('SELECT id, username, balance, is_admin FROM users ORDER BY username').fetchall()
        devices = db.execute('SELECT id, name, status, location FROM devices ORDER BY id').fetchall()
        return render_template('admin.html', users=users, devices=devices, format_seconds=format_seconds)

    @app.route('/api/devices/register', methods=['POST'])
    def register_device():
        payload = request.get_json(silent=True) or {}
        name = payload.get('name')
        kind = payload.get('kind', 'arduino')
        location = payload.get('location', 'unknown')
        if not name:
            return jsonify({'status': 'error', 'message': 'name is required'}), 400

        db = get_db()
        db.execute('INSERT OR IGNORE INTO devices (name, kind, location, status) VALUES (?, ?, ?, ?)', (name, kind, location, 'available'))
        db.commit()
        device = db.execute('SELECT id, name, kind, location, status FROM devices WHERE name = ?', (name,)).fetchone()
        return jsonify({'status': 'registered', 'device': dict(device)})

    @app.route('/api/control', methods=['POST'])
    def api_control():
        payload = request.get_json(silent=True) or {}
        action = payload.get('action')
        if not action:
            return jsonify({'status': 'error', 'message': 'action is required'}), 400

        pending_commands.put(action)
        return jsonify({'status': 'ok', 'action': action})

    @app.route('/api/board/command')
    def board_command():
        token = request.args.get('token')
        if token != app.config['BOARD_TOKEN']:
            return jsonify({'status': 'error', 'message': 'invalid token'}), 403

        try:
            action = pending_commands.get_nowait()
        except Empty:
            return jsonify({'status': 'ok', 'action': None})
        return jsonify({'status': 'ok', 'action': action})

    @app.route('/api/stream/chunk', methods=['POST'])
    def api_stream_chunk():
        nonlocal latest_stream_chunk
        data = request.get_data()
        if not data:
            return jsonify({'status': 'error', 'message': 'chunk data is required'}), 400

        latest_stream_chunk = data
        if stream_queue.full():
            try:
                stream_queue.get_nowait()
            except Empty:
                pass
        stream_queue.put(data)
        return jsonify({'status': 'ok', 'received': len(data)})

    @app.route('/api/video/pipeline/frame', methods=['POST'])
    def api_video_pipeline_frame():
        nonlocal latest_stream_chunk, pipeline_frame_counter
        data = request.get_data()
        if not data:
            return jsonify({'status': 'error', 'message': 'frame data is required'}), 400

        pipeline_frame_counter += 1
        latest_stream_chunk = data
        if stream_queue.full():
            try:
                stream_queue.get_nowait()
            except Empty:
                pass
        stream_queue.put(data)
        return jsonify({'status': 'ok', 'received': len(data), 'frame_id': pipeline_frame_counter})

    @app.route('/video_feed')
    def video_feed():
        mode = request.args.get('mode', 'stream')

        def generate_stream():
            while True:
                try:
                    chunk = stream_queue.get(timeout=0.25)
                except Empty:
                    continue
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + chunk + b'\r\n')

        if mode == 'single':
            if latest_stream_chunk is None:
                return Response(b'', status=204, mimetype='application/octet-stream')
            return Response(latest_stream_chunk, mimetype='image/jpeg')

        return Response(generate_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

    @app.route('/video/pipeline/stream')
    def video_pipeline_stream():
        def generate_stream():
            while True:
                try:
                    chunk = stream_queue.get(timeout=0.25)
                except Empty:
                    continue
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + chunk + b'\r\n')

        return Response(generate_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

    return app


app = create_app()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
