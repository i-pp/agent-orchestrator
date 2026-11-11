"""Inert plan builder for one separately authorized native approval trial.

No process, native request, credential read or live activation exists here.
The current normal-host guard is evidence only, never permission to launch.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

from codex_app_protocol import _params
from isolation_guard import environment

COMMAND = '/usr/bin/python3 -I -S -c \'print("LAF_APPROVAL_TEST")\''
MARKER = 'LAF_APPROVAL_TEST'
SCHEMA_VERSION = '0.167.0'


class TrialPlanError(ValueError):
    pass


def _scratch(path):
    root = Path(path).absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise TrialPlanError('PRIVATE_EMPTY_SCRATCH_REQUIRED')
    info = root.stat()
    if (not root.is_dir() or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700 or any(root.iterdir())
            or any((p / '.git').exists() for p in (root, *root.parents))):
        raise TrialPlanError('PRIVATE_EMPTY_SCRATCH_REQUIRED')
    return str(root)


def build_trial_plan(scratch, model, *, approved_models):
    """Return data only. approved_models is host policy, not proof of billing."""
    root = _scratch(scratch)
    if not isinstance(model, str) or model not in approved_models:
        raise TrialPlanError('OPERATOR_APPROVED_MODEL_REQUIRED')
    instructions = (
        'This is one dedicated harmless approval-transport trial. Work only in '
        'the empty scratch directory. Do not inspect other files, use network or '
        'MCP tools, write files, request sandbox escalation, amend permissions, '
        'or start any other command. Propose only this exact command: ' + COMMAND +
        '. Preserve read-only sandbox and ordinary user approval. Stop after '
        'the command or if it cannot be performed within these limits.')
    rpc = {
        'initialize': {'clientInfo': {'name': 'laf-one-shot-approval-probe', 'version': '1'}},
        'model/list': {'limit': 100, 'includeHidden': False},
        'thread/start': {'model': model, 'cwd': root, 'approvalPolicy': 'untrusted',
                         'approvalsReviewer': 'user', 'sandbox': 'read-only',
                         'ephemeral': False, 'developerInstructions': instructions},
        'turn/start': {'threadId': '<exact-owned-thread-id-from-thread/start>',
                       'clientUserMessageId': '<fresh-logical-task-uuid>', 'model': model,
                       'cwd': root, 'approvalPolicy': 'untrusted', 'approvalsReviewer': 'user',
                       'sandboxPolicy': {'type': 'readOnly'},
                       'input': [{'type': 'text', 'text': instructions, 'text_elements': []}]},
    }
    for method, params in rpc.items(): _params(method, params)
    context = environment()
    return {
        'status': 'PREPARED_NOT_AUTHORIZED', 'executed': False,
        'app_server_processes_started': 0, 'native_threads_created': 0,
        'native_turns_started': 0, 'credentials_read': False,
        'schema_version': SCHEMA_VERSION, 'environment': context,
        'launch_blockers': (['NORMAL_HOST_CONTEXT_NOT_CONFIRMED'] if context['state'] != 'HOST_CONTEXT' else [])
                           + ['EXPLICIT_NATIVE_TRIAL_AUTHORIZATION_REQUIRED'],
        'scope': {'scratch': root, 'model': model, 'command': COMMAND,
                  'command_sha256': hashlib.sha256(COMMAND.encode()).hexdigest(),
                  'expected_stdout_marker': MARKER, 'max_turns': 1,
                  'max_wall_seconds': 600, 'approval_policy': 'untrusted',
                  'sandbox': 'readOnly', 'network_access': False,
                  'existing_service_changes': False, 'global_policy_changes': False,
                  'new_login_or_permission_enrollment': False},
        'rpc_templates': rpc,
        'required_checks': [
            'Use a separately approved normal-host entry; run the same guard there before process/secret access.',
            'Pin the native ELF/version; initialize once; check approved model availability and effective scope.',
            'Use one dedicated persisted thread for auditable history; archive only that owned thread if separately authorized.',
            'Bind only the newly created real thread/turn and exact original approval request to the local ledger.',
            'Accept only the exact command/cwd and an unambiguous ordinary command approval; stop on escalation, permissions, extra commands or unknown outcomes.',
            'A matching approval is not guaranteed: inherited rules and safe-command classification may auto-allow. No approval means inconclusive, never manufacture escalation.',
            'The read-only policy blocks writes and defaults network off; it is not a guarantee that arbitrary reads outside cwd are impossible.',
            'Do not claim a text prompt is a hard single-command boundary; stop/reconcile deviations without retries.',
            'Share only the exact approved request preview/bindings with the existing private Site.',
            'Require the actual authenticated human one-shot click and consume its receipt once on the same connection.',
            'Distinguish SENT, typed server resolution, matching native history and terminal output.',
            'On timeout/unknown, never resend or recreate. Interrupt only the owned turn and reap only the owned process identities.',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch', required=True)
    parser.add_argument('--model', required=True, choices=('gpt-6-luna', 'gpt-6.1-sol'))
    args = parser.parse_args()
    print(json.dumps(build_trial_plan(args.scratch, args.model,
        approved_models=('gpt-6-luna', 'gpt-6.1-sol')), ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
