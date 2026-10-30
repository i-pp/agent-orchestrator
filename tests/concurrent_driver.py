"""Owned deterministic worker for concurrency and parent-death fault injection."""
import os
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from runner import Runner
from storage import Store

root, task_id, mode = sys.argv[1:4]
if mode == "submit":
    with_store = Store(Path(root) / "tasks.sqlite")
    try:
        print(with_store.submit("idempotent", request_key=task_id, workspace={"fixture": "seed"}), flush=True)
    finally:
        with_store.close()
else:
    def launcher(root, binary, args):
        return [sys.executable, str(Path(__file__).with_name("fake_codex.py")), mode, str(root / "child.pid")], {
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    worker = Runner(root, sys.executable, launcher=launcher)
    try:
        task = worker.run(task_id, float(sys.argv[4]) if len(sys.argv) > 4 else 10)
        print(task["state"], flush=True)
    finally:
        worker.store.close()
