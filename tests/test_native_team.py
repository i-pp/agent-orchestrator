"""Requirements tests for durable native admission; no model/service/network use."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'src'))
from native_team import NativeTeam,registry,sha

POLICY={'version':'fixture-v1','sha256':'a'*64}
VERIFIED={'commands':['independent fixture check'],'exit_codes':[0]}

class NativeTeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='native-team-unit-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.project=self.root/'project';self.project.mkdir()
        self.git(self.project,'init','-q')
        (self.project/'seed.txt').write_text('immutable input\n')
        self.git(self.project,'add','seed.txt')
        self.git(self.project,'-c','user.name=fixture','-c','user.email=fixture@invalid','commit','-qm','seed')
        self.workspaces={}
        for name in ['a','b','third','merge']:
            p=self.root/name
            self.git(self.project,'worktree','add','--quiet','--detach',str(p),'HEAD');self.workspaces[name]=p
        self.clock=[1000.0]
        self.loader=patch('native_team.load_project_policy',return_value=SimpleNamespace(**POLICY));self.loader.start();self.addCleanup(self.loader.stop)
        self.spec={'schema':1,'goal':'bounded requirement checks','requirements':['durable admission','independent worktrees'],
            'project':str(self.project),'policy':POLICY.copy(),'registry':registry(),
            'tasks':[self.task('a'),self.task('b'),self.task('third'),self.task('merge',['a','b'])]}
        self.team=NativeTeam.create(self.root/'run',self.spec,clock=lambda:self.clock[0])
    @staticmethod
    def git(path,*args):
        return subprocess.run(['git','-C',str(path),*args],check=True,capture_output=True,text=True,timeout=10).stdout
    def task(self,name,deps=(),backend='cloud'):
        return {'id':name,'role':'Implementer','backend':backend,'deps':list(deps),'workspace':str(self.workspaces[name]),
                'instruction':'Complete only '+name,'allowed':['result.txt']}
    def admit(self,name='a'):
        t=self.team.admit(name,'fixture coordinator');self.team.bind(name,t['dispatch_id'],'agent-'+name);return t
    def finish(self,name,ticket,success=True,verification=None):
        if success:(self.workspaces[name]/'result.txt').write_text('verified '+name+'\n')
        return self.team.complete(name,ticket['dispatch_id'],'agent-'+name,success,'observed terminal',verification or VERIFIED)
    def reject_unchanged(self,call,pattern=None):
        before=self.team.path.read_bytes()
        with self.assertRaisesRegex((ValueError,KeyError),pattern or '.*'):call()
        self.assertEqual(self.team.path.read_bytes(),before)
    def test_admission_precedes_spawn_and_reload_preserves_native_binding(self):
        t=self.admit();reloaded=NativeTeam(self.root/'run',clock=lambda:self.clock[0]);v=reloaded.snapshot()
        self.assertEqual(v['plan']['tasks']['a']['state'],'RUNNING')
        self.assertEqual(v['bindings']['a']['agent_id'],'agent-a');self.assertEqual(v['plan']['dispatches'],1)
        self.assertEqual(v['bindings']['a']['dispatch_id'],t['dispatch_id'])
        self.assertFalse(v['bindings']['a']['released']);self.assertFalse(v['automatic_dispatch'])
        self.assertEqual(self.team.path.stat().st_mode&0o777,0o600)
    def test_dependency_rejects_until_both_predecessors_confirmed(self):
        self.reject_unchanged(lambda:self.team.admit('merge','parent'))
        a=self.admit('a');self.finish('a',a)
        self.reject_unchanged(lambda:self.team.admit('merge','parent'))
        b=self.admit('b');self.finish('b',b)
        self.assertTrue(self.team.admit('merge','parent')['dispatch_id'])
    def test_cycles_unknown_dependencies_and_over_budget_rejected_at_creation(self):
        variants=[]
        cyclic=deepcopy(self.spec);cyclic['tasks'][0]['deps']=['merge'];variants.append(cyclic)
        bad=deepcopy(self.spec);bad['tasks'][0]['deps']=['absent'];variants.append(bad)
        budget=deepcopy(self.spec);budget['registry']['budgets']['cloud_concurrency']=3;variants.append(budget)
        for i,spec in enumerate(variants):
            with self.subTest(i=i),self.assertRaises(ValueError):NativeTeam.create(self.root/('bad-'+str(i)),spec)
    def test_two_slots_and_same_workspace_rejection(self):
        self.admit('a');self.admit('b')
        self.reject_unchanged(lambda:self.team.admit('third','third'),'CONCURRENCY')
        invalid=deepcopy(self.spec);invalid['tasks'][1]['workspace']=invalid['tasks'][0]['workspace']
        with self.assertRaisesRegex(ValueError,'WORKSPACES'):NativeTeam.create(self.root/'duplicate',invalid)
    def test_local_backend_disabled_and_dirty_worktree_not_admitted(self):
        spec=deepcopy(self.spec);spec['tasks'][0]['backend']='local'
        box=NativeTeam.create(self.root/'local',spec,clock=lambda:self.clock[0])
        with self.assertRaisesRegex(ValueError,'BACKEND_DISABLED'):box.admit('a','local')
        (self.workspaces['a']/'unrelated.txt').write_text('preserve')
        self.reject_unchanged(lambda:self.team.admit('a','worker'),'DIRTY_WORKTREE')
        self.assertEqual((self.workspaces['a']/'unrelated.txt').read_text(),'preserve')
    def test_native_binding_and_dispatch_ticket_cannot_be_replaced(self):
        t=self.admit()
        self.reject_unchanged(lambda:self.team.bind('a','wrong-ticket','agent-a'),'TICKET')
        self.reject_unchanged(lambda:self.team.bind('a',t['dispatch_id'],'other-agent'),'CONFLICT')
        self.reject_unchanged(lambda:self.team.complete('a',t['dispatch_id'],'other-agent',False,'not observed',VERIFIED),'TERMINAL')
    def test_completion_receipt_pins_artifact_revision_and_verification(self):
        t=self.admit();receipt=self.finish('a',t)
        self.assertEqual(receipt['artifacts']['result.txt'],hashlib.sha256(b'verified a\n').hexdigest())
        self.assertEqual(receipt['verification'],VERIFIED)
        self.assertEqual(receipt['input_revision'],receipt['output_revision'])
        v=self.team.snapshot();self.assertEqual(v['plan']['tasks']['a']['state'],'SUCCEEDED')
        self.assertTrue(v['bindings']['a']['released']);self.assertTrue(receipt['native_terminal_acknowledged'])
        self.reject_unchanged(lambda:self.finish('a',t),'TERMINAL')
    def test_failed_validation_and_out_of_scope_change_cannot_succeed(self):
        t=self.admit();(self.workspaces['a']/'result.txt').write_text('candidate')
        self.reject_unchanged(lambda:self.finish('a',t,verification={'commands':['failing check'],'exit_codes':[1]}),'VERIFICATION')
        (self.workspaces['a']/'unrelated.txt').write_text('not owned')
        self.reject_unchanged(lambda:self.finish('a',t),'OUT_OF_SCOPE')
    def test_unknown_holds_slot_until_native_release_and_never_retries(self):
        a=self.admit('a');self.admit('b');self.team.unknown('a',a['dispatch_id'],'lost acknowledgement')
        self.reject_unchanged(lambda:self.team.admit('third','third'),'CONCURRENCY')
        self.reject_unchanged(lambda:self.team.retry('a','KNOWN_CAUSE'))
        self.reject_unchanged(lambda:self.team.acknowledge_release('a',a['dispatch_id'],'wrong','terminal'),'RECONCILIATION')
        self.team.acknowledge_release('a',a['dispatch_id'],'agent-a','standard tool confirms terminal')
        self.assertTrue(self.team.admit('third','third')['dispatch_id'])
        self.assertEqual(self.team.snapshot()['plan']['tasks']['a']['state'],'UNKNOWN')
        self.reject_unchanged(lambda:self.team.retry('a','KNOWN_CAUSE'))
    def test_timeout_keeps_slot_and_late_success_is_not_adopted(self):
        a=self.admit('a');self.admit('b');self.clock[0]+=300
        self.assertEqual(set(self.team.expire()),{'a','b'})
        self.reject_unchanged(lambda:self.team.admit('third','third'),'CONCURRENCY')
        self.assertFalse(self.team.snapshot()['bindings']['a']['released'])
        self.reject_unchanged(lambda:self.finish('a',a))
        self.team.acknowledge_release('a',a['dispatch_id'],'agent-a','interrupt terminal observed')
        self.reject_unchanged(lambda:self.team.retry('a','TIMEOUT'))
    def test_retries_require_known_failure_new_cause_and_respect_attempt_limit(self):
        a=self.admit('a');self.finish('a',a,False);self.team.retry('a','FIXED_INPUT')
        a=self.admit('a');self.finish('a',a,False)
        self.reject_unchanged(lambda:self.team.retry('a','FIXED_INPUT'),'new retry cause')
        self.team.retry('a','OTHER_FIXED_CAUSE');a=self.admit('a');self.finish('a',a,False)
        self.reject_unchanged(lambda:self.team.retry('a','THIRD_CAUSE'),'retry budget')
    def test_clock_regression_and_run_deadline_do_not_dispatch(self):
        a=self.admit('a');self.finish('a',a,False)
        self.clock[0]=999;self.reject_unchanged(lambda:self.team.admit('b','worker'),'CLOCK_REGRESSED')
        self.clock[0]=1900;self.reject_unchanged(lambda:self.team.admit('b','worker'),'deadline')
    def test_corrupted_envelope_and_immutable_task_are_refused(self):
        original=self.team.path.read_bytes();envelope=json.loads(original)
        envelope['data']['plan']['tasks']['a']['instruction']='altered';self.team.path.write_text(json.dumps(envelope));self.team.path.chmod(0o600)
        with self.assertRaisesRegex(ValueError,'HASH'):self.team.snapshot()
        envelope['sha256']=sha(envelope['data']);self.team.path.write_text(json.dumps(envelope));self.team.path.chmod(0o600)
        with self.assertRaisesRegex(ValueError,'IMMUTABLE'):self.team.snapshot()
        self.team.path.write_bytes(original)
    def test_policy_pin_change_and_escalation_block_dependent_execution(self):
        with patch('native_team.load_project_policy',return_value=SimpleNamespace(version='changed',sha256='b'*64)):
            self.reject_unchanged(lambda:self.team.admit('a','worker'),'POLICY_PIN')
        self.team.escalate('a','architecture decision required')
        self.assertEqual(self.team.snapshot()['plan']['tasks']['a']['state'],'WAITING_FOR_HUMAN')
        self.reject_unchanged(lambda:self.team.admit('a','worker'))
        self.reject_unchanged(lambda:self.team.admit('merge','parent'))
    def test_atomic_save_failure_preserves_prior_state_and_cleans_temporary_file(self):
        before=self.team.path.read_bytes()
        with patch('native_team.os.replace',side_effect=OSError('fixture before replacement')):
            with self.assertRaises(OSError):self.team.admit('a','worker')
        self.assertEqual(self.team.path.read_bytes(),before)
        self.assertEqual(list(self.team.root.glob('state-*.tmp')),[])
    def test_total_dispatch_budget_cannot_be_bypassed_by_releasing_a_slot(self):
        spec=deepcopy(self.spec);spec['registry']['budgets']['max_dispatches']=1
        box=NativeTeam.create(self.root/'limited',spec,clock=lambda:self.clock[0])
        t=box.admit('a','worker');box.bind('a',t['dispatch_id'],'agent-a')
        box.complete('a',t['dispatch_id'],'agent-a',False,'known failure',VERIFIED)
        before=box.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'dispatch budget'):box.admit('b','worker')
        self.assertEqual(box.path.read_bytes(),before)
    def test_foreign_git_repository_is_not_accepted_as_a_shared_worktree(self):
        foreign=self.root/'foreign';foreign.mkdir();self.git(foreign,'init','-q')
        spec=deepcopy(self.spec);spec['tasks'][0]['workspace']=str(foreign)
        box=NativeTeam.create(self.root/'foreign-run',spec,clock=lambda:self.clock[0]);before=box.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'EXPECTED_GIT_WORKTREE'):box.admit('a','worker')
        self.assertEqual(box.path.read_bytes(),before)

    def test_alias_run_parent_rejected_before_creating_any_directory(self):
        alias=self.root/'alias';alias.symlink_to(self.root,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'symlink'):NativeTeam.create(alias/'unexpected-run',self.spec)
        self.assertFalse((self.root/'unexpected-run').exists())

class InteractiveCliTests(unittest.TestCase):
    def test_read_only_terminal_commands_do_not_wait_for_eof(self):
        import importlib.util
        import io
        spec=importlib.util.spec_from_file_location('native_team_cli_fixture',PROJECT/'scripts/native_team.py')
        cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        class Terminal:
            def isatty(self):return True
            def read(self,size):raise AssertionError('Interactive show/expire must not read stdin')
        for operation in ['show','expire']:
            out=io.StringIO()
            with patch.object(cli,'invoke',return_value={}) as called:
                self.assertEqual(cli.main([operation,'--run','/unused'],Terminal(),out),0)
                self.assertEqual(called.call_args.args[-1],{})
                self.assertEqual(json.loads(out.getvalue())['status'],'OK')

if __name__=='__main__':unittest.main()
