# Run a Basic Task

This how-to runs a read-only fixture task in a fresh, dedicated runtime's shared workspace. Run it from the repository root. The example uses `.runtime/docs-howto`; choose a different new path if it already exists. For isolated or write-enabled fixture tasks, follow the dedicated guidance in [workspace isolation](../workspace-isolation.md).

## Prerequisites

- The documented Linux environment and existing Codex CLI setup are available; see the [README](../../README.md#installation).
- The pinned common-policy checkout is configured at the operator-selected path. The configured pin is `394bf5377c2b82bb6f660ca917192bdbe337bc85`; the default path is `../ai-orchestration` relative to the repository root. Verify it with `python3 -B scripts/policy_context.py --format inspect`; see the [common-policy guide](../common-policy.md).
- `.runtime/docs-howto` is a new runtime directory with no existing task data. Use another unused name if needed.
- The acceptance fixture is tracked at `examples/read-fixture.acceptance.json`.

## Procedure

Check the environment and create the fixture exclusively. The Python command fails with `FileExistsError` if `fixture.txt` already exists, preserving the existing file.

> `submit` only records the task and acceptance policy in the local runtime queue. `run` starts the Codex CLI and may consume subscription allowance; continue only when you intend to perform a live Codex check.

```bash
python3 -B scripts/policy_context.py --format inspect
python3 src/runner.py --runtime .runtime/docs-howto check
python3 - <<'PY'
from pathlib import Path

fixture = Path('.runtime/docs-howto/workspace/fixture.txt')
fixture.parent.mkdir(parents=True, exist_ok=True)
with fixture.open('x', encoding='utf-8') as stream:
    stream.write('phase1-marker: CERULEAN-17\n')
PY
python3 src/runner.py --runtime .runtime/docs-howto submit 'Read only fixture.txt and report its marker.' \
  --acceptance examples/read-fixture.acceptance.json
```

Copy the `task_id` returned by `submit` into `TASK_ID`, then run and inspect it:

```bash
TASK_ID='<task_id returned by submit>'
python3 src/runner.py --runtime .runtime/docs-howto run "$TASK_ID" --timeout 60
python3 src/runner.py --runtime .runtime/docs-howto show "$TASK_ID"
```

## Expected boundaries

- `submit` queues the task locally and does not start Codex; `run` performs the synchronous Codex execution.
- `check` tests nested isolation and existing ChatGPT authentication. A `BLOCKED` result is not a pass; stop and do not bypass the isolation guard.
- This example uses the fresh `.runtime/docs-howto` runtime, a shared workspace with a read-only sandbox, and serial execution.
- The configured runtime must stay within the repository's `.runtime/` directory.
- Passing a result string condition is not by itself proof of semantic correctness. Review the saved artifact and the acceptance evidence.
- If isolation or verification fails, stop and follow the documented recovery path; do not treat an unverified task as complete.

For isolated parallel tasks, recovery, or cancellation, see [workspace isolation](../workspace-isolation.md), [concurrency design](../concurrency-design.md), and [recovery design](../recovery-design.md).
