# MCP Events Runtime Safety

## Default and opt-in behavior

The existing stdio adapter remains the default. `build_systemd_notification_runtime()` does not import the OS provider module, read credentials, create `mcp-events.sqlite3`, start a worker, or make network requests unless all of these are true:

1. The adapter is started with `--transport http --allow-codex-pipeline` (the daemon is allowed to start its background worker).
2. `ACEIS_MCP_EVENTS_ENABLED=1`.
3. A pinned RS256 JWKS, exact OAuth issuer, audience, required scopes, and an allowlist of opaque `sub` values are configured for both the HTTP adapter and daemon worker.
4. Both systemd encryption/HMAC credentials are present and every storage, resolver, sender, authorization, and ledger check passes.

Missing or malformed configuration fails closed. The Events flag, OAuth allowlist, JWKS and the same credential pair must be available to both daemon and HTTP adapter. The Tunnel runtime API key remains separate from these at-rest keys.

## Caller and task authorization

HTTP mode requires a Bearer JWT verified against a pinned RS256 JWKS and checks the exact issuer, audience, expiry, scope, and an allowlisted OAuth `sub`. Each request is authenticated; legacy MCP sessions are pinned to the same principal. The resource metadata endpoint is public so the OAuth client can discover the configured authorization server. The adapter never issues tokens.

`openai/subject`, `openai/session`, email, and the local process UID are not authorization evidence. Use a stable `sub` issued by the external identity provider. The configured allowlist may contain separate users; the outbox stores only HMAC owner hashes. Threads are bound when created and tasks when stored/submitted. Task get/cancel/queue, thread steer/archive, list results, event replay, and notification subscriptions are filtered by that owner. Global pipeline ticks are unavailable to HTTP callers. Unbound preexisting threads/tasks are not accessible through the authenticated HTTP pipeline. Local stdio keeps its existing local no-OAuth behavior.

Subscription requires a persisted task owner binding and a current source-ledger task. Delivery rechecks that the owner remains in the configured allowlist and that the task still exists. If the source ledger is unreadable or malformed, authorization fails closed; unknown task state never becomes permission. Existing outbox rows bound to the old local UID are preserved but cannot be delivered under an OAuth owner hash without a deliberate migration.

The resource server does not create an OAuth authorization server or map ChatGPT workspace email addresses to OAuth `sub` values. Every intended caller must authenticate through the chosen external IdP. The IdP, subject mapping, reachable discovery metadata, and live authorization flow remain deployment prerequisites.

If multiple ChatGPT users can reach the tunnel, this local principal cannot distinguish them. Keep Events disabled until the Tunnel/app is demonstrably restricted to one user, or until a verified remote identity and per-caller task ACL are implemented.

## Systemd credential provider

The provider factory only reads pre-provisioned 32-byte credentials from `CREDENTIALS_DIRECTORY`:

- `mcp-events-aes-key`: AES-256-GCM encryption for callback URLs, signing secrets, and one-time challenges in the outbox.
- `mcp-events-owner-hmac-key`: HMAC key for local task-owner bindings and callback URL hashes.

Files must be regular, owner-only, single-link, read-only credentials. The factory never creates keys. Tests inject disposable keys and a fake provider loader. Production credentials should be separately provisioned by an authorized operator through systemd encrypted credentials; no literal values belong in unit files, logs, or this repository.

Both the daemon and HTTP adapter must receive the same two credentials. A missing credential disables notification setup. A mismatch between processes makes existing bindings or ciphertext unusable and blocks delivery; it does not rerun Codex tasks.

The current AES envelope does not support master-key rotation in place. Do not replace either key while subscriptions exist. A future rotation must either add tested versioned multi-key decryption and transactional re-encryption, or explicitly drain/unsubscribe and recreate subscriptions with a documented gap. The outbox must be preserved if a key is missing or mismatched. Losing the encryption key makes stored callback secrets unrecoverable; it does not affect the pipeline task ledger. Signing-secret refresh has its own short dual-signature overlap and does not rotate the at-rest master keys.

## Outbound callback boundary

Only HTTPS callbacks are accepted. The callback verifier and delivery sender share the same bounded, interruptible resolver. Addresses are revalidated immediately before connection, private/local/mixed answers are rejected, the validated IP is pinned, TLS checks the original hostname, redirects are refused, and request plus response deadlines cover the full operation. DNS resolution runs through a killable fixed `getent` executable; an unverifiable executable owner disables the provider. The tests use a fake resolver and fake sender only.

Event payloads contain an event ID, event name, timestamp, cursor, task ID, terminal state, and the approved thread ID. Blocked events add a fixed allowlisted reason code. Prompt, result, exception text, logs, local paths, callback URL, and secrets are never included.

## Recovery

The pipeline ledger remains the source of truth. The separate `mcp-events.sqlite3` outbox stores per-destination state and cursors. The daemon replays source events from durable cursors after restart; expired delivery leases are reconciled and retried with the same event ID. An unknown HTTP result never resubmits a Codex task. MCP Events has no remote receipt query in this implementation; absent a receipt, the same event ID is retried so the receiver can deduplicate it.

If provider loading fails, leave the outbox and task ledger intact, keep delivery off, and restore the exact prior credentials before restarting the adapter and daemon. If the key was rotated or lost, do not delete or recreate the outbox automatically; recover the original key or perform an explicitly reviewed subscription migration. Existing task execution and status lookup continue independently of notification delivery.

## Verification scope

The isolated suite covers OAuth JWT signature/claim checks, forged `_meta` rejection as an identity source, session-principal pinning, per-subject thread/task ACLs, unbound legacy resources, provider-off defaults, persistence/retry behavior, payload minimization, callback validation, DNS pinning, and fake HTTP/Events dispatch. It does not prove the production IdP, subject mapping, secret storage, Tunnel scope, a real callback, or ChatGPT conversation resumption.
