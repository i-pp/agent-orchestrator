"""Operator workflow tests: fixtures only, no models, services or namespaces."""
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'src'),str(PROJECT/'scripts')]
from chat_mailbox import Mailbox,MailboxError,TOOLS,digest
from mailbox_operations import MailboxWorkflow,StdioMailbox,OperationError,prepare_request
from poc_services import Services
from development_poc import MODE,MARKER
import mcp_adapter as mcp

TASK='00000000-0000-4000-8000-000000000001'
REQUEST='00000000-0000-4000-8000-000000000002'
BODY='Non-secret workflow fixture question'
KEY={'task_id':TASK,'request_id':REQUEST,'request_sha256':digest(BODY)}
BUNDLE={**KEY,'request':BODY}

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'logs').mkdir(mode=0o700)
        p=self.root/'chat-mailbox-policy.json';p.write_text(json.dumps({'schema':1,'allowed':[KEY]}));p.chmod(0o600)
        self.box=Mailbox(self.root);self.connections=[];self.fault=None;self.sent=[]
    def tearDown(self):self.tmp.cleanup()
    def factory(self):
        parent=self
        class Connection:
            closed=False
            def exchange(self,name,args):
                parent.sent.append(name)
                if parent.fault:
                    result=parent.fault(name,args)
                    if result is not None:return result
                try:data=parent.box.dispatch(name,args)
                except MailboxError as exc:raise OperationError(exc.code,exc.status)
                return {'call_id':'00000000-0000-4000-8000-000000000003','data':data}
            def close(self):self.closed=True
        conn=Connection();self.connections.append(conn);return conn

    def test_register_once_readback_then_reuse_without_new_write(self):
        result=MailboxWorkflow(self.factory).register(BUNDLE)
        self.assertEqual(result['status'],'PENDING');self.assertEqual(result['write_attempts'],1)
        again=MailboxWorkflow(self.factory).register(BUNDLE)
        self.assertTrue(again['reused_existing']);self.assertEqual(again['write_attempts'],0)
        self.assertEqual(self.sent.count(TOOLS[0]),1)
        self.assertTrue(all(c.closed for c in self.connections))
        self.assertNotIn(BODY,json.dumps(result))

    def test_lost_ack_reconnects_for_getter_only(self):
        def fault(name,args):
            if name==TOOLS[0]:self.box.dispatch(name,args);raise OperationError('ACK_LOST')
        self.fault=fault
        result=MailboxWorkflow(self.factory).register(BUNDLE)
        self.assertEqual(result['status'],'PENDING');self.assertTrue(result['reconciled'])
        self.assertEqual(result['reconnections'],1);self.assertEqual(self.sent.count(TOOLS[0]),1)

    def test_timed_out_write_missing_on_readback_stays_unknown_without_resend(self):
        def fault(name,args):
            if name==TOOLS[0]:raise OperationError('MCP_TIMEOUT')
        self.fault=fault
        result=MailboxWorkflow(self.factory).register(BUNDLE)
        self.assertEqual(result['status'],'UNKNOWN');self.assertFalse(result['automatic_retry'])
        self.assertEqual(result['code'],'WRITE_NOT_YET_RECONCILED');self.assertEqual(self.sent.count(TOOLS[0]),1)

    def test_reconnect_failure_cannot_turn_uncertain_write_into_blocked(self):
        for failure in [OSError('private-fixture-error'),OperationError('MAILBOX_NOT_READY','BLOCKED')]:
            self.connections=[]
            def fault(name,args):
                if name==TOOLS[0]:raise OperationError('ACK_LOST')
            self.fault=fault
            def factory():
                if self.connections:raise failure
                return self.factory()
            result=MailboxWorkflow(factory).register(BUNDLE)
            self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(result['write_attempts'],1)
            self.assertNotIn('private-',json.dumps(result))

    def test_readonly_reconnect_is_bounded_and_answer_hash_is_verified(self):
        self.box.dispatch(TOOLS[0],BUNDLE)
        self.box.dispatch(TOOLS[2],{**KEY,'answer':'Non-secret fixture answer'})
        failed=[False]
        def fault(name,args):
            if not failed[0]:failed[0]=True;raise OperationError('MCP_CONNECTION_CLOSED')
        self.fault=fault
        result=MailboxWorkflow(self.factory).inspect(KEY)
        self.assertEqual(result['status'],'ANSWERED');self.assertEqual(result['reconnections'],1)
        self.assertEqual(result['answer_sha256'],digest('Non-secret fixture answer'))
        self.assertFalse(any(n in (TOOLS[0],TOOLS[2]) for n in self.sent))
        self.assertNotIn('Non-secret fixture answer',json.dumps(result['calls']))

    def test_repeated_read_failure_stops_after_one_reconnect(self):
        def fault(name,args):raise OperationError('MCP_TIMEOUT')
        self.fault=fault;result=MailboxWorkflow(self.factory).inspect(KEY)
        self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(len(self.connections),2)
        self.assertEqual(result['reconnections'],1);self.assertEqual(result['write_attempts'],0)

    def test_corrupt_answer_or_slot_is_not_reported_as_success(self):
        self.box.dispatch(TOOLS[0],BUNDLE)
        def fault(name,args):
            if name==TOOLS[3]:
                return {'call_id':'fixture','data':{**KEY,'status':'ANSWERED','persisted':True,
                    'answer':'Tampered fixture','answer_sha256':digest('Other')}}
        self.fault=fault
        self.assertEqual(MailboxWorkflow(self.factory).inspect(KEY)['status'],'UNKNOWN')
        def wrong_slot(name,args):return {'call_id':'fixture','data':{**KEY,'request_id':TASK,'status':'PENDING','persisted':True,'request':BODY}}
        self.fault=wrong_slot
        self.assertEqual(MailboxWorkflow(self.factory).register(BUNDLE)['write_attempts'],0)

    def test_prepare_never_installs_policy_or_changes_existing_pin(self):
        allowed={(TASK,REQUEST):KEY['request_sha256']}
        before=(self.root/'chat-mailbox-policy.json').read_bytes()
        plan=prepare_request({'task_id':TASK,'request_id':TASK,'request':BODY},allowed)
        self.assertTrue(plan['approval_required']);self.assertFalse(plan['install_performed'])
        self.assertEqual(len(plan['candidate_policy']['allowed']),2)
        self.assertEqual((self.root/'chat-mailbox-policy.json').read_bytes(),before)
        with self.assertRaisesRegex(OperationError,'CONFLICT'):
            prepare_request({'task_id':TASK,'request_id':REQUEST,'request':'Changed'},allowed)
        with self.assertRaises((MailboxError,OperationError)):
            prepare_request({'task_id':'../escape','request_id':REQUEST,'request':BODY},allowed)

    def test_invalid_envelopes_and_call_ids_fail_closed_without_payload_in_receipt(self):
        for envelope in [None,[],{'call_id':'private-call-id','data':{}},
                         {'call_id':'00000000-0000-4000-8000-000000000003','data':None}]:
            self.fault=lambda name,args,value=envelope: {'invalid':True} if value is None else value
            result=MailboxWorkflow(self.factory).inspect(KEY)
            self.assertEqual(result['status'],'UNKNOWN')
            self.assertNotIn('private-',json.dumps(result));self.assertEqual(result['write_attempts'],0)

    def test_content_in_an_incompatible_state_is_rejected(self):
        self.box.dispatch(TOOLS[0],BUNDLE)
        def pending_answer(name,args):
            data=self.box.dispatch(name,args)
            if name==TOOLS[3]:data.update(answer='private-unverified-answer',answer_sha256=digest('wrong'))
            return {'call_id':'00000000-0000-4000-8000-000000000003','data':data}
        self.fault=pending_answer
        result=MailboxWorkflow(self.factory).inspect(KEY)
        self.assertEqual(result['status'],'UNKNOWN');self.assertNotIn('private-',json.dumps(result))
        def missing_request(name,args):
            data=self.box.dispatch(name,args)
            data.update(status='NOT_FOUND',persisted=False,request=BODY)
            return {'call_id':'00000000-0000-4000-8000-000000000003','data':data}
        self.fault=missing_request
        self.assertEqual(MailboxWorkflow(self.factory).inspect(KEY)['status'],'UNKNOWN')

    def test_audit_records_validation_rejection_and_outcome_without_payload(self):
        with patch.object(mcp,'runtime_path',return_value=self.root):adapter=mcp.Adapter(self.root,allow_chat_mailbox=True)
        try:
            def rpc(name,args):return {'jsonrpc':'2.0','id':33,'method':'tools/call','params':{
                '_meta':{mcp.VERSION_META:mcp.REQUEST_PROTOCOL,mcp.CAPABILITIES_META:{}},'name':name,'arguments':args}}
            self.assertIn('error',adapter.handle(rpc(TOOLS[2],{**KEY,'answer':'private-payload-sentinel','path':'private-path'})))
            self.assertIn('error',adapter.handle(rpc('private-unknown-tool',{})))
            self.assertIn('error',adapter.handle({'jsonrpc':'2.0','id':34,'method':'tools/call','params':[]}))
            adapter.handle(rpc(TOOLS[0],BUNDLE))
            adapter.handle(rpc(TOOLS[2],{**KEY,'answer':'private-answer-sentinel'}))
            text=adapter.audit_path.read_text();self.assertNotIn('private-',text);self.assertNotIn(BODY,text)
            rows=[json.loads(x) for x in text.splitlines()]
            self.assertEqual(sum(x['event']=='tool_rejected' for x in rows),3)
            end=[x for x in rows if x.get('tool')==TOOLS[2] and x['event']=='end'][0]
            self.assertEqual(end['mailbox']['status'],'ANSWERED')
            self.assertEqual(end['mailbox']['answer_sha256'],digest('private-answer-sentinel'))
        finally:adapter.audit.close()

class StdioTests(unittest.TestCase):
    def test_pipe_write_timeout_is_bounded_even_when_server_does_not_read(self):
        transport=StdioMailbox.__new__(StdioMailbox)
        transport.timeout=.1;transport.counter=0;transport.buffer=bytearray()
        transport.process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        os.set_blocking(transport.process.stdin.fileno(),False)
        os.set_blocking(transport.process.stdout.fileno(),False)
        transport.selector=selectors.DefaultSelector();transport.selector.register(transport.process.stdout,selectors.EVENT_READ)
        text='\x00'*4096;args={**KEY,'request':text,'request_sha256':digest(text)}
        started=time.monotonic()
        try:
            with self.assertRaisesRegex(OperationError,'TIMEOUT'):transport.exchange(TOOLS[0],args)
            self.assertLess(time.monotonic()-started,1)
        finally:transport.close()
        self.assertIsNotNone(transport.process.poll())

    def test_actual_stdio_register_inspect_and_cli_metadata_output(self):
        with tempfile.TemporaryDirectory(dir=PROJECT/'.runtime',prefix='development-poc-ops-') as tmp:
            root=Path(tmp);(root/'logs').mkdir(mode=0o700)
            for name,value in [(MARKER,{'schema':1,'mode':MODE,'runtime':str(root)}),
                               ('chat-mailbox-policy.json',{'schema':1,'allowed':[KEY]})]:
                p=root/name;p.write_text(json.dumps(value));p.chmod(0o600)
            manager=Services(runtime=root)
            state={'schema':1,'runtime':str(root),'token':'a'*32,'units':manager.units,'invocations':{},'phase':'READY',
                'local_llm_enabled':False,'local_models_enabled':False,'chat_mailbox_enabled':True}
            p=root/'poc-services-state.json';p.write_text(json.dumps(state));p.chmod(0o600)
            result=MailboxWorkflow(lambda:StdioMailbox(root)).register(BUNDLE)
            self.assertEqual(result['status'],'PENDING');self.assertEqual(result['write_attempts'],1)
            answer='Non-secret stdio fixture reply';box=Mailbox(root);box.dispatch(TOOLS[2],{**KEY,'answer':answer})
            response=MailboxWorkflow(lambda:StdioMailbox(root)).inspect(KEY)
            self.assertEqual(response['answer'],answer)
            cli=subprocess.run([sys.executable,'-B',str(PROJECT/'scripts/mailbox_ops.py'),'inspect','--runtime',str(root)],
                input=json.dumps(KEY),capture_output=True,text=True,timeout=10)
            self.assertEqual(cli.returncode,0,cli.stderr)
            self.assertEqual(json.loads(cli.stdout)['status'],'ANSWERED')
            self.assertNotIn(answer,cli.stdout);self.assertNotIn(BODY,cli.stdout)

if __name__=='__main__':unittest.main()
