"""Parent-driven collaboration tool envelopes; no tool execution or wake service.

Only the currently documented collaboration namespace is supported. The parent
executes descriptors directly, then records observations through this adapter.
"""
import hashlib
import json
from pathlib import Path
import re
import tomllib

from common_policy import load_project_policy
from native_team import sha
from native_team_artifacts import _no_symlinks

PROFILE = 'collaboration-parent-v2'
TOOLS = {
    'collaboration.spawn_agent': {'task_name', 'message', 'fork_turns'},
    'collaboration.list_agents': {'path_prefix'},
    'collaboration.interrupt_agent': {'target'},
    'collaboration.wait_agent': {'timeout_ms'},
}
ROLES = {'Implementer': 'implementer', 'Verifier': 'verifier', 'Researcher': 'researcher'}


class AdapterError(ValueError):
    pass


class NativeParentAdapter:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.team = dispatcher.team
        self._config(self.team.snapshot())

    @staticmethod
    def _config(state):
        config = state['dispatcher'].get('parent_adapter')
        if (not isinstance(config, dict) or config.get('profile') != PROFILE or config.get('schema') != 1
                or config.get('fork_turns') != 'none'):
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
            if path.stat().st_size > 65536:
                raise AdapterError('ROLE_SIZE_LIMIT')
            raw = path.read_bytes()
            value = tomllib.loads(raw.decode('utf-8'))
            # This tool profile has no custom-agent/model/config-file parameter.
            # Do not silently ignore role settings which this adapter cannot apply.
            if (set(value) != {'name', 'description', 'developer_instructions'}
                    or value['name'] != ROLES[role]
                    or any(not isinstance(v, str) or not v.strip() for v in value.values())):
                raise AdapterError('UNSUPPORTED_ROLE_SETTINGS')
            roles[role] = {'sha256': hashlib.sha256(raw).hexdigest(),
                           'instructions': value['developer_instructions']}
        return bundle, roles

    @classmethod
    def configure(cls, dispatcher, parent_task, step_limit=1000):
        cls._scope(parent_task, step_limit)
        state = dispatcher.snapshot()
        _, roles = cls._context(state)
        fixed = {'schema': 1, 'profile': PROFILE, 'parent_task': parent_task,
                 'step_limit': step_limit, 'fork_turns': 'none',
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
                call = self._call('collaboration.spawn_agent', {
                    'task_name': self._name(state, action).rsplit('/', 1)[1],
                    'message': message, 'fork_turns': 'none'}, [action['action_id']])
                call['expected_agent_name'] = self._name(state, action)
                budgets = state['spec']['registry']['budgets']
                call['not_after'] = min(state['plan']['tasks'][action['task_id']]['started'] + budgets['task_timeout_s'],
                                        state['plan']['run_started'] + budgets['run_timeout_s'])
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
                and not calls and not jobs else None)
        return {'profile': PROFILE, 'progress': progress, 'tool_calls': calls, 'parent_jobs': jobs,
                'pending_reconciliation': pending, 'optional_wait': wait,
                'steps_used': self._config(state)['steps'], 'automatic_tool_execution': False,
                'wake_after_parent_turn': False}

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
