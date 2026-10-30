"""Linux launch policy: isolated Codex home, read-only existing authentication."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from isolation_guard import require_isolation_creation


CONFIG = '''model = "gpt-6.1-sol"
model_provider = "openai"
forced_login_method = "chatgpt"
cli_auth_credentials_store = "file"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
[features]
apps = false
plugins = false
remote_plugin = false
hooks = false
shell_snapshot = false
memories = false
multi_agent = false
[analytics]
enabled = false
[feedback]
enabled = false
[history]
persistence = "none"
'''


def prepare(root, binary=None):
    root = Path(root).expanduser().resolve()
    binary = str(Path(binary or shutil.which("codex") or "/missing-codex").resolve())
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    for name in ["codex-home", "workspace", "tmp", "logs", "db-backups", "tasks.sqlite", "runner.lock", "codex-home/auth.json"]:
        if (root / name).is_symlink():
            raise ValueError(f"Symlink is not allowed for runtime state: {name}")
    for name in ["codex-home", "workspace", "tmp", "logs"]:
        (root / name).mkdir(exist_ok=True, mode=0o700)
    config = root / "codex-home/config.toml"
    if config.exists() and config.read_text() != CONFIG:
        raise ValueError("Isolated config differs from the fixed read-only policy")
    if not config.exists():
        config.write_text(CONFIG)
    instructions = root / "codex-home/AGENTS.md"
    if not instructions.exists():
        source = Path.home() / ".codex/AGENTS.md"
        if source.is_file():
            instructions.write_text(source.read_text())
    return root, binary


def launch(root, binary, args):
    """No token is opened by Python; a private mount binds auth without copying it."""
    require_isolation_creation('codex-namespace')
    root = Path(root).resolve()
    if not all(shutil.which(x) for x in ["unshare", "mount", "setpriv", "bwrap"]):
        raise FileNotFoundError("Linux namespace tools and bubblewrap are required; no unisolated fallback")
    auth = Path.home() / ".codex/auth.json"
    if not auth.is_file():
        raise FileNotFoundError("Existing ChatGPT file authentication is required")
    env = {k: os.environ[k] for k in [
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM",
        "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR"
    ] if k in os.environ}
    env.update({"CODEX_HOME": str(root / "codex-home"), "TMPDIR": str(root / "tmp")})
    argv = [shutil.which("unshare"), "--user", "--map-root-user", "--mount", "--pid",
            "--fork", "--kill-child=SIGKILL", "--mount-proc", sys.executable,
            str(Path(__file__).with_name("ns_entry.py")), str(root), binary, *args]
    return argv, env


def sandbox_preflight(root, timeout=15):
    probe, probe_env = launch(root, "/usr/bin/bwrap", [
        "--unshare-user", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--", "/usr/bin/true"
    ])
    check = subprocess.run(probe, env=probe_env, capture_output=True, timeout=timeout)
    if check.returncode:
        raise RuntimeError("Nested sandbox preflight failed; no model turn was started")


def identity(root, binary):
    sandbox_preflight(root)
    argv, env = launch(root, binary, ["login", "status"])
    auth = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=15)
    if auth.returncode or "Logged in using ChatGPT" not in auth.stdout + auth.stderr:
        raise RuntimeError("Existing ChatGPT subscription authentication is unavailable")
    version = binary_version(root, binary, timeout=10)
    return {"binary": binary, "version": version, "auth": "chatgpt", "sandbox": "read-only",
            "transport": "dedicated stdio", "codex_home": str(Path(root) / "codex-home")}


def isolated_launch(root, binary, args, *, access="read-only", authenticate=True):
    """Dedicated filesystem, with no coordinator or sibling workspace mounts."""
    require_isolation_creation('private-codex-namespace')
    root = Path(root).resolve()
    executable = Path(binary).resolve()
    if executable.is_relative_to(Path(__file__).resolve().parents[1] / ".runtime"):
        raise ValueError("Isolated executable must be a system program or the installed Codex binary; runtime executable refused")
    installed_codex = Path(shutil.which("codex") or "/missing-codex").resolve()
    version_only = not authenticate and args == ["--version"] and executable == installed_codex
    if not executable.is_relative_to("/usr") and not (executable == installed_codex and (authenticate or version_only)):
        raise ValueError("Isolated executable must be a system program or the installed Codex binary")
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(Path.home()),
           "CODEX_HOME": str(root / "codex-home"), "TMPDIR": "/tmp",
           "PYTHONDONTWRITEBYTECODE": "1"}
    argv = ["/usr/bin/unshare", "--user", "--map-root-user", "--mount", "--pid", "--ipc",
            "--fork", "--kill-child=SIGKILL", "--mount-proc"]
    if not authenticate:
        argv.append("--net")
    argv += [sys.executable, str(Path(__file__).with_name("isolated_entry.py")), str(root),
             access, "auth" if authenticate else "no-auth", binary, *args]
    return argv, env


def binary_version(root, binary, timeout=10):
    """Even metadata probes run without host config, credentials, or networking."""
    argv, env = isolated_launch(root, binary, ["--version"], authenticate=False)
    # no-auth replaces this dedicated directory with empty read-only tmpfs.
    env["HOME"] = env["CODEX_HOME"]
    return subprocess.run(argv, env=env, cwd=root, capture_output=True, text=True,
                          check=True, timeout=timeout).stdout.strip()


def isolated_preflight(root, access="read-only", timeout=15):
    argv, env = isolated_launch(root, "/usr/bin/bwrap", ["--unshare-user", "--ro-bind", "/", "/",
                       "--proc", "/proc", "--dev", "/dev", "--", "/usr/bin/true"], access=access)
    check = subprocess.run(argv, env=env, capture_output=True, timeout=timeout)
    if check.returncode:
        raise RuntimeError("Private filesystem/nested sandbox preflight failed; execution refused: "
                           + check.stderr.decode(errors="replace")[-1500:])
