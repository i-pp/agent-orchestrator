# Persistent Scheduler Design

This reference describes Phase 2-D2. The [Japanese historical source](scheduler-design_jp.md) and [phase report](phase2-d2-report.md) retain the original evidence and limitations.

## FIFO and one-shot execution

The scheduler claims SQLite `QUEUED` tasks and runs at most two workers. FIFO uses insertion `rowid`, not the wall-clock `created` timestamp. It does not overtake a head task blocked by a workspace lease. Legacy shared-workspace work is serial; independent workspaces can use two slots. Concurrent resume of a workspace/thread is forbidden. Reconstructing order through external DB edits, automatic deletion, or VACUUM is outside this phase.

The daemon exclusively owns `daemon.lock`. Ordinary `run`/`run-pair` need a shared lease and are rejected while the daemon runs. Submission and inspection are separate from execution; queued work submitted without the socket is eligible too. An existing ordinary runner blocks daemon ownership and is not killed.

1. Check recovery/offline reconciliation, storage, pause, existing owners, and workspace boundaries.
2. In `BEGIN IMMEDIATE`, recheck `QUEUED` and persist the timeout and unique dispatch reservation.
3. Start a worker in a private resource scope, save PID/start identity, and mark `STARTED`.
4. The worker waits a bounded time for handoff and validates daemon boot/PID/start, nonce, and ancestry from the recorded process.
5. Consume the nonce once with a conditional update and mark `AUTHORIZED`.
6. Existing task/workspace/slot leases, the `QUEUED` compare-and-swap claim, and unique `executions.task_id` still enforce one execution.
7. The runner executes, independently verifies, and cleans up. Worker stdout or an exit code alone never establishes task success.

Persist the reservation before spawning. Do not fill a fork/DB gap by rerunning. A lost reservation becomes `UNKNOWN`; an unreserved queued task remains queued. The nonce correlates a one-time handoff; it is not a replacement for Unix peer authentication. Models cannot see the control socket or DB.

## Durable state and compatibility

Core schema `user_version=3` and existing IDs remain intact. The first daemon start takes exclusivity and a SQLite backup before transactionally adding tables:

| Table | Data |
| --- | --- |
| `scheduler_state` | Extension `schema_version=1`, pause, update time |
| `scheduler_options` | Task ID and fixed timeout |
| `scheduler_dispatches` | Unique task ID, nonce, daemon/worker owner, phase, processes, scope description, cleanup, timestamps |

Pause survives restart. Shutdown does not change it; a later explicit start respects the stored value. Socket submission fixes timeout in its submission transaction; legacy submission fixes it with the dispatch reservation. Reusing a request key returns the same task only for identical criteria and timeout.

Backups use `db-backups/before-scheduler-v1-UUID.sqlite`. A failed extension transaction rolls back without clearing existing rows. Unknown schemas are not guessed. DB rollback checks daemon ownership and scheduler tables and refuses to discard durable queue records. Stop daemon/workers and verify recovery, backup, and matching source before reverting code. Never run old D1 rollback code against a D2 DB; no automatic table-drop procedure exists.

## Recovery and `UNKNOWN`

Startup and worker exit use existing abandoned-owner recovery. Reconcile reservations, worker boot/PID/start and cgroup, Codex processes/scopes, manifests, and logs. While a worker is still stopping, `recovery_pending` blocks new dispatch. A recorded private cgroup path also covers the pre-launch checkpoint gap after daemon death. Unproven cleanup keeps the reservation unknown and stops dispatch for human investigation.

`UNKNOWN`, `WAITING_FOR_HUMAN`, and `FAILED` are not automatically resubmitted. Conversation resume is an explicit new task with an `allow_resume` decision, not restoration of an interrupted command. The decision must match the current execution, and the runner rechecks official history, resources, side effects, and approval freshness immediately before resume.

| Condition | Dispatch effect |
| --- | --- |
| Worker/Codex cleanup unknown or handoff lost | Stop all dispatch |
| Unknown legacy shared workspace | Stop all dispatch |
| Unknown workspace cannot be validated/reconciled offline | Stop all dispatch |
| Independent unknown workspace, reconciled and resources released | Protect that workspace; other independent work may proceed |
| Workspace/thread in use | Wait at the FIFO head |
| Explicit resume approval for current execution | New task may run after runner rechecks |
| Storage/free-space budget exceeded | Stop dispatch; keep control socket available |
| SQLite contention | Bounded delay/error, not duplicate claim or inferred success |

A task DB `COMPLETED` checkpoint can still have `worker_pending=true` and `completion_verified=false` in socket status. Later resource-limit or supervision failure returns that checkpoint to `UNKNOWN`, preserving verification evidence. Keep scheduler phase/timeout, core execution, acceptance, and human decision separate. `FINISHED`/`ORPHANED` describe the worker journal, not task success.

## Stop and preservation

Pause stops new claims only; cancel targets one task; shutdown interrupts only daemon-owned tasks. Preserve unclaimed queued work, unknown work, acceptance evidence, logs, snapshots, and backups. Forced cleanup records its reason and does not turn uncertainty into success. Existing slot leases and core unique constraints remain independent safeguards.

The D2 evidence measured foreground-daemon restart, not systemd automatic restart; later service work is separate. Multi-day soak, high load, hard total-disk quotas, OS restart/power loss, other operating systems, and other CLI versions were not verified by D2.
