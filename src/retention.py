"""Storage inventory only. There is deliberately no delete/prune operation."""

from resources import usage


def report(root, store):
    categories = ["logs", "snapshots", "workspaces", "db-backups", "resource-events", "codex-home", "workspace", "tmp", "fixtures"]
    protected = [dict(row) for row in store.db.execute("""SELECT id,state,workspace_id,snapshot_id,active_execution_id
        FROM tasks WHERE state IN ('UNKNOWN','WAITING_FOR_HUMAN','QUEUED','RUNNING','VERIFYING') ORDER BY created""")]
    return {"runtime": str(root), "total": usage(root),
            "categories": {name: usage(root / name) for name in categories if (root / name).exists()},
            "protected_tasks": protected, "automatic_deletion": False,
            "measurement": "lstat apparent bytes and st_blocks*512; no content read, no symlink/mount traversal; sampled, not quota"}
