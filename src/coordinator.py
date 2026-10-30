"""Foreground two-worker coordinator. No daemon or reusable server connection."""

import json
import math
import os
from pathlib import Path
import signal
import sys
import time

from concurrency import recover_abandoned
from storage import Store
from transport import Process


def run_pair(root, binary, task_ids, timeout=120):
    if len(task_ids) != 2 or len(set(task_ids)) != 2 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("run-pair requires two distinct tasks and a positive finite timeout")
    store = Store(Path(root) / "tasks.sqlite")
    workers = []
    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    handlers = {sig: signal.signal(sig, stop) for sig in [signal.SIGINT, signal.SIGTERM]}
    try:
        tasks = [store.get(t) for t in task_ids]
        if any(t["state"] != "QUEUED" or not t["workspace_id"] for t in tasks):
            raise ValueError("run-pair accepts only queued isolated tasks")
        if len({t["workspace_id"] for t in tasks}) != 2:
            raise ValueError("Concurrent tasks require different workspaces")
        for task in tasks:
            argv = [sys.executable, "-B", str(Path(__file__).with_name("runner.py")),
                    "--runtime", str(root), "--codex", binary, "run", task["id"], "--timeout", str(timeout)]
            # Preserve executable discovery used by the parent's installed-
            # binary check; pass no credentials or unrelated environment.
            workers.append(Process(argv, cwd=root, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}, raw_output=True))
        deadline = time.monotonic() + timeout + 30
        while any(worker.proc.poll() is None for worker in workers):
            if stopped or time.monotonic() >= deadline:
                for task_id in task_ids:
                    store.cancel(task_id)
            for worker in workers:
                try:
                    worker.next_frame(0.02)
                except EOFError:
                    pass
            if time.monotonic() >= deadline + 5:
                break
    finally:
        cleanup = [worker.close(grace=0.1) for worker in workers]
        recover_abandoned(root, store)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        store.close()
    store = Store(Path(root) / "tasks.sqlite")
    try:
        return {"tasks": [store.get(t) for t in task_ids], "workers": cleanup,
                "stopped": stopped, "limit": 2}
    finally:
        store.close()
