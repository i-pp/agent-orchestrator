"""Internal entry point, executed only inside unshare's new mount/user/PID namespaces."""

import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(sys.argv[1]).resolve()
    auth_home = Path.home() / ".codex"
    target = root / "codex-home/auth.json"
    if target.is_symlink():
        raise RuntimeError("Authentication mountpoint must not be a symlink")
    target.touch(exist_ok=True)  # Empty mountpoint, never a credentials copy.

    def mount(*args):
        subprocess.run(["/usr/bin/mount", *args], check=True)

    mount("--make-rprivate", "/")
    mount("--bind", str(Path.home()), str(Path.home()))
    mount("--bind", str(root), str(root))
    mount("--bind", str(auth_home / "auth.json"), str(target))
    mount("-o", "remount,bind,ro", str(target))
    # Hide the existing home, including DBs, sessions and daemon sockets.
    mount("-t", "tmpfs", "-o", "ro,size=4k", "tmpfs", str(auth_home))
    mount("-t", "tmpfs", "-o", "nosuid,nodev", "tmpfs", "/tmp")
    # A same-UID model must not reach the host daemon's control socket or bus.
    mount("-t", "tmpfs", "-o", "ro,size=4k", "tmpfs", "/run/user")
    mount("-o", "remount,bind,ro", str(Path.home()))
    os.chdir(root / "workspace")
    # SETFCAP is needed for nested root UID mappings on modern kernels.
    # No SYS_ADMIN capability remains, so the mounted policy cannot be remounted.
    os.execv("/usr/bin/setpriv", ["setpriv", "--bounding-set=-all,+setuid,+setgid,+setfcap",
                                *sys.argv[2:]])


if __name__ == "__main__":
    main()
