"""Process-scoped leases: two slots, one task and one workspace per worker."""

from contextlib import contextmanager, ExitStack
import fcntl
import os
from pathlib import Path
import uuid


def identifier(value):
    if str(uuid.UUID(value)) != value:
        raise ValueError("Expected a canonical UUID")
    return value


def lease(path, shared=False):
    path = Path(path)
    if path.parent.is_symlink():
        raise ValueError("Lease directory must not be a symlink")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    stream = os.fdopen(fd, "a")
    try:
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise RuntimeError("Resource is leased by another worker") from None
    return stream


@contextmanager
def guard(root, task, *, slot=True):
    root = Path(root)
    with ExitStack() as stack:
        stack.enter_context(lease(root / "runner.lock", shared=True))
        from codex_host import pipeline_held
        if pipeline_held(root): raise RuntimeError('Pipeline execution or cleanup remains unresolved')
        stack.enter_context(lease(root / "leases" / (identifier(task["id"]) + ".lock")))
        stack.enter_context(lease(root / "leases" / ("workspace-" + identifier(task["workspace_id"]) + ".lock")))
        if slot:
            for number in range(2):
                try:
                    held = lease(root / "leases" / f"slot-{number}.lock")
                    break
                except RuntimeError:
                    continue
            else:
                raise RuntimeError("At most two sessions may run concurrently; task remains queued")
            stack.enter_context(held)
        yield


def recover_abandoned(root, store):
    """Never infer death from another worker's live RUNNING database row."""
    recovered = []
    rows = store.db.execute("SELECT * FROM tasks WHERE state IN ('RUNNING','VERIFYING')").fetchall()
    for row in rows:
        task = dict(row)
        try:
            # Shared admission proves there is no legacy exclusive owner,
            # while allowing recovery alongside unrelated isolated workers.
            held = guard(root, task, slot=False) if task.get("workspace_id") else lease(Path(root) / "runner.lock", shared=True)
            with held:
                recovered.extend(store.recover(task["id"]))
        except RuntimeError:
            continue
    return recovered
