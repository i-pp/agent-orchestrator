# Common policy and project instructions

The common-policy source of truth is [`i-pp/ai-orchestration`](https://github.com/i-pp/ai-orchestration): `docs/ai/orchestration.md`, `docs/ai/policies.md`, `agents/README.md`, `docs/ai/editorial-policy.md`, and `policies/editorial.md`. `config/upstream-policy.json` pins the commit and hashes of all five documents. Read their immutable Git content from an existing local checkout. Editorial rules apply to technical articles and their editing/handoffs, not to the format of ordinary implementation results. Do not duplicate the canonical policy bodies here. Preserve the hash-pinned legacy [`policies.md`](policies.md) v1.0 as historical evidence; do not inject it as a second current policy.

Before policy-dependent work, run:

```bash
python3 -B scripts/policy_context.py --format inspect
```

Actually read the verified body, version, SHA-256, loaded revision, and checkout metadata. Reading a link or headings is not a successful policy load. On a load failure, hash/version mismatch, or policy conflict, stop the affected work and report the cause. Do not guess, weaken checks, or silently repair the source. Independent safe work may continue.

Higher-priority execution rules, environment permissions, and the user's explicit constraints take precedence. Common policy and project/role instructions do not grant permissions or broaden authorization.

## Project references and boundaries

- Policy adoption: `config/upstream-policy.json`, required by `config/policy.json`. Relative paths are repository-root relative; `~` expands to the user home. The ignored `config/policy-source.local.json` selects an existing checkout/commit; its loaded revision, hashes, and dirty state are recorded. The legacy pin is in `policies/manifest.json`.
- Policy transport and environment-specific evidence: [`docs/common-policy.md`](docs/common-policy.md). A reference update is not proof that every environment loaded it.
- Native team selection: [`docs/native-agent-team.md`](docs/native-agent-team.md), `.codex/config.toml`, and `.codex/agents/`. Use `scripts/plan_native_team.py` for a non-executing role recommendation. Select quick, standard, or advanced implementers, focused independent reviewers, and an advisory orchestrator as useful. Workers are leaves; the parent owns dispatch and integration.
- Model selection and fallback authority: follow the verified pinned [canonical model-selection policy](https://github.com/i-pp/ai-orchestration/blob/main/docs/ai/orchestration.md#explicit-model-selection). Inspect actual project/role settings in `.codex/config.toml` and `.codex/agents/`; the executable supported-value checks are in `src/native_agent_profiles.py`. These implementation settings do not replace the canonical policy or the user's stricter task constraints. See the [native guide](docs/native-agent-team.md#explicit-models-and-bounded-parallelism) for the v4 transport boundary.
- Native budgets: at most six cloud workers, 24 total dispatches, two retries for known failures, 300 seconds per task, and 900 seconds per run. A task or executor may impose lower limits. Unknown/timed-out workers retain their slots until actual terminal confirmation. These are per-run limits, not host-wide hard quotas.
- Existing runner time/resource settings remain in `scheduler-config.json` and `resource-limits.json`. Native team changes do not modify daemon, Tunnel, authentication, global model/MCP configuration, public exposure, or repository trust. Do not start an unverified local provider or infer that a shared 30B model is available or healthy.
- Keep eSOL business sources, secrets, and records separate from Project-KISS workspaces and context. Preserve unrelated existing changes. Push, merge, publish, and deployment require authorization for their exact scope; this role file supplies none.

Do not create nested isolation with `bwrap`, `unshare`, or equivalent inside an already isolated Codex environment. If an evaluation requires isolation that is unavailable, record `BLOCKED`, its reason, and what did not run; do not switch to an unisolated evaluation. Record uncertain outcomes as `UNKNOWN`, not a pass or model-quality failure. Continue independent ordinary edits/checks. See [`docs/nested-isolation-guard.md`](docs/nested-isolation-guard.md) for the code-enforced boundary. This restriction is separate from the legacy v1.0 policy body.
