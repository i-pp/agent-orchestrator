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

from chat_mailbox import _private, _json
from common_policy import load_project_policy, developer_instructions
from concurrency import lease
from control_socket import request
from codex_app_protocol import ProtocolClient, ProtocolUnknown, StdioTransport, schema_version_supported
from codex_pipeline import Pipeline, PipelineError, HELD, TERMINAL, TOOLS, validate_policy, validate_arguments
from isolation_guard import environment
from transport import proc_identity

CLEANUP = 'codex-pipeline-cleanup.json'


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
    if policy['concurrency'] != 1: raise PipelineError('HOST_PIPELINE_REQUIRES_SINGLE_TURN')
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
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
            state = json.loads(conn.execute('SELECT body FROM ledger WHERE id=1').fetchone()[0])
        return any(t['state'] in HELD for t in state['tasks'].values())
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, sqlite3.Error):
        return True


class DaemonPipelineProxy:
    def __init__(self, runtime): self.root = runtime
    def dispatch(self, tool, arguments):
        validate_arguments(tool, arguments)
        return request(self.root, 'codex_pipeline', {'tool': tool, 'arguments': arguments}, timeout=45)


class HostTransport(StdioTransport):
    """A fixed native ELF in its own process group; track PID/start identities."""
    def __init__(self, binary):
        super().__init__()
        self.COMMAND = (binary, 'app-server', '--listen', 'stdio://')
        self.seen = {}
        self.cleanup_confirmed = False

    def _spawn(self):
        return subprocess.Popen(list(self.COMMAND), shell=False, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, close_fds=True, start_new_session=True)

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
    def __init__(self, runtime, binary, project, ordinary_busy, *, transport_factory=HostTransport):
        self.root, self.binary = Path(runtime), binary
        self.require_host()
        self.snapshot = executor_snapshot(runtime, binary)
        self.ordinary_busy = ordinary_busy
        self.transport_factory = transport_factory
        self.transport = self.protocol = self.budget = None
        self.failed = False
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
            json.dump(facts, stream); stream.flush(); os.fsync(stream.fileno())
        os.replace(path, self.root / CLEANUP)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)

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
        self.protocol = ProtocolClient(self.transport, event_sink=self.pipeline.receive, timeout_s=2)
        self.transport.bind_client(self.protocol)
        self.pipeline.loaded.clear()
        self.pipeline.transport = self.transport
        try:
            self.transport.start()
            initialized = self.protocol.initialize()
            if not schema_version_supported(initialized['userAgent']): raise PipelineError('UNVERIFIED_SERVER_VERSION')
        except Exception:
            self.failed = True
            raise

    def rpc(self, method, params):
        self.connect()
        try: return self.protocol.request(method, params)
        except Exception:
            self.failed = True
            raise

    def release_idle(self):
        if self.pipeline._mutate(lambda s: any(t['state'] in HELD for t in s['tasks'].values())): return
        marker = self.root / CLEANUP
        safe = not marker.exists() or _json(marker.read_bytes()).get('status') == 'RELEASED'
        if self.transport is not None:
            self.transport.close(); safe = self.transport.cleanup_confirmed
            self.marker('RELEASED' if safe else 'UNKNOWN')
            self.protocol = None
            self.pipeline.transport = None
            if safe: self.transport = None
        if safe and self.budget is not None:
            self.budget.close(); self.budget = None
        if not safe:
            self.failed = True
            raise PipelineError('EXECUTOR_CLEANUP_UNCONFIRMED', 'UNKNOWN')

    def dispatch(self, tool, arguments):
        validate_arguments(tool, arguments)
        remote = tool in ('codex_capabilities', 'codex_thread_start', 'codex_chat_steer', 'codex_thread_archive')
        if remote and not self.reserve(): raise PipelineError('SHARED_BUDGET_HELD')
        try:
            if tool == 'codex_pipeline_tick': return self.pump()
            result = self.pipeline.dispatch(tool, arguments)
            if tool == 'codex_capabilities': result.update(resident_scheduler=True, executor_owner='existing_daemon')
            return result
        finally: self.release_idle()

    def pump(self):
        try:
            if self.held:
                if not self.reserve(): return {'status': 'SHARED_BUDGET_HELD'}
                self.pipeline.tick(dispatch=False)
            self.release_idle()
            if self.failed: return {'status': 'UNKNOWN', 'automatic_retry': False}
            return self.pipeline._dispatch()
        finally: self.release_idle()

    def close(self):
        try:
            ids = self.pipeline._mutate(lambda s: [t['id'] for t in s['tasks'].values() if t['state'] in HELD])
            for ident in ids:
                try: self.pipeline.cancel(ident, 'shutdown:' + ident)
                except Exception: pass
            if self.transport is not None:
                self.transport.close()
                self.marker('RELEASED' if self.transport.cleanup_confirmed else 'UNKNOWN')
            def uncertain(s):
                for task in s['tasks'].values():
                    if task['state'] in HELD: task['state'] = 'UNKNOWN'
            self.pipeline._mutate(uncertain)
        finally:
            if self.budget is not None: self.budget.close(); self.budget = None
