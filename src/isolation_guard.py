"""Pre-launch policy for coordinator-owned isolation, not a host security boundary."""

import json
from pathlib import Path


def environment():
    """Observe procfs only. HOST_CONTEXT is evidence, never a permission grant.

    Missing, unfamiliar or conflicting observations remain UNKNOWN. No probe
    creates a namespace, and no caller-supplied flag can override detection.
    """
    observations = {}
    for key, name in {"label": "self/attr/current", "init": "1/comm",
                      "uid_map": "self/uid_map", "status": "self/status"}.items():
        try:
            observations[key] = (Path('/proc') / name).read_text().strip()
        except OSError:
            observations[key] = None
    return classify(observations)


def classify(observations):
    label, init = observations.get('label'), observations.get('init')
    status = dict(line.split(':', 1) for line in (observations.get('status') or '').splitlines() if ':' in line)
    try:
        uid_map = [tuple(map(int, line.split())) for line in (observations.get('uid_map') or '').splitlines()]
        valid_map = bool(uid_map) and all(len(row) == 3 and row[2] > 0 for row in uid_map)
        nspids = [int(pid) for pid in status.get('NSpid', '').split()]
        valid_pids = bool(nspids) and all(pid > 0 for pid in nspids)
        nested_pids = valid_pids and len(nspids) > 1
    except ValueError:
        valid_map, valid_pids, nested_pids = False, False, False
    signals = []
    if label and 'bwrap' in label and label.endswith('(enforce)'):
        signals.append('BWRAP_PROFILE')
    if init in ('codex', 'bwrap'):
        signals.append('SANDBOX_PID1')
    if nested_pids:
        signals.append('NESTED_PID_NAMESPACE')
    if valid_map and uid_map != [(0, 0, 4294967295)]:
        signals.append('USER_NAMESPACE_MAPPING')
    if signals:
        return {'state': 'SANDBOXED', 'signals': signals}
    # Deliberately narrow: an unconfined label alone says nothing about PID,
    # user namespaces, seccomp, Landlock or privileges.
    if (init in ('systemd', 'init') and label == 'unconfined'
            and valid_map and uid_map == [(0, 0, 4294967295)]
            and valid_pids and len(nspids) == 1
            and status.get('NoNewPrivs', '').strip() == '0'
            and status.get('Seccomp', '').strip() == '0'):
        return {'state': 'HOST_CONTEXT', 'signals': ['HOST_PROCFS_OBSERVATIONS']}
    return {'state': 'UNKNOWN', 'signals': ['INSUFFICIENT_ENVIRONMENT_EVIDENCE']}


class IsolationBlocked(RuntimeError):
    """Raised only before the guarded operation is launched."""

    def __init__(self, operation, context):
        self.evidence = {
            'status': 'BLOCKED',
            'reason_code': ('NESTED_ISOLATION_FORBIDDEN' if context['state'] == 'SANDBOXED'
                            else 'ISOLATION_CONTEXT_UNKNOWN'),
            'operation': operation, 'environment': context,
            'executed': False, 'quality_verdict': 'UNASSESSED',
        }
        super().__init__(json.dumps(self.evidence, sort_keys=True))


def require_isolation_creation(operation):
    context = environment()
    if context['state'] != 'HOST_CONTEXT':
        raise IsolationBlocked(operation, context)
    return context


def check_command(argv):
    """Extra check for direct namespace tools at the transport entry point.

    Built-in wrapped launchers declare isolation before constructing argv.
    This does not parse arbitrary shell programs or inspect descendants.
    """
    if argv and Path(argv[0]).name in ('bwrap', 'unshare', 'nsjail', 'firejail'):
        require_isolation_creation('direct-' + Path(argv[0]).name)
