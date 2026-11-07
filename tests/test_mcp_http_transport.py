"""Pure HTTP request tests; they never bind a socket or send network traffic."""
import json
import base64
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))

from mcp_adapter import (Adapter, PROTOCOL, REQUEST_PROTOCOL, VERSION_META,
                         CAPABILITIES_META)
from mcp_http_transport import McpHttpEndpoint, make_http_server
from local_principal import LocalPrincipal, current_local_principal
from total_deadline import run_with_total_deadline


class FakeEvents:
    enabled = True
    last_principal = None

    def handle(self, method, params, principal):
        self.last_principal = principal
        return {'events': [], 'method': method}


class FakePipeline:
    def dispatch(self, name, arguments):
        return {'status': 'READY'}


class AuthenticatedCaptureAdapter:
    def __init__(self):
        self.last_principal = None

    def handle(self, message, *, principal=None):
        self.last_principal = principal
        return {'jsonrpc': '2.0', 'id': message.get('id'),
                'result': {'method': message.get('method')}}


class MCPHTTPTransportTests(unittest.TestCase):
    def setUp(self):
        runtime_parent = PROJECT / '.runtime'
        runtime_parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(runtime_parent, 0o700)
        self.temp = tempfile.TemporaryDirectory(prefix='mcp-http-pure-', dir=runtime_parent)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        (self.root / 'logs').mkdir(mode=0o700)
        self.adapter = Adapter(self.root, allow_codex_pipeline=True, pipeline=FakePipeline(),
                               events_service=FakeEvents(),
                               principal_provider=current_local_principal)
        self.endpoint = McpHttpEndpoint(
            self.adapter, expected_port=8765, allow_single_user_local=True,
            principal_provider=current_local_principal)
        self.auth_capture = AuthenticatedCaptureAdapter()
        self.local_endpoint = McpHttpEndpoint(
            self.auth_capture, expected_port=8765, allow_single_user_local=True,
            principal_provider=current_local_principal)
        self.common_headers = {
            'Host': '127.0.0.1:8765',
            'Content-Type': 'application/json; charset=utf-8',
            'Accept': 'application/json, text/event-stream',
        }

    def tearDown(self):
        self.adapter.audit.close()
        self.temp.cleanup()

    def _call(self, message, *, protocol, method_header=None, extra_headers=None,
              remote_addr='127.0.0.1', http_method='POST'):
        headers = dict(self.common_headers)
        headers['MCP-Protocol-Version'] = protocol
        if method_header is not None:
            headers['Mcp-Method'] = method_header
        if extra_headers:
            headers.update(extra_headers)
        raw = json.dumps(message, separators=(',', ':')).encode()
        headers['Content-Length'] = str(len(raw))
        return self.endpoint.handle(http_method, '/mcp', headers, raw,
                                    remote_addr=remote_addr)

    def _modern(self, method, params=None, identifier=1):
        value = dict(params or {})
        value['_meta'] = {VERSION_META: REQUEST_PROTOCOL, CAPABILITIES_META: {}}
        return {'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': value}

    def test_mcp_2_discover_advertises_events_without_a_session(self):
        status, headers, body = self._call(
            self._modern('server/discover'), protocol=REQUEST_PROTOCOL,
            method_header='server/discover')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
        result = json.loads(body)['result']
        self.assertIn('events', result['capabilities'])
        self.assertNotIn('Mcp-Session-Id', headers)

    def test_mcp_2_events_list_reaches_the_configured_service(self):
        status, _, body = self._call(
            self._modern('events/list'), protocol=REQUEST_PROTOCOL,
            method_header='events/list')
        self.assertEqual(status, 200)
        result = json.loads(body)['result']
        self.assertEqual(result['events'], [])
        self.assertEqual(result['method'], 'events/list')
        self.assertEqual(result['resultType'], 'complete')

    def test_mcp_2_requires_matching_method_and_protocol_headers(self):
        request = self._modern('server/discover')
        status, _, body = self._call(request, protocol=REQUEST_PROTOCOL,
                                     method_header='tools/list')
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)['error']['code'], -32020)
        headers = dict(self.common_headers)
        headers.update({'MCP-Protocol-Version': '2025-11-25', 'Mcp-Method': 'server/discover'})
        raw = json.dumps(request).encode()
        headers['Content-Length'] = str(len(raw))
        status, _, body = self.endpoint.handle('POST', '/mcp', headers, raw)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)['error']['code'], -32020)

    def test_tool_name_header_must_match_body(self):
        request = self._modern('tools/call', {'name': 'codex_capabilities', 'arguments': {}})
        status, _, body = self._call(request, protocol=REQUEST_PROTOCOL,
            method_header='tools/call', extra_headers={'Mcp-Name': 'codex_task_submit'})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)['error']['code'], -32020)

    def test_mcp_2_decodes_base64_encoded_non_ascii_name_before_comparing(self):
        name = 'レビュー 世界'
        encoded = '=?base64?' + base64.b64encode(name.encode('utf-8')).decode('ascii') + '?='
        request = self._modern('prompts/get', {'name': name})
        status, _, body = self._call(request, protocol=REQUEST_PROTOCOL,
            method_header='prompts/get', extra_headers={'Mcp-Name': encoded})
        # The name passes transport validation; the adapter then correctly reports
        # that this server does not expose prompts.
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)['error']['code'], -32601)
        status, _, body = self._call(request, protocol=REQUEST_PROTOCOL,
            method_header='prompts/get', extra_headers={'Mcp-Name': '=?base64?not-valid!?='})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)['error']['code'], -32020)

    def test_event_name_header_matches_for_subscribe_and_unsubscribe(self):
        for method in ('events/subscribe', 'events/unsubscribe'):
            with self.subTest(method=method):
                self.adapter.events_service.last_principal = None
                status, _, body = self._call(
                    self._modern(method, {'name': 'codex.task.completed'}),
                    protocol=REQUEST_PROTOCOL, method_header=method,
                    extra_headers={'Mcp-Name': 'codex.task.completed'})
                self.assertEqual(status, 200, body.decode())
                self.assertEqual(json.loads(body)['result']['method'], method)
                self.assertEqual(self.adapter.events_service.last_principal,
                                 current_local_principal())


    def test_headerless_events_remain_compatible(self):
        # Retain the existing ChatGPT integration profile as well as accepting
        # the newer draft's event routing header when a client provides it.
        for method in ('events/subscribe', 'events/unsubscribe'):
            with self.subTest(method=method):
                status, _, body = self._call(
                    self._modern(method, {'name': 'codex.task.completed'}),
                    protocol=REQUEST_PROTOCOL, method_header=method)
                self.assertEqual(status, 200, body.decode())


    def test_event_name_header_mismatch_is_rejected_before_adapter_audit(self):
        for method in ('events/subscribe', 'events/unsubscribe'):
            for header in ('codex.task.failed', '=?base64?not-valid!?=', 'x' * 8193):
                with self.subTest(method=method, header_length=len(header)):
                    self.adapter.events_service.last_principal = None
                    before = self.adapter.audit_path.read_bytes()
                    status, _, body = self._call(
                        self._modern(method, {'name': 'codex.task.completed'}),
                        protocol=REQUEST_PROTOCOL, method_header=method,
                        extra_headers={'Mcp-Name': header})
                    self.assertEqual(status, 400)
                    self.assertEqual(json.loads(body)['error']['code'], -32020)
                    self.assertIsNone(self.adapter.events_service.last_principal)
                    self.assertEqual(self.adapter.audit_path.read_bytes(), before)


    def test_event_name_header_decodes_base64_non_ascii_event_names(self):
        name = '記事.更新'
        encoded = '=?base64?' + base64.b64encode(name.encode()).decode() + '?='
        for method in ('events/subscribe', 'events/unsubscribe'):
            with self.subTest(method=method):
                status, _, body = self._call(
                    self._modern(method, {'name': name}), protocol=REQUEST_PROTOCOL,
                    method_header=method, extra_headers={'Mcp-Name': encoded})
                self.assertEqual(status, 200, body.decode())


    def test_event_name_header_does_not_bypass_http_authentication(self):
        method = 'events/subscribe'
        message = self._modern(method, {'name': 'codex.task.completed'})
        raw = json.dumps(message).encode()
        headers = {**self.common_headers, 'Content-Length': str(len(raw)),
                   'MCP-Protocol-Version': REQUEST_PROTOCOL, 'Mcp-Method': method,
                   'Mcp-Name': 'codex.task.completed'}
        self.adapter.events_service.last_principal = None
        status, _, _ = McpHttpEndpoint(self.adapter, expected_port=8765).handle('POST', '/mcp', headers, raw)
        self.assertEqual(status, 401)
        self.assertIsNone(self.adapter.events_service.last_principal)
        headers['Authorization'] = 'Bearer test-alice'
        status, _, body = McpHttpEndpoint(self.adapter, expected_port=8765).handle('POST', '/mcp', headers, raw)
        self.assertEqual(status, 401)
        self.assertIsNone(self.adapter.events_service.last_principal)


    def test_event_name_header_requires_a_valid_matching_body_name(self):
        for params in ({}, {'name': None}, {'name': 42}):
            with self.subTest(params=params):
                status, _, body = self._call(
                    self._modern('events/subscribe', params), protocol=REQUEST_PROTOCOL,
                    method_header='events/subscribe',
                    extra_headers={'Mcp-Name': 'codex.task.completed'})
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)['error']['code'], -32020)


    def test_origin_host_and_remote_address_are_loopback_bound(self):
        request = self._modern('server/discover')
        status, _, _ = self._call(request, protocol=REQUEST_PROTOCOL,
            method_header='server/discover', extra_headers={'Origin': 'https://attacker.example'})
        self.assertEqual(status, 403)
        status, _, _ = self._call(request, protocol=REQUEST_PROTOCOL,
            method_header='server/discover', remote_addr='203.0.113.9')
        self.assertEqual(status, 403)
        headers = dict(self.common_headers)
        headers['Host'] = 'rebound.example:8765'
        headers.update({'MCP-Protocol-Version': REQUEST_PROTOCOL, 'Mcp-Method': 'server/discover'})
        status, _, _ = self.endpoint.handle('POST', '/mcp', headers, json.dumps(request).encode())
        self.assertEqual(status, 403)

    def test_content_and_size_bounds_fail_before_dispatch(self):
        request = self._modern('server/discover')
        headers = dict(self.common_headers)
        headers.update({'MCP-Protocol-Version': REQUEST_PROTOCOL, 'Mcp-Method': 'server/discover',
                        'Accept': 'application/json'})
        status, _, _ = self.endpoint.handle('POST', '/mcp', headers,
                                            json.dumps(request).encode())
        self.assertEqual(status, 406)
        headers['Accept'] = 'application/json, text/event-stream'
        headers['Content-Length'] = '65537'
        status, _, _ = self.endpoint.handle('POST', '/mcp', headers, b'x' * 65537)
        self.assertEqual(status, 413)

    def test_2025_initialize_session_notification_and_tool_call(self):
        initialize = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': PROTOCOL, 'capabilities': {},
            'clientInfo': {'name': 'fixture', 'version': '1'}}}
        status, headers, body = self._call(initialize, protocol=PROTOCOL,
                                           method_header='initialize')
        self.assertEqual(status, 200)
        session_id = headers['Mcp-Session-Id']
        self.assertEqual(json.loads(body)['result']['protocolVersion'], PROTOCOL)
        initialized = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
        status, _, body = self._call(initialized, protocol=PROTOCOL,
            method_header='notifications/initialized',
            extra_headers={'Mcp-Session-Id': session_id})
        self.assertEqual((status, body), (202, b''))
        tools = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}
        status, _, body = self._call(tools, protocol=PROTOCOL, method_header='tools/list',
            extra_headers={'Mcp-Session-Id': session_id})
        self.assertEqual(status, 200)
        self.assertIn('codex_task_submit', {t['name'] for t in json.loads(body)['result']['tools']})
        status, _, body = self.endpoint.handle('DELETE', '/mcp', {
            **self.common_headers, 'Mcp-Session-Id': session_id}, b'')
        self.assertEqual((status, body), (204, b''))

    def test_sessions_are_required_for_legacy_requests(self):
        tools = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}
        status, _, body = self._call(tools, protocol=PROTOCOL, method_header='tools/list')
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)['error']['code'], -32001)

    def test_non_loopback_bind_is_refused_before_socket_creation(self):
        with self.assertRaisesRegex(ValueError, '127.0.0.1'):
            make_http_server(self.adapter, host='0.0.0.0', port=8765)

    def test_real_http_listener_requires_explicit_single_user_local_policy(self):
        with self.assertRaisesRegex(ValueError, 'Single-user local'):
            make_http_server(self.adapter, host='127.0.0.1', port=0)

    def test_listener_requires_local_principal_provider(self):
        with self.assertRaisesRegex(ValueError, 'Local principal provider'):
            make_http_server(AuthenticatedCaptureAdapter(), port=0,
                             allow_single_user_local=True)

    def test_http_request_deadline_bounds_a_trickling_reader(self):
        stopped = threading.Event()
        chunks = []
        result = []

        def slow_reader():
            while not stopped.wait(0.005):
                chunks.append(b'x')  # progress continues, so an idle timeout would not fire
            raise OSError('request socket shut down')

        def receive():
            try:
                run_with_total_deadline(0.05, lambda: stopped.set(), slow_reader)
            except TimeoutError:
                result.append('deadline')

        started = time.monotonic()
        thread = threading.Thread(target=receive)
        thread.start()
        thread.join(0.5)
        elapsed = time.monotonic() - started
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ['deadline'])
        self.assertGreater(len(chunks), 1)
        self.assertLess(elapsed, 0.3)

    def test_http_deadline_configuration_is_bounded_before_bind(self):
        with self.assertRaisesRegex(ValueError, 'deadline'):
            make_http_server(self.adapter, request_deadline_seconds=120.1)

    def test_single_local_principal_ignores_spoofed_client_metadata(self):
        request = self._modern('events/list')
        request['params']['_meta']['openai/subject'] = 'spoofed-second-user'
        request['params']['_meta']['openai/session'] = 'spoofed-session'
        raw = json.dumps(request, separators=(',', ':')).encode()
        fields = {**self.common_headers, 'Content-Length': str(len(raw)),
                  'MCP-Protocol-Version': REQUEST_PROTOCOL, 'Mcp-Method': 'events/list'}
        status, _, response = self.local_endpoint.handle('POST', '/mcp', fields, raw)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response)['result']['method'], 'events/list')
        self.assertEqual(self.auth_capture.last_principal, current_local_principal())
        self.assertIsInstance(self.auth_capture.last_principal, LocalPrincipal)

    def test_legacy_session_is_bound_to_the_local_uid(self):
        initialize = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': PROTOCOL, 'capabilities': {},
            'clientInfo': {'name': 'fixture', 'version': '1'}}}
        raw = json.dumps(initialize, separators=(',', ':')).encode()
        headers = {**self.common_headers, 'Content-Length': str(len(raw)),
                   'MCP-Protocol-Version': PROTOCOL, 'Mcp-Method': 'initialize'}
        status, response_headers, _ = self.local_endpoint.handle('POST', '/mcp', headers, raw)
        self.assertEqual(status, 200)
        session_id = response_headers['Mcp-Session-Id']
        notification = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
        raw = json.dumps(notification, separators=(',', ':')).encode()
        headers = {**self.common_headers, 'Content-Length': str(len(raw)),
                   'MCP-Protocol-Version': PROTOCOL, 'Mcp-Method': 'notifications/initialized',
                   'Mcp-Session-Id': session_id}
        status, _, _ = self.local_endpoint.handle('POST', '/mcp', headers, raw)
        self.assertEqual(status, 202)


if __name__ == '__main__':
    unittest.main()
