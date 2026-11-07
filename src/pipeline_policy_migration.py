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

from private_state import _json, _private
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
    normal_adjustment = old['schema'] == new['schema'] == 2
    for k in (['workspaces', 'models'] if normal_adjustment else ['workspaces', 'concurrency', 'timeout_seconds']):
        if old[k] != new[k]: _fail('MIGRATION_SCOPE_CHANGED')
    if normal_adjustment and (new['concurrency'] > min(new['max_threads'], new['max_tasks'])):
        _fail('MIGRATION_CONCURRENT_LIMIT_MISMATCH')
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
            and old['max_tasks'] == new['max_tasks'] and old['concurrency'] == new['concurrency']
            and old['timeout_seconds'] == new['timeout_seconds']): _fail('EMPTY_POLICY_MIGRATION')
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
    if previous is not None and previous != state['policy_hash']: _fail('POLICY_HISTORY_HEAD_MISMATCH')


def assert_migration_ready(db):
    """Read-only startup gate; legacy DBs without this table are unchanged."""
    db = Path(db)
    if not db.exists(): return
    _private(db, 8 * 1024**2)
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
        records = _records(conn)
        if any(r['state'] == 'PREPARED' for r in records): _fail('POLICY_MIGRATION_PENDING')
        if not records:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger'").fetchone():
                if _ledger(conn).get(HISTORY): _fail('UNKNOWN_POLICY_HISTORY')
            return
        state = _ledger(conn)
        _history(state, records)
        committed = [r for r in records if r['state'] == 'COMMITTED']
        if committed:
            _private(db.parent / POLICY, 16384)
            if _sha((db.parent / POLICY).read_bytes()) != committed[-1]['plan']['new_file_sha256']:
                _fail('POLICY_MIGRATION_FILE_DRIFT')


def _idle(root, state, *, stopped=False):
    if any(t.get('state') not in TERMINAL for t in state['tasks'].values()): _fail('MIGRATION_REQUIRES_TERMINAL_TASKS')
    from pipeline_absence import validated,resolved_archive
    if any(o.get('status') not in ['SUCCEEDED', 'FAILED'] and not resolved_archive(state,o) for o in state['ops'].values()): _fail('MIGRATION_UNCERTAIN_OPERATION')
    if any(t.get('active_task') or t.get('closing') or t.get('native_context_state', 'READY') != 'READY'
           or (not t.get('archived') and (not t.get('remote_id') or t.get('history_verified') is not True))
           for tid,t in state['threads'].items() if not validated(state,tid)): _fail('MIGRATION_THREAD_UNCERTAIN')
    marker = root / 'codex-pipeline-cleanup.json'
    _private(marker, 32768)
    if _json(marker.read_bytes()).get('status') != 'RELEASED': _fail('MIGRATION_CLEANUP_UNCONFIRMED')
    service = root / 'poc-services-state.json'
    if stopped and service.exists():
        _private(service)
        if _json(service.read_bytes()).get('phase') != 'STOPPED': _fail('MIGRATION_SERVICES_NOT_STOPPED')


def inspect_transition(runtime):
    """Read-only reconciliation facts; never print policies, prompts or answers."""
    root = _root(runtime)
    with closing(_connect(root)) as conn:
        state, records = _ledger(conn), _records(conn)
    pending = next((r for r in records if r['state'] == 'PREPARED'), None)
    current_file, current_ledger = _sha((root / POLICY).read_bytes()), digest(state)
    actions = []
    if pending:
        plan = pending['plan']
        if current_ledger == plan['ledger_before_sha256']:
            _history(state, [r for r in records if r['id'] != pending['id']])
        if current_ledger == plan['ledger_before_sha256'] and current_file in [plan['old_file_sha256'], plan['new_file_sha256']]:
            actions = ['abort']
        elif current_ledger == plan['ledger_after_sha256'] and current_file == plan['new_file_sha256']:
            _history(state, [{**r, 'state': 'COMMITTED'} if r['id'] == pending['id'] else r for r in records])
            actions = ['finalize']
        status = 'PREPARED' if actions else 'AMBIGUOUS'
    else:
        assert_migration_ready(root / DB)
        status = 'READY'
    return {'status': status, 'policy_file_sha256': current_file, 'ledger_sha256': current_ledger,
            'policy_hash': state['policy_hash'], 'counts': {k: len(state[k]) for k in ['threads', 'tasks', 'ops', 'events']},
            'transitions': [{'id': r['id'], 'state': r['state'], 'plan_sha256': r['plan_sha256']} for r in records],
            'explicit_recovery_actions': actions, 'automatic_retry': False}


def _transform(state, plan):
    result = json.loads(packed(state))
    result['policy_hash'] = plan['new_policy_hash']
    entry = {k: plan[k] for k in ['schema', 'id', 'old_file_sha256', 'new_file_sha256',
                                  'old_policy_hash', 'new_policy_hash', 'common_hash', 'direction']}
    # Exclude after-ledger fingerprint from the historical intent hash to avoid
    # a recursive hash definition; the journal separately pins the full plan.
    entry['plan_sha256'] = _history_hash(plan)
    result.setdefault(HISTORY, []).append(entry)
    if (_json(plan['new_raw'])['schema'] == _json(plan['old_raw'])['schema'] == 1
            and len(result['events']) >= MAX_EVENTS): _fail('EVENT_CAPACITY')
    result['events'].append({'seq': len(result['events']) + 1, 'kind': 'policy_migrated',
                             'time': plan['time'], 'migration_id': plan['id'],
                             'old_policy_hash': plan['old_policy_hash'], 'new_policy_hash': plan['new_policy_hash']})
    if len(packed(result).encode()) > 2 * 1024**2: _fail('LEDGER_CAPACITY')
    return result


def _history_hash(plan):
    return digest({k: v for k, v in plan.items() if k != 'ledger_after_sha256'})


def _bindings(state, records, plan):
    old, new = _plan_shape(plan)
    if state['policy_hash'] != plan['old_policy_hash']: _fail('MIGRATION_SOURCE_VERSION_UNKNOWN')
    if plan['direction'] == 'rollback' and not any(r['state'] == 'COMMITTED' and
            r['plan']['old_raw'] == plan['new_raw'] and r['plan']['common_hash'] == plan['common_hash'] for r in records):
        _fail('ROLLBACK_VERSION_NOT_RECORDED')
    known_models = set(old['models'])
    for r in records:
        if r['state'] == 'COMMITTED':
            known_models.update(_json(r['plan']['old_raw'])['models'])
            known_models.update(_json(r['plan']['new_raw'])['models'])
    for thread in state['threads'].values():
        if thread.get('workspace') not in old['workspaces'] or thread.get('model') not in known_models:
            _fail('MIGRATION_UNKNOWN_THREAD_BINDING')
        if thread['model'] not in new['models'] and not thread.get('archived'):
            _fail('ROLLBACK_REQUIRES_ARCHIVED_REMOVED_MODEL')
    if any(t.get('thread_id') not in state['threads'] or t.get('model') not in known_models
           for t in state['tasks'].values()): _fail('MIGRATION_UNKNOWN_TASK_BINDING')


def plan_transition(runtime, candidate, common_hash, migration_id, *, direction='expand', now=None):
    root = _root(runtime)
    assert_migration_ready(root / DB)
    _private(Path(candidate), 16384)
    old_raw, new_raw = (root / POLICY).read_bytes().decode(), Path(candidate).read_bytes().decode()
    with closing(_connect(root)) as conn:
        state, records = _ledger(conn), _records(conn)
    _history(state, records)
    _idle(root, state)
    if any(r['id'] == migration_id for r in records): _fail('MIGRATION_ID_ALREADY_USED')
    if len(records) >= 32: _fail('MIGRATION_JOURNAL_CAPACITY')
    plan = {'schema': 1, 'id': migration_id, 'direction': direction, 'common_hash': common_hash,
            'old_raw': old_raw, 'new_raw': new_raw, 'old_file_sha256': _sha(old_raw.encode()),
            'new_file_sha256': _sha(new_raw.encode()),
            'old_policy_hash': digest({'policy': _json(old_raw), 'common': common_hash}),
            'new_policy_hash': digest({'policy': _json(new_raw), 'common': common_hash}),
            'ledger_before_sha256': digest(state), 'ledger_after_sha256': '0' * 64,
            'time': time.time() if now is None else now}
    _bindings(state, records, plan)
    plan['ledger_after_sha256'] = digest(_transform(state, plan))
    return plan


def _write_policy(root, raw):
    path = root / POLICY
    _private(path, 16384)
    temporary = root / ('.policy-migration-' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw.encode()); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if temporary.exists(): temporary.unlink()


def apply_transition(runtime, plan, expected_plan_sha256):
    """Caller approval remains external. No automatic retry after PREPARED."""
    _plan_shape(plan)
    if digest(plan) != expected_plan_sha256: _fail('MIGRATION_PLAN_CHANGED')
    root = _root(runtime)
    with ExitStack() as locks:
        for name in ['poc-services.lock', 'daemon.lock', 'runner.lock']: locks.enter_context(lease(root / name))
        assert_migration_ready(root / DB)
        with closing(_connect(root, write=True)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                state, records = _ledger(conn), _records(conn)
                _history(state, records); _idle(root, state, stopped=True)
                if (digest(state) != plan['ledger_before_sha256'] or state['policy_hash'] != plan['old_policy_hash']
                        or (root / POLICY).read_bytes() != plan['old_raw'].encode()): _fail('MIGRATION_PLAN_STALE')
                _bindings(state, records, plan)
                if any(r['id'] == plan['id'] for r in records): _fail('MIGRATION_ID_ALREADY_USED')
                if len(records) >= 32: _fail('MIGRATION_JOURNAL_CAPACITY')
                target = _transform(state, plan)
                if digest(target) != plan['ledger_after_sha256']: _fail('MIGRATION_TARGET_CHANGED')
                body = packed({'plan': plan, 'plan_sha256': digest(plan)})
                if len(body.encode()) > 65536: _fail('MIGRATION_INTENT_CAPACITY')
                conn.execute('CREATE TABLE IF NOT EXISTS ' + TABLE +
                             ' (id TEXT PRIMARY KEY,schema INTEGER NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL)')
                conn.execute('INSERT INTO ' + TABLE + ' VALUES (?,1,\'PREPARED\',?)',
                             (plan['id'], body))
                conn.commit()
            except BaseException:
                conn.rollback(); raise
        _write_policy(root, plan['new_raw'])
        with closing(_connect(root, write=True)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                records = _records(conn)
                current = next((r for r in records if r['id'] == plan['id']), None)
                if (not current or current['state'] != 'PREPARED'
                        or current['plan_sha256'] != expected_plan_sha256):
                    _fail('MIGRATION_INTENT_CHANGED_DURING_APPLY')
                _history(_ledger(conn), [r for r in records if r['id'] != plan['id']])
                if (digest(_ledger(conn)) != plan['ledger_before_sha256']
                        or _sha((root / POLICY).read_bytes()) != plan['new_file_sha256']): _fail('MIGRATION_CHANGED_DURING_APPLY')
                conn.execute('UPDATE ledger SET body=? WHERE id=1', (packed(target),))
                conn.execute('UPDATE ' + TABLE + ' SET state=\'COMMITTED\' WHERE id=?', (plan['id'],))
                conn.commit()
            except BaseException:
                conn.rollback(); raise
    return {'status': 'COMMITTED', 'migration_id': plan['id'], 'policy_hash': plan['new_policy_hash'],
            'history_preserved': True, 'service_or_login_bindings_updated': False}


def recover_transition(runtime, migration_id, expected_plan_sha256, *, action):
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
