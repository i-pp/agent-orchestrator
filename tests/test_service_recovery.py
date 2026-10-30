"""Operator plans and late busy-state rejection using fixtures and mocked manager metadata."""
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'src'),str(PROJECT/'scripts')]
import poc_services as module
from poc_services import Services, Refused, MODE, MARKER, STATE
from chat_mailbox import digest


class RecoveryPlanTests(unittest.TestCase):
    def setUp(self):
        (PROJECT/'.runtime').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=PROJECT/'.runtime',prefix='development-poc-recovery-')
        self.root=Path(self.temp.name);(self.root/'logs').mkdir(mode=0o700)
        marker=self.root/MARKER
        marker.write_text(json.dumps({'schema':1,'mode':MODE,'runtime':str(self.root)}));marker.chmod(0o600)
        self.key_temp=tempfile.TemporaryDirectory(prefix='recovery-reference-')
        self.key=Path(self.key_temp.name)/'existing-reference'
        self.key.write_text('synthetic-reference-value-only');self.key.chmod(0o600)
        helper=self.root/'helpers';helper.mkdir()
        for name in ['extension.py','selection_extension.py']:(helper/name).write_text('# offline fixture')
        config=self.root/'local-llm.json';config.write_text(json.dumps({'schema':1,'helper_directory':str(helper)}));config.chmod(0o600)
        policy=self.root/'chat-mailbox-policy.json'
        policy.write_text(json.dumps({'schema':1,'allowed':[{
            'task_id':'00000000-0000-4000-8000-000000000001',
            'request_id':'00000000-0000-4000-8000-000000000002',
            'request_sha256':digest('Non-secret recovery fixture')}]}));policy.chmod(0o600)
        with contextlib.closing(sqlite3.connect(self.root/'tasks.sqlite')) as db:
            db.execute('CREATE TABLE tasks(state TEXT NOT NULL)');db.commit()
        self.client=patch.object(module,'CLIENT',Path('/usr/bin/true'));self.client.start()
        # Validate real private-file metadata; host ancestry ownership is mocked.
        self.metadata=patch.object(module,'credential_metadata',side_effect=module.private_file);self.metadata.start()
        self.service=Services(self.root)
        self.state={'schema':1,'runtime':str(self.root),'token':'a'*32,'units':self.service.units,
                    'invocations':{'daemon':'b'*32,'tunnel':'c'*32},'phase':'READY',
                    'credential_file_reference':str(self.key),'codex_file_reference':'/usr/bin/true',
                    'local_llm_enabled':True,'local_models_enabled':True,'chat_mailbox_enabled':True}
        self.save()
        self.props={role:{'LoadState':'loaded','ActiveState':'active','Transient':'yes',
                         'Description':self.service.description(self.state,role),
                         'InvocationID':self.state['invocations'][role]} for role in self.service.units}
        self.show=patch.object(Services,'show',side_effect=lambda name:self.props[next(k for k,v in self.service.units.items() if v==name)])
        self.show.start()
        self.manager=patch.object(Services,'call',side_effect=AssertionError('No real manager calls'));self.manager.start()

    def save(self):
        path=self.root/STATE;path.write_text(json.dumps(self.state));path.chmod(0o600)

    def configured(self):
        return Services(self.root,Path('/usr/bin/true'),self.key,True,True,True)

    def work(self,state):
        with contextlib.closing(sqlite3.connect(self.root/'tasks.sqlite')) as db:
            db.execute('DELETE FROM tasks');db.execute('INSERT INTO tasks VALUES(?)',(state,))
            db.commit()

    def tearDown(self):
        self.manager.stop();self.show.stop();self.metadata.stop();self.client.stop()
        self.key_temp.cleanup();self.temp.cleanup()

    def test_resume_plan_uses_saved_references_and_eleven_tools_without_apply(self):
        original=(self.root/STATE).read_bytes()
        with patch.object(Services,'launch') as launch,patch.object(Services,'stop_owned') as stop:
            result=self.service.recovery_plan()
        self.assertFalse(result['automatic_apply']);self.assertTrue(result['revalidate_before_apply'])
        self.assertEqual(len(result['preflight']['tool_names']),11)
        for operation,argv in result['commands'].items():
            self.assertEqual(argv[argv.index('--key-file')+1],str(self.key))
            self.assertEqual(argv[argv.index('--codex')+1],'/usr/bin/true')
            self.assertEqual(argv[argv.index('--runtime')+1],str(self.root))
        for op in ['preflight','start']:
            for flag in ['--allow-local-llm','--allow-local-models','--allow-chat-mailbox']:
                self.assertIn(flag,result['commands'][op])
        self.assertNotIn('synthetic-reference-value-only',json.dumps(result))
        self.assertFalse((self.root/'chat-mailbox.sqlite').exists())
        self.assertEqual((self.root/STATE).read_bytes(),original)
        launch.assert_not_called();stop.assert_not_called()

    def test_rollback_plan_removes_only_mailbox_and_keeps_old_seven(self):
        result=self.service.recovery_plan(True)
        self.assertEqual(result['operation'],'rollback-plan')
        self.assertEqual(len(result['preflight']['tool_names']),7)
        argv=result['commands']['start']
        self.assertIn('--allow-local-llm',argv);self.assertIn('--allow-local-models',argv)
        self.assertNotIn('--allow-chat-mailbox',argv);self.assertNotIn('--allow-writes',argv)
        self.assertTrue(json.loads((self.root/STATE).read_text())['chat_mailbox_enabled'])

    def test_incomplete_record_is_blocked_instead_of_using_defaults(self):
        for field in ['credential_file_reference','codex_file_reference','local_llm_enabled',
                      'local_models_enabled','chat_mailbox_enabled']:
            old=self.state.pop(field);self.save()
            with self.subTest(field=field),self.assertRaisesRegex(Refused,'incomplete'):
                self.service.recovery_plan()
            self.state[field]=old;self.save()
        (self.root/STATE).unlink()
        with self.assertRaisesRegex(Refused,'Missing'):self.service.recovery_plan()

    def test_uncertain_generation_is_not_planned_or_started(self):
        for phase in ['STARTING','CLEANUP_UNCONFIRMED']:
            self.state['phase']=phase;self.save()
            with self.subTest(phase=phase),self.assertRaisesRegex(Refused,'uncertain'):
                self.service.recovery_plan()
            with self.assertRaisesRegex(Refused,'uncertain'):self.configured().preflight()

    def test_foreign_invocation_and_in_progress_unit_are_blocked(self):
        self.props['tunnel']['InvocationID']='foreign'
        with self.assertRaisesRegex(Refused,'ownership'):self.service.recovery_plan()
        self.props['tunnel']['InvocationID']=self.state['invocations']['tunnel']
        self.props['tunnel']['ActiveState']='activating'
        with self.assertRaisesRegex(Refused,'unresolved'):self.service.recovery_plan()

    def test_confirmed_absent_unit_can_be_planned_without_duplicate_start(self):
        self.props['tunnel']={'LoadState':'not-found','ActiveState':'inactive'}
        result=self.service.recovery_plan()
        self.assertEqual(result['observed_units']['tunnel']['ownership'],'absent')
        self.assertFalse(result['automatic_apply'])

    def test_unresolved_work_blocks_plan_preflight_start_and_stop(self):
        configured=self.configured()
        for state in ['QUEUED','RUNNING','VERIFYING','UNKNOWN','WAITING_FOR_HUMAN']:
            self.work(state)
            with patch.object(Services,'stop_owned') as stop,patch.object(Services,'launch') as launch:
                for operation in [self.service.recovery_plan,configured.preflight,configured.start,self.service.stop]:
                    with self.subTest(state=state,operation=operation.__name__),self.assertRaisesRegex(Refused,'Unresolved'):
                        operation()
                stop.assert_not_called();launch.assert_not_called()

    def test_new_unknown_after_preflight_is_rechecked_before_stop(self):
        self.configured().preflight(self.key)
        self.work('UNKNOWN')
        with patch.object(Services,'stop_owned') as stop:
            with self.assertRaisesRegex(Refused,'Unresolved'):self.service.stop()
            stop.assert_not_called()

    def test_missing_recorded_database_is_not_recreated(self):
        (self.root/'tasks.sqlite').unlink()
        for operation in [self.service.recovery_plan,self.service.stop,self.configured().preflight]:
            with self.assertRaisesRegex(Refused,'database'):operation()
        self.assertFalse((self.root/'tasks.sqlite').exists())

    def test_terminal_tasks_do_not_block_planning(self):
        for state in ['COMPLETED','FAILED','CANCELLED','TIMED_OUT']:
            self.work(state)
            with self.subTest(state=state):self.assertFalse(self.service.recovery_plan()['automatic_apply'])

    def test_stopped_record_conflicting_with_active_unit_is_blocked(self):
        self.state['phase']='STOPPED';self.save()
        with self.assertRaisesRegex(Refused,'conflicts'):self.service.recovery_plan()

    def test_cli_plan_operations_route_only_to_readonly_planner(self):
        for operation,rollback in [('resume-plan',False),('rollback-plan',True)]:
            with patch.object(sys,'argv',['poc_services.py',operation]),patch.object(module,'Services') as factory,contextlib.redirect_stdout(io.StringIO()):
                factory.return_value.recovery_plan.return_value={'automatic_apply':False}
                self.assertEqual(module.main(),0)
                factory.return_value.recovery_plan.assert_called_once_with(rollback)
                factory.return_value.start.assert_not_called();factory.return_value.stop.assert_not_called()

if __name__=='__main__':unittest.main()
