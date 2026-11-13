# Codex pipeline clean-HEAD extraction draft

This review draft extracts the opt-in Codex app-server pipeline and resident HostGateway from the existing dirty working tree onto clean base `baee13cf06e013c5dcfe0e28ff594ca23947c6a8`. It is a code review patch only; it does not update a running service or publish an MCP endpoint.

## Scope

- Adds `codex_pipeline`, `codex_host`, `codex_app_protocol`, policy validation, cleanup reconciliation, policy migration, and a small `private_state` helper.
- Connects the pipeline through the daemon, runner, and stdio MCP adapter. The adapter changes were reconstructed from the pipeline-specific portions only; local-model, mailbox, grant, and experiment features from the dirty adapter were excluded.
- Adds an opt-in bounded daemon resource profile. Existing default memory and process limits remain unchanged when the flag is absent.
- Adds the minimal policy bundle files loaded by the pipeline and the exact audit allowlist entries needed for those files. No local override, credential, runtime database, Tunnel file, systemd unit, or network configuration is included.

## MCP activation boundary

The daemon and the MCP adapter both default to pipeline disabled. To expose the `codex_*` tools in `tools/list`, the daemon and adapter launch commands each need `--allow-codex-pipeline`. The repo's existing launch targets are `systemd/agent-orchestrator.service` (runner/daemon command) and `systemd/aceis-mcp-tunnel.service` (the adapter command inside `--mcp.command`). After a reviewed change to those commands, both services must restart and the ChatGPT MCP client must reconnect/rescan tools. The AceIS unit is currently a staged deletion in the user's worktree, so this draft leaves both unit files unchanged; do not regenerate or copy a live unit from older content. This draft remains stdio-only and implements no MCP Events subscription, HTTP callback route, or ChatGPT conversation wake.

## Review constraints and incomplete coverage

- The common-policy loader requires a separately managed, existing local checkout at the pinned relative path or a valid operator-local override. That ignored local override is intentionally excluded from this patch; the draft cannot start a real HostGateway in an environment without the required checkout.
- At the original extraction baseline, the canonical `tests/test_codex_pipeline.py` Peer fixture was not included. The earlier statement that it was absent from Git history was incorrect: it is recoverable from pipeline branch commit `c62d97f056e0eb3e1906ef5b1c114d5a11ba66d4`. The [2026-11-13 reconciliation](branch-reconciliation.md) restores compatible complete fake-peer tests and records which older service/login assertions remain unimplemented. Synthetic/offline passes are not live deployment evidence.
- Service startup, real Codex turns, TCP/network calls, and external notifications are not tested here.
