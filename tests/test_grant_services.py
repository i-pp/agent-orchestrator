"""Grant service wiring with private fixtures and mocked managers; no real services."""
import contextlib
import io
import json
from pathlib import Path
import shlex
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
from mailbox_grants import GrantPolicy, GrantError, PURPOSE, DAY


class GrantServiceTests(unittest.TestCase):
    def setUp(self):
        (PROJECT/'.runtime').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=PROJECT/'.runtime',prefix='development-poc-grant-services-')
        self.root=Path(self.temp.name);(self.root/'logs').mkdir(mode=0o700)
        self.write(MARKER,{'schema':1,'mode':MODE,'runtime':str(self.root)})
        self.key_temp=tempfile.TemporaryDirectory(prefix='grant-reference-')
        self.key=Path(self.key_temp.name)/'existing-reference'
        self.key.write_text('synthetic-reference-only');self.key.chmod(0o600)
        helper=self.root/'helpers';helper.mkdir()
        for name in ['extension.py','selection_extension.py']:(helper/name).write_text('# offline fixture')
        self.write('local-llm.json',{'schema':1,'helper_directory':str(helper)})
        self.static_task='00000000-0000-4000-8000-000000000001'
        self.write('chat-mailbox-policy.json',{'schema':1,'allowed':[{
            'task_id':self.static_task,'request_id':'00000000-0000-4000-8000-000000000002',
            'request_sha256':digest('Non-secret static fixture')}]})
        with contextlib.closing(sqlite3.connect(self.root/'tasks.sqlite')) as db:
            db.execute('CREATE TABLE tasks(state TEXT NOT NULL)');db.commit()
        self.client=patch.object(module,'CLIENT',Path('/usr/bin/true'));self.client.start()
        self.metadata=patch.object(module,'credential_metadata',side_effect=module.private_file);self.metadata.start()
        self.service=self.configured()
        self.state={'schema':1,'runtime':str(self.root),'token':'a'*32,'units':self.service.units,
                    'invocations':{'daemon':'b'*32,'tunnel':'c'*32},'phase':'READY',
                    'credential_file_reference':str(self.key),'codex_file_reference':'/usr/bin/true',
                    'local_llm_enabled':True,'local_models_enabled':True,'chat_mailbox_enabled':True,
                    'mailbox_grants_enabled':True}
        self.save()
        self.props={role:{'LoadState':'loaded','ActiveState':'active','Transient':'yes',
                         'Description':self.service.description(self.state,role),
                         'InvocationID':self.state['invocations'][role]} for role in self.service.units}
        self.show=patch.object(Services,'show',side_effect=lambda name:self.props[next(k for k,v in self.service.units.items() if v==name)])
        self.show.start()
        self.manager=patch.object(Services,'call',side_effect=AssertionError('No service manager calls'));self.manager.start()
        self.grant()

    def tearDown(self):
        self.manager.stop();self.show.stop();self.metadata.stop();self.client.stop()
        self.key_temp.cleanup();self.temp.cleanup()

    def write(self,name,value):
        path=self.root/name;path.write_text(json.dumps(value));path.chmod(0o600)

    def save(self):self.write(STATE,self.state)

    def configured(self,grants=True):return Services(self.root,Path('/usr/bin/true'),self.key,True,True,True,grants)

    def grant(self,task_id='00000000-0000-4000-8000-000000000003'):
        self.policy=GrantPolicy.from_dict({
            'schema_version':1,'grant_id':'00000000-0000-4000-8000-000000000004',
            'task_id':task_id,'purpose':PURPOSE,'not_before':1,'expires_at':1+7*DAY,
            'request_ttl_seconds':DAY,'max_requests':32,'max_pending':8,
            'max_text_bytes':4096,'retain_seconds':7*DAY,'audit_retention_seconds':30*DAY})
        self.write('chat-mailbox-grant.json',{'schema_version':1,'enabled':True,
                  'policy':self.policy.metadata(),'policy_sha256':self.policy.sha256()})

    @staticmethod
    def adapter(service):
        argument=next(v for v in service.commands()['tunnel'] if v.startswith('--mcp.command='))
        return shlex.split(argument.split('command=',2)[-1])

    def test_default_off_and_legacy_mock_have_no_grant_argument(self):
        service=Services(self.root,Path('/usr/bin/true'),self.key,True,True,True)
        self.assertFalse(service.plan()['mailbox_grants_enabled'])
        self.assertNotIn('--allow-mailbox-grants',self.adapter(service))
        del service.allow_mailbox_grants
        self.assertNotIn('--allow-mailbox-grants',self.adapter(service))
        result=service.preflight(self.key)
        self.assertFalse(result['mailbox_grants_enabled']);self.assertEqual(len(result['tool_names']),11)

    def test_grant_flag_explicit_once_and_requires_mailbox(self):
        self.assertEqual(self.adapter(self.service).count('--allow-mailbox-grants'),1)
        with self.assertRaisesRegex(Refused,'require'):
            Services(self.root,Path('/usr/bin/true'),self.key,True,True,False,True)
        self.service.allow_chat_mailbox=False
        with self.assertRaisesRegex(Refused,'require'):self.service.commands()

    def test_expired_preflight_loads_pinned_config_without_any_ledger_creation(self):
        before={p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with patch('mailbox_grants.GrantLedger',side_effect=AssertionError('Preflight must not open ledger')):
            result=self.service.preflight(self.key)
        self.assertTrue(result['passed']);self.assertTrue(result['mailbox_grants_enabled'])
        self.assertEqual(result['mailbox_grant_policy_sha256'],self.policy.sha256())
        self.assertEqual(result['mailbox_grant_task_id'],self.policy.task_id)
        self.assertEqual(len(result['tool_names']),11)
        self.assertFalse(result['mailbox_grant_db_created'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        self.assertFalse((self.root/'mailbox-grants-prepared.sqlite').exists())
        self.assertFalse((self.root/'chat-mailbox.sqlite').exists())

    def test_static_task_collision_rejected_even_for_different_request(self):
        self.grant(self.static_task)
        with self.assertRaisesRegex(Refused,'collides'):self.service.preflight(self.key)

    def test_missing_disabled_and_hash_mismatch_fail_before_interruption(self):
        path=self.root/'chat-mailbox-grant.json';original=json.loads(path.read_text())
        for change in [{'enabled':False},{'policy_sha256':'0'*64}]:
            self.write(path.name,{**original,**change})
            with self.subTest(change=change),self.assertRaises(GrantError):self.service.preflight(self.key)
        path.unlink()
        with self.assertRaises((GrantError,OSError)):self.service.preflight(self.key)

    def test_existing_ledger_mismatch_rejected_before_interruption(self):
        from mailbox_grants import GrantLedger
        GrantLedger(self.root,self.policy,enabled=True)
        self.assertTrue(self.service.preflight(self.key)['passed'])
        original=self.policy.metadata();original['expires_at']-=1
        changed=GrantPolicy.from_dict(original)
        self.write('chat-mailbox-grant.json',{'schema_version':1,'enabled':True,
            'policy':changed.metadata(),'policy_sha256':changed.sha256()})
        with self.assertRaisesRegex(GrantError,'INTEGRITY_UNKNOWN'):self.service.preflight(self.key)

    def test_command_builder_cannot_drop_grant_flag(self):
        command=self.service.commands()
        command['tunnel']=[v.replace(' --allow-mailbox-grants','') for v in command['tunnel']]
        with patch.object(self.service,'commands',return_value=command):
            with self.assertRaisesRegex(Refused,'capability argument'):self.service.preflight(self.key)

    def test_resume_preserves_all_flags_references_and_eleven_tools(self):
        original=(self.root/STATE).read_bytes()
        result=self.service.recovery_plan()
        self.assertEqual(result['operation'],'resume-plan');self.assertFalse(result['automatic_apply'])
        self.assertEqual(len(result['preflight']['tool_names']),11)
        for op in ['preflight','start']:
            argv=result['commands'][op]
            for flag in ['--allow-local-llm','--allow-local-models','--allow-chat-mailbox','--allow-mailbox-grants']:
                self.assertIn(flag,argv)
            self.assertEqual(argv[argv.index('--key-file')+1],str(self.key))
            self.assertEqual(argv[argv.index('--codex')+1],'/usr/bin/true')
        self.assertEqual(original,(self.root/STATE).read_bytes())

    def test_grant_only_rollback_keeps_static_eleven_and_needs_no_grant_config(self):
        (self.root/'chat-mailbox-grant.json').unlink()
        result=self.service.recovery_plan(rollback_grants=True)
        self.assertEqual(result['operation'],'grant-rollback-plan')
        self.assertEqual(len(result['preflight']['tool_names']),11)
        self.assertIn('--allow-chat-mailbox',result['commands']['start'])
        self.assertNotIn('--allow-mailbox-grants',result['commands']['start'])
        self.assertTrue(json.loads((self.root/STATE).read_text())['mailbox_grants_enabled'])

    def test_full_rollback_keeps_old_seven_and_disables_both_flags(self):
        result=self.service.recovery_plan(True)
        self.assertEqual(result['operation'],'rollback-plan')
        self.assertEqual(len(result['preflight']['tool_names']),7)
        for flag in ['--allow-chat-mailbox','--allow-mailbox-grants']:
            self.assertNotIn(flag,result['commands']['start'])

    def test_old_state_missing_grant_flag_is_safe_off_and_malformed_is_refused(self):
        del self.state['mailbox_grants_enabled'];self.save()
        self.assertNotIn('--allow-mailbox-grants',self.service.recovery_plan()['commands']['start'])
        self.state['mailbox_grants_enabled']='true';self.save()
        with self.assertRaisesRegex(Refused,'invalid'):self.service.read_state()
        self.state['mailbox_grants_enabled']=True;self.state['chat_mailbox_enabled']=False;self.save()
        with self.assertRaisesRegex(Refused,'invalid'):self.service.read_state()

    def test_mocked_start_persists_explicit_grant_state(self):
        for enabled in [True,False]:
            self.state['phase']='STOPPED';self.save()
            self.service.allow_mailbox_grants=enabled
            self.props={role:{'LoadState':'not-found'} for role in self.service.units}
            with self.subTest(enabled=enabled),patch.object(module,'endpoint',side_effect=FileNotFoundError),patch.object(Services,'launch') as launch,patch.object(Services,'wait_ready'):
                self.assertEqual(self.service.start()['phase'],'READY')
            self.assertEqual(launch.call_count,2)
            saved=self.service.read_state();self.assertIs(saved['mailbox_grants_enabled'],enabled)
            self.assertEqual(saved['credential_file_reference'],str(self.key))
            self.assertEqual(saved['codex_file_reference'],'/usr/bin/true')

    def test_unknown_work_and_generation_guards_still_block_without_manager_calls(self):
        with contextlib.closing(sqlite3.connect(self.root/'tasks.sqlite')) as db:
            db.execute('INSERT INTO tasks VALUES(?)',('UNKNOWN',));db.commit()
        with self.assertRaisesRegex(Refused,'Unresolved'):self.service.recovery_plan()
        self.state['phase']='CLEANUP_UNCONFIRMED';self.save()
        with self.assertRaisesRegex(Refused,'uncertain'):self.service.recovery_plan()

    def test_cli_grant_flag_and_rollback_route_without_service_mutation(self):
        argv=['poc_services.py','preflight','--allow-chat-mailbox','--allow-mailbox-grants']
        with patch.object(sys,'argv',argv),patch.object(module,'Services') as factory,contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.preflight.return_value={'passed':True}
            self.assertEqual(module.main(),0)
            self.assertEqual(factory.call_args.kwargs,{'allow_mailbox_grants':True})
        with patch.object(sys,'argv',['poc_services.py','grant-rollback-plan']),patch.object(module,'Services') as factory,contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.recovery_plan.return_value={'automatic_apply':False}
            self.assertEqual(module.main(),0)
            factory.return_value.recovery_plan.assert_called_once_with(rollback_grants=True)
            factory.return_value.start.assert_not_called();factory.return_value.stop.assert_not_called()


if __name__=='__main__':unittest.main()
