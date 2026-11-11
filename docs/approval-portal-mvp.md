# Authenticated remote approval MVP

Remote human approval is possible on Linux. Ordinary Codex command approvals use a correlated `accept` or `decline` response on the existing App Server connection. The separate native biometric verification API is one possible human-authentication mechanism, not a prerequisite for that response protocol.

This isolated candidate adds an authenticated approval-page core and an MCP interaction entry point. It proves the complete request-to-decision-to-same-task flow with fixture authentication and a synthetic native peer. It does not provide a production login provider, run a web listener, change existing services, or make the connected Chat app expose these tools.

## Workflow

1. `codex_approval_get` returns the exact pending approval and its immutable request hash.
2. New `codex_approval_begin(task_id, approval_id, request_sha256, request_key)` checks the existing task-owner ACL and native task/turn binding, then opens one public interaction identifier. Opening the interaction never approves or executes the action.
3. An approved web host can call `ApprovalPortal.handle` for the page. Its separately configured authenticator must return a verified browser session. A separate owner mapping must authorize that stable subject for the exact local task. Both dependencies are absent by default, which refuses every interaction.
4. The human reviews escaped request details and explicitly submits approve-once or decline-once. The server requires an exact HTTPS origin, authenticated session, session-bound CSRF value, unchanged request fingerprint and expiry. The URL is not a bearer credential; session and CSRF secrets are not returned through MCP or stored in plaintext.
5. A trusted host can install `portal.consume` as the existing pipeline's human-decision gate. The exact receipt is consumed durably once before the original request is answered. The pipeline checks task, turn and request state again. A write remains `SENT`, not confirmed resumption. Native resolution and matching history are still required before reporting `RUNNING`.

Canceled or completed tasks are not resurrected. Permission-profile requests remain native-only. File acceptance without a complete patch remains native-only, although a human may decline the file operation. Missing or withheld command details cannot be accepted. No session approval, persistent policy amendment, `grantRoot` or general permission grant is introduced. An approval-page receipt never substitutes for a required GitHub or Codex platform confirmation.

## MCP can be extended

The new `codex_approval_begin` is wired through the existing pipeline and HTTP owner ACL in this isolated checkout. An end-to-end fixture uses the actual MCP HTTP handler to begin an interaction, the approval-page HTTP boundary for the human decision, and MCP again to respond once to the original native request.

`mcp_interaction` also builds a standard `elicitation/create` URL-mode server request only for a peer declaring `elicitation.url`. Other peers receive a normal link descriptor. An `accept` response to URL elicitation means consent to open the interaction, not approval of the pending command. Only the authenticated decision recorded by the approval service supplies a receipt.

The updated adapter also implements the standard synchronous `URLElicitationRequiredError` (`-32042`, with `data.elicitations`). This does not require an SSE stream. Modern per-request capabilities and legacy HTTP session capabilities are kept separate; capability negotiation selects presentation only and never supplies human authority. For a URL-capable client, pending `codex_approval_begin` returns this standard error. Retrying the same idempotency key before a human decision returns the same interaction. After the authenticated decision, that same begin call returns `DECIDED` and the recorded choice; the separate decision call must still consume the exact server receipt.

The MCP HTTP fixture tests exercise this error-and-retry flow and two legacy sessions with different capabilities, including non-interference between them. Direct server-initiated SSE elicitation remains outside this candidate, and the real Chat client's interpretation of the standard URL-required error is not yet verified. A client without URL mode can still present the normal browser link.

## Authentication choices and deployment gates

A production deployment can use a real OIDC identity provider plus an explicit per-request confirmation, with stronger reauthentication where the action requires it. Browser WebAuthn/passkeys are another option and do not depend on the Linux server implementing Codex's native biometric API. Neither option is implemented by returning a model-supplied `human_approved` flag.

An optional managed Sites host is another possible provider. Its platform contract supplies a stable per-Site server-side user ID and a separate identity-less service-access path. It is not implemented or authenticated by this candidate. Workspace-private Sites can also be visited by workspace admins; a sole explicit viewer is not a guarantee that no admin can view the content. Personal-account private Sites are documented, but availability in the intended personal account has not been checked. Sites hosting has a future usage-based pricing model, so this must remain an optional provider rather than a required LAF dependency. See [approval host options](approval-host-options.md) for the verified boundaries, minimal configuration and synthetic-first check plan.

Before a live check, choose and approve the reachable HTTPS origin, actual authenticator and stable-account-to-local-owner mapping. Login sessions need the provider's normal issuer/audience/nonce/state/PKCE checks, secure cookie handling and expiry. Identity-only login is not action approval; the explicit one-shot POST remains separate. Do not reuse another plugin's OAuth grant or change an existing application's scopes silently.

The original task deadline continues to apply while a human reviews the action. The host must reconcile a timed-out or canceled task rather than re-create its approval. A live check requires separate authorization for any new credentials, account mapping, listener/tunnel configuration or service rollout. No such change is part of this candidate.

There is no production authentication-provider implementation in this PR. The real Chat URL interaction, a real authenticated browser decision, hosted access enforcement, and a live Codex request resuming the same task remain unverified. The connected runtime remains unchanged and must not be described as remotely approvable on the basis of these fixture tests.

## Offline verification

```sh
PYTHONPATH=tests python3 -B -m unittest \
  test_codex_approval_bridge test_approval_portal test_codex_schema_compatibility \
  test_pipeline_foundation_isolated test_codex_pipeline_adapter_extract \
  test_mcp_http_transport -q
```

The dedicated command passes 115 tests, including 33 new portal and interaction tests. These cover unauthenticated and wrong-owner access, caller authentication claims, CSRF, origin, session and hash mismatches, expiry, duplicate decisions and receipt consumption, unused-decision expiry and reauthentication, exact native decision choices, portal restart, canceled tasks, native-only permissions, withheld file acceptance, HTML escaping, MCP capability negotiation, the standard URL-required error/retry flow, session capability isolation, and same-task end-to-end fixtures. The broader existing offline selections plus the new tests pass 310 tests. These are fixture results, not a real-login or live-Chat approval result.

## Sources

- [App Server approvals](https://learn.chatgpt.com/docs/app-server#approvals).
- [MCP elicitation and URL-required errors](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/200d5339bbc7649ee92d6445dc17368e2b5b11ba/docs/specification/2025-11-25/client/elicitation.mdx#url-elicitation-required-error).
- [Server authorization and elicitation](https://developers.openai.com/plugins/build/mcp-server#tool-annotations-and-elicitation).
- [Separate native verification provider](https://github.com/openai/codex/blob/5228d94a27b7f84cae4854acbb5ea3b3e90dfeaf/codex-rs/user-verification/src/lib.rs). This public source identity is not asserted to be the exact installed binary source.
