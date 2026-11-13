"""Check staged source/configuration blobs for non-ASCII bytes; never print values."""
import argparse
import json
from pathlib import Path
import subprocess

SUFFIXES = {'.py', '.js', '.mjs', '.cjs', '.ts', '.mts', '.toml', '.json',
            '.jsonc', '.sql', '.sh', '.yml', '.yaml', '.service'}
CONFIG_ROOTS = {'config', 'systemd'}


def is_source_or_config(name):
    path = Path(name)
    # Examples keep the underlying format, including compound unit filenames.
    while path.suffix.lower() == '.example':
        path = path.with_suffix('')
    if path.suffix.lower() in {'.md', '.markdown'}:
        return False
    # Include suffixless/unusual configuration, such as the AppArmor draft.
    return (path.suffix.lower() in SUFFIXES or path.name == '.gitignore'
            or path.parts[0] in CONFIG_ROOTS)


def check(root):
    def git(*args):
        return subprocess.check_output(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                                        '-C', str(root), *args])
    records = git('ls-files', '--stage', '-z').split(b'\0')
    checked, findings = 0, []
    for record in filter(None, records):
        metadata, path = record.split(b'\t', 1)
        name = path.decode('utf-8')
        if not is_source_or_config(name):
            continue
        checked += 1
        mode, blob, stage = metadata.decode('ascii').split()
        if mode not in {'100644', '100755'} or stage != '0':
            findings.append({'path': name, 'rule': 'nonregular_or_conflicted_source'})
            continue
        raw = git('cat-file', 'blob', blob)
        if not raw.isascii():
            findings.extend({'path': name, 'line': index, 'rule': 'non_ascii_source'}
                            for index, line in enumerate(raw.splitlines(), 1) if not line.isascii())
    return {'passed': checked > 0 and not findings, 'checked_sources': checked,
            'findings': findings, 'scope': 'Exact Git index blobs; Markdown/history excluded'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        result = check(args.root)
    except (OSError, ValueError, subprocess.CalledProcessError):
        result = {'passed': False, 'code': 'SOURCE_INDEX_UNAVAILABLE'}
    print(json.dumps(result))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
