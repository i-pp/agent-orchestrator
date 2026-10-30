"""Kill only this fixture to exercise coordinator -> worker -> server death."""
from pathlib import Path
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from transport import Process

root = Path(sys.argv[1])
workers = [Process([sys.executable, "-B", str(PROJECT / "tests/concurrent_driver.py"),
                    str(root), task, "hang-no-child", "60"], cwd=root,
                   env={"PATH": "/usr/bin:/bin"}, raw_output=True) for task in sys.argv[2:4]]
while True:
    for worker in workers:
        worker.next_frame(.05)
