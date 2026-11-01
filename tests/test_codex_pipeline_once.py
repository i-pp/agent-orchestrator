"""One-shot host flow with fake native peer; real inference/process starts zero."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT / 'src'), str(PROJECT / 'scripts')]
import codex_pipeline_once as once
from test_codex_pipeline import Peer
from concurrency import lease


class FakeNative:
    def __init__(self, binary, workspace, mode='complete'):
        self.peer = Peer(str(workspace)); self.mode = mode
        self.inference_attempts = 0; self.cleanup_confirmed = False; self.closed = False
        self.deadline_fired = False
    def bind_client(self, client): self.client = client
    def start(self): pass
    def diagnostics(self): return {'reaped': self.closed}
    def close(self): self.closed = True; self.cleanup_confirmed = True
    def __call__(self, message, timeout_s):
        if message['method'] == 'turn/start': self.inference_attempts += 1
        response = self.peer(message, timeout_s)
        if message['method'] == 'turn/start' and self.mode == 'lost_ack': raise TimeoutError('canary-private')
        if message['method'] == 'model/list':
            response = copy.deepcopy(response)
            response['result']['data'][0].update(id=once.MODEL, model=once.MODEL)
        if message['method'] == 'turn/interrupt' and self.mode != 'stuck':
            self.peer.threads[message['params']['threadId']]['turns'][-1]['status'] = 'interrupted'
        return response
    def poll(self, timeout_s=0):
        remote, thread = next(iter(self.peer.threads.items()))
        turn = thread['turns'][-1]
        if turn['status'] in ('completed', 'interrupted'): return 0
        self.client.receive({'method': 'item/started', 'params': {'threadId': remote, 'turnId': turn['id'],
            'item': {'type': 'userMessage'}}})
        if self.mode == 'deadline':
            if self.deadline_fired: return 0
            self.deadline_fired = True
            signal.getsignal(signal.SIGALRM)(signal.SIGALRM, None)
        if self.mode in ('approval', 'stuck'):
            self.client.receive({'id': 'server-approval', 'method': 'item/commandExecution/requestApproval',
                'params': {'threadId': remote, 'turnId': turn['id']}})
        elif self.mode == 'tool':
            self.client.receive({'method': 'item/started', 'params': {'threadId': remote, 'turnId': turn['id'],
                'item': {'type': 'mcpToolCall', 'private': 'canary-private'}}})
        else:
            turn.update(status='completed', items=[{'type': 'agentMessage', 'text': once.EXPECTED if self.mode == 'complete' else 'canary-private'}])
            self.client.receive({'method': 'turn/completed', 'params': {'threadId': remote, 'turn': turn}})
        return 1


class OnceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        self.coordinator = self.root / 'coordinator'; self.coordinator.mkdir(mode=0o700)
        self.binary = self.root / 'codex'; self.binary.write_bytes(b'\x7fELFfixture')
        bundle = SimpleNamespace(sha256='a' * 64, metadata=lambda: {'sha256': 'a' * 64})
        for target, value in [('codex_pipeline_once.preflight', (self.binary, {'state': 'HOST_CONTEXT'})),
                              ('codex_pipeline_once.load_project_policy', bundle),
                              ('codex_pipeline_once.developer_instructions', 'fixed policy')]:
            p = patch(target, return_value=value); p.start(); self.addCleanup(p.stop)
        self.transports = []

    def run_once(self, mode='complete'):
        def factory(binary, workspace):
            transport = FakeNative(binary, workspace, mode); self.transports.append(transport); return transport
        return once.run(self.binary, 'a' * 64, self.workspace, self.coordinator,
                        self.root / 'once', transport_factory=factory)

    def test_request_task_response_and_private_record_end_to_end(self):
        result = self.run_once()
        self.assertEqual(result['status'], 'PASS'); self.assertEqual(result['answer'], once.EXPECTED)
        self.assertEqual(result['inference_attempts'], 1); self.assertEqual(result['automatic_retries'], 0)
        self.assertEqual(result['task_state'], 'COMPLETED'); self.assertEqual(result['held_tasks'], 0)
        self.assertTrue(result['cleanup_confirmed']); self.assertFalse(result['inherited_tools_restricted'])
        request = json.loads((self.root / 'once/request.json').read_text())
        self.assertEqual(request['prompt'], once.PROMPT)
        self.assertEqual(request['policy']['max_threads'], 1); self.assertEqual(request['policy']['max_tasks'], 1)
        self.assertEqual(request['policy']['timeout_seconds'], 120)
        self.assertEqual((self.root / 'once/result.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.transports[0].peer.count('turn/start'), 1)

    def test_existing_run_is_never_replayed(self):
        self.run_once()
        with self.assertRaises(FileExistsError): self.run_once()
        self.assertEqual(len(self.transports), 1)

    def test_shared_coordinator_budget_blocks_before_child(self):
        with lease(self.coordinator / 'runner.lock'):
            with self.assertRaises(RuntimeError): self.run_once()
        self.assertEqual(self.transports, [])

    def test_approval_is_not_answered_stop_requires_terminal(self):
        result = self.run_once('approval')
        self.assertNotEqual(result['status'], 'PASS'); self.assertTrue(result['stop_requested'])
        self.assertTrue(result['stop_confirmed']); self.assertEqual(result['task_state'], 'CANCELLED')
        self.assertEqual(self.transports[0].peer.count('turn/interrupt'), 1)
        self.assertEqual(result['stop_reason'], 'SERVER_REQUEST_OR_APPROVAL')

    def test_stop_ack_without_terminal_keeps_unknown_and_original_ledger(self):
        result = self.run_once('stuck')
        self.assertEqual(result['status'], 'UNKNOWN'); self.assertFalse(result['stop_confirmed'])
        self.assertEqual(result['held_tasks'], 1); self.assertTrue(result['cleanup_confirmed'])
        self.assertEqual(self.transports[0].peer.count('turn/interrupt'), 1)
        with self.assertRaises(FileExistsError): self.run_once()

    def test_lost_turn_ack_is_unknown_without_retry_or_guessed_cancel(self):
        result = self.run_once('lost_ack')
        self.assertEqual(result['status'], 'UNKNOWN'); self.assertEqual(result['inference_attempts'], 1)
        self.assertEqual(self.transports[0].peer.count('turn/start'), 1)
        self.assertEqual(self.transports[0].peer.count('turn/interrupt'), 0)
        self.assertTrue(result['cleanup_confirmed']); self.assertEqual(result['held_tasks'], 1)
        self.assertNotIn('canary', json.dumps(result))

    def test_tool_use_is_observed_then_cancelled_not_claimed_prevented(self):
        result = self.run_once('tool')
        self.assertEqual(result['stop_reason'], 'TOOL_USE_OBSERVED')
        self.assertNotEqual(result['status'], 'PASS'); self.assertTrue(result['stop_confirmed'])
        self.assertFalse(result['inherited_tools_restricted']); self.assertNotIn('canary', json.dumps(result))

    def test_arbitrary_answer_is_kept_private_not_echoed_as_success(self):
        result = self.run_once('mismatch')
        self.assertEqual(result['status'], 'RESULT_MISMATCH'); self.assertNotIn('answer', result)
        self.assertNotIn('canary', json.dumps(result))

    def test_native_launch_matches_successful_baseline_no_profile_flags(self):
        transport = once.OneShotTransport(self.binary, self.workspace)
        with patch.object(once.subprocess, 'Popen') as spawn:
            transport._spawn()
        self.assertEqual(spawn.call_args.args[0], [str(self.binary), 'app-server', '--listen', 'stdio://'])
        self.assertEqual(spawn.call_args.kwargs['cwd'], str(self.workspace))
        self.assertTrue(spawn.call_args.kwargs['start_new_session']); self.assertFalse(spawn.call_args.kwargs['shell'])

    def test_ledger_failure_after_start_still_closes_and_restores_handlers(self):
        from codex_pipeline import Pipeline
        original = Pipeline._mutate
        started = [False]; closed = [False]
        class Broken(FakeNative):
            def start(self): started[0] = True
            def __call__(self, message, timeout): raise TimeoutError('canary-private')
            def close(self): closed[0] = True; super().close()
        def mutate(instance, operation):
            if started[0]: raise OSError('canary-disk-error')
            return original(instance, operation)
        handlers = {s: signal.getsignal(s) for s in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM)}
        with patch.object(Pipeline, '_mutate', mutate):
            result = once.run(self.binary, 'a' * 64, self.workspace, self.coordinator,
                self.root / 'once', transport_factory=lambda b, w: Broken(b, w))
        self.assertTrue(closed[0]); self.assertEqual(result['status'], 'UNKNOWN')
        self.assertIsNone(result['held_tasks']); self.assertNotIn('canary', json.dumps(result))
        self.assertEqual({s: signal.getsignal(s) for s in handlers}, handlers)

    def test_final_receipt_failure_keeps_cleanup_and_signal_restoration(self):
        original = once._save
        def save(path, data):
            if path.name == 'result.json' and data.get('status') != 'STARTING':
                raise OSError('canary-disk-error')
            return original(path, data)
        handlers = {s: signal.getsignal(s) for s in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM)}
        with patch.object(once, '_save', side_effect=save): result = self.run_once()
        self.assertEqual(result['status'], 'UNKNOWN'); self.assertTrue(self.transports[0].closed)
        self.assertEqual({s: signal.getsignal(s) for s in handlers}, handlers)
        self.assertNotIn('canary', json.dumps(result))

    def test_deadline_requests_one_interrupt_and_does_not_retry(self):
        with patch.object(once.signal, 'alarm') as alarm: result = self.run_once('deadline')
        self.assertEqual(alarm.call_args_list[0].args, (120,))
        self.assertEqual(result['stop_reason'], 'DEADLINE')
        self.assertTrue(result['stop_requested']); self.assertTrue(result['stop_confirmed'])
        self.assertEqual(self.transports[0].peer.count('turn/start'), 1)
        self.assertEqual(self.transports[0].peer.count('turn/interrupt'), 1)
        self.assertNotEqual(result['status'], 'PASS'); self.assertTrue(result['cleanup_confirmed'])


if __name__ == '__main__': unittest.main()
