# Native Agent Team

Use this experimental, parent-driven path for bounded parallel engineering work. It does not replace the FIFO daemon or automatically dispatch models. Shared operating principles remain in the canonical [orchestration architecture](https://github.com/i-pp/ai-orchestration/blob/main/docs/ai/orchestration.md); this page documents the runtime profile.

The [pre-v4 Japanese source](native-agent-team_jp.md) preserves the earlier two-worker rollout and its historical boundaries. It is not a current translation.

## Choose roles before launching

Load the pinned common policy first, then ask the non-executing planner for a role mix:

```bash
python3 -B scripts/policy_context.py --format inspect
python3 -B scripts/plan_native_team.py \
  --purpose implementation --complexity advanced \
  --parallel-parts 3 --security-sensitive
```

This prints worker assignments, counts, model/effort settings, profile hashes, dispatch cost, and budget ceilings. The total also reserves one parent-only integration dispatch for implementation or documentation, because Coordinator admission consumes the same run budget even though it does not spawn a child. It does not create worktrees, construct a runnable DAG, call a model, verify model availability, or spend a model allowance. The parent reviews the recommendation and supplies requirements, clean worktrees, allowed paths, and dependencies in the [team specification](native-team-poc.md).

| Role | Selection |
| --- | --- |
| `ImplementerQuick` | Mechanical, narrow, low-risk work with explicit acceptance criteria. |
| `Implementer` | A bounded feature or fix with a known design. |
| `ImplementerAdvanced` | Cross-module contracts, concurrency, migration, or ambiguous behavior. |
| `Orchestrator` | Advisory dependency and ownership planning for advanced, cross-project, or parallel work. |
| `IndependentReviewer` | Correctness and compatibility review of a fixed candidate. |
| `SecurityReviewer` | Trust, authentication, permissions, data, or publication boundaries. |
| `TestReviewer` | Test adequacy, failure paths, flakiness, and false-positive checks. |
| `PerformanceReviewer` | Algorithmic, concurrency, memory, or resource-cost risks. |
| `DocumentationReviewer` | Product documentation accuracy, commands, links, and translations. |
| `Verifier` | Independent acceptance verification of the integrated result. |
| `Researcher` | Bounded read-only investigation. |
| `Coordinator` | Parent-only integration, not a spawned custom agent. |

The planner chooses correctness and test review for implementation, documentation review for documentation work, and additional security/performance review when requested. An advanced, cross-project, or multi-part assignment gets advisory orchestration. Research and orchestration are single advisory assignments. Documentation review is not the article Editor or Technical Reviewer workflow.

Multiple workers can share an implementation level, but never a writable checkout. A worker, reviewer, or advisory orchestrator is a leaf. It proposes subdivisions to the parent instead of launching a second team. The parent remains responsible for dispatch, authority, integration, and delivery.

## Explicit models and bounded parallelism

`.codex/config.toml` sets project defaults for new trusted sessions. Each `.codex/agents/*.toml` also sets `model = "gpt-6.1-sol"` and `model_reasoning_effort = "max"`. The [v4 parent adapter](native-parent-adapter.md) reads and pins these profiles, then passes `model` and `reasoning_effort` through the current documented collaboration spawn contract. It rejects unsupported or missing settings instead of dropping them.

Model choice, fallback permission, and uncertain-generation handling come from the verified pinned [canonical model-selection policy](https://github.com/i-pp/ai-orchestration/blob/main/docs/ai/orchestration.md#explicit-model-selection), not a second rule set here. The v4 implementation accepts only the model/effort values in `src/native_agent_profiles.py`, freezes the selected role file for a run, and does not implement automatic fallback. The parent checks actual executor support and task constraints before configuration. Requested settings and observed execution metadata are different evidence; a passing offline test is not proof of an actual model invocation.

| Limit | Ceiling |
| --- | --- |
| Cloud workers per run | 6 |
| Total dispatches, including retries | 24 |
| Retries after a known failure | 2, with a new cause and confirmed prior release |
| Per-task time | 300 seconds |
| Whole-run time | 900 seconds |
| Local backend | Disabled; its reserved numeric ceiling remains 1 |

Use lower budgets when the task, executor slots, available worktrees, or measured resources require them. An unknown or timed-out worker keeps its lease until its actual terminal state is confirmed. Interrupt acceptance is not terminal confirmation. These limits are not host-wide quotas, a billing cap, or an OS watchdog.

### Selecting new-run concurrency

[`config/native-team.json`](../config/native-team.json) selects the native profile's new-run default. It currently uses `"cloud_concurrency": "max"`, meaning the full six-child source ceiling, not an arbitrary platform maximum. Choose an integer from 1 to 6 to lower the default. The planner reads this file; Python callers use `registry(project)` to obtain the same resolved budgets for the immutable team specification. A run's explicit budget remains frozen after admission. Editing this default does not rebind an existing run or alter a live service.

The planner also accepts an explicit selection and a separately reported executor limit:

```bash
python3 -B scripts/plan_native_team.py --purpose implementation \
  --parallel-parts 8 --concurrency max --executor-limit 6
```

`--concurrency` deliberately overrides the new-run default. `--executor-limit` counts available **child** slots, excluding the coordinating parent. The resolved budget is the minimum of that choice, any reported executor limit, and this profile's six-child ceiling. The output records the request, effective budget, and whether it came from the project or an override. It labels the executor limit as caller-reported and unverified, not discovered capacity. The parent must check actual API slots, independent worktrees, and physical resource constraints before dispatch. Invalid, oversized, duplicate-key, or symlinked settings fail instead of silently selecting the maximum.

Native collaboration slots and model aliases are separate from Codex app-server sessions, its provider-advertised models/efforts, and a LAF pool's limit. Do not apply one transport's capacity or model names to another. The native role compatibility values above are not an account-entitlement catalogue; the planner does not claim model availability or observed execution settings.

## Runtime boundaries

- [NativeTeam](native-team-poc.md) validates DAG admission, clean worktrees, fixed policy/revisions, leases, artifact allowlists, and completion receipts.
- [NativeDispatcher](native-dispatcher-design.md) persists parent actions before handoff and reconciles observations without reissuing uncertain work.
- [NativeParentAdapter](native-parent-adapter.md) prepares documented tool descriptors. The parent calls those tools directly and records actual returns.
- Integration and independent verification happen after worker outputs are collected. The artifact helper adopts new allowlisted files; it does not automatically resolve arbitrary edits or merge conflicts.
- Role instructions and worktrees are operational separation, not independent OS sandboxes. Do not add nested isolation inside Codex.

Existing daemon/Tunnel services, dedicated Codex homes, authentication, global configuration, repository trust, GPU/local providers, MCP public exposure, and existing task databases are not modified by this profile. Native tasks are not automatically registered in the daemon DB; existing MCP status tools therefore do not prove native-team monitoring or submission.

No live service restart is needed for source review. New project configuration applies through a new applicable trusted session, not by forcibly rebinding an existing daemon or task. Do not claim live E2E execution, model compliance, automatic wake after the parent turn, cost enforcement, or deployment from configuration and offline fixtures alone. Track remaining work in [native follow-ups](issues/native-team-followups.md).
