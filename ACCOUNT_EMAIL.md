# Account confirmation and password reset

New registrations start with **0 minutes** and cannot sign in, book, buy minutes, or control a car until the email address is confirmed. A confirmation email contains a link valid for 24 hours. The recipient opens it and presses **Confirm email**; simply opening a link does not activate an account (email scanners may open links).

The login page includes **Forgot your password?** and **Resend confirmation email**. Password-reset links expire after 30 minutes. Successfully resetting a password consumes all outstanding account tokens, invalidates existing login sessions, and releases any active car session with the usual unused-time refund. Password reset alone does not enable an unverified account; request a new confirmation email if needed.

Existing accounts remain enabled and keep their balances and administrator permissions. This migration does not require existing users to reconfirm their addresses. The first new account on an empty installation retains the existing first-administrator behavior but must confirm its email before signing in.

## Plesk mail setup

1. In Plesk, open **Mail** for your domain. Create a sending mailbox, for example `noreply@kbob.org`, or choose an existing mailbox.
2. Open that mailbox's mail-client configuration information and note its outgoing SMTP hostname, secure port and full email username. Use the hostname supplied by your hosting provider, which must match its TLS certificate. Do not assume it is the website hostname.
3. Use authenticated SMTP with either STARTTLS (usually port 587) or implicit TLS (usually port 465). This app deliberately does not send credentials over unencrypted SMTP.
4. Enter the mailbox settings privately in the web server's existing `.env`. Keep your Stripe, database and other settings.

```bash
sudo nano /var/www/vhosts/drive.kbob.org/httpdocs/rc-car-arduino-uno-q/.env
```

Example for port 587 (replace the example values with the actual mailbox settings):

```dotenv
ACCOUNT_BASE_URL=https://drive.kbob.org
SMTP_HOST=YOUR_PLESK_SMTP_HOSTNAME
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=noreply@kbob.org
SMTP_PASSWORD=YOUR_MAILBOX_PASSWORD
SMTP_FROM=noreply@kbob.org
```

For port 465, set `SMTP_PORT=465` and `SMTP_SECURITY=ssl`. `SMTP_FROM` must be a plain email address, normally the authenticated mailbox. It must be permitted as a sender by your mail server. Do not paste mailbox passwords into chat or commit `.env`. If a password contains spaces or characters that need quoting, use systemd EnvironmentFile quoting (for example double quotes); do not add an `export` prefix. The installer preserves these entries on updates.

The public base URL controls links in outgoing emails. It must use HTTPS and point to this app. Host headers from visitors are not used to build account links. No new Python dependencies are needed: delivery uses the standard library SMTP client with TLS certificate validation.

## Update the website

Run from your source checkout:

```bash
cd "$HOME/rc-car-source" &&
git pull --ff-only origin main &&
APP_DIR=/var/www/vhosts/drive.kbob.org/httpdocs/rc-car-arduino-uno-q \
  bash install_server_linux.sh
```

After configuring SMTP, restart the service:

```bash
sudo systemctl restart --no-block rc-car-web
sudo systemctl --no-pager status rc-car-web
```

If the service is still restarting, check its status again after a few seconds. Set SMTP up before inviting new registrations. With mail unconfigured or failing, new accounts remain disabled; once mail is fixed, they can request a new confirmation email without registering again. Existing accounts continue to work.

## Check delivery and account behavior

1. Register a new test account with an inbox you control. Check the inbox and spam folder for confirmation. Before confirming, login must be blocked.
2. Open the link, confirm the address, and sign in. The available balance must be **0 minutes**.
3. Sign out, select **Forgot your password?**, and enter the registered email. Follow the mail link, enter matching new passwords, and sign in with the new password. The old password and used reset link must no longer work.
4. Try **Resend confirmation email** for a second account that has not been confirmed.

Use the Plesk mail logs and the application's log to diagnose failed delivery:

```bash
sudo journalctl -u rc-car-web -n 100 --no-pager
```

The app logs a generic mail failure without credentials or link tokens. SMTP acceptance is not proof the email reached the inbox; check the sending domain's SPF/DKIM settings and mail-provider delivery logs if messages go missing. Do not share reset links or secrets in screenshots or logs. Account-link URLs can appear in reverse-proxy access logs; restrict log access and redact these paths if exporting logs.

Recovery/resend requests always show the same response, including unknown emails and mail errors, to avoid directly revealing account existence. Mail requests are limited per email address and connection IP using the database (5 per hour per email/action and 20 per hour per IP/action). Behind a proxy, the app uses the direct connection IP; do not blindly trust a public `X-Forwarded-For` header. On busy sites, configure trusted proxy handling before adjusting limits.

Automated tests use a fake mail sender and never send real email:

```bash
python -m pytest tests/test_accounts.py tests/test_app.py tests/test_payments.py tests/test_admin_controls.py -q
```

Production SMTP delivery must still be tested using your Plesk mailbox after deployment.

Reference: [Plesk mailbox client configuration](https://support.plesk.com/hc/en-us/articles/12377322626327-How-to-configure-a-mail-client-for-a-mailbox-created-in-Plesk).
