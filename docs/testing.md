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

The review targeted PR head `2f4ae8d034087dd3e448c48e1a47d9b3950be75c`. The observed host was Ubuntu 26.04.1 LTS (x86_64) with Python 3.14.4; a Codex executable was present on PATH. A local checkout could not be created: `git clone` failed with `Could not resolve host: github.com`, and the execution environment subsequently became unavailable.

Therefore, these commands were **not executed against the PR head**:

- `python3 -B src/runner.py --help`
- `python3 -B src/runner.py --runtime .runtime/docs-howto check`
- the exclusive fixture creation snippet in [the runner how-to](how-to/run-task.md)
- the `submit` and `show` commands in that how-to

Source inspection confirms that `src/runner.py` defines `--runtime`, `check`, `submit --acceptance`, `run --timeout`, and `show`. This is static inspection, not execution. The `Path.open('x')` exclusive-create behavior was separately checked in a temporary directory: a second open raised `FileExistsError` and preserved the original contents; this does not validate runner integration.

The live `run` step was not attempted because it starts Codex and may consume subscription allowance. [Follow-up issue #2](https://github.com/i-pp/agent-orchestrator/issues/2) tracks checkout-level command validation.
