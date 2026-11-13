# Tunnel MCP Operations and Completion Criteria

Use the current shared-policy source and verified loader described in [AGENTS](../AGENTS.md) and [common-policy adoption](common-policy.md). This operational guide does not redefine permissions. The [Japanese historical source](mailbox-operations_jp.md) retains its earlier, now-superseded policy-location investigation and dated validation results.

## Completion criteria and evidence

| Criterion | Evidence |
| --- | --- |
| Keep eleven existing operations and full schemas; do not add submit/cancel | Schema regression and actual adapter discovery/tools-list |
| Reconstruct key/Codex/capability flags from saved state | `resume-plan` / `rollback-plan`; refuse missing references rather than guess |
| Refuse unresolved work, uncertain generations, or foreign owners before stop | Preflight, immediate pre-stop DB check, mock regressions |
| Do not retry writes or create another slot after lost ACK | At most one registration write, then getter-only reconciliation; missing state remains unknown |
| Bound read failures | At most one read-only reconnection, default five-second RPC, ten-second maximum, same pipe-write deadline |
| Recover matching request/answer IDs and hashes | `inspect`, server/client hash validation, real stdio fixtures |
| Distinguish reachability, input rejection, and storage without secrets | Begin/end, `tool_rejected`, fixed mailbox status/code/hash; no raw body in audit |
| Prepare a concrete approval candidate | `prepare` emits JSON only; no automatic allowlist expansion |
| Preserve existing answers and one-shot evidence | Do not alter/delete existing slots, DB, or policy |

The historical verified path was local Codex registration, MCP get/save by an already-running Chat, and local Codex body retrieval, with eleven tools seen in the ChatGPT UI. Direct parent-Dot catalog/calls and waking a dormant Chat were separate unverified capabilities. Explicit local stdio through `mailbox_ops.py` is not proof of hosted Chat/Dot calls.

## Reconnection, recovery, and rollback

Set [path variables](path-conventions.md), then inspect before operating:

```bash
cd "$PROJECT_ROOT"
python3 -B scripts/poc_services.py status
python3 -B scripts/poc_services.py resume-plan
python3 -B scripts/poc_services.py rollback-plan
```

Plans reconstruct preflight/stop/start/status argv from the same saved key/Codex references and flags. They do not read key contents, execute commands, or resubmit tasks. `rollback-plan` removes only the mailbox flag, returning to seven operations. Missing state references require matching existing nonsecret startup evidence, never a default credential path or a new key.

A plan is a snapshot. Before an authorized application, recheck work/unknown/human-waiting tasks, owner generation, health, and scope; run the returned arrays in order with explicit saved references/flags. The immediate pre-stop DB check is not exclusion of every other writer.

Also check work outside the task DB, such as local-LLM requests, incomplete MCP begin/end records, and uncertain executor state. A UI error/disconnect does not prove failure. The bounded read reconnection is not a service-watch daemon or permanent restart loop.

After restart, separately verify READY, both owned/active units, daemon/Tunnel health, eleven schemas, UI tool refresh, current conversation catalog, and actual tool calls. Do not bypass with new APIs or permissions. Rollback preserves DB/policy/logs; deleting state or locks to force startup is forbidden.

## Register once and retrieve the answer

`bundle.json` contains approved `task_id`, `request_id`, `request_sha256`, and `request`. `key.json` contains the same identifying triple and optionally `expected_answer_sha256`. Preserve IDs across timeout. UTF-8 JSON rejects unknown fields, oversized input, and added path/URL/command selectors.

```bash
python3 -B scripts/mailbox_ops.py register < bundle.json
python3 -B scripts/mailbox_ops.py inspect < key.json
python3 -B scripts/mailbox_ops.py inspect --include-content < key.json
```

`register` first gets existing state, reuses matching content, and calls `register_request` at most once only when absent. After lost ACK, reconnect and use getters only. `NOT_FOUND` does not prove a timed-out earlier write cannot later commit. Preserve `UNKNOWN`; do not resend, replace the ID, or regenerate an answer.

`inspect` reconciles request/answer reads with at most one read-only reconnect. RPC defaults to five seconds; `--timeout` accepts 0.05..10 seconds. Reap timed-out stdio children without stopping existing services/Chat. `PENDING` means waiting, `NOT_FOUND` means storage unconfirmed, and `ANSWERED` requires matching content/hash. Unknown/blocked exits 2; normal states exit 0.

Receipts show IDs, hashes, call IDs, state, counts, and time, not bodies. Request content explicitly with `--include-content` and use only nonconfidential material. MCP get itself returns content, so host/client history may retain it.

## Prepare ordinary work without expanding scope

Fixed-slot answers are immutable; never repurpose a slot. A draft contains task/request IDs and request text, fixed by the operator before reconciliation.

```bash
python3 -B scripts/mailbox_ops.py prepare < draft.json > candidate.json
```

Preparation returns a hash, request bundle, candidate policy preserving existing slots, and approval requirement. A new slot is `PENDING_SCOPE_APPROVAL`, not an applied policy, service change, or Chat send. Registration refuses anything outside the current pin. Limits remain 32 slots, 4,096 UTF-8 bytes per request/answer, and a 1 MiB ledger; no automatic capacity increase or old-slot deletion/reuse.

Approval must identify exact IDs, request hash/body, target runtime, added slot count, same-scope client read/write exposure, temporary interruption, application, and rollback. Only after approval may the operator change private policy and run preflight/work reconciliation/application. No extra public operation is needed. A mailbox slot transfers bounded consultation text; it does not create/execute a runner task or expose an arbitrary-command API.

## Audit and failure diagnosis

Use nonsecret metadata under private runtime `logs/mcp-*.jsonl`. Correlate begin/end with task/request/hash and call ID. End mailbox status/code/answer hash/duplicate informs storage state. `tool_rejected` records rejection without unchecked input. Do not infer Chat identity from slots.

- Returned begin/end plus answered/matching hash supports retrieval/save/readback.
- `error_or_uncertain` or unknown requires getters, not resend.
- Begin-only cannot distinguish continued work, lost response, or audit failure; investigate as unknown.
- No record means unobserved, not a blanket conclusion that Chat could not use MCP.

Never append raw bodies, unknown tool names, input values, key contents, or full external exceptions. MCP audit is at most 4 MiB per session; service logs have LimitFSIZE 64 MiB, not total-disk or indefinite-retention guarantees. Operators check capacity, incomplete calls, and preservation; no automatic prune exists. IDs/hashes are not responder authentication; same-scope clients lack role-specific authorization. Do not add inference, Chat sends, or broader permissions as diagnostic side effects.

## Regression commands

```bash
python3 -B -m unittest discover -s tests -p test_mailbox_operations.py
python3 -B -m unittest discover -s tests -p test_service_recovery.py
python3 -B -m unittest discover -s tests -p test_service_preflight.py
python3 -B -m unittest discover -s tests -p test_chat_mailbox.py
```

The original evidence used mocks/dedicated stdio without new namespaces, real models, or real services. Two socket-bind checks blocked by EPERM remained unverified in that environment. Those dated limits are not a claim these commands were rerun during translation. Real Chat sends are not part of this regression.
