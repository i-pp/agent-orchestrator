"""Fixture-only transitions; no live database, socket, service or model."""
import copy, json, os, sqlite3, sys, tempfile, unittest
from contextlib import closing, redirect_stdout
import importlib.util, io
from pathlib import Path
from unittest.mock import patch, Mock
PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT / 'src'), str(PROJECT / 'scripts')]
from codex_pipeline import Pipeline, PipelineError, digest, packed
import pipeline_policy_migration as m
from concurrency import lease
from poc_services import Services
from test_codex_pipeline import Peer

class MigrationTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'logs').mkdir()
        self.ws = self.root / 'workspace'
        self.ws.mkdir()
        self.common = 'a' * 64
        self.old = {'schema': 1, 'workspaces': {'fixture': str(self.ws)}, 'models': ['offline-model'], 'max_threads': 1, 'max_tasks': 1, 'concurrency': 1, 'timeout_seconds': 120}
        self.old_raw = json.dumps(self.old, indent=2) + '\n'
        self.write(m.POLICY, self.old_raw)
        self.write('codex-pipeline-cleanup.json', '{"status":"RELEASED"}')
        self.peer = Peer(str(self.ws))
        self.pipe = self.new(self.old)
        self.thread = self.pipe.start('fixture', 'offline-model', 'start')['thread_id']
        self.task = self.pipe.dispatch('codex_task_submit', {'thread_id': self.thread, 'prompt': 'public fixture', 'request_key': 'original'})['id']
        remote = self.state()['threads'][self.thread]['remote_id']
        self.peer.threads[remote]['turns'][0].update(status='completed', items=[{'type': 'agentMessage', 'text': 'PRESERVED_OK'}])
        self.pipe.refresh(self.task)
        self.pipe._mutate(lambda s: s['threads'][self.thread].update(history_verified=True, original_remote_id='old-empty', native_creation_count=2, native_replacements=[{'reason': 'EMPTY_NO_ROLLOUT'}]))
        self.initial = self.state()
        self.target = {**self.old, 'models': ['offline-model', 'offline-secondary'], 'max_threads': 2, 'max_tasks': 6}
        self.candidate = self.write('candidate.json', json.dumps(self.target, indent=2) + '\n')
        self.plan = m.plan_transition(self.root, self.candidate, self.common, 'expand-once', now=2000)

    def write(self, name, text):
        p = self.root / name
        p.write_text(text)
        p.chmod(384)
        return p

    def new(self, p):
        return Pipeline(self.root, p, self.peer.client, instructions='fixture policy', policy_sha256=self.common, clock=lambda: 1000)

    def sql(self, sql, args=()):
        with closing(sqlite3.connect(self.root / m.DB)) as c:
            with c:
                return c.execute(sql, args).fetchall()

    def state(self):
        return json.loads(self.sql('SELECT body FROM ledger WHERE id=1')[0][0])

    def apply(self, p=None):
        p = self.plan if p is None else p
        return m.apply_transition(self.root, p, digest(p))

    def pending(self, replace=False):
        real = m._write_policy

        def crash(root, raw):
            if replace:
                real(root, raw)
            raise OSError('fixture crash')
        with patch.object(m, '_write_policy', side_effect=crash), self.assertRaises(OSError):
            self.apply()

    def recover(self, a):
        return m.recover_transition(self.root, self.plan['id'], digest(self.plan), action=a)

    def test_readonly_plan_preserves_exact_old_bytes(self):
        before = (self.root / m.DB).read_bytes()
        p = m.plan_transition(self.root, self.candidate, self.common, 'other', now=2001)
        self.assertEqual(before, (self.root / m.DB).read_bytes())
        self.assertEqual(p['old_raw'], self.old_raw)
        self.assertEqual(self.state(), self.initial)

    def test_commit_retains_ids_attempts_answer_events_and_lineage(self):
        self.assertEqual(self.apply()['status'], 'COMMITTED')
        s = self.state()
        for k in ['threads', 'tasks', 'ops']:
            self.assertEqual(s[k], self.initial[k])
        self.assertEqual(s['events'][:-1], self.initial['events'])
        self.assertEqual(s['tasks'][self.task]['answer'], 'PRESERVED_OK')
        self.assertEqual(s['threads'][self.thread]['native_creation_count'], 2)
        self.assertEqual(s['policy_hash'], self.plan['new_policy_hash'])
        self.assertEqual(s['policy_migrations'][0]['old_policy_hash'], self.initial['policy_hash'])
        self.assertEqual((self.root / m.POLICY).read_bytes(), self.plan['new_raw'].encode())
        self.assertEqual((self.root / m.POLICY).stat().st_mode & 511, 384)
        m.assert_migration_ready(self.root / m.DB)
        self.new(self.target)

    def test_consumed_task_quota_is_not_reset(self):
        self.apply()
        p = self.new(self.target)
        for i in range(5):
            p.store(self.thread, 'stored fixture', 'extra-' + str(i))
        with self.assertRaisesRegex(PipelineError, 'TASK_CAPACITY'):
            p.store(self.thread, 'denied', 'excess')
        self.assertEqual(len(self.state()['tasks']), 6)
        self.assertIn(self.task, self.state()['tasks'])

    def test_pending_blocks_startup_before_mutation(self):
        self.pending()
        before = (self.root / m.DB).read_bytes()
        with patch.object(Pipeline, '_mutate', side_effect=AssertionError('no writes')):
            with self.assertRaisesRegex(PipelineError, 'POLICY_MIGRATION_PENDING'):
                self.new(self.old)
        self.assertEqual(before, (self.root / m.DB).read_bytes())
        self.assertEqual(self.state(), self.initial)


    def test_abort_after_file_replacement_preserves_untouched_ledger(self):
        self.pending(True)
        self.assertEqual(self.state(), self.initial)
        self.assertEqual((self.root / m.POLICY).read_text(), self.plan['new_raw'])
        self.assertEqual(self.recover('abort')['status'], 'ABORTED')
        self.assertEqual((self.root / m.POLICY).read_text(), self.old_raw)
        self.assertEqual(self.state(), self.initial)
        self.new(self.old)
        with self.assertRaisesRegex(PipelineError, 'ALREADY_USED'):
            m.plan_transition(self.root, self.candidate, self.common, 'expand-once')

    def test_finalize_only_exact_fully_new_pair(self):
        self.pending(True)
        with self.assertRaisesRegex(PipelineError, 'AMBIGUOUS'):
            self.recover('finalize')
        self.sql('UPDATE ledger SET body=? WHERE id=1', (packed(m._transform(self.initial, self.plan)),))
        self.assertEqual(self.recover('finalize')['status'], 'COMMITTED')
        m.assert_migration_ready(self.root / m.DB)

    def test_final_sql_failure_atomically_retains_old_ledger_and_pending_intent(self):
        self.sql('CREATE TABLE ' + m.TABLE + ' (id TEXT PRIMARY KEY,schema INTEGER NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL)')
        self.sql('CREATE TRIGGER fail_final BEFORE UPDATE ON ' + m.TABLE + " WHEN NEW.state='COMMITTED' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply()
        self.assertEqual(self.state(), self.initial)
        self.assertEqual(self.sql('SELECT state FROM ' + m.TABLE)[0][0], 'PREPARED')
        self.assertEqual(self.recover('abort')['status'], 'ABORTED')

    def test_ledger_drift_is_never_overwritten(self):
        self.pending()
        s = copy.deepcopy(self.initial)
        s['tasks'][self.task]['answer'] = 'foreign change'
        self.sql('UPDATE ledger SET body=? WHERE id=1', (packed(s),))
        with self.assertRaisesRegex(PipelineError, 'AMBIGUOUS'):
            self.recover('abort')
        self.assertEqual(self.state(), s)

    def test_semantically_equal_unrecorded_file_drift_refuses_recovery(self):
        self.pending()
        self.write(m.POLICY, json.dumps(self.old) + '\n')
        with self.assertRaisesRegex(PipelineError, 'AMBIGUOUS'):
            self.recover('abort')
        self.assertEqual(self.state(), self.initial)

    def test_unknown_journal_version_refuses_without_writes(self):
        self.pending()
        self.sql('UPDATE ' + m.TABLE + ' SET schema=3')
        before = (self.root / m.DB).read_bytes()
        with self.assertRaisesRegex(PipelineError, 'UNSUPPORTED_MIGRATION_JOURNAL'):
            m.assert_migration_ready(self.root / m.DB)
        with self.assertRaises(PipelineError):
            self.recover('abort')
        self.assertEqual(before, (self.root / m.DB).read_bytes())

    def test_unknown_common_or_ledger_version_refuses(self):
        with self.assertRaisesRegex(PipelineError, 'SOURCE_VERSION_UNKNOWN'):
            m.plan_transition(self.root, self.candidate, 'b' * 64, 'other')
        self.pipe._mutate(lambda s: s.update(format_version=99))
        with self.assertRaisesRegex(PipelineError, 'UNSUPPORTED_LEDGER_VERSION'):
            m.plan_transition(self.root, self.candidate, self.common, 'other')

    def test_expansion_does_not_retarget_workspace_or_other_permissions(self):
        for k, v in [('schema', 2), ('timeout_seconds', 121), ('concurrency', 2), ('workspaces', {'renamed': str(self.ws)}), ('models', ['offline-secondary'])]:
            self.write('candidate.json', json.dumps({**self.target, k: v}))
            with self.subTest(field=k), self.assertRaises(PipelineError):
                m.plan_transition(self.root, self.candidate, self.common, 'other')
        self.assertEqual(self.state(), self.initial)

    def test_uncertain_operation_task_thread_history_or_cleanup_refuses(self):
        for k in ['op', 'task', 'thread', 'history', 'cleanup']:
            s = copy.deepcopy(self.initial)
            self.write('codex-pipeline-cleanup.json', '{"status":"RELEASED"}')
            if k == 'op':
                s['ops']['original']['status'] = 'UNKNOWN'
            if k == 'task':
                s['tasks'][self.task]['state'] = 'QUEUED'
            if k == 'thread':
                s['threads'][self.thread]['closing'] = True
            if k == 'history':
                s['threads'][self.thread]['history_verified'] = False
            if k == 'cleanup':
                self.write('codex-pipeline-cleanup.json', '{"status":"UNKNOWN"}')
            self.sql('UPDATE ledger SET body=? WHERE id=1', (packed(s),))
            with self.subTest(case=k), self.assertRaises(PipelineError):
                m.plan_transition(self.root, self.candidate, self.common, 'other')

    def test_daemon_runner_or_service_operation_lease_blocks_apply(self):
        for name in ['daemon.lock', 'runner.lock', 'poc-services.lock']:
            with self.subTest(name=name), lease(self.root / name), self.assertRaises(RuntimeError):
                self.apply()
        self.assertEqual(self.state(), self.initial)
        self.assertEqual(self.sql('SELECT name FROM sqlite_master WHERE name=?', (m.TABLE,)), [])

    def test_service_requires_stopped_not_only_paused(self):
        self.write('poc-services-state.json', '{"phase":"READY"}')
        with self.assertRaisesRegex(PipelineError, 'SERVICES_NOT_STOPPED'):
            self.apply()
        self.write('poc-services-state.json', '{"phase":"STOPPED"}')
        self.apply()

    def test_changed_or_stale_plan_refuses(self):
        p = copy.deepcopy(self.plan)
        p['id'] = 'changed'
        with self.assertRaisesRegex(PipelineError, 'PLAN_CHANGED'):
            m.apply_transition(self.root, p, digest(self.plan))
        self.pipe._mutate(lambda s: s['events'].append({'seq': len(s['events']) + 1, 'kind': 'foreign'}))
        with self.assertRaisesRegex(PipelineError, 'PLAN_STALE'):
            self.apply()

    def test_fsync_policy_and_directory_before_final_db_commit(self):
        events = []
        real = m._connect
        sync = os.fsync

        def connect(root, **kw):
            c = real(root, **kw)
            if kw.get('write'):
                c.set_trace_callback(lambda sql: events.append('commit') if sql == 'COMMIT' else None)
            return c

        def fsync(fd):
            events.append('fsync')
            return sync(fd)
        with patch.object(m, '_connect', side_effect=connect), patch.object(m.os, 'fsync', side_effect=fsync):
            self.apply()
        self.assertEqual(events, ['commit', 'fsync', 'fsync', 'commit'])

    def test_reverse_known_version_retains_new_consumption(self):
        self.apply()
        p = self.new(self.target)
        from test_codex_app_protocol import MODEL
        self.peer.models.append({**MODEL, 'id':'offline-secondary', 'model':'offline-secondary', 'isDefault':False})
        task = p.store(self.thread, 'stored secondary', 'secondary', model='offline-secondary')['id']
        p.cancel(task, 'cancel-secondary')
        before = self.state()
        old = self.write('old.json', self.old_raw)
        plan = m.plan_transition(self.root, old, self.common, 'rollback', direction='rollback', now=2001)
        m.apply_transition(self.root, plan, digest(plan))
        state = self.state()
        self.assertEqual(state['tasks'], before['tasks'])
        self.assertEqual(state['ops'], before['ops'])
        self.assertEqual(len(state['policy_migrations']), 2)
        self.assertEqual(state['policy_hash'], self.initial['policy_hash'])
        with self.assertRaisesRegex(PipelineError, 'TASK_CAPACITY'):
            self.new(self.old).store(self.thread, 'denied', 'not-reset')

    def test_unrecorded_reverse_refuses(self):
        self.apply()
        p = self.write('unknown.json', json.dumps({**self.old, 'max_tasks': 2}))
        with self.assertRaisesRegex(PipelineError, 'ROLLBACK_VERSION_NOT_RECORDED'):
            m.plan_transition(self.root, p, self.common, 'rollback', direction='rollback')

    def test_rebind_prepares_only_new_policy_snapshot(self):
        self.apply()
        old = {'executor_policy_sha256': 'b' * 64, 'common_policy_sha256': self.common, 'binary_sha256': 'c' * 64, 'pipeline_policy_sha256': self.plan['old_file_sha256']}
        new = {**old, 'pipeline_policy_sha256': self.plan['new_file_sha256']}
        state = {'schema': 1, 'runtime': str(self.root), 'phase': 'STOPPED', 'codex_pipeline_enabled': True, 'codex_pipeline_snapshot': old, 'credential_file_reference': '/fixture/credential-reference', 'untouched': 'fixture metadata'}
        self.assertEqual(m.rebind_service_snapshot(self.root, state, new), {**state, 'codex_pipeline_snapshot': new})
        self.assertEqual(state['codex_pipeline_snapshot'], old)
        with self.assertRaisesRegex(PipelineError, 'BINDING_MIGRATION_MISMATCH'):
            m.rebind_service_snapshot(self.root, state, {**new, 'binary_sha256': 'd' * 64})

    def test_apply_revalidates_rollback_not_trusting_plan_producer(self):
        p = copy.deepcopy(self.plan)
        p['direction'] = 'rollback'
        p['ledger_after_sha256'] = digest(m._transform(self.initial, p))
        with self.assertRaisesRegex(PipelineError, 'ROLLBACK_VERSION_NOT_RECORDED'):
            self.apply(p)
        self.assertEqual(self.state(), self.initial)

    def test_unknown_retained_bindings_are_not_retargeted(self):
        self.pipe._mutate(lambda s: s['threads'][self.thread].update(workspace='unknown-workspace'))
        with self.assertRaisesRegex(PipelineError, 'UNKNOWN_THREAD_BINDING'):
            m.plan_transition(self.root, self.candidate, self.common, 'other')

    def test_conflicting_intent_during_file_write_is_rejected(self):
        real = m._write_policy

        def corrupt(root, raw):
            real(root, raw)
            rows = self.sql('SELECT body FROM ' + m.TABLE)
            body = json.loads(rows[0][0])
            body['plan_sha256'] = '0' * 64
            self.sql('UPDATE ' + m.TABLE + ' SET body=?', (packed(body),))
        with patch.object(m, '_write_policy', side_effect=corrupt), self.assertRaisesRegex(PipelineError, 'JOURNAL_HASH_MISMATCH'):
            self.apply()
        self.assertEqual(self.state(), self.initial)

    def test_multiple_prepared_intents_and_unknown_phase_are_blocked(self):
        self.pending()
        p = copy.deepcopy(self.plan)
        p['id'] = 'second-intent'
        self.sql('INSERT INTO ' + m.TABLE + ' VALUES (?,1,?,?)', (p['id'], 'PREPARED', packed({'plan': p, 'plan_sha256': digest(p)})))
        with self.assertRaisesRegex(PipelineError, 'AMBIGUOUS_POLICY_MIGRATION'):
            m.assert_migration_ready(self.root / m.DB)
        self.sql('DELETE FROM ' + m.TABLE + ' WHERE id=?', (p['id'],))
        self.sql('UPDATE ' + m.TABLE + " SET state='UNKNOWN'")
        with self.assertRaisesRegex(PipelineError, 'UNSUPPORTED_MIGRATION_JOURNAL'):
            m.assert_migration_ready(self.root / m.DB)

    def test_missing_version_journal_is_not_legacy_after_migration(self):
        self.apply()
        self.sql('DROP TABLE ' + m.TABLE)
        with self.assertRaisesRegex(PipelineError, 'UNKNOWN_POLICY_HISTORY'):
            m.assert_migration_ready(self.root / m.DB)

    def test_non_delete_database_mode_is_not_silently_changed(self):
        with closing(sqlite3.connect(self.root / m.DB)) as c:
            c.execute('PRAGMA journal_mode=WAL')
        with self.assertRaisesRegex(PipelineError, 'UNSUPPORTED_MIGRATION_SQLITE_MODE'):
            self.apply()
        self.assertEqual(self.state(), self.initial)

    def test_reverse_requires_archiving_removed_model_threads(self):
        self.apply()
        p = self.new(self.target)
        original = self.peer.client.exchange

        def catalog(req, timeout):
            reply = original(req, timeout)
            if req['method'] == 'model/list':
                reply['result']['data'].append({**reply['result']['data'][0], 'id': 'secondary', 'model': 'offline-secondary'})
            return reply
        self.peer.client.exchange = catalog
        thread = p.start('fixture', 'offline-secondary', 'secondary-thread')['thread_id']
        task = p.store(thread, 'secondary stored', 'secondary-task')['id']
        p.cancel(task, 'secondary-cancel')
        p._mutate(lambda s: s['threads'][thread].update(history_verified=True))
        old = self.write('old.json', self.old_raw)
        with self.assertRaisesRegex(PipelineError, 'REQUIRES_ARCHIVED_REMOVED_MODEL'):
            m.plan_transition(self.root, old, self.common, 'back', direction='rollback')
        p.archive(thread, 'archive-secondary')
        plan = m.plan_transition(self.root, old, self.common, 'back', direction='rollback')
        m.apply_transition(self.root, plan, digest(plan))
        self.assertEqual(len(self.state()['threads']), 2)
        self.assertEqual(len(self.state()['tasks']), 2)

    def test_cli_plan_apply_and_failure_never_export_ledger_or_raw_errors(self):
        spec = importlib.util.spec_from_file_location('migration_cli_fixture', PROJECT / 'scripts/pipeline_policy_migration.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        destination = self.root / 'reviewed-plan.json'
        out = io.StringIO()
        bundle = type('Bundle', (), {'sha256': self.common})()
        with patch.object(cli, 'load_project_policy', return_value=bundle), redirect_stdout(out):
            self.assertEqual(cli.main(['plan', '--runtime', str(self.root), '--candidate', str(self.candidate), '--migration-id', 'cli-once', '--output', str(destination)]), 0)
            planned = json.loads(destination.read_text())
            args = ['apply', '--runtime', str(self.root), '--plan', str(destination), '--expect-plan-sha256', digest(planned)]
            self.assertEqual(cli.main(args), 2)
            self.assertEqual(self.state(), self.initial)
            self.assertEqual(cli.main(args + ['--apply']), 0)
        self.assertNotIn('PRESERVED_OK', out.getvalue())
        self.assertNotIn('public fixture', out.getvalue())
        self.assertNotIn(str(self.ws), out.getvalue())
        self.assertEqual(destination.stat().st_mode & 511, 384)


    def test_pending_transition_holds_shared_runner_admission_until_resolved(self):
        from codex_host import pipeline_held
        self.pending()
        self.assertTrue(pipeline_held(self.root))
        self.recover('abort')
        self.assertFalse(pipeline_held(self.root))
    def test_inspection_is_readonly_and_does_not_disclose_task_content(self):
        before = (self.root / m.DB).read_bytes()
        result = m.inspect_transition(self.root)
        self.assertEqual(result['status'], 'READY')
        self.assertNotIn('PRESERVED_OK', json.dumps(result))
        self.assertNotIn('public fixture', json.dumps(result))
        self.assertEqual(before, (self.root / m.DB).read_bytes())

    def test_inspection_separates_proven_abort_from_ambiguous_drift(self):
        self.pending(True)
        self.assertEqual(m.inspect_transition(self.root)['explicit_recovery_actions'], ['abort'])
        self.write(m.POLICY, json.dumps(self.old))
        result = m.inspect_transition(self.root)
        self.assertEqual(result['status'], 'AMBIGUOUS')
        self.assertEqual(result['explicit_recovery_actions'], [])

if __name__ == '__main__':
    unittest.main()
