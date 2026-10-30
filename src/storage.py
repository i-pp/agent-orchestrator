"""Owned task database, execution identities, and transactional Phase 1 migration."""

import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import closing

from transport import redact

STATES = ("QUEUED", "RUNNING", "VERIFYING", "COMPLETED", "FAILED", "TIMED_OUT",
          "CANCELLED", "UNKNOWN", "WAITING_FOR_HUMAN")
LEGACY_COLUMNS = ("id", "request_key", "prompt", "resume_of", "state", "session_id", "turn_id",
                  "result", "error", "usage", "binary", "version", "log_path", "cleanup",
                  "created", "updated", "cancel_requested")
TASK_SQL = """CREATE TABLE {name} (
    id TEXT PRIMARY KEY, request_key TEXT UNIQUE, prompt TEXT NOT NULL,
    resume_of TEXT REFERENCES tasks(id), state TEXT NOT NULL
    CHECK(state IN ('QUEUED','RUNNING','VERIFYING','COMPLETED','FAILED','TIMED_OUT','CANCELLED','UNKNOWN','WAITING_FOR_HUMAN')),
    session_id TEXT, turn_id TEXT, result TEXT, error TEXT, usage TEXT,
    binary TEXT, version TEXT, log_path TEXT, cleanup TEXT,
    created REAL NOT NULL, updated REAL NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
    acceptance TEXT, active_execution_id TEXT, codex_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
    verification_status TEXT NOT NULL DEFAULT 'NOT_VERIFIED',
    human_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED', resume_approval TEXT UNIQUE
)"""
EXTRA_SQL = [
    """CREATE TABLE executions (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
        thread_id TEXT, turn_id TEXT, phase TEXT NOT NULL, owner TEXT NOT NULL,
        processes TEXT, workspace_before TEXT, terminal TEXT, codex_status TEXT NOT NULL,
        dispatched INTEGER NOT NULL DEFAULT 0, launch_pending INTEGER NOT NULL DEFAULT 0,
        agent_outcome TEXT, cleanup TEXT, started REAL NOT NULL,
        updated REAL NOT NULL, finished REAL
    )""",
    """CREATE TABLE reconciliations (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
        execution_id TEXT REFERENCES executions(id), facts TEXT NOT NULL, created REAL NOT NULL
    )""",
    """CREATE TABLE verifications (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
        execution_id TEXT REFERENCES executions(id), status TEXT NOT NULL,
        evidence TEXT NOT NULL, workspace_fingerprint TEXT NOT NULL, created REAL NOT NULL
    )""",
    """CREATE TABLE decisions (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
        execution_id TEXT REFERENCES executions(id), kind TEXT NOT NULL,
        actor TEXT NOT NULL, reason TEXT NOT NULL, reconciliation_id TEXT REFERENCES reconciliations(id),
        created REAL NOT NULL
    )""",
]


def runtime_lock(root):
    fd = (Path(root) / "runner.lock").open("a")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        raise RuntimeError("Another task runner owns this runtime") from None
    return fd


def _backup(db, path, label):
    directory = path.parent / "db-backups"
    if directory.is_symlink():
        raise ValueError("Backup directory must not be a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    backup = directory / f"{label}-{uuid.uuid4()}.sqlite"
    with closing(sqlite3.connect(backup)) as target:
        db.backup(target)
    return backup


def migrate(db, path):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version in [2, 3]:
        return
    if version != 0:
        raise ValueError(f"Unsupported task database version: {version}")
    with runtime_lock(path.parent):
        if db.execute("PRAGMA user_version").fetchone()[0] == 2:
            return
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()
        backup = None
        if exists:
            columns = tuple(r[1] for r in db.execute("PRAGMA table_info(tasks)"))
            if columns != LEGACY_COLUMNS:
                raise ValueError("Unrecognized legacy task schema; migration refused")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1").fetchone():
                raise ValueError("Custom legacy triggers/views require an explicit migration")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND tbl_name='tasks' AND sql IS NOT NULL LIMIT 1").fetchone():
                raise ValueError("Custom legacy task indexes require an explicit migration")
            backup = _backup(db, path, "phase1")
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            db.execute("BEGIN IMMEDIATE")
            if exists:
                db.execute(TASK_SQL.format(name="tasks_new"))
                names = ",".join(LEGACY_COLUMNS)
                db.execute(f"INSERT INTO tasks_new({names}) SELECT {names} FROM tasks")
                db.execute("UPDATE tasks_new SET codex_status='LEGACY_UNVERIFIED', verification_status='LEGACY_UNVERIFIED'")
                db.execute("DROP TABLE tasks")
                db.execute("ALTER TABLE tasks_new RENAME TO tasks")
                db.execute("ALTER TABLE transitions ADD COLUMN execution_id TEXT")
            else:
                db.execute(TASK_SQL.format(name="tasks"))
                db.execute("""CREATE TABLE transitions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
                    old_state TEXT, new_state TEXT NOT NULL, at REAL NOT NULL, reason TEXT, execution_id TEXT
                )""")
            for statement in EXTRA_SQL:
                db.execute(statement)
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("Foreign key validation failed during migration")
            db.execute("PRAGMA user_version=2")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys=ON")
        if backup:
            note = {"backup": str(backup), "sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                    "from_version": "phase1/user_version=0", "to_version": 2, "at": time.time()}
            backup.with_suffix(".json").write_text(json.dumps(note, indent=2))



def migrate_workspaces(db, path):
    if db.execute("PRAGMA user_version").fetchone()[0] == 3:
        return
    with runtime_lock(path.parent):
        if db.execute("PRAGMA user_version").fetchone()[0] == 3:
            return
        backup = _backup(db, path, "phase2-ab")
        try:
            db.execute("BEGIN IMMEDIATE")
            for table in ["tasks", "executions"]:
                for name, declaration in [("workspace_id", "TEXT"), ("role", "TEXT NOT NULL DEFAULT 'implementer'"),
                                          ("access", "TEXT NOT NULL DEFAULT 'read-only'"), ("snapshot_id", "TEXT")]:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            db.execute("ALTER TABLE tasks ADD COLUMN workspace_request TEXT")
            db.execute("""CREATE TABLE workspaces (
                id TEXT PRIMARY KEY, owner_task_id TEXT NOT NULL, access TEXT NOT NULL,
                state TEXT NOT NULL, snapshot_id TEXT, initial_fingerprint TEXT NOT NULL,
                created REAL NOT NULL, released REAL)""")
            db.execute("""CREATE TABLE snapshots (
                id TEXT PRIMARY KEY, source_task_id TEXT NOT NULL REFERENCES tasks(id),
                source_execution_id TEXT NOT NULL REFERENCES executions(id),
                workspace_id TEXT NOT NULL REFERENCES workspaces(id), fingerprint TEXT NOT NULL, created REAL NOT NULL)""")
            db.execute("PRAGMA user_version=3")
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("Workspace migration foreign key validation failed")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        backup.with_suffix(".json").write_text(json.dumps({"backup": str(backup), "from_version": 2,
            "to_version": 3, "sha256": hashlib.sha256(backup.read_bytes()).hexdigest(), "at": time.time()}, indent=2))


def rollback_database(path, backup_name):
    """Restore only if no Phase 2 data or changes would be discarded."""
    path = Path(path).resolve()
    backup = path.parent / "db-backups" / backup_name
    if not path.is_file() or backup.parent.is_symlink() or Path(backup_name).name != backup_name or backup.is_symlink() or not backup.is_file():
        raise ValueError("Select an existing backup filename inside db-backups")
    from concurrency import lease
    with lease(path.parent / "daemon.lock", shared=True), runtime_lock(path.parent), closing(sqlite3.connect(path)) as current, closing(sqlite3.connect(
        backup.as_uri() + "?mode=ro", uri=True
    )) as old:
        if current.execute("PRAGMA user_version").fetchone()[0] not in [2, 3]:
            raise ValueError("Rollback requires a Phase 2 task database")
        if current.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_state'").fetchone():
            raise ValueError("Scheduler metadata exists; rollback would discard persistent queue evidence")
        old_version = old.execute("PRAGMA user_version").fetchone()[0]
        if old_version not in [0, 2]:
            raise ValueError("Backup is not a supported pre-migration database")
        if current.execute("PRAGMA user_version").fetchone()[0] == 3:
            for table in ["workspaces", "snapshots"]:
                if current.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                    raise ValueError("Workspace records exist; destructive rollback refused")
        if old_version == 2:
            tables = [r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                columns = ",".join('"' + r[1] + '"' for r in old.execute(f'PRAGMA table_info("{table}")'))
                sql = f'SELECT {columns} FROM "{table}" ORDER BY rowid'
                if current.execute(sql).fetchall() != old.execute(sql).fetchall():
                    raise ValueError("Records changed; destructive rollback refused")
            archive = _backup(current, path, "before-rollback-v3")
            old.backup(current)
            return {"restored": str(backup), "phase2_archive": str(archive)}
        for table in ["executions", "reconciliations", "verifications", "decisions"]:
            if current.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                raise ValueError("Phase 2 records exist; destructive rollback refused")
        for table, columns, order in [
            ("tasks", ",".join(LEGACY_COLUMNS), "id"),
            ("transitions", "seq,task_id,old_state,new_state,at,reason", "seq"),
        ]:
            sql = f"SELECT {columns} FROM {table} ORDER BY {order}"
            if current.execute(sql).fetchall() != old.execute(sql).fetchall():
                raise ValueError("Legacy records changed; destructive rollback refused")
        archive = _backup(current, path, "before-rollback-v2")
        current.execute("PRAGMA foreign_keys=OFF")
        old.backup(current)
        if current.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Restored database failed integrity check")
        return {"restored": str(backup), "phase2_archive": str(archive)}


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            migrate(self.db, self.path)
            migrate_workspaces(self.db, self.path)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA synchronous=FULL")
        except BaseException:
            self.db.close()
            raise

    def get(self, task_id):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("Task not found")
        result = dict(row)
        result["completion_verified"] = (result["state"] == "COMPLETED" and result["codex_status"] == "SUCCEEDED"
                                         and result["verification_status"] == "PASSED"
                                         and result["human_status"] in ["NOT_REQUIRED", "APPROVED"])
        return result

    def execution(self, execution_id):
        row = self.db.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
        if row is None:
            raise ValueError("Execution not found")
        return dict(row)

    def submit(self, prompt, *, resume_of=None, request_key=None, acceptance=None, resume_approval=None, workspace=None, schedule_timeout=None):
        if schedule_timeout is not None and (type(schedule_timeout) not in [int, float] or not 0 < schedule_timeout <= 3600):
            raise ValueError("Invalid scheduled task timeout")
        def save_timeout(task_id):
            if schedule_timeout is None: return
            old_timeout = self.db.execute("SELECT timeout FROM scheduler_options WHERE task_id=?", (task_id,)).fetchone()
            if old_timeout and old_timeout[0] != schedule_timeout:
                raise ValueError("request_key already has a different timeout")
            self.db.execute("INSERT OR IGNORE INTO scheduler_options VALUES(?,?)", (task_id, schedule_timeout))
        from verification import normalize_acceptance
        acceptance = normalize_acceptance(acceptance)
        policy = json.dumps(acceptance, sort_keys=True, ensure_ascii=False)
        from workspaces import spec, allocate
        if workspace is not None:
            workspace = spec(**workspace)
        if resume_of:
            source = self.get(resume_of)
            if source["workspace_id"]:
                inherited = spec(**json.loads(source["workspace_request"]))
                if workspace is not None and workspace != inherited:
                    raise ValueError("Resume cannot change workspace policy/role")
                workspace = inherited
        workspace_request = json.dumps(workspace, sort_keys=True) if workspace is not None else None
        if workspace and workspace["access"] == "workspace-write":
            if not acceptance["tests"] or not acceptance["artifacts"] or any("sha256" not in a for a in acceptance["artifacts"]):
                raise ValueError("Write tasks require content hashes and approved tests")
        prompt = redact(prompt)
        if not prompt.strip():
            raise ValueError("Prompt must not be empty")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if request_key:
                old = self.db.execute("SELECT * FROM tasks WHERE request_key=?", (request_key,)).fetchone()
                if old:
                    if (old["prompt"], old["resume_of"], old["acceptance"], old["resume_approval"], old["workspace_request"]) != (
                        prompt, resume_of, policy, resume_approval, workspace_request
                    ):
                        raise ValueError("Request key already belongs to different task inputs")
                    save_timeout(old["id"])
                    return old["id"]
            if resume_of:
                old = self.get(resume_of)
                if old["workspace_id"]:
                    owned = self.db.execute("SELECT state FROM workspaces WHERE id=?", (old["workspace_id"],)).fetchone()
                    if not owned or owned["state"] != "AVAILABLE":
                        raise ValueError("Resume workspace is unavailable or released")
                if not old["session_id"] or old["state"] in ["QUEUED", "RUNNING", "VERIFYING"]:
                    raise ValueError("Resume requires an owned stopped session")
                if old["state"] in ["UNKNOWN", "WAITING_FOR_HUMAN"]:
                    if not resume_approval:
                        raise ValueError("UNKNOWN/reviewed resume needs an explicit recovery decision")
                if resume_approval:
                    decision = self.db.execute("SELECT * FROM decisions WHERE id=? AND task_id=? AND kind='allow_resume'",
                                               (resume_approval, resume_of)).fetchone()
                    if not decision or decision["execution_id"] != old["active_execution_id"]:
                        raise ValueError("UNKNOWN/reviewed resume needs an explicit current recovery decision")
            elif resume_approval:
                raise ValueError("Resume approval requires resume_of")
            task_id = str(uuid.uuid4())
            now = time.time()
            self.db.execute("""INSERT INTO tasks(id,request_key,prompt,resume_of,state,created,updated,acceptance,resume_approval)
                               VALUES(?,?,?,?,?,?,?,?,?)""",
                            (task_id, request_key, prompt, resume_of, "QUEUED", now, now, policy, resume_approval))
            if workspace:
                wid = self.get(resume_of)["workspace_id"] if resume_of else allocate(self, task_id, workspace)
                self.db.execute("UPDATE tasks SET workspace_id=?,role=?,access=?,snapshot_id=?,workspace_request=? WHERE id=?",
                                (wid, workspace["role"], workspace["access"], workspace["snapshot_id"], workspace_request, task_id))
            save_timeout(task_id)
            self._transition_log(task_id, None, "QUEUED", "submitted")
        return task_id

    def _transition_log(self, task_id, old, new, reason, execution_id=None):
        self.db.execute("""INSERT INTO transitions(task_id,old_state,new_state,at,reason,execution_id)
                           VALUES(?,?,?,?,?,?)""",
                        (task_id, old, new, time.time(), redact(reason), execution_id))

    def transition(self, task_id, old, new, reason):
        if new not in STATES:
            raise ValueError("Invalid task state")
        with self.db:
            cur = self.db.execute("UPDATE tasks SET state=?,updated=? WHERE id=? AND state=?",
                                  (new, time.time(), task_id, old))
            if cur.rowcount != 1:
                raise ValueError(f"Task is not {old}; automatic re-execution is forbidden")
            self._transition_log(task_id, old, new, reason, self.get(task_id)["active_execution_id"])

    def update(self, task_id, **values):
        allowed = set(LEGACY_COLUMNS) - {"id", "prompt", "resume_of", "state", "created", "request_key"}
        allowed |= {"codex_status", "verification_status", "human_status"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid task update fields")
        values = {k: redact(v) for k, v in values.items()}
        values["updated"] = time.time()
        with self.db:
            self.db.execute("UPDATE tasks SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                            (*values.values(), task_id))

    def execution_update(self, execution_id, **values):
        allowed = {"thread_id", "turn_id", "phase", "processes", "workspace_before", "terminal",
                   "codex_status", "dispatched", "launch_pending", "agent_outcome", "cleanup", "finished"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid execution update fields")
        values["updated"] = time.time()
        with self.db:
            self.db.execute("UPDATE executions SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                            (*values.values(), execution_id))

    def claim(self, task_id, owner, workspace_before):
        execution_id = str(uuid.uuid4())
        now = time.time()
        with self.db:
            cur = self.db.execute("""UPDATE tasks SET state='RUNNING',active_execution_id=?,updated=?
                                     WHERE id=? AND state='QUEUED'""", (execution_id, now, task_id))
            if cur.rowcount != 1:
                raise ValueError("Task is not QUEUED; automatic re-execution is forbidden")
            self.db.execute("""INSERT INTO executions(id,task_id,phase,owner,workspace_before,codex_status,started,updated)
                               VALUES(?,?,?,?,?,?,?,?)""", (execution_id, task_id, "RUNNING", json.dumps(owner),
                               json.dumps(workspace_before), "NOT_STARTED", now, now))
            task = self.get(task_id)
            self.db.execute("UPDATE executions SET workspace_id=?,role=?,access=?,snapshot_id=? WHERE id=?",
                            (task["workspace_id"], task["role"], task["access"], task["snapshot_id"], execution_id))
            self._transition_log(task_id, "QUEUED", "RUNNING", "claimed once", execution_id)
        return execution_id

    def recover(self, task_id=None):
        # Caller owns the runtime lock. Recovery records uncertainty, never dispatches work.
        rows = self.db.execute("SELECT id,active_execution_id FROM tasks WHERE state IN ('RUNNING','VERIFYING')"
                               + (" AND id=?" if task_id else ""), (task_id,) if task_id else ()).fetchall()
        with self.db:
            for row in rows:
                old = self.get(row["id"])["state"]
                self.db.execute("UPDATE tasks SET state='UNKNOWN',updated=? WHERE id=?", (time.time(), row["id"]))
                self.db.execute("UPDATE tasks SET codex_status='UNKNOWN' WHERE id=? AND codex_status IN ('DISPATCHING','RUNNING')", (row["id"],))
                self._transition_log(row["id"], old, "UNKNOWN", "owner exited without durable task completion", row["active_execution_id"])
                if row["active_execution_id"]:
                    self.db.execute("UPDATE executions SET phase='UNKNOWN',codex_status=CASE WHEN codex_status IN ('DISPATCHING','RUNNING') THEN 'UNKNOWN' ELSE codex_status END,updated=? WHERE id=?",
                                    (time.time(), row["active_execution_id"]))
        return [row["id"] for row in rows]

    def finish_verification(self, task_id, evidence, state, status, human_status):
        task = self.get(task_id)
        verification_id = str(uuid.uuid4())
        failed = [kind for kind in ["artifacts", "tests", "conditions"] if any(not x["passed"] for x in evidence[kind])]
        error = ("Acceptance checks failed: " + ", ".join(failed) if state == "FAILED" else
                 "Verification deadline exceeded" if state == "TIMED_OUT" else
                 "Verification cancelled" if state == "CANCELLED" else
                 "Verification cleanup/checkpoint is uncertain" if state == "UNKNOWN" else None)
        if evidence.get('blocked'):
            error = json.dumps(evidence['blocked'], sort_keys=True)
        with self.db:
            self.db.execute("""INSERT INTO verifications(id,task_id,execution_id,status,evidence,workspace_fingerprint,created)
                               VALUES(?,?,?,?,?,?,?)""", (verification_id, task_id, task["active_execution_id"], status,
                               json.dumps(redact(evidence), ensure_ascii=False), evidence["workspace"]["fingerprint"], time.time()))
            cur = self.db.execute("""UPDATE tasks SET state=?,verification_status=?,human_status=?,error=?,updated=?
                                     WHERE id=? AND state='VERIFYING'""", (state, status, human_status, error, time.time(), task_id))
            if cur.rowcount != 1:
                raise ValueError("Task is no longer VERIFYING")
            self._transition_log(task_id, "VERIFYING", state, "independent acceptance evidence recorded", task["active_execution_id"])
            self.db.execute("UPDATE executions SET phase=?,finished=?,updated=? WHERE id=?",
                            (state, time.time(), time.time(), task["active_execution_id"]))
        return verification_id

    def _cancel_locked(self, task_id):
        task = self.get(task_id)
        if task["state"] == "QUEUED":
            self.db.execute("UPDATE tasks SET state='CANCELLED',updated=? WHERE id=? AND state='QUEUED'", (time.time(), task_id))
            self._transition_log(task_id, "QUEUED", "CANCELLED", "cancelled before execution")
        elif task["state"] in ["RUNNING", "VERIFYING"]:
            self.db.execute("UPDATE tasks SET cancel_requested=1,updated=? WHERE id=?", (time.time(), task_id))
        return self.get(task_id)

    def cancel(self, task_id):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            return self._cancel_locked(task_id)

    def cancel_all(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            ids = [r[0] for r in self.db.execute("SELECT id FROM tasks WHERE state IN ('QUEUED','RUNNING','VERIFYING')")]
            return [self._cancel_locked(t)["id"] for t in ids]

    def close(self):
        self.db.close()
