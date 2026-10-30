"""Owned bounded fixtures and immutable review snapshots (no product checkouts)."""

import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from concurrency import identifier
from isolation import prepare, CONFIG
from verification import read_artifact, workspace_snapshot


def safe_tree(path):
    path = Path(path)
    # Check every ancestor before resolve, including bind mount boundaries.
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise ValueError("Symlink in workspace ancestry")
    if not path.is_dir():
        raise ValueError("Workspace/fixture must be a directory")
    resolved = path.resolve()
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        mountpoint = line.split()[4]
        mountpoint = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), mountpoint)
        mounted = Path(mountpoint)
        if mounted == resolved or mounted.is_relative_to(resolved):
            raise ValueError("Mount boundary inside workspace/fixture")
    snapshot = workspace_snapshot(path)
    if snapshot["problems"]:
        raise ValueError("Unsafe workspace: " + ", ".join(snapshot["problems"]))
    for parent, directories, names in os.walk(path, followlinks=False):
        if ".codex" in directories or ".codex" in names:
            raise ValueError("Project Codex configuration is not allowed in isolated fixtures")
    for name in snapshot["files"]:
        if (path / name).stat(follow_symlinks=False).st_nlink != 1:
            raise ValueError("Hardlinked workspace file is not allowed")
    return snapshot


def copy_tree(source, destination):
    before = safe_tree(source)
    destination.mkdir(mode=0o700)
    for parent, directories, names in os.walk(source, followlinks=False):
        relative = Path(parent).relative_to(source)
        for name in directories:
            (destination / relative / name).mkdir(mode=0o700, exist_ok=True)
    for name in before["files"]:
        content = read_artifact(source, name)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as stream:
            stream.write(content)
        target.chmod(0o600 | ((Path(source) / name).stat().st_mode & 0o100))
    after = safe_tree(source)
    copied = safe_tree(destination)
    if before != after or before["fingerprint"] != copied["fingerprint"]:
        raise ValueError("Source changed while copying; snapshot refused")
    return copied


def spec(fixture=None, access="read-only", role="implementer", snapshot_id=None):
    if access not in ["read-only", "workspace-write"] or role not in ["implementer", "reviewer"]:
        raise ValueError("Unsupported workspace access or role")
    if fixture is not None and (not isinstance(fixture, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fixture)):
        raise ValueError("Fixture must be a simple name under runtime/fixtures")
    if role == "reviewer" and (not snapshot_id or access != "read-only" or fixture):
        raise ValueError("Reviewer requires a snapshot and read-only access")
    if snapshot_id and role != "reviewer":
        raise ValueError("Snapshots are only accepted for independent reviewers")
    if access == "workspace-write" and not fixture:
        raise ValueError("Write tasks require an explicitly named dedicated fixture")
    return {"fixture": fixture, "access": access, "role": role, "snapshot_id": snapshot_id}


def allocate(store, task_id, request):
    root = store.path.parent
    snapshot_id = request["snapshot_id"]
    snapshot = None
    source = None
    if snapshot_id:
        identifier(snapshot_id)
        snapshot = store.db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not snapshot:
            raise ValueError("Snapshot not found")
        source = root / "snapshots" / snapshot_id
        if safe_tree(source)["fingerprint"] != snapshot["fingerprint"]:
            raise ValueError("Snapshot integrity check failed")
    elif request["fixture"]:
        source = root / "fixtures" / request["fixture"]
        safe_tree(source)
    wid = str(uuid.uuid4())
    directory = root / "workspaces"
    if directory.is_symlink():
        raise ValueError("Workspace directory must not be a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    session = directory / wid
    session.mkdir(mode=0o700)
    if source:
        copy_tree(source, session / "workspace")
    prepare(session)
    store.db.execute("INSERT INTO workspaces VALUES(?,?,?,?,?,?,?,?)",
                     (wid, task_id, request["access"], "AVAILABLE", snapshot_id,
                      safe_tree(session / "workspace")["fingerprint"], time.time(), None))
    return wid


def layout(root, store, task):
    if task["access"] not in ["read-only", "workspace-write"] or task["role"] not in ["implementer", "reviewer"]:
        raise ValueError("Unsupported persisted workspace policy")
    if not task.get("workspace_id"):
        if task["access"] != "read-only":
            raise ValueError("Legacy workspace must remain read-only")
        return Path(root)
    try:
        declared = spec(**json.loads(task["workspace_request"]))
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid persisted workspace declaration") from exc
    if any(declared[key] != task[key] for key in ["access", "role", "snapshot_id"]):
        raise ValueError("Persisted workspace declaration does not match task policy")
    wid = identifier(task["workspace_id"])
    row = store.db.execute("SELECT * FROM workspaces WHERE id=?", (wid,)).fetchone()
    if not row or row["state"] != "AVAILABLE" or row["access"] != task["access"] or row["snapshot_id"] != task["snapshot_id"]:
        raise ValueError("Workspace ownership/access metadata is invalid or released")
    ancestor = task
    for _ in range(128):
        if ancestor["id"] == row["owner_task_id"]:
            break
        if not ancestor["resume_of"]:
            raise ValueError("Task does not own this workspace or its resume chain")
        ancestor = store.get(ancestor["resume_of"])
        if ancestor["workspace_id"] != wid:
            raise ValueError("Resume crossed workspace ownership")
    else:
        raise ValueError("Resume ancestry exceeds the safety bound")
    directory = Path(root) / "workspaces" / wid
    for name in [directory, directory / "codex-home", directory / "tmp", directory / "workspace"]:
        if name.is_symlink() or not name.is_dir():
            raise ValueError("Workspace layout is unsafe")
    safe_tree(directory / "workspace")
    config = directory / "codex-home/config.toml"
    if config.is_symlink() or config.read_text() != CONFIG or (directory / "codex-home/auth.json").is_symlink():
        raise ValueError("Private Codex policy or authentication mountpoint changed")
    if task["role"] == "reviewer":
        snapshot = store.db.execute("SELECT * FROM snapshots WHERE id=?", (task["snapshot_id"],)).fetchone()
        if (not snapshot or task["access"] != "read-only" or
                safe_tree(directory / "workspace")["fingerprint"] != snapshot["fingerprint"]):
            raise ValueError("Reviewer snapshot copy changed")
    return directory


def create_snapshot(runner, task_id):
    task = runner.store.get(task_id)
    if not task["workspace_id"] or task["role"] != "implementer" or task["state"] in ["QUEUED", "RUNNING", "VERIFYING", "UNKNOWN"]:
        raise ValueError("Snapshot requires a stopped, isolated implementer with known outcome")
    from recovery import resource_facts
    execution = runner.store.execution(task["active_execution_id"]) if task["active_execution_id"] else None
    if not resource_facts(execution, task)["released"]:
        raise ValueError("Snapshot requires proven resource release")
    source = layout(runner.root, runner.store, task) / "workspace"
    sid = str(uuid.uuid4())
    directory = runner.root / "snapshots"
    if directory.is_symlink():
        raise ValueError("Unsafe snapshot directory")
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / sid
    manifest = copy_tree(source, target)
    for parent, directories, names in os.walk(target, topdown=False):
        for name in names:
            file = Path(parent) / name
            file.chmod(0o400 | (file.stat().st_mode & 0o100))
        Path(parent).chmod(0o500)
    with runner.store.db:
        runner.store.db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?)",
                                (sid, task_id, task["active_execution_id"], task["workspace_id"],
                                 manifest["fingerprint"], time.time()))
    return {"snapshot_id": sid, "source_task_id": task_id, "source_execution_id": task["active_execution_id"],
            "fingerprint": manifest["fingerprint"]}
