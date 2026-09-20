import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('migration', Path(__file__).resolve().parents[1] / 'migrate_domain.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MigrationTests(unittest.TestCase):
    def test_default_migration_preserves_payments_without_stripe_access(self):
        original = ('PAYMENTS_BASE_URL="https://drive.kbob.org"\n'
                    'STRIPE_SECRET_KEY=existing\nSTRIPE_WEBHOOK_SECRET=existing-signing-secret\n')
        with patch.object(m, 'stripe', side_effect=AssertionError('Stripe must not be contacted')) as api:
            result, key, endpoint = m.server_settings(original, {'STRIPE_SECRET_KEY': 'existing'})
        api.assert_not_called()
        self.assertTrue(result.startswith(original))
        self.assertIn('ACCOUNT_BASE_URL=https://stream-driver.com\n', result)
        self.assertEqual(key, '')
        self.assertIsNone(endpoint)

    def test_default_does_not_introduce_payment_override(self):
        result, _, endpoint = m.server_settings('SMTP_HOST=kbob.org\n', {})
        self.assertNotIn('PAYMENTS_BASE_URL', result)
        self.assertIsNone(endpoint)

    def test_explicit_stripe_migration_retains_validation(self):
        with patch.object(m, 'stripe', return_value={'data': [], 'has_more': False}):
            with self.assertRaises(RuntimeError):
                m.server_settings('', {'STRIPE_SECRET_KEY': 'existing'}, True)
        with self.assertRaises(RuntimeError):
            m.server_settings('', {}, True)

    def test_explicit_stripe_migration_plans_payment_change(self):
        endpoint = {'id': 'we_target', 'url': 'https://drive.kbob.org/payments/webhooks/stripe', 'status': 'enabled'}
        with patch.object(m, 'stripe', return_value={'data': [endpoint], 'has_more': False}):
            result, key, selected = m.server_settings('PAYMENTS_BASE_URL=https://drive.kbob.org\n',
                                                    {'STRIPE_SECRET_KEY': 'existing'}, True)
        self.assertIn('PAYMENTS_BASE_URL=https://stream-driver.com\n', result)
        self.assertEqual(key, 'existing')
        self.assertEqual(selected, endpoint)

    def test_env_preserves_secrets_and_removes_duplicate_setting(self):
        original = '# comment\nSMTP_PASSWORD="a=b # c"\nACCOUNT_BASE_URL=old\nACCOUNT_BASE_URL=older\n'
        result = m.update_env(original, {'ACCOUNT_BASE_URL': 'https://stream-driver.com'})
        self.assertIn('SMTP_PASSWORD="a=b # c"\n', result)
        self.assertEqual(result.count('ACCOUNT_BASE_URL='), 1)
        self.assertEqual(m.update_env(result, {'ACCOUNT_BASE_URL': 'https://stream-driver.com'}), result)

    def test_proxy_retains_other_settings_and_is_repeatable(self):
        source = m.BEGIN + '\nlocation / { proxy_pass http://127.0.0.1:8000; }\n' + m.END
        result = m.proxy_block(source, 'client_max_body_size 10m;\n')
        self.assertIn('client_max_body_size 10m;', result)
        self.assertEqual(m.proxy_block(source, result), result)

    def test_proxy_rejects_conflicts_and_bad_markers(self):
        source = m.BEGIN + '\nmanaged\n' + m.END
        for target in ('location / { }', 'ProxyPass / http://elsewhere/', m.BEGIN):
            with self.assertRaises(RuntimeError):
                m.proxy_block(source, target)

    def test_media_retains_existing_hosts_and_settings(self):
        source = 'webrtcAllowOrigin: "*"\nwebrtcAdditionalHosts:\n  - drive.kbob.org\n  - 217.154.249.28\napi: yes\n'
        result = m.media_hosts(source)
        self.assertIn('  - drive.kbob.org\n', result)
        self.assertIn('  - 217.154.249.28\n', result)
        self.assertIn('  - stream-driver.com\napi: yes', result)
        self.assertEqual(m.media_hosts(result), result)

    def test_empty_hosts(self):
        self.assertEqual(m.media_hosts('webrtcAdditionalHosts: []\n'), 'webrtcAdditionalHosts:\n  - stream-driver.com\n')

    def test_custom_yaml_is_rejected(self):
        for source in ('webrtcAdditionalHosts: [old]\n', 'webrtcAdditionalHosts: []\nwebrtcAllowOrigin: https://old\n'):
            with self.assertRaises(RuntimeError):
                m.media_hosts(source)

    def test_stripe_pagination_and_endpoint_selection(self):
        endpoint = {'id': 'we_target', 'url': 'https://drive.kbob.org/payments/webhooks/stripe', 'status': 'enabled'}
        with patch.object(m, 'stripe', side_effect=[{'data': [{'id': 'we_other', 'url': 'https://other'}], 'has_more': True}, {'data': [endpoint], 'has_more': False}]) as api:
            self.assertEqual(m.webhook('hidden'), endpoint)
            self.assertIn('starting_after=we_other', api.call_args.args[1])

    def test_stripe_duplicate_missing_disabled_are_rejected(self):
        endpoint = {'id': 'we_target', 'url': 'https://drive.kbob.org/payments/webhooks/stripe', 'status': 'enabled'}
        for endpoints in ([], [endpoint, endpoint], [dict(endpoint, status='disabled')]):
            with patch.object(m, 'stripe', return_value={'data': endpoints, 'has_more': False}):
                with self.assertRaises(RuntimeError):
                    m.webhook('hidden')


if __name__ == '__main__':
    unittest.main()
