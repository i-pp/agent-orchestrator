"""Offline adoption of a new common-policy hash, preserving existing history."""
import copy
from contextlib import closing
from contextlib import redirect_stdout
import importlib.util
import io
import os
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from codex_pipeline import Pipeline, PipelineError, digest, packed
import pipeline_policy_migration as migration
from codex_host import HostGateway
from test_codex_app_protocol import MODEL


class SyntheticClient:
    def __init__(self, workspace):
        self.workspace = workspace
        self.requests = []

    def request(self, method, params):
        self.requests.append((method, params))
        if method == 'account/read': return {'account': {'type': 'chatgpt'}}
        if method == 'model/list': return {'data': [copy.deepcopy(MODEL)], 'nextCursor': None}
        if method == 'thread/start':
            return {'thread': {'id': 'new-synthetic-remote'}, 'model': 'offline-model',
                    'modelProvider': 'openai', 'cwd': self.workspace,
                    'approvalPolicy': 'on-request', 'approvalsReviewer': 'user',
                    'sandbox': {'type': 'readOnly'}}
        raise AssertionError('Unexpected model/protocol action: ' + method)


class CommonPolicyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.old_common, self.new_common = 'a' * 64, 'b' * 64
        self.policy = {'schema': 1, 'workspaces': {'fixture': str(self.workspace)},
                       'models': ['offline-model'], 'max_threads': 4, 'max_tasks': 8,
                       'concurrency': 1, 'timeout_seconds': 60}
        self.raw = json.dumps(self.policy) + '\n'
        self.file = self.write(migration.POLICY, self.raw)
        self.write('codex-pipeline-cleanup.json', '{"status":"RELEASED"}')
        self.client = SyntheticClient(str(self.workspace))
        self.pipeline(self.old_common)
        self.old_thread = '10000000-0000-4000-8000-000000000001'
        self.old_task = '20000000-0000-4000-8000-000000000001'
        state = self.state()
        state['threads'][self.old_thread] = {
            'id': self.old_thread, 'remote_id': 'retained-remote', 'workspace': 'fixture',
            'model': 'offline-model', 'active_task': None, 'archived': False, 'closing': False,
            'history_verified': True, 'native_context_state': 'READY'}
        state['tasks'][self.old_task] = {
            'id': self.old_task, 'thread_id': self.old_thread, 'model': 'offline-model',
            'state': 'COMPLETED', 'answer': 'PRESERVED', 'owner': {'immutable': 'old identity'}}
        self.save(state)
        self.before = copy.deepcopy(state)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text); path.chmod(0o600)
        return path

    def state(self):
        with closing(sqlite3.connect(self.root / migration.DB)) as conn:
            return json.loads(conn.execute('SELECT body FROM ledger WHERE id=1').fetchone()[0])

    def save(self, state):
        with closing(sqlite3.connect(self.root / migration.DB)) as conn:
            with conn:
                conn.execute('UPDATE ledger SET body=? WHERE id=1', (packed(state),))

    def pipeline(self, common):
        return Pipeline(self.root, self.policy, self.client,
                        instructions='verified five-document context', policy_sha256=common)

    def plan(self, **kw):
        return migration.plan_transition(self.root, self.file, self.old_common,
            'adopt-editorial-v2', new_common_hash=self.new_common, **kw)

    def apply(self, plan):
        return migration.apply_transition(self.root, plan, digest(plan))

    def test_commit_preserves_records_and_rejects_old_thread_before_rpc(self):
        plan = self.plan()
        self.assertEqual(self.state(), self.before)
        self.assertEqual(self.apply(plan)['status'], 'COMMITTED')
        state = self.state()
        self.assertEqual(state['tasks'], self.before['tasks'])
        self.assertEqual(state['ops'], self.before['ops'])
        self.assertEqual(state['threads'], self.before['threads'])
        self.assertEqual(self.file.read_text(), self.raw)
        migration.assert_migration_ready(self.root / migration.DB)
        new = self.pipeline(self.new_common)
        calls = len(self.client.requests)
        with self.assertRaisesRegex(PipelineError, 'THREAD_POLICY_VERSION_CHANGED'):
            new.store(self.old_thread, 'must not resume', 'blocked-old-context', queue=True)
        self.assertEqual(len(self.client.requests), calls)
        self.assertEqual(new.refresh(self.old_task)['answer'], 'PRESERVED')
        result = new.start('fixture', 'offline-model', 'new-context')
        self.assertEqual(self.state()['threads'][result['thread_id']]['common_policy_sha256'], self.new_common)
        request = next(p for method, p in self.client.requests if method == 'thread/start')
        self.assertEqual(request['developerInstructions'], 'verified five-document context')

    def test_common_adoption_cannot_change_other_policy_fields(self):
        candidate = self.write('candidate.json', json.dumps({**self.policy, 'max_tasks': 9}))
        with self.assertRaisesRegex(PipelineError, 'MIGRATION_SCOPE_CHANGED'):
            migration.plan_transition(self.root, candidate, self.old_common, 'scope-change', new_common_hash=self.new_common)
        self.assertEqual(self.state(), self.before)

    def test_stale_common_hash_and_held_tasks_refuse_without_changes(self):
        with self.assertRaisesRegex(PipelineError, 'SOURCE_VERSION_UNKNOWN'):
            migration.plan_transition(self.root, self.file, 'c' * 64, 'wrong-old', new_common_hash=self.new_common)
        state = self.state(); state['tasks'][self.old_task]['state'] = 'RUNNING'; self.save(state)
        with self.assertRaisesRegex(PipelineError, 'TERMINAL_TASKS'):
            self.plan()
        self.assertEqual(self.state(), state)

    def test_pending_stops_startup_and_abort_keeps_old_ledger(self):
        plan = self.plan()
        with patch.object(migration, '_write_policy', side_effect=OSError('fixture interruption')):
            with self.assertRaises(OSError): self.apply(plan)
        with self.assertRaisesRegex(PipelineError, 'MIGRATION_PENDING'):
            self.pipeline(self.new_common)
        self.assertEqual(migration.recover_transition(self.root, plan['id'], digest(plan), action='abort')['status'], 'ABORTED')
        self.assertEqual(self.state(), self.before)
        self.pipeline(self.old_common)

    def test_rebind_changes_only_common_identity_after_commit(self):
        plan = self.plan(); self.apply(plan)
        old = {'executor_policy_sha256': 'c' * 64, 'binary_sha256': 'd' * 64,
               'pipeline_policy_sha256': hashlib.sha256(self.raw.encode()).hexdigest(),
               'common_policy_sha256': self.old_common}
        previous = {'schema': 1, 'runtime': str(self.root), 'phase': 'STOPPED',
                    'codex_pipeline_enabled': True, 'codex_pipeline_snapshot': old,
                    'preserve_other_settings': ['unchanged']}
        current = {**old, 'common_policy_sha256': self.new_common}
        result = migration.rebind_service_snapshot(self.root, previous, current)
        self.assertEqual(result, {**previous, 'codex_pipeline_snapshot': current})
        self.assertEqual(previous['codex_pipeline_snapshot'], old)
        with self.assertRaisesRegex(PipelineError, 'BINDING_MIGRATION_MISMATCH'):
            migration.rebind_service_snapshot(self.root, previous, {**current, 'binary_sha256': 'e' * 64})

    def test_legacy_migration_history_can_precede_common_adoption(self):
        expanded = {**self.policy, 'max_tasks': 9}
        candidate = self.write('candidate.json', json.dumps(expanded))
        first = migration.plan_transition(self.root, candidate, self.old_common, 'legacy-expand')
        self.apply(first)
        second = self.plan()
        self.apply(second)
        migration.assert_migration_ready(self.root / migration.DB)
        self.assertEqual([p['schema'] for p in self.state()['policy_migrations']], [1, 2])

    def test_regular_transition_after_common_adoption_keeps_old_context_blocked(self):
        self.apply(self.plan())
        expanded = {**self.policy, 'max_tasks': 9}
        candidate = self.write('candidate.json', json.dumps(expanded))
        following = migration.plan_transition(self.root, candidate, self.new_common, 'later-expand')
        self.apply(following)
        migration.assert_migration_ready(self.root / migration.DB)
        self.policy = expanded
        with self.assertRaisesRegex(PipelineError, 'THREAD_POLICY_VERSION_CHANGED'):
            self.pipeline(self.new_common).store(self.old_thread, 'blocked', 'still-old')
        self.assertEqual(self.state()['tasks'], self.before['tasks'])

    def test_common_adoption_rejects_noop_invalid_hash_and_unknown_journal(self):
        for new in [self.old_common, 'invalid']:
            with self.subTest(new=new), self.assertRaises(PipelineError):
                migration.plan_transition(self.root, self.file, self.old_common, 'invalid', new_common_hash=new)
        with self.assertRaises(PipelineError): self.plan(direction='rollback')
        self.apply(self.plan())
        with closing(sqlite3.connect(self.root / migration.DB)) as conn:
            with conn: conn.execute('UPDATE '+migration.TABLE+' SET schema=3')
        before=(self.root / migration.DB).read_bytes()
        with self.assertRaisesRegex(PipelineError, 'UNSUPPORTED_MIGRATION_JOURNAL'):
            migration.assert_migration_ready(self.root / migration.DB)
        self.assertEqual((self.root / migration.DB).read_bytes(), before)

    def test_cli_common_plan_and_apply_use_both_verified_hashes(self):
        spec = importlib.util.spec_from_file_location('migration_cli', PROJECT / 'scripts/pipeline_policy_migration.py')
        cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
        output = self.root / 'reviewed-plan.json'
        before = self.state()
        mask = os.umask(0o077)
        try:
            with patch.object(cli, 'load_project_policy', return_value=SimpleNamespace(sha256=self.new_common)):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(['plan', '--runtime', str(self.root), '--candidate', str(self.file),
                        '--previous-common-sha256', self.old_common, '--migration-id', 'cli-common',
                        '--output', str(output)]), 0)
                plan = json.loads(output.read_text())
                self.assertEqual(plan['schema'], 2)
                self.assertEqual(plan['common_hash'], self.old_common)
                self.assertEqual(plan['new_common_hash'], self.new_common)
                self.assertEqual(self.state(), before)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(['apply', '--runtime', str(self.root), '--plan', str(output),
                        '--expect-plan-sha256', digest(plan), '--apply']), 0)
                self.assertEqual(self.state()['tasks'], before['tasks'])
                migration.assert_migration_ready(self.root / migration.DB)
        finally:
            os.umask(mask)

    def test_operator_resume_paths_reject_old_policy_before_host_or_rpc(self):
        self.apply(self.plan())
        gateway = HostGateway.__new__(HostGateway)
        gateway.root = self.root
        gateway.pipeline = self.pipeline(self.new_common)
        gateway.transport = gateway.protocol = gateway.budget = None
        gateway.require_host = Mock(side_effect=AssertionError('No host probe/start allowed'))
        gateway.connect = Mock(side_effect=AssertionError('No transport allowed'))
        gateway.transport_factory = Mock(side_effect=AssertionError('No transport factory allowed'))
        for operation in [lambda: gateway.inspect_owned_thread(self.old_thread),
                          lambda: gateway.resolve_absent_contexts([self.old_thread]),
                          lambda: gateway.rpc('thread/resume', {'threadId': 'retained-remote'})]:
            with self.assertRaisesRegex(PipelineError, 'THREAD_POLICY_VERSION_CHANGED'):
                operation()
        gateway.require_host.assert_not_called()
        gateway.connect.assert_not_called()
        gateway.transport_factory.assert_not_called()


if __name__ == '__main__': unittest.main()
