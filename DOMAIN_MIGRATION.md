# Move drive.kbob.org to stream-driver.com

This script is for the existing Plesk server at 217.154.249.28. It keeps the
current application directory, database, accounts, balances, SMTP settings,
service user, and secrets. The old domain stays functional for boards and links
that have not yet switched. It does not change DNS, install a certificate,
delete the old website, or redirect old URLs.

## Before running

1. In the authoritative DNS manager, set `stream-driver.com`'s A record to
   `217.154.249.28`. Remove incorrect AAAA records. Preserve mail records.
   The script intentionally requires IPv4-only DNS for this deployment.
2. In Plesk, enable website hosting and a valid HTTPS certificate for the
   existing `stream-driver.com` domain. Do not delete/recreate the domain or
   mailbox. If HTTP is not redirected to HTTPS, enable that in Hosting Settings.
3. Take cars offline and wait until nobody is driving. Restarts interrupt control
   and video. Keep them offline until migration tests pass.

## On the Plesk server

Download and run the check first (no repository checkout required):

```bash
cd ~/rc-car-source
curl -fL https://raw.githubusercontent.com/lillithet-code/RC-Car-Arduino-UNO-Q/main/migrate_domain.py -o migrate_domain.py
sudo python3 migrate_domain.py
sudo python3 migrate_domain.py --apply
```

Checks include DNS, certificate validation, existing service configuration,
managed Plesk proxy blocks and MediaMTX configuration. By default, no Stripe
API calls are made and payment settings remain unchanged. The script supports this repository's standard MediaMTX
block-list YAML; it stops for custom origin restrictions or conflicting Plesk
proxy rules rather than overwriting them.

It copies the existing managed proxy blocks to the new domain, updates account,
WHEP and RTSP base URLs, adds the new WebRTC host, reconfigures Plesk,
restarts services and checks the new login page. Both domains share the same
application and database. The existing Stripe webhook can keep using the old
domain; keep its DNS, hosting and HTTPS certificate active. With the current
payments.py deployed, new checkout sessions return to the domain where checkout
started: drive.kbob.org or stream-driver.com. Both success and cancellation use
that domain over HTTPS. PAYMENTS_BASE_URL remains the fallback for other hosts.
Previously created Stripe sessions retain their original return URLs.

Only if you later want to move payments too, add `--migrate-stripe` to both the
check and apply commands. This explicitly enables Stripe endpoint discovery,
updates the one existing enabled old/new-domain webhook in the configured key's
account and mode, and changes `PAYMENTS_BASE_URL`. It preserves event subscriptions
and the signing secret. This optional mode still stops if the endpoint cannot be
identified. If using both Stripe test and live modes, update the other mode's
endpoint separately too. No Stripe dashboard changes are needed for the default
parallel-domain setup.

Backups are printed as `/var/backups/rc-car-domain-*`. They contain a SQLite
snapshot and numbered original configuration files, mapped by `files.json`.
Keep them private: configuration backups contain credentials. On failure the
script attempts to restore configuration and the original webhook URL and
restart services. It never restores the database automatically, since doing so
could discard new transactions. Read any manual-recovery message before retrying.
Power loss or forced termination cannot trigger automatic recovery.

## On each Pi or UNO Q

The website server cannot update a board without SSH access. On the board,
download the same script and specify the environment file used by its services.
For the standard Pi installation:

```bash
curl -fL https://raw.githubusercontent.com/lillithet-code/RC-Car-Arduino-UNO-Q/main/migrate_domain.py -o /tmp/rc-car-migrate-domain.py
sudo python3 /tmp/rc-car-migrate-domain.py --board --env-file "$HOME/rc-car-rpi4b-board/.env"
sudo python3 /tmp/rc-car-migrate-domain.py --board --env-file "$HOME/rc-car-rpi4b-board/.env" --apply
```

For UNO Q use `$HOME/rc-car-arduino-uno-q/.env`. For a custom installation use its
actual path. This updates SERVER_URL and restarts the installed command/stream
services while retaining the board token and other settings.

## Verify and future updates

Log into https://stream-driver.com (the old domain's login cookie does not
transfer). Test registration/confirmation, password reset, a Stripe test purchase
and webhook delivery, bookings, video, driving, trim, and offline controls.
Confirm balances and users match the old site before reopening cars.

Continue using the SAME application directory and service user for updates.
The script prints their actual values. For the standard app directory:

```bash
cd ~/rc-car-source
git pull --ff-only origin main
sudo env PLESK_DOMAIN=stream-driver.com \
  APP_DIR=/var/www/vhosts/drive.kbob.org/httpdocs/rc-car-arduino-uno-q \
  RUN_USER="$(systemctl show rc-car-web --property=User --value)" \
  bash install_server_linux.sh
```

Do not rerun the MediaMTX installer with its old default host; that overwrites its
configuration. Keep the old Plesk domain and its certificate while old links,
boards, or payment sessions still use it. Do not apply a blanket redirect to
board APIs, WebSockets or Stripe webhooks.

References: [Plesk custom virtual-host files](https://docs.plesk.com/en-US/obsidian/advanced-administration-guide-linux/virtual-hosts-configuration/virtual-hosts-and-hosting-types/virtual-host-configuration-files.72064/)
and [Stripe webhook URL updates](https://docs.stripe.com/api/webhook_endpoints/update).
