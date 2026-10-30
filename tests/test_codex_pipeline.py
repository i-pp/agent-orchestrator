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
        elif method == 'model/list': result = {'data': [MODEL], 'nextCursor': None}
        elif method in ('thread/start', 'thread/resume'):
            ident = params.get('threadId', 'remote-' + str(len(self.threads)))
            self.threads.setdefault(ident, {'id': ident, 'turns': [], 'status': {'type': 'idle'}})
            result = {'thread': self.threads[ident], 'model': params['model'], 'cwd': self.workspace,
                      'modelProvider': 'openai', 'approvalPolicy': 'on-request', 'approvalsReviewer': 'user', 'sandbox': {'type': self.scope}}
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
        self.assertEqual(result['models'], [{'model': 'offline-model', 'available': True}])
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
        self.complete(thread, answer='あ' * 2000)
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
                     {'thread_id': '0' * 36, 'prompt': 'あ' * 4096, 'request_key': 'b'}):
            with self.assertRaises(PipelineError): self.call('task_submit', **args)
        bad = copy.deepcopy(self.policy)
        bad['workspaces']['fixture'] = str(self.workspace / '..')
        with self.assertRaises(PipelineError): validate_policy(bad)

    def test_mcp_default_off_preserves_existing_11_schemas(self):
        old = tool_definitions(local_llm=True, local_models=True, chat_mailbox=True)
        enabled = tool_definitions(local_llm=True, local_models=True, chat_mailbox=True, codex_pipeline=True)
        self.assertEqual(len(old), 11)
        self.assertEqual(enabled[:11], old)
        self.assertEqual(len(enabled), 23)
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


if __name__ == '__main__': unittest.main()
