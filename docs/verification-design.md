# Task Acceptance Verification

Process exit, Codex `completed`, model-reported success, acceptance checks, and human approval are separate. A model's narrative does not establish artifact or test results. The [Japanese historical source](verification-design_jp.md) preserves the original phase description.

## Evidence and outcome

| Check | Evidence |
| --- | --- |
| Codex execution | Thread/turn, matching completion event, exit code, cleanup, saved result |
| Artifact | Regular file inside workspace, size, SHA-256, expected value |
| Test | Allowed ID, fixed argv, deadline, exit code, sanitized output, cleanup |
| Specific condition | `result_equals` / `result_contains` value, verdict, result hash |
| Human decision | Actor, reason, decision ID, valid evidence, workspace fingerprint |

A normal Codex exit with a missing artifact or failed test is `FAILED`. A normal result first enters `VERIFYING`; evidence and final state are committed together. Parent loss during verification is `UNKNOWN` while preserving a saved `codex_status=SUCCEEDED`.

## Acceptance specification

At submission the operator supplies JSON containing:

- `artifacts`: relative path and optional `sha256`.
- `tests`: allowed test IDs.
- `conditions`: `kind` (`result_equals` or `result_contains`) and nonempty `value`.
- `human`: whether a human decision is required.

Reject unknown keys, unapproved commands, out-of-bound paths, invalid types/hashes, and more than 30 combined items. Persist acceptance and command snapshots in SQLite; callers cannot alter them afterward. Missing criteria or result-string-only criteria require a human decision. Approval cannot override an automated failure.

Automatic `COMPLETED` requires a normal Codex result, a model success report without failure/refusal, independent artifact or test evidence, every criterion passing, and confirmed cleanup. Generated `outcome` is not independent verification. Mere file existence does not prove creation, content correctness, or quality.

## Files and commands

Artifacts must be relative regular files inside the runtime workspace. Reject absolute paths, `.`, `..`, symlinks, and special files. Open each component with `dir_fd` and `O_NOFOLLOW`, not just string-prefix checks. The per-file limit is 16 MiB; contents are not logged.

Commands are predefined in root `verification-commands.json`. Resolve and freeze an ID's argv at submission, then recheck against the allowlist before execution. Do not adopt model-suggested argv, executable, or cwd. Executables are absolute; shells and `shell=True` are forbidden. Each command has at most 60 seconds. The registry assumes operator-reviewed trusted programs; it is not a signature/authentication system.

Cwd is fixed to the workspace. Separate user/PID/mount/network namespaces make the root read-only, hide authentication, and provide private tmpfs. OS support files can still be read; this is not a claim that arbitrary programs can read only workspace files. Never register credential-reading verification or fall back to host writes/greater privileges. The [nested-isolation guard](nested-isolation-guard.md) can block launch before any namespace creation.

## Deadlines, cancellation, and approval

The run deadline covers Codex and verification; command-specific deadlines also apply. Either expiry yields `TIMED_OUT`. Cancellation remains available during verification. Uncertain cleanup or launch checkpoints yield `UNKNOWN`. Explicit recovery-time re-verification gets a new 60-second budget without generating another Codex turn.

Human approval requires successful automated checks and an unchanged post-verification workspace. Save actor and reason. Approval satisfies a task-specific human criterion; it does not waive failed checks. Actor is a local operator assertion, not external-service authentication.

## Evidence limits and phase extensions

`show` exposes `verifications.evidence`, linked by execution/reconciliation/decision IDs. Output is bounded and secret-pattern-sanitized, not raw originals; arbitrary confidential data cannot be automatically guaranteed absent. A manifest describes current regular files, not external or transient side effects. No semantic-judgment model, dynamic command generator, or verification-plugin framework is added here.

In isolated mode, artifacts/tests use only the owned workspace. Verifiers pivot to a minimal root hiding other workspaces, the coordinator, and Codex home, with independent networking. Workspace-write requires a dedicated fixture, artifact content hashes, and allowed tests; existence-only acceptance is insufficient. Reviewers validate independent read-only snapshot copies and never approve the source task automatically. See [workspace isolation](workspace-isolation.md).

D1 puts allowed commands under resource limits. A limit violation or unverified supervision is not a pass; uncertain scope cleanup is `UNKNOWN`. CPU throttling is distinct from termination for cumulative CPU-budget excess. Fixed command allowlists remain unchanged.
