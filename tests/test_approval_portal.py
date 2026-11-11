"""Authenticated human interaction core with fixture-only identity and native peer."""
from copy import deepcopy
import html
from pathlib import Path
import re
import sys
import json
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from urllib.parse import urlencode
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from approval_portal import (ApprovalPortal, PortalError, VerifiedHumanSession,
                             mcp_interaction)
import test_codex_approval_bridge as bridge


class ApprovalPortalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = bridge.ApprovalPipelineTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.pipeline = self.fixture.pipeline
        self.record = self.fixture.capture()
        self.now = [100.0]
        self.root = self.fixture.root / 'portal'
        self.root.mkdir(mode=0o700)
        self.session = VerifiedHumanSession('fixture-owner', 'fixture-browser-session', 100.0)
        self.authenticate = Mock(side_effect=lambda headers: self.session
                                 if headers.get('Cookie') == 'fixture-session' else None)
        self.authorize = Mock(side_effect=lambda subject, record: subject == 'fixture-owner')
        self.portal = self.make_portal()
        self.headers = {'Cookie': 'fixture-session', 'Origin': 'https://approval.example.test',
                        'Content-Type': 'application/x-www-form-urlencoded'}

    def lookup(self, approval_id):
        return next((a for a in self.pipeline.approval_get(bridge.TASK)['approvals']
                     if a['id'] == approval_id), None)

    def make_portal(self, **kw):
        return ApprovalPortal(self.root, self.lookup, origin='https://approval.example.test',
            clock=lambda: self.now[0], authenticate=kw.get('authenticate', self.authenticate),
            authorize=kw.get('authorize', self.authorize), ttl=120)

    def form(self, *, headers=None):
        interaction = self.portal.issue(self.record['id'])
        page = self.portal.form(interaction['challenge_id'], headers or self.headers)
        token = re.search('name="csrf" value="([^"]+)"', page).group(1)
        return interaction, page, token

    def decide(self, interaction, token, *, decision='accept', headers=None, **fields):
        data = {'csrf': token, 'decision': decision,
                'request_sha256': self.record['request_sha256'], **fields}
        return self.portal.decide(interaction['challenge_id'], headers or self.headers,
                                  urlencode(data).encode())

    def test_no_default_authenticator_or_owner_mapping_is_a_hard_block(self):
        for config in ({'authenticate': None}, {'authorize': None}):
            portal = self.make_portal(**config)
            self.assertFalse(portal.enabled)
            with self.assertRaisesRegex(PortalError, 'UNAVAILABLE'):
                portal.issue(self.record['id'])
        self.assertEqual(self.fixture.client.calls, [])

    def test_public_url_is_not_a_bearer_grant(self):
        i = self.portal.issue(self.record['id'])
        self.assertFalse(i['execution_authorized'])
        self.assertNotIn('fixture-owner', i['url'])
        self.assertNotIn(self.record['request_sha256'], i['url'])
        with self.assertRaisesRegex(PortalError, 'AUTHENTICATION_REQUIRED'):
            self.portal.form(i['challenge_id'], {})
        self.assertEqual(self.fixture.client.calls, [])

    def test_authentication_and_ownership_are_independent(self):
        self.session = VerifiedHumanSession('other-person', 'other-session', 100.0)
        with self.assertRaisesRegex(PortalError, 'NOT_OWNED'):
            self.form()
        self.assertEqual(self.fixture.client.calls, [])

    def test_caller_human_approved_flag_cannot_authenticate(self):
        i = self.portal.issue(self.record['id'])
        with self.assertRaisesRegex(PortalError, 'AUTHENTICATION_REQUIRED'):
            self.portal.form(i['challenge_id'], {'human_approved': True, 'subject': 'fixture-owner'})

    def test_stale_or_future_authentication_is_refused(self):
        for t in (-501, 101):
            self.session = VerifiedHumanSession('fixture-owner', 'session', t)
            with self.assertRaisesRegex(PortalError, 'AUTHENTICATION_REQUIRED'):
                self.form()

    def test_page_escapes_untrusted_request_content(self):
        self.pipeline._mutate(lambda s: s['approvals'][self.record['id']].update(
            command_preview='<script>not executable</script>'))
        _, page, _ = self.form()
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<script>', page)
        self.assertIn(self.record['request_sha256'], page)

    def test_valid_click_records_but_does_not_execute(self):
        i, _, token = self.form()
        result = self.decide(i, token)
        self.assertEqual(result['status'], 'DECISION_RECORDED')
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(self.fixture.client.calls, [])
        self.assertNotIn(token.encode(), self.portal.db.read_bytes())
        self.assertNotIn(b'fixture-browser-session', self.portal.db.read_bytes())

    def test_csrf_origin_hash_and_extra_fields_are_rejected(self):
        i, _, token = self.form()
        invalid = [dict(token='wrong'), dict(headers={**self.headers, 'Origin': 'https://other.test'}),
                   dict(request_sha256='b'*64), dict(human_approved='true')]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(PortalError):
                self.decide(i, args.pop('token', token), **args)
        self.assertEqual(self.fixture.client.calls, [])

    def test_expired_unused_decision_needs_a_fresh_authenticated_click(self):
        original, _, token = self.form()
        self.decide(original, token)
        self.now[0] += 120
        with self.assertRaisesRegex(PortalError, 'NO_UNAMBIGUOUS'):
            self.portal.consume(self.record, 'accept')
        fresh = self.portal.issue(self.record['id'])
        self.assertNotEqual(fresh['challenge_id'], original['challenge_id'])
        with sqlite3.connect(self.portal.db) as db:
            old = db.execute('SELECT status,decision,decided_at FROM interactions WHERE challenge=?',
                             (original['challenge_id'],)).fetchone()
        self.assertEqual(old, ('EXPIRED', 'accept', 100.0))
        with self.assertRaisesRegex(PortalError, 'NOT_PENDING'):
            self.decide(original, token)
        # A new URL is not fresh approval evidence.
        with self.assertRaisesRegex(PortalError, 'NO_UNAMBIGUOUS'):
            self.portal.consume(self.record, 'accept')
        _, _, fresh_token = self.form()
        self.decide(fresh, fresh_token)
        self.portal.consume(self.record, 'accept')

    def test_consumed_proof_cannot_be_reissued_even_after_expiry(self):
        i, _, token = self.form()
        self.decide(i, token)
        self.portal.consume(self.record, 'accept')
        self.now[0] += 120
        with self.assertRaisesRegex(PortalError, 'ALREADY_RECORDED'):
            self.portal.issue(self.record['id'])
        with self.assertRaisesRegex(PortalError, 'NO_UNAMBIGUOUS'):
            self.portal.consume(self.record, 'accept')

    def test_decline_only_native_choice_is_the_only_browser_and_wire_choice(self):
        from codex_app_protocol import ProtocolClient
        message = bridge.request(ident=88, availableDecisions=['decline'])
        self.pipeline.receive(message)
        self.record = next(r for r in self.pipeline.approval_get(bridge.TASK)['approvals']
                           if r['request_sha256'] == bridge.fingerprint(message))
        self.assertEqual(self.record['offered_decisions'], ['decline'])
        self.assertFalse(self.record['accept_supported'])
        exchange = bridge.Exchange()
        protocol = ProtocolClient(exchange)
        protocol.initialize()
        protocol.receive(message)
        self.pipeline.client = protocol
        self.pipeline.human_decision_gate = self.portal.consume
        i, page, token = self.form()
        self.assertNotIn('value="accept"', page)
        self.assertIn('value="decline"', page)
        with self.assertRaisesRegex(PortalError, 'NOT_OFFERED'):
            self.decide(i, token)
        self.assertEqual(exchange.responses, [])
        self.decide(i, token, decision='decline')
        result = self.fixture.decide(self.record, decision='decline')
        self.assertEqual(result['status'], 'SENT')
        self.assertEqual(exchange.responses, [{'id':88,'result':{'decision':'decline'}}])

    def test_accept_only_native_choice_does_not_offer_or_record_decline(self):
        message = bridge.request(ident=89, availableDecisions=['accept'])
        self.pipeline.receive(message)
        self.record = next(r for r in self.pipeline.approval_get(bridge.TASK)['approvals']
                           if r['request_sha256'] == bridge.fingerprint(message))
        i, page, token = self.form()
        self.assertIn('value="accept"', page)
        self.assertNotIn('value="decline"', page)
        with self.assertRaisesRegex(PortalError, 'NOT_OFFERED'):
            self.decide(i, token, decision='decline')

    def test_unknown_historical_native_choices_require_native_interface(self):
        self.pipeline._mutate(lambda s: s['approvals'][self.record['id']].pop('offered_decisions'))
        with self.assertRaisesRegex(PortalError, 'NATIVE_ONLY'):
            self.portal.issue(self.record['id'])

    def test_browser_session_change_invalidates_csrf_binding(self):
        i, _, token = self.form()
        self.session = VerifiedHumanSession('fixture-owner', 'new-browser-session', 100.0)
        with self.assertRaisesRegex(PortalError, 'BINDING_FAILED'):
            self.decide(i, token)

    def test_decision_is_exact_and_one_shot(self):
        i, _, token = self.form()
        with self.assertRaises(PortalError):
            self.decide(i, token, decision='acceptForSession')
        self.decide(i, token)
        with self.assertRaisesRegex(PortalError, 'NOT_PENDING'):
            self.decide(i, token)
        with self.assertRaisesRegex(PortalError, 'NO_UNAMBIGUOUS'):
            self.portal.consume(self.record, 'decline')
        receipt = self.portal.consume(self.record, 'accept')
        self.assertTrue(receipt.startswith('human-portal:'))
        with self.assertRaisesRegex(PortalError, 'NO_UNAMBIGUOUS'):
            self.portal.consume(self.record, 'accept')

    def test_expiry_blocks_forms_clicks_and_receipt_consumption(self):
        i, _, token = self.form()
        self.decide(i, token)
        self.now[0] += 120
        with self.assertRaisesRegex(PortalError, 'NO_UNAMBIGUOUS'):
            self.portal.consume(self.record, 'accept')
        with self.assertRaisesRegex(PortalError, 'NOT_PENDING'):
            self.portal.form(i['challenge_id'], self.headers)

    def test_changed_request_after_form_is_refused(self):
        i, _, token = self.form()
        self.pipeline._mutate(lambda s: s['approvals'][self.record['id']].update(
            command_preview='different command'))
        with self.assertRaisesRegex(PortalError, 'APPROVAL_CHANGED'):
            self.decide(i, token)
        self.assertEqual(self.fixture.client.calls, [])

    def test_canceled_native_task_cannot_be_revived(self):
        i, _, token = self.form()
        self.pipeline.receive({'method': 'turn/completed', 'params': {'threadId': 'native-thread',
            'turn': {'id': 'native-turn', 'status': 'interrupted', 'items': []}}})
        with self.assertRaisesRegex(PortalError, 'NOT_PENDING'):
            self.decide(i, token)
        self.assertEqual(self.pipeline.approval_get(bridge.TASK)['task_state'], 'CANCELLED')
        self.assertEqual(self.fixture.client.calls, [])

    def test_permission_approval_remains_native_only(self):
        self.pipeline.receive(bridge.request(ident=88, method=bridge.PERMISSIONS,
                                            permissions={'network': {'enabled': True}}))
        record = next(r for r in self.pipeline.approval_get(bridge.TASK)['approvals']
                      if r['method'] == bridge.PERMISSIONS)
        with self.assertRaisesRegex(PortalError, 'NATIVE_ONLY'):
            self.portal.issue(record['id'])
        self.assertEqual(self.fixture.client.calls, [])

    def test_missing_file_patch_cannot_be_accepted(self):
        self.pipeline.receive(bridge.request(ident=89, method=bridge.FILE))
        self.record = next(r for r in self.pipeline.approval_get(bridge.TASK)['approvals']
                           if r['method'] == bridge.FILE)
        i, page, token = self.form()
        self.assertNotIn('value="accept"', page)
        with self.assertRaisesRegex(PortalError, 'NATIVE_HUMAN'):
            self.decide(i, token)
        self.assertEqual(self.decide(i, token, decision='decline')['decision'], 'decline')

    def test_receipt_consumption_survives_portal_restart(self):
        i, _, token = self.form()
        self.decide(i, token)
        self.portal = self.make_portal()
        self.portal.consume(self.record, 'accept')
        self.portal = self.make_portal()
        with self.assertRaisesRegex(PortalError, 'NO_UNAMBIGUOUS'):
            self.portal.consume(self.record, 'accept')

    def test_negotiated_mcp_url_elicitation_is_not_an_approval(self):
        i = self.portal.issue(self.record['id'])
        descriptor = mcp_interaction(i, {'elicitation': {'url': {}}}, 'portal-1')
        self.assertEqual(descriptor['server_request']['method'], 'elicitation/create')
        self.assertEqual(descriptor['server_request']['params']['mode'], 'url')
        self.assertFalse(descriptor['execution_authorized'])
        # Even an accepted navigation could not produce a server-side receipt.
        navigation_reply = {'id': 'portal-1', 'result': {'action': 'accept'}}
        self.assertEqual(navigation_reply['result']['action'], 'accept')
        with self.assertRaisesRegex(PortalError, 'NO_UNAMBIGUOUS'):
            self.portal.consume(self.record, 'accept')

    def test_portal_origin_must_be_an_exact_https_origin(self):
        for origin in ('http://approval.example.test', 'https://user@approval.example.test',
                       'https://approval.example.test/', 'https://approval.example.test?q=x',
                       'https://approval.example.test:bad', 'https://approval.example.test\n'):
            with self.subTest(origin=origin), self.assertRaises(PortalError):
                ApprovalPortal(self.root, self.lookup, origin=origin)

    def test_elicitation_ids_are_typed_and_bounded(self):
        interaction = self.portal.issue(self.record['id'])
        for identifier in (True, None, '', 'x'*129, 2**63, -(2**63)-1):
            with self.subTest(identifier=identifier), self.assertRaises(PortalError):
                mcp_interaction(interaction, {'elicitation': {'url': {}}}, identifier)

    def test_unadvertised_url_elicitation_is_not_sent(self):
        i = self.portal.issue(self.record['id'])
        for caps in ({}, {'elicitation': {}}, {'elicitation': {'form': {}}}):
            result = mcp_interaction(i, caps, 1)
            self.assertIsNone(result['server_request'])
            self.assertFalse(result['elicitation_required'])
            self.assertEqual(result['interaction']['url'], i['url'])

    def test_begin_reuses_same_public_interaction_but_never_reissues_consumed_proof(self):
        first = self.portal.issue(self.record['id'])
        second = self.portal.issue(self.record['id'])
        self.assertEqual(first, second)
        _, _, token = self.form()
        self.decide(first, token)
        with self.assertRaisesRegex(PortalError, 'ALREADY_RECORDED'):
            self.portal.issue(self.record['id'])
        self.portal.consume(self.record, 'accept')
        with self.assertRaisesRegex(PortalError, 'ALREADY_RECORDED'):
            self.portal.issue(self.record['id'])

    def test_http_page_has_strict_headers_and_authentication(self):
        i = self.portal.issue(self.record['id'])
        path = '/approvals/' + i['challenge_id']
        headers = {**self.headers, 'Host': 'approval.example.test'}
        status, response_headers, page = self.portal.handle('GET', path, headers)
        self.assertEqual(status, 200)
        self.assertEqual(response_headers['Cache-Control'], 'no-store')
        self.assertEqual(response_headers['Referrer-Policy'], 'no-referrer')
        self.assertIn("frame-ancestors 'none'", response_headers['Content-Security-Policy'])
        self.assertIn(b'Approve once', page)
        self.assertEqual(self.portal.handle('GET', path, {'Host': 'approval.example.test'})[0], 401)
        self.assertEqual(self.portal.handle('GET', path, {**headers, 'Host': 'evil.test'})[0], 403)

    def test_actual_mcp_begin_browser_click_and_mcp_decide_fixture(self):
        from local_principal import current_local_principal
        from mcp_adapter import REQUEST_PROTOCOL, VERSION_META, CAPABILITIES_META
        from mcp_http_transport import McpHttpEndpoint
        adapter_fixture = bridge.ApprovalAdapterTests(methodName='runTest')
        adapter_fixture.setUp()
        self.addCleanup(adapter_fixture.doCleanups)
        adapter = adapter_fixture.adapter
        adapter.pipeline = self.pipeline
        adapter_fixture.outbox.task_owned_by.return_value = True
        self.pipeline.approval_portal = self.portal
        self.pipeline.human_decision_gate = self.portal.consume
        endpoint = McpHttpEndpoint(adapter, expected_port=8765,
            allow_single_user_local=True, principal_provider=current_local_principal)
        def call(name, args, identifier):
            params = {'name': name, 'arguments': args,
                '_meta': {VERSION_META: REQUEST_PROTOCOL, CAPABILITIES_META: {}}}
            payload = json.dumps({'jsonrpc': '2.0', 'id': identifier,
                'method': 'tools/call', 'params': params}).encode()
            status, _, response = endpoint.handle('POST', '/mcp', {
                'Host': '127.0.0.1:8765', 'Content-Type': 'application/json',
                'Accept': 'application/json, text/event-stream',
                'MCP-Protocol-Version': REQUEST_PROTOCOL, 'Mcp-Method': 'tools/call',
                'Mcp-Name': name, 'Content-Length': str(len(payload))}, payload)
            self.assertEqual(status, 200, response)
            return json.loads(response)['result']
        args = {'task_id': bridge.TASK, 'approval_id': self.record['id'],
                'request_sha256': self.record['request_sha256'], 'request_key': 'portal-open-1'}
        result = call('codex_approval_begin', args, 1)
        self.assertFalse(result['isError'])
        interaction = result['structuredContent']['data']
        self.assertFalse(interaction['execution_authorized'])
        browser_headers = {**self.headers, 'Host': 'approval.example.test'}
        path = '/approvals/' + interaction['challenge_id']
        status, _, page = self.portal.handle('GET', path, browser_headers)
        self.assertEqual(status, 200)
        token = re.search('name="csrf" value="([^\"]+)"', page.decode()).group(1)
        body = urlencode({'csrf': token, 'decision': 'accept',
                          'request_sha256': self.record['request_sha256']}).encode()
        self.assertEqual(self.portal.handle('POST', path, browser_headers, body)[0], 200)
        decision = call('codex_approval_decide', {**args, 'request_key': 'portal-decision-1',
                                                'decision': 'accept'}, 2)
        self.assertFalse(decision['isError'])
        self.assertEqual(decision['structuredContent']['data']['status'], 'SENT')
        self.assertEqual(len(self.fixture.client.calls), 1)
        self.assertEqual(self.pipeline.approval_get(bridge.TASK)['task_state'], 'WAITING_APPROVAL')
        # A new call cannot bypass the consumed native request or human receipt.
        again = call('codex_approval_decide', {**args, 'request_key': 'portal-decision-2',
                                             'decision': 'accept'}, 3)
        self.assertTrue(again['isError'])
        self.assertEqual(len(self.fixture.client.calls), 1)

    def test_mcp_begin_default_and_owner_checks_remain_closed(self):
        from codex_pipeline import PipelineError
        args = {'task_id': bridge.TASK, 'approval_id': self.record['id'],
                'request_sha256': self.record['request_sha256'], 'request_key': 'begin-blocked'}
        with self.assertRaisesRegex(PipelineError, 'CHANNEL_UNAVAILABLE'):
            self.pipeline.dispatch('codex_approval_begin', args)
        self.pipeline.approval_portal = self.portal
        with self.assertRaisesRegex(PipelineError, 'INVALID_FIELDS'):
            self.pipeline.dispatch('codex_approval_begin', {**args, 'human_approved': True})
        fixture = bridge.ApprovalAdapterTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.adapter.pipeline = self.pipeline
        result = fixture.adapter.call_tool('codex_approval_begin', args, 1,
                                          request_principal=fixture.principal)
        self.assertTrue(result['isError'])
        with sqlite3.connect(self.portal.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM interactions').fetchone()[0], 0)

    def test_synchronous_mcp_url_error_then_authenticated_retry_flow(self):
        from local_principal import current_local_principal
        from mcp_adapter import REQUEST_PROTOCOL, VERSION_META, CAPABILITIES_META
        from mcp_http_transport import McpHttpEndpoint
        fixture = bridge.ApprovalAdapterTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.adapter.pipeline = self.pipeline
        fixture.outbox.task_owned_by.return_value = True
        self.pipeline.approval_portal = self.portal
        self.pipeline.human_decision_gate = self.portal.consume
        endpoint = McpHttpEndpoint(fixture.adapter, expected_port=8765,
            allow_single_user_local=True, principal_provider=current_local_principal)
        def call(name, args, identifier):
            payload = json.dumps({'jsonrpc':'2.0','id':identifier,'method':'tools/call',
                'params':{'name':name,'arguments':args,'_meta':{VERSION_META:REQUEST_PROTOCOL,
                    CAPABILITIES_META:{'elicitation':{'url':{}}}}}}).encode()
            status, _, body = endpoint.handle('POST','/mcp',{
                'Host':'127.0.0.1:8765','Content-Type':'application/json',
                'Accept':'application/json, text/event-stream',
                'MCP-Protocol-Version':REQUEST_PROTOCOL,'Mcp-Method':'tools/call',
                'Mcp-Name':name,'Content-Length':str(len(payload))},payload)
            self.assertEqual(status,200)
            return json.loads(body)
        args={'task_id':bridge.TASK,'approval_id':self.record['id'],
              'request_sha256':self.record['request_sha256'],'request_key':'url-begin-1'}
        first=call('codex_approval_begin',args,1)
        self.assertEqual(first['error']['code'],-32042)
        elicitation=first['error']['data']['elicitations'][0]
        self.assertEqual(elicitation['mode'],'url')
        challenge=elicitation['elicitationId']
        self.assertEqual(call('codex_approval_begin',args,2)['error']['data']['elicitations'],
                         first['error']['data']['elicitations'])
        with sqlite3.connect(self.portal.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM interactions').fetchone()[0],1)
        with self.assertRaisesRegex(PortalError,'NO_UNAMBIGUOUS'):
            self.portal.consume(self.record,'accept')
        self.assertEqual(self.fixture.client.calls,[])
        headers={**self.headers,'Host':'approval.example.test'}
        path='/approvals/'+challenge
        status, _, page=self.portal.handle('GET',path,headers)
        self.assertEqual(status,200)
        token=re.search('name="csrf" value="([^\"]+)"',page.decode()).group(1)
        body=urlencode({'csrf':token,'decision':'accept',
                        'request_sha256':self.record['request_sha256']}).encode()
        self.assertEqual(self.portal.handle('POST',path,headers,body)[0],200)
        complete=call('codex_approval_begin',args,3)
        self.assertNotIn('error',complete)
        self.assertEqual(complete['result']['structuredContent']['data']['status'],'DECIDED')
        self.assertEqual(complete['result']['structuredContent']['data']['decision'],'accept')
        self.assertEqual(self.fixture.client.calls,[])
        sent=call('codex_approval_decide',{**args,'request_key':'url-decision-1','decision':'accept'},4)
        self.assertEqual(sent['result']['structuredContent']['data']['status'],'SENT')
        self.assertEqual(len(self.fixture.client.calls),1)

    def test_legacy_http_url_capability_is_scoped_to_its_own_session(self):
        from local_principal import current_local_principal
        from mcp_adapter import PROTOCOL
        from mcp_http_transport import McpHttpEndpoint
        fixture=bridge.ApprovalAdapterTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.adapter.pipeline=self.pipeline
        fixture.outbox.task_owned_by.return_value=True
        self.pipeline.approval_portal=self.portal
        endpoint=McpHttpEndpoint(fixture.adapter,expected_port=8765,
            allow_single_user_local=True,principal_provider=current_local_principal)
        def rpc(message,session=None):
            payload=json.dumps(message).encode()
            headers={'Host':'127.0.0.1:8765','Content-Type':'application/json',
                'Accept':'application/json, text/event-stream','MCP-Protocol-Version':PROTOCOL,
                'Content-Length':str(len(payload))}
            if session: headers['Mcp-Session-Id']=session
            return endpoint.handle('POST','/mcp',headers,payload)
        def initialize(capabilities):
            status,headers,_=rpc({'jsonrpc':'2.0','id':1,'method':'initialize',
                'params':{'protocolVersion':PROTOCOL,'capabilities':capabilities,
                          'clientInfo':{'name':'fixture','version':'1'}}})
            self.assertEqual(status,200)
            session=headers['Mcp-Session-Id']
            self.assertEqual(rpc({'jsonrpc':'2.0','method':'notifications/initialized'},session)[0],202)
            return session
        capable=initialize({'elicitation':{'url':{}}})
        incapable=initialize({})
        request={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{
            'name':'codex_approval_begin','arguments':{'task_id':bridge.TASK,
                'approval_id':self.record['id'],'request_sha256':self.record['request_sha256'],
                'request_key':'legacy-open'}}}
        status,_,body=rpc(request,capable)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['error']['code'],-32042)
        status,_,body=rpc(request,incapable)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['result']['structuredContent']['data']['status'],'PENDING')
        status,_,body=rpc(request,capable)
        self.assertEqual(json.loads(body)['error']['code'],-32042)
        self.assertEqual(self.fixture.client.calls,[])

    def test_interaction_status_is_metadata_not_human_evidence(self):
        i, _, token=self.form()
        before=self.portal.db.read_bytes()
        status=self.portal.interaction_status(i['challenge_id'])
        self.assertEqual(status,{'status':'PENDING','decision':None,'execution_authorized':False})
        self.assertEqual(self.portal.db.read_bytes(),before)
        self.decide(i,token)
        self.assertEqual(self.portal.interaction_status(i['challenge_id'])['status'],'DECIDED')
        self.now[0]+=120
        self.assertEqual(self.portal.interaction_status(i['challenge_id'])['status'],'EXPIRED')
        with self.assertRaisesRegex(PortalError,'NO_UNAMBIGUOUS'):
            self.portal.consume(self.record,'accept')

    def test_human_click_to_same_native_task_end_to_end_fixture(self):
        i, _, token = self.form()
        self.decide(i, token)
        self.pipeline.human_decision_gate = self.portal.consume
        result = self.fixture.decide(self.record)
        self.assertEqual(result['status'], 'SENT')
        self.assertEqual(self.fixture.client.calls, [(7, bridge.COMMAND,
            self.record['request_sha256'], 'accept')])
        self.assertEqual(self.pipeline.approval_get(bridge.TASK)['task_state'], 'WAITING_APPROVAL')
        self.pipeline.receive({'method': 'serverRequest/resolved',
            'params': {'threadId': 'native-thread', 'requestId': 7}})
        self.fixture.client.flags = []
        self.assertEqual(self.pipeline.refresh(bridge.TASK)['state'], 'RUNNING')
        state = self.pipeline._mutate(lambda s: s)
        self.assertEqual(list(state['tasks']), [bridge.TASK])
        self.assertEqual(list(state['threads']), [bridge.THREAD])
        self.assertTrue(any(e['kind'] == 'approval_response_sent' for e in state['events']))
        self.assertTrue(any(e['kind'] == 'approval_resume_confirmed' for e in state['events']))


if __name__ == '__main__':
    unittest.main()
