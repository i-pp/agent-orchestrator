# Branch Reconciliation: 2026-11-13

The integration baseline is `26de0b0e4919187c7b772723130556dad2bc1b2b`. This inventory compares actual final Git content, not only ahead/behind counts. Public source reconciliation does not deploy a local runtime, change service ownership, or publish local state/history.

## Branch decisions and recovery references

| Source branch and recovery commit | Evidence at the baseline | Resolution |
| --- | --- | --- |
| `ci/nonpublishing-docs-20261104` at `4fce3bd55420ca7b1c43921b4a8a818a6fa944c8` | Both changed files are byte-identical to main; patch-equivalent CI is integrated. | Do not replay. |
| `codex/aceis-events-pr-20261105` at `0e97252cf188e93513ca18fc04ee14caed012c99` | The tip is an ancestor of main. | Already integrated. |
| `codex/editorial-policy-v2-runtime` at `0610a1d3700fe9194c295a6dd8cb416816a59943` | All nine changed files are byte-identical to main. | Already integrated; keep its policy/evidence lineage. |
| `codex/laf-authenticated-approval-mvp-20261111` at `d5e95143db216cfee6f4c1f153426b736ec92284` | All 29 changed files are byte-identical to main, including approval source, tests, docs, and workflows. | Already integrated through squash; no second approval implementation merge. |
| `docs/readme-reorganization-2026-11-04` at `fdab3fd9f7e0f7f00452d7f0a4ff4b02a5e232b0` | Four of seven changed files match main. README, run-task, and testing contain later intentional main updates. | Retain current entry points and validation provenance; translate maintained guides without restoring older claims. |
| `feature/chat-codex-pipeline` at `c62d97f056e0eb3e1906ef5b1c114d5a11ba66d4` | Main retains newer core protocol/host/pipeline/daemon/runner behavior, including approval, absence, and notification handling. The old migration file contains a literal truncated-tool-output fragment and fails Python parsing. | Keep main core; selectively recover complete offline tests and document the remaining optional/unimplemented pieces below. Never merge the corrupted source. |
| `fix/ubuntu26-npm-codex-isolation-20261104` at `fc19802abc529685b709dfc54f3a9531111b50b7` | A real seven-file candidate exists, but its own host-integration report says `OVERALL=FAIL` and it was not selected. It depends on npm/Node staging APIs absent from main. | Preserve this recovery commit and [issue #3](https://github.com/i-pp/agent-orchestrator/issues/3). Do not enable it, import its incompatible tests alone, weaken isolation, or call the incomplete repair working. |

The commit references above preserve the original public work for recovery. Source integration, remote branch removal, deployment, and removal of local dirty worktrees are separate actions. No force-push or automatic cleanup of uncertain/unmerged local work is part of this change.

## Recovered pipeline evidence

The complete fake `Peer` fixture is recoverable from the pipeline branch, contrary to the earlier claim that it was absent from Git history. Seven recovered test files exercise current source without starting a live app-server/model, service, Tunnel client, or namespace:

- `test_codex_app_protocol.py`: bounded protocol, framing, schema, timing, and process-lifecycle fixtures.
- `test_codex_pipeline.py`: ledger, read-only scope, task lifecycle, idempotency, unknown outcomes, events, and default-off MCP behavior.
- `test_codex_pipeline_gateway.py`: fake-owned host transports, leases, typed proxying, and cleanup confirmation.
- `test_codex_lifecycle.py`: first-context/history/recovery behavior with a fake peer.
- `test_pipeline_capacity.py`: held concurrent slots, retained history, storage backpressure, and migration guards.
- `test_pipeline_memory_budget.py`: explicit current scheduler resource selection and mocked controller checks.
- `test_pipeline_policy_migration.py`: cold transition planning, durable intent, recovery, and history preservation.

Reconciliation changes tests to the current contracts rather than regressing implementation:

- At the reconciliation baseline, the opt-in pipeline exposed 15 operations, including three approval operations. The existing 11 base schemas remained exact and unchanged; that combined list was 26 rather than the older 23. The later [model-discovery interface](codex-model-selection.md) adds read-only catalogue paging without changing those base schemas.
- The current selected pipeline scheduler profile is 6 GiB, 3072 kernel tasks, and a 256 MiB file ceiling. Base daemon limits and worker/runtime/log limits remain distinct. Tests preserve these configured values and rejection boundaries rather than restoring older 1 GiB/512/64 MiB expectations.
- Migration journal versions 1 and 2 are supported; the unknown-version test now uses version 3, preserving fail-closed behavior.
- Importing a fixture TestCase through a module alias avoids accidentally collecting its tests twice.

The recovered public suite omits these unsupported legacy assertions rather than silently inventing APIs or weakening product checks:

- `test_generated_schema_provenance_when_available`, which required untracked generated host schema artifacts. Public version/schema-contract checks remain in `test_codex_schema_compatibility.py`; no private dump is published or represented as verified.
- `test_real_snapshot_contract_reaches_service_and_login_pins`: `Services.pipeline_snapshot` and pipeline-aware login pinning are absent from the selected main service helper.
- `test_pending_blocks_preflight_before_service_access` and `test_pipeline_off_preflight_cannot_hide_prepared_transition`: the older service-helper migration-preflight integration is not selected. Core migration, pipeline admission, pending-intent, and lease tests remain; full service-startup integration is not claimed.
- `test_outer_supervisor_tunnel_and_aggregate_are_explicit` and `test_actual_launch_keeps_supervisor_and_tunnel_caps`: the old helper's `memory_budget`/`kernel_task_budget` integration is absent or has a different contract. Current pure scheduler/controller checks are retained. This is not proof that a live supervisor can host the expanded pipeline resource profile.

Run the recovered offline suite from the repository root:

```bash
PYTHONPATH=tests python3 -B -m unittest \
  test_codex_app_protocol test_codex_pipeline test_codex_pipeline_gateway \
  test_codex_lifecycle test_pipeline_capacity test_pipeline_memory_budget \
  test_pipeline_policy_migration -v
```

These tests provide fixture evidence, not live model compliance, service/login deployment, billing guarantees, or host isolation acceptance. Do not blindly run historical whole-suite discovery in an isolated execution environment: some older suites exercise systemd, installed Tunnel binaries, or namespace tools.

## Preserved but unselected optional work

The pipeline branch also retains read-only experiment projections/cataloguing, host metadata probes, a one-shot model harness, related manifests/config examples, operator docs, and service/login integration tests. They depend on source or operational contracts not selected in main. Their original bytes remain recoverable at the branch commit above. Do not expose experiment artifacts, publish private runtime paths/state, run a generation harness, or imply those capabilities are active because this branch is reconciled.

The npm isolation candidate remains an incomplete host repair. Current namespace guards and no-unisolated-fallback behavior are unchanged. Its host harness is not run as part of source reconciliation, and no AppArmor/sysctl/security or live service configuration is modified.
