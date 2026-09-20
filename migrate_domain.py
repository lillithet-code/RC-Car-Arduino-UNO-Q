#!/usr/bin/env python3
"""Move this existing Plesk deployment to stream-driver.com; Python 3.9+."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

OLD = 'drive.kbob.org'
NEW = 'stream-driver.com'
BEGIN = '# BEGIN RC-CAR CARS PROXY'
END = '# END RC-CAR CARS PROXY'


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE).strip()


def environment(service):
    pid = run('systemctl', 'show', service, '--property=MainPID', '--value')
    if not pid or pid == '0':
        raise RuntimeError(f'{service} must be running.')
    return dict(part.decode().split('=', 1) for part in
                Path(f'/proc/{pid}/environ').read_bytes().split(b'\0') if b'=' in part)


def update_env(text, changes):
    # Preserve every unrelated line, including secrets and quoted passwords.
    lines = text.splitlines()
    for key, value in changes.items():
        pattern = re.compile(r'^\s*' + re.escape(key) + r'\s*=')
        lines = [line for line in lines if not pattern.match(line)]
        lines.append(f'{key}={value}')
    return '\n'.join(lines) + '\n'


def proxy_block(source, target):
    pattern = re.compile(re.escape(BEGIN) + r'.*?' + re.escape(END), re.S)
    blocks = pattern.findall(source)
    if len(blocks) != 1:
        raise RuntimeError('Old proxy configuration has no unique RC-CAR managed block.')
    if target.count(BEGIN) != target.count(END) or target.count(BEGIN) > 1:
        raise RuntimeError('New proxy configuration has malformed managed blocks.')
    remainder = pattern.sub('', target)
    if re.search(r'\blocation\s|\bProxyPass\b', remainder):
        raise RuntimeError('New domain has custom proxy/location rules; review them before migration.')
    return remainder.rstrip() + '\n' + blocks[0] + '\n'


def media_hosts(text):
    pattern = re.compile(r'^webrtcAdditionalHosts:[^\n]*(?:\n[ \t]+[^\n]*)*', re.M)
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise RuntimeError('Expected one webrtcAdditionalHosts entry in MediaMTX configuration.')
    block = matches[0].group()
    first = block.splitlines()[0].split(':', 1)[1].strip()
    if first not in ('', '[]'):
        raise RuntimeError('Use a YAML block list for webrtcAdditionalHosts before running this script.')
    if not re.search(r'^\s*-\s*[\'"]?' + re.escape(NEW) + r'[\'"]?\s*(?:#.*)?$', block, re.M):
        block = re.sub(r'^webrtcAdditionalHosts:\s*\[\][ \t]*', 'webrtcAdditionalHosts:', block)
        block += '\n  - ' + NEW
    # Do not silently widen a custom origin restriction.
    origins = re.findall(r'^webrtcAllowOrigin:\s*(.*?)\s*$', text, re.M)
    if origins and origins[0].strip('\'"') != '*':
        raise RuntimeError('Custom webrtcAllowOrigin: configure allowed origins for both domains first.')
    return text[:matches[0].start()] + block + text[matches[0].end():]


def stripe(key, path, data=None):
    request = urllib.request.Request('https://api.stripe.com/v1/' + path,
        data=None if data is None else urllib.parse.urlencode(data).encode(),
        headers={'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def webhook(key):
    endpoints = []
    path = 'webhook_endpoints?limit=100'
    while True:
        page = stripe(key, path)
        endpoints.extend(page['data'])
        if not page.get('has_more'):
            break
        path = 'webhook_endpoints?limit=100&starting_after=' + page['data'][-1]['id']
    urls = {f'https://{host}/payments/webhooks/stripe' for host in (OLD, NEW)}
    matches = [item for item in endpoints if item['url'].rstrip('/') in urls]
    if len(matches) != 1 or matches[0]['status'] != 'enabled':
        raise RuntimeError('Expected exactly one enabled Stripe webhook for the old or new site. Check Stripe first.')
    return matches[0]


def server_settings(text, env, migrate_stripe=False):
    changes = {
        'ACCOUNT_BASE_URL': f'https://{NEW}',
        'MEDIAMTX_WHEP_BASE': f'https://{NEW}',
        'MEDIAMTX_RTSP_BASE': f'rtsp://{NEW}:8554',
    }
    key = ''
    endpoint = None
    if migrate_stripe:
        key = env.get('STRIPE_SECRET_KEY', '').strip()
        if not key:
            raise RuntimeError('--migrate-stripe requires a configured Stripe API key.')
        endpoint = webhook(key)
        changes['PAYMENTS_BASE_URL'] = f'https://{NEW}'
        print('Stripe webhook located; its URL and payment return URLs will change.')
    else:
        print('Keeping existing payment URLs and Stripe configuration; no Stripe API calls.')
    return update_env(text, changes), key, endpoint


def atomic_write(path, content, metadata=None):
    path = Path(path)
    metadata = metadata or (path.stat() if path.exists() else None)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.rc-migration-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content)
        os.chmod(name, metadata.st_mode & 0o777 if metadata else 0o644)
        if metadata:
            os.chown(name, metadata.st_uid, metadata.st_gid)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='perform changes; otherwise check only')
    parser.add_argument('--board', action='store_true', help='run on a Pi/UNO Q instead of Plesk')
    parser.add_argument('--migrate-stripe', action='store_true',
                        help='also move Stripe webhook and payment return URLs (default: preserve payments)')
    parser.add_argument('--env-file', type=Path, help='required for --board')
    parser.add_argument('--server-ip', default='217.154.249.28')
    parser.add_argument('--mediamtx-config', type=Path, default=Path('/opt/mediamtx/mediamtx.yml'))
    args = parser.parse_args()
    if args.board and args.migrate_stripe:
        parser.error('--migrate-stripe is only for the website server.')
    if os.geteuid() != 0:
        parser.error('Run with sudo python3.')
    changes = {}
    endpoint = None
    key = ''
    if args.board:
        if not args.env_file:
            parser.error('--board requires --env-file /absolute/path/to/.env')
        services = [name for name in ('rc-car-command', 'rc-car-stream', 'rc-car-board')
                    if run('systemctl', 'show', name, '--property=LoadState', '--value') == 'loaded']
        if not services:
            raise RuntimeError('No supported board services found.')
        for service in services:
            files = run('systemctl', 'show', service, '--property=EnvironmentFiles', '--value')
            if str(args.env_file.resolve()) not in files:
                raise RuntimeError(f'{service} does not use the specified environment file.')
        changes[args.env_file.resolve()] = update_env(args.env_file.read_text(), {'SERVER_URL': f'https://{NEW}'})
    else:
        env = environment('rc-car-web')
        app_dir = Path(run('systemctl', 'show', 'rc-car-web', '--property=WorkingDirectory', '--value'))
        env_file = app_dir / '.env'
        files = run('systemctl', 'show', 'rc-car-web', '--property=EnvironmentFiles', '--value')
        if str(env_file) not in files:
            raise RuntimeError('Service uses an unexpected environment file; no changes made.')
        addresses = {item[4][0] for item in socket.getaddrinfo(NEW, 443, type=socket.SOCK_STREAM)}
        if addresses != {args.server_ip}:
            raise RuntimeError(f'DNS for {NEW} is {sorted(addresses)}. Set its A record to {args.server_ip} and remove incorrect AAAA records first.')
        with socket.create_connection((NEW, 443), timeout=15) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=NEW):
                pass
        print('DNS and HTTPS certificate checks passed.')
        old_conf = Path(f'/var/www/vhosts/system/{OLD}/conf')
        new_conf = Path(f'/var/www/vhosts/system/{NEW}/conf')
        if not new_conf.is_dir():
            raise RuntimeError('Enable website hosting for the new domain in Plesk first.')
        for name in ('vhost.conf', 'vhost_ssl.conf', 'vhost_nginx.conf'):
            source = old_conf / name
            if source.exists() and BEGIN in source.read_text():
                dest = new_conf / name
                changes[dest] = proxy_block(source.read_text(), dest.read_text() if dest.exists() else '')
        if not changes:
            raise RuntimeError('No managed Plesk proxy configuration found on the old domain.')
        changes[env_file], key, endpoint = server_settings(
            env_file.read_text(), env, args.migrate_stripe)
        changes[args.mediamtx_config] = media_hosts(args.mediamtx_config.read_text())
        services = ['mediamtx', 'rc-car-web']
        for service in services:
            run('systemctl', 'is-active', service)
        db_url = env.get('DATABASE_URL', '')
        if not db_url.startswith('sqlite:///'):
            raise RuntimeError('Expected SQLite DATABASE_URL; back up and migrate custom database deployments separately.')
        db_path = Path(db_url[len('sqlite:///'):])
        if not db_path.is_absolute():
            db_path = app_dir / db_path
        if not db_path.is_file():
            raise RuntimeError('Configured database does not exist.')
    for path in changes:
        print('Will update:', path)
    if not args.apply:
        print('Checks passed. Run again with --apply when nobody is driving; service restarts interrupt video/control.')
        return
    backup = Path(tempfile.mkdtemp(prefix='rc-car-domain-', dir='/var/backups'))
    os.chmod(backup, 0o700)
    originals = {}
    for index, path in enumerate(changes):
        originals[path] = (path.read_text(), path.stat()) if path.exists() else None
        if path.exists():
            shutil.copy2(path, backup / str(index))
    (backup / 'files.json').write_text(json.dumps({str(p): str(i) if originals[p] else None for i, p in enumerate(changes)}, indent=2))
    if not args.board:
        source = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)
        dest = sqlite3.connect(backup / 'app.db')
        try:
            source.backup(dest)
        finally:
            source.close()
            dest.close()
    print('Backup:', backup)
    stripe_attempted = False
    try:
        for path, content in changes.items():
            atomic_write(path, content)
        if not args.board:
            run('plesk', 'sbin', 'httpdmng', '--reconfigure-domain', NEW)
        for service in services:
            run('systemctl', 'restart', service)
        time.sleep(3)
        for service in services:
            run('systemctl', 'is-active', service)
        if not args.board:
            with urllib.request.urlopen(f'https://{NEW}/login', timeout=20) as response:
                body = response.read().decode()
                if (response.status != 200 or urllib.parse.urlsplit(response.url).hostname != NEW
                        or 'name="csrf_token"' not in body
                        or 'Resend confirmation email' not in body):
                    raise RuntimeError('New website login check failed.')
            if endpoint and endpoint['url'] != f'https://{NEW}/payments/webhooks/stripe':
                stripe_attempted = True
                stripe(key, 'webhook_endpoints/' + endpoint['id'], {'url': f'https://{NEW}/payments/webhooks/stripe'})
        print('Migration complete. Test login, confirmation/reset emails, a Stripe test purchase, video and steering.')
        if not args.board:
            print('Keep the old domain configured. Run --board on each board to finish moving its SERVER_URL.')
            print('For future installs, keep APP_DIR=' + str(app_dir) + ' and set PLESK_DOMAIN=' + NEW + ' and RUN_USER=' + run('systemctl', 'show', 'rc-car-web', '--property=User', '--value'))
    except BaseException:
        print('Migration failed; restoring original configuration. Database remains in place.')
        errors = []
        for path, original in originals.items():
            try:
                if original:
                    atomic_write(path, original[0], original[1])
                elif path.exists():
                    path.unlink()
            except Exception:
                errors.append('restore ' + str(path))
        actions = [] if args.board else [('plesk', 'sbin', 'httpdmng', '--reconfigure-domain', NEW)]
        actions += [('systemctl', 'restart', service) for service in services]
        for action in actions:
            try:
                run(*action)
            except Exception:
                errors.append(' '.join(action))
        if stripe_attempted:
            try:
                stripe(key, 'webhook_endpoints/' + endpoint['id'], {'url': endpoint['url']})
            except Exception:
                errors.append('Restore Stripe webhook URL manually: ' + endpoint['url'])
        if errors:
            print('Manual recovery required:', '; '.join(errors))
        raise


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Never dump subprocess output or environment containing credentials.
        if isinstance(exc, subprocess.CalledProcessError):
            print('ERROR: Command failed:', ' '.join(exc.cmd), '(inspect service/Plesk logs)')
        else:
            print('ERROR:', str(exc))
        raise SystemExit(1)
