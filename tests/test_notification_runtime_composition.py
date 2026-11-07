"""Adapter-to-daemon notification composition using disposable providers only."""
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
from types import SimpleNamespace
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))

from codex_host import HostGateway
from local_principal import current_local_principal
from mcp_adapter import (Adapter, CAPABILITIES_META, REQUEST_PROTOCOL,
                         VERSION_META)
from notification_outbox import BoundedResolver, EVENT_NAMES, NotificationError
from notification_runtime import (EVENTS_ENABLE_ENV, NotificationRuntime,
                                  build_systemd_notification_runtime,
                                  make_host_gateway_factory)


OWNER = current_local_principal()
OTHER = object()
TASK = '00000000-0000-4000-8000-000000000251'
THREAD = '10000000-0000-4000-8000-000000000251'
SECRET = 'whsec_' + base64.b64encode(b'test-only-signing-secret-32bytes!!').decode('ascii')
OWNER_KEY = b'test-only-owner-hash-key-0123456789abcdef'


class TestOnlyCipher:
    """Reversible test fixture; never use as a production secret provider."""

    def encrypt(self, value):
        return b'test-only:' + base64.urlsafe_b64encode(value)

    def decrypt(self, value):
        if not value.startswith(b'test-only:'):
            raise ValueError('invalid test ciphertext')
        return base64.urlsafe_b64decode(value[len(b'test-only:'):])


class InMemorySender:
    def __init__(self, resolver=None):
        self.resolver = resolver
        self.sent = threading.Event()
        self.requests = []

    def __call__(self, url, ips, body, headers):
        self.requests.append((url, tuple(ips), body, dict(headers)))
        self.sent.set()
        return {'status': 204}


class FixtureResolver:
    interruptible = True

    def resolve_with_timeout(self, _host, timeout_seconds):
        if not 0 < timeout_seconds <= 10:
            raise TimeoutError('test resolver deadline')
        return ['8.8.8.8']


class FixtureChallengeVerifier:
    def __init__(self, resolver):
        self.resolver = resolver

    def __call__(self, request):
        challenge = json.loads(request['body'].decode('utf-8'))['challenge']
        return {'status_code': 200, 'echoed_challenge': challenge}


class RuntimeCompositionTests(unittest.TestCase):
    def _runtime_root(self):
        runtime_parent = PROJECT / '.runtime'
        runtime_parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(runtime_parent, 0o700)
        temp = tempfile.TemporaryDirectory(prefix='notification-runtime-', dir=runtime_parent)
        root = Path(temp.name)
        os.chmod(root, 0o700)
        (root / 'logs').mkdir(mode=0o700)
        self._write_state(root, {'tasks': {}, 'events': []})
        return temp, root

    @staticmethod
    def _write_state(root, state):
        path = Path(root) / 'codex-pipeline.sqlite3'
        with closing(sqlite3.connect(path)) as conn:
            with conn:
                conn.execute('CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
                conn.execute('INSERT INTO ledger VALUES (1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                             (json.dumps(state, sort_keys=True, separators=(',', ':')),))
        os.chmod(path, 0o600)

    @staticmethod
    def _rpc(adapter, method, params=None, principal=OWNER):
        params = dict(params or {})
        params['_meta'] = {VERSION_META: REQUEST_PROTOCOL, CAPABILITIES_META: {}}
        return adapter.handle({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params},
                              principal=principal)

    def _components(self, root, allowed_tasks, sender):
        holder = {}
        resolver = BoundedResolver(FixtureResolver())
        sender.resolver = resolver

        def authorize(identity, task_id):
            return identity == holder.get('owner_identity') and task_id in allowed_tasks

        def owner_access(owner_hash, task_id):
            components = holder.get('runtime')
            return (components is not None and task_id in allowed_tasks
                    and owner_hash == components.outbox.owner_hash(
                        holder.get('owner_principal')))

        components = NotificationRuntime(
            root, secret_cipher=TestOnlyCipher(), owner_hash_key=OWNER_KEY,
            resolver=resolver, sender=sender,
            callback_verifier=FixtureChallengeVerifier(resolver), authorize_task=authorize,
            owner_access_check=owner_access, allow_external_callbacks=True,
            allow_external_notifications=True,
            allow_single_user_local=True, poll_seconds=0.05)
        holder['runtime'] = components
        holder['owner_principal'] = OWNER
        holder['owner_identity'] = OWNER.identity
        return components

    def test_missing_production_providers_keep_adapter_and_daemon_fail_closed(self):
        temp, root = self._runtime_root()
        adapter = None
        try:
            components = NotificationRuntime(
                root, allow_external_callbacks=True,
                allow_external_notifications=True,
                allow_single_user_local=True)
            self.assertFalse(components.enabled)
            self.assertEqual(components.disabled_reason, 'NOTIFICATION_PROVIDER_UNAVAILABLE')
            self.assertFalse(components.outbox_path.exists())

            unbounded_resolver = lambda _host: ['8.8.8.8']
            rejected = NotificationRuntime(
                root, secret_cipher=TestOnlyCipher(), owner_hash_key=OWNER_KEY,
                resolver=unbounded_resolver,
                sender=InMemorySender(unbounded_resolver),
                callback_verifier=lambda _request: {'status_code': 200},
                authorize_task=lambda _owner, _task: True,
                owner_access_check=lambda _owner, _task: True,
                allow_external_callbacks=True, allow_external_notifications=True,
                allow_single_user_local=True)
            self.assertFalse(rejected.enabled)
            self.assertEqual(rejected.disabled_reason, 'BOUNDED_RESOLVER_REQUIRED')

            adapter = Adapter(root, notification_runtime=components)
            result = self._rpc(adapter, 'server/discover', principal=None)['result']
            self.assertNotIn('events', result['capabilities'])
            self.assertEqual(self._rpc(adapter, 'events/list', principal=None)['error']['code'], -32601)
        finally:
            if adapter is not None:
                adapter.audit.close()
            temp.cleanup()

    def test_runtime_requires_explicit_single_user_local_policy(self):
        temp, root = self._runtime_root()
        resolver = BoundedResolver(FixtureResolver())
        sender = InMemorySender(resolver)
        runtime = NotificationRuntime(
            root, secret_cipher=TestOnlyCipher(), owner_hash_key=OWNER_KEY,
            resolver=resolver, sender=sender,
            callback_verifier=FixtureChallengeVerifier(resolver),
            authorize_task=lambda _owner, _task: True,
            owner_access_check=lambda _owner, _task: True,
            allow_external_callbacks=True, allow_external_notifications=True)
        try:
            self.assertFalse(runtime.enabled)
            self.assertIsNone(runtime.events_service)
            self.assertEqual(runtime.disabled_reason, 'SINGLE_USER_LOCAL_MODE_REQUIRED')
            self.assertFalse(runtime.outbox_path.exists())
        finally:
            temp.cleanup()

    def test_systemd_runtime_factory_is_off_by_default_and_never_loads_credentials_early(self):
        temp, root = self._runtime_root()
        loader_calls = []

        def loader(**_kwargs):
            loader_calls.append(True)
            raise AssertionError('provider loader must not run before both gates')

        try:
            off = build_systemd_notification_runtime(
                root, events_transport=True, environ={}, provider_loader=loader)
            self.assertFalse(off.enabled)
            self.assertEqual(off.disabled_reason, 'EXTERNAL_DELIVERY_DISABLED')
            self.assertFalse(off.outbox_path.exists())

            without_local_policy = build_systemd_notification_runtime(
                root, events_transport=True,
                environ={EVENTS_ENABLE_ENV: '1'}, provider_loader=loader)
            self.assertFalse(without_local_policy.enabled)
            self.assertEqual(without_local_policy.disabled_reason, 'EXTERNAL_DELIVERY_DISABLED')
            self.assertFalse(without_local_policy.outbox_path.exists())
            self.assertEqual(loader_calls, [])

            stdio = build_systemd_notification_runtime(
                root, events_transport=False,
                environ={EVENTS_ENABLE_ENV: '1'},
                provider_loader=loader)
            self.assertFalse(stdio.enabled)
            self.assertEqual(stdio.disabled_reason, 'EXTERNAL_DELIVERY_DISABLED')
            self.assertEqual(loader_calls, [])

            malformed = build_systemd_notification_runtime(
                root, events_transport=True,
                allow_single_user_local=True,
                environ={EVENTS_ENABLE_ENV: 'yes'},
                provider_loader=loader)
            self.assertFalse(malformed.enabled)
            self.assertEqual(malformed.disabled_reason, 'INVALID_NOTIFICATION_POLICY')
            self.assertEqual(loader_calls, [])
        finally:
            temp.cleanup()

    def test_systemd_runtime_factory_uses_only_injected_fake_providers_and_local_acl(self):
        temp, root = self._runtime_root()
        self._write_state(root, {'tasks': {TASK: {'id': TASK, 'state': 'QUEUED'}},
                                 'events': []})
        allowed_tasks = {TASK}
        resolver = BoundedResolver(FixtureResolver())
        sender = InMemorySender(resolver)
        providers = {
            'secret_cipher': TestOnlyCipher(),
            'owner_hash_key': OWNER_KEY,
            'resolver': resolver,
            'sender': sender,
            'callback_verifier': FixtureChallengeVerifier(resolver),
        }
        seen = []

        def loader(*, environ):
            seen.append(dict(environ))
            return providers

        runtime = build_systemd_notification_runtime(
            root, events_transport=True,
            environ={EVENTS_ENABLE_ENV: '1'},
            allow_single_user_local=True, provider_loader=loader)
        try:
            self.assertTrue(runtime.enabled, runtime.disabled_reason)
            self.assertEqual(len(seen), 1)
            self.assertTrue(runtime.events_service.bind_task_owner(TASK, OWNER))
            owner_hash = runtime.outbox.owner_hash(OWNER)
            self.assertTrue(runtime.events_service.worker_access_check(owner_hash, TASK))
            self.assertFalse(runtime.events_service.worker_access_check('0' * 64, TASK))
            self.assertFalse(runtime.events_service.worker_access_check(
                runtime.outbox.owner_hash('legacy-multi-user-principal'), TASK))
            self.assertFalse(runtime.events_service.worker_access_check(owner_hash, THREAD))
            with self.assertRaises(NotificationError):
                runtime.events_service._authorize(OTHER, TASK)
            with self.assertRaises(NotificationError):
                runtime.events_service._authorize(OWNER, THREAD)
        finally:
            temp.cleanup()

    def test_missing_systemd_credential_keeps_runtime_disabled_without_outbox(self):
        temp, root = self._runtime_root()

        def missing_credentials(**_kwargs):
            raise NotificationError('SYSTEMD_CREDENTIAL_UNAVAILABLE')

        try:
            runtime = build_systemd_notification_runtime(
                root, events_transport=True,
                environ={EVENTS_ENABLE_ENV: '1'},
                allow_single_user_local=True, provider_loader=missing_credentials)
            self.assertFalse(runtime.enabled)
            self.assertEqual(runtime.disabled_reason, 'SYSTEMD_CREDENTIAL_UNAVAILABLE')
            self.assertFalse(runtime.outbox_path.exists())
        finally:
            temp.cleanup()

    def test_adapter_and_daemon_share_outbox_with_test_only_providers(self):
        temp, root = self._runtime_root()
        self._write_state(root, {'tasks': {}, 'threads': {THREAD: {'id': THREAD}}, 'events': []})
        allowed_tasks = set()
        sender = InMemorySender()
        adapter_components = self._components(root, allowed_tasks, sender)
        daemon_components = self._components(root, allowed_tasks, sender)
        self.assertTrue(adapter_components.enabled)
        self.assertTrue(daemon_components.enabled)
        self.assertEqual(adapter_components.outbox_path, daemon_components.outbox_path)
        self.assertEqual(adapter_components.source_path, daemon_components.source_path)

        class SubmitPipeline:
            def dispatch(inner_self, name, _arguments):
                self.assertEqual(name, 'codex_task_submit')
                allowed_tasks.add(TASK)
                self._write_state(root, {'tasks': {TASK: {'id': TASK}}, 'events': []})
                return {'id': TASK, 'state': 'QUEUED'}

        adapter = Adapter(root, allow_codex_pipeline=True,
                          pipeline=SubmitPipeline(),
                          notification_runtime=adapter_components)
        self.assertTrue(adapter_components.events_service.bind_thread_owner(THREAD, OWNER))
        gateway = None
        try:
            submitted = self._rpc(adapter, 'tools/call', {
                'name': 'codex_task_submit',
                'arguments': {'thread_id': THREAD, 'prompt': 'mock task',
                              'request_key': 'runtime-test-001'}})
            data = submitted['result']['structuredContent']['data']
            self.assertEqual(data['notification_binding'], 'BOUND')
            self.assertTrue(adapter_components.outbox.task_owned_by(TASK, OWNER))

            event_name = EVENT_NAMES['task_completed'][0]
            subscribed = self._rpc(adapter, 'events/subscribe', {
                'name': event_name, 'arguments': {'task_id': TASK},
                'delivery': {'mode': 'webhook',
                             'url': 'https://events.example.test/callback',
                             'secret': SECRET},
            })
            self.assertIn('result', subscribed)
            self.assertEqual(adapter_components.outbox.summary()['active_subscriptions'], 1)

            event_recorded_at = time.monotonic()
            self._write_state(root, {
                'tasks': {TASK: {'id': TASK, 'state': 'COMPLETED'}},
                'events': [{'seq': 1, 'kind': 'task_completed', 'time': time.time(),
                            'task_id': TASK, 'thread_id': THREAD}],
            })

            class DaemonPipeline:
                def __init__(inner_self):
                    inner_self.db = root / 'codex-pipeline.sqlite3'
                    inner_self.notification_outbox = None
                def sync_notifications(inner_self):
                    return inner_self.notification_outbox.sync_from_ledger()
                def _dispatch(inner_self):
                    return {'status': 'IDLE'}

            # Exercise the same HostGateway worker seam used by Daemon.pipeline_factory.
            gateway = object.__new__(HostGateway)
            gateway.pipeline = DaemonPipeline()
            gateway.root = root
            gateway.failed = False
            gateway.release_idle = lambda: None
            gateway.notification_runtime = daemon_components
            gateway.notification_worker_factory = daemon_components.worker_factory
            gateway.allow_external_notifications = daemon_components.enabled
            gateway.notification_worker = None
            gateway.notification_worker_started = False
            gateway.notification_worker_status = {'status': 'OFF'}
            self.assertEqual(gateway._start_notification_worker()['status'], 'RUNNING')
            gateway.pipeline.sync_notifications()
            self.assertTrue(sender.sent.wait(1.0))
            self.last_mock_delivery_seconds = time.monotonic() - event_recorded_at
            self.assertLess(self.last_mock_delivery_seconds, 1.0)
            payload = json.loads(sender.requests[0][2].decode('utf-8'))
            self.assertEqual(payload['name'], event_name)
            self.assertEqual(payload['data']['task_id'], TASK)
            self.assertEqual(sender.requests[0][0], 'https://events.example.test/callback')
        finally:
            if gateway is not None:
                gateway._stop_notification_worker()
            adapter.audit.close()
            temp.cleanup()

    def test_http_pipeline_acl_and_listing_exclude_unbound_legacy_resources(self):
        temp, root = self._runtime_root()
        task_a = TASK
        thread_a = THREAD
        task_b = '00000000-0000-4000-8000-000000000252'
        thread_b = '10000000-0000-4000-8000-000000000252'
        self._write_state(root, {
            'tasks': {task_a: {'id': task_a, 'thread_id': thread_a, 'state': 'COMPLETED'},
                      task_b: {'id': task_b, 'thread_id': thread_b, 'state': 'COMPLETED'}},
            'threads': {thread_a: {'id': thread_a}, thread_b: {'id': thread_b}},
            'events': [
                {'seq': 1, 'kind': 'task_completed', 'time': 1.0,
                 'task_id': task_a, 'thread_id': thread_a, 'prompt': 'private'},
                {'seq': 2, 'kind': 'task_completed', 'time': 2.0,
                 'task_id': task_b, 'thread_id': thread_b},
                {'seq': 3, 'kind': 'task_blocked', 'time': 3.0,
                 'task_id': task_a, 'thread_id': thread_a,
                 'reason_code': 'MODEL_NOT_AVAILABLE_OR_ALLOWED'},
            ]})
        resolver = BoundedResolver(FixtureResolver())
        sender = InMemorySender(resolver)
        providers = {'secret_cipher': TestOnlyCipher(), 'owner_hash_key': OWNER_KEY,
                     'resolver': resolver, 'sender': sender,
                     'callback_verifier': FixtureChallengeVerifier(resolver)}
        runtime = build_systemd_notification_runtime(
            root, events_transport=True, allow_single_user_local=True,
            environ={EVENTS_ENABLE_ENV: '1'}, provider_loader=lambda **_kwargs: providers)
        self.assertTrue(runtime.enabled, runtime.disabled_reason)
        runtime.outbox.bind_thread_origin(thread_a, OWNER)
        runtime.outbox.bind_task_origin(task_a, OWNER)

        class Pipeline:
            calls = []

            def dispatch(self, name, arguments):
                self.calls.append((name, dict(arguments)))
                if name == 'codex_threads_list':
                    return {'threads': [{'id': thread_a}, {'id': thread_b}],
                            'tasks': [{'id': task_a, 'thread_id': thread_a},
                                      {'id': task_b, 'thread_id': thread_b}],
                            'next_cursor': None, 'total_records': 4,
                            'capacity': {'held_tasks': 2}}
                if name == 'codex_task_get':
                    return {'id': arguments['task_id'], 'state': 'COMPLETED'}
                raise AssertionError('unexpected pipeline tool')

        pipeline = Pipeline()
        adapter = Adapter(root, allow_codex_pipeline=True, pipeline=pipeline,
                          notification_runtime=runtime)
        try:
            listed = self._rpc(adapter, 'tools/call', {
                'name': 'codex_threads_list', 'arguments': {}}, OWNER)
            listed_data = listed['result']['structuredContent']['data']
            self.assertEqual({item['id'] for item in listed_data['threads']}, {thread_a})
            self.assertEqual({item['id'] for item in listed_data['tasks']}, {task_a})
            self.assertNotIn('capacity', listed_data)
            self.assertEqual(listed_data['total_records'], 2)

            calls_before = len(pipeline.calls)
            denied = self._rpc(adapter, 'tools/call', {
                'name': 'codex_task_get', 'arguments': {'task_id': task_b}}, OWNER)
            self.assertEqual(denied['result']['structuredContent']['data']['code'], 'NOT_AUTHORIZED')
            self.assertTrue(denied['result']['isError'])
            self.assertEqual(len(pipeline.calls), calls_before)

            allowed = self._rpc(adapter, 'tools/call', {
                'name': 'codex_task_get', 'arguments': {'task_id': task_a}}, OWNER)
            self.assertEqual(allowed['result']['structuredContent']['data']['id'], task_a)
            events_a = self._rpc(adapter, 'tools/call', {
                'name': 'codex_events', 'arguments': {'after': 0, 'limit': 20}}, OWNER)
            event_data = events_a['result']['structuredContent']['data']
            self.assertEqual([item['seq'] for item in event_data['events']], [1, 2])
            self.assertEqual(event_data['latest_cursor'], 2)
            self.assertTrue(all(item.get('task_id') == task_a for item in event_data['events']))
            self.assertTrue(all('prompt' not in item for item in event_data['events']))
        finally:
            adapter.audit.close()
            temp.cleanup()

    def test_daemon_pipeline_factory_receives_the_shared_runtime(self):
        temp, root = self._runtime_root()
        sender = InMemorySender()
        components = self._components(root, set(), sender)
        captured = {}

        def fake_host_gateway(*args, **kwargs):
            captured['args'] = args
            captured['kwargs'] = kwargs
            return object()

        try:
            factory = make_host_gateway_factory(lambda supplied_root: (
                self.assertEqual(Path(supplied_root).absolute(), root.absolute()) or components))
            with patch('codex_host.HostGateway', fake_host_gateway):
                factory(root, '/unused/codex', PROJECT, lambda: False)
            self.assertIs(captured['kwargs']['notification_runtime'], components)
        finally:
            temp.cleanup()

    def test_host_gateway_constructor_uses_runtime_and_worker_factory(self):
        temp, root = self._runtime_root()
        sender = InMemorySender()
        components = self._components(root, set(), sender)
        (root / 'codex-pipeline-policy.json').write_text('{}', encoding='utf-8')
        os.chmod(root / 'codex-pipeline-policy.json', 0o600)
        bundle = SimpleNamespace(sha256='a' * 64)

        class BarePipeline:
            def __init__(self, runtime, *_args, **_kwargs):
                self.db = Path(runtime) / 'codex-pipeline.sqlite3'

        gateway = None
        try:
            with patch('codex_host.HostGateway.require_host'), \
                    patch('codex_host.executor_snapshot', return_value={
                        'common_policy_sha256': bundle.sha256}), \
                    patch('codex_host.load_project_policy', return_value=bundle), \
                    patch('codex_host.developer_instructions', return_value='mock instructions'), \
                    patch('codex_host.Pipeline', BarePipeline):
                gateway = HostGateway(root, '/unused/codex', PROJECT, lambda: False,
                                      notification_runtime=components)
            self.assertTrue(gateway.allow_external_notifications)
            self.assertIs(gateway.notification_worker_factory.__self__, components)
            self.assertIs(gateway.notification_worker_factory.__func__,
                          components.worker_factory.__func__)
            self.assertEqual(gateway._start_notification_worker()['status'], 'RUNNING')
            self.assertEqual(gateway._stop_notification_worker()['status'], 'STOPPED')
        finally:
            if gateway is not None:
                gateway._stop_notification_worker()
            temp.cleanup()


if __name__ == '__main__':
    unittest.main()
