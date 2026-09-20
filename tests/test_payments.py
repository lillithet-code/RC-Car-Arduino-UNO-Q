import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import create_app
from payments import PACKAGES, PaymentError, stripe_signature_valid


@pytest.fixture
def env():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    app = create_app({'TESTING': True, 'SECRET_KEY': 'test-only', 'DATABASE_URL': 'sqlite:///' + path,
                      'STRIPE_SECRET_KEY': 'sk_test_example', 'STRIPE_WEBHOOK_SECRET': 'whsec_example',
                      'PAYMENTS_BASE_URL': 'https://drive.example'})
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    for number in (1, 2):
        db.execute('INSERT INTO users (id, username, email, password_hash, balance) VALUES (?, ?, ?, ?, 0)',
                   (number, f'user{number}', f'{number}@example.com', 'unused'))
    db.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = 1
    yield app, db, client, app.extensions['payments']
    db.close()
    os.unlink(path)


def form(client, provider='stripe', minutes=5):
    page = client.get('/buy-minutes')
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    return {name: re.search(r'name="' + name + r'" value="([^"]+)"', html)[1]
            for name in ('csrf_token', 'checkout_token')} | {'provider': provider, 'minutes': minutes}


def pending(env, monkeypatch, provider='stripe', minutes=5):
    app, db, client, service = env
    monkeypatch.setattr(service, 'stripe', lambda *a, **k: {'id': 'cs_order', 'url': 'https://checkout.stripe.com/c/pay/test'})
    assert client.post('/payments/checkout', data=form(client, provider, minutes)).status_code == 303
    return dict(db.execute('SELECT * FROM minute_purchases').fetchone())


def stripe_paid(purchase):
    return {'id': purchase['provider_order_id'], 'client_reference_id': purchase['id'],
            'amount_total': purchase['amount_cents'], 'currency': 'usd', 'mode': 'payment',
            'livemode': False, 'status': 'complete', 'payment_status': 'paid', 'payment_intent': 'pi_paid'}


def signature(body, timestamp=None):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(b'whsec_example', timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
    return f't={timestamp},v1={digest}'


def stripe_event(client, purchase):
    raw = json.dumps({'type': 'checkout.session.completed', 'data': {'object': {
        'id': purchase['provider_order_id'], 'client_reference_id': purchase['id']}}}).encode()
    return client.post('/payments/webhooks/stripe', data=raw, headers={'Stripe-Signature': signature(raw)})


def balance(db):
    return db.execute('SELECT balance FROM users WHERE id=1').fetchone()[0]


def test_shop_prices_methods_and_disabled_state(env):
    app, _, client, _ = env
    html = client.get('/buy-minutes').get_data(as_text=True)
    assert PACKAGES == {5: 599, 10: 1099, 15: 1499, 20: 1899, 30: 2599, 60: 5000}
    for minutes, cents in PACKAGES.items():
        assert f'{minutes} minutes' in html and f'${cents/100:.2f}' in html
    assert 'PayPal' not in html and 'Credit / debit card' in html
    assert 'sk_test_example' not in html and 'test-secret' not in html
    app.config.update(STRIPE_SECRET_KEY='')
    html = client.get('/buy-minutes').get_data(as_text=True)
    assert 'Purchases are temporarily unavailable' in html
    assert 'name="provider"' not in html


@pytest.mark.parametrize('minutes,cents', list(PACKAGES.items()))
def test_server_prices_checkout_and_reuses_order(env, monkeypatch, minutes, cents):
    provider = 'stripe'
    app, db, client, service = env
    calls = []
    def api(path, **kwargs):
        calls.append((path, kwargs))
        return {'id': 'ORDER1', 'url': 'https://checkout.stripe.com/c/pay/1'}
    monkeypatch.setattr(service, provider, api)
    data = form(client, provider, minutes) | {'amount': 1, 'amount_cents': 1, 'currency': 'EUR', 'user_id': 2}
    first = client.post('/payments/checkout', data=data)
    second = client.post('/payments/checkout', data=data)
    assert first.status_code == second.status_code == 303
    assert first.location == second.location and len(calls) == 1
    purchase = db.execute('SELECT * FROM minute_purchases').fetchone()
    assert purchase['amount_cents'] == cents and purchase['user_id'] == 1 and balance(db) == 0
    payload = calls[0][1]['data']
    assert payload['line_items[0][price_data][unit_amount]'] == cents
    assert payload['line_items[0][price_data][currency]'] == 'usd'
    assert calls[0][1]['idempotency']


@pytest.mark.parametrize('host,expected', [
    ('drive.kbob.org', 'https://drive.kbob.org'),
    ('stream-driver.com', 'https://stream-driver.com'),
    ('STREAM-DRIVER.COM:443', 'https://stream-driver.com'),
    ('stream-driver.com.evil.example', 'https://drive.example'),
    ('stream-driver.com:8000', 'https://drive.example'),
    ('evil.example', 'https://drive.example'),
])
def test_checkout_return_domain_is_allowed_request_host(env, monkeypatch, host, expected):
    app, db, client, service = env
    purchase = pending(env, monkeypatch)
    purchase['checkout_url'] = None
    calls = []
    def api(path, **kwargs):
        calls.append(kwargs['data'])
        return {'id': 'cs_domain', 'url': 'https://checkout.stripe.com/c/pay/domain'}
    monkeypatch.setattr(service, 'stripe', api)
    with app.test_request_context('/payments/checkout', base_url='http://' + host,
                                  headers={'Referer': 'https://evil.example',
                                           'X-Forwarded-Host': 'evil.example'}):
        service.checkout(purchase)
        # Existing sessions must stay reusable without creating another charge.
        service.checkout(service.purchase(purchase['id']))
    assert len(calls) == 1
    assert calls[0]['success_url'] == expected + '/payments/return/' + purchase['id'] + '?session_id={CHECKOUT_SESSION_ID}'
    assert calls[0]['cancel_url'] == expected + '/payments/purchases/' + purchase['id'] + '?cancelled=1'


def test_payment_base_outside_request_uses_configuration(env):
    assert env[3].base_url() == 'https://drive.example'


@pytest.mark.parametrize('override', [{'minutes': 1}, {'minutes': -5}, {'minutes': '5.5'}, {'provider': 'fake'}, {'checkout_token': 'tampered'}])
def test_invalid_checkout(env, override):
    _, db, client, _ = env
    assert client.post('/payments/checkout', data=form(client) | override).status_code == 400
    assert db.execute('SELECT COUNT(*) FROM minute_purchases').fetchone()[0] == 0


def test_auth_csrf_and_ownership(env, monkeypatch):
    app, db, client, service = env
    anon = app.test_client()
    assert anon.get('/buy-minutes').status_code == 302
    assert anon.post('/payments/checkout').status_code == 401
    assert client.post('/payments/checkout', data=form(client) | {'csrf_token': 'bad'}).status_code == 403
    purchase = pending(env, monkeypatch)
    with client.session_transaction() as session:
        session['user_id'] = 2
    for path in (f"/payments/purchases/{purchase['id']}", f"/payments/return/{purchase['id']}", f"/api/payments/purchases/{purchase['id']}"):
        assert client.get(path).status_code == 404
    assert client.post(f"/payments/purchases/{purchase['id']}/retry").status_code == 404
    assert purchase['id'] not in client.get('/buy-minutes').get_data(as_text=True)


def test_stripe_webhook_and_browser_return_credit_once(env, monkeypatch):
    app, db, client, service = env
    purchase = pending(env, monkeypatch, minutes=60)
    monkeypatch.setattr(service, 'stripe', lambda *a, **k: stripe_paid(purchase))
    # Provider delivery has no login cookie.
    assert stripe_event(app.test_client(), purchase).status_code == 200
    assert stripe_event(client, purchase).status_code == 200
    assert client.get(f"/payments/return/{purchase['id']}?session_id=forged").status_code == 302
    assert balance(db) == 3600
    assert client.get(f"/api/payments/purchases/{purchase['id']}").json['status'] == 'paid'
    assert '60 minutes have been added' in client.get(f"/payments/purchases/{purchase['id']}").get_data(as_text=True)


@pytest.mark.parametrize('field,value', [('id', 'different'), ('client_reference_id', 'other'), ('amount_total', 1),
    ('currency', 'eur'), ('mode', 'subscription'), ('livemode', True), ('payment_status', 'unpaid'), ('status', 'open')])
def test_stripe_unpaid_or_mismatched_orders_never_credit(env, monkeypatch, field, value):
    _, db, client, service = env
    purchase = pending(env, monkeypatch)
    result = stripe_paid(purchase) | {field: value}
    monkeypatch.setattr(service, 'stripe', lambda *a, **k: result)
    client.get(f"/payments/return/{purchase['id']}?paid=true")
    assert balance(db) == 0


def test_stripe_rejects_bad_signatures_and_malformed_payloads(env):
    app, _, client, _ = env
    for raw, header in [(b'{}', ''), (b'{}', signature(b'{}', 1)), (b'{"changed":true}', signature(b'{}')),
                        (b'[]', signature(b'[]'))]:
        assert client.post('/payments/webhooks/stripe', data=raw, headers={'Stripe-Signature': header}).status_code == 400
    assert stripe_signature_valid(b'{}', signature(b'{}'), 'whsec_example')
    assert not stripe_signature_valid(b'{}', 'malformed', 'whsec_example')


def test_atomic_credit_under_concurrent_deliveries(env, monkeypatch):
    app, db, _, service = env
    purchase = pending(env, monkeypatch)
    def credit(_):
        with app.app_context():
            service.credit(purchase, 'pi_same')
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(credit, range(16)))
    assert balance(db) == 300


def test_failed_account_credit_rolls_back_purchase(env, monkeypatch):
    app, db, _, service = env
    purchase = pending(env, monkeypatch)
    db.execute('DELETE FROM users WHERE id=1')
    db.commit()
    with app.app_context(), pytest.raises(PaymentError):
        service.credit(purchase, 'pi_missing')
    assert db.execute('SELECT status FROM minute_purchases').fetchone()[0] == 'pending'


def test_provider_failure_keeps_recoverable_purchase_and_no_credit(env, monkeypatch):
    _, db, client, service = env
    def api(*args, **kwargs):
        raise PaymentError('temporary failure')
    monkeypatch.setattr(service, 'stripe', api)
    response = client.post('/payments/checkout', data=form(client))
    assert response.status_code == 502
    assert 'Retry this purchase' in response.get_data(as_text=True)
    assert balance(db) == 0
    assert db.execute('SELECT COUNT(*) FROM minute_purchases').fetchone()[0] == 1


def test_balance_and_purchase_survive_restart(env, monkeypatch):
    app, db, _, service = env
    purchase = pending(env, monkeypatch)
    with app.app_context():
        service.credit(purchase, 'pi_paid')
    restarted = create_app(dict(app.config))
    with restarted.app_context():
        assert restarted.extensions['payments'].purchase(purchase['id'])['status'] == 'paid'
    assert balance(db) == 300


def test_paypal_cannot_be_enabled_or_used(env):
    app, db, client, service = env
    app.config.update(PAYPAL_CLIENT_ID='old-id', PAYPAL_CLIENT_SECRET='old-secret',
                      PAYPAL_WEBHOOK_ID='old-hook', PAYPAL_ENVIRONMENT='live')
    assert not service.enabled('paypal')
    assert client.post('/payments/checkout', data=form(client, 'paypal')).status_code == 400
    assert client.post('/payments/webhooks/paypal', json={}).status_code == 404
    assert db.execute('SELECT COUNT(*) FROM minute_purchases').fetchone()[0] == 0


def test_legacy_purchase_is_retained_but_cannot_resume(env, monkeypatch):
    app, db, client, service = env
    purchase = pending(env, monkeypatch)
    db.execute("UPDATE minute_purchases SET provider='paypal', checkout_url='https://www.paypal.com/checkoutnow' WHERE id=?",
               (purchase['id'],))
    db.commit()
    def unexpected(*args, **kwargs):
        pytest.fail('Legacy purchases must not contact Stripe')
    monkeypatch.setattr(service, 'stripe', unexpected)
    html = client.get(f"/payments/purchases/{purchase['id']}").get_data(as_text=True)
    assert 'no longer supported' in html and 'resume checkout' not in html
    assert client.post(f"/payments/purchases/{purchase['id']}/retry", data=form(client)).status_code == 503
    assert client.get(f"/payments/return/{purchase['id']}").status_code == 302
    assert balance(db) == 0
    assert db.execute('SELECT COUNT(*) FROM minute_purchases').fetchone()[0] == 1
    db.execute("UPDATE minute_purchases SET status='paid'")
    db.execute('UPDATE users SET balance=300 WHERE id=1')
    db.commit()
    restarted = create_app(dict(app.config))
    with restarted.app_context():
        assert restarted.extensions['payments'].purchase(purchase['id'])['status'] == 'paid'
    assert balance(db) == 300
