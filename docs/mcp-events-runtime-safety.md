# MCP Events Runtime Safety

## Default and opt-in behavior

The existing stdio adapter remains the default. `build_systemd_notification_runtime()` does not import the OS provider module, read credentials, create `mcp-events.sqlite3`, start a worker, or make network requests unless all of these are true:

1. The adapter is started with `--transport http --allow-codex-pipeline` (the daemon is allowed to start its background worker).
2. `ACEIS_MCP_EVENTS_ENABLED=1`.
3. One or more exact OIDC issuers, their pinned RS256 JWKS, the MCP audience, and required scopes are configured for both the HTTP adapter and daemon worker.
4. Both systemd encryption/HMAC credentials are present and every storage, resolver, sender, authorization, and ledger check passes.

Missing or malformed configuration fails closed. The Events flag, issuer/JWKS configuration and the same credential pair must be available to both daemon and HTTP adapter. The Tunnel runtime API key remains separate from these at-rest keys.

## Caller and task authorization

HTTP mode requires a Bearer access token verified against one configured OIDC issuer's pinned RS256 JWKS and checks exact issuer, MCP audience, expiry, and required scope. The verified `(issuer, subject)` pair is resolved through a private HMAC lookup to a random, provider-independent AceIS principal ID. Each request is authenticated; legacy MCP sessions are pinned to the same external identity using an opaque digest of that pair. The resource metadata endpoint is public so the OAuth client can discover the configured authorization server(s). The adapter never issues tokens.

`openai/subject`, `openai/session`, email, and the local process UID are not HTTP authorization evidence. Only the verified OIDC access token supplies the external identity. Principal mapping, task ownership, notification subscription ownership, and delivery destination are separate records: the outbox stores an HMAC of the internal principal for task/subscriber ACLs, while each subscription stores its callback URL encrypted and separately indexed by a keyed digest. A callback URL never identifies a user. Threads are bound when created and tasks when stored/submitted. Task get/cancel/queue, thread steer/archive, list results, event replay, and notification subscriptions are filtered by internal principal ID. Global pipeline ticks are unavailable to HTTP callers. Unbound preexisting threads/tasks are not accessible through the authenticated HTTP pipeline. Local stdio keeps its existing local no-OAuth behavior.

Subscription requires a persisted task owner binding and a current source-ledger task. Delivery rechecks that the persisted internal owner still matches the task and that the task still exists. If the source ledger is unreadable or malformed, authorization fails closed; unknown task state never becomes permission. Existing outbox rows bound to the old local UID are preserved but cannot be delivered under an OIDC-mapped owner without a deliberate migration.

The resource server does not create an OAuth authorization server or map ChatGPT workspace email addresses to OIDC subjects. It accepts standards-shaped RS256 JWT access tokens from any configured issuer, including a multi-issuer set with one MCP audience and scope contract. Each issuer must also meet ChatGPT's MCP OAuth authorization-server requirements and issue tokens with this resource's audience; the verifier does not treat an ID token or a token for another API as an MCP access token. Entra ID is the known company identity system, but its live tenant/app registration is not configured here. A provider-specific bridge may be needed if a provider cannot issue compatible resource tokens. Discovery metadata and live authorization remain deployment prerequisites.

Keep Events disabled until the HTTP endpoint is connected through a compatible OAuth authorization flow and the Tunnel/app access scope is understood. The authenticated OIDC subject distinguishes callers; ChatGPT session identity and same-conversation resumption are not inferred from that subject.

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

The isolated suite covers OAuth JWT signature/claim checks, multiple configured OIDC issuers, opaque internal-principal mapping without storing raw subjects, forged `_meta` rejection as an identity source, session-principal pinning, per-principal thread/task ACLs, unbound legacy resources, provider-off defaults, persistence/retry behavior, payload minimization, callback validation, DNS pinning, and fake HTTP/Events dispatch. It does not prove the production IdP, authorization-server compatibility, secret storage, Tunnel scope, a real callback, or ChatGPT conversation resumption.
