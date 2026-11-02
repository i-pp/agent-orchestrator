"""Opt-in, bounded app-server gateway. No listener, automatic retry or Chat wake.

The existing executor remains Codex app-server. This ledger only binds client
request keys to its thread/turn IDs and persists an explicit FIFO and cursor.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from chat_mailbox import _private, _json
from codex_app_protocol import ProtocolUnknown, RpcError

PREFIX = 'codex_'
ID = r'^[0-9a-f-]{36}$'
KEY = r'^[A-Za-z0-9_.:-]{1,128}$'
TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED'}
HELD = {'DISPATCHING', 'RUNNING', 'WAITING_APPROVAL', 'STOP_REQUESTED', 'UNKNOWN'}
MAX_EVENTS = 4096
MAX_OPS = 512
LEDGER_BYTES = 2 * 1024**2
COMPLETION_RESERVE = 64 * 1024


class PipelineError(ValueError):
    def __init__(self, code, status='BLOCKED'):
        self.code, self.status = code, status
        super().__init__(code)

    def result(self):
        return {'status': self.status, 'code': self.code, 'automatic_retry': False}


def packed(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def opaque(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value)


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise PipelineError('INVALID_TEXT')
    try:
        if len(value.encode()) > 4096: raise PipelineError('TEXT_TOO_LARGE')
    except UnicodeError:
        raise PipelineError('INVALID_TEXT') from None


def validate_policy(policy):
    keys = {'schema', 'workspaces', 'models', 'max_threads', 'max_tasks', 'concurrency', 'timeout_seconds'}
    if not isinstance(policy, dict) or set(policy) != keys or type(policy['schema']) is not int or policy['schema'] not in (1, 2):
        raise PipelineError('INVALID_PIPELINE_POLICY')
    for key, cap in [('max_threads', 8), ('max_tasks', 32), ('concurrency', 2), ('timeout_seconds', 300)]:
        if type(policy[key]) is not int or not 1 <= policy[key] <= cap:
            raise PipelineError('INVALID_PIPELINE_BOUND')
    if not isinstance(policy['models'], list) or not 1 <= len(policy['models']) <= 8:
        raise PipelineError('INVALID_MODEL_ALLOWLIST')
    if not all(opaque(m) for m in policy['models']): raise PipelineError('INVALID_MODEL_ALLOWLIST')
    if len(set(policy['models'])) != len(policy['models']): raise PipelineError('INVALID_MODEL_ALLOWLIST')
    if not isinstance(policy['workspaces'], dict) or not 1 <= len(policy['workspaces']) <= 8:
        raise PipelineError('INVALID_WORKSPACES')
    for key, value in policy['workspaces'].items():
        if not isinstance(key, str) or not re.fullmatch(KEY, key) or not isinstance(value, str):
            raise PipelineError('INVALID_WORKSPACE')
        path = Path(value)
        if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
            raise PipelineError('UNSAFE_WORKSPACE')
        if str(path.resolve()) != value or not path.is_dir() or path.stat().st_uid != os.getuid():
            raise PipelineError('UNSAFE_WORKSPACE')
    return json.loads(packed(policy))


def tool_definitions():
    ident = {'type': 'string', 'pattern': ID, 'maxLength': 36}
    key = {'type': 'string', 'pattern': KEY, 'maxLength': 128}
    body = {'type': 'string', 'minLength': 1, 'maxLength': 4096}
    model = {'type': 'string', 'minLength': 1, 'maxLength': 160}
    definitions = [
        ('capabilities', {}, [], True),
        ('thread_start', {'workspace': key, 'model': model, 'request_key': key}, ['workspace', 'model', 'request_key'], False),
        ('task_submit', {'thread_id': ident, 'prompt': body, 'model': model, 'request_key': key}, ['thread_id', 'prompt', 'request_key'], False),
        ('chat_store', {'thread_id': ident, 'prompt': body, 'model': model, 'request_key': key}, ['thread_id', 'prompt', 'request_key'], False),
        ('chat_queue', {'task_id': ident, 'request_key': key}, ['task_id', 'request_key'], False),
        ('chat_steer', {'thread_id': ident, 'prompt': body, 'request_key': key}, ['thread_id', 'prompt', 'request_key'], False),
        ('threads_list', {'limit': {'type':'integer','minimum':1,'maximum':20},
                          'cursor': {'type':'string','maxLength':80}}, [], True),
        ('task_get', {'task_id': ident}, ['task_id'], True),
        ('events', {'after': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50}}, [], True),
        ('task_cancel', {'task_id': ident, 'request_key': key}, ['task_id', 'request_key'], False),
        ('thread_archive', {'thread_id': ident, 'request_key': key}, ['thread_id', 'request_key'], False),
        ('pipeline_tick', {}, [], False),
    ]
    descriptions = {
        'capabilities': 'List operator-approved workspaces, bounds and available models. No inference. Availability is not billing proof.',
        'thread_start': 'Create a scoped Codex conversation with a validated model. Does not start a turn.',
        'task_submit': 'Persist input in FIFO, then dispatch if idle. Busy input is queued, never silently steered.',
        'chat_store': 'Store immutable input without executing or queueing it.',
        'chat_queue': 'Queue previously stored input. Explicit tick dispatches when idle.',
        'chat_steer': 'Add text to the exact active turn using expectedTurnId. Cannot change its model.',
        'threads_list': 'Read owned thread/task metadata; never import arbitrary server threads.',
        'task_get': 'Read one task and reconcile its known thread. Never retry generation.',
        'events': 'Replay durable events after a cursor; no push notification or Chat wake.',
        'task_cancel': 'Cancel pending input or request interrupt. Request acceptance is not confirmed stop.',
        'thread_archive': 'Archive an idle conversation without deleting its ledger or history.',
        'pipeline_tick': 'One bounded reconciliation pass and at most one FIFO dispatch. No resident scheduler.',
    }
    return [{'name': PREFIX + name, 'description': descriptions[name],
             'inputSchema': {'type': 'object', 'properties': props, 'required': required, 'additionalProperties': False},
             'outputSchema': {'type': 'object'},
             'annotations': {'readOnlyHint': read, 'destructiveHint': False, 'idempotentHint': read, 'openWorldHint': False}}
            for name, props, required, read in definitions]


TOOLS = tuple(t['name'] for t in tool_definitions())


def validate_arguments(name, args):
    definition = next((t for t in tool_definitions() if t['name'] == name), None)
    if definition is None or not isinstance(args, dict): raise PipelineError('INVALID_OPERATION')
    schema = definition['inputSchema']
    if set(args) - set(schema['properties']) or set(schema['required']) - set(args):
        raise PipelineError('INVALID_FIELDS')
    for key, value in args.items():
        if key == 'prompt': text(value)
        elif key in ('thread_id', 'task_id'):
            try:
                if not isinstance(value, str) or str(uuid.UUID(value)) != value: raise ValueError()
            except (ValueError, AttributeError): raise PipelineError('INVALID_ID') from None
        elif key in ('workspace', 'request_key'):
            if not isinstance(value, str) or not re.fullmatch(KEY, value): raise PipelineError('INVALID_KEY')
        elif key == 'model':
            if not opaque(value): raise PipelineError('INVALID_MODEL')
        elif key == 'cursor':
            if not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{64}:[0-9]{1,6}',value): raise PipelineError('INVALID_CURSOR')
        elif type(value) is not int or value < (1 if key == 'limit' else 0) or (key == 'limit' and value > 50):
            raise PipelineError('INVALID_PAGE')
        elif name == 'codex_threads_list' and value > 20: raise PipelineError('INVALID_PAGE')


class Pipeline:
    def __init__(self, runtime, policy, client, *, instructions, policy_sha256, clock=time.time, admission=None):
        self.root = Path(runtime).absolute()
        if any(p.is_symlink() for p in [self.root, *self.root.parents]): raise PipelineError('UNSAFE_RUNTIME')
        info = self.root.stat()
        if not self.root.is_dir() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PipelineError('UNSAFE_RUNTIME')
        self.policy = validate_policy(policy)
        if not isinstance(instructions, str) or not instructions.strip() or not re.fullmatch('[a-f0-9]{64}', policy_sha256):
            raise PipelineError('POLICY_NOT_LOADED')
        self.instructions, self.common_hash = instructions, policy_sha256
        self.policy_hash = digest({'policy': self.policy, 'common': policy_sha256})
        self.client, self.clock = client, clock
        self.admission = admission
        self.loaded = set()
        self.db = self.root / 'codex-pipeline.sqlite3'
        from pipeline_policy_migration import assert_migration_ready
        assert_migration_ready(self.db)
        if not self.db.exists():
            fd = os.open(self.db, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        _private(self.db, 8 * 1024**2)
        with self._connection() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
            connection.execute('INSERT OR IGNORE INTO ledger VALUES (1, ?)', (packed({'policy_hash': self.policy_hash, 'threads': {}, 'tasks': {}, 'ops': {}, 'events': []}),))
        # ISSUED survives crashes. Never send it a second time.
        def recover(s):
            if s['policy_hash'] != self.policy_hash: raise PipelineError('PIPELINE_POLICY_CHANGED')
            for op in s['ops'].values():
                if op['status'] == 'ISSUED': op['status'] = 'UNKNOWN'
            for task in s['tasks'].values():
                if task['state'] == 'DISPATCHING': task['state'] = 'UNKNOWN'
        self._mutate(recover)
        self.client.event_sink = self.receive

    @contextmanager
    def _connection(self):
        _private(self.db, 8 * 1024**2)
        conn = sqlite3.connect(self.db, timeout=2, isolation_level=None)
        conn.execute('PRAGMA journal_mode=DELETE')
        conn.execute('PRAGMA synchronous=FULL')
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally: conn.close()

    def _mutate(self, fn):
        with self._connection() as conn:
            state = json.loads(conn.execute('SELECT body FROM ledger WHERE id=1').fetchone()[0])
            result = fn(state)
            raw = packed(state)
            if len(raw.encode()) > LEDGER_BYTES: raise PipelineError('LEDGER_CAPACITY')
            conn.execute('UPDATE ledger SET body=? WHERE id=1', (raw,))
            return result

    def _event(self, s, kind, thread=None, task=None, **facts):
        if self.policy['schema'] == 1 and len(s['events']) >= MAX_EVENTS: raise PipelineError('EVENT_CAPACITY')
        event = {'seq': len(s['events']) + 1, 'kind': kind, 'time': self.clock(), **facts}
        if thread: event['thread_id'] = thread
        if task: event['task_id'] = task
        s['events'].append(event)

    def _thread(self, s, ident, *, idle=False):
        t = s['threads'].get(ident)
        if not t: raise PipelineError('THREAD_NOT_OWNED')
        if t['archived'] or t.get('closing') or not t.get('remote_id'): raise PipelineError('THREAD_NOT_READY')
        if t.get('native_context_state', 'READY') != 'READY':
            raise PipelineError('NATIVE_CONTEXT_RECONCILIATION_REQUIRED', 'UNKNOWN')
        if idle and t['active_task']: raise PipelineError('THREAD_BUSY')
        return t

    def _task(self, s, ident):
        if ident not in s['tasks']: raise PipelineError('TASK_NOT_OWNED')
        return s['tasks'][ident]

    def _operation(self, key, kind, args, prepare, effect, finish):
        fingerprint = digest([kind, args])
        def reserve(s):
            prior = s['ops'].get(key)
            if prior:
                if prior['fingerprint'] != fingerprint: raise PipelineError('REQUEST_KEY_CONFLICT')
                return False, prior
            if self.policy['schema'] == 1 and len(s['ops']) >= MAX_OPS: raise PipelineError('OPERATION_CAPACITY')
            if self.policy['schema'] == 2 and len(packed(s).encode()) > LEDGER_BYTES-COMPLETION_RESERVE:
                emergency_stop = (kind == 'cancel' and args in s['tasks']
                    and s['tasks'][args]['state'] in HELD and not s['tasks'][args]['stop_sent']) if isinstance(args, str) else False
                if not emergency_stop: raise PipelineError('HISTORY_STORAGE_CAPACITY')
            context = prepare(s)
            s['ops'][key] = {'fingerprint': fingerprint, 'kind': kind, 'status': 'ISSUED', 'context': context}
            self._event(s, 'operation_issued', **{'operation': kind, 'request_key_hash': digest(key)})
            return True, s['ops'][key]
        fresh, op = self._mutate(reserve)
        if not fresh:
            return {**op.get('result', {'status': 'UNKNOWN' if op['status'] == 'ISSUED' else op['status'],
                    **{k: v for k, v in op['context'].items() if k in ('thread_id', 'task_id')}}),
                    'duplicate': True, 'automatic_retry': False}
        try:
            result = effect(op['context'])
        except Exception as error:
            status = 'FAILED' if isinstance(error, RpcError) else 'UNKNOWN'
            def failed(s):
                stored = s['ops'][key]
                stored['status'] = status
                stored['result'] = {'status': status, 'automatic_retry': False}
                context = stored['context']
                if 'task_id' in context:
                    task = s['tasks'][context['task_id']]
                    # A refused interrupt/steer does not imply the turn stopped.
                    if kind == 'dispatch':
                        task['state'] = status
                        if status == 'FAILED': s['threads'][task['thread_id']]['active_task'] = None
                if kind == 'archive' and status == 'FAILED': s['threads'][context['thread_id']]['closing'] = False
                self._event(s, 'operation_' + status.lower(), operation=kind)
                return stored['result']
            return self._mutate(failed)
        def accept(s):
            output = finish(s, op['context'], result)
            s['ops'][key].update(status='SUCCEEDED', result=output)
            return output
        try:
            return self._mutate(accept)
        except Exception:
            # RPC may already have succeeded. A failed local commit is not a
            # rejected execution and must never invite a new delivery.
            return {'status': 'UNKNOWN', 'automatic_retry': False,
                    **{k: v for k, v in op['context'].items() if k in ('thread_id', 'task_id')}}

    def _catalog(self):
        account = self.client.request('account/read', {'refreshToken': False})
        if (account.get('account') or {}).get('type') != 'chatgpt': raise PipelineError('SUBSCRIPTION_AUTH_REQUIRED')
        data, cursor = [], None
        for _ in range(4):
            page = self.client.request('model/list', {'includeHidden': False, 'limit': 100, **({'cursor': cursor} if cursor else {})})
            data.extend(page['data'])
            cursor = page.get('nextCursor')
            if not cursor: break
        if cursor: raise PipelineError('MODEL_CATALOG_BOUND')
        return {m['model'] for m in data if not m.get('hidden', False)}

    def _model(self, model):
        if model not in self.policy['models'] or model not in self._catalog(): raise PipelineError('MODEL_NOT_AVAILABLE_OR_ALLOWED')

    def capabilities(self):
        available = self._catalog()
        result = {'models': [{'model': m, 'available': m in available} for m in self.policy['models']],
                'workspaces': sorted(self.policy['workspaces']),
                'bounds': {k: self.policy[k] for k in ('max_threads', 'max_tasks', 'concurrency', 'timeout_seconds')},
                'policy_sha256': self.common_hash, 'approval': 'notify_only', 'chat_wake': False,
                'resident_scheduler': False, 'billing_verified': False}
        if self.policy['schema'] == 2: result['capacity'] = self._mutate(self.capacity)
        return result

    def capacity(self, s):
        held = [t for t in s['tasks'].values() if t['state'] in HELD]
        threads = {t['thread_id'] for t in held}
        return {'mode': 'cumulative_trial' if self.policy['schema'] == 1 else 'concurrent_execution',
            'active_threads': len(threads), 'held_tasks': len(held),
            'available_execution_slots': max(0,min(self.policy['concurrency'],self.policy['max_threads'],self.policy['max_tasks'])-len(held)),
            'execution_slots': min(self.policy['concurrency'], self.policy['max_threads'], self.policy['max_tasks']) if self.policy['schema'] == 2 else self.policy['concurrency'],
            'stored_threads': len(s['threads']), 'archived_threads': sum(bool(t.get('archived')) for t in s['threads'].values()),
            'stored_tasks': len(s['tasks']), 'pending_inputs': sum(t['state'] in ('STORED','QUEUED','BLOCKED') for t in s['tasks'].values()),
            'terminal_tasks': sum(t['state'] in TERMINAL for t in s['tasks'].values()),
            'history_bytes': len(packed(s).encode()), 'history_byte_limit': LEDGER_BYTES,
            'completion_reserve_bytes': COMPLETION_RESERVE if self.policy['schema'] == 2 else 0}

    def room(self, s, thread_id):
        held = [t for t in s['tasks'].values() if t['state'] in HELD]
        if len(held) >= self.policy['concurrency']: return False
        if self.policy['schema'] == 2:
            threads = {t['thread_id'] for t in held}
            if len(held) >= self.policy['max_tasks']: return False
            if thread_id not in threads and len(threads) >= self.policy['max_threads']: return False
        return True

    def listing(self, s, args):
        def thread_view(t):
            return {k:v for k,v in t.items() if k not in ('remote_id','original_remote_id','native_replacements')}
        def task_view(t):return {k:v for k,v in self._view(t).items() if k!='answer'}
        if self.policy['schema']==1 and not args:
            return {'threads':[thread_view(t) for t in s['threads'].values()], 'tasks':[task_view(t) for t in s['tasks'].values()]}
        records=[]
        for t in s['tasks'].values():
            priority=0 if t['state'] in HELD else 2 if t['state'] not in TERMINAL else 4
            records.append((priority,'task',t['id'],task_view(t)))
        for t in s['threads'].values():
            priority=1 if t.get('active_task') else 5 if t['archived'] else 3
            records.append((priority,'thread',t['id'],thread_view(t)))
        records.sort(key=lambda r:r[:3])
        fingerprint=digest(records);offset=0
        if args.get('cursor'):
            snapshot,raw_offset=args['cursor'].split(':');offset=int(raw_offset)
            if snapshot!=fingerprint:raise PipelineError('LIST_SNAPSHOT_CHANGED')
            if offset>len(records):raise PipelineError('INVALID_CURSOR')
        end=min(len(records),offset+args.get('limit',20));page=records[offset:end]
        return {'threads':[r[3] for r in page if r[1]=='thread'], 'tasks':[r[3] for r in page if r[1]=='task'],
            'capacity':self.capacity(s),'total_records':len(records),
            'next_cursor':fingerprint+':'+str(end) if end<len(records) else None}

    def start(self, workspace, model, request_key):
        if workspace not in self.policy['workspaces']: raise PipelineError('WORKSPACE_NOT_ALLOWED')
        self._model(model)
        def prepare(s):
            if self.policy['schema'] == 1 and len(s['threads']) >= self.policy['max_threads']: raise PipelineError('THREAD_CAPACITY')
            ident = str(uuid.uuid4())
            s['threads'][ident] = {'id': ident, 'remote_id': None, 'workspace': workspace, 'model': model,
                                   'active_task': None, 'archived': False, 'closing': False}
            return {'thread_id': ident}
        def effect(_):
            result = self.client.request('thread/start', {'model': model, 'cwd': self.policy['workspaces'][workspace],
                'developerInstructions': self.instructions, 'approvalPolicy': 'on-request', 'approvalsReviewer': 'user',
                'sandbox': 'read-only', 'ephemeral': False})
            if (result.get('cwd') != self.policy['workspaces'][workspace] or result.get('model') != model
                    or result.get('modelProvider') != 'openai'
                    or result.get('approvalPolicy') != 'on-request' or result.get('approvalsReviewer') != 'user'
                    or result.get('sandbox', {}).get('type') != 'readOnly' or not opaque(result.get('thread', {}).get('id'))):
                raise PipelineError('EFFECTIVE_SCOPE_MISMATCH', 'UNKNOWN')
            return result
        def finish(s, c, result):
            t = s['threads'][c['thread_id']]
            t['remote_id'] = result['thread']['id']
            t.update(native_creation_count=1, native_context_state='READY', history_verified=False)
            if any(other['id'] != t['id'] and other['remote_id'] == t['remote_id'] for other in s['threads'].values()):
                raise PipelineError('REMOTE_THREAD_COLLISION', 'UNKNOWN')
            self.loaded.add(t['id'])
            self._event(s, 'thread_started', t['id'])
            return {'status': 'READY', 'thread_id': t['id'], 'model': model}
        return self._operation(request_key, 'thread_start', [workspace, model], prepare, effect, finish)

    def replace_empty_context(self, thread_id, request_key, evidence, *, admission=None):
        """Explicit operator recovery only. Never called from normal dispatch."""
        if not isinstance(request_key,str) or not re.fullmatch(KEY,request_key):
            raise PipelineError('INVALID_RECOVERY_KEY')
        keys={'schema','thread_id','ledger_sha256','remote_id_sha256','diagnostic_sha256','diagnostic'}
        if (not isinstance(evidence,dict) or set(evidence)!=keys or type(evidence['schema']) is not int
                or evidence['schema']!=1 or evidence['thread_id']!=thread_id
                or len(packed(evidence).encode())>16384):raise PipelineError('INVALID_EMPTY_CONTEXT_EVIDENCE')
        for field in ['ledger_sha256','remote_id_sha256','diagnostic_sha256']:
            if not isinstance(evidence[field],str) or not re.fullmatch('[a-f0-9]{64}',evidence[field]):
                raise PipelineError('INVALID_EMPTY_CONTEXT_EVIDENCE')
        proof=evidence['diagnostic']
        if (not isinstance(proof,dict) or digest(proof)!=evidence['diagnostic_sha256']
                or proof.get('thread_id')!=thread_id or proof.get('inference_calls')!=0
                or proof.get('new_threads')!=0 or proof.get('cleanup_status')!='RELEASED'
                or proof.get('remote_turn_count',0)!=0 or proof.get('history_unknown') is True
                or not any(c.get('method')=='thread/resume' and c.get('exception_class')=='RpcError'
                    and c.get('rpc_error_code')==-32600 and c.get('reason_categories')==['NO_ROLLOUT']
                    for c in proof.get('checks',[]) if isinstance(c,dict))):
            raise PipelineError('EMPTY_CONTEXT_NOT_PROVEN')
        proof_hash=digest(evidence)
        def prepare(s):
            t=s['threads'].get(thread_id)
            if digest(s)!=evidence['ledger_sha256'] or not t or digest(t.get('remote_id'))!=evidence['remote_id_sha256']:
                raise PipelineError('EMPTY_CONTEXT_EVIDENCE_STALE')
            if (t.get('active_task') or t.get('archived') or t.get('closing') or not t.get('remote_id')
                    or t.get('history_verified') or t.get('native_creation_count',1)!=1
                    or t.get('replacement_attempts',0)!=0 or t.get('native_replacements')):
                raise PipelineError('EMPTY_CONTEXT_RECOVERY_FORBIDDEN')
            starts=[op for op in s['ops'].values() if op['kind']=='thread_start'
                    and op.get('context',{}).get('thread_id')==thread_id and op['status']=='SUCCEEDED']
            if len(starts)!=1 or any(op['kind'] not in ['thread_start','input'] or op['status']!='SUCCEEDED'
                                     for op in s['ops'].values()):
                raise PipelineError('PRIOR_OR_UNKNOWN_EXECUTION_FORBIDS_REPLACEMENT')
            related=[task for task in s['tasks'].values() if task['thread_id']==thread_id]
            if not related or any(task['state'] not in ['STORED','QUEUED'] or task.get('remote_turn')
                    or task.get('deadline') is not None or task.get('pending_turn')
                    or task.get('pending_approval_turn') or task.get('answer') is not None for task in related):
                raise PipelineError('PRIOR_OR_UNKNOWN_EXECUTION_FORBIDS_REPLACEMENT')
            if any(e['kind'] not in ['operation_issued','thread_started','input_stored','input_queued'] for e in s['events']):
                raise PipelineError('UNKNOWN_HISTORY_FORBIDS_REPLACEMENT')
            t['replacement_attempts']=1
            t['original_remote_id']=t.get('original_remote_id',t['remote_id'])
            t['native_context_state']='REPLACEMENT_PENDING'
            return {'thread_id':thread_id,'previous_remote_id':t['remote_id'],'model':t['model'],
                    'workspace':t['workspace'],'proof_sha256':proof_hash}
        def effect(c):
            if admission is not None:admission()
            self._model(c['model'])
            result=self.client.request('thread/start',{'model':c['model'],'cwd':self.policy['workspaces'][c['workspace']],
                'developerInstructions':self.instructions,'approvalPolicy':'on-request','approvalsReviewer':'user',
                'sandbox':'read-only','ephemeral':False})
            if (not opaque(result.get('thread',{}).get('id')) or result['thread']['id']==c['previous_remote_id']
                    or result.get('model')!=c['model'] or result.get('cwd')!=self.policy['workspaces'][c['workspace']]
                    or result.get('modelProvider')!='openai' or result.get('approvalPolicy')!='on-request'
                    or result.get('approvalsReviewer')!='user' or result.get('sandbox',{}).get('type')!='readOnly'):
                raise PipelineError('REPLACEMENT_SCOPE_MISMATCH','UNKNOWN')
            return result
        def finish(s,c,result):
            t=s['threads'][thread_id];remote=result['thread']['id']
            if any(other['id']!=thread_id and other.get('remote_id')==remote for other in s['threads'].values()):
                raise PipelineError('REMOTE_THREAD_COLLISION','UNKNOWN')
            t['native_replacements']=[{'previous_remote_id':c['previous_remote_id'],'remote_id':remote,
                'reason':'EMPTY_NO_ROLLOUT','proof_sha256':proof_hash,'request_key_hash':digest(request_key)}]
            t.update(remote_id=remote,native_creation_count=2,native_context_state='READY',history_verified=False)
            self.loaded.add(thread_id)
            self._event(s,'native_context_replaced',thread_id,native_creation_count=2,reason_code='EMPTY_NO_ROLLOUT')
            return {'status':'READY','thread_id':thread_id,'native_creation_count':2}
        try:
            output=self._operation(request_key,'native_context_replace',[thread_id,proof_hash],prepare,effect,finish)
            if output.get('status') not in ['READY']:
                def uncertain(s):
                    t=s['threads'].get(thread_id)
                    if t and t.get('native_context_state')=='REPLACEMENT_PENDING':
                        t['native_context_state']='REPLACEMENT_UNKNOWN'
                self._mutate(uncertain)
            return output
        except Exception:
            # A committed intent/unknown ACK must never become a generic retry.
            def preserve(s):
                t=s['threads'].get(thread_id)
                if t and t.get('native_context_state')=='REPLACEMENT_PENDING':t['native_context_state']='REPLACEMENT_UNKNOWN'
            self._mutate(preserve)
            raise

    def store(self, thread_id, prompt, request_key, model=None, *, queue=False):
        text(prompt)
        def prepare(s):
            t = self._thread(s, thread_id)
            chosen = model or t['model']
            if chosen not in self.policy['models']: raise PipelineError('MODEL_NOT_ALLOWED')
            if self.policy['schema'] == 1 and len(s['tasks']) >= self.policy['max_tasks']: raise PipelineError('TASK_CAPACITY')
            ident = str(uuid.uuid4())
            s['tasks'][ident] = {'id': ident, 'thread_id': thread_id, 'model': chosen, 'prompt': prompt,
                'prompt_sha256': digest(prompt), 'state': 'QUEUED' if queue else 'STORED', 'remote_turn': None,
                'deadline': None, 'answer': None, 'answer_truncated': False, 'stop_sent': False,
                'queue_seq': len(s['events']) if queue else None, 'pending_turn': None, 'pending_approval_turn': None}
            self._event(s, 'input_queued' if queue else 'input_stored', thread_id, ident)
            return {'task_id': ident}
        return self._operation(request_key, 'input', [thread_id, prompt, model, queue], prepare, lambda c: c,
            lambda s, c, _: self._view(self._task(s, c['task_id'])))

    @staticmethod
    def _view(t):
        return {k: v for k, v in t.items() if k not in ('prompt', 'remote_turn', 'stop_sent', 'pending_turn', 'pending_approval_turn')}

    def queue(self, task_id, request_key):
        def prepare(s):
            task = self._task(s, task_id)
            self._thread(s, task['thread_id'])
            if task['state'] != 'STORED': raise PipelineError('INPUT_NOT_STORED')
            task['state'] = 'QUEUED'
            task['queue_seq'] = len(s['events'])
            self._event(s, 'input_queued', task['thread_id'], task_id)
            return {'task_id': task_id}
        return self._operation(request_key, 'queue', task_id, prepare, lambda c: c,
                               lambda s, c, _: self._view(self._task(s, c['task_id'])))

    def _blocked_before_turn(self, ident, reason):
        def block(s):
            current = self._task(s, ident)
            if current['state'] == 'QUEUED':
                current.update(state='BLOCKED', reason_code=reason, execution_started=False)
                self._event(s, 'task_blocked', current['thread_id'], ident, reason_code=reason)
            return self._view(current)
        return self._mutate(block)

    def _dispatch(self):
        def candidate(s):
            if sum(t['state'] in HELD for t in s['tasks'].values()) >= self.policy['concurrency']: return None
            pending = sorted((t for t in s['tasks'].values() if t['state'] == 'QUEUED'), key=lambda t: t['queue_seq'])
            return next((t['id'] for t in pending if not s['threads'][t['thread_id']]['active_task']
                         and not s['threads'][t['thread_id']].get('closing')
                         and not s['threads'][t['thread_id']]['archived'] and self.room(s, t['thread_id'])), None)
        ident = self._mutate(candidate)
        if not ident: return {'status': 'IDLE_OR_HELD'}
        if self.admission is not None and not self.admission(): return {'status': 'SHARED_BUDGET_HELD'}
        task = self._mutate(lambda s: dict(self._task(s, ident)))
        try:self._mutate(lambda s:self._thread(s,task['thread_id'],idle=True))
        except PipelineError as error:return {'status':'BLOCKED','code':error.code,'task_id':ident}
        try:
            self._model(task['model'])
        except RpcError:
            if self.policy['schema'] == 1: raise
            return self._blocked_before_turn(ident, 'CATALOG_REJECTED')
        except PipelineError as error:
            if error.code not in ('MODEL_NOT_AVAILABLE_OR_ALLOWED', 'SUBSCRIPTION_AUTH_REQUIRED'): raise
            def blocked(s):
                current = self._task(s, ident)
                if current['state'] == 'QUEUED':
                    current.update(state='BLOCKED', reason_code=error.code, execution_started=False)
                    self._event(s, 'task_blocked', current['thread_id'], ident, reason_code=error.code)
                return self._view(current)
            return self._mutate(blocked)
        # app-server restart does not imply a loaded thread. Resume only the
        # exact owned thread, with the same fixed policy; never import by path.
        if task['thread_id'] not in self.loaded:
            thread = self._mutate(lambda s: dict(self._thread(s, task['thread_id'], idle=True)))
            try:
                result = self.client.request('thread/resume', {'threadId': thread['remote_id'], 'excludeTurns': True,
                    'model': task['model'], 'cwd': self.policy['workspaces'][thread['workspace']],
                    'developerInstructions': self.instructions, 'approvalPolicy': 'on-request',
                    'approvalsReviewer': 'user', 'sandbox': 'read-only'})
            except RpcError:
                if self.policy['schema'] == 1: raise
                return self._blocked_before_turn(ident, 'RESUME_REJECTED')
            if (result.get('thread', {}).get('id') != thread['remote_id'] or result.get('model') != task['model']
                    or result.get('modelProvider') != 'openai'
                    or result.get('cwd') != self.policy['workspaces'][thread['workspace']]
                    or result.get('approvalPolicy') != 'on-request' or result.get('approvalsReviewer') != 'user'
                    or result.get('sandbox', {}).get('type') != 'readOnly'):
                # A validated reply with the wrong scope proves no turn was
                # dispatched. Persist a blocker instead of auto-resuming on
                # every daemon tick. Never import or execute the other scope.
                def block_resume(s):
                    current = self._task(s, ident)
                    current.update(state='BLOCKED', reason_code='RESUME_SCOPE_MISMATCH', execution_started=False)
                    self._event(s, 'task_blocked', current['thread_id'], ident,
                                reason_code='RESUME_SCOPE_MISMATCH')
                    return self._view(current)
                return self._mutate(block_resume)
            self.loaded.add(thread['id'])
        def prepare(s):
            task = self._task(s, ident)
            thread = self._thread(s, task['thread_id'], idle=True)
            if task['state'] != 'QUEUED': raise PipelineError('NOT_QUEUED')
            if not self.room(s, task['thread_id']): raise PipelineError('CONCURRENCY_HELD')
            task['state'], task['deadline'] = 'DISPATCHING', self.clock() + self.policy['timeout_seconds']
            thread['active_task'] = ident
            return {'task_id': ident, 'remote_thread': thread['remote_id']}
        def finish(s, c, result):
            task = self._task(s, ident)
            turn = result['turn']
            if not opaque(turn.get('id')): raise PipelineError('INVALID_TURN', 'UNKNOWN')
            task['remote_turn'] = turn['id']
            if task['state'] in ('DISPATCHING', 'UNKNOWN'): task['state'] = 'RUNNING'
            pending = task.pop('pending_turn', None)
            if pending and pending.get('id') == turn['id']: self._adopt_turn(s, task, pending)
            self._adopt_turn(s, task, turn)
            if task.pop('pending_approval_turn', None) == turn['id'] and task['state'] not in TERMINAL:
                task['state'] = 'WAITING_APPROVAL'
                self._event(s, 'approval_waiting', task['thread_id'], ident, automatic_approval=False)
            self._event(s, 'turn_bound', task['thread_id'], ident)
            return self._view(task)
        return self._operation('dispatch:' + ident, 'dispatch', ident, prepare,
            lambda c: self.client.request('turn/start', {'threadId': c['remote_thread'], 'clientUserMessageId': ident,
                'model': task['model'], 'input': [{'type': 'text', 'text': task['prompt'], 'text_elements': []}],
                'cwd': self.policy['workspaces'][self._mutate(lambda s: self._thread(s, task['thread_id'])['workspace'])],
                'approvalPolicy': 'on-request', 'approvalsReviewer': 'user', 'sandboxPolicy': {'type': 'readOnly'}}), finish)

    def _adopt_turn(self, s, task, turn):
        mapping = {'completed': 'COMPLETED', 'failed': 'FAILED', 'interrupted': 'CANCELLED'}
        state = mapping.get(turn.get('status'))
        if not state: return
        if task['state'] in TERMINAL: return
        answer = '\n'.join(i['text'] for i in turn.get('items', []) if isinstance(i, dict) and i.get('type') == 'agentMessage' and isinstance(i.get('text'), str))
        encoded = answer.encode()
        task.update(state=state, answer=encoded[:4096].decode('utf-8', errors='ignore'),
                    answer_truncated=len(encoded) > 4096 or turn.get('answer_truncated') is True)
        s['threads'][task['thread_id']]['active_task'] = None
        self._event(s, 'task_' + state.lower(), task['thread_id'], task['id'])

    def refresh(self, task_id):
        task, thread = self._mutate(lambda s: (dict(self._task(s, task_id)), dict(s['threads'][self._task(s, task_id)['thread_id']])) )
        if task['state'] not in HELD: return self._view(task)
        if not thread.get('remote_id'): return {**self._view(task), 'reconciliation': 'UNKNOWN'}
        try:
            result = self.client.request('thread/read', {'threadId': thread['remote_id'], 'includeTurns': True})
            remote = result['thread']
            if remote.get('id') != thread['remote_id']: raise PipelineError('THREAD_MISMATCH')
            def reconcile(s):
                task = self._task(s, task_id)
                turns = remote.get('turns', [])
                turn = next((t for t in turns if t.get('id') == task['remote_turn']), None)
                if turn: self._adopt_turn(s, task, turn)
                # A lost turn/start acknowledgement cannot be inferred from list order.
                if task['remote_turn'] is None: task['state'] = 'UNKNOWN'
                flags = remote.get('status', {}).get('activeFlags', [])
                if 'waitingOnApproval' in flags and task['state'] not in TERMINAL and task['state'] != 'WAITING_APPROVAL':
                    task['state'] = 'WAITING_APPROVAL'
                    self._event(s, 'approval_waiting', task['thread_id'], task_id, automatic_approval=False)
                return self._view(task)
            return self._mutate(reconcile)
        except (ProtocolUnknown, RpcError):
            return {**self._view(task), 'reconciliation': 'UNKNOWN', 'automatic_retry': False}

    def steer(self, thread_id, prompt, request_key):
        def prepare(s):
            thread = self._thread(s, thread_id)
            task = self._task(s, thread['active_task'])
            if task['state'] != 'RUNNING' or not task['remote_turn']: raise PipelineError('TURN_NOT_STEERABLE')
            return {'task_id': task['id'], 'threadId': thread['remote_id'], 'turnId': task['remote_turn']}
        def finish(s, c, result):
            if result.get('turnId') != c['turnId']: raise PipelineError('STEER_TURN_MISMATCH', 'UNKNOWN')
            return {'status': 'INPUT_ACCEPTED', 'task_id': c['task_id']}
        return self._operation(request_key, 'steer', [thread_id, prompt], prepare,
            lambda c: self.client.request('turn/steer', {'threadId': c['threadId'], 'expectedTurnId': c['turnId'],
                'clientUserMessageId': request_key, 'input': [{'type': 'text', 'text': prompt, 'text_elements': []}]}),
            finish)

    def cancel(self, task_id, request_key):
        def prepare(s):
            task = self._task(s, task_id)
            thread = self._thread(s, task['thread_id'])
            if task['state'] in TERMINAL: return {'task_id': task_id, 'local': True}
            if task['state'] in ('STORED', 'QUEUED', 'BLOCKED'):
                task['state'] = 'CANCELLED'
                self._event(s, 'task_cancelled', task['thread_id'], task_id)
                return {'task_id': task_id, 'local': True}
            if not task['remote_turn']: raise PipelineError('UNKNOWN_TURN_REQUIRES_RECONCILIATION', 'UNKNOWN')
            if task['stop_sent']: raise PipelineError('STOP_ALREADY_REQUESTED')
            task['state'], task['stop_sent'] = 'STOP_REQUESTED', True
            return {'task_id': task_id, 'local': False, 'threadId': thread['remote_id'], 'turnId': task['remote_turn']}
        return self._operation(request_key, 'cancel', task_id, prepare,
            lambda c: {} if c['local'] else self.client.request('turn/interrupt', {'threadId': c['threadId'], 'turnId': c['turnId']}),
            lambda s, c, _: {'status': self._task(s, task_id)['state'], 'stop_confirmed': self._task(s, task_id)['state'] in TERMINAL})

    def archive(self, thread_id, request_key):
        def prepare(s):
            thread = self._thread(s, thread_id, idle=True)
            if any(t['thread_id'] == thread_id and t['state'] not in TERMINAL for t in s['tasks'].values()):
                raise PipelineError('UNRESOLVED_INPUT')
            thread['closing'] = True
            return {'thread_id': thread_id, 'remote_id': thread['remote_id']}
        def finish(s, c, _):
            s['threads'][thread_id]['archived'] = True
            s['threads'][thread_id]['closing'] = False
            self._event(s, 'thread_archived', thread_id)
            return {'status': 'ARCHIVED', 'thread_id': thread_id, 'history_preserved': True}
        return self._operation(request_key, 'archive', thread_id, prepare,
            lambda c: self.client.request('thread/archive', {'threadId': c['remote_id']}), finish)

    def receive(self, message):
        # Only recognized metadata is exported. Approval IDs are not capabilities.
        if isinstance(message, dict) and 'message' in message: message = message['message']
        if not isinstance(message, dict) or not isinstance(message.get('params'), dict): return
        method, params = message.get('method'), message['params']
        def accept(s):
            thread = next((t for t in s['threads'].values() if t['remote_id'] == params.get('threadId')), None)
            if not thread: return
            if method == 'thread/archived' and thread.get('closing'):
                thread.update(archived=True, closing=False)
                self._event(s, 'thread_archived', thread['id'])
            if not thread['active_task']: return
            task = self._task(s, thread['active_task'])
            if method == 'turn/completed':
                turn = params.get('turn', {})
                if task['remote_turn'] and turn.get('id') == task['remote_turn']: self._adopt_turn(s, task, turn)
                elif task['state'] == 'DISPATCHING' and task['remote_turn'] is None and opaque(turn.get('id')):
                    # Retain only bounded answer text, never command/tool payloads.
                    answer = '\n'.join(i['text'] for i in turn.get('items', []) if isinstance(i, dict)
                                       and i.get('type') == 'agentMessage' and isinstance(i.get('text'), str))
                    task['pending_turn'] = {'id': turn['id'], 'status': turn.get('status'),
                        'answer_truncated': len(answer.encode()) > 4096,
                        'items': [{'type': 'agentMessage', 'text': answer.encode()[:4096].decode('utf-8', errors='ignore')}]}
            elif method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval', 'item/permissions/requestApproval'):
                if task['remote_turn'] and params.get('turnId') == task['remote_turn'] and task['state'] != 'WAITING_APPROVAL':
                    task['state'] = 'WAITING_APPROVAL'
                    self._event(s, 'approval_waiting', thread['id'], task['id'], automatic_approval=False)
                elif task['state'] == 'DISPATCHING' and task['remote_turn'] is None and opaque(params.get('turnId')):
                    task['pending_approval_turn'] = params['turnId']
        self._mutate(accept)

    def tick(self, *, dispatch=True):
        transport = getattr(self, 'transport', None)
        if transport is not None: transport.poll(timeout_s=0)
        tasks = self._mutate(lambda s: [t['id'] for t in s['tasks'].values() if t['state'] in HELD])
        for ident in tasks:
            self.refresh(ident)
            task = self._mutate(lambda s: dict(self._task(s, ident)))
            if task['state'] in HELD and task['deadline'] <= self.clock() and not task['stop_sent']:
                try: self.cancel(ident, 'deadline:' + ident)
                except PipelineError: pass  # UNKNOWN still holds its slot; never redispatch.
        return self._dispatch() if dispatch else {'status': 'RECONCILED'}

    def dispatch(self, name, arguments):
        validate_arguments(name, arguments)
        transport = getattr(self, 'transport', None)
        if transport is not None and name in ('codex_events', 'codex_task_get', 'codex_threads_list'):
            transport.poll(timeout_s=0)
        a = arguments
        if name == 'codex_capabilities': return self.capabilities()
        if name == 'codex_thread_start': return self.start(**a)
        if name in ('codex_chat_store', 'codex_task_submit'):
            result = self.store(**a, queue=name == 'codex_task_submit')
            if name == 'codex_task_submit' and not result.get('duplicate'): self._dispatch()
            return self._mutate(lambda s: self._view(self._task(s, result['id']))) if 'id' in result else result
        if name == 'codex_chat_queue': return self.queue(**a)
        if name == 'codex_chat_steer': return self.steer(**a)
        if name == 'codex_task_cancel': return self.cancel(**a)
        if name == 'codex_thread_archive': return self.archive(**a)
        if name == 'codex_task_get': return self.refresh(**a)
        if name == 'codex_threads_list':
            return self._mutate(lambda s:self.listing(s,a))
        if name == 'codex_events':
            return self._mutate(lambda s: {'events': s['events'][a.get('after', 0):a.get('after', 0)+a.get('limit', 20)],
                'latest_cursor': len(s['events'])})
        return self.tick()


def build_pipeline(runtime, project, *, transport_factory=None):
    """Explicit operator activation only. No caller-supplied command or provider."""
    from common_policy import load_project_policy, developer_instructions
    from codex_app_protocol import ProtocolClient, StdioTransport
    path = Path(runtime) / 'codex-pipeline-policy.json'
    _private(path, 16384)
    policy = validate_policy(_json(path.read_bytes()))
    bundle = load_project_policy(project)
    transport = (transport_factory or StdioTransport)()
    client = ProtocolClient(exchange=transport)
    pipeline = Pipeline(runtime, policy, client, instructions=developer_instructions(bundle), policy_sha256=bundle.sha256)
    transport.bind_client(client)
    try:
        transport.start()
        initialized = client.initialize()
        if not re.search(r'(?<![0-9])0\.165\.0(?![0-9])', initialized['userAgent']):
            raise PipelineError('UNVERIFIED_CODEX_VERSION')
        pipeline.capabilities()  # Verify existing ChatGPT auth; no login/key mutation.
    except BaseException:
        transport.close()
        raise
    pipeline.transport = transport
    return pipeline
