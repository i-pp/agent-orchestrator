# Preparing one real Codex approval trial

The authenticated Sites-to-provider path has passed a real HTTPS trial with a synthetic native peer. The next question is whether one genuine Codex approval can follow that path and resume its original request. No real native thread, model turn or approval has been started for this preparation.

The plan builder only emits data:

```sh
python3 -B src/codex_approval_trial_plan.py \
  --scratch /absolute/private/empty/non-repository-directory \
  --model gpt-6-luna
```

It starts no process, sends no request, reads no credential and contains no execution flag. The scratch directory must already be empty, user-owned, private and outside a repository. Seven offline tests check its scope and lack of side effects. It always reports that explicit native-trial authorization is still needed.

## Narrow proposed action

Use one new, persisted test thread and one short turn in a dedicated scratch directory, with a named operator-approved model. Current operator capabilities report `gpt-6-luna` and `gpt-6.1-sol` available, but availability is not billing proof. Do not change the model provider, account, authentication, global policy, existing workspaces or running service.

The proposed command is exactly:

```sh
/usr/bin/python3 -I -S -c 'print("LAF_APPROVAL_TEST")'
```

The requested policies are `approvalPolicy: untrusted`, `approvalsReviewer: user`, a read-only sandbox and no network access. The installed 0.167.0 stable schema supports those values; read-only network access defaults false. It does not establish that reads outside the working directory are impossible. Do not describe a prompt as a hard single-command security boundary.

Use a dedicated persisted thread for auditable `thread/read`, not an assumed ephemeral-history equivalent. The initial limit is one thread, one turn and 600 seconds, followed only by separately authorized interrupt/archive/owned-process cleanup. One harmless command might be allowed by inherited native rules without a prompt; that outcome is inconclusive for remote approval. Never manufacture a sandbox escalation or broaden permissions to force a prompt.

## Execution venue remains a blocker

The repository's existing normal-host guard currently classifies this tool context as `UNKNOWN`. No app-server executor is launched here. Authorization cannot turn that observation into proof. Do not substitute raw StdioTransport, systemd, a new wrapper, environment flags, changed security settings or disabled sandboxing to route around the guard.

A later approved launcher must re-run `HostGateway.require_host()` in a genuinely confirmed normal-host venue before native process creation or credential access. It must use a non-root owner, a validated pinned native ELF and the existing owned-process lifecycle. The npm entry point is a JavaScript wrapper; its inspected vendor ELF is group-writable and does not satisfy the pinned-runtime file policy as-is. A separate existing desktop-extension ELF is mode 0755, has no symlink ancestry and passes `binary_snapshot` against the already configured daemon executor hash. That read-only metadata check identifies a candidate, not a verified native version/schema or a valid execution venue. Its compatibility must be checked in the confirmed normal-host entry before a real trial. No installed permission was changed, no binary was copied to force acceptance, and no security guard was weakened.

## Why a separate driver is needed

ProtocolClient accepts `untrusted`, but production Pipeline start/resume/dispatch deliberately hardcode and verify `on-request`. Do not change that production behavior for a probe or return a fake on-request acknowledgement after sending untrusted.

Prepare an explicitly reviewed sidecar that reuses ProtocolClient, the approval projection and SitesApprovalProvider. It must transparently record the actual effective native policy and own only its newly acknowledged thread/turn. It needs a dedicated private journal; it is not evidence that the production Pipeline's normal submit path has been activated. A live-launching sidecar is not implemented by the plan builder.

The event sink records only the bounded metadata needed for this trial and never responds from inside an active transport operation. Bind only the acknowledged new thread/turn and exact pending request on that one live connection. Stop on unknown policy, extra commands/tools, altered command/cwd, network or permission requests, escalation ambiguity or uncertain IO. The stable approval envelope does not expose every original execution flag, so absence of an additional-permissions field alone is not proof of no escalation.

While the person reviews the actual request, continue processing typed terminal/resolved events. After one authenticated receipt is consumed, drain events and recheck live pending state, exact request hash/IDs and deadline immediately before sending one response. Never restart, reconnect, recreate or resend to deliver an old request.

## Evidence required for success

- The actual native command approval matches the exact command, scratch directory and owned thread/turn. The approved bounded preview and request binding are sent only to the already approved private Site.
- The person explicitly approves or declines once through the real authenticated page. A model-supplied flag, annotation, URL open or ordinary tool confirmation is not the receipt.
- Exactly one correlated response is written on the original native connection. SENT alone is not success.
- For accept, a typed commandExecution completion shows the exact command/cwd, exit code zero and aggregated output `LAF_APPROVAL_TEST` followed by a newline. Model prose is not execution evidence.
- Matching typed `serverRequest/resolved`, terminal turn and native history preserve the original request/task identity. Missing history remains an explicit verification limit.
- No extra command, file modification, real repository access, permission amendment, global setting change or existing-service restart occurred. Cleanup touches only the newly owned turn/thread/process identities.

If the person declines, respect it and verify non-execution rather than retrying accept. If no matching approval occurs or evidence is incomplete, report an inconclusive/blocked trial rather than relaxing the checks.

## Exact approval still required

Before running, identify the verified normal-host entry and safe pinned ELF, exact model/account route, scratch path, full command, one-thread/one-turn/time limit, bounded data sent to the same private Site, one human interaction, and permitted interrupt/archive/process cleanup. Confirm existing model-usage costs or limits without claiming the trial is free. Actual native execution and any real-service activation are separate decisions; the latter is not necessary for this proposed sidecar trial.

The current preparation used version/schema generation and read-only operator capabilities only. Stable ThreadStartParams and TurnStartParams hashes match the existing 0.167 contract. No account identity, authentication secret, model turn or user workspace content was retrieved for this plan.
