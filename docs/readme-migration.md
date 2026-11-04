# README Reorganization and Migration Map

This record describes the information audit for the README rewrite. It does not replace the linked technical or operational documents.

## Source state

- Repository: `i-pp/agent-orchestrator`
- `main` revision inspected: `1468c0abb06e0b36ad92a7f041d5305aab688947`
- Original README: 459 lines and 34,389 UTF-8 bytes, primarily Japanese.
- Revised README: 32 lines and 2,668 UTF-8 bytes in English; the new Japanese translation is 32 lines and 2,941 bytes.
- There was no `README_jp.md` or open PR at inspection time.

## Content map

| Original README topic | Destination |
| --- | --- |
| Overview, runtime ownership, environment and versions | Concise paired READMEs; [systemd deployment](systemd-deployment.md), [PoC services](poc-services.md) |
| Runner commands, acceptance fixture and task lifecycle | [Architecture](architecture.md), the new [runner how-to](how-to/run-task.md), and existing examples |
| Workspace isolation and maximum parallel sessions | [Workspace isolation](workspace-isolation.md), [concurrency design](concurrency-design.md) |
| Implementer/reviewer responsibilities and acceptance | [Native agent team](native-agent-team.md), [verification design](verification-design.md) |
| Interruption, restart, recovery, status, identifiers and saved-data migration | [Recovery design](recovery-design.md), [verification design](verification-design.md), [retention policy](retention-policy.md) |
| User daemon, FIFO and scheduling | [Daemon design](daemon-design.md), [scheduler design](scheduler-design.md) |
| CPU, memory, storage and retention limits | [Resource limits](resource-limits.md), [retention policy](retention-policy.md) |
| Local Git audit | [Git tracking](git-tracking.md) and `scripts/git_audit.py` |
| Unit, simulated integration, live Codex and phase-specific checks | [Testing guide](testing.md) and the new [phase report index](phase-reports.md) |
| MCP stdio adapter, tool scope, connection and mailbox behavior | [ChatGPT connection design](chatgpt-connection-design.md), [mailbox operations](mailbox-operations.md), [mailbox grants](mailbox-grants.md), [PoC services](poc-services.md) |
| Standard systemd deployment, current AppArmor blocker and separate development PoC | [Systemd deployment](systemd-deployment.md), [Phase 2-E3 report](phase2-e3-report.md), [Phase 2-E3.2 report](phase2-e3-2-report.md) |
| Common policy pinning and local loading | [Common policy loading](common-policy.md) and the canonical [ai-orchestration repository](https://github.com/i-pp/ai-orchestration) |

## Accuracy corrections and preservation

The original README referenced `systemd/aceis-mcp-tunnel.service` and `systemd/aceis-mcp-tunnel.yaml`, and used the first path in a `systemd-analyze` command. Those paths are not tracked; the repository contains example files whose names include `.example`. The new README links to the design documents instead of implying an installed unit exists.

The original overview presented mailbox request/answer and retrieval as confirmed, while a later section marked ChatGPT access, write approval, and parent-dot retrieval unverified. The rewritten status uses the narrower statement and keeps the standard systemd AppArmor blocker separate from the development PoC evidence.

The README rewrite does not remove runtime code, configuration, phase reports, or design documents. Detailed procedures remain in their mapped destinations; the exact former README remains available in Git history.

## Concrete README changes and validation limits

The English entry point now follows the Overview / Installation / Usage / Advanced Usage / Documentation structure, while the new Japanese page translates its scope, environment, status, and navigation. The old README was reduced from 459 lines to 32; the command-heavy operational detail is linked from dedicated guides and phase reports instead of being repeated in the landing page. The systemd/AppArmor blocker and the separate development PoC remain explicit.

The PR review verified referenced paths against the repository tree and checked the translated status statement for semantic alignment. The task commands were not executed against the PR head because no local checkout was available. No CI status checks or workflow runs were present at review time; a follow-up issue tracks repeatable CI and command validation.
