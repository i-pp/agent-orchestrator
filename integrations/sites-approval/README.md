# Offline Sites approval provider

This candidate connects a separately authenticated browser decision to LAF through a fixed-origin, machine-authenticated HTTPS API. It remains local and unconfigured. The existing synthetic Site is unchanged and cannot issue approval receipts.

Run the offline checks with Python 3 and Node 24 or newer:

```sh
PYTHONPATH=tests python3 -B -m unittest test_sites_approval_provider -q
node --test integrations/sites-approval/service.test.mjs
```

The Python tests use a socketless Node child and the actual Worker core/D1 SQL against in-memory SQLite. The only identities and credentials are synthetic constants. No listener, real identity provider, network call, Site deployment or model turn is involved. The fixture proves the binding and transport contracts, not hosted identity-header stripping or production network reachability.

## Separate human and machine channels

- The Site's browser adapter requires a trusted Sites Dispatch deployment. It pins the SHA-256 of the approved stable per-Site subject. Workspace admin visibility does not grant app-level approval authority.
- A browser must retrieve the exact request, receive an HttpOnly/Secure/SameSite cookie and a distinct CSRF token, and explicitly POST one offered decision. The request is immutable and expires. The application does not claim that session creation proves a fresh upstream login or step-up verification.
- The machine API needs a separate application key. It can stage/read a request and consume an already-recorded decision. It cannot create a human decision. The human endpoint rejects machine Authorization headers; the machine endpoint rejects browser identity.
- LAF sends credentials only to the configured exact HTTPS origin, verifies TLS, follows no redirects, uses no environment proxy, bounds replies, and does not automatically retry IO. It constructs the public URL from the local challenge, not a server-provided URL.
- A decision is consumed by one conditional SQL update. Concurrent clicks and consume calls have one winner. A local durable consume fence remains blocked after lost responses or restart. An uncertain consume is reconciled manually, never replayed. Expired unused decisions require a new challenge and explicit click; consumed decisions cannot be renewed.
- Status replies contain no proof. Model-provided receipts, identity flags and arbitrary callback URLs are not accepted. The existing pipeline rechecks its live request/turn/owner state after the provider gate. Native response send, typed resolution and confirmed task resumption remain separate states.

No human decision is exposed as MCP or WebMCP. Native permission/profile requests and incomplete file acceptance remain native-only. This provider does not replace required platform confirmations, security-setting approvals, credential enrollment or other actions requiring the person's original human interface.

## Exact configuration and approvals still needed

Before a synthetic hosted end-to-end test, approve the exact Site/source update and these settings. Do not add them to the existing pilot merely to make a test pass:

| Setting | Location | Purpose |
| --- | --- | --- |
| `APPROVAL_ORIGIN` | Site runtime | Exact approved HTTPS Site origin. |
| `APPROVAL_OWNER_BINDING` | Site runtime and local host config | Explicit opaque pairing to the local LAF owner. Never first-visitor registration. |
| `APPROVAL_OWNER_SUBJECT_SHA256` | Site runtime | Pin the intended owner's stable per-Site subject fingerprint through an approved human pairing step. Do not authorize by email. |
| `APPROVAL_MACHINE_KEY_SHA256` | Site runtime | Hash of a newly approved narrow machine API key. The raw key stays in approved local secret storage. |
| `APPROVAL_TRUSTED_SITES_DISPATCH=confirmed` | Site runtime | Explicit host deployment condition, not an HTTP field. Missing opt-in blocks the whole service. |
| `APPROVAL_DB` | Site D1 binding | New durable schema in `schema.sql`, applied by an approved migration rather than a request handler. |
| Site SIWC service token | Approved local secret storage | Pass the platform's private-Site gate; it supplies no human identity. Creating/rotating one is separate authorization and can affect existing callers. |
| Local `SitesApprovalConfig` | Local host-only dependency injection | Exact origin, owner binding, application key and Site service token. No MCP configuration fields are added. |

The optional local provider is installed as `pipeline.approval_portal` and its `consume` method as `pipeline.human_decision_gate`. There is no CLI flag, automatic environment discovery, production wiring or service restart in this candidate. Importing the example Worker entry without approved settings returns 503.

The first hosted test must still use a synthetic native peer and synthetic request, not a live task. Confirm the actual browser subject and host's identity-header stripping, then verify anonymous/wrong-owner/machine-only rejection and one authenticated click. Only after that should a separately approved runtime test attach this provider to a new disposable real Codex task. Do not reopen canceled work or change permissions to manufacture an approval request.

## Data, persistence and costs

Unlike the storage-free connectivity pilot, this design needs D1 to retain request bindings, bounded action previews, decision/consume timestamps, subject/session/CSRF hashes and audit history. That data leaves the PC for the approved Sites/Cloudflare hosting service. Raw subject IDs, email, cookies, CSRF tokens, API keys and consume receipts are not stored in the application database. Do not log them. Preview sanitization is conservative and cannot prove that arbitrary action content contains no private data; approve the exact data-sharing scope before sending real previews.

Workspace administrators may still have platform-level visibility or management authority. Server pinning separates approval authority from viewing access; it is not isolation from a workspace administrator who controls the hosting environment. Choose a separately verified personal Site or self-hosted provider if that trust boundary is unsuitable.

D1 also changes the future hosting cost: the documented proposed January 2027 storage base is 25 credits per Site-month before traffic and stored data, subject to plan allowance and contract. No storage binding, secret, billing commitment or production setting has been created here. See [host options](../../docs/approval-host-options.md) for the verified product limits and pricing source.

## Rollout and recovery limits

A real browser screenshot confirmed that the existing storage-free pilot can display authenticated connection success. It did not test this provider, stable owner pinning, credentialed machine access or native execution. Automated direct requests to that pilot met Cloudflare error 1010; that access restriction was not bypassed and is not a passing authentication test.

The production D1 API and Sites header injection remain unverified. Offline SQLite is useful contract evidence, not proof that a Cloudflare deployment behaves identically. Owner/key changes must account for deployment propagation and any platform token-rotation grace period. Rollback must retain the new ledger reader and consume-fence state; never discard approval history or reset a consumed/unknown interaction to pending.

## Current offline evidence

The current focused Python selection contains 136 tests, including 21 new cross-language/provider checks. The Worker core has 21 separate Node tests against the real SQL statements. The expiry regression covers slow request bodies, queued database mutations and a consume response delayed until after expiry. SQL checks database `unixepoch()` at the actual transition; post-commit expiry suppresses receipt return while retaining the consumed fence. The Python gate independently refuses an expired response without reopening that fence.

The new CI definition runs both commands with Node 24 and read-only repository permissions. It has not run remotely for this unpushed candidate. Passing these checks does not establish a production authenticator, a successful hosted D1 migration or a live approval flow.
