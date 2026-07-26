import os
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
