# Time- and Count-Bounded Development Grants

A grant pins requests on first registration inside one approved task UUID instead of adding fixed slots repeatedly. `--allow-mailbox-grants` defaults off and also requires `--allow-chat-mailbox`. The existing eleven-operation public schema does not change. Fixed slots remain in their existing policy/DB; grants use a separate ledger. Reject task-namespace collisions before startup. The [Japanese historical source](mailbox-grants_jp.md) is unchanged.

## Policy boundaries and activation evidence

The historical approval recorded at 2026-10-30 04:34 UTC allowed seven days, 32 total requests, eight pending requests, 4,096 UTF-8 bytes each for request/answer, a 24-hour request deadline, seven-day answer retrieval, and a 30-day audit view. Validation requests consume the same total budget. This historical record is not authorization to activate or renew a grant now.

Actual start/end and policy hash are fixed by the activation record and private runtime `chat-mailbox-grant.json`; restart or resend cannot extend them. Example timestamps are not activation evidence.

| Item | Enforced boundary |
| --- | --- |
| Target | One task UUID, purpose `agent-orchestrator-development` |
| Content | Operationally limited to nonconfidential development consultation; not automatic secret classification or author authentication |
| First registration | Immutable request UUID and request SHA-256; reject hash/body conflict |
| Grant window | Register/save answers only at or after `not_before` and before `expires_at`; maximum seven days |
| Request deadline | Earlier of first registration plus 24 hours and grant expiry; even identical write retries are refused afterward |
| Budget | 32 total, eight unexpired unanswered; answers free pending capacity only; expiry/revocation/reconnection never reduce total consumption |
| Body | At most 4,096 UTF-8 bytes each; reject invalid Unicode, whitespace-only text, and hash mismatch |
| Pending reads | Return request body until its deadline; then only `EXPIRED` metadata |
| Answered reads | Return request/answer for less than seven days after saving, even if the grant has since expired |
| Revocation | Durable `REVOKED`; forbid registration/save and return metadata without bodies, including for unregistered IDs |
| Audit | IDs, hashes, times, fixed events; logical 30-day view; no body in MCP reachability audit |
| Capacity | Private SQLite ledger at most 1 MiB; journals and total disk quota are separate |

A new ID under an expired grant returns `NOT_FOUND` with `grant_state=EXPIRED`, not permission to register. Retrieval expiry is `EXPIRED`; revocation is `REVOKED`. Distinguish saved state, grant state, hash, and body availability. On terminal state, clients must also suppress body text from earlier reads.

## Startup and uncertain writes

The fixed runtime file is owner-only 0600 under a 0700 runtime. Accept exactly `schema_version`, `enabled`, `policy`, and `policy_sha256`; no MCP argument can select an arbitrary path. `GrantPolicy` validates keys, purpose, types, UUIDs, and limits. The existing ledger rejects a changed policy hash for the same grant ID.

Preflight validates all startup flags, saved key references, and policy before stopping anything and does not create a ledger. Reconnection retains saved flags/key references and can read retained answers after expiry. Every write transaction independently checks permission. After `BEGIN IMMEDIATE`, recheck time and pending/total budgets, then check expiry again immediately before commit. Concurrent writes preserve limits, first hash, and answer immutability.

Lost ACK is `UNKNOWN`. Reconcile through getters using the same ID/hash, never an automatic write retry or a new ID.

## Operator CLI

Run from repository root. Set `RUNTIME` to the existing private runtime, and `GRANT_ID`/`POLICY_SHA256` to the activation record, not secret values. `mailbox_ops.py` accepts stdin JSON and suppresses bodies by default.

```bash
python3 -B scripts/mailbox_ops.py prepare --runtime "$RUNTIME" < nonsecret-draft.json
python3 -B scripts/mailbox_ops.py register --runtime "$RUNTIME" < nonsecret-request-bundle.json
python3 -B scripts/mailbox_ops.py inspect --runtime "$RUNTIME" < request-key.json
python3 -B scripts/mailbox_grant_admin.py status --runtime "$RUNTIME" --grant-id "$GRANT_ID" --policy-sha256 "$POLICY_SHA256"
python3 -B scripts/mailbox_grant_admin.py revoke --runtime "$RUNTIME" --grant-id "$GRANT_ID" --policy-sha256 "$POLICY_SHA256" --apply
```

`prepare` builds a candidate, not policy/ledger state or proof of write permission. `status`/`revoke` open an existing ledger only. Revocation requires explicit `--apply`, verifies grant ID/hash, is irreversible, and must be checked afterward. Never blindly retry uncertain revocation. Administration/revocation is not exposed through public MCP.

## Rollback and retention

```bash
python3 -B scripts/poc_services.py grant-rollback-plan --runtime "$RUNTIME"
```

The returned preflight/stop/start/status plan removes only the grant flag, preserving eleven operations, fixed slots, authentication, exposure, and private grant ledger/config. Reinstatement uses the same grant/policy/hash without resetting counts/time. `rollback-plan` instead removes the whole mailbox and returns to seven operations. Plans do not change services. Do not stop while active/unknown work remains.

Seven days is a body-return window and thirty days an audit-metadata view, not physical deletion. There is no VACUUM, backup deletion, or automatic prune; private DB bytes remain after expiry. Existing MCP session-audit limits are separate and do not implement 30-day deletion.

Clients sharing authentication scope can read/write that workstream. IDs/hashes do not authenticate a conversation author or answer quality. Grants do not wake Chat, expose arbitrary files/URLs/shell/tasks, create keys, or launch models. Report local stdio proof, hosted schema/health, direct Dot calls, and real Chat round trips as separate evidence.
