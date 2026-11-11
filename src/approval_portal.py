"""Inert authenticated approval-page core; no listener or production authenticator.

Authentication and owner mapping must be injected by a trusted host, never MCP
arguments. URL navigation and elicitation responses cannot authorize execution.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from codex_approvals import COMMAND, FILE
from private_state import _private


class PortalError(ValueError):
    pass


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _packed(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _binding(record):
    fields = ('id', 'task_id', 'thread_id', 'native_thread_id', 'native_turn_id',
              'request_sha256', 'method', 'action', 'target', 'command_preview',
              'reason_preview', 'risk', 'accept_supported', 'response_supported', 'offered_decisions')
    return _hash(_packed({k: record.get(k) for k in fields}))


@dataclass(frozen=True)
class VerifiedHumanSession:
    """Trusted authenticator output, not an HTTP/MCP input schema."""
    subject: str
    session_id: str
    authenticated_at: float


class ApprovalPortal:
    """Private durable one-shot receipts, with explicit authentication boundaries.

    Construction creates only the selected local private fixture/store. No
    authentication provider is implemented or activated by this module.
    """
    def __init__(self, root, lookup, *, origin, authenticate=None, authorize=None,
                 clock=time.time, ttl=120):
        self.root = Path(root).absolute()
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise PortalError('UNSAFE_PORTAL_PATH')
        info = self.root.stat()
        if (not self.root.is_dir() or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise PortalError('PRIVATE_PORTAL_STORE_REQUIRED')
        parsed = urlsplit(origin)
        try:
            _ = parsed.port
        except ValueError:
            raise PortalError('HTTPS_PORTAL_ORIGIN_REQUIRED') from None
        if (not isinstance(origin, str) or not origin.isascii()
                or any(ord(c) < 33 or ord(c) > 126 for c in origin)
                or origin != 'https://' + parsed.netloc
                or parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise PortalError('HTTPS_PORTAL_ORIGIN_REQUIRED')
        if not callable(lookup) or type(ttl) not in (int, float) or not 1 <= ttl <= 300:
            raise PortalError('INVALID_PORTAL_CONFIGURATION')
        if any(p is not None and not callable(p) for p in (authenticate, authorize)):
            raise PortalError('INVALID_AUTHENTICATOR')
        self.lookup, self.origin, self.clock, self.ttl = lookup, origin, clock, ttl
        self.authority = parsed.netloc
        self.authenticate, self.authorize = authenticate, authorize
        self.db = self.root / 'approval-portal.sqlite3'
        if not self.db.exists():
            fd = os.open(self.db, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        with self._tx() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS interactions (
                challenge TEXT PRIMARY KEY, approval_id TEXT NOT NULL,
                request_sha256 TEXT NOT NULL, binding_sha256 TEXT NOT NULL,
                expires REAL NOT NULL, status TEXT NOT NULL,
                subject_sha256 TEXT, session_sha256 TEXT, csrf_sha256 TEXT,
                decision TEXT, decided_at REAL, receipt_sha256 TEXT)''')

    @property
    def enabled(self):
        return self.authenticate is not None and self.authorize is not None

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
        finally:
            db.close()

    def _require_enabled(self):
        if not self.enabled:
            raise PortalError('HUMAN_AUTHENTICATION_CHANNEL_UNAVAILABLE')

    def _record(self, approval_id):
        record = self.lookup(approval_id)
        if (not isinstance(record, dict) or record.get('id') != approval_id
                or record.get('status') != 'PENDING'
                or record.get('method') not in (COMMAND, FILE)
                or record.get('response_supported') is not True):
            raise PortalError('APPROVAL_NOT_PENDING_OR_NATIVE_ONLY')
        if not self._allowed(record):
            raise PortalError('APPROVAL_NOT_PENDING_OR_NATIVE_ONLY')
        return record

    @staticmethod
    def _allowed(record):
        offered = record.get('offered_decisions')
        if not isinstance(offered, list):
            return ()
        return tuple(d for d in ('accept', 'decline') if d in offered
                     and (d != 'accept' or record.get('accept_supported') is True))

    def _session(self, headers, record):
        self._require_enabled()
        # The provider verifies its own credential/session. Names or booleans
        # supplied in a tool call or HTTP form are not accepted as proof.
        session = self.authenticate(headers)
        if (not isinstance(session, VerifiedHumanSession)
                or not isinstance(session.subject, str) or not session.subject
                or not isinstance(session.session_id, str) or not session.session_id
                or type(session.authenticated_at) not in (int, float)
                or not 0 <= self.clock() - session.authenticated_at <= 600):
            raise PortalError('HUMAN_AUTHENTICATION_REQUIRED')
        if self.authorize(session.subject, record) is not True:
            raise PortalError('APPROVAL_NOT_OWNED')
        return session

    def issue(self, approval_id):
        self._require_enabled()
        record = self._record(approval_id)
        # Opaque public routing ID only. It is never a bearer authorization.
        challenge = str(uuid.uuid4())
        with self._tx() as db:
            # Expired, unused human evidence stays in audit but authorizes nothing.
            # A new interaction requires a fresh authenticated human decision.
            # CONSUMED is never reset, including after expiry or uncertain IO.
            db.execute("UPDATE interactions SET status='EXPIRED',csrf_sha256=NULL "
                       "WHERE approval_id=? AND status IN ('PENDING','DECIDED') AND expires<=?",
                       (record['id'], self.clock()))
            prior = db.execute('SELECT * FROM interactions WHERE approval_id=?', (record['id'],)).fetchall()
            if any(row['status'] in ('DECIDED', 'CONSUMED') for row in prior):
                raise PortalError('HUMAN_DECISION_ALREADY_RECORDED')
            pending = [row for row in prior if row['status'] == 'PENDING' and row['expires'] > self.clock()]
            if pending:
                if len(pending) != 1 or pending[0]['binding_sha256'] != _binding(record):
                    raise PortalError('APPROVAL_CHANGED')
                challenge = pending[0]['challenge']
            else:
                db.execute("UPDATE interactions SET status='EXPIRED',csrf_sha256=NULL WHERE approval_id=? AND status='PENDING'", (record['id'],))
                db.execute('INSERT INTO interactions '
                       '(challenge,approval_id,request_sha256,binding_sha256,expires,status) '
                       "VALUES (?,?,?,?,?,'PENDING')", (challenge, record['id'],
                       record['request_sha256'], _binding(record), self.clock() + self.ttl))
        return {'challenge_id': challenge, 'url': self.origin + '/approvals/' + challenge,
                'approval_id': record['id'], 'request_sha256': record['request_sha256'],
                'human_final_approval_required': True, 'execution_authorized': False}

    def _interaction(self, db, challenge):
        row = db.execute('SELECT * FROM interactions WHERE challenge=?', (challenge,)).fetchone()
        if row is None or row['status'] != 'PENDING' or self.clock() >= row['expires']:
            raise PortalError('INTERACTION_NOT_PENDING')
        record = self._record(row['approval_id'])
        if (_binding(record) != row['binding_sha256']
                or record['request_sha256'] != row['request_sha256']):
            raise PortalError('APPROVAL_CHANGED')
        return row, record

    def form(self, challenge, headers):
        self._require_enabled()
        with self._tx() as db:
            row, record = self._interaction(db, challenge)
            session = self._session(headers, record)
            csrf = secrets.token_urlsafe(32)
            db.execute('UPDATE interactions SET subject_sha256=?,session_sha256=?,csrf_sha256=? '
                       'WHERE challenge=?', (_hash(session.subject), _hash(session.session_id),
                       _hash(csrf), challenge))
        escape = lambda value: html.escape(str(value), quote=True)
        accept = ('<button name="decision" value="accept">Approve once</button>'
                  if 'accept' in self._allowed(record) else
                  '<p>Acceptance requires the original native human interface.</p>')
        decline = ('<button name="decision" value="decline">Decline once</button>'
                   if 'decline' in self._allowed(record) else '')
        body = ('<!doctype html><meta charset="utf-8"><title>LAF approval</title>'
                '<h1>Review one pending action</h1><p>Untrusted request content follows.</p>'
                '<pre>' + escape(_packed({k: record.get(k) for k in
                    ('id', 'task_id', 'request_sha256', 'action', 'target', 'risk',
                     'command_preview', 'reason_preview')})) + '</pre>'
                '<form method="post"><input type="hidden" name="csrf" value="' + escape(csrf) + '">'
                '<input type="hidden" name="request_sha256" value="' + escape(row['request_sha256']) + '">'
                + accept + decline + '</form>')
        return body

    def decide(self, challenge, headers, body):
        self._require_enabled()
        if (headers.get('Origin') != self.origin
                or headers.get('Content-Type') != 'application/x-www-form-urlencoded'
                or not isinstance(body, bytes) or len(body) > 4096):
            raise PortalError('INVALID_BROWSER_DECISION')
        try:
            fields = parse_qs(body.decode('ascii'), strict_parsing=True,
                              keep_blank_values=True, max_num_fields=3)
        except (UnicodeError, ValueError):
            raise PortalError('INVALID_BROWSER_DECISION') from None
        if (set(fields) != {'csrf', 'decision', 'request_sha256'}
                or any(len(v) != 1 for v in fields.values())
                or fields['decision'][0] not in ('accept', 'decline')):
            raise PortalError('INVALID_BROWSER_DECISION')
        decision = fields['decision'][0]
        with self._tx() as db:
            row, record = self._interaction(db, challenge)
            session = self._session(headers, record)
            if (not row['csrf_sha256']
                    or not hmac.compare_digest(_hash(fields['csrf'][0]), row['csrf_sha256'])
                    or _hash(session.subject) != row['subject_sha256']
                    or _hash(session.session_id) != row['session_sha256']
                    or fields['request_sha256'][0] != row['request_sha256']):
                raise PortalError('BROWSER_DECISION_BINDING_FAILED')
            if decision not in self._allowed(record):
                raise PortalError('NATIVE_HUMAN_INTERFACE_REQUIRED_OR_DECISION_NOT_OFFERED')
            db.execute("UPDATE interactions SET status='DECIDED',decision=?,decided_at=?,csrf_sha256=NULL "
                       'WHERE challenge=?', (decision, self.clock(), challenge))
        return {'status': 'DECISION_RECORDED', 'approval_id': row['approval_id'],
                'request_sha256': row['request_sha256'], 'decision': decision,
                'execution_authorized': False}

    def interaction_status(self, challenge):
        """Bounded host/MCP status; never exposes or creates a proof."""
        self._require_enabled()
        with self._tx() as db:
            row = db.execute('SELECT * FROM interactions WHERE challenge=?', (challenge,)).fetchone()
            if row is None:
                raise PortalError('UNKNOWN_INTERACTION')
            record = self.lookup(row['approval_id'])
            state = row['status']
            if (not isinstance(record, dict) or record.get('id') != row['approval_id']
                    or record.get('request_sha256') != row['request_sha256']
                    or _binding(record) != row['binding_sha256']
                    or record.get('status') != 'PENDING'):
                state = 'STALE'
            elif state != 'CONSUMED' and self.clock() >= row['expires']:
                state = 'EXPIRED'
            return {'status': state, 'decision': row['decision'] if state == 'DECIDED' else None,
                    'execution_authorized': False}

    def consume(self, record, decision):
        """Host-only gate for the existing Pipeline; never exposed as MCP input."""
        self._require_enabled()
        if decision not in ('accept', 'decline'):
            raise PortalError('INVALID_DECISION')
        current = self._record(record['id'])
        if _binding(current) != _binding(record):
            raise PortalError('APPROVAL_CHANGED')
        if decision not in self._allowed(current):
            raise PortalError('NATIVE_HUMAN_INTERFACE_REQUIRED_OR_DECISION_NOT_OFFERED')
        receipt = 'human-portal:' + secrets.token_urlsafe(32)
        with self._tx() as db:
            consumed = db.execute("SELECT 1 FROM interactions WHERE approval_id=? AND status='CONSUMED'", (record['id'],)).fetchone()
            if consumed:
                raise PortalError('NO_UNAMBIGUOUS_HUMAN_DECISION')
            rows = db.execute("SELECT * FROM interactions WHERE approval_id=? AND request_sha256=? "
                              "AND binding_sha256=? AND decision=? AND status='DECIDED' AND expires>?",
                              (record['id'], record['request_sha256'], _binding(record),
                               decision, self.clock())).fetchall()
            if len(rows) != 1:
                raise PortalError('NO_UNAMBIGUOUS_HUMAN_DECISION')
            db.execute("UPDATE interactions SET status='CONSUMED',receipt_sha256=? WHERE challenge=?",
                       (_hash(receipt), rows[0]['challenge']))
        return receipt


    def handle(self, method, path, headers, body=b''):
        """Pure HTTP boundary for an approved host; never binds a socket."""
        response_headers = {
            'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin',
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}
        try:
            if headers.get('Host') != self.authority:
                raise PortalError('INVALID_PORTAL_HOST')
            if not path.startswith('/approvals/'):
                return 404, response_headers, b''
            challenge = path[len('/approvals/'):]
            if str(uuid.UUID(challenge)) != challenge:
                raise PortalError('INVALID_INTERACTION_ID')
            if method == 'GET':
                result = self.form(challenge, headers).encode()
                response_headers['Content-Type'] = 'text/html; charset=utf-8'
                return 200, response_headers, result
            if method == 'POST':
                result = self.decide(challenge, headers, body)
                response_headers['Content-Type'] = 'application/json'
                return 200, response_headers, _packed(result).encode()
            return 405, {**response_headers, 'Allow': 'GET, POST'}, b''
        except PortalError as error:
            code = str(error)
            status = (503 if code == 'HUMAN_AUTHENTICATION_CHANNEL_UNAVAILABLE' else
                      401 if code == 'HUMAN_AUTHENTICATION_REQUIRED' else
                      403 if code in ('APPROVAL_NOT_OWNED', 'INVALID_PORTAL_HOST',
                                      'BROWSER_DECISION_BINDING_FAILED') else 409)
            return status, response_headers, _packed({'error': code}).encode()
        except Exception:
            return 503, response_headers, b'{"error":"PORTAL_UNAVAILABLE"}'


def mcp_interaction(interaction, capabilities, request_id):
    """Build a negotiated URL elicitation descriptor; does not send or approve.

    A transport must correlate this server request and deliver it to the actual
    client. An accept reply means navigation consent only, never a gate receipt.
    """
    value = {'interaction': interaction, 'execution_authorized': False,
             'elicitation_required': False, 'server_request': None}
    elicitation = capabilities.get('elicitation') if isinstance(capabilities, dict) else None
    if isinstance(elicitation, dict) and isinstance(elicitation.get('url'), dict):
        if (type(request_id) not in (str, int)
                or type(request_id) is str and not 1 <= len(request_id.encode()) <= 128
                or type(request_id) is int and not -(2**63) <= request_id < 2**63):
            raise PortalError('INVALID_ELICITATION_REQUEST_ID')
        value.update(elicitation_required=True, server_request={
            'jsonrpc': '2.0', 'id': request_id, 'method': 'elicitation/create',
            'params': {'mode': 'url', 'elicitationId': interaction['challenge_id'],
                       'url': interaction['url'],
                       'message': 'Review the exact pending LAF action on the authenticated approval page.'}})
    return value
