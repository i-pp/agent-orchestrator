"""Socketless Python -> actual Worker/D1 SQL core -> synthetic native peer."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import unittest
from urllib.parse import urlencode
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from approval_portal import PortalError
from sites_approval_provider import SitesApprovalConfig, SitesApprovalProvider, SitesHTTPS
import test_codex_approval_bridge as bridge

ROOT=Path(__file__).resolve().parents[1]
CONFIG=SitesApprovalConfig('https://approval.example.test','fixture-local-owner-binding',
    'fixture-machine-key-000000000000000000','fixture-site-access-token-00000000000000')

class WorkerFixture:
    def __init__(self):
        self.process=subprocess.Popen(['node',str(ROOT/'tests/fixtures/sites-approval-rpc.mjs')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.lock=threading.Lock()
        self.calls=[]
        self.drop_consume=False
    def rpc(self, value):
        with self.lock:
            self.process.stdin.write(json.dumps(value)+'\n');self.process.stdin.flush()
            line=self.process.stdout.readline()
            if not line: raise RuntimeError('fixture process ended')
            return json.loads(line)
    def __call__(self, method,path,payload):
        self.calls.append((method,path,deepcopy(payload)))
        response=self.rpc({'method':method,'path':path,'body':json.dumps(payload) if payload is not None else None,
            'headers':{'authorization':'Bearer '+CONFIG.machine_key,'content-type':'application/json'}})
        if self.drop_consume and path.endswith('/consume'): raise RuntimeError('synthetic lost response')
        if response['status']!=200: raise RuntimeError('fixture request rejected')
        return json.loads(response['body'])
    def close(self):
        self.process.stdin.close();self.process.wait(timeout=3)
        self.process.stdout.close();self.process.stderr.close()

class RemoteProviderTests(unittest.TestCase):
    def setUp(self):
        self.f=bridge.ApprovalPipelineTests(methodName='runTest');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.pipeline=self.f.pipeline;self.record=self.f.capture()
        self.root=self.f.root/'remote-provider';self.root.mkdir(mode=0o700)
        self.now=[1000]
        self.worker=WorkerFixture();self.addCleanup(self.worker.close)
        self.provider=self.make_provider();self.pipeline.approval_portal=self.provider
        self.pipeline.human_decision_gate=self.provider.consume
    def lookup(self, ident):
        return next((a for a in self.pipeline.approval_get(bridge.TASK)['approvals'] if a['id']==ident),None)
    def make_provider(self, **kw):
        return SitesApprovalProvider(self.root,self.lookup,config=kw.get('config',CONFIG),transport=kw.get('transport',self.worker),clock=lambda:self.now[0])
    def issue(self):return self.provider.issue(self.record['id'])
    def form(self, interaction, subject='fixture-sites-subject'):
        response=self.worker.rpc({'method':'GET','path':'/approvals/'+interaction['challenge_id'],
            'headers':{'oai-authenticated-user-id':subject}})
        token=re.search('name="csrf" value="([^"]+)"',response['body'])
        return response, token.group(1) if token else None, (response['headers']['set-cookie'] or '').split(';')[0]
    def click(self, interaction, *, decision='accept', subject='fixture-sites-subject'):
        form,token,cookie=self.form(interaction,subject)
        if form['status']!=200:return form
        return self.worker.rpc({'method':'POST','path':'/approvals/'+interaction['challenge_id'],
            'headers':{'oai-authenticated-user-id':subject,'cookie':cookie,'origin':CONFIG.origin,'content-type':'application/x-www-form-urlencoded'},
            'body':urlencode({'csrf':token,'decision':decision,'request_sha256':self.record['request_sha256']})})
    def advance(self, seconds):
        self.now[0]+=seconds;self.worker.rpc({'op':'clock','now':self.now[0]})
    def test_default_unconfigured_cannot_send_any_request(self):
        p=self.make_provider(config=None)
        self.assertFalse(p.enabled)
        with self.assertRaisesRegex(PortalError,'UNAVAILABLE'):p.issue(self.record['id'])
        self.assertEqual(self.worker.calls,[])
    def test_credentials_are_not_repr_or_durable_state(self):
        self.issue();self.assertNotIn(CONFIG.machine_key,repr(CONFIG));self.assertNotIn(CONFIG.site_access_token,repr(CONFIG))
        data=self.provider.db.read_bytes()
        for s in (CONFIG.machine_key,CONFIG.site_access_token,'fixture-sites-subject'):self.assertNotIn(s.encode(),data)
    def test_issue_uses_exact_projection_not_native_rpc_or_raw_task(self):
        i=self.issue();self.assertEqual(i['url'],CONFIG.origin+'/approvals/'+i['challenge_id'])
        payload=self.worker.calls[0][2];self.assertNotIn('native_request_id',payload['record']);self.assertNotIn('human_approved',payload)
        self.assertFalse(i['execution_authorized']);self.assertEqual(self.f.client.calls,[])
    def test_url_and_machine_staging_supply_no_human_evidence(self):
        i=self.issue();self.assertEqual(self.provider.interaction_status(i['challenge_id'])['status'],'PENDING')
        with self.assertRaisesRegex(PortalError,'NO_UNAMBIGUOUS'):self.provider.consume(self.record,'accept')
        self.assertEqual(self.f.client.calls,[])
    def test_wrong_owner_cannot_view_or_click(self):
        i=self.issue();response=self.click(i,subject='another-fixture-person');self.assertEqual(response['status'],403)
        with self.assertRaises(PortalError):self.provider.consume(self.record,'accept')
    def test_cross_language_explicit_decision_then_real_pipeline_native_reply(self):
        i=self.pipeline.approval_begin(bridge.TASK,self.record['id'],self.record['request_sha256'],'remote-begin')
        self.assertEqual(i['status'],'PENDING');self.assertEqual(self.click(i)['status'],200)
        status=self.pipeline.approval_begin(bridge.TASK,self.record['id'],self.record['request_sha256'],'remote-begin')
        self.assertEqual(status['status'],'DECIDED')
        sent=self.pipeline.approval_decide(bridge.TASK,self.record['id'],self.record['request_sha256'],'accept','remote-decide')
        self.assertEqual(sent['status'],'SENT');self.assertTrue(sent['same_task_preserved']);self.assertFalse(sent['resume_confirmed'])
        self.assertEqual(len(self.f.client.calls),1)
        self.assertEqual(self.pipeline.approval_get(bridge.TASK)['task_state'],'WAITING_APPROVAL')
        self.f.client.flags=[]
        self.assertEqual(self.pipeline.refresh(bridge.TASK)['state'],'WAITING_APPROVAL')
        self.pipeline.receive({'method':'serverRequest/resolved','params':{'threadId':'native-thread','requestId':7}})
        self.assertEqual(self.pipeline.refresh(bridge.TASK)['state'],'RUNNING')
        self.assertEqual(self.pipeline._mutate(lambda s:list(s['tasks'])),[bridge.TASK])
    def test_mismatched_decision_does_not_consume_human_choice(self):
        i=self.issue();self.click(i,decision='decline')
        with self.assertRaisesRegex(PortalError,'NO_UNAMBIGUOUS'):self.provider.consume(self.record,'accept')
        self.assertTrue(self.provider.consume(self.record,'decline').startswith('sites-human:'))
    def test_concurrent_consume_has_one_native_gate_receipt(self):
        i=self.issue();self.click(i)
        def go(_):
            try:return self.provider.consume(self.record,'accept')
            except PortalError:return None
        with ThreadPoolExecutor(max_workers=6) as pool:answers=list(pool.map(go,range(6)))
        self.assertEqual(sum(a is not None for a in answers),1)
        self.assertEqual(sum(c[1].endswith('/consume') for c in self.worker.calls),1)
    def test_consumed_cannot_be_reissued_after_expiry_or_restart(self):
        i=self.issue();self.click(i);self.provider.consume(self.record,'accept');self.advance(120)
        p=self.make_provider()
        with self.assertRaisesRegex(PortalError,'CONSUMED_OR_RECONCILIATION'):p.issue(self.record['id'])
    def test_unused_expired_decision_requires_fresh_challenge_and_click(self):
        old=self.issue();self.click(old);self.advance(120)
        with self.assertRaises(PortalError):self.provider.consume(self.record,'accept')
        fresh=self.issue();self.assertNotEqual(fresh['challenge_id'],old['challenge_id'])
        with self.assertRaises(PortalError):self.provider.consume(self.record,'accept')
        self.click(fresh);self.assertTrue(self.provider.consume(self.record,'accept'))
    def test_lost_consume_response_is_durable_unknown_and_never_retried(self):
        i=self.issue();self.click(i);self.worker.drop_consume=True
        with self.assertRaises(PortalError):self.provider.consume(self.record,'accept')
        p=self.make_provider()
        with self.assertRaises(PortalError):p.consume(self.record,'accept')
        with self.assertRaises(PortalError):p.issue(self.record['id'])
        self.assertEqual(sum(c[1].endswith('/consume') for c in self.worker.calls),1)
        with closing(sqlite3.connect(p.db)) as db:self.assertEqual(db.execute('SELECT state FROM interactions').fetchone()[0],'UNKNOWN')
    def test_owner_pin_revocation_blocks_prior_decision(self):
        i=self.issue();self.click(i);self.worker.rpc({'op':'revoke'})
        self.assertEqual(self.provider.interaction_status(i['challenge_id'])['status'],'REVOKED')
        with self.assertRaises(PortalError):self.provider.consume(self.record,'accept')
    def test_changed_local_native_record_cannot_use_old_human_proof(self):
        i=self.issue();self.click(i)
        self.pipeline._mutate(lambda s:s['approvals'][self.record['id']].update(command_preview='different synthetic command'))
        with self.assertRaisesRegex(PortalError,'APPROVAL_CHANGED'):self.provider.consume(self.record,'accept')
    def test_invalid_remote_binding_url_or_receipt_is_not_evidence(self):
        i=self.issue();self.click(i)
        original=self.worker
        def bad(method,path,payload):
            result=original(method,path,payload);result['request_sha256']='b'*64;result['url']='https://untrusted.example.test';return result
        self.provider.transport=bad
        with self.assertRaisesRegex(PortalError,'BINDING_FAILED'):self.provider.consume(self.record,'accept')
        self.assertEqual(self.f.client.calls,[])
    def test_consume_reply_crossing_expiry_keeps_fence_but_returns_no_receipt(self):
        i=self.issue();self.click(i)
        original=self.worker
        def delayed(method,path,payload):
            result=original(method,path,payload)
            if path.endswith('/consume'):self.now[0]+=120
            return result
        self.provider.transport=delayed
        with self.assertRaisesRegex(PortalError,'EXPIRED_AFTER_CONSUMPTION'):self.provider.consume(self.record,'accept')
        with closing(sqlite3.connect(self.provider.db)) as db:self.assertEqual(db.execute('SELECT state FROM interactions').fetchone()[0],'CONSUMED')
        with self.assertRaises(PortalError):self.make_provider().issue(self.record['id'])
        self.assertEqual(self.f.client.calls,[])

    def test_stage_response_loss_reuses_the_same_public_challenge(self):
        original=self.worker
        lost=[False]
        def transport(method,path,payload):
            result=original(method,path,payload)
            if method=='PUT' and not lost[0]:
                lost[0]=True
                raise RuntimeError('synthetic lost staging reply')
            return result
        self.provider.transport=transport
        with self.assertRaises(PortalError):self.issue()
        first=self.worker.calls[0][2]['challenge_id']
        self.assertEqual(self.issue()['challenge_id'],first)
        self.assertEqual(self.f.client.calls,[])

    def test_idempotent_issue_keeps_challenge_before_and_after_click(self):
        i=self.issue();self.assertEqual(i,self.issue());self.click(i);self.assertEqual(i,self.issue())

class ConfigurationTests(unittest.TestCase):
    def test_unsafe_origins_pairings_and_credentials_rejected(self):
        for origin in ('http://example.test','https://example.test/','https://user@example.test','https://example.test?secret=x','https://example.test:8443','https://example.test:443','https://EXAMPLE.test'):
            with self.assertRaises(PortalError):SitesApprovalConfig(origin,CONFIG.owner_binding,CONFIG.machine_key,CONFIG.site_access_token)
        for owner in ('','first-visitor','owner\nheader'):
            with self.assertRaises(PortalError):SitesApprovalConfig(CONFIG.origin,owner,CONFIG.machine_key,CONFIG.site_access_token)
        with self.assertRaises(PortalError):SitesApprovalConfig(CONFIG.origin,CONFIG.owner_binding,'short',CONFIG.site_access_token)
    def test_https_redirect_handler_never_forwards_credentials(self):
        transport=SitesHTTPS(CONFIG)
        redirect=next(h for h in transport.opener.handlers if h.__class__.__name__=='_NoRedirect')
        self.assertIsNone(redirect.redirect_request(None,None,302,'redirect',{},'https://another.example.test'))
    def test_https_error_messages_do_not_echo_secret_or_response(self):
        transport=SitesHTTPS(CONFIG);transport.opener.open=Mock(side_effect=RuntimeError(CONFIG.machine_key))
        with self.assertRaises(PortalError) as error:transport('GET','/api/laf/approvals/11111111-1111-4111-8111-111111111111',None)
        self.assertEqual(str(error.exception),'REMOTE_APPROVAL_UNAVAILABLE')
    def test_response_size_and_content_type_are_bounded(self):
        transport=SitesHTTPS(CONFIG)
        response=Mock();response.status=200;response.headers.get_content_type.return_value='application/json';response.read.return_value=b'x'*8193
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);transport.opener.open=Mock(return_value=response)
        with self.assertRaises(PortalError):transport('GET','/api/laf/approvals/11111111-1111-4111-8111-111111111111',None)

if __name__=='__main__':unittest.main()
