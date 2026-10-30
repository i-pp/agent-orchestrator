"""Bounded local question/answer slots. No Chat activation or identity attestation."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

MAX_TEXT_BYTES = 4096
MAX_SLOTS = 32
MAX_DB_BYTES = 1024 * 1024
ID_PATTERN = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
HASH_PATTERN = r'^[0-9a-f]{64}$'
TOOLS = ('chat_mailbox_register_request', 'chat_mailbox_get_request',
         'chat_mailbox_save_answer', 'chat_mailbox_get_answer')


class MailboxError(ValueError):
    def __init__(self, code, status='BLOCKED'):
        self.code, self.status = code, status
        super().__init__(code)  # Never return body, path or sqlite diagnostics.

    def result(self):
        return {'status': self.status, 'code': self.code, 'automatic_retry': False,
                'persisted': None if self.status == 'UNKNOWN' else False,
                'reconcile_with': ['chat_mailbox_get_request', 'chat_mailbox_get_answer']}


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _text(value):
    try:
        if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > MAX_TEXT_BYTES:
            raise MailboxError('INVALID_TEXT_BOUND')
    except UnicodeError:
        raise MailboxError('INVALID_TEXT_BOUND') from None


def validate_arguments(name, args):
    base = {'task_id', 'request_id', 'request_sha256'}
    required = base.copy()
    if name == TOOLS[0]: required |= {'request'}
    elif name == TOOLS[2]: required |= {'answer'}
    elif name not in (TOOLS[1], TOOLS[3]): raise MailboxError('UNKNOWN_MAILBOX_OPERATION')
    allowed = required | ({'expected_answer_sha256'} if name == TOOLS[3] else set())
    if name == TOOLS[2]: allowed.add('answer_sha256')
    if not isinstance(args, dict) or set(args) - allowed or required - set(args):
        raise MailboxError('INVALID_MAILBOX_FIELDS')
    for key in ['task_id', 'request_id']:
        if not isinstance(args[key], str) or not re.fullmatch(ID_PATTERN, args[key]):
            raise MailboxError('INVALID_MAILBOX_ID')
    for key in ['request_sha256', 'answer_sha256', 'expected_answer_sha256']:
        if key in args and (not isinstance(args[key], str) or not re.fullmatch(HASH_PATTERN, args[key])):
            raise MailboxError('INVALID_MAILBOX_HASH')
    for key in ['request', 'answer']:
        if key in args:
            _text(args[key])
            if key + '_sha256' in args and digest(args[key]) != args[key + '_sha256']:
                raise MailboxError('CONTENT_HASH_MISMATCH')


def _private(path, limit=None):
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or (limit is not None and info.st_size > limit)):
        raise MailboxError('UNSAFE_MAILBOX_FILE')


def _json(raw):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj: raise MailboxError('INVALID_MAILBOX_POLICY')
            obj[key] = value
        return obj
    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError):
        raise MailboxError('INVALID_MAILBOX_POLICY') from None


class Mailbox:
    @staticmethod
    def policy_snapshot(runtime):
        """Validate references without creating a DB or registering a request."""
        root = Path(runtime).absolute()
        if any(p.is_symlink() for p in [root, *root.parents]):
            raise MailboxError('UNSAFE_MAILBOX_RUNTIME')
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise MailboxError('UNSAFE_MAILBOX_RUNTIME')
        policy = root / 'chat-mailbox-policy.json'
        _private(policy, 16384)
        raw = policy.read_bytes()
        config = _json(raw)
        if (not isinstance(config, dict) or set(config) != {'schema', 'allowed'} or type(config['schema']) is not int
                or config['schema'] != 1 or not isinstance(config['allowed'], list)
                or not 1 <= len(config['allowed']) <= MAX_SLOTS):
            raise MailboxError('INVALID_MAILBOX_POLICY')
        allowed = {}
        for slot in config['allowed']:
            validate_arguments(TOOLS[1], slot)
            pair = slot['task_id'], slot['request_id']
            if pair in allowed: raise MailboxError('DUPLICATE_MAILBOX_SLOT')
            allowed[pair] = slot['request_sha256']
        policy_sha256 = hashlib.sha256(raw).hexdigest()
        return root, allowed, policy_sha256

    def __init__(self, runtime):
        self.root, self.allowed, self.policy_sha256 = self.policy_snapshot(runtime)
        self.path = self.root / 'chat-mailbox.sqlite'
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                if db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]:
                    raise MailboxError('MAILBOX_SCHEMA_MISMATCH')
                db.execute('''CREATE TABLE mailbox (
                    task_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL, request TEXT NOT NULL,
                    answer_sha256 TEXT, answer TEXT,
                    PRIMARY KEY(task_id,request_id),
                    CHECK((answer IS NULL) = (answer_sha256 IS NULL)))''')
                db.execute('PRAGMA user_version=1')
            elif version != 1:
                raise MailboxError('MAILBOX_SCHEMA_MISMATCH')
            db.commit()

    @contextmanager
    def _db(self):
        _private(self.path, MAX_DB_BYTES)
        db = sqlite3.connect(self.path, timeout=1, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('PRAGMA synchronous=FULL')
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            db.execute('PRAGMA max_page_count=' + str(MAX_DB_BYTES // page_size))
            yield db
        finally:
            db.close()  # Uncommitted operations roll back, including exceptions.

    def _commit(self, db):
        db.commit()

    def _row(self, db, args):
        row = db.execute('SELECT * FROM mailbox WHERE task_id=? AND request_id=?',
                         (args['task_id'], args['request_id'])).fetchone()
        if row:
            try:
                _text(row['request'])
                if row['answer'] is not None: _text(row['answer'])
            except MailboxError:
                raise MailboxError('MAILBOX_INTEGRITY_ERROR', 'UNKNOWN') from None
            if row['request_sha256'] != args['request_sha256'] or digest(row['request']) != row['request_sha256']:
                raise MailboxError('MAILBOX_INTEGRITY_ERROR', 'UNKNOWN')
            if row['answer'] is not None:
                if digest(row['answer']) != row['answer_sha256']:
                    raise MailboxError('MAILBOX_INTEGRITY_ERROR', 'UNKNOWN')
        return row

    def dispatch(self, name, args):
        validate_arguments(name, args)
        if self.allowed.get((args['task_id'], args['request_id'])) != args['request_sha256']:
            raise MailboxError('MAILBOX_SLOT_NOT_ALLOWED')
        writing = name in (TOOLS[0], TOOLS[2])
        try:
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE' if writing else 'BEGIN')
                row = self._row(db, args)
                duplicate = False
                if name == TOOLS[0]:
                    if row:
                        if row['request'] != args['request']: raise MailboxError('REQUEST_CONTENT_CONFLICT')
                        duplicate = True
                    else:
                        if db.execute('SELECT count(*) FROM mailbox').fetchone()[0] >= MAX_SLOTS:
                            raise MailboxError('MAILBOX_SLOT_LIMIT')
                        db.execute('INSERT INTO mailbox(task_id,request_id,request_sha256,request) VALUES(?,?,?,?)',
                                   (args['task_id'], args['request_id'], args['request_sha256'], args['request']))
                elif name == TOOLS[2]:
                    if not row: raise MailboxError('REQUEST_NOT_FOUND')
                    if row['answer'] is not None:
                        if row['answer'] != args['answer'] or row['answer_sha256'] != digest(args['answer']):
                            raise MailboxError('ANSWER_CONTENT_CONFLICT')
                        duplicate = True
                    else:
                        db.execute('UPDATE mailbox SET answer=?,answer_sha256=? WHERE task_id=? AND request_id=?',
                                   (args['answer'], digest(args['answer']), args['task_id'], args['request_id']))
                row = self._row(db, args)
                if (name == TOOLS[3] and args.get('expected_answer_sha256') and row and row['answer'] is not None
                        and row['answer_sha256'] != args['expected_answer_sha256']):
                    raise MailboxError('ANSWER_CONTENT_CONFLICT')
                result = {k: args[k] for k in ['task_id', 'request_id', 'request_sha256']}
                result.update(status='NOT_FOUND' if not row else 'ANSWERED' if row['answer'] is not None else 'PENDING',
                              persisted=bool(row), policy_sha256=self.policy_sha256, automatic_retry=False,
                              identity_verified=False)
                if writing: result['duplicate'] = duplicate
                if row and name == TOOLS[1]: result['request'] = row['request']
                if row and row['answer'] is not None:
                    result['answer_sha256'] = row['answer_sha256']
                    if name == TOOLS[3]: result['answer'] = row['answer']
                if writing: self._commit(db)
                return result
        except MailboxError:
            raise
        except (OSError, sqlite3.Error):
            raise MailboxError('MAILBOX_WRITE_UNKNOWN' if writing else 'MAILBOX_READ_UNAVAILABLE', 'UNKNOWN') from None
