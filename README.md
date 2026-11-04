# Agent Orchestrator

## Overview

Agent Orchestrator helps operators distinguish running a Codex task from verifying its result and deciding whether it is safe to resume. It records execution, acceptance evidence, recovery decisions, and human review separately, so uncertain work is not blindly retried. It provides a Python runner and optional user daemon for Codex CLI tasks in dedicated workspaces, plus a local MCP stdio adapter.

> **Status: experimental.** Standard systemd deployment remains blocked by the current host's AppArmor namespace restriction. The separate development PoC has its own evidence and limits. End-to-end mailbox access, write approval, and parent-dot retrieval are not fully verified; passing unit tests do not establish production readiness.

## Installation

There is no Python package installer; the Python code uses the standard library. The documented environment requires Linux, Python 3.14, an existing Codex CLI and file-auth setup, `unshare`, `mount`, `setpriv`, `bwrap`, and cgroup v2 through a delegated systemd user manager. The reported verification host is Ubuntu 26.04.1 with Python 3.14.4 and `codex-cli 0.164.0-alpha.11.1`. Other operating systems, CLI versions, and keyring authentication are unverified. See the [systemd deployment gate](docs/systemd-deployment.md) and [PoC service setup](docs/poc-services.md).

## Usage

```bash
python3 -B src/runner.py --help
python3 -m unittest discover -s tests -v
```

A typical flow is to check a fresh runtime, prepare a fixture, submit a task, run it, and inspect its state. The [runner how-to](docs/how-to/run-task.md) shows a read-only example. Its commands have not been executed against this PR head because a checkout could not be obtained: GitHub DNS resolution failed and the execution environment became unavailable. The live `run` step was not attempted and may consume subscription allowance. See the [review-time validation note](docs/testing.md#review-time-validation) and [follow-up issue #2](https://github.com/i-pp/agent-orchestrator/issues/2).

## Advanced Usage

- [Architecture and task lifecycle](docs/architecture.md)
- [Workspace isolation](docs/workspace-isolation.md) · [Concurrency](docs/concurrency-design.md) · [Recovery](docs/recovery-design.md)
- [Daemon](docs/daemon-design.md) · [Scheduler](docs/scheduler-design.md) · [Resource limits](docs/resource-limits.md)
- [MCP connection design](docs/chatgpt-connection-design.md) · [Mailbox operations](docs/mailbox-operations.md) · [Mailbox grants](docs/mailbox-grants.md)
- [Systemd deployment status](docs/systemd-deployment.md) · [Phase 2-E3 evidence](docs/phase2-e3-report.md) · [Development PoC evidence](docs/phase2-e3-2-report.md)

## Documentation

See [verification design](docs/verification-design.md), [Git tracking](docs/git-tracking.md), [testing](docs/testing.md), and the [phase report index](docs/phase-reports.md). Shared orchestration policies are maintained in [`i-pp/ai-orchestration`](https://github.com/i-pp/ai-orchestration). The [Japanese README](README_jp.md) is the translation of this canonical English page.
