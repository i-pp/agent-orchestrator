# Concurrent capacity, preserving trial history

Implementation candidate; live migration/restart has not been performed.

Design mistake: cumulative use counts were used as normal operating capacity,
so finished/closed work permanently exhausted a tiny trial grant. Increasing the
cumulative number only delays the same failure. Normal capacity must count work
that is executing or whose stop/result remains unconfirmed, separately from
saved history.

## Two explicit policy meanings

Schema1 retains its exact trial behavior: max_threads/max_tasks are lifetime
counts, including archived threads and cancelled tasks. Its spent counters,
operation/event count bounds and old records are not reset or reinterpreted.

Schema2 keeps the same keys but gives max_threads/max_tasks concurrency meaning.
The proposed normal policy uses **1 active thread / 1 held task / concurrency1 /
120 seconds**. Workspace, model list, existing subscription authentication and
all OS resource/security settings are unchanged. No live inference test is part
of this implementation phase.

| State | Counts as held task / active thread? | Release condition |
| --- | --- | --- |
| Stored, queued, blocked before execution | No | Not executing; can cancel locally |
| DISPATCHING, RUNNING, WAITING_APPROVAL | Yes | Correlated terminal result; approval is never automatic |
| STOP_REQUESTED | Yes | Stop acceptance alone does not free capacity |
| UNKNOWN | Yes | Reconcile exact owned turn; never infer stop from age or restart |
| COMPLETED, FAILED, CANCELLED | No | Terminal proof or a validated rejected start; original records remain |
| Saved idle or archived conversation | No | No inference occupies a slot merely because the thread is saved |

Admission is checked twice, under the existing ledger transaction, before any
turn/start. Per-thread active_task ownership and the existing exclusive executor
lease remain enforced. Saving more inputs or creating metadata-only idle threads
does not start additional inference; queued work waits for the single slot.
Closing a busy thread is refused, without interrupting an unrelated active task.
Closing an idle resolved thread archives it and preserves all IDs/results/events.

Validated correlated RPC rejection is different from transport uncertainty.
Schema2 can continue with a *different explicitly submitted request* after a
rejected start; the failed original is not retried. Refused stop/steer keeps the
active task held until its terminal result. Missing/malformed/uncorrelated replies,
unknown ownership, unverified history and failed cleanup still fail closed.
Known catalogue/resume rejection before turn/start is persisted as
`BLOCKED` (`CATALOG_REJECTED`/`RESUME_REJECTED`, execution_started=false).
Clearing the refusal or restarting cannot silently execute that input; a caller
may explicitly submit a different input while the original remains recorded.

The new `capacity` view in capabilities/threads_list distinguishes active counts
from stored/archived/terminal/pending counts. The host gateway additionally
reports executor release and reconciliation state: terminal logical work does
not prove its process/lease was safely recovered. Legacy schema1 API results are
unchanged.

Schema2 `threads_list` returns at most20 combined metadata records, active work
first, with optional `limit`/`cursor` and total counts. Follow next_cursor to read
all retained history; no records are pruned to fit an MCP frame. A changed
snapshot rejects the old cursor, so restart the listing. Legacy no-argument
schema1 listing is unchanged. Discovery refresh is needed for clients that want
the new optional paging arguments; tool names and the27-operation set stay fixed.

## History is not an execution quota

Normal mode does not turn the old 512 operation /4096 event audit counts into
another lifetime job limit. It retains every record and existing cursor sequence.
The existing physical storage bounds remain: 2MiB serialized ledger,8MiB SQLite
file and outer runtime disk guard. Before the ledger is full, normal new writes
stop with `HISTORY_STORAGE_CAPACITY`, preserving64KiB for stop/completion metadata.
An outstanding task can still be stopped once and its result recorded. This is
explicit storage backpressure, not THREAD_CAPACITY/TASK_CAPACITY or model failure.
No automatic deletion, counter reset, history rotation or unbounded database is
introduced. Long-term history storage/export is a separate follow-up, not a
reason to retain tiny lifetime execution quotas.

Deferred storage issue: retaining all history will eventually reach a real byte
budget (including the existing MCP audit-file budget). New work then stops with
an explicit storage error; records remain readable and are never reset. No
automatic history export/rotation is implemented here. Revisit immutable history
export before history approaches the2MiB minus64KiB admission threshold. This
does not justify adding another lifetime job-count allowance.

## Explicit migration and deployment scope

Use the existing cold `pipeline_policy_migration.py` plan/apply/recover path.
It now permits exactly schema1→schema2 promotion with the same workspace, model
list, concurrency and timeout. Normal max_threads/max_tasks must equal that
unchanged concurrency for this promotion. Unknown versions or attempts to widen
models/workspaces/concurrency/timeout are rejected. Old tables, operation IDs,
failed/consumed attempts, answers, native replacement lineage and event cursors
are preserved. The migration appends its own recorded transition, not replacement
history. PREPARED/ambiguous migrations still block startup until explicit recovery.

For the observed live policy the meaningful difference is:

```diff
- "schema": 1,
- "max_threads": 2,
- "max_tasks": 6,
+ "schema": 2,
+ "max_threads": 1,
+ "max_tasks": 1,
```

Concurrency1 and120-second deadline stay fixed; the smaller numbers now mean
simultaneous work, not deletion/reset of the historical2 threads/5 tasks.

Live activation requires a separately approved idle check, existing daemon and
tunnel stop once, exact source/policy migration, service snapshot and existing
login pin update, then start once. Retain the experiment-read fingerprint and
all existing27 tools/auth/mailbox settings. No live task is interrupted to migrate;
pending/UNKNOWN work blocks the operation. Read-only counts/health after startup
can confirm the mode without starting a model turn.

Rollback restores only an exactly recorded earlier policy through the same
journal. It preserves any additional normal-use records, so old trial quotas may
correctly block new work after rollback. Keep the schema2-aware decoder while
history contains such transitions; do not restore an old decoder that cannot
read the journal or delete history to make it start.
