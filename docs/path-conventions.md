# Paths and Shell Variables

Documentation tokens such as `<PROJECT_ROOT>` are placeholders, not shell input. Set the corresponding quoted variables before using examples. Historical reports were anonymized using this notation; original logs, running configuration, and authentication files were not changed. The [Japanese historical source](path-conventions_jp.md) is preserved unchanged.

| Placeholder | Shell variable | Meaning |
| --- | --- | --- |
| `<PROJECT_ROOT>` | `$PROJECT_ROOT` | Absolute checkout path for this repository |
| `<STATE_DIR>` | `$STATE_DIR` | Dedicated state directory under the project's `.runtime/` |
| `<MODEL_DIR>` | `$MODEL_DIR` | Locally managed model directory |
| `<INFRA_ROOT>` | `$INFRA_ROOT` | Separately managed infrastructure repository |
| `<USER_HOME>` | `$HOME` | Operator home without a fixed username |
| `<CODEX_BINARY>` | `$CODEX_BINARY` | Absolute path to installed Codex executable |
| `<TUNNEL_CLIENT>` | `$TUNNEL_CLIENT` | Absolute path to installed Tunnel client |
| `<CONTROL_PLANE_KEY_FILE>` | `$CONTROL_PLANE_KEY_FILE` | Existing private credential file outside Git; never expand its contents |

These defaults illustrate a layout. Adjust only the variables needed for your installation. Setting them does not create files or install software.

```bash
export PROJECT_ROOT="${PROJECT_ROOT:-$HOME/src/agent-orchestrator}"
export STATE_DIR="${STATE_DIR:-$PROJECT_ROOT/.runtime/example}"
export MODEL_DIR="${MODEL_DIR:-$HOME/models}"
export INFRA_ROOT="${INFRA_ROOT:-$HOME/workspace/project-kiss-infra}"
export CONTROL_PLANE_KEY_FILE="${CONTROL_PLANE_KEY_FILE:-$HOME/.config/agent-orchestrator/credentials/control-plane-api-key}"
export CODEX_BINARY="${CODEX_BINARY:-$(command -v codex)}"
export TUNNEL_CLIENT="${TUNNEL_CLIENT:-$(command -v tunnel-client)}"
cd "$PROJECT_ROOT"
```

If an executable is not on PATH, set its variable to the actual installed absolute path. Keep `STATE_DIR` under `PROJECT_ROOT/.runtime/`; do not casually select a directory that owns existing work. Phase/run names in reports identify historical evidence, not directories guaranteed to exist elsewhere.

Standard OS paths such as `/usr/bin`, `/etc`, `/proc`, `/sys`, and `/dev` remain literal. `%h`, `%t`, and `%d` are systemd specifiers; `@{AO_PROJECT}` belongs to the AppArmor draft, not shell syntax.

Operational Tunnel configuration stays local and Git-ignored. Replace example placeholders locally before use. Editing a draft or document does not apply it to a running service. Do not run namespace-creating examples inside Codex isolation; follow the [nested-isolation guard](nested-isolation-guard.md).

Set `TASK_ID`, `TASK_A`, `SNAPSHOT_ID`, `DECISION_ID`, and similar variables from the corresponding preceding command's output. `BACKUP_FILENAME` must name a verified backup. Do not run examples with unset IDs or paste angle-bracket placeholders into a shell.
