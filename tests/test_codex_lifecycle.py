"""Lifecycle regression: fake peer only, no native process or inference."""
import copy
import json
import unittest
from unittest.mock import Mock, patch
import test_codex_pipeline_gateway as gateway_fixtures
from codex_pipeline import PipelineError, digest
from daemon import Daemon

class LifecycleTests(unittest.TestCase):
    setUp=gateway_fixtures.GatewayTests.setUp
    factory=gateway_fixtures.GatewayTests.factory
    call=gateway_fixtures.GatewayTests.call
    start=gateway_fixtures.GatewayTests.start
    submit=gateway_fixtures.GatewayTests.submit
    complete=gateway_fixtures.GatewayTests.complete

    def empty(self):
        thread=self.start();self.busy=True;task=self.submit(thread)
        self.gateway.release_idle(force=True)
        state=self.gateway.pipeline._mutate(copy.deepcopy)
        proof={'thread_id':thread,'inference_calls':0,'new_threads':0,'cleanup_status':'RELEASED',
            'checks':[{'method':'thread/resume','exception_class':'RpcError','rpc_error_code':-32600,'reason_categories':['NO_ROLLOUT']}]}
        evidence={'schema':1,'thread_id':thread,'ledger_sha256':digest(state),
            'remote_id_sha256':digest(state['threads'][thread]['remote_id']),
            'diagnostic_sha256':digest(proof),'diagnostic':proof}
        return thread,task,state,evidence

    def test_connection_retained_until_first_history_then_ordinary_resume(self):
        thread=self.start();self.assertFalse(self.transports[0].closed)
        for _ in range(3):self.call('threads_list')
        self.assertEqual(self.submit(thread)['state'],'RUNNING')
        self.assertEqual(self.peer.count('thread/resume'),0)
        self.assertEqual(len(self.transports),1)
        self.complete(thread);self.gateway.pump()
        self.assertTrue(self.transports[0].closed);self.assertIsNone(self.gateway.budget)
        self.assertTrue(self.gateway.pipeline._mutate(lambda s:s['threads'][thread]['history_verified']))
        self.submit(thread,'second')
        self.assertEqual(self.peer.count('thread/resume'),1);self.assertEqual(self.peer.count('thread/start'),1)

    def test_replacement_preserves_task_quota_old_id_and_runs_exactly_once(self):
        thread,task,before,e=self.empty()
        result=self.gateway.replace_empty_context(thread,'replace-once',e)
        self.assertEqual(result['native_creation_count'],2);self.assertEqual(self.peer.count('turn/start'),0)
        self.assertFalse(self.transports[-1].closed)
        after=self.gateway.pipeline._mutate(copy.deepcopy)
        self.assertEqual(after['tasks'],before['tasks']);self.assertEqual(len(after['threads']),1)
        self.assertEqual(after['threads'][thread]['original_remote_id'],before['threads'][thread]['remote_id'])
        self.assertEqual(after['threads'][thread]['native_replacements'][0]['reason'],'EMPTY_NO_ROLLOUT')
        self.assertTrue(self.gateway.pipeline.replace_empty_context(thread,'replace-once',e)['duplicate'])
        self.assertEqual(self.peer.count('thread/start'),2)
        view=self.call('threads_list')['threads'][0]
        self.assertNotIn('original_remote_id',view);self.assertNotIn('native_replacements',view)
        self.busy=False;self.gateway.pump();self.gateway.pump()
        self.assertEqual(self.peer.count('turn/start'),1);self.assertEqual(self.peer.count('thread/resume'),0)
        self.complete(thread);self.gateway.pump();self.assertTrue(self.transports[-1].closed)

    def test_stale_reason_prior_turn_unknown_history_forbid_replacement(self):
        thread,task,before,e=self.empty();calls=len(self.peer.calls)
        for change in ['stale','reason','prior-turn','unknown-op','history']:
            self.gateway.pipeline._mutate(lambda s:(s.clear(),s.update(copy.deepcopy(before))))
            proof=copy.deepcopy(e)
            if change=='stale':proof['ledger_sha256']='a'*64
            if change=='reason':
                proof['diagnostic']['checks'][0]['reason_categories']=['THREAD_NOT_FOUND']
                proof['diagnostic_sha256']=digest(proof['diagnostic'])
            if change in ['prior-turn','unknown-op','history']:
                def mutate(s):
                    if change=='prior-turn':s['tasks'][task['id']]['remote_turn']='turn-old'
                    if change=='unknown-op':s['ops']['start']['status']='UNKNOWN'
                    if change=='history':s['threads'][thread]['history_verified']=True
                self.gateway.pipeline._mutate(mutate);proof['ledger_sha256']=self.gateway.pipeline._mutate(digest)
            with self.subTest(change=change),self.assertRaises(PipelineError):
                self.gateway.replace_empty_context(thread,'recovery',proof)
        self.assertEqual(len(self.peer.calls),calls)

    def test_unknown_replacement_ack_never_recreated_or_dispatched(self):
        thread,task,before,e=self.empty();self.peer.failure['thread/start']=TimeoutError('PRIVATE_SENTINEL')
        result=self.gateway.replace_empty_context(thread,'replace-once',e)
        self.assertEqual(result['status'],'UNKNOWN');calls=self.peer.count('thread/start')
        self.assertTrue(self.gateway.pipeline.replace_empty_context(thread,'replace-once',e)['duplicate'])
        self.busy=False;self.gateway.pump();self.gateway.pump()
        self.assertEqual(self.peer.count('thread/start'),calls);self.assertEqual(self.peer.count('turn/start'),0)
        self.assertEqual(self.gateway.pipeline._mutate(lambda s:s['threads'][thread]['native_context_state']),'REPLACEMENT_UNKNOWN')
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(result))

    def test_expiry_closes_without_automatic_recreate(self):
        thread=self.start();self.busy=True;deadline=self.gateway.context_deadlines[thread]
        with patch('codex_host.time.monotonic',return_value=deadline+1):self.gateway.release_idle()
        self.assertTrue(self.transports[0].closed)
        self.assertEqual(self.gateway.pipeline._mutate(lambda s:s['threads'][thread]['native_context_state']),'EMPTY_CONTEXT_EXPIRED')
        self.busy=False;self.gateway.pump();self.gateway.pump()
        self.assertEqual(self.peer.count('thread/start'),1);self.assertEqual(self.peer.count('turn/start'),0)

    def test_in_progress_history_cannot_release_as_durable_terminal(self):
        thread=self.start();task=self.submit(thread)
        self.gateway.pipeline._mutate(lambda s:(s['tasks'][task['id']].update(state='COMPLETED'),
            s['threads'][thread].update(active_task=None)))
        self.gateway.release_idle()
        state=self.gateway.pipeline._mutate(lambda s:dict(s['threads'][thread]))
        self.assertFalse(state['history_verified'])
        self.assertEqual(state['native_context_state'],'HISTORY_UNKNOWN')
        self.assertTrue(self.transports[0].closed)
        self.assertEqual(self.peer.count('turn/start'),1)

    def test_operator_only_paused_not_mcp(self):
        d=Daemon.__new__(Daemon);d.allow_codex_pipeline=True;d.pipeline=Mock();d.children={};d.pending_recovery=[]
        d.status=Mock(return_value={'paused':False});args={'thread_id':'owned','request_key':'once','evidence':{}}
        with self.assertRaises(ValueError):d.handle('codex_pipeline_replace_empty',args)
        d.pipeline.replace_empty_context.assert_not_called()
        d.status.return_value={'paused':True};d.handle('codex_pipeline_replace_empty',args)
        d.pipeline.replace_empty_context.assert_called_once_with('owned','once',{})
        from mcp_adapter import tool_definitions
        self.assertNotIn('codex_pipeline_replace_empty',[t['name'] for t in tool_definitions(False,True,True,True,True)])
