"""Plan/apply an explicitly approved cold transition; never start a service/model."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from chat_mailbox import _private, _json, MailboxError
from common_policy import load_project_policy, PolicyError
from codex_pipeline import PipelineError, digest, packed
from pipeline_policy_migration import plan_transition, apply_transition, recover_transition, inspect_transition


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='operation', required=True)
    inspect = subs.add_parser('inspect')
    inspect.add_argument('--runtime', type=Path, required=True)
    plan = subs.add_parser('plan')
    plan.add_argument('--runtime', type=Path, required=True)
    plan.add_argument('--candidate', type=Path, required=True)
    plan.add_argument('--migration-id', required=True)
    plan.add_argument('--direction', choices=['expand', 'rollback'], default='expand')
    plan.add_argument('--output', type=Path, required=True)
    plan.add_argument('--previous-common-sha256', help='Explicit old common hash for an idle common-policy-only adoption')
    for operation in ['apply', 'recover']:
        sub = subs.add_parser(operation)
        sub.add_argument('--runtime', type=Path, required=True)
        sub.add_argument('--plan', type=Path, required=True)
        sub.add_argument('--expect-plan-sha256', required=True)
        sub.add_argument('--apply', action='store_true')
        if operation == 'recover': sub.add_argument('--action', choices=['abort', 'finalize'], required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.operation == 'inspect':
            print(json.dumps(inspect_transition(args.runtime))); return 0
        bundle = load_project_policy(PROJECT)
        if args.operation == 'plan':
            result = plan_transition(args.runtime, args.candidate, args.previous_common_sha256 or bundle.sha256,
                                     args.migration_id, direction=args.direction,
                                     new_common_hash=bundle.sha256 if args.previous_common_sha256 else None)
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(packed(result) + '\n'); stream.flush(); os.fsync(stream.fileno())
            print(json.dumps({'status': 'PLANNED', 'plan_sha256': digest(result), 'migration_id': result['id'],
                              'runtime_changed': False, 'services_started': False}))
            return 0
        if not args.apply: raise PipelineError('EXPLICIT_MIGRATION_APPLY_REQUIRED')
        _private(args.plan, 65536)
        planned = _json(args.plan.read_bytes())
        if planned.get('new_common_hash', planned.get('common_hash')) != bundle.sha256: raise PipelineError('MIGRATION_COMMON_POLICY_CHANGED')
        if args.operation == 'apply':
            result = apply_transition(args.runtime, planned, args.expect_plan_sha256)
        else:
            if digest(planned) != args.expect_plan_sha256: raise PipelineError('MIGRATION_PLAN_CHANGED')
            result = recover_transition(args.runtime, planned['id'], args.expect_plan_sha256, action=args.action)
        print(json.dumps(result)); return 0
    except PipelineError as error:
        print(json.dumps(error.result())); return 2
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error, MailboxError, PolicyError):
        print(json.dumps({'status': 'BLOCKED', 'code': 'MIGRATION_INPUT_OR_STORAGE_ERROR',
                          'automatic_retry': False})); return 2


if __name__ == '__main__': raise SystemExit(main())
