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
