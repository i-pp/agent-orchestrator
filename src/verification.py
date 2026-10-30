"""Task acceptance checks over a bounded workspace and fixed operator-approved commands."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time

from transport import Process, redact
from isolation_guard import IsolationBlocked, require_isolation_creation, check_command

PROJECT = Path(__file__).resolve().parents[1]
REGISTRY = PROJECT / "verification-commands.json"


def command_registry():
    if REGISTRY.is_symlink():
        raise ValueError("Command registry must not be a symlink")
    data = json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("Command registry must be an object")
    for name, spec in data.items():
        if not isinstance(name, str) or not isinstance(spec, dict) or set(spec) != {"argv", "timeout"}:
            raise ValueError("Invalid approved command specification")
        argv = spec["argv"]
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x or "\0" in x for x in argv):
            raise ValueError("Approved argv must contain non-empty strings")
        if not Path(argv[0]).is_absolute() or Path(argv[0]).name in ["sh", "bash", "dash", "zsh", "cmd", "powershell"]:
            raise ValueError("Approved executable must be absolute and must not be a shell")
        if type(spec["timeout"]) not in [int, float] or not 0 < spec["timeout"] <= 60:
            raise ValueError("Verification command timeout must be 0 < seconds <= 60")
    return data


def path_parts(name):
    if not isinstance(name, str) or not name or "\\" in name or "\0" in name:
        raise ValueError("Artifact path must be a relative POSIX path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(x in [".", "..", ""] for x in name.split("/")):
        raise ValueError("Artifact path must stay inside the workspace")
    return path.parts


def read_artifact(workspace, name, maximum=16 * 1024 * 1024):
    parts = path_parts(name)
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise ValueError("Artifact must be a bounded regular file")
            content = stream.read(maximum + 1)
            if len(content) > maximum:
                raise ValueError("Artifact exceeds size limit")
            return content
    finally:
        os.close(directory)


def workspace_snapshot(workspace):
    workspace = Path(workspace)
    files = {}
    problems = []
    total = 0
    if workspace.is_symlink() or not workspace.is_dir():
        payload = {"files": {}, "problems": ["unsafe_workspace_root"]}
        payload["fingerprint"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return payload
    visited = 0
    for parent, directories, names in os.walk(workspace, followlinks=False,
                                            onerror=lambda exc: problems.append("unreadable_directory")):
        visited += 1
        if visited > 1000:
            problems.append("directory_limit")
            break
        for name in directories[:]:
            path = Path(parent) / name
            if path.is_symlink():
                problems.append(f"symlink:{path.relative_to(workspace)}")
                directories.remove(name)
        for name in sorted(names):
            relative = str((Path(parent) / name).relative_to(workspace))
            if len(files) >= 1000 or total > 64 * 1024 * 1024:
                problems.append("snapshot_limit")
                break
            try:
                value = read_artifact(workspace, relative)
                total += len(value)
                files[relative] = {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
            except (OSError, ValueError):
                problems.append(f"unreadable_or_unsafe:{relative}")
        if "snapshot_limit" in problems:
            break
    payload = {"files": files, "problems": sorted(problems)}
    payload["fingerprint"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return payload


def normalize_acceptance(value):
    if value is None:
        value = {"human": True}
    if not isinstance(value, dict) or set(value) - {"artifacts", "tests", "conditions", "human"}:
        raise ValueError("Acceptance supports artifacts, tests, conditions and human only")
    result = {"artifacts": value.get("artifacts", []), "tests": value.get("tests", []),
              "conditions": value.get("conditions", []), "human": value.get("human", False)}
    if type(result["human"]) is not bool or any(not isinstance(result[k], list) for k in ["artifacts", "tests", "conditions"]):
        raise ValueError("Invalid acceptance field types")
    if sum(len(result[k]) for k in ["artifacts", "tests", "conditions"]) > 30:
        raise ValueError("At most 30 checks are allowed")
    for spec in result["artifacts"]:
        if not isinstance(spec, dict) or "path" not in spec or set(spec) - {"path", "sha256"}:
            raise ValueError("Artifact requires path and optional sha256")
        path_parts(spec["path"])
        if "sha256" in spec and (not isinstance(spec["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", spec["sha256"])):
            raise ValueError("Artifact SHA-256 must be 64 lowercase hexadecimal characters")
    for condition in result["conditions"]:
        if (not isinstance(condition, dict) or set(condition) != {"kind", "value"}
                or condition["kind"] not in ["result_equals", "result_contains"]
                or not isinstance(condition["value"], str) or not condition["value"]):
            raise ValueError("Condition must be a non-empty result_equals/result_contains check")
    registry = command_registry()
    if any(not isinstance(name, str) or name not in registry for name in result["tests"]):
        raise ValueError("Only pre-approved test command IDs are allowed")
    result["commands"] = {name: registry[name] for name in result["tests"]}
    # Round-trip so callers cannot mutate the policy after submission.
    return json.loads(json.dumps(result))


def verification_launch(root, argv):
    """Read-only verifier namespace: no original or isolated credentials are visible."""
    require_isolation_creation('verification-bwrap')
    import shutil
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise RuntimeError("Verification isolation unavailable; no unisolated fallback")
    root = Path(root)
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(Path.home()),
           "PYTHONDONTWRITEBYTECODE": "1"}
    return [bwrap, "--die-with-parent", "--unshare-user", "--unshare-pid", "--ro-bind", "/", "/",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--unshare-net",
            "--tmpfs", "/run/user", "--tmpfs", str(Path.home() / ".codex"), "--tmpfs", str(root / "codex-home"),
            "--chdir", str(root / "workspace"), "--", *argv], env


def verify(root, policy, result, *, cancelled=lambda: False, observer=None, log=None,
           launching=lambda: None, deadline=None, context=None, isolated=False, resource_factory=lambda: None, launcher=None):
    root = Path(root)
    evidence = {"artifacts": [], "tests": [], "conditions": [], "human_required": policy["human"]}
    stop_reason = None
    deadline = deadline if deadline is not None else time.monotonic() + 60
    for spec in policy["artifacts"]:
        if cancelled() or time.monotonic() >= deadline:
            stop_reason = "CANCELLED" if cancelled() else "TIMED_OUT"
            break
        item = {"path": spec["path"]}
        try:
            content = read_artifact(root / "workspace", spec["path"])
            digest = hashlib.sha256(content).hexdigest()
            item.update(passed=("sha256" not in spec or digest == spec["sha256"]),
                        bytes=len(content), sha256=digest, expected_sha256=spec.get("sha256"))
        except (OSError, ValueError) as exc:
            item.update(passed=False, reason=type(exc).__name__)
        evidence["artifacts"].append(item)
    registry = command_registry()
    for name in policy["tests"]:
        if stop_reason or cancelled() or time.monotonic() >= deadline:
            stop_reason = stop_reason or ("CANCELLED" if cancelled() else "TIMED_OUT")
            break
        spec = policy["commands"][name]
        item = {"id": name, "argv": spec["argv"], "timeout": spec["timeout"]}
        if registry.get(name) != spec:
            item.update(passed=False, reason="Approved command policy changed since submission")
            evidence["tests"].append(item)
            continue
        proc = None
        start = time.monotonic()
        stop = None
        try:
            if launcher:
                argv, env = launcher(spec["argv"])
            elif isolated:
                from isolation import isolated_launch
                argv, env = isolated_launch(root, spec["argv"][0], spec["argv"][1:], authenticate=False)
            else:
                argv, env = verification_launch(root, spec["argv"])
            check_command(argv)
            launching()
            proc = Process(argv, cwd=root / "workspace", env=env, log=log,
                           raw_output=True, on_observed=observer, context=context, resources=resource_factory())
            proc.proc.stdin.close()
            while True:
                if cancelled():
                    stop = "CANCELLED"
                    break
                if time.monotonic() - start >= spec["timeout"] or time.monotonic() >= deadline:
                    stop = "TIMED_OUT"
                    break
                try:
                    proc.next_frame(0.05)
                except EOFError:
                    break
                proc.observe_children()
        except IsolationBlocked as exc:
            item.update(passed=None, executed=False, blocked=exc.evidence)
            evidence['blocked'] = exc.evidence
        except (OSError, RuntimeError) as exc:
            item["reason"] = f"{type(exc).__name__}: {redact(str(exc))}"
        finally:
            cleanup = proc.close(grace=0.1) if proc else None
        resource_result = cleanup.get("resources", {}) if cleanup else {}
        if resource_result and (resource_result.get("exceeded") or not resource_result["confirmed"] or not resource_result["released"]):
            item["reason"] = "Resource limit or unconfirmed supervision"
        item.update(passed=(None if item.get('blocked') else bool(cleanup and cleanup["exit_code"] == 0 and not cleanup["survivors"] and not stop
                                and "reason" not in item)), cleanup=cleanup, stop_reason=stop,
                    elapsed_s=round(time.monotonic() - start, 4))
        evidence["tests"].append(item)
        if evidence.get('blocked'):
            break
        if stop:
            stop_reason = stop
            break
    for condition in policy["conditions"]:
        passed = (result == condition["value"] if condition["kind"] == "result_equals"
                  else condition["value"] in result)
        evidence["conditions"].append({**condition, "passed": passed,
                                       "result_sha256": hashlib.sha256(result.encode()).hexdigest()})
    evidence["independent_checks"] = len(policy["artifacts"]) + len(policy["tests"])
    rows = evidence["artifacts"] + evidence["tests"] + evidence["conditions"]
    evidence["all_passed"] = (len(rows) == len(policy["artifacts"]) + len(policy["tests"]) + len(policy["conditions"])
                              and all(item["passed"] for item in rows))
    if evidence.get('blocked'):
        evidence.update(status='BLOCKED', quality_verdict='UNASSESSED', all_passed=False)
    evidence["human_required"] |= evidence["independent_checks"] == 0
    snapshot = workspace_snapshot(root / "workspace")
    evidence["workspace"] = snapshot
    evidence["stop_reason"] = ("CANCELLED" if cancelled() else stop_reason or (
        "TIMED_OUT" if time.monotonic() >= deadline else None))
    if snapshot["problems"]:
        evidence["human_required"] = True
        if isolated:
            evidence["all_passed"] = False
    return redact(evidence)
