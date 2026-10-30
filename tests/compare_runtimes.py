"""Explicit live comparison; not collected by unittest. Uses subscription quota."""

import json
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from isolation import identity, launch, prepare
from transport import Process

PROJECT = Path(__file__).resolve().parents[1]
PROMPT = ("Read only fixture.txt in the working directory. Remember session nonce ORCH-CONTINUITY-49. "
          "Return the file marker and this nonce. Do not inspect other files or change anything.")
RESUME = "Without reading files, return the exact session nonce I supplied in the previous turn."


def cli_case(root, binary, name, prompt, session=None, timeout=90):
    args = ["--no-daemon", "-a", "never", "-s", "read-only", "exec"]
    if session:
        args += ["resume", "--json", "--skip-git-repo-check", session, prompt]
    else:
        args += ["--json", "--skip-git-repo-check", prompt]
    argv, env = launch(root, binary, args)
    events = []
    start = time.monotonic()
    with (root / f"logs/{name}.jsonl").open("w") as log:
        proc = Process(argv, cwd=root / "workspace", env=env, log=log)
        # exec appends piped stdin to the prompt and waits for EOF before starting.
        proc.proc.stdin.close()
        expired = False
        try:
            while True:
                if time.monotonic() - start > timeout:
                    expired = True
                    break
                try:
                    event = proc.next_frame(0.2)
                except EOFError:
                    break
                if event:
                    events.append(event)
                    proc.observe_children()
        finally:
            cleanup = proc.close()
    return {"name": name, "elapsed_s": round(time.monotonic() - start, 3),
            "timed_out": expired, "events": events, "cleanup": cleanup,
            "session_id": next((x.get("thread_id") for x in events if x.get("type") == "thread.started"), None)}


def app_start(root, binary, name):
    log = (root / f"logs/{name}.jsonl").open("w")
    argv, env = launch(root, binary, ["app-server", "--listen", "stdio://"])
    proc = Process(argv, cwd=root / "workspace", env=env, log=log)
    proc.call("initialize", {"clientInfo": {"name": "agent_orchestrator_phase1", "version": "0.1.0"},
                             "capabilities": {"experimentalApi": False}})
    proc.send({"method": "initialized", "params": {}})
    return proc, log


def app_turn(proc, thread, prompt, timeout=90):
    result = proc.call("turn/start", {"threadId": thread, "input": [{"type": "text", "text": prompt}]})
    turn_id = result["turn"]["id"]
    events = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = proc.next_message(0.2)
        if event:
            events.append(event)
            proc.observe_children()
            if event.get("method") == "turn/completed" and event["params"]["turn"]["id"] == turn_id:
                return {"turn_id": turn_id, "events": events, "terminal": event["params"]["turn"]}
            if "id" in event and "method" in event:
                proc.send({"id": event["id"], "error": {"code": -32601, "message": "Unsupported client request"}})
    proc.call("turn/interrupt", {"threadId": thread, "turnId": turn_id})
    raise TimeoutError("live comparison turn")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        parser.error("Pass --execute for live subscription-backed experiments")
    root, binary = prepare(PROJECT / ".runtime")
    evidence = root / "evidence/comparison.json"
    report = {"identity": identity(root, binary), "cases": []}

    def save(case):
        report["cases"].append(case)
        evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(case["name"], "elapsed", case.get("elapsed_s"), "saved", flush=True)

    first = cli_case(root, binary, "cli-normal", PROMPT)
    save(first)
    if first["session_id"]:
        save(cli_case(root, binary, "cli-resume", RESUME, first["session_id"]))
    start = time.monotonic()
    proc, log = app_start(root, binary, "app-normal")
    thread = None
    try:
        limits = proc.call("account/rateLimits/read", {})
        report["rate_limits_before"] = limits.get("rateLimits", {}).get("primary")
        result = proc.call("thread/start", {"cwd": str(root / "workspace"), "sandbox": "read-only",
                                            "approvalPolicy": "never", "ephemeral": False})
        thread = result["thread"]["id"]
        turn = app_turn(proc, thread, PROMPT)
        status = proc.call("thread/read", {"threadId": thread, "includeTurns": False})["thread"]["status"]
        unsubscribe = proc.call("thread/unsubscribe", {"threadId": thread})
    finally:
        cleanup = proc.close()
        log.close()
    save({"name": "app-normal", "elapsed_s": round(time.monotonic() - start, 3),
          "session_id": thread, "turn": turn, "status": status, "unsubscribe": unsubscribe, "cleanup": cleanup})
    start = time.monotonic()
    proc, log = app_start(root, binary, "app-resume")
    try:
        result = proc.call("thread/resume", {"threadId": thread, "cwd": str(root / "workspace"),
                                             "sandbox": "read-only", "approvalPolicy": "never"})
        turn = app_turn(proc, thread, RESUME)
        limits = proc.call("account/rateLimits/read", {})
        report["rate_limits_after"] = limits.get("rateLimits", {}).get("primary")
        unsubscribe = proc.call("thread/unsubscribe", {"threadId": thread})
    finally:
        cleanup = proc.close()
        log.close()
    save({"name": "app-resume", "elapsed_s": round(time.monotonic() - start, 3),
          "session_id": result["thread"]["id"], "turn": turn, "unsubscribe": unsubscribe, "cleanup": cleanup})


if __name__ == "__main__":
    main()
