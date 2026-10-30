"""Explicit disposable PoC mode: one bubblewrap boundary, never a fallback."""

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from isolation_guard import require_isolation_creation

PROJECT = Path(__file__).resolve().parents[1]
MODE = "development-poc-bwrap"
MARKER = "development-poc.json"


def configure(root, enabled=False):
    root = Path(root).expanduser().absolute()
    marker = root / MARKER
    if not enabled:
        if root.name.startswith("development-poc-") or marker.exists() or marker.is_symlink():
            raise ValueError("PoC runtime requires explicit --development-poc")
        return
    if (any(p.is_symlink() for p in [root, *root.parents])
            or root.parent != PROJECT / ".runtime"
            or not re.fullmatch(r"development-poc-[A-Za-z0-9_-]{1,80}", root.name)):
        raise ValueError("PoC requires a dedicated .runtime/development-poc-NAME directory")
    expected = {"schema": 1, "mode": MODE, "runtime": str(root)}
    root.parent.mkdir(mode=0o700, exist_ok=True)
    if not root.exists(): root.mkdir(mode=0o700)
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("PoC runtime must be private and operator-owned")
    if not marker.exists():
        if list(root.iterdir()):
            raise ValueError("Cannot convert existing state to PoC mode")
        with marker.open("x") as stream: json.dump(expected, stream)
        marker.chmod(0o600)
    info = marker.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 1024
            or json.loads(marker.read_text()) != expected):
        raise ValueError("Invalid PoC runtime declaration")


def validate_task(task):
    request = json.loads(task.get("workspace_request") or "null")
    if (not task.get("workspace_id") or not isinstance(request, dict) or not request.get("fixture")
            or task.get("role") != "implementer" or task.get("resume_of")):
        raise ValueError("PoC accepts only explicitly named test fixtures, without resume/review")


def launch(runtime, root, binary, args, *, access="read-only", authenticate=False):
    require_isolation_creation('development-poc-bwrap')
    configure(runtime, True)
    root = Path(root).absolute()
    if (root.parent != Path(runtime) / "workspaces"
            or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", root.name)
            or any(p.is_symlink() for p in [root, *root.parents])):
        raise ValueError("PoC execution requires its dedicated task workspace")
    if access not in ["read-only", "workspace-write"]:
        raise ValueError("Unsupported PoC access")
    binary = Path(binary).resolve(strict=True)
    if not (binary.is_relative_to("/usr") or binary == Path(shutil.which('codex') or '/missing').resolve()):
        raise ValueError("PoC executable must be a system tool or installed Codex")
    # The private /sys hides host cgroup topology. Bound CPU affinity as well as
    # cgroup quota so native runtimes cannot size thread pools from all host CPUs.
    cpu = min(os.sched_getaffinity(0))
    argv = ["/usr/bin/taskset", "--cpu-list", str(cpu), "/usr/bin/bwrap", "--die-with-parent", "--unshare-user", "--unshare-pid", "--unshare-ipc"]
    if not authenticate: argv += ["--unshare-net"]
    argv += ["--clearenv"]
    for key, value in {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(root / "codex-home"),
                       "CODEX_HOME": str(root / "codex-home"), "TMPDIR": "/tmp",
                       "PYTHONDONTWRITEBYTECODE": "1"}.items():
        argv += ["--setenv", key, value]
    argv += ["--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc"]
    for name in ["bin", "sbin", "lib", "lib64"]:
        path = Path("/") / name
        if path.is_symlink(): argv += ["--symlink", os.readlink(path), str(path)]
        elif path.exists(): argv += ["--ro-bind", str(path), str(path)]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
             "--ro-bind", str(root), str(root)]
    workspace = str(root / "workspace")
    argv += ["--bind" if access == "workspace-write" else "--ro-bind", workspace, workspace]
    if authenticate:
        home = str(root / "codex-home")
        argv += ["--bind", home, home]
        source = Path.home() / ".codex/auth.json"
        info = source.stat()
        if source.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Existing private ChatGPT authentication is required")
        target = root / "codex-home/auth.json"
        if not target.exists():
            with target.open("xb"): pass
            target.chmod(0o600)
        if target.is_symlink() or target.stat().st_size:
            raise ValueError("PoC authentication mountpoint must be empty")
        argv += ["--ro-bind", str(source), str(target)]
        resolver = Path("/etc/resolv.conf").resolve()
        if resolver.is_relative_to("/run"):
            argv += ["--ro-bind", str(resolver), str(resolver)]
    if not binary.is_relative_to("/usr"):
        argv += ["--ro-bind", str(binary.parent), str(binary.parent)]
    argv += ["--chdir", workspace, "--", str(binary), *args]
    # No API key / parent environment is inherited, and authentication is never copied.
    return argv, {"PATH": "/usr/bin:/bin", "HOME": str(root / "codex-home")}


def version(runtime, root, binary, timeout):
    argv, env = launch(runtime, root, binary, ["--version"])
    return subprocess.run(argv, env=env, capture_output=True, text=True, check=True, timeout=timeout).stdout.strip()
