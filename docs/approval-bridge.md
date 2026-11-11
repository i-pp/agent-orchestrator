# Codex approval bridge candidate

This candidate preserves an approval request's identity and returns bounded details through LAF MCP. It does **not** activate remote approval in the deployed daemon. The production host supplies no trusted human decision gate, so `codex_approval_decide` returns `HUMAN_APPROVAL_CHANNEL_UNAVAILABLE` before responding to Codex.

The [authenticated portal MVP](approval-portal-mvp.md) adds an inert page core and standard MCP URL interaction on top of this bridge. Production authentication and runtime activation remain separate work. [Host options](approval-host-options.md) records the managed-host privacy, cost and deployment boundaries.

## Read and decide contracts

- `codex_approval_get(task_id)` returns approval UUID, immutable request SHA-256, action, target fields supplied by Codex, conservative risk, source logical task/thread, native turn and item identity, and lifecycle state. Missing details remain missing; polling a `waitingOnApproval` flag does not reconstruct the original request.
- Projections are bounded to 32 records per task. Obvious credential/control content and oversized strings are withheld. The preview is untrusted and is not a guarantee of secret detection, complete target discovery, or action safety. It is not adequate evidence for a high-impact human authorization by itself.
- `codex_approval_decide(task_id, approval_id, request_sha256, decision, request_key)` accepts only one-shot `accept` or `decline`. Both require an independently configured trusted human decision gate. MCP arguments cannot enable the gate or assert `human_approved`.
- The HTTP adapter checks the existing authenticated task-owner binding for both operations. The legacy single-user stdio boundary is unchanged. OS-user identity, a request ID/hash, a prompt, a mailbox answer, or model text does not prove a human decision.

The gate is an internal dependency-injection seam, not a new authentication implementation. Its provider must verify an actual human action bound to the exact request, decision, target, and required platform gate, then return an opaque audit receipt. The default host still has no gate. An optional [Sites provider](../integrations/sites-approval/README.md) is now implemented separately and requires explicitly approved identity pairing, credentials and hosting. No production LAF service wiring, signing key, credential value, permission expansion or service change is introduced by this source change.

## Native protocol and human-only boundaries

The supported App Server flow is a server-initiated JSON-RPC request followed by a correlated response on the same live connection. The candidate implements that narrow response path for complete, unwithheld command requests and gated rejection of file changes. Command kind, environment and callback identity are shown when supplied. It does not start another turn, recreate a thread, or submit the task again.

Permission-profile requests, connector approval elicitation (`item/tool/requestUserInput` or `mcpServer/elicitation/request`), and GitHub permission changes require the native human approval UI. Their details can be shown when the request carries an exact turn binding, but this candidate never answers those request types. File-change acceptance remains native-only because the approval request does not contain the complete patch. Missing or withheld command details, `writeStdin`/`humanCommand`, network contexts and additional permission profiles also require the native human UI for acceptance. No `acceptForSession`, exec-policy/network-policy amendment, general permission grant, or file `grantRoot` acceptance is exposed. A caller cannot use the bridge to bypass a platform approval, denial, or safety boundary.

Arbitrary commands cannot be reliably classified by keywords. Every command acceptance therefore requires the independent human gate, including commands that change GitHub permissions without using a dedicated permissions request. The gate must not infer authorization from a benign-looking preview.

## State and audit

The existing ledger gains an optional `approvals` dictionary. Request projections, hashes, source bindings, operation idempotency, decision and hashed human receipt remain with the original logical task. Trace context and raw request parameters are not persisted.

A successful response write produces `SENT`, not approval success or task completion. The logical task remains `WAITING_APPROVAL`. Only a matching typed `serverRequest/resolved` followed by matching native history with an in-progress turn and no approval-wait flag confirms `RUNNING`. Terminal turns invalidate unanswered requests. Multiple pending approvals must all resolve.

Unknown response writes become `RECONCILIATION_REQUIRED` with an explicit audit event. The human receipt is followed by a second atomic check of request content, task, turn, ownership and pending status, so a terminal or changed-request notification during human interaction prevents the send. The protocol also invalidates pending requests on matching terminal turn/thread events. It consumes each native request before IO. Duplicate requests cannot reopen it. An uncertain send is never replayed, including under a new request key. After process restart, pending or sending records become `RECONCILIATION_REQUIRED`; historical RPC IDs cannot be sent on a new connection. History is retained rather than fabricated or silently retried.

Older code rejects a ledger containing this new field. Rollback must retain the updated ledger reader, or use a separately reviewed forward-compatible reader. Never restore an old task database or discard approval history to make old code start.

## Verification and remaining work

Run the self-contained focused checks:

```bash
PYTHONPATH=tests python3 -B -m unittest \
  test_codex_approval_bridge test_approval_portal test_codex_schema_compatibility \
  test_pipeline_foundation_isolated \
  test_codex_pipeline_adapter_extract test_mcp_http_transport -q
```

The current dedicated CI command passes **115 tests**, including 33 portal/interaction tests added to the original 82 bridge/schema/foundation tests. The broader offline selections for MCP Events, HTTP, notifications, desktop fallback and common policy, together with the new tests, pass **310 tests**. Python compilation and `git diff --check` also pass. The workflow runs these checks without live services, authentication changes, models, or enrollment.

This candidate is now based on the complete current repository, not a mixed historical snapshot. All 210 tracked blobs and the root tree `26a5c5347335c2ad212d9760029642cd485d0d07` were verified against private GitHub commit `4c095bb00a0ad9e02cb2a373a013e4b25fc9a7e1`. The exact signed Git commit object was also reconstructed through the authorized GitHub API and hash-verified. The local checkout is API-materialized; no direct network clone or full history is claimed.

Whole-repository discovery was attempted on that base plus the initial bridge revision: **724 tests, 113 failures and 52 errors**. Examples include the deliberately ignored operator Tunnel profile being absent and namespace/isolation prerequisites being blocked. These failures are not hidden or changed into skips. The complete suite is not green, and all failures have not been proved pre-existing. The focused final revision and current offline CI selections above pass independently.

### Installed Codex 0.167.0 compatibility

Seventeen representative stable request, response and resolution examples validate against the exact schemas generated by the installed 0.167.0 binary. The extracted contract records 14 schema identities and hashes in `tests/fixtures/codex-0.167-contract.json`. The adapter now admits this exact version for its inspected stable subset, without enabling experimental APIs in the production connection.

Read-only real stdio probes passed initialization, `account/read` with `refreshToken=false`, and `model/list`. No thread or model turn was created. This establishes those reads and schema compatibility, not live approval or inference.

### Production human decision channel remains unconfigured

The installed binary also generates experimental native user-verification schemas. A separate bounded readiness probe used `userVerification/status`, which reports local readiness without prompting or backend registration. On EVO-X3 it returned **`providerUnavailable`**, with no credential. No enroll, verify, delete, registration or authenticator change was attempted.

That result concerns the separate native verification provider. It does not make ordinary command/file approval responses unavailable on Linux. The portal MVP exercises those responses with fixture authentication; a real authenticated human channel is still needed before deployment.

The official native verification capability is restricted to supported local UI/device/provider combinations; merely using a recognized client name or enabling `experimentalApi` does not make a compatible human approval channel. Tool annotations and ordinary model-provided arguments are not server authorization evidence.

A protocol-supported candidate is URL-mode MCP elicitation to a separately authenticated human approval page. The current client's advertised URL-elicitation support has not been verified. This route also requires selecting and provisioning an actual user-authentication mechanism and approved origin, with user/decision/request binding and expiry. This candidate neither invents such a service nor silently creates credentials, exposes a new listener, or expands GitHub access. The default decision gate therefore remains blocked. Critical GitHub changes must still receive their actual required human/platform approval.

Before activation: independently review this updated patch, authorize and provision the chosen human decision channel, reconcile held/unknown tasks, obtain explicit rollout approval, and test one bounded same-turn request end to end. Existing pending requests whose payload was never recorded cannot be recovered by installing this candidate.

## Sources

- [OpenAI App Server approvals](https://learn.chatgpt.com/docs/app-server#approvals): supported decisions, request/response lifecycle, permission requests, and connector elicitation.
- [Native user verification](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md#user-verification-experimental): platform/provider restrictions and human verification lifecycle.
- [MCP URL elicitation](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/client/elicitation.mdx): authenticated out-of-band human interactions.
- [Server authorization guidance](https://developers.openai.com/plugins/build/mcp-server#tool-annotations-and-elicitation): annotations do not replace server authorization or confirmation.
- [Private source baseline](https://github.com/i-pp/agent-orchestrator/tree/4c095bb00a0ad9e02cb2a373a013e4b25fc9a7e1).
