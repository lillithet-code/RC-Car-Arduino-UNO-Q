import os
import asyncio
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from queue import Empty, Full, Queue
from sqlite3 import dbapi2 as sqlite3
from urllib import error as urllib_error
from urllib import request as urllib_request

from flask import Flask, g, redirect, render_template, request, session, url_for, jsonify, Response
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, RTCConfiguration, RTCIceServer
    from av import VideoFrame, CodecContext, Packet
    import cv2
    import numpy as np
    WEBRTC_AVAILABLE = True
except Exception:
    RTCPeerConnection = None
    RTCSessionDescription = None
    VideoStreamTrack = None
    RTCConfiguration = None
    RTCIceServer = None
    VideoFrame = None
    CodecContext = None
    Packet = None
    cv2 = None
    np = None
    WEBRTC_AVAILABLE = False


def create_app(test_config=None):
    app = Flask(__name__)
    app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key')
    app.config['DATABASE_URL'] = os.environ.get('DATABASE_URL', 'sqlite:///app.db')
    app.config['STREAM_MAX_QUEUE'] = int(os.environ.get('STREAM_MAX_QUEUE', '1'))
    app.config['BOARD_TOKEN'] = os.environ.get('BOARD_TOKEN', 'dev-board-token')
    app.config['SESSION_DURATION_SECONDS'] = int(os.environ.get('SESSION_DURATION_SECONDS', '300'))
    app.config['STREAM_STALE_SECONDS'] = float(os.environ.get('STREAM_STALE_SECONDS', '6.0'))
    app.config['STREAM_READY_WINDOW_SECONDS'] = max(1.0, float(os.environ.get('STREAM_READY_WINDOW_SECONDS', '2.0')))
    app.config['STREAM_READY_MIN_FRAMES'] = max(2, int(os.environ.get('STREAM_READY_MIN_FRAMES', '6')))
    app.config['STREAM_LOSS_GRACE_SECONDS'] = float(os.environ.get('STREAM_LOSS_GRACE_SECONDS', '30.0'))
    app.config['WEBRTC_TRACK_FPS'] = int(os.environ.get('WEBRTC_TRACK_FPS', '25'))
    app.config['STREAM_CROP_BOTTOM_PX'] = max(0, int(os.environ.get('STREAM_CROP_BOTTOM_PX', '2')))
    app.config['STREAM_JPEG_QUALITY'] = max(30, min(95, int(os.environ.get('STREAM_JPEG_QUALITY', '65'))))
    app.config['H264_INGEST_QUEUE_MAX'] = max(1, int(os.environ.get('H264_INGEST_QUEUE_MAX', '8')))
    app.config['STREAM_PROFILE'] = os.environ.get('STREAM_PROFILE', '0').strip().lower() in {'1', 'true', 'yes', 'on'}
    app.config['BOARD_POLL_URL'] = os.environ.get('BOARD_POLL_URL', '').strip()
    app.config['BOARD_POLL_INTERVAL_SECONDS'] = max(1.0, float(os.environ.get('BOARD_POLL_INTERVAL_SECONDS', '2.0')))
    app.config['BOARD_POLL_TIMEOUT_SECONDS'] = max(0.2, float(os.environ.get('BOARD_POLL_TIMEOUT_SECONDS', '1.5')))
    app.config['BOARD_ACTIVE_STALE_SECONDS'] = max(2.0, float(os.environ.get('BOARD_ACTIVE_STALE_SECONDS', '8.0')))

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
            ensure_column('sessions', 'billing_started_at', 'TEXT')
            ensure_column('sessions', 'allocated_seconds', "INTEGER DEFAULT 300")
            ensure_column('devices', 'poll_url', 'TEXT')
            ensure_column('devices', 'last_seen_at', 'TEXT')
            ensure_column('devices', 'last_poll_ok', 'INTEGER DEFAULT 0')
            ensure_column('devices', 'last_poll_error', 'TEXT')
            db.commit()

    def refresh_sessions():
        db = get_db()
        now = datetime.now(timezone.utc)
        last_stream_at = app_state.get('last_stream_at')
        active_rows = db.execute(
            "SELECT id, user_id, device_id, billing_started_at, allocated_seconds FROM sessions WHERE status = 'active'"
        ).fetchall()
        changed = False

        for row in active_rows:
            billing_started_at = parse_timestamp(row['billing_started_at'])
            if billing_started_at is None:
                continue

            allocated_seconds = max(0, int(row['allocated_seconds'] or app.config['SESSION_DURATION_SECONDS']))
            elapsed_seconds = max(0, int((now - billing_started_at).total_seconds()))
            remaining_seconds = max(0, allocated_seconds - elapsed_seconds)

            if remaining_seconds <= 0:
                db.execute("UPDATE sessions SET status = 'expired' WHERE id = ?", (row['id'],))
                db.execute("UPDATE devices SET status = 'available' WHERE id = ?", (row['device_id'],))
                changed = True
                continue

            # Only enforce stream-loss after at least one stream frame has been observed.
            if billing_started_at is not None and last_stream_at is not None:
                stream_gap_seconds = (now - last_stream_at).total_seconds()
            else:
                stream_gap_seconds = None

            if (
                stream_gap_seconds is not None
                and stream_gap_seconds > app.config['STREAM_LOSS_GRACE_SECONDS']
            ):
                db.execute("UPDATE sessions SET status = 'stream_lost' WHERE id = ?", (row['id'],))
                db.execute("UPDATE devices SET status = 'available' WHERE id = ?", (row['device_id'],))
                db.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (remaining_seconds, row['user_id']))
                changed = True

        if changed:
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
            SELECT s.id, s.device_id, s.expires_at, s.billing_started_at, s.allocated_seconds, d.name
            FROM sessions s
            JOIN devices d ON d.id = s.device_id
            WHERE s.user_id = ? AND s.status = 'active'
            ORDER BY s.id DESC LIMIT 1
        ''', (user_id,)).fetchone()

    def get_remaining_seconds(user_id, active_session=None):
        if active_session is None:
            active_session = get_active_session(user_id)
        if active_session is not None:
            allocated_seconds = max(0, int(active_session['allocated_seconds'] or app.config['SESSION_DURATION_SECONDS']))
            billing_started_at = parse_timestamp(active_session['billing_started_at'])
            if billing_started_at is None:
                return allocated_seconds
            now = datetime.now(timezone.utc)
            elapsed_seconds = max(0, int((now - billing_started_at).total_seconds()))
            return max(0, allocated_seconds - elapsed_seconds)
        user = get_user_view(user_id)
        return max(0, int(user['balance'])) if user else 0

    init_db()

    stream_queue = Queue(maxsize=app.config['STREAM_MAX_QUEUE'])
    latest_stream_chunk = None
    pending_commands = Queue()
    pipeline_frame_counter = 0
    app_state = {
        'last_stream_at': None,
        'stream_frame_times': deque(maxlen=300),
        'board_last_seen_at': None,
        'board_last_source': None,
        'board_last_poll_at': None,
        'board_poll_ok': None,
        'board_last_error': None,
    }
    peer_connections = set()
    h264_decoder = CodecContext.create('h264', 'r') if CodecContext is not None else None
    h264_parse_errors = 0
    h264_headers_seen = False
    h264_ingest_queue = Queue(maxsize=app.config['H264_INGEST_QUEUE_MAX'])

    webrtc_loop = None
    if WEBRTC_AVAILABLE:
        webrtc_loop = asyncio.new_event_loop()

        def run_webrtc_loop(loop):
            asyncio.set_event_loop(loop)
            loop.run_forever()

        threading.Thread(target=run_webrtc_loop, args=(webrtc_loop,), daemon=True).start()

    def run_webrtc(coro, timeout=10):
        if not WEBRTC_AVAILABLE or webrtc_loop is None:
            raise RuntimeError('WebRTC backend not available')
        future = asyncio.run_coroutine_threadsafe(coro, webrtc_loop)
        return future.result(timeout=timeout)

    def get_latest_stream_chunk():
        chunk = stream_queue.get(timeout=0.25)
        while True:
            try:
                chunk = stream_queue.get_nowait()
            except Empty:
                return chunk

    class StreamVideoTrack(VideoStreamTrack if WEBRTC_AVAILABLE else object):
        def __init__(self, frame_provider, fallback_width=640, fallback_height=480, fps=20):
            if WEBRTC_AVAILABLE:
                super().__init__()
            self._frame_provider = frame_provider
            self._fps = max(1, int(fps))
            self._frame_period = 1.0 / self._fps
            self._fallback_frame = None
            if WEBRTC_AVAILABLE and np is not None:
                self._fallback_frame = np.zeros((fallback_height, fallback_width, 3), dtype=np.uint8)

        async def recv(self):
            await asyncio.sleep(self._frame_period)

            chunk = self._frame_provider()
            frame_array = None
            if WEBRTC_AVAILABLE and chunk and cv2 is not None and np is not None:
                encoded = np.frombuffer(chunk, dtype=np.uint8)
                decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                if decoded is not None:
                    frame_array = decoded

            if frame_array is None:
                frame_array = self._fallback_frame

            frame = VideoFrame.from_ndarray(frame_array, format='bgr24')
            pts, time_base = await self.next_timestamp()
            frame.pts = pts
            frame.time_base = time_base
            return frame

    def parse_timestamp(raw_value):
        if not raw_value:
            return None
        return datetime.strptime(raw_value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)

    def format_timestamp(value):
        if value is None:
            return None
        return value.strftime('%Y-%m-%d %H:%M:%S')

    def resolve_board_name():
        board_name = (request.args.get('board_name') or request.headers.get('X-Board-Name') or '').strip()
        return board_name or None

    def is_stream_live(now=None):
        if now is None:
            now = datetime.now(timezone.utc)
        last_stream_at = app_state['last_stream_at']
        if last_stream_at is None:
            return False
        return (now - last_stream_at).total_seconds() <= app.config['STREAM_STALE_SECONDS']

    def is_stream_ready(now=None):
        if now is None:
            now = datetime.now(timezone.utc)
        if not is_stream_live(now):
            return False

        frame_times = app_state['stream_frame_times']
        if not frame_times:
            return False

        window_seconds = float(app.config['STREAM_READY_WINDOW_SECONDS'])
        cutoff = now - timedelta(seconds=window_seconds)
        recent_frames = sum(1 for ts in frame_times if ts >= cutoff)
        return recent_frames >= int(app.config['STREAM_READY_MIN_FRAMES'])

    def mark_stream_activity():
        now = datetime.now(timezone.utc)
        app_state['last_stream_at'] = now
        app_state['stream_frame_times'].append(now)

    def h264_chunk_has_parameter_sets(data):
        # Annex-B NAL scan for SPS (7) and PPS (8). We decode only after both are seen.
        saw_sps = False
        saw_pps = False
        i = 0
        length = len(data)
        while i + 4 <= length:
            if data[i:i + 3] == b'\x00\x00\x01':
                nal_start = i + 3
                i = nal_start
            elif i + 4 <= length and data[i:i + 4] == b'\x00\x00\x00\x01':
                nal_start = i + 4
                i = nal_start
            else:
                i += 1
                continue

            if nal_start >= length:
                break

            nal_type = data[nal_start] & 0x1F
            if nal_type == 7:
                saw_sps = True
            elif nal_type == 8:
                saw_pps = True

            if saw_sps and saw_pps:
                return True

        return False

    def decode_h264_chunk(data):
        nonlocal latest_stream_chunk, pipeline_frame_counter
        nonlocal h264_parse_errors, h264_decoder, h264_headers_seen

        has_parameter_sets = h264_chunk_has_parameter_sets(data)
        if has_parameter_sets:
            # Fresh SPS/PPS boundary: reset decoder state to avoid stale refs.
            h264_headers_seen = True
            if CodecContext is not None:
                try:
                    h264_decoder = CodecContext.create('h264', 'r')
                except Exception:
                    pass

        if not h264_headers_seen:
            # Skip decode until a self-describing chunk arrives.
            return False, 0

        decoded_any = False
        decoded_frames = 0
        latest_decoded_frame = None
        packets = []
        try:
            parsed_packets = h264_decoder.parse(data)
            if parsed_packets:
                packets.extend(parsed_packets)
        except Exception:
            # Parsing can fail on partial chunk boundaries; keep ingesting future chunks.
            h264_parse_errors += 1
            if h264_parse_errors <= 3 or h264_parse_errors % 100 == 0:
                app.logger.warning('h264 parse failed on chunk; waiting for more data')

        if not packets:
            try:
                packets = [Packet(data)]
            except Exception:
                packets = []

        for packet in packets:
            try:
                frames = h264_decoder.decode(packet)
            except Exception:
                continue
            for frame in frames:
                decoded_frames += 1
                latest_decoded_frame = frame

        if latest_decoded_frame is not None:
            frame_bgr = latest_decoded_frame.to_ndarray(format='bgr24')
            crop_bottom = int(app.config.get('STREAM_CROP_BOTTOM_PX', 0) or 0)
            if crop_bottom > 0 and frame_bgr.shape[0] > crop_bottom:
                frame_bgr = frame_bgr[:-crop_bottom, :]
            ok, encoded = cv2.imencode(
                '.jpg',
                frame_bgr,
                [int(cv2.IMWRITE_JPEG_QUALITY), int(app.config['STREAM_JPEG_QUALITY'])],
            )
            if ok:
                latest_stream_chunk = encoded.tobytes()
                if stream_queue.full():
                    try:
                        stream_queue.get_nowait()
                    except Empty:
                        pass
                stream_queue.put(latest_stream_chunk)
                pipeline_frame_counter += 1
                decoded_any = True

        return decoded_any, decoded_frames

    def h264_decode_worker():
        while True:
            data = h264_ingest_queue.get()
            if data is None:
                return

            ingest_started = time.monotonic()
            decoded_any, decoded_frames = decode_h264_chunk(data)
            ingest_ms = (time.monotonic() - ingest_started) * 1000.0

            if ingest_ms >= 120.0:
                app.logger.warning(
                    'h264 ingest slow decoded=%s decoded_frames=%s bytes=%s queue=%s ingest_ms=%.1f parse_errors=%s',
                    decoded_any,
                    decoded_frames,
                    len(data),
                    stream_queue.qsize(),
                    ingest_ms,
                    h264_parse_errors,
                )

            if app.config['STREAM_PROFILE'] and (pipeline_frame_counter <= 3 or pipeline_frame_counter % 30 == 0):
                app.logger.info(
                    'stream profile ingest=h264 decoded=%s decoded_frames=%s frame_id=%s bytes=%s queue=%s ingest_ms=%.1f parse_errors=%s',
                    decoded_any,
                    decoded_frames,
                    pipeline_frame_counter,
                    len(data),
                    stream_queue.qsize(),
                    ingest_ms,
                    h264_parse_errors,
                )

    if h264_decoder is not None and cv2 is not None:
        threading.Thread(target=h264_decode_worker, daemon=True).start()

    def is_device_online(device_row, now=None):
        if now is None:
            now = datetime.now(timezone.utc)
        last_seen_at = parse_timestamp(device_row['last_seen_at'])
        if last_seen_at is None:
            return False
        return (now - last_seen_at).total_seconds() <= app.config['BOARD_ACTIVE_STALE_SECONDS']

    def sync_device_statuses(db=None, now=None):
        if db is None:
            db = get_db()
        if now is None:
            now = datetime.now(timezone.utc)

        active_device_ids = {
            row['device_id']
            for row in db.execute("SELECT device_id FROM sessions WHERE status = 'active'").fetchall()
        }
        devices = db.execute('SELECT id, status, last_seen_at FROM devices ORDER BY id').fetchall()

        changed = False
        for device in devices:
            if device['id'] in active_device_ids:
                desired_status = 'in_use'
            elif is_device_online(device, now):
                desired_status = 'available'
            else:
                desired_status = 'offline'

            if device['status'] != desired_status:
                db.execute('UPDATE devices SET status = ? WHERE id = ?', (desired_status, device['id']))
                changed = True

        if changed:
            db.commit()

    def mark_board_activity(source):
        now = datetime.now(timezone.utc)
        app_state['board_last_seen_at'] = now
        app_state['board_last_source'] = source

    def mark_board_activity_for_device(source, board_name=None):
        mark_board_activity(source)
        db = get_db()
        now = datetime.now(timezone.utc)
        timestamp_raw = format_timestamp(now)

        if board_name:
            db.execute(
                'UPDATE devices SET last_seen_at = ?, last_poll_ok = 1, last_poll_error = NULL WHERE name = ?',
                (timestamp_raw, board_name),
            )
        else:
            rows = db.execute('SELECT name FROM devices ORDER BY id').fetchall()
            if len(rows) == 1:
                db.execute(
                    'UPDATE devices SET last_seen_at = ?, last_poll_ok = 1, last_poll_error = NULL WHERE name = ?',
                    (timestamp_raw, rows[0]['name']),
                )

        db.commit()
        sync_device_statuses(db, now)

    def is_board_active(now=None):
        db = get_db()
        row = db.execute("SELECT 1 FROM devices WHERE status IN ('available', 'in_use') LIMIT 1").fetchone()
        return row is not None

    def poll_board_loop():
        poll_url = app.config['BOARD_POLL_URL']
        interval = app.config['BOARD_POLL_INTERVAL_SECONDS']
        timeout = app.config['BOARD_POLL_TIMEOUT_SECONDS']

        while True:
            now = datetime.now(timezone.utc)
            app_state['board_last_poll_at'] = now
            try:
                with app.app_context():
                    db = get_db()
                    devices = db.execute('SELECT id, poll_url FROM devices ORDER BY id').fetchall()
                    any_ok = False
                    last_error = None

                    for device in devices:
                        device_poll_url = (device['poll_url'] or '').strip() or poll_url
                        if not device_poll_url:
                            continue

                        try:
                            req = urllib_request.Request(device_poll_url, headers={'User-Agent': 'RC-Car-Server-Poller/1.0'})
                            with urllib_request.urlopen(req, timeout=timeout) as response:
                                status_code = int(getattr(response, 'status', 200) or 200)
                                if status_code >= 400:
                                    raise urllib_error.HTTPError(device_poll_url, status_code, 'board poll failed', response.headers, None)
                            db.execute(
                                'UPDATE devices SET last_seen_at = ?, last_poll_ok = 1, last_poll_error = NULL WHERE id = ?',
                                (format_timestamp(now), device['id']),
                            )
                            any_ok = True
                        except Exception as exc:
                            last_error = str(exc)
                            db.execute(
                                'UPDATE devices SET last_poll_ok = 0, last_poll_error = ? WHERE id = ?',
                                (last_error, device['id']),
                            )

                    db.commit()
                    sync_device_statuses(db, now)
                    app_state['board_poll_ok'] = any_ok
                    app_state['board_last_error'] = last_error
            except Exception as exc:
                app_state['board_poll_ok'] = False
                app_state['board_last_error'] = str(exc)
            time.sleep(interval)

    threading.Thread(target=poll_board_loop, daemon=True).start()

    @app.before_request
    def ensure_session_state():
        if 'user_id' in session:
            refresh_sessions()
            sync_device_statuses()

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
        duration_seconds = max(0, int(user['balance']))
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
        if duration_seconds <= 0:
            return render_template('dashboard.html', user=user, error='Not enough credit', active_session=active_session, remaining_seconds=remaining_seconds)

        sync_device_statuses(db)

        device = db.execute('SELECT id, name FROM devices WHERE status = ? ORDER BY id LIMIT 1', ('available',)).fetchone()
        if not device:
            return render_template('dashboard.html', user=user, error='No cars available right now', active_session=active_session, remaining_seconds=remaining_seconds)

        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)).strftime('%Y-%m-%d %H:%M:%S')
        db.execute(
            'INSERT INTO sessions (user_id, device_id, expires_at, billing_started_at, allocated_seconds) VALUES (?, ?, ?, ?, ?)',
            (user['id'], device['id'], expires_at, None, duration_seconds)
        )
        db.execute('UPDATE devices SET status = ? WHERE id = ?', ('in_use', device['id']))
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
        releasable_session = db.execute(
            """
            SELECT id, device_id, status, billing_started_at, allocated_seconds
            FROM sessions
            WHERE user_id = ? AND status IN ('active', 'stream_lost')
            ORDER BY id DESC
            LIMIT 1
            """,
            (session['user_id'],),
        ).fetchone()

        if releasable_session:
            refundable_seconds = 0
            if releasable_session['status'] == 'active':
                refundable_seconds = get_remaining_seconds(session['user_id'], releasable_session)

            db.execute("UPDATE sessions SET status = 'released' WHERE id = ?", (releasable_session['id'],))
            if refundable_seconds > 0:
                db.execute('UPDATE users SET balance = balance + ? WHERE id = ?', (refundable_seconds, session['user_id']))
            db.commit()

        sync_device_statuses(db)
        return redirect(url_for('index'))

    @app.route('/api/session/status')
    def session_status():
        if 'user_id' not in session:
            return jsonify({'status': 'unauthorized'}), 401

        active_session = get_active_session(session['user_id'])
        if not active_session:
            user = get_user_view(session['user_id'])
            return jsonify({
                'status': 'ok',
                'active': False,
                'remaining_seconds': max(0, int(user['balance'])) if user else 0,
                'stream_live': is_stream_live(),
                'stream_ready': is_stream_ready(),
                'billing_started': False,
            })

        return jsonify({
            'status': 'ok',
            'active': True,
            'remaining_seconds': get_remaining_seconds(session['user_id'], active_session),
            'stream_live': is_stream_live(),
            'stream_ready': is_stream_ready(),
            'billing_started': active_session['billing_started_at'] is not None,
            'device': active_session['name'],
        })

    @app.route('/api/health')
    def health_check():
        db = get_db()
        db.execute('SELECT 1')
        return jsonify({'status': 'ok'})

    @app.route('/api/stream/live')
    def stream_live_status():
        return jsonify({
            'status': 'ok',
            'live': is_stream_live(),
            'ready': is_stream_ready(),
            'stale_after_seconds': app.config['STREAM_STALE_SECONDS'],
            'ready_window_seconds': app.config['STREAM_READY_WINDOW_SECONDS'],
            'ready_min_frames': app.config['STREAM_READY_MIN_FRAMES'],
        })

    @app.route('/api/session/start', methods=['POST'])
    def session_start():
        if 'user_id' not in session:
            return jsonify({'status': 'unauthorized'}), 401

        active_session = get_active_session(session['user_id'])
        if not active_session:
            return jsonify({'status': 'error', 'message': 'no active session'}), 400

        if not is_stream_ready():
            return jsonify({'status': 'error', 'message': 'stream not ready'}), 409

        if active_session['billing_started_at'] is None:
            now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            db = get_db()
            db.execute('UPDATE sessions SET billing_started_at = ? WHERE id = ?', (now, active_session['id']))
            db.commit()
            active_session = get_active_session(session['user_id'])

        return jsonify({
            'status': 'ok',
            'billing_started': True,
            'remaining_seconds': get_remaining_seconds(session['user_id'], active_session),
        })

    @app.route('/api/webrtc/offer', methods=['POST'])
    def webrtc_offer():
        if not WEBRTC_AVAILABLE:
            return jsonify({'status': 'error', 'message': 'WebRTC backend not available'}), 503
        if 'user_id' not in session:
            return jsonify({'status': 'error', 'message': 'unauthorized'}), 401

        active_session = get_active_session(session['user_id'])
        if not active_session:
            return jsonify({'status': 'error', 'message': 'no active session'}), 400

        payload = request.get_json(silent=True) or {}
        sdp = payload.get('sdp')
        offer_type = payload.get('type')
        if not sdp or not offer_type:
            return jsonify({'status': 'error', 'message': 'invalid offer payload'}), 400

        async def handle_offer():
            pc = RTCPeerConnection(
                RTCConfiguration(
                    iceServers=[RTCIceServer(urls=['stun:stun.l.google.com:19302'])]
                )
            )
            peer_connections.add(pc)

            @pc.on('connectionstatechange')
            async def on_connectionstatechange():
                if pc.connectionState in {'failed', 'closed', 'disconnected'}:
                    await pc.close()
                    peer_connections.discard(pc)

            track = StreamVideoTrack(lambda: latest_stream_chunk, fps=app.config['WEBRTC_TRACK_FPS'])
            pc.addTrack(track)

            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=offer_type))
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            if pc.iceGatheringState != 'complete':
                gather_complete = asyncio.Event()

                @pc.on('icegatheringstatechange')
                async def on_ice_gathering_state_change():
                    if pc.iceGatheringState == 'complete':
                        gather_complete.set()

                await gather_complete.wait()

            return {
                'status': 'ok',
                'sdp': pc.localDescription.sdp,
                'type': pc.localDescription.type,
            }

        try:
            result = run_webrtc(handle_offer())
            return jsonify(result)
        except Exception as exc:
            return jsonify({'status': 'error', 'message': f'webrtc offer failed: {exc}'}), 500

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
        poll_url = (payload.get('poll_url') or '').strip() or None
        if not name:
            return jsonify({'status': 'error', 'message': 'name is required'}), 400

        db = get_db()
        db.execute(
            'INSERT OR IGNORE INTO devices (name, kind, location, status, poll_url, last_seen_at, last_poll_ok, last_poll_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (name, kind, location, 'offline', poll_url, None, 0, None),
        )
        db.execute(
            'UPDATE devices SET kind = ?, location = ?, poll_url = COALESCE(?, poll_url) WHERE name = ?',
            (kind, location, poll_url, name),
        )
        db.commit()
        sync_device_statuses(db)
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

        mark_board_activity_for_device('command_poll', resolve_board_name())

        try:
            action = pending_commands.get_nowait()
        except Empty:
            return jsonify({'status': 'ok', 'action': None})
        return jsonify({'status': 'ok', 'action': action})

    @app.route('/api/board/stream/status')
    def board_stream_status():
        token = request.args.get('token')
        if token != app.config['BOARD_TOKEN']:
            return jsonify({'status': 'error', 'message': 'invalid token'}), 403

        board_name = resolve_board_name()
        mark_board_activity_for_device('stream_status_poll', board_name)

        db = get_db()
        sync_device_statuses(db)
        if board_name:
            active_row = db.execute(
                """
                SELECT s.id
                FROM sessions s
                JOIN devices d ON d.id = s.device_id
                WHERE s.status = 'active' AND d.name = ?
                ORDER BY s.id DESC
                LIMIT 1
                """,
                (board_name,),
            ).fetchone()
        else:
            active_row = db.execute("SELECT id FROM sessions WHERE status = 'active' ORDER BY id DESC LIMIT 1").fetchone()
        return jsonify({'status': 'ok', 'enabled': active_row is not None, 'board_active': is_board_active()})

    @app.route('/api/board/status')
    def board_status():
        status = {
            'status': 'ok',
            'board_active': is_board_active(),
            'last_seen_at': app_state['board_last_seen_at'].isoformat() if app_state['board_last_seen_at'] else None,
            'last_source': app_state['board_last_source'],
            'last_poll_at': app_state['board_last_poll_at'].isoformat() if app_state['board_last_poll_at'] else None,
            'poll_ok': app_state['board_poll_ok'],
            'last_error': app_state['board_last_error'],
        }
        return jsonify(status)

    @app.route('/api/stream/chunk', methods=['POST'])
    def api_stream_chunk():
        nonlocal latest_stream_chunk
        data = request.get_data()
        if not data:
            return jsonify({'status': 'error', 'message': 'chunk data is required'}), 400

        mark_stream_activity()
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
        board_name = resolve_board_name()
        data = request.get_data()
        if not data:
            return jsonify({'status': 'error', 'message': 'frame data is required'}), 400

        ingest_started = time.monotonic()
        mark_stream_activity()
        if board_name:
            mark_board_activity_for_device('pipeline_frame', board_name)
        pipeline_frame_counter += 1
        latest_stream_chunk = data
        if stream_queue.full():
            try:
                stream_queue.get_nowait()
            except Empty:
                pass
        stream_queue.put(data)
        if app.config['STREAM_PROFILE'] and (pipeline_frame_counter <= 3 or pipeline_frame_counter % 30 == 0):
            app.logger.info(
                'stream profile ingest=frame frame_id=%s bytes=%s queue=%s ingest_ms=%.1f',
                pipeline_frame_counter,
                len(data),
                stream_queue.qsize(),
                (time.monotonic() - ingest_started) * 1000.0,
            )
        return jsonify({'status': 'ok', 'received': len(data), 'frame_id': pipeline_frame_counter})

    @app.route('/api/video/h264/chunk', methods=['POST'])
    def api_video_h264_chunk():
        board_name = resolve_board_name()

        token = request.headers.get('X-Board-Token', '')
        if token != app.config['BOARD_TOKEN']:
            return jsonify({'status': 'error', 'message': 'invalid token'}), 403

        data = request.get_data()
        if not data:
            return jsonify({'status': 'error', 'message': 'chunk data is required'}), 400

        if h264_decoder is None or cv2 is None:
            return jsonify({'status': 'error', 'message': 'h264 decode not available'}), 503

        enqueue_started = time.monotonic()
        mark_stream_activity()
        if board_name:
            mark_board_activity_for_device('h264_chunk', board_name)
        dropped_oldest = False
        try:
            h264_ingest_queue.put_nowait(data)
        except Full:
            try:
                h264_ingest_queue.get_nowait()
                dropped_oldest = True
            except Empty:
                pass
            try:
                h264_ingest_queue.put_nowait(data)
            except Full:
                pass

        enqueue_ms = (time.monotonic() - enqueue_started) * 1000.0
        if app.config['STREAM_PROFILE'] and enqueue_ms >= 20.0:
            app.logger.info(
                'stream profile ingest=h264 enqueue_ms=%.1f bytes=%s backlog=%s dropped_oldest=%s',
                enqueue_ms,
                len(data),
                h264_ingest_queue.qsize(),
                dropped_oldest,
            )

        return jsonify({'status': 'ok', 'queued': h264_ingest_queue.qsize(), 'dropped_oldest': dropped_oldest})

    @app.route('/video_feed')
    def video_feed():
        mode = request.args.get('mode', 'stream')

        def generate_stream():
            while True:
                try:
                    chunk = get_latest_stream_chunk()
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
                    chunk = get_latest_stream_chunk()
                except Empty:
                    continue
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + chunk + b'\r\n')

        return Response(generate_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

    return app


app = create_app()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
