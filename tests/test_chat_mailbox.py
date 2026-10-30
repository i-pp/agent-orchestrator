"""No model, network, namespace, live services or production mailbox writes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'src'),str(PROJECT/'scripts')]
from chat_mailbox import Mailbox, MailboxError, TOOLS, MAX_TEXT_BYTES, digest
import mcp_adapter as mcp
from poc_services import Services

TASK='00000000-0000-4000-8000-000000000001'
REQUEST='00000000-0000-4000-8000-000000000002'
TEXT='Non-secret fixture question: choose a small reversible design.'
KEY={'task_id':TASK,'request_id':REQUEST,'request_sha256':digest(TEXT)}


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='mailbox-unit-')
        self.root=Path(self.temp.name)
        (self.root/'logs').mkdir(mode=0o700)
        self.policy=self.root/'chat-mailbox-policy.json'
        self.policy.write_text(json.dumps({'schema':1,'allowed':[KEY]}));self.policy.chmod(0o600)
        self.box=Mailbox(self.root)

    def tearDown(self):self.temp.cleanup()
    def register(self):return self.box.dispatch(TOOLS[0],{**KEY,'request':TEXT})
    def answer(self,text='Non-secret fixture answer'):
        return self.box.dispatch(TOOLS[2],{**KEY,'answer':text,'answer_sha256':digest(text)})

    def test_roundtrip_immutable_question_answer_and_identity_limit(self):
        self.assertEqual(self.box.dispatch(TOOLS[1],KEY)['status'],'NOT_FOUND')
        self.assertEqual(self.register()['status'],'PENDING')
        self.assertEqual(self.box.dispatch(TOOLS[1],KEY)['request'],TEXT)
        self.answer();reply=self.box.dispatch(TOOLS[3],KEY)
        self.assertEqual(reply['answer'],'Non-secret fixture answer')
        self.assertFalse(reply['identity_verified'])
        self.assertNotIn('request',reply)
        self.assertEqual(Mailbox(self.root).dispatch(TOOLS[3],KEY),reply)

    def test_duplicate_and_conflicting_answer(self):
        self.register();self.assertTrue(self.register()['duplicate'])
        self.answer();self.assertTrue(self.answer()['duplicate'])
        with self.assertRaisesRegex(MailboxError,'ANSWER_CONTENT_CONFLICT'):self.answer('Different answer')
        with self.assertRaisesRegex(MailboxError,'ANSWER_CONTENT_CONFLICT'):
            self.box.dispatch(TOOLS[3],{**KEY,'expected_answer_sha256':digest('Other expected answer')})

    def test_unknown_ids_hashes_fields_and_path_url_command_are_refused(self):
        cases=[{**KEY,'task_id':'../secrets'},{**KEY,'request_id':'http://other'},
               {**KEY,'task_id':'00000000-0000-4000-8000-000000000003'},
               {**KEY,'request_sha256':'f'*64},{**KEY,'path':'/outside'},
               {**KEY,'url':'http://other'},{**KEY,'command':'true'}]
        for args in cases:
            with self.subTest(args=list(args)),self.assertRaises(MailboxError):self.box.dispatch(TOOLS[1],args)
        with self.assertRaisesRegex(MailboxError,'CONTENT_HASH_MISMATCH'):
            self.box.dispatch(TOOLS[0],{**KEY,'request':'Changed question'})
        with self.assertRaisesRegex(MailboxError,'REQUEST_NOT_FOUND'):self.answer()

    def test_utf8_size_bounds_and_surrogates(self):
        for text in ['x'*(MAX_TEXT_BYTES+1),'あ'*(MAX_TEXT_BYTES//3+1),'\ud800']:
            with self.subTest(size=len(text)),self.assertRaises(MailboxError):
                self.box.dispatch(TOOLS[0],{**KEY,'request':text})

    def test_policy_permissions_duplicate_ids_and_symlink_are_rejected(self):
        self.policy.chmod(0o644)
        with self.assertRaises(MailboxError):Mailbox(self.root)
        self.policy.chmod(0o600);self.policy.write_text(json.dumps({'schema':1,'allowed':[KEY,KEY]}))
        with self.assertRaisesRegex(MailboxError,'DUPLICATE_MAILBOX_SLOT'):Mailbox(self.root)
        self.policy.unlink();self.policy.symlink_to('/nonexistent-fixture')
        with self.assertRaises(MailboxError):Mailbox(self.root)

    def test_storage_corruption_is_not_returned_as_answer(self):
        self.register();self.answer()
        with sqlite3.connect(self.box.path) as db:db.execute("UPDATE mailbox SET answer='tampered'")
        with self.assertRaisesRegex(MailboxError,'MAILBOX_INTEGRITY_ERROR'):
            self.box.dispatch(TOOLS[3],KEY)

    def test_parallel_same_request_and_conflicting_answers(self):
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:self.register(),range(8)))
        self.assertEqual(sum(not r['duplicate'] for r in results),1)
        def save(text):
            try:return self.answer(text)['status']
            except MailboxError as exc:return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(save,['Answer A','Answer B']))
        self.assertEqual(sorted(results),['ANSWERED','ANSWER_CONTENT_CONFLICT'])

    def test_lost_acknowledgement_is_unknown_then_reconciled_by_getter(self):
        original=self.box._commit
        def lose_ack(db):original(db);raise OSError('fixture acknowledgement lost')
        with patch.object(self.box,'_commit',side_effect=lose_ack):
            with self.assertRaisesRegex(MailboxError,'MAILBOX_WRITE_UNKNOWN') as caught:self.register()
        self.assertEqual(caught.exception.status,'UNKNOWN')
        self.assertEqual(self.box.dispatch(TOOLS[1],KEY)['request'],TEXT)
        with patch.object(self.box,'_commit',side_effect=lose_ack):
            with self.assertRaisesRegex(MailboxError,'MAILBOX_WRITE_UNKNOWN'):self.answer()
        self.assertEqual(self.box.dispatch(TOOLS[3],{**KEY,'expected_answer_sha256':digest('Non-secret fixture answer')})['status'],'ANSWERED')

    def test_process_crash_before_and_after_commit(self):
        code='''import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from chat_mailbox import Mailbox,TOOLS
import json
b=Mailbox(Path(sys.argv[2]));args=json.loads(sys.argv[3])
original=b._commit
def crash(db):
 if sys.argv[4]=='after':original(db)
 os._exit(23)
b._commit=crash
b.dispatch(TOOLS[0],args)
'''
        args=json.dumps({**KEY,'request':TEXT})
        for point,status in [('before','NOT_FOUND'),('after','PENDING')]:
            result=subprocess.run([sys.executable,'-B','-c',code,str(PROJECT/'src'),str(self.root),args,point],capture_output=True,timeout=5)
            self.assertEqual(result.returncode,23)
            self.assertEqual(Mailbox(self.root).dispatch(TOOLS[1],KEY)['status'],status)

    def test_default_seven_tools_unchanged_and_disabled_mailbox_not_created(self):
        fresh=self.root/'disabled';fresh.mkdir(mode=0o700);(fresh/'logs').mkdir(mode=0o700)
        with patch.object(mcp,'runtime_path',return_value=fresh),patch.object(mcp,'Mailbox',side_effect=AssertionError('Default off')):
            adapter=mcp.Adapter(fresh)
        try:
            self.assertEqual(len(mcp.tool_definitions(False,True,True)),7)
            self.assertFalse(any(t['name'] in TOOLS for t in mcp.tool_definitions(False,True,True)))
            self.assertIsNone(adapter.mailbox)
            self.assertFalse((fresh/'chat-mailbox.sqlite').exists())
            with self.assertRaises(mcp.Invalid):adapter.call_tool(TOOLS[0],{**KEY,'request':TEXT},1)
        finally:adapter.audit.close()

    def test_answer_hash_is_server_computed_and_optional_client_hash_is_checked(self):
        self.register()
        reply=self.box.dispatch(TOOLS[2],{**KEY,'answer':'Server hashes this'})
        self.assertEqual(reply['answer_sha256'],digest('Server hashes this'))
        with self.assertRaisesRegex(MailboxError,'CONTENT_HASH_MISMATCH'):
            self.box.dispatch(TOOLS[2],{**KEY,'answer':'Server hashes this','answer_sha256':'a'*64})

    def test_slot_bound_and_wrong_database_file_are_refused(self):
        self.policy.write_text(json.dumps({'schema':1,'allowed':[KEY]*33}))
        with self.assertRaisesRegex(MailboxError,'INVALID_MAILBOX_POLICY'):Mailbox(self.root)
        self.box.path.chmod(0o644)
        with self.assertRaisesRegex(MailboxError,'UNSAFE_MAILBOX_FILE'):self.register()

    def test_real_stdio_mailbox_roundtrip_in_owned_fixture(self):
        from test_phase2_e1 import Client
        (PROJECT/'.runtime').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=PROJECT/'.runtime',prefix='mailbox-stdio-') as tmp:
            root=Path(tmp);(root/'logs').mkdir(mode=0o700)
            policy=root/'chat-mailbox-policy.json';policy.write_text(json.dumps({'schema':1,'allowed':[KEY]}));policy.chmod(0o600)
            code=f"import sys;sys.path.insert(0,{str(PROJECT/'src')!r});import mcp_adapter;sys.argv.append('--allow-chat-mailbox');raise SystemExit(mcp_adapter.main())"
            client=Client(root,server_code=code)
            try:
                self.assertIn('result',client.init())
                for name,args in [(TOOLS[0],{**KEY,'request':TEXT}),(TOOLS[1],KEY),
                                  (TOOLS[2],{**KEY,'answer':'Wire fixture answer'}),(TOOLS[3],KEY)]:
                    response=client.call(name,args)
                    self.assertFalse(response['result']['isError'],response)
                self.assertEqual(response['result']['structuredContent']['data']['answer'],'Wire fixture answer')
            finally:client.close()

    def test_enabled_adapter_roundtrip_is_local_and_payload_free_audit(self):
        with patch.object(mcp,'runtime_path',return_value=self.root):adapter=mcp.Adapter(self.root,allow_chat_mailbox=True)
        try:
            with patch.object(adapter,'daemon',side_effect=AssertionError('No scheduler calls')),patch.object(mcp.subprocess,'run',side_effect=AssertionError('No external command')):
                for name,args in [(TOOLS[0],{**KEY,'request':TEXT}),(TOOLS[1],KEY),
                                  (TOOLS[2],{**KEY,'answer':'Fixture answer','answer_sha256':digest('Fixture answer')}),(TOOLS[3],KEY)]:
                    self.assertFalse(adapter.call_tool(name,args,1)['isError'])
                out=adapter.call_tool(TOOLS[1],{**KEY,'request_sha256':'e'*64},1)
                self.assertTrue(out['isError']);self.assertEqual(out['structuredContent']['data']['code'],'MAILBOX_SLOT_NOT_ALLOWED')
            adapter.audit.flush();log=adapter.audit_path.read_text()
            self.assertNotIn(TEXT,log);self.assertNotIn('Fixture answer',log)
            self.assertIn(REQUEST,log);self.assertIn(KEY['request_sha256'],log)
            self.assertNotIn('orchestrator_submit_task',[t['name'] for t in adapter.tools])
        finally:adapter.audit.close()

    def test_message_bounds_and_service_optin_only(self):
        with patch.object(mcp,'runtime_path',return_value=self.root):adapter=mcp.Adapter(self.root,allow_chat_mailbox=True)
        try:
            self.register();self.answer('\x00'*4096)
            envelope=adapter.call_tool(TOOLS[3],KEY,1)
            self.assertLess(len(json.dumps(envelope,ensure_ascii=False).encode()),mcp.MAX_OUTPUT)
        finally:adapter.audit.close()
        service=Services.__new__(Services);service.root=Path('/tmp/public-fixture')
        service.codex=service.client=Path('/usr/bin/false');service.key=Path('/tmp/metadata-only')
        service.health=Path('/tmp/public-fixture/health');service.allow_local_llm=True;service.allow_local_models=True
        before=service.commands();service.allow_chat_mailbox=True;after=service.commands()
        self.assertEqual(before['daemon'],after['daemon'])
        self.assertNotIn('--allow-chat-mailbox',' '.join(before['tunnel']))
        self.assertIn('--allow-chat-mailbox',' '.join(after['tunnel']))
        self.assertNotIn('--allow-writes',' '.join(after['tunnel']))


if __name__=='__main__':unittest.main()
