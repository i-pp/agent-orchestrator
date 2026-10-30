"""Private filesystem bootstrap. Run only in a new user/mount/PID namespace.

The jail is a mount, never a persistent copy of the host filesystem. No source
directory descriptor or host cwd survives pivot_root. CAP_SYS_ADMIN/SYS_CHROOT are
dropped before starting any untrusted executable.
"""

import ctypes
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(sys.argv[1]).resolve()
    access, auth, binary = sys.argv[2:5]
    if access not in ["read-only", "workspace-write"] or auth not in ["auth", "no-auth"]:
        raise ValueError("Invalid isolated launch policy")

    def mount(*args):
        subprocess.run(["/usr/bin/mount", *map(str, args)], check=True, stdout=subprocess.DEVNULL)

    mount("--make-rprivate", "/")
    jail = root / "tmp/jail"
    if jail.is_symlink():
        raise ValueError("Unsafe jail mountpoint")
    jail.mkdir(exist_ok=True)
    mount("-t", "tmpfs", "-o", "nosuid,nodev,size=32m", "tmpfs", jail)

    def target(path):
        return jail / str(path).lstrip("/")

    def bind(source, destination, readonly=True):
        destination = target(destination)
        if Path(source).is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.touch()
        mount("--bind", source, destination)
        if readonly:
            mount("-o", "remount,bind,ro,nosuid,nodev", destination)

    for name in ["usr", "etc"]:
        bind("/" + name, "/" + name)
    for name in ["bin", "sbin", "lib", "lib64"]:
        source = Path("/" + name)
        if source.is_symlink():
            target(source).symlink_to(os.readlink(source))
        elif source.exists():
            bind(source, source)
    # Only this session's state is visible; coordinator DB, logs and other
    # sessions are absent. Non-recursive bind does not carry the jail submount.
    bind(root, root)
    bind(root / "codex-home", root / "codex-home", False)
    bind(root / "workspace", root / "workspace", access == "read-only")
    executable = Path(binary).resolve()
    if not str(executable).startswith("/usr/"):
        bind(executable.parent, executable.parent)
    if auth == "auth":
        auth_target = root / "codex-home/auth.json"
        if auth_target.is_symlink():
            raise ValueError("Unsafe authentication mountpoint")
        auth_target.touch(exist_ok=True)
        bind(Path.home() / ".codex/auth.json", auth_target)
    else:
        # Verifiers cannot inspect private Codex state or credentials.
        mount("-t", "tmpfs", "-o", "ro,size=4k", "tmpfs", target(root / "codex-home"))
    for name in ["proc", "dev", "tmp", "run", "var/tmp", "sys"]:
        target(name).mkdir(parents=True, exist_ok=True)
    mount("-t", "proc", "-o", "nosuid,nodev,noexec", "proc", target("proc"))
    mount("-t", "tmpfs", "-o", "nosuid,noexec,size=4m", "tmpfs", target("dev"))
    for name in ["null", "zero", "full", "tty", "random", "urandom"]:
        bind("/dev/" + name, "/dev/" + name, False)
    for name, source in [("fd", "/proc/self/fd"), ("stdin", "/proc/self/fd/0"),
                         ("stdout", "/proc/self/fd/1"), ("stderr", "/proc/self/fd/2")]:
        target("dev/" + name).symlink_to(source)
    # /etc/resolv.conf may point into /run; expose only its resolved file.
    resolver = Path("/etc/resolv.conf").resolve()
    if resolver.is_relative_to("/run"):
        bind(resolver, resolver)
    mount("-t", "tmpfs", "-o", "nosuid,nodev,size=64m", "tmpfs", target("tmp"))
    mount("-t", "tmpfs", "-o", "nosuid,nodev,size=16m", "tmpfs", target("var/tmp"))
    target("oldroot").mkdir()
    mount("-o", "remount,bind,ro,nosuid,nodev", jail)
    os.chdir(jail)
    # A chroot cannot create nested user namespaces. pivot_root also removes
    # the host mount tree, instead of leaving it reachable outside a chroot.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.pivot_root(b".", b"oldroot") != 0:
        raise OSError(ctypes.get_errno(), "pivot_root failed")
    os.chdir("/")
    if libc.umount2(b"/oldroot", 2) != 0:  # MNT_DETACH
        raise OSError(ctypes.get_errno(), "Old root detach failed")
    os.chdir(root / "workspace")
    os.execv("/usr/bin/setpriv", ["setpriv", "--bounding-set=-all,+setuid,+setgid,+setfcap",
                                binary, *sys.argv[5:]])


if __name__ == "__main__":
    main()
