"""Offline candidate manifest builder for existing quality-batch v1 records.

No import of trial/evaluator code, no execution, no source modifications, and no
automatic artifact release. Review projections and pin the final manifest before
separately approving publication through the existing MCP service.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from experiments import BACKENDS, TASKS, EvidenceError, canonical, decode, digest, safe_read


def build(journal, report):
    roots = {'journal': str(Path(journal).absolute()), 'report': str(Path(report).absolute())}
    summary = safe_read(roots['report'], 'FINAL.json')
    final = decode(summary)
    if not isinstance(final, dict) or not isinstance(final.get('rows'), list): raise EvidenceError('UNSUPPORTED_REPORT')
    batch_hash = digest(summary)
    runs = []
    for backend in BACKENDS:
        for task in TASKS:
            rows = [r for r in final['rows'] if r.get('backend') == backend and r.get('task') == task]
            if len(rows) != 1: raise EvidenceError('AMBIGUOUS_BATCH_ROWS')
            key = backend+'-'+task
            paths = {'intent':key+'-intent.json', 'response':key+'-response.json', 'grading':key+'-grading.json',
                'candidate':key+'-candidate/'+TASKS[task], 'scope':'scope.json', 'summary':'FINAL.json'}
            sources = {}
            for kind, relative in paths.items():
                root = 'report' if kind == 'summary' else 'journal'
                try: raw = safe_read(roots[root], relative)
                except FileNotFoundError: sources[kind] = None; continue
                sources[kind] = {'root':root, 'path':relative, 'sha256':digest(raw)}
            runs.append({'id':'q-'+batch_hash[:16]+'-'+key, 'key':key, 'sources':sources, 'release':{}})
    return {'schema':1, 'roots':roots, 'runs':runs}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--journal', type=Path, required=True)
    p.add_argument('--report-directory', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    candidate = build(args.journal, args.report_directory)
    raw = json.dumps(candidate, indent=2, ensure_ascii=False).encode()+b'\n'
    fd = os.open(args.output, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd,'wb') as f: f.write(raw); f.flush(); os.fsync(f.fileno())
    print(json.dumps({'runs':len(candidate['runs']), 'manifest_sha256':digest(raw),
        'artifacts_released':0, 'model_requests':0, 'candidate_only':True}))


if __name__ == '__main__': main()
