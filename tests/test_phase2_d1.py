import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from resources import Control, ResourceExceeded, BudgetLog, policy, usage, storage_check, scope_released
from transport import Process
from runner import Runner
from isolation import isolated_launch
from retention import report


class ResourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT / ".runtime", prefix="d1-unit-")
        self.root = Path(self.temp.name)
        self.results = []

    def tearDown(self):
        if self.results:
            directory = PROJECT / ".runtime/phase2-d1-evidence"
            directory.mkdir(exist_ok=True)
            with (directory / "kernel-measurements.jsonl").open("a") as stream:
                stream.write(json.dumps({"test": self.id(), "observations": self.results}) + "\n")
        self.temp.cleanup()

    def run_code(self, code, **values):
        limits = policy(); limits.update(values)
        control = Control(self.root, limits)
        proc = Process([sys.executable, "-B", "-c", code], cwd=self.root,
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())}, raw_output=True, resources=control)
        limit_error = None
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try: proc.next_frame(.05)
                except EOFError: break
        except ResourceExceeded as exc:
            limit_error = str(exc)
        finally:
            result = proc.close(.2)
        result["limit_error"] = limit_error
        self.results.append(result)
        self.assertFalse(result["survivors"])
        self.assertTrue(result["resources"]["released"])
        return result

    def test_effective_cgroup_values_and_hard_fsize(self):
        r = self.run_code("import time;print('bounded',flush=True);time.sleep(.2)")
        self.assertEqual(r["exit_code"], 0)
        self.assertTrue(r["resources"]["confirmed"])
        data = r["resources"]["receipt_data"]
        self.assertEqual(data["effective"]["cpu.max"], "100000 100000")
        self.assertEqual(data["rlimit_fsize"], [67108864, 67108864])
        self.assertTrue(scope_released(r["resources"]))

    def test_cpu_throttle_is_real_and_not_task_failure(self):
        r = self.run_code("import time;t=time.monotonic();\nwhile time.monotonic()-t<.8:pass", cpu_percent=25)
        stats = r["resources"]["receipt_data"]["counters"]["cpu.stat"]
        self.assertGreater(stats["nr_throttled"], 0)
        self.assertEqual(r["exit_code"], 0)

    def test_cpu_budget_terminates_and_releases_scope(self):
        r = self.run_code("while True:pass", cpu_budget_ms=200)
        self.assertEqual(r["resources"]["exceeded"], "cpu_budget_ms")
        self.assertNotEqual(r["exit_code"], 0)

    def test_memory_limit_real_oom_is_recorded(self):
        r = self.run_code("values=[]\nwhile True:values.append(bytearray(4*1024*1024))", memory_bytes=64*1024**2)
        self.assertEqual(r["resources"]["exceeded"], "memory.max")
        self.assertNotEqual(r["exit_code"], 0)

    def test_pids_limit_even_if_child_handles_fork_failure(self):
        r = self.run_code("import subprocess,time;children=[]\ntry:\n for n in range(40):children.append(subprocess.Popen(['/usr/bin/sleep','10']))\nexcept OSError:pass\ntime.sleep(10)", pids=12)
        self.assertEqual(r["resources"]["exceeded"], "pids.max")

    def test_per_file_kernel_limit(self):
        r = self.run_code("import os,signal;signal.signal(signal.SIGXFSZ,signal.SIG_DFL);f=os.open('bounded.bin',os.O_WRONLY|os.O_CREAT,0o600);os.write(f,b'x'*65536);os.write(f,b'y')", file_bytes=65536)
        self.assertLessEqual((self.root / "bounded.bin").stat().st_size, 65536)
        self.assertEqual(r["resources"]["exceeded"], "RLIMIT_FSIZE")

    def test_runtime_budget_stops_without_deleting_evidence(self):
        r = self.run_code("from pathlib import Path;import time\nfor n in range(32):Path('disk-'+str(n)).write_bytes(b'x'*65536)\ntime.sleep(10)", runtime_bytes=1024**2, file_bytes=65536)
        self.assertEqual(r["resources"]["exceeded"], "runtime_storage")
        self.assertTrue((self.root / "disk-0").exists())

    def test_permissions_failure_does_not_launch_command(self):
        limits = policy();control = Control(self.root, limits)
        # Simulate a missing user bus, without changing any host permissions.
        original = control.wrap
        def no_bus(argv, env):
            args, changed = original(argv, env)
            changed["XDG_RUNTIME_DIR"] = str(self.root / "no-user-bus")
            return args, changed
        control.wrap = no_bus
        proc = Process([sys.executable, "-c", "from pathlib import Path;Path('must-not-run').touch()"],
                       cwd=self.root, env={"PATH": "/usr/bin:/bin"}, raw_output=True, resources=control)
        try:
            while True:
                try: proc.next_frame(.05)
                except EOFError: break
        finally: result = proc.close(.1)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertFalse(result["resources"]["confirmed"])
        self.assertFalse((self.root / "must-not-run").exists())

    def test_invalid_and_disabled_limits_rejected(self):
        for value in [None, 0, -1, True, "max"]:
            limits = policy();limits["memory_bytes"] = value
            with self.assertRaises(ValueError): policy(limits)

    def test_corrupt_resource_receipt_never_confirms_success(self):
        control = Control(self.root, policy())
        control.receipt.write_text('{"torn":')
        with self.assertRaises(ResourceExceeded): control.check()
        result = control.finish()
        self.assertFalse(result["confirmed"])
        self.assertFalse(result["released"])
        self.assertTrue(result["receipt_invalid"])

    def test_log_ceiling_retains_complete_prefix(self):
        with (self.root / "bounded.jsonl").open("w") as stream:
            bounded = BudgetLog(stream, 4096)
            bounded.write('{"initial":true}\n')
            with self.assertRaises(ResourceExceeded): bounded.write("x" * 4096)
            bounded.write("discarded after durable failure")
        lines = (self.root / "bounded.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(lines[0]), {"initial": True})
        self.assertEqual(json.loads(lines[1])["direction"], "log_limit")
        self.assertLess((self.root / "bounded.jsonl").stat().st_size, 4096)

    def test_inventory_does_not_follow_links(self):
        (self.root / "link").symlink_to(Path.home() / ".codex")
        result = usage(self.root)
        self.assertTrue(result["problems"])
        self.assertEqual(result["files"], 0)
        with self.assertRaises(ResourceExceeded): storage_check(self.root, policy())


class LimitedTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT / ".runtime", prefix="d1-task-")
        self.root = Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def runner(self, mode, limits):
        code = (PROJECT / "tests/fake_codex.py").read_text()
        def launcher(root, binary, args):
            return isolated_launch(root, sys.executable, ["-B", "-c", code, mode, "/tmp/child.pid"], authenticate=False)
        return Runner(self.root, sys.executable, launcher=launcher, resource_limits=limits)

    def test_limits_are_failed_not_success_and_database_is_consistent(self):
        for mode, change in [("resource-cpu", {"cpu_budget_ms": 250}),
                             ("resource-memory", {"memory_bytes": 96*1024**2}),
                             ("resource-pids", {"pids": 16})]:
            with self.subTest(mode=mode):
                limits = policy();limits.update(change)
                r = self.runner(mode, limits)
                try:
                    task_id = r.store.submit("bounded fault fixture", workspace={})
                    task = r.run(task_id, 8)
                    self.assertEqual(task["state"], "FAILED", task["error"])
                    self.assertFalse(task["completion_verified"])
                    self.assertTrue(json.loads(task["cleanup"])["resources"]["exceeded"])
                    self.assertEqual(r.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    with self.assertRaises(ValueError): r.run(task_id, 1)
                finally:r.store.close()

    def test_normal_task_with_limits_and_verification(self):
        r = self.runner("normal", policy())
        try:
            seed = self.root / "fixtures/seed";seed.mkdir(parents=True)
            (seed / "result.txt").write_text("checked")
            task_id = r.store.submit("normal bounded", workspace={"fixture": "seed"},
                acceptance={"artifacts": [{"path": "result.txt", "sha256": hashlib.sha256(b'checked').hexdigest()}]})
            task = r.run(task_id, 8)
            self.assertEqual(task["state"], "COMPLETED", task["error"])
            self.assertTrue(json.loads(task["cleanup"])["resources"]["confirmed"])
        finally:r.store.close()

    def test_coordinator_passes_discovery_path_without_credentials(self):
        from coordinator import run_pair
        from unittest.mock import MagicMock
        r = self.runner("normal", policy())
        try:
            ids = [r.store.submit("path regression", workspace={}) for _ in range(2)]
            worker = MagicMock()
            worker.proc.poll.return_value = 0
            worker.close.return_value = {"survivors": []}
            with patch.dict(os.environ, {"PATH": "/usr/local/bin:/usr/bin:/bin"}), patch("coordinator.Process", return_value=worker) as launch:
                run_pair(self.root, sys.executable, ids, 1)
                for call in launch.call_args_list:
                    self.assertEqual(call.kwargs["env"], {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"})
        finally:r.store.close()

    def test_retention_preserves_unresolved_evidence_and_performs_no_deletion(self):
        r = self.runner("normal", policy())
        try:
            task_id = r.store.submit("unknown evidence", workspace={})
            r.store.transition(task_id, "QUEUED", "RUNNING", "fault injection")
            r.store.recover(task_id)
            (self.root / "logs/evidence.txt").write_text("retain")
            result = report(self.root, r.store)
            self.assertFalse(result["automatic_deletion"])
            self.assertEqual(result["protected_tasks"][0]["id"], task_id)
            self.assertTrue((self.root / "logs/evidence.txt").exists())
            with self.assertRaises(ValueError):r.run(task_id, 1)
        finally:r.store.close()


class GitAuditTests(unittest.TestCase):
    def test_exact_staged_blob_is_scanned_not_later_worktree_contents(self):
        sys.path.insert(0, str(PROJECT / "scripts"))
        from git_audit import audit
        with tempfile.TemporaryDirectory(dir=PROJECT / ".runtime", prefix="d1-git-") as name:
            root = Path(name)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "README.md").write_text("Safe fixture")
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
            (root / "README.md").write_text("sk-" + "A" * 32)
            self.assertTrue(audit(root)["passed"])
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
            result = audit(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any(f["rule"] == "api_key" for f in result["findings"]))
            self.assertNotIn("A" * 32, json.dumps(result))

    def test_credentials_binary_and_symlink_paths_are_refused(self):
        sys.path.insert(0, str(PROJECT / "scripts"))
        from git_audit import audit
        with tempfile.TemporaryDirectory(dir=PROJECT / ".runtime", prefix="d1-git-") as name:
            root = Path(name)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "auth.json").write_bytes(b"\x00fixture")
            (root / "README.md").symlink_to("auth.json")
            subprocess.run(["git", "-C", str(root), "add", "-f", "auth.json", "README.md"], check=True)
            result = audit(root)
            self.assertFalse(result["passed"])
            self.assertTrue({"forbidden_path_or_extension", "binary_blob", "nonregular_or_conflicted_index"} <= {f["rule"] for f in result["findings"]})


if __name__ == "__main__": unittest.main()
