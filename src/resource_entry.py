"""Trusted supervisor inside a newly limited systemd scope; never prints secrets."""

import ctypes
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys

from resources import counters, violation


def main():
    receipt, limits, owner = Path(sys.argv[1]), json.loads(sys.argv[2]), int(sys.argv[3])
    parent = os.getppid()
    ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0)
    ancestors = set()
    pid = parent
    while pid > 1 and pid not in ancestors:
        ancestors.add(pid)
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        pid = int(fields[1])
    if os.getppid() != parent or owner not in ancestors:
        raise RuntimeError("Resource supervisor lost its owning worker")
    relative = Path('/proc/self/cgroup').read_text().strip().split('::')[1]
    unit = receipt.name[:-5]
    cgroup = Path('/sys/fs/cgroup') / relative.lstrip('/')
    if cgroup.name != unit or cgroup.parent.name != "agentorchestrator.slice":
        raise RuntimeError("Process was not admitted to its private scope")
    expected = {"memory.max": str(limits["memory_bytes"]), "memory.swap.max": "0", "pids.max": str(limits["pids"]),
                "cpu.max": str(limits["cpu_percent"] * 1000) + " 100000"}
    for name, value in expected.items():
        if (cgroup / name).read_text().strip() != value:
            raise RuntimeError("Effective cgroup limit differs from requested policy: " + name)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (limits["file_bytes"], limits["file_bytes"]))
    data = {"unit": unit, "cgroup": str(cgroup), "limits": limits, "effective": expected,
            "rlimit_fsize": list(resource.getrlimit(resource.RLIMIT_FSIZE)), "state": "READY"}

    def save():
        temporary = receipt.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(data, stream)
            stream.flush(); os.fsync(stream.fileno())
        temporary.replace(receipt)

    data["counters"] = counters(cgroup)
    save()
    child = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("child_entry.py")), str(os.getpid()), *sys.argv[4:]])
    def stop(signum, frame):
        child.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    result = child.wait()
    data.update(state="FINISHED", exit_code=result, counters=counters(cgroup))
    data["violation"] = violation(data["counters"], limits) or ("RLIMIT_FSIZE" if result == -signal.SIGXFSZ else None)
    save()
    return result if result >= 0 else 128 - result


if __name__ == "__main__":
    raise SystemExit(main())
