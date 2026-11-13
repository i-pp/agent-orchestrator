"""Composed local-owner access to persisted blockers, with fake peers only.

Adapted from the independently reviewed LAF blocked-input regressions. Keep the
canonical LocalPrincipal and approval routes; do not import an OAuth boundary.
"""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT / 'src'), str(PROJECT / 'scripts')]
import test_codex_pipeline as pipeline_cases
import test_codex_pipeline_gateway as gateway_cases
from codex_host import DaemonPipelineProxy
from daemon import Daemon
from local_principal import current_local_principal
from mcp_adapter import Adapter, REQUEST_PROTOCOL, VERSION_META, CAPABILITIES_META
from mcp_events_service import McpEventsService
from mcp_http_transport import McpHttpEndpoint, MAX_HTTP_BODY
from notification_outbox import NotificationOutbox, NotificationError
from test_mcp_events_lifecycle import Cipher


class PersistedBlockedOwnershipTests(unittest.TestCase):
    setUp = pipeline_cases.PipelineTests.setUp
    tearDown = pipeline_cases.PipelineTests.tearDown
    new_pipeline = pipeline_cases.PipelineTests.new_pipeline

    def compose(self, *, fixture=None, proxy=False, http=False):
        target = fixture or self
        self.owner = current_local_principal()
        # A historical low-level foreign origin, not a second LocalPrincipal.
        self.other_identity = 'local-uid:foreign-fixture'
        self.source = target.gateway.pipeline if fixture else self.pipeline
        self.outbox = NotificationOutbox(target.root / 'owner-outbox.sqlite3',
            source_db_path=self.source.db, secret_cipher=Cipher(),
            owner_hash_key=b'blocked-input-owner-key-0000000000000000000000',
            resolver=lambda host: ['8.8.8.8'])
        self.service = McpEventsService(self.outbox,
            authorize_task=lambda identity, task: identity == self.owner.identity
                and self.outbox.source_task_exists(task),
            owner_access_check=lambda owner_hash, task: self.outbox.task_owned_by_hash(task, owner_hash),
            callback_verifier=lambda request: {'status_code': 200}, allow_external_callbacks=True)
        backend = DaemonPipelineProxy(target.root) if proxy else self.source
        self.adapter = Adapter(target.root, allow_codex_pipeline=True, pipeline=backend,
                               events_service=self.service)
        self.adapter.notification_runtime = SimpleNamespace(outbox=self.outbox)
        self.addCleanup(self.adapter.audit.close)
        self.endpoint = (McpHttpEndpoint(self.adapter, expected_port=8765,
            allow_single_user_local=True, principal_provider=current_local_principal) if http else None)
        self.rpc_id = 0
        result = self.api('codex_thread_start', {'workspace': 'fixture', 'model': 'offline-model',
            'reasoning_effort': 'medium', 'request_key': 'local-owner-thread'})
        self.assertFalse(result['isError'])
        return result['structuredContent']['data']['thread_id']

    def api(self, name, arguments):
        self.rpc_id += 1
        if self.endpoint is None:
            return self.adapter.call_tool(name, arguments, self.rpc_id, request_principal=self.owner)
        request = {'jsonrpc': '2.0', 'id': self.rpc_id, 'method': 'tools/call', 'params': {
            'name': name, 'arguments': arguments,
            '_meta': {VERSION_META: REQUEST_PROTOCOL, CAPABILITIES_META: {}}}}
        raw = json.dumps(request, separators=(',', ':')).encode()
        headers = {'Host': '127.0.0.1:8765', 'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream', 'Content-Length': str(len(raw)),
            'MCP-Protocol-Version': REQUEST_PROTOCOL, 'Mcp-Method': 'tools/call', 'Mcp-Name': name}
        status, _, wire = self.endpoint.handle('POST', '/mcp', headers, raw)
        self.assertEqual(status, 200, wire[:200])
        self.assertLess(len(wire), MAX_HTTP_BODY)
        result = json.loads(wire)['result']
        self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])
        return result

    def arguments(self, thread, key='blocked-owned'):
        return {'thread_id': thread, 'prompt': 'Private synthetic blocked input',
                'reasoning_effort': 'not-advertised', 'request_key': key}

    def blocked_without_adapter(self, thread):
        arguments = self.arguments(thread)
        result = self.source.dispatch('codex_task_submit', arguments)
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertFalse(self.outbox.task_owned_by(result['id'], self.owner))
        return arguments, result

    def assert_owner_access(self, task, thread):
        self.assertTrue(self.outbox.task_owned_by(task, self.owner))
        self.assertFalse(self.outbox.task_owned_by(task, self.other_identity))
        read = self.api('codex_task_get', {'task_id': task})['structuredContent']['data']
        self.assertEqual((read['id'], read['state'], read['thread_id']), (task, 'BLOCKED', thread))
        listing = self.api('codex_threads_list', {})['structuredContent']['data']
        self.assertIn(task, {item['id'] for item in listing['tasks']})
        events = self.api('codex_events', {})['structuredContent']['data']['events']
        self.assertTrue(any(item.get('task_id') == task and item['kind'] == 'task_blocked' for item in events))
        self.assertNotIn('Private synthetic blocked input', repr(events))
        approval = self.api('codex_approval_get', {'task_id': task})['structuredContent']['data']
        self.assertEqual((approval['task_id'], approval['task_state']), (task, 'BLOCKED'))
        self.assertTrue(approval['human_final_approval_required'])
        self.assertFalse(approval['automatic_retry'])
        cancel = self.api('codex_task_cancel', {'task_id': task, 'request_key': 'owner-cancel-' + task})
        self.assertFalse(cancel['isError'])
        self.assertEqual(cancel['structuredContent']['data']['status'], 'CANCELLED')

    def test_local_unsupported_effort_submit_retains_owner_access_without_generation(self):
        thread = self.compose()
        arguments = self.arguments(thread)
        reply = self.api('codex_task_submit', arguments)
        task = reply['structuredContent']['data']
        self.assertTrue(reply['isError'])
        self.assertEqual((task['state'], task['reason_code']), ('BLOCKED', 'REASONING_EFFORT_NOT_SUPPORTED'))
        self.assertEqual(task.get('access_binding'), 'BOUND')
        self.assertEqual(task.get('notification_binding'), 'BOUND')
        before = len(self.peer.calls)
        duplicate = self.api('codex_task_submit', arguments)['structuredContent']['data']
        self.assertEqual(duplicate['id'], task['id'])
        self.assertEqual(duplicate.get('access_binding'), 'BOUND')
        self.assertEqual(len(self.peer.calls), before)
        self.assert_owner_access(task['id'], thread)
        self.assertEqual(self.peer.count('turn/start'), 0)
        self.assertEqual(self.peer.count('turn/interrupt'), 0)

    def test_http_blocked_submission_has_the_same_owner_access_and_error_result(self):
        thread = self.compose(http=True)
        reply = self.api('codex_task_submit', self.arguments(thread))
        task = reply['structuredContent']['data']
        self.assertTrue(reply['isError'])
        self.assertEqual(task.get('access_binding'), 'BOUND')
        self.assert_owner_access(task['id'], thread)
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_proxy_daemon_gateway_uses_the_same_readonly_ledger_proof(self):
        fixture = gateway_cases.GatewayTests('test_metadata_session_closes_and_releases_runner_budget')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        daemon = Daemon.__new__(Daemon)
        daemon.allow_codex_pipeline, daemon.pipeline = True, fixture.gateway
        calls = []
        def route(runtime, method, params, *, timeout):
            self.assertEqual(runtime, fixture.root)
            self.assertEqual(method, 'codex_pipeline')
            calls.append(params['tool'])
            return daemon.handle(method, params)
        with patch('codex_host.request', side_effect=route):
            thread = self.compose(fixture=fixture, proxy=True)
            task = self.api('codex_task_submit', self.arguments(thread))['structuredContent']['data']
            self.assertEqual(task['reason_code'], 'REASONING_EFFORT_NOT_SUPPORTED')
            self.assertEqual(task.get('access_binding'), 'BOUND')
            self.assertEqual(task.get('notification_binding'), 'BOUND')
            self.assert_owner_access(task['id'], thread)
        self.assertTrue(set(calls) <= {'codex_thread_start', 'codex_task_submit', 'codex_task_get',
            'codex_threads_list', 'codex_task_cancel', 'codex_approval_get'})
        self.assertEqual(fixture.peer.count('turn/start'), 0)
        fixture.gateway.release_idle(force=True)
        self.assertTrue(all(transport.closed for transport in fixture.transports))

    def test_other_definitive_pre_turn_blockers_retain_owner_access(self):
        thread = self.compose()
        for index, (kind, reason) in enumerate([
                ('unknown', 'MODEL_CATALOG_UNKNOWN'), ('rejected', 'CATALOG_REJECTED'),
                ('absent', 'MODEL_NOT_AVAILABLE_OR_ALLOWED'), ('resume', 'RESUME_REASONING_EFFORT_MISMATCH')]):
            with self.subTest(kind=kind):
                self.peer.failure.clear()
                self.peer.models = [copy.deepcopy(pipeline_cases.MODEL)]
                self.peer.effort_echo = 'requested'
                if kind == 'unknown': self.peer.failure['model/list'] = TimeoutError('Synthetic private cause')
                if kind == 'rejected': self.peer.failure['model/list'] = True
                if kind == 'absent': self.peer.models = []
                if kind == 'resume': self.pipeline.loaded.clear(); self.peer.effort_echo = 'high'
                arguments = {**self.arguments(thread, 'blocked-' + str(index)), 'reasoning_effort': 'medium'}
                reply = self.api('codex_task_submit', arguments)
                task = reply['structuredContent']['data']
                self.assertEqual(task['reason_code'], reason)
                self.assertTrue(reply['isError'])
                self.assertEqual(task.get('access_binding'), 'BOUND')
                self.assert_owner_access(task['id'], thread)
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_unknown_failed_or_ambiguous_results_are_not_ownership_proof(self):
        thread = self.compose()
        arguments, task = self.blocked_without_adapter(thread)
        variants = [{'status': 'UNKNOWN'}, {'status': 'FAILED'}, {'status': 'BLOCKED'},
            {'state': 'UNKNOWN'}, {'state': 'FAILED', 'status': 'FAILED'},
            {'task_id': '00000000-0000-4000-8000-000000000099'},
            {'execution_started': True}, {'execution_started': 0}]
        for change in variants:
            with self.subTest(change=change), patch.object(self.pipeline, 'dispatch', return_value={**task, **change}):
                reply = self.api('codex_task_submit', arguments)['structuredContent']['data']
                self.assertNotEqual(reply.get('access_binding'), 'BOUND')
                self.assertNotEqual(reply.get('notification_binding'), 'BOUND')
                self.assertFalse(self.outbox.task_owned_by(task['id'], self.owner))
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_real_lost_ack_unknown_stays_unbound_and_is_never_replayed(self):
        thread = self.compose(http=True)
        self.peer.failure['turn/start'] = TimeoutError('Private lost acknowledgement fixture')
        arguments = {**self.arguments(thread), 'reasoning_effort': 'medium'}
        first = self.api('codex_task_submit', arguments)
        task = first['structuredContent']['data']
        self.assertTrue(first['isError'])
        self.assertEqual(task['state'], 'UNKNOWN')
        self.assertFalse(self.outbox.task_owned_by(task['id'], self.owner))
        calls = self.peer.count('turn/start')
        self.assertEqual(calls, 1)
        retry = self.api('codex_task_submit', arguments)['structuredContent']['data']
        self.assertEqual((retry['id'], retry['state']), (task['id'], 'UNKNOWN'))
        self.assertNotEqual(retry.get('access_binding'), 'BOUND')
        self.assertNotEqual(retry.get('notification_binding'), 'BOUND')
        self.assertFalse(self.outbox.task_owned_by(task['id'], self.owner))
        self.assertEqual(self.peer.count('turn/start'), calls)

    def test_principal_provider_only_stdio_behavior_is_not_broadened(self):
        thread = self.compose()
        self.adapter.principal_provider = current_local_principal
        with patch.object(self.outbox, 'source_blocked_input_matches') as proof:
            result = self.adapter.call_tool('codex_task_submit', self.arguments(thread),
                                           101, request_principal=None)
            proof.assert_not_called()
        task = result['structuredContent']['data']
        self.assertTrue(result['isError'])
        self.assertEqual(task['state'], 'BLOCKED')
        self.assertNotEqual(task.get('notification_binding'), 'BOUND')
        self.assertFalse(self.outbox.task_owned_by(task['id'], self.owner))
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_healthy_store_keeps_existing_binding_without_new_proof(self):
        thread = self.compose()
        with patch.object(self.outbox, 'source_blocked_input_matches') as proof:
            reply = self.api('codex_chat_store', {**self.arguments(thread), 'reasoning_effort': 'medium'})
            proof.assert_not_called()
        task = reply['structuredContent']['data']
        self.assertFalse(reply['isError'])
        self.assertEqual(task['state'], 'STORED')
        self.assertEqual(task.get('access_binding'), 'BOUND')
        self.assertEqual(self.api('codex_task_get', {'task_id': task['id']})['structuredContent']['data']['id'], task['id'])
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_exact_stored_input_retry_can_reconcile_its_later_pre_turn_blocker(self):
        thread = self.compose()
        arguments = self.arguments(thread)
        task = self.source.dispatch('codex_chat_store', arguments)
        self.source.dispatch('codex_chat_queue', {'task_id': task['id'], 'request_key': 'queue-stored'})
        self.source.dispatch('codex_pipeline_tick', {})
        self.assertFalse(self.outbox.task_owned_by(task['id'], self.owner))
        result = self.api('codex_chat_store', arguments)
        current = result['structuredContent']['data']
        self.assertTrue(result['isError'])
        self.assertEqual((current['id'], current['state']), (task['id'], 'BLOCKED'))
        self.assertEqual(current.get('access_binding'), 'BOUND')
        self.assert_owner_access(task['id'], thread)
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_foreign_or_mismatched_submissions_cannot_create_an_origin(self):
        thread = self.compose()
        arguments, task = self.blocked_without_adapter(thread)
        other = self.source.dispatch('codex_thread_start', {'workspace': 'fixture', 'model': 'offline-model',
            'reasoning_effort': 'medium', 'request_key': 'foreign-thread'})['thread_id']
        self.outbox.bind_thread_origin(other, self.other_identity)
        foreign = self.source.dispatch('codex_task_submit', self.arguments(other, 'foreign-input'))
        for forged in ({**foreign, 'thread_id': thread}, {**task, 'prompt_sha256': '0' * 64},
                       {**task, 'selection_sha256': '0' * 64}):
            with self.subTest(task=forged['id']), patch.object(self.pipeline, 'dispatch', return_value=forged):
                reply = self.api('codex_task_submit', arguments)['structuredContent']['data']
                self.assertNotEqual(reply.get('access_binding'), 'BOUND')
                self.assertFalse(self.outbox.task_owned_by(forged['id'], self.owner))
        for change in ({'request_key': 'different'}, {'prompt': 'Different input'},
                       {'model': 'offline-model'}, {'reasoning_effort': 'medium'}):
            with self.subTest(change=change), patch.object(self.pipeline, 'dispatch', return_value=copy.deepcopy(task)):
                reply = self.api('codex_task_submit', {**arguments, **change})['structuredContent']['data']
                self.assertNotEqual(reply.get('access_binding'), 'BOUND')
                self.assertFalse(self.outbox.task_owned_by(task['id'], self.owner))
        for tool in ('codex_task_get', 'codex_task_cancel'):
            args = {'task_id': foreign['id'], **({'request_key': 'foreign-cancel'} if tool.endswith('cancel') else {})}
            self.assertEqual(self.api(tool, args)['structuredContent']['data']['code'], 'NOT_AUTHORIZED')
        listing = self.api('codex_threads_list', {})['structuredContent']['data']
        self.assertNotIn(foreign['id'], {item['id'] for item in listing['tasks']})
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_durable_operation_and_selection_provenance_are_required(self):
        thread = self.compose()
        arguments, task = self.blocked_without_adapter(thread)
        original = self.pipeline._mutate(lambda state: copy.deepcopy(state))
        changes = [lambda state: state['ops'][arguments['request_key']].update(status='UNKNOWN'),
            lambda state: state['ops'][arguments['request_key']].update(fingerprint='0' * 64),
            lambda state: state['ops'][arguments['request_key']]['context'].update(task_id=thread),
            lambda state: state['ops'][arguments['request_key']]['context'].pop('selection_sha256'),
            lambda state: state['tasks'][task['id']].update(remote_turn='uncertain-turn'),
            lambda state: state['tasks'][task['id']].update(deadline=100),
            lambda state: state['tasks'][task['id']].update(state='UNKNOWN')]
        for change in changes:
            def restore(state): state.clear(); state.update(copy.deepcopy(original)); change(state)
            self.pipeline._mutate(restore)
            with patch.object(self.pipeline, 'dispatch', return_value=copy.deepcopy(task)):
                reply = self.api('codex_task_submit', arguments)['structuredContent']['data']
            self.assertNotEqual(reply.get('access_binding'), 'BOUND')
            self.assertFalse(self.outbox.task_owned_by(task['id'], self.owner))
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_existing_authorizer_and_foreign_origin_still_control_binding(self):
        thread = self.compose()
        arguments, task = self.blocked_without_adapter(thread)
        with patch.object(self.service, 'authorize_task', return_value=False), \
                patch.object(self.pipeline, 'dispatch', return_value=copy.deepcopy(task)):
            denied = self.api('codex_task_submit', arguments)['structuredContent']['data']
        self.assertNotEqual(denied.get('access_binding'), 'BOUND')
        self.assertFalse(self.outbox.task_owned_by(task['id'], self.owner))
        self.outbox.bind_task_origin(task['id'], self.other_identity)
        with patch.object(self.pipeline, 'dispatch', return_value=copy.deepcopy(task)):
            conflict = self.api('codex_task_submit', arguments)['structuredContent']['data']
        self.assertEqual(conflict.get('access_binding'), 'UNAVAILABLE')
        self.assertEqual(conflict.get('notification_binding'), 'UNAVAILABLE')
        self.assertTrue(self.outbox.task_owned_by(task['id'], self.other_identity))
        self.assertFalse(self.outbox.task_owned_by(task['id'], self.owner))
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_temporary_proof_failure_retry_binds_existing_input_without_replay(self):
        thread = self.compose()
        arguments = self.arguments(thread)
        with patch.object(self.outbox, 'source_blocked_input_matches',
                side_effect=NotificationError('Synthetic private proof failure')):
            first = self.api('codex_task_submit', arguments)['structuredContent']['data']
        self.assertEqual(first['state'], 'BLOCKED')
        self.assertFalse(self.outbox.task_owned_by(first['id'], self.owner))
        before = self.pipeline._mutate(lambda state: copy.deepcopy(state))
        calls = len(self.peer.calls)
        retry = self.api('codex_task_submit', arguments)['structuredContent']['data']
        self.assertEqual(retry['id'], first['id'])
        self.assertEqual(retry.get('access_binding'), 'BOUND')
        self.assertEqual(self.pipeline._mutate(lambda state: state), before)
        self.assertEqual(len(self.peer.calls), calls)
        self.assertNotIn('Synthetic private proof failure', json.dumps(first))
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_ledger_lock_size_and_runtime_path_fail_closed_without_mutation(self):
        thread = self.compose()
        arguments, task = self.blocked_without_adapter(thread)
        before = len(self.peer.calls)
        with closing(sqlite3.connect(self.pipeline.db)) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            with patch.object(self.pipeline, 'dispatch', return_value=copy.deepcopy(task)):
                reply = self.api('codex_task_submit', arguments)['structuredContent']['data']
            self.assertNotEqual(reply.get('access_binding'), 'BOUND')
            writer.rollback()
        self.assertFalse(self.outbox.task_owned_by(task['id'], self.owner))
        source = self.outbox.source_path
        with patch.object(self.outbox, 'source_path', self.root / 'different-ledger.sqlite3'), \
                patch.object(self.pipeline, 'dispatch', return_value=copy.deepcopy(task)):
            reply = self.api('codex_task_submit', arguments)['structuredContent']['data']
        self.assertNotEqual(reply.get('access_binding'), 'BOUND')
        with closing(sqlite3.connect(source)) as connection:
            raw = connection.execute('SELECT body FROM ledger WHERE id=1').fetchone()[0]
            connection.execute('UPDATE ledger SET body=? WHERE id=1', (raw.ljust(2 * 1024**2 + 1),))
            connection.commit()
        with patch.object(self.pipeline, 'dispatch', return_value=copy.deepcopy(task)):
            reply = self.api('codex_task_submit', arguments)['structuredContent']['data']
        self.assertNotEqual(reply.get('access_binding'), 'BOUND')
        self.assertFalse(self.outbox.task_owned_by(task['id'], self.owner))
        with closing(sqlite3.connect(source)) as connection:
            self.assertEqual(connection.execute('SELECT length(CAST(body AS BLOB)) FROM ledger WHERE id=1').fetchone()[0], 2 * 1024**2 + 1)
        self.assertEqual(len(self.peer.calls), before)

    def test_missing_symlink_and_nonprivate_source_fail_closed(self):
        thread = self.compose()
        arguments, task = self.blocked_without_adapter(thread)
        source = self.outbox.source_path
        backup = source.with_name('source-proof-backup.sqlite3')
        original = source.read_bytes()
        source.rename(backup)
        try:
            with patch.object(self.pipeline, 'dispatch', return_value=copy.deepcopy(task)):
                result = self.api('codex_task_submit', arguments)['structuredContent']['data']
            self.assertNotEqual(result.get('access_binding'), 'BOUND')
            self.assertFalse(source.exists())
            source.symlink_to(backup)
            try:
                with patch.object(self.pipeline, 'dispatch', return_value=copy.deepcopy(task)):
                    result = self.api('codex_task_submit', arguments)['structuredContent']['data']
                self.assertNotEqual(result.get('access_binding'), 'BOUND')
                self.assertEqual(backup.read_bytes(), original)
            finally:
                source.unlink()
        finally:
            backup.rename(source)
        source.chmod(0o644)
        try:
            with patch.object(self.pipeline, 'dispatch', return_value=copy.deepcopy(task)):
                result = self.api('codex_task_submit', arguments)['structuredContent']['data']
            self.assertNotEqual(result.get('access_binding'), 'BOUND')
        finally:
            source.chmod(0o600)
        self.assertFalse(self.outbox.task_owned_by(task['id'], self.owner))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self.peer.count('turn/start'), 0)

    def test_canonical_approval_routes_keep_the_existing_owner_gate(self):
        thread = self.compose()
        arguments, task = self.blocked_without_adapter(thread)
        self.outbox.bind_task_origin(task['id'], self.other_identity)
        approval = {'task_id': task['id'], 'approval_id': '00000000-0000-4000-8000-000000000099',
                    'request_sha256': 'a' * 64, 'request_key': 'fixture-approval'}
        tools = {'codex_approval_get': {'task_id': task['id']},
                 'codex_approval_begin': approval,
                 'codex_approval_decide': {**approval, 'decision': 'decline'}}
        for name, params in tools.items():
            with self.subTest(name=name), patch.object(self.pipeline, 'dispatch') as dispatch:
                result = self.api(name, params)['structuredContent']['data']
                self.assertEqual(result['code'], 'NOT_AUTHORIZED')
                dispatch.assert_not_called()
        self.assertFalse(self.outbox.task_owned_by(task['id'], self.owner))
        self.assertEqual(self.peer.count('turn/start'), 0)


if __name__ == '__main__':
    unittest.main()
