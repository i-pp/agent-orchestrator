"""Concurrent capacity and legacy migration with fake RPCs only."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'src'),str(PROJECT/'scripts'),str(PROJECT/'tests')]
from codex_pipeline import Pipeline,PipelineError,HELD,MAX_OPS,MAX_EVENTS,LEDGER_BYTES,COMPLETION_RESERVE,packed,digest,validate_policy
from codex_app_protocol import ProtocolUnknown
from test_codex_pipeline import Peer
import pipeline_policy_migration as migration


class ConcurrentCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'logs').mkdir(mode=0o700)
        self.ws=self.root/'workspace';self.ws.mkdir()
        self.policy={'schema':2,'workspaces':{'fixture':str(self.ws)},'models':['offline-model'],
            'max_threads':1,'max_tasks':1,'concurrency':1,'timeout_seconds':120}
        self.peer=Peer(str(self.ws));self.pipe=self.new()
    def new(self):return Pipeline(self.root,self.policy,self.peer.client,instructions='fixed fixture',policy_sha256='a'*64,clock=lambda:1000)
    def call(self,name,**args):return self.pipe.dispatch('codex_'+name,args)
    def start(self,key):return self.call('thread_start',workspace='fixture',model='offline-model',request_key=key)['thread_id']
    def submit(self,t,key):return self.call('task_submit',thread_id=t,prompt='fixture',request_key=key)
    def state(self):return self.pipe._mutate(lambda s:copy.deepcopy(s))
    def usage(self):return self.call('threads_list')['capacity']
    def complete(self,task,status='completed'):
        s=self.state();remote=s['threads'][task['thread_id']]['remote_id'];turn=self.peer.threads[remote]['turns'][-1]
        turn.update(status=status,items=[{'type':'agentMessage','text':'KEPT'}])
        self.pipe.receive({'method':'turn/completed','params':{'threadId':remote,'turn':copy.deepcopy(turn)}})
    def fill_storage(self):
        def pad(s):
            s['ops']['fixture-history']={'kind':'fixture','status':'SUCCEEDED','result':{'padding':''}}
            s['ops']['fixture-history']['result']['padding']='x'*(LEDGER_BYTES-COMPLETION_RESERVE+1024-len(packed(s).encode()))
        self.pipe._mutate(pad)

    def test_idle_and_archived_thread_history_does_not_consume_inference_slots(self):
        for i in range(12):
            t=self.start('thread-'+str(i))
            if i%2:self.call('thread_archive',thread_id=t,request_key='archive-'+str(i))
        usage=self.usage()
        self.assertEqual(usage['stored_threads'],12);self.assertEqual(usage['archived_threads'],6)
        self.assertEqual(usage['active_threads'],0);self.assertEqual(usage['held_tasks'],0)
        self.assertEqual(self.peer.count('turn/start'),0)

    def test_repeated_completion_does_not_exhaust_task_count_and_keeps_history(self):
        t=self.start('thread')
        for i in range(10):
            task=self.submit(t,'task-'+str(i));self.assertEqual(task['state'],'RUNNING');self.complete(task)
        self.assertEqual(self.usage()['terminal_tasks'],10);self.assertEqual(self.usage()['held_tasks'],0)
        self.assertEqual(len(self.state()['tasks']),10);self.assertTrue(all(t['answer']=='KEPT' for t in self.state()['tasks'].values()))

    def test_each_held_state_blocks_other_inference_but_not_saved_inputs(self):
        t=self.start('thread');active=self.submit(t,'active')
        other=self.start('other-idle')
        for i,state in enumerate(sorted(HELD)):
            self.pipe._mutate(lambda s:s['tasks'][active['id']].update(state=state))
            queued=self.submit(other,'pending-'+str(i));self.assertEqual(queued['state'],'QUEUED')
            self.assertEqual(self.usage()['held_tasks'],1);self.assertEqual(self.usage()['active_threads'],1)
            self.assertEqual(self.peer.count('turn/start'),1)

    def test_interrupt_acceptance_is_not_slot_release_then_terminal_starts_fifo(self):
        t=self.start('thread');a=self.submit(t,'active');b=self.submit(t,'queued')
        reply=self.call('task_cancel',task_id=a['id'],request_key='stop')
        self.assertEqual(reply['status'],'STOP_REQUESTED');self.assertFalse(reply['stop_confirmed'])
        self.call('pipeline_tick');self.assertEqual(self.peer.count('turn/start'),1)
        self.complete(a,'interrupted');self.assertEqual(self.usage()['held_tasks'],0)
        self.call('pipeline_tick');self.assertEqual(self.peer.count('turn/start'),2)
        self.assertEqual(self.state()['tasks'][b['id']]['state'],'RUNNING')

    def test_pending_cancel_and_idle_archive_preserve_records(self):
        t=self.start('thread');task=self.call('chat_store',thread_id=t,prompt='pending',request_key='stored')
        self.call('task_cancel',task_id=task['id'],request_key='cancel')
        self.call('thread_archive',thread_id=t,request_key='close')
        self.assertEqual(self.usage()['held_tasks'],0);self.assertIn(task['id'],self.state()['tasks'])
        self.assertTrue(self.state()['threads'][t]['archived']);self.assertEqual(self.peer.count('turn/start'),0)
        self.start('new-idle')

    def test_close_busy_does_not_interrupt_or_release(self):
        t=self.start('thread');self.submit(t,'active')
        with self.assertRaisesRegex(PipelineError,'THREAD_BUSY'):self.call('thread_archive',thread_id=t,request_key='close-busy')
        self.assertEqual(self.peer.count('turn/interrupt'),0);self.assertEqual(self.usage()['held_tasks'],1)

    def test_provider_failure_terminal_releases_before_next_distinct_input(self):
        t=self.start('thread');a=self.submit(t,'active');self.submit(t,'queued')
        self.assertEqual(self.usage()['available_execution_slots'],0)
        self.complete(a,'failed');self.assertEqual(self.usage()['available_execution_slots'],1)
        self.call('pipeline_tick');self.assertEqual(self.peer.count('turn/start'),2)
        self.assertEqual(self.state()['tasks'][a['id']]['state'],'FAILED')

    def test_unknown_start_remains_held_across_restart_no_replay(self):
        t=self.start('thread');self.peer.failure['turn/start']=TimeoutError()
        task=self.submit(t,'unknown');self.assertEqual(task['state'],'UNKNOWN')
        self.pipe=self.new();self.assertEqual(self.usage()['held_tasks'],1)
        self.pipe.store(t,'queued','after-unknown',queue=True);self.call('pipeline_tick')
        self.assertEqual(self.peer.count('turn/start'),1)

    def test_known_rejected_turn_releases_logical_slot_without_retrying_original(self):
        t=self.start('thread');self.peer.failure['turn/start']='refused'
        task=self.submit(t,'rejected');self.assertEqual(task['state'],'FAILED');self.assertEqual(self.usage()['held_tasks'],0)
        del self.peer.failure['turn/start'];self.submit(t,'different-request')
        self.assertEqual(self.peer.count('turn/start'),2);self.assertEqual(self.state()['tasks'][task['id']]['state'],'FAILED')

    def test_cumulative_audit_counts_do_not_become_normal_job_limits(self):
        def history(s):
            s['ops'].update({f'old-{i}':{'status':'SUCCEEDED'} for i in range(MAX_OPS)})
            s['events']=[{'seq':i+1,'kind':'fixture','time':1} for i in range(MAX_EVENTS)]
        self.pipe._mutate(history)
        self.start('normal-work');self.assertGreater(len(self.state()['ops']),MAX_OPS)
        self.assertGreater(len(self.state()['events']),MAX_EVENTS)

    def test_storage_backpressure_preserves_stop_completion_and_readback(self):
        t=self.start('thread');task=self.submit(t,'active');self.fill_storage()
        before=self.state()
        with self.assertRaisesRegex(PipelineError,'HISTORY_STORAGE_CAPACITY'):self.pipe.store(t,'new','denied')
        self.assertEqual(self.state(),before)
        self.call('task_cancel',task_id=task['id'],request_key='emergency-stop')
        self.complete(task,'interrupted')
        self.assertEqual(self.usage()['held_tasks'],0);self.assertLessEqual(self.usage()['history_bytes'],LEDGER_BYTES)
        self.assertEqual(self.state()['tasks'][task['id']]['state'],'CANCELLED')

    def test_legacy_schema_retains_trial_caps_and_unknown_schema_fails(self):
        bad={**self.policy,'schema':3}
        with self.assertRaises(PipelineError):validate_policy(bad)
        legacy={**self.policy,'schema':1}
        self.assertEqual(validate_policy(legacy)['schema'],1)

    def test_saved_history_is_paged_with_stable_cursor_and_bounded_responses(self):
        t=self.start('thread');task=self.submit(t,'task');self.complete(task)
        def history(s):
            thread=s['threads'][t];old=s['tasks'][task['id']]
            for _ in range(120):
                tid=str(uuid.uuid4());ident=str(uuid.uuid4())
                s['threads'][tid]={**thread,'id':tid,'remote_id':'fixture-'+tid}
                s['tasks'][ident]={**old,'id':ident,'thread_id':tid}
        self.pipe._mutate(history);expected=set(self.state()['threads'])|set(self.state()['tasks'])
        seen=set();args={};first=None
        while True:
            page=self.call('threads_list',**args)
            if first is None:first=page
            self.assertLess(len(json.dumps(page).encode()),16000)
            ids={x['id'] for x in page['threads']+page['tasks']};self.assertFalse(seen&ids);seen|=ids
            if page['next_cursor'] is None:break
            args={'cursor':page['next_cursor']}
        self.assertEqual(seen,expected)
        self.pipe.store(t,'new pending','change-list')
        with self.assertRaisesRegex(PipelineError,'SNAPSHOT_CHANGED'):self.call('threads_list',cursor=first['next_cursor'])


class ConcurrentMigrationTests(unittest.TestCase):
    def setUp(self):
        from test_pipeline_policy_migration import MigrationTests
        self.fixture=MigrationTests('test_commit_retains_ids_attempts_answer_events_and_lineage')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.f=self.fixture
        self.target={**self.f.old,'schema':2,'max_threads':1,'max_tasks':1}
    def plan(self,target=None,direction='expand',key='normal'):
        f=self.f;candidate=f.write(key+'.json',json.dumps(target or self.target))
        return migration.plan_transition(f.root,candidate,f.common,key,direction=direction,now=2001)
    def test_promotion_preserves_old_consumed_records_and_does_not_reset(self):
        f=self.f;p=self.plan();before=f.state();migration.apply_transition(f.root,p,digest(p))
        after=f.state()
        for key in ['threads','tasks','ops']:self.assertEqual(after[key],before[key])
        self.assertEqual(after['events'][:-1],before['events']);self.assertEqual(after['tasks'][f.task]['answer'],'PRESERVED_OK')
        normal=f.new(self.target)
        for i in range(7):normal.store(f.thread,'stored','normal-'+str(i))
        self.assertEqual(len(f.state()['tasks']),8)
        self.assertEqual(normal.dispatch('codex_threads_list',{})['capacity']['held_tasks'],0)
    def test_promotion_cannot_expand_models_concurrency_timeout_or_workspace(self):
        changes=[{'models':['offline-model','other']},{'concurrency':2,'max_tasks':2,'max_threads':2},
            {'timeout_seconds':121},{'workspaces':{'other':str(self.f.ws)}},{'max_threads':2}]
        for i,change in enumerate(changes):
            with self.subTest(change=change),self.assertRaises(PipelineError):self.plan({**self.target,**change},key='bad-'+str(i))
    def test_direct_policy_replacement_without_journal_refused(self):
        self.f.write(migration.POLICY,json.dumps(self.target))
        with self.assertRaisesRegex(PipelineError,'PIPELINE_POLICY_CHANGED'):self.f.new(self.target)
    def test_failed_promotion_keeps_pending_gate_and_supports_explicit_abort(self):
        f=self.f;p=self.plan()
        with patch.object(migration,'_write_policy',side_effect=OSError('fixture crash')),self.assertRaises(OSError):
            migration.apply_transition(f.root,p,digest(p))
        with self.assertRaisesRegex(PipelineError,'POLICY_MIGRATION_PENDING'):f.new(f.old)
        migration.recover_transition(f.root,p['id'],digest(p),action='abort')
        self.assertEqual(f.state(),f.initial)

    def test_rollback_retains_extra_normal_use_records_and_old_trial_exhaustion(self):
        f=self.f;p=self.plan();migration.apply_transition(f.root,p,digest(p));normal=f.new(self.target)
        task=normal.dispatch('codex_task_submit',{'thread_id':f.thread,'prompt':'new fixture','request_key':'normal-task'})
        remote=f.state()['threads'][f.thread]['remote_id'];turn=f.peer.threads[remote]['turns'][-1]
        turn.update(status='completed',items=[])
        normal.receive({'method':'turn/completed','params':{'threadId':remote,'turn':copy.deepcopy(turn)}})
        before=f.state()
        candidate=f.write('back.json',f.old_raw)
        back=migration.plan_transition(f.root,candidate,f.common,'back',direction='rollback',now=2002)
        migration.apply_transition(f.root,back,digest(back))
        after=f.state()
        for key in ['threads','tasks','ops']:self.assertEqual(after[key],before[key])
        self.assertIn(task['id'],after['tasks'])
        with self.assertRaisesRegex(PipelineError,'TASK_CAPACITY'):f.new(f.old).store(f.thread,'denied','legacy-exhausted')

    def test_recorded_rollback_preserves_history_beyond_old_event_count(self):
        f=self.f;p=self.plan();migration.apply_transition(f.root,p,digest(p));normal=f.new(self.target)
        def history(s):
            n=len(s['events']);s['events'].extend({'seq':n+i+1,'kind':'fixture','time':1} for i in range(MAX_EVENTS))
        normal._mutate(history);before=f.state()
        candidate=f.write('back.json',f.old_raw)
        back=migration.plan_transition(f.root,candidate,f.common,'back',direction='rollback',now=2002)
        migration.apply_transition(f.root,back,digest(back))
        self.assertEqual(f.state()['events'][:-1],before['events'])


class ConcurrentGatewayTests(unittest.TestCase):
    def setUp(self):
        from test_codex_pipeline_gateway import GatewayTests
        from codex_host import HostGateway
        self.fixture=GatewayTests('test_metadata_session_closes_and_releases_runner_budget');self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups);f=self.fixture
        f.gateway.close();f.policy.update(schema=2,max_tasks=1,max_threads=1)
        path=f.root/'codex-pipeline-policy.json';path.write_text(json.dumps(f.policy))
        (f.root/'codex-pipeline.sqlite3').unlink()  # Empty, owned fixture DB only.
        f.gateway=HostGateway(f.root,str(f.binary),PROJECT,lambda:f.busy,transport_factory=f.factory)
        self.addCleanup(f.gateway.close);self.f=f
    def test_known_rejection_can_continue_on_same_live_metadata_context(self):
        f=self.f;t=f.start();f.peer.failure['turn/start']='refused'
        task=f.submit(t);self.assertEqual(task['state'],'FAILED');self.assertFalse(f.gateway.failed)
        del f.peer.failure['turn/start'];next_task=f.submit(t,key='different')
        self.assertEqual(next_task['state'],'RUNNING');self.assertEqual(f.peer.count('turn/start'),2)
    def test_unknown_transport_keeps_held_slot_no_reconnection(self):
        f=self.f;t=f.start();f.peer.failure['turn/start']=TimeoutError()
        task=f.submit(t);self.assertEqual(task['state'],'UNKNOWN');self.assertTrue(f.gateway.failed)
        self.assertTrue(f.gateway.held);f.gateway.pump();self.assertEqual(f.peer.count('turn/start'),1)

    def test_cleanup_failure_stays_held_even_after_logical_terminal_result(self):
        f=self.f;t=f.start();task=f.submit(t);f.complete(t)
        f.gateway.transport.cleanup_confirmed=False
        with self.assertRaisesRegex(PipelineError,'CLEANUP_UNCONFIRMED'):f.gateway.pump()
        self.assertEqual(f.gateway.pipeline._mutate(lambda s:s['tasks'][task['id']]['state']),'COMPLETED')
        self.assertTrue(f.gateway.failed);self.assertTrue(f.gateway.held)
        self.assertEqual(f.peer.count('turn/start'),1)

    def test_rejected_cold_resume_blocked_durably_without_automatic_retry(self):
        from codex_host import HostGateway
        f=self.f;t=f.start();f.submit(t);f.complete(t);f.gateway.pump()
        before=f.peer.count('thread/resume');f.peer.failure['thread/resume']='refused'
        task=f.submit(t,key='cold-rejected')
        self.assertEqual(task['state'],'BLOCKED');self.assertEqual(task['reason_code'],'RESUME_REJECTED')
        self.assertFalse(task['execution_started']);self.assertFalse(f.gateway.failed)
        self.assertFalse(f.gateway.held)
        del f.peer.failure['thread/resume'];f.gateway.pump()
        self.assertEqual(f.peer.count('thread/resume'),before+1);self.assertEqual(f.peer.count('turn/start'),1)
        f.gateway.close();f.gateway=HostGateway(f.root,str(f.binary),PROJECT,lambda:f.busy,transport_factory=f.factory)
        self.addCleanup(f.gateway.close);f.gateway.pump()
        self.assertEqual(f.peer.count('thread/resume'),before+1);self.assertEqual(f.peer.count('turn/start'),1)
        self.assertEqual(f.gateway.pipeline._mutate(lambda s:s['tasks'][task['id']]['state']),'BLOCKED')
        self.assertEqual(f.submit(t,key='different-after-refusal')['state'],'RUNNING')
        self.assertEqual(f.peer.count('turn/start'),2)

    def test_rejected_catalog_is_blocked_without_implicit_reexecution(self):
        f=self.f;t=f.start();f.submit(t);f.complete(t);f.gateway.pump()
        for i,method in enumerate(['account/read','model/list']):
            before=f.peer.count(method);f.peer.failure[method]='refused'
            task=f.submit(t,key='catalog-rejected-'+str(i))
            self.assertEqual(task['state'],'BLOCKED');self.assertEqual(task['reason_code'],'CATALOG_REJECTED')
            del f.peer.failure[method];f.gateway.pump()
            self.assertEqual(f.peer.count(method),before+1);self.assertEqual(f.peer.count('turn/start'),1)


if __name__=='__main__':unittest.main()
