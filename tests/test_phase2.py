import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import closing

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from runner import Runner, Store
from storage import migrate, rollback_database, LEGACY_COLUMNS
from transport import proc_identity
from transport import Process
from verification import normalize_acceptance, workspace_snapshot
import verification
import recovery


class Phase2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT / ".runtime", prefix="phase2-unit-")
        self.root = Path(self.temp.name)
        self.runners = []

    def tearDown(self):
        for runner in self.runners:
            runner.store.close()
        self.temp.cleanup()

    def runner(self, mode="normal"):
        def launcher(root, binary, args):
            return [sys.executable, str(Path(__file__).with_name("fake_codex.py")), mode,
                    str(root / "child.pid")], {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        runner = Runner(self.root, sys.executable, launcher=launcher)
        self.runners.append(runner)
        (self.root / "workspace/fixture.txt").write_text("phase1-marker: CERULEAN-17\n")
        return runner

    def policy(self, **extra):
        data = b"phase1-marker: CERULEAN-17\n"
        return {"artifacts": [{"path": "fixture.txt", "sha256": hashlib.sha256(data).hexdigest()}],
                "conditions": [{"kind": "result_equals", "value": "mock result"}], **extra}

    def run_task(self, runner, policy=None):
        task_id = runner.store.submit("read fixture", acceptance=policy or self.policy())
        return runner.run(task_id, 5)

    def test_codex_success_then_independent_verification(self):
        runner = self.runner()
        task = self.run_task(runner)
        execution = runner.store.execution(task["active_execution_id"])
        self.assertEqual(task["state"], "COMPLETED")
        self.assertEqual(task["codex_status"], "SUCCEEDED")
        self.assertEqual(task["verification_status"], "PASSED")
        self.assertNotEqual(task["id"], execution["id"])
        self.assertNotEqual(execution["id"], execution["thread_id"])
        row = runner.store.db.execute("SELECT evidence FROM verifications WHERE task_id=?", (task["id"],)).fetchone()
        self.assertTrue(json.loads(row[0])["artifacts"][0]["sha256"])
        states = [r[0] for r in runner.store.db.execute("SELECT new_state FROM transitions WHERE task_id=? ORDER BY seq", (task["id"],))]
        self.assertEqual(states, ["QUEUED", "RUNNING", "VERIFYING", "COMPLETED"])

    def test_normal_codex_exit_with_missing_artifact_fails(self):
        task = self.run_task(self.runner(), {"artifacts": [{"path": "missing.txt"}]})
        self.assertEqual(task["codex_status"], "SUCCEEDED")
        self.assertEqual(task["state"], "FAILED")
        self.assertEqual(task["verification_status"], "FAILED")

    def test_task_specific_condition_failure_is_distinct(self):
        task = self.run_task(self.runner(), self.policy(conditions=[{"kind": "result_equals", "value": "different"}]))
        self.assertEqual(task["state"], "FAILED")
        self.assertEqual(task["codex_status"], "SUCCEEDED")

    def test_no_acceptance_never_auto_completes(self):
        runner = self.runner()
        task = runner.run(runner.store.submit("unverified response"), 5)
        self.assertEqual(task["state"], "WAITING_FOR_HUMAN")
        self.assertEqual(task["human_status"], "PENDING")

    def test_result_text_only_needs_human(self):
        task = self.run_task(self.runner(), {"conditions": [{"kind": "result_contains", "value": "mock"}]})
        self.assertEqual(task["state"], "WAITING_FOR_HUMAN")

    def test_human_review_and_stale_evidence(self):
        runner = self.runner()
        task = self.run_task(runner, self.policy(human=True))
        self.assertEqual(task["state"], "WAITING_FOR_HUMAN")
        (self.root / "workspace/fixture.txt").write_text("changed")
        with self.assertRaises(ValueError):
            runner.review(task["id"], True, "tester", "reviewed fixture")
        (self.root / "workspace/fixture.txt").write_text("phase1-marker: CERULEAN-17\n")
        runner.review(task["id"], True, "tester", "independently reviewed fixture")
        self.assertEqual(runner.store.get(task["id"])["state"], "COMPLETED")
        self.assertEqual(runner.store.get(task["id"])["human_status"], "APPROVED")

    def test_human_rejection_is_recorded(self):
        runner = self.runner()
        task = self.run_task(runner, self.policy(human=True))
        runner.review(task["id"], False, "tester", "rejected after review")
        self.assertEqual(runner.store.get(task["id"])["state"], "FAILED")

    def registry(self, argv, timeout=2):
        registry = self.root / "approved.json"
        registry.write_text(json.dumps({"approved-test": {"argv": argv, "timeout": timeout}}))
        return patch.object(verification, "REGISTRY", registry)

    def test_approved_test_command_failure(self):
        runner = self.runner()
        with self.registry([sys.executable, "-B", "-c", "import sys; print('test evidence'); sys.exit(7)"]):
            task = self.run_task(runner, self.policy(tests=["approved-test"]))
        self.assertEqual(task["state"], "FAILED")
        row = runner.store.db.execute("SELECT evidence FROM verifications WHERE task_id=?", (task["id"],)).fetchone()
        self.assertEqual(json.loads(row[0])["tests"][0]["cleanup"]["exit_code"], 7)

    def test_approved_test_command_success(self):
        runner = self.runner()
        with self.registry([sys.executable, "-B", "-c", "print('approved evidence')"]):
            task = self.run_task(runner, self.policy(tests=["approved-test"]))
        self.assertEqual(task["state"], "COMPLETED")

    def test_verification_timeout_releases_resources(self):
        runner = self.runner()
        with self.registry([sys.executable, "-B", "-c", "import time; time.sleep(20)"], timeout=0.2):
            task = self.run_task(runner, self.policy(tests=["approved-test"]))
        self.assertEqual(task["state"], "TIMED_OUT")
        evidence = json.loads(runner.store.db.execute("SELECT evidence FROM verifications WHERE task_id=?", (task["id"],)).fetchone()[0])
        self.assertEqual(evidence["tests"][0]["cleanup"]["survivors"], [])

    def test_overall_deadline_includes_verification(self):
        runner = self.runner()
        with self.registry([sys.executable, "-B", "-c", "import time; time.sleep(20)"], timeout=30):
            task_id = runner.store.submit("test", acceptance=self.policy(tests=["approved-test"]))
            task = runner.run(task_id, 1)
        self.assertEqual(task["codex_status"], "SUCCEEDED")
        self.assertEqual(task["state"], "TIMED_OUT")
        self.assertEqual(task["verification_status"], "TIMED_OUT")

    def test_cancel_during_verification_releases_resources(self):
        runner = self.runner()
        with self.registry([sys.executable, "-B", "-c", "import time; time.sleep(20)"], timeout=30):
            task_id = runner.store.submit("test", acceptance=self.policy(tests=["approved-test"]))
            def cancel():
                store = Store(self.root / "tasks.sqlite")
                try:
                    until = time.monotonic() + 4
                    while time.monotonic() < until:
                        if store.get(task_id)["state"] == "VERIFYING":
                            store.cancel(task_id)
                            return
                        time.sleep(0.01)
                finally:
                    store.close()
            worker = threading.Thread(target=cancel)
            worker.start()
            try:
                task = runner.run(task_id, 5)
            finally:
                worker.join(timeout=5)
        self.assertEqual(task["state"], "CANCELLED")
        facts = recovery.resource_facts(runner.store.execution(task["active_execution_id"]), task)
        self.assertTrue(facts["released"])

    def test_completion_racing_cancel_keeps_execution_and_task_outcomes_separate(self):
        runner = self.runner("complete-on-interrupt")
        task_id = runner.store.submit("race fixture", acceptance=self.policy())
        def cancel():
            store = Store(self.root / "tasks.sqlite")
            try:
                until = time.monotonic() + 3
                while time.monotonic() < until:
                    if store.get(task_id)["turn_id"]:
                        store.cancel(task_id)
                        return
                    time.sleep(0.01)
            finally:
                store.close()
        worker = threading.Thread(target=cancel)
        worker.start()
        try:
            task = runner.run(task_id, 5)
        finally:
            worker.join(timeout=4)
        self.assertEqual(task["state"], "CANCELLED")
        self.assertEqual(task["codex_status"], "SUCCEEDED")
        self.assertFalse(task["completion_verified"])

    def test_other_turn_result_is_not_used(self):
        task = self.run_task(self.runner("wrong-turn"))
        self.assertEqual(task["state"], "UNKNOWN")
        self.assertFalse(task["completion_verified"])

    def test_command_policy_cannot_be_replaced_after_submit(self):
        runner = self.runner()
        with self.registry([sys.executable, "-B", "-c", "print('approved')"]):
            task_id = runner.store.submit("test", acceptance=self.policy(tests=["approved-test"]))
            verification.REGISTRY.write_text("{}")
            task = runner.run(task_id, 5)
        self.assertEqual(task["state"], "FAILED")

    def test_unapproved_model_commands_rejected_before_execution(self):
        for policy in [{"tests": ["model-invented-command"]}, {"tests": [["/bin/sh", "-c", "false"]]}]:
            with self.assertRaises(ValueError):
                normalize_acceptance(policy)
        with self.registry(["/bin/sh", "-c", "true"]):
            with self.assertRaises(ValueError):
                normalize_acceptance({"tests": ["approved-test"]})

    def test_artifact_boundary_and_symlink(self):
        runner = self.runner()
        for path in ["../outside", "/etc/passwd", "a/../../outside"]:
            with self.assertRaises(ValueError):
                runner.store.submit("test", acceptance={"artifacts": [{"path": path}]})
        (self.root / "outside.txt").write_text("outside")
        (self.root / "workspace/escape").symlink_to(self.root / "outside.txt")
        task = self.run_task(runner, {"artifacts": [{"path": "escape"}]})
        self.assertEqual(task["state"], "FAILED")

    def test_truncated_event_cannot_complete(self):
        for mode in ["truncated", "truncated-valid"]:
            runner = self.runner(mode)
            task = runner.run(runner.store.submit("test", acceptance=self.policy()), 5)
            self.assertEqual(task["state"], "UNKNOWN")
            self.assertIn("Truncated JSONL", task["error"])
            facts = runner.reconcile(task["id"], offline=True)
            self.assertFalse(facts["session"]["exists"])

    def test_lost_completion_is_reconciled_without_rerun(self):
        runner = self.runner("disconnect-after-complete")
        task = self.run_task(runner)
        self.assertEqual(task["state"], "UNKNOWN")
        facts = runner.reconcile(task["id"])
        self.assertTrue(facts["session"]["codex_succeeded"])
        self.assertEqual(runner.store.get(task["id"])["state"], "UNKNOWN")
        runner.resolve(task["id"], facts["reconciliation_id"], "verify", "tester", "checked saved completion and unchanged workspace")
        self.assertEqual(runner.store.get(task["id"])["state"], "COMPLETED")
        self.assertEqual(runner.store.db.execute("SELECT COUNT(*) FROM executions WHERE task_id=?", (task["id"],)).fetchone()[0], 1)

    def test_unknown_resume_requires_decision_and_cannot_double_use_it(self):
        runner = self.runner("disconnect-after-complete")
        task = self.run_task(runner)
        with self.assertRaises(ValueError):
            runner.store.submit("continue", resume_of=task["id"], acceptance=self.policy())
        facts = runner.reconcile(task["id"])
        decision = runner.resolve(task["id"], facts["reconciliation_id"], "allow_resume", "tester", "reviewed side effects")
        task_id = runner.store.submit("continue", resume_of=task["id"], resume_approval=decision["decision_id"], acceptance=self.policy())
        with self.assertRaises(sqlite3.IntegrityError):
            runner.store.submit("duplicate", resume_of=task["id"], resume_approval=decision["decision_id"], acceptance=self.policy())
        normal = self.runner()
        resumed = normal.run(task_id, 5)
        self.assertEqual(resumed["state"], "COMPLETED")
        self.assertEqual(resumed["session_id"], task["session_id"])

    def test_changed_side_effects_and_stale_resume_approval(self):
        runner = self.runner("disconnect-after-complete")
        task = self.run_task(runner)
        (self.root / "workspace/new-effect.txt").write_text("effect")
        facts = runner.reconcile(task["id"])
        self.assertEqual(facts["side_effects"], "CHANGED")
        with self.assertRaises(ValueError):
            runner.resolve(task["id"], facts["reconciliation_id"], "allow_resume", "tester", "not acknowledged")
        decision = runner.resolve(task["id"], facts["reconciliation_id"], "allow_resume", "tester", "reviewed effect", True)
        task_id = runner.store.submit("continue", resume_of=task["id"], resume_approval=decision["decision_id"], acceptance=self.policy())
        (self.root / "workspace/new-effect.txt").write_text("changed again")
        normal = self.runner()
        self.assertEqual(normal.run(task_id, 5)["state"], "FAILED")

    def test_live_resource_is_not_hidden_by_old_cleanup_record(self):
        runner = self.runner()
        task = self.run_task(runner)
        execution = runner.store.execution(task["active_execution_id"])
        identity = proc_identity(os.getpid())
        execution["processes"] = json.dumps({str(os.getpid()): identity["start"]})
        self.assertFalse(recovery.resource_facts(execution, task)["released"])

    def test_process_launch_gap_never_claims_resource_release(self):
        runner = self.runner()
        task = self.run_task(runner)
        execution = runner.store.execution(task["active_execution_id"])
        execution["launch_pending"] = 1
        self.assertFalse(recovery.resource_facts(execution, task)["released"])

    def test_abnormal_server_exit_is_unknown_not_success(self):
        runner = self.runner("crash")
        task = self.run_task(runner)
        self.assertEqual(task["state"], "UNKNOWN")
        self.assertEqual(json.loads(task["cleanup"])["exit_code"], 17)
        self.assertFalse(task["completion_verified"])

    def test_launch_checkpoint_failure_still_releases_child(self):
        recorded = {}
        def reject_checkpoint(identities):
            recorded.update(identities)
            raise RuntimeError("injected durable checkpoint failure")
        with self.assertRaises(RuntimeError):
            Process([sys.executable, "-c", "import time; time.sleep(20)"], cwd=self.root,
                    env={"PATH": "/usr/bin:/bin"}, on_observed=reject_checkpoint)
        self.assertTrue(recorded)
        for pid, start in recorded.items():
            current = proc_identity(pid)
            self.assertTrue(current is None or current["start"] != start or current["state"] in ["Z", "X"])

    def test_pid_reuse_does_not_identify_unrelated_process_as_owned(self):
        runner = self.runner()
        task = self.run_task(runner)
        execution = runner.store.execution(task["active_execution_id"])
        execution["processes"] = json.dumps({str(os.getpid()): "wrong-start-time"})
        self.assertTrue(recovery.resource_facts(execution, task)["released"])

    def crash_case(self, phase):
        driver = subprocess.Popen([sys.executable, str(Path(__file__).with_name("crash_driver.py")),
                                   str(self.root), phase], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        task_id = None
        try:
            until = time.monotonic() + 5
            while time.monotonic() < until:
                marker = self.root / "driver-task.json"
                if marker.exists():
                    task_id = json.loads(marker.read_text())["task_id"]
                    with closing(sqlite3.connect((self.root / "tasks.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
                        row = db.execute("SELECT state,active_execution_id,turn_id FROM tasks WHERE id=?", (task_id,)).fetchone()
                        processes = db.execute("SELECT processes,launch_pending FROM executions WHERE id=?", (row[1],)).fetchone()
                    ready = (row[0] == ("VERIFYING" if phase == "verify" else "RUNNING") and row[2]
                             and processes and not processes[1] and len(json.loads(processes[0] or "{}")) >= (2 if phase == "verify" else 1))
                    if ready:
                        break
                if driver.poll() is not None:
                    self.fail("Crash fixture exited before the injection checkpoint")
                time.sleep(0.02)
            else:
                self.fail("Crash injection checkpoint not reached")
            driver.kill()
            driver.wait(timeout=3)
            restarted = self.runner()
            task = restarted.store.get(task_id)
            self.assertEqual(task["state"], "UNKNOWN")
            if phase == "verify":
                self.assertEqual(task["codex_status"], "SUCCEEDED")
            until = time.monotonic() + 3
            while time.monotonic() < until:
                facts = recovery.resource_facts(restarted.store.execution(task["active_execution_id"]), task)
                if facts["released"]:
                    break
                time.sleep(0.02)
            self.assertTrue(facts["released"])
            with self.assertRaises(ValueError):
                restarted.run(task_id, 1)
            reconciliation = restarted.reconcile(task_id)
            restarted.resolve(task_id, reconciliation["reconciliation_id"], "fail", "tester",
                              "explicitly stop recovered task; no automatic rerun")
            self.assertEqual(restarted.store.get(task_id)["state"], "FAILED")
            self.assertEqual(restarted.store.db.execute("SELECT COUNT(*) FROM executions WHERE task_id=?", (task_id,)).fetchone()[0], 1)
        finally:
            if driver.poll() is None:
                driver.kill()
                driver.wait(timeout=3)

    def test_parent_sigkill_during_codex_execution_then_restart(self):
        self.crash_case("run")

    def test_parent_sigkill_during_verification_then_restart(self):
        self.crash_case("verify")

    def test_startup_detects_running_and_verifying_without_dispatch(self):
        runner = self.runner()
        for state in ["RUNNING", "VERIFYING"]:
            task_id = runner.store.submit("do not re-execute", acceptance=self.policy())
            runner.store.transition(task_id, "QUEUED", state, "simulate lost owner")
            restarted = self.runner()
            self.assertEqual(restarted.store.get(task_id)["state"], "UNKNOWN")
            with self.assertRaises(ValueError):
                restarted.run(task_id, 1)

    def test_single_task_has_one_execution_even_after_second_run_request(self):
        runner = self.runner()
        task = self.run_task(runner)
        with self.assertRaises(ValueError):
            runner.run(task["id"], 5)
        self.assertEqual(runner.store.db.execute("SELECT COUNT(*) FROM executions WHERE task_id=?", (task["id"],)).fetchone()[0], 1)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT / ".runtime", prefix="migration-")
        self.root = Path(self.temp.name)
        self.path = self.root / "tasks.sqlite"
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript((Path(__file__).parent / "fixtures/phase1.sql").read_text())
            self.original = db.execute("SELECT * FROM tasks ORDER BY id").fetchall()

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_preserves_rows_relations_and_supports_safe_rollback(self):
        store = Store(self.path)
        try:
            rows = store.db.execute("SELECT " + ",".join(LEGACY_COLUMNS) + " FROM tasks ORDER BY id").fetchall()
            self.assertEqual([tuple(row) for row in rows], self.original)
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(store.db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(store.get("legacy-parent")["verification_status"], "LEGACY_UNVERIFIED")
        finally:
            store.close()
        backup = next((self.root / "db-backups").glob("phase1-*.sqlite"))
        rollback_database(self.path, backup.name)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT * FROM tasks ORDER BY id").fetchall(), self.original)

    def test_rollback_refuses_to_discard_new_records(self):
        store = Store(self.path)
        try:
            store.submit("new request")
        finally:
            store.close()
        backup = next((self.root / "db-backups").glob("phase1-*.sqlite"))
        with self.assertRaises(ValueError):
            rollback_database(self.path, backup.name)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 3)

    def test_failed_migration_rolls_back_atomically(self):
        db = sqlite3.connect(self.path)
        def authorizer(action, table, arg2, database, source):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_TABLE and table == "tasks" else sqlite3.SQLITE_OK
        db.set_authorizer(authorizer)
        with self.assertRaises(sqlite3.DatabaseError):
            migrate(db, self.path)
        db.set_authorizer(None)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT * FROM tasks ORDER BY id").fetchall(), self.original)
        self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='tasks_new'").fetchone())
        db.close()

    def test_custom_index_is_not_silently_dropped(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE INDEX operator_index ON tasks(updated)")
            db.commit()
        with self.assertRaises(ValueError):
            Store(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='operator_index'").fetchone())
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_backup_symlink_cannot_redirect_migration_writes(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "db-backups").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            Store(self.path)
        self.assertEqual(list(outside.iterdir()), [])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT * FROM tasks ORDER BY id").fetchall(), self.original)


if __name__ == "__main__":
    unittest.main()
