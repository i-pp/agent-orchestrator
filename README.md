# Agent Orchestrator

## Overview

Agent Orchestrator helps operators distinguish running a Codex task from verifying its result and deciding whether it is safe to resume. It records execution, acceptance evidence, recovery decisions, and human review separately, so uncertain work is not blindly retried. It provides a Python runner and optional user daemon for Codex CLI tasks in dedicated workspaces, plus a local MCP stdio adapter.

> **Status: experimental.** Standard systemd deployment remains blocked by the current host's AppArmor namespace restriction. The separate development PoC has its own evidence and limits. End-to-end mailbox access, write approval, and parent-dot retrieval are not fully verified; passing unit tests do not establish production readiness.

## Installation

There is no Python package installer; the Python code uses the standard library. The documented environment requires Linux, Python 3.14, an existing Codex CLI and file-auth setup, `unshare`, `mount`, `setpriv`, `bwrap`, and cgroup v2 through a delegated systemd user manager. The reported verification host is Ubuntu 26.04.1 with Python 3.14.4 and `codex-cli 0.164.0-alpha.11.1`. Other operating systems, CLI versions, and keyring authentication are unverified. See the [systemd deployment gate](docs/systemd-deployment.md) and [PoC service setup](docs/poc-services.md).

Before `run`, configure the operator-managed common-policy checkout. `config/upstream-policy.json` pins commit `aeef6db1cc9680833277f76962044da0dae501b9`; the default checkout path is `../ai-orchestration` relative to the repository root. Verify the selected checkout and pin with `python3 -B scripts/policy_context.py --format inspect`. See [common-policy setup](docs/common-policy.md) for path overrides and integrity requirements.

## Usage

```bash
python3 -B src/runner.py --help
python3 -m unittest discover -s tests -v
```

The [runner how-to](docs/how-to/run-task.md) demonstrates a read-only flow: check a fresh runtime, create a fixture, queue a task, then inspect its state. Review-time help commands and `policy_context inspect` passed against hash-verified GitHub API-materialized working trees at the reviewed PR code head and pinned policy commit; subsequent commits changed documentation only. Fixture creation, queue-only `submit`, and `show` passed against a source snapshot whose runtime blobs match the current PR head. `check` at the reviewed code head returned `BLOCKED` in this Codex sandbox (`executed=false`), and live `run` was not attempted. The [testing guide](docs/testing.md#review-time-validation) records provenance and limits; follow-up issue [#2](https://github.com/i-pp/agent-orchestrator/issues/2) tracks canonical-origin verification and live execution.

## Advanced Usage

- [Architecture and task lifecycle](docs/architecture.md)
- [Workspace isolation](docs/workspace-isolation.md) · [Concurrency](docs/concurrency-design.md) · [Recovery](docs/recovery-design.md)
- [Daemon](docs/daemon-design.md) · [Scheduler](docs/scheduler-design.md) · [Resource limits](docs/resource-limits.md)
- [MCP connection design](docs/chatgpt-connection-design.md) · [Mailbox operations](docs/mailbox-operations.md) · [Mailbox grants](docs/mailbox-grants.md)
- [Systemd deployment status](docs/systemd-deployment.md) · [Phase 2-E3 evidence](docs/phase2-e3-report.md) · [Development PoC evidence](docs/phase2-e3-2-report.md)

## Documentation

See [verification design](docs/verification-design.md), [Git tracking](docs/git-tracking.md), [testing](docs/testing.md), and the [phase report index](docs/phase-reports.md). Shared orchestration policies are maintained in [`i-pp/ai-orchestration`](https://github.com/i-pp/ai-orchestration). The [Japanese README](README_jp.md) is the translation of this canonical English page.

## Optional MCP Events integration

An opt-in Codex pipeline can expose loopback-only Streamable HTTP and MCP Events for one local OS user, with a durable notification outbox. The default local stdio adapter and its existing opt-in model and mailbox tools remain available. HTTP mode requires explicit pipeline and single-user-local flags; it exposes only owner-scoped Codex tools. Keep the ChatGPT Dev app private and unpublished. See [MCP Events deployment](docs/mcp-events-deployment.md) and [runtime safety](docs/mcp-events-runtime-safety.md). Live callback delivery, ChatGPT conversation resumption, and the three-minute notification target have not been verified in a deployed environment.
