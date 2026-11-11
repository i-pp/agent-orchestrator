# Choosing a host for remote approval

The reviewed approval candidate can enforce a separately authenticated, one-shot human decision. It still needs a real web host, identity provider and local owner mapping. No provider, hosted Site, credential or runtime service is provisioned by this PR.

For an initial managed-host check, use only synthetic requests in a new dedicated private Site. Do not send live task details or secrets, change the existing runtime, or reuse an existing public Site. A hosted check is a production deployment even when its data is synthetic.

## Managed Sites: supported contract and limits

Sites supports private Sites in workspace and personal accounts. Workspace-private access includes the owner and workspace admins. The private-deployment tool requires a sole explicitly allowed viewer and no groups, but that check does not eliminate implicit admin access. A strict requirement that no other workspace admin can view approval details needs a separately verified personal account or a user-controlled host.

The current Sites integration contract supplies `oai-authenticated-user-id`, which is stable within one Site and different across Sites. Server code must authorize the exact pinned ID; email and display name are not authorization keys. The public documentation separately confirms the hosted sign-in flow and server-side authorization requirement. The actual identity headers and access enforcement of a new approval Site have not yet been tested.

Browser sign-in uses the platform's `/signin-with-chatgpt`, `/signout-with-chatgpt` and `/callback` integration. Do not create a second OAuth flow or reuse another plugin's credential. A successful login identifies the visitor, but never approves a pending action by itself. Preserve the exact-request, explicit POST and CSRF checks.

For machine calls, Sites supports a Site-specific SIWC bypass token in `OAI-Sites-Authorization`. Dispatch consumes that token and supplies no signed-in visitor identity. It is not human approval evidence. The human decision endpoint must reject an identity-less request even if service access is valid. If additional machine authorization is used, Sites preserves the ordinary `Authorization` header for that purpose.

The Site's request-staging and decision-reading API must be separate from its human decision POST. Do not expose the human decision as a Site MCP or browser-agent tool. Site MCP calls can carry authenticated user identity too, so an identity header alone is not evidence of a browser click.

Sites server code runs in Cloudflare Workers. Durable requests and decisions would be stored in D1. Approval details therefore leave the PC and are processed by the managed hosting service. Do not describe this option as local-only or as confidential from workspace administrators. Do not connect other apps, upload raw prompts/results, or log credentials, cookies, CSRF values or decision proofs.

## Minimum configuration after authorization

The following is a proposed integration contract, not existing executable provider settings:

1. A new dedicated Site in the explicitly selected account, its returned exact HTTPS origin, and verified private audience. Do not change another Site's audience to obtain a deployable target.
2. An approved owner pairing: the stable per-Site subject mapped to the local LAF owner. Never let the first arbitrary visitor claim ownership or let MCP arguments set this value.
3. A server-backed Worker and durable D1 schema for request state, explicit decisions, expiry and audit. This requires a port or a separate provider service; the Python MVP cannot be deployed unchanged as a Worker. No connected-app declarations, Site MCP capability, R2 storage, custom domain or inbound PC tunnel is needed for the minimum design.
4. A narrowly used machine-access path. Any supplied or explicitly requested SIWC token is secret, sent only to the exact Site, and stored in approved local secret storage. Add an application credential if endpoint-level machine scopes are required; store its hosted value as a secret through Sites environment tools. Creating or rotating a SIWC token is not a read-only probe and can invalidate existing callers.
5. A local optional provider that makes outbound HTTPS calls to the pinned origin, verifies the exact request hash, owner, task/turn, decision and expiry, and durably consumes the decision once. It must not trust an arbitrary model-supplied URL, receipt or identity. When this provider retrieves the decision directly from the trusted HTTPS service, a separate signing key is not inherently required. A receipt relayed through untrusted callers would instead require a reviewed cryptographic proof and pinned verification key.
6. Separate authorization for source publication, private deployment, owner/credential configuration and any runtime activation. No service restart, live configuration update or real native request is implied by a test Site.

The existing Secure MCP Tunnel is not a browser approval origin. Sites private connections have separate product eligibility and configuration; do not assume an existing MCP tunnel can be reused. The proposed outbound HTTPS design avoids that dependency.

## Synthetic-first verification

Before creating a Site, finish local provider/Worker tests with synthetic owner IDs, commands and native responses. These tests cannot establish hosted sign-in behavior.

After the target account and dedicated private deployment are authorized:

1. Deploy a page with synthetic content only. Check anonymous rejection and the real owner's normal browser sign-in, without recording raw identity headers, cookies or tokens. Pin the intended owner through the approved pairing process.
2. Verify that another identity, missing identity, forged identity headers, and machine bypass access cannot invoke the human decision POST. Verify CSRF, exact origin, allowed native choices, expiry and immutable request binding. A workspace admin's implicit visibility must be assessed separately from whether app-level approval is denied.
3. Only after credential configuration is approved, test staging and reading one synthetic request through the machine API. Verify that those credentials cannot manufacture a human decision. Do not add a model-callable approve endpoint for convenience.
4. Use the isolated fixture MCP to check URL interaction or normal-link presentation, explicit approve/decline, one-shot receipt consumption and synthetic same-task resolution. Real Chat presentation remains a separate result from provider authentication.
5. Seek separate approval before enabling the provider in a live LAF runtime or sending an actual task preview. Keep important native/platform confirmation steps intact and do not reopen old canceled tasks.

No hosted step above has run. The existing candidate's 115 focused tests and 310 broader offline tests use fixture authentication and synthetic peers, not Sites or a production identity provider.

## Privacy and cost choice

Current official pricing states that hosting is free through December 31, 2026. From January 1, 2027, Plus, Pro and Business usage above the plan's hosting allowance is billable; Enterprise follows its agreement. A Site with D1 has a monthly base charge even when the database is empty. Actual account billing eligibility, spend controls and accepted terms have not been inspected. Do not promise a permanently free service or enter a new paid commitment for this pilot.

Preserve a self-hosted HTTPS/OIDC or WebAuthn provider as an alternative. It needs a reachable origin, TLS, authentication enrollment and owner mapping, but avoids making LAF depend on a particular managed hosting product. The inspected local environment did not establish an existing ready-to-use browser approval origin or identity provider; it did not rule out an existing cloud identity registration.

## Sources

- [Sites audience and personal-account sharing](https://learn.chatgpt.com/docs/sites/publishing-and-sharing#choose-who-can-visit).
- [Hosted sign-in and machine service access](https://learn.chatgpt.com/docs/sites/data-and-connections#add-sign-in-with-chatgpt).
- [Sites hosting and pricing](https://learn.chatgpt.com/docs/sites#pricing).
- [Authenticated approval candidate and offline verification](approval-portal-mvp.md).

Public documentation and the current integration contract were checked on November 11, 2026. Actual access enforcement, the intended personal account's Sites availability, real Chat interaction and live Codex resumption remain unverified.
