"""Parent-driven collaboration tool envelopes; no tool execution or wake service.

Only the currently documented collaboration namespace is supported. The parent
executes descriptors directly, then records observations through this adapter.
"""
import hashlib
import json
from pathlib import Path
import re
import shlex
import math

from common_policy import load_project_policy
from native_team import sha
from chat_mailbox import _json
from native_team_artifacts import _no_symlinks
from native_handoff import HandoffStore
from native_agent_profiles import ROLES as ROLE_PROFILES, MODELS, load_profile

PROFILE = 'collaboration-parent-v4'
TOOLS = {
    'collaboration.spawn_agent': {'task_name', 'message', 'fork_turns', 'model', 'reasoning_effort'},
    'collaboration.list_agents': {'path_prefix'},
    'collaboration.interrupt_agent': {'target'},
    'collaboration.wait_agent': {'timeout_ms'},
}
ROLES = {role: profile[0] for role, profile in ROLE_PROFILES.items() if role != 'Coordinator'}


class AdapterError(ValueError):
    pass


class NativeParentAdapter:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.team = dispatcher.team
        self._config(self.team.snapshot())
        self.handoffs = HandoffStore(self.team.root)

    @staticmethod
    def _config(state):
        config = state['dispatcher'].get('parent_adapter')
        if (not isinstance(config, dict) or config.get('profile') != PROFILE or config.get('schema') != 1
                or config.get('fork_turns') != 'none' or config.get('task_transport') != 'sha256-file'):
            raise AdapterError('PARENT_ADAPTER_NOT_CONFIGURED')
        return config

    @staticmethod
    def _scope(parent_task, step_limit):
        if (not isinstance(parent_task, str)
                or not re.fullmatch(r'/root(?:/[a-z0-9_]+)*', parent_task)
                or len(parent_task) > 100):
            raise AdapterError('INVALID_PARENT_TASK')
        if type(step_limit) is not int or not 1 <= step_limit <= 1000:
            raise AdapterError('INVALID_STEP_LIMIT')

    @staticmethod
    def _context(state):
        spec = state['spec']
        bundle = load_project_policy(Path(spec['project']))
        if spec['policy'] != {'version': bundle.version, 'sha256': bundle.sha256}:
            raise AdapterError('POLICY_PIN_MISMATCH')
        roles = {}
        for role in {t['role'] for t in spec['tasks']} - {'Coordinator'}:
            if role not in ROLES or spec['registry']['roles'][role].get('native_role') != ROLES[role]:
                raise AdapterError('UNSUPPORTED_ROLE_PROFILE')
            path = _no_symlinks(Path(spec['project']) / '.codex' / 'agents' / (ROLES[role] + '.toml'))
            try:
                raw, value = load_profile(spec['project'], role)
            except ValueError as error:
                raise AdapterError(str(error)) from None
            roles[role] = {'sha256': hashlib.sha256(raw).hexdigest(),
                           'instructions': value['developer_instructions'],
                           'model': value['model'],
                           'reasoning_effort': value['model_reasoning_effort']}
        return bundle, roles

    @classmethod
    def configure(cls, dispatcher, parent_task, step_limit=1000):
        cls._scope(parent_task, step_limit)
        state = dispatcher.snapshot()
        _, roles = cls._context(state)
        fixed = {'schema': 1, 'profile': PROFILE, 'parent_task': parent_task,
                 'step_limit': step_limit, 'fork_turns': 'none', 'task_transport': 'sha256-file',
                 'role_hashes': {k: v['sha256'] for k, v in roles.items()}}
        with dispatcher.team._change() as (data, plan, now):
            journal = dispatcher._journal(data)
            existing = journal.get('parent_adapter')
            if existing:
                if {k: existing.get(k) for k in fixed} != fixed:
                    raise AdapterError('PARENT_ADAPTER_CONFIG_CONFLICT')
            else:
                if journal['actions'] or data['bindings']:
                    raise AdapterError('ADAPTER_REQUIRES_UNSTARTED_RUN')
                journal['parent_adapter'] = {**fixed, 'steps': 0}
        return cls(dispatcher)

    def _action(self, action_id, kind=None):
        if not isinstance(action_id, str):
            raise AdapterError('INVALID_ACTION_ID')
        state = self.team.snapshot()
        action = state['dispatcher']['actions'].get(action_id)
        if not action:
            retired = next((x for x in state['dispatcher']['retired'] if x['action_id'] == action_id), None)
            if retired:
                action = {**retired, 'status': 'RETIRED', 'agent_id': None}
                if action['kind'] not in ['SPAWN', 'COORDINATE']:
                    action['agent_id'] = self._name(state, action)
        if not action or action['status'] not in ['ISSUED', 'OBSERVED', 'RETIRED']:
            raise AdapterError('ACTION_REQUIRES_RECONCILIATION')
        if kind and action['kind'] != kind:
            raise AdapterError('ACTION_KIND_MISMATCH')
        return state, action

    def _name(self, state, action):
        parent = self._config(state)['parent_task']
        token = sha({'spec': state['spec_sha256'], 'dispatch': action['dispatch_id']})[:32]
        return parent + '/nt_' + token

    @staticmethod
    def _call(tool, arguments, actions):
        if tool not in TOOLS or set(arguments) != TOOLS[tool]:
            raise AdapterError('TOOL_CONTRACT_MISMATCH')
        if tool == 'collaboration.spawn_agent' and (arguments['model'] not in MODELS
                or arguments['reasoning_effort'] != 'max' or arguments['fork_turns'] != 'none'):
            raise AdapterError('UNSUPPORTED_MODEL_SELECTION')
        return {'tool': tool, 'arguments': arguments, 'action_ids': actions}

    def _bound_agent(self, state, action):
        expected = self._name(state, action)
        if action['agent_id'] != expected:
            raise AdapterError('NATIVE_BINDING_SCOPE_MISMATCH')
        return expected

    def step(self):
        state = self.team.snapshot()
        config = self._config(state)
        bundle, roles = self._context(state)
        if {k: v['sha256'] for k, v in roles.items()} != config['role_hashes']:
            raise AdapterError('ROLE_PIN_MISMATCH')
        # Reserve a finite iteration before any new action is handed out.
        with self.team._change() as (data, plan, now):
            current = self._config(data)
            if current['steps'] >= current['step_limit']:
                raise AdapterError('PARENT_STEP_LIMIT_REACHED')
            current['steps'] += 1
        progress = self.dispatcher.advance()
        actions = self.dispatcher.take()
        state = self.team.snapshot()
        calls, jobs, poll_ids = [], [], []
        for action in actions:
            kind = action['kind']
            if kind == 'SPAWN':
                task = action['task']
                prompt = {
                    'goal': state['spec']['goal'], 'requirements': state['spec']['requirements'],
                    'task_id': action['task_id'], 'dispatch_id': action['dispatch_id'],
                    'workspace': task['workspace'], 'input_revision': action['input_revision'],
                    'role': task['role'], 'instruction': task['instruction'], 'allowed': task['allowed'],
                    'policy': action['policy'],
                    'model': roles[task['role']]['model'],
                    'reasoning_effort': roles[task['role']]['reasoning_effort'],
                    'task_timeout_s': state['spec']['registry']['budgets']['task_timeout_s'],
                }
                message = (
                    'Work only in the absolute workspace in the task below. Use it explicitly for every tool call.\n'
                    'Do not create subagents, extra isolation/namespaces, commit/push, change services or credentials.\n'
                    'Higher-priority rules, environment permissions and explicit user constraints remain binding.\n'
                    'This role message does not grant permissions or enforce an OS sandbox.\n'
                    'COMMON POLICY (verified body)\n' + bundle.text + '\nROLE INSTRUCTIONS\n'
                    + roles[task['role']]['instructions'] + '\nTASK\n'
                    + json.dumps(prompt, ensure_ascii=False, sort_keys=True)
                )
                budgets = state['spec']['registry']['budgets']
                deadline = min(state['plan']['tasks'][action['task_id']]['started'] + budgets['task_timeout_s'],
                               state['plan']['run_started'] + budgets['run_timeout_s'])
                reference = self.handoffs.put('task', action['action_id'], {
                    'schema': 1, 'action_id': action['action_id'], 'dispatch_id': action['dispatch_id'],
                    'policy': action['policy'], 'message': message, 'not_after': deadline})
                cli = Path(__file__).resolve().parents[1] / 'scripts/native_parent.py'
                command = shlex.join(['python3', '-B', str(cli), 'context', '--run', str(self.team.root)])
                body = json.dumps({'action_id': action['action_id'], 'sha256': reference['sha256']}, sort_keys=True)
                bootstrap = ('Read the canonical task before doing work. Run this fixed local command with the JSON on stdin:\n'
                             + command + '\n' + body + '\n'
                             'Follow result.message only if status is OK and its context_sha256 matches the requested hash. '
                             'On any failure, stop and report BLOCKED. Do not edit files, create children, namespaces, '
                             'services, credentials or external messages while loading the task. Higher-priority rules apply.')
                call = self._call('collaboration.spawn_agent', {
                    'task_name': self._name(state, action).rsplit('/', 1)[1],
                    'message': bootstrap, 'fork_turns': 'none',
                    'model': roles[task['role']]['model'],
                    'reasoning_effort': roles[task['role']]['reasoning_effort']}, [action['action_id']])
                call['expected_agent_name'] = self._name(state, action)
                call['not_after'] = deadline
                call['context_ref'] = reference
                call['arguments_sha256'] = sha(call['arguments'])
                calls.append(call)
            elif kind == 'POLL':
                self._bound_agent(state, action)
                poll_ids.append(action['action_id'])
            elif kind == 'INTERRUPT':
                target = self._bound_agent(state, action)
                calls.append(self._call('collaboration.interrupt_agent', {'target': target}, [action['action_id']]))
            else:
                task = state['plan']['tasks'][action['task_id']]
                jobs.append({'action_id': action['action_id'], 'kind': kind,
                             'task_id': action['task_id'], 'dispatch_id': action['dispatch_id'],
                             'workspace': task['workspace'], 'instruction': task['instruction'],
                             'requirements': state['spec']['requirements'],
                             'allowed': task['allowed'],
                             'inputs': {dep: {'workspace': state['plan']['tasks'][dep]['workspace'],
                                             'receipt': state['receipts'][dep]}
                                        for dep in task['deps']},
                             'receipt_required': True})
        if poll_ids:
            calls.append(self._call('collaboration.list_agents',
                                    {'path_prefix': config['parent_task']}, poll_ids))
        for call in calls:
            call['arguments_sha256'] = sha(call['arguments'])
            budgets = state['spec']['registry']['budgets']
            call['deadlines'] = {i: min(state['plan']['tasks'][state['dispatcher']['actions'][i]['task_id']]['started'] + budgets['task_timeout_s'],
                                       state['plan']['run_started'] + budgets['run_timeout_s'])
                                 for i in call['action_ids']}
            call['request_ref'] = self.handoffs.put('request', sha(call['action_ids']), call)
        issued = {a['action_id'] for a in actions}
        progress['states'] = {k: t['state'] for k, t in state['plan']['tasks'].items()}
        if not actions and any(a['status'] == 'BLOCKED' for a in state['dispatcher']['actions'].values()):
            progress['status'] = 'BLOCKED'
        pending = [self._pending(state, a) for a in state['dispatcher']['actions'].values()
                   if a['action_id'] not in issued and a['status'] in ['ISSUED', 'OBSERVED', 'BLOCKED']]
        now = self.team.clock()
        deadlines = [min(t['started'] + state['spec']['registry']['budgets']['task_timeout_s'],
                         state['plan']['run_started'] + state['spec']['registry']['budgets']['run_timeout_s'])
                     for t in state['plan']['tasks'].values() if t['started'] is not None
                     and t['id'] in state['bindings'] and not state['bindings'][t['id']]['released']]
        wait = (self._call('collaboration.wait_agent', {'timeout_ms': 10000}, [])
                if progress['unreleased'] and deadlines and min(deadlines) - now > 10
                and not calls and not jobs and not pending
                and all(b['agent_id'] for b in state['bindings'].values() if not b['released']) else None)
        if wait:
            # wait_agent returns a notification, not an Agent result. Pin the
            # held dispatches so its receipt cannot refer to another attempt.
            wait['wait_scope'] = {task_id: {'dispatch_id': b['dispatch_id'], 'agent_id': b['agent_id']}
                                  for task_id, b in state['bindings'].items() if not b['released']}
            wait['step_number'] = self._config(state)['steps']
            wait['arguments_sha256'] = sha(wait['arguments'])
            wait['request_ref'] = self.handoffs.put('request', sha({
                'wait_at_step': wait['step_number'], 'spec_sha256': state['spec_sha256']}), wait)
        return {'profile': PROFILE, 'progress': progress, 'tool_calls': calls, 'parent_jobs': jobs,
                'pending_reconciliation': pending, 'optional_wait': wait,
                'steps_used': self._config(state)['steps'], 'automatic_tool_execution': False,
                'wake_after_parent_turn': False}

    def context(self, action_id, sha256):
        state, action = self._action(action_id, 'SPAWN')
        if (state['plan']['tasks'][action['task_id']]['state'] != 'RUNNING'
                or state['bindings'][action['task_id']]['released']):
            raise AdapterError('CONTEXT_NOT_ACTIVE')
        value = self.handoffs.read({'kind': 'task', 'key': action_id, 'sha256': sha256})
        if (value['action_id'] != action_id or value['dispatch_id'] != action['dispatch_id']
                or value['policy'] != state['spec']['policy']):
            raise AdapterError('CONTEXT_CORRELATION_MISMATCH')
        if self.team.clock() >= value['not_after']:
            raise AdapterError('CONTEXT_DEADLINE_REACHED')
        return {'context_sha256': sha256, 'message': value['message']}

    def _readonly_scope(self):
        state = self.team.snapshot()
        tasks = state['spec']['tasks']
        budgets = state['spec']['registry']['budgets']
        if (len(tasks) != 1 or tasks[0]['role'] != 'Researcher' or tasks[0]['allowed']
                or tasks[0]['deps'] or tasks[0]['backend'] != 'cloud'
                or budgets['max_dispatches'] != 1 or budgets['max_retries'] != 0
                or budgets['cloud_concurrency'] != 1
                or budgets['task_timeout_s'] > 120 or budgets['run_timeout_s'] > 120):
            raise AdapterError('READONLY_SMOKE_SCOPE_REQUIRED')
        return state

    def readonly_prepare(self):
        # Reject general development tasks before admission; this fixed caller
        # verifies only the bounded synthetic Researcher contract below.
        self._readonly_scope()
        return self.step()

    def parent_result(self, request_ref, result):
        from native_tool_driver import NativeToolDriver
        return NativeToolDriver(self).record_parent_result(request_ref, result)

    def handoff_request(self, request_ref):
        self._readonly_scope()
        call = self.handoffs.read(request_ref)
        if call['tool'] != 'collaboration.spawn_agent' or len(call['action_ids']) != 1:
            raise AdapterError('SPAWN_REQUEST_REQUIRED')
        state, action = self._action(call['action_ids'][0], 'SPAWN')
        if call['expected_agent_name'] != self._name(state, action):
            raise AdapterError('SPAWN_RESULT_SCOPE_MISMATCH')
        return {**call, 'request_ref': request_ref,
                'binding_confirmed': state['bindings'][action['task_id']]['agent_id'] == call['expected_agent_name'],
                'receipt': state['receipts'].get(action['task_id'])}

    def readonly_verified(self, action_id, capture_ref, spawn_request_ref):
        self._readonly_scope()
        state, action = self._action(action_id, 'VERIFY')
        spawn = self.handoff_request(spawn_request_ref)
        _, spawn_action = self._action(spawn['action_ids'][0], 'SPAWN')
        if capture_ref is None:
            # Recover a previously captured terminal reply after a bounded
            # caller return. No new poll/spawn or answer transcription.
            candidates = []
            for saved in self._config(state).get('captures', {}).values():
                value = self.handoffs.read(saved['capture_ref'])
                if (value.get('measurement_tool') != 'parent_standard_tool'
                        and value.get('provider_evidence', {}).get('state') != 'COMPLETED'):
                    continue
                request = self.handoffs.read(value['request'])
                if request['tool'] != 'collaboration.list_agents':
                    continue
                ids = value.get('effective_action_ids', request['action_ids'])
                if len(ids) == 1 and self._action(ids[0])[1]['dispatch_id'] == action['dispatch_id']:
                    if value.get('measurement_tool') == 'parent_standard_tool':
                        rows = value['result']['agents']
                        matched = [r['agent_status'] for r in rows
                                   if r['agent_name'] == self._bound_agent(state, action)]
                        if (len(matched) != 1 or not isinstance(matched[0], dict)
                                or set(matched[0]) != {'completed'}
                                or not isinstance(matched[0]['completed'], str)):
                            continue
                    candidates.append(saved['capture_ref'])
            if len(candidates) != 1:
                raise AdapterError('READONLY_CAPTURE_CORRELATION_REQUIRED')
            capture_ref = candidates[0]
        capture = self.handoffs.read(capture_ref)
        poll = self.handoffs.read(capture['request'])
        poll_ids = capture.get('effective_action_ids', poll['action_ids'])
        if len(poll_ids) != 1:
            raise AdapterError('READONLY_CAPTURE_CORRELATION_REQUIRED')
        _, poll_action = self._action(poll_ids[0], 'POLL')
        if (spawn_action['dispatch_id'] != action['dispatch_id']
                or poll_action['dispatch_id'] != action['dispatch_id']
                or poll['tool'] != 'collaboration.list_agents'
                or capture.get('measurement_tool') not in ['codex_app.read_thread', 'parent_standard_tool']
                or capture_ref['key'] not in self._config(state).get('captures', {})):
            raise AdapterError('READONLY_CAPTURE_CORRELATION_REQUIRED')
        context = self.handoffs.read(spawn['context_ref'])
        if capture.get('measurement_tool') == 'parent_standard_tool':
            rows = capture['result']['agents']
            statuses = [r['agent_status'] for r in rows if r['agent_name'] == self._bound_agent(state, action)]
            status = statuses[0] if len(statuses) == 1 else None
            evidence = ({'state': 'COMPLETED', 'answer': status['completed']}
                        if isinstance(status, dict) and set(status) == {'completed'}
                        and isinstance(status['completed'], str) else {'state': 'UNKNOWN'})
        else:
            evidence = capture['provider_evidence']
        expected = {'context_sha256': spawn['context_ref']['sha256'],
                    'policy_version': context['policy']['version'],
                    'policy_sha256': context['policy']['sha256'], 'role': 'Researcher'}
        try:
            answer = _json(evidence['answer'].encode())
        except (ValueError, TypeError, AttributeError, KeyError):
            answer = None
        accepted = (evidence.get('state') == 'COMPLETED' and answer == expected
                    and self.team.clock() < context['not_after'])
        _, roles = self._context(state)
        if {k: v['sha256'] for k, v in roles.items()} != self._config(state)['role_hashes']:
            raise AdapterError('ROLE_PIN_MISMATCH')
        if self.team.clock() >= context['not_after']:
            self.team.expire()
        registration = self.verified(action_id, accepted, 'Fixed read-only context/policy/role check',
            {'commands': ['read-only canonical context/policy/role JSON comparison',
                          'NativeTeam input revision and empty artifact allowlist check'],
             'exit_codes': [0 if accepted else 1, 0]})
        return {'accepted': accepted, 'registration': registration,
                'capture_ref': capture_ref, 'state': self.team.snapshot()['plan']['tasks'][action['task_id']]['state']}

    def thread_observation(self, request_ref, capture):
        """Ingest the mechanical read-only app capture; never parse model times."""
        from native_tool_driver import NativeToolDriver
        if (not isinstance(capture, dict) or capture.get('schema') != 1
                or capture.get('source') != 'codex_app.read_thread'):
            raise AdapterError('INVALID_THREAD_CAPTURE')
        call = self.handoffs.read(request_ref)
        if call['tool'] != 'collaboration.list_agents':
            raise AdapterError('POLL_REQUEST_REQUIRED')
        matches = []
        for action_id in call['action_ids']:
            state, action = self._action(action_id, 'POLL')
            if self._bound_agent(state, action) == capture.get('agent_path'):
                matches.append((action_id, state, action))
        if len(matches) != 1:
            raise AdapterError('CAPTURE_AGENT_MISMATCH')
        action_id, state, action = matches[0]
        child_id = capture.get('child_thread_id')
        if not isinstance(child_id, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', child_id):
            raise AdapterError('CAPTURE_THREAD_ID_REQUIRED')
        if capture.get('deadline_at') != call['deadlines'][action_id]:
            raise AdapterError('CAPTURE_DEADLINE_MISMATCH')
        old_thread = self._config(state).get('native_threads', {}).get(action['dispatch_id'])
        if old_thread not in [None, child_id]:
            raise AdapterError('CAPTURE_THREAD_ID_CONFLICT')
        for field in ['poll_started_at', 'poll_finished_at', 'provider_started_at']:
            value = capture.get(field)
            if type(value) not in [int, float] or not math.isfinite(value) or value < 0:
                raise AdapterError('INVALID_CAPTURE_TIME')
        if not capture['poll_started_at'] <= capture['poll_finished_at'] <= self.team.clock():
            raise AdapterError('INVALID_CAPTURE_TIME')
        completed = capture.get('state') == 'COMPLETED'
        if capture.get('state') not in ['COMPLETED', 'RUNNING', 'FAILED']:
            raise AdapterError('INVALID_CAPTURE_STATE')
        if completed:
            at = capture.get('provider_completed_at')
            if (type(at) not in [int, float] or not math.isfinite(at)
                    or not capture['provider_started_at'] <= at <= capture['poll_finished_at']
                    or not isinstance(capture.get('answer'), str)):
                raise AdapterError('INVALID_COMPLETED_CAPTURE')
        def pin(journal, data, now):
            entries = journal['parent_adapter'].setdefault('native_threads', {})
            ticket = action['dispatch_id']
            if entries.get(ticket) not in [None, child_id]:
                raise AdapterError('CAPTURE_THREAD_ID_CONFLICT')
            entries[ticket] = child_id
        # Freeze the effective return first; the model body remains data only.
        key = sha([action_id])
        status = {'completed': capture['answer']} if completed else ('running' if capture['state'] == 'RUNNING' else 'unknown')
        result = {'agents': [{'agent_name': action['agent_id'], 'agent_status': status}]}
        reference = self.handoffs.put('result', key, {
            'request': request_ref, 'effective_action_ids': [action_id],
            'measurement_tool': 'codex_app.read_thread',
            'tool_started_at': capture['poll_started_at'], 'tool_finished_at': capture['poll_finished_at'],
            # Transport receipt is immutable across an identical redelivery;
            # the separate registered_at field records local durable admission.
            'captured_at': capture['poll_finished_at'], 'provider_evidence': capture,
            'provider_completed_at': capture.get('provider_completed_at'), 'result': result,
            'error_type': None, 'arguments_mutated': False})
        self.dispatcher._edit(pin)
        return {'capture_ref': reference, 'registration': NativeToolDriver(self).ingest(reference)}

    def bind_from_thread(self, request_ref, capture):
        """Bind from the app's canonical agent-path/thread relation, not typing an ID."""
        from native_tool_driver import NativeToolDriver
        call = self.handoffs.read(request_ref)
        if call['tool'] != 'collaboration.spawn_agent' or len(call['action_ids']) != 1:
            raise AdapterError('SPAWN_REQUEST_REQUIRED')
        action_id = call['action_ids'][0]
        state, action = self._action(action_id, 'SPAWN')
        if (not isinstance(capture, dict) or capture.get('schema') != 1
                or capture.get('source') != 'codex_app.read_thread'
                or capture.get('agent_path') != self._name(state, action)
                or capture.get('deadline_at') != call['deadlines'][action_id]):
            raise AdapterError('CAPTURE_AGENT_MISMATCH')
        child = capture.get('child_thread_id')
        if not isinstance(child, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', child):
            raise AdapterError('CAPTURE_THREAD_ID_REQUIRED')
        start, finish = capture.get('poll_started_at'), capture.get('poll_finished_at')
        if (any(type(v) not in [int, float] or not math.isfinite(v) for v in [start, finish])
                or not 0 <= start <= finish <= self.team.clock()):
            raise AdapterError('INVALID_CAPTURE_TIME')
        old = self._config(state).get('native_threads', {}).get(action['dispatch_id'])
        if old not in [None, child]:
            raise AdapterError('CAPTURE_THREAD_ID_CONFLICT')
        reference = self.handoffs.put('result', sha([action_id]), {
            'request': request_ref, 'tool_started_at': start, 'tool_finished_at': finish,
            'captured_at': finish, 'measurement_tool': 'codex_app.read_thread',
            'provider_evidence': capture, 'result': {'task_name': capture['agent_path']},
            'error_type': None, 'arguments_mutated': False})
        def pin(journal, data, now):
            threads = journal['parent_adapter'].setdefault('native_threads', {})
            if threads.get(action['dispatch_id']) not in [None, child]:
                raise AdapterError('CAPTURE_THREAD_ID_CONFLICT')
            threads[action['dispatch_id']] = child
        self.dispatcher._edit(pin)
        return {'capture_ref': reference, 'registration': NativeToolDriver(self).ingest(reference)}

    def _pending(self, state, action):
        return {'action_id': action['action_id'], 'kind': action['kind'], 'task_id': action['task_id'],
                'status': action['status'],
                'expected_agent_name': (self._config(state)['parent_task'] if action['kind'] == 'COORDINATE'
                                        else self._name(state, action)),
                'reissue': False}

    def spawned(self, action_id, agent_name):
        state, action = self._action(action_id, 'SPAWN')
        if agent_name != self._name(state, action):
            raise AdapterError('SPAWN_RESULT_SCOPE_MISMATCH')
        # agent_name is extracted by the parent from an actual spawn result.
        # No undocumented raw spawn-return schema is assumed here.
        return self.dispatcher.observe(action_id, {'agent_id': agent_name})

    def inventory(self, action_ids, result):
        if (not isinstance(action_ids, list) or not action_ids or len(action_ids) > 6
                or any(not isinstance(x, str) for x in action_ids) or len(set(action_ids)) != len(action_ids)):
            raise AdapterError('INVALID_POLL_ACTION_IDS')
        if not isinstance(result, dict) or set(result) != {'agents'} or not isinstance(result['agents'], list):
            raise AdapterError('UNSUPPORTED_INVENTORY_RESULT')
        rows = result['agents']
        if any(not isinstance(x, dict) or set(x) != {'agent_name', 'agent_status'}
               or not isinstance(x['agent_name'], str) for x in rows):
            raise AdapterError('UNSUPPORTED_INVENTORY_RESULT')
        observations = []
        for action_id in action_ids:
            state, action = self._action(action_id, 'POLL')
            target = self._bound_agent(state, action)
            statuses = [x['agent_status'] for x in rows if x['agent_name'] == target]
            status = statuses[0] if len(statuses) == 1 else None
            native_state = ('RUNNING' if status == 'running' else 'SUCCEEDED'
                            if isinstance(status, dict) and set(status) == {'completed'}
                            and isinstance(status['completed'], str) else 'UNKNOWN')
            observations.append((action_id, {'agent_id': target, 'state': native_state}))
        # All IDs/shapes are checked first. A crash between observations leaves
        # the remaining issued actions visible; no spawn or whole batch replay.
        return [self.dispatcher.observe(action_id, event) for action_id, event in observations]

    def interrupted(self, action_id, accepted):
        state, action = self._action(action_id, 'INTERRUPT')
        return self.dispatcher.observe(action_id, {'agent_id': self._bound_agent(state, action),
                                                   'accepted': accepted})

    def verified(self, action_id, accepted, reason, verification):
        state, action = self._action(action_id)
        if action['kind'] not in ['COORDINATE', 'VERIFY']:
            raise AdapterError('ACTION_KIND_MISMATCH')
        agent = (self._config(state)['parent_task'] if action['kind'] == 'COORDINATE'
                 else self._bound_agent(state, action))
        return self.dispatcher.observe(action_id, {'agent_id': agent, 'accepted': accepted,
                                                   'reason': reason, 'verification': verification})
