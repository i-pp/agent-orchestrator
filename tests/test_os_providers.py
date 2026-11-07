"""OS-provider contract tests using synthetic files and local fake children."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from notification_outbox import BoundedResolver, NotificationError
from os_providers import (AesGcmSecretCipher, GetentResolver,
                          SystemdCredentialKeyProvider,
                          _trusted_system_uid,
                          load_systemd_notification_providers)


TEST_KEY = b'test-only-key-material-00000000000'


class FakeKeyProvider:
    def get_key(self):
        return TEST_KEY[:32]


class OsProviderTests(unittest.TestCase):
    def test_only_explicit_root_owner_is_trusted_across_uid_maps(self):
        # In the first map host UID 0 is mapped; in the second it is absent
        # and could be represented by the namespace overflow UID. Neither map
        # can prove that a stat result of 65534 came from root rather than some
        # other unmapped host UID, so only explicit UID 0 is trusted.
        maps = (('1000 0 1\n', True), ('0 100000 65536\n', False))
        for uid_map, expected_root_mapping in maps:
            rows = [tuple(map(int, line.split())) for line in uid_map.splitlines()]
            host_root_mapped = any(start <= 0 < start + length
                                   for _inside, start, length in rows)
            self.assertEqual(host_root_mapped, expected_root_mapping)
            self.assertTrue(_trusted_system_uid(0))
            self.assertFalse(_trusted_system_uid(65534))

    def test_current_untrusted_overflow_owned_getent_fails_closed(self):
        executable = Path('/usr/bin/getent')
        if executable.is_file() and executable.stat().st_uid == 65534:
            with self.assertRaisesRegex(NotificationError, 'UNSAFE_RESOLVER_EXECUTABLE'):
                GetentResolver()

    def test_systemd_credential_provider_fails_closed_without_service_credential(self):
        with self.assertRaisesRegex(NotificationError, 'SYSTEMD_CREDENTIAL_UNAVAILABLE'):
            SystemdCredentialKeyProvider('mcp-events-encryption-key', environ={})
        with self.assertRaisesRegex(NotificationError, 'SYSTEMD_CREDENTIAL_UNAVAILABLE'):
            load_systemd_notification_providers(environ={})
        with self.assertRaisesRegex(NotificationError, 'UNSAFE_CREDENTIAL_DIRECTORY'):
            SystemdCredentialKeyProvider(
                'mcp-events-encryption-key',
                environ={'CREDENTIALS_DIRECTORY': '/tmp/unused/../credentials'})

    def test_systemd_credential_provider_reads_only_private_readonly_key(self):
        with tempfile.TemporaryDirectory(prefix='fake-systemd-credentials-') as temp:
            directory = Path(temp)
            key_file = directory / 'mcp-events-encryption-key'
            key_file.write_bytes(TEST_KEY[:32])
            os.chmod(key_file, 0o400)
            os.chmod(directory, 0o500)
            provider = SystemdCredentialKeyProvider(
                'mcp-events-encryption-key',
                environ={'CREDENTIALS_DIRECTORY': str(directory)})
            self.assertEqual(provider.get_key(), TEST_KEY[:32])
            os.chmod(directory, 0o700)

    def test_systemd_credential_provider_rejects_writable_key_file(self):
        with tempfile.TemporaryDirectory(prefix='fake-systemd-credentials-') as temp:
            directory = Path(temp)
            key_file = directory / 'mcp-events-encryption-key'
            key_file.write_bytes(TEST_KEY[:32])
            os.chmod(key_file, 0o600)
            os.chmod(directory, 0o500)
            provider = SystemdCredentialKeyProvider(
                'mcp-events-encryption-key',
                environ={'CREDENTIALS_DIRECTORY': str(directory)})
            with self.assertRaisesRegex(NotificationError, 'UNSAFE_SYSTEMD_CREDENTIAL'):
                provider.get_key()
            os.chmod(directory, 0o700)

    def test_systemd_credential_provider_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory(prefix='fake-systemd-credentials-') as temp:
            directory = Path(temp)
            os.mkfifo(directory / 'mcp-events-encryption-key')
            os.chmod(directory, 0o500)
            provider = SystemdCredentialKeyProvider(
                'mcp-events-encryption-key',
                environ={'CREDENTIALS_DIRECTORY': str(directory)})
            started = time.monotonic()
            with self.assertRaisesRegex(NotificationError, 'UNSAFE_SYSTEMD_CREDENTIAL'):
                provider.get_key()
            self.assertLess(time.monotonic() - started, 0.2)
            os.chmod(directory, 0o700)

    @unittest.skipUnless(Path('/usr/bin/getent').is_file(), 'getent is not installed')
    def test_systemd_provider_factory_uses_only_preexisting_fake_credentials(self):
        with tempfile.TemporaryDirectory(prefix='fake-systemd-credentials-') as temp:
            directory = Path(temp)
            (directory / 'mcp-events-aes-key').write_bytes(TEST_KEY[:32])
            (directory / 'mcp-events-owner-hmac-key').write_bytes(b'O' * 32)
            os.chmod(directory / 'mcp-events-aes-key', 0o400)
            os.chmod(directory / 'mcp-events-owner-hmac-key', 0o400)
            os.chmod(directory, 0o500)
            with patch('os_providers._validate_getent_executable'):
                providers = load_systemd_notification_providers(
                    environ={'CREDENTIALS_DIRECTORY': str(directory)},
                    process_factory=lambda *_args, **_kwargs: self.fail(
                        'provider construction must not resolve DNS'))
            plaintext = b'test-only callback URL'
            ciphertext = providers['secret_cipher'].encrypt(plaintext)
            self.assertEqual(providers['secret_cipher'].decrypt(ciphertext), plaintext)
            self.assertEqual(providers['owner_hash_key'], b'O' * 32)
            self.assertIs(providers['sender'].resolver, providers['resolver'])
            self.assertIs(providers['callback_verifier'].__self__, providers['sender'])
            os.chmod(directory, 0o700)

    def test_aes_gcm_cipher_roundtrips_and_rejects_tampering(self):
        cipher = AesGcmSecretCipher(FakeKeyProvider())
        plaintext = b'test-only callback signing secret'
        first = cipher.encrypt(plaintext)
        second = cipher.encrypt(plaintext)
        self.assertNotEqual(first, second)
        self.assertEqual(cipher.decrypt(first), plaintext)
        corrupted = first[:-1] + bytes([first[-1] ^ 1])
        with self.assertRaisesRegex(NotificationError, 'INVALID_SECRET_CIPHERTEXT'):
            cipher.decrypt(corrupted)
        with self.assertRaisesRegex(NotificationError, 'INVALID_SECRET_CIPHERTEXT'):
            AesGcmSecretCipher(type('OtherKey', (), {'get_key': lambda _self: b'Z' * 32})()).decrypt(first)

    @unittest.skipUnless(Path('/usr/bin/getent').is_file(), 'getent is not installed')
    def test_getent_resolver_parses_addresses_without_running_real_nss(self):
        observed = {}

        def fake_popen(args, **kwargs):
            observed['args'] = args
            observed['kwargs'] = kwargs
            script = ("import sys; sys.stdout.buffer.write("
                      "b'8.8.8.8 STREAM callback.example.net\\n' + "
                      "b'8.8.8.8 DGRAM callback.example.net\\n' + "
                      "b'2001:4860:4860::8888 STREAM callback.example.net\\n')")
            return subprocess.Popen([sys.executable, '-c', script], **kwargs)

        with patch('os_providers._validate_getent_executable'):
            resolver = GetentResolver(process_factory=fake_popen, max_timeout=0.5)
        result = BoundedResolver(resolver, timeout=0.5).resolve_with_timeout(
            'callback.example.net', 10.0)
        self.assertEqual(result, ['2001:4860:4860::8888', '8.8.8.8'])
        self.assertEqual(observed['args'], [resolver.binary, 'ahosts', 'callback.example.net'])
        self.assertEqual(observed['kwargs']['stdin'], subprocess.DEVNULL)
        self.assertEqual(observed['kwargs']['stderr'], subprocess.DEVNULL)
        self.assertEqual(observed['kwargs']['start_new_session'], True)
        self.assertNotIn('shell', observed['kwargs'])

    @unittest.skipUnless(Path('/usr/bin/getent').is_file(), 'getent is not installed')
    def test_getent_resolver_kills_and_reaps_on_timeout(self):
        processes = []

        def sleeping_child(_args, **kwargs):
            process = subprocess.Popen(
                [sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            processes.append(process)
            return process

        with patch('os_providers._validate_getent_executable'):
            resolver = GetentResolver(process_factory=sleeping_child, max_timeout=0.2)
        with self.assertRaisesRegex(TimeoutError, 'DNS deadline'):
            resolver.resolve_with_timeout('callback.example.net', 0.2)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertIsNotNone(processes[0].wait(timeout=0.1))

    @unittest.skipUnless(Path('/usr/bin/getent').is_file(), 'getent is not installed')
    def test_getent_resolver_stops_child_when_output_limit_is_exceeded(self):
        processes = []
        with patch('os_providers._validate_getent_executable'):
            resolver = GetentResolver(max_timeout=1.0)

        def noisy_child(_args, **kwargs):
            process = subprocess.Popen(
                [sys.executable, '-c',
                 "import sys; sys.stdout.buffer.write(b'8.8.8.8 STREAM h\\n' * 20000)"],
                **kwargs)
            processes.append(process)
            return process

        resolver.process_factory = noisy_child
        with self.assertRaisesRegex(NotificationError, 'CALLBACK_DNS_RESULT_TOO_LARGE'):
            resolver.resolve_with_timeout('callback.example.net', 1.0)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertIsNotNone(processes[0].wait(timeout=0.1))

    def test_bounded_resolver_rejects_provider_without_cancellation_contract(self):
        provider = type('UnboundedResolver', (), {
            'resolve_with_timeout': lambda _self, _host, _timeout: ['8.8.8.8']})()
        with self.assertRaisesRegex(NotificationError, 'BOUNDED_RESOLVER_REQUIRED'):
            BoundedResolver(provider)


if __name__ == '__main__':
    unittest.main()
