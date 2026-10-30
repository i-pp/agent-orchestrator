"""Fault-injection fixture: parent tests kill only this owned runner process."""

import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from runner import Runner
import verification
import runner as runner_module


def fixture_binary_version(root, binary, timeout=10):
    """Only this fake-peer fixture uses CPython metadata, not a real model."""
    if Path(binary).resolve() != Path(sys.executable).resolve():
        raise ValueError("Offline crash fixture only accepts its CPython executable")
    return "offline CPython fixture " + sys.version.split()[0]


runner_module.binary_version = fixture_binary_version

root = Path(sys.argv[1]).resolve()
phase = sys.argv[2]
mode = "normal" if phase == "verify" else "hang-no-child"


def launcher(root, binary, args):
    return [sys.executable, str(Path(__file__).with_name("fake_codex.py")), mode,
            str(root / "child.pid")], {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


runner = Runner(root, sys.executable, launcher=launcher)
(root / "workspace/fixture.txt").write_text("phase1-marker: CERULEAN-17\n")
policy = {"artifacts": [{"path": "fixture.txt"}]}
if phase == "verify":
    registry = root / "approved.json"
    registry.write_text(json.dumps({"pause": {"argv": [sys.executable, "-B", "-c", "import time; time.sleep(20)"], "timeout": 30}}))
    verification.REGISTRY = registry
    policy["tests"] = ["pause"]
task_id = runner.store.submit("fault injection", acceptance=policy)
(root / "driver-task.json").write_text(json.dumps({"task_id": task_id}))
runner.run(task_id, 60)
