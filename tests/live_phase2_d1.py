"""Opt-in: 2 native sessions killed with their coordinator, then 1 fresh normal task."""

import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from runner import Runner
from resources import policy
from recovery import resource_facts
from concurrency import recover_abandoned


def main():
    identifier = str(uuid.uuid4())[:8]
    root = PROJECT / ".runtime" / ("phase2-d1-live-" + identifier)
    runner = Runner(root)
    evidence_path = PROJECT / ".runtime/phase2-d1-evidence" / ("native-" + identifier + ".json")
    evidence = {"runtime": str(root), "binary": runner.binary, "policy": policy(), "passed": False}
    coordinator = None

    def save():
        evidence_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False))

    def submit(name, prompt):
        directory = root / "fixtures" / name
        directory.mkdir(parents=True)
        content = ("phase2-d1: " + name + "-" + identifier + "\n").encode()
        for file in ["expected.txt", "result.txt"]:(directory / file).write_bytes(content)
        return runner.store.submit(prompt, workspace={"fixture": name}, acceptance={
            "artifacts": [{"path": "result.txt", "sha256": hashlib.sha256(content).hexdigest()}],
            "tests": ["workspace-result-check"]})

    try:
        ids = [submit(name, "Read expected.txt, then execute a Python command that prints the marker and sleeps 90 seconds. "
            "Do not write any files. This is an isolated process-lifecycle fixture.") for name in ["left", "right"]]
        evidence["task_ids"] = ids
        command = [sys.executable, "-B", str(PROJECT / "src/runner.py"), "--runtime", str(root),
                   "--codex", runner.binary, "run-pair", *ids, "--timeout", "180"]
        coordinator = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        started = set()
        deadline = time.monotonic() + 90
        while len(started) < 2 and time.monotonic() < deadline:
            for task_id in ids:
                task = runner.store.get(task_id)
                if task["state"] not in ["QUEUED", "RUNNING"]:
                    raise RuntimeError("Fixture ended before injection: " + task["state"] + " " + str(task["error"]))
                if task["log_path"]:
                    for line in Path(task["log_path"]).read_text().splitlines():
                        try: message = json.loads(line)["message"]
                        except (ValueError, KeyError):continue
                        item = message.get("params", {}).get("item", {})
                        if (message.get("method") == "item/started" and item.get("type") == "commandExecution"
                                and "sleep" in str(item.get("command", ""))):
                            started.add(task_id)
            time.sleep(.1)
        if len(started) != 2: raise RuntimeError("Both native sleep commands were not observed")
        evidence["before_kill"] = [runner.store.get(t) for t in ids]
        evidence["coordinator_pid"] = coordinator.pid
        evidence["both_native_commands_started"] = True
        save()
        coordinator.kill()  # Only this Popen-owned experiment process is targeted.
        coordinator.communicate(timeout=5)
        recovered = Runner(root)
        try:
            deadline = time.monotonic() + 10
            while True:
                recover_abandoned(root, recovered.store)
                facts = [resource_facts(recovered.store.execution(recovered.store.get(t)["active_execution_id"]), recovered.store.get(t)) for t in ids]
                states = [recovered.store.get(t)["state"] for t in ids]
                if (all(f["released"] for f in facts) and states == ["UNKNOWN", "UNKNOWN"]) or time.monotonic() >= deadline: break
                time.sleep(.1)
            states = [recovered.store.get(t)["state"] for t in ids]
            before_count = recovered.store.db.execute("SELECT count(*) FROM executions").fetchone()[0]
            denied = []
            for task_id in ids:
                try: recovered.run(task_id, 1)
                except ValueError:denied.append(task_id)
            evidence["after_kill"] = {"states": states, "resources": facts, "unknown_reruns_denied": denied,
                "execution_count_unchanged": before_count == recovered.store.db.execute("SELECT count(*) FROM executions").fetchone()[0],
                "integrity": recovered.store.db.execute("PRAGMA integrity_check").fetchone()[0],
                "foreign_key_errors": len(recovered.store.db.execute("PRAGMA foreign_key_check").fetchall())}
            evidence["kill_passed"] = states == ["UNKNOWN", "UNKNOWN"] and len(denied) == 2 and all(f["released"] for f in facts)
        finally:recovered.store.close()
        save()
        normal_id = submit("normal", "Read expected.txt and result.txt, verify exact byte equality with Python, and return the marker. Do not modify files.")
        normal = runner.run(normal_id, 180)
        evidence["normal"] = normal
        evidence["passed"] = evidence["kill_passed"] and normal["completion_verified"]
        evidence["model_turn_starts"] = sum(1 for task in runner.store.db.execute("SELECT log_path FROM tasks") if task[0]
            for line in Path(task[0]).read_text().splitlines()
            if json.loads(line).get("direction") == "send" and json.loads(line).get("message", {}).get("method") == "turn/start")
        save()
        print(json.dumps({"evidence": str(evidence_path), "passed": evidence["passed"], "kill_passed": evidence["kill_passed"], "normal_state": normal["state"]}))
        return 0 if evidence["passed"] else 1
    finally:
        if coordinator and coordinator.poll() is None:
            coordinator.terminate()
            try:coordinator.communicate(timeout=10)
            except subprocess.TimeoutExpired:coordinator.kill();coordinator.communicate(timeout=3)
        save()
        runner.store.close()


if __name__ == "__main__":raise SystemExit(main())
