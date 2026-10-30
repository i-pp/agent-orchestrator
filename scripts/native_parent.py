"""Local parent-tool handoff CLI. Never calls collaboration tools itself."""
import argparse
import json
from pathlib import Path
import sys
import subprocess

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from chat_mailbox import _json
from native_dispatcher import NativeDispatcher
from native_parent_adapter import NativeParentAdapter
from native_team import NativeTeam

FIELDS = {
    'init': {'spec', 'submission_key', 'parent_task', 'step_limit'},
    'step': set(),
    'spawned': {'action_id', 'agent_name'},
    'inventory': {'action_ids', 'result'},
    'interrupted': {'action_id', 'accepted'},
    'verified': {'action_id', 'accepted', 'reason', 'verification'},
}
MAX_INPUT = 65536


class InputError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError('INVALID_CLI_ARGUMENTS')


def invoke(operation, root, value):
    if operation == 'init':
        NativeParentAdapter._scope(value['parent_task'], value['step_limit'])
        dispatcher = NativeDispatcher.receive(root, value['spec'], value['submission_key'])
        NativeParentAdapter.configure(dispatcher, value['parent_task'], value['step_limit'])
        return {'initialized': True}
    adapter = NativeParentAdapter(NativeDispatcher(NativeTeam(root)))
    return getattr(adapter, operation)(**value)


def main(argv=None, stdin=None, stdout=None):
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    parser = Parser(description=__doc__)
    parser.add_argument('operation', choices=list(FIELDS))
    parser.add_argument('--run', type=Path, required=True)
    operation = 'unparsed'
    try:
        args = parser.parse_args(argv)
        operation = args.operation
        raw = stdin.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise InputError('INPUT_SIZE_LIMIT')
        try:
            value = _json(raw) if raw.strip() else {}
        except ValueError:
            raise InputError('INVALID_JSON') from None
        if not isinstance(value, dict) or set(value) != FIELDS[operation]:
            raise InputError('INVALID_OPERATION_FIELDS')
        result = invoke(operation, args.run, value)
        reply, code = {'status': 'OK', 'operation': operation, 'result': result}, 0
    except (ValueError, KeyError, TypeError, AttributeError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        # A local snapshot might have committed before fsync/ACK failed. Never
        # claim a mutation was not performed, or echo raw file/tool diagnostics.
        reply = {'status': 'BLOCKED' if isinstance(error, InputError) else 'UNKNOWN',
                 'operation': operation,
                 'code': str(error) if isinstance(error, InputError) else 'PARENT_ADAPTER_RECONCILIATION_REQUIRED'}
        code = 2
    reply.update(automatic_tool_execution=False, wake_after_parent_turn=False)
    print(json.dumps(reply, ensure_ascii=False), file=stdout)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
