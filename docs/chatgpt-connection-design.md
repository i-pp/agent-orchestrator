# ChatGPT Connection Design

This is the **Phase 2-E1 stdio design**, checked on 2026-10-28 (Asia/Tokyo), not a current deployment recipe. That phase implemented/tested locally and checked documentation without connecting ChatGPT/Platform, obtaining/starting a Tunnel client, issuing keys, or changing permissions/contracts/billing. The [Japanese historical source](chatgpt-connection-design_jp.md) is unchanged.

A 2026-11-05 follow-up recorded a separately isolated HTTP/Events candidate with offline tests, not a production/Tunnel rollout or verified real callback/conversation wake. Keep notifications disabled until effective owner-only access is verified. See [Events deployment](mcp-events-deployment.md) and [runtime safety](mcp-events-runtime-safety.md); their own evidence controls later status.

## Boundary

```text
Proposed: ChatGPT web -> OpenAI Tunnel -> tunnel-client
                                           | stdio (not connected in E1)
E1: fake MCP client -> mcp_adapter.py -> fixed Unix socket -> existing daemon -> runner
```

The adapter uses Python 3.14 standard-library code, not an HTTP listener. It adds no authentication server, scheduler, DB schema, lease, model call, or autonomous loop following external instructions. It uses the existing Unix-socket client without starting/stopping the daemon.

## Account, authentication, and cost checks

The [Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) describes outbound HTTPS to private MCP, local stdio/HTTP support, a Tunnel ID/runtime API key, Platform Tunnels Read/Manage for management and Read/Use for operation, and association with the intended ChatGPT workspace. E1 did not verify the user's actual permissions, association, workspace policy, administrator approval, or restrictions. A Business subscription alone does not prove entitlement.

[Custom MCP connections](https://developers.openai.com/api/docs/guides/custom-mcp-server) distinguish a direct HTTP URL from a Tunnel forwarding local stdio. Server-side OAuth, Tunnel runtime authentication, and local Unix UID checks are separate layers.

Codex tasks retain existing ChatGPT subscription authentication; E1 made zero model generations. No documentation established that this login substitutes for a Tunnel runtime API key. Requiring that key does not imply use of model-metered Responses API: the adapter does not call it. The cited Tunnel/[pricing](https://developers.openai.com/api/docs/pricing) pages did not settle standalone Tunnel fees, Business inclusion, or billing activation. Do not call it free/included; confirm contract/admin/provider details before proceeding. Never paste runtime keys into MCP responses or a ChatGPT server-authentication form.

## Protocol and tool surface

E1 explicitly pins MCP **2025-11-25**, implementing initialize, initialized notification, tools/list, tools/call, ping, JSON-RPC 2.0, and UTF-8 JSONL stdio. Only tools capability is advertised: no resources, prompts, sampling, elicitation, MCP Tasks, or dynamic tool changes. See [lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) and [stdio](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

The newer [2026-07-28 specification](https://modelcontextprotocol.io/specification/2026-07-28) changes stateless behavior; this implementation does not claim latest-revision support. It returns its supported revision for client compatibility decisions. **Real Tunnel/ChatGPT acceptance of that revision and capability set was unverified in E1.**

| Tool | Daemon method | Input | Default |
| --- | --- | --- | --- |
| `orchestrator_status` | status | None | Enabled |
| `orchestrator_list_tasks` | list | Limit 1..50, offset 0..1000000 | Enabled |
| `orchestrator_get_task` | status | Task UUID | Enabled |
| `orchestrator_submit_task` | submit | Prompt, request key | Disabled |
| `orchestrator_cancel_task` | cancel | Task UUID | Disabled |

Use both input schemas and runtime validation. Reject extra fields and arbitrary socket/runtime/path/shell selection. Do not expose shutdown, pause, configuration/deletion, unknown-resolution, or resume. Accurate read-only annotations are hints, not authorization. Disabled write tools are absent from discovery and rejected on direct calls; see [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).

## Writes are local-test-only in E1

Enable writes only when the operator explicitly supplies `--allow-writes` and an existing runtime `mcp-task-policy.json`: owned regular file, owner-only permissions, at most 16 KiB. Pin workspace, acceptance, and timeout; clients cannot override them.

```json
{
  "workspace": {"fixture": "seed", "access": "read-only", "role": "implementer"},
  "acceptance": {"human": true},
  "timeout": 120
}
```

This example waits for a human decision, not automatic success. Automatic acceptance needs operator-defined artifact hashes/test IDs. `Store.submit` revalidates; workspace-write still requires hashes and allowed tests. E1 tested only read-only fixture submit/cancel. **Do not pass `--allow-writes` for a ChatGPT trial; remote write exposure requires separate authorization.**

## Responses, auditing, and deadlines

- Return only allowlisted state/IDs, verification metadata, scheduler counts, and similar metadata. Do not return prompts, free-form model results/errors, acceptance bodies, configuration, credentials, environment, owner, or local paths. Validate states/IDs; detailed results remain local.
- A successful tool call means the MCP operation succeeded; submission means accepted, not completed. Preserve `completion_verified` semantics, including worker cleanup. Fail with fixed code and `isError`, not raw daemon exceptions.
- Stable request keys deduplicate identical definitions; conflicting input is rejected. After timeout/disconnect, reconcile the same key, never blindly submit a new key or assume side-effect rollback.
- Frames/responses are at most 64 KiB; prompts at most 4,096 characters and 16 KiB. Reject duplicate keys, nonfinite values, invalid UTF-8, batches, and malformed structure.
- Daemon RPC is bounded to two seconds overall, recomputing remaining I/O time. Stdout writes get two seconds; incomplete input gets three seconds. Idle input alone does not terminate the adapter.
- Operator fixes runtime at startup under project `.runtime`, with no symlinks and owner-only root/log directories. Existing client checks endpoint/peer UID. Remote users gain no authority beyond the local operator.
- Private `logs/mcp-SESSION_UUID.jsonl` records call ID, hashed RPC/request IDs, tool/task/execution IDs, and start/end, not prompt or raw request key. Later task reads connect submissions to executions. Limit one log to 4 MiB; reject new operations if audit durability/capacity fails. No automatic deletion; total storage remains monitoring, not hard quota.
- Adapter disconnect does not cancel daemon tasks. Use explicit cancel for a selected task. Returned strings/descriptions never become instructions in an autonomous execution loop.

## Separate version-helper change

The two D2.1 version checks were consolidated into `binary_version`. It runs only `--version` in existing isolation with dedicated HOME/Codex home, no authentication mount, and a network namespace. The installed non-`/usr` Codex exception requires argv exactly `["--version"]`; it cannot authorize App Server or arbitrary arguments and has no host fallback. Historical checks used zero model generations. This is distinct from the D2 configuration change attributed to user app actions.

## Proposed next-stage checklist, not executed by E1

1. Identify the exact workspace/Platform organization; verify custom-MCP permission, Tunnel rights, association, contract, and fees.
2. Approve runtime-key scope/storage and prepare a dedicated nonconfidential read-only runtime.
3. Verify official client version/distribution, installed help, and 2025-11-25 compatibility before launch.
4. Use an approved existing/new Tunnel and authorized association. Supply secrets through an approved local authentication mechanism, never chat, Git, logs, or literal CLI arguments.
5. Compare the guide's command shape with actual help and approved scope:

   ```bash
   tunnel-client help quickstart
   tunnel-client init --sample sample_mcp_stdio_local --profile orchestrator-readonly \
     --tunnel-id <approved-tunnel-id> \
     --mcp-command "python3 -B /absolute/repo/src/mcp_adapter.py --runtime /absolute/repo/.runtime/approved-readonly"
   tunnel-client doctor --profile orchestrator-readonly --explain
   tunnel-client run --profile orchestrator-readonly
   ```

6. Check readiness/polling plus any default loopback management UI's bind/permissions. Do not add public exposure/systemd deployment automatically.
7. In the intended workspace, select the approved Tunnel and appropriate server authentication. Discover exactly three read-only tools, manually test status/list/get, and correlate call/task/execution audit IDs.
8. Unknown compatibility, cost, association, or permissions means connection incomplete. Writes remain a separate decision.

The [official stdio example](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels#set-up-tunnel-client) supplies the proposed shape, not evidence that these commands were executed. Local success is not real ChatGPT connection success.
