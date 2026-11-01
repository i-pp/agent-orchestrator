# Explicit cold policy-version migration

This implements local preparation for an approved policy change, not permission to deploy one. It adds no public MCP operation, service, model request, credential access or host-security configuration. Formal supported execution is valid; no human-only or HOST_CONTEXT requirement is introduced.

The pipeline previously rejected every changed policy with `PIPELINE_POLICY_CHANGED`. The existing policy hash, task/thread counters, completed answers and native replacement lineage must remain auditable when a finite quota is extended. Never delete/reset/copy the ledger to obtain fresh attempts.

## Scope and invariants

- Policy schema1, workspace keys/paths, concurrency, timeout and common-policy hash remain fixed.
- Expansion allows only monotonic additions to the model allowlist and monotonic increases in max_threads/max_tasks, within the existing validator bounds and an explicitly reviewed plan hash. Existing task/thread model and workspace bindings never change.
- Reverse migration can target only exact policy bytes recorded in committed history with the same common-policy identity. Threads using a removed model must be archived; all tasks must already be terminal. Counts can remain above the restored admission cap: old records are retained and new admission stays blocked.
- The existing service-operation lease (poc-services.lock), then daemon.lock, then runner.lock are acquired without replacing or unlinking any lock. A paused daemon still holds daemon.lock. Actual application requires the same owned services stopped, cleanup RELEASED, terminal tasks, known operations and confirmed thread histories.
- Known ledger/journal schema and exact old/new file/semantic hashes are required. A plan hash pins content; it is not an authorization token.

## Durability and partial application

The same SQLite file adds a small version1 transition table. It commits a PREPARED intent before replacing the private policy file. The replacement is mode0600, uses atomic rename and fsyncs the file and directory. Updating the ledger and marking COMMITTED then occur in one SQLite transaction with synchronous FULL. Existing IDs, operations, consumed attempts, events, answers and native lineage are preserved. A version-history entry and migration event are appended.

A PREPARED or unknown journal blocks Pipeline startup before ledger creation/recovery writes, Services.preflight even when pipeline-OFF is requested, and shared runner admission. Legacy DBs without migration history retain their normal path. No startup performs an automatic migration or recovery.

Inspection is read-only and reports hashes/counts/transition states, without prompt, answer or raw policy contents. Explicit abort is permitted only with the untouched old ledger and a file matching the exact old or new bytes; it restores only the old policy file, not an old DB. Explicit finalize requires the exact fully-new policy/ledger pair and matching prepared intent. Drift, unknown versions and multiple pending intents are rejected. A successful transition can later be reversed with another approved recorded transition, preserving all subsequent consumption.

## CLI preparation — no current deployment

Use locally configured absolute paths for PROJECT_ROOT, RUNTIME, CANDIDATE_POLICY and REVIEWED_PLAN; candidate and plan files must be owner-only mode0600. The plan output file is created exclusively and never overwritten. A fixed migration ID cannot be reused, including after abort.

```bash
python3 -B "$PROJECT_ROOT/scripts/pipeline_policy_migration.py" inspect --runtime "$RUNTIME"
python3 -B "$PROJECT_ROOT/scripts/pipeline_policy_migration.py" plan \
  --runtime "$RUNTIME" --candidate "$CANDIDATE_POLICY" \
  --migration-id "$MIGRATION_ID" --output "$REVIEWED_PLAN"
```

After explicit deployment approval and stopped/idle verification only:

```bash
python3 -B "$PROJECT_ROOT/scripts/pipeline_policy_migration.py" apply \
  --runtime "$RUNTIME" --plan "$REVIEWED_PLAN" \
  --expect-plan-sha256 "$PLAN_SHA256" --apply
```

The CLI loads the verified common-policy source; a changed common-policy identity is out of scope. Errors print fixed codes without raw input/storage errors. It never executes a service command, starts a model or installs a login entry.

## Actual service operation needed for rollout

1. Verify owned services, idle/known state, current source/config hashes and unchanged authentication references. Prepare and review the exact candidate and plan. No unrelated jobs may be stopped.
2. Stop the existing dedicated daemon and tunnel once through their current supported lifecycle helper. Copy only the reviewed source changes; use fresh Python invocations for new code. Do not treat a pause as a stop.
3. Apply the pinned transition. Unknown/partial results require read-only inspection, not replay. No task is submitted by migration.
4. Compute the actual new executor snapshot with the existing `executor_snapshot` helper. `rebind_service_snapshot` validates the committed old/new file hashes, same runtime, STOPPED state and unchanged binary/common/executor hashes; it returns a new service-state object preserving all other fields. Persist it through existing `Services.write_state`. This function does not install it automatically.
5. With that state installed, use existing `poc_login.snapshot` and `render_unit` to stage the new invocation pin. Preserve the prior login entry without reading its protected contents, install only the reviewed new entry, and run user daemon-reload. Retain existing authentication references, flags, enabled state and approved linger setting.
6. Run preflight with every existing capability flag and the existing key reference, then start those same two services once. Verify health, public schema, snapshot/pin consistency and retained completed answer before new approved work.

The policy/ledger commit is distinct from service/login binding installation. Existing snapshot checks reject stale bindings; do not claim full rollout completion merely because migration reports COMMITTED. Unit installation, daemon-reload and stop/start remain approval-time actions. No new launcher or approval bypass is required.

On failure, stop admission and inspect. Do not restore an older DB, remove transition evidence or regenerate an uncertain task. Core abort/finalize and any service-binding repair must match known hashes. Source rollback must retain the new journal gate while a transition is unresolved; reverting the gate would not be recovery.

## Verification

Fixture tests exercise completed-task and native-lineage preservation, consumed limits, pending startup/preflight/runner rejection, stopped-lock enforcement, fsync/transaction ordering, SQLite failure injection, exact abort/finalize, conflicting intent, unknown versions, drift and recorded reverse migration. Service binding updates are tested as data transformations. No live DB, runtime policy, service configuration, quota or inference is changed by these tests.
