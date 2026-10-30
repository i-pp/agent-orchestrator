"""Host probe contract fixtures only. No installed binary/server is executed."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT / 'src'), str(PROJECT / 'scripts')]
import codex_host_read_probe as probe
from test_codex_app_protocol import RESULTS


class FakeTransport:
    def __init__(self): self.started, self.closed, self.calls = False, False, []
    def bind_client(self, client): self.client = client
    def start(self): self.started = True
    def close(self): self.closed = True
    def diagnostics(self): return {'reaped': self.closed}
    def __call__(self, request, timeout_s):
        self.calls.append(request)
        if request['method'] == 'initialized': return None
        return {'id': request['id'], 'result': RESULTS[request['method']]}


class HostProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / 'codex'
        self.binary.write_bytes(b'\x7fELFoffline-fixture')
        self.binary.chmod(0o700)
        self.sha = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        self.transport = FakeTransport()

    def test_sandbox_and_unknown_block_before_binary_read_or_process(self):
        for state in ['SANDBOXED', 'UNKNOWN']:
            with patch.object(probe, 'environment', return_value={'state': state}), patch.object(probe, 'StdioTransport') as spawn:
                with self.assertRaisesRegex(ValueError, 'NORMAL_HOST_CONTEXT_NOT_CONFIRMED'):
                    probe.preflight('/missing/not-read', '0' * 64)
                spawn.assert_not_called()

    def test_host_pinned_binary_preflight_uses_existing_home_without_auth_reads(self):
        with patch.object(probe, 'environment', return_value={'state': 'HOST_CONTEXT'}), patch.dict(os.environ, {'CODEX_HOME': str(self.root)}):
            self.assertEqual(probe.preflight(self.binary, self.sha)[0], self.binary)
            with self.assertRaisesRegex(ValueError, 'BINARY_HASH_MISMATCH'): probe.preflight(self.binary, '0' * 64)
            self.binary.write_bytes(b'#!/bin/sh\n')
            with self.assertRaisesRegex(ValueError, 'NATIVE_ELF_REQUIRED'): probe.preflight(self.binary, self.sha)

    def test_readonly_mount_blocks_without_relocating_home(self):
        flags = type('Flags', (), {'f_flag': os.ST_RDONLY})()
        with patch.object(probe, 'environment', return_value={'state': 'HOST_CONTEXT'}), patch.dict(os.environ, {'CODEX_HOME': str(self.root)}), patch.object(os, 'statvfs', return_value=flags):
            with self.assertRaisesRegex(ValueError, 'CODEX_STATE_NOT_WRITABLE'): probe.preflight(self.binary, self.sha)

    def test_fixed_read_methods_and_exclusive_receipt_prevent_second_start(self):
        receipt = self.root / 'receipt.json'
        with patch.object(probe, 'preflight', return_value=(self.binary, {'state': 'HOST_CONTEXT'})):
            result = probe.run(self.binary, self.sha, receipt, transport_factory=lambda: self.transport)
            self.assertEqual(result['status'], 'READ_PROBE_COMPLETED')
            self.assertEqual([x['method'] for x in self.transport.calls], ['initialize', 'initialized', 'account/read', 'model/list'])
            self.assertEqual(self.transport.calls[2]['params'], {'refreshToken': False})
            self.assertTrue(self.transport.closed)
            self.assertEqual(self.transport.COMMAND, (str(self.binary), 'app-server', '--listen', 'stdio://'))
            with self.assertRaises(FileExistsError): probe.run(self.binary, self.sha, receipt, transport_factory=lambda: self.fail('duplicate startup'))
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('email', receipt.read_text())

    def test_private_receipt_directory_is_required(self):
        self.root.chmod(0o755)
        with patch.object(probe, 'preflight', return_value=(self.binary, {'state': 'HOST_CONTEXT'})):
            with self.assertRaisesRegex(ValueError, 'PRIVATE_RECEIPT_DIRECTORY_REQUIRED'):
                probe.run(self.binary, self.sha, self.root / 'receipt.json', transport_factory=lambda: self.fail('must not start'))

    def test_server_auth_refresh_request_stops_without_answering(self):
        class NeedsAuth(FakeTransport):
            def __call__(self, request, timeout_s):
                self.calls.append(request)
                self.client.receive({'id': 52, 'method': 'account/chatgptAuthTokens/refresh', 'params': {}})
        transport = NeedsAuth()
        with patch.object(probe, 'preflight', return_value=(self.binary, {'state': 'HOST_CONTEXT'})):
            result = probe.run(self.binary, self.sha, self.root / 'auth-required.json', transport_factory=lambda: transport)
        self.assertEqual(result['status'], 'READ_PROBE_NOT_COMPLETED')
        self.assertTrue(result['blocked_server_request'])
        self.assertEqual([r['method'] for r in transport.calls], ['initialize'])
        self.assertTrue(transport.closed)

    def test_unclassified_io_error_does_not_claim_no_child_started(self):
        output = io.StringIO()
        args = ['probe', '--binary', '/fixture/codex', '--sha256', 'a' * 64, '--receipt', '/fixture/receipt']
        with patch.object(sys, 'argv', args), patch.object(probe, 'run', side_effect=OSError('private diagnostic')), patch.object(sys, 'stdout', output):
            self.assertEqual(probe.main(), 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertIsNone(result['child_started'])
        self.assertNotIn('private diagnostic', output.getvalue())


if __name__ == '__main__': unittest.main()
