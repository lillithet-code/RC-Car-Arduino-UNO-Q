import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uno_q_board_commands as board_commands
from app import create_app


@pytest.fixture()
def client():
    db_fd, db_path = tempfile.mkstemp()
    os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'
    app = create_app({
        'STREAM_READY_MIN_FRAMES': 1,
        'STREAM_READY_WINDOW_SECONDS': 2.0,
        'MEDIAMTX_TEST_PATH_STATES': {},
        'BOARD_TOKEN': 'dev-board-token',
    })
    app.config['TESTING'] = True
    with app.test_client() as client:
        client.environ_base['HTTP_AUTHORIZATION'] = 'Bearer dev-board-token'
        yield client
    os.close(db_fd)
    os.unlink(db_path)


def mark_board_online(client, board_name):
    response = client.get(f'/api/board/stream/status?board_name={board_name}')
    assert response.status_code == 200


def set_mediamtx_state(
    client,
    board_name,
    *,
    reachable=True,
    exists=True,
    ready=True,
    has_h264=True,
    bytes_received=4096,
    stream_live=True,
):
    states = client.application.config['MEDIAMTX_TEST_PATH_STATES']
    stream_ready = bool(reachable and exists and ready and has_h264 and int(bytes_received or 0) > 0)
    states[board_name] = {
        'reachable': reachable,
        'exists': exists,
        'ready': ready,
        'has_h264': has_h264,
        'bytes_received': bytes_received,
        'bytes_received_ok': int(bytes_received or 0) > 0,
        'stream_ready': stream_ready,
        'stream_live': bool(stream_live and stream_ready),
        'tracks': [{'codec': 'H264'}] if has_h264 else [{'codec': 'VP8'}],
        'error': None if reachable else 'override unreachable',
    }


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


def test_device_registration_requires_board_token(client):
    client.environ_base.pop('HTTP_AUTHORIZATION', None)
    response = client.post('/api/devices/register', json={
        'name': 'unauthorized-car',
        'kind': 'arduino',
        'location': 'garage',
    })
    assert response.status_code == 403
    query_token_response = client.get('/api/board/stream/status?token=dev-board-token&board_name=legacy-car')
    assert query_token_response.status_code == 403


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

    mark_board_online(client, 'rc-car-1')

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
    mark_board_online(client, 'rc-car-dave')

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
    mark_board_online(client, 'rc-car-erin')

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
    mark_board_online(client, 'rc-car-frank')

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

    set_mediamtx_state(client, 'rc-car-frank', ready=True, has_h264=True, bytes_received=1000, stream_live=True)

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
    mark_board_online(client, 'rc-car-gary')

    first_request = client.post('/request_car')
    assert first_request.status_code == 200
    assert b'Control session started' in first_request.data

    set_mediamtx_state(client, 'rc-car-gary', ready=True, has_h264=True, bytes_received=1000, stream_live=True)

    start_response = client.post('/api/session/start')
    assert start_response.status_code == 200

    # Force stream-loss quickly and trigger refresh via a page request.
    set_mediamtx_state(client, 'rc-car-gary', exists=False, ready=False, has_h264=False, bytes_received=0, stream_live=False)
    client.application.config['STREAM_LOSS_GRACE_SECONDS'] = 0.0
    dashboard_response = client.get('/')
    assert dashboard_response.status_code == 200

    # Register an additional car so a new session can be allocated.
    second_register_response = client.post('/api/devices/register', json={
        'name': 'rc-car-gary-2',
        'kind': 'arduino',
        'location': 'garage'
    })
    assert second_register_response.status_code == 200
    mark_board_online(client, 'rc-car-gary-2')

    second_request = client.post('/request_car')
    assert second_request.status_code == 200
    assert b'Control session started' in second_request.data


def test_session_start_requires_stream_ready(client):
    client.post('/register', data={
        'username': 'ruth',
        'password': 'secret123',
        'email': 'ruth@example.com'
    })
    client.post('/login', data={
        'username': 'ruth',
        'password': 'secret123'
    })

    register_response = client.post('/api/devices/register', json={
        'name': 'rc-car-ruth',
        'kind': 'arduino',
        'location': 'garage'
    })
    assert register_response.status_code == 200
    mark_board_online(client, 'rc-car-ruth')

    request_response = client.post('/request_car')
    assert request_response.status_code == 200

    set_mediamtx_state(client, 'rc-car-ruth', ready=False, has_h264=False, bytes_received=0, stream_live=False)

    start_response = client.post('/api/session/start')
    assert start_response.status_code == 409
    payload = start_response.get_json()
    assert payload['status'] == 'error'
    assert payload['message'] == 'stream not ready'


def test_session_start_rejects_ready_but_frozen_stream(client):
    client.post('/register', data={
        'username': 'frozen',
        'password': 'secret123',
        'email': 'frozen@example.com'
    })
    client.post('/login', data={
        'username': 'frozen',
        'password': 'secret123'
    })
    client.post('/api/devices/register', json={
        'name': 'rc-car-frozen',
        'kind': 'arduino',
        'location': 'garage'
    })
    mark_board_online(client, 'rc-car-frozen')
    assert client.post('/request_car').status_code == 200

    set_mediamtx_state(
        client,
        'rc-car-frozen',
        ready=True,
        has_h264=True,
        bytes_received=5000,
        stream_live=False,
    )
    status_payload = client.get('/api/session/status').get_json()
    assert status_payload['stream_ready'] is True
    assert status_payload['stream_live'] is False
    assert status_payload['stream_operational'] is False

    start_response = client.post('/api/session/start')
    assert start_response.status_code == 409


def test_billing_pauses_when_ready_stream_stops_receiving_bytes(client):
    client.post('/register', data={
        'username': 'stalled',
        'password': 'secret123',
        'email': 'stalled@example.com'
    })
    client.post('/login', data={
        'username': 'stalled',
        'password': 'secret123'
    })
    client.post('/api/devices/register', json={
        'name': 'rc-car-stalled',
        'kind': 'arduino',
        'location': 'garage'
    })
    mark_board_online(client, 'rc-car-stalled')
    client.post('/request_car')
    set_mediamtx_state(client, 'rc-car-stalled', bytes_received=1000, stream_live=True)
    assert client.post('/api/session/start').status_code == 200

    with client.application.app_context():
        database_url = client.application.config['DATABASE_URL']
        path = database_url.replace('sqlite:///', '', 1)
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("UPDATE sessions SET last_billing_at = datetime('now', '-5 seconds') WHERE status = 'active'")
        conn.commit()
        conn.close()

    before = client.get('/api/session/status').get_json()['remaining_seconds']
    client.post('/api/session/visibility', json={'visible': True})
    set_mediamtx_state(client, 'rc-car-stalled', bytes_received=1000, stream_live=False)
    client.get('/')
    after = client.get('/api/session/status').get_json()['remaining_seconds']
    assert after == before


def test_billing_pauses_when_stream_not_ready(client):
    client.post('/register', data={
        'username': 'sara',
        'password': 'secret123',
        'email': 'sara@example.com'
    })
    client.post('/login', data={
        'username': 'sara',
        'password': 'secret123'
    })

    register_response = client.post('/api/devices/register', json={
        'name': 'rc-car-sara',
        'kind': 'arduino',
        'location': 'garage'
    })
    assert register_response.status_code == 200
    mark_board_online(client, 'rc-car-sara')

    request_response = client.post('/request_car')
    assert request_response.status_code == 200

    set_mediamtx_state(client, 'rc-car-sara', ready=True, has_h264=True, bytes_received=900, stream_live=True)
    start_response = client.post('/api/session/start')
    assert start_response.status_code == 200

    first_status = client.get('/api/session/status').get_json()
    remaining_before = first_status['remaining_seconds']

    # Force stream to become not-ready and trigger session refresh.
    set_mediamtx_state(client, 'rc-car-sara', ready=False, has_h264=False, bytes_received=0, stream_live=False)
    client.get('/')

    second_status = client.get('/api/session/status').get_json()
    remaining_after = second_status['remaining_seconds']

    # Billing should pause when stream is not ready.
    assert remaining_after == remaining_before


def test_billing_requires_client_visibility_heartbeat(client):
    client.post('/register', data={
        'username': 'tina',
        'password': 'secret123',
        'email': 'tina@example.com'
    })
    client.post('/login', data={
        'username': 'tina',
        'password': 'secret123'
    })

    register_response = client.post('/api/devices/register', json={
        'name': 'rc-car-tina',
        'kind': 'arduino',
        'location': 'garage'
    })
    assert register_response.status_code == 200
    mark_board_online(client, 'rc-car-tina')

    request_response = client.post('/request_car')
    assert request_response.status_code == 200

    set_mediamtx_state(client, 'rc-car-tina', ready=True, has_h264=True, bytes_received=1200, stream_live=True)
    start_response = client.post('/api/session/start')
    assert start_response.status_code == 200

    with client.application.app_context():
        database_url = client.application.config['DATABASE_URL']
        path = database_url.replace('sqlite:///', '', 1)
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("UPDATE sessions SET last_billing_at = datetime('now', '-5 seconds') WHERE status = 'active'")
        conn.commit()
        conn.close()

    # Keep stream ready but do not send visibility heartbeat.
    set_mediamtx_state(client, 'rc-car-tina', ready=True, has_h264=True, bytes_received=1400, stream_live=True)
    before_status = client.get('/api/session/status').get_json()
    before_remaining = before_status['remaining_seconds']

    client.get('/')
    no_visibility_status = client.get('/api/session/status').get_json()
    assert no_visibility_status['remaining_seconds'] == before_remaining

    # Send visibility heartbeat and verify billing resumes.
    visibility_response = client.post('/api/session/visibility', json={'visible': True})
    assert visibility_response.status_code == 200
    set_mediamtx_state(client, 'rc-car-tina', ready=True, has_h264=True, bytes_received=1800, stream_live=True)
    with client.application.app_context():
        database_url = client.application.config['DATABASE_URL']
        path = database_url.replace('sqlite:///', '', 1)
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("UPDATE sessions SET last_billing_at = datetime('now', '-3 seconds') WHERE status = 'active'")
        conn.commit()
        conn.close()

    client.get('/')
    with_visibility_status = client.get('/api/session/status').get_json()
    assert with_visibility_status['remaining_seconds'] < before_remaining


def test_release_still_works_when_stream_never_started(client):
    client.post('/register', data={
        'username': 'nina',
        'password': 'secret123',
        'email': 'nina@example.com'
    })
    client.post('/login', data={
        'username': 'nina',
        'password': 'secret123'
    })

    register_response = client.post('/api/devices/register', json={
        'name': 'rc-car-nina',
        'kind': 'arduino',
        'location': 'garage'
    })
    assert register_response.status_code == 200
    mark_board_online(client, 'rc-car-nina')

    first_request = client.post('/request_car')
    assert first_request.status_code == 200
    assert b'Control session started' in first_request.data

    # Simulate stale stream before billing started; session should stay active until manual release.
    client.application.config['STREAM_STALE_SECONDS'] = 0.0
    dashboard_response = client.get('/')
    assert dashboard_response.status_code == 200
    assert b'Release RC Car' in dashboard_response.data

    release_response = client.post('/release_car', follow_redirects=True)
    assert release_response.status_code == 200
    assert b'Request RC Car' in release_response.data


def test_stream_urls_build_expected_whep_and_rtsp(client):
    client.post('/register', data={
        'username': 'uma',
        'password': 'secret123',
        'email': 'uma@example.com'
    })
    client.post('/login', data={
        'username': 'uma',
        'password': 'secret123'
    })
    client.post('/api/devices/register', json={
        'name': 'rc-car-uma',
        'kind': 'arduino',
        'location': 'garage'
    })
    mark_board_online(client, 'rc-car-uma')
    client.post('/request_car')

    response = client.get('/api/session/stream')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'ok'
    assert payload['stream_path'] == 'cars/rc-car-uma'
    assert payload['rtsp_url'].endswith('/cars/rc-car-uma')
    assert payload['whep_url'].endswith('/cars/rc-car-uma/whep')


def test_mediamtx_ready_offline_unreachable_statuses(client):
    client.post('/register', data={
        'username': 'vera',
        'password': 'secret123',
        'email': 'vera@example.com'
    })
    client.post('/login', data={
        'username': 'vera',
        'password': 'secret123'
    })
    client.post('/api/devices/register', json={
        'name': 'rc-car-vera',
        'kind': 'arduino',
        'location': 'garage'
    })
    mark_board_online(client, 'rc-car-vera')
    client.post('/request_car')

    set_mediamtx_state(client, 'rc-car-vera', ready=True, has_h264=True, bytes_received=3000, stream_live=True)
    ready_payload = client.get('/api/session/status').get_json()
    assert ready_payload['stream_ready'] is True
    assert ready_payload['stream_live'] is True

    set_mediamtx_state(client, 'rc-car-vera', ready=False, has_h264=False, bytes_received=0, stream_live=False)
    offline_payload = client.get('/api/session/status').get_json()
    assert offline_payload['stream_ready'] is False

    set_mediamtx_state(client, 'rc-car-vera', reachable=False, exists=False, ready=False, has_h264=False, bytes_received=0, stream_live=False)
    unreachable_payload = client.get('/api/session/status').get_json()
    assert unreachable_payload['stream_ready'] is False
    assert unreachable_payload['mediamtx_reachable'] is False


def test_board_name_path_isolation(client):
    client.post('/register', data={
        'username': 'walt',
        'password': 'secret123',
        'email': 'walt@example.com'
    })
    client.post('/login', data={
        'username': 'walt',
        'password': 'secret123'
    })
    client.post('/api/devices/register', json={
        'name': 'car-alpha',
        'kind': 'arduino',
        'location': 'garage'
    })
    client.post('/api/devices/register', json={
        'name': 'car-beta',
        'kind': 'arduino',
        'location': 'garage'
    })
    mark_board_online(client, 'car-alpha')
    mark_board_online(client, 'car-beta')
    client.post('/request_car')

    set_mediamtx_state(client, 'car-alpha', ready=True, has_h264=True, bytes_received=4000, stream_live=True)
    set_mediamtx_state(client, 'car-beta', ready=False, has_h264=False, bytes_received=0, stream_live=False)
    stream_payload = client.get('/api/session/stream').get_json()
    assert stream_payload['stream_path'] == 'cars/car-alpha'
    assert stream_payload['ready'] is True
    assert stream_payload['rtsp_url'].endswith('/cars/car-alpha')


def test_legacy_jpeg_heartbeat_does_not_make_rtsp_ready(client):
    client.post('/register', data={
        'username': 'xena',
        'password': 'secret123',
        'email': 'xena@example.com'
    })
    client.post('/login', data={
        'username': 'xena',
        'password': 'secret123'
    })
    client.post('/api/devices/register', json={
        'name': 'rc-car-xena',
        'kind': 'arduino',
        'location': 'garage'
    })
    mark_board_online(client, 'rc-car-xena')
    client.post('/request_car')

    set_mediamtx_state(client, 'rc-car-xena', ready=False, has_h264=False, bytes_received=0, stream_live=False)
    frame_response = client.post('/api/video/pipeline/frame', data=b'legacy-frame', content_type='image/jpeg')
    assert frame_response.status_code == 200

    start_response = client.post('/api/session/start')
    assert start_response.status_code == 409
    payload = client.get('/api/session/status').get_json()
    assert payload['stream_ready'] is False


def test_dashboard_has_webrtc_setup_inflight_guard(client):
    client.post('/register', data={
        'username': 'yuri',
        'password': 'secret123',
        'email': 'yuri@example.com'
    })
    response = client.get('/')
    assert response.status_code == 200
    html = response.data.decode('utf-8')
    assert 'let webRtcSetupInFlight = false;' in html
    assert 'if (webRtcSetupInFlight) {' in html
    assert 'queuedIceCandidates.push(event.candidate);' in html
    assert 'a=ice-pwd:${offerData.icePwd}' in html
    assert "return [{ urls: 'stun:stun.l.google.com:19302' }]" not in html


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
    mark_board_online(client, 'rc-car-helen')

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
    client.post('/register', data={
        'username': 'otto',
        'password': 'secret123',
        'email': 'otto@example.com'
    })
    client.post('/login', data={
        'username': 'otto',
        'password': 'secret123'
    })
    register_response = client.post('/api/devices/register', json={
        'name': 'rc-car-otto',
        'kind': 'arduino',
        'location': 'garage'
    })
    assert register_response.status_code == 200
    mark_board_online(client, 'rc-car-otto')
    request_response = client.post('/request_car')
    assert request_response.status_code == 200

    response = client.post('/api/control', json={'action': 'forward'})
    assert response.status_code == 200

    board_response = client.get('/api/board/command?board_name=rc-car-otto')
    assert board_response.status_code == 200
    payload = board_response.get_json()
    assert payload['status'] == 'ok'
    assert payload['action'] == 'forward'


def test_board_stream_status_enables_registered_online_boards(client):
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
    mark_board_online(client, 'rc-car-ivy')

    response = client.get('/api/board/stream/status?board_name=rc-car-ivy')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['status'] == 'ok'
    assert payload['enabled'] is True
    assert 'board_active' in payload


def test_board_stream_status_reflects_active_sessions(client):
    inactive_response = client.get('/api/board/stream/status?board_name=rc-car-ivy')
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
    mark_board_online(client, 'rc-car-ivy')
    client.post('/request_car')

    active_response = client.get('/api/board/stream/status?board_name=rc-car-ivy')
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


def test_board_websocket_url_does_not_expose_token():
    url = board_commands.build_command_ws_url()
    assert 'board_name=' in url
    assert 'token=' not in url
