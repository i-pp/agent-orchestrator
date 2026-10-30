"""Offline real MCP stdio and owner CLI, with no service/network/model/namespace."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'src'),str(PROJECT/'scripts')]
from chat_mailbox import Mailbox,TOOLS,digest
from mailbox_grants import GrantPolicy,GrantLedger,GrantError,load_runtime_grant,DAY,PURPOSE
from mailbox_operations import MailboxWorkflow,StdioMailbox
from poc_services import Services,MARKER,MODE
import mcp_adapter as mcp

TASK='ef28b268-b62a-5e30-8796-83681f872e35'
GRANT='d6cac07c-5dda-5cd9-acc5-a3e6494ebb31'
STATIC={'task_id':'00000000-0000-4000-8000-000000000001',
        'request_id':'00000000-0000-4000-8000-000000000002','request_sha256':digest('Static question')}
KEY={'task_id':TASK,'request_id':'00000000-0000-4000-8000-000000000003','request_sha256':digest('Grant question')}
BUNDLE={**KEY,'request':'Grant question'}

class ConnectionTests(unittest.TestCase):
    def setUp(self):
        (PROJECT/'.runtime').mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=PROJECT/'.runtime',prefix='development-poc-grant-connection-')
        self.root=Path(self.tmp.name);(self.root/'logs').mkdir(mode=0o700)
        self.write(MARKER,{'schema':1,'mode':MODE,'runtime':str(self.root)})
        self.write('chat-mailbox-policy.json',{'schema':1,'allowed':[STATIC]})
        self.policy=GrantPolicy.from_dict({'schema_version':1,'grant_id':GRANT,'task_id':TASK,
            'purpose':PURPOSE,'not_before':int(time.time())-2,'expires_at':int(time.time())-2+7*DAY,
            'request_ttl_seconds':DAY,'max_requests':32,'max_pending':8,'max_text_bytes':4096,
            'retain_seconds':7*DAY,'audit_retention_seconds':30*DAY})
        self.config()
        manager=Services(runtime=self.root)
        self.state={'schema':1,'runtime':str(self.root),'token':'a'*32,'units':manager.units,
            'invocations':{},'phase':'READY','local_llm_enabled':False,'local_models_enabled':False,
            'chat_mailbox_enabled':True,'mailbox_grants_enabled':True}
        self.write('poc-services-state.json',self.state)
    def tearDown(self):self.tmp.cleanup()
    def write(self,name,value):
        path=self.root/name;path.write_text(json.dumps(value));path.chmod(0o600)
    def config(self,enabled=True):self.write('chat-mailbox-grant.json',{'schema_version':1,'enabled':enabled,
        'policy':self.policy.metadata(),'policy_sha256':self.policy.sha256()})
    def adapter(self,enabled=True):return mcp.Adapter(self.root,allow_chat_mailbox=True,allow_mailbox_grants=enabled)
    def cli(self,operation,value,include=False):
        cmd=[sys.executable,'-B',str(PROJECT/'scripts/mailbox_ops.py'),operation,'--runtime',str(self.root)]
        if include:cmd+=['--include-content']
        p=subprocess.run(cmd,input=json.dumps(value),text=True,capture_output=True,timeout=10)
        return p,json.loads(p.stdout)
    def owner(self,operation,apply=False,**overrides):
        cmd=[sys.executable,'-B',str(PROJECT/'scripts/mailbox_grant_admin.py'),operation,'--runtime',str(self.root),
             '--grant-id',overrides.get('grant_id',GRANT),'--policy-sha256',overrides.get('sha',self.policy.sha256())]
        if apply:cmd+=['--apply']
        p=subprocess.run(cmd,input='',capture_output=True,text=True,timeout=5)
        return p,json.loads(p.stdout)
    def test_loader_strict_private_shape_hash_and_default_off(self):
        self.assertEqual(load_runtime_grant(self.root),self.policy)
        self.config(False)
        with self.assertRaisesRegex(GrantError,'DISABLED'):load_runtime_grant(self.root)
        self.assertEqual(load_runtime_grant(self.root,require_enabled=False),self.policy)
        self.config();path=self.root/'chat-mailbox-grant.json';base=json.loads(path.read_text())
        for change in [{'enabled':'true'},{'policy_sha256':'0'*64},{'extra':'field'}]:
            self.write(path.name,{**base,**change})
            with self.assertRaises(GrantError):load_runtime_grant(self.root)
        path.write_text('{"schema_version":1,"schema_version":1}');path.chmod(0o600)
        with self.assertRaises(GrantError):load_runtime_grant(self.root)
        self.config();path.chmod(0o644)
        with self.assertRaises(GrantError):load_runtime_grant(self.root)
    def test_portable_example_is_strict_shape_but_default_disabled(self):
        example=json.loads((PROJECT/'examples/mailbox-grant.example.json').read_text())
        self.write('chat-mailbox-grant.json',example)
        with self.assertRaisesRegex(GrantError,'DISABLED'):load_runtime_grant(self.root)
        self.assertEqual(load_runtime_grant(self.root,require_enabled=False).sha256(),example['policy_sha256'])
    def test_default_off_ignores_grant_file_and_does_not_create_ledger(self):
        self.write('chat-mailbox-grant.json',{'malformed':True})
        a=self.adapter(False)
        try:
            self.assertIsNone(a.grant);self.assertFalse((self.root/'mailbox-grants-prepared.sqlite').exists())
        finally:a.audit.close()
        with self.assertRaises(ValueError):mcp.Adapter(self.root,allow_mailbox_grants=True)
    def test_catalog_unchanged_and_static_task_collision_rejected(self):
        a=self.adapter();b=self.adapter(False)
        try:self.assertEqual(a.tools,b.tools)
        finally:a.audit.close();b.audit.close()
        self.policy=replace(self.policy,task_id=STATIC['task_id']);self.config()
        with self.assertRaisesRegex(ValueError,'conflicts'):self.adapter()
    def test_stdio_and_cli_register_answer_inspect_and_no_content_by_default(self):
        p,prepared=self.cli('prepare',{k:BUNDLE[k] for k in ['task_id','request_id','request']})
        self.assertEqual(p.returncode,0,p.stderr);self.assertFalse(prepared['install_performed'])
        self.assertFalse(prepared['write_permission_verified'])
        self.assertFalse((self.root/'mailbox-grants-prepared.sqlite').exists())
        p,result=self.cli('register',BUNDLE);self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(result['status'],'PENDING');self.assertEqual(result['write_attempts'],1)
        t=StdioMailbox(self.root)
        try:self.assertEqual(t.exchange(TOOLS[2],{**KEY,'answer':'Operator synthetic answer'})['data']['status'],'ANSWERED')
        finally:t.close()
        p,result=self.cli('inspect',KEY);self.assertEqual(p.returncode,0,p.stderr)
        self.assertNotIn('Operator synthetic answer',p.stdout);self.assertNotIn('Grant question',p.stdout)
        p,result=self.cli('inspect',KEY,True);self.assertEqual(result['answer'],'Operator synthetic answer')
        p,result=self.cli('register',BUNDLE);self.assertEqual(result['write_attempts'],0)
        self.assertEqual(self.owner('status')[1]['total_requests'],1)
    def test_owner_requires_explicit_apply_exact_confirmation_and_never_creates_missing_db(self):
        p,result=self.owner('status');self.assertEqual(p.returncode,2)
        self.assertFalse((self.root/'mailbox-grants-prepared.sqlite').exists())
        GrantLedger(self.root,self.policy,enabled=True)
        p,result=self.owner('revoke');self.assertEqual(result['status'],'REQUIRES_APPLY')
        p,result=self.owner('revoke',True,sha='0'*64);self.assertEqual(result['code'],'GRANT_OWNER_CONFIRMATION_MISMATCH')
        self.assertEqual(self.owner('status')[1]['status'],'ACTIVE')
    def test_owner_revocation_persists_reconnect_and_body_suppression(self):
        self.cli('register',BUNDLE)
        p,result=self.owner('revoke',True);self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(result['status'],'REVOKED');self.assertEqual(self.owner('revoke',True)[1]['status'],'REVOKED')
        p,result=self.cli('inspect',KEY,True);self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(result['status'],'REVOKED');self.assertNotIn('request',result)
        p,result=self.cli('register',BUNDLE);self.assertEqual(result['status'],'BLOCKED');self.assertEqual(result['write_attempts'],0)
        self.config(False);self.assertEqual(self.owner('status')[1]['status'],'REVOKED')
    def test_owner_lost_ack_reports_unknown_then_status_reconciles_without_retry(self):
        import mailbox_grant_admin as owner
        ledger=GrantLedger(self.root,self.policy,enabled=True)
        commit=ledger._commit
        def lose(db):commit(db);raise OSError('private-ack-sentinel')
        out=io.StringIO()
        with patch.object(owner,'GrantLedger',return_value=ledger),patch.object(ledger,'_commit',side_effect=lose) as called,contextlib.redirect_stdout(out):
            rc=owner.main(['revoke','--runtime',str(self.root),'--grant-id',GRANT,
                '--policy-sha256',self.policy.sha256(),'--apply'])
        self.assertEqual(rc,2);self.assertEqual(called.call_count,1)
        self.assertEqual(json.loads(out.getvalue())['status'],'UNKNOWN')
        self.assertNotIn('private-',out.getvalue())
        self.assertEqual(self.owner('status')[1]['status'],'REVOKED')
    def test_rollback_off_keeps_static_and_ledger_then_same_policy_restores_budget(self):
        self.cli('register',BUNDLE)
        old=Mailbox(self.root);old.dispatch(TOOLS[0],{**STATIC,'request':'Static question'})
        old.dispatch(TOOLS[2],{**STATIC,'answer':'Existing static answer'})
        self.state['mailbox_grants_enabled']=False;self.write('poc-services-state.json',self.state)
        p,result=self.cli('register',BUNDLE);self.assertEqual(result['status'],'BLOCKED')
        p,result=self.cli('inspect',STATIC,True);self.assertEqual(result['answer'],'Existing static answer')
        self.assertEqual(self.owner('status')[1]['total_requests'],1)
        self.state['mailbox_grants_enabled']=True;self.write('poc-services-state.json',self.state)
        p,result=self.cli('register',BUNDLE);self.assertEqual(result['write_attempts'],0)
        self.assertEqual(self.owner('status')[1]['total_requests'],1)
    def test_audit_no_bodies_or_rejected_input_and_contains_grant_id(self):
        a=self.adapter()
        def call(name,args):return a.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
            '_meta':{mcp.VERSION_META:mcp.REQUEST_PROTOCOL,mcp.CAPABILITIES_META:{}},'name':name,'arguments':args}})
        try:
            call(TOOLS[0],BUNDLE);call(TOOLS[2],{**KEY,'answer':'private-answer-sentinel'})
            call(TOOLS[0],{**BUNDLE,'path':'private-path-sentinel'})
            raw=a.audit_path.read_text();self.assertNotIn('private-',raw);self.assertNotIn(BUNDLE['request'],raw)
            rows=[json.loads(v) for v in raw.splitlines()]
            self.assertTrue(any(r.get('mailbox',{}).get('grant_id')==GRANT for r in rows))
        finally:a.audit.close()
    def test_total_race_counts_expired_requests_and_cannot_reset_by_reopen(self):
        clock=[self.policy.not_before]
        p=replace(self.policy,max_requests=4,max_pending=4,request_ttl_seconds=1)
        self.policy=p;self.config();box=GrantLedger(self.root,p,enabled=True,clock=lambda:clock[0])
        def reg(n):
            try:return box.dispatch(TOOLS[0],{**BUNDLE,'request_id':str(uuid.UUID(int=100+n))})['status']
            except GrantError as e:return e.code
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(reg,range(10)))
        self.assertEqual(results.count('PENDING'),4);self.assertEqual(results.count('GRANT_TOTAL_LIMIT'),6)
        clock[0]+=2
        self.assertEqual(box.status()['pending_requests'],0);self.assertEqual(reg(99),'GRANT_TOTAL_LIMIT')
        reopened=GrantLedger(self.root,p,enabled=True,clock=lambda:clock[0]);self.assertEqual(reopened.status()['total_requests'],4)
    def test_expiry_checked_after_lock_and_before_commit_rolls_back(self):
        now=self.policy.not_before
        box=GrantLedger(self.root,self.policy,enabled=True,clock=lambda:now)
        with patch.object(box,'_now',side_effect=[now,self.policy.expires_at]):
            with self.assertRaisesRegex(GrantError,'GRANT_EXPIRED'):box.dispatch(TOOLS[0],BUNDLE)
        self.assertEqual(box.status()['total_requests'],0)
        # The transaction/lock is acquired before the clock is consulted.
        with sqlite3.connect(box.path,timeout=1,isolation_level=None) as db:
            db.execute('BEGIN IMMEDIATE')
            with patch.object(box,'_now',side_effect=AssertionError('clock read before lock')):
                with self.assertRaisesRegex(GrantError,'WRITE_UNKNOWN'):box.dispatch(TOOLS[0],BUNDLE)
            db.rollback()
    def test_expiry_write_and_read_boundaries_are_distinct(self):
        clock=[self.policy.not_before];box=GrantLedger(self.root,self.policy,enabled=True,clock=lambda:clock[0])
        box.dispatch(TOOLS[0],BUNDLE);clock[0]+=DAY
        with self.assertRaisesRegex(GrantError,'REQUEST_EXPIRED'):box.dispatch(TOOLS[0],BUNDLE)
        self.assertEqual(box.dispatch(TOOLS[1],KEY)['status'],'EXPIRED')
        self.assertNotIn('request',box.dispatch(TOOLS[1],KEY))
        new={**BUNDLE,'request_id':str(uuid.UUID(int=99))}
        clock[0]=self.policy.expires_at-2;box.dispatch(TOOLS[0],new);clock[0]+=1
        key={k:new[k] for k in KEY};box.dispatch(TOOLS[2],{**key,'answer':'retained'})
        clock[0]=self.policy.expires_at
        with self.assertRaisesRegex(GrantError,'GRANT_EXPIRED'):box.dispatch(TOOLS[2],{**key,'answer':'retained'})
        self.assertEqual(box.dispatch(TOOLS[3],key)['answer'],'retained')
        clock[0]+=7*DAY
        self.assertNotIn('answer',box.dispatch(TOOLS[3],key))

if __name__=='__main__':unittest.main()
