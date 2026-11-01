# Chat–Codex pipeline: verified state and bounded next checks

## Verified on 2026-11-01

The public Chat MCP submitted one fixed request. After explicit recovery of its unused native context, the same persisted logical task returned `PIPELINE_SMOKE_OK`. Chat independently recovered that answer with `codex_task_get`. The original logical thread, task, request keys and input hash were retained. This was one inference attempt, with no automatic retry or task resubmission.

The first native context had been created and its app-server connection closed before any turn. Its later `thread/resume` returned `NO_ROLLOUT`. A paused operator-only action now permits one explicit replacement when the exact ledger and saved diagnostic prove that no dispatch or turn ever occurred. The private ledger preserves the original native ID, replacement relation and reason. Public logical quotas remain one thread and one task; historical native creation count is two. This is not general recreate-on-error recovery. Prior/unknown dispatch, history, stale proof, or an uncertain replacement acknowledgement blocks recreation.

A new native context retains its connection and runner lease until its first terminal turn is confirmed by `thread/read` with matching ID and terminal status. The initial empty-context wait is bounded by the existing timeout. A failed history check or expired context is explicit and never causes an automatic replacement. Resume of persisted history remains the ordinary later-task path; a new real turn after such a resume has not yet been verified.

The pipeline-only inner daemon budget is 1GiB and 512 kernel tasks (processes plus threads). The separate supervisor remains 512MiB/64 tasks; the tunnel remains 256MiB/64 tasks. Memory sum is 1.75GiB, CPU and 64MiB single-file limits are unchanged. During the successful run, inner peak memory was 496,209,920 bytes and peak kernel tasks were 137; memory/PID limit-event and OOM counts were zero. The executor child was reaped, cleanup was RELEASED, and held tasks were zero.

The existing login entry was updated after success, without starting it or restarting healthy services. Its previous entry was retained for rollback without reading its pin contents. After a separate explicit lifecycle approval, lingering was enabled only for the existing service owner. An actual reboot then demonstrated external MCP health and recovery of the original saved answer before desktop login; the user subsequently confirmed graphical-login application autostart. Boot ID and user-manager/bootstrap/daemon/tunnel invocation generations all changed. No inference was used in that reboot verification. Host-specific references, runtime state and raw receipts remain outside Git.

The graphical entry reuses the installed desktop launcher; it does not make the GUI a headless service or migrate a pre-login GUI process into a later display session. Lingering also affects the existing owner's other enabled units subject to their targets and conditions. No additional root service, display server, keyring change or automatic login was introduced. Before-login Codex authentication and a new model turn remain untested; successful readback of a completed task does not establish either.

The executor uses existing ChatGPT authentication and `gpt-6.1-sol`. Account type does not establish billing or usage-pool guarantees. Inherited MCP/apps/plugins remain unrestricted; filesystem read-only does not prevent external connector writes. The separately prepared restriction profile is deferred, not asserted effective.

## Tested versus untested

| Operation or condition | Evidence |
| --- | --- |
| Model/account catalogue, allowed workspace/model and thread creation | Real read probe and public MCP |
| Chat task submission, same-task continuation, completion and answer readback | Real public MCP smoke |
| Events, operation deduplication, unknown ACK, queue/steer/cancel/archive contracts | Fake-peer/offline tests; full live functional coverage pending |
| Paused NO_ROLLOUT replacement, lineage and first-response cleanup | Real single recovery and offline rejection cases |
| Resume after completed durable history and another turn | Offline only |
| Reboot recovery before desktop login | External MCP status and saved-answer readback PASS; new boot/invocation generations |
| Graphical-login ChatGPT autostart | User-confirmed PASS; existing launcher reused |
| New Codex authentication/inference before login | Not tested |
| Approval-wait notification | Offline only; never automatic approval |
| Codex-initiated Chat wake | Not connected; mailbox storage is a separate feature |

## Minimal next verification proposal — not activated

Keep the same workspace and model allowlist, existing logical thread, concurrency one, 120-second turn deadline, 4KiB input/answer and zero retries. The existing lifetime task quota is consumed; do not reset its ledger or request keys.

A separately approved finite extension from cumulative one to **five tasks** would provide four additional records for: (1) ordinary resume and a fixed response with explicit model selection; (2) one bounded active task for a single steer and cancel attempt; (3) a stored/queued task cancelled before dispatch; (4) a stored/queued fixed-response task, completion and event-cursor recovery. At most three additional turn starts are intended. If the active task completes before steer/cancel, report that boundary as untested; do not generate another attempt. Reject a disallowed model without starting it. Archive only after all owned tasks are terminal, preserving history. Do not request a second workspace/model or automatic quota renewal.

A completed-task readback across a real reboot is now verified. After idle/cleanup verification, any additional stop/start or new-history/inference test still needs its own bounded scope. Preserve keys, DB, mailbox grant, service ownership and the separately approved linger setting; do not add another service. These are proposals, not changes to the current runtime policy.

Routine operation should first use a finite cumulative allowance such as one thread/eight tasks, concurrency one, with explicit renewal after reviewing results. Current policy has no automatic daily reset or task-quota expiry; do not describe a manual operating window as an enforced scheduler limit. No permanent expansion is installed by this document.

## Recovery and rollback

Check persisted operations before retrying any uncertain write. Cancel acceptance is separate from confirmed terminal state. A logical deadline does not prove an OS process was killed. Keep UNKNOWN operations and the cleanup lease until reconciled.

For code rollback, pause admission, reconcile active or uncertain work, and stop only the same owned services when idle. Restore reviewed source preimages and the retained login entry as needed; reload definitions and start the approved configuration. Never restore an older task DB, erase replacement lineage, reset quota, synthesize a native session or reissue an uncertain inference. Local run artifacts contain the exact source hashes and rollback preimages; no host-specific artifact path is part of this repository.
