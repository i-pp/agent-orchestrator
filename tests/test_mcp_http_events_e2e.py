"""Offline Streamable HTTP -> MCP Events -> durable outbox integration.

Every destination, encryption primitive, resolver, and callback response is a
disposable in-process fixture. No socket or external network is used.
"""
import base64
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))

from local_principal import OAuthPrincipal
from mcp_adapter import (CAPABILITIES_META, REQUEST_PROTOCOL, VERSION_META)
from mcp_http_transport import McpHttpEndpoint
from notification_outbox import BoundedResolver, EVENT_NAMES
from notification_runtime import (EVENTS_ENABLE_ENV,
                                  build_systemd_notification_runtime)


OWNER = OAuthPrincipal('https://issuer.example.test', 'test-owner')
OTHER = OAuthPrincipal('https://issuer.example.test', 'other-user')
TASK = '00000000-0000-4000-8000-000000000301'
THREAD = '10000000-0000-4000-8000-000000000301'
CALLBACK = 'https://events.example.test/synthetic-callback'
SIGNING_SECRET = 'whsec_' + base64.b64encode(
    b'test-only-signing-secret-32bytes!!').decode('ascii')
OWNER_HASH_KEY = b'test-only-owner-hash-key-0123456789abcdef'


class TestOnlyCipher:
    def encrypt(self, value):
        return b'test-only:' + base64.urlsafe_b64encode(value)

    def decrypt(self, value):
        prefix = b'test-only:'
        if not value.startswith(prefix):
            raise ValueError('invalid test ciphertext')
        return base64.urlsafe_b64decode(value[len(prefix):])


class FixtureResolver:
    interruptible = True

    def resolve_with_timeout(self, _host, timeout_seconds):
        if not 0 < timeout_seconds <= 10:
            raise TimeoutError('fixture deadline')
        return ['8.8.8.8']


class InMemorySender:
    def __init__(self, resolver):
        self.resolver = resolver
        self.requests = []
        self.sent = threading.Event()

    def __call__(self, url, ips, body, headers):
        self.requests.append((url, tuple(ips), body, dict(headers)))
        self.sent.set()
        return {'status': 204}


class FixtureChallengeVerifier:
    def __init__(self, resolver):
        self.resolver = resolver

    def __call__(self, request):
        challenge = json.loads(request['body'].decode('utf-8'))['challenge']
        return {'status_code': 200, 'echoed_challenge': challenge}


class FixtureOAuthVerifier:
    def verify(self, authorization):
        principals = {'Bearer offline-fixture-token': OWNER,
                      'Bearer offline-other-token': OTHER}
        if authorization not in principals:
            raise ValueError('unauthorized')
        return principals[authorization]


class SubmitPipeline:
    def __init__(self, write_state):
        self.write_state = write_state

    def dispatch(self, name, _arguments):
        if name != 'codex_task_submit':
            raise AssertionError('unexpected pipeline tool')
        self.write_state({'tasks': {TASK: {'id': TASK, 'state': 'QUEUED'}},
                          'events': []})
        return {'id': TASK, 'state': 'QUEUED'}


class PipelineView:
    def __init__(self, path, outbox):
        self.db = Path(path)
        self.notification_outbox = outbox

    def sync_notifications(self):
        return self.notification_outbox.sync_from_ledger()


class HttpEventsE2ETests(unittest.TestCase):
    def setUp(self):
        runtime_parent = PROJECT / '.runtime'
        runtime_parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(runtime_parent, 0o700)
        self.temp = tempfile.TemporaryDirectory(prefix='http-events-e2e-', dir=runtime_parent)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        (self.root / 'logs').mkdir(mode=0o700)
        self.source_db = self.root / 'codex-pipeline.sqlite3'
        self.write_state({'tasks': {}, 'threads': {THREAD: {'id': THREAD}}, 'events': []})

    def tearDown(self):
        self.temp.cleanup()

    def write_state(self, state):
        packed = json.dumps(state, sort_keys=True, separators=(',', ':'))
        with closing(sqlite3.connect(self.source_db)) as conn:
            with conn:
                conn.execute('''CREATE TABLE IF NOT EXISTS ledger
                    (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)''')
                conn.execute('''INSERT INTO ledger VALUES (1,?) ON CONFLICT(id)
                    DO UPDATE SET body=excluded.body''', (packed,))
        os.chmod(self.source_db, 0o600)

    def http_call(self, endpoint, method, params, identifier,
                  authorization='Bearer offline-fixture-token'):
        payload = dict(params)
        payload['_meta'] = {VERSION_META: REQUEST_PROTOCOL, CAPABILITIES_META: {}}
        message = {'jsonrpc': '2.0', 'id': identifier, 'method': method,
                   'params': payload}
        body = json.dumps(message, separators=(',', ':')).encode('utf-8')
        headers = {
            'Host': '127.0.0.1:8765',
            'Content-Type': 'application/json; charset=utf-8',
            'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': REQUEST_PROTOCOL,
            'Mcp-Method': method,
            'Content-Length': str(len(body)),
        }
        if method == 'tools/call':
            headers['Mcp-Name'] = payload.get('name')
        headers['Authorization'] = authorization
        status, _, response = endpoint.handle(
            'POST', '/mcp', headers, body, remote_addr='127.0.0.1')
        self.assertEqual(status, 200, response.decode('utf-8', errors='replace'))
        return json.loads(response)

    def test_http_subscription_persists_and_delivers_one_synthetic_completion(self):
        resolver = BoundedResolver(FixtureResolver())
        sender = InMemorySender(resolver)
        providers = {
            'secret_cipher': TestOnlyCipher(),
            'owner_hash_key': OWNER_HASH_KEY,
            'resolver': resolver,
            'sender': sender,
            'callback_verifier': FixtureChallengeVerifier(resolver),
        }
        runtime = build_systemd_notification_runtime(
            self.root, events_transport=True,
            environ={EVENTS_ENABLE_ENV: '1'},
            authenticated_principal=OWNER,
            provider_loader=lambda **_kwargs: providers)
        self.assertTrue(runtime.enabled, runtime.disabled_reason)

        from mcp_adapter import Adapter
        adapter = Adapter(self.root, allow_codex_pipeline=True,
                          pipeline=SubmitPipeline(self.write_state),
                          notification_runtime=runtime)
        self.assertTrue(runtime.events_service.bind_thread_owner(THREAD, OWNER))
        endpoint = McpHttpEndpoint(
            adapter, expected_port=8765, oauth_verifier=FixtureOAuthVerifier(),
            oauth_metadata={'resource': 'https://mcp.example.test/mcp',
                            'authorization_servers': ['https://issuer.example.test'],
                            'scopes_supported': ['mcp:access'],
                            'bearer_methods_supported': ['header']},
            oauth_metadata_uri='https://mcp.example.test/.well-known/oauth-protected-resource')
        try:
            discovered = self.http_call(endpoint, 'server/discover', {}, 1)
            self.assertIn('events', discovered['result']['capabilities'])

            event_name = EVENT_NAMES['task_completed'][0]
            submitted = self.http_call(endpoint, 'tools/call', {
                'name': 'codex_task_submit',
                'arguments': {'thread_id': THREAD, 'prompt': 'synthetic fixture',
                              'request_key': 'http-e2e-001'},
            }, 3)
            data = submitted['result']['structuredContent']['data']
            self.assertEqual(data['notification_binding'], 'BOUND')
            self.assertTrue(runtime.outbox.task_owned_by(TASK, OWNER))

            listed = self.http_call(endpoint, 'events/list', {}, 4)
            self.assertIn(event_name, {item['name'] for item in listed['result']['events']})

            other_listed = self.http_call(endpoint, 'events/list', {}, 41,
                                          authorization='Bearer offline-other-token')
            self.assertEqual(other_listed['result']['events'], [])
            other_subscribe = self.http_call(endpoint, 'events/subscribe', {
                'name': event_name, 'arguments': {'task_id': TASK},
                'delivery': {'mode': 'webhook', 'url': CALLBACK,
                             'secret': SIGNING_SECRET}}, 42,
                authorization='Bearer offline-other-token')
            self.assertEqual(other_subscribe['error']['data']['code'], 'TASK_NOT_AUTHORIZED')
            self.assertEqual(runtime.outbox.summary()['active_subscriptions'], 0)

            subscribed = self.http_call(endpoint, 'events/subscribe', {
                'name': event_name,
                'arguments': {'task_id': TASK},
                'delivery': {'mode': 'webhook', 'url': CALLBACK,
                             'secret': SIGNING_SECRET},
            }, 5)
            subscription_id = subscribed['result']['id']
            self.assertEqual(runtime.outbox.summary()['active_subscriptions'], 1)

            occurred = time.monotonic()
            self.write_state({
                'tasks': {TASK: {'id': TASK, 'state': 'COMPLETED'}},
                'events': [{'seq': 1, 'kind': 'task_completed',
                            'time': time.time(), 'task_id': TASK,
                            'thread_id': THREAD}],
            })
            worker = runtime.worker_factory(
                PipelineView(self.source_db, runtime.outbox))
            result = worker.run_once()
            self.assertEqual(result['status'], 'DELIVERED')
            self.assertEqual(len(sender.requests), 1)
            self.assertLess(time.monotonic() - occurred, 1.0)

            url, addresses, body, headers = sender.requests[0]
            payload = json.loads(body.decode('utf-8'))
            self.assertEqual(url, CALLBACK)
            self.assertEqual(addresses, ('8.8.8.8',))
            self.assertEqual(payload['name'], event_name)
            self.assertEqual(payload['data'], {
                'task_id': TASK, 'state': 'COMPLETED', 'thread_id': THREAD})
            self.assertEqual(headers['webhook-id'], payload['eventId'])
            self.assertEqual(headers['X-MCP-Subscription-Id'], subscription_id)
            self.assertTrue(headers['webhook-signature'].startswith('v1,'))

            # A freshly reconstructed runtime represents a daemon/adapter
            # restart. Reconciliation after the event is already delivered is
            # idempotent and cannot produce a duplicate callback.
            restarted = build_systemd_notification_runtime(
                self.root, events_transport=True,
                environ={EVENTS_ENABLE_ENV: '1'},
                authenticated_principal=OWNER,
                provider_loader=lambda **_kwargs: providers)
            self.assertTrue(restarted.enabled, restarted.disabled_reason)
            restarted_worker = restarted.worker_factory(
                PipelineView(self.source_db, restarted.outbox))
            second = restarted_worker.run_once()
            self.assertEqual(second['status'], 'EMPTY')
            self.assertEqual(len(sender.requests), 1)
            self.assertEqual(runtime.outbox.summary()['delivery_states'],
                             {'DELIVERED': 1})
        finally:
            adapter.audit.close()


if __name__ == '__main__':
    unittest.main()
