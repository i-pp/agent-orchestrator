#!/usr/bin/env python3
"""One-shot host validation for npm Codex isolation; never reads real auth."""

import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
import isolation_guard


def emit(name, value):
    print(f"{name}={value}", flush=True)


def safe_failure_class(exc):
    """Classify captured failures without returning command output or paths."""
    streams = []
    if isinstance(exc, subprocess.CalledProcessError):
        streams.extend([exc.stdout, exc.stderr])
    elif isinstance(exc, subprocess.TimeoutExpired):
        streams.extend([exc.stdout, exc.stderr])
    else:
        streams.append(str(exc))
    detail = " ".join(
        item.decode(errors="replace") if isinstance(item, bytes) else str(item or "")
        for item in streams
    ).lower()
    if "error while loading shared libraries" in detail:
        return "MISSING_SHARED_LIBRARY"
    if "err_module_not_found" in detail or "cannot find package" in detail:
        return "MISSING_NPM_DEPENDENCY"
    if ("node: not found" in detail
            or ("no such file or directory" in detail and "node" in detail)):
        return "NODE_RUNTIME_OR_EXECUTABLE_MISSING"
    if "operation not permitted" in detail or "permission denied" in detail:
        return "NAMESPACE_OR_CAPABILITY_DENIED"
    if "bwrap" in detail or "bubblewrap" in detail:
        return "SANDBOX_SETUP_FAILURE"
    if "no such file or directory" in detail:
        return "MISSING_EXECUTABLE_OR_LIBRARY"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "TIMEOUT"
    if isinstance(exc, subprocess.CalledProcessError) and exc.returncode < 0:
        return "SIGNAL_TERMINATION"
    if isinstance(exc, subprocess.CalledProcessError):
        return "PROCESS_EXIT"
    return "UNCLASSIFIED"


def safe_process_failure(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        return f"exit={exc.returncode}, class={safe_failure_class(exc)}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"class={safe_failure_class(exc)}"
    if isinstance(exc, FileNotFoundError):
        return "class=MISSING_EXECUTABLE_OR_LIBRARY"
    return f"class={safe_failure_class(exc)}"


def canary_failure_details(result):
    stages = {"START", "PACKAGE_VISIBLE", "PACKAGE_ALLOWLIST_OK", "PACKAGE_READONLY_OK",
              "WORKSPACE_READONLY_OK", "AUTH_HIDDEN", "SECRET_HIDDEN", "SOCKET_PATH_HIDDEN",
              "HOST_NETWORK_BLOCKED", "PASS"}
    failures = {"PACKAGE_LAYOUT", "PACKAGE_ALLOWLIST", "PACKAGE_READONLY",
                "WORKSPACE_READONLY", "MASK_MOUNT_FLAGS", "DEPENDENCY_MOUNT_FLAGS",
                "AUTH_VISIBLE", "SECRET_VISIBLE", "DAEMON_SOCKET_VISIBLE", "RUN_USER_VISIBLE",
                "HOST_SOCKET_REACHABLE", "LOOPBACK_REACHABLE"}
    marker = "NO_CANARY_MARKER"
    failure = "NONE"
    for line in result.stdout.splitlines():
        if line.startswith("ACEIS_STAGE=") and line.partition("=")[2] in stages:
            marker = line.partition("=")[2]
        elif line.startswith("ACEIS_FAIL=") and line.partition("=")[2] in failures:
            failure = line.partition("=")[2]
    error = subprocess.CalledProcessError(result.returncode, "inner-canary",
                                          output=result.stdout, stderr=result.stderr)
    return (f"exit={result.returncode}, stage={marker}, check={failure}, "
            f"class={safe_failure_class(error)}")


def regression_failure_summary(result):
    failed = []
    for line in result.stderr.splitlines():
        if line.startswith(("FAIL: ", "ERROR: ")):
            name = line.partition(": ")[2].split(" ", 1)[0]
            if name.startswith("test_") and name.replace("_", "").isalnum():
                failed.append(name)
    suffix = f"; failed={','.join(failed[:3])}" if failed else ""
    return f"FAIL (exit {result.returncode}{suffix})"


def add_network_namespace(argv, python_executable):
    """Reduce network access for the synthetic outer-auth canary process."""
    argv = list(argv)
    argv.insert(argv.index(python_executable), "--net")
    return argv


def run_runner_check_gate(isolation, root, binary):
    """Run the real check flow through preflight, intercepting login status."""
    original_run = subprocess.run
    original_preflight = isolation.sandbox_preflight
    state = {"preflight": False, "auth_gate": False, "unexpected": False,
             "auth_before_preflight": False}

    class SyntheticAuthGateReached(Exception):
        pass

    class UnexpectedPostPreflightProcess(Exception):
        pass

    def tracked_preflight(path, timeout=15):
        result = original_preflight(path, timeout)
        state["preflight"] = True
        return result

    def intercept(argv, *args, **kwargs):
        values = [str(item) for item in argv]
        if values[-2:] == ["login", "status"]:
            if not state["preflight"]:
                state["auth_before_preflight"] = True
            state["auth_gate"] = True
            raise SyntheticAuthGateReached()
        if state["preflight"]:
            state["unexpected"] = True
            raise UnexpectedPostPreflightProcess()
        return original_run(argv, *args, **kwargs)

    try:
        with patch.object(isolation, "sandbox_preflight", tracked_preflight), \
             patch.object(subprocess, "run", intercept):
            isolation.identity(root, binary)
    except SyntheticAuthGateReached:
        return ("PASS" if state["preflight"] and state["auth_gate"]
                and not state["auth_before_preflight"] else "FAIL")
    except Exception as exc:
        return f"FAIL ({type(exc).__name__})" if not state["preflight"] else "FAIL (unexpected stage)"
    if state["unexpected"]:
        return "FAIL (unexpected post-preflight process was blocked)"
    return "FAIL (auth stage was not intercepted)"


def outer_auth_canary_code(runtime, auth_text, secret_path, socket_path,
                           abstract_name, loopback_port):
    auth_path = runtime / "codex-home/auth.json"
    workspace = runtime / "workspace"
    return textwrap.dedent(f"""\
        from pathlib import Path
        import re
        import socket

        auth = Path({str(auth_path)!r})
        expected = {auth_text!r}
        def unescape(value):
            return re.sub(r'\\\\([0-7]{{3}})', lambda match: chr(int(match.group(1), 8)), value)
        def options_for(path):
            found = []
            target = Path(path)
            for line in Path('/proc/self/mountinfo').read_text().splitlines():
                fields = line.split()
                if len(fields) > 5:
                    mountpoint = Path(unescape(fields[4]))
                    if target == mountpoint or mountpoint in target.parents:
                        found.append((len(str(mountpoint)), set(fields[5].split(','))))
            return max(found, default=(0, set()), key=lambda item: item[0])[1]

        assert auth.read_text() == expected
        assert 'ro' in options_for(auth)
        assert 'ro' in options_for(Path({str(workspace)!r}))
        try:
            auth.write_text('synthetic mutation')
        except OSError:
            pass
        else:
            raise AssertionError('synthetic auth mount accepted a write')
        assert auth.read_text() == expected
        assert not Path({str(secret_path)!r}).exists()
        assert not Path({str(socket_path)!r}).exists()
        assert not Path('/run/user').exists()

        def blocked(family, address):
            client = socket.socket(family, socket.SOCK_STREAM)
            client.settimeout(0.5)
            try:
                client.connect(address)
            except OSError:
                return
            finally:
                client.close()
            raise AssertionError('synthetic host socket was reachable')

        blocked(socket.AF_UNIX, {abstract_name!r})
        blocked(socket.AF_INET, ('127.0.0.1', {loopback_port}))
        print('ACEIS_OUTER_CANARIES_PASS')
    """)


def inner_codex_canary_code(layout, runtime, secret_path, socket_path,
                            abstract_name, loopback_port):
    package_root = layout["package_root"]
    dependency_name = layout["dependency_name"]
    auth_path = runtime / "codex-home/auth.json"
    return textwrap.dedent(f"""\
        from pathlib import Path
        import re
        import socket

        def mark(stage):
            print('ACEIS_STAGE=' + stage, flush=True)
        def require(condition, check):
            if not condition:
                print('ACEIS_FAIL=' + check, flush=True)
                raise SystemExit(24)

        mark('START')
        package = Path({str(package_root)!r})
        modules = package / 'node_modules'
        dependency = modules / '@openai' / {dependency_name!r}
        require(package.is_dir() and dependency.is_dir(), 'PACKAGE_LAYOUT')
        mark('PACKAGE_VISIBLE')
        require(sorted(item.name for item in modules.iterdir()) == ['@openai'] and
                sorted(item.name for item in (modules / '@openai').iterdir()) == [{dependency_name!r}],
                'PACKAGE_ALLOWLIST')
        mark('PACKAGE_ALLOWLIST_OK')

        def unescape(value):
            return re.sub(r'\\\\([0-7]{{3}})', lambda match: chr(int(match.group(1), 8)), value)

        def options_for(path):
            found = []
            target = Path(path)
            for line in Path('/proc/self/mountinfo').read_text().splitlines():
                fields = line.split()
                if len(fields) > 5:
                    mountpoint = Path(unescape(fields[4]))
                    if target == mountpoint or mountpoint in target.parents:
                        found.append((len(str(mountpoint)), set(fields[5].split(','))))
            return max(found, default=(0, set()), key=lambda item: item[0])[1]

        require('ro' in options_for(package), 'PACKAGE_READONLY')
        mark('PACKAGE_READONLY_OK')
        require('ro' in options_for(Path.cwd()), 'WORKSPACE_READONLY')
        mark('WORKSPACE_READONLY_OK')
        modules_options = options_for(modules)
        require('ro' in modules_options and 'noexec' in modules_options, 'MASK_MOUNT_FLAGS')
        dependency_options = options_for(dependency)
        require('ro' in dependency_options and 'noexec' not in dependency_options,
                'DEPENDENCY_MOUNT_FLAGS')

        auth = Path({str(auth_path)!r})
        try:
            auth.read_bytes()
        except OSError:
            pass
        else:
            require(False, 'AUTH_VISIBLE')
        mark('AUTH_HIDDEN')
        require(not Path({str(secret_path)!r}).exists(), 'SECRET_VISIBLE')
        mark('SECRET_HIDDEN')
        require(not Path({str(socket_path)!r}).exists(), 'DAEMON_SOCKET_VISIBLE')
        require(not Path('/run/user').exists(), 'RUN_USER_VISIBLE')
        mark('SOCKET_PATH_HIDDEN')

        def cannot_connect(family, sock_type, address):
            client = socket.socket(family, sock_type)
            client.settimeout(0.5)
            try:
                client.connect(address)
            except OSError:
                return
            finally:
                client.close()
            raise AssertionError('synthetic host socket was reachable')

        try:
            cannot_connect(socket.AF_UNIX, socket.SOCK_STREAM, {abstract_name!r})
        except AssertionError:
            require(False, 'HOST_SOCKET_REACHABLE')
        try:
            cannot_connect(socket.AF_INET, socket.SOCK_STREAM, ('127.0.0.1', {loopback_port}))
        except AssertionError:
            require(False, 'LOOPBACK_REACHABLE')
        mark('HOST_NETWORK_BLOCKED')
        mark('PASS')
        print('ACEIS_INNER_CANARIES_PASS', flush=True)
    """)


def main():
    context = isolation_guard.environment()
    try:
        sysctl = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns").read_text().strip()
    except OSError:
        sysctl = "UNKNOWN"
    try:
        apparmor = Path("/sys/module/apparmor/parameters/enabled").read_text().strip()
    except OSError:
        apparmor = "UNKNOWN"
    emit("HOST_CONTEXT", context["state"])
    emit("APPARMOR_ENABLED", apparmor)
    emit("APPARMOR_RESTRICT_UNPRIVILEGED_USERNS", sysctl)
    if context["state"] != "HOST_CONTEXT":
        emit("INTEGRATION", "BLOCKED (no namespaces or temporary fixtures created)")
        return 2

    import isolation
    import isolated_entry
    from runner import Runner
    if (PROJECT / ".runtime").is_symlink():
        emit("TEMP_RUNTIME", "BLOCKED (project .runtime is a symlink)")
        return 2
    runtime_parent = PROJECT / ".runtime"
    created_runtime_parent = not runtime_parent.exists()
    try:
        runtime_parent.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        emit("TEMP_RUNTIME", f"BLOCKED ({type(exc).__name__})")
        return 2

    overall = []
    runtime_temp = None
    socket_temp = None
    runtime_base = None
    runtime = None
    synthetic_home = None
    auth_path = None
    synthetic_auth = '{"tokens":{"access_token":"SYNTHETIC_CANARY_ONLY"}}'
    secret_path = None
    socket_path = None
    abstract_name = None
    servers = []
    runner_instance = None

    try:
        runtime_temp = tempfile.TemporaryDirectory(prefix="npm-isolation-host-", dir=runtime_parent)
        socket_temp = tempfile.TemporaryDirectory(prefix="aceis-sock-")
        runtime_base = Path(runtime_temp.name)
        runtime = runtime_base / "runner"
        synthetic_home = runtime_base / "synthetic-home"
        codex_dir = synthetic_home / ".codex"
        codex_dir.mkdir(parents=True, mode=0o700)
        auth_path = codex_dir / "auth.json"
        auth_path.write_text(synthetic_auth, encoding="utf-8")
        auth_path.chmod(0o600)
        secret_path = synthetic_home / ".ssh" / "id_ed25519"
        secret_path.parent.mkdir(mode=0o700)
        secret_path.write_text("SYNTHETIC_HOST_SECRET_CANARY", encoding="utf-8")
        secret_path.chmod(0o600)
        socket_path = Path(socket_temp.name) / "daemon.sock"
        abstract_name = "\0aceis-" + secrets.token_hex(8)

        binary_link = shutil.which("codex")
        if not binary_link:
            emit("CODEX_PACKAGE", "FAIL (Codex CLI not found)")
            overall.append("FAIL")
            layout = None
        else:
            try:
                layout = isolated_entry.codex_install_layout(binary_link)
                manifest = json.loads((layout["package_root"] / "package.json").read_text()) \
                    if layout["package_root"] else {}
                version = manifest.get("version", "system")
                if layout["package_root"] is None:
                    emit("CODEX_PACKAGE", "BLOCKED (installed Codex is not the npm package layout)")
                    overall.append("BLOCKED")
                else:
                    emit("CODEX_PACKAGE", f"PASS (version={version}, dependency={layout['dependency_name']})")
                    overall.append("PASS")
            except Exception as exc:
                layout = None
                emit("CODEX_PACKAGE", f"FAIL ({type(exc).__name__})")
                overall.append("FAIL")

        if layout and layout["package_root"] is not None:
            try:
                node = isolated_entry.validate_codex_node(layout, shutil.which("node"))
                node_scope = "system-root" if node.is_relative_to("/usr") else "single-file-bind"
                emit("CODEX_NODE_RUNTIME", f"PASS ({node_scope})")
                overall.append("PASS")
            except Exception:
                emit("CODEX_NODE_RUNTIME", "FAIL (missing-or-invalid)")
                overall.append("FAIL")
        elif layout:
            emit("CODEX_NODE_RUNTIME", "NOT_REQUIRED (system Codex executable)")
            overall.append("PASS")
        else:
            emit("CODEX_NODE_RUNTIME", "BLOCKED (Codex package unavailable)")
            overall.append("BLOCKED")

        # All CLI and test subprocesses receive a synthetic HOME. No host auth
        # file, global Codex config, API key, proxy credential, or user socket is
        # copied into the fixture environment.
        safe_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": str(synthetic_home), "LANG": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": "/tmp"}
        with patch.dict(os.environ, safe_env, clear=True):
            runtime.mkdir(mode=0o700)
            runner_instance = Runner(runtime, binary_link)
            binary = runner_instance.binary

            try:
                version_output = isolation.binary_version(runtime, binary, timeout=25)
                if not version_output.startswith("codex-cli "):
                    raise RuntimeError("unexpected version output")
                emit("ISOLATED_CODEX_VERSION", f"PASS ({version_output})")
                overall.append("PASS")
            except Exception as exc:
                emit("ISOLATED_CODEX_VERSION", f"FAIL ({safe_process_failure(exc)})")
                overall.append("FAIL")

            # Start harmless local canaries only; no system daemon is contacted.
            socket_setup = "PASS"
            try:
                file_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                servers.append(file_socket)
                file_socket.bind(str(socket_path))
                file_socket.listen(1)
                abstract_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                servers.append(abstract_socket)
                abstract_socket.bind(abstract_name)
                abstract_socket.listen(1)
                loopback_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                servers.append(loopback_socket)
                loopback_socket.bind(("127.0.0.1", 0))
                loopback_socket.listen(1)
                loopback_port = loopback_socket.getsockname()[1]
            except OSError as exc:
                socket_setup = f"BLOCKED ({type(exc).__name__})"
                for server in servers:
                    server.close()
                servers = []
                loopback_port = 0
            emit("SYNTHETIC_SOCKET_SETUP", socket_setup)

            # Test the same preflight stage as `runner.py check`, then stop
            # immediately before any login-status subprocess can execute.
            gate = run_runner_check_gate(isolation, runtime, binary)
            if gate == "PASS":
                emit("RUNNER_CHECK_PREFLIGHT", "PASS")
                emit("RUNNER_CHECK_AUTH_STAGE", "REACHED_BUT_NOT_RUN (synthetic HOME only)")
                overall.append("PASS")
            else:
                emit("RUNNER_CHECK_PREFLIGHT", gate)
                emit("RUNNER_CHECK_AUTH_STAGE", "NOT_REACHED_OR_INTERCEPTED")
                overall.append("FAIL")

            if socket_setup == "PASS":
                # The direct Python probe uses synthetic auth and an ephemeral
                # network namespace so it cannot contact a model or host socket.
                try:
                    probe = outer_auth_canary_code(runtime, synthetic_auth, secret_path,
                                                   socket_path, abstract_name, loopback_port)
                    compile(probe, "<outer synthetic canary>", "exec")
                    argv, env = isolation.isolated_launch(
                        runtime, "/usr/bin/python3", ["-B", "-c", probe], authenticate=True)
                    argv = add_network_namespace(argv, sys.executable)
                    result = subprocess.run(argv, env=env, cwd=runtime, capture_output=True,
                                            text=True, timeout=25, check=False)
                    if result.returncode != 0 or "ACEIS_OUTER_CANARIES_PASS" not in result.stdout:
                        raise RuntimeError(f"probe exit {result.returncode}")
                    if auth_path.read_text(encoding="utf-8") != synthetic_auth:
                        raise RuntimeError("synthetic auth canary changed")
                    emit("OUTER_AUTH_SECRET_SOCKET_RO_CANARIES", "PASS")
                    overall.append("PASS")
                except Exception as exc:
                    emit("OUTER_AUTH_SECRET_SOCKET_RO_CANARIES", f"FAIL ({type(exc).__name__})")
                    overall.append("FAIL")

                # Exercise the actual npm wrapper and Codex-provided sandbox
                # with a local Python canary; this does not invoke a model.
                if layout and layout["package_root"] is not None:
                    inner_result = None
                    try:
                        inner_probe = inner_codex_canary_code(
                            layout, runtime, secret_path, socket_path, abstract_name, loopback_port)
                        argv, env = isolation.isolated_launch(
                            runtime, binary,
                            ["sandbox", "--", "/usr/bin/python3", "-B", "-c", inner_probe],
                            access="read-only", authenticate=True)
                        result = subprocess.run(argv, env=env, cwd=runtime, capture_output=True,
                                                text=True, timeout=35, check=False)
                        inner_result = result
                        if result.returncode == 0 and "ACEIS_INNER_CANARIES_PASS" in result.stdout:
                            emit("CODEX_INNER_PACKAGE_RO_AUTH_SECRET_SOCKET_CANARIES", "PASS")
                            overall.append("PASS")
                        else:
                            emit("CODEX_INNER_PACKAGE_RO_AUTH_SECRET_SOCKET_CANARIES",
                                 f"FAIL ({canary_failure_details(result)})")
                            overall.append("FAIL")
                    except Exception as exc:
                        detail = (canary_failure_details(inner_result) if inner_result is not None
                                  else safe_process_failure(exc))
                        emit("CODEX_INNER_PACKAGE_RO_AUTH_SECRET_SOCKET_CANARIES", f"FAIL ({detail})")
                        overall.append("FAIL")
                else:
                    emit("CODEX_INNER_PACKAGE_RO_AUTH_SECRET_SOCKET_CANARIES", "BLOCKED (npm layout unavailable)")
                    overall.append("BLOCKED")
            else:
                emit("OUTER_AUTH_SECRET_SOCKET_RO_CANARIES", "BLOCKED")
                emit("CODEX_INNER_PACKAGE_RO_AUTH_SECRET_SOCKET_CANARIES", "BLOCKED")
                overall.extend(["BLOCKED", "BLOCKED"])

            # The fail-closed path is covered offline by these suites; they are
            # run with synthetic HOME and a credential-free environment.
            test_env = dict(safe_env)
            for label, pattern in [
                    ("ISOLATED_ENTRY_REGRESSION", "test_isolated_entry.py"),
                    ("VERSION_ISOLATION_REGRESSION", "test_version_isolation.py"),
                    ("ISOLATION_GUARD_REGRESSION", "test_isolation_guard.py"),
                    ("RUNNER_REGRESSION", "test_runner.py"),
                    ("HOST_DIAGNOSTICS_REGRESSION", "test_host_validation_diagnostics.py")]:
                try:
                    test_result = subprocess.run(
                        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests",
                         "-p", pattern], cwd=PROJECT, env=test_env, capture_output=True,
                        text=True, timeout=180, check=False)
                    state = "PASS" if test_result.returncode == 0 else regression_failure_summary(test_result)
                except subprocess.TimeoutExpired:
                    state = "FAIL (timeout)"
                except Exception as exc:
                    state = f"FAIL ({type(exc).__name__})"
                emit(label, state)
                overall.append("PASS" if state == "PASS" else "FAIL")

            if auth_path.read_text(encoding="utf-8") != synthetic_auth:
                emit("SYNTHETIC_AUTH_UNCHANGED", "FAIL")
                overall.append("FAIL")
            else:
                emit("SYNTHETIC_AUTH_UNCHANGED", "PASS")
                overall.append("PASS")
    except Exception as exc:
        emit("HARNESS", f"FAIL ({type(exc).__name__})")
        overall.append("FAIL")
    finally:
        if runner_instance is not None:
            try:
                runner_instance.store.close()
            except Exception:
                overall.append("FAIL")
        for server in servers:
            server.close()
        cleanup_ok = True
        if socket_path is not None:
            try:
                socket_path.unlink(missing_ok=True)
            except OSError:
                cleanup_ok = False
        if socket_temp is not None:
            try:
                socket_temp.cleanup()
                cleanup_ok = cleanup_ok and not Path(socket_temp.name).exists()
            except OSError:
                cleanup_ok = False
        if runtime_temp is not None:
            try:
                runtime_temp.cleanup()
                cleanup_ok = cleanup_ok and runtime_base is not None and not runtime_base.exists()
            except OSError:
                cleanup_ok = False
        emit("TEMPORARY_CLEANUP", "PASS" if cleanup_ok else "FAIL (temporary path remains)")
        if not cleanup_ok:
            emit("CLEANUP_HINT", "inspect .runtime/npm-isolation-host-* and /tmp/aceis-sock-* after all child namespaces exit")
        overall.append("PASS" if cleanup_ok else "FAIL")
        if created_runtime_parent:
            try:
                runtime_parent.rmdir()
            except OSError:
                pass
        try:
            sysctl_after = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns").read_text().strip()
        except OSError:
            sysctl_after = "UNKNOWN"
        try:
            apparmor_after = Path("/sys/module/apparmor/parameters/enabled").read_text().strip()
        except OSError:
            apparmor_after = "UNKNOWN"
        emit("APPARMOR_RESTRICT_UNPRIVILEGED_USERNS_AFTER", sysctl_after)
        emit("APPARMOR_ENABLED_AFTER", apparmor_after)
        if sysctl_after != sysctl or apparmor_after != apparmor:
            overall.append("FAIL")

    if overall and all(item == "PASS" for item in overall):
        emit("OVERALL", "PASS")
        return 0
    if "FAIL" in overall:
        emit("OVERALL", "FAIL")
        return 1
    emit("OVERALL", "BLOCKED")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
