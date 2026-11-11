"""Offline approval contracts; no auth, service change, live model, or grant."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codex_approvals import COMMAND, FILE, PERMISSIONS, projection, fingerprint, preview
from codex_app_protocol import ProtocolClient, ProtocolError, ProtocolUnknown, StdioTransport
from codex_pipeline import Pipeline, PipelineError, validate_arguments, tool_definitions

TASK = '11111111-1111-4111-8111-111111111111'
THREAD = '22222222-2222-4222-8222-222222222222'


def request(ident=7, method=COMMAND, **params):
    return {'id': ident, 'method': method, 'params': {'threadId': 'native-thread',
        'turnId': 'native-turn', 'itemId': 'item-1', 'command': 'git status --short',
        'cwd': '/workspace/project', **params}}


class Client:
    def __init__(self): self.calls = []; self.fail = False; self.flags = ['waitingOnApproval']
    def respond_approval(self, *args):
        self.calls.append(args)
        if self.fail: raise OSError('lost after send')
    def request(self, method, params):
        return {'thread': {'id': 'native-thread', 'status': {'activeFlags': self.flags},
                'turns': [{'id': 'native-turn', 'status': 'inProgress', 'items': []}]}}


class ApprovalPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); os.chmod(self.root, 0o700)
        self.client = Client(); self.gate = Mock(return_value='human-receipt-1')
        self.policy = {'schema': 2, 'workspaces': {'development': str(self.root)},
            'models': ['test-model'], 'max_threads': 2, 'max_tasks': 4, 'concurrency': 2,
            'timeout_seconds': 30}
        self.pipeline = self.new_pipeline(gate=self.gate)
        def seed(s):
            s['threads'][THREAD] = {'id': THREAD, 'remote_id': 'native-thread',
                'active_task': TASK, 'archived': False, 'model': 'test-model', 'workspace': 'development'}
            s['tasks'][TASK] = {'id': TASK, 'thread_id': THREAD, 'remote_turn': 'native-turn',
                'state': 'RUNNING', 'answer': None, 'stop_sent': False, 'deadline': 1000}
        self.pipeline._mutate(seed)
    def new_pipeline(self, gate=None):
        return Pipeline(self.root, self.policy, self.client, instructions='test policy',
            policy_sha256='a'*64, clock=lambda: 100, human_decision_gate=gate)
    def capture(self, message=None):
        self.pipeline.receive(message or request())
        return self.pipeline.approval_get(TASK)['approvals'][0]
    def decide(self, record, key='decision-1', **changes):
        return self.pipeline.dispatch('codex_approval_decide', {'task_id': TASK,
            'approval_id': record['id'], 'request_sha256': record['request_sha256'],
            'decision': 'accept', 'request_key': key, **changes})
    def test_scoped_visible_details_and_private_wire_id(self):
        r = self.capture()
        self.assertEqual((r['task_id'], r['action'], r['target']['cwd']),
                         (TASK, 'command_execution', '/workspace/project'))
        self.assertNotIn('native_request_id', r)
        self.assertTrue(r['human_final_approval_required'])
        self.assertEqual(self.pipeline.approval_get(TASK)['task_state'], 'WAITING_APPROVAL')
    def test_missing_payload_not_synthesized(self):
        self.client.flags = ['waitingOnApproval']; self.pipeline.refresh(TASK)
        self.assertFalse(self.pipeline.approval_get(TASK)['details_available'])
    def test_default_host_has_no_remote_authority(self):
        r = self.capture(); self.pipeline.human_decision_gate = None
        with self.assertRaisesRegex(PipelineError, 'HUMAN_APPROVAL_CHANNEL_UNAVAILABLE'): self.decide(r)
        self.assertEqual(self.client.calls, [])
    def test_decision_tool_is_marked_consequential(self):
        tool = next(t for t in tool_definitions() if t['name'] == 'codex_approval_decide')
        self.assertTrue(tool['annotations']['destructiveHint'])
        self.assertTrue(tool['annotations']['openWorldHint'])
        self.assertFalse(tool['annotations']['readOnlyHint'])
    def test_no_caller_assertion_can_supply_human_approval(self):
        r = self.capture()
        with self.assertRaisesRegex(PipelineError, 'INVALID_FIELDS'):
            self.decide(r, human_approved=True)
    def test_missing_or_withheld_action_requires_native_ui(self):
        for params in ({'command': None}, {'command': 'print token-value'}, {'kind':'writeStdin'},
                       {'networkApprovalContext': {'host':'example.com','protocol':'https'}}):
            self.pipeline._mutate(lambda s: s.pop('approvals', None))
            self.pipeline.receive(request(**params))
            r = self.pipeline.approval_get(TASK)['approvals'][0]
            self.assertFalse(r['accept_supported'])
            with self.assertRaisesRegex(PipelineError, 'NATIVE_HUMAN_UI'): self.decide(r)
        self.assertEqual(self.client.calls, [])
    def test_withheld_or_malformed_environment_never_accepts(self):
        for ident, environment in enumerate(('secret-target', '', 3, 'x'*1025), 200):
            self.pipeline.receive(request(ident=ident, environmentId=environment))
        self.assertTrue(all(not a['accept_supported'] for a in self.pipeline.approval_get(TASK)['approvals']))
    def test_hidden_unicode_control_content_never_accepts(self):
        for ident, hidden in enumerate(('\x7f', '\x85', '\u202e', '\u200b', '\u2028'), 400):
            self.pipeline.receive(request(ident=ident, command='git '+hidden+'status'))
        self.assertTrue(all(not a['accept_supported'] for a in self.pipeline.approval_get(TASK)['approvals']))
    def test_supplied_empty_permission_context_never_accepts(self):
        for ident, params in enumerate(({'additionalPermissions':{}}, {'networkApprovalContext':{}}), 300):
            self.pipeline.receive(request(ident=ident, **params))
        self.assertTrue(all(not a['accept_supported'] for a in self.pipeline.approval_get(TASK)['approvals']))
    def test_action_kind_and_environment_are_visible(self):
        r = self.capture(request(kind='writeStdin', environmentId='remote-1'))
        self.assertEqual(r['action_kind'], 'writeStdin')
        self.assertEqual(r['target']['environment_id'], 'remote-1')
    def test_file_patch_unavailable_requires_native_acceptance(self):
        r = self.capture(request(method=FILE))
        with self.assertRaisesRegex(PipelineError, 'NATIVE_HUMAN_UI'): self.decide(r)
        self.assertEqual(self.client.calls, [])
    def test_permission_changes_are_critical_native_only(self):
        r = self.capture(request(method=PERMISSIONS, permissions={'network': {'enabled': True}}))
        self.assertEqual(r['risk'], 'CRITICAL'); self.assertFalse(r['response_supported'])
        with self.assertRaisesRegex(PipelineError, 'NATIVE_HUMAN_UI'): self.decide(r)
        self.gate.assert_not_called(); self.assertEqual(self.client.calls, [])
    def test_connector_elicitation_visible_but_never_auto_answered(self):
        r = self.capture(request(method='item/tool/requestUserInput', questions=[{
            'id': 'q', 'question': 'Change GitHub app permissions?', 'options': [{'label': 'Accept'}]}]))
        self.assertIn('GitHub', r['target']['questions'][0]['question'])
        with self.assertRaisesRegex(PipelineError, 'NATIVE_HUMAN_UI'): self.decide(r)
    def test_same_request_deduplicated(self):
        a = self.capture(); self.pipeline.receive(request()); b = self.pipeline.approval_get(TASK)['approvals']
        self.assertEqual([a], b)
    def test_reused_native_id_changed_content_blocks(self):
        r = self.capture(); self.pipeline.receive(request(command='git push'))
        with self.assertRaisesRegex(PipelineError, 'NOT_PENDING'): self.decide(r)
    def test_wrong_thread_and_turn_not_adopted(self):
        self.pipeline.receive(request(threadId='other'))
        self.pipeline.receive(request(turnId='other'))
        self.assertEqual(self.pipeline.approval_get(TASK)['approvals'], [])
    def test_wrong_task_or_hash_blocks(self):
        r = self.capture()
        with self.assertRaisesRegex(PipelineError, 'CONTENT_CHANGED'):
            self.decide(r, request_sha256='b'*64)
        other = dict(r, id='33333333-3333-4333-8333-333333333333')
        with self.assertRaisesRegex(PipelineError, 'NOT_OWNED'): self.decide(other)
        self.assertEqual(self.client.calls, [])
    def test_sent_once_same_task_not_resumed_prematurely(self):
        r = self.capture(); result = self.decide(r)
        self.assertEqual(result['status'], 'SENT'); self.assertFalse(result['resume_confirmed'])
        self.assertEqual(self.pipeline.approval_get(TASK)['task_state'], 'WAITING_APPROVAL')
        self.assertTrue(self.decide(r)['duplicate']); self.assertEqual(len(self.client.calls), 1)
        self.gate.assert_called_once()
        with self.assertRaisesRegex(PipelineError, 'NOT_PENDING'): self.decide(r, key='different')
    def test_decline_uses_same_turn_without_new_task(self):
        r = self.capture(); self.decide(r, decision='decline')
        self.assertEqual(self.client.calls[0][-1], 'decline')
        self.assertEqual(self.pipeline._mutate(lambda s: list(s['tasks'])), [TASK])
    def test_only_correlated_resolution_and_history_confirm_resume(self):
        r = self.capture(); self.decide(r); self.client.flags = []
        self.assertEqual(self.pipeline.refresh(TASK)['state'], 'WAITING_APPROVAL')
        self.pipeline.receive({'method': 'serverRequest/resolved',
            'params': {'threadId': 'native-thread', 'requestId': '7'}})
        self.assertEqual(self.pipeline.refresh(TASK)['state'], 'WAITING_APPROVAL')
        self.pipeline.receive({'method': 'serverRequest/resolved',
            'params': {'threadId': 'native-thread', 'requestId': 7}})
        self.assertEqual(self.pipeline.refresh(TASK)['state'], 'RUNNING')
    def test_unknown_send_is_never_replayed(self):
        r = self.capture(); self.client.fail = True
        self.assertEqual(self.decide(r)['status'], 'UNKNOWN')
        self.assertTrue(self.decide(r)['duplicate'])
        with self.assertRaisesRegex(PipelineError, 'NOT_PENDING'): self.decide(r, key='new')
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.pipeline.approval_get(TASK)['approvals'][0]['status'], 'RECONCILIATION_REQUIRED')
    def test_restart_keeps_audit_but_invalidates_rpc_response(self):
        r = self.capture(); self.pipeline = self.new_pipeline(gate=self.gate)
        self.assertEqual(self.pipeline.approval_get(TASK)['approvals'][0]['status'], 'RECONCILIATION_REQUIRED')
        with self.assertRaisesRegex(PipelineError, 'NOT_PENDING'): self.decide(r)
    def test_terminal_task_marks_request_stale(self):
        r = self.capture()
        self.pipeline.receive({'method': 'turn/completed', 'params': {'threadId': 'native-thread',
            'turn': {'id': 'native-turn', 'status': 'completed', 'items': []}}})
        self.assertEqual(self.pipeline.approval_get(TASK)['approvals'][0]['status'], 'STALE')
        with self.assertRaises(PipelineError): self.decide(r)
    def test_unsupported_session_and_policy_decisions(self):
        r = self.capture()
        for d in ('acceptForSession', 'cancel', {'acceptWithExecpolicyAmendment': {}}):
            with self.assertRaises(PipelineError): self.decide(r, decision=d)
    def test_unverified_gate_receipt_never_writes(self):
        r = self.capture(); self.gate.return_value = None
        self.assertEqual(self.decide(r)['status'], 'UNKNOWN'); self.assertEqual(self.client.calls, [])
    def test_terminal_event_during_gate_prevents_send(self):
        r = self.capture()
        def gate(record, decision):
            self.pipeline.receive({'method':'turn/completed', 'params': {'threadId':'native-thread',
                'turn':{'id':'native-turn','status':'completed','items':[]}}})
            return 'human-receipt-1'
        self.pipeline.human_decision_gate = gate
        self.assertEqual(self.decide(r)['status'], 'UNKNOWN')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.pipeline.approval_get(TASK)['approvals'][0]['status'], 'STALE')
    def test_changed_payload_during_gate_prevents_send(self):
        r = self.capture()
        def gate(record, decision):
            self.pipeline.receive(request(command='git push'))
            return 'human-receipt-1'
        self.pipeline.human_decision_gate = gate
        self.assertEqual(self.decide(r)['status'], 'UNKNOWN')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.pipeline.approval_get(TASK)['approvals'][0]['status'], 'RECONCILIATION_REQUIRED')
    def test_sensitive_preview_not_persisted(self):
        r = self.capture(request(command='curl -H Authorization:Bearer SECRET_VALUE'))
        self.assertIn('withheld', r['command_preview'])
        self.assertNotIn('SECRET_VALUE', self.pipeline.db.read_bytes().decode(errors='ignore'))
    def test_bounded_records_hold_without_payload_overflow(self):
        for ident in range(40): self.pipeline.receive(request(ident=ident))
        self.assertEqual(len(self.pipeline.approval_get(TASK)['approvals']), 32)
    def test_dispatch_race_request_is_retained(self):
        self.pipeline._mutate(lambda s: s['tasks'][TASK].update(remote_turn=None, state='DISPATCHING'))
        r = self.capture()
        self.assertEqual(r['native_turn_id'], 'native-turn')
        with self.assertRaisesRegex(PipelineError, 'NOT_PENDING'): self.decide(r)


class Exchange:
    def __init__(self): self.responses = []; self.fail = False
    def __call__(self, message, timeout_s):
        return None if message['method'] == 'initialized' else {
            'id': message['id'], 'result': {'userAgent': 'codex-cli 0.165.0', 'codexHome': '/fake',
                'platformFamily': 'unix', 'platformOs': 'linux'}}
    def write_response(self, message, timeout_s):
        self.responses.append(message)
        if self.fail: raise OSError('write lost')


class ProtocolApprovalTests(unittest.TestCase):
    def setUp(self):
        self.exchange = Exchange(); self.client = ProtocolClient(self.exchange); self.client.initialize()
    def send(self, msg=None, decision='accept'):
        msg = msg or request(); self.client.receive(msg)
        self.client.respond_approval(msg['id'], msg['method'], fingerprint(msg), decision)
    def test_exact_jsonrpc_response_once(self):
        m = request(); self.send(m)
        self.assertEqual(self.exchange.responses, [{'id': 7, 'result': {'decision': 'accept'}}])
        with self.assertRaises(ProtocolError): self.client.respond_approval(7, COMMAND, fingerprint(m), 'accept')
    def test_duplicate_after_send_never_reopens_request(self):
        m = request(); self.send(m); self.client.receive(m)
        with self.assertRaises(ProtocolError): self.client.respond_approval(7, COMMAND, fingerprint(m), 'accept')
        self.assertEqual(len(self.exchange.responses), 1)
    def test_decline(self):
        self.send(decision='decline'); self.assertEqual(self.exchange.responses[0]['result'], {'decision': 'decline'})
    def test_not_an_arbitrary_response_writer(self):
        with self.assertRaises(ProtocolError): self.client.respond_approval('unknown', COMMAND, 'a'*64, 'accept')
        self.assertEqual(self.exchange.responses, [])
    def test_native_id_type_is_preserved(self):
        m=request(); self.client.receive(m)
        with self.assertRaises(ProtocolError): self.client.respond_approval('7', COMMAND, fingerprint(m), 'accept')
    def test_unknown_write_consumed_before_io(self):
        self.exchange.fail = True; m = request()
        with self.assertRaises(ProtocolUnknown): self.send(m)
        with self.assertRaises(ProtocolError): self.client.respond_approval(7, COMMAND, fingerprint(m), 'accept')
        self.assertEqual(len(self.exchange.responses), 1)
    def test_resolution_prevents_stale_write(self):
        m = request(); self.client.receive(m)
        self.client.receive({'method':'serverRequest/resolved', 'params': {'threadId':'native-thread','requestId':7}})
        with self.assertRaises(ProtocolError): self.client.respond_approval(7, COMMAND, fingerprint(m), 'accept')
    def test_terminal_turn_invalidates_pending_wire_request(self):
        m = request(); self.client.receive(m)
        self.client.receive({'method':'turn/completed','params':{'threadId':'native-thread',
            'turn':{'id':'native-turn','status':'completed','items':[]}}})
        with self.assertRaises(ProtocolError): self.client.respond_approval(7, COMMAND, fingerprint(m), 'accept')
        self.assertEqual(self.exchange.responses, [])
    def test_terminal_other_turn_does_not_invalidate_this_request(self):
        m = request(); self.client.receive(m)
        self.client.receive({'method':'turn/completed','params':{'threadId':'native-thread',
            'turn':{'id':'different-turn','status':'completed','items':[]}}})
        self.client.respond_approval(7, COMMAND, fingerprint(m), 'decline')
        self.assertEqual(len(self.exchange.responses), 1)
    def test_no_connection_replacement(self):
        m = request(); self.client.receive(m)
        fresh = ProtocolClient(self.exchange); fresh.initialize()
        with self.assertRaises(ProtocolError): fresh.respond_approval(7, COMMAND, fingerprint(m), 'accept')
    def test_no_unoffered_decision(self):
        with self.assertRaises(ProtocolError): self.send(request(availableDecisions=['decline']))
    def test_incomplete_or_other_action_not_accepted(self):
        for index, params in enumerate(({'command': None}, {'kind':'writeStdin'}, {'command':'echo password'},
                       {'additionalPermissions': {'network':{'enabled':True}}})):
            ident = 90 + index
            m = request(ident=ident, **params)
            self.client.receive(m)
            with self.assertRaises(ProtocolError):
                self.client.respond_approval(ident, COMMAND, fingerprint(m), 'accept')
            self.client.receive({'method':'serverRequest/resolved','params':{'requestId':ident}})
        self.assertEqual(self.exchange.responses, [])
    def test_no_session_grant_root_accept(self):
        with self.assertRaises(ProtocolError): self.send(request(method=FILE, grantRoot='/workspace'))
    def test_no_permission_grant(self):
        with self.assertRaises(ProtocolError): self.send(request(method=PERMISSIONS))
    def test_reused_id_with_new_content_rejected(self):
        self.client.receive(request())
        with self.assertRaises(ProtocolError): self.client.receive(request(command='git push'))
    def test_trace_not_saved_or_hashed_as_authority(self):
        m = request(); m['trace'] = {'traceparent': 'untrusted'}
        self.client.receive(m)
        clean = {k:v for k,v in m.items() if k != 'trace'}
        self.client.respond_approval(7, COMMAND, fingerprint(clean), 'decline')
        self.assertEqual(len(self.exchange.responses), 1)
    def test_transport_only_one_shot_wire_shape(self):
        t = StdioTransport(); t._ready = lambda: None; t._write = Mock()
        t.write_response({'id':7,'result':{'decision':'decline'}}, 1)
        t._write.assert_called_once()
        with self.assertRaises(ProtocolError): t.write_response({'id':7,'result':{'decision':'acceptForSession'}}, 1)


class ApprovalAdapterTests(unittest.TestCase):
    def setUp(self):
        from mcp_adapter import Adapter
        from local_principal import current_local_principal
        self.principal = current_local_principal()
        self.parent = Path(__file__).resolve().parents[1] / '.runtime'
        self.parent.mkdir(mode=0o700, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.parent)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name); (root/'logs').mkdir(mode=0o700)
        self.outbox = SimpleNamespace(task_owned_by=Mock(return_value=False))
        service = SimpleNamespace(enabled=True)
        runtime = SimpleNamespace(runtime_root=root, outbox=self.outbox,
            adapter_options=lambda: {'events_service': service})
        self.proxy = SimpleNamespace(dispatch=Mock(return_value={'status':'PENDING'}))
        self.adapter = Adapter(root, allow_codex_pipeline=True, pipeline=self.proxy,
            notification_runtime=runtime)
        self.addCleanup(self.adapter.audit.close)
    def arguments(self, name):
        a = {'task_id':TASK}
        if name.endswith('decide'): a.update(approval_id=THREAD,
            request_sha256='a'*64, decision='accept', request_key='decision-1')
        return a
    def test_read_and_decision_both_require_owner_binding(self):
        from mcp_adapter import Invalid
        for name in ('codex_approval_get', 'codex_approval_decide'):
            result = self.adapter.call_tool(name, self.arguments(name), 1, request_principal=self.principal)
            self.assertTrue(result['isError'])
        self.proxy.dispatch.assert_not_called()
        self.assertEqual(self.outbox.task_owned_by.call_count, 2)
    def test_owned_read_reaches_only_scoped_backend(self):
        self.outbox.task_owned_by.return_value = True
        r = self.adapter.call_tool('codex_approval_get', {'task_id':TASK}, 1,
                                   request_principal=self.principal)
        self.assertFalse(r['isError'])
        self.proxy.dispatch.assert_called_once_with('codex_approval_get', {'task_id':TASK})


if __name__ == '__main__': unittest.main()
