import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import create_app


@pytest.fixture
def setup(monkeypatch):
    fd, path = tempfile.mkstemp()
    os.close(fd)
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + path)
    config = {'TESTING': True, 'BOARD_TOKEN': 'test-board-token', 'MEDIAMTX_TEST_PATH_STATES': {}}
    app = create_app(config)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    for number in (1, 2, 3):
        db.execute('INSERT INTO users (id, username, email, password_hash, balance, is_admin) VALUES (?, ?, ?, ?, 600, ?)',
                   (number, f'user{number}', f'user{number}@example.com', 'unused', int(number == 1)))
    for number in (1, 2):
        db.execute("INSERT INTO devices (id, name, kind, location, status, last_seen_at) VALUES (?, ?, 'pi', 'test', 'available', ?)",
                   (number, f'car{number}', now))
        config['MEDIAMTX_TEST_PATH_STATES'][f'car{number}'] = {'reachable': False}
    db.commit()
    admin = app.test_client()
    driver = app.test_client()
    board = app.test_client()
    board.environ_base['HTTP_AUTHORIZATION'] = 'Bearer test-board-token'
    with admin.session_transaction() as session:
        session['user_id'] = 1
    with driver.session_transaction() as session:
        session['user_id'] = 2
    yield app, db, admin, driver, board, config
    db.close()
    os.unlink(path)


def test_trim_is_admin_only_and_persists_for_next_driver(setup):
    app, db, admin, driver, board, config = setup
    assert driver.post('/api/admin/steering-trim', json={'trim': 0.1}).status_code == 403
    assert admin.post('/api/admin/steering-trim', json={'trim': 0.1}).status_code == 409
    admin.post('/request_car')
    html = admin.get('/').get_data(as_text=True)
    assert html.index('id="joystick"') < html.index('Steering trim')
    assert admin.post('/api/admin/steering-trim', json={'trim': -0.12}).status_code == 200
    assert board.get('/api/board/command?board_name=car1').json['steering_trim'] == -0.12
    admin.post('/release_car')
    driver.post('/request_car')
    assert 'Steering trim' not in driver.get('/').get_data(as_text=True)
    driver.post('/api/control', json={'throttle': 1, 'steering': 0, 'steering_trim': 0.3})
    assert board.get('/api/board/command?board_name=car1').json['steering_trim'] == -0.12
    restarted = create_app(config).test_client()
    assert restarted.get('/api/board/command?board_name=car1', headers={'Authorization': 'Bearer test-board-token'}).json['steering_trim'] == -0.12
    assert db.execute('SELECT steering_trim FROM devices WHERE id=2').fetchone()[0] == 0


@pytest.mark.parametrize('value', [0.31, -0.31, 'NaN', 'inf', None, 'bad'])
def test_trim_rejects_invalid_values(setup, value):
    _, _, admin, _, _, _ = setup
    admin.post('/request_car')
    assert admin.post('/api/admin/steering-trim', json={'trim': value}).status_code == 400


def test_offline_countdown_stops_session_refunds_and_survives_heartbeat(setup):
    app, db, admin, driver, board, config = setup
    driver.post('/request_car')
    driver.post('/api/control', json={'throttle': 1, 'steering': 0.5})
    start = datetime.now(timezone.utc)
    response = admin.post('/api/admin/availability', json={'device_id': 1, 'online': False})
    deadline = datetime.fromisoformat(response.json['offline_at']).replace(tzinfo=timezone.utc)
    assert 9.9 <= (deadline - start).total_seconds() <= 11
    status = driver.get('/api/session/status').json
    assert status['active'] is True
    assert status['offline_at'] == response.json['offline_at']
    assert board.get('/api/board/command?board_name=car1').json['control_active'] is True
    # Repeated offline clicks cannot extend the deadline.
    admin.post('/api/admin/availability', json={'device_id': 1, 'online': False})
    assert db.execute('SELECT admin_offline_at FROM devices WHERE id=1').fetchone()[0] == response.json['offline_at']
    db.execute("UPDATE devices SET admin_offline_at='2000-01-01 00:00:00' WHERE id=1")
    db.commit()
    stopped = board.get('/api/board/command?board_name=car1').json
    assert stopped['control_active'] is False and stopped['stop'] is True
    assert stopped['throttle'] == stopped['steering'] == 0
    assert db.execute('SELECT balance FROM users WHERE id=2').fetchone()[0] == 600
    board.get('/api/board/command?board_name=car1')
    assert db.execute('SELECT balance FROM users WHERE id=2').fetchone()[0] == 600
    assert db.execute('SELECT status FROM devices WHERE id=1').fetchone()[0] == 'offline'
    assert 'taken offline by an administrator' in driver.get('/').get_data(as_text=True)
    driver.post('/request_car')
    assert db.execute("SELECT device_id FROM sessions WHERE status='active' AND user_id=2").fetchone()[0] == 2
    admin.post('/api/admin/availability', json={'device_id': 1, 'online': True})
    assert db.execute('SELECT status FROM devices WHERE id=1').fetchone()[0] == 'available'


def test_all_cars_block_booking_and_can_be_restored_or_cancelled(setup):
    app, db, admin, driver, board, config = setup
    driver.post('/request_car')
    assert driver.post('/api/admin/availability', json={'all': True, 'online': False}).status_code == 403
    admin.post('/api/admin/availability', json={'all': True, 'online': False})
    deadlines = [row[0] for row in db.execute('SELECT admin_offline_at FROM devices')]
    assert deadlines[0] and deadlines[0] == deadlines[1]
    assert 'No cars available' in admin.post('/request_car').get_data(as_text=True)
    # Restarting the app does not remove the booking block.
    create_app(config)
    assert db.execute('SELECT count(*) FROM devices WHERE admin_offline_at IS NOT NULL').fetchone()[0] == 2
    admin.post('/api/admin/availability', json={'all': True, 'online': True})
    assert db.execute('SELECT count(*) FROM devices WHERE admin_offline_at IS NULL').fetchone()[0] == 2
    assert driver.get('/api/session/status').json['active'] is True
    assert driver.get('/api/session/status').json['offline_at'] is None
    assert 'Control session started' in admin.post('/request_car').get_data(as_text=True)


def test_availability_validation_and_missing_vehicle(setup):
    _, _, admin, _, _, _ = setup
    assert admin.post('/api/admin/availability', json={'online': 'false'}).status_code == 400
    assert admin.post('/api/admin/availability', json={'online': False, 'device_id': 999}).status_code == 404
