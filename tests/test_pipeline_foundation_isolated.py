"""Focused foundation tests with synthetic app-server behavior, not canonical fixtures.

These tests deliberately do not provide the missing historical Peer fixture. They
exercise selected pipeline/host contracts with bounded in-memory responses and no
Codex process, socket, network, or service startup.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from codex_app_protocol import ProtocolUnknown
from codex_host import CLEANUP, pipeline_held
from codex_pipeline import Pipeline
from test_codex_app_protocol import MODEL


THREAD = '10000000-0000-4000-8000-000000000091'
TASK = '00000000-0000-4000-8000-000000000091'


class PipelineFoundationIsolatedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pipeline-foundation-review-')
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir(mode=0o700)
        policy = {'schema': 1, 'workspaces': {'fixture': str(self.workspace)},
                  'models': ['offline-model'], 'max_threads': 1, 'max_tasks': 1,
                  'concurrency': 1, 'timeout_seconds': 60}
        self.client = None
        self.pipeline = Pipeline(self.root, policy, SyntheticUnusedClient(),
            instructions='synthetic test instructions', policy_sha256='a' * 64,
            clock=lambda: 1800000000.0)
        self._seed_queued_task()
        self.pipeline.loaded.add(THREAD)

    def tearDown(self):
        self.temp.cleanup()

    def _seed_queued_task(self):
        def seed(state):
            state['threads'][THREAD] = {
                'id': THREAD, 'remote_id': 'synthetic-remote-thread', 'workspace': 'fixture',
                'model': 'offline-model', 'active_task': None, 'archived': False, 'closing': False,
                'native_context_state': 'READY'}
            state['tasks'][TASK] = {
                'id': TASK, 'thread_id': THREAD, 'model': 'offline-model',
                **Pipeline._selection('offline-model', None, {'default_reasoning_effort':'medium',
                    'supported_reasoning_efforts':['medium', 'high']}),
                'reasoning_effort_evidence': {'turn_transport':'not_sent'},
                'prompt': 'private synthetic prompt', 'prompt_sha256': 'f' * 64,
                'state': 'QUEUED', 'remote_turn': None, 'deadline': None, 'answer': None,
                'answer_truncated': False, 'stop_sent': False, 'queue_seq': 0,
                'pending_turn': None, 'pending_approval_turn': None}
        self.pipeline._mutate(seed)

    def test_auth_required_becomes_a_blocked_user_action_with_fixed_reason(self):
        client = AuthRequiredSyntheticClient()
        self.pipeline.client = client

        result = self.pipeline._dispatch()

        self.assertEqual(result['state'], 'BLOCKED')
        self.assertEqual(result['reason_code'], 'SUBSCRIPTION_AUTH_REQUIRED')
        self.assertFalse(result['execution_started'])
        self.assertEqual(client.methods, ['account/read'])
        state = self.pipeline._mutate(lambda value: value)
        event = next(item for item in state['events'] if item['kind'] == 'task_blocked')
        self.assertEqual({key: event[key] for key in ('kind', 'task_id', 'thread_id', 'reason_code')}, {
            'kind': 'task_blocked', 'task_id': TASK, 'thread_id': THREAD,
            'reason_code': 'SUBSCRIPTION_AUTH_REQUIRED'})
        self.assertNotIn('private synthetic prompt', json.dumps(event))

    def test_unknown_dispatch_ack_is_not_reported_as_a_terminal_failure(self):
        client = UnknownDispatchSyntheticClient()
        self.pipeline.client = client

        result = self.pipeline._dispatch()

        self.assertEqual(result['status'], 'UNKNOWN')
        state = self.pipeline._mutate(lambda value: value)
        self.assertEqual(state['tasks'][TASK]['state'], 'UNKNOWN')
        self.assertFalse(any(item['kind'] == 'task_failed' for item in state['events']))
        self.assertFalse(any(item['kind'] == 'operation_failed' and item.get('operation') == 'dispatch'
                             for item in state['events']))
        self.assertTrue(any(item['kind'] == 'operation_unknown' for item in state['events']))
        self.assertNotIn('private synthetic prompt', json.dumps(state['events']))

    def test_host_pipeline_guard_fails_closed_on_unknown_cleanup_marker(self):
        self.assertFalse(pipeline_held(self.root))
        marker = self.root / CLEANUP
        marker.write_text(json.dumps({'status': 'UNKNOWN'}))
        os.chmod(marker, 0o600)
        self.assertTrue(pipeline_held(self.root))


class SyntheticUnusedClient:
    event_sink = None

    def request(self, method, params):
        raise AssertionError('unexpected synthetic app-server call: ' + method)


class AuthRequiredSyntheticClient:
    event_sink = None

    def __init__(self):
        self.methods = []

    def request(self, method, params):
        self.methods.append(method)
        if method == 'account/read':
            return {'account': {'type': 'api'}}
        raise AssertionError('auth blocker must stop before ' + method)


class UnknownDispatchSyntheticClient:
    event_sink = None

    def request(self, method, params):
        if method == 'account/read':
            return {'account': {'type': 'chatgpt'}}
        if method == 'model/list':
            return {'data': [MODEL], 'nextCursor': None}
        if method == 'turn/start':
            raise ProtocolUnknown('synthetic lost acknowledgement')
        raise AssertionError('unexpected synthetic app-server call: ' + method)


if __name__ == '__main__':
    unittest.main()
