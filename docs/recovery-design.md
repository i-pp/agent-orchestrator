# Recovery and Reconciliation

Determine what happened before deciding what to execute next. Never treat `UNKNOWN` as permission to rerun. The [Japanese historical source](recovery-design_jp.md) retains the original phase evidence.

## Identity and durable records

| ID | Meaning |
| --- | --- |
| `task_id` | Objective, prompt, acceptance criteria |
| `execution_id` | One claimed execution; a distinct UUID |
| `thread_id` / `session_id` | Saved Codex conversation; legacy task session ID remains |
| `turn_id` | One generation in that conversation |
| `reconciliation_id` | Evidence gathered at a particular point |
| `decision_id` | Decision with actor and reason |

Unique `executions.task_id` rejects a second execution of the same task. Resume uses a new task/execution rather than restoring/repeating the original; `clientUserMessageId` also uses the execution ID.

Phase 2-A/B used schema v2; v3 adds workspace/role/access/snapshot. See [concurrency](concurrency-design.md) for additive migration and leases. Records separate tasks/acceptance/status/resume approval; execution owner/processes/launch intent/thread/turn/result/manifest/cleanup; transitions; reconciliations; verification fingerprints; and decisions. Legacy `COMPLETED` rows remain historical `LEGACY_UNVERIFIED`, with `completion_verified=false`, not newly verified success.

## Startup, abnormal exit, and cleanup

A runner never recovers another live owner while its lease is held. After acquiring appropriate leases, it marks abandoned `RUNNING`/`VERIFYING` as `UNKNOWN` without resubmitting. Preserve a verified checkpoint's saved Codex success; only in-flight execution status becomes unknown.

Save `launch_pending` before spawn and PID/start afterward. A gap is not proof of release. Boot ID plus PID/start distinguishes PID reuse. Old cleanup does not prove release if a later verifier is alive. Parent-death guards, process groups, kill-child, and PID namespaces support bounded cleanup. Timeout/cancel first sends `turn/interrupt`; unconfirmed checkpoints or surviving owned processes leave `UNKNOWN`. Cleanup does not roll back side effects.

## Reconciliation, not generation

`recover` records uncertainty. `reconcile` saves:

- Owned PID/start/boot and live descendants.
- Current regular-file manifest and differences from the pre-execution manifest.
- Complete runner JSONL records, corruption, and truncated tails.
- Target/latest turns and session state from official `thread/read` and `thread/turns/list` over dedicated stdio.

It never calls `thread/resume` or `turn/start`, nor directly edits/reads Codex internal databases. History is bounded to ten pages of twenty turns; missing evidence is not guessed. The phase checked the [App Server API](https://learn.chatgpt.com/docs/app-server) against installed generated schemas.

Manifests store regular-file sizes/SHA-256, not contents. Limits are 1,000 regular files, 16 MiB each, about 64 MiB total, and 1,000 directories. Exceeded limits, symlinks, or unreadable files make reconciliation uncertain. `UNCHANGED` means only manifest equality, not absence of external, network, or temporary effects.

## Explicit decisions

| Operation | Preconditions and effect |
| --- | --- |
| `resolve verify` | Saved normal turn for unknown/human-waiting work, confirmed cleanup; verify acceptance without a generation |
| `resolve fail` | Close unknown/human-waiting work as failed with a reason |
| `resolve allow_resume` | Terminal execution, latest matching turn, confirmed cleanup; explicitly acknowledge changed/unknown effects; do not submit |
| `submit --resume-of` | Explicit new task; unknown/human-waiting sources need an approval decision ID |
| `run` | Execute the new task once; reconcile again immediately before resume |

Resume approval is single-use and invalidated by workspace changes. Advanced conversation history blocks reuse. Resume adds new input to the latest conversation; it does not restore an interrupted command. Missing legacy evidence must be reported, not automatically approved.

## Migration and rollback

Recognize the Phase-1 tasks/transitions schema, not unknown versions or custom indexes/triggers/views. Under exclusivity, back up through SQLite to `db-backups/phase1-UUID.sqlite`. Follow [SQLite schema-change guidance](https://www.sqlite.org/lang_altertable.html): adjust foreign-key handling outside the transaction, copy/rename/create tables transactionally, run `foreign_key_check`, then commit or roll back. See [Python sqlite3](https://docs.python.org/3.14/library/sqlite3.html).

With all clients stopped and [variables](path-conventions.md) set:

```bash
python3 src/runner.py --runtime "$STATE_DIR" db-rollback "$BACKUP_FILENAME"
```

Rollback acquires exclusivity and requires no newer execution/reconciliation/verification/decision data, plus equality of legacy task columns/transitions with the backup. New tasks or changes reject rollback; there is no discard-data option. Preserve the current DB separately, restore via SQLite backup, and run `integrity_check`, not a force overwrite/reset.

New code remigrates a restored DB. Reverting code requires review of corresponding archived source. The original A/B source archive was `.runtime/phase2-evidence/phase1-source-backup.tar.gz`; no automatic source extraction occurs. Historical migration tests used a copy of twelve Phase-1 rows and dedicated fixtures without altering the original DB.

## Later workspace, scope, and scheduler records

C selects the owned workspace through `task.workspace_id`; recovery must not affect independent work. Respect task/workspace leases and live workers. Resume probes join the current execution's process records and finish before an execution server opens. Released or invalid workspaces are refused for human investigation.

D1 adds owned receipts and cgroup absence/`populated=0` to cleanup evidence. Only UUID scopes under `resource-events` are considered, never arbitrary units, Codex DBs, or existing daemons. A leftover `READY` receipt is not normal completion. D2 also reconciles one-shot dispatch reservations and refuses old-DB restore that would discard durable queue state; see [scheduler](scheduler-design.md).

Historical tests covered fake-server death, parent SIGKILL during execution/verification, JSON truncation, launch gaps, duplicate execution, and migration failure. Real Codex evidence covered parent-loss `UNKNOWN`, official history, and cleanup. OS restart/power loss, disk failure, real network interruption, and recovery of arbitrary external side effects remain outside that evidence.
