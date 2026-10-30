"""Inspect the exact staged blobs; output locations/rules, never matched values."""

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

FORBIDDEN = {".runtime", ".codex", "codex-home", "logs", "snapshots", "db-backups", "resource-events",
             "__pycache__", ".cache", ".venv", "node_modules", ".git", "backups", "secrets",
             ".serena", ".vscode", ".idea", ".aws", ".ssh", ".config", ".direnv", "credentials",
             ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", ".tox", ".nox", "tmp", ".tmp"}
ROOT_FILES = {"README.md", "AGENTS.md", ".gitignore", "resource-limits.json", "scheduler-config.json", "verification-commands.json"}
# Deliberate source exceptions, not a blanket exemption for assistant state.
# Review this set when adding another shared role/Skill/config source.
PORTABLE_CONFIG_FILES = {
    ".agents/skills/common-policy/SKILL.md",
    ".codex/config.toml",
    ".codex/agents/implementer.toml",
    ".codex/agents/verifier.toml",
    ".codex/agents/researcher.toml",
    ".codex/agents/independent_reviewer.toml",
    "config/policy.json",
    "policies.md",
    "policies/manifest.json",
    "policies/orchestration.md",
    "policies/decision-making.md",
    "policies/escalation.md",
}
LOCAL_ONLY_FILES = {
    "systemd/aceis-mcp-tunnel.service",
    "systemd/aceis-mcp-tunnel.yaml",
    "docs/config-change-investigation.md",
}
SOURCE_ROOTS = {"src", "tests", "docs", "examples", "scripts", "systemd"}
GENERATED_ROOTS = {"artifacts", "scratch", "worktrees", ".worktrees", "build", "dist", "htmlcov"}
PATTERNS = {
    "api_key": rb"\bsk-[A-Za-z0-9_-]{20,}",
    "github_token": rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    "jwt": rb"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    "private_key": rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\r?\n",
    "credential_assignment": rb"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password)\s*[:=]\s*[\"'][A-Za-z0-9_./+=-]{24,}[\"']",
    "unquoted_env_credential": rb"(?im)^[ \t]*(?:export[ \t]+)?[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|SECRET)[ \t]*=[ \t]*[A-Za-z0-9_./+=-]{24,}[ \t]*(?:\r?$)",
}


def path_allowed(path):
    parts = PurePosixPath(path).parts
    if not parts or path in LOCAL_ONLY_FILES or parts[0] in GENERATED_ROOTS:
        return False
    portable = path in PORTABLE_CONFIG_FILES
    if ((any(part in FORBIDDEN for part in parts) and not (portable and parts[0] == ".codex"))
            or any(part.endswith('.egg-info') for part in parts)):
        return False
    # Only sanitized environment examples at root or in examples/ are sources.
    env_example = (len(parts) == 1 or parts[0] == "examples") and bool(re.fullmatch(
        r"\.env(?:\.[A-Za-z0-9_-]+)?\.example", parts[-1]))
    sensitive_path = str(PurePosixPath(path).with_name('environment-example')) if env_example else path
    sensitive = re.search(
        r"(?i)(?:^|/)(?:\.env(?:\..*)?|\.envrc|auth\.json|credentials.*|id_rsa.*|id_ed25519.*|\.netrc|\.git-credentials|\.npmrc|\.pypirc)$"
        r"|\.(?:sqlite.*|db(?:-.*)?|log|jsonl|pem|key|p12|pfx|token|secret|pid(?:\.lock)?|pidfile|sock|socket|bak|backup|tmp|temp|zip|tar(?:\.gz)?)$"
        r"|(?:^|/)(?:core(?:\.[0-9]+)?|\.coverage(?:\..*)?|\.DS_Store)$"
        r"|(?:\.local|\.private)(?:[./]|$)|(?:\.env|\.swp|\.swo|\.code-workspace|~)$", sensitive_path)
    if sensitive:
        return False
    return portable or path in ROOT_FILES or env_example or parts[0] in SOURCE_ROOTS


def audit(root):
    root = Path(root).resolve()
    records = subprocess.check_output(["git", "-C", str(root), "ls-files", "--stage", "-z"]).split(b"\0")
    findings, files = [], []
    for record in records:
        if not record: continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, blob, stage = metadata.decode().split()
        path = raw_path.decode("utf-8", errors="strict")
        files.append(path)
        if mode not in ["100644", "100755"] or stage != "0":
            findings.append({"path": path, "rule": "nonregular_or_conflicted_index"})
            continue
        if not path_allowed(path):
            findings.append({"path": path, "rule": "forbidden_path_or_extension"})
        size = int(subprocess.check_output(["git", "-C", str(root), "cat-file", "-s", blob]))
        if size > 2 * 1024**2:
            findings.append({"path": path, "rule": "blob_size"}); continue
        content = subprocess.check_output(["git", "-C", str(root), "cat-file", "blob", blob])
        if b"\0" in content:
            findings.append({"path": path, "rule": "binary_blob"})
        for rule, pattern in PATTERNS.items():
            for match in re.finditer(pattern, content):
                findings.append({"path": path, "line": content[:match.start()].count(b"\n") + 1, "rule": rule})
    return {"passed": bool(files) and not findings, "staged_files": files, "findings": findings,
            "scope": "Staged blobs and paths; heuristic scan plus manual review required, no values printed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = audit(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
