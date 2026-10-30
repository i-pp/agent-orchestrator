"""Opt-in Phase 2 native integration, including SIGKILL of an owned runner fixture."""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        parser.error("Use --execute for subscription-backed native tests")
    suffix = uuid.uuid4().hex[:8]
    root = PROJECT / ".runtime" / f"phase2-live-{suffix}"
    evidence = PROJECT / ".runtime/phase2-evidence" / f"live-{suffix}.json"
    base = [sys.executable, str(PROJECT / "src/runner.py"), "--runtime", str(root)]
    calls = []
    report = {"runtime": str(root), "cases": [], "calls": calls, "model_turns_requested": 0}

    def save():
        evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    def cli(args, timeout=90):
        proc = subprocess.run(base + args, cwd=PROJECT, capture_output=True, text=True, timeout=timeout)
        value = json.loads(proc.stdout)
        calls.append({"args": args, "exit_code": proc.returncode, "stderr_bytes": len(proc.stderr)})
        return value

    identity = cli(["check"])
    if "error" in identity:
        raise RuntimeError(identity["error"])
    report["identity"] = identity
    fixture = b"phase1-marker: CERULEAN-17\n"
    (root / "workspace/fixture.txt").write_bytes(fixture)
    nonce = "PHASE2-" + suffix
    policy = {"artifacts": [{"path": "fixture.txt", "sha256": hashlib.sha256(fixture).hexdigest()}],
              "tests": ["fixture-check"], "conditions": [{"kind": "result_contains", "value": nonce}]}
    policy_file = root / "acceptance.json"
    policy_file.write_text(json.dumps(policy))

    def submit(prompt, resume=None):
        args = ["submit", prompt, "--acceptance", str(policy_file)]
        if resume:
            args += ["--resume-of", resume]
        return cli(args)["task_id"]

    def record(name, task, elapsed, **extra):
        report["cases"].append({"name": name, "task": task, "elapsed_s": round(elapsed, 3), **extra})
        save()
        print(name, task.get("state"), flush=True)

    first_id = submit(f"Read only fixture.txt. Remember session nonce {nonce}. Return the file marker and nonce; change nothing.")
    report["model_turns_requested"] += 1
    start = time.monotonic()
    first = cli(["run", first_id, "--timeout", "60"])
    record("normal_verified", first, time.monotonic() - start)
    if first.get("state") != "COMPLETED":
        raise RuntimeError("Native normal task did not pass independent verification")
    resumed_id = submit("Without reading files, return the session nonce I provided in the previous turn.", first_id)
    report["model_turns_requested"] += 1
    start = time.monotonic()
    resumed = cli(["run", resumed_id, "--timeout", "60"])
    record("saved_session_resume", resumed, time.monotonic() - start,
           same_session=resumed.get("session_id") == first.get("session_id"))
    policy_file.write_text(json.dumps({"artifacts": [{"path": "fixture.txt"}]}))
    prompt = "Run exactly `sleep 60` once. Wait for completion. Do not read or change files."

    timed_id = submit(prompt)
    report["model_turns_requested"] += 1
    start = time.monotonic()
    timed = cli(["run", timed_id, "--timeout", "12"])
    record("timeout_cleanup", timed, time.monotonic() - start)

    for kind in ["cancel", "parent_sigkill"]:
        task_id = submit(prompt)
        report["model_turns_requested"] += 1
        start = time.monotonic()
        process = subprocess.Popen(base + ["run", task_id, "--timeout", "60"], cwd=PROJECT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        command_seen = False
        try:
            until = time.monotonic() + 35
            while time.monotonic() < until and process.poll() is None:
                with closing(sqlite3.connect((root / "tasks.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
                    row = db.execute("SELECT log_path FROM tasks WHERE id=?", (task_id,)).fetchone()
                if row and row[0] and Path(row[0]).exists():
                    for line in Path(row[0]).read_text().splitlines():
                        try:
                            message = json.loads(line)["message"]
                        except (ValueError, KeyError):
                            continue
                        item = message.get("params", {}).get("item", {})
                        if message.get("method") == "item/started" and item.get("type") == "commandExecution":
                            command_seen = True
                            break
                if command_seen:
                    break
                time.sleep(0.05)
            if not command_seen:
                raise RuntimeError("No command start at the native fault-injection checkpoint")
            if kind == "cancel":
                cli(["cancel", task_id])
                stdout, stderr = process.communicate(timeout=20)
                task = json.loads(stdout)
                record("cancel_cleanup", task, time.monotonic() - start, command_seen=True,
                       frontend_exit_code=process.returncode)
            else:
                os.kill(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
                task = cli(["show", task_id])
                # Never infer completion from process disappearance.
                facts = cli(["reconcile", task_id], timeout=90)
                decision = cli(["resolve", task_id, "--reconciliation", facts["reconciliation_id"],
                                "--kind", "fail", "--actor", "phase2-live-test",
                                "--reason", "owned runner was SIGKILLed; record failure by explicit decision without rerun"])
                record("parent_sigkill_reconcile", task, time.monotonic() - start, command_seen=True,
                       reconciliation=facts, decision=decision, frontend_exit_code=process.returncode)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)
    cases = {case["name"]: case for case in report["cases"]}
    report["passed"] = (
        first.get("completion_verified") and resumed.get("completion_verified")
        and cases["saved_session_resume"]["same_session"]
        and timed.get("state") == "TIMED_OUT" and not json.loads(timed["cleanup"])["survivors"]
        and cases["cancel_cleanup"]["task"]["state"] == "CANCELLED"
        and not json.loads(cases["cancel_cleanup"]["task"]["cleanup"])["survivors"]
        and cases["parent_sigkill_reconcile"]["task"]["state"] == "UNKNOWN"
        and cases["parent_sigkill_reconcile"]["reconciliation"]["resources"]["released"]
        and cases["parent_sigkill_reconcile"]["decision"]["task_state"] == "FAILED")
    save()
    print("evidence", evidence, "passed", report["passed"], flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
