# Concurrency, Persistence, and Recovery

This reference describes the Phase 2-C execution contract and its D1 resource extension. Historical observations remain scoped to the [C report](phase2-c-report.md) and [D1 report](phase2-d1-report.md). The [Japanese historical source](concurrency-design_jp.md) is unchanged.

## Foreground execution and limits

One task has one worker and at most one dedicated App Server stdio connection at a time. Connections, JSONL, monitoring, and cleanup are not shared. `run-pair` starts two tasks and waits; it is not a daemon or background scheduler. Independently started `run` commands use the same leases.

The limit is **two sessions in the same runtime**. It does not count other runtimes or directly launched Codex processes.

| Lease | Isolated mode | Legacy shared workspace |
| --- | --- | --- |
| `runner.lock` | Shared | Exclusive |
| Task UUID | Exclusive | Serialized by the global lock |
| Workspace UUID | Exclusive | Serialized by the global lock |
| `slot-0` / `slot-1` | One available slot | Global exclusivity excludes other execution |

Nonblocking `flock` contention leaves a task `QUEUED` without starting a model or automatically retrying. Acquisition order is admission, task, workspace, slot; FDs are close-on-exec. Worker death releases leases. Resume takes the original workspace lease, rejecting concurrent resume of the same thread. History probes finish before an execution server opens.

## IDs, events, and cancellation

Task, execution, thread, turn, workspace, and snapshot have separate IDs. Logs include task/execution/workspace/role/snapshot and add thread once known. Notifications must match the expected thread and producing turn. Known historical usage notifications received during resume are recorded as `historical_usage_ignored`, not charged to the current turn. Unknown turns, foreign threads, and mismatched `item/completed` remain protocol errors.

A failure, timeout, or cancellation cleans up only that task's process group. Individual cancellation, runtime-wide `cancel-all`, and stopping `run-pair` are distinct. `cancel-all` fixes its target set inside `BEGIN IMMEDIATE`, cancels queued work, and marks running work `cancel_requested`. `run-pair` SIGINT/SIGTERM targets only its two tasks; SIGKILL relies on the parent-death guard. Cancellation does not roll back prior writes or side effects.

## SQLite and migration

Use standard `sqlite3`, per-connection `foreign_keys=ON`, `synchronous=FULL`, and a five-second timeout. Short transactions, conditional updates, and unique constraints protect concurrency; no new WAL configuration is introduced. Claim atomically updates `QUEUED` and inserts the execution. `executions.task_id` remains unique. Concurrent submission of the same `request_key` is serialized with `BEGIN IMMEDIATE`; identical input returns the same task/workspace.

| Schema-v3 table | Added data |
| --- | --- |
| `tasks` | `workspace_id`, `role`, `access`, `snapshot_id`, `workspace_request` |
| `executions` | `workspace_id`, `role`, `access`, `snapshot_id` |
| `workspaces` | ID, owner task, access, state, snapshot, initial fingerprint, creation/release time |
| `snapshots` | ID, source task/execution, workspace, fingerprint, creation time |

Legacy rows retain null workspace/snapshot IDs, `role=implementer`, and `access=read-only`. Existing workspace locations, history, completed states, evidence, decisions, and IDs are preserved; no new success evidence is invented. Version 0 passes through the existing v2 migration; v2 receives additive columns/tables transactionally. Take a runtime-exclusive lease and SQLite backup, run `foreign_key_check`, then commit. Failure rolls back the schema too; conflicting custom columns are rejected rather than overwritten.

Rollback requires all workers/clients stopped and a verified backup filename:

```bash
python3 src/runner.py --runtime "$STATE_DIR" db-rollback "$BACKUP_FILENAME"
```

A v3-to-v2 rollback is allowed only before workspace/snapshot records exist and when every legacy column/row matches the backup. Preserve the current DB as well. There is no force-restore that discards changes. New code remigrates the restored DB, so reverting code requires a separately reviewed source restore. The original C experiment preserved its prior source under `.runtime/phase2-c-evidence/phase2-ab-source-backup.tar.gz`; that is an evidence identifier, not a portable installed file. After workspace use, preserve state and fix forward.

## Recovery and acceptance

Startup changes `RUNNING`/`VERIFYING` to `UNKNOWN` only after acquiring the task and workspace leases. Live workers are untouched. `UNKNOWN` never starts automatically. Reconciliation, explicit decisions, snapshots, and release take the same workspace lease as resume.

Reconciliation inspects that workspace's manifest and official `thread/read` / `thread/turns/list` through its own Codex home. It does not edit Codex databases, use an existing daemon, or access existing IPC. Resume permission and a new submission are separate; immediately before execution, recheck latest turn, resources, side effects, and the approved fingerprint. Workspace-write changes require explicit side-effect acknowledgement. Acceptance remains limited to the owned workspace; a reviewer verifies an independent snapshot copy and does not change the source task's decision.

## Evidence and later additions

Dedicated fixtures covered two workers, rejection of a third, task exclusion, concurrent request keys, stopping one worker, and parent/coordinator SIGKILL. The C report records real-Codex workspace-write, independent review, same-session resume, and one task timing out/cancelling while the other completed. These are historical results, not new checks run during translation.

D1 added private scopes for Codex trees, probes, and verifiers without increasing the two-session limit. The coordinator passes only executable-search PATH and LANG and retains installed-binary validation. Historical coordinator-SIGKILL evidence respects live owners until parent-death cleanup and lease release finish, then recovery records `UNKNOWN`.

High load, multiple hosts, network filesystems, OS restart, power loss, and other environments were not verified by these phases. Later daemon work is documented separately.
