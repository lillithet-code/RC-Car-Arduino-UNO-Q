import copy
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
                      'PAYPAL_CLIENT_ID': 'test-client', 'PAYPAL_CLIENT_SECRET': 'test-secret',
                      'PAYPAL_WEBHOOK_ID': 'test-hook', 'PAYPAL_ENVIRONMENT': 'sandbox',
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
    if provider == 'stripe':
        monkeypatch.setattr(service, 'stripe', lambda *a, **k: {'id': 'cs_order', 'url': 'https://checkout.stripe.com/c/pay/test'})
    else:
        monkeypatch.setattr(service, 'paypal', lambda *a, **k: {'id': 'PPORDER', 'links': [
            {'rel': 'payer-action', 'href': 'https://www.sandbox.paypal.com/checkoutnow?token=PPORDER'}]})
    assert client.post('/payments/checkout', data=form(client, provider, minutes)).status_code == 303
    return dict(db.execute('SELECT * FROM minute_purchases').fetchone())


def stripe_paid(purchase):
    return {'id': purchase['provider_order_id'], 'client_reference_id': purchase['id'],
            'amount_total': purchase['amount_cents'], 'currency': 'usd', 'mode': 'payment',
            'livemode': False, 'status': 'complete', 'payment_status': 'paid', 'payment_intent': 'pi_paid'}


def paypal_order(purchase, status='COMPLETED'):
    return {'id': purchase['provider_order_id'], 'intent': 'CAPTURE', 'status': status,
            'purchase_units': [{'custom_id': purchase['id'],
                                'amount': {'currency_code': 'USD', 'value': f"{purchase['amount_cents']/100:.2f}"},
                                'payments': {'captures': [{'id': 'CAPTURE1', 'status': 'COMPLETED',
                                                          'amount': {'currency_code': 'USD', 'value': f"{purchase['amount_cents']/100:.2f}"}}]}}]}


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
    assert 'PayPal' in html and 'Credit / debit card' in html
    assert 'sk_test_example' not in html and 'test-secret' not in html
    app.config.update(STRIPE_SECRET_KEY='', PAYPAL_CLIENT_SECRET='')
    html = client.get('/buy-minutes').get_data(as_text=True)
    assert 'Purchases are temporarily unavailable' in html
    assert 'name="provider"' not in html


@pytest.mark.parametrize('minutes,cents', list(PACKAGES.items()))
@pytest.mark.parametrize('provider', ['stripe', 'paypal'])
def test_server_prices_checkout_and_reuses_order(env, monkeypatch, minutes, cents, provider):
    app, db, client, service = env
    calls = []
    def api(path, **kwargs):
        calls.append((path, kwargs))
        return {'id': 'ORDER1', 'url': 'https://checkout.stripe.com/c/pay/1',
                'links': [{'rel': 'payer-action', 'href': 'https://www.sandbox.paypal.com/checkoutnow?token=ORDER1'}]}
    monkeypatch.setattr(service, provider, api)
    data = form(client, provider, minutes) | {'amount': 1, 'amount_cents': 1, 'currency': 'EUR', 'user_id': 2}
    first = client.post('/payments/checkout', data=data)
    second = client.post('/payments/checkout', data=data)
    assert first.status_code == second.status_code == 303
    assert first.location == second.location and len(calls) == 1
    purchase = db.execute('SELECT * FROM minute_purchases').fetchone()
    assert purchase['amount_cents'] == cents and purchase['user_id'] == 1 and balance(db) == 0
    payload = calls[0][1]['data']
    if provider == 'stripe':
        assert payload['line_items[0][price_data][unit_amount]'] == cents
        assert payload['line_items[0][price_data][currency]'] == 'usd'
    else:
        assert payload['purchase_units'][0]['amount'] == {'currency_code': 'USD', 'value': f'{cents/100:.2f}'}
    assert calls[0][1]['idempotency']


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


def test_paypal_capture_and_duplicate_return(env, monkeypatch):
    _, db, client, service = env
    purchase = pending(env, monkeypatch, 'paypal', 30)
    captured = []
    def api(path, **kwargs):
        if path.endswith('/capture'):
            captured.append(kwargs['idempotency'])
            return {'id': purchase['provider_order_id'], 'status': 'COMPLETED'}
        return paypal_order(purchase, 'COMPLETED' if captured else 'APPROVED')
    monkeypatch.setattr(service, 'paypal', api)
    for _ in range(2):
        assert client.get(f"/payments/return/{purchase['id']}?token=forged&PayerID=forged").status_code == 302
    assert balance(db) == 1800 and captured == ['cap-' + purchase['id']]


@pytest.mark.parametrize('variant', ['amount', 'currency', 'reference', 'id', 'intent', 'capture_amount', 'capture_currency', 'pending', 'multiple'])
def test_paypal_invalid_or_pending_orders_never_credit(env, monkeypatch, variant):
    _, db, client, service = env
    purchase = pending(env, monkeypatch, 'paypal')
    result = paypal_order(purchase)
    unit = result['purchase_units'][0]
    capture = unit['payments']['captures'][0]
    if variant == 'amount': unit['amount']['value'] = '0.01'
    if variant == 'currency': unit['amount']['currency_code'] = 'EUR'
    if variant == 'reference': unit['custom_id'] = 'someone-else'
    if variant == 'id': result['id'] = 'OTHER'
    if variant == 'intent': result['intent'] = 'AUTHORIZE'
    if variant == 'capture_amount': capture['amount']['value'] = '0.01'
    if variant == 'capture_currency': capture['amount']['currency_code'] = 'EUR'
    if variant == 'pending': capture['status'] = 'PENDING'
    if variant == 'multiple': unit['payments']['captures'].append(copy.deepcopy(capture))
    monkeypatch.setattr(service, 'paypal', lambda *a, **k: result)
    client.get(f"/payments/return/{purchase['id']}")
    assert balance(db) == 0


def paypal_event(purchase, kind='PAYMENT.CAPTURE.COMPLETED'):
    return {'event_type': kind, 'resource': {'id': purchase['provider_order_id'],
            'supplementary_data': {'related_ids': {'order_id': purchase['provider_order_id']}}}}


def paypal_headers():
    return {name: 'test-value' for name in ('PAYPAL-AUTH-ALGO', 'PAYPAL-CERT-URL', 'PAYPAL-TRANSMISSION-ID',
                                           'PAYPAL-TRANSMISSION-SIG', 'PAYPAL-TRANSMISSION-TIME')}


def test_paypal_webhook_authenticates_and_fetches_authoritative_order(env, monkeypatch):
    app, db, _, service = env
    purchase = pending(env, monkeypatch, 'paypal', 20)
    verified = []
    def api(path, **kwargs):
        if path.endswith('verify-webhook-signature'):
            verified.append(kwargs['data'])
            return {'verification_status': 'SUCCESS'}
        return paypal_order(purchase)
    monkeypatch.setattr(service, 'paypal', api)
    client = app.test_client()
    assert client.post('/payments/webhooks/paypal', json=paypal_event(purchase)).status_code == 400
    for _ in range(2):
        assert client.post('/payments/webhooks/paypal', json=paypal_event(purchase), headers=paypal_headers()).status_code == 200
    assert balance(db) == 1200 and verified[0]['webhook_id'] == 'test-hook'


def test_paypal_failed_verification_never_credits(env, monkeypatch):
    _, db, client, service = env
    purchase = pending(env, monkeypatch, 'paypal')
    monkeypatch.setattr(service, 'paypal', lambda *a, **k: {'verification_status': 'FAILURE'})
    assert client.post('/payments/webhooks/paypal', json=paypal_event(purchase), headers=paypal_headers()).status_code == 400
    assert balance(db) == 0


def test_paypal_capture_failure_requests_webhook_retry_then_recovers(env, monkeypatch):
    _, db, client, service = env
    purchase = pending(env, monkeypatch, 'paypal')
    monkeypatch.setattr(service, 'verify_paypal_event', lambda *a: True)
    def api(path, **kwargs):
        if path.endswith('/capture'):
            raise PaymentError('Temporary failure')
        return paypal_order(purchase, 'APPROVED')
    monkeypatch.setattr(service, 'paypal', api)
    event = paypal_event(purchase, 'CHECKOUT.ORDER.APPROVED')
    assert client.post('/payments/webhooks/paypal', json=event).status_code == 503
    assert balance(db) == 0
    monkeypatch.setattr(service, 'paypal', lambda *a, **k: paypal_order(purchase))
    assert client.post('/payments/webhooks/paypal', json=event).status_code == 200
    assert balance(db) == 300


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
