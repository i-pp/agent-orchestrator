"""Operator-run host metadata probe. No install, login, turn or network listener.

Normal host only; never relocate CODEX_HOME or override a managed sandbox.
An exclusive receipt prevents reusing an uncertain run. CLI paths are operator
arguments, never an MCP operation or untrusted task argument.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import sys
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from isolation_guard import environment
from codex_app_protocol import ProtocolClient, ProtocolError, StdioTransport, schema_version_supported


def preflight(binary, expected_sha256):
    context = environment()
    if context['state'] != 'HOST_CONTEXT': raise ValueError('NORMAL_HOST_CONTEXT_NOT_CONFIRMED')
    if os.getuid() == 0: raise ValueError('NORMAL_USER_REQUIRED')
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
    # Use the existing identity/state root. No empty replacement home/auth copy.
    if not home.is_absolute() or not home.is_dir() or home.stat().st_uid != os.getuid():
        raise ValueError('EXISTING_CODEX_HOME_REQUIRED')
    if os.statvfs(home).f_flag & os.ST_RDONLY or not os.access(home, os.W_OK):
        raise ValueError('CODEX_STATE_NOT_WRITABLE')
    path = Path(binary)
    if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError('PINNED_NATIVE_BINARY_REQUIRED')
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
            or info.st_mode & 0o022 or not os.access(path, os.X_OK)):
        raise ValueError('UNSAFE_NATIVE_BINARY')
    h = hashlib.sha256()
    with path.open('rb') as stream:
        if stream.read(4) != b'\x7fELF': raise ValueError('NATIVE_ELF_REQUIRED')
        stream.seek(0)
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): h.update(chunk)
    if h.hexdigest() != expected_sha256: raise ValueError('BINARY_HASH_MISMATCH')
    return path, context


def run(binary, expected_sha256, receipt_path, *, transport_factory=StdioTransport):
    binary, context = preflight(binary, expected_sha256)
    receipt_path = Path(receipt_path)
    parent = receipt_path.parent
    if not receipt_path.is_absolute() or any(p.is_symlink() for p in [parent, *parent.parents]):
        raise ValueError('PRIVATE_RECEIPT_DIRECTORY_REQUIRED')
    info = parent.stat()
    if not parent.is_dir() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('PRIVATE_RECEIPT_DIRECTORY_REQUIRED')
    fd = os.open(receipt_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    receipt = os.fdopen(fd, 'w')
    result = {'diagnostic_id': str(uuid.uuid4()), 'status': 'STARTING', 'context': context['state'],
              'binary_sha256': expected_sha256, 'model_requests': 0, 'automatic_retries': 0}
    receipt.write(json.dumps(result) + '\n'); receipt.flush(); os.fsync(receipt.fileno())
    transport = transport_factory()
    # Approved fixed native ELF, not PATH or an extra shell/npm wrapper.
    transport.COMMAND = (str(binary), 'app-server', '--listen', 'stdio://')
    def receive(event):
        if event.get('kind') == 'server_request':
            result['blocked_server_request'] = True
            raise ProtocolError('READ_ONLY_PROBE_CANNOT_ANSWER_SERVER_REQUESTS')
    client = ProtocolClient(transport, event_sink=receive, timeout_s=10)
    transport.bind_client(client)
    def deadline(signum, frame): raise TimeoutError('HOST_METADATA_PROBE_DEADLINE')
    old_handler = signal.signal(signal.SIGALRM, deadline)
    signal.alarm(45)
    try:
        transport.start()
        initialized = client.initialize()
        result['protocol_schema_supported'] = schema_version_supported(initialized['userAgent'])
        if not result['protocol_schema_supported']: raise ProtocolError('UNVERIFIED_SERVER_VERSION')
        account = client.request('account/read', {'refreshToken': False})
        result['account_type'] = (account.get('account') or {}).get('type')
        page = client.request('model/list', {'includeHidden': False, 'limit': 100})
        result['models'] = [{'model': m['model'], 'is_default': m['isDefault']} for m in page['data']]
        result['catalog_complete'] = not bool(page.get('nextCursor'))
        result['billing_verified'] = False
        result['status'] = 'READ_PROBE_COMPLETED'
    except Exception as error:
        result.update(status='READ_PROBE_NOT_COMPLETED', exception_class=type(error).__name__)
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, old_handler)
        try: transport.close()
        except Exception as error: result['close_exception_class'] = type(error).__name__
        result['client'] = client.diagnostics()
        result['transport'] = transport.diagnostics()
        if not result['transport'].get('reaped', False): result['status'] = 'CLEANUP_UNCONFIRMED'
        receipt.seek(0); receipt.truncate(); json.dump(result, receipt, indent=2)
        receipt.write('\n'); receipt.flush(); os.fsync(receipt.fileno()); receipt.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--receipt', required=True)
    args = parser.parse_args()
    try:
        result = run(args.binary, args.sha256, args.receipt)
    except FileExistsError:
        result = {'status': 'BLOCKED', 'code': 'RECEIPT_ALREADY_EXISTS', 'child_started': False,
                  'previous_outcome_requires_reconciliation': True, 'automatic_retry': False}
    except (OSError, ValueError) as error:
        # Never expose OS filenames or raw startup/credential diagnostics.
        allowed = {'NORMAL_HOST_CONTEXT_NOT_CONFIRMED', 'NORMAL_USER_REQUIRED', 'EXISTING_CODEX_HOME_REQUIRED',
                   'CODEX_STATE_NOT_WRITABLE', 'PINNED_NATIVE_BINARY_REQUIRED', 'UNSAFE_NATIVE_BINARY',
                   'NATIVE_ELF_REQUIRED', 'BINARY_HASH_MISMATCH', 'PRIVATE_RECEIPT_DIRECTORY_REQUIRED'}
        known_preflight = str(error) in allowed
        result = {'status': 'BLOCKED' if known_preflight else 'UNKNOWN',
                  'code': str(error) if known_preflight else 'HOST_IO_OR_RECEIPT_UNCERTAIN',
                  'child_started': False if known_preflight else None, 'automatic_retry': False}
    print(json.dumps(result))
    return 0 if result['status'] == 'READ_PROBE_COMPLETED' else 1


if __name__ == '__main__': raise SystemExit(main())
