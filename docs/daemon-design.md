# Persistent Daemon Design

This reference describes the Phase 2-D2 daemon and later E3 deployment notes. Historical service results are not a statement about a reader's current installation. The [Japanese historical source](daemon-design_jp.md) is unchanged. Set [path variables](path-conventions.md) before using examples.

## Execution and deployment boundary

The daemon is a foreground event loop that invokes the existing runner as independent workers. It retains Python 3.14 standard-library code, App Server stdio, owned SQLite, namespaces, and acceptance verification. The daemon itself adds no HTTP/MCP/network listener, ChatGPT connection, or local-LLM connection; later adapters are separate components.

```bash
cd "$PROJECT_ROOT"
python3 -B src/runner.py --runtime .runtime/daemon-demo daemon
```

This manual launch enters a systemd user **scope**, not an installed/enabled service. Foreground helper, resource supervisor, daemon, and worker retain parent-child relationships and parent-death guards. Stop through socket shutdown from another terminal or SIGINT/SIGTERM to the foreground helper. Do not launch namespace-creating paths from a forbidden [nested-isolation context](nested-isolation-guard.md).

`systemd/agent-orchestrator.service` is a reviewed manual-deployment candidate. Check for existing units/configuration before installation. Its intended EnvironmentFile is `~/.config/agent-orchestrator/daemon.env`, containing only `CODEX_BINARY=/absolute/path/to/codex`, not an API key. Review WorkingDirectory, ExecStart, and runtime placement locally; the repository does not create or overwrite that file.

E3 removed the Install section. Future automatic startup requires a separately reviewed `WantedBy` and authorization. The candidate uses `Restart=on-failure`, `RestartSec=2`, `StartLimitIntervalSec=600`, `StartLimitBurst=3`, CPUQuota 50%, MemoryMax 512M, TasksMax 64, no swap/core dumps, LimitFSIZE 64M, and a 60-second stop grace. It does not change system services, root settings, or linger.

The E3 record did **not** install, enable, or start the operational unit. Transient service fixtures verified restart, dependency stopping, credentials, and resource values, but AppArmor denied user namespaces under systemd, blocking running-task stop/recovery tests. Stage A was incomplete; see [systemd deployment](systemd-deployment.md). Foreground restart is not evidence of service restart behavior. Later development-PoC services use a separately documented boundary.

## Control socket and ownership

The socket is `/run/user/<uid>/agent-orchestrator/<runtime-path-hash>.sock`, avoiding Unix path limits even for long runtime names. There is no TCP fallback. Validate owner UID and mode 0700 for runtime/directories and 0600 for the socket. Both client and server use Linux `SO_PEERCRED` and accept only the same UID. An operator controlling that host UID is inside the trust boundary; this is not multi-user authentication.

Hold `daemon.lock` exclusively per runtime. Remove a stale socket only after obtaining the lease and checking type, owner, mode, failed connection, and unchanged inode. Never replace a regular file, symlink, overly broad socket, or live socket. Normal shutdown unlinks only the bound inode, not existing Codex IPC.

Models and verifiers do not receive the control socket or user bus. `/run/user` is absent from isolated minimal roots and hidden by empty mounts for legacy read-only/verifier paths. Same UID alone does not grant model control access. See [unix(7)](https://man7.org/linux/man-pages/man7/unix.7.html).

## Protocol

One connection carries one JSONL request/response. The request has only `id`, `method`, and `params`:

```json
{"id":"request-id","method":"status","params":{}}
```

| Method | Operation |
| --- | --- |
| `submit` | Persist `QUEUED`; stable `request_key` required |
| `status` | Daemon state, or task evidence selected by `task_id` |
| `list` | FIFO listing; limit 1..100 and offset pagination |
| `cancel` | Cancel queued work or request interruption; never rerun `UNKNOWN` |
| `pause` | Persistently stop new dispatch while running work continues |
| `resume_scheduler` | Clear pause; claim only eligible queued work |
| `shutdown` | Stop dispatch and safely stop owned workers |

Submission reuses `Store.submit` acceptance, workspace, role, and resume checks. Socket default workspaces are independent/read-only. No method selects arbitrary commands or executables. Persist the complete definition, request key, and timeout in the same SQLite transaction.

Only pre-connection backlog congestion is retried within a deadline. A disconnect after sending never automatically resends a submission. CLI supports `--via-daemon submit/show/cancel` and `status/list/pause/resume_scheduler/shutdown`. Its generated key is shown even in failure responses; preserve it for reconciliation.

Nonblocking selector I/O permits at most 64 connections, 128 KiB requests, 4 MiB responses, and bounded I/O waits. Malformed JSON, truncated input, oversized frames, or unsupported methods do not terminate the daemon. Logs record task IDs, operations, and exits, not duplicate request bodies or worker stdout. Oversized status directs the operator to local `show`; it is not silently truncated into success.

## Resources and capacity

`scheduler-config.json` defines control-side CPU, memory, TIDs, CPU-time budgets, and deadlines. Task/verifier limits remain in `resource-limits.json`.

| Role | CPU bandwidth | Memory | TIDs | CPU-time budget |
| --- | --- | --- | --- | --- |
| Daemon | 50% | 512 MiB | 64 | 24 CPU-hours per start |
| Worker | 100% | 256 MiB | 32 | 60 CPU-seconds per task |
| Codex/verifier | Existing policy | 2 GiB | 512 | 60 CPU-seconds per scope |

Budgets cannot be disabled with zero/unlimited values. Validate effective scope values before daemon start; direct `--serve` refuses mismatches. Worker and Codex scopes are separate, not squeezed into the daemon's 512 MiB. An installed service would also constrain the foreground helper; manual launch leaves a small outer control process.

The scheduler stops new dispatch on runtime-capacity pressure while keeping the socket available. Task/worker storage monitoring and daemon JSONL/per-file limits remain. **Total storage is a monitored budget, not a hard quota.** Strict quota requirements need a separately approved and measured filesystem design. There is no automatic pruning or deletion of unknown-task evidence; see [retention](retention-policy.md).

## Shutdown and faults

Stop dispatch, request cancellation for owned tasks, wait for runner `turn/interrupt` and verification cleanup, then force cleanup only after the deadline. Preserve unclaimed queued work. Confirmed interruption and cleanup produces `CANCELLED`; uncertainty produces `UNKNOWN`.

The foreground helper requests socket shutdown before killing a scope supervisor, preserving completion records. SIGKILL propagates through parent-death guards. Restart reconciles leases, boot/PID/start identity, worker scope, and Codex scope without racing a still-live owner. Existing daemons, other runtimes, unrelated processes, and units are not stopped. SQLite contention delays responses/dispatch within a bound without duplicate claims. Dedicated fixtures covered process death, socket loss, and contention; power loss and OS restart were not tested by this phase.
