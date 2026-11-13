# Readiness Requires a Fresh Tunnel Poll

This reference records the 2026-10-30 readiness fix and offline evidence. Code placement, Git integration, and real-service verification are separate. Editing this guide does not operate a service. The [Japanese historical source](poll-freshness_jp.md) is unchanged.

## Problem

During the incident, external MCP returned `tunnel_client_not_seen` while local health reported `result=ok` and true flags. `control_plane_poll.value` retained a successful 13:29:02 UTC timestamp despite later transport timeouts.

The observed value in tunnel-client v0.0.18 was Unix seconds. Checking flags alone accepted stale success as READY. The change fixes readiness interpretation; it does not establish the root cause of transport timeout.

## Conditions

Retain the private URL file, loopback restriction, fixed key-free health command, deadlines, and all existing ok-flag checks. Additionally:

- Poll value and local wall clock must be positive finite numbers. Reject bool, string, missing/nonfinite values, and overflow.
- The last success must not be in the future and must be **at most 90 seconds old, inclusive**.
- Malformed JSON/object structure is not presumed healthy. Callers treat uninterpretable health or timeout as NOT_READY.

Ninety seconds is roughly two cycles of the observed 35-second poll deadline plus ten-second maximum backoff, earlier than the external 300-second disappearance error. It is an initial threshold, not an SLA or transport guarantee. Backward wall-clock movement conservatively fails readiness. Revalidate units/types after a client update; missing values never fall back to flags alone.

## Effects and non-effects

`poc_services status` reports stale polls as NOT_READY without changing saved owner/phase. Login restoration no longer treats unhealthy existing units as `ALREADY_READY`; it waits for reconciliation rather than restarting them. Future start/ExecStartPost waits require freshness while retaining existing startup deadlines and cleanup rules.

The fix adds no restart/watch loop, MCP tool, reauthentication, or permissions. `orchestrator_status` scheduler metadata does not call this helper and keeps its schema. New CLI/login processes import it; applying the source does not require restarting a live daemon/Tunnel just to load it. Native tunnel-client health output is not changed.

## Historical verification

Sixteen new freshness tests covered the incident, inclusive 90-second boundary, future/invalid/missing values, status/login paths, bounded CLI failure, and the fixed key-free command. The old helper failed the incident assertion as a control. With related login/grant/readiness fixtures, the recorded total was **54 passing offline checks**. No live service/model/GPU/NPU/namespace operation or real-systemd integration success was claimed.

Fresh checkouts lacked ignored Tunnel configuration required by existing mock preflight. Only the test worktree copied public synthetic examples, without substituting real deployment values:

```bash
cp systemd/aceis-mcp-tunnel.example.yaml systemd/aceis-mcp-tunnel.yaml
cp systemd/aceis-mcp-tunnel.example.service systemd/aceis-mcp-tunnel.service
```

These ignored copies are for mocks, not real tunnel-client use. Do not copy or overwrite live configuration. No checks were skipped or weakened to make that evidence pass.

## Application and rollback

The original candidate comprised `service_wait.py`, one existing readiness assertion, new tests, and this guide. Recheck current hashes/diffs before authorized integration. Do not change services, units, keys, runtime configuration, or DB. Inspect readiness through a new CLI without restarting, and check external MCP reachability separately.

Rollback only the candidate's changes, integrating any later edits rather than overwriting them. Reverting also restores the stale-success limitation. Recurring transport diagnosis, external reachability monitoring, and actual dispatcher/adapter tool smoke tests remain independent follow-ups.
