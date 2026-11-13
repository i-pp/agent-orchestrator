# Resource Limits

This reference describes Phase 2-D1 and its D2 control-side extension. Environment-specific results remain historical; see the [Japanese source](resource-limits_jp.md) and [D1 report](phase2-d1-report.md).

## Boundary and configuration

Codex App Server trees, history probes, and allowed verification commands each enter a new private systemd user scope. Existing user/mount/PID/IPC namespaces, dedicated Codex home, read-only authentication bind, and sandbox policy remain. D1's coordinator/Python workers were outside those task scopes; D2 adds separate control-side limits.

The original environment had delegated cpu/memory/pids controllers under its user service, and systemd 259.9 accepted scope values under a dedicated `agentorchestrator.slice`. This is not a guarantee for another installation. Do not directly change host cgroups, existing slices/services, Delegate, sudo, or packages to make a launch pass. See [cgroup v2](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html) and [systemd resource control](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html).

Operators manage `resource-limits.json`; prompts/model output cannot configure it. Freeze policy per worker and record it in execution-owner JSON. There is no disable-limits CLI flag. Unknown keys, zero/negative values, and invalid types are rejected.

| Setting | Default | Meaning |
| --- | --- | --- |
| `cpu_percent` | 100 | One CPU across the scope; `cpu.max=100000 100000` |
| `cpu_budget_ms` | 60000 | Cumulative scope CPU time; monitoring stops excess |
| `memory_bytes` | 2147483648 | `memory.max=2 GiB` |
| `pids` | 512 | Scope process/thread limit |
| `file_bytes` | 67108864 | Inherited soft/hard `RLIMIT_FSIZE=64 MiB` |
| `runtime_bytes` | 2147483648 | Reject/stop at 2 GiB runtime storage |
| `free_bytes` | 1073741824 | Reject/stop below 1 GiB filesystem free space |
| `log_bytes` | 33554432 | Normal execution JSONL limit, 32 MiB |

Swap and core limits are zero. Accepted CPU values are 1..200%, pids 8..4096, memory at least 32 MiB, and logs at least 4 KiB. Reevaluate host capacity and two-way concurrency before relaxing values. Defaults were tested with small Codex fixtures, not arbitrary build workloads.

## Enforcement and receipts

1. Check runtime usage/free space and start a fresh UUID-named scope.
2. Trusted `resource_entry` validates cgroup path and effective CPU/memory/swap/pids values.
3. Set `RLIMIT_FSIZE`/`RLIMIT_CORE` and fsync a `READY` receipt before spawning namespace children.
4. Validate the parent chain and parent-death signal; retain `child_entry` for child launch.
5. Monitor `cpu.stat`, `memory.events`, `pids.events`, and storage usage.
6. Save `FINISHED`, exit code, and final counters after child exit.
7. `Process.close` confirms owned PID/start and scope release and appends cleanup evidence.

Scopes are `agentorchestrator-<UUID>.scope`; receipts live under runtime `resource-events/`. Requested/effective values, counters, file limits, and termination are traceable from owner/cleanup/log records. Core schema stays v3; old rows remain. Scope collection can leave an empty shared slice without live processes or memory charges.

[User scopes](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html) preserve caller-child ancestry rather than detaching as services. Unavailable user bus/controllers or effective-value mismatch refuses child launch. There is no unconstrained fallback. The [nested-isolation guard](nested-isolation-guard.md) remains independently applicable.

## Outcomes and recovery

CPU bandwidth throttling is normal control, not failure. CPU-time exhaustion, memory/pid/file/storage/log limits do not count as success. Confirmed excess with cleanup is `FAILED`; supervision/cleanup uncertainty after dispatch is `UNKNOWN`. Even a normal Codex result cannot complete a task with unmet resource conditions. Verifier excess fails acceptance; unconfirmed scope release is unknown.

After parent loss, a `READY` receipt may remain. Respect live leases, then mark only abandoned `RUNNING`/`VERIFYING` unknown. Reconcile boot/PID/start and the receipt's private cgroup absence or `populated=0`. Parent-death and lease release are asynchronous; do not race a stopping worker. Missing/corrupt receipts are not success, and unknown work is never automatically rerun.

## Storage is not a hard total quota

`io.max` limits I/O rate/count, not stored bytes. The implementation combines:

- Kernel-enforced **per-file** `RLIMIT_FSIZE`; descendants cannot raise the hard limit.
- Sampled runtime/free-space checks before and during work, normally about every 0.2 seconds but possibly delayed by long OS operations.
- Bounded JSONL retaining a complete-line prefix and a limit marker, without deleting or rotating existing evidence. Cleanup also remains in SQLite/receipts.

See [getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html). Rapid many-file writes can overshoot between samples. Usage uses the larger of apparent bytes and `st_blocks * 512`; filesystem metadata is not fully included. Do not start an operation requiring strict total-storage guarantees on this mechanism alone. Project quotas/dedicated filesystems need separate design, permission where applicable, and measurement; D1 installed none.

## Verification scope and D2

Historical fixtures exercised real cgroup throttling/CPU budgets/OOM/fork denial, real file limits, and storage-budget stopping without changing host-wide limits or existing processes. Real-Codex evidence covered two sessions, coordinator SIGKILL, and a separate normal task. D1 did not verify control-side kernel limits, hard disk quotas, OS restart/power loss, high load, or other environments.

D2 uses separate daemon/worker scopes from `scheduler-config.json`, leaving task limits unchanged. Long-lived daemon and short task CPU budgets differ. Storage pressure stops new dispatch while preserving the control socket. See [daemon](daemon-design.md) and [D2 report](phase2-d2-report.md).
