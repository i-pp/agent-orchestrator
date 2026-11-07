# Local MCP Events deployment

This mode is for one local OS user and a private ChatGPT development app. OAuth is not used. The HTTP listener binds only to `127.0.0.1`; `--allow-single-user-local-http` is required. Keep the app in the user's private Dev settings and do not publish it to the workspace.

Secure MCP Tunnel is the transport from OpenAI to the private server. Its client needs outbound HTTPS to OpenAI and local reachability to the MCP URL; it does not require inbound Internet access. This repository does not modify the Tunnel profile or services. Use the existing Tunnel only after its health/readiness checks pass. See [OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

## Start conditions

The existing stdio transport remains the default. For HTTP, start the adapter with `--transport http --allow-codex-pipeline --allow-single-user-local-http`; the daemon must also have its existing Codex pipeline enabled. The adapter fails closed unless all of these are true:

1. `ACEIS_MCP_EVENTS_ENABLED=1` is set for the adapter and daemon.
2. Both processes can read the same pre-provisioned 32-byte systemd credentials: `mcp-events-aes-key` and `mcp-events-owner-hmac-key`.
3. The bounded resolver, HTTPS sender, callback verifier, private runtime, and shared pipeline ledger pass startup checks.
4. The HTTP process uses the local OS UID principal. Client `_meta`, email, session IDs, and bearer headers are ignored as identity.

The credentials encrypt callback URLs/secrets and derive owner hashes. Code never creates or rotates them. A missing credential disables Events; never replace a key while subscriptions exist. Preserve `mcp-events.sqlite3` and the Codex task ledger during rollback.

Only tasks and threads bound when created/submitted through this local HTTP adapter are exposed to its Events and owner-scoped pipeline tools. Old tasks with no owner binding remain unavailable for notifications. An uncertain task submission is never retried as part of delivery recovery.

## Verification and rollback

The offline tests use a fake callback verifier, fake sender, fake resolver, and disposable keys. They verify signed payload construction, persistence, retry/restart recovery, duplicate suppression, task binding, and the loopback HTTP route. They do not prove the live Tunnel, OpenAI callback receipt, ChatGPT conversation resumption, or the three-minute target.

For an approved synthetic live test, record the task terminal time, callback response time, and the first observed ChatGPT follow-up separately. A successful `2xx` proves callback receipt only; it does not prove that ChatGPT resumed this conversation. If delivery outcome is unknown, inspect the existing subscription/outbox before retrying.

Rollback by stopping Tunnel forwarding to the HTTP adapter, restoring the prior Tunnel stdio profile, and unsetting `ACEIS_MCP_EVENTS_ENABLED` for adapter and daemon. Keep both credential files, the outbox, and the task ledger. Verify the old stdio status path. Do not replay Codex tasks to test notification recovery.
