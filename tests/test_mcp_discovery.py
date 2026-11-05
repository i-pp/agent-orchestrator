"""Modern discovery regression; only private fixtures and loopback control plane."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from mcp_adapter import Adapter, PROTOCOL, REQUEST_PROTOCOL, VERSION_META, CAPABILITIES_META, SERVER_META
from control_socket import request
from systemd_fixture import ControlPlane
from test_phase2_e1 import Client

CLIENT = Path.home() / '.local/opt/tunnel-client/v0.0.18/tunnel-client'
READ_TOOLS = ['orchestrator_status', 'orchestrator_list_tasks', 'orchestrator_get_task']


def rpc(method, params=None, identifier=1):
    return {'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': {
        '_meta': {VERSION_META: REQUEST_PROTOCOL, CAPABILITIES_META: {}}, **(params or {})}}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT/'.runtime', prefix='mcp-discovery-')
        self.root = Path(self.temp.name)
        (self.root/'logs').mkdir(mode=0o700)
        self.adapter = Adapter(self.root)

    def tearDown(self):
        self.adapter.audit.close()
        self.temp.cleanup()

    def test_discover_repeatable_without_handshake(self):
        for identifier in [1, 2]:
            reply = self.adapter.handle(rpc('server/discover', identifier=identifier))
            result = reply['result']
            self.assertEqual(reply['id'], identifier)
            self.assertEqual(result['supportedVersions'], [REQUEST_PROTOCOL, PROTOCOL])
            self.assertEqual(result['resultType'], 'complete')
            self.assertEqual(result['cacheScope'], 'private')
            self.assertEqual(result['ttlMs'], 0)
            self.assertEqual(set(result['capabilities']), {'tools'})
            self.assertIn('name', result['_meta'][SERVER_META])
        self.assertFalse(self.adapter.initialized)
        self.assertFalse(self.adapter.ready)

    def test_list_is_readonly_with_self_contained_metadata(self):
        message = rpc('tools/list')
        message['params']['_meta'][CAPABILITIES_META] = {'experimental': {'allow-writes': True}}
        result = self.adapter.handle(message)['result']
        self.assertEqual([t['name'] for t in result['tools']], READ_TOOLS)
        self.assertTrue(all(t['annotations']['readOnlyHint'] for t in result['tools']))
        self.assertEqual(result['resultType'], 'complete')
        self.assertEqual(result['ttlMs'], 0)
        self.assertEqual(result['cacheScope'], 'private')

    def test_status_result_and_daemon_failure_are_structured(self):
        message = rpc('tools/call', {'name': 'orchestrator_status'})
        with patch('mcp_adapter.request', return_value={'task_counts': {'COMPLETED': 1}, 'secret': 'private-value'}):
            result = self.adapter.handle(message)['result']
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['data']['task_counts'], {'COMPLETED': 1})
        self.assertEqual(result['resultType'], 'complete')
        self.assertNotIn('private-value', json.dumps(result))
        with patch('mcp_adapter.request', side_effect=OSError('private-value')):
            result = self.adapter.handle(message)['result']
        self.assertTrue(result['isError'])
        self.assertEqual(result['resultType'], 'complete')
        self.assertNotIn('private-value', json.dumps(result))

    def test_modern_requests_do_not_initialize_legacy_connection(self):
        self.assertIn('result', self.adapter.handle(rpc('tools/list')))
        legacy = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}
        self.assertEqual(self.adapter.handle(legacy)['error']['code'], -32602)
        init = {'jsonrpc': '2.0', 'id': 3, 'method': 'initialize', 'params': {
            'protocolVersion': PROTOCOL, 'capabilities': {}, 'clientInfo': {'name': 'fixture', 'version': '1'}}}
        self.assertEqual(self.adapter.handle(init)['result']['protocolVersion'], PROTOCOL)
        self.adapter.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.assertNotIn('resultType', self.adapter.handle(legacy)['result'])
        self.assertEqual(self.adapter.handle(init)['error']['code'], -32602)
        self.assertEqual(self.adapter.handle(rpc('ping'))['result']['resultType'], 'complete')
        self.assertEqual(self.adapter.handle({'jsonrpc': '2.0', 'id': 4, 'method': 'ping'})['result'], {})

    def test_incomplete_or_malformed_metadata_never_reaches_daemon(self):
        cases = [None, [], {}, {VERSION_META: REQUEST_PROTOCOL}, {CAPABILITIES_META: {}},
                 {VERSION_META: REQUEST_PROTOCOL, CAPABILITIES_META: []},
                 {VERSION_META: 'private-not-a-version', CAPABILITIES_META: {}},
                 {VERSION_META: REQUEST_PROTOCOL, CAPABILITIES_META: {}, 'io.modelcontextprotocol/clientInfo': {}}]
        for meta in cases:
            with self.subTest(meta_type=type(meta).__name__), patch('mcp_adapter.request') as send:
                message = rpc('server/discover', {'_meta': meta})
                self.assertEqual(self.adapter.handle(message)['error']['code'], -32602)
                send.assert_not_called()
        self.adapter.handle(rpc('server/discover'))
        message = rpc('tools/call', {'name': 'orchestrator_status', '_meta': {VERSION_META: REQUEST_PROTOCOL}})
        with patch('mcp_adapter.request') as send:
            self.assertEqual(self.adapter.handle(message)['error']['code'], -32602)
            send.assert_not_called()

    def test_unsupported_version_has_bounded_negotiation_error(self):
        message = rpc('server/discover')
        message['params']['_meta'][VERSION_META] = '2099-01-01'
        result = self.adapter.handle(message)
        self.assertEqual(result['error']['code'], -32022)
        self.assertEqual(result['error']['data'], {'requested': '2099-01-01', 'supported': [REQUEST_PROTOCOL]})

    def test_modern_metadata_does_not_authorize_writes_or_arbitrary_paths(self):
        for name in ['orchestrator_submit_task', 'orchestrator_cancel_task', 'shutdown']:
            with patch('mcp_adapter.request') as send:
                self.assertIn('error', self.adapter.handle(rpc('tools/call', {'name': name, 'arguments': {}})))
                send.assert_not_called()
        with patch('mcp_adapter.request') as send:
            result = self.adapter.handle(rpc('tools/call', {'name': 'orchestrator_status', 'arguments': {'runtime': '/tmp'}}))
            self.assertIn('error', result)
            send.assert_not_called()

    def test_discovery_audit_is_metadata_only_and_fails_closed(self):
        message = rpc('server/discover')
        message['params']['_meta']['private-field'] = 'private-sentinel'
        message['id'] = 'private-sentinel'
        self.adapter.handle(message)
        records = [json.loads(l) for l in self.adapter.audit_path.read_text().splitlines()]
        self.assertEqual([r['event'] for r in records], ['protocol_request', 'protocol_response'])
        self.assertNotIn('private-sentinel', self.adapter.audit_path.read_text())
        self.assertIsNone(records[-1]['error_code'])
        with patch.object(self.adapter, 'record', side_effect=OSError('full')):
            self.assertEqual(self.adapter.handle(rpc('tools/list'))['error']['code'], -32603)
            self.assertIsNone(self.adapter.handle({'jsonrpc': '2.0', 'method': 'tools/list'}))

    def test_one_page_cursor_is_bounded(self):
        self.assertIn('result', self.adapter.handle(rpc('tools/list', {'cursor': None})))
        for cursor in ['unknown', '', 1, []]:
            self.assertEqual(self.adapter.handle(rpc('tools/list', {'cursor': cursor}))['error']['code'], -32602)

    def test_real_stdio_discovery_list_and_unavailable_daemon(self):
        client = Client(self.root)
        try:
            client.send(rpc('server/discover'))
            self.assertEqual(client.receive()['result']['resultType'], 'complete')
            client.send(rpc('tools/list', identifier=2))
            self.assertEqual([t['name'] for t in client.receive()['result']['tools']], READ_TOOLS)
            client.send(rpc('tools/call', {'name': 'orchestrator_status'}, 3))
            result = client.receive()['result']
            self.assertTrue(result['isError'])
            self.assertEqual(result['resultType'], 'complete')
        finally:
            client.close()


class NativeDiscoveryTests(unittest.TestCase):
    def test_native_client_modern_discovery_list_status_without_legacy_handshake(self):
        # Actual tunnel-client, synthetic loopback plane, private daemon, no model
        # turn and no second connection to the user's hosted Tunnel.
        with tempfile.TemporaryDirectory(dir=PROJECT/'.runtime', prefix='mcp-native-discovery-') as temp:
            root = Path(temp)
            with tempfile.TemporaryFile() as output:
                processes = []
                plane = ControlPlane()
                try:
                    daemon = subprocess.Popen([sys.executable, '-B', str(PROJECT/'tests/daemon_fixture.py'), str(root)],
                        stdout=output, stderr=output, start_new_session=True)
                    processes.append(daemon)
                    deadline = time.monotonic()+12
                    while True:
                        try:
                            status = request(root, 'status', timeout=.2)
                            break
                        except (OSError, ValueError):
                            if time.monotonic() > deadline: self.fail('private daemon readiness deadline')
                            time.sleep(.05)
                    key = root/'synthetic-key'
                    key.write_text('public-synthetic-mcp-discovery-fixture')
                    key.chmod(0o600)
                    profile = root/'tunnel-client-test.yaml'
                    profile.write_text(f'''config_version: 1
control_plane:
  base_url: "{plane.url}"
  tunnel_id: "tunnel_22222222222222222222222222222222"
  api_key: "file:{key}"
health:
  listen_addr: "127.0.0.1:0"
admin_ui:
  open_browser: false
log:
  level: warn
  format: json
mcp:
  commands: []
''')
                    profile.chmod(0o600)
                    import shlex
                    command = shlex.join([sys.executable, '-B', str(PROJECT/'src/mcp_adapter.py'), '--runtime', str(root)])
                    env = {k: v for k, v in os.environ.items() if k in ['HOME', 'PATH', 'LANG', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS']}
                    tunnel = subprocess.Popen([str(CLIENT), 'run', '--profile-file', str(profile),
                        '--control-plane.base-url='+plane.url, '--control-plane.tunnel-id=tunnel_22222222222222222222222222222222',
                        '--control-plane.api-key=file:'+str(key), '--control-plane.poll-timeout=1s', '--control-plane.initial-poll-timeout=1s',
                        '--mcp.command=channel=main,command='+command, '--health.listen-addr=127.0.0.1:0',
                        '--log.http-raw-unsafe=false', '--log.level=warn'], stdout=output, stderr=output, env=env, start_new_session=True)
                    processes.append(tunnel)
                    discovery = plane.call(rpc('server/discover'), timeout=12)['resp_json']
                    self.assertEqual(discovery['result']['resultType'], 'complete')
                    listed = plane.call(rpc('tools/list', identifier=2))['resp_json']['result']
                    self.assertEqual([t['name'] for t in listed['tools']], READ_TOOLS)
                    result = plane.call(rpc('tools/call', {'name': 'orchestrator_status'}, 3))['resp_json']['result']
                    self.assertFalse(result['isError'])
                    self.assertEqual(result['structuredContent']['data']['task_counts'], status['task_counts'])
                    self.assertEqual(result['resultType'], 'complete')
                finally:
                    for process in reversed(processes):
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGTERM)
                            try: process.wait(timeout=15)
                            except subprocess.TimeoutExpired:
                                os.killpg(process.pid, signal.SIGKILL)
                                process.wait(timeout=5)
                    plane.close()


if __name__ == '__main__':
    unittest.main()
