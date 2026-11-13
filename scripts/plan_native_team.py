"""Print a role/model recommendation; never start a model or create a run."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from native_agent_profiles import ROLES, recommend, load_profile
from native_team_artifacts import _no_symlinks


def concurrency_value(value):
    if value == 'max':
        return value
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Use max or a positive integer.') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--purpose', required=True,
                        choices=['implementation', 'documentation', 'research', 'orchestration'])
    parser.add_argument('--complexity', default='standard', choices=['quick', 'standard', 'advanced'])
    parser.add_argument('--parallel-parts', type=int, default=1)
    parser.add_argument('--security-sensitive', action='store_true')
    parser.add_argument('--performance-sensitive', action='store_true')
    parser.add_argument('--cross-project', action='store_true')
    parser.add_argument('--concurrency', type=concurrency_value,
                        help='New-run worker choice: max or a lower count; defaults to project settings.')
    parser.add_argument('--executor-limit', type=int,
                        help='Caller-reported available child slots, not total slots including the parent.')
    args = parser.parse_args(argv)
    try:
        result = recommend(args.purpose, args.complexity,
                           parallel_parts=args.parallel_parts,
                           security_sensitive=args.security_sensitive,
                           performance_sensitive=args.performance_sensitive,
                           cross_project=args.cross_project, project=PROJECT,
                           cloud_concurrency=args.concurrency, executor_limit=args.executor_limit)
        for assignment in result['assignments']:
            native = assignment['role']
            path = PROJECT / '.codex/agents' / (ROLES[native][0] + '.toml')
            _no_symlinks(path)
            raw, profile = load_profile(PROJECT, native)
            assignment.update(model=profile['model'], reasoning_effort=profile['model_reasoning_effort'],
                              profile_sha256=hashlib.sha256(raw).hexdigest())
        print(json.dumps({'status': 'OK', **result}, indent=2))
        return 0
    except (ValueError, OSError):
        print(json.dumps({'status': 'BLOCKED', 'code': 'ROLE_RECOMMENDATION_REJECTED',
                          'automatic_dispatch': False, 'automatic_retry': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
