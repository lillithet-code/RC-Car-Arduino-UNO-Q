# Minute packages: PayPal and Stripe

The dashboard's **Buy minutes** link opens `/buy-minutes`. Users select a package and one of the configured providers. Prices are fixed on the server in `payments.py`; no Stripe products or PayPal catalog entries need to be created.

| Minutes | Price (USD) |
| --- | --- |
| 5 | $5.99 |
| 10 | $10.99 |
| 15 | $14.99 |
| 20 | $18.99 |
| 30 | $25.99 |
| 60 | $50.00 |

Payments are one-time purchases, not subscriptions. Stripe provides card checkout; PayPal provides PayPal checkout. Only configured providers appear. With neither configured, purchases are unavailable and the existing driving controls still work.

## Install and configure

Update from your **source checkout**, not the installed directory (which does not contain `.git`):

```bash
cd "$HOME/rc-car-source" &&
git pull --ff-only origin main &&
APP_DIR=/var/www/vhosts/drive.kbob.org/httpdocs/rc-car-arduino-uno-q \
  bash install_server_linux.sh
```

The installer preserves the payment settings below when updating. The purchase table is created automatically at startup without changing existing balances. Back up your database before an update. Keep the database across deployments: it holds both the purchased balances and the records that prevent duplicate credits.

Edit the deployed `.env` privately:

```bash
sudo nano /var/www/vhosts/drive.kbob.org/httpdocs/rc-car-arduino-uno-q/.env
```

Set the values below (start with test/sandbox credentials). Replace placeholders; do not commit credentials or paste secrets into chat.

```dotenv
PAYMENTS_BASE_URL=https://drive.kbob.org
STRIPE_SECRET_KEY=sk_test_REPLACE_ME
STRIPE_WEBHOOK_SECRET=whsec_REPLACE_ME
PAYPAL_ENVIRONMENT=sandbox
PAYPAL_CLIENT_ID=REPLACE_ME
PAYPAL_CLIENT_SECRET=REPLACE_ME
PAYPAL_WEBHOOK_ID=REPLACE_ME
```

Leave a provider's credential fields empty to disable it. `PAYMENTS_BASE_URL` must be the public HTTPS origin serving this Flask app. It must route both payment webhook paths to the app without a login wall or proxy challenge. Keep a stable, private `SECRET_KEY`; the installer already generates one. Production runs through systemd's `EnvironmentFile`; if running Flask manually, supply these variables in the process environment.

After editing:

```bash
sudo systemctl restart rc-car-web
sudo systemctl --no-pager status rc-car-web
```

### Stripe

1. In your Stripe account's test environment, obtain the standard secret API key (`sk_test_…`). Put it in `STRIPE_SECRET_KEY`.
2. Create a webhook destination at **`https://drive.kbob.org/payments/webhooks/stripe`** for:
   - `checkout.session.completed`
   - `checkout.session.async_payment_succeeded`
3. Put that endpoint's signing secret (`whsec_…`) in `STRIPE_WEBHOOK_SECRET`.
4. Complete a hosted checkout using Stripe's documented test card details. Check that the purchase becomes **Minutes added**, and redeliver the event to verify it does not credit again.
5. For production, replace both with the live account's secret key (`sk_live_…`) and live webhook signing secret; configure the same event types in the live environment. Restart the service.

See [Stripe hosted checkout fulfillment](https://docs.stripe.com/checkout/fulfillment) and [Stripe test payments](https://docs.stripe.com/testing).

### PayPal

1. Create a REST app in the PayPal developer dashboard. Use its sandbox client ID and client secret first.
2. Add a webhook to **that app** at **`https://drive.kbob.org/payments/webhooks/paypal`** for:
   - `CHECKOUT.ORDER.APPROVED`
   - `PAYMENT.CAPTURE.COMPLETED`
3. Copy the webhook's ID into `PAYPAL_WEBHOOK_ID`. This is the webhook registration ID, not the app's client ID.
4. Keep `PAYPAL_ENVIRONMENT=sandbox`, restart, and complete checkout with a sandbox buyer account. Approval alone does not credit minutes: the server captures and verifies the payment.
5. For production, use the live app's client ID, secret and webhook ID, set `PAYPAL_ENVIRONMENT=live`, and restart. Register the same events in the live app.

See [PayPal checkout integration](https://developer.paypal.com/studio/checkout/standard/integrate), [Orders API](https://developer.paypal.com/api/orders/v2) and [webhooks](https://developer.paypal.com/api/rest/webhooks/rest/).

Complete pending test purchases before changing environment or account. Old sandbox purchases cannot be verified using live credentials. Test both providers in their sandbox environments before accepting real payments; mocked automated tests do not prove merchant onboarding or webhook delivery is configured.

## Credit and recovery behavior

- The server chooses the price and currency, checks purchase ownership, and verifies the actual order with the provider. Browser return parameters cannot grant credit. Card details are entered on the provider's hosted checkout page, never stored in this app.
- Webhooks are authenticated. A verified successful payment updates the purchase and adds `minutes × 60` to the user's balance in one SQLite transaction. Repeated or concurrent notifications cannot add the same purchase twice.
- Purchased time goes into the user's available balance for their **next booking**. It does not extend a currently running session. Existing registration credits and admin balance adjustments are unchanged.
- Users can reopen a receipt from purchase history and use **Check payment / resume checkout**. This rechecks the existing order before returning to its checkout; it does not deliberately create a second order. An expired hosted checkout may require a new purchase. If already charged, do not pay again: inspect webhook delivery and the order in the provider dashboard first.
- If the browser closes, webhooks still fulfill the payment. If a provider request fails, relevant webhooks return an error so delivery can retry. Monitor failed deliveries in each provider dashboard and redeliver after recovery.
- Refunds and disputes are not automatically reflected in driving balances. Handle the payment in the provider dashboard and adjust remaining time through the existing admin controls. The app does not calculate taxes or issue tax invoices; configure your business's pricing and receipts separately as needed.

## Automated checks

```bash
python -m pytest tests/test_payments.py -q
```

Tests use fake provider responses and do not charge money. They cover package prices, ownership, CSRF, signatures, incomplete and mismatched payments, capture failure recovery, duplicate delivery and concurrent crediting.
