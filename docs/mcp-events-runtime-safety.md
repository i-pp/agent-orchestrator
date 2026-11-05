# MCP Events runtime safety

## Trust boundary

This implementation deliberately assumes one local OS user. HTTP is opt-in, binds to `127.0.0.1`, rejects non-loopback clients and non-local Host/Origin values, and derives its sole principal from the server process UID. It does not authenticate a ChatGPT workspace user. `_meta` identity fields, emails, session IDs, and bearer headers cannot change the principal.

Use only with the private, unpublished Dev app installed for that user and the existing Secure MCP Tunnel. Do not publish to the workspace or give another user access to the tunnel/app. If the app or Tunnel becomes shared, this mode is no longer appropriate; use per-user authentication before sharing. Secure MCP Tunnel keeps the MCP server private and forwards OpenAI requests through its client; it does not add a remote-user identity to this service. [OpenAI Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

stdio remains the default and unchanged. HTTP startup requires `--transport http --allow-codex-pipeline --allow-single-user-local-http` and the explicit Events flag. Other legacy/helper tools are not exposed over HTTP. Client-supplied identity metadata is never treated as authorization evidence.

## Task and subscription ownership

The adapter binds newly created threads and submitted/stored tasks to the local UID in a keyed owner table. Task get/cancel/queue, thread steer/archive, task listing, event history, and subscriptions require that binding. Unbound pre-existing tasks are not retroactively assigned to the local user. Subscriptions store a separately keyed owner digest and encrypted callback URL and signing secret. Delivery checks both the current task ledger and stored owner binding; failed checks stop delivery.

The durable pipeline ledger is read-only to the notification outbox. The outbox tracks event cursors, destination state, retry leases, and event IDs. An unknown callback result is reconciled from the existing outbox and retried with the same event ID; Codex tasks are never re-executed to recover notification delivery.

## Credentials and callback boundary

`CREDENTIALS_DIRECTORY` must contain the same two pre-provisioned 32-byte credentials for adapter and daemon:

- `mcp-events-aes-key` encrypts callback URLs and webhook secrets with AES-256-GCM.
- `mcp-events-owner-hmac-key` derives task/subscription owner hashes and keyed callback URL indexes.

The provider only reads credentials; it does not generate them. Both keys are required. A lost or changed key can make existing callback data unreadable, so preserve the keys and `mcp-events.sqlite3` together. Key rotation needs a separate migration plan.

Callbacks require HTTPS. The verifier and sender use the same bounded, interruptible DNS resolver, reject local/private/mixed DNS answers, pin the selected IP, validate TLS against the hostname, refuse redirects, and apply total request/response deadlines. Payloads include event ID, name, timestamp, cursor, task ID, state, and approved thread ID; blocked events add a fixed reason code. Prompts, results, logs, paths, callback URLs, and secrets are excluded.

## Evidence limits

Offline tests use disposable providers and fake networking. They do not prove live tunnel forwarding, real callback receipt, same-conversation wake/resume, or the three-minute target. The official Secure MCP Tunnel documentation describes transport forwarding, while ChatGPT's custom-MCP documentation requires the app connection to be separately created/tested and published only through the workspace app workflow. This project leaves the app private and unpublished. A callback `2xx` is receipt evidence only; conversation resumption requires a separate observed ChatGPT response.
