import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from runner import Runner
from storage import Store, rollback_database
from isolation import prepare, isolated_launch, isolated_preflight
from concurrency import guard
from workspaces import safe_tree, spec
from transport import proc_identity


class Phase2CTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT / ".runtime", prefix="phase2-c-unit-")
        self.root = Path(self.temp.name)
        self.children = []
        self.runners = []
        self.runner = self.make_runner()
        seed = self.root / "fixtures/seed"
        seed.mkdir(parents=True)
        (seed / "expected.txt").write_text("isolated fixture\n")
        (seed / "result.txt").write_text("isolated fixture\n")
        self.policy = {"artifacts": [{"path": "result.txt", "sha256": hashlib.sha256(b"isolated fixture\n").hexdigest()}],
                       "tests": ["workspace-result-check"]}

    def make_runner(self):
        def launcher(root, binary, args):
            return [sys.executable, str(PROJECT / "tests/fake_codex.py"), "normal", str(root / "child.pid")], {"PATH": "/usr/bin:/bin"}
        value = Runner(self.root, sys.executable, launcher=launcher)
        self.runners.append(value)
        return value

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        for value in self.runners:
            value.store.close()
        self.temp.cleanup()

    def submit(self, **kw):
        return self.runner.store.submit("test isolated fixture", acceptance=self.policy,
                                        workspace={"fixture": "seed", **kw})

    def worker(self, task, mode="normal", timeout=10):
        p = subprocess.Popen([sys.executable, "-B", str(PROJECT / "tests/concurrent_driver.py"),
                              str(self.root), task, mode, str(timeout)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(p)
        return p

    def wait_turn(self, task):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = self.runner.store.get(task)
            if row["turn_id"]:
                return row
            time.sleep(0.02)
        self.fail("Worker failed to start turn")

    def finish(self, child):
        out, err = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0, err + out)
        return out.strip()

    def test_two_sessions_and_events_are_independent(self):
        a, b = self.submit(), self.submit()
        x, y = self.worker(a, "slow-normal"), self.worker(b, "slow-normal")
        self.wait_turn(a); self.wait_turn(b)
        self.assertEqual(self.make_runner().startup_recovered, [])
        self.assertEqual(self.finish(x), "COMPLETED")
        self.assertEqual(self.finish(y), "COMPLETED")
        rows = [self.runner.store.get(t) for t in [a, b]]
        for field in ["session_id", "turn_id", "active_execution_id", "workspace_id"]:
            self.assertNotEqual(rows[0][field], rows[1][field])
        executions = [self.runner.store.execution(t["active_execution_id"]) for t in rows]
        self.assertLess(max(e["started"] for e in executions), min(e["finished"] for e in executions))
        for row in rows:
            events = [json.loads(line) for line in Path(row["log_path"]).read_text().splitlines()]
            self.assertTrue(all(e["task_id"] == row["id"] and e["execution_id"] == row["active_execution_id"] for e in events))
            for e in events:
                params = e.get("message", {}).get("params", {})
                if "threadId" in params:
                    self.assertEqual(params["threadId"], row["session_id"])
        self.assertEqual(self.runner.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_third_session_stays_queued(self):
        a, b, c = self.submit(), self.submit(), self.submit()
        x, y = self.worker(a, "hang-no-child"), self.worker(b, "hang-no-child")
        self.wait_turn(a); self.wait_turn(b)
        with self.assertRaisesRegex(RuntimeError, "two sessions"):
            self.runner.run(c, 3)
        self.assertEqual(self.runner.store.get(c)["state"], "QUEUED")
        self.assertIsNone(self.runner.store.get(c)["active_execution_id"])
        for task in [a, b]: self.runner.store.cancel(task)
        self.assertEqual(self.finish(x), "CANCELLED"); self.assertEqual(self.finish(y), "CANCELLED")

    def test_one_timeout_does_not_stop_other(self):
        a, b = self.submit(), self.submit()
        x, y = self.worker(a, "hang", .5), self.worker(b, "slow-normal")
        self.assertEqual(self.finish(x), "TIMED_OUT"); self.assertEqual(self.finish(y), "COMPLETED")
        self.assertEqual(json.loads(self.runner.store.get(a)["cleanup"])["survivors"], [])

    def test_individual_cancel_keeps_other_running(self):
        a, b = self.submit(), self.submit()
        x, y = self.worker(a, "hang"), self.worker(b, "slow-normal")
        self.wait_turn(a); self.wait_turn(b)
        self.runner.store.cancel(a)
        self.assertEqual(self.finish(x), "CANCELLED"); self.assertEqual(self.finish(y), "COMPLETED")
        self.assertFalse(self.runner.store.get(b)["cancel_requested"])

    def test_parent_crash_recovers_only_unleased_task(self):
        a, b = self.submit(), self.submit()
        x, y = self.worker(a, "hang-no-child"), self.worker(b, "hang-no-child")
        self.wait_turn(a); self.wait_turn(b)
        x.kill(); x.communicate(timeout=3)
        fresh = self.make_runner()
        self.assertEqual(fresh.startup_recovered, [a])
        self.assertEqual(fresh.store.get(a)["state"], "UNKNOWN")
        self.assertEqual(fresh.store.get(b)["state"], "RUNNING")
        with self.assertRaises(ValueError): fresh.run(a, 2)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            facts = fresh.reconcile(a, offline=True)
            if facts["resources"]["released"]: break
            time.sleep(.02)
        self.assertTrue(facts["resources"]["released"])
        self.assertFalse(facts["resume_eligible"])
        fresh.store.cancel(b); self.assertEqual(self.finish(y), "CANCELLED")

    def test_coordinator_sigkill_recovers_both_sessions_without_dispatch(self):
        a, b = self.submit(), self.submit()
        parent = subprocess.Popen([sys.executable, "-B", str(PROJECT / "tests/pair_crash_driver.py"),
                                   str(self.root), a, b], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(parent)
        self.wait_turn(a); self.wait_turn(b)
        before = [self.runner.store.get(t)["active_execution_id"] for t in [a, b]]
        parent.kill(); parent.communicate(timeout=3)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            fresh = self.make_runner()
            if all(fresh.store.get(t)["state"] == "UNKNOWN" for t in [a, b]): break
            time.sleep(.02)
        self.assertEqual([fresh.store.get(t)["state"] for t in [a, b]], ["UNKNOWN", "UNKNOWN"])
        self.assertEqual([fresh.store.get(t)["active_execution_id"] for t in [a, b]], before)
        for task_id in [a, b]:
            facts = fresh.reconcile(task_id, offline=True)
            self.assertTrue(facts["resources"]["released"])
            with self.assertRaises(ValueError): fresh.run(task_id, 1)

    def test_workspace_ownership_and_config_tampering_are_refused(self):
        a, b = self.submit(), self.submit()
        task = self.runner.store.get(a)
        root = self.runner.workroot(task)
        with self.runner.store.db:
            self.runner.store.db.execute("UPDATE tasks SET access='invalid' WHERE id=?", (a,))
        with self.assertRaisesRegex(ValueError, "policy"): self.runner.run(a, 2)
        with self.runner.store.db:
            self.runner.store.db.execute("UPDATE tasks SET access='read-only' WHERE id=?", (a,))
        with self.runner.store.db:
            self.runner.store.db.execute("UPDATE workspaces SET owner_task_id=? WHERE id=?", (b, task["workspace_id"]))
        with self.assertRaisesRegex(ValueError, "own this workspace"): self.runner.run(a, 2)
        with self.runner.store.db:
            self.runner.store.db.execute("UPDATE workspaces SET owner_task_id=? WHERE id=?", (a, task["workspace_id"]))
        (root / "codex-home/config.toml").write_text("approval_policy='untrusted'")
        with self.assertRaisesRegex(ValueError, "policy"): self.runner.run(a, 2)
        self.assertEqual(self.runner.store.get(a)["state"], "QUEUED")

    def test_snapshot_preserves_empty_directories_and_executable_files(self):
        seed = self.root / "fixtures/seed"
        (seed / "empty").mkdir()
        script = seed / "script.py"
        script.write_text("print('safe fixture')\n"); script.chmod(0o700)
        task = self.runner.run(self.submit(), 5)
        frozen = self.runner.snapshot(task["id"])
        review = self.runner.store.submit("review", acceptance=self.policy,
            workspace={"role": "reviewer", "snapshot_id": frozen["snapshot_id"]})
        directory = self.runner.workroot(self.runner.store.get(review)) / "workspace"
        self.assertTrue((directory / "empty").is_dir())
        self.assertTrue((directory / "script.py").stat().st_mode & 0o100)

    def test_duplicate_task_cannot_dispatch_twice(self):
        a = self.submit()
        child = self.worker(a, "hang-no-child")
        self.wait_turn(a)
        with self.assertRaises(RuntimeError): self.runner.run(a, 2)
        self.assertEqual(self.runner.store.db.execute("SELECT count(*) FROM executions WHERE task_id=?", (a,)).fetchone()[0], 1)
        self.runner.store.cancel(a); self.finish(child)

    def test_concurrent_request_key_creates_one_task_and_workspace(self):
        children = [self.worker("shared-request", "submit") for _ in range(6)]
        ids = [self.finish(p) for p in children]
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.runner.store.db.execute("SELECT count(*) FROM workspaces").fetchone()[0], 1)

    def test_legacy_execution_excludes_isolated_sessions(self):
        task = self.runner.store.get(self.submit())
        legacy = self.runner.store.submit("legacy owner crash fixture")
        self.runner.store.transition(legacy, "QUEUED", "RUNNING", "simulate missing owner")
        with self.runner.lock():
            with self.assertRaises(RuntimeError):
                with guard(self.root, task): pass
            self.assertEqual(self.make_runner().startup_recovered, [])
        with guard(self.root, task):
            with self.assertRaises(RuntimeError): self.runner.lock()
            self.assertEqual(self.make_runner().startup_recovered, [legacy])
            self.assertEqual(self.runner.store.get(task["id"])["state"], "QUEUED")

    def test_snapshot_is_independent_and_review_uses_new_thread(self):
        task = self.runner.run(self.submit(), 5)
        frozen = self.runner.snapshot(task["id"])
        review_id = self.runner.store.submit("independent review", acceptance=self.policy,
            workspace={"role": "reviewer", "snapshot_id": frozen["snapshot_id"]})
        original = self.runner.workroot(task) / "workspace/result.txt"
        original.write_text("implementation changed later")
        review = self.runner.run(review_id, 5)
        self.assertEqual(review["state"], "COMPLETED")
        self.assertEqual(review["role"], "reviewer")
        self.assertNotEqual(task["session_id"], review["session_id"])
        self.assertNotEqual(task["workspace_id"], review["workspace_id"])
        self.assertEqual(self.runner.store.get(task["id"])["state"], "COMPLETED")
        self.assertEqual((self.runner.workroot(review) / "workspace/result.txt").read_text(), "isolated fixture\n")

    def test_active_or_unknown_implementation_cannot_be_snapshotted(self):
        a = self.submit(); child = self.worker(a, "hang-no-child"); self.wait_turn(a)
        with self.assertRaises(RuntimeError): self.runner.snapshot(a)
        child.kill(); child.communicate(timeout=3); self.make_runner()
        with self.assertRaises(ValueError): self.runner.snapshot(a)

    def test_tampered_snapshot_refused(self):
        task = self.runner.run(self.submit(), 5)
        frozen = self.runner.snapshot(task["id"])
        file = self.root / "snapshots" / frozen["snapshot_id"] / "result.txt"
        file.chmod(0o600)
        file.write_text("tampered")
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.runner.store.submit("review", workspace={"role": "reviewer", "snapshot_id": frozen["snapshot_id"]})

    def test_write_policy_requires_fixture_content_and_test(self):
        for workspace in [{"access": "workspace-write"}, {"fixture": "../seed"},
                          {"role": "reviewer", "access": "workspace-write", "snapshot_id": "x"}]:
            with self.assertRaises(ValueError): self.runner.store.submit("bad", workspace=workspace)
        with self.assertRaisesRegex(ValueError, "content hashes"):
            self.runner.store.submit("weak", workspace={"fixture": "seed", "access": "workspace-write"}, acceptance={"artifacts": [{"path": "result.txt"}]})
        task = self.runner.run(self.submit(access="workspace-write"), 5)
        self.assertEqual(task["state"], "COMPLETED")
        self.assertEqual(task["access"], "workspace-write")

    def test_isolation_failure_refuses_dispatch_without_fallback(self):
        runner = Runner(self.root, sys.executable)
        self.runners.append(runner)
        for error, expected in [(RuntimeError("namespace policy unavailable"), "FAILED"),
                                (subprocess.TimeoutExpired("namespace-preflight", .1), "TIMED_OUT")]:
            task_id = self.submit(access="workspace-write")
            with patch("runner.isolated_preflight", side_effect=error):
                task = runner.run(task_id, 3)
            self.assertEqual(task["state"], expected)
            self.assertIsNone(task["turn_id"])
            self.assertEqual(runner.store.execution(task["active_execution_id"])["dispatched"], 0)

    def test_executable_cannot_mount_a_sibling_workspace_as_resources(self):
        task = self.runner.store.get(self.submit())
        sibling_program = self.runner.workroot(task) / "workspace/executable"
        sibling_program.write_text("untrusted")
        with self.assertRaisesRegex(ValueError, "system program"):
            isolated_launch(self.runner.workroot(task), str(sibling_program), [], authenticate=False)

    def test_symlink_hardlink_and_project_config_fixtures_are_refused(self):
        seed = self.root / "fixtures/seed"
        (seed / "link").symlink_to(PROJECT / "README.md")
        with self.assertRaises(ValueError): self.submit()
        (seed / "link").unlink()
        os.link(seed / "result.txt", seed / "hardlink")
        with self.assertRaisesRegex(ValueError, "Hardlinked"): self.submit()
        (seed / "hardlink").unlink()
        (seed / ".codex").mkdir()
        (seed / ".codex/config.toml").write_text('approval_policy="never"')
        with self.assertRaisesRegex(ValueError, "Project Codex configuration"): self.submit()

    def test_mount_boundary_refused_in_real_namespace(self):
        path = self.root / "fixtures/seed"
        code = "import sys,subprocess;sys.path.insert(0,sys.argv[1]);from workspaces import safe_tree;subprocess.run(['/usr/bin/mount','--bind',sys.argv[2],sys.argv[2]],check=True);\ntry:safe_tree(sys.argv[2])\nexcept ValueError:sys.exit(0)\nraise SystemExit(1)"
        result = subprocess.run(["unshare", "--user", "--map-root-user", "--mount", sys.executable, "-B", "-c", code, str(PROJECT / "src"), str(path)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_real_namespace_denies_cross_reads_writes_and_enforces_access(self):
        a, b = self.submit(), self.submit()
        task = self.runner.store.get(a)
        own = self.runner.workroot(task)
        sibling = self.runner.workroot(self.runner.store.get(b)) / "workspace/result.txt"
        for access in ["read-only", "workspace-write"]:
            isolated_preflight(own, access)
            code = """import json,sys
import subprocess
from pathlib import Path
def read(p):
 try: Path(p).read_bytes(); return True
 except OSError:return False
def write(p):
 try: Path(p).write_text('probe'); return True
 except OSError:return False
escape = subprocess.run(['/usr/bin/unshare','--user','--map-root-user','--mount',
 '/usr/bin/mount','-o','remount,bind,rw','/usr'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode == 0
print(json.dumps({'sibling_read':read(sys.argv[1]),'sibling_write':write(sys.argv[1]),'mount_escape':escape,
 'traversal':read(sys.argv[3]),'proc_escape':read('/proc/1/root'+sys.argv[1]),
 'coordinator':read(sys.argv[2]),'own_write':write('probe.txt'),'host_write':write('/etc/phase2-c-probe')}))
"""
            argv, env = isolated_launch(own, "/usr/bin/python3", ["-B", "-c", code, str(sibling), str(self.root / "tasks.sqlite"), os.path.relpath(sibling, own / "workspace")], access=access, authenticate=False)
            result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(result.stdout)
            self.assertEqual(observed, {"sibling_read": False, "sibling_write": False, "traversal": False,
                "proc_escape": False, "mount_escape": False, "coordinator": False, "own_write": access == "workspace-write", "host_write": False})
        self.assertEqual(sibling.read_text(), "isolated fixture\n")

    def test_resume_reuses_owned_workspace_and_is_serialized(self):
        first = self.runner.run(self.submit(), 5)
        second_id = self.runner.store.submit("continue", resume_of=first["id"], acceptance=self.policy)
        self.assertEqual(self.runner.store.get(second_id)["workspace_id"], first["workspace_id"])
        with guard(self.root, first):
            with self.assertRaises(RuntimeError): self.runner.run(second_id, 3)
        second = self.runner.run(second_id, 5)
        self.assertEqual(second["state"], "COMPLETED")
        self.assertEqual(second["session_id"], first["session_id"])

    def test_resume_ignores_only_known_historical_usage(self):
        first = self.runner.run(self.submit(), 5)
        concurrent_servers = []
        def launcher(root, binary, args):
            marker = root / "server.pid"
            current = proc_identity(int(marker.read_text())) if marker.exists() else None
            concurrent_servers.append(bool(current and current["state"] not in ["Z", "X"]))
            return [sys.executable, str(PROJECT / "tests/fake_codex.py"), "previous-usage", str(root / "child.pid")], {"PATH": "/usr/bin:/bin"}
        resumed_runner = Runner(self.root, sys.executable, launcher=launcher)
        self.runners.append(resumed_runner)
        task_id = resumed_runner.store.submit("continue with historical notification", resume_of=first["id"], acceptance=self.policy)
        task = resumed_runner.run(task_id, 5)
        self.assertEqual(task["state"], "COMPLETED")
        self.assertEqual(json.loads(task["usage"])["last"]["totalTokens"], 7)
        events = [json.loads(s) for s in Path(task["log_path"]).read_text().splitlines()]
        self.assertTrue(any(e["direction"] == "historical_usage_ignored" for e in events))
        self.assertEqual(concurrent_servers, [False, False])

    def test_global_cancel_marks_both_workers_and_queued_tasks(self):
        a, b, c = self.submit(), self.submit(), self.submit()
        x, y = self.worker(a, "hang-no-child"), self.worker(b, "hang-no-child")
        self.wait_turn(a); self.wait_turn(b)
        self.assertEqual(set(self.runner.store.cancel_all()), {a, b, c})
        self.assertEqual(self.finish(x), "CANCELLED"); self.assertEqual(self.finish(y), "CANCELLED")
        self.assertEqual(self.runner.store.get(c)["state"], "CANCELLED")

    def test_release_retains_files_and_blocks_future_dispatch(self):
        task = self.runner.run(self.submit(), 5)
        root = self.runner.workroot(task)
        self.assertTrue(self.runner.release(task["id"])["files_retained"])
        self.assertTrue((root / "workspace/result.txt").is_file())
        with self.assertRaises(ValueError): self.runner.workroot(task)
        with self.assertRaisesRegex(ValueError, "released"):
            self.runner.store.submit("resume released session", resume_of=task["id"], acceptance=self.policy)


class Phase2CMigrationTests(unittest.TestCase):
    def test_v2_migration_failure_keeps_schema_and_rows(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / ".runtime", prefix="phase2-c-migration-failure-") as name:
            root = Path(name); path = root / "tasks.sqlite"
            store = Store(path); store.close()
            backup = next((root / "db-backups").glob("phase2-ab-*.sqlite"))
            rollback_database(path, backup.name)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("ALTER TABLE executions ADD COLUMN workspace_id TEXT")
                before = db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
            with self.assertRaises(sqlite3.OperationalError): Store(path)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall(), before)

    def test_v2_rows_preserved_and_guarded_rollback(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / ".runtime", prefix="phase2-c-migration-") as name:
            root = Path(name); path = root / "tasks.sqlite"
            store = Store(path); store.close()
            backup = next((root / "db-backups").glob("phase2-ab-*.sqlite"))
            rollback_database(path, backup.name)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                db.execute("INSERT INTO tasks(id,prompt,state,created,updated) VALUES('legacy-v2','preserve','COMPLETED',1,1)")
            store = Store(path)
            self.assertEqual(store.get("legacy-v2")["prompt"], "preserve")
            self.assertIsNone(store.get("legacy-v2")["workspace_id"])
            self.assertFalse(store.get("legacy-v2")["completion_verified"])
            self.assertEqual(store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            store.close()
            latest = max((root / "db-backups").glob("phase2-ab-*.sqlite"), key=lambda p:p.stat().st_mtime_ns)
            rollback_database(path, latest.name)
            store = Store(path)
            store.submit("new legacy workspace task")
            store.close()
            with self.assertRaisesRegex(ValueError, "Records changed"):
                rollback_database(path, latest.name)


if __name__ == "__main__": unittest.main()
