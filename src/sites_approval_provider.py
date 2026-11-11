"""Optional HTTPS approval provider. Inert until a host supplies approved config.

Credentials and owner pairing are host-only dependencies, never MCP arguments.
No enrollment, listener, environment discovery, or service activation occurs here.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import ssl
import stat
import time
from urllib.parse import urlsplit
from urllib.request import (Request, build_opener, HTTPSHandler, HTTPRedirectHandler,
                            ProxyHandler)
import uuid

from approval_portal import ApprovalPortal, PortalError, _binding, _packed
from codex_approvals import COMMAND, FILE
from private_state import _private, _json


@dataclass(frozen=True)
class SitesApprovalConfig:
    origin: str
    owner_binding: str
    machine_key: str = field(repr=False)
    site_access_token: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.origin, str):
            raise PortalError('EXACT_HTTPS_ORIGIN_REQUIRED')
        p = urlsplit(self.origin)
        if (not self.origin.isascii() or self.origin != 'https://' + p.netloc
                or not p.hostname or p.netloc != p.hostname or p.username or p.password or p.path or p.query or p.fragment
                or any(ord(c) < 33 or ord(c) > 126 for c in self.origin)):
            raise PortalError('EXACT_HTTPS_ORIGIN_REQUIRED')
        try:
            if p.port is not None: raise ValueError()
        except ValueError:
            raise PortalError('EXACT_HTTPS_ORIGIN_REQUIRED') from None
        if not re.fullmatch(r'[a-zA-Z0-9_-]{16,128}', self.owner_binding):
            raise PortalError('EXPLICIT_OWNER_PAIRING_REQUIRED')
        for secret in (self.machine_key, self.site_access_token):
            if not isinstance(secret, str) or not 32 <= len(secret) <= 4096 or any(
                    ord(c) < 33 or ord(c) > 126 for c in secret):
                raise PortalError('APPROVED_MACHINE_CREDENTIAL_REQUIRED')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class SitesHTTPS:
    """Fixed-origin, verified TLS, bounded, no redirects or automatic retries."""
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(ProxyHandler({}), _NoRedirect(),
                                   HTTPSHandler(context=ssl.create_default_context()))

    def __call__(self, method, path, payload):
        if not re.fullmatch(r'/api/laf/approvals/[0-9a-f-]{36}(?:/consume)?', path):
            raise PortalError('INVALID_PROVIDER_PATH')
        data = None if payload is None else _packed(payload).encode()
        request = Request(self.config.origin + path, data=data, method=method, headers={
            'Authorization': 'Bearer ' + self.config.machine_key,
            'OAI-Sites-Authorization': 'Bearer ' + self.config.site_access_token,
            'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=5) as response:
                if response.status != 200 or response.headers.get_content_type() != 'application/json':
                    raise ValueError()
                body = response.read(8193)
                if len(body) > 8192: raise ValueError()
                return _json(body)
        except Exception:
            # Never propagate a credential, redirect URL, response body or header.
            raise PortalError('REMOTE_APPROVAL_UNAVAILABLE') from None


class SitesApprovalProvider:
    """ApprovalPortal-compatible bridge with a durable local consume fence."""
    def __init__(self, root, lookup, *, config=None, transport=None, clock=time.time, ttl=120):
        self.enabled = config is not None
        if config is not None and not isinstance(config, SitesApprovalConfig):
            raise PortalError('INVALID_PROVIDER_CONFIGURATION')
        if not callable(lookup) or type(ttl) is not int or not 1 <= ttl <= 300:
            raise PortalError('INVALID_PROVIDER_CONFIGURATION')
        self.config, self.lookup, self.clock, self.ttl = config, lookup, clock, ttl
        self.transport = transport if transport is not None else SitesHTTPS(config) if config else None
        self.root = Path(root).absolute()
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise PortalError('PRIVATE_PROVIDER_STORE_REQUIRED')
        info = self.root.stat()
        if not self.root.is_dir() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PortalError('PRIVATE_PROVIDER_STORE_REQUIRED')
        self.db = self.root / 'sites-approval.sqlite3'
        if not self.db.exists():
            fd = os.open(self.db, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        with self._tx() as db:
            db.execute('CREATE TABLE IF NOT EXISTS interactions ('
                       'challenge TEXT PRIMARY KEY, approval_id TEXT NOT NULL, binding TEXT NOT NULL,'
                       'request_sha256 TEXT NOT NULL, expires INTEGER NOT NULL, state TEXT NOT NULL,'
                       'receipt_sha256 TEXT)')

    @contextmanager
    def _tx(self):
        _private(self.db, 8 * 1024**2)
        db = sqlite3.connect(self.db, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally: db.close()

    def _current(self, approval_id):
        if not self.enabled: raise PortalError('HUMAN_AUTHENTICATION_CHANNEL_UNAVAILABLE')
        record = self.lookup(approval_id)
        if (not isinstance(record, dict) or record.get('id') != approval_id
                or record.get('status') != 'PENDING' or record.get('method') not in (COMMAND, FILE)
                or record.get('response_supported') is not True or not ApprovalPortal._allowed(record)):
            raise PortalError('APPROVAL_NOT_PENDING_OR_NATIVE_ONLY')
        return record

    def _row(self, challenge):
        with self._tx() as db:
            row = db.execute('SELECT * FROM interactions WHERE challenge=?', (challenge,)).fetchone()
        if row is None: raise PortalError('UNKNOWN_INTERACTION')
        return dict(row)

    def _envelope(self, row):
        return {'approval_id': row['approval_id'], 'binding_sha256': row['binding'],
                'request_sha256': row['request_sha256'], 'owner_binding': self.config.owner_binding,
                'expires_at': row['expires'], 'challenge_id': row['challenge']}

    def _request(self, method, row, payload=None, *, consume=False):
        path = '/api/laf/approvals/' + row['challenge'] + ('/consume' if consume else '')
        try: result = self.transport(method, path, payload)
        except Exception: raise PortalError('REMOTE_APPROVAL_UNAVAILABLE') from None
        expected = self._envelope(row)
        if (not isinstance(result, dict) or any(result.get(k) != v for k, v in expected.items())
                or result.get('execution_authorized') is not False
                or result.get('status') not in ('PENDING', 'DECIDED', 'CONSUMED', 'EXPIRED', 'REVOKED')):
            raise PortalError('REMOTE_APPROVAL_BINDING_FAILED')
        if result['status'] == 'DECIDED' and result.get('decision') not in ('accept', 'decline'):
            raise PortalError('REMOTE_APPROVAL_BINDING_FAILED')
        return result

    def issue(self, approval_id):
        record = self._current(approval_id)
        binding = _binding(record)
        now = int(self.clock())
        with self._tx() as db:
            prior = db.execute('SELECT * FROM interactions WHERE approval_id=?', (approval_id,)).fetchall()
            if any(r['state'] in ('CONSUMING', 'CONSUMED', 'UNKNOWN') for r in prior):
                raise PortalError('APPROVAL_CONSUMED_OR_RECONCILIATION_REQUIRED')
            active = [r for r in prior if r['expires'] > now]
            if active:
                if len(active) != 1 or active[0]['binding'] != binding:
                    raise PortalError('APPROVAL_CHANGED')
                row = dict(active[0])
            else:
                row = {'challenge': str(uuid.uuid4()), 'approval_id': approval_id, 'binding': binding,
                       'request_sha256': record['request_sha256'], 'expires': now + self.ttl}
                db.execute("INSERT INTO interactions VALUES (?,?,?,?,?,'ACTIVE',NULL)",
                           tuple(row[k] for k in ('challenge', 'approval_id', 'binding', 'request_sha256', 'expires')))
        fields = ('id', 'task_id', 'thread_id', 'native_thread_id', 'native_turn_id', 'request_sha256',
                  'method', 'action', 'target', 'command_preview', 'reason_preview', 'risk',
                  'accept_supported', 'response_supported', 'offered_decisions')
        payload = {**self._envelope(row), 'record': {k: record.get(k) for k in fields}}
        if len(_packed(payload).encode()) > 16384: raise PortalError('APPROVAL_PREVIEW_TOO_LARGE')
        result = self._request('PUT', row, payload)
        if self.clock() >= row['expires'] or result['status'] not in ('PENDING', 'DECIDED'):
            raise PortalError('INTERACTION_NOT_PENDING')
        return {'challenge_id': row['challenge'], 'url': self.config.origin + '/approvals/' + row['challenge'],
                'approval_id': approval_id, 'request_sha256': record['request_sha256'],
                'human_final_approval_required': True, 'execution_authorized': False}

    def interaction_status(self, challenge):
        row = self._row(challenge)
        record = self._current(row['approval_id'])
        if _binding(record) != row['binding']: raise PortalError('APPROVAL_CHANGED')
        if row['expires'] <= self.clock(): return {'status': 'EXPIRED', 'decision': None, 'execution_authorized': False}
        result = self._request('GET', row)
        if self.clock() >= row['expires']:
            return {'status': 'EXPIRED', 'decision': None, 'execution_authorized': False}
        return {'status': result['status'], 'decision': result.get('decision') if result['status'] == 'DECIDED' else None,
                'execution_authorized': False}

    def consume(self, record, decision):
        current = self._current(record['id'])
        if _binding(current) != _binding(record): raise PortalError('APPROVAL_CHANGED')
        if decision not in ApprovalPortal._allowed(current): raise PortalError('DECISION_NOT_OFFERED')
        with self._tx() as db:
            rows = db.execute("SELECT * FROM interactions WHERE approval_id=? AND binding=? AND expires>? AND state='ACTIVE'",
                              (record['id'], _binding(record), self.clock())).fetchall()
            if len(rows) != 1: raise PortalError('NO_UNAMBIGUOUS_HUMAN_DECISION')
            row = dict(rows[0])
        status = self._request('GET', row)
        if status['status'] != 'DECIDED' or status.get('decision') != decision:
            raise PortalError('NO_UNAMBIGUOUS_HUMAN_DECISION')
        with self._tx() as db:
            changed = db.execute("UPDATE interactions SET state='CONSUMING' WHERE challenge=? AND state='ACTIVE' AND expires>?",
                                 (row['challenge'], self.clock())).rowcount
            if changed != 1: raise PortalError('APPROVAL_CONSUMED_OR_RECONCILIATION_REQUIRED')
        try:
            result = self._request('POST', row, {**self._envelope(row), 'decision': decision}, consume=True)
            receipt = result.get('receipt')
            if result['status'] != 'CONSUMED' or result.get('decision') != decision or not isinstance(receipt, str) or not re.fullmatch(r'[0-9a-f]{64}', receipt):
                raise PortalError('REMOTE_APPROVAL_BINDING_FAILED')
            digest = hashlib.sha256(receipt.encode()).hexdigest()
            with self._tx() as db:
                db.execute("UPDATE interactions SET state='CONSUMED',receipt_sha256=? WHERE challenge=? AND state='CONSUMING'", (digest, row['challenge']))
            if self.clock() >= row['expires']:
                raise PortalError('HUMAN_DECISION_EXPIRED_AFTER_CONSUMPTION')
            return 'sites-human:' + receipt
        except BaseException:
            with self._tx() as db:
                db.execute("UPDATE interactions SET state='UNKNOWN' WHERE challenge=? AND state='CONSUMING'", (row['challenge'],))
            raise
