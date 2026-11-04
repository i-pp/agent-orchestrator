"""Private filesystem bootstrap. Run only in a new user/mount/PID namespace.

The jail is a mount, never a persistent copy of the host filesystem. No source
directory descriptor or host cwd survives pivot_root. CAP_SYS_ADMIN/SYS_CHROOT are
dropped before starting any untrusted executable.
"""

import ctypes
import json
import os
import platform
from pathlib import Path
import re
import stat
import subprocess
import sys


def codex_install_layout(binary):
    """Resolve only the installed system binary or supported npm Codex layout.

    npm's public bin is commonly a symlink to @openai/codex/bin/codex.js. Resolve
    that link once, validate the canonical package relationship, then use this
    exact executable both for the mount plan and exec. Manifest metadata checks
    detect layout mismatches; they do not authenticate the installed package.
    """
    try:
        executable = Path(binary).resolve(strict=True)
        info = executable.stat()
    except (OSError, RuntimeError, ValueError):
        raise ValueError("Codex executable is unavailable") from None
    if not stat.S_ISREG(info.st_mode) or not os.access(executable, os.X_OK):
        raise ValueError("Codex executable is not a regular executable")

    npm_layout = (executable.name == "codex.js" and executable.parent.name == "bin"
                  and executable.parent.parent.name == "codex"
                  and executable.parent.parent.parent.name == "@openai"
                  and executable.parent.parent.parent.parent.name == "node_modules")
    if not npm_layout:
        if executable.is_relative_to("/usr"):
            return {"executable": executable, "package_root": None, "dependency": None,
                    "dependency_name": None}
        raise ValueError("Installed Codex executable has an unsupported package layout")

    package_root = executable.parent.parent
    manifest_path = package_root / "package.json"
    modules = package_root / "node_modules"
    scope = modules / "@openai"
    for path, want_directory in [(package_root, True), (manifest_path, False),
                                 (modules, True), (scope, True)]:
        try:
            mode = path.lstat().st_mode
        except OSError:
            raise ValueError("Installed Codex package is incomplete") from None
        correct_type = stat.S_ISDIR(mode) if want_directory else stat.S_ISREG(mode)
        if stat.S_ISLNK(mode) or not correct_type:
            raise ValueError("Installed Codex package contains an unsafe path")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Installed Codex package metadata is invalid") from None
    if not isinstance(manifest, dict) or manifest.get("name") != "@openai/codex":
        raise ValueError("Installed Codex package identity is invalid")
    bin_map = manifest.get("bin")
    if not isinstance(bin_map, dict) or bin_map.get("codex") != "bin/codex.js":
        raise ValueError("Installed Codex executable does not match package metadata")
    version = manifest.get("version")
    if not isinstance(version, str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version):
        raise ValueError("Installed Codex package version is invalid")

    targets = {
        "x86_64": ("@openai/codex-linux-x64", "x64", "x86_64-unknown-linux-musl", "linux-x64"),
        "aarch64": ("@openai/codex-linux-arm64", "arm64", "aarch64-unknown-linux-musl", "linux-arm64"),
    }
    target = targets.get(platform.machine()) if platform.system() == "Linux" else None
    if target is None:
        raise ValueError("Installed Codex platform is unsupported")
    dependency_name, cpu_name, target_triple, package_suffix = target
    expected_spec = f"npm:@openai/codex@{version}-{package_suffix}"
    optional = manifest.get("optionalDependencies")
    if not isinstance(optional, dict) or optional.get(dependency_name) != expected_spec:
        raise ValueError("Installed Codex platform dependency is not declared")

    dependency = scope / dependency_name.rsplit("/", 1)[1]
    try:
        dependency_mode = dependency.lstat().st_mode
        dependency_manifest_path = dependency / "package.json"
        dependency_manifest_mode = dependency_manifest_path.lstat().st_mode
        dependency_manifest = json.loads(dependency_manifest_path.read_text(encoding="utf-8"))
        vendor_binary = dependency / "vendor" / target_triple / "bin" / "codex"
        vendor_mode = vendor_binary.lstat().st_mode
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Installed Codex platform dependency is incomplete") from None
    if (stat.S_ISLNK(dependency_mode) or not stat.S_ISDIR(dependency_mode)
            or stat.S_ISLNK(dependency_manifest_mode) or not stat.S_ISREG(dependency_manifest_mode)
            or stat.S_ISLNK(vendor_mode) or not stat.S_ISREG(vendor_mode)
            or not os.access(vendor_binary, os.X_OK)):
        raise ValueError("Installed Codex platform dependency contains an unsafe path")
    dep_os = dependency_manifest.get("os") if isinstance(dependency_manifest, dict) else None
    dep_cpu = dependency_manifest.get("cpu") if isinstance(dependency_manifest, dict) else None
    if (not isinstance(dep_os, list) or not isinstance(dep_cpu, list)
            or dependency_manifest.get("name") != "@openai/codex"
            or dependency_manifest.get("version") != f"{version}-{package_suffix}"
            or "linux" not in dep_os or cpu_name not in dep_cpu):
        raise ValueError("Installed Codex platform dependency metadata does not match")
    try:
        if dependency.resolve(strict=True) != dependency or vendor_binary.resolve(strict=True) != vendor_binary:
            raise ValueError("Installed Codex platform dependency resolves outside its package")
    except (OSError, RuntimeError):
        raise ValueError("Installed Codex platform dependency is unavailable") from None

    return {"executable": executable, "package_root": package_root,
            "dependency": dependency, "dependency_name": dependency.name}


def validate_codex_node(layout, node_binary):
    """Validate the one host Node executable required by the npm wrapper."""
    if layout["package_root"] is None:
        if node_binary is not None:
            raise ValueError("Unexpected Node.js runtime for a system Codex executable")
        return None
    if not node_binary:
        raise ValueError("Node.js runtime required by installed npm Codex is unavailable")
    try:
        node = Path(node_binary).resolve(strict=True)
        info = node.stat()
    except (OSError, RuntimeError, ValueError):
        raise ValueError("Node.js runtime required by installed npm Codex is unavailable") from None
    if not node.is_absolute() or not stat.S_ISREG(info.st_mode) or not os.access(node, os.X_OK):
        raise ValueError("Node.js runtime required by installed npm Codex is not executable")
    return node


def mount_codex_node(node, root, bind):
    """Expose only an external Node executable; /usr is already mounted read-only."""
    if node is None or node.is_relative_to("/usr"):
        return
    if node.is_relative_to(root):
        raise ValueError("Node.js runtime cannot come from the isolated runtime tree")
    bind(node, node)


def mount_codex_package(layout, target, mount, bind):
    """Expose the npm package read-only and mask every sibling dependency."""
    package_root = layout["package_root"]
    if package_root is None:
        return
    bind(package_root, package_root)
    modules = package_root / "node_modules"
    modules_target = target(modules)
    if modules_target.is_symlink() or not modules_target.is_dir():
        raise ValueError("Installed Codex dependency mountpoint is unavailable")
    mount("-t", "tmpfs", "-o", "mode=0755,nosuid,nodev,noexec,size=64k", "tmpfs", modules_target)
    scope_target = target(modules / "@openai")
    scope_target.mkdir(mode=0o755, parents=True, exist_ok=True)
    dependency_target = modules / "@openai" / layout["dependency_name"]
    bind(layout["dependency"], dependency_target)
    mount("-o", "remount,ro,nosuid,nodev,noexec", modules_target)


def main():
    root = Path(sys.argv[1]).resolve()
    access, auth, binary = sys.argv[2:5]
    if access not in ["read-only", "workspace-write"] or auth not in ["auth", "no-auth"]:
        raise ValueError("Invalid isolated launch policy")
    layout = codex_install_layout(binary)
    binary = str(layout["executable"])
    node = validate_codex_node(layout, os.environ.pop("ACEIS_CODEX_NODE", None))

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
    mount_codex_package(layout, target, mount, bind)
    mount_codex_node(node, root, bind)
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
    command = [binary, *sys.argv[5:]]
    if node is not None:
        # The npm bin uses /usr/bin/env node. Invoke the one validated Node
        # binary directly so its host-only parent directory need not enter PATH.
        command = [str(node), binary, *sys.argv[5:]]
    os.execv("/usr/bin/setpriv", ["setpriv", "--bounding-set=-all,+setuid,+setgid,+setfcap",
                                *command])


if __name__ == "__main__":
    main()
