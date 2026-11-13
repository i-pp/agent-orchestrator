# Native Team Admission and Persistent State

`src/native_team.py` connects the existing DAG plan and artifact-adoption modules. It is a local coordinator API, not a model launcher, resident scheduler, or replacement for the FIFO daemon. `scripts/native_team.py` accepts JSON and updates state; it never executes model APIs, services, or command strings.

The [pre-v4 Japanese record](native-team-poc_jp.md) preserves earlier numerical limits and rollout history. Current limits are in the [native team guide](native-agent-team.md).

## Specification and worktrees

Load the full pinned policy through the repository instructions. Freeze its version/hash in the specification. Inspect existing changes and prepare a dedicated clean worktree for each writer and verifier. Do not pull, reset, clone automatically, create credentials, or change trust as a policy-load workaround.

```bash
# Set these variables to authorized paths and an inspected input revision.
git worktree add -b team-task-a "$WORKTREE_A" "$INPUT_REVISION"
git worktree add -b team-task-b "$WORKTREE_B" "$INPUT_REVISION"
```

Each workspace must be a distinct, non-nested worktree in the project's Git common directory. Configure any ignored operator-local policy selector separately in each worktree. Keep runtime state, secrets, deployment settings, and logs out of tracked source.

A specification has exactly `schema=1`, `goal`, `requirements`, `project`, `policy`, `registry`, and `tasks`. `requirements` is a nonempty string list; `project` is an absolute repository path; `policy` contains `version` and `sha256`. Start the registry with `native_team.registry(project)` so the project's [new-run concurrency setting](native-agent-team.md#selecting-new-run-concurrency) is applied, then lower explicit budgets as needed before freezing the specification. Each task has `id`, `role`, `backend`, `deps`, `workspace`, `instruction`, and `allowed` paths. The registry supports the current [selectable roles](native-agent-team.md#choose-roles-before-launching); local dispatch remains disabled.

```bash
python3 -B scripts/native_team.py init --run "$RUN_DIR" < team-spec.json
python3 -B scripts/native_team.py show --run "$RUN_DIR"
```

Use a new dedicated private run directory, never a live daemon runtime. `init` rejects an existing run; do not resubmit uncertain existing work under a fresh ID.

## Admission and observations

1. The parent chooses a ready task. `admit` checks dependencies, deadlines, attempts, dispatch cost, clean worktree identity, policy, and held leases.
2. Only after durable admission may the parent call the standard native spawn tool once.
3. Bind the actual returned native ID. If the spawn acknowledgment is uncertain, record `UNKNOWN` and reconcile the existing action; do not resend it.
4. Observe actual terminal state, independently inspect requirements/scope/diff/hash/checks, then record `complete`.

```bash
python3 -B scripts/native_team.py admit --run "$RUN_DIR" < admission.json
# The parent performs the authorized standard spawn outside this CLI.
python3 -B scripts/native_team.py bind --run "$RUN_DIR" < native-binding.json
# The parent obtains a real terminal observation and verifies the result.
python3 -B scripts/native_team.py complete --run "$RUN_DIR" < completion.json
```

| Operation | Exact stdin object fields |
| --- | --- |
| `show`, `expire` | `{}`; empty stdin is also accepted |
| `admit` | `task_id`, `actor` |
| `bind` | `task_id`, `dispatch_id`, `agent_id` |
| `complete` | `task_id`, `dispatch_id`, `agent_id`, `success`, `reason`, `verification` |
| `unknown` | `task_id`, `dispatch_id`, `reason` |
| `release` | `task_id`, `dispatch_id`, `agent_id`, `reason` |
| `retry` | `task_id`, `cause_code` |
| `escalate` | `task_id`, `reason` |

`verification` contains nonempty equal-length lists `commands` and `exit_codes`. Codes are integers; a successful receipt requires every code to be zero. Command strings are records of observed checks, not commands for the CLI to execute. `cause_code` is a stable uppercase code. Unknown fields, duplicate JSON keys, malformed JSON, and inputs above 64 KiB are rejected. Error replies do not reflect arbitrary input or external diagnostics. A failed mutation may already have been saved, so it returns `UNKNOWN`; inspect stored state and actual tool history rather than assuming nothing happened.

## Integration and independent verification

Only after predecessor success receipts may the parent admit integration. `native_team_artifacts.integrate` checks each source root, allowed paths, and expected hash before adopting new files. It rejects duplicate outputs, existing destinations, symlinks, and changed hashes. It is not a general code-patch merger or conflict resolver.

Record the integrated revision and hashes, and create a separate verifier worktree. The Verifier derives checks from the original requirements and fixed inputs rather than accepting implementer test success as sufficient. Product-source changes outside the verifier's allowed outputs are rejected. Receipts record commands, exits, hashes, IDs, and judgment. Collect artifacts and evidence before cleanup; never automatically remove dirty or `UNKNOWN` worktrees.

## Deadlines, uncertainty, and recovery

```bash
python3 -B scripts/native_team.py expire --run "$RUN_DIR"
# The parent requests standard interruption and observes actual terminal state.
python3 -B scripts/native_team.py release --run "$RUN_DIR" < confirmed-native-release.json
```

Expired and `UNKNOWN` workers retain their leases until actual terminal confirmation. Reload does not release or respawn them. `release` checks the real ID and terminal observation; it never converts timeout/uncertainty into quality success. Retry is available only after a known `FAILED` result and confirmed release, requires a new cause, and obeys the frozen attempt/dispatch/time limits. The CLI never retries automatically or interprets the meaning of a cause code.

Escalation blocks the affected dependency when authorization is missing or a material mismatch cannot be safely resolved. It does not obtain approval or send messages. Native state remains separate from daemon SQLite, MCP Chat submission/cancel, cloud/local mixed execution, GPU/llama.cpp, billing, and service deployment. Configuration and fixture passes do not establish a successful live parallel-agent PoC.
