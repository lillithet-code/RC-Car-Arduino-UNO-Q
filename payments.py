"""Server-priced minute purchases with hosted checkout and idempotent fulfillment."""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from urllib import parse, request as http, error as http_error

from flask import Blueprint, abort, has_request_context, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired


PACKAGES = {5: 599, 10: 1099, 15: 1499, 20: 1899, 30: 2599, 60: 5000}
CONFIG_DEFAULTS = {
    'PAYMENTS_BASE_URL': 'https://drive.kbob.org',
    'STRIPE_SECRET_KEY': '', 'STRIPE_WEBHOOK_SECRET': '',
}


class PaymentError(Exception):
    pass


def api_json(url, *, method='GET', data=None, headers=None, form=False):
    headers = dict(headers or {})
    if data is not None:
        body = parse.urlencode(data).encode() if form else json.dumps(data).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    else:
        body = None
    try:
        with http.urlopen(http.Request(url, data=body, headers=headers, method=method), timeout=15) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise ValueError('Invalid provider response')
        return result
    except (http_error.URLError, TimeoutError, OSError, ValueError) as exc:
        # Never expose credentials or raw provider responses in the browser.
        raise PaymentError('The payment provider could not be reached. Please try again.') from exc


def provider_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,255}', value):
        raise PaymentError('Invalid payment reference')
    return value


def stripe_signature_valid(body, header, secret, now=None):
    if not secret:
        return False
    try:
        parts = [item.split('=', 1) for item in header.split(',')]
        timestamps = [value for key, value in parts if key == 't']
        if len(timestamps) != 1:
            return False
        timestamp = timestamps[0]
        if abs((time.time() if now is None else now) - int(timestamp)) > 300:
            return False
        expected = hmac.new(secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(expected, value) for key, value in parts if key == 'v1')
    except (ValueError, TypeError):
        return False


class Payments:
    def __init__(self, app, get_db):
        self.app = app
        self.get_db = get_db

    def enabled(self, provider):
        c = self.app.config
        if provider == 'stripe':
            return bool(c['STRIPE_SECRET_KEY'] and c['STRIPE_WEBHOOK_SECRET'])
        return False

    def base_url(self):
        # Plesk preserves Host, but its backend connection can be HTTP. Use a
        # fixed HTTPS origin for our known public domains, never arbitrary
        # Host/Referer/return_to values supplied by a client.
        if has_request_context():
            host = request.host.lower()
            for domain in ('drive.kbob.org', 'stream-driver.com'):
                if host in (domain, domain + ':443'):
                    return 'https://' + domain
        value = self.app.config['PAYMENTS_BASE_URL'].rstrip('/')
        parsed = parse.urlparse(value)
        if (parsed.scheme != 'https' and not self.app.testing) or not parsed.netloc or parsed.query or parsed.fragment:
            raise PaymentError('Payment checkout is not configured correctly.')
        return value

    def stripe(self, path, *, data=None, idempotency=None):
        headers = {'Authorization': 'Bearer ' + self.app.config['STRIPE_SECRET_KEY']}
        if idempotency:
            headers['Idempotency-Key'] = idempotency
        return api_json('https://api.stripe.com/v1/' + path, method='POST' if data is not None else 'GET',
                        data=data, headers=headers, form=True)

    def purchase(self, purchase_id):
        return self.get_db().execute('SELECT * FROM minute_purchases WHERE id = ?', (purchase_id,)).fetchone()

    def checkout(self, purchase):
        if purchase['provider'] != 'stripe':
            raise PaymentError('This payment method is no longer supported.')
        if purchase['status'] == 'paid':
            return url_for('payments.receipt', purchase_id=purchase['id'])
        if purchase['checkout_url']:
            return purchase['checkout_url']
        base = self.base_url()
        done = base + '/payments/return/' + purchase['id']
        cancel = base + '/payments/purchases/' + purchase['id'] + '?cancelled=1'
        result = self.stripe('checkout/sessions', data={
            'mode': 'payment', 'payment_method_types[0]': 'card',
            'client_reference_id': purchase['id'], 'metadata[purchase_id]': purchase['id'],
            'line_items[0][price_data][currency]': 'usd',
            'line_items[0][price_data][unit_amount]': purchase['amount_cents'],
            'line_items[0][price_data][product_data][name]': f"RC Car — {purchase['minutes']} minutes",
            'line_items[0][quantity]': 1,
            'success_url': done + '?session_id={CHECKOUT_SESSION_ID}', 'cancel_url': cancel,
        }, idempotency='checkout-' + purchase['id'])
        checkout_url = result.get('url', '')
        allowed_hosts = {'checkout.stripe.com'}
        parsed = parse.urlparse(checkout_url)
        if parsed.scheme != 'https' or parsed.hostname not in allowed_hosts:
            raise PaymentError('The provider did not return a valid checkout page.')
        order_id = provider_id(result.get('id'))
        db = self.get_db()
        db.execute('UPDATE minute_purchases SET provider_order_id = ?, checkout_url = ? WHERE id = ?',
                   (order_id, checkout_url, purchase['id']))
        db.commit()
        return checkout_url

    def credit(self, purchase, payment_id):
        db = self.get_db()
        # Status transition and balance increment are one transaction, even if
        # a redirect and multiple webhook workers arrive at the same time.
        with db:
            changed = db.execute("""UPDATE minute_purchases SET status = 'paid', provider_payment_id = ?, paid_at = ?
                                    WHERE id = ? AND status = 'pending'""",
                                 (provider_id(payment_id), datetime.now(timezone.utc).isoformat(), purchase['id']))
            if changed.rowcount:
                updated = db.execute('UPDATE users SET balance = balance + ? WHERE id = ?',
                                     (purchase['minutes'] * 60, purchase['user_id']))
                if updated.rowcount != 1:
                    raise PaymentError('The purchase account no longer exists.')

    def reconcile(self, purchase):
        if purchase['provider'] != 'stripe':
            raise PaymentError('This payment method is no longer supported.')
        if purchase['status'] == 'paid' or not purchase['provider_order_id']:
            return
        order_id = provider_id(purchase['provider_order_id'])
        result = self.stripe('checkout/sessions/' + order_id)
        if result.get('id') != order_id or result.get('client_reference_id') != purchase['id']:
            raise PaymentError('Payment reference did not match.')
        if (result.get('amount_total') != purchase['amount_cents'] or result.get('currency') != 'usd' or
                result.get('mode') != 'payment' or
                result.get('livemode') is not self.app.config['STRIPE_SECRET_KEY'].startswith('sk_live_')):
            raise PaymentError('Payment details did not match the purchased package.')
        if result.get('payment_status') == 'paid' and result.get('status') == 'complete':
            self.credit(purchase, result.get('payment_intent'))


def init_payments(app, get_db):
    for key, default in CONFIG_DEFAULTS.items():
        app.config.setdefault(key, os.environ.get(key, default).strip())
    service = Payments(app, get_db)
    app.extensions['payments'] = service
    with app.app_context():
        db = get_db()
        db.execute('''CREATE TABLE IF NOT EXISTS minute_purchases (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, provider TEXT NOT NULL,
            minutes INTEGER NOT NULL, amount_cents INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'USD',
            status TEXT NOT NULL DEFAULT 'pending', provider_order_id TEXT, provider_payment_id TEXT,
            checkout_url TEXT, created_at TEXT NOT NULL, paid_at TEXT,
            UNIQUE(provider, provider_order_id), UNIQUE(provider, provider_payment_id)
        )''')
        db.commit()
    bp = Blueprint('payments', __name__)
    signer = URLSafeTimedSerializer(app.secret_key, salt='minute-checkout')

    def require_user():
        user = get_db().execute('SELECT id, username, balance FROM users WHERE id = ?', (session.get('user_id'),)).fetchone()
        if not user:
            abort(401)
        return user

    def csrf_token():
        if 'payments_csrf' not in session:
            session['payments_csrf'] = secrets.token_hex(32)
        return session['payments_csrf']

    def check_csrf():
        if not hmac.compare_digest(request.form.get('csrf_token', ''), session.get('payments_csrf', '') or '!'):
            abort(403)

    def owned_purchase(purchase_id):
        user = require_user()
        purchase = service.purchase(purchase_id)
        if not purchase or purchase['user_id'] != user['id']:
            abort(404)
        return purchase

    @bp.get('/buy-minutes')
    def shop():
        if not session.get('user_id'):
            return redirect(url_for('login'))
        user = require_user()
        providers = [name for name in ('stripe',) if service.enabled(name)]
        history = get_db().execute('SELECT * FROM minute_purchases WHERE user_id = ? ORDER BY created_at DESC LIMIT 20',
                                   (user['id'],)).fetchall()
        token = signer.dumps({'id': secrets.token_hex(16), 'user_id': user['id']})
        return render_template('buy_minutes.html', user=user, packages=PACKAGES, providers=providers,
                               history=history, checkout_token=token, csrf_token=csrf_token(), error=None)

    @bp.post('/payments/checkout')
    def checkout():
        user = require_user()
        check_csrf()
        try:
            intent = signer.loads(request.form.get('checkout_token', ''), max_age=3600)
            if intent['user_id'] != user['id']:
                abort(403)
            minutes = int(request.form.get('minutes', ''))
            amount = PACKAGES[minutes]
            provider = request.form.get('provider')
            if not service.enabled(provider):
                raise ValueError()
        except (BadSignature, SignatureExpired, ValueError, KeyError, TypeError):
            return render_template('payment_error.html', message='Choose an available package and payment method from a fresh purchase page.'), 400
        db = get_db()
        db.execute('''INSERT OR IGNORE INTO minute_purchases (id, user_id, provider, minutes, amount_cents, created_at)
                      VALUES (?, ?, ?, ?, ?, ?)''',
                   (intent['id'], user['id'], provider, minutes, amount, datetime.now(timezone.utc).isoformat()))
        db.commit()
        purchase = service.purchase(intent['id'])
        if (purchase['user_id'], purchase['provider'], purchase['minutes']) != (user['id'], provider, minutes):
            return render_template('payment_error.html', message='This checkout is already for a different package. Start a new purchase.'), 409
        try:
            return redirect(service.checkout(purchase), code=303)
        except PaymentError:
            return render_template('payment_receipt.html', purchase=purchase, csrf_token=csrf_token(),
                                   message='Checkout could not be opened. Retry this purchase below; your account has not been credited.'), 502

    @bp.get('/payments/purchases/<purchase_id>')
    def receipt(purchase_id):
        purchase = owned_purchase(purchase_id)
        message = 'Checkout was cancelled. No minutes have been added unless payment was already completed.' if request.args.get('cancelled') else None
        return render_template('payment_receipt.html', purchase=purchase, csrf_token=csrf_token(), message=message)

    @bp.post('/payments/purchases/<purchase_id>/retry')
    def retry(purchase_id):
        purchase = owned_purchase(purchase_id)
        check_csrf()
        if not service.enabled(purchase['provider']):
            return render_template('payment_error.html', message='This payment method is temporarily unavailable.'), 503
        try:
            service.reconcile(purchase)
            purchase = service.purchase(purchase_id)
            if purchase['status'] != 'paid':
                return redirect(service.checkout(purchase), code=303)
        except PaymentError:
            return render_template('payment_receipt.html', purchase=purchase, csrf_token=csrf_token(),
                                   message='Payment confirmation is unavailable. Try again later; do not pay a second time if already charged.'), 502
        return redirect(url_for('payments.receipt', purchase_id=purchase_id), code=303)

    @bp.get('/payments/return/<purchase_id>')
    def payment_return(purchase_id):
        purchase = owned_purchase(purchase_id)
        try:
            # Browser parameters never determine price, credit, or payment ID.
            service.reconcile(purchase)
        except PaymentError:
            pass  # Signed webhooks can finish fulfillment if the browser leaves.
        return redirect(url_for('payments.receipt', purchase_id=purchase_id))

    @bp.get('/api/payments/purchases/<purchase_id>')
    def purchase_status(purchase_id):
        purchase = owned_purchase(purchase_id)
        return jsonify(status=purchase['status'], minutes=purchase['minutes'])

    @bp.post('/payments/webhooks/stripe')
    def stripe_webhook():
        if not service.enabled('stripe'):
            return '', 503
        if request.content_length and request.content_length > 262144:
            abort(413)
        raw = request.get_data()
        if not stripe_signature_valid(raw, request.headers.get('Stripe-Signature', ''), app.config['STRIPE_WEBHOOK_SECRET']):
            return '', 400
        try:
            event = json.loads(raw)
            if event.get('type') not in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
                return '', 200
            obj = event['data']['object']
            purchase = service.purchase(obj.get('client_reference_id'))
            if not purchase or purchase['provider'] != 'stripe':
                return '', 200
            if not purchase['provider_order_id']:
                return '', 503  # Checkout creation may still be committing.
            if obj.get('id') != purchase['provider_order_id']:
                return '', 400
            service.reconcile(purchase)
        except (ValueError, KeyError, TypeError, AttributeError):
            return '', 400
        except PaymentError:
            return '', 503
        return '', 200

    app.register_blueprint(bp)
