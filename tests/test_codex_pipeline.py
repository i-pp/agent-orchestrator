"""Real protocol boundary, fake peer, private ledger. No app-server/model starts."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from codex_pipeline import Pipeline, PipelineError, validate_policy, validate_arguments, TOOLS, build_pipeline
from codex_app_protocol import ProtocolClient
from mcp_adapter import Adapter, tool_definitions
from chat_mailbox import Mailbox, digest as mailbox_digest
from test_codex_app_protocol import INITIALIZE, MODEL


class Peer:
    def __init__(self, workspace):
        self.workspace, self.calls, self.threads = workspace, [], {}
        self.failure, self.account, self.scope = {}, 'chatgpt', 'readOnly'
        self.models = [copy.deepcopy(MODEL)]
        self.effort_echo = 'requested'
        self.client = ProtocolClient(self)
        self.client.initialize()

    def __call__(self, request, timeout_s):
        method, params = request['method'], request.get('params', {})
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'initialized': return None
        if method in self.failure:
            outcome = self.failure[method]
            if isinstance(outcome, Exception): raise outcome
            return {'id': request['id'], 'error': {'code': -32000, 'message': 'private refusal'}}
        if method == 'initialize': result = INITIALIZE
        elif method == 'account/read': result = {'requiresOpenaiAuth': True, 'account': {'type': self.account, 'email': 'never-export@example.invalid'}}
        elif method == 'model/list': result = {'data': copy.deepcopy(self.models), 'nextCursor': None}
        elif method in ('thread/start', 'thread/resume'):
            ident = params.get('threadId', 'remote-' + str(len(self.threads)))
            self.threads.setdefault(ident, {'id': ident, 'turns': [], 'status': {'type': 'idle'}})
            result = {'thread': self.threads[ident], 'model': params['model'], 'cwd': self.workspace,
                      'modelProvider': 'openai', 'approvalPolicy': 'on-request', 'approvalsReviewer': 'user', 'sandbox': {'type': self.scope}}
            if self.effort_echo == 'requested':
                result['reasoningEffort'] = params.get('config', {}).get('model_reasoning_effort')
            elif self.effort_echo != 'absent': result['reasoningEffort'] = self.effort_echo
        elif method == 'turn/start':
            thread = self.threads[params['threadId']]
            turn = {'id': 'turn-' + str(len(thread['turns'])), 'status': 'inProgress', 'items': []}
            thread['turns'].append(turn)
            thread['status'] = {'type': 'active', 'activeFlags': []}
            result = {'turn': copy.deepcopy(turn)}
        elif method == 'thread/read': result = {'thread': copy.deepcopy(self.threads[params['threadId']])}
        elif method == 'turn/steer': result = {'turnId': params['expectedTurnId']}
        else: result = {}
        return {'id': request['id'], 'result': result}

    def count(self, method): return sum(m == method for m, _ in self.calls)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        (PROJECT / '.runtime').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT / '.runtime', prefix='pipeline-test-')
        self.root = Path(self.temp.name)
        (self.root / 'logs').mkdir(mode=0o700)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.policy = {'schema': 1, 'workspaces': {'fixture': str(self.workspace)}, 'models': ['offline-model'],
                       'max_threads': 4, 'max_tasks': 16, 'concurrency': 1, 'timeout_seconds': 120}
        self.now = 1000
        self.peer = Peer(str(self.workspace))
        self.pipeline = self.new_pipeline()

    def tearDown(self): self.temp.cleanup()

    def new_pipeline(self):
        return Pipeline(self.root, self.policy, self.peer.client, instructions='Fixed common policy. No nested isolation.',
                        policy_sha256='a' * 64, clock=lambda: self.now)

    def call(self, name, **args): return self.pipeline.dispatch('codex_' + name, args)

    def start(self, key='start'):
        return self.call('thread_start', workspace='fixture', model='offline-model', request_key=key)['thread_id']

    def submit(self, thread, key='submit'):
        return self.call('task_submit', thread_id=thread, prompt='Synthetic public input', request_key=key)

    def complete(self, thread, status='completed', answer='Synthetic answer', notify=True):
        remote = self.pipeline._mutate(lambda s: s['threads'][thread]['remote_id'])
        turn = self.peer.threads[remote]['turns'][-1]
        turn.update(status=status, items=[{'type': 'agentMessage', 'id': 'answer', 'text': answer}])
        self.peer.threads[remote]['status'] = {'type': 'idle'}
        if notify: self.peer.client.receive({'method': 'turn/completed', 'params': {'threadId': remote, 'turn': copy.deepcopy(turn)}})

    def test_capabilities_no_generation_and_no_account_identity(self):
        result = self.call('capabilities')
        self.assertEqual(result['models'][0]['model'], 'offline-model')
        self.assertTrue(result['models'][0]['available'])
        self.assertNotIn('email', json.dumps(result))
        self.assertNotIn(str(self.workspace), json.dumps(result))
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_api_key_route_refused_before_thread_or_turn(self):
        self.peer.account = 'apiKey'
        with self.assertRaisesRegex(PipelineError, 'SUBSCRIPTION_AUTH_REQUIRED'): self.start()
        self.assertEqual(self.peer.count('thread/start'), 0)

    def test_invalid_workspace_and_model_never_start(self):
        for args in ({'workspace': '../../private', 'model': 'offline-model'}, {'workspace': 'fixture', 'model': 'other'}):
            with self.assertRaises(PipelineError): self.call('thread_start', request_key='bad', **args)
        self.assertEqual(self.peer.count('thread/start'), 0)

    def test_thread_and_turn_use_fixed_readonly_scope(self):
        thread = self.start()
        task = self.submit(thread)
        self.assertEqual(task['state'], 'RUNNING')
        start = next(p for m, p in self.peer.calls if m == 'thread/start')
        turn = next(p for m, p in self.peer.calls if m == 'turn/start')
        self.assertEqual(start['sandbox'], 'read-only')
        self.assertEqual(turn['sandboxPolicy'], {'type': 'readOnly'})
        self.assertEqual(start['approvalsReviewer'], 'user')
        self.assertEqual(turn['clientUserMessageId'], task['id'])

    def test_store_is_inert_queue_requires_tick(self):
        thread = self.start()
        task = self.call('chat_store', thread_id=thread, prompt='Later', request_key='store')
        self.assertEqual(task['state'], 'STORED')
        self.call('chat_queue', task_id=task['id'], request_key='queue')
        self.assertEqual(self.peer.count('turn/start'), 0)
        self.call('pipeline_tick')
        self.assertEqual(self.peer.count('turn/start'), 1)

    def test_fifo_and_busy_inputs_do_not_steer(self):
        thread = self.start()
        first = self.submit(thread, 'one')
        second = self.submit(thread, 'two')
        third = self.submit(thread, 'three')
        self.assertEqual(second['state'], 'QUEUED')
        self.assertEqual(self.peer.count('turn/start'), 1)
        self.assertEqual(self.peer.count('turn/steer'), 0)
        self.complete(thread)
        self.call('pipeline_tick')
        tasks = self.call('threads_list')['tasks']
        self.assertEqual(next(t for t in tasks if t['id'] == second['id'])['state'], 'RUNNING')
        self.assertEqual(next(t for t in tasks if t['id'] == third['id'])['state'], 'QUEUED')

    def test_stored_input_uses_queue_order_not_creation_order(self):
        thread = self.start()
        one = self.call('chat_store', thread_id=thread, prompt='one', request_key='one')
        two = self.call('chat_store', thread_id=thread, prompt='two', request_key='two')
        self.call('chat_queue', task_id=two['id'], request_key='q2')
        self.call('chat_queue', task_id=one['id'], request_key='q1')
        self.call('pipeline_tick')
        self.assertEqual(self.call('task_get', task_id=two['id'])['state'], 'RUNNING')
        self.assertEqual(self.call('task_get', task_id=one['id'])['state'], 'QUEUED')

    def test_explicit_steer_binds_expected_turn_and_cannot_change_model(self):
        thread = self.start()
        self.submit(thread)
        result = self.call('chat_steer', thread_id=thread, prompt='Additional public input', request_key='steer')
        self.assertEqual(result['status'], 'INPUT_ACCEPTED')
        args = next(p for m, p in self.peer.calls if m == 'turn/steer')
        self.assertEqual(args['expectedTurnId'], 'turn-0')
        with self.assertRaises(PipelineError): self.call('chat_steer', thread_id=thread, prompt='x', model='x', request_key='bad')

    def test_mismatched_steer_ack_is_unknown_and_not_resent(self):
        thread = self.start()
        self.submit(thread)
        original = self.peer.client.exchange
        def exchange(request, timeout_s):
            response = original(request, timeout_s)
            if request['method'] == 'turn/steer': response['result']['turnId'] = 'different-turn'
            return response
        self.peer.client.exchange = exchange
        args = {'thread_id': thread, 'prompt': 'more input', 'request_key': 'mismatched-steer'}
        self.assertEqual(self.call('chat_steer', **args)['status'], 'UNKNOWN')
        self.assertEqual(self.call('chat_steer', **args)['status'], 'UNKNOWN')
        self.assertEqual(self.peer.count('turn/steer'), 1)

    def test_idempotent_same_content_and_conflict(self):
        thread = self.start()
        self.assertEqual(self.start(), thread)
        first, again = self.submit(thread), self.submit(thread)
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(self.peer.count('thread/start'), 1)
        self.assertEqual(self.peer.count('turn/start'), 1)
        with self.assertRaisesRegex(PipelineError, 'REQUEST_KEY_CONFLICT'):
            self.call('task_submit', thread_id=thread, prompt='Changed', request_key='submit')

    def test_lost_ack_is_unknown_and_never_resubmitted(self):
        thread = self.start()
        self.peer.failure['turn/start'] = TimeoutError('private detail')
        task = self.submit(thread)
        self.assertEqual(task['state'], 'UNKNOWN')
        self.submit(thread)
        self.call('pipeline_tick')
        self.assertEqual(self.peer.count('turn/start'), 1)
        self.assertNotIn('private detail', json.dumps(self.call('threads_list')))

    def test_crash_reservation_recovery_never_redispatches(self):
        thread = self.start()
        task = self.call('chat_store', thread_id=thread, prompt='Stored', request_key='store')
        def crash(s):
            s['tasks'][task['id']]['state'] = 'DISPATCHING'
            s['tasks'][task['id']]['deadline'] = 1120
            s['threads'][thread]['active_task'] = task['id']
            s['ops']['crash'] = {'status': 'ISSUED', 'kind': 'dispatch', 'fingerprint': 'x', 'context': {'task_id': task['id']}}
        self.pipeline._mutate(crash)
        self.pipeline = self.new_pipeline()
        self.assertEqual(self.call('task_get', task_id=task['id'])['state'], 'UNKNOWN')
        self.call('pipeline_tick')
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_response_scope_mismatch_unknown_not_adopted(self):
        self.peer.scope = 'dangerFullAccess'
        result = self.call('thread_start', workspace='fixture', model='offline-model', request_key='scope')
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_terminal_notification_releases_and_replays_after_reconnect(self):
        thread = self.start()
        task = self.submit(thread)
        before = self.call('events')['latest_cursor']
        self.complete(thread)
        self.pipeline = self.new_pipeline()
        self.assertEqual(self.call('task_get', task_id=task['id'])['state'], 'COMPLETED')
        events = self.call('events', after=before)['events']
        self.assertIn('task_completed', [e['kind'] for e in events])
        self.assertEqual(events, self.call('events', after=before)['events'])

    def test_missed_notification_reconciles_exact_known_turn(self):
        thread = self.start()
        task = self.submit(thread)
        self.complete(thread, notify=False)
        result = self.call('task_get', task_id=task['id'])
        self.assertEqual((result['state'], result['answer']), ('COMPLETED', 'Synthetic answer'))

    def test_foreign_completion_cannot_finish_task(self):
        thread = self.start()
        task = self.submit(thread)
        self.peer.client.receive({'method': 'turn/completed', 'params': {'threadId': 'foreign', 'turn': {'id': 'turn-0', 'status': 'completed'}}})
        self.assertEqual(self.call('task_get', task_id=task['id'])['state'], 'RUNNING')

    def test_completion_before_start_ack_is_bound_only_to_matching_turn(self):
        thread = self.start()
        original = self.peer.client.exchange
        def exchange(request, timeout_s):
            result = original(request, timeout_s)
            if request['method'] == 'turn/start':
                turn = copy.deepcopy(result['result']['turn'])
                turn.update(status='completed', items=[{'type': 'agentMessage', 'text': 'early answer'}])
                self.peer.client.receive({'method': 'turn/completed', 'params': {'threadId': request['params']['threadId'], 'turn': turn}})
            return result
        self.peer.client.exchange = exchange
        task = self.submit(thread)
        self.assertEqual((task['state'], task['answer']), ('COMPLETED', 'early answer'))

    def test_pre_ack_events_without_turn_id_cannot_complete_or_request_approval(self):
        thread = self.start()
        original = self.peer.client.exchange
        def exchange(request, timeout_s):
            result = original(request, timeout_s)
            if request['method'] == 'turn/start':
                remote = request['params']['threadId']
                self.peer.client.receive({'method': 'turn/completed', 'params': {'threadId': remote, 'turn': {'status': 'completed'}}})
                self.peer.client.receive({'id': 13, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': remote}})
            return result
        self.peer.client.exchange = exchange
        self.assertEqual(self.submit(thread)['state'], 'RUNNING')
        self.assertNotIn('approval_waiting', [e['kind'] for e in self.call('events')['events']])

    def test_approval_event_never_replies_or_autoapproves(self):
        thread = self.start()
        task = self.submit(thread)
        remote = self.pipeline._mutate(lambda s: s['threads'][thread]['remote_id'])
        self.peer.client.receive({'id': 42, 'method': 'item/commandExecution/requestApproval', 'params': {
            'threadId': remote, 'turnId': 'turn-0', 'command': 'secret-command-do-not-log'}})
        self.assertEqual(self.call('task_get', task_id=task['id'])['state'], 'WAITING_APPROVAL')
        events = self.call('events')['events']
        self.assertFalse(next(e for e in events if e['kind'] == 'approval_waiting')['automatic_approval'])
        self.assertNotIn('secret-command', json.dumps(events))
        self.assertEqual(self.peer.count('turn/start'), 1)

    def test_approval_before_start_ack_is_retained_without_command_text(self):
        thread = self.start()
        original = self.peer.client.exchange
        def exchange(request, timeout_s):
            result = original(request, timeout_s)
            if request['method'] == 'turn/start':
                self.peer.client.receive({'id': 91, 'method': 'item/commandExecution/requestApproval', 'params': {
                    'threadId': request['params']['threadId'], 'turnId': result['result']['turn']['id'], 'command': 'secret-canary'}})
            return result
        self.peer.client.exchange = exchange
        task = self.submit(thread)
        self.assertEqual(task['state'], 'WAITING_APPROVAL')
        events = self.call('events')['events']
        self.assertIn('approval_waiting', [e['kind'] for e in events])
        self.assertNotIn('secret-canary', json.dumps(events))
        self.assertNotIn('secret-canary', self.pipeline._mutate(lambda s: json.dumps(s)))

    def test_stop_acceptance_separate_from_confirmation(self):
        thread = self.start()
        task = self.submit(thread)
        result = self.call('task_cancel', task_id=task['id'], request_key='stop')
        self.assertEqual(result, {'status': 'STOP_REQUESTED', 'stop_confirmed': False})
        self.call('task_cancel', task_id=task['id'], request_key='stop')
        self.assertEqual(self.peer.count('turn/interrupt'), 1)
        self.complete(thread, status='interrupted')
        self.assertEqual(self.call('task_get', task_id=task['id'])['state'], 'CANCELLED')

    def test_queued_cancel_never_interrupts(self):
        thread = self.start()
        self.submit(thread)
        queued = self.submit(thread, 'queued')
        result = self.call('task_cancel', task_id=queued['id'], request_key='cancel-queued')
        self.assertTrue(result['stop_confirmed'])
        self.assertEqual(self.peer.count('turn/interrupt'), 0)

    def test_deadline_interrupt_once_and_hold_until_terminal(self):
        thread = self.start()
        self.submit(thread)
        self.now += 121
        self.call('pipeline_tick')
        self.call('pipeline_tick')
        self.assertEqual(self.peer.count('turn/interrupt'), 1)
        self.assertEqual(self.peer.count('turn/start'), 1)

    def test_archive_preserves_history_and_refuses_unresolved(self):
        thread = self.start()
        task = self.submit(thread)
        with self.assertRaisesRegex(PipelineError, 'THREAD_BUSY'):
            self.call('thread_archive', thread_id=thread, request_key='close')
        self.complete(thread)
        self.assertTrue(self.call('thread_archive', thread_id=thread, request_key='close')['history_preserved'])
        self.assertEqual(self.call('task_get', task_id=task['id'])['state'], 'COMPLETED')
        with self.assertRaises(PipelineError): self.submit(thread, 'after-close')

    def test_archive_unknown_holds_thread_and_cannot_restart_work(self):
        thread = self.start()
        self.peer.failure['thread/archive'] = TimeoutError('lost ack')
        result = self.call('thread_archive', thread_id=thread, request_key='close')
        self.assertEqual(result['status'], 'UNKNOWN')
        with self.assertRaisesRegex(PipelineError, 'THREAD_NOT_READY'): self.submit(thread)
        self.call('thread_archive', thread_id=thread, request_key='close')
        self.assertEqual(self.peer.count('thread/archive'), 1)

    def test_archive_inflight_reservation_prevents_concurrent_submission(self):
        thread = self.start()
        original = self.peer.client.exchange
        def exchange(request, timeout_s):
            if request['method'] == 'thread/archive':
                with self.assertRaisesRegex(PipelineError, 'THREAD_NOT_READY'): self.submit(thread)
            return original(request, timeout_s)
        self.peer.client.exchange = exchange
        self.call('thread_archive', thread_id=thread, request_key='close')
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_events_poll_transport_without_new_model_request(self):
        class Transport:
            def __init__(self): self.calls = 0
            def poll(self, timeout_s): self.calls += 1
        transport = Transport()
        self.pipeline.transport = transport
        self.call('events')
        self.assertEqual(transport.calls, 1)
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_reconnect_resumes_owned_thread_with_fixed_scope(self):
        thread = self.start()
        self.pipeline = self.new_pipeline()
        self.submit(thread)
        self.assertEqual(self.peer.count('thread/resume'), 1)
        self.assertEqual(self.peer.count('turn/start'), 1)

    def test_concurrency_limit_holds_other_threads(self):
        one, two = self.start('one'), self.start('two')
        self.submit(one, 'a')
        task = self.submit(two, 'b')
        self.assertEqual(task['state'], 'QUEUED')
        self.assertEqual(self.peer.count('turn/start'), 1)

    def test_answer_bound_and_list_excludes_answer(self):
        thread = self.start()
        task = self.submit(thread)
        self.complete(thread, answer='\u3042' * 2000)
        result = self.call('task_get', task_id=task['id'])
        self.assertLessEqual(len(result['answer'].encode()), 4096)
        self.assertTrue(result['answer_truncated'])
        self.assertNotIn('answer', self.call('threads_list')['tasks'][0])

    def test_policy_mismatch_fails_without_new_execution(self):
        self.policy['max_tasks'] = 15
        with self.assertRaisesRegex(PipelineError, 'PIPELINE_POLICY_CHANGED'): self.new_pipeline()
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_parallel_same_key_reserves_single_input(self):
        thread = self.start()
        barrier = threading.Barrier(3)
        results, errors = [], []
        def worker():
            try:
                barrier.wait()
                results.append(self.call('chat_store', thread_id=thread, prompt='same', request_key='parallel'))
            except Exception as error: errors.append(error)
        workers = [threading.Thread(target=worker) for _ in range(2)]
        for worker in workers: worker.start()
        barrier.wait()
        for worker in workers: worker.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(self.call('threads_list')['tasks']), 1)
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_task_limit_rejects_before_extra_record(self):
        thread = self.start()
        for i in range(self.policy['max_tasks']):
            self.call('chat_store', thread_id=thread, prompt='bounded', request_key='task-' + str(i))
        with self.assertRaisesRegex(PipelineError, 'TASK_CAPACITY'):
            self.call('chat_store', thread_id=thread, prompt='overflow', request_key='overflow')
        self.assertEqual(len(self.call('threads_list')['tasks']), 16)

    def test_failed_interrupt_does_not_release_running_slot(self):
        thread = self.start()
        task = self.submit(thread)
        self.peer.failure['turn/interrupt'] = 'refused'
        result = self.call('task_cancel', task_id=task['id'], request_key='refused-stop')
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(self.call('task_get', task_id=task['id'])['state'], 'STOP_REQUESTED')
        self.call('pipeline_tick')
        self.assertEqual(self.peer.count('turn/start'), 1)

    def test_build_uses_injected_transport_and_reaps_on_non_subscription(self):
        policy_file = self.root / 'codex-pipeline-policy.json'
        policy_file.write_text(json.dumps(self.policy))
        policy_file.chmod(0o600)
        peer = self.peer
        class Transport:
            def __init__(self): self.started, self.closed = False, False
            def __call__(self, request, timeout_s): return peer(request, timeout_s)
            def bind_client(self, client): self.client = client
            def start(self): self.started = True
            def close(self): self.closed = True
        transport = Transport()
        peer.account = 'apiKey'
        bundle = type('Bundle', (), {'sha256': 'a' * 64})()
        with patch('common_policy.load_project_policy', return_value=bundle), patch('common_policy.developer_instructions', return_value='fixed'):
            with self.assertRaisesRegex(PipelineError, 'SUBSCRIPTION_AUTH_REQUIRED'):
                build_pipeline(self.root, PROJECT, transport_factory=lambda: transport)
        self.assertTrue(transport.started)
        self.assertTrue(transport.closed)
        self.assertEqual(peer.count('turn/start'), 0)

    def test_build_success_binds_real_protocol_to_mcp_and_completed_answer(self):
        policy_file = self.root / 'codex-pipeline-policy.json'
        policy_file.write_text(json.dumps(self.policy))
        policy_file.chmod(0o600)
        peer = self.peer
        class Transport:
            def __init__(self): self.started, self.closed = False, False
            def __call__(self, request, timeout_s): return peer(request, timeout_s)
            def bind_client(self, client): self.client = client
            def start(self): self.started = True
            def close(self): self.closed = True
            def poll(self, timeout_s): return 0
        transport = Transport()
        bundle = type('Bundle', (), {'sha256': 'a' * 64})()
        with patch('common_policy.load_project_policy', return_value=bundle), patch('common_policy.developer_instructions', return_value='fixed'):
            pipeline = build_pipeline(self.root, PROJECT, transport_factory=lambda: transport)
        adapter = Adapter(self.root, allow_codex_pipeline=True, pipeline=pipeline)
        self.addCleanup(adapter.audit.close)
        self.addCleanup(transport.close)
        def call(name, args):
            reply = adapter.call_tool('codex_' + name, args, 1)
            self.assertFalse(reply['isError'], reply)
            return reply['structuredContent']['data']
        thread = call('thread_start', {'workspace': 'fixture', 'model': 'offline-model', 'request_key': 'full-start'})['thread_id']
        task = call('task_submit', {'thread_id': thread, 'prompt': 'synthetic request', 'request_key': 'full-task'})
        remote = pipeline._mutate(lambda s: s['threads'][thread]['remote_id'])
        turn = peer.threads[remote]['turns'][0]
        turn.update(status='completed', items=[{'type': 'agentMessage', 'text': 'synthetic response'}])
        transport.client.receive({'method': 'turn/completed', 'params': {'threadId': remote, 'turn': turn}})
        self.assertEqual(call('task_get', {'task_id': task['id']})['answer'], 'synthetic response')
        self.assertEqual(peer.count('turn/start'), 1)

    def test_policy_scalar_types_are_strict(self):
        for field, value in [('schema', True), ('max_tasks', True), ('models', [{}])]:
            bad = copy.deepcopy(self.policy)
            bad[field] = value
            with self.assertRaises(PipelineError): validate_policy(bad)

    def test_private_db_and_path_traversal_limits(self):
        self.assertEqual(self.pipeline.db.stat().st_mode & 0o777, 0o600)
        for args in ({'thread_id': '../../etc', 'prompt': 'x', 'request_key': 'a'},
                     {'thread_id': '0' * 36, 'prompt': '\u3042' * 4096, 'request_key': 'b'}):
            with self.assertRaises(PipelineError): self.call('task_submit', **args)
        bad = copy.deepcopy(self.policy)
        bad['workspaces']['fixture'] = str(self.workspace / '..')
        with self.assertRaises(PipelineError): validate_policy(bad)

    def test_mcp_default_off_preserves_existing_11_schemas(self):
        old = tool_definitions(local_llm=True, local_models=True, chat_mailbox=True)
        enabled = tool_definitions(local_llm=True, local_models=True, chat_mailbox=True, codex_pipeline=True)
        self.assertEqual(len(old), 11)
        self.assertEqual(enabled[:11], old)
        self.assertEqual(len(enabled), 27)
        self.assertEqual({tool['name'] for tool in enabled[11:]}, set(TOOLS))
        with patch('mcp_adapter.subprocess.Popen', side_effect=AssertionError('no process')):
            adapter = Adapter(self.root)
            self.addCleanup(adapter.audit.close)
            self.assertFalse(any(t['name'] in TOOLS for t in adapter.tools))

    def test_mcp_audit_omits_input_and_answer(self):
        adapter = Adapter(self.root, allow_codex_pipeline=True, pipeline=self.pipeline)
        self.addCleanup(adapter.audit.close)
        thread = self.start()
        result = adapter.call_tool('codex_chat_store', {'thread_id': thread, 'prompt': 'private-test-canary', 'request_key': 'audit'}, 1)
        self.assertFalse(result['isError'])
        audit = adapter.audit_path.read_text()
        self.assertNotIn('private-test-canary', audit)
        self.assertNotIn(str(self.workspace), audit)

    def test_offline_roundtrip_mailbox_save_is_explicit_separate_step(self):
        thread = self.start()
        task = self.submit(thread)
        self.complete(thread)
        answer = self.call('task_get', task_id=task['id'])['answer']
        import uuid
        slot = {'task_id': str(uuid.uuid4()), 'request_id': str(uuid.uuid4()), 'request_sha256': mailbox_digest('Synthetic public input')}
        path = self.root / 'chat-mailbox-policy.json'
        path.write_text(json.dumps({'schema': 1, 'allowed': [slot]}))
        path.chmod(0o600)
        box = Mailbox(self.root)
        box.dispatch('chat_mailbox_register_request', {**slot, 'request': 'Synthetic public input'})
        box.dispatch('chat_mailbox_save_answer', {**slot, 'answer': answer})
        result = box.dispatch('chat_mailbox_get_answer', slot)
        self.assertEqual(result['answer'], answer)
        self.assertFalse(self.call('capabilities')['chat_wake'])


    def test_models_list_has_same_authenticated_metadata_prerequisite_as_capabilities(self):
        from local_principal import current_local_principal
        from types import SimpleNamespace
        adapter = Adapter(self.root,allow_codex_pipeline=True,pipeline=self.pipeline)
        self.addCleanup(adapter.audit.close)
        principal = current_local_principal()
        before = len(self.peer.calls)
        for tool in ['codex_capabilities','codex_models_list']:
            result = adapter.call_tool(tool,{},1,request_principal=principal)
            self.assertTrue(result['isError'])
            self.assertEqual(result['structuredContent']['data']['code'],'NOT_AUTHORIZED')
        self.assertEqual(len(self.peer.calls),before)
        # Once the exact existing prerequisite is satisfied, metadata remains
        # read-only and introduces no task/thread ownership import.
        adapter.events_enabled = True
        adapter.notification_runtime = SimpleNamespace(outbox=object())
        result = adapter.call_tool('codex_models_list',{},2,request_principal=principal)
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['data']['catalog']['status'],'observed')
        self.assertEqual(self.peer.count('turn/start'),0)

    def test_build_uses_shared_inspected_version_gate_for_0167_without_generation(self):
        path = self.root/'codex-pipeline-policy.json'
        path.write_text(json.dumps(self.policy));path.chmod(0o600)
        peer=self.peer
        class Transport:
            def __init__(self):self.closed=False
            def __call__(self,request,timeout_s):return peer(request,timeout_s)
            def bind_client(self,client):self.client=client
            def start(self):pass
            def close(self):self.closed=True
        transport=Transport()
        bundle=type('Bundle',(),{'sha256':'a'*64})()
        with patch('common_policy.load_project_policy',return_value=bundle), \
                patch('common_policy.developer_instructions',return_value='Fixed policy'), \
                patch.dict(INITIALIZE,{'userAgent':'codex_work_desktop/0.167.0 (Linux)'}):
            pipeline=build_pipeline(self.root,PROJECT,transport_factory=lambda:transport)
        self.addCleanup(transport.close)
        self.assertEqual(pipeline.capabilities()['catalog']['status'],'observed')
        self.assertEqual(self.peer.count('turn/start'),0)




class CatalogEffortTests(unittest.TestCase):
    setUp = PipelineTests.setUp
    tearDown = PipelineTests.tearDown
    new_pipeline = PipelineTests.new_pipeline
    call = PipelineTests.call
    start = PipelineTests.start
    submit = PipelineTests.submit
    complete = PipelineTests.complete

    def test_fresh_catalog_projects_selection_metadata_without_descriptions(self):
        extra = copy.deepcopy(MODEL)
        extra.update(id='other-model', model='other-model', displayName='Other', isDefault=False)
        self.peer.models.append(extra)
        result = self.call('capabilities')
        row = result['models'][0]
        self.assertEqual(row['availability'], 'observed')
        self.assertEqual(row['default_reasoning_effort'], 'medium')
        self.assertEqual(row['supported_reasoning_efforts'], ['medium', 'high'])
        self.assertEqual(row['display_name'], 'Fixture')
        self.assertFalse(result['model_execution_verified'])
        other = result['provider_catalog']['models'][1]
        self.assertFalse(other['operator_allowed'])
        self.assertFalse(other['selectable'])
        self.assertNotIn('description', json.dumps(result))
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_hidden_absent_and_discovery_failure_do_not_claim_availability(self):
        self.peer.models[0]['hidden'] = True
        hidden = self.call('capabilities')['models'][0]
        self.assertFalse(hidden['available'])
        self.assertEqual(hidden['availability'], 'unavailable')
        self.peer.models = []
        self.assertFalse(self.call('capabilities')['models'][0]['available'])
        self.peer.failure['model/list'] = TimeoutError('private authentication detail')
        result = self.call('capabilities')
        self.assertEqual(result['catalog']['status'], 'unknown')
        self.assertEqual(result['models'][0]['availability'], 'unknown')
        self.assertFalse(result['models'][0]['available'])
        self.assertNotIn('private authentication', json.dumps(result))

    def test_malformed_default_and_duplicate_models_fail_closed(self):
        for change in ('effort', 'default', 'duplicate', 'label', 'id'):
            with self.subTest(change=change):
                self.peer.models = [copy.deepcopy(MODEL)]
                row = self.peer.models[0]
                if change == 'effort': row['supportedReasoningEfforts'].append(copy.deepcopy(row['supportedReasoningEfforts'][0]))
                if change == 'default': row['defaultReasoningEffort'] = 'not-advertised'
                if change == 'duplicate': self.peer.models.append(copy.deepcopy(row))
                if change == 'label': row['displayName'] = 'bad\nlabel'
                if change == 'id': row['id'] = '../bad'
                result = self.call('capabilities')
                self.assertEqual(result['catalog']['status'], 'unknown')
                self.assertFalse(result['models'][0]['available'])
        self.assertEqual(self.peer.count('thread/start'), 0)

    def test_explicit_and_omitted_effort_are_persisted_and_transported(self):
        thread = self.call('thread_start', workspace='fixture', model='offline-model',
            reasoning_effort='high', request_key='start')['thread_id']
        task = self.call('task_submit', thread_id=thread, prompt='Synthetic public input', request_key='task')
        self.assertEqual(task['reasoning_effort'], 'medium')
        self.assertEqual(task['reasoning_effort_source'], 'provider_default')
        self.assertIsNone(task['requested_reasoning_effort'])
        start = next(p for m, p in self.peer.calls if m == 'thread/start')
        turn = next(p for m, p in self.peer.calls if m == 'turn/start')
        self.assertEqual(start['config'], {'model_reasoning_effort': 'high'})
        self.assertEqual(turn['effort'], 'medium')
        self.assertEqual(task['reasoning_effort_evidence']['turn_transport'], 'acknowledged')
        self.assertFalse(task['reasoning_effort_evidence']['actual_execution_verified'])
        self.assertEqual(task['reasoning_effort_evidence']['turn_provider_echo'], 'not_available')

    def test_explicit_unsupported_effort_rejected_before_generation(self):
        with self.assertRaisesRegex(PipelineError, 'REASONING_EFFORT_NOT_SUPPORTED'):
            self.call('thread_start', workspace='fixture', model='offline-model', reasoning_effort='max', request_key='bad')
        thread = self.start()
        result = self.call('task_submit', thread_id=thread, prompt='No turn', reasoning_effort='max', request_key='bad-task')
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertEqual(result['reason_code'], 'REASONING_EFFORT_NOT_SUPPORTED')
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_retry_precedes_discovery_and_retains_original_default_and_fingerprint(self):
        thread = self.start()
        task = self.call('chat_store', thread_id=thread, prompt='Fixed input', request_key='store')
        original = self.pipeline._mutate(lambda s: copy.deepcopy(s['ops']['store']))
        self.peer.models[0]['defaultReasoningEffort'] = 'high'
        duplicate = self.call('chat_store', thread_id=thread, prompt='Fixed input', request_key='store')
        self.assertEqual(duplicate['reasoning_effort'], 'medium')
        before = len(self.peer.calls)
        self.peer.failure['model/list'] = TimeoutError()
        self.assertEqual(self.start(), thread)
        self.assertEqual(self.call('chat_store', thread_id=thread, prompt='Fixed input', request_key='store')['id'], task['id'])
        self.assertEqual(len(self.peer.calls), before)
        self.assertEqual(self.pipeline._mutate(lambda s:s['ops']['store']), original)
        with self.assertRaisesRegex(PipelineError, 'REQUEST_KEY_CONFLICT'):
            self.call('chat_store', thread_id=thread, prompt='Fixed input', reasoning_effort='medium', request_key='store')

    def test_queued_effort_stays_fixed_when_provider_default_changes(self):
        thread = self.start()
        task = self.call('chat_store', thread_id=thread, prompt='Fixed', request_key='task')
        self.peer.models[0]['defaultReasoningEffort'] = 'high'
        self.call('chat_queue', task_id=task['id'], request_key='queue')
        self.call('pipeline_tick')
        turn = next(p for m, p in self.peer.calls if m == 'turn/start')
        self.assertEqual(turn['effort'], 'medium')

    def test_changed_supported_effort_blocks_queued_task(self):
        thread = self.start()
        task = self.call('chat_store', thread_id=thread, prompt='Fixed', request_key='task')
        self.call('chat_queue', task_id=task['id'], request_key='queue')
        self.peer.models[0].update(defaultReasoningEffort='high', supportedReasoningEfforts=[
            {'reasoningEffort':'high', 'description':'Fixture'}])
        self.call('pipeline_tick')
        current = self.call('task_get', task_id=task['id'])
        self.assertEqual(current['state'], 'BLOCKED')
        self.assertEqual(current['reason_code'], 'REASONING_EFFORT_NOT_SUPPORTED')
        self.assertEqual(current['reasoning_effort'], 'medium')
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_legacy_input_is_blocked_without_history_relabel_or_catalogue(self):
        thread = self.start()
        task = self.call('chat_store', thread_id=thread, prompt='Legacy', request_key='task')
        self.call('chat_queue', task_id=task['id'], request_key='queue')
        def legacy(s):
            row = s['tasks'][task['id']]
            for k in list(row):
                if k.startswith(('reasoning_effort', 'requested_reasoning_effort', 'selection_')): del row[k]
            return copy.deepcopy(s['ops'])
        ops = self.pipeline._mutate(legacy)
        before = len(self.peer.calls)
        self.call('pipeline_tick')
        current = self.call('task_get', task_id=task['id'])
        self.assertEqual(current['state'], 'BLOCKED')
        self.assertEqual(current['reason_code'], 'LEGACY_REASONING_EFFORT_MISSING')
        self.assertNotIn('reasoning_effort', current)
        self.assertEqual(self.pipeline._mutate(lambda s:s['ops']), ops)
        self.assertEqual(len(self.peer.calls), before)

    def test_unknown_legacy_execution_keeps_held_slot(self):
        thread = self.start()
        task = self.submit(thread)
        def legacy(s):
            s['tasks'][task['id']].pop('reasoning_effort')
            s['tasks'][task['id']]['state'] = 'UNKNOWN'
        self.pipeline._mutate(legacy)
        self.call('pipeline_tick')
        state = self.pipeline._mutate(lambda s:copy.deepcopy(s))
        self.assertEqual(state['threads'][thread]['active_task'], task['id'])
        self.assertNotIn('reasoning_effort', state['tasks'][task['id']])
        self.assertEqual(self.peer.count('turn/start'), 1)

    def test_thread_effort_echo_absence_is_not_execution_proof(self):
        self.peer.effort_echo = 'absent'
        thread = self.start()
        row = self.pipeline._mutate(lambda s:s['threads'][thread])
        self.assertEqual(row['context_reasoning_effort_evidence']['echo_status'], 'not_reported')
        self.assertFalse(row['context_reasoning_effort_evidence']['actual_execution_verified'])
        self.submit(thread)
        self.assertEqual(self.peer.count('turn/start'), 1)

    def test_effective_thread_echo_mismatch_is_unknown_and_never_runs(self):
        self.peer.effort_echo = 'high'
        result = self.call('thread_start', workspace='fixture', model='offline-model', request_key='bad-echo')
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(self.peer.count('turn/start'), 0)
        self.assertTrue(self.call('thread_start', workspace='fixture', model='offline-model', request_key='bad-echo')['duplicate'])
        self.assertEqual(self.peer.count('thread/start'), 1)

    def test_resume_transports_fixed_effort_and_checks_echo_before_turn(self):
        thread = self.start()
        task = self.call('chat_store', thread_id=thread, prompt='Resume', reasoning_effort='high', request_key='task')
        self.call('chat_queue', task_id=task['id'], request_key='queue')
        self.pipeline.loaded.clear()
        self.peer.effort_echo = 'medium'
        self.call('pipeline_tick')
        resume = next(p for m, p in self.peer.calls if m == 'thread/resume')
        self.assertEqual(resume['config'], {'model_reasoning_effort':'high'})
        self.assertEqual(self.call('task_get', task_id=task['id'])['reason_code'], 'RESUME_REASONING_EFFORT_MISMATCH')
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_provider_catalog_paging_is_bounded_and_unknown_never_selectable(self):
        self.peer.models = []
        for n in range(41):
            row = copy.deepcopy(MODEL)
            row.update(id='model-'+str(n), model='model-'+str(n), isDefault=False)
            self.peer.models.append(row)
        first = self.call('models_list', limit=20)
        second = self.call('models_list', limit=20, cursor=first['next_cursor'])
        third = self.call('models_list', limit=20, cursor=second['next_cursor'])
        self.assertEqual([len(x['models']) for x in [first, second, third]], [20,20,1])
        self.assertIsNone(third['next_cursor'])
        self.assertTrue(all(not row['selectable'] for row in first['models']))
        self.assertLess(len(json.dumps(first).encode()), 64*1024)
        self.peer.models[0]['displayName'] = 'Changed'
        with self.assertRaisesRegex(PipelineError, 'LIST_SNAPSHOT_CHANGED'):
            self.call('models_list', cursor=first['next_cursor'])

    def test_catalog_pagination_rejects_repeated_cursors_and_cross_page_duplicates(self):
        original = self.peer.client.request
        for duplicate in [True,False]:
            with self.subTest(duplicate=duplicate):
                count = 0
                def request(method, params):
                    nonlocal count
                    if method != 'model/list': return original(method, params)
                    count += 1
                    row = copy.deepcopy(MODEL)
                    if not duplicate: row.update(id='row-'+str(count), model='row-'+str(count))
                    return {'data':[row], 'nextCursor':'repeated'}
                with patch.object(self.peer.client, 'request', side_effect=request):
                    result = self.call('capabilities')
                self.assertEqual(result['catalog']['status'], 'unknown')
                self.assertLessEqual(count, 4)
        self.assertEqual(self.peer.count('turn/start'), 0)


class EffortStorageBoundaryTests(unittest.TestCase):
    setUp = PipelineTests.setUp
    tearDown = PipelineTests.tearDown
    new_pipeline = PipelineTests.new_pipeline
    call = PipelineTests.call
    start = PipelineTests.start

    def test_explicit_store_is_offline_and_keeps_intent_without_entitlement_claim(self):
        thread = self.start()
        before = len(self.peer.calls)
        self.peer.failure['account/read'] = TimeoutError('secret transport detail')
        task = self.call('chat_store', thread_id=thread, prompt='Keep intent', reasoning_effort='high', request_key='store')
        self.assertEqual(task['state'], 'STORED')
        self.assertEqual(task['reasoning_effort_source'], 'explicit')
        self.assertIsNone(task['selection_catalog_sha256'])
        self.call('chat_queue', task_id=task['id'], request_key='queue')
        self.assertEqual(len(self.peer.calls), before)
        self.call('pipeline_tick')
        current = self.call('task_get', task_id=task['id'])
        self.assertEqual(current['state'], 'BLOCKED')
        self.assertEqual(current['reason_code'], 'MODEL_CATALOG_UNKNOWN')
        self.assertEqual(current['reasoning_effort'], 'high')
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_new_omission_fails_closed_without_creating_partial_task(self):
        thread = self.start()
        self.peer.failure['model/list'] = TimeoutError('private')
        before = self.pipeline._mutate(lambda s: copy.deepcopy(s))
        with self.assertRaisesRegex(PipelineError, 'MODEL_CATALOG_UNKNOWN'):
            self.call('chat_store', thread_id=thread, prompt='No guessed default', request_key='task')
        self.assertEqual(self.pipeline._mutate(lambda s:s), before)
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_unusual_advertised_effort_is_preserved_not_mapped_to_native_enum(self):
        effort = 'custom strength/v2'
        self.peer.models[0]['supportedReasoningEfforts'].append({'reasoningEffort':effort, 'description':'Fixture'})
        self.peer.models[0]['defaultReasoningEffort'] = effort
        thread = self.start()
        result = self.call('task_submit', thread_id=thread, prompt='Exact raw effort', request_key='task')
        self.assertEqual(result['reasoning_effort'], effort)
        turn = next(p for m, p in self.peer.calls if m == 'turn/start')
        self.assertEqual(turn['effort'], effort)

    def test_changed_immutable_selection_is_blocked_before_discovery(self):
        thread = self.start()
        task = self.call('chat_store', thread_id=thread, prompt='Fixed', request_key='task')
        self.call('chat_queue', task_id=task['id'], request_key='queue')
        self.pipeline._mutate(lambda s:s['tasks'][task['id']].update(reasoning_effort='high'))
        before = len(self.peer.calls)
        self.call('pipeline_tick')
        current = self.call('task_get', task_id=task['id'])
        self.assertEqual(current['reason_code'], 'IMMUTABLE_MODEL_SELECTION_CHANGED')
        self.assertEqual(len(self.peer.calls), before)

    def test_legacy_stored_input_rejected_without_mutating_history(self):
        thread = self.start()
        task = self.call('chat_store', thread_id=thread, prompt='Legacy stored', request_key='task')
        self.pipeline._mutate(lambda s:s['tasks'][task['id']].pop('reasoning_effort'))
        before = self.pipeline._mutate(lambda s:copy.deepcopy(s))
        with self.assertRaisesRegex(PipelineError, 'LEGACY_REASONING_EFFORT_MISSING'):
            self.call('chat_queue', task_id=task['id'], request_key='queue')
        self.assertEqual(self.pipeline._mutate(lambda s:s), before)

    def test_null_effort_echo_is_distinct_from_matched_and_does_not_prove_execution(self):
        self.peer.effort_echo = None
        thread = self.start()
        self.pipeline.loaded.clear()
        task = self.call('task_submit',thread_id=thread,prompt='Null receipt',request_key='task')
        for row in [self.pipeline._mutate(lambda s:s['threads'][thread]),task]:
            evidence = row['context_reasoning_effort_evidence']
            self.assertEqual(evidence['echo_status'],'not_reported')
            self.assertIsNone(evidence['provider_echo'])
            self.assertFalse(evidence['actual_execution_verified'])
        self.assertEqual(task['state'],'RUNNING')

    def test_catalog_four_page_bound_with_no_fifth_read(self):
        original = self.peer.client.request
        pages = 0
        def request(method, params):
            nonlocal pages
            if method != 'model/list': return original(method,params)
            pages += 1
            return {'data': [], 'nextCursor':'page-'+str(pages)}
        with patch.object(self.peer.client, 'request', side_effect=request):
            result = self.call('capabilities')
        self.assertEqual(pages,4)
        self.assertEqual(result['catalog']['code'],'MODEL_CATALOG_BOUND')
        self.assertFalse(result['models'][0]['available'])

    def test_complete_large_projection_stays_under_wire_budget_with_explicit_truncation(self):
        self.peer.models = []
        for n in range(30):
            row = copy.deepcopy(MODEL)
            row.update(id=('model'+str(n)).ljust(160,'x'),model=('model'+str(n)).ljust(160,'x'),
                displayName='\u00e9'*80, isDefault=False)
            row['supportedReasoningEfforts'] = [
                {'reasoningEffort':(str(k)+'\u00e9'*31), 'description':'Fixture'} for k in range(16)]
            row['defaultReasoningEffort'] = row['supportedReasoningEfforts'][0]['reasoningEffort']
            self.peer.models.append(row)
        policy = copy.deepcopy(self.policy)
        policy['models'] = [row['model'] for row in self.peer.models[:8]]
        self.pipeline.policy = policy
        for tool in ['capabilities','models_list']:
            result = self.call(tool)
            self.assertLessEqual(len(json.dumps(result,ensure_ascii=True).encode()),20*1024)
            page = result.get('provider_catalog',result)
            self.assertTrue(page['truncated'])
            self.assertIsNotNone(page['next_cursor'])
            self.assertGreater(len(page['models']),0)


    def test_exact_duplicated_mcp_wire_envelope_fits_64k_for_escaped_metadata(self):
        from mcp_adapter import REQUEST_PROTOCOL, VERSION_META, CAPABILITIES_META
        from mcp_http_transport import MAX_HTTP_BODY
        self.peer.models = []
        for n in range(40):
            row = copy.deepcopy(MODEL)
            row.update(id=('model'+str(n)).ljust(160,'x'),model=('model'+str(n)).ljust(160,'x'),
                displayName='"\\'*80, isDefault=False)
            row['supportedReasoningEfforts'] = [
                {'reasoningEffort':str(k)+'"\\'*30, 'description':'Fixture'} for k in range(16)]
            row['defaultReasoningEffort'] = row['supportedReasoningEfforts'][0]['reasoningEffort']
            self.peer.models.append(row)
        self.pipeline.policy['models'] = [row['model'] for row in self.peer.models[:8]]
        adapter = Adapter(self.root,allow_codex_pipeline=True,pipeline=self.pipeline)
        self.addCleanup(adapter.audit.close)
        for tool in ['codex_capabilities','codex_models_list']:
            reply = adapter.handle({'jsonrpc':'2.0','id':'q'*128,'method':'tools/call','params':{
                'name':tool,'arguments':{},'_meta':{VERSION_META:REQUEST_PROTOCOL,CAPABILITIES_META:{}}}})
            self.assertNotIn('error',reply)
            self.assertFalse(reply['result']['isError'])
            wire = json.dumps(reply,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
            self.assertLess(len(wire),MAX_HTTP_BODY)
            body = reply['result']['structuredContent']
            self.assertEqual(json.loads(reply['result']['content'][0]['text']),body)
            self.assertLessEqual(len(json.dumps(body['data'],ensure_ascii=True).encode()),20*1024)
            page = body['data'].get('provider_catalog',body['data'])
            self.assertTrue(page['truncated'])
            self.assertTrue(page['models'])


    def test_large_legacy_listing_uses_existing_pagination_instead_of_oversized_wire(self):
        self.pipeline.policy['max_tasks'] = 32
        thread = self.start()
        for n in range(32):
            self.call('chat_store',thread_id=thread,prompt='Stored '+str(n),
                reasoning_effort='medium',request_key='large-'+str(n))
        result = self.call('threads_list')
        self.assertIsNotNone(result['next_cursor'])
        self.assertLess(len(json.dumps(result).encode()),16000)
        seen = set()
        while True:
            rows = result['threads']+result['tasks']
            self.assertFalse(seen.intersection(row['id'] for row in rows))
            seen.update(row['id'] for row in rows)
            if result['next_cursor'] is None: break
            result = self.call('threads_list',cursor=result['next_cursor'])
        self.assertEqual(len(seen),33)


class OwnerScopedListingBoundaryTests(unittest.TestCase):
    setUp = PipelineTests.setUp
    tearDown = PipelineTests.tearDown
    new_pipeline = PipelineTests.new_pipeline
    call = PipelineTests.call
    complete = PipelineTests.complete

    def owned_adapter(self, tasks, threads):
        from types import SimpleNamespace
        class Owned:
            def thread_owned_by(self, identifier, principal): return identifier in threads
            def task_owned_by(self, identifier, principal): return identifier in tasks
        adapter = Adapter(self.root, allow_codex_pipeline=True, pipeline=self.pipeline)
        adapter.events_enabled = True
        adapter.notification_runtime = SimpleNamespace(outbox=Owned())
        self.addCleanup(adapter.audit.close)
        return adapter

    def test_authenticated_owner_pages_fit_exact_wire_and_keep_all_owned_records(self):
        from local_principal import current_local_principal
        from mcp_adapter import REQUEST_PROTOCOL, VERSION_META, CAPABILITIES_META
        from mcp_http_transport import McpHttpEndpoint, MAX_HTTP_BODY
        model, effort = 'm' * 160, '"\\' * 32
        row = copy.deepcopy(MODEL)
        row.update(id=model, model=model, defaultReasoningEffort=effort,
                   supportedReasoningEfforts=[{'reasoningEffort': effort, 'description': ''}])
        self.peer.models = [row]
        self.pipeline.policy['models'] = [model]
        self.pipeline.policy['max_tasks'] = 32
        owned_thread = self.call('thread_start', workspace='fixture', model=model,
            reasoning_effort=effort, request_key='owned-thread')['thread_id']
        other_thread = self.call('thread_start', workspace='fixture', model=model,
            reasoning_effort=effort, request_key='other-thread')['thread_id']
        tasks = set()
        for n in range(32):
            thread = other_thread if n == 0 else owned_thread
            task = self.call('task_submit', thread_id=thread, prompt='Owned fixture ' + str(n),
                            reasoning_effort=effort, request_key='owned-task-' + str(n))
            self.complete(thread)
            if n: tasks.add(task['id'])
        adapter = self.owned_adapter(tasks, {owned_thread})
        endpoint = McpHttpEndpoint(adapter, expected_port=8765, allow_single_user_local=True,
                                   principal_provider=current_local_principal)
        cursor, seen, offsets = None, set(), []
        integer_limit = sys.get_int_max_str_digits()
        digits = min(4096, integer_limit - 1) if integer_limit else 4096
        request_ids = ['q' * 128, int('9' * digits), chr(0x1f642) * 128, '"\\' * 64]
        for page_index in range(40):
            arguments = {'limit': 20, **({'cursor': cursor} if cursor else {})}
            request = {'jsonrpc': '2.0', 'id': request_ids[page_index % len(request_ids)], 'method': 'tools/call', 'params': {
                'name': 'codex_threads_list', 'arguments': arguments,
                '_meta': {VERSION_META: REQUEST_PROTOCOL, CAPABILITIES_META: {}}}}
            raw = json.dumps(request, separators=(',', ':')).encode()
            headers = {'Host': '127.0.0.1:8765', 'Content-Type': 'application/json',
                'Accept': 'application/json, text/event-stream', 'Content-Length': str(len(raw)),
                'MCP-Protocol-Version': REQUEST_PROTOCOL, 'Mcp-Method': 'tools/call',
                'Mcp-Name': 'codex_threads_list'}
            status, _, wire = endpoint.handle('POST', '/mcp', headers, raw)
            self.assertEqual(status, 200, wire[:200])
            self.assertLess(len(wire), MAX_HTTP_BODY)
            result = json.loads(wire)['result']
            self.assertFalse(result['isError'])
            self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])
            page = result['structuredContent']['data']
            identifiers = {x['id'] for x in page['threads'] + page['tasks']}
            self.assertTrue(identifiers)
            self.assertFalse(seen & identifiers)
            seen.update(identifiers)
            self.assertNotIn(other_thread, identifiers)
            self.assertEqual(page['total_records'], len(tasks) + 1)
            cursor = page['next_cursor']
            if cursor is None: break
            offsets.append(int(cursor.split(':')[1]))
        else: self.fail('Owner cursor did not reach its terminal page')
        self.assertEqual(seen, tasks | {owned_thread})
        self.assertTrue(offsets)
        self.assertEqual(offsets, sorted(set(offsets)))

    def test_owner_scan_keeps_logical_record_bound_with_short_global_pages(self):
        from local_principal import current_local_principal
        from mcp_adapter import Invalid
        import uuid
        tasks = {str(uuid.UUID(int=n + 1)) for n in range(81)}
        adapter = self.owned_adapter(tasks, set())
        class Pages:
            def __init__(self, count): self.count, self.calls = count, 0
            def dispatch(self, name, arguments):
                offset = self.calls
                self.calls += 1
                return {'threads': [], 'tasks': [{'id': str(uuid.UUID(int=offset + 1))}],
                    'next_cursor': 'a' * 64 + ':' + str(self.calls) if self.calls < self.count else None}
        adapter.pipeline = pages = Pages(80)
        result = adapter._owner_scoped_thread_listing(current_local_principal(), {})
        self.assertEqual(result['total_records'], 80)
        self.assertEqual(pages.calls, 80)
        adapter.pipeline = pages = Pages(81)
        with self.assertRaisesRegex(Invalid, 'Owner listing exceeds'):
            adapter._owner_scoped_thread_listing(current_local_principal(), {})
        self.assertLessEqual(pages.calls, 80)

    def test_owner_scan_deadline_stops_before_another_global_read(self):
        from local_principal import current_local_principal
        from mcp_adapter import Invalid
        adapter = self.owned_adapter(set(), set())
        class Pages:
            def __init__(self): self.calls = 0
            def dispatch(self, name, arguments):
                self.calls += 1
                return {'threads': [], 'tasks': [], 'next_cursor': 'a' * 64 + ':1'}
        adapter.pipeline = pages = Pages()
        with patch('mcp_adapter.time.monotonic', side_effect=[0, 0, 6]):
            with self.assertRaisesRegex(Invalid, 'Owner listing scan deadline'):
                adapter._owner_scoped_thread_listing(current_local_principal(), {})
        self.assertEqual(pages.calls, 1)

    def test_owner_scan_rejects_repeated_cursor_without_looping(self):
        from local_principal import current_local_principal
        from mcp_adapter import Invalid
        adapter = self.owned_adapter(set(), set())
        class Pages:
            def __init__(self): self.calls = 0
            def dispatch(self, name, arguments):
                self.calls += 1
                return {'threads': [], 'tasks': [], 'next_cursor': 'a' * 64 + ':1'}
        adapter.pipeline = pages = Pages()
        with self.assertRaisesRegex(Invalid, 'Invalid pipeline listing cursor'):
            adapter._owner_scoped_thread_listing(current_local_principal(), {})
        self.assertEqual(pages.calls, 2)

    def test_oversized_single_owned_record_is_not_dropped_or_partially_returned(self):
        from local_principal import current_local_principal
        from mcp_adapter import Invalid
        import uuid
        task = str(uuid.uuid4())
        adapter = self.owned_adapter({task}, set())
        class Page:
            def dispatch(self, name, arguments):
                return {'threads': [], 'tasks': [{'id': task, 'legacy_receipt': 'x' * 30000}],
                        'next_cursor': None}
        adapter.pipeline = Page()
        with self.assertRaisesRegex(Invalid, 'Owner listing exceeds bounded record size'):
            adapter._owner_scoped_thread_listing(current_local_principal(), {})


if __name__ == '__main__':
    unittest.main()
