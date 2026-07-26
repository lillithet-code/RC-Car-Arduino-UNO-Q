import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import board_commands
from app import create_app


@pytest.fixture()
def client():
    db_fd, db_path = tempfile.mkstemp()
    os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client
    os.close(db_fd)
    os.unlink(db_path)


def test_register_and_login(client):
    response = client.post('/register', data={
        'username': 'alice',
        'password': 'secret123',
        'email': 'alice@example.com'
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b'Welcome, alice' in response.data

    response = client.post('/login', data={
        'username': 'alice',
        'password': 'secret123'
    }, follow_redirects=True)
    assert response.status_code == 200
    assert b'Welcome, alice' in response.data


def test_inactive_user_is_logged_out_after_timeout(client):
    client.post('/register', data={
        'username': 'carol',
        'password': 'secret123',
        'email': 'carol@example.com'
    })

    with client.session_transaction() as session:
        session['last_activity'] = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()

    response = client.get('/', follow_redirects=True)
    assert response.status_code == 200
    assert b'Login' in response.data


def test_device_registration_and_booking(client):
    client.post('/register', data={
        'username': 'bob',
        'password': 'secret123',
        'email': 'bob@example.com'
    })
    client.post('/login', data={
        'username': 'bob',
        'password': 'secret123'
    })

    response = client.post('/api/devices/register', json={
        'name': 'rc-car-1',
        'kind': 'arduino',
        'location': 'garage'
    })
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'registered'
    assert payload['device']['name'] == 'rc-car-1'

    response = client.post('/request_car')
    assert response.status_code == 200
    assert b'Control session started' in response.data


def test_request_car_does_not_double_charge_with_active_session(client):
    client.post('/register', data={
        'username': 'dave',
        'password': 'secret123',
        'email': 'dave@example.com'
    })
    client.post('/login', data={
        'username': 'dave',
        'password': 'secret123'
    })

    register_response = client.post('/api/devices/register', json={
        'name': 'rc-car-dave',
        'kind': 'arduino',
        'location': 'garage'
    })
    assert register_response.status_code == 200

    first_request = client.post('/request_car')
    assert first_request.status_code == 200
    assert b'Control session started' in first_request.data

    second_request = client.post('/request_car')
    assert second_request.status_code == 200
    assert b'You already have an active session' in second_request.data


def test_release_car_refunds_unused_time(client):
    client.post('/register', data={
        'username': 'erin',
        'password': 'secret123',
        'email': 'erin@example.com'
    })
    client.post('/login', data={
        'username': 'erin',
        'password': 'secret123'
    })

    register_response = client.post('/api/devices/register', json={
        'name': 'rc-car-erin',
        'kind': 'arduino',
        'location': 'garage'
    })
    assert register_response.status_code == 200

    first_request = client.post('/request_car')
    assert first_request.status_code == 200

    release_response = client.post('/release_car', follow_redirects=True)
    assert release_response.status_code == 200
    html = release_response.data.decode('utf-8')
    match = re.search(r'id="remaining-time"\s+data-seconds="(\d+)"', html)
    assert match is not None
    assert int(match.group(1)) > 500


def test_session_countdown_starts_after_stream_appears(client):
    client.post('/register', data={
        'username': 'frank',
        'password': 'secret123',
        'email': 'frank@example.com'
    })
    client.post('/login', data={
        'username': 'frank',
        'password': 'secret123'
    })

    register_response = client.post('/api/devices/register', json={
        'name': 'rc-car-frank',
        'kind': 'arduino',
        'location': 'garage'
    })
    assert register_response.status_code == 200

    request_response = client.post('/request_car')
    assert request_response.status_code == 200

    pre_stream_status = client.get('/api/session/status')
    assert pre_stream_status.status_code == 200
    pre_payload = pre_stream_status.get_json()
    assert pre_payload['active'] is True
    assert pre_payload['billing_started'] is False
    assert pre_payload['remaining_seconds'] == 600

    # Billing should not start until client explicitly confirms stream playback.
    start_before_stream = client.post('/api/session/start')
    assert start_before_stream.status_code == 409

    frame_response = client.post('/api/video/pipeline/frame', data=b'frame-start', content_type='image/jpeg')
    assert frame_response.status_code == 200

    start_after_stream = client.post('/api/session/start')
    assert start_after_stream.status_code == 200
    start_payload = start_after_stream.get_json()
    assert start_payload['status'] == 'ok'
    assert start_payload['billing_started'] is True

    post_stream_status = client.get('/api/session/status')
    assert post_stream_status.status_code == 200
    post_payload = post_stream_status.get_json()
    assert post_payload['active'] is True
    assert post_payload['billing_started'] is True


def test_stream_loss_refunds_and_allows_new_request(client):
    client.post('/register', data={
        'username': 'gary',
        'password': 'secret123',
        'email': 'gary@example.com'
    })
    client.post('/login', data={
        'username': 'gary',
        'password': 'secret123'
    })

    register_response = client.post('/api/devices/register', json={
        'name': 'rc-car-gary',
        'kind': 'arduino',
        'location': 'garage'
    })
    assert register_response.status_code == 200

    first_request = client.post('/request_car')
    assert first_request.status_code == 200
    assert b'Control session started' in first_request.data

    frame_response = client.post('/api/video/pipeline/frame', data=b'frame-1', content_type='image/jpeg')
    assert frame_response.status_code == 200

    start_response = client.post('/api/session/start')
    assert start_response.status_code == 200

    # Force stream staleness quickly and trigger refresh via a page request.
    client.application.config['STREAM_STALE_SECONDS'] = 0.0
    dashboard_response = client.get('/')
    assert dashboard_response.status_code == 200

    # Register an additional car so a new session can be allocated.
    second_register_response = client.post('/api/devices/register', json={
        'name': 'rc-car-gary-2',
        'kind': 'arduino',
        'location': 'garage'
    })
    assert second_register_response.status_code == 200

    second_request = client.post('/request_car')
    assert second_request.status_code == 200
    assert b'Control session started' in second_request.data


def test_request_car_allows_partial_session_when_balance_under_default(client):
    client.post('/register', data={
        'username': 'helen',
        'password': 'secret123',
        'email': 'helen@example.com'
    })
    client.post('/login', data={
        'username': 'helen',
        'password': 'secret123'
    })

    client.post('/api/devices/register', json={
        'name': 'rc-car-helen',
        'kind': 'arduino',
        'location': 'garage'
    })

    # Reduce user balance below default session duration.
    with client.application.app_context():
        database_url = client.application.config['DATABASE_URL']
        path = database_url.replace('sqlite:///', '', 1)
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("UPDATE users SET balance = 120 WHERE username = 'helen'")
        conn.commit()
        conn.close()

    response = client.post('/request_car')
    assert response.status_code == 200
    assert b'Control session started' in response.data


def test_stream_chunk_endpoint_and_video_feed(client):
    response = client.post('/api/stream/chunk', data=b'chunk-one', content_type='image/jpeg')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'ok'

    video_response = client.get('/video_feed?mode=single')
    assert video_response.status_code == 200
    assert video_response.mimetype == 'image/jpeg'
    assert b'chunk-one' in video_response.data


def test_video_pipeline_endpoint_accepts_frames(client):
    response = client.post('/api/video/pipeline/frame', data=b'pipeline-frame', content_type='image/jpeg')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'ok'
    assert payload['frame_id'] == 1

    video_response = client.get('/video_feed?mode=single')
    assert video_response.status_code == 200
    assert video_response.mimetype == 'image/jpeg'
    assert b'pipeline-frame' in video_response.data


def test_video_pipeline_stream_route_is_available(client):
    response = client.post('/api/video/pipeline/frame', data=b'pipeline-stream-frame', content_type='image/jpeg')
    assert response.status_code == 200

    stream_response = client.get('/video/pipeline/stream')
    assert stream_response.status_code == 200
    assert stream_response.content_type.startswith('multipart/x-mixed-replace')


def test_board_commands_are_exposed_for_the_board_client(client):
    response = client.post('/api/control', json={'action': 'forward'})
    assert response.status_code == 200

    board_response = client.get('/api/board/command?token=dev-board-token')
    assert board_response.status_code == 200
    payload = board_response.get_json()
    assert payload['status'] == 'ok'
    assert payload['action'] == 'forward'


def test_board_stream_status_reflects_active_sessions(client):
    inactive_response = client.get('/api/board/stream/status?token=dev-board-token')
    assert inactive_response.status_code == 200
    inactive_payload = inactive_response.get_json()
    assert inactive_payload['status'] == 'ok'
    assert inactive_payload['enabled'] is False
    assert 'board_active' in inactive_payload

    client.post('/register', data={
        'username': 'ivy',
        'password': 'secret123',
        'email': 'ivy@example.com'
    })
    client.post('/login', data={
        'username': 'ivy',
        'password': 'secret123'
    })
    client.post('/api/devices/register', json={
        'name': 'rc-car-ivy',
        'kind': 'arduino',
        'location': 'garage'
    })
    client.post('/request_car')

    active_response = client.get('/api/board/stream/status?token=dev-board-token')
    assert active_response.status_code == 200
    active_payload = active_response.get_json()
    assert active_payload['status'] == 'ok'
    assert active_payload['enabled'] is True
    assert 'board_active' in active_payload


def test_board_status_endpoint_reports_liveness(client):
    response = client.get('/api/board/status')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'ok'
    assert 'board_active' in payload
    assert 'last_seen_at' in payload
    assert 'last_source' in payload


def test_health_endpoint_is_public(client):
    response = client.get('/api/health')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'ok'


def test_board_commands_map_to_serial_bytes(monkeypatch):
    created = []

    class FakeSerial:
        def __init__(self, port, baud_rate, timeout=1):
            self.port = port
            self.baud_rate = baud_rate
            self.timeout = timeout
            self.writes = []
            created.append(self)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def write(self, payload):
            self.writes.append(payload)

    monkeypatch.setattr(board_commands, 'serial', SimpleNamespace(Serial=lambda port, baud_rate, timeout=1: FakeSerial(port, baud_rate, timeout)))

    assert board_commands.send_command('forward', port='/dev/ttyUSB0', baud_rate=115200)
    assert created[0].writes == [b'F']
