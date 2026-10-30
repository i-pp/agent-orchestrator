"""Daemon gateway contracts with fake executor; no service/socket/model starts."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import signal
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT / 'src'), str(PROJECT / 'scripts')]
from codex_host import HostGateway, HostTransport, DaemonPipelineProxy, executor_snapshot, pipeline_held
from codex_pipeline import PipelineError
from codex_app_protocol import StdioTransport
from concurrency import lease
from concurrency import guard
from runner import Runner
from daemon import Daemon
from test_codex_pipeline import Peer
from test_codex_app_protocol import PipeProcess


class FakeOwned:
    def __init__(self, peer, safe=True): self.peer, self.cleanup_confirmed, self.closed = peer, safe, False
    def bind_client(self, client): self.client = client
    def start(self): pass
    def __call__(self, request, timeout_s): return self.peer(request, timeout_s)
    def poll(self, timeout_s=0): return 0
    def close(self): self.closed = True


class GatewayTests(unittest.TestCase):
    def setUp(self):
        (PROJECT / '.runtime').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT / '.runtime', prefix='gateway-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'logs').mkdir(mode=0o700)
        self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        self.binary = self.root / 'codex'; self.binary.write_bytes(b'\x7fELFfixture'); self.binary.chmod(0o700)
        self.policy = {'schema': 1, 'workspaces': {'fixture': str(self.workspace)}, 'models': ['offline-model'],
                       'max_threads': 4, 'max_tasks': 16, 'concurrency': 1, 'timeout_seconds': 120}
        for name, value in [('codex-pipeline-policy.json', self.policy), ('codex-pipeline-executor.json',
                {'schema': 1, 'binary_sha256': hashlib.sha256(self.binary.read_bytes()).hexdigest()})]:
            path = self.root / name; path.write_text(json.dumps(value)); path.chmod(0o600)
        self.bundle = type('Bundle', (), {'sha256': 'a' * 64})()
        for target, value in [('codex_host.load_project_policy', self.bundle), ('codex_host.developer_instructions', 'fixed policy')]:
            p = patch(target, return_value=value); p.start(); self.addCleanup(p.stop)
        p = patch.object(HostGateway, 'require_host'); p.start(); self.addCleanup(p.stop)
        self.busy, self.transports = False, []
        self.peer = Peer(str(self.workspace))
        self.gateway = HostGateway(self.root, str(self.binary), PROJECT, lambda: self.busy, transport_factory=self.factory)
        self.addCleanup(self.gateway.close)

    def factory(self, binary):
        self.assertEqual(binary, str(self.binary))
        transport = FakeOwned(self.peer); self.transports.append(transport); return transport

    def call(self, name, **args): return self.gateway.dispatch('codex_' + name, args)
    def start(self): return self.call('thread_start', workspace='fixture', model='offline-model', request_key='start')['thread_id']
    def submit(self, thread, key='task'): return self.call('task_submit', thread_id=thread, prompt='public fixture', request_key=key)
    def complete(self, thread):
        remote = self.gateway.pipeline._mutate(lambda s: s['threads'][thread]['remote_id'])
        self.peer.threads[remote]['turns'][-1].update(status='completed', items=[])

    def test_snapshot_is_metadata_only_and_single_turn_is_required(self):
        result = executor_snapshot(self.root, self.binary)
        self.assertEqual(set(result), {'executor_policy_sha256', 'pipeline_policy_sha256', 'common_policy_sha256', 'binary_sha256'})
        self.assertEqual(self.transports, [])
        self.policy['concurrency'] = 2
        (self.root / 'codex-pipeline-policy.json').write_text(json.dumps(self.policy))
        with self.assertRaisesRegex(PipelineError, 'HOST_PIPELINE_REQUIRES_SINGLE_TURN'): executor_snapshot(self.root, self.binary)

    def test_real_snapshot_contract_reaches_service_and_login_pins(self):
        from poc_services import Services
        from poc_login import snapshot, LoginBlocked
        service = Services.__new__(Services); service.root = self.root; service.codex = self.binary
        hashes = service.pipeline_snapshot()
        self.assertEqual(hashes, self.gateway.snapshot)
        state = {'schema': 1, 'local_llm_enabled': True, 'local_models_enabled': True,
                 'chat_mailbox_enabled': True, 'mailbox_grants_enabled': True, 'codex_pipeline_enabled': True,
                 'codex_pipeline_snapshot': hashes, 'credential_file_reference': '/fixture/credential-reference',
                 'codex_file_reference': str(self.binary)}
        service.read_state = lambda: state
        grant = Mock(); grant.sha256.return_value = 'b' * 64
        with patch('poc_login.Mailbox.policy_snapshot', return_value=(None, None, 'c' * 64)), patch('poc_login.load_runtime_grant', return_value=grant):
            self.assertEqual(snapshot(service)[1]['codex_pipeline_snapshot'], hashes)
            state['codex_pipeline_snapshot'] = {**hashes, 'common_policy_sha256': 'd' * 64}
            with self.assertRaisesRegex(LoginBlocked, 'PIPELINE_EXECUTOR_PIN_MISMATCH'): snapshot(service)
        self.assertEqual(self.transports, [])

    def test_metadata_session_closes_and_releases_runner_budget(self):
        self.start()
        self.assertTrue(self.transports[0].closed)
        self.assertFalse(self.gateway.held)
        with lease(self.root / 'runner.lock', shared=True): pass

    def test_unconfirmed_cleanup_never_reports_ready_or_releases_budget(self):
        self.gateway.transport_factory = lambda _: FakeOwned(self.peer, safe=False)
        with self.assertRaisesRegex(PipelineError, 'EXECUTOR_CLEANUP_UNCONFIRMED'): self.start()
        self.assertTrue(self.gateway.held)
        with self.assertRaises(RuntimeError): lease(self.root / 'runner.lock', shared=True)
        with self.assertRaisesRegex(PipelineError, 'EXECUTOR_CLEANUP_UNCONFIRMED'): self.call('threads_list')
        with self.assertRaises(RuntimeError): lease(self.root / 'runner.lock', shared=True)

    def test_busy_ordinary_worker_accepts_queue_but_never_starts_turn(self):
        thread = self.start(); count = len(self.transports); self.busy = True
        task = self.submit(thread)
        self.assertEqual(task['state'], 'QUEUED')
        self.assertEqual(len(self.transports), count)
        self.assertEqual(self.peer.count('turn/start'), 0)
        self.busy = False; self.gateway.pump()
        self.assertEqual(self.peer.count('turn/start'), 1)

    def test_changed_auth_blocks_queued_task_once_without_automatic_relaunch(self):
        thread = self.start(); self.peer.account = 'apiKey'
        task = self.submit(thread)
        self.assertEqual(task['state'], 'BLOCKED')
        self.assertFalse(task['execution_started'])
        count = len(self.transports)
        self.gateway.pump(); self.gateway.pump()
        self.assertEqual(len(self.transports), count)
        self.assertEqual(self.peer.count('turn/start'), 0)
        result = self.call('task_cancel', task_id=task['id'], request_key='cancel-blocked')
        self.assertTrue(result['stop_confirmed'])

    def test_actual_runner_lease_excludes_pipeline_turn(self):
        thread = self.start()
        with lease(self.root / 'runner.lock', shared=True):
            task = self.submit(thread)
            self.assertEqual(task['state'], 'QUEUED')
            self.assertEqual(self.peer.count('turn/start'), 0)
        self.gateway.pump()
        with self.assertRaises(RuntimeError): lease(self.root / 'runner.lock', shared=True)
        self.complete(thread); self.gateway.pump()
        with lease(self.root / 'runner.lock', shared=True): pass

    def test_paused_ordinary_budget_does_not_dispatch_next_pipeline_turn(self):
        thread = self.start(); self.submit(thread); queued = self.submit(thread, 'queued')
        self.busy = True; self.complete(thread); self.gateway.pump()
        self.assertEqual(self.peer.count('turn/start'), 1)
        self.assertEqual(self.call('task_get', task_id=queued['id'])['state'], 'QUEUED')

    def test_lost_ack_holds_budget_and_cannot_restart_executor(self):
        thread = self.start(); self.peer.failure['turn/start'] = TimeoutError()
        task = self.submit(thread); before = len(self.transports)
        self.assertEqual(task['state'], 'UNKNOWN')
        self.assertTrue(self.gateway.held)
        self.gateway.pump(); self.submit(thread)
        self.assertEqual(len(self.transports), before)
        self.assertEqual(self.peer.count('turn/start'), 1)
        with self.assertRaises(RuntimeError): lease(self.root / 'runner.lock', shared=True)

    def test_cleanup_unknown_remains_blocker_across_restart(self):
        self.start()
        self.gateway.marker('UNKNOWN')
        self.assertTrue(pipeline_held(self.root))
        with self.assertRaisesRegex(PipelineError, 'EXECUTOR_CLEANUP_REQUIRES_RECONCILIATION'):
            HostGateway(self.root, str(self.binary), PROJECT, lambda: False, transport_factory=lambda _: self.fail('no restart'))

    def test_persistent_unknown_blocks_both_legacy_runner_paths(self):
        import uuid
        self.gateway.marker('UNKNOWN')
        task = {'id': str(uuid.uuid4()), 'workspace_id': str(uuid.uuid4())}
        with self.assertRaisesRegex(RuntimeError, 'unresolved'):
            with guard(self.root, task): pass
        runner = Runner.__new__(Runner); runner.root = self.root
        with self.assertRaisesRegex(RuntimeError, 'unresolved'): runner.lock()

    def test_corrupt_ledger_is_not_idle(self):
        self.gateway.pipeline.db.write_bytes(b'not sqlite')
        self.assertTrue(pipeline_held(self.root))
        self.gateway.pipeline._mutate = lambda _: []  # Cleanup fixture only; no production fallback.

    def test_shutdown_preserves_unconfirmed_turn_as_unknown(self):
        thread = self.start(); task = self.submit(thread)
        self.gateway.close()
        state = self.gateway.pipeline._mutate(lambda s: s['tasks'][task['id']]['state'])
        self.assertEqual(state, 'UNKNOWN')
        self.assertTrue(pipeline_held(self.root))

    def test_proxy_is_typed_and_never_spawns_locally(self):
        proxy = DaemonPipelineProxy(self.root)
        with patch('codex_host.request', return_value={'models': []}) as req, patch('codex_host.subprocess.Popen') as spawn:
            self.assertEqual(proxy.dispatch('codex_capabilities', {}), {'models': []})
            req.assert_called_once_with(self.root, 'codex_pipeline', {'tool': 'codex_capabilities', 'arguments': {}}, timeout=45)
            with self.assertRaises(PipelineError): proxy.dispatch('command/exec', {'command': 'bad'})
            spawn.assert_not_called()

    def test_daemon_disabled_and_invalid_fields_never_reach_gateway(self):
        daemon = Daemon.__new__(Daemon); daemon.allow_codex_pipeline = False; daemon.pipeline = Mock()
        with self.assertRaises(ValueError): daemon.handle('codex_pipeline', {'tool': 'codex_capabilities', 'arguments': {}})
        daemon.pipeline.dispatch.assert_not_called()
        daemon.allow_codex_pipeline = True
        with self.assertRaises(ValueError): daemon.handle('codex_pipeline', {'tool': 'codex_capabilities', 'arguments': {}, 'command': 'bad'})
        result = daemon.handle('codex_pipeline', {'tool': 'unknown', 'arguments': {}})
        self.assertEqual(result['status'], 'BLOCKED')
        daemon.pipeline.dispatch.assert_not_called()

    def test_adapter_proxy_daemon_gateway_roundtrip_and_stop_confirmation(self):
        from mcp_adapter import Adapter
        daemon = Daemon.__new__(Daemon); daemon.allow_codex_pipeline = True; daemon.pipeline = self.gateway
        proxy = DaemonPipelineProxy(self.root)
        adapter = Adapter(self.root, allow_codex_pipeline=True, pipeline=proxy)
        self.addCleanup(adapter.audit.close)
        def forward(runtime, method, params, timeout):
            self.assertEqual(method, 'codex_pipeline')
            return daemon.handle(method, params)
        def call(name, **args):
            result = adapter.call_tool('codex_' + name, args, 1)
            self.assertFalse(result['isError'], result)
            return result['structuredContent']['data']
        with patch('codex_host.request', side_effect=forward):
            thread = call('thread_start', workspace='fixture', model='offline-model', request_key='wire-start')['thread_id']
            task = call('task_submit', thread_id=thread, prompt='wire-public-canary', request_key='wire-task')
            self.assertEqual(task['state'], 'RUNNING')
            self.assertFalse(call('task_cancel', task_id=task['id'], request_key='wire-stop')['stop_confirmed'])
            remote = self.gateway.pipeline._mutate(lambda s: s['threads'][thread]['remote_id'])
            self.peer.threads[remote]['turns'][-1].update(status='interrupted')
            self.gateway.pump()
            self.assertEqual(call('task_get', task_id=task['id'])['state'], 'CANCELLED')
        self.assertFalse(self.gateway.held)
        self.assertNotIn('wire-public-canary', adapter.audit_path.read_text())

    def test_host_transport_uses_separate_process_group_without_shell(self):
        process = PipeProcess(); self.addCleanup(process.cleanup)
        transport = HostTransport(str(self.binary))
        with patch('codex_host.subprocess.Popen', return_value=process) as popen:
            self.assertIs(transport._spawn(), process)
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertFalse(popen.call_args.kwargs['shell'])

    def test_reparented_same_group_child_ignoring_term_is_killed_before_release(self):
        root = {'pid': 100, 'ppid': 1, 'pgrp': 100, 'start': 'root-token', 'state': 'Z'}
        child = {'pid': 101, 'ppid': 1, 'pgrp': 100, 'start': 'child-token', 'state': 'S'}
        identities, signals = {100: root, 101: child}, []
        transport = HostTransport(str(self.binary)); transport._proc = SimpleNamespace(pid=100)
        transport.seen = {100: 'root-token'}
        def base_close(self):
            identities.pop(100, None); self._closed = True; self._diag['reaped'] = True
        def killpg(pid, sig):
            signals.append((pid, sig))
            if sig == signal.SIGKILL: identities.pop(101, None)
        with patch('codex_host.process_snapshot', side_effect=lambda: list(identities.values())), patch('codex_host.proc_identity', side_effect=identities.get), patch.object(StdioTransport, 'close', base_close), patch('codex_host.os.killpg', side_effect=killpg), patch('codex_host.os.kill'):
            transport.close()
        self.assertIn((100, signal.SIGKILL), signals)
        self.assertEqual(transport.seen[101], 'child-token')
        self.assertTrue(transport.cleanup_confirmed)

    def test_reused_group_identity_is_unknown_and_never_signalled(self):
        current = {'pid': 100, 'ppid': 1, 'pgrp': 100, 'start': 'different-token', 'state': 'S'}
        transport = HostTransport(str(self.binary)); transport._proc = SimpleNamespace(pid=100)
        transport.seen = {100: 'old-token'}
        def base_close(self): self._closed = True; self._diag['reaped'] = True
        with patch('codex_host.process_snapshot', return_value=[current]), patch('codex_host.proc_identity', return_value=current), patch.object(StdioTransport, 'close', base_close), patch('codex_host.os.killpg') as group, patch('codex_host.os.kill') as kill:
            transport.close()
        group.assert_not_called(); kill.assert_not_called()
        self.assertFalse(transport.cleanup_confirmed)


if __name__ == '__main__': unittest.main()
