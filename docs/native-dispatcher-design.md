# Native Parent-Action Dispatcher

`NativeDispatcher` performs one bounded scheduling step and journals the parent actions needed to continue. It does not launch a model or service, add a socket/database/provider, call native tools, or connect native tasks to the FIFO daemon. Its code and offline fixtures are distinct from live execution evidence. The [pre-v4 Japanese record](native-dispatcher-design_jp.md) preserves the initial rollout and historical counts.

## Responsibilities

| Component | Responsibility |
| --- | --- |
| Existing daemon/scheduler/runner | SQLite reservations, process identity, worker/app-server execution, and cgroup release. This path is not replaced. |
| NativeTeam | DAG admission, worktree/policy/revision validation, budgets, leases, native binding, completion receipts, and artifact hashes. |
| NativeDispatcher | Idempotent receipt of work, durable parent actions, observation reconciliation, and one-step dependency progress. |
| Parent | Standard spawn/poll/interrupt calls, integration, independent checks, and explicit recording of actual results. |

```python
from native_dispatcher import NativeDispatcher

# Use a verified specification and a new dedicated private run directory.
dispatcher = NativeDispatcher.receive(root, spec, submission_key="request-v1")
progress = dispatcher.advance()
actions = dispatcher.take()  # Atomically persists ISSUED before returning.
# The parent handles each action with actual tools/checks, then records it:
# dispatcher.observe(action["action_id"], observation)
```

`advance()` is not a resident loop. The parent must continue it within the frozen limits. It returns `COMPLETE`, `READY`, `AWAITING_OBSERVATION`, or `BLOCKED`, with `parent_executor_required=true`. It cannot enforce timeout or reliable interruption while the parent is stopped. Instruction and command strings are records, not executed input.

## Action contract

| Action | Required parent work | Exact observation fields |
| --- | --- | --- |
| `SPAWN` | Launch the assigned role in its dedicated worktree once. | `agent_id` |
| `COORDINATE` | Integrate and verify as the parent. | `agent_id`, `accepted`, `reason`, `verification` |
| `POLL` | Observe the bound native ID's actual status. | `agent_id`, `state` |
| `INTERRUPT` | Request interruption of that same bound ID. | `agent_id`, `accepted` |
| `VERIFY` | Check original requirements and artifacts before acceptance. | `agent_id`, `accepted`, `reason`, `verification` |

States are `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, and `UNKNOWN`. Verification has equal-length nonempty `commands`/`exit_codes` lists, with zero exits for acceptance. Extra fields and mismatched native IDs are rejected before recording. IDs are local correlation, not caller authentication or proof of provider idempotency. Keep secrets out of instructions, reasons, and command records.

Native terminal state alone does not release dependent work. The parent handles `VERIFY`; NativeTeam checks scope, revision, and hashes and persists a success receipt before dependencies become eligible. `accepted=true` is not itself evidence of review independence or correct verification content.

## Resume without replay

- Receiving the same run/key/spec is idempotent; a changed key/spec conflicts. Plain NativeTeam runs are not silently adopted.
- A stop between admission and action journaling leaves uncertain work held, not respawned. A stop between NativeTeam creation and dispatcher initialization likewise requires reconciliation.
- `take()` persists `ISSUED` before handoff. A stop before or after external spawn cannot establish exactly-once execution. The parent must correlate the stored action with actual history; another `take()` or reload does not resend it.
- Observations are saved as `OBSERVED` first. Lost acknowledgments after bind/complete are reconciled against existing bindings/receipts, resuming local registration only. Different replacement observations are rejected.
- Unknown POLL/INTERRUPT outcomes are not automatically reissued. Ordinary polls are spaced by at least one second.
- `UNKNOWN` neither relaunches nor immediately interrupts work. The existing deadline can require interruption. Timeout/unknown results never become quality success after late terminal observation.
- An accepted interrupt is not a terminal receipt. Keep the lease until an actual terminal poll; never guess an interrupt target when the native ID is unknown.
- A deadline reached between admission and `take()` blocks stale `SPAWN`/`COORDINATE` handoffs without pretending their leases were released.
- Identical observations can be acknowledged again within retained history. The most recent 32 retired actions and 32 lightweight events are retained. Late acknowledgment of older/truncated history is conservatively rejected, never replayed.
- No automatic retry is added. Use existing bounded retry/escalation only after actual prior release and a known failure.

## Persistence and limits

The journal uses NativeTeam's private `_change()` transaction and the same atomic snapshot under `dispatcher` schema 1. Dedicated 0700 control directories and runtime locks serialize parent-action changes separately from the NativeTeam lock. State inherits the 1 MiB limit; oversize writes fail. Keep large outputs in artifact files, not snapshot bodies.

This is an explicit private-API dependency, not a stable deployment extension contract. Before service integration, review extension/schema compatibility and storage bounds. The [parent adapter](native-parent-adapter.md) adds tool descriptors and observation registration; it does not turn fixtures into unattended execution or automatic wake.

## Offline verification

```bash
PYTHONPATH=tests python3 -B -m unittest \
  test_native_team test_native_dispatcher test_native_parent_adapter \
  test_native_handoff test_native_agent_profiles -v
node --test tests/native_thread_capture.cjs
```

Fixtures cover independent worktrees, parent integration, verifier ordering, held leases, acknowledgment loss, model/role pins, and bounded fan-out. They do not call live models, GPU providers, deployment services, or namespace launchers. Fixture timing is not live-agent latency evidence.
