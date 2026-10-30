"""Offline grant workflows: terminal metadata, one-write reconciliation and flag inheritance."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'src'),str(PROJECT/'scripts')]
from chat_mailbox import MailboxError, TOOLS, digest
from mailbox_grants import GrantPolicy, GrantLedger, PURPOSE
from mailbox_operations import MailboxWorkflow, StdioMailbox, OperationError
import mailbox_operations as module

TASK='00000000-0000-4000-8000-000000000010'
REQUEST='00000000-0000-4000-8000-000000000011'
BODY='Non-secret grant workflow fixture'
ANSWER='Non-secret retained reply'
KEY={'task_id':TASK,'request_id':REQUEST,'request_sha256':digest(BODY)}
BUNDLE={**KEY,'request':BODY}


class GrantWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.now=100
        policy=GrantPolicy.from_dict({'schema_version':1,
            'grant_id':'00000000-0000-4000-8000-000000000012','task_id':TASK,'purpose':PURPOSE,
            'not_before':100,'expires_at':200,'request_ttl_seconds':10,'max_requests':32,
            'max_pending':8,'max_text_bytes':4096,'retain_seconds':200,'audit_retention_seconds':300})
        self.box=GrantLedger(self.root,policy,enabled=True,clock=lambda:self.now)
        self.sent=[];self.connections=[];self.fault=None

    def tearDown(self):self.temp.cleanup()

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
                return parent.envelope(data)
            def close(self):self.closed=True
        connection=Connection();self.connections.append(connection);return connection

    @staticmethod
    def envelope(data):return {'call_id':'00000000-0000-4000-8000-000000000013','data':data}

    def answer(self):
        self.box.dispatch(TOOLS[0],BUNDLE)
        self.box.dispatch(TOOLS[2],{**KEY,'answer':ANSWER})

    def terminal(self,result,status,persisted=True):
        self.assertEqual(result['status'],status);self.assertIs(result['persisted'],persisted)
        self.assertNotIn('request',result);self.assertNotIn('answer',result)
        self.assertNotIn(BODY,json.dumps(result));self.assertNotIn(ANSWER,json.dumps(result))
        self.assertFalse(result['automatic_retry'])

    def test_register_and_retained_answer_after_grant_expiry(self):
        registered=MailboxWorkflow(self.factory).register(BUNDLE)
        self.assertEqual(registered['status'],'PENDING');self.assertEqual(registered['write_attempts'],1)
        self.box.dispatch(TOOLS[2],{**KEY,'answer':ANSWER})
        self.now=201
        result=MailboxWorkflow(self.factory).inspect(KEY)
        self.assertEqual(result['status'],'ANSWERED');self.assertEqual(result['grant_state'],'EXPIRED')
        self.assertEqual(result['request'],BODY);self.assertEqual(result['answer'],ANSWER)

    def test_expiry_between_getters_never_returns_stale_request(self):
        self.box.dispatch(TOOLS[0],BUNDLE)
        def fault(name,args):
            if name==TOOLS[3]:self.now=110
        self.fault=fault
        result=MailboxWorkflow(self.factory).inspect(KEY)
        self.terminal(result,'EXPIRED')
        self.assertEqual(result['write_attempts'],0);self.assertEqual(result['reconnections'],0)

    def test_revoke_between_getters_never_returns_stale_request(self):
        self.box.dispatch(TOOLS[0],BUNDLE)
        def fault(name,args):
            if name==TOOLS[3]:self.box.revoke()
        self.fault=fault
        result=MailboxWorkflow(self.factory).inspect(KEY)
        self.terminal(result,'REVOKED')
        self.assertEqual(result['write_attempts'],0);self.assertEqual(result['reconnections'],0)

    def test_answer_retention_expiring_between_getters_returns_metadata_only(self):
        self.answer();self.now=299
        def fault(name,args):
            if name==TOOLS[3]:self.now=300
        self.fault=fault
        result=MailboxWorkflow(self.factory).inspect(KEY)
        self.terminal(result,'EXPIRED')
        self.assertTrue(result['answer_saved']);self.assertEqual(result['answer_sha256'],digest(ANSWER))

    def test_inactive_missing_requests_are_blocked_without_write(self):
        for now,expected in [(99,'GRANT_NOT_YET_ACTIVE'),(200,'GRANT_EXPIRED')]:
            self.now=now
            result=MailboxWorkflow(self.factory).register(BUNDLE)
            self.assertEqual(result['status'],'BLOCKED');self.assertEqual(result['code'],expected)
            self.assertEqual(result['write_attempts'],0)
        self.box.revoke()
        result=MailboxWorkflow(self.factory).register(BUNDLE)
        self.assertEqual(result['status'],'BLOCKED');self.assertEqual(result['code'],'GRANT_REVOKED')
        self.assertFalse(result['persisted']);self.assertEqual(result['write_attempts'],0)
        self.assertNotIn(TOOLS[0],self.sent)

    def test_existing_expired_and_revoked_requests_are_blocked_without_write(self):
        self.box.dispatch(TOOLS[0],BUNDLE);self.now=110
        result=MailboxWorkflow(self.factory).register(BUNDLE)
        self.assertEqual(result['status'],'BLOCKED');self.assertEqual(result['observed_status'],'EXPIRED')
        self.assertEqual(result['code'],'REQUEST_EXPIRED');self.assertEqual(result['write_attempts'],0)
        self.box.revoke();result=MailboxWorkflow(self.factory).register(BUNDLE)
        self.assertEqual(result['status'],'BLOCKED');self.assertEqual(result['code'],'GRANT_REVOKED')
        self.assertEqual(result['write_attempts'],0);self.assertNotIn(TOOLS[0],self.sent)

    def test_lost_ack_reconciles_persisted_expired_without_resend(self):
        def fault(name,args):
            if name==TOOLS[0]:
                self.box.dispatch(name,args);self.now=110;raise OperationError('ACK_LOST')
        self.fault=fault
        result=MailboxWorkflow(self.factory).register(BUNDLE)
        self.terminal(result,'EXPIRED');self.assertTrue(result['reconciled'])
        self.assertEqual(result['write_attempts'],1);self.assertEqual(result['reconnections'],1)
        self.assertEqual(self.sent.count(TOOLS[0]),1)
        self.assertTrue(all(c.closed for c in self.connections))

    def test_lost_ack_reconciles_persisted_revoked_without_resend(self):
        def fault(name,args):
            if name==TOOLS[0]:
                self.box.dispatch(name,args);self.box.revoke();raise OperationError('ACK_LOST')
        self.fault=fault
        result=MailboxWorkflow(self.factory).register(BUNDLE)
        self.terminal(result,'REVOKED');self.assertTrue(result['reconciled'])
        self.assertEqual(self.sent.count(TOOLS[0]),1)

    def test_lost_ack_with_revoked_but_unpersisted_row_stays_unknown(self):
        def fault(name,args):
            if name==TOOLS[0]:self.box.revoke();raise OperationError('ACK_LOST')
        self.fault=fault
        result=MailboxWorkflow(self.factory).register(BUNDLE)
        self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(result['code'],'WRITE_NOT_YET_RECONCILED')
        self.assertEqual(result['write_attempts'],1);self.assertEqual(self.sent.count(TOOLS[0]),1)

    def test_revoke_after_not_found_getter_is_valid_terminal_without_body(self):
        def fault(name,args):
            if name==TOOLS[3]:self.box.revoke()
        self.fault=fault
        result=MailboxWorkflow(self.factory).inspect(KEY)
        self.terminal(result,'REVOKED',False)
        self.assertEqual(self.sent,[TOOLS[1],TOOLS[3],TOOLS[1]])

    def test_terminal_body_and_invalid_grant_metadata_are_rejected(self):
        self.answer();self.box.revoke()
        mutations=[{'request':'private-terminal-sentinel'}, {'answer':'private-terminal-sentinel'},
                   {'grant_state':'INVALID'}, {'grant_id':'../spoof'},
                   {'request_expires_at':'110'}, {'persisted':1}, {'answer_saved':False}]
        for change in mutations:
            def fault(name,args):
                return self.envelope({**self.box.dispatch(name,args),**change})
            self.fault=fault
            result=MailboxWorkflow(self.factory).inspect(KEY)
            self.assertEqual(result['status'],'UNKNOWN',change)
            self.assertNotIn('private-',json.dumps(result));self.assertEqual(result['write_attempts'],0)

    def test_policy_or_grant_identity_change_between_calls_is_unknown(self):
        self.answer()
        for change in [{'policy_sha256':'0'*64},{'grant_id':'00000000-0000-4000-8000-000000000099'}]:
            def fault(name,args):
                data=self.box.dispatch(name,args)
                if name==TOOLS[3]:data.update(change)
                return self.envelope(data)
            self.fault=fault
            result=MailboxWorkflow(self.factory).inspect(KEY)
            self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(result['code'],'MCP_POLICY_MISMATCH')
            self.assertNotIn('request',result);self.assertNotIn('answer',result)
            self.assertEqual(result['reconnections'],0)

    def test_backward_message_state_is_unknown_without_returning_body(self):
        self.answer()
        def fault(name,args):
            data=self.box.dispatch(name,args)
            if name==TOOLS[3]:
                data.update(status='PENDING',answer_saved=False)
                data.pop('answer');data.pop('answer_sha256')
            return self.envelope(data)
        self.fault=fault;result=MailboxWorkflow(self.factory).inspect(KEY)
        self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(result['code'],'MCP_STATE_INCONSISTENT')
        self.assertNotIn('request',result);self.assertNotIn('answer',result)

    def test_backward_grant_state_is_unknown_even_if_retained_answer_valid(self):
        self.answer();self.now=201
        def fault(name,args):
            data=self.box.dispatch(name,args)
            if name==TOOLS[3]:data['grant_state']='ACTIVE'
            return self.envelope(data)
        self.fault=fault;result=MailboxWorkflow(self.factory).inspect(KEY)
        self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(result['code'],'MCP_STATE_INCONSISTENT')
        self.assertNotIn('request',result);self.assertNotIn('answer',result)

    def test_persisted_row_cannot_disappear_when_revoked(self):
        self.box.dispatch(TOOLS[0],BUNDLE)
        def fault(name,args):
            if name==TOOLS[3]:
                self.box.revoke();data=self.box.dispatch(name,args)
                data.update(persisted=False)
                data.pop('request_expires_at');data.pop('answer_saved')
                return self.envelope(data)
        self.fault=fault;result=MailboxWorkflow(self.factory).inspect(KEY)
        self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(result['code'],'MCP_STATE_INCONSISTENT')

    def test_answer_hash_cannot_change_or_disappear_in_terminal_metadata(self):
        self.answer()
        for change in ['replace','remove']:
            def fault(name,args):
                if name==TOOLS[3]:
                    data=self.box.dispatch(name,args);data.pop('answer')
                    data.update(status='EXPIRED',grant_state='EXPIRED')
                    if change=='replace':data['answer_sha256']='0'*64
                    else:data.pop('answer_sha256');data['answer_saved']=False
                    return self.envelope(data)
            self.fault=fault;result=MailboxWorkflow(self.factory).inspect(KEY)
            self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(result['code'],'MCP_STATE_INCONSISTENT')


class GrantStdioFlagTests(unittest.TestCase):
    def test_state_flag_defaults_off_and_explicit_true_is_forwarded(self):
        for flag in ['missing',False,True]:
            state={'phase':'READY','chat_mailbox_enabled':True,'local_llm_enabled':False,'local_models_enabled':False}
            if flag!='missing':state['mailbox_grants_enabled']=flag
            with patch('poc_services.Services') as manager,patch.object(module.subprocess,'Popen',side_effect=OperationError('CAPTURE_ONLY')) as process:
                manager.return_value.root=Path('/tmp/offline-fixture');manager.return_value.read_state.return_value=state
                with self.assertRaisesRegex(OperationError,'CAPTURE_ONLY'):StdioMailbox('/tmp/offline-fixture')
                argv=process.call_args.args[0]
                self.assertEqual(argv.count('--allow-mailbox-grants'),int(flag is True))
                self.assertIn('--allow-chat-mailbox',argv)

    def test_non_boolean_grant_flag_never_launches_adapter(self):
        for flag in [None,1,'true',[],{}]:
            with patch('poc_services.Services') as manager,patch.object(module.subprocess,'Popen') as process:
                manager.return_value.root=Path('/tmp/offline-fixture')
                manager.return_value.read_state.return_value={'phase':'READY','chat_mailbox_enabled':True,
                    'local_llm_enabled':False,'local_models_enabled':False,'mailbox_grants_enabled':flag}
                with self.assertRaisesRegex(OperationError,'INVALID_INVOCATION_STATE'):StdioMailbox('/tmp/offline-fixture')
                process.assert_not_called()


if __name__=='__main__':unittest.main()
