"""Prepared grants only: private SQLite fixtures, no MCP scope changes or services."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'src'))
from chat_mailbox import MailboxError,TOOLS,digest
from mailbox_grants import GrantError,GrantPolicy,GrantLedger,DAY,MAX_DB_BYTES,PURPOSE

GRANT='00000000-0000-4000-8000-000000000001'
TASK='00000000-0000-4000-8000-000000000002'
REQUEST='00000000-0000-4000-8000-000000000003'
QUESTION='Non-secret development question'
KEY={'task_id':TASK,'request_id':REQUEST,'request_sha256':digest(QUESTION)}


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='bounded-grant-unit-');self.root=Path(self.temp.name)
        self.now=1000
        self.policy=GrantPolicy.from_dict({'schema_version':1,'grant_id':GRANT,'task_id':TASK,
            'purpose':PURPOSE,'not_before':1000,'expires_at':1000+7*DAY,'request_ttl_seconds':DAY,
            'max_requests':32,'max_pending':8,'max_text_bytes':4096,'retain_seconds':7*DAY,
            'audit_retention_seconds':30*DAY})
        self.box=GrantLedger(self.root,self.policy,enabled=True,clock=lambda:self.now)
    def tearDown(self):self.temp.cleanup()
    def request(self,key=KEY,text=QUESTION):return self.box.dispatch(TOOLS[0],{**key,'request':text})
    def answer(self,key=KEY,text='Non-secret answer'):return self.box.dispatch(TOOLS[2],{**key,'answer':text})
    def key(self,n,text=QUESTION):return {'task_id':TASK,'request_id':str(uuid.UUID(int=n+100)),
                                          'request_sha256':digest(text)}

    def test_default_off_does_not_create_or_inspect_a_runtime(self):
        absent=self.root/'not-created'
        with self.assertRaisesRegex(GrantError,'DISABLED'):GrantLedger(absent,self.policy)
        self.assertFalse(absent.exists())

    def test_policy_exact_fields_uuid_ceiling_and_purpose(self):
        original=self.policy.metadata()
        cases=[dict(original,schema_version=True),dict(original,grant_id='../escape'),dict(original,purpose='other'),
               dict(original,expires_at=original['expires_at']+1),dict(original,max_requests=33),
               dict(original,max_pending=9),dict(original,request_ttl_seconds=DAY+1),
               dict(original,max_text_bytes=4097),dict(original,retain_seconds=7*DAY+1),
               dict(original,audit_retention_seconds=30*DAY+1),dict(original,max_pending=True),
               dict(original,not_before=original['expires_at']),dict(original,path='/outside')]
        for case in cases:
            with self.subTest(fields=list(case)),self.assertRaises(GrantError):GrantPolicy.from_dict(case)

    def test_dynamic_requests_first_pin_without_reinstalling_policy(self):
        original_hash=self.policy.sha256()
        self.assertEqual(self.request()['status'],'PENDING')
        self.answer()
        second=self.key(1,'Another non-secret question')
        self.assertEqual(self.request(second,'Another non-secret question')['status'],'PENDING')
        self.assertEqual(self.policy.sha256(),original_hash)
        self.assertEqual(self.box.dispatch(TOOLS[1],KEY)['request'],QUESTION)
        self.assertFalse(self.box.dispatch(TOOLS[3],KEY)['identity_verified'])

    def test_scope_fields_paths_commands_and_other_workstream_rejected(self):
        for args in [{**KEY,'task_id':GRANT},{**KEY,'path':'/etc/passwd'},
                     {**KEY,'url':'https://other'}, {**KEY,'command':'true'}]:
            with self.subTest(fields=list(args)),self.assertRaises(MailboxError):self.box.dispatch(TOOLS[1],args)
        self.assertEqual(self.box.dispatch(TOOLS[1],KEY)['status'],'NOT_FOUND')

    def test_duplicate_is_idempotent_and_request_answer_conflicts_rejected(self):
        first=self.request();self.now+=10;again=self.request()
        self.assertTrue(again['duplicate']);self.assertEqual(first['request_expires_at'],again['request_expires_at'])
        changed={**KEY,'request_sha256':digest('Different')}
        with self.assertRaisesRegex(GrantError,'HASH_CONFLICT'):self.request(changed,'Different')
        self.answer();self.assertTrue(self.answer()['duplicate'])
        with self.assertRaisesRegex(GrantError,'ANSWER_CONTENT_CONFLICT'):self.answer(text='Different answer')
        with self.assertRaises(MailboxError):self.box.dispatch(TOOLS[2],{**KEY,'answer':'x','answer_sha256':'0'*64})

    def test_pending_and_total_limits_and_answer_frees_only_pending_slot(self):
        for n in range(8):self.request(self.key(n))
        with self.assertRaisesRegex(GrantError,'PENDING_LIMIT'):self.request(self.key(8))
        self.answer(self.key(0));self.request(self.key(8))
        for n in range(1,9):self.answer(self.key(n))
        for n in range(9,32):self.request(self.key(n));self.answer(self.key(n))
        with self.assertRaisesRegex(GrantError,'TOTAL_LIMIT'):self.request(self.key(32))
        self.assertTrue(self.request(self.key(0))['duplicate'])

    def test_concurrent_budget_check_and_insert_are_atomic(self):
        def register(n):
            try:return self.request(self.key(n))['status']
            except GrantError as exc:return exc.code
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(register,range(12)))
        self.assertEqual(results.count('PENDING'),8);self.assertEqual(results.count('GRANT_PENDING_LIMIT'),4)

    def test_concurrent_first_hash_pin_has_one_winner(self):
        def register(text):
            try:return self.request({**KEY,'request_sha256':digest(text)},text)['status']
            except GrantError as exc:return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(register,['Candidate A','Candidate B']))
        self.assertEqual(sorted(results),['PENDING','REQUEST_HASH_CONFLICT'])

    def test_grant_window_blocks_all_writes_and_request_ttl_does_not_extend(self):
        self.now=self.policy.not_before-1
        with self.assertRaisesRegex(GrantError,'NOT_YET_ACTIVE'):self.request()
        self.now=self.policy.not_before;first=self.request()
        self.now=first['request_expires_at']
        result=self.box.dispatch(TOOLS[1],KEY)
        self.assertEqual(result['status'],'EXPIRED');self.assertNotIn('request',result)
        with self.assertRaisesRegex(GrantError,'REQUEST_EXPIRED'):self.answer()
        self.now=self.policy.expires_at
        with self.assertRaisesRegex(GrantError,'GRANT_EXPIRED'):self.request(self.key(99))

    def test_answer_available_for_seven_days_then_metadata_only(self):
        self.now=self.policy.expires_at-DAY//2
        self.request();self.now+=1;self.answer()
        answered=self.now
        self.now=self.policy.expires_at+1
        self.assertEqual(self.box.dispatch(TOOLS[3],KEY)['status'],'ANSWERED')
        self.now=answered+self.policy.retain_seconds
        result=self.box.dispatch(TOOLS[3],KEY)
        self.assertEqual(result['status'],'EXPIRED');self.assertNotIn('answer',result)
        self.assertTrue(result['answer_saved'])

    def test_revocation_is_durable_stops_writes_and_hides_contents(self):
        self.request();self.answer();self.assertEqual(self.box.revoke()['status'],'REVOKED')
        self.assertEqual(self.box.revoke()['status'],'REVOKED')
        for name,args in [(TOOLS[0],{**KEY,'request':QUESTION}),(TOOLS[2],{**KEY,'answer':'Non-secret answer'})]:
            with self.assertRaisesRegex(GrantError,'GRANT_REVOKED'):self.box.dispatch(name,args)
        reopened=GrantLedger(self.root,self.policy,enabled=True,clock=lambda:self.now)
        for name in [TOOLS[1],TOOLS[3]]:
            result=reopened.dispatch(name,KEY)
            self.assertEqual(result['status'],'REVOKED');self.assertTrue(result['persisted'])
            self.assertNotIn('request',result);self.assertNotIn('answer',result)
        with self.assertRaisesRegex(GrantError,'POLICY_CONFLICT'):
            GrantLedger(self.root,replace(self.policy,expires_at=self.policy.expires_at-1),enabled=True)

    def test_lost_ack_is_unknown_and_getter_reconciles_committed_state(self):
        commit=self.box._commit
        def lose(db):commit(db);raise OSError('private-fixture-ack')
        with patch.object(self.box,'_commit',side_effect=lose):
            with self.assertRaisesRegex(GrantError,'WRITE_UNKNOWN') as caught:self.request()
        self.assertEqual(caught.exception.status,'UNKNOWN')
        self.assertEqual(self.box.dispatch(TOOLS[1],KEY)['request'],QUESTION)
        with patch.object(self.box,'_commit',side_effect=lose):
            with self.assertRaisesRegex(GrantError,'WRITE_UNKNOWN'):self.answer()
        self.assertEqual(self.box.dispatch(TOOLS[3],KEY)['answer_sha256'],digest('Non-secret answer'))

    def test_failure_before_commit_rolls_back_without_hidden_retry(self):
        with patch.object(self.box,'_commit',side_effect=OSError('fixture before commit')):
            with self.assertRaisesRegex(GrantError,'WRITE_UNKNOWN'):self.request()
        self.assertEqual(self.box.dispatch(TOOLS[1],KEY)['status'],'NOT_FOUND')

    def test_utf8_bounds_and_corruption_fail_closed(self):
        for text in ['x'*4097,'\u3042'*1366,'\ud800']:
            with self.subTest(size=len(text)),self.assertRaises(MailboxError):
                self.box.dispatch(TOOLS[0],{**KEY,'request':text})
        self.request();self.answer()
        with sqlite3.connect(self.box.path) as db:db.execute("UPDATE messages SET answer='tampered'")
        with self.assertRaisesRegex(GrantError,'INTEGRITY_UNKNOWN'):self.box.dispatch(TOOLS[3],KEY)

    def test_audit_is_metadata_only_with_logical_thirty_day_view(self):
        self.request();self.answer();self.box.revoke()
        records=self.box.audit_metadata()
        self.assertEqual([r['event'] for r in records],['register','answer','revoke'])
        self.assertNotIn(QUESTION,json.dumps(records));self.assertNotIn('Non-secret answer',json.dumps(records))
        before=self.box.path.stat().st_size;self.now+=31*DAY
        self.assertEqual(self.box.audit_metadata(),[])
        self.assertEqual(self.box.path.stat().st_size,before)  # No implicit deletion or retention promise.

    def test_corrupted_expiry_metadata_is_unknown_not_an_unhandled_exception(self):
        self.request()
        with sqlite3.connect(self.box.path) as db:db.execute("UPDATE messages SET request_expires_at='invalid'")
        with self.assertRaisesRegex(GrantError,'INTEGRITY_UNKNOWN') as caught:self.box.dispatch(TOOLS[1],KEY)
        self.assertEqual(caught.exception.status,'UNKNOWN')

    def test_separate_ledger_size_permissions_and_existing_tools_unchanged(self):
        import mcp_adapter
        self.assertEqual(len(mcp_adapter.tool_definitions(False,True,True,True)),11)
        self.assertNotIn('grant',[t['name'] for t in mcp_adapter.tool_definitions(False,True,True,True)])
        self.assertFalse((self.root/'chat-mailbox.sqlite').exists())
        self.box.path.chmod(0o644)
        with self.assertRaises(MailboxError):self.request()
        self.box.path.chmod(0o600)
        with self.box.path.open('ab') as out:out.truncate(MAX_DB_BYTES+1)
        with self.assertRaises(MailboxError):self.box.dispatch(TOOLS[1],KEY)

if __name__=='__main__':unittest.main()
