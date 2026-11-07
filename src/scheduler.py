"""One persistent FIFO dispatcher; task execution remains in the existing runner."""

from contextlib import nullcontext
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from concurrency import lease, recover_abandoned
from resources import policy, scope_released
from storage import _backup, runtime_lock
from transport import proc_identity

CONFIG = Path(__file__).resolve().parents[1] / "scheduler-config.json"


def configuration(*, codex_pipeline=False):
    if type(codex_pipeline) is not bool:
        raise ValueError("Pipeline budget selection must be boolean")
    if CONFIG.is_symlink():
        raise ValueError("Scheduler configuration must not be a symlink")
    value = json.loads(CONFIG.read_text())
    required = {"poll_seconds", "task_timeout", "shutdown_seconds", "daemon", "worker"}
    optional = {"pipeline_daemon_memory_bytes", "pipeline_daemon_pids", "pipeline_daemon_file_bytes"}
    if not required <= set(value) or set(value) - required - optional:
        raise ValueError("Unknown scheduler configuration")
    for name, maximum in [("poll_seconds", 5), ("task_timeout", 3600), ("shutdown_seconds", 30)]:
        v = value[name]
        if type(v) not in [float, int] or not math.isfinite(v) or not 0 < v <= maximum:
            raise ValueError("Invalid scheduler timing")
    for role in ["daemon", "worker"]:
        if not isinstance(value[role], dict) or set(value[role]) != {"cpu_percent", "cpu_budget_ms", "memory_bytes", "pids"}:
            raise ValueError("Invalid controller resource policy")
        value[role] = policy({**policy(), **value[role]})
    pipeline_memory = value.get("pipeline_daemon_memory_bytes")
    if "pipeline_daemon_memory_bytes" in value:
        if type(pipeline_memory) is not int or pipeline_memory < value["daemon"]["memory_bytes"]:
            raise ValueError("Pipeline memory must be a finite integer at least the base daemon limit")
        policy({**value["daemon"], "memory_bytes": pipeline_memory})
    pipeline_pids = value.get("pipeline_daemon_pids")
    if "pipeline_daemon_pids" in value:
        if type(pipeline_pids) is not int or not value["daemon"]["pids"] <= pipeline_pids <= 3072:
            raise ValueError("Pipeline task budget must be a finite integer between the base limit and 3072")
    pipeline_file = value.get("pipeline_daemon_file_bytes", value["daemon"]["file_bytes"])
    if type(pipeline_file) is not int or not value["daemon"]["file_bytes"] <= pipeline_file <= 256*1024**2:
        raise ValueError("Pipeline file ceiling must be finite between the base limit and256MiB")
    if codex_pipeline:
        if pipeline_memory is None or pipeline_pids is None:
            raise ValueError("Pipeline memory and task budgets must be explicitly configured")
        value["daemon"]["memory_bytes"] = pipeline_memory
        value["daemon"]["pids"] = pipeline_pids
        value["daemon"]["file_bytes"] = pipeline_file
    return value


def initialize(store):
    db = store.db
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_state'").fetchone()
    if exists:
        if db.execute("SELECT schema_version FROM scheduler_state WHERE id=1").fetchone()[0] != 1:
            raise ValueError("Unsupported scheduler schema")
        return
    with runtime_lock(store.path.parent):
        _backup(db, store.path, "before-scheduler-v1")
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE scheduler_state (
                id INTEGER PRIMARY KEY CHECK(id=1), schema_version INTEGER NOT NULL,
                paused INTEGER NOT NULL, updated REAL NOT NULL)""")
            db.execute("INSERT INTO scheduler_state VALUES(1,1,0,?)", (time.time(),))
            db.execute("""CREATE TABLE scheduler_options (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id), timeout REAL NOT NULL)""")
            db.execute("""CREATE TABLE scheduler_dispatches (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id), token TEXT NOT NULL UNIQUE,
                owner TEXT NOT NULL, phase TEXT NOT NULL, processes TEXT,
                worker TEXT, resources TEXT NOT NULL, cleanup TEXT,
                created REAL NOT NULL, finished REAL)""")


def owner_identity():
    return {"boot_id": Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            "process": proc_identity(os.getpid())}


def live(identity):
    if identity["boot_id"] != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
        return False
    old = identity["process"]
    now = proc_identity(old["pid"])
    return bool(now and now["start"] == old["start"] and now["state"] not in ["Z", "X"])


def execution_admission(root, store, task_id, token=None):
    if token is None:
        return lease(Path(root) / "daemon.lock", shared=True)
    deadline = time.monotonic() + 5
    while True:
        row = store.db.execute("SELECT * FROM scheduler_dispatches WHERE task_id=? AND token=?", (task_id, token)).fetchone()
        if not row or not live(json.loads(row["owner"])):
            raise ValueError("Dispatch owner/token is invalid or no longer alive")
        if row["phase"] == "STARTED":
            break
        if row["phase"] != "RESERVED" or time.monotonic() >= deadline:
            raise ValueError("Dispatch handoff was not authorized")
        time.sleep(.02)
    ancestors = {}
    pid = os.getpid()
    while pid > 1 and pid not in ancestors:
        actual = proc_identity(pid)
        if not actual: break
        ancestors[pid] = actual["start"]
        pid = actual["ppid"]
    recorded = json.loads(row["processes"] or "{}")
    if not any(ancestors.get(int(pid)) == start for pid, start in recorded.items()):
        raise ValueError("Worker is not descended from the reserved process")
    with store.db:
        cur = store.db.execute("""UPDATE scheduler_dispatches SET phase='AUTHORIZED',worker=?
            WHERE task_id=? AND token=? AND phase='STARTED' AND worker IS NULL""",
            (json.dumps(owner_identity()), task_id, token))
        if cur.rowcount != 1:
            raise ValueError("Dispatch token was already consumed")
    return nullcontext()



def worker_scope_clear(descriptor, owner):
    if scope_released(descriptor): return True
    # A durable prospective path closes the pre-receipt launch gap only after
    # the owning daemon is dead; the guarded worker cannot start later.
    expected = descriptor.get("expected_cgroup")
    if expected and not live(owner):
        relative = Path('/proc/self/cgroup').read_text().strip().split('::')[1]
        own_parent = (Path('/sys/fs/cgroup') / relative.lstrip('/')).parent
        candidate = Path(expected).resolve()
        return (candidate.parent == own_parent and own_parent.name == 'agentorchestrator.slice'
                and candidate.name == descriptor["unit"] and not candidate.exists())
    return False

def recover_dispatches(runner):
    """Reconcile old reservations; never dispatch or infer successful execution."""
    recover_abandoned(runner.root, runner.store)
    db = runner.store.db
    pending = []
    for row in db.execute("SELECT * FROM scheduler_dispatches WHERE phase IN ('RESERVED','STARTED','AUTHORIZED')").fetchall():
        if live(json.loads(row["owner"])):
            continue
        processes = json.loads(row["processes"] or "{}")
        busy = any((now := proc_identity(int(pid))) and now["start"] == start and now["state"] not in ["Z", "X"]
                   for pid, start in processes.items())
        descriptor = json.loads(row["resources"])
        scope_clear = worker_scope_clear(descriptor, json.loads(row["owner"]))
        if busy or not scope_clear:
            task = runner.store.get(row["task_id"])
            if task["state"] == "QUEUED":
                runner.store.transition(task["id"], "QUEUED", "UNKNOWN", "Lost dispatch; worker release is unconfirmed")
            pending.append(row["task_id"])
            continue
        task = runner.store.get(row["task_id"])
        if task["state"] == "QUEUED":
            runner.store.transition(task["id"], "QUEUED", "UNKNOWN", "Lost durable dispatch reservation; never replayed")
            if scope_clear:
                runner.store.update(task["id"], cleanup=json.dumps({"survivors": [], "scheduler": "No execution claim; lost handoff closed"}))
        elif task["state"] in ["RUNNING", "VERIFYING"]:
            pending.append(task["id"])
            continue
        elif task["state"] == "COMPLETED":
            try:
                receipt = json.loads(Path(descriptor["receipt"]).read_text())
                confirmed = receipt["state"] == "FINISHED" and receipt["exit_code"] == 0 and not receipt.get("violation")
            except (OSError, ValueError, KeyError): confirmed = False
            if not confirmed:
                runner.store.transition(task["id"], "COMPLETED", "UNKNOWN", "Worker supervision lost after task checkpoint")
        with db:
            db.execute("UPDATE scheduler_dispatches SET phase='ORPHANED',finished=? WHERE task_id=?", (time.time(), task["id"]))
    return pending


def blocked_unknown(runner):
    from recovery import resource_facts
    blocked, workspaces = [], set()
    for row in runner.store.db.execute("SELECT * FROM tasks WHERE state='UNKNOWN'"):
        task = dict(row)
        execution = runner.store.execution(task["active_execution_id"]) if task["active_execution_id"] else None
        dispatch = runner.store.db.execute("SELECT resources,owner FROM scheduler_dispatches WHERE task_id=?", (task["id"],)).fetchone()
        worker_clear = not dispatch or worker_scope_clear(json.loads(dispatch[0]), json.loads(dispatch[1]))
        if not resource_facts(execution, task)["released"] or not worker_clear or not task["workspace_id"]:
            blocked.append(task["id"])
        if task["workspace_id"]: workspaces.add(task["workspace_id"])
    return blocked, workspaces
