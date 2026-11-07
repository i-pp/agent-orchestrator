"""Opt-in durable MCP Events delivery outbox.

This module is a local prototype. It has no HTTP listener or configured network
transport. The MCP Events service validates its local principal before calling
these storage APIs. Callers must provide a secret-encryption provider and a
transport that pins validated public IPs, preserves TLS hostname checks, and
refuses redirects. No transport is configured by default.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import sqlite3
import stat
import threading
import time
from urllib.parse import urlsplit
import uuid

from local_principal import principal_identity
from total_deadline import TotalDeadline


EVENT_NAMES = {
    'task_completed': ('codex.task.completed', 'COMPLETED'),
    'task_failed': ('codex.task.failed', 'FAILED'),
    'task_cancelled': ('codex.task.cancelled', 'CANCELLED'),
    'approval_waiting': ('codex.task.approval_required', 'WAITING_APPROVAL'),
    'task_blocked': ('codex.task.action_required', 'BLOCKED'),
}
BLOCKED_REASON_CODES = frozenset({
    'CATALOG_REJECTED',
    'MODEL_NOT_AVAILABLE_OR_ALLOWED',
    'SUBSCRIPTION_AUTH_REQUIRED',
    'RESUME_REJECTED',
    'RESUME_SCOPE_MISMATCH',
})
DELIVERY_STATES = frozenset({
    'PENDING', 'DELIVERING', 'RETRY', 'UNKNOWN', 'DELIVERED',
    'PERMANENT_FAILURE', 'UNKNOWN_FINAL', 'CANCELLED', 'EXPIRED',
})
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_OUTBOX_ROWS = 4096
MAX_OUTBOX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_SUBSCRIPTIONS = 256
MIN_TTL_MS = 60_000
MAX_TTL_MS = 30 * 24 * 60 * 60 * 1000
CALLBACK_VERIFICATION_CACHE_SECONDS = 300
SECRET_ROTATION_SECONDS = 300
MAX_CALLBACK_RESPONSE_BYTES = 4096


class NotificationError(ValueError):
    """Fixed-code notification error; never include secrets or response bodies."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def owner_digest(authenticated_principal, key):
    identity = principal_identity(authenticated_principal)
    if (not isinstance(identity, str) or not identity
            or len(identity.encode('utf-8')) > 512):
        raise NotificationError('AUTHENTICATED_PRINCIPAL_REQUIRED')
    if not isinstance(key, bytes) or len(key) < 32:
        raise NotificationError('OWNER_HASH_KEY_UNAVAILABLE')
    return hmac.new(key, ('aceis-mcp-owner:v1:' + identity).encode('utf-8'),
                    hashlib.sha256).hexdigest()


def validate_signing_secret(secret):
    if not isinstance(secret, str) or not secret.startswith('whsec_'):
        raise NotificationError('INVALID_WEBHOOK_SECRET')
    try:
        raw = base64.b64decode(secret[6:], validate=True)
    except (ValueError, base64.binascii.Error):
        raise NotificationError('INVALID_WEBHOOK_SECRET') from None
    if not 24 <= len(raw) <= 64:
        raise NotificationError('INVALID_WEBHOOK_SECRET')
    return raw


def sign_request(secret, event_id, timestamp, body, *, additional_secrets=()):
    """Build Standard Webhooks headers over the exact serialized body bytes."""
    if not isinstance(body, bytes) or len(body) > MAX_PAYLOAD_BYTES:
        raise NotificationError('INVALID_PAYLOAD')
    stamp = str(int(timestamp))
    signed = event_id.encode('utf-8') + b'.' + stamp.encode('ascii') + b'.' + body
    signatures = []
    for candidate in (secret, *tuple(additional_secrets)):
        key = validate_signing_secret(candidate)
        mac = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode('ascii')
        signatures.append('v1,' + mac)
    return {
        'Content-Type': 'application/json',
        'webhook-id': event_id,
        'webhook-timestamp': stamp,
        'webhook-signature': ' '.join(signatures),
    }


def verify_challenge(expected, echoed):
    """Constant-time comparison for the single-use callback verification echo."""
    if not isinstance(expected, str) or not isinstance(echoed, str):
        return False
    if len(expected) > 256 or len(echoed) > 256:
        return False
    return hmac.compare_digest(expected.encode('utf-8'), echoed.encode('utf-8'))


def normalize_callback_url(url):
    """Canonicalize the URL identity without changing its path or query semantics."""
    if not isinstance(url, str) or len(url) > 2048:
        raise NotificationError('INVALID_CALLBACK_URL')
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise NotificationError('INVALID_CALLBACK_URL') from None
    host = parsed.hostname
    if (parsed.scheme.lower() != 'https' or not host or parsed.username is not None
            or parsed.password is not None or parsed.fragment or '#' in url or port not in (None, 443)):
        raise NotificationError('INVALID_CALLBACK_URL')
    if any(ord(char) < 0x21 or ord(char) > 0x7e for char in url) or '\\' in url:
        raise NotificationError('INVALID_CALLBACK_URL')
    host = host.lower().rstrip('.')
    if host in {'localhost', 'localhost.localdomain'}:
        raise NotificationError('UNSAFE_CALLBACK_HOST')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise NotificationError('UNSAFE_CALLBACK_HOST')
    labels = host.split('.')
    if (len(labels) < 2 or len(host) > 253
            or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-')
                   or re.fullmatch(r'[a-z0-9-]+', label) is None for label in labels)):
        raise NotificationError('UNSAFE_CALLBACK_HOST')
    path = parsed.path or '/'
    if any(ord(char) < 0x21 or ord(char) == 0x7f for char in path + parsed.query):
        raise NotificationError('INVALID_CALLBACK_URL')
    return 'https://' + host + path + (('?' + parsed.query) if parsed.query else '')


class BoundedResolver:
    """Adapter for resolvers that enforce the supplied per-call timeout.

    The provider contract is essential: Python cannot forcibly cancel a blocked
    synchronous resolver. Providers that cannot honor this bound must not be
    used to enable MCP Events.
    """

    def __init__(self, provider, *, timeout=10.0):
        if (not callable(getattr(provider, 'resolve_with_timeout', None))
                or getattr(provider, 'interruptible', False) is not True
                or type(timeout) not in (int, float) or not 0 < timeout <= 10):
            raise NotificationError('BOUNDED_RESOLVER_REQUIRED')
        self.provider = provider
        self.timeout = float(timeout)
        self.interruptible = True

    def resolve_with_timeout(self, host, timeout_seconds):
        if (type(timeout_seconds) not in (int, float)
                or not timeout_seconds > 0):
            raise NotificationError('INVALID_RESOLVER_DEADLINE')
        budget = min(float(timeout_seconds), self.timeout)
        started = time.monotonic()
        addresses = self.provider.resolve_with_timeout(host, budget)
        if time.monotonic() - started > budget:
            raise TimeoutError('Callback DNS deadline exceeded')
        return addresses

    def __call__(self, host):
        return self.resolve_with_timeout(host, self.timeout)


def validate_callback_url(url, resolver=None, *, timeout_seconds=None):
    """Return validated public IPs; the transport must pin one for each connection."""
    if not callable(resolver):
        raise NotificationError('BOUNDED_RESOLVER_REQUIRED')
    normalized = normalize_callback_url(url)
    host = urlsplit(normalized).hostname
    try:
        if timeout_seconds is None:
            raw_addresses = resolver(host)
        else:
            bounded = getattr(resolver, 'resolve_with_timeout', None)
            if not callable(bounded):
                raise NotificationError('BOUNDED_RESOLVER_REQUIRED')
            raw_addresses = bounded(host, timeout_seconds)
        addresses = sorted(set(raw_addresses))
        parsed_addresses = [ipaddress.ip_address(address) for address in addresses]
    except (NotificationError, TimeoutError):
        raise
    except (OSError, ValueError, TypeError):
        raise NotificationError('CALLBACK_DNS_FAILED') from None
    if not parsed_addresses or any(not address.is_global for address in parsed_addresses):
        raise NotificationError('UNSAFE_CALLBACK_ADDRESS')
    return tuple(str(address) for address in parsed_addresses)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Direct HTTPS connection pinned to a validated IP, with hostname TLS checks."""

    def __init__(self, host, pinned_ip, *, timeout=10.0, context=None,
                 socket_factory=socket.create_connection):
        address = ipaddress.ip_address(pinned_ip)
        if not address.is_global:
            raise NotificationError('UNSAFE_CALLBACK_ADDRESS')
        context = context or ssl.create_default_context()
        if (not isinstance(context, ssl.SSLContext) or not context.check_hostname
                or context.verify_mode != ssl.CERT_REQUIRED):
            raise NotificationError('TLS_VERIFICATION_REQUIRED')
        self.pinned_ip = str(address)
        self._socket_factory = socket_factory
        super().__init__(host, port=443, timeout=timeout, context=context)

    def connect(self):
        # Never honor HTTP_PROXY/HTTPS_PROXY, CONNECT tunneling, or a hostname DNS lookup here.
        if self._tunnel_host:
            raise NotificationError('PROXY_TUNNEL_FORBIDDEN')
        raw = self._socket_factory((self.pinned_ip, self.port), self.timeout,
                                   self.source_address)
        try:
            # Publish the SSLSocket before handshake so the total-deadline
            # watchdog can interrupt a peer that trickles handshake bytes.
            self.sock = self._wrap_tls(raw)
            self.sock.settimeout(self.timeout)
            self.sock.do_handshake()
        except BaseException:
            if self.sock is not None:
                self.sock.close()
                self.sock = None
            else:
                raw.close()
            raise

    def _wrap_tls(self, raw):
        return self._context.wrap_socket(raw, server_hostname=self.host,
                                         do_handshake_on_connect=False)


class PinnedWebhookSender:
    """Minimal no-redirect HTTPS POST transport for MCP Events.

    This class is never configured by default. Tests inject a fake connection
    factory; production must separately review egress policy, CA roots and
    the resolver deployment before enabling it.
    """

    ALLOWED_HEADERS = frozenset({
        'Content-Type', 'webhook-id', 'webhook-timestamp',
        'webhook-signature', 'X-MCP-Subscription-Id',
    })

    def __init__(self, *, resolver=None, timeout=10.0,
                 connection_factory=PinnedHTTPSConnection):
        if not 0 < float(timeout) <= 10:
            raise NotificationError('INVALID_TRANSPORT_TIMEOUT')
        if (not isinstance(resolver, BoundedResolver)
                or not callable(getattr(resolver, 'resolve_with_timeout', None))):
            raise NotificationError('BOUNDED_RESOLVER_REQUIRED')
        self.resolver = resolver
        self.timeout = float(timeout)
        self.connection_factory = connection_factory

    def _request(self, url, pinned_ips, body, headers):
        normalized = normalize_callback_url(url)
        parsed = urlsplit(normalized)
        if len(body) > MAX_PAYLOAD_BYTES:
            raise NotificationError('PAYLOAD_TOO_LARGE')
        if (not isinstance(headers, dict)
                or set(headers) != self.ALLOWED_HEADERS
                or headers.get('Content-Type') != 'application/json'
                or any(not isinstance(value, str) or len(value) > 8192
                       or any(ord(char) < 0x20 or ord(char) > 0x7e for char in value)
                       for value in headers.values())):
            raise NotificationError('INVALID_WEBHOOK_HEADERS')
        connection = None
        def abort_connection():
            if connection is not None:
                sock = getattr(connection, 'sock', None)
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                try:
                    connection.close()
                except Exception:
                    pass

        deadline = TotalDeadline(self.timeout, abort_connection)
        deadline.start()
        try:
            # Include fresh DNS validation in the same end-to-end callback budget.
            current_ips = validate_callback_url(
                normalized, self.resolver, timeout_seconds=deadline.remaining)
            if deadline.expired:
                raise TimeoutError('Callback total deadline exceeded')
            if not current_ips or not pinned_ips:
                raise NotificationError('UNSAFE_CALLBACK_ADDRESS')
            ip = current_ips[0]
            target = parsed.path or '/'
            if parsed.query:
                target += '?' + parsed.query
            remaining = deadline.remaining
            if remaining <= 0:
                raise TimeoutError('Callback total deadline exceeded')
            connection = self.connection_factory(parsed.hostname, ip, timeout=remaining)
            connection.request('POST', target, body=body,
                               headers={**headers, 'Connection': 'close'})
            if deadline.expired:
                raise TimeoutError('Callback total deadline exceeded')
            response = connection.getresponse()
            if deadline.expired:
                raise TimeoutError('Callback total deadline exceeded')
            status = response.status
            response_body = response.read(MAX_CALLBACK_RESPONSE_BYTES + 1)
            if deadline.expired:
                raise TimeoutError('Callback total deadline exceeded')
            if len(response_body) > MAX_CALLBACK_RESPONSE_BYTES:
                response_body = b''
            return {'status': status, 'body': response_body}
        except BaseException:
            if deadline.expired:
                raise TimeoutError('Callback total deadline exceeded') from None
            raise
        finally:
            deadline.cancel()
            if connection is not None:
                connection.close()

    def __call__(self, url, pinned_ips, body, headers):
        result = self._request(url, pinned_ips, body, headers)
        return {'status': result['status']}

    def verify_callback(self, request):
        result = self._request(request['url'], request['pinned_ips'],
                               request['body'], request['headers'])
        echoed = None
        try:
            payload = json.loads(result['body'].decode('utf-8'))
            if isinstance(payload, dict) and isinstance(payload.get('challenge'), str):
                echoed = payload['challenge']
        except (UnicodeError, ValueError, TypeError):
            pass
        return {'status_code': result['status'], 'echoed_challenge': echoed}


def _event_payload(event):
    if not isinstance(event, dict) or type(event.get('seq')) is not int or event['seq'] < 1:
        return None
    source_kind = event.get('kind')
    kind = source_kind
    if source_kind == 'operation_failed' and event.get('operation') == 'dispatch':
        # A definitive dispatch rejection is recorded in the existing operation
        # event with task/thread IDs. Route it as a failed task without requiring
        # a second event-ledger slot.
        kind = 'task_failed'
    mapped = EVENT_NAMES.get(kind)
    task_id = event.get('task_id')
    if mapped is None or not isinstance(task_id, str) or not UUID_RE.fullmatch(task_id):
        return None
    reason_code = event.get('reason_code') if kind == 'task_blocked' else None
    if kind == 'task_blocked' and reason_code not in BLOCKED_REASON_CODES:
        return None
    event_name, state = mapped
    occurred = event.get('time')
    if type(occurred) not in (int, float):
        return None
    try:
        timestamp = datetime.fromtimestamp(occurred, tz=timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    except (OverflowError, OSError, ValueError):
        return None
    identity = {key: event.get(key) for key in ('seq', 'kind', 'task_id', 'thread_id', 'time')}
    if source_kind == 'operation_failed':
        identity['operation'] = event.get('operation')
    if kind == 'task_blocked':
        identity['reason_code'] = reason_code
    event_id = 'aceis_' + str(event['seq']) + '_' + hashlib.sha256(canonical(identity).encode('utf-8')).hexdigest()[:20]
    data = {'task_id': task_id, 'state': state}
    thread_id = event.get('thread_id')
    if isinstance(thread_id, str) and UUID_RE.fullmatch(thread_id):
        data['thread_id'] = thread_id
    if kind == 'task_blocked':
        data.update(action_required=True, reason_code=reason_code)
    payload = {'eventId': event_id, 'name': event_name, 'timestamp': timestamp,
               'data': data, 'cursor': str(event['seq'])}
    packed = canonical(payload).encode('utf-8')
    if len(packed) > MAX_PAYLOAD_BYTES:
        raise NotificationError('PAYLOAD_TOO_LARGE')
    return event_id, event_name, task_id, packed


def _recognized_notification_source(event):
    if not isinstance(event, dict):
        return False
    kind = event.get('kind')
    return (kind in EVENT_NAMES
            or (kind == 'operation_failed' and event.get('operation') == 'dispatch'))


class NotificationOutbox:
    """Per-subscription durable queue in a private SQLite database.

    Destination secrets are never persisted in plaintext. The caller must
    inject an authenticated SecretCipher and an explicit mock/live sender.
    The existing pipeline ledger is read-only and remains the event source.
    """

    def __init__(self, db_path, *, source_db_path, secret_cipher=None, owner_hash_key=None, clock=time.time,
                 resolver=None, base_backoff=1.0, max_backoff=60.0,
                 max_attempts=8, lease_seconds=30.0, max_outbox_rows=MAX_OUTBOX_ROWS,
                 max_payload_store_bytes=MAX_OUTBOX_PAYLOAD_BYTES,
                 max_subscriptions=MAX_SUBSCRIPTIONS):
        self.path = Path(db_path).absolute()
        self.source_path = Path(source_db_path).absolute()
        if self.path == self.source_path:
            raise NotificationError('OUTBOX_DATABASE_MUST_BE_SEPARATE')
        self.secret_cipher = secret_cipher
        if not isinstance(owner_hash_key, bytes) or len(owner_hash_key) < 32:
            raise NotificationError('OWNER_HASH_KEY_UNAVAILABLE')
        self.owner_hash_key = bytes(owner_hash_key)
        self.clock = clock
        if not callable(resolver):
            raise NotificationError('BOUNDED_RESOLVER_REQUIRED')
        self.resolver = resolver
        self.base_backoff = float(base_backoff)
        self.max_backoff = float(max_backoff)
        self.max_attempts = int(max_attempts)
        self.lease_seconds = float(lease_seconds)
        self.max_outbox_rows = int(max_outbox_rows)
        self.max_payload_store_bytes = int(max_payload_store_bytes)
        self.max_subscriptions = int(max_subscriptions)
        if (self.base_backoff <= 0 or self.max_backoff < self.base_backoff
                or self.max_attempts < 1 or self.lease_seconds <= 0
                or self.max_outbox_rows < 1 or self.max_payload_store_bytes < 1
                or self.max_subscriptions < 1):
            raise NotificationError('INVALID_RETRY_POLICY')
        self._prepare_path()
        self._init_schema()

    def owner_hash(self, authenticated_principal):
        return owner_digest(authenticated_principal, self.owner_hash_key)

    def _callback_url_digest(self, owner_hash, callback_url):
        message = ('aceis-mcp-callback-url:v1:' + owner_hash + ':' + callback_url).encode('utf-8')
        return hmac.new(self.owner_hash_key, message, hashlib.sha256).hexdigest()

    def _encrypt_callback_url(self, callback_url):
        if self.secret_cipher is None:
            raise NotificationError('SECRET_CIPHER_UNAVAILABLE')
        raw = callback_url.encode('utf-8')
        try:
            ciphertext = self.secret_cipher.encrypt(raw)
            if (not isinstance(ciphertext, bytes) or not ciphertext or ciphertext == raw
                    or self.secret_cipher.decrypt(ciphertext) != raw):
                raise ValueError('ciphertext invalid')
            return ciphertext
        except Exception:
            raise NotificationError('SECRET_CIPHER_INVALID') from None

    def _prepare_path(self):
        if any(parent.is_symlink() for parent in [self.path, *self.path.parents]):
            raise NotificationError('UNSAFE_DATABASE_PATH')
        if any(parent.is_symlink() for parent in [self.source_path, *self.source_path.parents]):
            raise NotificationError('UNSAFE_SOURCE_PATH')
        if (not self.path.parent.is_dir() or self.path.parent.stat().st_uid != os.getuid()
                or stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077):
            raise NotificationError('UNSAFE_DATABASE_PARENT')
        if (not self.source_path.is_file() or self.source_path.stat().st_uid != os.getuid()
                or stat.S_IMODE(self.source_path.stat().st_mode) & 0o077
                or stat.S_IMODE(self.source_path.parent.stat().st_mode) & 0o077):
            raise NotificationError('UNSAFE_SOURCE_FILE')
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise NotificationError('UNSAFE_DATABASE_FILE')

    @contextmanager
    def _tx(self):
        conn = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        try:
            conn.execute('PRAGMA journal_mode=DELETE')
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self._tx() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS notification_task_origins (
                task_id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, bound_at REAL NOT NULL)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS notification_thread_origins (
                thread_id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL, bound_at REAL NOT NULL)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS notification_subscriptions (
                subscription_id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL,
                callback_url_ciphertext BLOB NOT NULL, callback_url_hash TEXT NOT NULL,
                secret_ciphertext BLOB NOT NULL,
                event_name TEXT NOT NULL, arguments_json TEXT NOT NULL,
                cursor_seq INTEGER NOT NULL, expires_at REAL NOT NULL,
                active INTEGER NOT NULL, verified INTEGER NOT NULL,
                verify_challenge_ciphertext BLOB, verify_challenge_hash TEXT,
                verify_expires_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(notification_subscriptions)')}
            if (('callback_url' in columns and 'callback_url_ciphertext' not in columns)
                    or ('callback_url_ciphertext' in columns and 'callback_url_hash' not in columns)):
                raise NotificationError('OUTBOX_SCHEMA_MIGRATION_REQUIRED')
            if 'previous_secret_ciphertext' not in columns:
                conn.execute('ALTER TABLE notification_subscriptions ADD COLUMN previous_secret_ciphertext BLOB')
            if 'previous_secret_expires_at' not in columns:
                conn.execute('ALTER TABLE notification_subscriptions ADD COLUMN previous_secret_expires_at REAL')
            conn.execute('''CREATE TABLE IF NOT EXISTS notification_callback_verification_cache (
                owner_hash TEXT NOT NULL, callback_url_hash TEXT NOT NULL, verified_until REAL NOT NULL,
                PRIMARY KEY(owner_hash,callback_url_hash))''')
            cache_columns = {row[1] for row in conn.execute(
                'PRAGMA table_info(notification_callback_verification_cache)')}
            if 'callback_url' in cache_columns and 'callback_url_hash' not in cache_columns:
                raise NotificationError('OUTBOX_SCHEMA_MIGRATION_REQUIRED')
            conn.execute('''CREATE TABLE IF NOT EXISTS notification_outbox (
                event_id TEXT NOT NULL, subscription_id TEXT NOT NULL,
                task_id TEXT NOT NULL, event_name TEXT NOT NULL, payload BLOB NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL, lease_token TEXT, lease_until REAL,
                last_http_status INTEGER, last_error_code TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(event_id, subscription_id))''')
            conn.execute('CREATE INDEX IF NOT EXISTS notification_due_idx ON notification_outbox(status, next_attempt_at)')
            conn.execute('CREATE INDEX IF NOT EXISTS notification_owner_idx ON notification_subscriptions(owner_hash, active)')

    def bind_task_origin(self, task_id, authenticated_principal):
        """Bind a task to the already-verified MCP principal; no raw identity is stored."""
        if not isinstance(task_id, str) or not UUID_RE.fullmatch(task_id):
            raise NotificationError('INVALID_TASK_ID')
        if task_id not in self._read_source_state().get('tasks', {}):
            raise NotificationError('TASK_NOT_FOUND')
        owner_hash = self.owner_hash(authenticated_principal)
        now = self.clock()
        with self._tx() as conn:
            row = conn.execute('SELECT owner_hash FROM notification_task_origins WHERE task_id=?', (task_id,)).fetchone()
            if row and row[0] != owner_hash:
                raise NotificationError('TASK_ORIGIN_CONFLICT')
            conn.execute('INSERT OR IGNORE INTO notification_task_origins VALUES (?,?,?)', (task_id, owner_hash, now))
        return owner_hash

    def task_owned_by(self, task_id, authenticated_principal):
        owner_hash = self.owner_hash(authenticated_principal)
        with self._tx() as conn:
            row = conn.execute('SELECT owner_hash FROM notification_task_origins WHERE task_id=?',
                               (task_id,)).fetchone()
            return bool(row and hmac.compare_digest(row[0], owner_hash))

    def task_owned_by_hash(self, task_id, owner_hash):
        """Check a durable principal digest without recovering its raw identity."""
        if (not isinstance(task_id, str) or not UUID_RE.fullmatch(task_id)
                or not isinstance(owner_hash, str) or re.fullmatch(r'[0-9a-f]{64}', owner_hash) is None):
            return False
        with self._tx() as conn:
            row = conn.execute('SELECT owner_hash FROM notification_task_origins WHERE task_id=?',
                               (task_id,)).fetchone()
            return bool(row and hmac.compare_digest(row[0], owner_hash))

    def has_owned_tasks(self, authenticated_principal):
        owner_hash = self.owner_hash(authenticated_principal)
        with self._tx() as conn:
            return conn.execute('SELECT 1 FROM notification_task_origins WHERE owner_hash=? LIMIT 1',
                                (owner_hash,)).fetchone() is not None

    def owned_task_ids(self, authenticated_principal):
        owner_hash = self.owner_hash(authenticated_principal)
        with self._tx() as conn:
            return [row[0] for row in conn.execute(
                'SELECT task_id FROM notification_task_origins WHERE owner_hash=? ORDER BY task_id',
                (owner_hash,))]

    def _subscription_id(self, owner_hash, callback_url, event_name, arguments):
        identity = {'owner_hash': owner_hash, 'callback_url': callback_url,
                    'event_name': event_name, 'arguments': arguments}
        return 'sub_' + hashlib.sha256(canonical(identity).encode('utf-8')).hexdigest()[:40]

    def _capacity(self, conn):
        rows, payload_bytes = conn.execute(
            'SELECT COUNT(*),COALESCE(SUM(length(payload)),0) FROM notification_outbox').fetchone()
        return {'rows': rows, 'payload_bytes': payload_bytes}

    @staticmethod
    def _safe_cursor(conn, subscription_id, imported_cursor):
        """Return the high-water mark after which no event is awaiting delivery."""
        rows = conn.execute('''SELECT payload FROM notification_outbox
            WHERE subscription_id=? AND status<>'DELIVERED'
              AND (status<>'CANCELLED' OR last_error_code LIKE 'CALLBACK_%')''',
            (subscription_id,)).fetchall()
        unresolved = []
        for (payload,) in rows:
            try:
                cursor = json.loads(bytes(payload).decode('utf-8')).get('cursor')
                if isinstance(cursor, str) and cursor.isdecimal():
                    unresolved.append(int(cursor))
            except (UnicodeError, ValueError, TypeError):
                # Corrupt local state must hold the resume cursor at zero.
                return 0
        return min(unresolved) - 1 if unresolved else imported_cursor

    def _enqueue_for_subscription(self, conn, subscription, events, *, now, capacity=None):
        capacity = capacity if capacity is not None else self._capacity(conn)
        args = json.loads(subscription['arguments_json'])
        cursor = subscription['cursor_seq']
        for event in events:
            seq = event.get('seq') if isinstance(event, dict) else None
            if type(seq) is not int or seq <= cursor:
                continue
            prepared = _event_payload(event)
            if prepared is None and _recognized_notification_source(event):
                raise NotificationError('INVALID_SOURCE_EVENT')
            if prepared is not None:
                event_id, event_name, task_id, payload = prepared
                if (event_name == subscription['event_name']
                        and task_id == args.get('task_id')):
                    exists = conn.execute('SELECT 1 FROM notification_outbox WHERE event_id=? AND subscription_id=?',
                                          (event_id, subscription['subscription_id'])).fetchone()
                    if exists is None:
                        if capacity['rows'] >= self.max_outbox_rows:
                            raise NotificationError('OUTBOX_CAPACITY')
                        if capacity['payload_bytes'] + len(payload) > self.max_payload_store_bytes:
                            raise NotificationError('OUTBOX_PAYLOAD_CAPACITY')
                    inserted = conn.execute('''INSERT OR IGNORE INTO notification_outbox
                        (event_id,subscription_id,task_id,event_name,payload,status,attempts,
                         next_attempt_at,created_at,updated_at)
                        VALUES (?,?,?,?,?,'PENDING',0,?,?,?)''',
                        (event_id, subscription['subscription_id'], task_id, event_name, payload, now, now, now))
                    if inserted.rowcount:
                        capacity['rows'] += 1
                        capacity['payload_bytes'] += len(payload)
            cursor = max(cursor, seq)
        conn.execute('UPDATE notification_subscriptions SET cursor_seq=?,updated_at=? WHERE subscription_id=?',
                     (cursor, now, subscription['subscription_id']))

    def _read_source_state(self):
        try:
            conn = sqlite3.connect(self.source_path, timeout=2)
            conn.execute('PRAGMA query_only=ON')
            row = conn.execute('SELECT body FROM ledger WHERE id=1').fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc).lower():
                return {'tasks': {}, 'events': []}
            raise NotificationError('SOURCE_LEDGER_UNAVAILABLE') from None
        finally:
            try:
                conn.close()
            except UnboundLocalError:
                pass
        if row is None:
            return {'tasks': {}, 'events': []}
        try:
            state = json.loads(row[0])
        except (ValueError, TypeError):
            raise NotificationError('INVALID_EVENT_LEDGER') from None
        if (not isinstance(state, dict) or not isinstance(state.get('tasks'), dict)
                or not isinstance(state.get('events'), list)):
            raise NotificationError('INVALID_EVENT_LEDGER')
        return state

    def _read_source_events(self):
        state = self._read_source_state()
        events = state.get('events')
        if not isinstance(events, list):
            raise NotificationError('INVALID_EVENT_LEDGER')
        return events

    def source_task_exists(self, task_id):
        """Read-only existence check used alongside the per-principal task ACL."""
        if not isinstance(task_id, str) or UUID_RE.fullmatch(task_id) is None:
            return False
        tasks = self._read_source_state()['tasks']
        return isinstance(tasks.get(task_id), dict)

    def source_thread_exists(self, thread_id):
        if not isinstance(thread_id, str) or UUID_RE.fullmatch(thread_id) is None:
            return False
        threads = self._read_source_state().get('threads', {})
        return isinstance(threads, dict) and isinstance(threads.get(thread_id), dict)

    def bind_thread_origin(self, thread_id, authenticated_principal):
        if not self.source_thread_exists(thread_id):
            raise NotificationError('THREAD_NOT_FOUND')
        owner_hash = self.owner_hash(authenticated_principal)
        now = self.clock()
        with self._tx() as conn:
            row = conn.execute('SELECT owner_hash FROM notification_thread_origins WHERE thread_id=?',
                               (thread_id,)).fetchone()
            if row and not hmac.compare_digest(row[0], owner_hash):
                raise NotificationError('THREAD_ORIGIN_CONFLICT')
            conn.execute('INSERT OR IGNORE INTO notification_thread_origins VALUES (?,?,?)',
                         (thread_id, owner_hash, now))
        return owner_hash

    def thread_owned_by(self, thread_id, authenticated_principal):
        owner_hash = self.owner_hash(authenticated_principal)
        with self._tx() as conn:
            row = conn.execute('SELECT owner_hash FROM notification_thread_origins WHERE thread_id=?',
                               (thread_id,)).fetchone()
            return bool(row and hmac.compare_digest(row[0], owner_hash))

    def thread_owned_by_hash(self, thread_id, owner_hash):
        if (not isinstance(thread_id, str) or not UUID_RE.fullmatch(thread_id)
                or not isinstance(owner_hash, str) or re.fullmatch(r'[0-9a-f]{64}', owner_hash) is None):
            return False
        with self._tx() as conn:
            row = conn.execute('SELECT owner_hash FROM notification_thread_origins WHERE thread_id=?',
                               (thread_id,)).fetchone()
            return bool(row and hmac.compare_digest(row[0], owner_hash))

    def source_events_for_owner(self, authenticated_principal, *, after=0, limit=20):
        """Return a sanitized per-owner event stream with an owner-local cursor."""
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise NotificationError('INVALID_EVENT_CURSOR')
        owner_hash = self.owner_hash(authenticated_principal)
        with self._tx() as conn:
            task_ids = {row[0] for row in conn.execute(
                'SELECT task_id FROM notification_task_origins WHERE owner_hash=?',
                (owner_hash,)).fetchall()}
            thread_ids = {row[0] for row in conn.execute(
                'SELECT thread_id FROM notification_thread_origins WHERE owner_hash=?',
                (owner_hash,)).fetchall()}
        source = self._read_source_events()
        if len(source) > 4096:
            raise NotificationError('INVALID_EVENT_LEDGER')
        visible = []
        for event in source:
            if not isinstance(event, dict):
                raise NotificationError('INVALID_EVENT_LEDGER')
            task_id, thread_id = event.get('task_id'), event.get('thread_id')
            if task_id:
                owned = task_id in task_ids and (not thread_id or thread_id in thread_ids)
            else:
                owned = bool(thread_id and thread_id in thread_ids)
            if not owned:
                continue
            kind, happened = event.get('kind'), event.get('time')
            if (not isinstance(kind, str) or re.fullmatch(r'[a-z_]{1,64}', kind) is None
                    or type(happened) not in (int, float) or not 0 <= happened < 10**15):
                raise NotificationError('INVALID_EVENT_LEDGER')
            item = {'kind': kind, 'time': happened}
            if task_id:
                if not isinstance(task_id, str) or UUID_RE.fullmatch(task_id) is None:
                    raise NotificationError('INVALID_EVENT_LEDGER')
                item['task_id'] = task_id
            if thread_id:
                if not isinstance(thread_id, str) or UUID_RE.fullmatch(thread_id) is None:
                    raise NotificationError('INVALID_EVENT_LEDGER')
                item['thread_id'] = thread_id
            for key in ('operation', 'reason_code'):
                value = event.get(key)
                if value is not None:
                    pattern = r'[a-z_]{1,32}' if key == 'operation' else r'[A-Z0-9_]{1,64}'
                    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
                        raise NotificationError('INVALID_EVENT_LEDGER')
                    item[key] = value
            request_key_hash = event.get('request_key_hash')
            if request_key_hash is not None:
                if not isinstance(request_key_hash, str) or re.fullmatch(r'[a-f0-9]{64}', request_key_hash) is None:
                    raise NotificationError('INVALID_EVENT_LEDGER')
                item['request_key_hash'] = request_key_hash
            if 'automatic_approval' in event:
                if type(event['automatic_approval']) is not bool:
                    raise NotificationError('INVALID_EVENT_LEDGER')
                item['automatic_approval'] = event['automatic_approval']
            item['seq'] = len(visible) + 1
            visible.append(item)
        return {'events': visible[after:after + limit], 'latest_cursor': len(visible)}

    def subscribe(self, *, callback_url, event_name, arguments,
                  authenticated_principal, signing_secret, ttl_ms=3_600_000,
                  cursor=None):
        """Create/refresh one finite subscription and replay matching history."""
        owner_hash = self.owner_hash(authenticated_principal)
        if event_name not in {name for name, _ in EVENT_NAMES.values()}:
            raise NotificationError('UNSUPPORTED_EVENT')
        if not isinstance(arguments, dict) or set(arguments) != {'task_id'}:
            raise NotificationError('INVALID_SUBSCRIPTION_ARGUMENTS')
        task_id = arguments['task_id']
        if not isinstance(task_id, str) or not UUID_RE.fullmatch(task_id):
            raise NotificationError('INVALID_TASK_ID')
        if type(ttl_ms) is not int or not MIN_TTL_MS <= ttl_ms <= MAX_TTL_MS:
            raise NotificationError('INVALID_SUBSCRIPTION_TTL')
        if isinstance(cursor, str) and cursor.isdecimal() and len(cursor) <= 10:
            cursor = int(cursor)
        if cursor is not None and (type(cursor) is not int or cursor < 0):
            raise NotificationError('INVALID_CURSOR')
        with self._tx() as conn:
            origin = conn.execute('SELECT owner_hash FROM notification_task_origins WHERE task_id=?',
                                  (task_id,)).fetchone()
            if origin is None or not hmac.compare_digest(origin[0], owner_hash):
                raise NotificationError('TASK_NOT_OWNED')
        callback_url = normalize_callback_url(callback_url)
        callback_url_ciphertext = self._encrypt_callback_url(callback_url)
        callback_url_hash = self._callback_url_digest(owner_hash, callback_url)
        raw_secret = validate_signing_secret(signing_secret)
        if self.secret_cipher is None:
            raise NotificationError('SECRET_CIPHER_UNAVAILABLE')
        try:
            ciphertext = self.secret_cipher.encrypt(signing_secret.encode('utf-8'))
            if not isinstance(ciphertext, bytes) or not ciphertext or ciphertext == signing_secret.encode('utf-8'):
                raise ValueError('ciphertext is invalid')
            # Confirm the provider can decrypt its own value before persisting it.
            if self.secret_cipher.decrypt(ciphertext) != signing_secret.encode('utf-8'):
                raise ValueError('ciphertext did not roundtrip')
        except Exception:
            raise NotificationError('SECRET_CIPHER_INVALID') from None
        del raw_secret
        subscription_id = self._subscription_id(owner_hash, callback_url, event_name, arguments)
        now = self.clock()
        expires_at = now + ttl_ms / 1000
        # Recheck ownership after potentially slow encryption but before DNS.
        with self._tx() as conn:
            origin = conn.execute('SELECT owner_hash FROM notification_task_origins WHERE task_id=?',
                                  (task_id,)).fetchone()
            if origin is None or not hmac.compare_digest(origin[0], owner_hash):
                raise NotificationError('TASK_NOT_OWNED')
        ips = validate_callback_url(callback_url, self.resolver)
        events = self._read_source_events()
        with self._tx() as conn:
            conn.execute('DELETE FROM notification_callback_verification_cache WHERE verified_until<=?', (now,))
            origin = conn.execute('SELECT owner_hash FROM notification_task_origins WHERE task_id=?', (task_id,)).fetchone()
            if origin is None or not hmac.compare_digest(origin[0], owner_hash):
                raise NotificationError('TASK_NOT_OWNED')
            existing = conn.execute('''SELECT cursor_seq,verified,secret_ciphertext,previous_secret_ciphertext,
                previous_secret_expires_at FROM notification_subscriptions WHERE subscription_id=?''',
                                    (subscription_id,)).fetchone()
            if existing is None and conn.execute('SELECT COUNT(*) FROM notification_subscriptions').fetchone()[0] >= self.max_subscriptions:
                raise NotificationError('SUBSCRIPTION_CAPACITY')
            if existing is None and cursor not in (None, 0):
                raise NotificationError('CURSOR_REQUIRES_EXISTING_SUBSCRIPTION')
            safe_cursor = self._safe_cursor(conn, subscription_id, existing[0]) if existing else 0
            start_cursor = min(cursor, safe_cursor) if cursor is not None else safe_cursor
            latest = max((event.get('seq', 0) for event in events if isinstance(event, dict)
                          and type(event.get('seq')) is int), default=0)
            if start_cursor > latest:
                raise NotificationError('CURSOR_AHEAD_OF_LEDGER')
            available = [event['seq'] for event in events if isinstance(event, dict)
                         and type(event.get('seq')) is int]
            truncated = bool(available and start_cursor < min(available) - 1)
            args_json = canonical(arguments)
            cache = conn.execute('''SELECT verified_until FROM notification_callback_verification_cache
                WHERE owner_hash=? AND callback_url_hash=?''',
                (owner_hash, callback_url_hash)).fetchone()
            already_verified = bool(cache and cache[0] > now)
            challenge = None
            challenge_ciphertext = challenge_hash = challenge_expiry = None
            if not already_verified:
                challenge = secrets.token_urlsafe(32)
                try:
                    challenge_ciphertext = self.secret_cipher.encrypt(challenge.encode('utf-8'))
                except Exception:
                    raise NotificationError('SECRET_CIPHER_INVALID') from None
                if (not isinstance(challenge_ciphertext, bytes) or not challenge_ciphertext
                        or challenge_ciphertext == challenge.encode('utf-8')):
                    raise NotificationError('SECRET_CIPHER_INVALID')
                challenge_hash = hashlib.sha256(challenge.encode('utf-8')).hexdigest()
                challenge_expiry = now + 60
            conn.execute('''INSERT INTO notification_subscriptions
                (subscription_id,owner_hash,callback_url_ciphertext,callback_url_hash,secret_ciphertext,event_name,arguments_json,
                 cursor_seq,expires_at,active,verified,verify_challenge_ciphertext,
                 verify_challenge_hash,verify_expires_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)
                ON CONFLICT(subscription_id) DO UPDATE SET
                 secret_ciphertext=excluded.secret_ciphertext, expires_at=excluded.expires_at,
                 active=1, verified=excluded.verified,
                 verify_challenge_ciphertext=excluded.verify_challenge_ciphertext,
                 verify_challenge_hash=excluded.verify_challenge_hash,
                 verify_expires_at=excluded.verify_expires_at, updated_at=excluded.updated_at''',
                (subscription_id, owner_hash, callback_url_ciphertext, callback_url_hash,
                 ciphertext, event_name, args_json,
                 start_cursor, expires_at, int(already_verified), challenge_ciphertext,
                 challenge_hash, challenge_expiry, now, now))
            previous_ciphertext = None
            previous_expires_at = None
            if existing is not None:
                try:
                    old_secret = self.secret_cipher.decrypt(bytes(existing[2]))
                except Exception:
                    raise NotificationError('SECRET_DECRYPT_FAILED') from None
                if not hmac.compare_digest(old_secret, signing_secret.encode('utf-8')):
                    previous_ciphertext = existing[2]
                    previous_expires_at = now + SECRET_ROTATION_SECONDS
                elif existing[3] is not None and existing[4] is not None and existing[4] > now:
                    previous_ciphertext = existing[3]
                    previous_expires_at = existing[4]
            conn.execute('''UPDATE notification_subscriptions SET previous_secret_ciphertext=?,
                previous_secret_expires_at=? WHERE subscription_id=?''',
                (previous_ciphertext, previous_expires_at, subscription_id))
            row = conn.execute('SELECT * FROM notification_subscriptions WHERE subscription_id=?',
                               (subscription_id,)).fetchone()
            names = [d[0] for d in conn.execute('SELECT * FROM notification_subscriptions').description]
            self._enqueue_for_subscription(conn, dict(zip(names, row)), events, now=now,
                                           capacity=self._capacity(conn))
            conn.execute('UPDATE notification_outbox SET status="EXPIRED",updated_at=? WHERE subscription_id=? AND status IN ("PENDING","RETRY","UNKNOWN") AND ? >= ?',
                         (now, subscription_id, now, expires_at))
            if already_verified:
                conn.execute('''UPDATE notification_outbox SET status='PENDING',next_attempt_at=?,
                    lease_token=NULL,lease_until=NULL,updated_at=? WHERE subscription_id=? AND status='EXPIRED' ''',
                    (now, now, subscription_id))
        return {'id': subscription_id, 'refreshBefore': datetime.fromtimestamp(expires_at, timezone.utc).isoformat().replace('+00:00', 'Z'),
                'cursor': str(self._safe_cursor_from_subscription(subscription_id, latest)),
                'truncated': truncated, 'validated_ips': ips,
                'verified': already_verified,
                'verification_required': not already_verified}

    def _safe_cursor_from_subscription(self, subscription_id, fallback):
        with self._tx() as conn:
            row = conn.execute('SELECT cursor_seq FROM notification_subscriptions WHERE subscription_id=?',
                               (subscription_id,)).fetchone()
            return self._safe_cursor(conn, subscription_id, row[0] if row else fallback)

    def build_verification_request(self, subscription_id, *, now=None):
        """Prepare the signed one-time callback challenge; caller owns transport."""
        now = self.clock() if now is None else float(now)
        with self._tx() as conn:
            row = conn.execute('''SELECT callback_url_ciphertext,secret_ciphertext,verify_challenge_ciphertext,
                verify_challenge_hash,verify_expires_at,active,verified FROM notification_subscriptions
                WHERE subscription_id=?''', (subscription_id,)).fetchone()
        if row is None or not row[5] or row[6] or not row[2] or row[4] is None or row[4] <= now:
            raise NotificationError('VERIFICATION_CHALLENGE_UNAVAILABLE')
        try:
            callback_url = self.secret_cipher.decrypt(bytes(row[0])).decode('utf-8')
            secret = self.secret_cipher.decrypt(bytes(row[1])).decode('utf-8')
            challenge = self.secret_cipher.decrypt(bytes(row[2])).decode('utf-8')
        except Exception:
            raise NotificationError('SECRET_DECRYPT_FAILED') from None
        if hashlib.sha256(challenge.encode('utf-8')).hexdigest() != row[3]:
            raise NotificationError('VERIFICATION_CHALLENGE_CORRUPT')
        ips = validate_callback_url(callback_url, self.resolver)
        body = canonical({'type': 'verification', 'challenge': challenge}).encode('utf-8')
        event_id = 'verify_' + subscription_id[4:] + '_' + row[3][:16]
        headers = sign_request(secret, event_id, int(now), body)
        headers['X-MCP-Subscription-Id'] = subscription_id
        return {'url': callback_url, 'pinned_ips': ips, 'body': body, 'headers': headers,
                'event_id': event_id, 'expires_at': row[4]}

    def complete_verification(self, subscription_id, *, status_code, echoed_challenge, now=None):
        """Activate only after a timely 2xx echo; challenge is consumed exactly once."""
        now = self.clock() if now is None else float(now)
        if type(status_code) is not int:
            raise NotificationError('INVALID_VERIFICATION_RESPONSE')
        with self._tx() as conn:
            row = conn.execute('''SELECT verify_challenge_hash,verify_expires_at,active,verified,
                owner_hash,callback_url_hash
                FROM notification_subscriptions WHERE subscription_id=?''', (subscription_id,)).fetchone()
            if row is None or not row[2] or row[3] or not row[0]:
                raise NotificationError('VERIFICATION_CHALLENGE_UNAVAILABLE')
            matched = False
            if isinstance(echoed_challenge, str):
                try:
                    echoed_bytes = echoed_challenge.encode('utf-8')
                    if len(echoed_bytes) <= 256:
                        matched = hmac.compare_digest(hashlib.sha256(echoed_bytes).hexdigest(), row[0])
                except UnicodeEncodeError:
                    matched = False
            if status_code < 200 or status_code >= 300 or row[1] is None or row[1] <= now or not matched:
                conn.execute('''UPDATE notification_subscriptions SET active=0,verified=0,
                    verify_challenge_ciphertext=NULL,verify_challenge_hash=NULL,verify_expires_at=NULL,updated_at=?
                    WHERE subscription_id=?''', (now, subscription_id))
                conn.execute('''UPDATE notification_outbox SET status="CANCELLED",lease_token=NULL,
                    lease_until=NULL,last_error_code='CALLBACK_CHALLENGE_FAILED',updated_at=?
                    WHERE subscription_id=? AND status IN
                    ("PENDING","RETRY","UNKNOWN")''', (now, subscription_id))
                return {'status': 'VERIFICATION_FAILED', 'id': subscription_id}
            conn.execute('''UPDATE notification_subscriptions SET verified=1,
                verify_challenge_ciphertext=NULL,verify_challenge_hash=NULL,verify_expires_at=NULL,updated_at=?
                WHERE subscription_id=?''', (now, subscription_id))
            conn.execute('''INSERT INTO notification_callback_verification_cache
                (owner_hash,callback_url_hash,verified_until) VALUES (?,?,?)
                ON CONFLICT(owner_hash,callback_url_hash) DO UPDATE SET verified_until=excluded.verified_until''',
                (row[4], row[5], now + CALLBACK_VERIFICATION_CACHE_SECONDS))
            conn.execute('''UPDATE notification_outbox SET status='PENDING',next_attempt_at=?,
                lease_token=NULL,lease_until=NULL,last_error_code=NULL,updated_at=?
                WHERE subscription_id=? AND (status='EXPIRED' OR
                    (status='CANCELLED' AND last_error_code LIKE 'CALLBACK_%')) ''',
                (now, now, subscription_id))
        return {'status': 'VERIFIED', 'id': subscription_id}

    def unsubscribe(self, *, callback_url, event_name, arguments, authenticated_principal):
        if (event_name not in {name for name, _ in EVENT_NAMES.values()}
                or not isinstance(arguments, dict) or set(arguments) != {'task_id'}
                or not isinstance(arguments.get('task_id'), str)
                or not UUID_RE.fullmatch(arguments['task_id'])):
            raise NotificationError('INVALID_UNSUBSCRIBE_ARGUMENTS')
        callback_url = normalize_callback_url(callback_url)
        owner_hash = self.owner_hash(authenticated_principal)
        subscription_id = self._subscription_id(owner_hash, callback_url, event_name, arguments)
        now = self.clock()
        with self._tx() as conn:
            conn.execute('UPDATE notification_subscriptions SET active=0,updated_at=? WHERE subscription_id=? AND owner_hash=?',
                         (now, subscription_id, owner_hash))
            conn.execute('UPDATE notification_outbox SET status="CANCELLED",lease_token=NULL,lease_until=NULL,last_error_code="SUBSCRIPTION_UNSUBSCRIBED",updated_at=? WHERE subscription_id=? AND status IN ("PENDING","RETRY","UNKNOWN")',
                         (now, subscription_id))
        return {'status': 'UNSUBSCRIBED', 'id': subscription_id}

    def deactivate_subscription(self, subscription_id, *, error_code='OWNER_ACCESS_REVOKED'):
        """Stop one subscription after an authenticated access recheck fails."""
        if not isinstance(error_code, str) or re.fullmatch(r'[A-Z0-9_]{1,64}', error_code) is None:
            raise NotificationError('INVALID_ERROR_CODE')
        now = self.clock()
        with self._tx() as conn:
            conn.execute('UPDATE notification_subscriptions SET active=0,verified=0,updated_at=? WHERE subscription_id=?',
                         (now, subscription_id))
            conn.execute('''UPDATE notification_outbox SET status='CANCELLED',lease_token=NULL,
                lease_until=NULL,last_error_code=?,updated_at=? WHERE subscription_id=? AND status IN
                ('PENDING','RETRY','UNKNOWN')''', (error_code, now, subscription_id))
        return {'status': 'CANCELLED', 'subscription_id': subscription_id}

    def sync_from_events(self, events):
        """Copy durable pipeline events into a separate DB without blocking task commits."""
        if not events:
            return 0
        now = self.clock()
        with self._tx() as conn:
            rows = conn.execute('SELECT * FROM notification_subscriptions WHERE active=1 AND expires_at>?', (now,)).fetchall()
            columns = [column[0] for column in conn.execute('SELECT * FROM notification_subscriptions LIMIT 0').description]
            capacity = self._capacity(conn)
            inserted_before = capacity['rows']
            for row in rows:
                subscription = dict(zip(columns, row))
                self._enqueue_for_subscription(conn, subscription, events, now=now, capacity=capacity)
            return capacity['rows'] - inserted_before

    def sync_from_ledger(self):
        """Restart reconciliation: replay source events after each durable cursor."""
        now = self.clock()
        events = self._read_source_events()
        with self._tx() as conn:
            rows = conn.execute('SELECT * FROM notification_subscriptions WHERE active=1 AND expires_at>?', (now,)).fetchall()
            columns = [column[0] for column in conn.execute('SELECT * FROM notification_subscriptions LIMIT 0').description]
            capacity = self._capacity(conn)
            for row in rows:
                self._enqueue_for_subscription(conn, dict(zip(columns, row)), events, now=now, capacity=capacity)
        return self.summary()

    def _expire(self, conn, now):
        conn.execute('UPDATE notification_subscriptions SET active=0,updated_at=? WHERE active=1 AND expires_at<=?', (now, now))
        conn.execute('UPDATE notification_outbox SET status="EXPIRED",lease_token=NULL,lease_until=NULL,updated_at=? WHERE status IN ("PENDING","RETRY","UNKNOWN") AND subscription_id IN (SELECT subscription_id FROM notification_subscriptions WHERE active=0 AND expires_at<=?)', (now, now))

    def claim_due(self, *, now=None):
        now = self.clock() if now is None else float(now)
        with self._tx() as conn:
            conn.execute('''UPDATE notification_subscriptions SET previous_secret_ciphertext=NULL,
                previous_secret_expires_at=NULL,updated_at=? WHERE previous_secret_expires_at<=?''',
                (now, now))
            self._expire(conn, now)
            conn.execute('''UPDATE notification_outbox SET status="UNKNOWN",lease_token=NULL,lease_until=NULL,
                next_attempt_at=?,last_error_code="LEASE_EXPIRED",updated_at=?
                WHERE status="DELIVERING" AND lease_until<=? AND subscription_id IN
                  (SELECT subscription_id FROM notification_subscriptions
                   WHERE active=1 AND verified=1 AND expires_at>?)''', (now, now, now, now))
            conn.execute('''UPDATE notification_outbox SET status="EXPIRED",lease_token=NULL,
                lease_until=NULL,last_error_code="SUBSCRIPTION_EXPIRED",updated_at=?
                WHERE status="DELIVERING" AND lease_until<=? AND subscription_id IN
                  (SELECT subscription_id FROM notification_subscriptions WHERE expires_at<=?)''',
                (now, now, now))
            conn.execute('''UPDATE notification_outbox SET status="CANCELLED",lease_token=NULL,
                lease_until=NULL,last_error_code="SUBSCRIPTION_INACTIVE",updated_at=?
                WHERE status="DELIVERING" AND lease_until<=? AND subscription_id IN
                  (SELECT subscription_id FROM notification_subscriptions
                   WHERE active=0 OR verified=0)''', (now, now))
            conn.execute('''UPDATE notification_outbox SET status="UNKNOWN_FINAL",updated_at=?
                WHERE status="UNKNOWN" AND attempts>=? AND next_attempt_at<=?''',
                (now, self.max_attempts, now))
            row = conn.execute('''SELECT o.*,s.callback_url_ciphertext,s.secret_ciphertext,s.expires_at,
                s.owner_hash,s.previous_secret_ciphertext,s.previous_secret_expires_at
                FROM notification_outbox o JOIN notification_subscriptions s USING(subscription_id)
                WHERE o.status IN ("PENDING","RETRY","UNKNOWN") AND o.next_attempt_at<=?
                  AND o.attempts<? AND s.active=1 AND s.verified=1 AND s.expires_at>?
                ORDER BY o.created_at,o.event_id,o.subscription_id LIMIT 1''',
                (now, self.max_attempts, now)).fetchone()
            if row is None:
                return None
            columns = [column[0] for column in conn.execute('''SELECT o.*,s.callback_url_ciphertext,s.secret_ciphertext,s.expires_at,
                s.owner_hash,s.previous_secret_ciphertext,s.previous_secret_expires_at
                FROM notification_outbox o JOIN notification_subscriptions s USING(subscription_id) LIMIT 0''').description]
            job = dict(zip(columns, row))
            token = str(uuid.uuid4())
            attempt = job['attempts'] + 1
            updated = conn.execute('''UPDATE notification_outbox SET status="DELIVERING",attempts=?,
                lease_token=?,lease_until=?,updated_at=? WHERE event_id=? AND subscription_id=?
                AND status IN ("PENDING","RETRY","UNKNOWN")''',
                (attempt, token, now + self.lease_seconds, now, job['event_id'], job['subscription_id']))
            if updated.rowcount != 1:
                return None
            job.update(status='DELIVERING', attempts=attempt, lease_token=token,
                       lease_until=now + self.lease_seconds)
            return job

    def _finish(self, job, *, status, now, http_status=None, error_code=None, retry=False):
        if status not in DELIVERY_STATES:
            raise NotificationError('INVALID_DELIVERY_STATE')
        next_attempt = now
        if retry:
            next_attempt = now + min(self.base_backoff * (2 ** max(0, job['attempts'] - 1)), self.max_backoff)
        with self._tx() as conn:
            result = conn.execute('''UPDATE notification_outbox SET status=?,next_attempt_at=?,
                lease_token=NULL,lease_until=NULL,last_http_status=?,last_error_code=?,updated_at=?
                WHERE event_id=? AND subscription_id=? AND status="DELIVERING" AND lease_token=?''',
                (status, next_attempt, http_status, error_code, now,
                 job['event_id'], job['subscription_id'], job['lease_token']))
            if result.rowcount != 1:
                return {'status': 'UNKNOWN', 'code': 'DELIVERY_LEASE_LOST', 'event_id': job['event_id']}
        return {'status': status, 'event_id': job['event_id'], 'next_attempt_at': next_attempt if retry else None}

    def delivery_permitted(self, job, *, now=None):
        """Fence sends after unsubscribe/revocation unless a delivery is in flight."""
        now = self.clock() if now is None else float(now)
        with self._tx() as conn:
            row = conn.execute('''SELECT 1 FROM notification_outbox o
                JOIN notification_subscriptions s USING(subscription_id)
                WHERE o.event_id=? AND o.subscription_id=? AND o.status="DELIVERING"
                  AND o.lease_token=? AND s.active=1 AND s.verified=1 AND s.expires_at>?''',
                (job['event_id'], job['subscription_id'], job['lease_token'], now)).fetchone()
            return row is not None

    def deliver_one(self, sender, *, now=None, access_check=None):
        """Use only an injected sender. No default network transport exists."""
        explicit_now = now is not None
        now = self.clock() if now is None else float(now)
        job = self.claim_due(now=now)
        if job is None:
            return {'status': 'EMPTY'}
        if access_check is None:
            self._finish(job, status='RETRY', now=now,
                         error_code='ACCESS_RECHECK_REQUIRED', retry=job['attempts'] < self.max_attempts)
            return {'status': 'ACCESS_RECHECK_REQUIRED', 'event_id': job['event_id']}
        try:
            authorized = access_check(job['owner_hash'], job['task_id'])
        except Exception:
            state = 'RETRY' if job['attempts'] < self.max_attempts else 'PERMANENT_FAILURE'
            return self._finish(job, status=state, now=now,
                                error_code='ACCESS_RECHECK_UNAVAILABLE', retry=state == 'RETRY')
        if authorized is not True:
            self.deactivate_subscription(job['subscription_id'])
            return self._finish(job, status='CANCELLED', now=now,
                                error_code='OWNER_ACCESS_REVOKED')
        try:
            if self.secret_cipher is None:
                raise NotificationError('SECRET_CIPHER_UNAVAILABLE')
            callback_url = self.secret_cipher.decrypt(
                bytes(job['callback_url_ciphertext'])).decode('utf-8')
            ips = validate_callback_url(callback_url, self.resolver)
            secret = self.secret_cipher.decrypt(bytes(job['secret_ciphertext'])).decode('utf-8')
            previous = ()
            if (job.get('previous_secret_ciphertext') is not None
                    and job.get('previous_secret_expires_at') is not None
                    and job['previous_secret_expires_at'] > now):
                previous = (self.secret_cipher.decrypt(
                    bytes(job['previous_secret_ciphertext'])).decode('utf-8'),)
            timestamp = int(now)
            headers = sign_request(secret, job['event_id'], timestamp, bytes(job['payload']),
                                   additional_secrets=previous)
            headers['X-MCP-Subscription-Id'] = job['subscription_id']
        except NotificationError as exc:
            return self._finish(job, status='PERMANENT_FAILURE', now=now, error_code=exc.code)
        except Exception:
            return self._finish(job, status='PERMANENT_FAILURE', now=now, error_code='SECRET_DECRYPT_FAILED')
        # Recheck the authoritative ACL after DNS/secret work, immediately
        # before the durable send fence. A concurrent unsubscribe that wins
        # before this fence cancels the send. An already fenced HTTP request
        # may still complete after unsubscribe returns.
        try:
            final_authorized = access_check(job['owner_hash'], job['task_id'])
        except Exception:
            state = 'RETRY' if job['attempts'] < self.max_attempts else 'PERMANENT_FAILURE'
            return self._finish(job, status=state, now=now,
                                error_code='ACCESS_RECHECK_UNAVAILABLE', retry=state == 'RETRY')
        if final_authorized is False:
            self.deactivate_subscription(job['subscription_id'])
            return self._finish(job, status='CANCELLED', now=now,
                                error_code='OWNER_ACCESS_REVOKED')
        if final_authorized is not True:
            state = 'RETRY' if job['attempts'] < self.max_attempts else 'PERMANENT_FAILURE'
            return self._finish(job, status=state, now=now,
                                error_code='ACCESS_RECHECK_UNAVAILABLE', retry=state == 'RETRY')
        guard_now = self.clock() if not explicit_now else now
        if not self.delivery_permitted(job, now=guard_now):
            return self._finish(job, status='CANCELLED', now=guard_now,
                                error_code='SUBSCRIPTION_STOPPED')
        try:
            # Contract: connect only to one of ips; retain callback hostname for TLS;
            # disable redirects; bound connect/read timeouts to <=10 seconds.
            response = sender(callback_url, ips, bytes(job['payload']), headers)
            status_code = response.get('status') if isinstance(response, dict) else None
        except Exception:
            # The peer may have accepted the event before the transport failed.
            # Reconcile the local receipt, then retry only this stable event ID.
            state = 'UNKNOWN' if job['attempts'] < self.max_attempts else 'UNKNOWN_FINAL'
            retry = state == 'UNKNOWN'
            return self._finish(job, status=state, now=now, error_code='DELIVERY_OUTCOME_UNKNOWN', retry=retry)
        if type(status_code) is not int:
            state = 'UNKNOWN' if job['attempts'] < self.max_attempts else 'UNKNOWN_FINAL'
            return self._finish(job, status=state, now=now, error_code='INVALID_DELIVERY_ACK', retry=state == 'UNKNOWN')
        if 200 <= status_code < 300:
            return self._finish(job, status='DELIVERED', now=now, http_status=status_code)
        if status_code in (410, 413) or status_code < 200 or status_code >= 300 and status_code < 400:
            return self._finish(job, status='PERMANENT_FAILURE', now=now,
                                http_status=status_code, error_code='NON_RETRYABLE_RESPONSE')
        if status_code in (408, 425, 429) or status_code >= 500:
            state = 'RETRY' if job['attempts'] < self.max_attempts else 'PERMANENT_FAILURE'
            return self._finish(job, status=state, now=now, http_status=status_code,
                                error_code='TRANSIENT_RESPONSE', retry=state == 'RETRY')
        return self._finish(job, status='PERMANENT_FAILURE', now=now, http_status=status_code,
                            error_code='REJECTED_RESPONSE')

    def reconcile_unknown(self, event_id, subscription_id, *, remote_receipt=None, now=None):
        """Recheck durable state before same-ID retry; never resubmit a Codex task.

        MCP Events has no delivery-status lookup method. If no receipt query is
        available, the retry keeps the same eventId so the receiver can dedupe.
        """
        now = self.clock() if now is None else float(now)
        if remote_receipt not in (None, True, False):
            raise NotificationError('INVALID_RECEIPT')
        with self._tx() as conn:
            row = conn.execute('SELECT status,attempts,lease_until FROM notification_outbox WHERE event_id=? AND subscription_id=?',
                               (event_id, subscription_id)).fetchone()
            if row is None:
                return {'status': 'NOT_FOUND'}
            status, attempts, lease_until = row
            if status == 'DELIVERED' or remote_receipt is True:
                conn.execute('''UPDATE notification_outbox SET status="DELIVERED",lease_token=NULL,
                    lease_until=NULL,last_error_code=NULL,updated_at=? WHERE event_id=? AND subscription_id=?''',
                    (now, event_id, subscription_id))
                return {'status': 'DELIVERED', 'event_id': event_id, 'reconciled': True}
            if status == 'DELIVERING' and lease_until is not None and lease_until > now:
                return {'status': 'DELIVERING', 'event_id': event_id, 'reconciled': False,
                        'action': 'WAIT_FOR_DELIVERY_LEASE'}
            if status not in ('UNKNOWN', 'UNKNOWN_FINAL', 'DELIVERING'):
                return {'status': status, 'event_id': event_id, 'reconciled': False}
            if remote_receipt is False or remote_receipt is None:
                if attempts >= self.max_attempts:
                    return {'status': 'UNKNOWN_FINAL', 'event_id': event_id,
                            'reconciled': False, 'action': 'OPERATOR_REVIEW'}
                conn.execute('''UPDATE notification_outbox SET status="RETRY",next_attempt_at=?,
                    lease_token=NULL,lease_until=NULL,last_error_code="SAME_EVENT_ID_RETRY",updated_at=?
                    WHERE event_id=? AND subscription_id=?''', (now, now, event_id, subscription_id))
                return {'status': 'RETRY', 'event_id': event_id, 'reconciled': True,
                        'action': 'RETRY_SAME_EVENT_ID', 'task_reexecution': False}
        return {'status': 'UNKNOWN', 'event_id': event_id}

    def delivery_status(self, event_id, subscription_id):
        with self._tx() as conn:
            row = conn.execute('''SELECT event_id,subscription_id,task_id,event_name,status,attempts,
                next_attempt_at,lease_until,last_http_status,last_error_code,created_at,updated_at
                FROM notification_outbox WHERE event_id=? AND subscription_id=?''',
                (event_id, subscription_id)).fetchone()
            if row is None:
                return None
            columns = [column[0] for column in conn.execute('''SELECT event_id,subscription_id,task_id,event_name,status,attempts,
                next_attempt_at,lease_until,last_http_status,last_error_code,created_at,updated_at
                FROM notification_outbox LIMIT 0''').description]
            return dict(zip(columns, row))

    def summary(self):
        with self._tx() as conn:
            states = {row[0]: row[1] for row in conn.execute('SELECT status,COUNT(*) FROM notification_outbox GROUP BY status')}
            return {'delivery_states': states, 'active_subscriptions': conn.execute(
                'SELECT COUNT(*) FROM notification_subscriptions WHERE active=1 AND verified=1 AND expires_at>?',
                (self.clock(),)).fetchone()[0],
                'pending_verification': conn.execute(
                'SELECT COUNT(*) FROM notification_subscriptions WHERE active=1 AND verified=0 AND expires_at>?',
                (self.clock(),)).fetchone()[0]}


class NotificationWorker:
    """Single-threaded bounded dispatcher, independent from the Codex event loop."""

    def __init__(self, outbox, sender, *, access_check, poll_seconds=1.0):
        if not callable(access_check):
            raise NotificationError('ACCESS_RECHECK_REQUIRED')
        if not callable(sender):
            raise NotificationError('SENDER_REQUIRED')
        if not 0.05 <= float(poll_seconds) <= 60:
            raise NotificationError('INVALID_WORKER_POLL')
        self.outbox = outbox
        self.sender = sender
        self.access_check = access_check
        self.poll_seconds = float(poll_seconds)
        self._stop = threading.Event()
        self._thread = None
        self.last_result = {'status': 'NOT_STARTED'}

    def run_once(self, *, now=None):
        sync_error = None
        try:
            self.outbox.sync_from_ledger()
        except Exception:
            sync_error = 'EVENT_SYNC_UNAVAILABLE'
        try:
            result = self.outbox.deliver_one(self.sender, now=now,
                                             access_check=self.access_check)
        except Exception:
            result = {'status': 'WORKER_ERROR', 'code': 'DELIVERY_WORKER_ERROR'}
        if sync_error:
            result = {**result, 'sync_error': sync_error}
        self.last_result = result
        return result

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name='mcp-events-delivery', daemon=True)
        self._thread.start()
        return True

    def _run(self):
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.poll_seconds)

    def stop(self, *, timeout=10.0):
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(max(0.0, float(timeout)))
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped
