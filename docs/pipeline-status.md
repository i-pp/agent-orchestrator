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

## Subsequent native discovery verification

The previously blocked native tunnel discovery integration was rerun through the supported approved execution mechanism on 2026-11-01: focused test **PASS**, then aggregate **186 tests PASS, zero skips/errors**. The focused run took 0.796 seconds and the aggregate suite 2.524 seconds. All original assertions were retained. A temporary synthetic YAML profile replaces dependency on the ignored operator-local live profile; HTTP control plane and health endpoints remain loopback-only and credentials are public fake fixture values.

The private fixture daemon and real installed tunnel-client completed discovery, tool listing and readonly status. Owned cgroups, control sockets and temporary runtime directories were recovered. Live daemon/tunnel PID and invocation IDs stayed unchanged. No inference, credential/keyring change, deployed quota change or live-service restart occurred.

An overrestrictive check in the standalone evidence wrapper had additionally treated a conservative UNKNOWN context classification as a reason to block this namespace-free test. Its correction was explicitly approved. The classifier and repository namespace guard were preserved, and OS/Codex sandbox enforcement was not altered. The result does not grant namespace creation or prove headless model authentication.

Offline policy-version migration is now prepared and fixture-tested; see [pipeline-policy-migration.md](pipeline-policy-migration.md). It preserves consumed attempts and history, blocks partial/unknown transitions, and still requires explicit live rollout approval. No runtime quota or model allowlist has been changed.

## Minimal next verification proposal — not activated

Retain the same workspace, existing authentication and resource limits. Proposed models are `gpt-6.1-sol` and `gpt-6-luna`; validate current catalogue availability after the approved policy change, with no automatic model substitution. Proposed cumulative quotas are two logical threads and six tasks, concurrency one, 120-second logical turn deadlines, 4KiB input/answer and zero generation retries. Five additional task records permit four intended new turn starts; the absolute ceiling is five if pending-input cancellation races dispatch. No existing ID, request key, attempt or history is reset. Internal native-context recreation is not part of this proposal.

Use the original Sol-default thread for one explicit Luna task override, then later an omitted-model Sol task to check that the override does not change the original default. Create one Luna-default thread immediately before its first task; steer that active task once and confirm same-turn acceptance and terminal state. For the later active Sol task, store/queue one input for pending cancellation and another for automatic queued completion, then interrupt the active task once. Confirm actual terminal stop separately from request acceptance. If completion races steer/cancel, record the unverified boundary instead of generating another test. Replay events by cursor; archive both threads only after every input is terminal, preserving original smoke history.

Read-only account/catalogue/status/events need no new model turn. A coordinated no-login reboot test may use the first already-planned fixed-response task to test actual pre-login authentication without adding a sixth attempt. If authentication is unavailable, do not unlock the keyring or log in automatically; keep that outcome separate from preserved-answer readback. Existing inherited MCP/apps/plugins remain as previously disclosed. No new API billing route is enabled, and billing/usage-pool guarantees remain unverified.

The coordinator operating budget is at most24 MCP write calls,160 read calls and20 minutes of active monitoring, excluding user reboot/login wait. These are run-control ceilings, not new policy counters; background daemon metadata polling remains governed by existing scheduling/deadline behavior. UNKNOWN operations retain evidence/slots and are not replayed. Do not force a service stop if the supported lifecycle helper refuses active/UNKNOWN work; stop that verification phase and reconcile.

Application requires one stop/start of the same owned daemon and tunnel after idle verification, the reviewed history-preserving policy transition, updated service snapshot and matching existing login-entry pin. User daemon-reload updates definitions; no new service, credential, port or security configuration is needed. The approved linger setting is retained. See [pipeline-policy-migration.md](pipeline-policy-migration.md) for exact stages and rollback. Core policy COMMITTED does not mean service/login binding installation is complete. No part of this proposed activation has occurred.

Offline implementation adds33 migration checks; the full219-test fixture aggregate passes, including the synthetic loopback native-tunnel integration. The pending Git preservation scope is nine reviewed source/test/doc files, including the previously pending discovery test and this status document. New migration publication and runtime expansion remain separately approval-gated. There is no automatic budget renewal, daily reset or task-quota expiry.

## Recovery and rollback

Check persisted operations before retrying any uncertain write. Cancel acceptance is separate from confirmed terminal state. A logical deadline does not prove an OS process was killed. Keep UNKNOWN operations and the cleanup lease until reconciled.

For code rollback, pause admission, reconcile active or uncertain work, and stop only the same owned services when idle. Restore reviewed source preimages and the retained login entry as needed; reload definitions and start the approved configuration. Never restore an older task DB, erase replacement lineage, reset quota, synthesize a native session or reissue an uncertain inference. Local run artifacts contain the exact source hashes and rollback preimages; no host-specific artifact path is part of this repository.
