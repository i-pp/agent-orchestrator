# MCP Service Restoration at Login

This guide explains the persistent login entry point around existing transient PoC services. The [Japanese historical source](login-autorecovery_jp.md) preserves the full dated application record. Historical approval does not authorize installing or enabling anything on another environment.

## Recorded deployment status

On 2026-10-30, after explicit user approval, four reviewed files and one dedicated login-entry unit were placed, daemon-reload ran, and the entry was enabled **without `--now`**. It was loaded/enabled/inactive with no LoadError and zero executions. Existing daemon/Tunnel PIDs, InvocationIDs, health, DB, key references, settings, and grant were unchanged; linger was no. The recorded mock/unit total was 53 passes. Actual logout/login restoration was not verified.

The first unit had a bad shell-style WorkingDirectory quote. Removing the unnecessary directive and adding regression coverage fixed manager loading without ever starting the entry. The failure remains in the record. Standalone `systemd-analyze verify` was blocked by socket EPERM in that environment. No live-service restart or model/GPU operation was performed.

## Design and pinned inputs

One persistent **entry user unit** is enabled under `default.target` and calls existing `poc_services.Services` once when the user manager starts. Actual daemon/Tunnel units remain transient. Replacing them with two persistent units would change transient/nonce/InvocationID ownership and is not part of this design.

With `Linger=no`, the user manager starts at login and may stop at the last logout. This is not unattended boot startup or execution on every screen unlock. Multiple concurrent logins share one manager and one entry execution. Enabling linger changes key/service lifetime and needs separate explicit authorization.

- `scripts/poc_login.py`: plan/render/restore, not install/enable/linger or credential creation.
- `systemd/agent-orchestrator-login.example.service`: oneshot, `RemainAfterExit=yes`, `WantedBy=default.target`, no restart, 240-second start limit, no ExecStop.
- `tests/test_login_recovery.py`: offline fixtures without actual service/socket/namespace/model use.

The runtime-path hash determines `agentorchestrator-poc-<runtime-hash>-login.service`. Existing daemon/Tunnel names, DB, scopes, key location, resources, and authentication stay unchanged. No public tool, credential copy, LoadCredential, shell, timer, or retry loop is added.

Pin runtime/Codex/key paths, four flags, fixed-mailbox policy hash, and grant-policy hash in an invocation SHA-256 checked at render and restore. Only the existing eleven-operation combination is accepted. A changed Codex path or new approved grant requires reviewed rerendering, not silent tracking; mismatch is BLOCKED. Expired grants retain permitted reads without resetting time, revocation, or budget.

## Restoration state machine

1. Match saved state/policy to the expected invocation hash.
2. If both correctly owned services are READY, return `ALREADY_READY` with no changes, even if work is running.
3. Consider restoration only when both units are not found, socket/health locators are absent, and state is STOPPED or a stale READY after manager loss.
4. Under the existing operation lock, reread pins, unit absence, and zero unresolved work. Run existing preflight with the original key reference. Reconcile only stale READY to STOPPED; do not stop units or delete locators.
5. Call existing `Services.start` once, retaining its ownership/lock/idle checks, daemon readiness, Tunnel health/poll, resource limits, clean environment, and no-added-isolation boundary.
6. On normal failure, existing cleanup targets only that newly owned generation, recording FAILED or CLEANUP_UNCONFIRMED. The entry never retries.

STARTING/FAILED/CLEANUP_UNCONFIRMED, a surviving single unit, foreign generation/owner, remaining locators, or queued/running/verifying/unknown/human-waiting work is blocked/unknown for manual reconciliation. Do not replace uncertainty with a resend or another ID. A concurrent legitimate CLI start is not stopped, adopted, or retried by the entry.

This is not a monitoring daemon. RemainAfterExit active is not MCP health; use existing status/health. SIGKILL, entry timeout, or user-manager loss may leave incomplete cleanup, and the next entry refuses unconditional startup. No extra isolation layer is created.

## Preparation and authorized application

Set `RUNTIME` to the existing runtime, `PREPARED_UNIT` to an authorized local output file, and `UNIT_NAME` to the calculated name. Do not create another runtime/key.

```bash
python3 -B scripts/poc_login.py plan --runtime "$RUNTIME"
python3 -B scripts/poc_login.py render --runtime "$RUNTIME" > "$PREPARED_UNIT"
systemd-analyze --user --generators=no --man=no verify "$PREPARED_UNIT"
```

Rendered local paths stay out of Git. Compare plan/render hashes, key references, flags, and policy hashes without reading/copying/importing key contents. Templates retain placeholders.

Application requires authorization for the exact new unit, reload, enable without immediate start, and future automatic use of existing key references/eleven operations. Do not overwrite an existing unit, add authentication, or enable linger. Recheck live status/work and concurrent GPU plans before applying; placement/enable alone does not require stopping the existing services.

```bash
# First verify directory ownership, absence of symlinks, and no existing same-name unit.
install -m 0600 "$PREPARED_UNIT" "$HOME/.config/systemd/user/$UNIT_NAME"
systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"
systemctl --user is-enabled "$UNIT_NAME"
```

Do not add `--now`, restart, logout, or `loginctl enable-linger`. Compare existing InvocationIDs, health, DB, and grant before/after. A separately authorized one-shot no-op smoke may start the entry once only after both services are READY, checking ALREADY_READY, unchanged InvocationIDs, and zero model turns. Stopped-state restore and actual logout/login are different tests requiring coordinated timing, not a combined model/GPU evaluation.

## Rollback and limits

```bash
systemctl --user disable "$UNIT_NAME"
systemctl --user is-enabled "$UNIT_NAME"
```

Without `--now`, disabling removes future startup without stopping daemon/Tunnel/GPU work; there is no ExecStop. Keep the installed file disabled, or, if authorized, move only a matching-hash file to an approved backup and reload. Preserve keys, DB, grant, transient units, and authentication. Existing resume-plan remains available. Disable the entry first if an intentional stop must survive the next login. Do not renew grants or change exposure as a rerender/rollback side effect.

Historical mock/unit success and manager loading do not prove real login restoration or survival after logout. If network readiness misses the existing 75-second Tunnel deadline, fail within the bound without automatic retries. Consider a separately bounded retry design only after observing that need.

References for the installed 259 series: [oneshot/RemainAfterExit](https://www.freedesktop.org/software/systemd/man/259/systemd.service.html), [enable versus start](https://www.freedesktop.org/software/systemd/man/259/systemctl.html), and [linger](https://www.freedesktop.org/software/systemd/man/259/loginctl.html#enable-linger%20USER%E2%80%A6).
