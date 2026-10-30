"""Bounded readiness checks. No start/stop, credential contents, or task writes."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from control_socket import request
from transport import proc_identity


def daemon_ready(root, unit=None, timeout=1, *, manager_env=None):
    raw = Path(root).expanduser().absolute()
    if any(p.is_symlink() for p in [raw, *raw.parents]):
        raise ValueError('Unsafe runtime ancestry')
    root = raw.resolve()
    if not root.is_relative_to(PROJECT / '.runtime'):
        raise ValueError('Runtime must be inside the project')
    state = request(root, 'status', {}, timeout=timeout)
    owner = state['owner']
    identity = proc_identity(state['pid'])
    if (state['runtime'] != str(root) or state['stopping'] or not identity
            or identity['state'] in ['Z', 'X'] or owner['process']['start'] != identity['start']
            or owner['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip()):
        return False
    if unit is not None:
        if not re.fullmatch(r'[A-Za-z0-9_.@:-]{1,180}\.service', unit):
            raise ValueError('Invalid owner unit')
        probe = subprocess.run(['/usr/bin/systemctl', '--user', 'show', unit, '-p', 'MainPID', '--value'],
                               capture_output=True, text=True, timeout=timeout, check=True, env=manager_env)
        main_pid = int(probe.stdout.strip())
        if main_pid <= 1: return False
        current, seen = identity, set()
        while current and current['pid'] not in seen and current['pid'] > 1:
            if current['pid'] == main_pid: return True
            seen.add(current['pid'])
            current = proc_identity(current['ppid'])
        return False
    return True


def tunnel_ready(client, url_file, timeout=2):
    path = Path(url_file)
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError('Unsafe health URL path')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 512 or info.st_mode & 0o077:
        raise ValueError('Health URL file must be private and bounded')
    url = urlsplit(path.read_text().strip())
    if (url.scheme != 'http' or url.hostname not in ['127.0.0.1', '::1'] or not url.port
            or url.username or url.password or url.query or url.fragment or url.path not in ['', '/']):
        raise ValueError('Health must use a loopback endpoint')
    # This health subcommand needs no key, even when the service holds credentials.
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'LANG': 'C.UTF-8'}
    result = subprocess.run([str(client), 'health', '--url-file', str(path),
        '--require-control-plane-poll', '--json'], env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0: return False
    value = json.loads(result.stdout)
    return (value.get('result') == 'ok' and value.get('healthz', {}).get('ok') is True
            and value.get('readyz', {}).get('ok') is True
            and value.get('control_plane_poll', {}).get('ok') is True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='kind', required=True)
    daemon = sub.add_parser('daemon')
    daemon.add_argument('--runtime', type=Path, required=True)
    daemon.add_argument('--unit')
    tunnel = sub.add_parser('tunnel')
    tunnel.add_argument('--client', type=Path, required=True)
    tunnel.add_argument('--url-file', type=Path, required=True)
    for command in [daemon, tunnel]: command.add_argument('--timeout', type=float, default=20)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 90:
        parser.error('timeout must be finite and 0 < seconds <= 90')
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        remaining = max(.01, min(1, deadline - time.monotonic()))
        try:
            ready = (daemon_ready(args.runtime, args.unit, remaining) if args.kind == 'daemon'
                     else tunnel_ready(args.client, args.url_file, remaining))
            if ready:
                print('Readiness confirmed: ' + args.kind)
                return 0
        except (OSError, ValueError, RuntimeError, EOFError, KeyError, TypeError, subprocess.SubprocessError):
            pass  # A bounded startup wait, never task replay or secret-bearing diagnostics.
        time.sleep(min(.1, max(0, deadline - time.monotonic())))
    print('Readiness deadline exceeded: ' + args.kind, file=sys.stderr)
    return 1


if __name__ == '__main__': raise SystemExit(main())
