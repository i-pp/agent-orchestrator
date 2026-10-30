"""Opt-in real Codex integration: eight model turns, dedicated fixture state only."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from runner import Runner
from coordinator import run_pair


def main():
    run_id = str(uuid.uuid4())[:8]
    root = PROJECT / ".runtime" / ("phase2-c-live-" + run_id)
    runner = Runner(root)
    evidence = {"run_id": run_id, "runtime": str(root), "binary": runner.binary, "cases": {}, "tasks": []}
    output = PROJECT / ".runtime/phase2-c-evidence" / ("live-" + run_id + ".json")
    output.parent.mkdir(exist_ok=True)
    children = []

    def save():
        evidence["tasks"] = [dict(r) for r in runner.store.db.execute("SELECT * FROM tasks ORDER BY created")]
        output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False))

    def fixture(name, complete=False):
        directory = root / "fixtures" / name
        directory.mkdir(parents=True)
        value = ("phase2-c: " + name + "-" + run_id + "\n").encode()
        (directory / "expected.txt").write_bytes(value)
        if complete:
            (directory / "result.txt").write_bytes(value)
        return {"artifacts": [{"path": "result.txt", "sha256": hashlib.sha256(value).hexdigest()}],
                "tests": ["workspace-result-check"]}

    def worker(task_id, timeout=180):
        p = subprocess.Popen([sys.executable, "-B", str(PROJECT / "src/runner.py"), "--runtime", str(root),
                              "--codex", runner.binary, "run", task_id, "--timeout", str(timeout)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(p)
        return p

    def finish(child):
        out, err = child.communicate(timeout=210)
        if child.returncode not in [0, 2]:
            raise RuntimeError("Worker failed: " + out[-1500:] + err[-500:])
        return json.loads(out)

    def wait_command(task_id, limit=60):
        until = time.monotonic() + limit
        while time.monotonic() < until:
            task = runner.store.get(task_id)
            if task["log_path"]:
                lines = Path(task["log_path"]).read_text().splitlines()
                for line in lines:
                    try:
                        message = json.loads(line)["message"]
                    except (ValueError, KeyError):
                        continue
                    item = message.get("params", {}).get("item", {})
                    if message.get("method") == "item/started" and item.get("type") == "commandExecution":
                        return
            if task["state"] not in ["QUEUED", "RUNNING"]:
                raise RuntimeError("Task ended before command: " + task["state"] + " " + str(task["error"]))
            time.sleep(.1)
        raise TimeoutError("Native command start not observed")

    try:
        first = []
        for name in ["alpha", "beta"]:
            policy = fixture(name)
            prompt = ("In this dedicated fixture only, read expected.txt. Execute a Python command that sleeps 8 seconds "
                      "then writes result.txt with exactly the bytes of expected.txt. Verify byte equality using Python. "
                      "Do not modify expected.txt. Return a concise result with the marker read from the file.")
            first.append(runner.store.submit(prompt, acceptance=policy, workspace={"fixture": name, "access": "workspace-write"}))
        paired = run_pair(root, runner.binary, first, 180)
        rows = paired["tasks"]
        executions = [runner.store.execution(t["active_execution_id"]) for t in rows]
        passed = (all(t["completion_verified"] for t in rows) and
                  len({t["session_id"] for t in rows}) == 2 and len({t["workspace_id"] for t in rows}) == 2 and
                  max(e["started"] for e in executions) < min(e["finished"] for e in executions))
        evidence["cases"]["parallel_write"] = {"passed": passed, "task_ids": first, "executions": executions,
                                                "worker_cleanup": paired["workers"]}
        save()
        if not passed:
            raise RuntimeError("Parallel native fixture did not pass")
        source = rows[0]
        snapshot = runner.snapshot(source["id"])
        policy = json.loads(source["acceptance"])
        policy.pop("commands")
        marker = "REVIEW-" + run_id
        policy["conditions"] = [{"kind": "result_equals", "value": marker}]
        reviewer = runner.store.submit("Independently review this frozen snapshot. Read expected.txt and result.txt, "
            "verify byte equality with a Python command. Do not modify files. If verified, return exactly " + marker,
            acceptance=policy, workspace={"role": "reviewer", "snapshot_id": snapshot["snapshot_id"]})
        reviewed = runner.run(reviewer, 180)
        evidence["cases"]["snapshot_review"] = {"passed": reviewed["completion_verified"] and
            reviewed["session_id"] != source["session_id"], "task_id": reviewer, **snapshot}
        save()
        resumed = runner.store.submit("Continue this review conversation. Verify the same two files again using Python. "
            "Return exactly " + marker, resume_of=reviewer, acceptance=policy)
        continued = runner.run(resumed, 180)
        evidence["cases"]["isolated_resume"] = {"passed": continued["completion_verified"] and
            continued["session_id"] == reviewed["session_id"], "task_id": resumed}
        save()
        for operation in ["timeout", "cancel"]:
            long_policy = fixture(operation + "-long", True)
            short_policy = fixture(operation + "-other", True)
            long_task = runner.store.submit("Run a Python command that sleeps 60 seconds, then read result.txt and report its marker. "
                "Do not modify files.", acceptance=long_policy, workspace={"fixture": operation + "-long"})
            other_task = runner.store.submit("Run a Python command that sleeps 16 seconds, then read expected.txt and result.txt "
                "and verify byte equality. Report the file marker. Do not modify files.", acceptance=short_policy,
                workspace={"fixture": operation + "-other"})
            a = worker(long_task, 14 if operation == "timeout" else 180)
            b = worker(other_task)
            if operation == "cancel":
                wait_command(long_task)
                evidence["cancel_other_state_at_request"] = runner.store.get(other_task)["state"]
                runner.store.cancel(long_task)
            left, right = finish(a), finish(b)
            expected = "TIMED_OUT" if operation == "timeout" else "CANCELLED"
            evidence["cases"][operation + "_independence"] = {"passed": left["state"] == expected and right["completion_verified"]
                and not json.loads(left["cleanup"] or "{}").get("survivors", ["unknown"]),
                "task_ids": [long_task, other_task], "states": [left["state"], right["state"]]}
            save()
        evidence["passed"] = all(c["passed"] for c in evidence["cases"].values())
        save()
        print(json.dumps({"evidence": str(output), "passed": evidence["passed"],
                          "cases": {k:v["passed"] for k,v in evidence["cases"].items()}}))
        return 0 if evidence["passed"] else 1
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                try: child.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill(); child.communicate(timeout=3)
        save()
        runner.store.close()


if __name__ == "__main__": raise SystemExit(main())
