# Manual Management of Terminal-independent PoC Services

This reference describes an environment-specific, manually approved development PoC from 2026-10-29/30, not an installed default or permission to operate services. The [Japanese historical source](poc-services_jp.md) preserves the exact application record. Set [path variables](path-conventions.md) before examples.

The PoC reuses its existing runner, systemd user manager, native tunnel-client, and readiness checks, not another daemon, PID supervisor, SDK, or authentication system. It does not change the standard-mode units or [AppArmor design](apparmor-policy-design.md).

## Owned targets and credentials

The dedicated runtime determines `agentorchestrator-poc-<runtime-hash>-daemon.service` and `-tunnel.service`. Save a per-start nonce and systemd InvocationIDs in private `poc-services-state.json`; protect operations with `poc-services.lock`/flock without forced unlocking. Runtime/log directories are UID-owned 0700; state/markers are private regular non-symlink files. Logs and a loopback-only health locator remain inside the runtime.

Use installed Codex, native tunnel-client v0.0.18 for the historical trial, and local Git-ignored Tunnel configuration. Do not install persistent units, overwrite existing units, enable autostart/linger, gain system/root permissions, or import keys into the manager.

The historical authorization allowed continued use of one existing external key file, a separately performed mode change from 0664 to 0600 on that inode, and manual startup of the dedicated two transient services with logs. It did not authorize migration/copying/committing the key. That record does not apply to another file or future setup.

`--key-file` requires a repository-external UID-owned 0600 regular file, no symlink, one hardlink, 1..16384 bytes, and safe ancestor directories (root-owned sticky temporary directories are fixture-only exceptions). The helper checks metadata, never creates/copies/moves/chmods the key. Native `--control-plane.api-key=file:/absolute/path` reads it without expanding secret contents into environment or argv. No LoadCredential copy is made. Same-UID processes remain trusted; replacement/revocation/rotation belongs to the user.

## Planning, start, status, and stop

Planning reads metadata without creating units/state or reading key contents:

```bash
cd "$PROJECT_ROOT"
python3 -B scripts/poc_services.py plan --key-file "$CONTROL_PLANE_KEY_FILE"
```

Only with authorization for the exact target and key reference:

```bash
python3 -B scripts/poc_services.py start --key-file "$CONTROL_PLANE_KEY_FILE" --apply
python3 -B scripts/poc_services.py status
```

Do not repeat start while running. Reject socket/unit collisions and unresolved existing queued/running/verifying work; reconcile instead of dispatching unknown jobs as a startup side effect. Unknown tasks never rerun automatically.

The daemon explicitly uses `--development-poc`, retaining its existing bwrap/resource boundary and [no-added-isolation guard](nested-isolation-guard.md). Start Tunnel only after daemon readiness and ancestry to the owned unit MainPID. READY requires healthz/readyz/control-plane poll, including current freshness checks. It does not prove MCP or ChatGPT round trips. Status rechecks current owner generation/readiness, not stale saved READY.

The user manager parents the processes so terminal closure does not stop them. The original manual configuration used Restart=no, no reconnect/restart/resubmission loop, no boot startup, and no guarantee after last logout. Later [login restoration](login-autorecovery.md) is a separate addition.

```bash
python3 -B scripts/poc_services.py stop --apply
```

Stop only the two recorded units after checking Description nonce, Transient property, and InvocationID; Tunnel first, daemon second. Never signal by PID alone, delete stale locks, or stop another runtime. Existing foreground-helper SIGTERM/shutdown performs daemon cleanup. Active tasks could cancel/become unknown; side effects do not roll back. Stop grace is 60 seconds for daemon and 20 for Tunnel; systemd handles remaining owned-unit processes afterward. Unconfirmed stop is not success.

Manual recovery is status, runner state/resource reconciliation, authorized stop, then new start. Reject foreign generations, missing ownership, and uncertain partial startup. Do not delete state/health files and retry. Normal start failure cleans up only identified newly owned units within a bound, recording FAILED/CLEANUP_UNCONFIRMED. Frontend SIGKILL may leave incomplete cleanup; next start refuses uncertainty.

## Environment, resources, and logs

Validate fixed `/run/user/<UID>` ownership/mode/non-symlink and its UID-owned bus socket. Systemctl/readiness share a fixed environment; service commands use `env -i` with PATH/HOME/LANG/user-bus data only, not parent-terminal/manager API keys or MCP-target overrides.

Initial MCP exposure was three read-only tools. Existing CPU quotas are daemon 50% and Tunnel 25%, memory 512/256 MiB, TasksMax 64, swap/core zero, and KillMode=mixed. Worker/App Server scopes remain separate; outer unit limits are not a hard aggregate cap for all workers. Append private logs with LimitFSIZE 64M, no automatic prune, raw HTTP logging false, and no debug environment dumps. Source and same-UID operator trust remain necessary.

## Historical tests and later operation scope

```bash
python3 -B -m unittest discover -s tests -p test_poc_services.py -v
PYTHONPATH=tests:scripts:src python3 -B -m unittest test_phase2_e3.ReadinessTests test_mcp_discovery -v
```

The original integration fixtures used dedicated synthetic runtimes/keys, transient user services, real v0.0.18 client, and a loopback fake control plane, not real credentials/Platform. They checked clean-environment start/status/stop, terminal independence, ownership/collisions, unsafe keys, pending work, failed-start cleanup, and secret/argv/log boundaries. These are historical tests to be run only from a permitted host context, not automatically during document review.

The approved real start observed owned/active units, health HTTP 200/poll, connector status/list/get, matching local completed-task metadata, systemd parentage, and no TTY. Real stop, ChatGPT answer display, logout, and OS restart were not verified by that step. Two prior AppArmor failures and an unexplained EOF remained unresolved; see [E3.2 report](phase2-e3-2-report.md).

On 2026-10-29, separate approval added only `aceis_local_llm_generate` as a fourth operation via `--allow-local-llm`; submit/cancel stayed disabled. Historical limits were localhost resident 30B only, sequential, 16,384 UTF-8 input bytes, 1,024 output tokens, and 90-second MCP deadline, with verified policy and `NOT_EXECUTED` advisory output. It added no namespace/code execution/model startup/API billing/retry. Fifty-one offline checks and catalog/health did not prove ChatGPT inference E2E or current provider health.

By the 2026-10-30 record, seven existing operations plus four mailbox operations made eleven; UI refresh and one bounded answer save/retrieval were observed. Direct Dot catalog/calls and dormant-Chat wake remained separate. Those numbers describe that deployment date, not all present opt-in adapters. Use saved key/Codex/flags through resume-plan or mailbox-only rollback-plan; do not switch credentials to defaults. See [mailbox operations](mailbox-operations.md).

References: [systemd-run 259](https://www.freedesktop.org/software/systemd/man/259/systemd-run.html), [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels), and [client file references](https://github.com/openai/tunnel-client/blob/master/docs/configuration.md).
