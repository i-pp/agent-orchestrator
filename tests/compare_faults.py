"""Explicit CLI fault/cancel experiments; normal comparison is a separate command."""

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from isolation import identity, launch, prepare
from transport import Process


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        parser.error("Pass --execute for live subscription-backed experiments")
    root, binary = prepare(Path(__file__).resolve().parents[1] / ".runtime")
    identity(root, binary)
    results = []
    for name in ["abnormal", "timeout", "cancel"]:
        args = ["--no-daemon", "-a", "never", "-s", "read-only"]
        if name == "abnormal":
            args += ["-c", 'sandbox_mode="invalid-phase1-test"']
        args += ["exec", "--json", "--skip-git-repo-check",
                 "Run exactly `sleep 60` once. Wait for completion. Do not inspect or change files."]
        argv, env = launch(root, binary, args)
        start = time.monotonic()
        events = []
        stop = None
        with (root / f"logs/cli-{name}.jsonl").open("w") as log:
            proc = Process(argv, cwd=root / "workspace", env=env, log=log)
            proc.proc.stdin.close()
            try:
                while True:
                    elapsed = time.monotonic() - start
                    if elapsed > (9 if name == "timeout" else 30):
                        stop = "TIMED_OUT"
                        break
                    try:
                        event = proc.next_frame(0.1)
                    except EOFError:
                        break
                    if event:
                        events.append(event)
                        proc.observe_children()
                        item = event.get("item", {})
                        if name == "cancel" and item.get("type") == "command_execution":
                            stop = "CANCELLED"
                            break
            finally:
                cleanup = proc.close(grace=0.2)
        case = {"name": name, "elapsed_s": round(time.monotonic() - start, 3),
                "stop_reason": stop, "events": events, "cleanup": cleanup}
        results.append(case)
        (root / "evidence/cli-faults.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(name, stop, cleanup["exit_code"], cleanup["survivors"], flush=True)


if __name__ == "__main__":
    main()
