# Testing Guide

This page separates offline or simulated checks from live Codex checks. Review the target host, project instructions, and test scope before running any command.

## Offline and simulated checks

The repository's unit suite uses fixtures, simulated App Server behavior, and local system facilities. These commands do not imply that a live Codex integration or production deployment works.

```bash
python3 -m unittest discover -s tests -v

# MCP stdio adapter and simulated App Server
python3 -B -m unittest discover -s tests -p test_phase2_e1.py -v

# Request-level discovery and permission-boundary checks
python3 -B -m unittest discover -s tests -p test_mcp_discovery.py -v

# systemd deployment fixtures; failures on a host are evidence and should not be skipped
python3 -B -m unittest discover -s tests -p test_phase2_e3.py -v
```

## Live Codex checks

These checks use the existing Codex CLI and may consume the user's subscription allowance, make model calls, or create a dedicated runtime. Run them only when the task explicitly requires a live check and the environment is suitable.

```bash
python3 tests/live_phase2.py --execute
python3 -B tests/live_phase2_c.py
python3 -B tests/live_phase2_d1.py
python3 -B tests/live_phase2_d2.py
python3 -B tests/live_phase2_e3_2.py --execute
```

## Interpreting results

- Keep offline, simulated, and live results separate.
- A pass in a fixture does not prove host deployment, namespace availability, Tunnel connectivity, or ChatGPT UI behavior.
- Record the exact test, host, CLI version, observed result, and relevant limitations.
- The standard systemd deployment remains blocked by the documented namespace/AppArmor issue; see [Phase 2-E3](phase2-e3-report.md). The separate development PoC evidence is in [Phase 2-E3.2](phase2-e3-2-report.md).
- See the [phase report index](phase-reports.md) for the historical test and design records.
- Preserve historical counts and failed or blocked results in their phase reports.

## Review-time validation

### Source and environment

The initial runner checks used a GitHub API source snapshot at commit `2f4ae8d034087dd3e448c48e1a47d9b3950be75c`; the runtime blobs were confirmed unchanged at PR head `f89b2663dcd372e7fd911ca069c4cd104a41b870`. A later check materialized a disposable one-commit Git working tree from GitHub API tree, blob, and commit objects at the reviewed PR code head `f89b2663dcd372e7fd911ca069c4cd104a41b870`. All 167 blob hashes, 17 directory tree hashes, the root tree SHA, and commit SHA matched, and the working tree was clean. Subsequent commits changed documentation only; runtime/source files are unchanged. `git clone` remained unavailable because DNS could not resolve `github.com`, so this API-materialized checkout has no remote origin and is not origin-verified.

The host was Ubuntu 26.04.1 LTS, x86_64, Python 3.14.4. Checks used a temporary HOME and a fresh runtime under `.runtime/docs-howto`; no existing Codex auth file was exposed.

### Commands and results

- `python3 -B src/runner.py --help`, plus `check --help`, `submit --help`, `run --help`, and `show --help`: all exited 0.
- `python3 -B src/runner.py --runtime .runtime/docs-howto check`: returned `IsolationBlocked` with `status=BLOCKED`, `reason_code=NESTED_ISOLATION_FORBIDDEN`, `operation=codex-namespace`, and `executed=false`. The host was detected as sandboxed (`BWRAP_PROFILE`, `SANDBOX_PID1`, `USER_NAMESPACE_MAPPING`). The guard stopped before launching namespace tools or checking Codex login status. This is a blocked preflight, not a pass; no guard was bypassed.
- The how-to's fixture snippet created `.runtime/docs-howto/workspace/fixture.txt`. Repeating exclusive creation raised `FileExistsError` and the original marker remained unchanged.
- The queue-only `submit` command exited 0 and returned a task ID; `show` exited 0 and reported `QUEUED`. No Codex process or model call was started.
- `python3 -B scripts/policy_context.py --help` and `--format inspect` exited 0 from the hash-verified working tree at the reviewed code head. The inspect output loaded pinned upstream commit `aeef6db1cc9680833277f76962044da0dae501b9` with aggregate SHA-256 `3a6e2876cb93f60a0261bce0488bd67ac93053d52fb8fe6fd6821fe9489d4efd`. The sibling policy checkout was independently reconstructed and hash-checked from GitHub API objects. Metadata reported `checkout_dirty=false`, `checkout_origin_verified=false`, `local_commit_override=true`, `source_proof=pinned_document_hashes`, `working_tree_body_used=false`, and `automatic_network=false`. The ignored override was review-local only; no origin was fabricated.
- The live `run` command was not attempted. It requires the isolation preflight and may use the existing subscription. Do not treat the blocked `check` as permission to bypass isolation.

[Follow-up issue #2](https://github.com/i-pp/agent-orchestrator/issues/2) tracks canonical-origin checkout verification and the live execution boundary. The API-materialized, hash-verified checkout and pinned-policy read succeeded, but this environment could not verify a canonical Git remote.

## Editorial Policy v2 runtime verification (2026-11-06)

The new bundle selects accepted `ai-orchestration` commit `394bf5377c2b82bb6f660ca917192bdbe337bc85`. It adds the authoritative Editorial Policy v2.0 and its workflow supplement to the three common-governance documents. The earlier inspection record above describes the previous three-document pin; it remains historical evidence.

On the dedicated Policy v2 worktree, `python3 -B scripts/policy_context.py --format inspect` exited 0 against the existing canonical checkout. All five Git objects matched their pinned SHA-256 and Git blob SHA. The aggregate SHA-256 was `e8a458430fb66791300c942d6eb49e967a184989b858ec597b7c0b22541cf0ce`. Metadata reported canonical origin, unchanged clean checkout HEAD, `working_tree_body_used=false`, and `automatic_network=false`. A Git-ignored local checkout configuration selected that exact commit; other machines must configure an existing checkout as described in the [common-policy guide](common-policy.md).

Run the focused regression checks after configuring the pinned checkout:

```bash
PYTHONPATH=tests python3 -B -m unittest test_common_policy test_common_policy_acceptance test_upstream_policy test_policy_runner -v
```

All 57 tests passed in the selected executor. They cover exact source hashes, required editorial documents, changed/missing Git objects, immutable context revalidation, full text delivered once to fresh and resumed threads, missing editorial pins preventing dispatch through the real loader, and old recorded policy versions preventing resume before peer launch. Historical owner metadata remains unchanged.

The protocol checks use `tests/fake_codex.py`, a test-owned runtime, and a version-probe stub. They do not invoke a live Codex model or create nested isolation. This evidence does not establish model compliance, article acceptance, publication readiness, or deployment. The live daemon, AceIS notification worktree, and existing task databases were not used for these checks.
