"""Login entry fixtures only: no manager operations, live sockets, or model calls."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'src'),str(PROJECT/'scripts'),str(PROJECT/'tests')]
import poc_login as login
import poc_services as services
import test_grant_services as fixtures
from mailbox_grants import GrantLedger

class LoginTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.GrantServiceTests('test_default_off_and_legacy_mock_have_no_grant_argument')
        self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.s=self.fixture.service;self.root=self.fixture.root
        self.patchers=[patch.object(login,'endpoint',return_value=self.root/'synthetic-control.sock'),
                       patch.object(services,'daemon_ready',return_value=True), \
                       patch.object(services,'tunnel_ready',return_value=True)]
        for p in self.patchers:p.start();self.addCleanup(p.stop)
        self.pin=login.snapshot(self.s)[2]
    def absent(self,phase='READY'):
        self.fixture.state['phase']=phase;self.fixture.save()
        self.fixture.props={r:{'LoadState':'not-found','ActiveState':'inactive'} for r in self.s.units}
    def become_ready(self):
        self.fixture.state=self.s.read_state();self.fixture.state['phase']='READY'
        self.fixture.state['invocations']={'daemon':'d'*32,'tunnel':'e'*32};self.fixture.save()
        self.fixture.props={r:{'LoadState':'loaded','ActiveState':'active','Transient':'yes',
          'Description':self.s.description(self.fixture.state,r),'InvocationID':self.fixture.state['invocations'][r]}
          for r in self.s.units}
        return {'phase':'READY'}
    def test_plan_and_render_only_read_and_pin_references_flags_policies(self):
        before={p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        v=login.plan(self.s);text=login.render_unit(PROJECT,self.root,v['expected_invocation_sha256'])
        self.assertFalse(v['installation_performed']);self.assertFalse(v['enable_performed'])
        self.assertEqual(v['stops_or_launches'],0);self.assertIn('WantedBy=default.target',text)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        self.assertNotIn('synthetic-reference-only',json.dumps(v)+text)
        self.assertFalse((self.root/'mailbox-grants-prepared.sqlite').exists())
    def test_unit_is_one_shot_no_restart_no_stop_no_linger_and_finite_timeout(self):
        text=login.render_unit(PROJECT,self.root,self.pin)
        for field in ['Type=oneshot','RemainAfterExit=yes','Restart=no','TimeoutStartSec=240',
                      'After=basic.target','UMask=0077','/usr/bin/env -i','--apply']:
            self.assertIn(field,text)
        active='\n'.join(s for s in text.splitlines() if not s.startswith('#'))
        for forbidden in ['ExecStop=','ExecStopPost=','Restart=on-failure','loginctl','LoadCredential=',
                          'After=default.target','--allow-writes','enable-linger']:
            self.assertNotIn(forbidden,active)
    def test_renderer_quotes_spaces_specifiers_and_dollars_without_shell(self):
        project=Path('/tmp/a space %i $VAR "quote"')
        text=login.render_unit(project,project/'.runtime/development-poc-test',self.pin)
        self.assertIn('%%i',text);self.assertIn('$$VAR',text);self.assertIn('\\"quote\\"',text)
        self.assertNotIn('/bin/sh',text)
        for bad in ['/tmp/a\nline','/tmp/a\x00line']:
            with self.assertRaises(login.LoginBlocked):login.render_unit(Path(bad),Path(bad)/'.runtime/x',self.pin)
    def test_unit_does_not_pass_shell_quotes_as_a_working_directory_path(self):
        import configparser
        unit=configparser.RawConfigParser()
        unit.read_string(login.render_unit(PROJECT,self.root,self.pin))
        working=unit['Service'].get('WorkingDirectory')
        if working is not None:self.assertTrue(Path(working).is_absolute(),working)
        self.assertIn(str(PROJECT/'scripts/poc_login.py'),unit['Service']['ExecStart'])
    def test_pin_or_scope_change_blocks_before_any_manager_operation(self):
        with patch.object(self.s,'status',side_effect=AssertionError('Must not reach manager')):
            with self.assertRaisesRegex(login.LoginBlocked,'PIN_MISMATCH'):login.restore(self.s,'0'*64)
            self.fixture.state['local_models_enabled']=False;self.fixture.save()
            with self.assertRaisesRegex(login.LoginBlocked,'SCOPE_CHANGED'):login.restore(self.s,self.pin)
    def test_changed_fixed_policy_changes_fingerprint_without_scope_install(self):
        initial=self.pin
        p=self.root/'chat-mailbox-policy.json';v=json.loads(p.read_text());v['allowed'][0]['request_sha256']='0'*64
        self.fixture.write(p.name,v)
        self.assertNotEqual(login.snapshot(self.s)[2],initial)
        with self.assertRaisesRegex(login.LoginBlocked,'PIN_MISMATCH'):login.restore(self.s,initial)
    def test_already_ready_does_not_preflight_stop_or_restart_even_with_active_work(self):
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.root/'tasks.sqlite')) as db:
            db.execute('INSERT INTO tasks VALUES(?)',('RUNNING',));db.commit()
        with patch.object(self.s,'preflight',side_effect=AssertionError('Do not interrupt active work')), \
             patch.object(self.s,'start',side_effect=AssertionError('No launch')), \
             patch.object(self.s,'stop',side_effect=AssertionError('No stop')):
            self.assertEqual(login.restore(self.s,self.pin)['status'],'ALREADY_READY')
    def test_lost_manager_ready_record_reconciled_then_one_start_without_stop(self):
        self.absent()
        with patch.object(self.s,'start',side_effect=self.become_ready) as start,patch.object(self.s,'stop',side_effect=AssertionError('Never stop')):
            result=login.restore(self.s,self.pin)
        self.assertEqual(result['status'],'READY');self.assertEqual(start.call_count,1)
        self.assertEqual(self.s.read_state()['phase'],'READY')
        self.assertFalse((self.root/'mailbox-grants-prepared.sqlite').exists())
    def test_stopped_record_is_reused_and_budget_policy_not_reset(self):
        self.absent('STOPPED');box=GrantLedger(self.root,self.fixture.policy,enabled=True)
        before=box.status()
        with patch.object(self.s,'start',side_effect=self.become_ready):result=login.restore(self.s,self.pin)
        self.assertEqual(result['status'],'READY');self.assertEqual(box.status(),before)
    def test_incomplete_unknown_failed_states_block_without_cleanup_or_start(self):
        for phase in ['STARTING','CLEANUP_UNCONFIRMED','FAILED']:
            self.absent(phase)
            with self.subTest(phase=phase),patch.object(self.s,'start',side_effect=AssertionError('No retry')), \
                 patch.object(self.s,'stop',side_effect=AssertionError('No cleanup of old owner')):
                with self.assertRaisesRegex(login.LoginBlocked,'RECONCILIATION'):login.restore(self.s,self.pin)
    def test_partial_or_foreign_units_block_without_stopping_them(self):
        self.absent();self.fixture.props['daemon']={'LoadState':'loaded','ActiveState':'active','Transient':'no'}
        with patch.object(self.s,'start',side_effect=AssertionError('No launch')),patch.object(self.s,'stop',side_effect=AssertionError('No stop')):
            with self.assertRaisesRegex(login.LoginBlocked,'EXISTING_UNIT'):login.restore(self.s,self.pin)
    def test_missing_health_of_owned_service_does_not_restart_it(self):
        with patch.object(services,'tunnel_ready',return_value=False),patch.object(self.s,'start',side_effect=AssertionError('No restart')):
            with self.assertRaisesRegex(login.LoginBlocked,'EXISTING_UNIT'):login.restore(self.s,self.pin)
    def test_unresolved_work_blocks_absent_unit_start_and_preserves_ready_record(self):
        import sqlite3
        for state in ['QUEUED','RUNNING','VERIFYING','UNKNOWN','WAITING_FOR_HUMAN']:
            self.absent()
            with contextlib.closing(sqlite3.connect(self.root/'tasks.sqlite')) as db:
                db.execute('DELETE FROM tasks');db.execute('INSERT INTO tasks VALUES(?)',(state,));db.commit()
            with self.subTest(state=state),patch.object(self.s,'start',side_effect=AssertionError('No launch')):
                with self.assertRaises(services.Refused):login.restore(self.s,self.pin)
            self.assertEqual(self.s.read_state()['phase'],'READY')
    def test_socket_or_health_locator_is_not_removed_or_overwritten(self):
        self.absent()
        for path in [self.root/'synthetic-control.sock',self.s.health]:
            path.write_text('preserved')
            with self.subTest(name=path.name),patch.object(self.s,'start',side_effect=AssertionError('No launch')):
                with self.assertRaisesRegex(login.LoginBlocked,'RECONCILIATION'):login.restore(self.s,self.pin)
            self.assertEqual(path.read_text(),'preserved');path.unlink()
    def test_state_changed_while_locking_is_rechecked(self):
        self.absent();original=self.s.locked
        @contextlib.contextmanager
        def changed():
            with original():
                self.fixture.state['phase']='CLEANUP_UNCONFIRMED';self.fixture.save();yield
        # Initial status also locks, so use an explicit observed absent snapshot.
        with patch.object(self.s,'status',return_value={'phase':'STALE_OR_STOPPED'}),patch.object(self.s,'locked',changed),patch.object(self.s,'start',side_effect=AssertionError('No launch')):
            with self.assertRaisesRegex(login.LoginBlocked,'RECONCILIATION'):login.restore(self.s,self.pin)
    def test_late_concurrent_owner_is_never_stopped_or_retried(self):
        self.absent()
        def race():self.become_ready();raise services.Refused('Owner appeared')
        with patch.object(self.s,'start',side_effect=race) as start,patch.object(self.s,'stop',side_effect=AssertionError('Do not stop new owner')):
            result=login.restore(self.s,self.pin)
        self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(start.call_count,1)
        self.assertEqual(self.s.read_state()['phase'],'READY')
    def test_existing_start_failure_rolls_back_only_owned_generation_without_retry(self):
        for fail_cleanup in [False,True]:
            self.absent('STOPPED')
            error=services.Refused('fixture startup failure')
            with self.subTest(cleanup_failure=fail_cleanup),patch.object(self.s,'launch') as launch, \
                 patch.object(self.s,'wait_ready',side_effect=[None,error]), \
                 patch.object(self.s,'stop_owned',side_effect=services.Refused('fixture cleanup failure') if fail_cleanup else None) as cleanup:
                result=login.restore(self.s,self.pin)
            self.assertEqual(launch.call_count,2);self.assertEqual(cleanup.call_count,1)
            self.assertEqual(result['status'],'UNKNOWN' if fail_cleanup else 'BLOCKED')
            self.assertEqual(self.s.read_state()['phase'],'CLEANUP_UNCONFIRMED' if fail_cleanup else 'FAILED')
            with patch.object(self.s,'start',side_effect=AssertionError('No automatic retry')):
                with self.assertRaises(login.LoginBlocked):login.restore(self.s,self.pin)
    def test_cli_requires_apply_and_sanitizes_reference_errors(self):
        for argv,code in [(['restore'],'EXPLICIT_APPLY_REQUIRED'),(['plan','--apply'],'INVALID_APPLY_OPTION')]:
            out=io.StringIO()
            with contextlib.redirect_stdout(out):rc=login.main(argv)
            self.assertEqual(rc,2);self.assertEqual(json.loads(out.getvalue())['code'],code)
        out=io.StringIO()
        with patch.object(login,'configured',side_effect=ValueError('private-reference-sentinel')),contextlib.redirect_stdout(out):rc=login.main(['plan'])
        self.assertEqual(rc,2);self.assertNotIn('private-',out.getvalue())
    def test_lost_state_after_start_attempt_stays_unknown_without_retry(self):
        self.absent();original=self.s.read_state
        failed=[False]
        def read():
            if failed[0]:raise ValueError('private-state-sentinel')
            return original()
        def start():failed[0]=True;raise services.Refused('fixture acknowledgement lost')
        with patch.object(self.s,'read_state',side_effect=read),patch.object(self.s,'start',side_effect=start) as called:
            result=login.restore(self.s,self.pin)
        self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(called.call_count,1)
        self.assertNotIn('private-',json.dumps(result))
    def test_health_failure_after_start_is_unknown_and_does_not_stop_services(self):
        self.absent();initial=self.s.status
        attempted=[False]
        def status():
            if attempted[0]:raise OSError('private-readiness-sentinel')
            return initial()
        def start():attempted[0]=True;return self.become_ready()
        with patch.object(self.s,'start',side_effect=start) as called,patch.object(self.s,'status',side_effect=status),patch.object(self.s,'stop',side_effect=AssertionError('No stop')):
            result=login.restore(self.s,self.pin)
        self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(called.call_count,1)
        self.assertNotIn('private-',json.dumps(result))

if __name__=='__main__':unittest.main()
