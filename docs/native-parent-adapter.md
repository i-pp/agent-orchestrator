# Parent-Driven Native Tool Adapter

The adapter prepares fixed tool descriptors; the parent executes standard tools and explicitly records their actual returns. Python and the CLI do not call collaboration tools, start a model, or arrange automatic wake after the parent turn. The current profile is `collaboration-parent-v4` with explicit model settings. The [pre-v4 Japanese record](native-parent-adapter_jp.md) preserves older contracts and experiments, not current live instructions.

## Current execution boundary

A previous live smoke could not correlate a new child from `read_thread` because the current parent turn did not expose `subAgentActivity`. The child returned its requested JSON, but the caller remained blocked and did not adopt its result. A historical thread read or fixture pass is not proof that live correlation is available.

Use actual parent standard-tool returns instead:

1. `step`, or `readonly_prepare` for the fixed one-shot read-only scope, persists a canonical descriptor. The parent calls its standard `collaboration.spawn_agent` once with the exact supported arguments before the deadline.
2. Pass the actual return `{"task_name":"/root/..."}` and that descriptor's `request_ref` to `parent_result`. Registration checks the fixed request/scope and binds the actual ID. Never substitute an expected value or fixture response.
3. On a `POLL` descriptor, the parent calls `collaboration.list_agents` and records its actual `{"agents":[{"agent_name":"/root/...","agent_status":...}]}` result through the same entry point. Missing, duplicate, or unknown statuses do not establish terminal state; they become `UNKNOWN`. Unrelated agent bodies are not stored.
4. `wait_agent`, where available, supplies a notification only, not an agent result or terminal receipt. Reconcile with actual inventory after waiting. If the current executor lacks that tool, do not invent a call; continue with supported inventory within the budget.
5. After actual terminal observation, the parent handles `VERIFY`. General implementation uses requirement-derived checks and `verified`; the fixed read-only scope uses `readonly_verified` to compare saved context/policy/role JSON and Git scope.

```bash
python3 -B scripts/native_parent.py parent_result --run "$RUN_DIR" < native-result.json
```

The input has exactly `request_ref` and `result`. `recordParentResult` is a JavaScript helper for passing that actual value to the fixed CLI. It is not automatic wiring from standard tools into Code Mode. App `wait_threads` is a different UUID-based contract and is not automatically substituted for canonical task names.

Receipts correlate request hash, actual-return hash, canonical agent name, and first local registration time. This path does not know standard-tool start/end or provider completion time, so they remain null/`UNKNOWN`. Never infer timing from model text. A late completion can release a confirmed terminal worker but cannot become quality success after the end-to-end deadline.

Identical repeated registration is idempotent; different content conflicts. Lost acknowledgments are reconciled against saved receipts, not by blindly repeating the external tool. A spawn without a known return remains an unresolved `ISSUED` action; another `step` does not respawn it. Hashes detect modification, not cryptographic provider authenticity or accurate human/model forwarding.

## v4 role and model contract

Current official [Subagents documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents) documents project custom-agent files, model/reasoning settings, concurrency defaults, and inherited permissions. This adapter's exact collaboration argument names come from its execution session's documented tool schema, not an assumption that every CLI/app-server/MCP exposes the same API.

| Operation | Descriptor contract | Boundary |
| --- | --- | --- |
| Spawn | `collaboration.spawn_agent(task_name, message, fork_turns, model, reasoning_effort)` | Exact explicit model/effort; no invented `agent_type` or `config_file`. |
| Poll | `collaboration.list_agents(path_prefix)` | Batches pending polls and matches exact canonical names. |
| Interrupt | `collaboration.interrupt_agent(target)` | Only the bound target; acceptance is not terminal release. |
| Optional wait | `collaboration.wait_agent(timeout_ms)` | Notification hint only, where that tool is actually available. |

Role profiles accept exactly `name`, `description`, `developer_instructions`, `model`, and `model_reasoning_effort`. The v4 implementation's supported values are defined in `src/native_agent_profiles.py`: `gpt-6.1-sol` or `gpt-6-luna`, with `max` effort. These are executable compatibility limits, not a second source of model-selection authority. Follow the verified pinned [canonical model-selection policy](https://github.com/i-pp/ai-orchestration/blob/main/docs/ai/orchestration.md#explicit-model-selection) and task constraints when choosing among them. Missing/extra/unsupported settings are rejected before admission, role files are frozen, and no automatic fallback or uncertain-generation retry is implemented.

The adapter reads and hashes every used role file before configuring the run. A later role/model change fails the pin check before admission. `fork_turns=none` is required for explicit model overrides and an immutable self-contained task. It does not erase system/developer instructions, permissions, tools, or skills. The exact verified common-policy text, role instructions, and bounded task are loaded through the fixed context artifact rather than a manually transcribed long spawn prompt.

The profile version changes because model arguments change the handoff contract. Saved v1/v2/v3 adapter runs are not silently migrated, regenerated, or marked successful. Reconcile existing actions/artifacts and actual terminal states using their original contract before deliberately starting separately authorized new work.

Model settings in a descriptor are a request, not proof of the executing model. Record observed model metadata separately when available. All roles inherit execution permissions; a reviewer instruction is not a separate read-only OS sandbox. Advisory Orchestrator is a leaf planning assignment, while parent-only `Coordinator` performs integration.

## Immutable task transport

The full verified task is stored in the private run's immutable handoff files. Spawn receives only a short bootstrap, fixed local `context` command, and SHA-256. The child must load it successfully, match the requested hash, and obey the deadline before doing work.

`context` accepts exactly `action_id` and `sha256`. It validates active dispatch correlation, policy, hash, and expiry, and returns `result.message` only on success. References stay inside the fixed run; arbitrary paths/URLs and new MCP permissions are not exposed. Task/file/bootstrap/argument mismatches are rejected before a supplied mechanical tool port is called.

`NativeToolDriver` can use an explicitly supplied callable tool port. Without one, `tick`/`execute` stop before admission. This is not a Python API for the session's standard collaboration tools. Fixture ports never count as live connections. A durable exclusive start receipt prevents two executors from replaying a handoff after a lost response.

## Parent continuation with the CLI

Set these shell variables to inspected, authorized absolute paths. `RUN_DIR` must be a new private run, not an existing daemon runtime. Input JSON files are bounded local specifications/observations, not an arbitrary-command MCP API.

```bash
PROJECT_ROOT="/path/to/agent-orchestrator"
RUN_DIR="/path/to/private/run-new"
cd "$PROJECT_ROOT"
python3 -B scripts/native_parent.py init --run "$RUN_DIR" < init.json
python3 -B scripts/native_parent.py step --run "$RUN_DIR" < /dev/null
```

`init.json` has exactly `spec`, `submission_key`, `parent_task`, and `step_limit`. Use the [NativeTeam specification](native-team-poc.md), the actual canonical parent path, and a step limit from 1 to 1000. Reusing the same key/spec/config is idempotent; changing parent, budget, or role hashes conflicts.

1. Check every returned `tool`/`arguments` against the actual available schema and scope. `action_ids` and other fields are correlation metadata, not extra tool arguments. Check `not_after` immediately before external spawn. There is no atomic boundary between returning a descriptor and the parent calling the tool.
2. Execute the standard tool once, then use `parent_result` for supported actual spawn/inventory returns. Existing `spawned`/`inventory` operations also accept explicit observations, but do not prove a fabricated value came from a provider.
3. Register actual interrupt acceptance with `interrupted` (`action_id`, `accepted`). Do not release the lease from acceptance alone; unsupported raw interrupt/error schemas remain a reconciliation boundary.
4. Handle `parent_jobs`: `COORDINATE` integrates the supplied input receipts/hashes; `VERIFY` checks requirements, instruction, artifacts, and scope. `verified` accepts `action_id`, `accepted`, `reason`, and `verification={commands:[...],exit_codes:[...]}`. Command strings are evidence only.
5. Repeat `step` within the deadline and finite step budget. Handle `COMPLETE` as completion; reconcile and report `BLOCKED`, `UNKNOWN`, or pending actions. A wait notification never replaces a poll.

```bash
python3 -B scripts/native_parent.py spawned --run "$RUN_DIR" < spawn-observation.json
python3 -B scripts/native_parent.py inventory --run "$RUN_DIR" < inventory-observation.json
python3 -B scripts/native_parent.py verified --run "$RUN_DIR" < verification-observation.json
```

Input is bounded to 64 KiB; duplicate keys and unexpected fields are rejected. Error output does not echo raw diagnostics or inputs. Mutations with uncertain acknowledgments return `UNKNOWN`. `pending_reconciliation` lists unresolved actions with no reissue. Identical acknowledgment is idempotent within retained history; when a batch partly succeeded, reconcile each remaining action rather than replaying the batch.

An optional wait receipt stores only response hash, held dispatch correlation, and first registration time. It returns `notification_only=true`, `terminal_observation=false`, `next_operation=step`. Conflicting notifications are rejected. Pending unresolved actions take precedence over suggesting another wait. Logical lease release is not deletion of an agent thread or proof of extra executor slots.

## Fixed read-only and historical capture helpers

The fixed read-only scope requires exactly one `Researcher`/cloud task, no dependencies or allowed outputs, one cloud slot/dispatch, zero retries, task/run deadlines at most 120 seconds, and a finite step budget. Its expected JSON identifies the context hash, policy version/hash, and `Researcher` role; it is not a general product acceptance test.

`makeCliPort(execCommand, projectRoot, runDir)` fixes absolute project/run paths, POSIX-quotes JSON on stdin, permits only known operations, and treats nonzero exit, an ongoing session, or missing acknowledgment as reconciliation required. `prepareReadonly` returns a single fresh v4 descriptor, never a model call. The parent still performs standard spawn once.

Live `finishReadonly` remains disabled before touching app/CLI with `LIVE_READ_THREAD_UNAVAILABLE_USE_PARENT_STANDARD_TOOLS`. `finishHistoricalReadonly`, `captureExistingThread`, `bind_from_thread`, and `thread_observation` are retained for historical or fixture correlation only. They mechanically read structured provider timing and answer bytes, not timestamps claimed in text. Do not use the old historical read-thread procedure as a current live monitoring promise.

Offline integration tests exercise real JavaScript, shell quoting, CLI, durable state/receipts, wrong answers, scope changes, lost bind/poll acknowledgments, and idempotent reentry. Child responses and app reads in those tests are fixtures. v4 tests additionally cover all role profiles, explicit model parameters, unsupported/missing settings, pin changes, six-worker admission, and held unknown leases. These checks do not establish a new live smoke, provider compliance, billing class, OS timeout, daemon/MCP deployment, or cross-runtime compatibility.

## Data, authorization, and remaining limits

A delegated task contains the verified common policy, role text, bounded requirements, worktree path/revision, and allowed list. Avoid secrets, unrelated conversation history, and sensitive data outside the authorized assignment. Do not contact external agents, change providers/authentication/trust, restart services, or widen exposure to make this path appear complete.

The adapter cannot wake after the parent turn, enforce OS kill while the parent is stopped, or cryptographically authenticate local callbacks. Its use of NativeTeam private transactions needs a reviewed extension/schema contract before service integration. Availability of raw interrupt/error schemas and compatibility with a different runtime remain unverified. Preserve original failed/blocked results and collect artifacts before cleanup or new work.
