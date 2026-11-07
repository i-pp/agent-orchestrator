"""Durable, local-only desktop delivery from the existing Codex event ledger.

No task execution, network callback, ChatGPT wake, or credentials are involved.
The event mapper is shared with MCP Events; only IDs, fixed states and times
reach the desktop session. An uncertain Notify result is never blindly resent.
"""
from contextlib import closing, contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time

from notification_outbox import NotificationError, _event_payload, _recognized_notification_source
from private_state import _private


class FreedesktopSender:
    """A successful result means desktop-service acceptance, not user viewing."""

    def __init__(self, *, run=subprocess.run, timeout=5.0):
        self.run = run
        self.timeout = timeout

    def __call__(self, payload):
        base = ['/usr/bin/gdbus', 'call', '--session', '--dest',
                'org.freedesktop.Notifications', '--object-path',
                '/org/freedesktop/Notifications', '--method']
        # No Notify call occurs on this failure, so retrying is safe.
        try:
            ready = self.run(base + ['org.freedesktop.Notifications.GetServerInformation'],
                             capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return {'status': 'RETRY', 'code': 'DESKTOP_UNAVAILABLE'}
        if ready.returncode:
            return {'status': 'RETRY', 'code': 'DESKTOP_UNAVAILABLE'}
        data = payload['data']
        summary = 'AceIS Codex: ' + data['state']
        body = 'Task: ' + data['task_id'] + '\nUTC: ' + payload['timestamp']
        try:
            result = self.run(base + ['org.freedesktop.Notifications.Notify',
                'AceIS Codex', '0', '', summary, body, '[]', '{}', '10000'],
                capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return {'status': 'UNKNOWN', 'code': 'DESKTOP_RESULT_UNKNOWN'}
        match = re.fullmatch(r'\(uint32 ([1-9][0-9]*),\)\s*', result.stdout)
        if result.returncode or match is None:
            return {'status': 'UNKNOWN', 'code': 'DESKTOP_RESULT_UNKNOWN'}
        return {'status': 'DELIVERED', 'desktop_id': int(match[1])}


class DesktopNotificationQueue:
    """One durable destination, with a persisted cursor and event-ID deduplication.

    The first run starts at the ledger's current head. Subsequent runs recover
    events since that baseline. A crash after sending leaves UNKNOWN rather
    than risking a second report. Notification failures never write the ledger.
    """

    def __init__(self, source_path, queue_path, *, clock=time.time, max_attempts=8):
        self.source = Path(source_path).absolute()
        self.path = Path(queue_path).absolute()
        self.clock = clock
        self.max_attempts = max_attempts
        if self.path == self.source or max_attempts < 1:
            raise NotificationError('INVALID_DESKTOP_CONFIGURATION')
        for path in (self.path, self.source):
            if any(p.is_symlink() for p in [path, *path.parents]):
                raise NotificationError('UNSAFE_DESKTOP_PATH')
            info = path.parent.stat()
            if (not path.parent.is_dir() or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise NotificationError('UNSAFE_DESKTOP_PARENT')
        _private(self.source)
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        _private(self.path)
        self.lock_path = self.path.with_suffix(self.path.suffix + '.lock')
        with self._tx() as db:
            db.execute('CREATE TABLE IF NOT EXISTS desktop_cursor '
                       '(id INTEGER PRIMARY KEY CHECK(id=1), seq INTEGER NOT NULL, source TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS desktop_deliveries (
                event_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, payload BLOB NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL, desktop_id INTEGER, error_code TEXT,
                occurred_at REAL NOT NULL, delivered_at REAL, latency_ms REAL)''')

    @contextmanager
    def _tx(self):
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _events(self):
        _private(self.source)
        with closing(sqlite3.connect(self.source.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            raw = db.execute('SELECT body FROM ledger WHERE id=1').fetchone()
        if raw is None or len(raw[0]) > 16 * 1024 * 1024:
            raise NotificationError('INVALID_EVENT_LEDGER')
        state = json.loads(raw[0])
        events = state.get('events') if isinstance(state, dict) else None
        if (not isinstance(events, list) or len(events) > 8192
                or any(not isinstance(e, dict) or type(e.get('seq')) is not int
                       or e['seq'] != i for i, e in enumerate(events, 1))):
            raise NotificationError('INVALID_EVENT_LEDGER')
        return events

    def _sync(self):
        events = self._events()
        latest = len(events)
        info = self.source.stat()
        identity = str(self.source) + ':' + str(info.st_dev) + ':' + str(info.st_ino)
        with self._tx() as db:
            row = db.execute('SELECT seq,source FROM desktop_cursor WHERE id=1').fetchone()
            if row is None:
                db.execute('INSERT INTO desktop_cursor VALUES (1,?,?)', (latest, identity))
                return 0
            cursor, original = row
            if original != identity or latest < cursor:
                raise NotificationError('DESKTOP_SOURCE_CHANGED')
            for event in events[cursor:]:
                prepared = _event_payload(event)
                if prepared is None:
                    if _recognized_notification_source(event):
                        raise NotificationError('INVALID_SOURCE_EVENT')
                    continue
                event_id, _name, _task, payload = prepared
                db.execute('''INSERT OR IGNORE INTO desktop_deliveries
                    (event_id,seq,payload,status,next_attempt_at,occurred_at)
                    VALUES (?,?,?,'PENDING',?,?)''',
                    (event_id, event['seq'], payload, self.clock(), event['time']))
            db.execute('UPDATE desktop_cursor SET seq=? WHERE id=1', (latest,))
        return latest - cursor

    def run_once(self, sender):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            _private(self.lock_path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'status': 'BUSY'}
            # A previous process died after committing its delivery intent.
            with self._tx() as db:
                db.execute("UPDATE desktop_deliveries SET status='UNKNOWN',"
                           "error_code='PROCESS_INTERRUPTED' WHERE status='SENDING'")
            imported = self._sync()
            count = 0
            started = time.monotonic()
            while count < 20 and time.monotonic() - started < 15:
                with self._tx() as db:
                    row = db.execute('''SELECT event_id,payload,attempts FROM desktop_deliveries
                        WHERE status IN ('PENDING','RETRY') AND next_attempt_at<=?
                        ORDER BY seq LIMIT 1''', (self.clock(),)).fetchone()
                    if row is None:
                        break
                    event_id, payload, attempts = row
                    db.execute("UPDATE desktop_deliveries SET status='SENDING',attempts=attempts+1 "
                               'WHERE event_id=?', (event_id,))
                try:
                    result = sender(json.loads(payload))
                except Exception:
                    result = {'status': 'UNKNOWN', 'code': 'DESKTOP_RESULT_UNKNOWN'}
                status = result.get('status') if isinstance(result, dict) else None
                if status not in ('DELIVERED', 'RETRY', 'UNKNOWN'):
                    status = 'UNKNOWN'
                desktop_id = result.get('desktop_id') if status == 'DELIVERED' else None
                if status == 'DELIVERED' and (type(desktop_id) is not int or desktop_id < 1):
                    status, desktop_id = 'UNKNOWN', None
                if status == 'RETRY' and attempts + 1 >= self.max_attempts:
                    status = 'FAILED'
                code = (None if status == 'DELIVERED' else
                        'DESKTOP_UNAVAILABLE' if status in ('RETRY', 'FAILED') else 'DESKTOP_RESULT_UNKNOWN')
                now = self.clock()
                with self._tx() as db:
                    db.execute('''UPDATE desktop_deliveries SET status=?,desktop_id=?,error_code=?,
                        next_attempt_at=?,delivered_at=?,latency_ms=CASE WHEN ?='DELIVERED'
                        THEN MAX(0,(?-occurred_at)*1000) ELSE NULL END WHERE event_id=?''',
                        (status, desktop_id, code, now + min(60, 2 ** min(attempts, 6)),
                         now if status == 'DELIVERED' else None, status, now, event_id))
                count += 1
            return {'status': 'OK', 'imported': imported, 'attempted': count, **self.summary()}
        finally:
            os.close(fd)

    def initialize(self):
        """Persist the first-run baseline without delivering any notifications."""
        self._sync()

    @staticmethod
    def _summary(db):
        row = db.execute('SELECT seq FROM desktop_cursor WHERE id=1').fetchone()
        latest = db.execute('SELECT delivered_at,latency_ms FROM desktop_deliveries '
                            "WHERE status='DELIVERED' ORDER BY delivered_at DESC LIMIT 1").fetchone()
        return {'cursor': row[0] if row else None,
                'delivery_states': dict(db.execute(
                    'SELECT status,COUNT(*) FROM desktop_deliveries GROUP BY status')),
                'last_delivery_at': latest[0] if latest else None,
                'last_latency_ms': latest[1] if latest else None,
                'chat_wake': False}

    @classmethod
    def read_summary(cls, path):
        """Read an existing queue without creating it or changing receipts."""
        path = Path(path).absolute()
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise NotificationError('UNSAFE_DESKTOP_PATH')
        _private(path)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            return cls._summary(db)

    def summary(self):
        with self._tx() as db:
            return self._summary(db)
