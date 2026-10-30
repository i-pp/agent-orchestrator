"""Offline parent-action fixtures only; no actual native model/namespace/socket."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'src'),str(PROJECT/'tests')]
import test_native_team as core_fixtures
from native_team import NativeTeam
from native_team_artifacts import integrate
from native_dispatcher import NativeDispatcher,DispatchError
from storage import runtime_lock

VALIDATION={'commands':['fixture acceptance check'],'exit_codes':[0]}

class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.f=core_fixtures.NativeTeamTests('test_admission_precedes_spawn_and_reload_preserves_native_binding')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.clock=self.f.clock;self.spec=deepcopy(self.f.spec);self.spec['tasks']=self.spec['tasks'][:2]
        self.d=NativeDispatcher.receive(self.f.root/'dispatch-run',self.spec,'request-v1',clock=lambda:self.clock[0])
        self.spawn_calls=0
    def snapshot(self):return self.d.team.snapshot()
    def take(self,kind=None):
        self.d.advance();a=self.d.take()
        return a if kind is None else [x for x in a if x['kind']==kind]
    def spawn(self):
        actions=self.take('SPAWN')
        for a in actions:
            v=self.snapshot();self.assertEqual(v['dispatcher']['actions'][a['action_id']]['status'],'ISSUED')
            self.assertEqual(v['plan']['tasks'][a['task_id']]['state'],'RUNNING')
            self.spawn_calls+=1;self.d.observe(a['action_id'],{'agent_id':'native-'+a['task_id']})
        return actions
    def polls(self):return self.take('POLL')
    def terminal(self,a,state='SUCCEEDED'):
        self.d.observe(a['action_id'],{'agent_id':a['agent_id'],'state':state})
    def result(self,a,accepted=True):
        task=self.snapshot()['plan']['tasks'][a['task_id']]
        for name in task['allowed']:
            p=Path(task['workspace'])/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('accepted '+a['task_id'])
        return {'agent_id':a['agent_id'],'accepted':accepted,'reason':'fixture result verified',
                'verification':VALIDATION if accepted else {'commands':['fixture acceptance check'],'exit_codes':[1]}}
    def test_receive_is_idempotent_and_conflicting_key_or_spec_is_rejected(self):
        before=self.snapshot()
        again=NativeDispatcher.receive(self.d.team.root,self.spec,'request-v1',clock=lambda:self.clock[0])
        self.assertEqual(again.snapshot(),before)
        for key,spec in [('other',self.spec),('request-v1',{**self.spec,'goal':'changed'})]:
            with self.assertRaisesRegex(DispatchError,'SUBMISSION_CONFLICT'):NativeDispatcher.receive(self.d.team.root,spec,key)
        self.assertEqual(self.snapshot(),before)
    def test_existing_plain_team_is_not_silently_adopted(self):
        with self.assertRaisesRegex(DispatchError,'RECONCILIATION'):NativeDispatcher(self.f.team)
    def test_admit_and_issue_precede_fake_spawn_and_repeated_advance_never_respawns(self):
        actions=self.take('SPAWN');self.assertEqual(len(actions),2)
        for _ in range(3):self.d.advance();self.assertEqual(self.d.take(),[])
        resumed=NativeDispatcher(NativeTeam(self.d.team.root,clock=lambda:self.clock[0]))
        resumed.advance();self.assertEqual(resumed.take(),[])
        self.assertEqual(self.snapshot()['plan']['dispatches'],2)
        for a in actions:resumed.observe(a['action_id'],{'agent_id':'native-'+a['task_id']})
        self.assertTrue(all(b['agent_id'] for b in self.snapshot()['bindings'].values()))
    def test_duplicate_ack_does_not_bind_twice_and_conflicting_ack_is_rejected(self):
        a=self.take('SPAWN')[0];event={'agent_id':'actual-id'};self.d.observe(a['action_id'],event)
        before=self.snapshot();self.assertTrue(self.d.observe(a['action_id'],event)['duplicate'])
        self.assertEqual(self.snapshot(),before)
        with self.assertRaisesRegex(DispatchError,'CONFLICT'):self.d.observe(a['action_id'],{'agent_id':'other-id'})
    def test_wrong_id_or_unknown_fields_are_rejected_before_recording_private_body(self):
        self.spawn();a=self.polls()[0];before=self.snapshot()
        for event in [{'agent_id':'wrong','state':'SUCCEEDED'}, {'agent_id':a['agent_id'],'state':'RUNNING','body':'private-sentinel'}]:
            with self.assertRaises(DispatchError):self.d.observe(a['action_id'],event)
        self.assertEqual(self.snapshot(),before);self.assertNotIn('private-sentinel',self.d.team.path.read_text())
    def test_lost_bind_ack_replays_local_correlation_without_another_spawn(self):
        a=self.take('SPAWN')[0];original=self.d.team.bind
        def lost(*args):original(*args);raise OSError('private lost acknowledgment')
        with patch.object(self.d.team,'bind',side_effect=lost):
            with self.assertRaises(OSError):self.d.observe(a['action_id'],{'agent_id':'actual-id'})
        resumed=NativeDispatcher(NativeTeam(self.d.team.root,clock=lambda:self.clock[0]));resumed.advance()
        self.assertEqual(resumed.snapshot()['bindings'][a['task_id']]['agent_id'],'actual-id')
        self.assertFalse(any(x['kind']=='SPAWN' for x in resumed.take()))
        self.assertEqual(resumed.snapshot()['plan']['dispatches'],2)
    def test_native_terminal_waits_for_acceptance_then_receipt_releases_slot(self):
        self.spawn();poll=self.polls()[0];self.terminal(poll)
        v=self.snapshot();self.assertEqual(v['plan']['tasks'][poll['task_id']]['state'],'RUNNING')
        self.assertFalse(v['bindings'][poll['task_id']]['released'])
        a=self.take('VERIFY')[0];self.d.observe(a['action_id'],self.result(a))
        v=self.snapshot();self.assertEqual(v['plan']['tasks'][a['task_id']]['state'],'SUCCEEDED')
        self.assertTrue(v['bindings'][a['task_id']]['released']);self.assertTrue(v['receipts'][a['task_id']]['artifacts'])
    def test_lost_complete_ack_reconciles_saved_receipt_without_second_completion(self):
        self.spawn();self.terminal(self.polls()[0]);a=self.take('VERIFY')[0];event=self.result(a);original=self.d.team.complete
        def lost(*args):original(*args);raise OSError('private after commit')
        with patch.object(self.d.team,'complete',side_effect=lost) as called:
            with self.assertRaises(OSError):self.d.observe(a['action_id'],event)
            self.assertEqual(called.call_count,1)
        resumed=NativeDispatcher(NativeTeam(self.d.team.root,clock=lambda:self.clock[0]))
        with patch.object(resumed.team,'complete',side_effect=AssertionError('No second completion')):resumed.advance()
        self.assertEqual(resumed.snapshot()['plan']['tasks'][a['task_id']]['state'],'SUCCEEDED')
    def test_timeout_interrupt_is_once_and_acceptance_is_not_terminal_release(self):
        self.spawn();self.clock[0]+=301;self.d.advance();actions=self.d.take()
        interrupts=[a for a in actions if a['kind']=='INTERRUPT'];polls=[a for a in actions if a['kind']=='POLL']
        self.assertEqual(len(interrupts),2)
        for a in interrupts:self.d.observe(a['action_id'],{'agent_id':a['agent_id'],'accepted':True})
        self.assertTrue(all(not b['released'] for b in self.snapshot()['bindings'].values()))
        for a in polls:self.terminal(a,'CANCELLED')
        self.clock[0]+=2;self.d.advance();self.assertFalse(any(a['kind']=='INTERRUPT' for a in self.d.take()))
        self.assertTrue(all(b['released'] for b in self.snapshot()['bindings'].values()))
        self.assertTrue(all(t['state']=='TIMED_OUT' for t in self.snapshot()['plan']['tasks'].values()))
    def test_unknown_does_not_relaunch_or_immediately_interrupt_and_terminal_only_releases(self):
        self.spawn();poll=self.polls()[0];self.terminal(poll,'UNKNOWN');self.clock[0]+=2
        actions=self.take();self.assertFalse(any(a['kind'] in ['SPAWN','INTERRUPT'] for a in actions))
        p=next(a for a in actions if a['kind']=='POLL' and a['task_id']==poll['task_id']);self.terminal(p)
        v=self.snapshot();self.assertEqual(v['plan']['tasks'][p['task_id']]['state'],'UNKNOWN');self.assertTrue(v['bindings'][p['task_id']]['released'])
        self.assertEqual(v['plan']['dispatches'],2)
    def test_orphaned_admission_is_unknown_without_spawn(self):
        self.d.team.admit('a','parent before journal checkpoint');self.d.advance();v=self.snapshot()
        self.assertEqual(v['plan']['tasks']['a']['state'],'UNKNOWN');self.assertIn('a',v['dispatcher']['orphans'])
        self.assertFalse(any(a['task_id']=='a' for a in self.d.take()))
    def test_unknown_reaches_deadline_without_relaunch_and_requires_terminal_release(self):
        self.spawn();poll=self.polls()[0];self.terminal(poll,'UNKNOWN')
        self.clock[0]+=301
        actions=self.take()
        interrupts=[a for a in actions if a['kind']=='INTERRUPT' and a['task_id']==poll['task_id']]
        self.assertEqual(len(interrupts),1)
        self.d.observe(interrupts[0]['action_id'],{'agent_id':poll['agent_id'],'accepted':True})
        self.assertFalse(self.snapshot()['bindings'][poll['task_id']]['released'])
        final_poll=next(a for a in actions if a['kind']=='POLL' and a['task_id']==poll['task_id'])
        self.terminal(final_poll,'CANCELLED')
        self.assertTrue(self.snapshot()['bindings'][poll['task_id']]['released'])
        self.assertEqual(self.snapshot()['plan']['tasks'][poll['task_id']]['state'],'UNKNOWN')
        self.assertEqual(self.snapshot()['plan']['dispatches'],2)
    def test_failed_acceptance_and_scope_violation_never_become_success(self):
        self.spawn();polls=self.polls()
        for a in polls:self.terminal(a)
        verifications=self.take('VERIFY')
        first=verifications[0];self.d.observe(first['action_id'],self.result(first,False))
        second=verifications[1];event=self.result(second);task=self.snapshot()['plan']['tasks'][second['task_id']]
        (Path(task['workspace'])/'outside.txt').write_text('out of scope')
        self.assertFalse(self.d.observe(second['action_id'],event)['applied'])
        states=self.snapshot()['plan']['tasks'];self.assertEqual(states[first['task_id']]['state'],'FAILED');self.assertEqual(states[second['task_id']]['state'],'UNKNOWN')
    def test_poll_interval_and_retained_history_are_bounded(self):
        self.spawn()
        for n in range(40):
            for a in self.polls():self.terminal(a,'RUNNING')
            self.assertFalse(self.take('POLL'));self.clock[0]+=.0+1.1
        v=self.snapshot();self.assertLessEqual(len(v['dispatcher']['retired']),32)
        self.assertEqual(v['plan']['dispatches'],2);self.assertLess(self.d.team.path.stat().st_size,1024*1024)
    def test_control_lock_and_clock_regression_do_not_issue_actions(self):
        with runtime_lock(self.d.lock_dir):
            with self.assertRaises(RuntimeError):self.d.advance()
        self.clock[0]-=1
        with self.assertRaisesRegex(ValueError,'CLOCK_REGRESSED'):self.d.advance()
    def test_deadline_between_admit_and_take_never_hands_out_stale_spawn(self):
        self.d.advance()
        self.clock[0]+=301
        self.assertEqual(self.d.take(),[])
        state=self.snapshot()
        self.assertTrue(all(t['state']=='TIMED_OUT' for t in state['plan']['tasks'].values()))
        self.assertTrue(all(a['status']=='BLOCKED' for a in state['dispatcher']['actions'].values()))
        self.assertTrue(all(not b['released'] for b in state['bindings'].values()))
    def test_two_workers_parent_integration_and_verifier_progress_to_complete(self):
        # New run uses four existing Git worktrees, all operations are fixture events.
        spec=deepcopy(self.f.spec);by={t['id']:t for t in spec['tasks']}
        by['a']['allowed']=['a.txt'];by['b']['allowed']=['b.txt'];by['third'].update(role='Verifier',deps=['merge'],allowed=['report.txt'])
        by['merge'].update(role='Coordinator',allowed=['a.txt','b.txt'])
        d=NativeDispatcher.receive(self.f.root/'dag-run',spec,'dag-fixture',clock=lambda:self.clock[0]);fake_spawns=0
        for step in range(16):
            summary=d.advance();actions=d.take()
            for a in actions:
                if a['kind']=='SPAWN':fake_spawns+=1;d.observe(a['action_id'],{'agent_id':'fixture-'+a['task_id']})
                elif a['kind']=='POLL':
                    task=d.snapshot()['plan']['tasks'][a['task_id']]
                    for name in task['allowed']:(Path(task['workspace'])/name).write_text('fixture '+name)
                    d.observe(a['action_id'],{'agent_id':a['agent_id'],'state':'SUCCEEDED'})
                elif a['kind']=='VERIFY':
                    task=d.snapshot()['plan']['tasks'][a['task_id']]
                    self.assertTrue(all((Path(task['workspace'])/p).read_text()=='fixture '+p for p in task['allowed']))
                    d.observe(a['action_id'],{'agent_id':a['agent_id'],'accepted':True,'reason':'fixture acceptance','verification':VALIDATION})
                elif a['kind']=='COORDINATE':
                    state=d.snapshot();self.assertTrue(all(state['plan']['tasks'][k]['state']=='SUCCEEDED' for k in ['a','b']))
                    sources=[{'root':state['plan']['tasks'][k]['workspace'],'allowed':state['plan']['tasks'][k]['allowed'],'expected':state['receipts'][k]['artifacts']} for k in ['a','b']]
                    integrate(sources,Path(state['plan']['tasks']['merge']['workspace']))
                    d.observe(a['action_id'],{'agent_id':'parent-fixture','accepted':True,'reason':'hash-pinned integration','verification':VALIDATION})
            self.clock[0]+=1.1
            if summary['status']=='COMPLETE':break
        self.assertEqual(d.advance()['status'],'COMPLETE');self.assertEqual(fake_spawns,3)
        self.assertTrue(all(b['released'] for b in d.snapshot()['bindings'].values()))
        resumed=NativeDispatcher(NativeTeam(d.team.root,clock=lambda:self.clock[0]));self.assertEqual(resumed.advance()['status'],'COMPLETE');self.assertEqual(resumed.take(),[])

if __name__=='__main__':unittest.main()
