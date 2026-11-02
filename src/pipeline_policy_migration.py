"""Explicit cold policy transitions. No service, authentication or model actions.

The existing SQLite DB journals intent before the policy-file replacement. The
ledger update and COMMITTED marker share one transaction. A PREPARED transition
blocks startup; recovery never guesses or restores an older task database.
"""
from contextlib import closing, ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from chat_mailbox import _json, _private
from codex_pipeline import PipelineError, validate_policy, digest, packed, KEY, TERMINAL, MAX_EVENTS
from concurrency import lease

TABLE = 'pipeline_policy_transitions'
POLICY = 'codex-pipeline-policy.json'
DB = 'codex-pipeline.sqlite3'
HISTORY = 'policy_migrations'
PLAN_KEYS = {'schema', 'id', 'direction', 'common_hash', 'old_raw', 'new_raw',
             'old_file_sha256', 'new_file_sha256', 'old_policy_hash', 'new_policy_hash',
             'ledger_before_sha256', 'ledger_after_sha256', 'time'}


def _fail(code):
    raise PipelineError(code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _root(runtime):
    root = Path(runtime).absolute()
    if any(p.is_symlink() for p in [root, *root.parents]): _fail('UNSAFE_MIGRATION_RUNTIME')
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        _fail('UNSAFE_MIGRATION_RUNTIME')
    _private(root / DB, 8 * 1024**2)
    _private(root / POLICY, 16384)
    return root


def _connect(root, *, write=False):
    conn = sqlite3.connect((root / DB).as_uri() + ('?mode=rw' if write else '?mode=ro'),
                           uri=True, isolation_level=None, timeout=2)
    if write:
        if conn.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            conn.close(); _fail('UNSUPPORTED_MIGRATION_SQLITE_MODE')
        conn.execute('PRAGMA synchronous=FULL')
    return conn


def _ledger(conn):
    state = _json(conn.execute('SELECT body FROM ledger WHERE id=1').fetchone()[0])
    required = {'policy_hash', 'threads', 'tasks', 'ops', 'events'}
    if (not isinstance(state, dict) or not required <= set(state)
            or set(state) - required - {HISTORY}
            or any(not isinstance(state[k], dict) for k in ['threads', 'tasks', 'ops'])
            or not isinstance(state['events'], list)):
        _fail('UNSUPPORTED_LEDGER_VERSION')
    return state


def _plan_shape(plan):
    if (not isinstance(plan, dict) or set(plan) != PLAN_KEYS or type(plan['schema']) is not int
            or plan['schema'] != 1 or not isinstance(plan['id'], str) or not re.fullmatch(KEY, plan['id'])
            or plan['direction'] not in ['expand', 'rollback']
            or type(plan['time']) not in [int, float] or not 0 < plan['time'] < 10**12):
        _fail('UNSUPPORTED_MIGRATION_VERSION')
    for k in ['common_hash', 'old_file_sha256', 'new_file_sha256', 'old_policy_hash',
              'new_policy_hash', 'ledger_before_sha256', 'ledger_after_sha256']:
        if not isinstance(plan[k], str) or not re.fullmatch('[a-f0-9]{64}', plan[k]):
            _fail('INVALID_MIGRATION_HASH')
    policies = []
    for prefix in ['old', 'new']:
        raw = plan[prefix + '_raw']
        if not isinstance(raw, str) or len(raw.encode()) > 16384: _fail('INVALID_MIGRATION_POLICY')
        policy = validate_policy(_json(raw))
        if (_sha(raw.encode()) != plan[prefix + '_file_sha256']
                or digest({'policy': policy, 'common': plan['common_hash']}) != plan[prefix + '_policy_hash']):
            _fail('MIGRATION_INTENT_HASH_MISMATCH')
        policies.append(policy)
    old, new = policies
    for k in ['workspaces', 'concurrency', 'timeout_seconds']:
        if old[k] != new[k]: _fail('MIGRATION_SCOPE_CHANGED')
    mode_change = old['schema'] != new['schema']
    if mode_change:
        valid_direction = ((old['schema'], new['schema'], plan['direction']) in [(1,2,'expand'),(2,1,'rollback')])
        if not valid_direction or old['models'] != new['models']: _fail('MIGRATION_SCOPE_CHANGED')
        normal = new if new['schema'] == 2 else old
        if normal['max_threads'] != normal['concurrency'] or normal['max_tasks'] != normal['concurrency']:
            _fail('MIGRATION_CONCURRENT_LIMIT_MISMATCH')
    if not mode_change and plan['direction'] == 'expand' and (not set(old['models']) <= set(new['models'])
            or new['max_threads'] < old['max_threads'] or new['max_tasks'] < old['max_tasks']):
        _fail('MIGRATION_NOT_MONOTONIC')
    if (not mode_change and set(old['models']) == set(new['models']) and old['max_threads'] == new['max_threads']
            and old['max_tasks'] == new['max_tasks']): _fail('EMPTY_POLICY_MIGRATION')
    return old, new


def _records(conn):
    exists = conn.execute('SELECT type FROM sqlite_master WHERE name=?', (TABLE,)).fetchone()
    if not exists: return []
    columns = conn.execute('PRAGMA table_info(' + TABLE + ')').fetchall()
    if exists[0] != 'table' or [(v[1], v[2], v[3], v[5]) for v in columns] != [
            ('id', 'TEXT', 0, 1), ('schema', 'INTEGER', 1, 0),
            ('state', 'TEXT', 1, 0), ('body', 'TEXT', 1, 0)]:
        _fail('UNSUPPORTED_MIGRATION_JOURNAL')
    rows = conn.execute('SELECT id,schema,state,body FROM ' + TABLE + ' ORDER BY rowid LIMIT 33').fetchall()
    if len(rows) > 32: _fail('MIGRATION_JOURNAL_CAPACITY')
    records = []
    for ident, version, phase, body in rows:
        if version != 1 or phase not in ['PREPARED', 'COMMITTED', 'ABORTED'] or len(body.encode()) > 65536:
            _fail('UNSUPPORTED_MIGRATION_JOURNAL')
        item = _json(body)
        if (not isinstance(item, dict) or set(item) != {'plan', 'plan_sha256'}
                or digest(item['plan']) != item['plan_sha256'] or ident != item['plan'].get('id')):
            _fail('MIGRATION_JOURNAL_HASH_MISMATCH')
        _plan_shape(item['plan'])
        records.append({'id': ident, 'state': phase, **item})
    if sum(r['state'] == 'PREPARED' for r in records) > 1: _fail('AMBIGUOUS_POLICY_MIGRATION')
    return records


def _history(state, records):
    committed = [r for r in records if r['state'] == 'COMMITTED']
    history = state.get(HISTORY, [])
    if not isinstance(history, list) or len(history) != len(committed): _fail('UNKNOWN_POLICY_HISTORY')
    previous = None
    for entry, record in zip(history, committed):
        p = record['plan']
        expected = {k: p[k] for k in ['schema', 'id', 'old_file_sha256', 'new_file_sha256',
                                      'old_policy_hash', 'new_policy_hash', 'common_hash', 'direction']}
        expected['plan_sha256'] = _history_hash(p)
        if entry != expected or (previous is not None and p['old_policy_hash'] != previous):
            _fail('UNKNOWN_POLICY_HISTORY')
        previous = p['new_policy_hash']
    if previous is not None a…2845 tokens truncated…, action):
    """Explicit abort of an untouched ledger, or finalize an exactly-new pair."""
    root = _root(runtime)
    if action not in ['abort', 'finalize']: _fail('INVALID_MIGRATION_RECOVERY')
    with ExitStack() as locks:
        for name in ['poc-services.lock', 'daemon.lock', 'runner.lock']: locks.enter_context(lease(root / name))
        with closing(_connect(root, write=True)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                records = _records(conn)
                record = next((r for r in records if r['id'] == migration_id), None)
                if (not record or record['state'] != 'PREPARED'
                        or record['plan_sha256'] != expected_plan_sha256): _fail('MIGRATION_RECOVERY_INTENT_MISMATCH')
                plan = record['plan']; state = _ledger(conn); _idle(root, state, stopped=True)
                file_hash, ledger_hash = _sha((root / POLICY).read_bytes()), digest(state)
                if action == 'abort':
                    if (ledger_hash != plan['ledger_before_sha256'] or file_hash not in
                            [plan['old_file_sha256'], plan['new_file_sha256']]): _fail('MIGRATION_RECOVERY_AMBIGUOUS')
                    _history(state, [r for r in records if r['id'] != migration_id])
                    _write_policy(root, plan['old_raw']); phase = 'ABORTED'
                else:
                    if file_hash != plan['new_file_sha256'] or ledger_hash != plan['ledger_after_sha256']:
                        _fail('MIGRATION_RECOVERY_AMBIGUOUS')
                    _history(state, [{**r, 'state': 'COMMITTED'} if r['id'] == migration_id else r for r in records])
                    phase = 'COMMITTED'
                conn.execute('UPDATE ' + TABLE + ' SET state=? WHERE id=?', (phase, migration_id)); conn.commit()
            except BaseException:
                conn.rollback(); raise
    return {'status': phase, 'migration_id': migration_id, 'automatic_retry': False}


def rebind_service_snapshot(runtime, previous, current_snapshot):
    """Pure data preparation, not service-state/unit installation or a restart."""
    root = _root(runtime)
    assert_migration_ready(root / DB)
    with closing(_connect(root)) as conn:
        committed = [r for r in _records(conn) if r['state'] == 'COMMITTED']
    if not committed: _fail('NO_COMMITTED_POLICY_MIGRATION')
    plan = committed[-1]['plan']
    if not isinstance(previous, dict) or not isinstance(current_snapshot, dict):
        _fail('SERVICE_BINDING_MIGRATION_MISMATCH')
    keys = {'executor_policy_sha256', 'pipeline_policy_sha256', 'common_policy_sha256', 'binary_sha256'}
    old_snapshot = previous.get('codex_pipeline_snapshot', {})
    if (type(previous.get('schema')) is not int or previous.get('schema') != 1
            or previous.get('runtime') != str(root) or previous.get('phase') != 'STOPPED'
            or previous.get('codex_pipeline_enabled') is not True
            or set(old_snapshot) != keys or set(current_snapshot) != keys
            or old_snapshot['pipeline_policy_sha256'] != plan['old_file_sha256']
            or current_snapshot['pipeline_policy_sha256'] != plan['new_file_sha256']
            or current_snapshot['common_policy_sha256'] != plan['common_hash']
            or any(old_snapshot[k] != current_snapshot[k] for k in keys - {'pipeline_policy_sha256'})):
        _fail('SERVICE_BINDING_MIGRATION_MISMATCH')
    if any(not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value)
           for snapshot in [old_snapshot, current_snapshot] for value in snapshot.values()):
        _fail('SERVICE_BINDING_MIGRATION_MISMATCH')
    result = json.loads(packed(previous))
    result['codex_pipeline_snapshot'] = dict(current_snapshot)
    return result
