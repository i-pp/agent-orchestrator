# Retention and Storage Management

This reference describes the Phase 2-D1/D2 retention boundary, not an automated deletion policy. The [Japanese historical source](retention-policy_jp.md) is unchanged. Set [path variables](path-conventions.md) before using examples.

## Current behavior

No automatic deletion, pruning, compression, or rotation is implemented. Workspace `release` prevents reuse but keeps files. At capacity, reject new execution or stop current execution; never delete evidence to make space.

```bash
cd "$PROJECT_ROOT"
python3 -B src/runner.py --runtime "$STATE_DIR" usage
```

JSON inventory reports apparent bytes, allocated bytes, and file counts for totals, logs, snapshots, workspaces, DB backups, resource events, and other categories. It uses `lstat`/mount information without reading authentication or Codex-history contents. It does not follow symlinks or nested mounts; incomplete measurement is reported as a problem.

Only the runner's `tasks.sqlite` is queried for protected `UNKNOWN`, `WAITING_FOR_HUMAN`, `QUEUED`, `RUNNING`, and `VERIFYING` work. Normal startup reconciliation still runs and may mark an absent owner unknown. Inventory itself does not move/delete files.

## Proposed retention, not automatically applied

| Data | Proposed minimum | Preconditions for cleanup |
| --- | --- | --- |
| Normal completed logs and resource receipts | 30 days after completion | Preserve/verify result, acceptance, decisions, and related IDs |
| Snapshots | 30 days after last reference ends | No unfinished reviewer, resume decision, or unresolved source reference |
| Workspace/dedicated Codex home | 30 days after termination/release | All descendant tasks ended, no resume needed, resources released |
| DB migration backups | 90 days; keep latest valid generation | Record schema/matching source and verify recoverability |
| Unknown/human-waiting/unresolved failures | Indefinite until resolved | Age alone never authorizes cleanup |

The 2 GiB runtime and 1 GiB free-space budgets are stop boundaries, not cleanup authorization or host-wide guarantees. Snapshot copies and backups count toward usage. Before future automation, define capacity warnings, dependency closure of protected references, destination capacity, and a candidate-only dry run.

## Proposed manual procedure

1. Stop owned runtime workers and use recover/reconcile to inspect resources and unresolved state. Do not stop an existing Codex daemon.
2. Preserve usage/show output and protected task/execution/workspace/snapshot/receipt IDs.
3. Follow `resume_of`, snapshot source IDs, and reviewer snapshot references to protect dependent evidence too.
4. Back up the owned SQLite DB through its backup API and bind artifacts/logs/configuration to the source commit. Do not copy authentication files.
5. Review a sanitized target list and cleanup plan. Do not edit Codex internals or credentials to manufacture consistency.
6. Perform only separately authorized manual cleanup and record action, reason, and recovery destination. This document did not implement or execute such deletion.

Empty tmp/jail mountpoints and released kernel scopes differ from retained artifacts. Test `TemporaryDirectory` cleanup is fixture teardown, not operational retention. Capacity/recovery checks cannot identify arbitrary confidential contents; external storage/publication needs separate authorization and secret review.

D2 also retains scheduler state/options/dispatches, daemon JSONL, and daemon/worker resource receipts. Reconcile both worker scopes and tasks for lost reservations/unknown work. Never delete a reservation or nonce to reclaim the task. No automatic table pruning is added; preserve existing evidence when stopping new dispatch.
