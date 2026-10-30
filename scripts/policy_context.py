"""Validate/materialize common policy locally; no model, network, or state writes."""
import argparse
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from common_policy import (PolicyError, load_project_policy, developer_instructions,
                           chat_messages, context_packet)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=PROJECT)
    parser.add_argument('--format', choices=['inspect', 'developer', 'chat', 'packet'], default='inspect')
    parser.add_argument('--target', choices=['chatgpt','dot','codex','local'], default='codex')
    parser.add_argument('--task', default='Read the common policy before the requested task.')
    parser.add_argument('--role-instructions', default='')
    parser.add_argument('--expect-sha256')
    args = parser.parse_args()
    try:
        bundle = load_project_policy(args.project)
        if args.expect_sha256 is not None and args.expect_sha256 != bundle.sha256:
            raise PolicyError('Caller expected a different policy hash')
        if args.format == 'developer':
            print(developer_instructions(bundle, args.role_instructions))
            return 0
        if args.format == 'chat':
            value = chat_messages(bundle, args.task, args.role_instructions)
        elif args.format == 'packet':
            value = context_packet(bundle, args.task, args.target)
        else:
            value = {**bundle.metadata(), 'text': bundle.text}
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except PolicyError as error:
        print('Policy load failed: '+str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
