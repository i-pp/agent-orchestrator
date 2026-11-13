# Standard systemd User Deployment: Phase 2-E3

**Status: blocked; Stage A incomplete. Do not deploy this candidate.** Unit syntax, read-only MCP forwarding, and credential passing worked in fixtures, but AppArmor denied existing namespace isolation under systemd. Stage B installation requires resolution, all required tests passing, and separate authorization. The [Japanese historical source](systemd-deployment_jp.md) is unchanged; later [development PoC services](poc-services.md) are a different boundary.

## Layout and readiness

```text
ChatGPT -> existing OpenAI Tunnel -> aceis-mcp-tunnel.service
                                    tunnel-client v0.0.18
                                      -> read-only stdio mcp_adapter.py
                                           -> private Unix socket
                                                -> agent-orchestrator.service
                                                     foreground helper
                                                       -> guarded daemon scope
                                                            -> worker/App Server scopes
```

The `systemd/` candidates retain the runner, FIFO, leases, SQLite, two-session limit, unknown protection, workspace isolation, and acceptance. The dedicated candidate runtime is `.runtime/systemd-e3`, not an existing E2 manual runtime to stop/migrate.

`After=` alone is insufficient. Daemon ExecStartPost and Tunnel ExecStartPre verify private socket/status/runtime, boot/PID/start, and ancestry to the owned MainPID. Tunnel ExecStartPost checks native healthz/readyz/control-plane poll with a private owner-only non-symlink loopback HTTP URL file. Readiness waits are bounded and fail the unit when unmet.

Tunnel uses BindsTo/After/PartOf dependencies. Fixtures observed stop/restart propagation for manual daemon operations; Tunnel-only loss does not stop/resubmit tasks. Automatic Tunnel recovery after daemon failure/dependency stop is **not guaranteed**. Reconcile daemon readiness and unknown work before an explicit Tunnel start.

## Known blocker

A transient user unit running `unshare --user --map-root-user --mount /usr/bin/true` exited 1 on uid_map write. Correlated kernel evidence showed transition `unconfined` to `unprivileged_userns`, `apparmor=DENIED`, `operation=capable`, `capname=sys_admin`, with userns restriction enabled.

The IDE-origin context `snap.code.code (complain)` passed earlier tests; borrowing that profile under systemd is not an acceptable workaround. Idle daemon startup does not prove isolated task execution. Running-task SIGTERM/SIGKILL fixtures failed during preparation, before the intended fault injection.

See [Ubuntu restrictions](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/#apparmor-unprivileged-user-namespace-restrictions) and the later [AppArmor design review](apparmor-policy-design.md). Do not grant generic Python/unshare permission, disable sysctl restrictions, switch to complain, use root/setuid, or remove namespace isolation. No host-security change is authorized by this design.

## Credentials and profile

The original E2 profile used `env:CONTROL_PLANE_API_KEY`, resolved before CLI overrides; simply adding a file-reference override still failed without that environment variable. The service-specific local profile instead uses fixed stdio target, credential reference, and loopback health, without exposing writes or extra channels. Operational configuration is local/Git-ignored; repository examples are not live configuration.

`LoadCredential=control-plane-api-key:...` passes a systemd-managed runtime credential copy, read natively through `--control-plane.api-key=file:%d/control-plane-api-key`. `%d` is a systemd directory specifier; the secret is not expanded into environment or argv. LoadCredential does not set an API-key environment variable. This mechanism creates a managed credential copy, not a manual file-copy operation. See [systemd credentials](https://www.freedesktop.org/software/systemd/man/259/systemd.exec.html#Credentials).

Dummy-key fixtures verified doctor/forwarding without real credentials. Actual credential migration remains a user/approved authentication step, never a key pasted into chat. Do not import key values into the user manager. UnsetEnvironment blocks alternate key/target overrides; Type=exec avoids the credentials/ExecStartPost/Type=simple warning.

## Resources and shutdown

| Component | CPU | Memory | Swap | Tasks |
| --- | --- | --- | --- | --- |
| Daemon foreground unit | 50% / 100 ms | 512 MiB | 0 | 64 |
| Existing daemon scope | Scheduler 50% | 512 MiB | 0 | 64 |
| Tunnel/stdio adapter | 25% / 100 ms | 256 MiB | 0 | 64 |
| Worker/App Server scopes | Existing scheduler/resource policy | Existing values | Existing values | Existing values |

Outer units and inner scopes are different cgroups. Unit memory limits do not bound all independent worker scopes in aggregate. Preserve guards, cleanup, and receipts. Fixtures measured cpu/memory/pids/swap values, not every OOM fault. Resource excess is not success; uncertainty is unknown. Storage remains sampled monitoring, not hard total quota, with no evidence deletion.

KillMode=mixed first sends SIGTERM only to MainPID so the helper can perform shutdown before losing guards/workers. Daemon/Tunnel stop grace is 60/20 seconds, followed by systemd cleanup of unit remnants; independent scopes still require reconciliation. Restart=on-failure is capped at three starts per ten minutes. Both candidates lack Install sections; this phase does not enable autostart.

## Proposed Stage B, only after resolving Stage A

1. Check unit/show/cat, drop-ins, configuration, and dedicated runtime for collisions. Never overwrite or stop existing unrelated/manual services.
2. Review exact new unit destinations under `~/.config/systemd/user/`, daemon.env containing only installed absolute `CODEX_BINARY`, and owner-only directory/file modes.
3. Let the user or approved authentication mechanism provide the existing external credential to the reviewed location. Do not print, copy into chat/Git, or overwrite an existing credential/symlink. Do not modify Codex authentication.
4. Place only authorized new units, verify, and reload. Do not enable, enable-now, or change linger.
5. Start only the approved two services in readiness order:

   ```bash
   systemctl --user start agent-orchestrator.service
   python3 -B scripts/service_wait.py daemon --runtime .runtime/systemd-e3 --unit agent-orchestrator.service --timeout 20
   systemctl --user start aceis-mcp-tunnel.service
   "$TUNNEL_CLIENT" health \
     --url-file "$XDG_RUNTIME_DIR/aceis-mcp-tunnel/health-url" --require-control-plane-poll --json
   ```

6. Record unit ownership/state, health/poll, stdio child, and read-only discovery separately. No raw HTTP logging or secret-match values. A shutdown WARN alone is not proof of connection failure.

Authorized stop targets only these installed services, Tunnel first, then daemon. Before restart, reconcile readiness, queued/unknown work, and released scopes. Do not rewrite unknown work as queued or automatically resume. Withdrawal preserves runtime/DB/evidence/credentials and removes only authorized newly installed units after safe stop/reload.

## ChatGPT validation after Stage B

Select the approved existing Tunnel in the intended workspace, not a new Tunnel created as a workaround. Confirm actual server-authentication choices; this adapter has no custom OAuth. Missing compatible authentication, association, or Read/Use permission blocks connection pending administrator/user action. Never enter the Tunnel runtime key into a ChatGPT server-auth form. See [official connection steps](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels#connect-from-chatgpt).

Verify exactly three read-only tools for this candidate and stop if submit/cancel appears. Perform one explicitly requested `orchestrator_status`, retain time/JSON/call ID, compare local task counts/session limit/pause, and correlate runtime audit begin/end. ChatGPT prose alone does not prove a real tool call. This E3 plan is not a claim that the steps were executed.
