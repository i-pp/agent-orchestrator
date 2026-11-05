"""Fail-closed per-process cgroup admission and durable resource evidence.

Disk total is a sampled admission/stop budget, not a filesystem quota.
RLIMIT_FSIZE is a separate hard per-file ceiling. No controller is disabled.
"""

import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT / "resource-limits.json"
KEYS = {"cpu_percent", "cpu_budget_ms", "memory_bytes", "pids", "file_bytes", "runtime_bytes", "free_bytes", "log_bytes"}


class ResourceExceeded(RuntimeError):
    pass


def policy(value=None):
    if value is None:
        if CONFIG.is_symlink():
            raise ValueError("Resource configuration must not be a symlink")
        value = json.loads(CONFIG.read_text())
    if not isinstance(value, dict) or set(value) != KEYS or any(type(x) is not int or x <= 0 for x in value.values()):
        raise ValueError("Every resource limit must be an explicit positive integer")
    if not 1 <= value["cpu_percent"] <= 200 or not 8 <= value["pids"] <= 4096:
        raise ValueError("CPU must be 1..200 percent and pids 8..4096")
    if value["memory_bytes"] < 32 * 1024**2 or value["log_bytes"] < 4096:
        raise ValueError("Memory/log limits are too small for safe supervision")
    if value["file_bytes"] > value["runtime_bytes"]:
        raise ValueError("Per-file ceiling must not exceed runtime storage budget")
    return dict(value)


def usage(path):
    """Metadata only, never follow symlinks or enter nested mounts."""
    root = Path(path)
    if root.is_symlink():
        raise ValueError("Storage root must not be a symlink")
    total = {"apparent_bytes": 0, "allocated_bytes": 0, "files": 0, "symlinks": 0, "problems": []}
    mounts = {Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[4]))
              for line in Path("/proc/self/mountinfo").read_text().splitlines()}
    count = 0
    def scan_error(exc):
        raise ResourceExceeded("Storage scan is incomplete") from exc
    for parent, directories, names in os.walk(root, followlinks=False, onerror=scan_error):
        for name in directories[:]:
            child = Path(parent) / name
            if child.is_symlink() or child.absolute() in mounts:
                directories.remove(name)
                total["problems"].append("link_or_mount:" + str(child.relative_to(root)))
        for name in names:
            count += 1
            if count > 100000:
                raise ResourceExceeded("Storage scan file-count ceiling reached")
            item = Path(parent) / name
            try:
                s = item.lstat()
            except FileNotFoundError:
                continue  # Concurrent creation/removal is sampled again, never deletion by us.
            if item.is_symlink():
                total["symlinks"] += 1
                continue
            total["files"] += 1
            total["apparent_bytes"] += s.st_size
            total["allocated_bytes"] += s.st_blocks * 512
    return total


def storage_check(root, limits):
    measured = usage(root)
    if measured["problems"]:
        raise ResourceExceeded("Storage budget cannot be established across links/mounts")
    if max(measured["allocated_bytes"], measured["apparent_bytes"]) >= limits["runtime_bytes"]:
        raise ResourceExceeded("Runtime storage budget exceeded; retained evidence was not deleted")
    if shutil.disk_usage(root).free < limits["free_bytes"]:
        raise ResourceExceeded("Filesystem free-space admission floor reached")
    return measured


def counters(cgroup):
    path = Path(cgroup)
    result = {}
    for name in ["cpu.stat", "memory.events", "pids.events", "cgroup.events"]:
        result[name] = {k: int(v) for k, v in (line.split() for line in (path / name).read_text().splitlines())}
    for name in ["memory.current", "memory.peak", "pids.current", "pids.peak"]:
        result[name] = int((path / name).read_text())
    memory_stat = path / "memory.stat"
    if memory_stat.exists():
        fields = {"anon", "file", "kernel", "sock", "shmem", "file_mapped", "file_dirty",
                  "file_writeback", "inactive_anon", "active_anon", "inactive_file", "active_file",
                  "slab_reclaimable", "slab_unreclaimable"}
        result["memory.stat"] = {k: int(v) for k, v in
            (line.split() for line in memory_stat.read_text().splitlines()) if k in fields}
    return result


def violation(stats, limits):
    if stats.get("memory.events", {}).get("max", 0):
        return "memory.max"
    if stats.get("pids.events", {}).get("max", 0):
        return "pids.max"
    if stats.get("cpu.stat", {}).get("usage_usec", 0) >= limits["cpu_budget_ms"] * 1000:
        return "cpu_budget_ms"
    return None


class Control:
    def __init__(self, root, limits, execution_id=None, *, scheduler_storage=False):
        self.root = Path(root)
        self.limits = policy(limits)
        self.scheduler_storage = scheduler_storage
        self.unit = "agentorchestrator-" + str(uuid.uuid4()) + ".scope"
        directory = self.root / "resource-events"
        if directory.is_symlink():
            raise ValueError("Resource evidence directory must not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        self.receipt = directory / (self.unit + ".json")
        self.descriptor = {"unit": self.unit, "receipt": str(self.receipt), "limits": self.limits,
                           "execution_id": execution_id, "boot_id": Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
        self.cgroup = None
        self.last = None
        self.exceeded = None
        self.last_scan = 0

    def wrap(self, argv, env):
        if not self.scheduler_storage:
            storage_check(self.root, self.limits)
        command = ["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect", "--no-ask-password",
                   "--expand-environment=no", "--slice=agentorchestrator.slice", "--unit=" + self.unit,
                   "-pCPUQuota=" + str(self.limits["cpu_percent"]) + "%", "-pCPUQuotaPeriodSec=100ms",
                   "-pMemoryMax=" + str(self.limits["memory_bytes"]), "-pMemorySwapMax=0",
                   "-pTasksMax=" + str(self.limits["pids"]), sys.executable, "-B",
                   str(Path(__file__).with_name("resource_entry.py")), str(self.receipt),
                   json.dumps(self.limits), str(os.getpid()), *argv]
        return command, {**env, "XDG_RUNTIME_DIR": "/run/user/" + str(os.getuid())}

    def read(self):
        if self.receipt.is_symlink():
            raise ResourceExceeded("Unsafe resource receipt")
        if self.receipt.exists():
            try:
                data = json.loads(self.receipt.read_text())
                group = Path(data["cgroup"]).resolve()
                if (data["unit"] != self.unit or data["limits"] != self.limits or group.name != self.unit
                        or group.parent.name != "agentorchestrator.slice" or not group.is_relative_to('/sys/fs/cgroup')):
                    raise ValueError("Identity/policy mismatch")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ResourceExceeded("Resource receipt is unreadable or does not match policy") from exc
            self.cgroup = Path(data["cgroup"]).resolve()
            self.last = data
        return self.last

    def check(self):
        data = self.read()
        if data:
            stats = data.get("counters", {})
            if self.cgroup.exists():
                try:
                    stats = counters(self.cgroup)
                except FileNotFoundError:
                    pass  # Final receipt, or conservative missing-receipt result at close.
            cause = violation(stats, self.limits)
            if cause:
                self.exceeded = cause
                raise ResourceExceeded("Resource limit exceeded: " + cause)
        if not self.scheduler_storage and time.monotonic() - self.last_scan >= .2:
            self.last_scan = time.monotonic()
            try:
                storage_check(self.root, self.limits)
            except ResourceExceeded as exc:
                self.exceeded = "runtime_storage"
                raise exc

    def finish(self):
        try:
            self.check()
        except ResourceExceeded:
            pass
        malformed = False
        try:
            data = self.read()
        except (OSError, ValueError, ResourceExceeded):
            data, malformed = self.last, True
        if data:
            self.exceeded = self.exceeded or data.get("violation")
        released = bool(data and not self.cgroup.exists())
        if data and self.cgroup.exists():
            try:
                released = counters(self.cgroup)["cgroup.events"].get("populated") == 0
            except FileNotFoundError:
                released = True
        return {**self.descriptor, "receipt_data": data, "exceeded": self.exceeded,
                "confirmed": bool(data and data.get("state") == "FINISHED" and not malformed), "released": released,
                "receipt_invalid": malformed}


class BudgetLog:
    """Keep complete JSONL records and reserve space for cleanup evidence."""
    def __init__(self, stream, maximum):
        self.stream, self.maximum, self.full = stream, maximum, False

    def write(self, value):
        if self.full:
            return len(value)  # The durable failure and cleanup remain in SQLite/resource receipts.
        if self.stream.tell() + len(value.encode()) > self.maximum - 1024:
            self.full = True
            self.stream.write(json.dumps({"direction": "log_limit", "message": {"maximum_bytes": self.maximum}}) + "\n")
            self.stream.flush()
            os.fsync(self.stream.fileno())
            raise ResourceExceeded("Execution JSONL storage ceiling reached")
        return self.stream.write(value)

    def flush(self): self.stream.flush()
    def fileno(self): return self.stream.fileno()


def scope_released(descriptor):
    """Read only our receipt/cgroup metadata; never stop an arbitrary unit."""
    try:
        unit = descriptor["unit"]
        if not re.fullmatch(r"agentorchestrator-[0-9a-f-]{36}\.scope", unit):
            return False
        receipt = Path(descriptor["receipt"])
        allowed = PROJECT / ".runtime"
        if (receipt.is_symlink() or receipt.parent.name != "resource-events" or
                receipt.name != unit + ".json" or not receipt.resolve().is_relative_to(allowed)):
            return False
        data = json.loads(receipt.read_text())
        group = Path(data["cgroup"]).resolve()
        if (data["unit"] != unit or group.name != unit or group.parent.name != "agentorchestrator.slice"
                or not group.is_relative_to('/sys/fs/cgroup')):
            return False
        return not group.exists() or counters(group)["cgroup.events"].get("populated") == 0
    except (OSError, ValueError, KeyError, TypeError):
        return False
