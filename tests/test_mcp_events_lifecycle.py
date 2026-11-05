"""Offline MCP Events lifecycle, auth, transport and worker tests; no sockets sent."""
import base64
from contextlib import closing
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import sqlite3
import ssl
import sys
import tempfile
import threading
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from mcp_adapter import Adapter, PROTOCOL, REQUEST_PROTOCOL, VERSION_META, CAPABILITIES_META
from mcp_events_service import McpEventsService, event_definitions
from notification_outbox import (BLOCKED_REASON_CODES, EVENT_NAMES, BoundedResolver, NotificationError, NotificationOutbox,
    NotificationWorker, PinnedHTTPSConnection, PinnedWebhookSender, sign_request,
    validate_callback_url)
from local_principal import LocalPrincipal, current_local_principal


OWNER = current_local_principal()
OTHER = 'local-uid:other-process-user'
TASK = '00000000-0000-4000-8000-000000000041'
THREAD = '10000000-0000-4000-8000-000000000041'
SECRET = 'whsec_' + base64.b64encode(b'a' * 32).decode('ascii')
ROTATED_SECRET = 'whsec_' + base64.b64encode(b'b' * 32).decode('ascii')


class Cipher:
    """Explicitly insecure test-only encryption provider."""
    def encrypt(self, value):
        return b'lab:' + base64.urlsafe_b64encode(value)

    def decrypt(self, value):
        if not value.startswith(b'lab:'):
            raise ValueError('bad fixture')
        return base64.urlsafe_b64decode(value[4:])


class Sender:
    def __init__(self, status=204):
        self.status = status
        self.requests = []

    def __call__(self, url, ips, body, headers):
        self.requests.append((url, tuple(ips), body, dict(headers)))
        return {'status': self.status}


class EventLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mcp-events-lifecycle-')
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.source_db = self.root / 'pipeline.sqlite3'
        self.outbox_db = self.root / 'outbox.sqlite3'
        self.now = [1_800_000_000.0]
        self._write_state({'tasks': {TASK: {'id': TASK}}, 'events': []})
        self.resolutions = []
        self.outbox = self._outbox()
        self.bound_access = {(OWNER.identity, TASK)}
        self.challenge_count = []
        self.service = self._service()
        self.principal = OWNER

    def tearDown(self):
        self.temp.cleanup()

    def _write_state(self, state):
        with closing(sqlite3.connect(self.source_db)) as conn:
            with conn:
                conn.execute('CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
                conn.execute('INSERT INTO ledger VALUES (1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                             (json.dumps(state, sort_keys=True, separators=(',', ':')),))
        os.chmod(self.source_db, 0o600)

    def _outbox(self, resolver=None):
        return NotificationOutbox(self.outbox_db, source_db_path=self.source_db,
            secret_cipher=Cipher(), owner_hash_key=b'events-test-owner-key-000000000000000000000000',
            clock=lambda: self.now[0],
            resolver=resolver or self._resolve, base_backoff=0.1,
            max_backoff=0.4, max_attempts=4, lease_seconds=2)

    def _resolve(self, host):
        self.resolutions.append(host)
        return ['8.8.8.8']

    def _authorize(self, principal, task_id):
        return (principal, task_id) in self.bound_access

    def _owner_access(self, owner_hash, task_id):
        return (owner_hash == self.outbox.owner_hash(OWNER) and (OWNER.identity, task_id) in self.bound_access)

    def _verify_callback(self, request):
        self.challenge_count.append(request)
        payload = json.loads(request['body'])
        return {'status_code': 200, 'echoed_challenge': payload['challenge']}

    def _service(self, verifier=None):
        return McpEventsService(self.outbox, authorize_task=self._authorize,
            owner_access_check=self._owner_access,
            callback_verifier=self._verify_callback if verifier is None else verifier,
            default_ttl_ms=3_600_000, allow_external_callbacks=True)

    def _event(self, seq=1, kind='task_completed'):
        state = self.outbox._read_source_state()
        state['events'].append({'seq': seq, 'kind': kind, 'time': self.now[0],
                                'task_id': TASK, 'thread_id': THREAD})
        self._write_state(state)

    def _rpc(self, adapter, method, params=None, identifier=1):
        params = dict(params or {})
        params['_meta'] = {VERSION_META: REQUEST_PROTOCOL, CAPABILITIES_META: {}}
        return adapter.handle({'jsonrpc': '2.0', 'id': identifier,
                               'method': method, 'params': params})

    def _adapter(self, *, service=None, principal_provider=None):
        parent = PROJECT / '.runtime'
        parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
        tmp = tempfile.TemporaryDirectory(dir=parent, prefix='events-adapter-')
        root = Path(tmp.name)
        (root / 'logs').mkdir(mode=0o700)
        adapter = Adapter(root, events_service=service,
                          principal_provider=principal_provider)
        return tmp, adapter

    def _subscription_params(self, *, url='https://notify.example.net/mcp-events/cb', secret=SECRET):
        return {'name': EVENT_NAMES['task_completed'][0], 'arguments': {'task_id': TASK},
                'delivery': {'mode': 'webhook', 'url': url, 'secret': secret}, 'cursor': None}

    def test_action_required_event_schema_is_minimal_and_reason_allowlisted(self):
        self.assertEqual(BLOCKED_REASON_CODES, frozenset({
            'CATALOG_REJECTED', 'MODEL_NOT_AVAILABLE_OR_ALLOWED',
            'SUBSCRIPTION_AUTH_REQUIRED', 'RESUME_REJECTED',
            'RESUME_SCOPE_MISMATCH'}))
        definition = next(event for event in event_definitions()
                          if event['name'] == EVENT_NAMES['task_blocked'][0])
        data = definition['payloadSchema']['properties']['data']
        self.assertEqual(data['properties']['state'], {'type': 'string', 'const': 'BLOCKED'})
        self.assertEqual(data['properties']['action_required'], {'type': 'boolean', 'const': True})
        self.assertEqual(data['properties']['reason_code']['enum'], sorted(BLOCKED_REASON_CODES))
        self.assertEqual(set(data['required']), {
            'task_id', 'state', 'action_required', 'reason_code'})
        self.assertFalse(data['additionalProperties'])
        self.assertIn('user or operator action', definition['description'])

    def test_events_are_advertised_only_when_trusted_service_is_configured(self):
        tmp, disabled = self._adapter()
        try:
            discovered = self._rpc(disabled, 'server/discover')['result']
            self.assertEqual(set(discovered['capabilities']), {'tools'})
            self.assertEqual(self._rpc(disabled, 'events/list')['error']['code'], -32601)
        finally:
            disabled.audit.close()
            tmp.cleanup()

        self.service.bind_task_owner(TASK, OWNER)
        tmp, enabled = self._adapter(service=self.service, principal_provider=lambda: self.principal)
        try:
            discovered = self._rpc(enabled, 'server/discover')['result']
            self.assertEqual(set(discovered['capabilities']), {'tools', 'events'})
            listed = self._rpc(enabled, 'events/list')['result']['events']
            self.assertEqual({entry['name'] for entry in listed},
                             {name for name, _ in EVENT_NAMES.values()})
            self.assertTrue(all(entry['delivery'] == ['webhook'] for entry in listed))
            self.assertTrue(all(entry['inputSchema']['additionalProperties'] is False for entry in listed))
        finally:
            enabled.audit.close()
            tmp.cleanup()

    def test_legacy_task_without_owner_binding_stays_notification_disabled(self):
        self.assertEqual(self.service.list_events(OWNER), {'events': []})
        with self.assertRaisesRegex(NotificationError, 'TASK_NOT_AUTHORIZED'):
            self.service.subscribe(self._subscription_params(), OWNER)
        self.assertFalse(self.outbox.task_owned_by(TASK, OWNER))
        self.assertEqual(self.outbox.summary()['active_subscriptions'], 0)

    def test_external_callback_subscription_is_off_by_default(self):
        disabled = McpEventsService(self.outbox, authorize_task=self._authorize,
            owner_access_check=self._owner_access, callback_verifier=self._verify_callback)
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.list_events(OWNER), {'events': []})
        with self.assertRaisesRegex(NotificationError, 'EXTERNAL_CALLBACKS_DISABLED'):
            disabled.subscribe(self._subscription_params(), OWNER)
        tmp, adapter = self._adapter(service=disabled, principal_provider=lambda: OWNER)
        try:
            capabilities = self._rpc(adapter, 'server/discover')['result']['capabilities']
            self.assertEqual(set(capabilities), {'tools'})
            self.assertEqual(self._rpc(adapter, 'events/list')['error']['code'], -32601)
        finally:
            adapter.audit.close()
            tmp.cleanup()

    def test_owner_identity_is_derived_from_the_process_uid(self):
        self.service.bind_task_owner(TASK, OWNER)
        self.assertEqual(OWNER.identity, f'local-uid:{os.getuid()}')
        self.assertEqual(self.outbox.owner_hash(OWNER),
                         self.outbox.owner_hash(current_local_principal()))
        with self.assertRaisesRegex(ValueError, 'current local UID'):
            LocalPrincipal(os.getuid() + 1)

    def test_mcp_meta_and_plain_subject_strings_are_not_local_identity(self):
        with self.assertRaisesRegex(NotificationError, 'AUTHENTICATED_PRINCIPAL_REQUIRED'):
            self.service.bind_task_owner(TASK, OWNER.identity)
        tmp, adapter = self._adapter(service=self.service,
                                     principal_provider=lambda: OWNER.identity)
        try:
            reply = self._rpc(adapter, 'events/list', {'_meta': {
                'openai/subject': 'forged-user-subject',
                'openai/session': 'conversation-that-is-not-an-auth-claim'}})
            self.assertEqual(reply['error']['code'], -32000)
            self.assertEqual(reply['error']['data']['code'], 'AUTHENTICATED_PRINCIPAL_REQUIRED')
            self.assertFalse(self.outbox.task_owned_by(TASK, OWNER))
        finally:
            adapter.audit.close()
            tmp.cleanup()

    def test_subscribe_verifies_callback_returns_spec_fields_and_refresh_is_idempotent(self):
        self.service.bind_task_owner(TASK, OWNER)
        calls = []
        tmp, adapter = self._adapter(service=self.service,
                                     principal_provider=lambda: calls.append('auth') or self.principal)
        try:
            first = self._rpc(adapter, 'events/subscribe', self._subscription_params(
                url='https://NOTIFY.example.net:443/mcp-events/cb'))['result']
            second = self._rpc(adapter, 'events/subscribe', self._subscription_params())['result']
            self.assertEqual(first['id'], second['id'])
            self.assertEqual(set(first), {'id', 'refreshBefore', 'cursor', 'truncated', 'resultType', '_meta'})
            self.assertEqual(len(self.challenge_count), 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(self.outbox.summary()['active_subscriptions'], 1)
            self.assertNotIn(SECRET, json.dumps(first))
            self.assertNotIn('validated_ips', first)
            verification = self.challenge_count[0]
            signed_headers = verification['headers']
            signed_input = (signed_headers['webhook-id'] + '.' +
                            signed_headers['webhook-timestamp'] + '.').encode() + verification['body']
            expected_signature = 'v1,' + base64.b64encode(hmac.new(
                base64.b64decode(SECRET[6:]), signed_input, hashlib.sha256).digest()).decode()
            self.assertEqual(signed_headers['webhook-signature'], expected_signature)
            self.assertEqual(signed_headers['X-MCP-Subscription-Id'], first['id'])
            with closing(sqlite3.connect(self.outbox_db)) as conn:
                secret_ciphertext = conn.execute('SELECT secret_ciphertext FROM notification_subscriptions').fetchone()[0]
            self.assertNotIn(SECRET.encode(), secret_ciphertext)
        finally:
            adapter.audit.close()
            tmp.cleanup()

    def test_non_expiring_ttl_request_is_granted_only_finite_default_lifetime(self):
        self.service.bind_task_owner(TASK, OWNER)
        result = self.service.subscribe(
            {**self._subscription_params(), 'ttlMs': None}, OWNER)
        self.assertIsInstance(result['refreshBefore'], str)
        self.assertGreater(
            datetime.fromisoformat(result['refreshBefore'].replace('Z', '+00:00')).timestamp(),
            self.now[0])
        self.assertEqual(len(self.challenge_count), 1)

    def test_identity_comes_from_trusted_context_not_request_parameters(self):
        self.service.bind_task_owner(TASK, OWNER)
        tmp, adapter = self._adapter(service=self.service, principal_provider=lambda: self.principal)
        try:
            forged = self._subscription_params()
            forged['authenticated_principal'] = OTHER
            result = self._rpc(adapter, 'events/subscribe', forged)
            self.assertEqual(result['error']['code'], -32602)
            self.assertEqual(result['error']['data']['code'], 'INVALID_SUBSCRIBE_PARAMS')
            self.assertEqual(self.outbox.summary()['active_subscriptions'], 0)
            self.assertEqual(self.challenge_count, [])
            self.principal = OTHER
            denied = self._rpc(adapter, 'events/subscribe', self._subscription_params())
            self.assertEqual(denied['error']['data']['code'], 'AUTHENTICATED_PRINCIPAL_REQUIRED')
            self.assertEqual(self.resolutions, [])
        finally:
            adapter.audit.close()
            tmp.cleanup()

    def test_subscribe_rechecks_current_task_acl_after_owner_binding(self):
        self.service.bind_task_owner(TASK, OWNER)
        self.bound_access.remove((OWNER.identity, TASK))
        with self.assertRaisesRegex(NotificationError, 'TASK_NOT_AUTHORIZED'):
            self.service.subscribe(self._subscription_params(), OWNER)
        self.assertEqual(self.outbox.summary()['active_subscriptions'], 0)
        self.assertEqual(self.challenge_count, [])
        self.assertEqual(self.resolutions, [])

    def test_unsafe_callback_is_a_categorized_callback_endpoint_error(self):
        self.service.bind_task_owner(TASK, OWNER)
        tmp, adapter = self._adapter(service=self.service, principal_provider=lambda: OWNER)
        try:
            reply = self._rpc(adapter, 'events/subscribe', self._subscription_params(
                url='https://127.0.0.1/callback'))
            self.assertEqual(reply['error']['code'], -32015)
            self.assertEqual(reply['error']['message'], 'CallbackEndpointError')
            self.assertEqual(reply['error']['data'], {'reason': 'unsafe_destination'})
            self.assertEqual(self.resolutions, [])
        finally:
            adapter.audit.close()
            tmp.cleanup()

    def test_successful_task_submission_binds_local_owner_without_exporting_identity(self):
        # Rebuild the ledger with the task present; the fake submit returns its stable ID.
        class FakePipeline:
            calls = []
            def dispatch(inner_self, name, arguments):
                inner_self.calls.append((name, dict(arguments)))
                return {'id': TASK, 'state': 'QUEUED'}

        pipeline = FakePipeline()
        tmp = PROJECT / '.runtime'
        tmp.mkdir(mode=0o700, exist_ok=True)
        os.chmod(tmp, 0o700)
        temp_runtime = tempfile.TemporaryDirectory(dir=tmp, prefix='events-submit-')
        root = Path(temp_runtime.name)
        (root / 'logs').mkdir(mode=0o700)
        adapter = Adapter(root, allow_codex_pipeline=True, pipeline=pipeline,
                          events_service=self.service, principal_provider=lambda: OWNER)
        try:
            reply = self._rpc(adapter, 'tools/call', {'name': 'codex_task_submit',
                'arguments': {'thread_id': THREAD, 'prompt': 'private prompt',
                              'request_key': 'task-001'}})
            result = reply['result']['structuredContent']['data']
            self.assertEqual(result['notification_binding'], 'BOUND')
            self.assertTrue(self.outbox.task_owned_by(TASK, OWNER))
            self.assertNotIn(OWNER.identity, json.dumps(reply))
            self.assertNotIn('private prompt', adapter.audit_path.read_text())
        finally:
            adapter.audit.close()
            temp_runtime.cleanup()

    def test_submit_without_notification_context_still_runs_but_never_binds_an_owner(self):
        class FakePipeline:
            calls = []
            def dispatch(inner_self, name, arguments):
                inner_self.calls.append(name)
                return {'id': TASK, 'state': 'QUEUED'}

        pipeline = FakePipeline()
        parent = PROJECT / '.runtime'
        parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
        temp_runtime = tempfile.TemporaryDirectory(dir=parent, prefix='events-noauth-')
        root = Path(temp_runtime.name)
        (root / 'logs').mkdir(mode=0o700)
        adapter = Adapter(root, allow_codex_pipeline=True, pipeline=pipeline,
                          events_service=self.service,
                          principal_provider=lambda: (_ for _ in ()).throw(RuntimeError('no auth')))
        try:
            reply = self._rpc(adapter, 'tools/call', {'name': 'codex_task_submit',
                'arguments': {'thread_id': THREAD, 'prompt': 'must not submit',
                              'request_key': 'task-002'}})
            self.assertFalse(reply['result']['isError'])
            self.assertEqual(reply['result']['structuredContent']['data']['notification_binding'],
                             'UNAVAILABLE')
            self.assertEqual(pipeline.calls, ['codex_task_submit'])
            self.assertFalse(self.outbox.task_owned_by(TASK, OWNER))
        finally:
            adapter.audit.close()
            temp_runtime.cleanup()

    def test_owner_binding_failure_after_submit_never_replays_the_task(self):
        class FakePipeline:
            calls = []
            def dispatch(inner_self, name, arguments):
                inner_self.calls.append(name)
                return {'id': TASK, 'state': 'QUEUED'}

        pipeline = FakePipeline()
        original_authorizer = self.service.authorize_task
        self.service.authorize_task = lambda principal, task_id: False
        parent = PROJECT / '.runtime'
        parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
        temp_runtime = tempfile.TemporaryDirectory(dir=parent, prefix='events-bind-failure-')
        root = Path(temp_runtime.name)
        (root / 'logs').mkdir(mode=0o700)
        adapter = Adapter(root, allow_codex_pipeline=True, pipeline=pipeline,
                          events_service=self.service, principal_provider=lambda: OWNER)
        try:
            reply = self._rpc(adapter, 'tools/call', {'name': 'codex_task_submit',
                'arguments': {'thread_id': THREAD, 'prompt': 'submit once',
                              'request_key': 'bind-failure-001'}})
            data = reply['result']['structuredContent']['data']
            self.assertEqual(data['notification_binding'], 'UNAVAILABLE')
            self.assertEqual(pipeline.calls, ['codex_task_submit'])
            self.assertFalse(self.outbox.task_owned_by(TASK, OWNER))
        finally:
            self.service.authorize_task = original_authorizer
            adapter.audit.close()
            temp_runtime.cleanup()

    def test_callback_challenge_failure_is_protocol_error_and_stops_delivery(self):
        self.service = self._service(lambda request: {
            'status_code': 200, 'echoed_challenge': 'wrong-value'})
        self.service.bind_task_owner(TASK, OWNER)
        tmp, adapter = self._adapter(service=self.service, principal_provider=lambda: OWNER)
        try:
            reply = self._rpc(adapter, 'events/subscribe', self._subscription_params())
            self.assertEqual(reply['error']['code'], -32015)
            self.assertEqual(reply['error']['message'], 'CallbackEndpointError')
            self.assertEqual(reply['error']['data'], {'reason': 'challenge_failed'})
            self.assertEqual(self.outbox.summary()['active_subscriptions'], 0)
            self.assertEqual(self.outbox.summary()['pending_verification'], 0)
        finally:
            adapter.audit.close()
            tmp.cleanup()

    def test_failed_callback_can_be_reverified_and_replay_its_unacknowledged_event(self):
        self._event()
        self.service = self._service(lambda request: {
            'status_code': 200, 'echoed_challenge': 'wrong'})
        self.service.bind_task_owner(TASK, OWNER)
        with self.assertRaisesRegex(NotificationError, 'CALLBACK_CHALLENGE_FAILED'):
            self.service.subscribe(self._subscription_params(), OWNER)
        self.assertEqual(self.outbox.summary()['delivery_states'], {'CANCELLED': 1})
        self.service.callback_verifier = self._verify_callback
        replay = self.service.subscribe(self._subscription_params(), OWNER)
        self.assertEqual(replay['cursor'], '0')
        self.assertEqual(self.outbox.summary()['delivery_states'], {'PENDING': 1})
        sender = Sender()
        worker = NotificationWorker(self.outbox, sender,
                                    access_check=self.service.worker_access_check)
        self.assertEqual(worker.run_once(now=self.now[0])['status'], 'DELIVERED')
        self.assertEqual(len(sender.requests), 1)

    def test_unsubscribe_is_owner_bound_and_idempotent(self):
        self.service.bind_task_owner(TASK, OWNER)
        sub = self.service.subscribe(self._subscription_params(), OWNER)
        params = {'name': EVENT_NAMES['task_completed'][0],
                  'arguments': {'task_id': TASK},
                  'delivery': {'mode': 'webhook', 'url': 'https://notify.example.net/mcp-events/cb'}}
        with self.assertRaisesRegex(NotificationError, 'AUTHENTICATED_PRINCIPAL_REQUIRED'):
            self.service.unsubscribe(params, OTHER)
        self.assertEqual(self.service.unsubscribe(params, OWNER), {})
        self.assertEqual(self.service.unsubscribe(params, OWNER), {})
        self.assertEqual(self.outbox.summary()['active_subscriptions'], 0)
        self.assertEqual(sub['id'], self.outbox._subscription_id(
            self.outbox.owner_hash(OWNER),
            'https://notify.example.net/mcp-events/cb', EVENT_NAMES['task_completed'][0], {'task_id': TASK}))

    def test_owner_can_unsubscribe_after_current_task_acl_is_revoked(self):
        self.service.bind_task_owner(TASK, OWNER)
        self.service.subscribe(self._subscription_params(), OWNER)
        self.bound_access.remove((OWNER.identity, TASK))
        params = {'name': EVENT_NAMES['task_completed'][0],
                  'arguments': {'task_id': TASK},
                  'delivery': {'mode': 'webhook', 'url': 'https://notify.example.net/mcp-events/cb'}}
        self.assertEqual(self.service.unsubscribe(params, OWNER), {})
        self.assertEqual(self.outbox.summary()['active_subscriptions'], 0)

    def test_unsubscribe_wins_before_final_send_fence(self):
        self.service.bind_task_owner(TASK, OWNER)
        self._event()
        self.service.subscribe(self._subscription_params(), OWNER)
        params = {'name': EVENT_NAMES['task_completed'][0],
                  'arguments': {'task_id': TASK},
                  'delivery': {'mode': 'webhook', 'url': 'https://notify.example.net/mcp-events/cb'}}
        second_acl_check = threading.Event()
        release_acl_check = threading.Event()
        calls = []

        def access_check(owner_hash, task_id):
            calls.append((owner_hash, task_id))
            if len(calls) == 2:
                second_acl_check.set()
                release_acl_check.wait(2)
            return True

        sender = Sender()
        result = []
        thread = threading.Thread(target=lambda: result.append(
            self.outbox.deliver_one(sender, now=self.now[0], access_check=access_check)))
        thread.start()
        self.assertTrue(second_acl_check.wait(2))
        self.assertEqual(self.service.unsubscribe(params, OWNER), {})
        release_acl_check.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(sender.requests, [])
        self.assertEqual(result[0]['status'], 'CANCELLED')
        self.assertEqual(self.outbox.summary()['delivery_states'], {'CANCELLED': 1})

    def test_in_flight_send_can_finish_after_unsubscribe_ack(self):
        self.service.bind_task_owner(TASK, OWNER)
        self._event()
        self.service.subscribe(self._subscription_params(), OWNER)
        params = {'name': EVENT_NAMES['task_completed'][0],
                  'arguments': {'task_id': TASK},
                  'delivery': {'mode': 'webhook', 'url': 'https://notify.example.net/mcp-events/cb'}}
        send_started = threading.Event()
        release_send = threading.Event()

        class BlockingSender:
            def __call__(self, url, ips, body, headers):
                send_started.set()
                release_send.wait(2)
                return {'status': 204}

        result = []
        thread = threading.Thread(target=lambda: result.append(
            self.outbox.deliver_one(BlockingSender(), now=self.now[0],
                                    access_check=lambda owner_hash, task_id: True)))
        thread.start()
        self.assertTrue(send_started.wait(2))
        self.assertEqual(self.service.unsubscribe(params, OWNER), {})
        release_send.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]['status'], 'DELIVERED')
        self.assertEqual(self.outbox.summary()['active_subscriptions'], 0)

    def test_unsubscribe_protocol_handler_matches_canonical_callback_url(self):
        self.service.bind_task_owner(TASK, OWNER)
        tmp, adapter = self._adapter(service=self.service, principal_provider=lambda: OWNER)
        try:
            subscribed = self._rpc(adapter, 'events/subscribe', self._subscription_params(
                url='https://NOTIFY.example.net:443/mcp-events/cb'))['result']
            response = self._rpc(adapter, 'events/unsubscribe', {
                'name': EVENT_NAMES['task_completed'][0],
                'arguments': {'task_id': TASK},
                'delivery': {'mode': 'webhook',
                             'url': 'https://notify.example.net/mcp-events/cb'}})
            self.assertEqual(response['result'], {'resultType': 'complete',
                                                   '_meta': {'io.modelcontextprotocol/serverInfo': {
                                                       'name': 'agent-orchestrator-local', 'version': '0.2.0'}}})
            self.assertEqual(self.outbox.summary()['active_subscriptions'], 0)
            self.assertTrue(subscribed['id'].startswith('sub_'))
        finally:
            adapter.audit.close()
            tmp.cleanup()

    def test_cursor_does_not_advance_past_unacknowledged_delivery(self):
        self.service.bind_task_owner(TASK, OWNER)
        self._event(seq=1, kind='task_completed')
        result = self.service.subscribe(self._subscription_params(), OWNER)
        self.assertEqual(result['cursor'], '0')
        self.assertFalse(result['truncated'])
        sender = Sender()
        worker = NotificationWorker(self.outbox, sender, access_check=self.service.worker_access_check,
                                    poll_seconds=0.05)
        delivered = worker.run_once(now=self.now[0])
        self.assertEqual(delivered['status'], 'DELIVERED')
        self.assertEqual(json.loads(sender.requests[0][2])['cursor'], '1')
        refreshed = self.service.subscribe({**self._subscription_params(), 'cursor': '1'}, OWNER)
        self.assertEqual(refreshed['cursor'], '1')

    def test_expired_subscription_refresh_recovers_unacknowledged_delivery(self):
        self.service.bind_task_owner(TASK, OWNER)
        params = {**self._subscription_params(), 'ttlMs': 60_000}
        subscription = self.service.subscribe(params, OWNER)
        self._event()
        self.outbox.sync_from_ledger()
        self.now[0] += 61
        self.assertIsNone(self.outbox.claim_due(now=self.now[0]))
        self.assertEqual(self.outbox.summary()['delivery_states'], {'EXPIRED': 1})
        refreshed = self.service.subscribe({**params, 'cursor': subscription['cursor']}, OWNER)
        self.assertEqual(refreshed['id'], subscription['id'])
        self.assertEqual(self.outbox.summary()['delivery_states'], {'PENDING': 1})
        sender = Sender()
        worker = NotificationWorker(self.outbox, sender,
                                    access_check=self.service.worker_access_check)
        self.assertEqual(worker.run_once(now=self.now[0])['status'], 'DELIVERED')

    def test_callback_verification_cache_expires(self):
        self.service.bind_task_owner(TASK, OWNER)
        params = self._subscription_params()
        self.service.subscribe(params, OWNER)
        self.service.subscribe(params, OWNER)
        self.assertEqual(len(self.challenge_count), 1)
        self.now[0] += 301
        self.service.subscribe(params, OWNER)
        self.assertEqual(len(self.challenge_count), 2)

    def test_worker_rechecks_owner_revocation_before_any_send(self):
        self.service.bind_task_owner(TASK, OWNER)
        self.service.subscribe(self._subscription_params(), OWNER)
        self._event()
        self.bound_access.clear()
        sender = Sender()
        worker = NotificationWorker(self.outbox, sender,
                                    access_check=self.service.worker_access_check)
        result = worker.run_once(now=self.now[0])
        self.assertEqual(result['status'], 'CANCELLED')
        self.assertEqual(sender.requests, [])
        self.assertEqual(self.outbox.summary()['active_subscriptions'], 0)

    def test_acl_revocation_during_callback_resolution_stops_send(self):
        self.service.bind_task_owner(TASK, OWNER)
        self._event()
        self.service.subscribe(self._subscription_params(), OWNER)

        def revoke_during_resolution(host):
            self.bound_access.discard((OWNER.identity, TASK))
            return ['8.8.8.8']

        self.outbox.resolver = revoke_during_resolution
        sender = Sender()
        worker = NotificationWorker(self.outbox, sender,
                                    access_check=self.service.worker_access_check)
        result = worker.run_once(now=self.now[0])
        self.assertEqual(result['status'], 'CANCELLED')
        self.assertEqual(sender.requests, [])
        self.assertEqual(self.outbox.summary()['delivery_states'], {'CANCELLED': 1})

    def test_worker_retries_when_access_recheck_is_unknown(self):
        self.service.bind_task_owner(TASK, OWNER)
        subscription = self.service.subscribe(self._subscription_params(), OWNER)
        self._event()
        self.service.owner_access_check = lambda owner_hash, task_id: None
        sender = Sender()
        worker = NotificationWorker(self.outbox, sender,
                                    access_check=self.service.worker_access_check)
        result = worker.run_once(now=self.now[0])
        self.assertEqual(result['status'], 'RETRY')
        status = self.outbox.delivery_status(result['event_id'], subscription['id'])
        self.assertEqual(status['last_error_code'], 'ACCESS_RECHECK_UNAVAILABLE')
        self.assertEqual(sender.requests, [])
        self.assertEqual(self.outbox.summary()['active_subscriptions'], 1)

    def test_delivery_rejects_dns_rebinding_to_private_address(self):
        self.service.bind_task_owner(TASK, OWNER)
        subscription = self.service.subscribe(self._subscription_params(), OWNER)
        self._event()
        sender = Sender()
        self.outbox.resolver = lambda host: ['127.0.0.1']
        worker = NotificationWorker(self.outbox, sender,
                                    access_check=self.service.worker_access_check)
        result = worker.run_once(now=self.now[0])
        self.assertEqual(result['status'], 'PERMANENT_FAILURE')
        self.assertEqual(self.outbox.delivery_status(result['event_id'], subscription['id'])['last_error_code'],
                         'UNSAFE_CALLBACK_ADDRESS')
        self.assertEqual(sender.requests, [])

    def test_worker_runs_in_background_and_stops_cleanly(self):
        self.service.bind_task_owner(TASK, OWNER)
        self.service.subscribe(self._subscription_params(), OWNER)
        self._event()
        delivered = threading.Event()

        class SignalSender(Sender):
            def __call__(inner_self, url, ips, body, headers):
                result = super().__call__(url, ips, body, headers)
                delivered.set()
                return result

        sender = SignalSender()
        worker = NotificationWorker(self.outbox, sender,
                                    access_check=self.service.worker_access_check,
                                    poll_seconds=0.05)
        self.assertTrue(worker.start())
        self.assertTrue(delivered.wait(1.0))
        self.assertTrue(worker.stop(timeout=1.0))
        self.assertEqual(len(sender.requests), 1)

    def test_signing_secret_rotation_uses_both_keys_temporarily(self):
        self.service.bind_task_owner(TASK, OWNER)
        first = self.service.subscribe(self._subscription_params(), OWNER)
        self.service.subscribe(self._subscription_params(secret=ROTATED_SECRET), OWNER)
        self._event()
        sender = Sender()
        worker = NotificationWorker(self.outbox, sender,
                                    access_check=self.service.worker_access_check)
        self.assertEqual(worker.run_once(now=self.now[0])['status'], 'DELIVERED')
        signatures = sender.requests[0][3]['webhook-signature'].split(' ')
        self.assertEqual(len(signatures), 2)
        body = sender.requests[0][2]
        timestamp = sender.requests[0][3]['webhook-timestamp']
        event_id = sender.requests[0][3]['webhook-id']
        for signature, secret in zip(signatures, (ROTATED_SECRET, SECRET)):
            key = base64.b64decode(secret[6:])
            expected = base64.b64encode(hmac.new(key, f'{event_id}.{timestamp}.'.encode() + body,
                                                 hashlib.sha256).digest()).decode()
            self.assertEqual(signature, 'v1,' + expected)
        self.now[0] += 301
        self._event(seq=2)
        self.outbox.sync_from_ledger()
        self.assertEqual(worker.run_once(now=self.now[0])['status'], 'DELIVERED')
        self.assertEqual(len(sender.requests[-1][3]['webhook-signature'].split(' ')), 1)
        self.assertEqual(first['id'], self.outbox._subscription_id(
            self.outbox.owner_hash(OWNER),
            'https://notify.example.net/mcp-events/cb', EVENT_NAMES['task_completed'][0], {'task_id': TASK}))


class SsrfAndTransportTests(unittest.TestCase):
    def test_outbox_requires_vault_backed_owner_hash_and_explicit_bounded_resolver(self):
        with tempfile.TemporaryDirectory(prefix='mcp-events-key-required-') as tmp:
            root = Path(tmp)
            os.chmod(root, 0o700)
            source = root / 'pipeline.sqlite3'
            with closing(sqlite3.connect(source)) as conn:
                with conn:
                    conn.execute('CREATE TABLE ledger (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
                    conn.execute('INSERT INTO ledger VALUES (1,?)', ('{"events":[],"tasks":{}}',))
            os.chmod(source, 0o600)
            with self.assertRaisesRegex(NotificationError, 'OWNER_HASH_KEY_UNAVAILABLE'):
                NotificationOutbox(root / 'outbox.sqlite3', source_db_path=source,
                                   secret_cipher=Cipher(), resolver=lambda host: ['8.8.8.8'])
            with self.assertRaisesRegex(NotificationError, 'BOUNDED_RESOLVER_REQUIRED'):
                NotificationOutbox(root / 'outbox.sqlite3', source_db_path=source,
                                   secret_cipher=Cipher(), owner_hash_key=b'x' * 32)

    def test_callback_validation_rejects_private_mixed_ip_bad_host_userinfo_and_port(self):
        unsafe = [
            ('https://callback.example.net/x', ['8.8.8.8', '127.0.0.1']),
            ('https://callback.example.net/x', ['10.0.0.7']),
            ('https://localhost/x', ['8.8.8.8']),
            ('https://bad..example.net/x', ['8.8.8.8']),
            ('https://user@callback.example.net/x', ['8.8.8.8']),
            ('https://callback.example.net:444/x', ['8.8.8.8']),
            ('http://callback.example.net/x', ['8.8.8.8']),
        ]
        for url, addresses in unsafe:
            with self.subTest(url=url), self.assertRaises(NotificationError):
                validate_callback_url(url, lambda host: addresses)
        self.assertEqual(validate_callback_url('https://CALLBACK.example.net:443/x',
                         lambda host: ['8.8.8.8']), ('8.8.8.8',))

    def test_sender_resolves_again_pins_ip_and_does_not_follow_redirect(self):
        calls = []

        class Response:
            status = 302
            def read(self, size): return b''

        class Connection:
            def request(self, method, path, *, body, headers):
                calls.append(('request', method, path, body, dict(headers)))
            def getresponse(self): return Response()
            def close(self): calls.append(('close',))

        def factory(host, ip, *, timeout):
            calls.append(('connect', host, ip, timeout))
            return Connection()

        lookups = []
        class Resolver:
            interruptible = True
            def resolve_with_timeout(self, host, _timeout_seconds):
                lookups.append(host)
                return ['1.1.1.1']

        resolver = BoundedResolver(Resolver())
        sender = PinnedWebhookSender(resolver=resolver,
                                     connection_factory=factory)
        result = sender('https://callback.example.net/path?q=1', ('8.8.8.8',), b'{}',
                        {'Content-Type': 'application/json', 'webhook-id': 'evt',
                         'webhook-timestamp': '1', 'webhook-signature': 'v1,abc',
                         'X-MCP-Subscription-Id': 'sub'})
        self.assertEqual(result, {'status': 302})
        self.assertEqual(lookups, ['callback.example.net'])
        connect_call = next(call for call in calls if call[0] == 'connect')
        self.assertEqual(connect_call[:3], ('connect', 'callback.example.net', '1.1.1.1'))
        self.assertAlmostEqual(connect_call[3], 10.0, places=2)
        self.assertIn(('request', 'POST', '/path?q=1', b'{}', {
            'Content-Type': 'application/json', 'webhook-id': 'evt',
            'webhook-timestamp': '1', 'webhook-signature': 'v1,abc',
            'X-MCP-Subscription-Id': 'sub', 'Connection': 'close'}), calls)
        self.assertEqual(sum(1 for call in calls if call[0] == 'request'), 1)

    def test_sender_rejects_missing_or_injected_headers_before_connect(self):
        calls = []

        class Connection:
            def request(self, *args, **kwargs): calls.append('request')
            def close(self): calls.append('close')

        class Resolver:
            interruptible = True
            def resolve_with_timeout(self, _host, _timeout_seconds): return ['1.1.1.1']

        sender = PinnedWebhookSender(
            resolver=BoundedResolver(Resolver()),
            connection_factory=lambda *args, **kwargs: calls.append('connect') or Connection())
        valid = {'Content-Type': 'application/json', 'webhook-id': 'evt',
                 'webhook-timestamp': '1', 'webhook-signature': 'v1,abc',
                 'X-MCP-Subscription-Id': 'sub'}
        malformed = [
            {key: value for key, value in valid.items() if key != 'webhook-id'},
            {**valid, 'webhook-signature': 'v1,abc\r\nX-Evil: yes'},
            {**valid, 'webhook-id': 'evt\x00'},
        ]
        for headers in malformed:
            with self.subTest(headers=headers), self.assertRaisesRegex(
                    NotificationError, 'INVALID_WEBHOOK_HEADERS'):
                sender('https://callback.example.net/path', ('8.8.8.8',), b'{}', headers)
        self.assertEqual(calls, [])

    def test_tls_connection_connects_to_ip_but_checks_original_hostname(self):
        observed = []

        class RawSocket:
            def close(self): observed.append(('raw_closed',))

        class TestConnection(PinnedHTTPSConnection):
            def _wrap_tls(self, raw):
                observed.append(('tls', self.host))
                class TlsSocket:
                    def settimeout(self, timeout): observed.append(('timeout', timeout))
                    def do_handshake(self): observed.append(('handshake',))
                    def close(self): observed.append(('tls_closed',))
                return TlsSocket()

        def socket_factory(address, timeout, source_address):
            observed.append(('tcp', address, timeout, source_address))
            return RawSocket()

        conn = TestConnection('callback.example.net', '8.8.8.8', timeout=7,
                              context=ssl.create_default_context(), socket_factory=socket_factory)
        conn.connect()
        self.assertEqual(observed, [('tcp', ('8.8.8.8', 443), 7.0, None),
                                    ('tls', 'callback.example.net'), ('timeout', 7.0),
                                    ('handshake',)])
        conn.close()
        called = []
        with self.assertRaisesRegex(NotificationError, 'UNSAFE_CALLBACK_ADDRESS'):
            PinnedHTTPSConnection('callback.example.net', '127.0.0.1', context=ssl.create_default_context(),
                                  socket_factory=lambda *args: called.append(args))
        self.assertEqual(called, [])

    def test_callback_response_total_deadline_stops_trickling_body(self):
        stopped = threading.Event()
        chunks = []

        class SlowSocket:
            def shutdown(self, _how):
                stopped.set()
            def close(self):
                stopped.set()

        class SlowResponse:
            status = 204
            def read(self, _limit):
                while not stopped.wait(0.005):
                    chunks.append(b'x')
                raise OSError('callback socket shut down')

        class SlowConnection:
            def __init__(self, *_args, **_kwargs):
                self.sock = SlowSocket()
            def request(self, *_args, **_kwargs):
                return None
            def getresponse(self):
                return SlowResponse()
            def close(self):
                self.sock.close()

        class Resolver:
            interruptible = True
            def resolve_with_timeout(self, _host, _timeout_seconds): return ['8.8.8.8']

        sender = PinnedWebhookSender(resolver=BoundedResolver(Resolver()), timeout=0.05,
                                     connection_factory=SlowConnection)
        headers = {'Content-Type': 'application/json', 'webhook-id': 'evt',
                   'webhook-timestamp': '1', 'webhook-signature': 'v1,abc',
                   'X-MCP-Subscription-Id': 'sub'}
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, 'deadline'):
            sender('https://callback.example.net/path', ('8.8.8.8',), b'{}', headers)
        elapsed = time.monotonic() - started
        self.assertGreater(len(chunks), 1)
        self.assertLess(elapsed, 0.3)

    def test_callback_total_deadline_includes_bounded_dns_lookup(self):
        observed = []

        class SlowResolver:
            interruptible = True
            def resolve_with_timeout(self, _host, timeout_seconds):
                observed.append(timeout_seconds)
                # Deliberately return slightly after its advertised budget to
                # verify both the provider bound and the enclosing deadline.
                time.sleep(timeout_seconds + 0.01)
                return ['8.8.8.8']

        sender = PinnedWebhookSender(
            resolver=BoundedResolver(SlowResolver(), timeout=1.0), timeout=0.05,
            connection_factory=lambda *_args, **_kwargs: self.fail('DNS timeout must prevent connect'))
        headers = {'Content-Type': 'application/json', 'webhook-id': 'evt',
                   'webhook-timestamp': '1', 'webhook-signature': 'v1,abc',
                   'X-MCP-Subscription-Id': 'sub'}
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, 'deadline'):
            sender('https://callback.example.net/path', ('8.8.8.8',), b'{}', headers)
        elapsed = time.monotonic() - started
        self.assertEqual(len(observed), 1)
        self.assertLessEqual(observed[0], 0.05)
        self.assertLess(elapsed, 0.2)

    def test_bounded_resolver_clamps_sender_budget_to_provider_limit(self):
        observed = []

        class Resolver:
            interruptible = True
            def resolve_with_timeout(self, _host, timeout_seconds):
                observed.append(timeout_seconds)
                return ['8.8.8.8']

        resolver = BoundedResolver(Resolver(), timeout=0.1)
        self.assertEqual(resolver.resolve_with_timeout('callback.example.net', 10.0),
                         ['8.8.8.8'])
        self.assertEqual(observed, [0.1])

    def test_tls_verification_cannot_be_disabled(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with self.assertRaisesRegex(NotificationError, 'TLS_VERIFICATION_REQUIRED'):
            PinnedHTTPSConnection('callback.example.net', '8.8.8.8', context=context)


if __name__ == '__main__':
    unittest.main()
