"""Small single-coordinator DAG admission policy; no process or model launcher."""
from copy import deepcopy
import json
import math
import os


class Plan:
    def __init__(self, tasks, registry, budgets):
        if not isinstance(registry, dict) or any(not isinstance(k, str) or not k for k in registry):
            raise ValueError('registry must map nonempty role names')
        required = {'cloud_concurrency', 'local_concurrency', 'max_dispatches',
                    'max_retries', 'task_timeout_s', 'run_timeout_s'}
        if not isinstance(budgets, dict) or not required <= budgets.keys():
            raise ValueError('missing budgets')
        for key in required:
            value = budgets[key]
            if key.endswith('_s'):
                valid = self._number(value) and value > 0
            else:
                valid = type(value) is int and value >= (0 if key == 'max_retries' else 1)
            if not valid or (key == 'max_retries' and value > 2):
                raise ValueError('invalid budget: ' + key)
        if not isinstance(tasks, list):
            raise ValueError('tasks must be a list')
        self.tasks = {}
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError('invalid task')
            for key in ['id', 'role', 'backend', 'workspace', 'instruction']:
                if not isinstance(task.get(key), str) or not task[key].strip():
                    raise ValueError('invalid task field: ' + key)
            deps = task.get('deps')
            if (not isinstance(deps, list) or any(not isinstance(x, str) for x in deps)
                    or len(set(deps)) != len(deps)):
                raise ValueError('invalid dependencies')
            if task['id'] in self.tasks or task['role'] not in registry:
                raise ValueError('duplicate id or unknown role')
            if task['backend'] not in ['cloud', 'local']:
                raise ValueError('unknown backend')
            item = deepcopy(task)
            item.update(state='QUEUED', attempts=0, retry_causes=[], actor=None,
                        started=None, ended=None, reason=None, artifacts=None)
            self.tasks[task['id']] = item
        visited, visiting = set(), set()

        def visit(task_id):
            if task_id not in self.tasks:
                raise ValueError('unknown dependency: ' + task_id)
            if task_id in visiting:
                raise ValueError('dependency cycle')
            if task_id in visited:
                return
            visiting.add(task_id)
            for dep in self.tasks[task_id]['deps']:
                visit(dep)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in self.tasks:
            visit(task_id)
        self.budgets = deepcopy(budgets)
        self.registry = deepcopy(registry)
        self.events = []
        self.run_started = None
        self.dispatches = 0
        try:
            json.dumps(self.snapshot(), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError('plan must be JSON serializable') from error

    @staticmethod
    def _number(value):
        return type(value) in (int, float) and math.isfinite(value)

    def _time(self, now, task=None):
        if not self._number(now) or now < 0:
            raise ValueError('invalid time')
        if self.run_started is not None and now < self.run_started:
            raise ValueError('time precedes run')
        if task and task['started'] is not None and now < task['started']:
            raise ValueError('time precedes task')

    def _task(self, task_id):
        if task_id not in self.tasks:
            raise ValueError('unknown task')
        return self.tasks[task_id]

    def _expired(self, task, now):
        return ((self.run_started is not None
                 and now - self.run_started >= self.budgets['run_timeout_s'])
                or (task['started'] is not None
                    and now - task['started'] >= self.budgets['task_timeout_s']))

    def _record(self, action, task, now):
        self.events.append({'action': action, 'time': now, 'task': deepcopy(task)})

    def ready(self):
        return [key for key, task in self.tasks.items()
                if task['state'] == 'QUEUED'
                and all(self.tasks[dep]['state'] == 'SUCCEEDED' for dep in task['deps'])]

    def start(self, task_id, actor, now):
        task = self._task(task_id)
        self._time(now, task)
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError('actor required')
        if task_id not in self.ready():
            raise ValueError('task is not ready')
        if self._expired(task, now):
            raise ValueError('deadline reached')
        running = [x for x in self.tasks.values() if x['state'] == 'RUNNING']
        if sum(x['backend'] == task['backend'] for x in running) >= self.budgets[task['backend'] + '_concurrency']:
            raise ValueError('backend concurrency budget reached')
        canonical = lambda value: os.path.normcase(os.path.abspath(value))
        if any(canonical(x['workspace']) == canonical(task['workspace']) for x in running):
            raise ValueError('workspace already running')
        if self.dispatches >= self.budgets['max_dispatches']:
            raise ValueError('dispatch budget reached')
        if task['attempts'] >= 1 + self.budgets['max_retries']:
            raise ValueError('attempt budget reached')
        if self.run_started is None:
            self.run_started = now
        task.update(state='RUNNING', actor=actor, started=now, ended=None,
                    reason=None, artifacts=None, attempts=task['attempts'] + 1)
        self.dispatches += 1
        self._record('start', task, now)

    def finish(self, task_id, success, now, reason, artifacts=None):
        task = self._task(task_id)
        self._time(now, task)
        if task['state'] != 'RUNNING' or type(success) is not bool:
            raise ValueError('finish requires a running task and boolean result')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('reason required')
        try:
            artifacts = json.loads(json.dumps(artifacts, allow_nan=False))
        except (TypeError, ValueError) as error:
            raise ValueError('artifacts must be JSON serializable') from error
        state = 'TIMED_OUT' if self._expired(task, now) else ('SUCCEEDED' if success else 'FAILED')
        task.update(state=state, ended=now, reason=reason, artifacts=artifacts)
        self._record('finish', task, now)

    def retry(self, task_id, cause, now):
        task = self._task(task_id)
        self._time(now, task)
        if task['state'] != 'FAILED':
            raise ValueError('only known failures can retry')
        if not isinstance(cause, str) or not cause.strip() or cause in task['retry_causes']:
            raise ValueError('new retry cause required')
        if task['attempts'] >= 1 + self.budgets['max_retries']:
            raise ValueError('retry budget reached')
        if self.run_started is not None and now - self.run_started >= self.budgets['run_timeout_s']:
            raise ValueError('run deadline reached')
        task['retry_causes'].append(cause)
        task.update(state='QUEUED', started=None, ended=None, reason=cause, artifacts=None)
        self._record('retry', task, now)

    def escalate(self, task_id, reason, now):
        task = self._task(task_id)
        self._time(now, task)
        if task['state'] not in ['QUEUED', 'FAILED']:
            raise ValueError('only queued or failed tasks can escalate')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('reason required')
        task.update(state='WAITING_FOR_HUMAN', reason=reason, ended=now)
        self._record('escalate', task, now)

    def expire(self, now):
        self._time(now)
        expired = []
        for task_id, task in self.tasks.items():
            if task['state'] == 'RUNNING':
                self._time(now, task)
        for task_id, task in self.tasks.items():
            if task['state'] == 'RUNNING' and self._expired(task, now):
                task.update(state='TIMED_OUT', ended=now, reason='deadline reached')
                self._record('expire', task, now)
                expired.append(task_id)
        return expired

    def snapshot(self):
        return deepcopy({'tasks': self.tasks, 'registry': self.registry,
                         'budgets': self.budgets, 'events': self.events,
                         'run_started': self.run_started, 'dispatches': self.dispatches,
                         'credit_cost': 'unmeasured'})
