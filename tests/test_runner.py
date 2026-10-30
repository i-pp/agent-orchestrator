import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import signal

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from runner import Runner, Store
from transport import Process, ProtocolError, proc_identity, redact


class RunnerTests(unittest.TestCase):
    def setUp(self):
        (PROJECT / ".runtime").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT / ".runtime", prefix="unit-")
        self.root = Path(self.temp.name)
        self.runners = []

    def tearDown(self):
        for runner in self.runners:
            runner.store.close()
        self.temp.cleanup()

    def runner(self, mode="normal"):
        def launcher(root, binary, args):
            env = {k: os.environ[k] for k in ["PATH", "LANG"] if k in os.environ}
            return [sys.executable, str(Path(__file__).with_name("fake_codex.py")), mode,
                    str(root / "child.pid")], env
        value = Runner(self.root, sys.executable, launcher=launcher)
        self.runners.append(value)
        return value

    def test_normal_execution_and_database_reload(self):
        runner = self.runner()
        (self.root / "workspace/evidence.txt").write_text("verified fixture")
        task_id = runner.store.submit("test", acceptance={"artifacts": [{"path": "evidence.txt"}]})
        task = runner.run(task_id, 3)
        self.assertEqual(task["state"], "COMPLETED")
        self.assertEqual(task["result"], "mock result")
        self.assertEqual(json.loads(task["cleanup"])["survivors"], [])
        fresh = Store(self.root / "tasks.sqlite")
        try:
            self.assertEqual(fresh.get(task_id)["result"], "mock result")
        finally:
            fresh.close()
        events = [json.loads(x) for x in Path(task["log_path"]).read_text().splitlines()]
        self.assertTrue(any(x["direction"] == "monitor" for x in events))

    def test_startup_failure_is_failed(self):
        runner = self.runner()
        task_id = runner.store.submit("test")
        runner.binary = "/nonexistent/phase1-codex"
        self.assertEqual(runner.run(task_id, 3)["state"], "FAILED")

    def test_malformed_event_is_unknown_and_no_retry(self):
        runner = self.runner("malformed")
        task_id = runner.store.submit("test")
        task = runner.run(task_id, 3)
        self.assertEqual(task["state"], "UNKNOWN")
        self.assertIn("ProtocolError", task["error"])
        with self.assertRaises(ValueError):
            runner.run(task_id, 3)
        with self.assertRaises(ValueError):
            runner.store.submit("resume", resume_of=task_id)

    def test_disconnect_after_acceptance_is_unknown(self):
        runner = self.runner("disconnect")
        task_id = runner.store.submit("test")
        self.assertEqual(runner.run(task_id, 3)["state"], "UNKNOWN")

    def test_structurally_invalid_event_is_unknown(self):
        runner = self.runner("bad-shape")
        task = runner.run(runner.store.submit("test"), 3)
        self.assertEqual(task["state"], "UNKNOWN")
        self.assertEqual(json.loads(task["cleanup"])["survivors"], [])

    def test_timeout_releases_child(self):
        runner = self.runner("hang")
        task_id = runner.store.submit("test")
        task = runner.run(task_id, 0.4)
        self.assertEqual(task["state"], "TIMED_OUT")
        cleanup = json.loads(task["cleanup"])
        self.assertEqual(cleanup["survivors"], [])
        pid = int((self.root / "child.pid").read_text())
        current = proc_identity(pid)
        self.assertTrue(current is None or current["state"] in ["Z", "X"])

    def test_cancellation_releases_child(self):
        runner = self.runner("hang")
        task_id = runner.store.submit("test")

        def cancel():
            deadline = time.monotonic() + 2
            while not (self.root / "child.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            store = Store(self.root / "tasks.sqlite")
            try:
                store.cancel(task_id)
            finally:
                store.close()
        worker = threading.Thread(target=cancel)
        worker.start()
        try:
            task = runner.run(task_id, 3)
        finally:
            worker.join(timeout=3)
        self.assertEqual(task["state"], "CANCELLED")
        self.assertEqual(json.loads(task["cleanup"])["survivors"], [])

    def test_timeout_releases_detached_child(self):
        runner = self.runner("hang-detached")
        task = runner.run(runner.store.submit("test"), 0.4)
        self.assertEqual(task["state"], "TIMED_OUT")
        self.assertEqual(json.loads(task["cleanup"])["survivors"], [])

    def test_completed_turn_with_reported_failure_is_failed(self):
        runner = self.runner("reported-failure")
        task = runner.run(runner.store.submit("test"), 3)
        self.assertEqual(task["state"], "FAILED")
        self.assertIn("unsuccessful", task["error"])

    def test_resume_uses_owned_session(self):
        runner = self.runner()
        (self.root / "workspace/evidence.txt").write_text("verified fixture")
        policy = {"artifacts": [{"path": "evidence.txt"}]}
        first = runner.run(runner.store.submit("first", acceptance=policy), 3)
        second = runner.run(runner.store.submit("resume", resume_of=first["id"], acceptance=policy), 3)
        self.assertEqual(second["state"], "COMPLETED")
        self.assertEqual(second["session_id"], first["session_id"])
        messages = [json.loads(x)["message"] for x in Path(second["log_path"]).read_text().splitlines()]
        self.assertTrue(any(x.get("method") == "thread/resume" for x in messages))

    def test_permission_denial_is_not_success(self):
        runner = self.runner("denied")
        task = runner.run(runner.store.submit("test"), 3)
        self.assertEqual(task["state"], "FAILED")
        self.assertIn("denied", task["error"])

    def test_api_key_auth_is_rejected(self):
        runner = self.runner("api-key")
        task = runner.run(runner.store.submit("test"), 3)
        self.assertEqual(task["state"], "FAILED")
        self.assertIsNone(task["turn_id"])

    def test_explicit_rpc_failure_is_failed(self):
        runner = self.runner("rpc-error")
        task = runner.run(runner.store.submit("test"), 3)
        self.assertEqual(task["state"], "FAILED")

    def test_recovery_marks_unknown_without_execution(self):
        runner = self.runner()
        task_id = runner.store.submit("test")
        runner.store.transition(task_id, "QUEUED", "RUNNING", "simulate crash")
        with runner.lock():
            self.assertEqual(runner.store.recover(), [task_id])
        self.assertEqual(runner.store.get(task_id)["state"], "UNKNOWN")
        with self.assertRaises(ValueError):
            runner.run(task_id)

    def test_single_runner_lock(self):
        runner = self.runner()
        with runner.lock():
            with self.assertRaises(RuntimeError):
                runner.lock()

    def test_request_key_idempotency_and_conflict(self):
        store = self.runner().store
        first = store.submit("same", request_key="key")
        self.assertEqual(store.submit("same", request_key="key"), first)
        with self.assertRaises(ValueError):
            store.submit("different", request_key="key")

    def test_queued_cancel_never_runs(self):
        runner = self.runner()
        task_id = runner.store.submit("test")
        self.assertEqual(runner.store.cancel(task_id)["state"], "CANCELLED")
        with self.assertRaises(ValueError):
            runner.run(task_id)

    def test_invalid_timeout_does_not_claim_task(self):
        runner = self.runner()
        task_id = runner.store.submit("test")
        for timeout in [0, -1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                runner.run(task_id, timeout)
        self.assertEqual(runner.store.get(task_id)["state"], "QUEUED")

    def test_runtime_state_symlink_is_rejected(self):
        (self.root / "codex-home").mkdir()
        (self.root / "codex-home/auth.json").symlink_to(self.root / "must-not-be-touched")
        with self.assertRaises(ValueError):
            self.runner()
        self.assertFalse((self.root / "must-not-be-touched").exists())

    def test_python_api_rejects_runtime_outside_dedicated_area(self):
        with self.assertRaises(ValueError):
            Runner(PROJECT / "not-a-runtime", sys.executable)
        self.assertFalse((PROJECT / "not-a-runtime").exists())

    def test_redaction(self):
        value = {"accessToken": "private", "usage": {"totalTokens": 42},
                 "text": "Bearer private-credential sk-" + "x" * 30,
                 "pem": "-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----"}
        output = json.dumps(redact(value))
        self.assertNotIn("private-credential", output)
        self.assertNotIn("x" * 30, output)
        self.assertNotIn("\\nprivate", output)
        self.assertEqual(redact(value)["usage"]["totalTokens"], 42)

    def test_parent_death_guard_stops_owned_child(self):
        pid_file = self.root / "guard.pid"
        driver_code = (
            "import sys,time; from pathlib import Path; "
            f"sys.path.insert(0,{str(PROJECT / 'src')!r}); "
            "from transport import Process; "
            f"p=Process([sys.executable,'-c','import time; time.sleep(60)'],cwd={str(self.root)!r},env={{'PATH':'/usr/bin'}}); "
            f"Path({str(pid_file)!r}).write_text(str(p.proc.pid)); time.sleep(60)"
        )
        driver = subprocess.Popen([sys.executable, "-c", driver_code])
        child_pid = None
        try:
            until = time.monotonic() + 3
            while not pid_file.exists() and time.monotonic() < until:
                time.sleep(0.01)
            self.assertTrue(pid_file.exists())
            child_pid = int(pid_file.read_text())
            self.assertIsNotNone(proc_identity(child_pid))
            driver.kill()
            driver.wait(timeout=3)
            until = time.monotonic() + 2
            while time.monotonic() < until:
                current = proc_identity(child_pid)
                if current is None or current["state"] in ["Z", "X"]:
                    break
                time.sleep(0.02)
            current = proc_identity(child_pid)
            self.assertTrue(current is None or current["state"] in ["Z", "X"])
        finally:
            if driver.poll() is None:
                driver.kill()
                driver.wait(timeout=3)
            if child_pid:
                current = proc_identity(child_pid)
                if current and current["state"] not in ["Z", "X"]:
                    os.kill(child_pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
