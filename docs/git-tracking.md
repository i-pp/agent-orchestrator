# Git Content and Pre-push Audit

Keep portable source and synthetic fixtures in Git, and operational state outside it. This is a maintenance guide, not authorization to commit, push, change remotes, delete files, or rewrite history. The [Japanese historical source](git-tracking_jp.md) preserves the 2026-10-29 initial-push investigation; its repository visibility, pending index work, role counts, and file-line references are historical, not current state.

## Include portable inputs

- Implementation, scripts, tests, and synthetic fixtures under `src/`, `scripts/`, and `tests/`.
- Policy references/manifests and project pins. Shared current policy is owned by [ai-orchestration](https://github.com/i-pp/ai-orchestration); follow [AGENTS](../AGENTS.md) and the loader rather than duplicating its bodies. Preserve immutable legacy `policies.md` bytes.
- Reviewed `AGENTS.md`, `.agents/skills/`, and explicitly approved portable `.codex` project/role profiles, not the entire assistant-state directory.
- Resource/scheduler/verification defaults and sanitized examples, reviewed for the deployment's capacity.
- Documentation and sanitized phase reports, not raw logs, databases, or private host investigations.
- Portable systemd examples and clearly marked AppArmor drafts, never automatic host application.

Ignore runtimes, Codex homes, logs, SQLite/WAL, sockets/PIDs, caches, environments/dependencies, backups, generated artifacts, and temporary worktrees. Exclude credentials, keys, tokens, `.env`, local/private configuration, and private assistant state. Ignore rules are not content inspection: examples must contain synthetic values only. Private host/Codex investigations and unverified sharing candidates remain local.

## Tunnel examples versus operational configuration

Keep operational `systemd/aceis-mcp-tunnel.service` and `.yaml` Git-ignored. Use the `.example.service` and `.example.yaml` files as portable inputs, replace placeholders locally, and retain credential references outside Git. Preparing examples does not install or restart a service.

```yaml
# Example only: replace the placeholder locally before use.
config_version: 1
control_plane:
  base_url: "https://api.openai.com"
  tunnel_id: "REPLACE_WITH_YOUR_TUNNEL_ID"
health:
  listen_addr: "127.0.0.1:0"
admin_ui:
  open_browser: false
log:
  level: warn
  format: json
mcp:
  stdio_send_initialized_notification: true
```

If an operational file is already tracked, adding an ignore pattern does not remove it from the index. Inspect staged changes first and obtain authorization for any recoverable removal. `git rm --cached` leaves the working file but does not remove historical commits. History remediation is a separate reviewed operation. Connection identifiers are not authentication secrets, but still require a portability/privacy decision.

## Audit the actual candidate

`scripts/git_audit.py` has an exact allowlist for necessary policy/assistant configuration. Adding a new role/skill/source requires review of that allowlist as well as content, size, secret-pattern, binary, and index-conflict checks. Do not permit a whole private directory merely to admit one file.

```bash
git status --short
git ls-files --cached --ignored --exclude-standard
git ls-files --others --exclude-standard
git check-ignore --no-index -v .serena/project.yml .runtime/tasks.sqlite
git diff --cached --name-status
git diff --cached --check
python3 -B scripts/git_audit.py
```

Audit working files before adding them and actual staged blobs before an authorized commit. Report suspicious paths/categories without printing secret matches. Zero heuristic findings do not guarantee absence of every secret. Do not begin with `git add .` or `git add -f`; reconcile tracked exclusions and review each candidate first.

Use [path placeholders](path-conventions.md) in shared documents. Source/tests should derive repository paths rather than embed personal home directories. Coordinate edits to files owned by another worker; do not overwrite their implementation or stage unreviewed changes. Original logs and live configuration stay unchanged when documentation is anonymized.
