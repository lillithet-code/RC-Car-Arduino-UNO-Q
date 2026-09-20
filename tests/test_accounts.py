import os
import re
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest
from werkzeug.security import check_password_hash

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import create_app
from accounts import AccountMailError


@pytest.fixture
def setup():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    outbox = []
    app = create_app({'TESTING': True, 'SECRET_KEY': 'test-account-secret',
                      'DATABASE_URL': 'sqlite:///' + path, 'ACCOUNT_BASE_URL': 'https://drive.example',
                      'ACCOUNT_MAIL_SENDER': outbox.append})
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    yield app, app.test_client(), db, outbox
    db.close()
    os.unlink(path)


def post(client, path, data=None, **kwargs):
    html = client.get(path).get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', html)[1]
    return client.post(path, data=dict(data or {}, csrf_token=token), **kwargs)


def register(client, email='Alice@example.com'):
    return post(client, '/register', {'username': 'alice', 'email': email, 'password': 'secret123'})


def link(outbox):
    return urlsplit(re.search(r'https://\S+', outbox[-1].get_content())[0]).path


def verify(client, outbox):
    assert post(client, link(outbox)).status_code == 200


def test_new_account_requires_email_and_starts_zero(setup):
    app, client, db, outbox = setup
    assert register(client).status_code == 200
    user = db.execute('SELECT * FROM users').fetchone()
    assert user['balance'] == 0 and user['email_verified'] == 0
    assert user['email'] == 'alice@example.com'
    with client.session_transaction() as session:
        assert 'user_id' not in session
    assert outbox[-1]['To'] == 'alice@example.com'
    assert 'https://drive.example/confirm-email/' in outbox[-1].get_content()
    assert post(client, '/login', {'username': 'alice', 'password': 'secret123'}).status_code == 403
    assert client.get('/buy-minutes').status_code == 302
    url = link(outbox)
    assert client.get(url).status_code == 200  # Mail scanners do not consume links.
    assert db.execute('SELECT email_verified FROM users').fetchone()[0] == 0
    verify(client, outbox)
    assert post(client, '/login', {'username': 'alice', 'password': 'secret123'}).status_code == 302
    assert db.execute('SELECT balance FROM users').fetchone()[0] == 0
    assert client.get(url).status_code == 400


@pytest.mark.parametrize('route', ['/register', '/login', '/forgot-password', '/resend-confirmation'])
def test_auth_forms_require_csrf(setup, route):
    _, client, _, _ = setup
    assert client.post(route, data={'email': 'alice@example.com'}).status_code == 403


def test_unverified_session_cannot_bypass_confirmation(setup):
    _, client, _, _ = setup
    register(client)
    with client.session_transaction() as session:
        session['user_id'] = 1
    assert client.post('/api/control', json={'throttle': 1}).status_code == 401
    with client.session_transaction() as session:
        assert 'user_id' not in session


def test_reset_single_use_revokes_other_tokens_and_sessions(setup):
    app, client, db, outbox = setup
    register(client)
    verify(client, outbox)
    post(client, '/login', {'username': 'alice', 'password': 'secret123'})
    other = app.test_client()
    post(other, '/login', {'username': 'alice', 'password': 'secret123'})
    post(client, '/forgot-password', {'email': 'alice@example.com'})
    first = link(outbox)
    post(client, '/forgot-password', {'email': 'alice@example.com'})
    second = link(outbox)
    assert client.get(first).status_code == 200
    assert post(client, first, {'password': 'new-password', 'password_confirm': 'new-password'}).status_code == 200
    user = db.execute('SELECT * FROM users').fetchone()
    assert check_password_hash(user['password_hash'], 'new-password')
    assert user['auth_version'] == 1
    assert other.get('/').status_code == 302
    assert client.get(first).status_code == client.get(second).status_code == 400
    assert b'Invalid username or password' in post(client, '/login', {'username': 'alice', 'password': 'secret123'}).data
    assert post(client, '/login', {'username': 'alice', 'password': 'new-password'}).status_code == 302


def test_reset_stops_active_car_and_refunds_time(setup):
    _, client, db, outbox = setup
    register(client)
    verify(client, outbox)
    db.execute("INSERT INTO devices(id,name,kind,status,location) VALUES(1,'car1','pi','in_use','test')")
    db.execute("INSERT INTO sessions(user_id,device_id,expires_at,status,allocated_seconds,consumed_seconds) VALUES(1,1,'2099-01-01','active',300,0)")
    db.commit()
    post(client, '/forgot-password', {'email': 'alice@example.com'})
    assert post(client, link(outbox), {'password': 'new-password', 'password_confirm': 'new-password'}).status_code == 200
    assert db.execute('SELECT status FROM sessions').fetchone()[0] == 'password_reset'
    assert db.execute('SELECT balance FROM users').fetchone()[0] == 300


@pytest.mark.parametrize('purpose,route', [('verify','/resend-confirmation'), ('reset','/forgot-password')])
def test_links_expire_and_are_stored_as_hashes(setup, purpose, route):
    _, client, db, outbox = setup
    register(client)
    post(client, route, {'email': 'alice@example.com'})
    url = link(outbox)
    raw = url.rsplit('/', 1)[1]
    assert all(row[0] != raw for row in db.execute('SELECT token_hash FROM account_tokens'))
    db.execute('UPDATE account_tokens SET expires_at=0')
    db.commit()
    assert client.get(url).status_code == 400
    assert client.get(url + 'tampered').status_code == 400


def test_confirmation_token_cannot_reset_password(setup):
    _, client, _, outbox = setup
    register(client)
    assert client.get(link(outbox).replace('/confirm-email/', '/reset-password/')).status_code == 400


def test_resend_and_unknown_email_have_same_response_and_rate_limit(setup):
    _, client, _, outbox = setup
    register(client)
    for route in ('/forgot-password', '/resend-confirmation'):
        known = post(client, route, {'email': 'alice@example.com'})
        unknown = post(client, route, {'email': 'nobody@example.com'})
        assert known.status_code == unknown.status_code == 200
        assert known.data == unknown.data
    before = len(outbox)
    for _ in range(10):
        post(client, '/forgot-password', {'email': 'alice@example.com'})
    assert len(outbox) - before == 4


def test_mail_failure_does_not_enable_account_and_resend_recovers(setup):
    app, client, db, outbox = setup
    def fail(message):
        raise AccountMailError('unavailable')
    app.config['ACCOUNT_MAIL_SENDER'] = fail
    assert register(client).status_code == 503
    assert db.execute('SELECT email_verified FROM users').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM account_tokens').fetchone()[0] == 0
    app.config['ACCOUNT_MAIL_SENDER'] = outbox.append
    post(client, '/resend-confirmation', {'email': 'alice@example.com'})
    verify(client, outbox)
    assert post(client, '/login', {'username': 'alice', 'password': 'secret123'}).status_code == 302


def test_invalid_password_does_not_consume_reset_link(setup):
    _, client, _, outbox = setup
    register(client)
    post(client, '/forgot-password', {'email': 'alice@example.com'})
    url = link(outbox)
    assert client.post(url, data={'password': 'new-password'}).status_code == 403
    assert post(client, url, {'password': 'short', 'password_confirm': 'short'}).status_code == 400
    assert post(client, url, {'password': 'new-password', 'password_confirm': 'different'}).status_code == 400
    assert post(client, url, {'password': 'new-password', 'password_confirm': 'new-password'}).status_code == 200
    # Password recovery does not bypass email confirmation.
    assert post(client, '/login', {'username': 'alice', 'password': 'new-password'}).status_code == 403


def test_duplicate_email_case_and_header_injection_rejected(setup):
    _, client, db, _ = setup
    register(client)
    assert post(client, '/register', {'username': 'other', 'email': 'ALICE@example.com', 'password': 'secret123'}).status_code == 400
    assert register(client, 'other@example.com\nBcc: victim@example.com').status_code == 400
    assert db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1


def test_existing_accounts_and_balances_survive_migration(setup):
    app, _, db, _ = setup
    # Simulate the old users schema before the upgrade.
    db.execute('DROP TABLE users')
    db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, email TEXT, password_hash TEXT, balance INTEGER, is_admin INTEGER)')
    db.execute("INSERT INTO users VALUES(1,'old','old@example.com','unused',420,1)")
    db.commit()
    create_app(dict(app.config))
    user = db.execute('SELECT * FROM users').fetchone()
    assert user['email_verified'] == 1 and user['balance'] == 420 and user['is_admin'] == 1


def test_reset_race_consumes_token_once(setup):
    app, client, db, outbox = setup
    register(client)
    post(client, '/forgot-password', {'email': 'alice@example.com'})
    url = link(outbox)
    clients = [app.test_client(), app.test_client()]
    tokens = [re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).get_data(as_text=True))[1] for c in clients]
    def use(index):
        return clients[index].post(url, data={'csrf_token': tokens[index], 'password': 'new-password', 'password_confirm': 'new-password'}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(use, range(2))) == [200, 400]
    assert db.execute('SELECT auth_version FROM users').fetchone()[0] == 1


def test_account_links_do_not_leak_through_referrers_or_cache(setup):
    _, client, _, outbox = setup
    register(client)
    response = client.get(link(outbox))
    assert response.headers['Referrer-Policy'] == 'no-referrer'
    assert response.headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('security,port', [('starttls',587), ('ssl',465)])
def test_smtp_uses_tls_before_credentials_and_sends_message(setup, monkeypatch, security, port):
    app, client, _, _ = setup
    events = []
    class SMTP:
        def __init__(self, host, selected_port, **kwargs):
            assert host == 'smtp.example.com' and selected_port == port
            assert kwargs['timeout'] == 10
            if security == 'ssl':
                assert kwargs['context'].check_hostname
            events.append('connect')
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def starttls(self, context):
            assert context.check_hostname
            events.append('tls')
        def login(self, username, password):
            assert username == 'sender@example.com' and password == 'mail-password'
            events.append('login')
        def send_message(self, message):
            assert message['From'] == 'sender@example.com'
            assert message['To'] == 'alice@example.com'
            assert 'Confirm your RC Car email' in message['Subject']
            events.append('send')
    monkeypatch.setattr('accounts.smtplib.SMTP_SSL' if security == 'ssl' else 'accounts.smtplib.SMTP', SMTP)
    app.config.update(ACCOUNT_MAIL_SENDER=None, SMTP_HOST='smtp.example.com', SMTP_PORT=str(port),
                      SMTP_SECURITY=security, SMTP_FROM='sender@example.com',
                      SMTP_USERNAME='sender@example.com', SMTP_PASSWORD='mail-password')
    assert register(client).status_code == 200
    assert events == (['connect','tls','login','send'] if security == 'starttls' else ['connect','login','send'])


def test_mail_not_configured_fails_closed(setup):
    app, client, db, _ = setup
    app.config.update(ACCOUNT_MAIL_SENDER=None, SMTP_HOST='')
    assert register(client).status_code == 503
    assert db.execute('SELECT email_verified FROM users').fetchone()[0] == 0
    assert post(client, '/login', {'username': 'alice', 'password': 'secret123'}).status_code == 403


def test_reset_link_works_with_expired_login_session(setup):
    _, client, _, outbox = setup
    register(client)
    verify(client, outbox)
    post(client, '/login', {'username': 'alice', 'password': 'secret123'})
    post(client, '/forgot-password', {'email': 'alice@example.com'})
    with client.session_transaction() as session:
        session['last_activity'] = '2000-01-01T00:00:00+00:00'
    response = client.get(link(outbox))
    assert response.status_code == 200 and b'Choose a new password' in response.data
