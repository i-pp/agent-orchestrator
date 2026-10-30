"""Opt-in live integration checks. Each invocation consumes ChatGPT subscription quota."""

import argparse
import json
from pathlib import Path
import sys
import threading
import time
import hashlib

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from runner import Runner, Store
from transport import redact


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("Pass --execute to authorize live subscription-backed checks")
    root = PROJECT / ".runtime/runner-live"
    runner = Runner(root)
    (root / "workspace/fixture.txt").write_text("phase1-marker: CERULEAN-17\n")
    output = PROJECT / ".runtime/evidence/live-checks.json"
    report = {"cases": [], "model_turns_requested": 0}

    def run(name, prompt, *, timeout=60, resume=None, cancel=False):
        policy = {"artifacts": [{"path": "fixture.txt", "sha256": hashlib.sha256(
            b"phase1-marker: CERULEAN-17\n").hexdigest()}]}
        task_id = runner.store.submit(prompt, resume_of=resume, acceptance=policy)
        errors = []
        if cancel:
            def request_cancel():
                deadline = time.monotonic() + 30
                store = Store(root / "tasks.sqlite")
                try:
                    while time.monotonic() < deadline:
                        path = store.get(task_id)["log_path"]
                        if path and Path(path).exists() and '"type": "commandExecution"' in Path(path).read_text():
                            break
                        time.sleep(0.05)
                    else:
                        errors.append("No commandExecution event before cancellation guard")
                    store.cancel(task_id)
                finally:
                    store.close()
            worker = threading.Thread(target=request_cancel)
            worker.start()
        start = time.monotonic()
        report["model_turns_requested"] += 1
        try:
            task = runner.run(task_id, timeout)
        finally:
            if cancel:
                worker.join(timeout=35)
        case = {"name": name, "elapsed_s": round(time.monotonic() - start, 3), "task": task,
                "guard_errors": errors}
        report["cases"].append(case)
        output.write_text(json.dumps(redact(report), ensure_ascii=False, indent=2))
        print(name, task["state"], round(time.monotonic() - start, 3), flush=True)
        return task

    try:
        first = run("normal", "Read only fixture.txt. Remember session nonce RUNNER-CONTINUITY-83. "
                    "Return the marker and the nonce. Do not inspect any other file or change anything.")
        if first["state"] == "COMPLETED":
            run("resume", "Without reading files, return the exact session nonce I supplied in the previous turn.",
                resume=first["id"])
        sleep = "Run exactly `sleep 60` once in the working directory. Wait for completion. Do not inspect or change files."
        run("timeout", sleep, timeout=9)
        run("cancel", sleep, cancel=True)
        run("permission-denial", "Authorized negative sandbox test in this test directory only: run exactly "
            "`python3 -c \"open('blocked-write.txt','w').write('test')\"` once. "
            "Do not request elevated permissions or retry. Report the failure if it is denied.")
        report["blocked_file_exists"] = (root / "workspace/blocked-write.txt").exists()
        fresh = Store(root / "tasks.sqlite")
        try:
            report["db_reload_matches"] = all(fresh.get(c["task"]["id"])["state"] == c["task"]["state"]
                                               for c in report["cases"])
        finally:
            fresh.close()
        expected = {"normal": "COMPLETED", "resume": "COMPLETED", "timeout": "TIMED_OUT",
                    "cancel": "CANCELLED", "permission-denial": "FAILED"}
        report["passed"] = (len(report["cases"]) == 5 and all(
            c["task"]["state"] == expected[c["name"]] and not c["guard_errors"] and
            json.loads(c["task"]["cleanup"])["survivors"] == [] for c in report["cases"]
        ) and not report["blocked_file_exists"] and report["db_reload_matches"] and
            "CERULEAN-17" in first["result"] and all(
                c["task"]["session_id"] == first["session_id"] and "RUNNER-CONTINUITY-83" in c["task"]["result"]
                for c in report["cases"] if c["name"] == "resume"))
        output.write_text(json.dumps(redact(report), ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1
    finally:
        runner.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
