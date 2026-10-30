"""One synchronous mechanical handoff through an explicitly supplied tool port.

There is no default collaboration API here. Without a host-provided callable,
tick/execute fail before admission. Local capture ingestion remains available.
Fixture ports never count as a native connection.
"""
from copy import deepcopy

from native_handoff import HandoffStore, encoded
from native_team import sha


class NativeToolDriver:
    def __init__(self, adapter, tool_port=None):
        if tool_port is not None and not callable(tool_port):
            raise ValueError('STANDARD_TOOL_PORT_NOT_CONNECTED')
        self.adapter = adapter
        self.port = tool_port
        self.store = HandoffStore(adapter.team.root)

    def tick(self):
        if not callable(self.port):
            raise ValueError('STANDARD_TOOL_PORT_NOT_CONNECTED')
        step = self.adapter.step()
        captures = []
        for call in step['tool_calls']:
            captures.append(self.execute(call))
        return {'step': step, 'captures': captures, 'automatic_wake': False}

    def record_parent_result(self, request_ref, result):
        """Register a parent's actual standard-tool return, without calling it.

        This receipt preserves local integrity, not provider authenticity. There
        is no API to pipe a collaboration return into Code Mode automatically.
        Unknown native timings stay unknown; registration time is authoritative.
        """
        call = self.store.read(request_ref)
        tool = call['tool']
        expected = {'collaboration.spawn_agent': 'SPAWN', 'collaboration.list_agents': 'POLL'}
        if tool not in expected:
            raise ValueError('PARENT_RETURN_SCHEMA_UNVERIFIED')
        ids = call['action_ids']
        if not ids or request_ref['key'] != sha(ids) or call['arguments_sha256'] != sha(call['arguments']):
            raise ValueError('CALL_ARGUMENTS_CHANGED')
        self.adapter._call(tool, call['arguments'], ids)
        for action_id in ids:
            self.adapter._action(action_id, expected[tool])
        encoded(result)
        original_hash = sha(result)
        if tool == 'collaboration.spawn_agent':
            if (len(ids) != 1 or not isinstance(result, dict) or set(result) != {'task_name'}
                    or result['task_name'] != call['expected_agent_name']):
                raise ValueError('SPAWN_RESULT_SCOPE_MISMATCH')
        else:
            if (not isinstance(result, dict) or set(result) != {'agents'} or not isinstance(result['agents'], list)
                    or any(not isinstance(r, dict) or set(r) != {'agent_name', 'agent_status'}
                           or not isinstance(r['agent_name'], str) for r in result['agents'])):
                raise ValueError('UNSUPPORTED_INVENTORY_RESULT')
            wanted = {self.adapter._action(i, 'POLL')[1]['agent_id'] for i in ids}
            result = {'agents': [r for r in result['agents'] if r['agent_name'] in wanted]}
        # No fabricated start/end/provider clock. A repeated identical delivery
        # has identical bytes, even after an ACK was lost. A changed one conflicts.
        reference = self.store.put('result', request_ref['key'], {
            'request': request_ref, 'measurement_tool': 'parent_standard_tool',
            'tool_started_at': None, 'tool_finished_at': None, 'captured_at': None,
            'provider_completed_at': None, 'raw_response_sha256': original_hash,
            'result': result, 'error_type': None, 'arguments_mutated': False})
        return {'capture_ref': reference, 'registration': self.ingest(reference),
                'source': 'parent_standard_tool', 'automatic_tool_execution': False,
                'provider_timing': 'UNKNOWN'}

    def execute(self, call):
        if not callable(self.port):
            raise ValueError('STANDARD_TOOL_PORT_NOT_CONNECTED')
        request = call['request_ref']
        canonical = self.store.read(request)
        if {k: v for k, v in call.items() if k != 'request_ref'} != canonical:
            raise ValueError('CANONICAL_CALL_CHANGED')
        call = canonical
        tool = call['tool']
        if tool not in ['collaboration.spawn_agent', 'collaboration.list_agents', 'collaboration.interrupt_agent']:
            raise ValueError('UNSUPPORTED_NATIVE_TOOL')
        ids = call['action_ids']
        if not ids or len(ids) != len(set(ids)):
            raise ValueError('INVALID_CALL_CORRELATION')
        # Validate exact argument fields against the session's fixed contract.
        self.adapter._call(tool, call['arguments'], ids)
        expected_kind = {'collaboration.spawn_agent': 'SPAWN', 'collaboration.list_agents': 'POLL',
                         'collaboration.interrupt_agent': 'INTERRUPT'}[tool]
        for action_id in ids:
            _, action = self.adapter._action(action_id, expected_kind)
            if action['status'] != 'ISSUED':
                raise ValueError('ACTION_NOT_FRESHLY_ISSUED')
        request_key = request['key']
        if request_key != sha(ids) or call['arguments_sha256'] != sha(call['arguments']):
            raise ValueError('CALL_ARGUMENTS_CHANGED')
        if tool == 'collaboration.spawn_agent':
            if call.get('arguments_sha256') != sha(call['arguments']):
                raise ValueError('CALL_ARGUMENTS_CHANGED')
            self.adapter.context(call['context_ref']['key'], call['context_ref']['sha256'])
        started = self.adapter.team.clock()
        if call.get('not_after') is not None and started >= call['not_after']:
            raise ValueError('CALL_DEADLINE_REACHED')
        # An exclusive durable start prevents a second executor from replaying
        # a call after a lost return/ACK, including when native effects are UNKNOWN.
        self.store.put('started', request_key, {'request': request, 'tool_started_at': started}, exclusive=True)
        arguments = deepcopy(call['arguments'])
        try:
            raw = self.port(tool, arguments)
            finished = self.adapter.team.clock()
            encoded(raw)  # Enforce bounds without logging arbitrary diagnostics.
            error = None
        except Exception as exception:
            finished = self.adapter.team.clock()
            raw, error = None, type(exception).__name__
        try:
            mutated = sha(arguments) != sha(call['arguments'])
        except (TypeError, ValueError):
            mutated = True
        # Do not persist unrelated agents' response bodies from a broad listing.
        original_hash = sha(raw)
        if tool == 'collaboration.list_agents' and isinstance(raw, dict) and isinstance(raw.get('agents'), list):
            wanted = {self.adapter._action(i, 'POLL')[1]['agent_id'] for i in ids}
            raw = {**raw, 'agents': [r for r in raw['agents'] if isinstance(r, dict) and r.get('agent_name') in wanted]}
        result = self.store.put('result', request_key, {
            'request': request, 'tool_started_at': started, 'tool_finished_at': finished,
            'captured_at': self.adapter.team.clock(), 'provider_completed_at': None,
            'raw_response_sha256': original_hash, 'result': raw, 'error_type': error,
            'arguments_mutated': mutated})
        return {'capture_ref': result, 'registration': self.ingest(result)}

    def ingest(self, capture_ref):
        capture = self.store.read(capture_ref)
        call = self.store.read(capture['request'])
        effective = capture.get('effective_action_ids', call['action_ids'])
        if (not effective or len(effective) != len(set(effective))
                or set(effective) - set(call['action_ids'])):
            raise ValueError('CAPTURE_ACTION_MISMATCH')
        call = {**call, 'action_ids': effective}
        started, finished, captured = (capture[k] for k in ['tool_started_at', 'tool_finished_at', 'captured_at'])
        now = self.adapter.team.clock()
        parent_return = capture.get('measurement_tool') == 'parent_standard_tool'
        if parent_return and (started, finished, captured) != (None, None, None):
            raise ValueError('PARENT_RETURN_CLOCK_MUST_REMAIN_UNKNOWN')
        if not parent_return and not started <= finished <= captured <= now:
            raise ValueError('CAPTURE_CLOCK_ORDER_UNKNOWN')
        facts = {}
        for action_id in call['action_ids']:
            state, action = self.adapter._action(action_id)
            task = state['plan']['tasks'][action['task_id']]
            budgets = state['spec']['registry']['budgets']
            deadline = min(task['started'] + budgets['task_timeout_s'],
                           state['plan']['run_started'] + budgets['run_timeout_s'])
            evidence = capture.get('provider_evidence', {})
            facts[action_id] = {'deadline_at': deadline, 'tool_return_late': None if parent_return else finished >= deadline,
                                'registration_late': now >= deadline,
                                'provider_completed_at': evidence.get('provider_completed_at')}
        def log(journal, data, at):
            entries = journal['parent_adapter'].setdefault('captures', {})
            prior = entries.get(capture_ref['key'])
            if prior:
                if prior['capture_ref'] != capture_ref:
                    raise ValueError('CAPTURE_CONFLICT')
                return
            entries[capture_ref['key']] = {'capture_ref': capture_ref, 'request_ref': capture['request'],
                'logical_action_tool': call['tool'], 'tool': capture.get('measurement_tool', call['tool']),
                'tool_started_at': started, 'tool_finished_at': finished,
                'captured_at': captured, 'registered_at': now, 'deadlines': facts}
        self.adapter.dispatcher._edit(log)
        # The deadline remains end-to-end. Neither a quoted completion time nor
        # late local registration may turn an expired task into success.
        self.adapter.team.expire()
        if capture['error_type'] or capture['arguments_mutated']:
            state = self.adapter.team.snapshot()
            for action_id in call['action_ids']:
                _, action = self.adapter._action(action_id)
                if state['plan']['tasks'][action['task_id']]['state'] == 'RUNNING':
                    self.adapter.team.unknown(action['task_id'], action['dispatch_id'],
                                              'TOOL_RESULT_REQUIRES_RECONCILIATION')
            return {'applied': False, 'code': 'TOOL_RESULT_REQUIRES_RECONCILIATION'}
        raw = capture['result']
        if call['tool'] == 'collaboration.spawn_agent':
            if not isinstance(raw, dict) or set(raw) != {'task_name'}:
                return {'applied': False, 'code': 'UNSUPPORTED_SPAWN_REPLY'}
            return self.adapter.spawned(call['action_ids'][0], raw['task_name'])
        if call['tool'] == 'collaboration.list_agents':
            return self.adapter.inventory(call['action_ids'], raw)
        # No actual raw interrupt-return schema was observed in the approved run.
        # Preserve it mechanically, but do not invent acceptance or terminality.
        return {'applied': False, 'code': 'INTERRUPT_REPLY_SCHEMA_UNVERIFIED'}
