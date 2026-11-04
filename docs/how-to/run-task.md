# Run a Basic Task

This how-to runs a read-only fixture task in the existing shared workspace. Run it from the repository root. For isolated or write-enabled fixture tasks, follow the dedicated guidance in [workspace isolation](../workspace-isolation.md).

## Prerequisites

- The documented Linux environment and existing Codex CLI setup are available; see the [README](../../README.md#installation).
- The default runtime has passed `check`.
- The acceptance fixture is tracked at `examples/read-fixture.acceptance.json`.

## Procedure

Create a harmless fixture and submit a read-only task:

```bash
python3 src/runner.py check
python3 -c "from pathlib import Path; Path('.runtime/workspace/fixture.txt').write_text('phase1-marker: CERULEAN-17\n')"
python3 src/runner.py submit 'Read only fixture.txt and report its marker.' \
  --acceptance examples/read-fixture.acceptance.json
```

Copy the `task_id` returned by `submit` into `TASK_ID`, then run and inspect it:

```bash
TASK_ID='<task_id returned by submit>'
python3 src/runner.py run "$TASK_ID" --timeout 60
python3 src/runner.py show "$TASK_ID"
```

## Expected boundaries

- `submit` queues the task; `run` performs the synchronous execution.
- The existing mode uses a shared workspace with a read-only sandbox and serial execution.
- The configured runtime must stay within the repository's `.runtime/` directory.
- Passing a result string condition is not by itself proof of semantic correctness. Review the saved artifact and the acceptance evidence.
- If isolation or verification fails, stop and follow the documented recovery path; do not treat an unverified task as complete.

For isolated parallel tasks, recovery, or cancellation, see [workspace isolation](../workspace-isolation.md), [concurrency design](../concurrency-design.md), and [recovery design](../recovery-design.md).
