import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from notification_outbox import (BLOCKED_REASON_CODES, EVENT_NAMES, NotificationError, NotificationOutbox,
    NotificationWorker, sign_request, validate_callback_url, validate_signing_secret)
from codex_pipeline import Pipeline
from codex_host import HostGateway
from codex_app_protocol import RpcError
from local_principal import AcePrincipal, OAuthPrincipal


OWNER = 'mcp-principal:test-user-1'
OTHER_OWNER = 'mcp-principal:test-user-2'
TASK_A = '00000000-0000-4000-8000-000000000001'
TASK_B = '00000000-0000-4000-8000-000000000002'
THREAD_A = '10000000-0000-4000-8000-000000000001'
SECRET = 'whsec_' + base64.b64encode(b'x' * 32).decode('ascii')


class TestCipher:
    """Insecure reversible test double; production requires a real secret vault."""
    def encrypt(self, value):
        return b'test-cipher:' + base64.urlsafe_b64encode(value)

    def decrypt(self, value):
        if not value.startswith(b'test-cipher:'):
            raise ValueError('bad fixture ciphertext')
        return base64.urlsafe_b64decode(value[len(b'test-cipher:'):])


class FakeSender:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, url, pinned_ips, body, headers):
        self.requests.append((url, tuple(pinned_ips), body, dict(headers)))
        if not self.outcomes:
            return {'status': 204}
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class NotificationOutboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='notification-outbox-')
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.source_db = self.root / 'codex-pipeline.sqlite3'
        self.outbox_db = self.root / 'notification-outbox.sqlite3'
        self.now = [1_800_000_000.0]
        self.write_source({'tasks': {TASK_A: {'id': TASK_A}, TASK_B: {'id': TASK_B}}, 'events': []})
        self.outbox = self.make_outbox()

    def tearDown(self):
        self.temp.cleanup()

    def make_outbox(self):
        return NotificationOutbox(self.outbox_db, source_db_path=self.source_db,
            secret_cipher=TestCipher(), owner_hash_key=b'outbox-test-owner-key-000000000000000000000000',
            clock=lambda: self.now[0],
            resolver=lambda host: ['8.8.8.8'], base_backoff=1, max_backoff=4,
            max_attempts=4, lease_seconds=5)

    def test_verified_oidc_identity_maps_to_stable_opaque_aceis_principal(self):
        alice_oidc = OAuthPrincipal('https://issuer-a.example.test', 'opaque-sub-alice')
        bob_oidc = OAuthPrincipal('https://issuer-a.example.test', 'opaque-sub-bob')
        same_subject_from_other_issuer = OAuthPrincipal(
            'https://issuer-b.example.test', 'opaque-sub-alice')
        alice = self.outbox.resolve_oidc_principal(alice_oidc)
        self.assertIsInstance(alice, AcePrincipal)
        self.assertEqual(alice, self.outbox.resolve_oidc_principal(alice_oidc))
        bob = self.outbox.resolve_oidc_principal(bob_oidc)
        self.assertNotEqual(alice, bob)
        issuer_b_alice = self.outbox.resolve_oidc_principal(same_subject_from_other_issuer)
        self.assertNotEqual(alice, issuer_b_alice)

        restarted_outbox = self.make_outbox()
        self.assertEqual(alice, restarted_outbox.resolve_oidc_principal(alice_oidc))
        raw = self.outbox_db.read_bytes()
        self.assertNotIn(b'issuer-a.example.test', raw)
        self.assertNotIn(b'opaque-sub-alice', raw)
        self.assertNotIn(b'opaque-sub-bob', raw)

        with sqlite3.connect(self.outbox_db) as conn:
            rows = conn.execute(
                'SELECT external_identity_hash, principal_id FROM notification_principal_mappings'
            ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(len(identity_hash) == 64 for identity_hash, _ in rows))
        self.assertTrue(all(principal_id.startswith('pid_v1_') for _, principal_id in rows))

    def write_source(self, state):
        conn = sqlite3.connect(self.source_db)
        conn.execute('CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
        conn.execute('INSERT INTO ledger VALUES (1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                     (json.dumps(state, sort_keys=True, separators=(',', ':')),))
        conn.commit()
        conn.close()
        os.chmod(self.source_db, 0o600)

    def append_events(self, *events):
        state = self.outbox._read_source_state()
        state['events'].extend(events)
        self.write_source(state)

    def event(self, kind, task_id=TASK_A, seq=1, **extra):
        return {'seq': seq, 'kind': kind, 'time': self.now[0], 'task_id': task_id,
                'thread_id': THREAD_A, **extra}

    def test_source_task_existence_is_readonly_and_rejects_malformed_ledger(self):
        self.assertTrue(self.outbox.source_task_exists(TASK_A))
        self.assertFalse(self.outbox.source_task_exists('not-a-task-id'))
        self.assertFalse(self.outbox.source_task_exists(
            '00000000-0000-4000-8000-000000000099'))
        self.write_source({'tasks': [], 'events': []})
        with self.assertRaisesRegex(NotificationError, 'INVALID_EVENT_LEDGER'):
            self.outbox.source_task_exists(TASK_A)

    def subscribe(self, task_id=TASK_A, owner=OWNER, callback='https://notify.example.net/mcp-events/cb', event_name=None):
        if event_name is None:
            event_name = EVENT_NAMES['task_completed'][0]
        self.outbox.bind_task_origin(task_id, owner)
        result = self.outbox.subscribe(callback_url=callback, event_name=event_name,
            arguments={'task_id': task_id}, authenticated_principal=owner,
            signing_secret=SECRET, ttl_ms=60_000, cursor=0)
        if result['verification_required']:
            request = self.outbox.build_verification_request(result['id'], now=self.now[0])
            challenge = json.loads(request['body'])['challenge']
            self.outbox.complete_verification(result['id'], status_code=200,
                echoed_challenge=challenge, now=self.now[0])
        return result['id']

    def status(self, event_id, subscription_id):
        return self.outbox.delivery_status(event_id, subscription_id)

    def deliver(self, sender, *, outbox=None, now=None):
        target = outbox or self.outbox
        return target.deliver_one(sender, now=self.now[0] if now is None else now,
                                  access_check=lambda owner_hash, task_id: True)

    def queued_event_id(self, subscription_id):
        with closing(sqlite3.connect(self.outbox_db)) as conn:
            row = conn.execute('SELECT event_id FROM notification_outbox WHERE subscription_id=?',
                               (subscription_id,)).fetchone()
        return row[0]

    def test_normal_completion_builds_minimal_signed_event_and_acks_2xx(self):
        subscription = self.subscribe()
        self.append_events(self.event('task_completed', answer='private answer', prompt='private prompt'))
        self.assertEqual(self.outbox.sync_from_ledger()['active_subscriptions'], 1)
        sender = FakeSender({'status': 202})
        result = self.deliver(sender)
        self.assertEqual(result['status'], 'DELIVERED')
        request = sender.requests[0]
        payload = json.loads(request[2])
        self.assertEqual(payload['name'], 'codex.task.completed')
        self.assertEqual(payload['data'], {'task_id': TASK_A, 'state': 'COMPLETED', 'thread_id': THREAD_A})
        self.assertNotIn('answer', payload)
        self.assertNotIn('prompt', payload)
        self.assertEqual(request[1], ('8.8.8.8',))
        signature = request[3]['webhook-signature'].split(',', 1)[1]
        raw_key = validate_signing_secret(SECRET)
        signed = (request[3]['webhook-id'] + '.' + request[3]['webhook-timestamp'] + '.').encode() + request[2]
        expected = base64.b64encode(hmac.new(raw_key, signed, hashlib.sha256).digest()).decode()
        self.assertEqual(signature, expected)
        self.assertEqual(self.status(result['event_id'], subscription)['status'], 'DELIVERED')

    def test_callback_verification_is_required_and_single_use(self):
        self.outbox.bind_task_origin(TASK_A, OWNER)
        subscription = self.outbox.subscribe(callback_url='https://verify.example.net/cb',
            event_name=EVENT_NAMES['task_completed'][0], arguments={'task_id': TASK_A},
            authenticated_principal=OWNER, signing_secret=SECRET, ttl_ms=60_000, cursor=0)
        self.assertTrue(subscription['verification_required'])
        self.append_events(self.event('task_completed'))
        self.outbox.sync_from_ledger()
        self.assertEqual(self.deliver(FakeSender())['status'], 'EMPTY')
        request = self.outbox.build_verification_request(subscription['id'], now=self.now[0])
        self.assertEqual(request['headers']['webhook-id'], request['event_id'])
        self.assertIn('webhook-signature', request['headers'])
        challenge = json.loads(request['body'])['challenge']
        self.assertEqual(self.outbox.complete_verification(subscription['id'],
            status_code=200, echoed_challenge=challenge, now=self.now[0])['status'], 'VERIFIED')
        with self.assertRaisesRegex(NotificationError, 'VERIFICATION_CHALLENGE_UNAVAILABLE'):
            self.outbox.complete_verification(subscription['id'], status_code=200,
                echoed_challenge=challenge, now=self.now[0])
        self.assertEqual(self.deliver(FakeSender({'status': 204}))['status'], 'DELIVERED')

    def test_failed_approval_and_interrupted_tasks_use_fixed_event_kinds(self):
        names = [EVENT_NAMES[kind][0] for kind in ('task_failed', 'approval_waiting', 'task_cancelled')]
        event_kinds = ['task_failed', 'approval_waiting', 'task_cancelled']
        subscriptions = []
        for i, (kind, name) in enumerate(zip(event_kinds, names), start=1):
            task_id = f'00000000-0000-4000-8000-{i:012d}'
            self.write_source({'tasks': {task_id: {'id': task_id}}, 'events': []})
            subscriptions.append((task_id, self.subscribe(task_id=task_id, event_name=name)))
        source = self.outbox._read_source_state()
        events = [self.event(kind, task_id=task_id, seq=index) for index, (kind, (task_id, _)) in enumerate(zip(event_kinds, subscriptions), start=1)]
        source['events'].extend(events)
        self.write_source(source)
        self.outbox.sync_from_ledger()
        for (task_id, sub_id), expected in zip(subscriptions, names):
            sender = FakeSender({'status': 204})
            result = self.deliver(sender)
            self.assertEqual(result['status'], 'DELIVERED')
            self.assertEqual(json.loads(sender.requests[0][2])['name'], expected)
        self.assertNotIn('approval_token', json.dumps(self.outbox.summary()))

    def test_blocked_task_emits_allowlisted_action_required_payload_only(self):
        subscription = self.subscribe(event_name=EVENT_NAMES['task_blocked'][0])
        reason_codes = sorted(BLOCKED_REASON_CODES)
        self.append_events(*(self.event('task_blocked', seq=index, reason_code=reason_code,
            prompt='private prompt', answer='private answer', server_message='private server details',
            auth_token='secret-token', log='private log')
            for index, reason_code in enumerate(reason_codes, start=1)))

        self.outbox.sync_from_ledger()
        delivered_reasons = []
        for _ in reason_codes:
            sender = FakeSender({'status': 204})
            result = self.deliver(sender)
            self.assertEqual(result['status'], 'DELIVERED')
            payload = json.loads(sender.requests[0][2])
            self.assertEqual(payload['name'], 'codex.task.action_required')
            self.assertEqual(payload['data']['task_id'], TASK_A)
            self.assertEqual(payload['data']['state'], 'BLOCKED')
            self.assertEqual(payload['data']['thread_id'], THREAD_A)
            self.assertIs(payload['data']['action_required'], True)
            delivered_reasons.append(payload['data']['reason_code'])
            serialized = json.dumps(payload)
            for private_value in ('private prompt', 'private answer', 'private server details',
                                  'secret-token', 'private log', 'auth_token'):
                self.assertNotIn(private_value, serialized)
            self.assertEqual(self.status(result['event_id'], subscription)['status'], 'DELIVERED')
        self.assertCountEqual(delivered_reasons, reason_codes)

    def test_blocked_task_rejects_unknown_reason_without_advancing_cursor(self):
        subscription = self.subscribe(event_name=EVENT_NAMES['task_blocked'][0])
        self.append_events(self.event('task_blocked', reason_code='UNREVIEWED_PRIVATE_REASON',
                                      diagnostic='must not be forwarded'))

        with self.assertRaisesRegex(NotificationError, 'INVALID_SOURCE_EVENT'):
            self.outbox.sync_from_ledger()
        with closing(sqlite3.connect(self.outbox_db)) as conn:
            queued = conn.execute('SELECT COUNT(*) FROM notification_outbox WHERE subscription_id=?',
                                  (subscription,)).fetchone()[0]
            cursor = conn.execute('SELECT cursor_seq FROM notification_subscriptions WHERE subscription_id=?',
                                  (subscription,)).fetchone()[0]
        self.assertEqual(queued, 0)
        self.assertEqual(cursor, 0)
        self.assertEqual(len(BLOCKED_REASON_CODES), 5)

    def test_daemon_restart_recovers_expired_delivery_lease_and_reuses_event_id(self):
        sub_id = self.subscribe()
        self.append_events(self.event('task_completed'))
        self.outbox.sync_from_ledger()
        first_claim = self.outbox.claim_due(now=self.now[0])
        event_id = first_claim['event_id']
        self.assertEqual(first_claim['attempts'], 1)
        restarted = self.make_outbox()
        self.now[0] += 6
        sender = FakeSender({'status': 200})
        self.assertEqual(self.deliver(sender, outbox=restarted)['status'], 'DELIVERED')
        self.assertEqual(sender.requests[0][3]['webhook-id'], event_id)
        self.assertEqual(restarted.delivery_status(event_id, sub_id)['attempts'], 2)
        self.assertEqual(len(restarted._read_source_events()), 1)

    def test_transient_503_retries_with_exponential_backoff_without_task_reexecution(self):
        self.subscribe()
        self.append_events(self.event('task_completed'))
        self.outbox.sync_from_ledger()
        sender = FakeSender({'status': 503}, {'status': 204})
        first = self.deliver(sender)
        self.assertEqual(first['status'], 'RETRY')
        self.assertEqual(first['next_attempt_at'], self.now[0] + 1)
        event_id = sender.requests[0][3]['webhook-id']
        second = self.deliver(sender, now=self.now[0] + 1)
        self.assertEqual(second['status'], 'DELIVERED')
        self.assertEqual(sender.requests[1][3]['webhook-id'], event_id)
        self.assertNotEqual(sender.requests[0][3]['webhook-timestamp'],
                            sender.requests[1][3]['webhook-timestamp'])
        self.assertNotEqual(sender.requests[0][3]['webhook-signature'],
                            sender.requests[1][3]['webhook-signature'])
        self.assertEqual(len(self.outbox._read_source_state()['tasks']), 2)

    def test_delivery_fails_closed_without_owner_access_recheck(self):
        sub_id = self.subscribe()
        self.append_events(self.event('task_completed'))
        self.outbox.sync_from_ledger()
        sender = FakeSender({'status': 204})
        result = self.outbox.deliver_one(sender, now=self.now[0])
        self.assertEqual(result['status'], 'ACCESS_RECHECK_REQUIRED')
        self.assertEqual(sender.requests, [])
        self.assertEqual(self.outbox.delivery_status(result['event_id'], sub_id)['status'], 'RETRY')

    def test_backoff_grows_exponentially_and_stops_at_bound(self):
        self.subscribe()
        self.append_events(self.event('task_completed'))
        self.outbox.sync_from_ledger()
        sender = FakeSender({'status': 503}, {'status': 503}, {'status': 503}, {'status': 204})
        now = self.now[0]
        observed_delays = []
        event_ids = []
        for delay in (1, 2, 4):
            result = self.deliver(sender, now=now)
            self.assertEqual(result['status'], 'RETRY')
            event_ids.append(sender.requests[-1][3]['webhook-id'])
            observed_delays.append(result['next_attempt_at'] - now)
            now = result['next_attempt_at']
        delivered = self.deliver(sender, now=now)
        self.assertEqual(delivered['status'], 'DELIVERED')
        event_ids.append(sender.requests[-1][3]['webhook-id'])
        self.assertEqual(observed_delays, [1, 2, 4])
        self.assertEqual(len(set(event_ids)), 1)

    def test_unknown_http_outcome_is_reconciled_by_local_receipt_or_same_id_retry(self):
        sub_id = self.subscribe()
        self.append_events(self.event('task_completed'))
        self.outbox.sync_from_ledger()
        sender = FakeSender(TimeoutError('fixture timeout'))
        unknown = self.deliver(sender)
        self.assertEqual(unknown['status'], 'UNKNOWN')
        event_id = unknown['event_id']
        reconciled = self.outbox.reconcile_unknown(event_id, sub_id,
            remote_receipt=True, now=self.now[0] + 0.1)
        self.assertEqual(reconciled['status'], 'DELIVERED')
        self.assertEqual(len(sender.requests), 1)
        self.assertEqual(self.status(event_id, sub_id)['status'], 'DELIVERED')

    def test_unknown_without_remote_receipt_retries_only_the_same_event_id(self):
        sub_id = self.subscribe()
        self.append_events(self.event('task_completed'))
        self.outbox.sync_from_ledger()
        sender = FakeSender(TimeoutError('fixture timeout'), {'status': 202})
        first = self.deliver(sender)
        self.assertEqual(first['status'], 'UNKNOWN')
        reconciled = self.outbox.reconcile_unknown(first['event_id'], sub_id,
            now=first['next_attempt_at'])
        self.assertEqual(reconciled['action'], 'RETRY_SAME_EVENT_ID')
        self.assertFalse(reconciled['task_reexecution'])
        second = self.deliver(sender, now=first['next_attempt_at'])
        self.assertEqual(second['status'], 'DELIVERED')
        self.assertEqual(sender.requests[0][3]['webhook-id'], sender.requests[1][3]['webhook-id'])

    def test_duplicate_event_replay_is_idempotent_and_delivery_is_per_destination(self):
        first_sub = self.subscribe()
        second_sub = self.subscribe(callback='https://second.example.net/events')
        duplicate = self.event('task_completed')
        self.append_events(duplicate, duplicate)
        self.outbox.sync_from_ledger()
        self.outbox.sync_from_ledger()
        with closing(sqlite3.connect(self.outbox_db)) as conn:
            rows = conn.execute('SELECT event_id,subscription_id,status FROM notification_outbox ORDER BY subscription_id').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row[0] for row in rows}, {rows[0][0]})
        # 410 is final for one subscription; 2xx for the other is independent.
        sender = FakeSender({'status': 410}, {'status': 204})
        outcomes = [self.deliver(sender) for _ in range(2)]
        self.assertEqual({item['status'] for item in outcomes}, {'DELIVERED', 'PERMANENT_FAILURE'})
        event_id = rows[0][0]
        self.assertNotEqual(self.status(event_id, first_sub)['status'], self.status(event_id, second_sub)['status'])

    def test_two_tasks_complete_together_and_route_only_to_their_owners(self):
        sub_a = self.subscribe(task_id=TASK_A)
        sub_b = self.subscribe(task_id=TASK_B, owner=OWNER,
                               event_name=EVENT_NAMES['task_completed'][0],
                               callback='https://notify.example.net/second')
        self.append_events(self.event('task_completed', task_id=TASK_A, seq=1),
                           self.event('task_completed', task_id=TASK_B, seq=2))
        self.outbox.sync_from_ledger()
        self.assertEqual(self.outbox.summary()['delivery_states'], {'PENDING': 2})
        sender = FakeSender({'status': 204}, {'status': 204})
        results = [self.deliver(sender) for _ in range(2)]
        self.assertEqual([r['status'] for r in results], ['DELIVERED', 'DELIVERED'])
        self.assertEqual({r['event_id'] for r in results}, {self.queued_event_id(sub_a), self.queued_event_id(sub_b)})

    def test_outbox_capacity_failure_keeps_source_event_and_cursor_replayable(self):
        small = NotificationOutbox(self.root / 'small-outbox.sqlite3', source_db_path=self.source_db,
            secret_cipher=TestCipher(), owner_hash_key=b'outbox-test-owner-key-000000000000000000000000',
            clock=lambda: self.now[0], resolver=lambda host: ['8.8.8.8'],
            max_outbox_rows=1)
        for task_id, callback in ((TASK_A, 'https://a.example.net/cb'), (TASK_B, 'https://b.example.net/cb')):
            small.bind_task_origin(task_id, OWNER)
            subscription = small.subscribe(callback_url=callback,
                event_name=EVENT_NAMES['task_completed'][0], arguments={'task_id': task_id},
                authenticated_principal=OWNER, signing_secret=SECRET, ttl_ms=60_000, cursor=0)
            challenge = small.build_verification_request(subscription['id'], now=self.now[0])
            small.complete_verification(subscription['id'], status_code=200,
                echoed_challenge=json.loads(challenge['body'])['challenge'], now=self.now[0])
        self.append_events(self.event('task_completed', task_id=TASK_A, seq=1),
                           self.event('task_completed', task_id=TASK_B, seq=2))
        with self.assertRaisesRegex(NotificationError, 'OUTBOX_CAPACITY'):
            small.sync_from_ledger()
        self.assertEqual(len(self.outbox._read_source_events()), 2)
        self.assertEqual(small.summary()['delivery_states'], {})
        with closing(sqlite3.connect(small.path)) as conn:
            cursors = [row[0] for row in conn.execute('SELECT cursor_seq FROM notification_subscriptions ORDER BY subscription_id')]
        self.assertEqual(cursors, [0, 0])

    def test_permanent_statuses_do_not_retry_and_unsubscribe_cancels_pending(self):
        sub_id = self.subscribe()
        self.append_events(self.event('task_completed'))
        self.outbox.sync_from_ledger()
        sender = FakeSender({'status': 413})
        result = self.deliver(sender)
        self.assertEqual(result['status'], 'PERMANENT_FAILURE')
        self.assertEqual(self.status(result['event_id'], sub_id)['attempts'], 1)
        self.assertIsNone(self.outbox.claim_due(now=self.now[0] + 100))

        other = self.subscribe(callback='https://cancel.example.net/events')
        self.append_events(self.event('task_completed', seq=2))
        self.outbox.sync_from_ledger()
        canceled_id = self.queued_event_id(other)
        self.outbox.unsubscribe(callback_url='https://cancel.example.net/events',
            event_name=EVENT_NAMES['task_completed'][0], arguments={'task_id': TASK_A},
            authenticated_principal=OWNER)
        self.assertEqual(self.status(canceled_id, other)['status'], 'CANCELLED')

    def test_task_owner_binding_ssrf_checks_expiry_and_secret_redaction(self):
        self.outbox.bind_task_origin(TASK_A, OWNER)
        with self.assertRaisesRegex(NotificationError, 'TASK_ORIGIN_CONFLICT'):
            self.outbox.bind_task_origin(TASK_A, OTHER_OWNER)
        with self.assertRaisesRegex(NotificationError, 'TASK_NOT_OWNED'):
            self.outbox.subscribe(callback_url='https://notify.example.net/cb',
                event_name=EVENT_NAMES['task_completed'][0], arguments={'task_id': TASK_A},
                authenticated_principal=OTHER_OWNER, signing_secret=SECRET, ttl_ms=60_000)
        with self.assertRaisesRegex(NotificationError, 'UNSAFE_CALLBACK_ADDRESS'):
            validate_callback_url('https://internal.example.net/cb', lambda host: ['127.0.0.1'])
        with self.assertRaisesRegex(NotificationError, 'INVALID_CALLBACK_URL'):
            validate_callback_url('http://notify.example.net/cb', lambda host: ['8.8.8.8'])
        with self.assertRaisesRegex(NotificationError, 'INVALID_WEBHOOK_SECRET'):
            validate_signing_secret('whsec_not-base64')
        callback_url = 'https://notify.example.net/cb?token=route-secret-123'
        sub_id = self.outbox.subscribe(callback_url=callback_url,
            event_name=EVENT_NAMES['task_completed'][0], arguments={'task_id': TASK_A},
            authenticated_principal=OWNER, signing_secret=SECRET, ttl_ms=60_000)['id']
        raw_db = self.outbox_db.read_bytes()
        self.assertNotIn(SECRET.encode(), raw_db)
        self.assertNotIn(OWNER.encode(), raw_db)
        self.assertNotIn(callback_url.encode(), raw_db)
        self.assertNotIn(b'route-secret-123', raw_db)
        self.now[0] += 61
        self.assertEqual(self.outbox.summary()['active_subscriptions'], 0)
        self.assertTrue(sub_id.startswith('sub_'))

    def test_legacy_plaintext_callback_schema_fails_closed(self):
        legacy_path = self.root / 'legacy-outbox.sqlite3'
        with closing(sqlite3.connect(legacy_path)) as conn:
            with conn:
                conn.execute('CREATE TABLE notification_subscriptions '
                             '(subscription_id TEXT PRIMARY KEY, callback_url TEXT NOT NULL)')
        os.chmod(legacy_path, 0o600)
        with self.assertRaisesRegex(NotificationError, 'OUTBOX_SCHEMA_MIGRATION_REQUIRED'):
            NotificationOutbox(legacy_path, source_db_path=self.source_db,
                secret_cipher=TestCipher(), owner_hash_key=b'outbox-test-owner-key-000000000000000000000000',
                clock=lambda: self.now[0], resolver=lambda host: ['8.8.8.8'])


class PipelineNotificationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pipeline-notification-integration-')
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        workspace = self.root / 'workspace'
        workspace.mkdir(mode=0o700)
        self.policy = {'schema': 1, 'workspaces': {'fixture': str(workspace)},
            'models': ['offline-model'], 'max_threads': 1, 'max_tasks': 1,
            'concurrency': 1, 'timeout_seconds': 120}

        class EmptyClient:
            event_sink = None

            def request(self, method, params):
                raise AssertionError('offline integration must not contact Codex')

        self.pipeline = Pipeline(self.root, self.policy, EmptyClient(),
            instructions='offline fixture', policy_sha256='a' * 64, clock=lambda: 1000.0)
        self.outbox = NotificationOutbox(self.root / 'notification-outbox.sqlite3',
            source_db_path=self.pipeline.db, secret_cipher=TestCipher(),
            owner_hash_key=b'outbox-test-owner-key-000000000000000000000000', clock=lambda: 1000.0,
            resolver=lambda host: ['8.8.8.8'])

    def tearDown(self):
        self.temp.cleanup()

    def _verified_subscription(self, event_name):
        self.outbox.bind_task_origin(TASK_A, OWNER)
        subscription = self.outbox.subscribe(callback_url='https://notify.example.net/cb',
            event_name=event_name, arguments={'task_id': TASK_A},
            authenticated_principal=OWNER, signing_secret=SECRET, ttl_ms=60_000, cursor=0)
        challenge = self.outbox.build_verification_request(subscription['id'], now=1000.0)
        self.outbox.complete_verification(subscription['id'], status_code=200,
            echoed_challenge=json.loads(challenge['body'])['challenge'], now=1000.0)
        return subscription['id']

    def _seed_queued_task(self):
        def seed(state):
            state['threads'][THREAD_A] = {
                'id': THREAD_A, 'remote_id': 'remote-thread-fixture', 'workspace': 'fixture',
                'model': 'offline-model', 'active_task': None, 'archived': False, 'closing': False,
                'native_context_state': 'READY'}
            state['tasks'][TASK_A] = {
                'id': TASK_A, 'thread_id': THREAD_A, 'model': 'offline-model',
                'prompt': 'private synthetic prompt', 'prompt_sha256': 'f' * 64,
                'state': 'QUEUED', 'remote_turn': None, 'deadline': None, 'answer': None,
                'answer_truncated': False, 'stop_sent': False, 'queue_seq': 0,
                'pending_turn': None, 'pending_approval_turn': None}
        self.pipeline._mutate(seed)

    def test_actual_blocked_transition_routes_safe_action_required_event(self):
        self._seed_queued_task()
        self._verified_subscription(EVENT_NAMES['task_blocked'][0])

        view = self.pipeline._blocked_before_turn(TASK_A, 'RESUME_SCOPE_MISMATCH')
        self.assertEqual(view['state'], 'BLOCKED')
        self.assertEqual(view['reason_code'], 'RESUME_SCOPE_MISMATCH')
        self.assertFalse(view['execution_started'])
        self.outbox.sync_from_ledger()
        sender = FakeSender({'status': 204})
        result = self.outbox.deliver_one(sender, now=1000.0,
            access_check=lambda owner_hash, task_id: True)

        self.assertEqual(result['status'], 'DELIVERED')
        payload = json.loads(sender.requests[0][2])
        self.assertEqual(payload['name'], 'codex.task.action_required')
        self.assertEqual(payload['data']['state'], 'BLOCKED')
        self.assertIs(payload['data']['action_required'], True)
        self.assertEqual(payload['data']['reason_code'], 'RESUME_SCOPE_MISMATCH')
        self.assertNotIn('private synthetic prompt', json.dumps(payload))

    def test_dispatch_rpc_rejection_records_and_delivers_terminal_failure(self):
        self._seed_queued_task()
        self.pipeline.loaded.add(THREAD_A)
        self._verified_subscription(EVENT_NAMES['task_failed'][0])

        class SyntheticDispatchClient:
            event_sink = None
            def request(inner_self, method, params):
                if method == 'account/read':
                    return {'account': {'type': 'chatgpt'}}
                if method == 'model/list':
                    return {'data': [{'model': 'offline-model', 'hidden': False}], 'nextCursor': None}
                if method == 'turn/start':
                    raise RpcError(-32000, 'private server rejection detail')
                raise AssertionError('unexpected synthetic app-server request: ' + method)

        self.pipeline.client = SyntheticDispatchClient()
        result = self.pipeline._dispatch()
        self.assertEqual(result['status'], 'FAILED')
        state = self.pipeline._mutate(lambda value: value)
        self.assertEqual(state['tasks'][TASK_A]['state'], 'FAILED')
        failures = [event for event in state['events']
                    if event['kind'] == 'operation_failed' and event.get('operation') == 'dispatch']
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['task_id'], TASK_A)
        self.assertEqual(failures[0]['thread_id'], THREAD_A)
        self.assertNotIn('server rejection', json.dumps(failures[0]))

        self.outbox.sync_from_ledger()
        sender = FakeSender({'status': 204})
        delivered = self.outbox.deliver_one(sender, now=1000.0,
            access_check=lambda owner_hash, task_id: True)
        self.assertEqual(delivered['status'], 'DELIVERED')
        payload = json.loads(sender.requests[0][2])
        self.assertEqual(payload['name'], 'codex.task.failed')
        self.assertEqual(payload['data'], {
            'task_id': TASK_A, 'state': 'FAILED', 'thread_id': THREAD_A})
        self.assertNotIn('private synthetic prompt', json.dumps(payload))
        self.assertNotIn('private server rejection detail', json.dumps(payload))

    def test_existing_pipeline_tick_replays_event_to_separate_outbox(self):
        self.pipeline._mutate(lambda state: state['tasks'].__setitem__(TASK_A,
            {'id': TASK_A, 'thread_id': THREAD_A, 'state': 'COMPLETED'}))
        self.outbox.bind_task_origin(TASK_A, OWNER)
        subscription = self.outbox.subscribe(callback_url='https://notify.example.net/cb',
            event_name=EVENT_NAMES['task_completed'][0], arguments={'task_id': TASK_A},
            authenticated_principal=OWNER, signing_secret=SECRET, ttl_ms=60_000, cursor=0)
        challenge = self.outbox.build_verification_request(subscription['id'], now=1000.0)
        self.outbox.complete_verification(subscription['id'], status_code=204,
            echoed_challenge=json.loads(challenge['body'])['challenge'], now=1000.0)
        self.pipeline.notification_outbox = self.outbox
        self.pipeline._mutate(lambda state: self.pipeline._event(state,
            'task_completed', THREAD_A, TASK_A))
        self.assertEqual(self.pipeline.tick(dispatch=False)['status'], 'RECONCILED')
        self.assertEqual(self.outbox.summary()['delivery_states'], {'PENDING': 1})

    def test_outbox_failure_does_not_change_or_block_pipeline_state(self):
        class BrokenOutbox:
            def sync_from_ledger(self):
                raise OSError('fixture failure with private details')

        self.pipeline._mutate(lambda state: (
            state['tasks'].__setitem__(TASK_A, {'id': TASK_A, 'state': 'COMPLETED'}),
            self.pipeline._event(state, 'task_completed', THREAD_A, TASK_A)))
        before = self.pipeline._mutate(lambda state: (json.dumps(state['tasks'], sort_keys=True),
            json.dumps(state['events'], sort_keys=True)))
        self.pipeline.notification_outbox = BrokenOutbox()
        self.assertEqual(self.pipeline.tick(dispatch=False)['status'], 'RECONCILED')
        after = self.pipeline._mutate(lambda state: (json.dumps(state['tasks'], sort_keys=True),
            json.dumps(state['events'], sort_keys=True)))
        self.assertEqual(after, before)
        self.assertEqual(self.pipeline.notification_sync_error, 'OUTBOX_SYNC_FAILED')

    def test_existing_gateway_pump_retries_sync_after_task_is_terminal(self):
        self.pipeline._mutate(lambda state: state['tasks'].__setitem__(TASK_A,
            {'id': TASK_A, 'thread_id': THREAD_A, 'state': 'COMPLETED'}))
        self.outbox.bind_task_origin(TASK_A, OWNER)
        subscription = self.outbox.subscribe(callback_url='https://notify.example.net/cb',
            event_name=EVENT_NAMES['task_completed'][0], arguments={'task_id': TASK_A},
            authenticated_principal=OWNER, signing_secret=SECRET, ttl_ms=60_000, cursor=0)
        challenge = self.outbox.build_verification_request(subscription['id'], now=1000.0)
        self.outbox.complete_verification(subscription['id'], status_code=200,
            echoed_challenge=json.loads(challenge['body'])['challenge'], now=1000.0)
        self.pipeline.notification_outbox = self.outbox
        self.pipeline._mutate(lambda state: self.pipeline._event(state, 'task_completed', THREAD_A, TASK_A))
        gateway = HostGateway.__new__(HostGateway)
        gateway.root = self.root
        gateway.pipeline = self.pipeline
        gateway.failed = False
        gateway.transport = None
        gateway.protocol = None
        gateway.budget = None
        gateway.context_deadlines = {}
        self.assertFalse(gateway.held)
        gateway.pump()
        self.assertEqual(self.outbox.summary()['delivery_states'], {'PENDING': 1})

    def test_external_notification_worker_factory_is_not_called_by_default(self):
        factory_calls = []
        gateway = HostGateway.__new__(HostGateway)
        gateway.root = self.root
        gateway.pipeline = self.pipeline
        gateway.failed = False
        gateway.transport = None
        gateway.protocol = None
        gateway.budget = None
        gateway.context_deadlines = {}
        gateway.notification_worker_factory = lambda pipeline: factory_calls.append(pipeline)
        gateway.notification_worker = None
        gateway.notification_worker_started = False
        gateway.notification_worker_status = {'status': 'OFF'}

        result = gateway.pump()

        self.assertEqual(result['status'], 'IDLE_OR_HELD')
        self.assertEqual(gateway.notification_worker_status, {'status': 'DISABLED'})
        self.assertEqual(factory_calls, [])
        self.assertIsNone(gateway.notification_worker)

    def test_resident_gateway_bridges_terminal_events_to_background_worker(self):
        self.pipeline._mutate(lambda state: state['tasks'].__setitem__(TASK_A,
            {'id': TASK_A, 'thread_id': THREAD_A, 'state': 'COMPLETED'}))
        self.outbox.bind_task_origin(TASK_A, OWNER)
        subscription = self.outbox.subscribe(callback_url='https://notify.example.net/cb',
            event_name=EVENT_NAMES['task_completed'][0], arguments={'task_id': TASK_A},
            authenticated_principal=OWNER, signing_secret=SECRET, ttl_ms=60_000, cursor=0)
        challenge = self.outbox.build_verification_request(subscription['id'], now=1000.0)
        self.outbox.complete_verification(subscription['id'], status_code=204,
            echoed_challenge=json.loads(challenge['body'])['challenge'], now=1000.0)
        self.pipeline.notification_outbox = None
        emitted_at = time.monotonic()
        self.pipeline._mutate(lambda state: self.pipeline._event(state,
            'task_completed', THREAD_A, TASK_A))
        delivered = threading.Event()
        delivery_latency = []

        class SignalSender(FakeSender):
            def __call__(inner_self, *args):
                result = super().__call__(*args)
                delivery_latency.append(time.monotonic() - emitted_at)
                delivered.set()
                return result

        sender = SignalSender()
        worker = NotificationWorker(self.outbox, sender, access_check=lambda owner_hash, task_id: True,
                                    poll_seconds=0.05)
        gateway = HostGateway.__new__(HostGateway)
        gateway.root = self.root
        gateway.pipeline = self.pipeline
        gateway.failed = False
        gateway.transport = None
        gateway.protocol = None
        gateway.budget = None
        gateway.context_deadlines = {}
        gateway.notification_worker_factory = lambda pipeline: worker
        gateway.allow_external_notifications = True
        gateway.notification_worker = None
        gateway.notification_worker_started = False
        gateway.notification_worker_status = {'status': 'OFF'}

        gateway.pump()
        self.assertTrue(delivered.wait(1.0))
        self.resident_delivery_latency = delivery_latency[0]
        self.assertLess(delivery_latency[0], 3.0)
        self.assertEqual(gateway.notification_worker_status, {'status': 'RUNNING'})
        self.assertEqual(len(sender.requests), 1)
        event_id = json.loads(sender.requests[0][2])['eventId']
        deadline = time.monotonic() + 1.0
        while (self.outbox.delivery_status(event_id, subscription['id'])['status'] != 'DELIVERED'
               and time.monotonic() < deadline):
            time.sleep(0.005)
        self.assertEqual(self.outbox.delivery_status(event_id, subscription['id'])['status'], 'DELIVERED')
        self.assertEqual(gateway._stop_notification_worker(), {'status': 'STOPPED'})

    def test_invalid_worker_source_is_disabled_without_blocking_pipeline(self):
        class WrongSourceOutbox:
            source_path = self.root / 'other-ledger.sqlite3'

        class InvalidWorker:
            outbox = WrongSourceOutbox()
            started = False
            def start(inner_self):
                inner_self.started = True
            def stop(inner_self, timeout=2.0):
                return True

        worker = InvalidWorker()
        gateway = HostGateway.__new__(HostGateway)
        gateway.root = self.root
        gateway.pipeline = self.pipeline
        gateway.failed = False
        gateway.transport = None
        gateway.protocol = None
        gateway.budget = None
        gateway.context_deadlines = {}
        gateway.notification_worker_factory = lambda pipeline: worker
        gateway.allow_external_notifications = True
        gateway.notification_worker = None
        gateway.notification_worker_started = False
        gateway.notification_worker_status = {'status': 'OFF'}

        result = gateway.pump()
        self.assertEqual(result['status'], 'IDLE_OR_HELD')
        self.assertEqual(gateway.notification_worker_status,
                         {'status': 'UNAVAILABLE', 'code': 'WORKER_CONFIGURATION_INVALID'})
        self.assertFalse(worker.started)


if __name__ == '__main__':
    unittest.main()
