"""Only deterministic fixtures inject a worker factory; production socket cannot."""

from pathlib import Path
import signal
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from daemon import Daemon, foreground, verify_self_limits
from runner import Runner
from isolation import isolated_launch
from resources import policy

root = Path(sys.argv[1]).resolve()
if "--worker" in sys.argv:
    task_id, token, timeout = sys.argv[3:6]
    source = (PROJECT / "tests/fake_codex.py").read_text()
    probe = Runner(root, sys.executable, launcher=lambda *_: None)
    prompt = probe.store.get(task_id)["prompt"]
    probe.store.close()
    mode = "crash" if prompt.startswith("crash") else "hang-no-child" if prompt.startswith("hang") else "slow-normal"
    def launcher(session, binary, args):
        return isolated_launch(session, sys.executable, ["-B", "-c", source, mode, "/tmp/child.pid"], authenticate=False)
    runner = Runner(root, sys.executable, launcher=launcher, resource_limits=policy())
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, lambda *_: setattr(runner, "signal_cancel", True))
    try: result = runner.run(task_id, float(timeout), dispatch_token=token)
    finally: runner.store.close()
    raise SystemExit(0 if result["state"] == "COMPLETED" else 2)
elif "--serve" in sys.argv:
    verify_self_limits()
    def factory(task, token, timeout):
        return [sys.executable, "-B", str(Path(__file__)), str(root), "--worker", task["id"], token, str(timeout)]
    raise SystemExit(Daemon(root, sys.executable, worker_factory=factory).run())
else:
    raise SystemExit(foreground(root, sys.executable,
        serve_argv=[sys.executable, "-B", str(Path(__file__)), str(root), "--serve"]))
