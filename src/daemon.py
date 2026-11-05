"""Foreground daemon core: private local socket and at most two guarded workers."""

import errno
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import sqlite3
import sys
import time
import uuid

from concurrency import lease, recover_abandoned
from control_socket import endpoint, peer_uid, validate_socket, MAX_REQUEST, MAX_RESPONSE
from resources import Control, ResourceExceeded, BudgetLog, storage_check
from runner import Runner
import scheduler
import development_poc
from transport import Process, redact


class Daemon:
    def __init__(self, root, binary=None, *, worker_factory=None, development=False,
                 allow_codex_pipeline=False, pipeline_factory=None):
        self.settings = (scheduler.configuration(codex_pipeline=True) if allow_codex_pipeline
                         else scheduler.configuration())
        self.runner = Runner(root, binary, development=development)
        self.root, self.store = self.runner.root, self.runner.store
        try: self.lease = lease(self.root / "daemon.lock")
        except BaseException:
            self.store.close(); raise
        try:
            scheduler.initialize(self.store)
        except BaseException:
            self.lease.close(); self.store.close(); raise
        self.owner = scheduler.owner_identity()
        self.worker_factory = worker_factory
        self.children = {}
        self.stopping = False
        self.blocked = []
        self.pending_recovery = []
        self.reconciled_unknowns = set()
        self.selector = selectors.DefaultSelector()
        self.clients = {}
        self.listener = None
        self.socket_path = None
        self.socket_inode = None
        self.exit_code = 0
        self.boot_id = str(uuid.uuid4())
        self.allow_codex_pipeline = allow_codex_pipeline
        self.pipeline_factory = pipeline_factory
        self.pipeline = None
        self.events_stream = (self.root / "logs" / ("daemon-" + self.boot_id + ".jsonl")).open("x")
        self.events = BudgetLog(self.events_stream, self.settings["daemon"]["log_bytes"])

    def event(self, kind, **facts):
        self.events.write(json.dumps({"time": time.time(), "boot": self.boot_id, "event": kind, **redact(facts)}) + "\n")
        self.events.flush()

    def bind(self):
        self.socket_path = endpoint(self.root, create=True)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            before = validate_socket(self.socket_path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(.2)
                code = probe.connect_ex(str(self.socket_path))
                if code not in [errno.ECONNREFUSED, errno.ENOENT]:
                    raise RuntimeError("An existing socket may still have an owner; refusing replacement")
            if self.socket_path.lstat().st_ino != before.st_ino:
                raise RuntimeError("Socket changed during stale endpoint validation")
            self.socket_path.unlink()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path))
        self.socket_path.chmod(0o600)
        self.socket_inode = validate_socket(self.socket_path).st_ino
        self.listener.listen(64)
        self.listener.setblocking(False)
        self.selector.register(self.listener, selectors.EVENT_READ, None)

    def status(self):
        return {"pid": os.getpid(), "owner": self.owner, "boot": self.boot_id, "runtime": str(self.root),
                "execution_mode": development_poc.MODE if self.runner.development else "namespace",
                "socket": str(self.socket_path), "paused": bool(self.store.db.execute("SELECT paused FROM scheduler_state WHERE id=1").fetchone()[0]),
                "stopping": self.stopping, "active_tasks": list(self.children), "blocked": self.blocked,
                "pending_recovery": self.pending_recovery, "maximum_sessions": 2,
                "task_counts": {r[0]: r[1] for r in self.store.db.execute("SELECT state,count(*) FROM tasks GROUP BY state")},
                "resource_policy": self.settings["daemon"], "worker_resource_policy": self.settings["worker"],
                "codex_pipeline_enabled": self.allow_codex_pipeline,
                "codex_pipeline_held": bool(self.pipeline and self.pipeline.held)}

    def ordinary_busy(self):
        return bool(self.stopping or self.children or self.blocked or self.pending_recovery
            or self.store.db.execute("SELECT paused FROM scheduler_state WHERE id=1").fetchone()[0]
            or self.store.db.execute("SELECT 1 FROM tasks WHERE state IN ('RUNNING','VERIFYING') LIMIT 1").fetchone())

    def handle(self, method, params):
        if not isinstance(params, dict): raise ValueError("params must be an object")
        if method == 'codex_pipeline_replace_empty':
            if (not self.allow_codex_pipeline or self.pipeline is None
                    or set(params)!={'thread_id','request_key','evidence'}
                    or not self.status()['paused'] or self.children or self.pending_recovery):
                raise ValueError('Paused idle owned pipeline required for empty-context replacement')
            return self.pipeline.replace_empty_context(params['thread_id'],params['request_key'],params['evidence'])
        if method == 'codex_pipeline_resolve_absent':
            if (not self.allow_codex_pipeline or self.pipeline is None
                    or set(params)!={'thread_ids','apply'} or not self.status()['paused']
                    or self.children or self.pending_recovery):
                raise ValueError('Paused idle owned pipeline required for absence reconciliation')
            return self.pipeline.resolve_absent_contexts(params['thread_ids'],params['apply'])
        if method == 'codex_pipeline_inspect':
            if (not self.allow_codex_pipeline or self.pipeline is None or set(params)!={'thread_id'}
                    or not self.status()['paused'] or self.children or self.pending_recovery):
                raise ValueError('Paused idle owned pipeline required for operator inspection')
            return self.pipeline.inspect_owned_thread(params['thread_id'])
        if method == 'codex_pipeline':
            if not self.allow_codex_pipeline or self.pipeline is None: raise ValueError('Pipeline capability is not enabled')
            from codex_pipeline import PipelineError, validate_arguments
            if set(params) != {'tool', 'arguments'}: raise ValueError('Unexpected pipeline fields')
            try:
                validate_arguments(params['tool'], params['arguments'])
                return self.pipeline.dispatch(params['tool'], params['arguments'])
            except PipelineError as error:
                return error.result()
        if method == "submit":
            if self.stopping: raise RuntimeError("Daemon is stopping; submission refused")
            allowed = {"prompt", "request_key", "acceptance", "workspace", "resume_of", "resume_approval", "timeout"}
            if set(params) - allowed: raise ValueError("Unsupported task fields")
            if not isinstance(params.get("prompt"), str) or not params["prompt"].strip():
                raise ValueError("prompt must be a nonempty string")
            key = params.get("request_key")
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ValueError("A stable request_key is required for socket submission")
            timeout = params.get("timeout", self.settings["task_timeout"])
            if type(timeout) not in [int, float] or not 0 < timeout <= 3600:
                raise ValueError("Task timeout must be 0 < seconds <= 3600")
            task_id = self.store.submit(params["prompt"], request_key=key, acceptance=params.get("acceptance"),
                workspace=params.get("workspace", None if params.get("resume_of") else {}), resume_of=params.get("resume_of"),
                resume_approval=params.get("resume_approval"), schedule_timeout=timeout)
            self.event("submitted", task_id=task_id)
            return {"task_id": task_id, "request_key": key}
        if method == "status":
            if set(params) - {"task_id"}: raise ValueError("Unsupported status fields")
            if params.get("task_id"):
                task = self.store.get(params["task_id"])
                if task["active_execution_id"]: task["execution"] = self.store.execution(task["active_execution_id"])
                dispatch = self.store.db.execute("SELECT phase FROM scheduler_dispatches WHERE task_id=?", (task["id"],)).fetchone()
                task["scheduler_phase"] = dispatch[0] if dispatch else None
                option = self.store.db.execute("SELECT timeout FROM scheduler_options WHERE task_id=?", (task["id"],)).fetchone()
                task["scheduler_timeout"] = option[0] if option else None
                task["worker_pending"] = bool(dispatch and dispatch[0] in ["RESERVED", "STARTED", "AUTHORIZED"])
                task["completion_verified"] &= not task["worker_pending"]
                return task
            return self.status()
        if method == "list":
            if set(params) - {"limit", "offset"}: raise ValueError("Unsupported list fields")
            limit = params.get("limit", 100)
            offset = params.get("offset", 0)
            if type(limit) is not int or not 1 <= limit <= 100: raise ValueError("limit must be 1..100")
            if type(offset) is not int or not 0 <= offset <= 1000000: raise ValueError("Invalid list offset")
            return [dict(r) for r in self.store.db.execute("""SELECT id,state,created,updated,workspace_id,
                codex_status,verification_status FROM tasks ORDER BY rowid LIMIT ? OFFSET ?""", (limit, offset))]
        if method == "cancel":
            if set(params) != {"task_id"}: raise ValueError("cancel requires only task_id")
            return self.store.cancel(params["task_id"])
        if method in ["pause", "resume_scheduler"]:
            if params: raise ValueError("Scheduler control takes no parameters")
            with self.store.db:
                self.store.db.execute("UPDATE scheduler_state SET paused=?,updated=? WHERE id=1", (method == "pause", time.time()))
            self.event(method)
            return {"paused": method == "pause"}
        if method == "shutdown":
            if params: raise ValueError("shutdown takes no parameters")
            self.stopping = True
            return {"stopping": True}
        raise ValueError("Unsupported control method")

    def observed(self, task_id, processes):
        row = self.store.db.execute("SELECT processes,phase FROM scheduler_dispatches WHERE task_id=?", (task_id,)).fetchone()
        old = json.loads(row[0] or "{}")
        old.update({str(pid): start for pid, start in processes.items()})
        with self.store.db:
            self.store.db.execute("""UPDATE scheduler_dispatches SET processes=?,phase=CASE
                WHEN phase='RESERVED' THEN 'STARTED' ELSE phase END WHERE task_id=?""", (json.dumps(old), task_id))

    def start_worker(self, task):
        token = str(uuid.uuid4())
        control = Control(self.root, self.settings["worker"])
        relative = Path('/proc/self/cgroup').read_text().strip().split('::')[1]
        parent = (Path('/sys/fs/cgroup') / relative.lstrip('/')).parent
        if parent.name != 'agentorchestrator.slice':
            raise RuntimeError("Scheduler worker scopes require the verified private slice")
        control.descriptor["expected_cgroup"] = str(parent / control.unit)
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            current = self.store.get(task["id"])
            if current["state"] != "QUEUED": return
            option = self.store.db.execute("SELECT timeout FROM scheduler_options WHERE task_id=?", (task["id"],)).fetchone()
            timeout = option[0] if option else self.settings["task_timeout"]
            self.store.db.execute("INSERT OR IGNORE INTO scheduler_options VALUES(?,?)", (task["id"], timeout))
            self.store.db.execute("INSERT INTO scheduler_dispatches VALUES(?,?,?,'RESERVED',NULL,NULL,?,NULL,?,NULL)",
                (task["id"], token, json.dumps(self.owner), json.dumps(control.descriptor), time.time()))
        argv = (self.worker_factory(task, token, timeout) if self.worker_factory else
            [sys.executable, "-B", str(Path(__file__).with_name("runner.py")), "--runtime", str(self.root),
             "--codex", self.runner.binary, *(["--development-poc"] if self.runner.development else []), "run", task["id"], "--timeout", str(timeout), "--dispatch-token", token])
        env = {"PATH": str(Path(self.runner.binary).parent) + ":" + os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
        try:
            child = Process(argv, cwd=self.root, env=env, raw_output=True, resources=control)
        except (OSError, RuntimeError) as exc:
            self.store.transition(task["id"], "QUEUED", "UNKNOWN", "Worker launch/handoff failed; no automatic replay")
            self.event("launch_failed", task_id=task["id"], error=type(exc).__name__)
            raise
        self.children[task["id"]] = child
        self.observed(task["id"], child.seen)
        self.event("dispatched", task_id=task["id"])

    def reap(self):
        for task_id, child in list(self.children.items()):
            resource_error = False
            if not child.closed:
                try: child.next_frame(.001)
                except EOFError: pass
                except ResourceExceeded: resource_error = True
            if child.proc.poll() is None and not resource_error: continue
            cleanup = child.close(.1)
            # Stdout contains task data, not daemon diagnostics; never duplicate it.
            cleanup.pop("stdout_redacted", None)
            with self.store.db:
                self.store.db.execute("UPDATE scheduler_dispatches SET phase='FINISHED',cleanup=?,finished=? WHERE task_id=?",
                                      (json.dumps(cleanup), time.time(), task_id))
            recover_abandoned(self.root, self.store)
            task = self.store.get(task_id)
            if not cleanup["resources"]["released"] and task["state"] != "UNKNOWN":
                self.store.transition(task_id, task["state"], "UNKNOWN", "Worker resource scope did not release")
            elif task["state"] == "QUEUED":
                self.store.transition(task_id, "QUEUED", "FAILED", "Worker ended before execution claim")
            elif task["state"] == "COMPLETED" and (resource_error or cleanup["exit_code"] != 0 or cleanup["resources"]["exceeded"]
                    or not cleanup["resources"]["released"] or not cleanup["resources"]["confirmed"]):
                self.store.transition(task_id, "COMPLETED", "UNKNOWN", "Worker resource supervision failed after task checkpoint")
            self.event("worker_exit", task_id=task_id, exit_code=cleanup["exit_code"])
            del self.children[task_id]

    def tick(self):
        for task_id, child in self.children.items():
            if not child.closed:
                child.observe_children(persist=False)
                self.observed(task_id, child.seen)
        self.reap()
        self.pending_recovery = scheduler.recover_dispatches(self.runner)
        blocked, protected_workspaces = scheduler.blocked_unknown(self.runner)
        self.blocked = (["recovery_pending"] if self.pending_recovery else []) + (["unreleased_unknown"] if blocked else [])
        for row in self.store.db.execute("SELECT id FROM tasks WHERE state='UNKNOWN'").fetchall():
            if row[0] in self.reconciled_unknowns or row[0] in blocked: continue
            try:
                facts = self.runner.reconcile(row[0], offline=True)
                if facts["resources"]["released"]: self.reconciled_unknowns.add(row[0])
                else: self.blocked.append("unreconciled_unknown")
            except (ValueError, RuntimeError): self.blocked.append("unreconciled_unknown")
        try: storage_check(self.root, self.runner.resource_limits)
        except ResourceExceeded: self.blocked.append("storage_budget")
        if self.pipeline is not None:
            try: self.pipeline.pump()
            except (OSError, ValueError, RuntimeError, sqlite3.Error): self.blocked.append('pipeline_operation_unknown')
            if self.pipeline.held: return
        else:
            from codex_host import pipeline_held
            if pipeline_held(self.root):
                self.blocked.append('pipeline_recovery_pending'); return
        if self.stopping or self.blocked or self.status()["paused"]: return
        active = [dict(r) for r in self.store.db.execute("SELECT * FROM tasks WHERE state IN ('RUNNING','VERIFYING')")]
        if any(t["id"] not in self.children for t in active):
            self.blocked.append("external_active_owner"); return
        if any(not self.store.get(t)["workspace_id"] for t in self.children): return
        rows = self.store.db.execute("""SELECT t.* FROM tasks t WHERE state='QUEUED' AND NOT EXISTS
            (SELECT 1 FROM scheduler_dispatches d WHERE d.task_id=t.id) ORDER BY t.rowid LIMIT 2""").fetchall()
        for row in rows:
            if len(self.children) >= 2: break
            task = dict(row)
            if task["workspace_id"] in protected_workspaces:
                approval = self.store.db.execute("""SELECT 1 FROM decisions d JOIN tasks s ON s.id=d.task_id
                    WHERE d.id=? AND d.kind='allow_resume' AND d.task_id=? AND d.execution_id=s.active_execution_id""",
                    (task["resume_approval"], task["resume_of"])).fetchone()
                if not approval:
                    self.blocked.append("unknown_workspace"); break
            if not task["workspace_id"] and self.children: break
            if task["workspace_id"] and any(self.store.get(t)["workspace_id"] == task["workspace_id"] for t in self.children): break
            self.start_worker(task)
            if not task["workspace_id"]: break

    def close_client(self, connection):
        self.selector.unregister(connection)
        self.clients.pop(connection, None)
        connection.close()

    def io(self, key, mask):
        if key.data is None:
            for _ in range(32):
                try: conn, _ = self.listener.accept()
                except BlockingIOError: break
                if len(self.clients) >= 64 or peer_uid(conn) != os.getuid(): conn.close(); continue
                conn.setblocking(False)
                data = {"input": bytearray(), "output": None, "deadline": time.monotonic() + 3}
                self.clients[conn] = data
                self.selector.register(conn, selectors.EVENT_READ, data)
            return
        conn, data = key.fileobj, key.data
        if mask & selectors.EVENT_READ:
            chunk = conn.recv(65536)
            if not chunk: self.close_client(conn); return
            data["input"].extend(chunk)
            if len(data["input"]) > MAX_REQUEST: self.close_client(conn); return
            if b"\n" not in data["input"]: return
            identifier = None
            try:
                request = json.loads(data["input"].split(b"\n", 1)[0])
                if not isinstance(request, dict) or set(request) != {"id", "method", "params"}: raise ValueError("Invalid request frame")
                identifier = request["id"]
                if not isinstance(identifier, str) or len(identifier) > 128: raise ValueError("Invalid request ID")
                reply = {"id": identifier, "result": self.handle(request["method"], request["params"])}
            except (ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as exc:
                reply = {"id": identifier, "error": redact(str(exc))}
            encoded = json.dumps(redact(reply), ensure_ascii=False).encode() + b"\n"
            if len(encoded) > MAX_RESPONSE: encoded = json.dumps({"id": identifier, "error": "Response too large; use local show"}).encode() + b"\n"
            data["output"], data["deadline"] = encoded, time.monotonic() + 3
            self.selector.modify(conn, selectors.EVENT_WRITE, data)
        if mask & selectors.EVENT_WRITE:
            count = conn.send(data["output"])
            data["output"] = data["output"][count:]
            if not data["output"]: self.close_client(conn)

    def stop(self):
        self.stopping = True
        if self.pipeline is not None:
            try: self.pipeline.close()
            except (OSError, ValueError, RuntimeError, sqlite3.Error): self.exit_code = 1
        for task_id in self.children: self.store.cancel(task_id)
        # Let the runner send turn/interrupt and persist its result before
        # terminating its scope supervisor. Unacknowledged stops remain UNKNOWN.
        deadline = time.monotonic() + self.settings["shutdown_seconds"]
        while self.children and time.monotonic() < deadline:
            self.reap(); time.sleep(.02)
        for task_id, child in self.children.items():
            cleanup = child.close(.1)
            cleanup.pop("stdout_redacted", None)
            with self.store.db:
                self.store.db.execute("UPDATE scheduler_dispatches SET phase='FINISHED',cleanup=?,finished=? WHERE task_id=?",
                                      (json.dumps(cleanup), time.time(), task_id))
            self.event("stop_escalated", task_id=task_id, exit_code=cleanup["exit_code"])
        self.children.clear()
        recover_abandoned(self.root, self.store)

    def run(self):
        handlers = {sig: signal.signal(sig, lambda *_: setattr(self, "stopping", True)) for sig in [signal.SIGTERM, signal.SIGINT]}
        try:
            if self.allow_codex_pipeline:
                from codex_host import HostGateway
                if self.pipeline_factory is not None:
                    self.pipeline = self.pipeline_factory(
                        self.root, self.runner.binary,
                        Path(__file__).resolve().parents[1], self.ordinary_busy)
                else:
                    from notification_runtime import (EVENTS_ENABLE_ENV,
                                                      build_systemd_notification_runtime)
                    principals = None
                    if os.environ.get(EVENTS_ENABLE_ENV) == '1':
                        try:
                            from oauth_auth import OAuthJWTVerifier
                            principals = OAuthJWTVerifier.from_environment().principals
                        except (ValueError, OSError):
                            # The worker has no inbound token; it receives only the
                            # configured principal allowlist and stays off on failure.
                            principals = None
                    self.pipeline = HostGateway(
                        self.root, self.runner.binary,
                        Path(__file__).resolve().parents[1], self.ordinary_busy,
                        notification_runtime=build_systemd_notification_runtime(
                            self.root, events_transport=True,
                            authenticated_principals=principals))
            self.pending_recovery = scheduler.recover_dispatches(self.runner)
            # Reconcile retained UNKNOWN evidence before accepting new dispatch.
            for row in self.store.db.execute("SELECT id FROM tasks WHERE state='UNKNOWN'").fetchall():
                try:
                    facts = self.runner.reconcile(row[0], offline=True)
                    if facts["resources"]["released"]: self.reconciled_unknowns.add(row[0])
                except (ValueError, RuntimeError): pass  # Explicit blocker is reported by tick.
            self.bind()
            self.event("started", pid=os.getpid())
            while not self.stopping:
                for key, mask in self.selector.select(self.settings["poll_seconds"]):
                    try: self.io(key, mask)
                    except (OSError, EOFError):
                        if key.data is not None and key.fileobj in self.clients: self.close_client(key.fileobj)
                for conn, data in list(self.clients.items()):
                    if time.monotonic() >= data["deadline"]: self.close_client(conn)
                try: self.tick()
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower(): raise
                    self.blocked = ["sqlite_locked"]
            # Flush small shutdown acknowledgements without allowing more dispatch.
            until = time.monotonic() + .5
            while any(c["output"] for c in self.clients.values()) and time.monotonic() < until:
                for key, mask in self.selector.select(.02):
                    if key.data is not None and key.data["output"]:
                        try: self.io(key, mask)
                        except OSError: self.close_client(key.fileobj)
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            self.exit_code = 1
            raise
        finally:
            try: self.stop()
            finally:
                for connection in list(self.clients): self.close_client(connection)
                if self.listener: self.listener.close()
                if self.socket_path and self.socket_path.exists() and self.socket_path.lstat().st_ino == self.socket_inode:
                    self.socket_path.unlink()
                self.selector.close(); self.events_stream.close(); self.store.close(); self.lease.close()
                for sig, handler in handlers.items(): signal.signal(sig, handler)
        return self.exit_code


def foreground(root, binary=None, serve_argv=None, *, development=False, allow_codex_pipeline=False):
    settings = (scheduler.configuration(codex_pipeline=True) if allow_codex_pipeline
                else scheduler.configuration())
    from isolation import prepare
    development_poc.configure(root, development)
    root, binary = prepare(root, binary)
    argv = serve_argv or [sys.executable, "-B", str(Path(__file__)), "--serve", "--runtime", str(root), "--codex", binary,
                         *(["--development-poc"] if development else []), *(['--allow-codex-pipeline'] if allow_codex_pipeline else [])]
    control = Control(root, settings["daemon"], scheduler_storage=True)
    child = Process(argv, cwd=root, env={"PATH": str(Path(binary).parent) + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
                    "LANG": "C.UTF-8", "HOME": str(Path.home())}, raw_output=True, resources=control)
    stop = False
    def signal_stop(*_):
        nonlocal stop
        stop = True
    handlers = {sig: signal.signal(sig, signal_stop) for sig in [signal.SIGINT, signal.SIGTERM]}
    stop_requested = False
    try:
        while child.proc.poll() is None:
            if stop and not stop_requested:
                from control_socket import request
                try: request(root, "shutdown", timeout=2)
                except (OSError, ValueError, RuntimeError, EOFError): child.proc.terminate()
                stop_requested = True
            try: child.next_frame(.1)
            except EOFError: time.sleep(.02)
            except ResourceExceeded:
                from control_socket import request
                try: request(root, "shutdown", timeout=2)
                except (OSError, ValueError, RuntimeError, EOFError): child.proc.terminate()
                break
    finally:
        result = child.close(grace=40)
        (root / "logs" / ("daemon-controller-" + control.unit + ".json")).write_text(json.dumps(result))
        for sig, handler in handlers.items(): signal.signal(sig, handler)
    return 0 if result["exit_code"] == 0 and result["resources"]["confirmed"] and not result["resources"]["exceeded"] and result["resources"]["released"] else 1


def verify_self_limits(*, allow_codex_pipeline=False):
    settings = (scheduler.configuration(codex_pipeline=True) if allow_codex_pipeline
                else scheduler.configuration())
    values = settings["daemon"]
    from resources import counters
    rel = Path('/proc/self/cgroup').read_text().strip().split('::')[1]
    group = Path('/sys/fs/cgroup') / rel.lstrip('/')
    expected = {"cpu.max": str(values["cpu_percent"]*1000)+" 100000", "memory.max": str(values["memory_bytes"]),
                "pids.max": str(values["pids"]), "memory.swap.max": "0"}
    if any((group / name).read_text().strip() != value for name, value in expected.items()):
        raise RuntimeError("Daemon must be started with its verified cgroup limits")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--development-poc", action="store_true")
    parser.add_argument("--serve", action="store_true", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--codex")
    parser.add_argument('--allow-codex-pipeline', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    verify_self_limits(allow_codex_pipeline=args.allow_codex_pipeline)
    raise SystemExit(Daemon(args.runtime, args.codex, development=args.development_poc, allow_codex_pipeline=args.allow_codex_pipeline).run())
