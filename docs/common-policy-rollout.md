# Adopt a new common policy in an existing pipeline

The pipeline binds its ledger and native conversations to the verified common-policy hash. Changing only the source checkout or the pin causes `PIPELINE_POLICY_CHANGED`; it does not deploy a new policy safely.

The operator-only migration CLI supports a schema 2 transition for adopting a new common-policy hash. It requires identical pipeline-policy bytes, terminal tasks, known operations and thread history, released executor cleanup, and the existing service/daemon/runner leases. Application requires the owned services stopped. Workspace paths, models, quotas, authentication and executor binary cannot change in this transition.

## Prepare and apply

Configure a candidate checkout with the accepted five-document pin. Verify it with `scripts/policy_context.py --format inspect`. Keep the old verified common hash and original source bytes for review and recovery; do not rehash arbitrary local edits into a new pin.

Use absolute operator-selected paths. The runtime policy and plan are owner-only files; the plan is created exclusively. Planning reads the live ledger without modifying it:

```bash
python3 -B "$PROJECT_ROOT/scripts/pipeline_policy_migration.py" plan \
  --runtime "$RUNTIME" --candidate "$RUNTIME/codex-pipeline-policy.json" \
  --previous-common-sha256 "$OLD_COMMON_SHA256" \
  --migration-id "$MIGRATION_ID" --output "$REVIEWED_PLAN"
```

After deployment authorization, idle verification and stopping the same owned services, install only the reviewed source/pin changes and apply the reviewed plan:

```bash
python3 -B "$PROJECT_ROOT/scripts/pipeline_policy_migration.py" apply \
  --runtime "$RUNTIME" --plan "$REVIEWED_PLAN" \
  --expect-plan-sha256 "$PLAN_SHA256" --apply
```

The CLI checks that its loaded common hash equals the plan's new hash. Schema 1 quota/model transitions keep their original behavior. A schema 2 common transition cannot also change pipeline policy fields. It appends a durable journal, migration history and an event, and updates the current ledger binding. Existing task, thread, operation, answer and owner records remain intact; no DB is replaced or reset.

Historical threads without a per-thread policy field retain the old identity recorded by the first common-policy adoption. New threads and inputs record the current common hash. Reusing an older native context under a different common policy fails with `THREAD_POLICY_VERSION_CHANGED` before RPC. Terminal answers and history remain readable. Start a newly reviewed thread for new-policy work instead of relabeling an old conversation.

## Services and failure handling

`rebind_service_snapshot` prepares a new STOPPED service-state object only after the migration is committed. For a common adoption, it validates the exact old/new common hashes and unchanged binary, executor and pipeline-policy hashes. Preserve other service settings and use the existing lifecycle/login helpers for installation and restart. A returned object alone is not deployment evidence.

PREPARED or unknown journal state blocks startup. Inspect before any retry. Existing abort/finalize rules require exact known file/ledger fingerprints. Never restore an older task DB, discard history, disable the journal gate or regenerate a task with an uncertain outcome. Source rollback must retain support for schema 2 journals.

Validate first with a dedicated runtime and synthetic data. Separately verify a bounded real Codex task using the normal host service path. Capture the actual `thread/start` or `thread/resume` developer instructions for that test, compare version/hash and both editorial section counts, and record the task outcome. Injection evidence is distinct from model compliance or article acceptance. Recheck idle state, historical record fingerprints, authentication references and notification services before and after the target rollout.
