import os
import threading
import time
import re
import json
import ipaddress
from collections import deque
from datetime import datetime, timedelta, timezone
from queue import Empty, Full, Queue
from sqlite3 import dbapi2 as sqlite3
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from flask import Flask, g, redirect, render_template, request, session, url_for, jsonify, Response
from flask_sock import Sock
from simple_websocket import ConnectionClosed
from werkzeug.security import generate_password_hash, check_password_hash


def _extract_mediamtx_tracks(payload):
    tracks = payload.get('tracks')
    if tracks is None and isinstance(payload.get('source'), dict):
        tracks = payload['source'].get('tracks')
    if tracks is None and isinstance(payload.get('stream'), dict):
        tracks = payload['stream'].get('tracks')
    if tracks is None:
        return []
    if isinstance(tracks, dict):
        return list(tracks.values())
    if isinstance(tracks, list):
        return tracks
    return [tracks]


def _mediamtx_track_is_h264(track):
    if isinstance(track, dict):
        for key in ('codec', 'type', 'format', 'name', 'media'):
            value = track.get(key)
            if value and 'h264' in str(value).lower():
                return True
        return 'h264' in json.dumps(track).lower()
    return 'h264' in str(track).lower()


def _extract_mediamtx_bytes(payload):
    candidates = [
        payload.get('bytesReceived'),
        payload.get('bytes_received'),
        payload.get('receivedBytes'),
    ]
    source = payload.get('source')
    if isinstance(source, dict):
        candidates.extend([
            source.get('bytesReceived'),
            source.get('bytes_received'),
            source.get('receivedBytes'),
        ])

    for candidate in candidates:
        try:
            if candidate is None:
                continue
            value = int(candidate)
            if value >= 0:
                return value
        except (TypeError, ValueError):
            continue
    return None


def parse_mediamtx_path_payload(payload):
    payload = payload or {}
    tracks = _extract_mediamtx_tracks(payload)
    has_h264 = any(_mediamtx_track_is_h264(track) for track in tracks)
    bytes_received = _extract_mediamtx_bytes(payload)
    ready = bool(payload.get('ready') or payload.get('sourceReady'))
    bytes_received_ok = bytes_received is not None and bytes_received > 0
    return {
        'exists': True,
        'ready': ready,
        'tracks': tracks,
        'has_h264': has_h264,
        'bytes_received': bytes_received,
        'bytes_received_ok': bytes_received_ok,
        'stream_ready': bool(ready and has_h264 and bytes_received_ok),
    }


def create_app(test_config=None):
    app = Flask(__name__)
    sock = Sock(app)
    app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key')
    app.config['DATABASE_URL'] = os.environ.get('DATABASE_URL', 'sqlite:///app.db')
    app.config['STREAM_MAX_QUEUE'] = int(os.environ.get('STREAM_MAX_QUEUE', '1'))
    app.config['BOARD_TOKEN'] = os.environ.get('BOARD_TOKEN', 'dev-board-token')
    app.config['SESSION_DURATION_SECONDS'] = int(os.environ.get('SESSION_DURATION_SECONDS', '300'))
    app.config['STREAM_STALE_SECONDS'] = float(os.environ.get('STREAM_STALE_SECONDS', '6.0'))
    app.config['STREAM_READY_WINDOW_SECONDS'] = max(1.0, float(os.environ.get('STREAM_READY_WINDOW_SECONDS', '2.0')))
    app.config['STREAM_READY_MIN_FRAMES'] = max(2, int(os.environ.get('STREAM_READY_MIN_FRAMES', '6')))
    app.config['STREAM_VISIBILITY_TTL_SECONDS'] = max(1.0, float(os.environ.get('STREAM_VISIBILITY_TTL_SECONDS', '2.5')))
    app.config['STREAM_LOSS_GRACE_SECONDS'] = float(os.environ.get('STREAM_LOSS_GRACE_SECONDS', '30.0'))
    app.config['WEBRTC_TRACK_FPS'] = int(os.environ.get('WEBRTC_TRACK_FPS', '30'))
    app.config['STREAM_CROP_BOTTOM_PX'] = max(0, int(os.environ.get('STREAM_CROP_BOTTOM_PX', '2')))
    app.config['STREAM_JPEG_QUALITY'] = max(30, min(95, int(os.environ.get('STREAM_JPEG_QUALITY', '65'))))
    app.config['H264_INGEST_QUEUE_MAX'] = max(1, int(os.environ.get('H264_INGEST_QUEUE_MAX', '8')))
    app.config['STREAM_PROFILE'] = os.environ.get('STREAM_PROFILE', '0').strip().lower() in {'1', 'true', 'yes', 'on'}
    app.config['BOARD_POLL_URL'] = os.environ.get('BOARD_POLL_URL', '').strip()
    app.config['BOARD_POLL_INTERVAL_SECONDS'] = max(1.0, float(os.environ.get('BOARD_POLL_INTERVAL_SECONDS', '2.0')))
    app.config['BOARD_POLL_TIMEOUT_SECONDS'] = max(0.2, float(os.environ.get('BOARD_POLL_TIMEOUT_SECONDS', '1.5')))
    app.config['BOARD_ACTIVE_STALE_SECONDS'] = max(2.0, float(os.environ.get('BOARD_ACTIVE_STALE_SECONDS', '8.0')))
    app.config['COMMAND_QUEUE_MAX'] = max(1, int(os.environ.get('COMMAND_QUEUE_MAX', '32')))
    app.config['MEDIAMTX_RTSP_BASE'] = os.environ.get('MEDIAMTX_RTSP_BASE', '').strip().rstrip('/')
    app.config['MEDIAMTX_WHEP_BASE'] = os.environ.get('MEDIAMTX_WHEP_BASE', '').strip().rstrip('/')
    app.config['MEDIAMTX_STREAM_PREFIX'] = os.environ.get('MEDIAMTX_STREAM_PREFIX', 'cars').strip().strip('/') or 'cars'
    app.config['MEDIAMTX_CONTROL_API'] = os.environ.get('MEDIAMTX_CONTROL_API', 'http://127.0.0.1:9997').strip().rstrip('/')
    app.config['MEDIAMTX_CONTROL_TIMEOUT_SECONDS'] = max(0.1, float(os.environ.get('MEDIAMTX_CONTROL_TIMEOUT_SECONDS', '1.0')))
    app.config['MEDIAMTX_STATUS_CACHE_TTL_SECONDS'] = max(0.1, float(os.environ.get('MEDIAMTX_STATUS_CACHE_TTL_SECONDS', '1.0')))
    app.config['MEDIAMTX_BYTES_STALE_SECONDS'] = max(1.0, float(os.environ.get('MEDIAMTX_BYTES_STALE_SECONDS', '6.0')))

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
            ensure_column('sessions', 'consumed_seconds', 'INTEGER DEFAULT 0')
            ensure_column('sessions', 'last_billing_at', 'TEXT')
            ensure_column('devices', 'poll_url', 'TEXT')
            ensure_column('devices', 'last_seen_at', 'TEXT')
            ensure_column('devices', 'last_poll_ok', 'INTEGER DEFAULT 0')
            ensure_column('devices', 'last_poll_error', 'TEXT')
            db.commit()

    def refresh_sessions():
        db = get_db()
        now = datetime.now(timezone.utc)
        active_rows = db.execute(
            """
            SELECT
                s.id,
                s.user_id,
                s.device_id,
                s.billing_started_at,
                s.allocated_seconds,
                s.consumed_seconds,
                s.last_billing_at,
                d.name AS device_name
            FROM sessions s
            JOIN devices d ON d.id = s.device_id
            WHERE s.status = 'active'
            """
        ).fetchall()
        changed = False

        for row in active_rows:
            device_name = row['device_name']
            billing_started_at = parse_timestamp(row['billing_started_at'])
            if billing_started_at is None:
                continue

            allocated_seconds = max(0, int(row['allocated_seconds'] or app.config['SESSION_DURATION_SECONDS']))
            consumed_seconds = max(0, int(row['consumed_seconds'] or 0))
            last_billing_at = parse_timestamp(row['last_billing_at']) or billing_started_at

            if is_stream_ready(device_name, now) and is_session_visible(row['id'], now):
                elapsed_seconds = max(0, int((now - last_billing_at).total_seconds()))
                if elapsed_seconds > 0:
                    consumed_seconds = min(allocated_seconds, consumed_seconds + elapsed_seconds)
                    db.execute(
                        'UPDATE sessions SET consumed_seconds = ?, last_billing_at = ? WHERE id = ?',
                        (consumed_seconds, format_timestamp(now), row['id']),
                    )
                    changed = True
            elif row['last_billing_at'] != format_timestamp(now):
                # Keep a moving anchor while stream is not ready so offline time is not billed.
                db.execute(
                    'UPDATE sessions SET last_billing_at = ? WHERE id = ?',
                    (format_timestamp(now), row['id']),
                )
                changed = True

            remaining_seconds = max(0, allocated_seconds - consumed_seconds)

            if remaining_seconds <= 0:
                db.execute("UPDATE sessions SET status = 'expired' WHERE id = ?", (row['id'],))
                db.execute("UPDATE devices SET status = 'available' WHERE id = ?", (row['device_id'],))
                mark_session_visible(row['id'], False)
                changed = True
                continue

            # Only enforce stream-loss after at least one stream frame has been observed.
            last_stream_at = get_board_last_stream_at(device_name)
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
                mark_session_visible(row['id'], False)
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
            SELECT s.id, s.device_id, s.expires_at, s.billing_started_at, s.allocated_seconds, s.consumed_seconds, s.last_billing_at, d.name
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
            consumed_seconds = max(0, int(active_session['consumed_seconds'] or 0))
            return max(0, allocated_seconds - consumed_seconds)
        user = get_user_view(user_id)
        return max(0, int(user['balance'])) if user else 0

    init_db()

    default_board_name = '__default__'
    stream_states = {}
    command_queues = {}
    stream_state_lock = threading.Lock()
    command_queue_lock = threading.Lock()
    pipeline_frame_counter = 0
    app_state = {
        'session_visible_at': {},
        'board_last_seen_at': None,
        'board_last_source': None,
        'board_last_poll_at': None,
        'board_poll_ok': None,
        'board_last_error': None,
        'mediamtx_status_cache': {},
        'mediamtx_bytes_state': {},
    }

    def normalize_board_name(raw_board_name):
        value = (raw_board_name or '').strip()
        if not value:
            return None
        return re.sub(r'[^a-zA-Z0-9_.-]', '-', value)

    def board_key(raw_board_name):
        return normalize_board_name(raw_board_name) or default_board_name

    def get_stream_state(raw_board_name):
        key = board_key(raw_board_name)
        with stream_state_lock:
            state = stream_states.get(key)
            if state is None:
                state = {
                    'latest_stream_chunk': None,
                    'stream_queue': Queue(maxsize=app.config['STREAM_MAX_QUEUE']),
                    'last_stream_at': None,
                    'stream_frame_times': deque(maxlen=300),
                }
                stream_states[key] = state
        return state

    def get_command_queue(raw_board_name):
        key = board_key(raw_board_name)
        with command_queue_lock:
            queue = command_queues.get(key)
            if queue is None:
                queue = Queue(maxsize=app.config['COMMAND_QUEUE_MAX'])
                command_queues[key] = queue
        return queue

    def put_command(raw_board_name, action):
        queue = get_command_queue(raw_board_name)
        dropped_oldest = False
        try:
            queue.put_nowait(action)
        except Full:
            try:
                queue.get_nowait()
                dropped_oldest = True
            except Empty:
                pass
            try:
                queue.put_nowait(action)
            except Full:
                pass
        return dropped_oldest

    def get_latest_stream_chunk(raw_board_name=None):
        state = get_stream_state(raw_board_name)
        queue = state['stream_queue']
        chunk = queue.get(timeout=0.25)
        while True:
            try:
                chunk = queue.get_nowait()
            except Empty:
                return chunk

    def build_stream_path(raw_board_name):
        safe_board = board_key(raw_board_name)
        prefix = app.config['MEDIAMTX_STREAM_PREFIX']
        return f'{prefix}/{safe_board}'

    def build_stream_urls(raw_board_name):
        def is_loopback_or_unspecified_host(base_url):
            try:
                parsed = urllib_parse.urlparse(base_url)
            except Exception:
                return False

            host = (parsed.hostname or '').strip().lower()
            if not host:
                return False
            if host in {'localhost', '0.0.0.0'}:
                return True

            try:
                ip = ipaddress.ip_address(host)
                return ip.is_loopback or ip.is_unspecified
            except ValueError:
                return False

        path = build_stream_path(raw_board_name)
        encoded_path = '/'.join(urllib_parse.quote(segment, safe='') for segment in path.split('/'))
        rtsp_base = app.config['MEDIAMTX_RTSP_BASE']
        whep_base = app.config['MEDIAMTX_WHEP_BASE']

        if not rtsp_base or not whep_base:
            host_header = (request.host or '').strip()
            host_only = host_header.split(':', 1)[0] if host_header else ''
            if host_only:
                if not rtsp_base:
                    rtsp_base = f'rtsp://{host_only}:8554'
                if not whep_base:
                    whep_base = request.url_root.rstrip('/')

        host_header = (request.host or '').strip()
        host_only = host_header.split(':', 1)[0] if host_header else ''
        if host_only:
            if rtsp_base and is_loopback_or_unspecified_host(rtsp_base):
                rtsp_base = f'rtsp://{host_only}:8554'
            if whep_base and is_loopback_or_unspecified_host(whep_base):
                whep_base = request.url_root.rstrip('/')

        rtsp_url = f'{rtsp_base}/{encoded_path}' if rtsp_base else None
        whep_url = f'{whep_base}/{encoded_path}/whep' if whep_base else None
        return {
            'path': path,
            'rtsp_url': rtsp_url,
            'whep_url': whep_url,
        }

    def build_mediamtx_control_url(raw_board_name):
        base = app.config.get('MEDIAMTX_CONTROL_API', '').rstrip('/')
        if not base:
            return None
        encoded_path = urllib_parse.quote(build_stream_path(raw_board_name), safe='')
        return f'{base}/v3/paths/get/{encoded_path}'

    def resolve_mediamtx_override(raw_board_name):
        overrides = app.config.get('MEDIAMTX_TEST_PATH_STATES')
        if not isinstance(overrides, dict):
            return None

        key = board_key(raw_board_name)
        override = overrides.get(key)
        if override is None:
            return None

        if isinstance(override, dict) and {'reachable', 'exists'} & set(override.keys()):
            state = {
                'reachable': bool(override.get('reachable', True)),
                'exists': bool(override.get('exists', False)),
                'ready': bool(override.get('ready', False)),
                'has_h264': bool(override.get('has_h264', False)),
                'bytes_received': override.get('bytes_received'),
                'bytes_received_ok': bool(override.get('bytes_received_ok', False)),
                'stream_ready': bool(override.get('stream_ready', False)),
                'stream_live': bool(override.get('stream_live', False)),
                'tracks': list(override.get('tracks', [])),
                'error': override.get('error'),
            }
            return state

        if isinstance(override, str):
            lowered = override.strip().lower()
            if lowered in {'unreachable', 'error'}:
                return {
                    'reachable': False,
                    'exists': False,
                    'ready': False,
                    'has_h264': False,
                    'bytes_received': None,
                    'bytes_received_ok': False,
                    'stream_ready': False,
                    'stream_live': False,
                    'tracks': [],
                    'error': 'override unreachable',
                }
            if lowered in {'offline', 'missing', 'not_found'}:
                return {
                    'reachable': True,
                    'exists': False,
                    'ready': False,
                    'has_h264': False,
                    'bytes_received': 0,
                    'bytes_received_ok': False,
                    'stream_ready': False,
                    'stream_live': False,
                    'tracks': [],
                    'error': None,
                }

        parsed = parse_mediamtx_path_payload(override if isinstance(override, dict) else {})
        return {
            'reachable': True,
            'exists': parsed['exists'],
            'ready': parsed['ready'],
            'has_h264': parsed['has_h264'],
            'bytes_received': parsed['bytes_received'],
            'bytes_received_ok': parsed['bytes_received_ok'],
            'stream_ready': parsed['stream_ready'],
            'stream_live': parsed['stream_ready'],
            'tracks': parsed['tracks'],
            'error': None,
        }

    def fetch_mediamtx_path_state(raw_board_name, now=None, force_refresh=False):
        if now is None:
            now = datetime.now(timezone.utc)

        def update_bytes_live_state(key, state):
            bytes_state = app_state['mediamtx_bytes_state']
            previous = bytes_state.get(key)
            bytes_received = state.get('bytes_received')
            media_seen_at = None

            if state.get('stream_ready') and bytes_received is not None and bytes_received > 0:
                if previous is None or bytes_received > previous.get('bytes_received', -1):
                    media_seen_at = now
                else:
                    media_seen_at = previous.get('media_seen_at')

                bytes_state[key] = {
                    'bytes_received': bytes_received,
                    'media_seen_at': media_seen_at,
                }
            elif previous is not None:
                media_seen_at = previous.get('media_seen_at')

            if media_seen_at is not None:
                stale_seconds = max(float(app.config['STREAM_STALE_SECONDS']), float(app.config['MEDIAMTX_BYTES_STALE_SECONDS']))
                state['stream_live'] = bool(state.get('stream_ready')) and (now - media_seen_at).total_seconds() <= stale_seconds
            else:
                state['stream_live'] = False

            return state

        override = resolve_mediamtx_override(raw_board_name)
        if override is not None:
            return update_bytes_live_state(board_key(raw_board_name), dict(override))

        key = board_key(raw_board_name)
        cache = app_state['mediamtx_status_cache']
        ttl_seconds = float(app.config['MEDIAMTX_STATUS_CACHE_TTL_SECONDS'])
        if not force_refresh:
            cached = cache.get(key)
            if cached is not None and (time.monotonic() - cached['monotonic']) <= ttl_seconds:
                return dict(cached['state'])

        default_state = {
            'reachable': False,
            'exists': False,
            'ready': False,
            'has_h264': False,
            'bytes_received': None,
            'bytes_received_ok': False,
            'stream_ready': False,
            'stream_live': False,
            'tracks': [],
            'error': None,
        }

        control_url = build_mediamtx_control_url(raw_board_name)
        if not control_url:
            state = dict(default_state)
            state['error'] = 'MEDIAMTX_CONTROL_API is not configured'
            cache[key] = {'monotonic': time.monotonic(), 'state': state}
            return state

        try:
            req = urllib_request.Request(
                control_url,
                headers={'Accept': 'application/json', 'User-Agent': 'RC-Car-Web/2.0'},
                method='GET',
            )
            with urllib_request.urlopen(req, timeout=float(app.config['MEDIAMTX_CONTROL_TIMEOUT_SECONDS'])) as response:
                status_code = int(getattr(response, 'status', 200) or 200)
                body = response.read().decode('utf-8', errors='replace')

            if status_code == 404:
                state = dict(default_state)
                state['reachable'] = True
                state['exists'] = False
            elif status_code >= 400:
                state = dict(default_state)
                state['error'] = f'HTTP {status_code}'
            else:
                payload = json.loads(body) if body else {}
                parsed = parse_mediamtx_path_payload(payload)
                state = {
                    'reachable': True,
                    'exists': parsed['exists'],
                    'ready': parsed['ready'],
                    'has_h264': parsed['has_h264'],
                    'bytes_received': parsed['bytes_received'],
                    'bytes_received_ok': parsed['bytes_received_ok'],
                    'stream_ready': parsed['stream_ready'],
                    'stream_live': False,
                    'tracks': parsed['tracks'],
                    'error': None,
                }
        except urllib_error.HTTPError as exc:
            if int(getattr(exc, 'code', 500)) == 404:
                state = dict(default_state)
                state['reachable'] = True
                state['exists'] = False
            else:
                state = dict(default_state)
                state['error'] = f'HTTP {getattr(exc, "code", "error")}'
        except Exception as exc:
            state = dict(default_state)
            state['error'] = str(exc)

        state = update_bytes_live_state(key, state)

        cache[key] = {'monotonic': time.monotonic(), 'state': dict(state)}
        return state

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

    def is_stream_live(raw_board_name=None, now=None):
        if now is None:
            now = datetime.now(timezone.utc)
        mediamtx_state = fetch_mediamtx_path_state(raw_board_name, now=now)
        return bool(mediamtx_state['stream_live'])

    def is_stream_ready(raw_board_name=None, now=None):
        if now is None:
            now = datetime.now(timezone.utc)
        mediamtx_state = fetch_mediamtx_path_state(raw_board_name, now=now)
        return bool(mediamtx_state['stream_ready'])

    def mark_stream_activity(raw_board_name=None):
        now = datetime.now(timezone.utc)
        stream_state = get_stream_state(raw_board_name)
        stream_state['last_stream_at'] = now
        stream_state['stream_frame_times'].append(now)

    def get_board_last_stream_at(raw_board_name):
        state = app_state['mediamtx_bytes_state'].get(board_key(raw_board_name))
        if state is not None:
            return state.get('media_seen_at')
        fetch_mediamtx_path_state(raw_board_name, force_refresh=True)
        state = app_state['mediamtx_bytes_state'].get(board_key(raw_board_name))
        return state.get('media_seen_at') if state else None

    def set_latest_stream_chunk(raw_board_name, data):
        stream_state = get_stream_state(raw_board_name)
        stream_state['latest_stream_chunk'] = data
        queue = stream_state['stream_queue']
        if queue.full():
            try:
                queue.get_nowait()
            except Empty:
                pass
        queue.put(data)

    def mark_session_visible(session_id, visible):
        session_visible_at = app_state['session_visible_at']
        key = int(session_id)
        if visible:
            session_visible_at[key] = datetime.now(timezone.utc)
        else:
            session_visible_at.pop(key, None)

    def is_session_visible(session_id, now=None):
        if now is None:
            now = datetime.now(timezone.utc)
        seen_at = app_state['session_visible_at'].get(int(session_id))
        if seen_at is None:
            return False
        return (now - seen_at).total_seconds() <= app.config['STREAM_VISIBILITY_TTL_SECONDS']

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
            existing = db.execute('SELECT id FROM devices WHERE name = ?', (board_name,)).fetchone()
            if existing is None:
                db.execute(
                    'INSERT INTO devices (name, kind, location, status, last_seen_at, last_poll_ok, last_poll_error) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (board_name, 'arduino', 'unknown', 'offline', timestamp_raw, 1, None),
                )
            else:
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
            SELECT id, device_id, status, billing_started_at, allocated_seconds, consumed_seconds, last_billing_at
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
            mark_session_visible(releasable_session['id'], False)
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
                'stream_live': False,
                'stream_ready': False,
                'billing_started': False,
            })

        board_name = active_session['name']
        mediamtx_state = fetch_mediamtx_path_state(board_name)

        return jsonify({
            'status': 'ok',
            'active': True,
            'remaining_seconds': get_remaining_seconds(session['user_id'], active_session),
            'stream_live': bool(mediamtx_state['stream_live']),
            'stream_ready': bool(mediamtx_state['stream_ready']),
            'stream_visible': is_session_visible(active_session['id']),
            'billing_started': active_session['billing_started_at'] is not None,
            'device': board_name,
            'mediamtx_reachable': bool(mediamtx_state['reachable']),
            'mediamtx_path_exists': bool(mediamtx_state['exists']),
            'mediamtx_has_h264': bool(mediamtx_state['has_h264']),
            'mediamtx_bytes_received': mediamtx_state['bytes_received'],
            'mediamtx_error': mediamtx_state['error'],
        })

    @app.route('/api/session/stream')
    def session_stream():
        if 'user_id' not in session:
            return jsonify({'status': 'unauthorized'}), 401

        active_session = get_active_session(session['user_id'])
        if not active_session:
            return jsonify({'status': 'error', 'message': 'no active session'}), 400

        board_name = active_session['name']
        stream_urls = build_stream_urls(board_name)
        mediamtx_state = fetch_mediamtx_path_state(board_name)
        return jsonify({
            'status': 'ok',
            'device': board_name,
            'stream_path': stream_urls['path'],
            'whep_url': stream_urls['whep_url'],
            'rtsp_url': stream_urls['rtsp_url'],
            'ready': bool(mediamtx_state['stream_ready']),
            'live': bool(mediamtx_state['stream_live']),
            'mediamtx_reachable': bool(mediamtx_state['reachable']),
            'mediamtx_path_exists': bool(mediamtx_state['exists']),
            'mediamtx_has_h264': bool(mediamtx_state['has_h264']),
            'mediamtx_bytes_received': mediamtx_state['bytes_received'],
            'mediamtx_error': mediamtx_state['error'],
        })

    @app.route('/api/session/visibility', methods=['POST'])
    def session_visibility():
        if 'user_id' not in session:
            return jsonify({'status': 'unauthorized'}), 401

        active_session = get_active_session(session['user_id'])
        if not active_session:
            return jsonify({'status': 'ok', 'active': False})

        payload = request.get_json(silent=True) or {}
        visible = bool(payload.get('visible'))
        mark_session_visible(active_session['id'], visible)
        return jsonify({'status': 'ok', 'active': True, 'visible': visible})

    @app.route('/api/health')
    def health_check():
        db = get_db()
        db.execute('SELECT 1')
        return jsonify({'status': 'ok'})

    @app.route('/api/stream/live')
    def stream_live_status():
        board_name = normalize_board_name(resolve_board_name())
        mediamtx_state = fetch_mediamtx_path_state(board_name)
        return jsonify({
            'status': 'ok',
            'live': bool(mediamtx_state['stream_live']),
            'ready': bool(mediamtx_state['stream_ready']),
            'board_name': board_name,
            'mediamtx_reachable': bool(mediamtx_state['reachable']),
            'path_exists': bool(mediamtx_state['exists']),
            'has_h264': bool(mediamtx_state['has_h264']),
            'bytes_received': mediamtx_state['bytes_received'],
            'error': mediamtx_state['error'],
        })

    @app.route('/api/session/start', methods=['POST'])
    def session_start():
        if 'user_id' not in session:
            return jsonify({'status': 'unauthorized'}), 401

        active_session = get_active_session(session['user_id'])
        if not active_session:
            return jsonify({'status': 'error', 'message': 'no active session'}), 400

        board_name = active_session['name']
        if not is_stream_ready(board_name):
            return jsonify({'status': 'error', 'message': 'stream not ready'}), 409

        if active_session['billing_started_at'] is None:
            now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            db = get_db()
            db.execute(
                'UPDATE sessions SET billing_started_at = ?, last_billing_at = ?, consumed_seconds = 0 WHERE id = ?',
                (now, now, active_session['id']),
            )
            db.commit()
            active_session = get_active_session(session['user_id'])

        return jsonify({
            'status': 'ok',
            'billing_started': True,
            'remaining_seconds': get_remaining_seconds(session['user_id'], active_session),
        })

    @app.route('/api/webrtc/offer', methods=['POST'])
    def webrtc_offer():
        return jsonify({
            'status': 'error',
            'message': 'server-side WebRTC offer is deprecated; use /api/session/stream and connect to MediaMTX WHEP',
        }), 410

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
        if 'user_id' not in session:
            return jsonify({'status': 'error', 'message': 'unauthorized'}), 401

        active_session = get_active_session(session['user_id'])
        if not active_session:
            return jsonify({'status': 'error', 'message': 'no active session'}), 409

        payload = request.get_json(silent=True) or {}
        action = (payload.get('action') or '').strip().lower()
        if not action:
            return jsonify({'status': 'error', 'message': 'action is required'}), 400

        if action not in {'forward', 'back', 'left', 'right', 'stop', 'lights_on', 'lights_off'}:
            return jsonify({'status': 'error', 'message': 'invalid action'}), 400

        board_name = active_session['name']
        dropped_oldest = put_command(board_name, action)
        return jsonify({'status': 'ok', 'action': action, 'board_name': board_name, 'dropped_oldest': dropped_oldest})

    @sock.route('/ws/board/commands')
    def board_command_ws(ws):
        token = request.args.get('token')
        if token != app.config['BOARD_TOKEN']:
            try:
                ws.send(json.dumps({'status': 'error', 'message': 'invalid token'}))
            except Exception:
                pass
            return

        board_name = normalize_board_name(resolve_board_name())
        if not board_name:
            try:
                ws.send(json.dumps({'status': 'error', 'message': 'board_name is required'}))
            except Exception:
                pass
            return

        mark_board_activity_for_device('command_ws_connect', board_name)
        command_queue = get_command_queue(board_name)

        while True:
            try:
                action = command_queue.get(timeout=1.0)
            except Empty:
                action = None

            payload = {
                'status': 'ok',
                'action': action,
                'board_name': board_name,
                'keepalive': action is None,
            }
            try:
                ws.send(json.dumps(payload))
            except (ConnectionClosed, BrokenPipeError):
                break
            except Exception:
                break

    @app.route('/api/board/command')
    def board_command():
        token = request.args.get('token')
        if token != app.config['BOARD_TOKEN']:
            return jsonify({'status': 'error', 'message': 'invalid token'}), 403

        board_name = normalize_board_name(resolve_board_name())
        if not board_name:
            return jsonify({'status': 'error', 'message': 'board_name is required'}), 400

        mark_board_activity_for_device('command_poll', board_name)

        try:
            action = get_command_queue(board_name).get_nowait()
        except Empty:
            return jsonify({'status': 'ok', 'action': None, 'board_name': board_name})
        return jsonify({'status': 'ok', 'action': action, 'board_name': board_name})

    @app.route('/api/board/stream/status')
    def board_stream_status():
        token = request.args.get('token')
        if token != app.config['BOARD_TOKEN']:
            return jsonify({'status': 'error', 'message': 'invalid token'}), 403

        board_name = normalize_board_name(resolve_board_name())
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
            stream_urls = build_stream_urls(board_name)
        else:
            active_row = db.execute("SELECT id FROM sessions WHERE status = 'active' ORDER BY id DESC LIMIT 1").fetchone()
            stream_urls = {'path': None, 'whep_url': None, 'rtsp_url': None}
        return jsonify({
            'status': 'ok',
            'enabled': active_row is not None,
            'board_active': is_board_active(),
            'board_name': board_name,
            'stream_path': stream_urls['path'],
            'whep_url': stream_urls['whep_url'],
            'rtsp_url': stream_urls['rtsp_url'],
        })

    @app.route('/api/board/stream/config')
    def board_stream_config():
        token = request.args.get('token')
        if token != app.config['BOARD_TOKEN']:
            return jsonify({'status': 'error', 'message': 'invalid token'}), 403

        board_name = normalize_board_name(resolve_board_name())
        if not board_name:
            return jsonify({'status': 'error', 'message': 'board_name is required'}), 400

        mark_board_activity_for_device('stream_config_poll', board_name)

        db = get_db()
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
        stream_urls = build_stream_urls(board_name)
        return jsonify({
            'status': 'ok',
            'enabled': active_row is not None,
            'board_name': board_name,
            'stream_path': stream_urls['path'],
            'whep_url': stream_urls['whep_url'],
            'rtsp_url': stream_urls['rtsp_url'],
        })

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
        board_name = normalize_board_name(resolve_board_name())
        if not board_name and 'user_id' in session:
            active_session = get_active_session(session['user_id'])
            if active_session:
                board_name = active_session['name']
        data = request.get_data()
        if not data:
            return jsonify({'status': 'error', 'message': 'chunk data is required'}), 400

        mark_stream_activity(board_name)
        set_latest_stream_chunk(board_name, data)
        if board_name:
            mark_board_activity_for_device('stream_chunk', board_name)
        return jsonify({'status': 'ok', 'received': len(data), 'board_name': board_name or default_board_name})

    @app.route('/api/video/pipeline/frame', methods=['POST'])
    def api_video_pipeline_frame():
        nonlocal pipeline_frame_counter
        board_name = normalize_board_name(resolve_board_name())
        if not board_name and 'user_id' in session:
            active_session = get_active_session(session['user_id'])
            if active_session:
                board_name = active_session['name']
        data = request.get_data()
        if not data:
            return jsonify({'status': 'error', 'message': 'frame data is required'}), 400

        ingest_started = time.monotonic()
        mark_stream_activity(board_name)
        if board_name:
            mark_board_activity_for_device('pipeline_frame', board_name)
        pipeline_frame_counter += 1
        set_latest_stream_chunk(board_name, data)
        if app.config['STREAM_PROFILE'] and (pipeline_frame_counter <= 3 or pipeline_frame_counter % 30 == 0):
            app.logger.info(
                'stream profile ingest=frame frame_id=%s bytes=%s queue=%s ingest_ms=%.1f',
                pipeline_frame_counter,
                len(data),
                get_stream_state(board_name)['stream_queue'].qsize(),
                (time.monotonic() - ingest_started) * 1000.0,
            )
        return jsonify({'status': 'ok', 'received': len(data), 'frame_id': pipeline_frame_counter, 'board_name': board_key(board_name)})

    @app.route('/api/video/h264/chunk', methods=['POST'])
    def api_video_h264_chunk():
        return jsonify({
            'status': 'error',
            'message': 'deprecated: publish H264 directly to MediaMTX (RTSP/SRT) and use WHEP for playback',
        }), 410

    def resolve_feed_board_name():
        candidate = normalize_board_name(request.args.get('board_name'))
        if candidate:
            return candidate
        if 'user_id' in session:
            active_session = get_active_session(session['user_id'])
            if active_session:
                return active_session['name']
        return None

    @app.route('/video_feed')
    def video_feed():
        mode = request.args.get('mode', 'stream')
        board_name = resolve_feed_board_name()
        stream_state = get_stream_state(board_name)

        def generate_stream():
            while True:
                try:
                    chunk = get_latest_stream_chunk(board_name)
                except Empty:
                    continue
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + chunk + b'\r\n')

        if mode == 'single':
            latest_stream_chunk = stream_state['latest_stream_chunk']
            if latest_stream_chunk is None:
                return Response(b'', status=204, mimetype='application/octet-stream')
            return Response(latest_stream_chunk, mimetype='image/jpeg')

        return Response(generate_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

    @app.route('/video/pipeline/stream')
    def video_pipeline_stream():
        board_name = resolve_feed_board_name()

        def generate_stream():
            while True:
                try:
                    chunk = get_latest_stream_chunk(board_name)
                except Empty:
                    continue
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + chunk + b'\r\n')

        return Response(generate_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

    return app


app = create_app()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
