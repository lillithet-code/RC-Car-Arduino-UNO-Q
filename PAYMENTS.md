# Minute packages: Stripe

The dashboard's **Buy minutes** link opens `/buy-minutes`. Users select a package and pay by credit or debit card through Stripe. Prices are fixed on the server in `payments.py`; no Stripe products need to be created.

| Minutes | Price (USD) |
| --- | --- |
| 5 | $5.99 |
| 10 | $10.99 |
| 15 | $14.99 |
| 20 | $18.99 |
| 30 | $25.99 |
| 60 | $50.00 |

Payments are one-time purchases, not subscriptions. Stripe provides card checkout. Without Stripe credentials, purchases are unavailable and the existing driving controls still work.

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
```

Leave the Stripe credential fields empty to disable purchases. `PAYMENTS_BASE_URL` must be the public HTTPS origin serving this Flask app. It must route the Stripe webhook path to the app without a login wall or proxy challenge. Keep a stable, private `SECRET_KEY`; the installer already generates one. Production runs through systemd's `EnvironmentFile`; if running Flask manually, supply these variables in the process environment.

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

Complete pending test purchases before changing environment or account. Test purchases cannot be verified using live credentials. Test Stripe checkout before accepting real payments; mocked automated tests do not prove merchant onboarding or webhook delivery is configured.

PayPal has been removed. Old `PAYPAL_*` environment settings are ignored and the installer removes them when rewriting `.env`. Existing balances and purchase history are retained. Pending purchases from the removed provider cannot be resumed or automatically fulfilled; contact the administrator to reconcile any payment already made. Remove any old PayPal webhook registration from your merchant dashboard.

## Credit and recovery behavior

- The server chooses the price and currency, checks purchase ownership, and verifies the actual order with the provider. Browser return parameters cannot grant credit. Card details are entered on the provider's hosted checkout page, never stored in this app.
- Webhooks are authenticated. A verified successful payment updates the purchase and adds `minutes × 60` to the user's balance in one SQLite transaction. Repeated or concurrent notifications cannot add the same purchase twice.
- Purchased time goes into the user's available balance for their **next booking**. It does not extend a currently running session. New accounts start with zero minutes; existing balances and admin balance adjustments are preserved.
- Users can reopen a receipt from purchase history and use **Check payment / resume checkout**. This rechecks the existing order before returning to its checkout; it does not deliberately create a second order. An expired hosted checkout may require a new purchase. If already charged, do not pay again: inspect webhook delivery and the order in the provider dashboard first.
- If the browser closes, webhooks still fulfill the payment. If a provider request fails, relevant webhooks return an error so delivery can retry. Monitor failed deliveries in the Stripe dashboard and redeliver after recovery.
- Refunds and disputes are not automatically reflected in driving balances. Handle the payment in the provider dashboard and adjust remaining time through the existing admin controls. The app does not calculate taxes or issue tax invoices; configure your business's pricing and receipts separately as needed.

## Automated checks

```bash
python -m pytest tests/test_payments.py -q
```

Tests use fake provider responses and do not charge money. They cover package prices, ownership, CSRF, signatures, incomplete and mismatched payments, provider failure recovery, duplicate delivery and concurrent crediting.
