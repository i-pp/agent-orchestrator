# MCP Events Deployment Plan

This describes the shortest deployment path for the isolated candidate. Nothing in this file has been applied to the host or ChatGPT workspace.

## Runtime topology

```text
ChatGPT Work chat
    ⇅ OpenAI Secure MCP Tunnel
tunnel-client --mcp-server-url=http://127.0.0.1:8765/mcp
    ⇅ loopback Streamable HTTP
mcp_adapter.py --transport http --allow-codex-pipeline
    ⇅ existing private control socket
agent-orchestrator daemon + Codex pipeline
    ⇅ read-only source ledger / durable outbox
bounded notification worker → validated HTTPS callback
```

OpenAI's current Tunnel guide supports an MCP server reachable over stdio or HTTP and documents `--mcp-server-url` for HTTP. The existing Tunnel command uses `--mcp.command` for stdio; Events needs the HTTP endpoint. The adapter binds only to `127.0.0.1`, and the Tunnel client should target that endpoint from the same host. Do not expose the adapter port on a LAN interface.

## Required configuration

The daemon and dedicated HTTP adapter service each need the same systemd encrypted credentials and OAuth resource-server configuration:

```ini
LoadCredentialEncrypted=mcp-events-aes-key:%h/.config/agent-orchestrator/credentials/mcp-events-aes-key.cred
LoadCredentialEncrypted=mcp-events-owner-hmac-key:%h/.config/agent-orchestrator/credentials/mcp-events-owner-hmac-key.cred
Environment=ACEIS_MCP_EVENTS_ENABLED=1
Environment=ACEIS_OAUTH_ISSUER=https://<existing-idp>/
Environment=ACEIS_OAUTH_AUDIENCE=https://<public-mcp-host>/mcp
Environment=ACEIS_OAUTH_ALLOWED_SUBJECTS_JSON=["<opaque-sub-1>","<opaque-sub-2>"]
Environment=ACEIS_OAUTH_JWKS_FILE=/etc/agent-orchestrator/mcp-jwks.json
Environment=ACEIS_OAUTH_RESOURCE_URL=https://<public-mcp-host>/mcp
Environment=ACEIS_OAUTH_AUTHORIZATION_SERVER=https://<existing-idp>/
Environment=ACEIS_OAUTH_REQUIRED_SCOPES=mcp:access
```

All values above are placeholders. Select an existing OAuth 2.1 authorization server, configure its audience/scopes, and map each permitted caller to a stable opaque `sub`; do not use email or OpenAI `_meta` as an ACL. The server verifies RS256 JWTs locally using the pinned public JWKS and does not issue tokens or fetch keys. Its protected-resource metadata route points ChatGPT to the configured authorization server. The issuer and discovery endpoints must be reachable from ChatGPT and the Tunnel host. No keys, client credentials, access tokens, or live settings are created by this candidate.

The AES and owner-HMAC keys remain separate systemd credentials. The daemon and HTTP adapter need identical values. The Events flag defaults off; OAuth/JWKS configuration alone does not turn on callbacks. `--transport http` now refuses to start without valid OAuth configuration. The existing stdio/local mode remains unauthenticated and independent.

Start in dependency order: the existing daemon, the loopback HTTP adapter, then `tunnel-client`. Configure the Tunnel client with `--mcp-server-url=http://127.0.0.1:8765/mcp` instead of the stdio `--mcp.command`. Both daemon and HTTP adapter must load identical AES and owner-HMAC credentials. The Tunnel runtime API key is separate and remains the existing control-plane credential.

After integrating code into a clean, reviewed base, configure an existing OAuth provider and confirm each intended user's distinct `sub` through its approved connection flow. Then confirm the still-required Owner-only draft condition for the previously approved synthetic callback, update the ChatGPT MCP connection to authenticated HTTP, refresh its advertised tools/events, and run the synthetic callback once. Stop on an unknown result and inspect the subscription/outbox state before retrying. A `2xx` callback acknowledges receipt; it does not prove the ChatGPT conversation has resumed or produced a response.

## Startup and service checks

1. Verify the daemon is ready and the source ledger is private.
2. Verify the HTTP adapter reports readiness on loopback and refuses non-loopback binds.
3. Verify `server/discover` advertises Events only when all gates and providers pass.
4. In ChatGPT, verify the connected app shows the expected events, create one synthetic subscription, and check callback verification.
5. Trigger one synthetic terminal event and confirm one callback, safe payload, and successful ChatGPT processing. Record task terminal time, callback response time, and first observed ChatGPT response separately.
6. Verify stdio status tools and the Codex pipeline still work after the HTTP adapter is enabled.

## Rollback

1. Stop `tunnel-client` so ChatGPT stops reaching the HTTP adapter.
2. Stop the HTTP adapter and unset `ACEIS_MCP_EVENTS_ENABLED` for both processes. Keep the daemon and existing stdio path available.
3. Restore the previous Tunnel stdio command and service dependency order. Start the original Tunnel and verify the prior `tools/list` / read-only status call.
4. Preserve both the Codex pipeline ledger and `mcp-events.sqlite3`. Do not resubmit or replay Codex tasks as part of notification rollback.
5. If credentials are merely unavailable, restore the exact previous pair before retrying. If either key was changed or lost, keep delivery disabled and preserve the outbox for recovery; do not delete it or silently create replacement keys.

Rollback has not been executed against production. No authorization server, opaque subject allowlist, JWKS, audience, Tunnel HTTP route, or service setting has been configured in the live environment. Secure MCP Tunnel association and ChatGPT workspace app access are separate controls; tunnel association alone does not establish a per-user boundary.
