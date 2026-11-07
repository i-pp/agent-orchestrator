"""Daemon-owned, opt-in native gateway. No listener or authentication changes."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import time
import uuid

from private_state import _private, _json
from common_policy import load_project_policy, developer_instructions
from concurrency import lease
from control_socket import request
from codex_app_protocol import ProtocolClient, ProtocolUnknown, RpcError, StdioTransport, schema_version_supported
from codex_pipeline import Pipeline, PipelineError, HELD, TERMINAL, TOOLS, validate_policy, validate_arguments
from isolation_guard import environment
from transport import proc_identity

CLEANUP = 'codex-pipeline-cleanup.json'
# Cold owned-thread dispatch has at most nine 10-second exchanges (including
# initialized). Allow bounded cleanup/IPC headroom below the MCP 120s boundary.
PIPELINE_CONTROL_TIMEOUT_SECONDS = 110


def rpc_failure(method, error, client=None):
    """Fixed categories only; server error bodies/paths/auth never enter logs."""
    result = {'method': method, 'exception_class': type(error).__name__}
    if isinstance(error, RpcError):
        result['rpc_error_code'] = error.code
        message = error.server_message.lower()
        patterns = [('no rollout found', 'NO_ROLLOUT'), ('not materialized', 'NOT_MATERIALIZED'),
                    ('before first user message', 'BEFORE_FIRST_MESSAGE'),
                    ('thread not found', 'THREAD_NOT_FOUND'), ('invalid argument', 'INVALID_ARGUMENT'),
                    ('experimental', 'EXPERIMENTAL_FEATURE_REQUIRED')]
        result['reason_categories'] = [name for text, name in patterns if text in message]
        if not result['reason_categories']: result['reason_categories'] = ['UNCLASSIFIED_SERVER_REJECTION']
    if client is not None:
        diag = client.diagnostics()
        result['protocol'] = {k:v for k,v in diag.items() if k in
            ['last_method','stage','failure_code','rpc_error_code','initialized','request_sequence']}
    return result


def process_snapshot():
    return [value for p in Path('/proc').iterdir() if p.name.isdigit()
            if (value := proc_identity(int(p.name))) is not None]


def binary_snapshot(binary, expected):
    path = Path(binary)
    if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
        raise PipelineError('PINNED_NATIVE_BINARY_REQUIRED')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
            or info.st_mode & 0o022 or not os.access(path, os.X_OK)):
        raise PipelineError('UNSAFE_NATIVE_BINARY')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        if stream.read(4) != b'\x7fELF': raise PipelineError('NATIVE_ELF_REQUIRED')
        stream.seek(0)
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
    if digest.hexdigest() != expected: raise PipelineError('BINARY_HASH_MISMATCH')
    return str(path)


def executor_snapshot(runtime, binary):
    root = Path(runtime)
    path = root / 'codex-pipeline-executor.json'
    _private(path, 4096)
    raw = path.read_bytes(); config = _json(raw)
    if (not isinstance(config, dict) or set(config) != {'schema', 'binary_sha256'}
            or type(config['schema']) is not int or config['schema'] != 1
            or not isinstance(config['binary_sha256'], str) or len(config['binary_sha256']) != 64):
        raise PipelineError('INVALID_EXECUTOR_POLICY')
    binary_snapshot(binary, config['binary_sha256'])
    policy_file = root / 'codex-pipeline-policy.json'
    _private(policy_file, 16384)
    policy_raw = policy_file.read_bytes(); policy = validate_policy(_json(policy_raw))
    if policy['schema'] == 1 and policy['concurrency'] != 1: raise PipelineError('HOST_PIPELINE_REQUIRES_SINGLE_TURN')
    bundle = load_project_policy(Path(__file__).resolve().parents[1])
    return {'executor_policy_sha256': hashlib.sha256(raw).hexdigest(),
            'pipeline_policy_sha256': hashlib.sha256(policy_raw).hexdigest(),
            'common_policy_sha256': bundle.sha256, 'binary_sha256': config['binary_sha256']}


def pipeline_held(runtime):
    """Read-only idle evidence; corruption/missing table is not proof of idle."""
    root = Path(runtime)
    try:
        cleanup = root / CLEANUP
        if cleanup.exists() or cleanup.is_symlink():
            _private(cleanup, 32768)
            if _json(cleanup.read_bytes()).get('status') != 'RELEASED': return True
        db = root / 'codex-pipeline.sqlite3'
        if not db.exists() and not db.is_symlink(): return False
        _private(db, 8 * 1024**2)
        from pipeline_policy_migration import assert_migration_ready
        assert_migration_ready(db)
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
            state = json.loads(conn.execute('SELECT body FROM ledger WHERE id=1').fetchone()[0])
        return any(t['state'] in HELD for t in state['tasks'].values())
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, sqlite3.Error):
        return True


class DaemonPipelineProxy:
    def __init__(self, runtime): self.root = runtime
    def dispatch(self, tool, arguments):
        validate_arguments(tool, arguments)
        return request(self.root, 'codex_pipeline', {'tool': tool, 'arguments': arguments},
                       timeout=PIPELINE_CONTROL_TIMEOUT_SECONDS)


class HostTransport(StdioTransport):
    """A fixed native ELF in its own process group; track PID/start identities."""
    def __init__(self, binary, workspace=None):
        super().__init__()
        self.COMMAND = (binary, 'app-server', '--listen', 'stdio://')
        self.workspace = workspace
        self.seen = {}
        self.cleanup_confirmed = False

    def _spawn(self):
        return subprocess.Popen(list(self.COMMAND), shell=False, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, close_fds=True, start_new_session=True,
            **({'cwd': self.workspace} if self.workspace is not None else {}))

    def observe(self):
        if self._proc is None: return
        identities = process_snapshot()
        root = next((v for v in identities if v['pid'] == self._proc.pid), None)
        if not self.seen and root and root['pgrp'] == root['pid']: self.seen[root['pid']] = root['start']
        anchored = any(self.seen.get(v['pid']) == v['start'] and v['pgrp'] == self._proc.pid for v in identities)
        if anchored:
            for v in identities:
                if v['pgrp'] == self._proc.pid: self.seen.setdefault(v['pid'], v['start'])
        parents = {v['pid'] for v in identities if self.seen.get(v['pid']) == v['start']}
        while True:
            children = {v['pid'] for v in identities if v['ppid'] in parents}
            if children <= parents: break
            parents |= children
        for v in identities:
            if v['pid'] in parents: self.seen.setdefault(v['pid'], v['start'])

    def start(self):
        super().start(); self.observe()
        if self._proc.pid not in self.seen:
            self.close(); raise PipelineError('EXECUTOR_IDENTITY_UNCONFIRMED', 'UNKNOWN')
        return self

    def __call__(self, message, timeout_s):
        self.observe()
        try: return super().__call__(message, timeout_s)
        finally: self.observe()

    def close(self):
        if self._closed: return
        self.observe()
        members = [current for pid, start in self.seen.items()
                   if (current := proc_identity(pid)) and current['start'] == start]
        if self._proc is not None and any(v and v['pgrp'] == self._proc.pid for v in members):
            try: os.killpg(self._proc.pid, signal.SIGTERM)
            except ProcessLookupError: pass
        super().close()
        deadline = time.monotonic() + 2
        survivors = []
        group_members = []
        while True:
            self.observe()
            identities = process_snapshot()
            group_members = [v for v in identities if self._proc is not None
                             and v['pgrp'] == self._proc.pid and v['state'] not in ('Z', 'X')]
            anchored = any(self.seen.get(v['pid']) == v['start'] for v in group_members)
            if group_members and not anchored: break  # Never signal a reused/unproven group.
            if group_members:
                try: os.killpg(self._proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            survivors = [(pid, start) for pid, start in self.seen.items()
                         if (cur := proc_identity(pid)) and cur['start'] == start and cur['state'] not in ('Z', 'X')]
            for pid, start in survivors:
                cur = proc_identity(pid)
                if cur and cur['start'] == start:
                    try: os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError: pass
            if (not survivors and not group_members) or time.monotonic() >= deadline: break
            time.sleep(.02)
        self.cleanup_confirmed = bool(self.seen) and not survivors and not group_members and self.diagnostics()['reaped']


class _LazyClient:
    event_sink = None
    def __init__(self, gateway): self.gateway = gateway
    def request(self, method, params): return self.gateway.rpc(method, params)


class HostGateway:
    def __init__(self, runtime, binary, project, ordinary_busy, *, transport_factory=HostTransport,
                 notification_worker_factory=None, allow_external_notifications=False,
                 notification_runtime=None):
        self.root, self.binary = Path(runtime), binary
        self.require_host()
        self.snapshot = executor_snapshot(runtime, binary)
        self.ordinary_busy = ordinary_busy
        self.transport_factory = transport_factory
        if notification_runtime is not None:
            if notification_worker_factory is not None or allow_external_notifications:
                raise PipelineError('CONFLICTING_NOTIFICATION_CONFIGURATION')
            worker_factory = getattr(notification_runtime, 'worker_factory', None)
            enabled = getattr(notification_runtime, 'enabled', None)
            runtime_root = getattr(notification_runtime, 'runtime_root', None)
            if (not callable(worker_factory) or type(enabled) is not bool
                    or runtime_root is None
                    or Path(runtime_root).absolute() != self.root.absolute()):
                raise PipelineError('INVALID_NOTIFICATION_RUNTIME')
            notification_worker_factory = worker_factory if enabled else None
            allow_external_notifications = enabled
        if notification_worker_factory is not None and not callable(notification_worker_factory):
            raise PipelineError('INVALID_NOTIFICATION_WORKER_FACTORY')
        if type(allow_external_notifications) is not bool:
            raise PipelineError('INVALID_NOTIFICATION_POLICY')
        self.notification_worker_factory = notification_worker_factory
        self.allow_external_notifications = allow_external_notifications
        self.notification_worker = None
        self.notification_worker_started = False
        self.notification_worker_status = {'status': 'OFF'}
        self.transport = self.protocol = self.budget = None
        self.failed = False
        self.context_deadlines = {}
        marker = self.root / CLEANUP
        if marker.exists() or marker.is_symlink():
            _private(marker, 32768)
            if _json(marker.read_bytes()).get('status') != 'RELEASED':
                raise PipelineError('EXECUTOR_CLEANUP_REQUIRES_RECONCILIATION', 'UNKNOWN')
        bundle = load_project_policy(project)
        if bundle.sha256 != self.snapshot['common_policy_sha256']: raise PipelineError('COMMON_POLICY_CHANGED')
        policy = _json((self.root / 'codex-pipeline-policy.json').read_bytes())
        self.pipeline = Pipeline(runtime, policy, _LazyClient(self), instructions=developer_instructions(bundle),
            policy_sha256=bundle.sha256, admission=self.admit_turn)

    def _start_notification_worker(self):
        """Start isolated delivery without allowing its failure to stop Codex."""
        if not getattr(self, 'allow_external_notifications', False):
            self.notification_worker_status = {'status': 'DISABLED'}
            return self.notification_worker_status
        factory = getattr(self, 'notification_worker_factory', None)
        if factory is None:
            return getattr(self, 'notification_worker_status', {'status': 'OFF'})
        if getattr(self, 'notification_worker_status', {}).get('status') == 'UNAVAILABLE':
            return self.notification_worker_status
        try:
            if getattr(self, 'notification_worker', None) is None:
                worker = factory(self.pipeline)
                outbox = getattr(worker, 'outbox', None)
                source_path = getattr(outbox, 'source_path', None)
                if (not callable(getattr(worker, 'start', None))
                        or not callable(getattr(worker, 'stop', None))
                        or source_path is None
                        or Path(source_path).absolute() != self.pipeline.db.absolute()):
                    self.notification_worker_status = {
                        'status': 'UNAVAILABLE', 'code': 'WORKER_CONFIGURATION_INVALID'}
                    return self.notification_worker_status
                self.notification_worker = worker
                self.pipeline.notification_outbox = outbox
            if not getattr(self, 'notification_worker_started', False):
                self.notification_worker.start()
                self.notification_worker_started = True
            self.notification_worker_status = {'status': 'RUNNING'}
        except Exception:
            self.notification_worker_status = {
                'status': 'UNAVAILABLE', 'code': 'WORKER_START_FAILED'}
        return self.notification_worker_status

    def _stop_notification_worker(self):
        worker = self.notification_worker
        if worker is None or not getattr(self, 'notification_worker_started', False):
            return {'status': 'OFF'}
        try:
            stopped = worker.stop(timeout=2.0)
        except Exception:
            stopped = False
        self.notification_worker_started = stopped is not True
        self.notification_worker_status = {
            'status': 'STOPPED' if stopped is True else 'STOP_UNCONFIRMED'}
        return self.notification_worker_status

    @staticmethod
    def require_host():
        if os.getuid() == 0 or environment()['state'] != 'HOST_CONTEXT':
            raise PipelineError('NORMAL_HOST_CONTEXT_NOT_CONFIRMED')
        home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        if (not home.is_dir() or home.stat().st_uid != os.getuid() or not os.access(home, os.W_OK)
                or os.statvfs(home).f_flag & os.ST_RDONLY): raise PipelineError('CODEX_STATE_NOT_WRITABLE')

    def marker(self, status):
        path = self.root / (CLEANUP + '.' + uuid.uuid4().hex + '.tmp')
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            facts = {'status': status, 'automatic_retry': False}
            if self.transport is not None:
                facts['processes'] = getattr(self.transport, 'seen', {})
                diagnostic = getattr(self.transport, 'diagnostics', None)
                if callable(diagnostic):
                    data = diagnostic()
                    facts['transport'] = {k:data[k] for k in ('stage','failure_code','stdout_bytes','stderr_bytes','exit_code_before_close','reaped') if k in data}
            json.dump(facts, stream); stream.flush(); os.fsync(stream.fileno())
        os.replace(path, self.root / CLEANUP)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)

    def record_failure(self, method, error):
        facts = rpc_failure(method, error, self.protocol)
        diagnostic = getattr(self.transport, 'diagnostics', None)
        if callable(diagnostic):
            raw = diagnostic()
            allowed = ('stage','failure_code','request_count','frames_received','stdout_bytes','stderr_bytes',
                       'termination_requested','exit_code_before_close','reaped')
            facts['transport'] = {k:raw[k] for k in allowed if k in raw}
        path = self.root / 'logs' / ('pipeline-rpc-failure-' + uuid.uuid4().hex + '.json')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump(facts,stream);stream.flush();os.fsync(stream.fileno())
        return facts

    def resolve_absent_contexts(self, thread_ids, apply=False):
        from codex_pipeline import digest
        from pipeline_absence import eligible,record,exact_missing_reason
        if (type(apply) is not bool or not isinstance(thread_ids,list) or not 1<=len(thread_ids)<=16
                or any(not isinstance(i,str) for i in thread_ids) or len(set(thread_ids))!=len(thread_ids)):
            raise PipelineError('INVALID_ABSENCE_SCOPE')
        if self.held or self.transport is not None or self.budget is not None:
            raise PipelineError('ABSENCE_REQUIRES_IDLE_EXECUTOR')
        current=self.pipeline._mutate(lambda s:json.loads(json.dumps(s)))
        for tid in thread_ids:eligible(current,tid)
        if apply:
            receipt=getattr(self,'absence_receipt',None)
            if (not receipt or receipt['ids']!=thread_ids or receipt['ledger']!=digest(current)
                    or time.monotonic()-receipt['time']>60
                    or any(not 0<=time.time()-p['observed_at']<=60 for p in receipt['proofs'])):raise PipelineError('ABSENCE_PROOF_STALE')
            _private(self.root/CLEANUP,32768)
            if _json((self.root/CLEANUP).read_bytes())['status']!='RELEASED':raise PipelineError('ABSENCE_CLEANUP_UNCONFIRMED')
            result=self.pipeline._mutate(lambda s:record(s,receipt['proofs']))
            self.absence_receipt=None
            return result
        self.absence_receipt=None;proofs=[];observations=[]
        self.budget=lease(self.root/'runner.lock')
        try:
            for tid in thread_ids:
                t=current['threads'][tid]
                try:
                    reply=self.rpc('thread/read',{'threadId':t['remote_id'],'includeTurns':False})
                except RpcError as error:
                    observed_method='thread/read'
                    if error.code==-32600 and error.server_message=='thread not loaded: '+t['remote_id']:
                        # Resume only this unused owned ID, under the original scope. No turn or archive.
                        try:
                            present=self.rpc('thread/resume',{'threadId':t['remote_id'],'excludeTurns':True,
                                'model':t['model'],'cwd':self.pipeline.policy['workspaces'][t['workspace']],
                                'developerInstructions':self.pipeline.instructions,'approvalPolicy':'on-request',
                                'approvalsReviewer':'user','sandbox':'read-only'})
                        except RpcError as resume_error:
                            error=resume_error;observed_method='thread/resume'
                        else:
                            matched=(present.get('thread',{}).get('id')==t['remote_id']
                                and present.get('model')==t['model'] and present.get('modelProvider')=='openai'
                                and present.get('cwd')==self.pipeline.policy['workspaces'][t['workspace']]
                                and present.get('sandbox',{}).get('type')=='readOnly'
                                and present.get('approvalPolicy')=='on-request' and present.get('approvalsReviewer')=='user')
                            observations.append({'thread_id':tid,'state':'PRESENT' if matched else 'UNKNOWN'})
                            continue
                    facts=rpc_failure(observed_method,error,self.protocol)
                    reason=('NO_ROLLOUT' if error.code==-32600 and 'NO_ROLLOUT' in facts.get('reason_categories',[]) else exact_missing_reason(error,t['remote_id']) if observed_method=='thread/read' else None)
                    if reason:
                        proofs.append({'thread_id':tid,'remote_id_sha256':digest(t['remote_id']),
                            'policy_hash':current['policy_hash'],'observed_at':time.time(),'method':observed_method,
                            'rpc_error_code':-32600,'reason':reason})
                        observations.append({'thread_id':tid,'state':('NO_DURABLE_ROLLOUT' if reason=='NO_ROLLOUT' else 'NATIVE_THREAD_NOT_FOUND')})
                    else:
                        import re
                        tokens=set(re.findall('[a-z]+',error.server_message.lower()))
                        flags={k:k in tokens for k in ('thread','rollout','found','not','loaded','materialized','before','read','resume','history','missing','must','available','exists','turns','paginated','support','deprecated','experimental','enabled','section','invalid')}
                        observations.append({'thread_id':tid,'state':'UNKNOWN','fixed_word_flags':flags})
                else:
                    remote=reply.get('thread',{})
                    observations.append({'thread_id':tid,'state':'PRESENT' if remote.get('id')==t['remote_id'] else 'UNKNOWN'})
        finally:self.release_idle(force=True)
        if len(proofs)==len(thread_ids) and not self.failed:
            if self.pipeline._mutate(digest)!=digest(current):raise PipelineError('ABSENCE_PROOF_STALE')
            self.absence_receipt={'ids':list(thread_ids),'ledger':digest(current),'proofs':proofs,'time':time.monotonic()}
        return {'status':'CONFIRMED' if getattr(self,'absence_receipt',None) else 'BLOCKED','observations':observations,
            'no_inference':True,'native_archive_confirmed':False}

    def inspect_owned_thread(self, thread_id):
        """Operator-only paused reconciliation; fixed read/resume, never a turn."""
        if self.held or self.transport is not None or self.protocol is not None or self.budget is not None:
            raise PipelineError('EXECUTOR_NOT_IDLE_FOR_INSPECTION')
        with closing(sqlite3.connect(self.pipeline.db.as_uri()+'?mode=ro',uri=True)) as conn:
            state=json.loads(conn.execute('SELECT body FROM ledger WHERE id=1').fetchone()[0])
        thread=state['threads'].get(thread_id)
        if (not thread or thread['active_task'] or not thread.get('remote_id')
                or thread.get('archived') or thread.get('closing')):
            raise PipelineError('OWNED_IDLE_THREAD_REQUIRED')
        if any(op.get('kind')=='dispatch' for op in state['ops'].values()):
            raise PipelineError('TURN_DISPATCH_REQUIRES_SEPARATE_RECONCILIATION')
        self.require_host()
        self.budget=lease(self.root/'runner.lock')
        result={'thread_id':thread_id,'inference_calls':0,'new_threads':0,'checks':[]}
        client=None
        stage='preflight'
        try:
            if executor_snapshot(self.root,self.binary)!=self.snapshot:
                raise PipelineError('EXECUTOR_POLICY_CHANGED')
            self.marker('UNKNOWN')
            self.transport=self.transport_factory(self.binary)
            self.transport.workspace=self.pipeline.policy['workspaces'][thread['workspace']]
            client=ProtocolClient(self.transport,timeout_s=10)
            self.transport.bind_client(client);self.transport.start()
            stage='initialize'
            initialized=client.initialize()
            if not schema_version_supported(initialized['userAgent']):raise PipelineError('UNVERIFIED_SERVER_VERSION')
            calls=[('account/read',{'refreshToken':False}),('model/list',{'includeHidden':False,'limit':100}),
                   ('thread/read',{'threadId':thread['remote_id'],'includeTurns':True}),
                   ('thread/resume',{'threadId':thread['remote_id'],'excludeTurns':True,
                    'model':thread['model'],'cwd':self.transport.workspace,'developerInstructions':self.pipeline.instructions,
                    'approvalPolicy':'on-request','approvalsReviewer':'user','sandbox':'read-only'})]
            for method,params in calls:
                stage=method
                try:reply=client.request(method,params)
                except RpcError as error:
                    result['checks'].append(rpc_failure(method,error,client))
                    if method in ['account/read','model/list']:break
                    continue
                result['checks'].append({'method':method,'status':'ACKNOWLEDGED'})
                if method=='account/read':
                    result['account_type_is_chatgpt']=(reply.get('account') or {}).get('type')=='chatgpt'
                    if not result['account_type_is_chatgpt']:break
                if method=='thread/read':
                    result['read_scope_matches']=reply['thread'].get('id')==thread['remote_id']
                    if not result['read_scope_matches']:break
                    count=len(reply['thread'].get('turns',[]))
                    result['remote_turn_count']=count
                    if count:break
                if method=='thread/resume':
                    result['resume_scope_matches']=(reply['thread']['id']==thread['remote_id']
                        and reply.get('model')==thread['model'] and reply.get('modelProvider')=='openai'
                        and reply.get('cwd')==self.transport.workspace and reply.get('approvalPolicy')=='on-request'
                        and reply.get('approvalsReviewer')=='user' and reply.get('sandbox',{}).get('type')=='readOnly')
            return result
        except Exception as error:
            result['checks'].append(rpc_failure(stage,error,client))
            return result
        finally:
            try:
                safe=True
                if self.transport is not None:
                    self.transport.close();safe=self.transport.cleanup_confirmed
                    self.marker('RELEASED' if safe else 'UNKNOWN')
                    if safe:self.transport=None
                elif client is None:
                    # No transport was constructed, so no native process forked.
                    self.marker('RELEASED')
                if safe and self.budget is not None:self.budget.close();self.budget=None
                if not safe:self.failed=True
                result['cleanup_status']='RELEASED' if safe else 'UNKNOWN'
            except Exception as error:
                self.failed=True
                result.update(cleanup_status='UNKNOWN',cleanup_exception_class=type(error).__name__)

    def replace_empty_context(self, thread_id, request_key, evidence):
        """Same-UID paused operator action; exactly one proven-empty replacement."""
        if self.held or self.transport is not None or self.protocol is not None or self.budget is not None:
            raise PipelineError('EXECUTOR_NOT_IDLE_FOR_REPLACEMENT')
        def admit():
            # Called only after durable proof validation/intent. Paused ordinary
            # admission is intentionally bypassed for metadata creation, not a turn.
            self.require_host()
            self.budget=lease(self.root/'runner.lock')
            self.failed=False
        try:
            result=self.pipeline.replace_empty_context(thread_id,request_key,evidence,admission=admit)
            if result.get('status')=='READY' and not result.get('duplicate'):
                self.context_deadlines[thread_id]=time.monotonic()+self.pipeline.policy['timeout_seconds']
            return result
        finally:self.release_idle()

    @property
    def held(self): return pipeline_held(self.root)

    def reserve(self):
        if self.budget is not None: return True
        if self.ordinary_busy(): return False
        try: self.budget = lease(self.root / 'runner.lock')
        except RuntimeError: return False
        return True

    def admit_turn(self):
        return not self.ordinary_busy() and self.reserve()

    def connect(self):
        if self.protocol is not None: return
        if self.failed: raise PipelineError('EXECUTOR_CONNECTION_REQUIRES_RECONCILIATION', 'UNKNOWN')
        if not self.reserve(): raise PipelineError('SHARED_BUDGET_HELD')
        self.require_host()
        if executor_snapshot(self.root, self.binary) != self.snapshot: raise PipelineError('EXECUTOR_POLICY_CHANGED')
        self.marker('UNKNOWN')  # Before fork; crash never proves cleanup.
        self.transport = self.transport_factory(self.binary)
        self.transport.workspace = sorted(self.pipeline.policy['workspaces'].values())[0]
        self.protocol = ProtocolClient(self.transport, event_sink=self.pipeline.receive, timeout_s=10)
        self.transport.bind_client(self.protocol)
        self.pipeline.loaded.clear()
        self.pipeline.transport = self.transport
        try:
            self.transport.start()
            initialized = self.protocol.initialize()
            if not schema_version_supported(initialized['userAgent']): raise PipelineError('UNVERIFIED_SERVER_VERSION')
        except Exception as error:
            self.failed = True
            self.record_failure('initialize',error)
            raise

    def rpc(self, method, params):
        self.connect()
        try: return self.protocol.request(method, params)
        except RpcError as error:
            # A correlated rejection of a read is not a lost connection or a
            # successful turn. Keep the task held until notification/history
            # reconciliation; never replay a turn because this read failed.
            # Normal operation keeps a valid, correlated rejection distinct
            # from transport uncertainty. The task/operation state still holds
            # refused stop/steer or marks a rejected start FAILED; never replay.
            handled = {'thread/read','thread/start','turn/start','turn/steer','turn/interrupt','thread/archive',
                       'thread/resume','account/read','model/list'}
            if method != 'thread/read' and (self.pipeline.policy['schema'] == 1 or method not in handled): self.failed = True
            self.record_failure(method,error)
            raise
        except Exception as error:
            self.failed = True
            self.record_failure(method,error)
            raise

    def release_idle(self, *, force=False):
        if self.pipeline._mutate(lambda s: any(t['state'] in HELD for t in s['tasks'].values())): return
        if self.protocol is not None and not force:
            threads=self.pipeline._mutate(lambda s:[dict(t) for t in s['threads'].values()
                if t['id'] in self.pipeline.loaded and not t.get('history_verified')
                and not t.get('archived') and not t.get('closing')])
            for thread in threads:
                tasks=self.pipeline._mutate(lambda s:[dict(t) for t in s['tasks'].values() if t['thread_id']==thread['id']])
                turned=[t for t in tasks if t.get('remote_turn')]
                if turned:
                    try:
                        remote=self.rpc('thread/read',{'threadId':thread['remote_id'],'includeTurns':True})['thread']
                        expected={'COMPLETED':'completed','FAILED':'failed','CANCELLED':'interrupted'}
                        observed={t.get('id'):t.get('status') for t in remote.get('turns',[])}
                        if (remote.get('id')!=thread['remote_id'] or
                                any(expected.get(t['state']) is None or
                                    observed.get(t['remote_turn'])!=expected[t['state']] for t in turned)):
                            raise PipelineError('FIRST_TURN_HISTORY_UNCONFIRMED','UNKNOWN')
                        self.pipeline._mutate(lambda s:s['threads'][thread['id']].update(history_verified=True))
                        self.context_deadlines.pop(thread['id'],None)
                    except Exception:
                        self.failed=True
                        self.pipeline._mutate(lambda s:s['threads'][thread['id']].update(native_context_state='HISTORY_UNKNOWN'))
                elif not self.failed:
                    deadline=self.context_deadlines.setdefault(thread['id'],time.monotonic()+self.pipeline.policy['timeout_seconds'])
                    if time.monotonic()<deadline:return
                    self.pipeline._mutate(lambda s:s['threads'][thread['id']].update(native_context_state='EMPTY_CONTEXT_EXPIRED'))
                    self.context_deadlines.pop(thread['id'],None)
        marker = self.root / CLEANUP
        safe = not marker.exists() or _json(marker.read_bytes()).get('status') == 'RELEASED'
        if self.transport is not None:
            self.transport.close(); safe = self.transport.cleanup_confirmed
            self.marker('RELEASED' if safe else 'UNKNOWN')
            self.protocol = None
            self.pipeline.transport = None
            self.pipeline.loaded.clear()
            if safe: self.transport = None
        if safe and self.budget is not None:
            self.budget.close(); self.budget = None
        if not safe:
            self.failed = True
            raise PipelineError('EXECUTOR_CLEANUP_UNCONFIRMED', 'UNKNOWN')

    def dispatch(self, tool, arguments):
        validate_arguments(tool, arguments)
        remote = tool in ('codex_capabilities', 'codex_thread_start', 'codex_chat_steer', 'codex_thread_archive')
        result = None
        if remote and not self.reserve(): raise PipelineError('SHARED_BUDGET_HELD')
        try:
            if tool == 'codex_pipeline_tick': return self.pump()
            result = self.pipeline.dispatch(tool, arguments)
            if tool == 'codex_capabilities': result.update(resident_scheduler=True, executor_owner='existing_daemon')
            return result
        finally:
            self.release_idle()
            if isinstance(result, dict) and isinstance(result.get('capacity'), dict):
                result['capacity'].update(executor_release_confirmed=not self.held,
                    executor_reconciliation_required=self.failed)

    def pump(self):
        self._start_notification_worker()
        try:
            if self.held:
                if not self.reserve(): return {'status': 'SHARED_BUDGET_HELD'}
                self.pipeline.tick(dispatch=False)
            else:
                # Reconcile missed durable events even when the last task is terminal.
                sync = getattr(self.pipeline, 'sync_notifications', None)
                if callable(sync): sync()
            self.release_idle()
            if self.failed: return {'status': 'UNKNOWN', 'automatic_retry': False}
            return self.pipeline._dispatch()
        finally: self.release_idle()

    def close(self):
        uncertain = False
        try:
            try:
                ids = self.pipeline._mutate(lambda s: [t['id'] for t in s['tasks'].values() if t['state'] in HELD])
                for ident in ids:
                    try: self.pipeline.cancel(ident, 'shutdown:' + ident)
                    except Exception: pass
            except Exception: uncertain = True
            finally:
                if self.transport is not None:
                    try: self.transport.close()
                    finally: self.marker('RELEASED' if self.transport.cleanup_confirmed and not uncertain else 'UNKNOWN')
            def preserve_unknown(s):
                for task in s['tasks'].values():
                    if task['state'] in HELD: task['state'] = 'UNKNOWN'
            try: self.pipeline._mutate(preserve_unknown)
            except Exception: self.marker('UNKNOWN')
        finally:
            self._stop_notification_worker()
            if self.budget is not None: self.budget.close(); self.budget = None
