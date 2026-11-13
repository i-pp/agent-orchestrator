# Pre-launch Guard Against Nested Isolation

Do not create another isolation layer from inside an isolated Codex environment. This operational constraint was added on 2026-10-29; it does not modify the hash-pinned legacy policy body. The [Japanese historical source](nested-isolation-guard_jp.md) is unchanged.

## Detection and enforcement

`src/isolation_guard.py` reads procfs without changing settings:

- `SANDBOXED`: known bwrap profiles, sandbox PID 1, or evidence of additional PID/user namespaces.
- `HOST_CONTEXT`: a limited set of matching host observations. This is not permission or proof that execution will succeed.
- `UNKNOWN`: missing evidence or an unknown profile. A user namespace alone does not identify Codex or prove a permission failure. Sysctl values are not used as an execution-eligibility shortcut.

The guard applies to `isolation.launch`, `isolated_launch` (including preflight/version/identity), `verification_launch`, `development_poc.launch`, and direct bwrap/unshare/nsjail/firejail commands passed to `transport.Process`. Declared isolation entry points check before argument construction, authentication-mount preparation, or process creation. Known isolation is rejected with `NESTED_ISOLATION_FORBIDDEN`; unknown context with `ISOLATION_CONTEXT_UNKNOWN`. The implementation does not launch a probe to decide, and has no bypass setting or environment variable.

## Recorded outcome

A refused task uses `WAITING_FOR_HUMAN`; the affected Codex or verification status is `BLOCKED`. Error/evidence records contain `status=BLOCKED`, `reason_code`, `executed=false`, and `quality_verdict=UNASSESSED`. If Codex already succeeded but only verification is blocked, preserve `codex_status=SUCCEEDED`.

Individual verification `passed` values are null; `all_passed=false` means acceptance is not established, not that model quality failed. There is no unisolated fallback or automatic rerun. Ordinary human review cannot turn an unexecuted mandatory check into a pass. Resumption needs a suitable execution context and an explicit new execution decision.

Read-only MCP exposes only `BLOCKED` and a fixed reason code; CLI/DB evidence retains diagnostic reasons. No extra MCP permissions, tools, or write exposure are added. Existing daemons are not restarted automatically; already-running Python processes need their normal next start to load new code.

## Scope and safe checks

This is not an OS sandbox, a parser for arbitrary shell programs, comprehensive monitoring of a model's internal tools, or control over another application's launchers. Arbitrary wrappers and descendants are not fully controlled. Fixed developer instructions and AGENTS also state the rule, but that part is operational guidance rather than enforcement. Existing external evaluation launchers were not patched by this guard; isolation-required evaluations remain unexecuted and blocked where the boundary is unavailable.

File inspection, static checks, and ordinary unit tests that do not add isolation can continue. Do not retry namespace integration tests from the forbidden context. Guard tests use mocks and fake peers without real models, authentication, or namespace operations.
