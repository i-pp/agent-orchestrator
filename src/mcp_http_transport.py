"""Loopback-only Streamable HTTP transport for MCP 2.0 and initialized MCP.

It supports stateless protocol version 2026-07-28 and session-based 2025-11-25.
Each request is one JSON-RPC POST; the modern mode validates request metadata
headers against the body. The stdio entrypoint remains the default, and this
module does not bind until `make_http_server` is explicitly called.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import binascii
import ipaddress
import json
import socket
import threading
import time
from urllib.parse import urlsplit

from mcp_adapter import (PROTOCOL, REQUEST_PROTOCOL, VERSION_META,
                         CAPABILITIES_META, SERVER_INFO)
from local_principal import LocalPrincipal, principal_identity
from total_deadline import run_with_total_deadline


ENDPOINT_PATH = '/mcp'
MAX_HTTP_BODY = 65_536
MAX_HTTP_SESSIONS = 128
MAX_MCP_NAME = 8192
DEFAULT_HTTP_REQUEST_DEADLINE_SECONDS = 120.0
REQUIRED_ACCEPT_TYPES = frozenset({'application/json', 'text/event-stream'})
LOCAL_HOSTS = frozenset({'127.0.0.1', 'localhost'})


def _header_map(headers):
    result = {}
    items = headers.items() if hasattr(headers, 'items') else headers
    for name, value in items:
        key = str(name).lower()
        if key in result:
            raise ValueError('duplicate header')
        result[key] = str(value).strip()
    return result


def _json_body(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate JSON key')
            value[key] = item
        return value

    def reject_constant(_value):
        raise ValueError('non-finite JSON number')

    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                       parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError('JSON-RPC message must be an object')
    return value


def _accepts_required(value):
    if not isinstance(value, str):
        return False
    found = set()
    for entry in value.split(','):
        parts = [part.strip() for part in entry.split(';')]
        media_type = parts[0].lower()
        quality = 1.0
        for parameter in parts[1:]:
            key, separator, raw = parameter.partition('=')
            if separator and key.strip().lower() == 'q':
                try:
                    quality = float(raw.strip())
                except ValueError:
                    quality = 0.0
        if quality > 0:
            found.add(media_type)
    return REQUIRED_ACCEPT_TYPES <= found


def _local_origin(value, expected_port):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (parsed.scheme == 'http' and parsed.hostname in LOCAL_HOSTS
            and parsed.username is None and parsed.password is None
            and parsed.path in ('', '/') and not parsed.query and not parsed.fragment
            and port == expected_port)


def _decode_mcp_name(value):
    """Decode the 2026 transport's ASCII-safe representation of a name/URI."""
    if not isinstance(value, str) or len(value) > MAX_MCP_NAME:
        return None
    prefix, suffix = '=?base64?', '?='
    if value.startswith(prefix) or value.endswith(suffix):
        if not (value.startswith(prefix) and value.endswith(suffix)):
            return None
        encoded = value[len(prefix):-len(suffix)]
        try:
            decoded = base64.b64decode(encoded, validate=True).decode('utf-8', errors='strict')
        except (binascii.Error, UnicodeError, ValueError):
            return None
        return decoded if len(decoded) <= MAX_MCP_NAME else None
    if not value.isascii() or any(ord(char) < 0x20 or ord(char) > 0x7e for char in value):
        return None
    return value


class McpHttpEndpoint:
    """Pure request handler; can be exercised without opening a network socket."""

    def __init__(self, adapter, *, expected_port, clock=time.time,
                 session_ttl_seconds=3600, allow_single_user_local=False,
                 principal_provider=None):
        if type(expected_port) is not int or not 1 <= expected_port <= 65535:
            raise ValueError('Expected loopback port is required')
        if not callable(getattr(adapter, 'handle', None)):
            raise ValueError('MCP adapter required')
        self.adapter = adapter
        self.expected_port = expected_port
        if not callable(clock) or type(session_ttl_seconds) not in (int, float) or session_ttl_seconds <= 0:
            raise ValueError('Invalid MCP session configuration')
        self.clock = clock
        self.session_ttl_seconds = float(session_ttl_seconds)
        if type(allow_single_user_local) is not bool:
            raise ValueError('Single-user local mode must be explicit')
        if allow_single_user_local:
            if not callable(principal_provider):
                raise ValueError('Local principal provider is required')
        elif principal_provider is not None:
            raise ValueError('Principal provider requires single-user local mode')
        self.allow_single_user_local = allow_single_user_local
        self.principal_provider = principal_provider
        self._sessions = {}
        self._session_lock = threading.RLock()
        # Pipeline operations and the stdio adapter were designed as one
        # serialized request stream. Preserve that property across HTTP workers.
        self._adapter_lock = threading.RLock()

    @staticmethod
    def _reply(status, payload=None, *, headers=None):
        body = b'' if payload is None else json.dumps(
            payload, ensure_ascii=False, allow_nan=False,
            separators=(',', ':')).encode('utf-8')
        response_headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        if payload is not None:
            response_headers['Content-Type'] = 'application/json; charset=utf-8'
        if headers:
            response_headers.update(headers)
        return status, response_headers, body

    @staticmethod
    def _error(identifier, code, message, data=None):
        error = {'code': code, 'message': message}
        if data is not None:
            error['data'] = data
        return {'jsonrpc': '2.0', 'id': identifier, 'error': error}

    def _new_session(self, principal=None, client_capabilities=None):
        import uuid
        now = self.clock()
        with self._session_lock:
            self._expire_sessions(now)
            if len(self._sessions) >= MAX_HTTP_SESSIONS:
                return None
            session_id = str(uuid.uuid4())
            self._sessions[session_id] = {'created_at': now, 'last_seen': now, 'ready': False,
                                          'protocol_version': PROTOCOL,
                                          'principal_id': principal_identity(principal),
                                          'client_capabilities': json.loads(json.dumps(client_capabilities or {}))}
        return session_id

    def _expire_sessions(self, now):
        cutoff = now - self.session_ttl_seconds
        for session_id, session in list(self._sessions.items()):
            if session['last_seen'] < cutoff:
                self._sessions.pop(session_id, None)

    def _get_session(self, session_id):
        if not isinstance(session_id, str) or len(session_id) > 128:
            return None
        now = self.clock()
        with self._session_lock:
            self._expire_sessions(now)
            session = self._sessions.get(session_id)
            if session is not None:
                session['last_seen'] = now
            return session

    def handle(self, method, path, headers, body, *, remote_addr='127.0.0.1'):
        if path != ENDPOINT_PATH:
            return self._reply(404)
        try:
            address = ipaddress.ip_address(remote_addr)
        except (TypeError, ValueError):
            return self._reply(403)
        if not address.is_loopback:
            return self._reply(403)
        try:
            fields = _header_map(headers)
        except (AttributeError, TypeError, ValueError):
            return self._reply(400)
        host = fields.get('host')
        if not host:
            return self._reply(400)
        try:
            authority = urlsplit('http://' + host)
            host_port = authority.port
        except ValueError:
            return self._reply(403)
        if (authority.hostname not in LOCAL_HOSTS or authority.username is not None
                or authority.password is not None or authority.path or authority.query
                or authority.fragment or host_port != self.expected_port):
            return self._reply(403)
        origin = fields.get('origin')
        if origin is not None and not _local_origin(origin, self.expected_port):
            return self._reply(403)
        if not self.allow_single_user_local:
            return self._reply(401)
        try:
            principal = self.principal_provider()
        except Exception:
            return self._reply(503)
        if not isinstance(principal, LocalPrincipal):
            return self._reply(503)
        if method == 'DELETE':
            session_id = fields.get('mcp-session-id')
            with self._session_lock:
                session = self._sessions.get(session_id)
                same_principal = (session is not None
                                  and session.get('principal_id') == principal_identity(principal))
                existed = self._sessions.pop(session_id, None) is not None if same_principal else False
            return self._reply(204 if existed else 404)
        if method != 'POST':
            return self._reply(405, headers={'Allow': 'POST, DELETE'})
        if fields.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            return self._reply(415)
        if not _accepts_required(fields.get('accept')):
            return self._reply(406)
        if 'transfer-encoding' in fields:
            return self._reply(400)
        try:
            length = int(fields.get('content-length', '-1'))
        except (TypeError, ValueError):
            return self._reply(400)
        body_length = len(body) if isinstance(body, bytes) else -1
        if length < 0 or length > MAX_HTTP_BODY or body_length != length:
            return self._reply(413 if length > MAX_HTTP_BODY or body_length > MAX_HTTP_BODY else 400)
        try:
            message = _json_body(body)
        except (UnicodeError, ValueError, RecursionError, json.JSONDecodeError):
            return self._reply(400)
        identifier = message.get('id')
        request_method = message.get('method')
        if (message.get('jsonrpc') != '2.0' or not isinstance(request_method, str)
                or set(message) - {'jsonrpc', 'id', 'method', 'params'}
                or ('params' in message and not isinstance(message['params'], dict))):
            return self._reply(400, self._error(None, -32600, 'Invalid JSON-RPC request'))
        params = message.get('params')
        if not isinstance(params, dict):
            params = {}
        metadata = params.get('_meta')
        if not isinstance(metadata, dict):
            metadata = {}
        client_capabilities = {}
        protocol_header = fields.get('mcp-protocol-version')
        rpc_method_header = fields.get('mcp-method')
        if protocol_header is None and request_method != 'initialize':
            session = self._get_session(fields.get('mcp-session-id'))
            if session is not None:
                # The 2025 transport permits the negotiated version to be
                # inferred from the initialized session when this header is absent.
                protocol_header = session.get('protocol_version')
        if ('id' in message and not (type(identifier) is int
                or isinstance(identifier, str) and len(identifier) <= 128)):
            return self._reply(400, self._error(None, -32600, 'Invalid JSON-RPC request ID'))
        if protocol_header == REQUEST_PROTOCOL:
            requested_version = metadata.get(VERSION_META)
            if protocol_header != requested_version or rpc_method_header != request_method:
                return self._reply(400, self._error(identifier, -32020,
                                                    'MCP request headers do not match the JSON body'))
        elif protocol_header == PROTOCOL or (protocol_header is None and request_method == 'initialize'):
            if (protocol_header == PROTOCOL and VERSION_META in metadata
                    and metadata.get(VERSION_META) != PROTOCOL):
                return self._reply(400, self._error(identifier, -32020,
                    'MCP request headers do not match the JSON body'))
            if rpc_method_header is not None and rpc_method_header != request_method:
                return self._reply(400, self._error(identifier, -32020,
                                                    'MCP request headers do not match the JSON body'))
        else:
            return self._reply(400, self._error(identifier, -32022,
                'Unsupported protocol version', {'supported': [REQUEST_PROTOCOL, PROTOCOL]}))
        if (request_method == 'initialize' and protocol_header is not None
                and protocol_header != params.get('protocolVersion')):
            return self._reply(400, self._error(identifier, -32020,
                                                'MCP request headers do not match the JSON body'))
        expected_name = None
        event_name_method = request_method in ('events/subscribe', 'events/unsubscribe')
        if request_method == 'tools/call' or event_name_method:
            expected_name = params.get('name')
        elif request_method == 'resources/read':
            expected_name = params.get('uri')
        elif request_method == 'prompts/get':
            expected_name = params.get('name')
        supplied_name = fields.get('mcp-name')
        if request_method in ('tools/call', 'resources/read', 'prompts/get') or event_name_method:
            decoded_name = _decode_mcp_name(supplied_name) if supplied_name is not None else None
            if (not isinstance(expected_name, str) or len(expected_name) > MAX_MCP_NAME
                    # Preserve the existing headerless Events profile while
                    # validating the newer draft's routing header when supplied.
                    or (protocol_header == REQUEST_PROTOCOL and supplied_name is None
                        and not event_name_method)
                    or (supplied_name is not None and decoded_name != expected_name)):
                return self._reply(400, self._error(identifier, -32020,
                    'MCP request headers do not match the JSON body'))
        elif supplied_name is not None:
            return self._reply(400, self._error(identifier, -32020,
                'MCP request headers do not match the JSON body'))

        if protocol_header == PROTOCOL or (protocol_header is None and request_method == 'initialize'):
            if request_method == 'initialize':
                if 'id' not in message:
                    return self._reply(400, self._error(None, -32600, 'Initialize requires a request ID'))
                if (set(params) - {'protocolVersion', 'capabilities', 'clientInfo', '_meta'}
                        or params.get('protocolVersion') != PROTOCOL
                        or not isinstance(params.get('capabilities'), dict)):
                    return self._reply(400, self._error(identifier, -32602, 'Invalid initialization request'))
                client_info = params.get('clientInfo')
                if (not isinstance(client_info, dict)
                        or any(not isinstance(client_info.get(key), str) or len(client_info[key]) > 128
                               for key in ('name', 'version'))):
                    return self._reply(400, self._error(identifier, -32602, 'Invalid initialization request'))
                session_id = self._new_session(principal, params['capabilities'])
                if session_id is None:
                    return self._reply(503, self._error(identifier, -32000,
                                                        'MCP session capacity reached'))
                self.adapter.initialized = True
                capabilities = {'tools': {'listChanged': False}}
                if getattr(self.adapter, 'events_enabled', False):
                    capabilities['events'] = {}
                response = {'jsonrpc': '2.0', 'id': identifier, 'result': {
                    'protocolVersion': PROTOCOL, 'capabilities': capabilities,
                    'serverInfo': dict(SERVER_INFO)}}
                return self._reply(200, response, headers={'Mcp-Session-Id': session_id})
            session_id = fields.get('mcp-session-id')
            session = self._get_session(session_id)
            if session is None:
                return self._reply(404, self._error(identifier, -32001, 'Session not found'))
            if session.get('principal_id') != principal_identity(principal):
                return self._reply(403, self._error(identifier, -32003, 'Session principal mismatch'))
            if protocol_header is not None and protocol_header != session.get('protocol_version'):
                return self._reply(400, self._error(identifier, -32020,
                                                    'MCP protocol version does not match the session'))
            if request_method == 'notifications/initialized' and 'id' not in message:
                session['ready'] = True
                self.adapter.initialized = True
                self.adapter.ready = True
                return self._reply(202)
            client_capabilities = session.get('client_capabilities', {})
            if not session['ready']:
                return self._reply(400, self._error(identifier, -32002, 'Session is not initialized'))
            if 'id' not in message:
                return self._reply(400, self._error(None, -32600, 'Unsupported notification'))
        else:
            if 'id' not in message:
                return self._reply(400, self._error(None, -32600,
                                                    'This protocol version requires a request ID'))
            if protocol_header is None or not isinstance(metadata.get(CAPABILITIES_META), dict):
                return self._reply(400, self._error(identifier, -32602,
                                                    'Missing client capabilities'))
            client_capabilities = metadata[CAPABILITIES_META]
        try:
            with self._adapter_lock:
                contextual = getattr(self.adapter, 'handle_with_client_capabilities', None)
                if callable(contextual):
                    response = contextual(message, principal=principal,
                                          client_capabilities=client_capabilities)
                else:
                    response = self.adapter.handle(message, principal=principal)
        except Exception:
            response = self._error(identifier, -32603, 'MCP server unavailable')
        if not isinstance(response, dict):
            return self._reply(400, self._error(identifier, -32600,
                                                'Expected a JSON-RPC request'))
        error = response.get('error')
        if isinstance(error, dict) and error.get('code') == -32601:
            return self._reply(404, response)
        try:
            encoded = json.dumps(response, ensure_ascii=False, allow_nan=False,
                                  separators=(',', ':')).encode('utf-8')
        except (TypeError, ValueError):
            return self._reply(500, self._error(identifier, -32603, 'MCP response unavailable'))
        if len(encoded) > MAX_HTTP_BODY:
            return self._reply(500, self._error(identifier, -32603, 'MCP response exceeds bound'))
        return self._reply(200, response)


def make_http_server(adapter, *, host='127.0.0.1', port=8765,
                     request_deadline_seconds=DEFAULT_HTTP_REQUEST_DEADLINE_SECONDS,
                     allow_single_user_local=False):
    """Create a loopback HTTP server only under the explicit local-user policy."""
    if host != '127.0.0.1':
        raise ValueError('MCP HTTP server must bind to 127.0.0.1')
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('Invalid MCP HTTP port')
    if (type(request_deadline_seconds) not in (int, float)
            or not 0.05 <= float(request_deadline_seconds) <= 120.0):
        raise ValueError('HTTP request deadline must be between 0.05 and 120 seconds')
    if allow_single_user_local is not True:
        raise ValueError('Single-user local HTTP mode must be explicitly enabled')
    principal_provider = getattr(adapter, 'principal_provider', None)
    if not callable(principal_provider):
        raise ValueError('Local principal provider is required for HTTP mode')

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = False

    endpoint_holder = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'MCP/2.0'
        sys_version = ''

        def log_message(self, _format, *args):
            # HTTP request lines can contain private callback URLs and ids.
            return

        def _abort_request(self):
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def handle_one_request(self):
            try:
                run_with_total_deadline(
                    float(request_deadline_seconds), self._abort_request,
                    lambda: super(Handler, self).handle_one_request())
            except (OSError, TimeoutError):
                self.close_connection = True

        def _dispatch(self):
            header_items = list(self.headers.items())
            try:
                normalized_headers = _header_map(header_items)
            except (AttributeError, TypeError, ValueError):
                self.close_connection = True
                self.send_response(400)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.command != 'POST':
                status, headers, body = endpoint_holder['endpoint'].handle(
                    self.command, self.path, header_items, b'',
                    remote_addr=self.client_address[0])
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)
                return
            if self.path != ENDPOINT_PATH:
                self.close_connection = True
                self.send_response(404)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if 'transfer-encoding' in normalized_headers:
                self.close_connection = True
                self.send_response(400)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            length_values = self.headers.get_all('Content-Length') or []
            length_text = length_values[0] if len(length_values) == 1 else None
            try:
                length = int(length_text) if length_text is not None else -1
            except ValueError:
                length = -1
            if length < 0 or length > MAX_HTTP_BODY:
                self.close_connection = True
                self.send_response(413 if length > MAX_HTTP_BODY else 400)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            body = self.rfile.read(length)
            response_status, response_headers, response_body = endpoint_holder['endpoint'].handle(
                self.command, self.path, header_items, body,
                remote_addr=self.client_address[0])
            self.send_response(response_status)
            for name, value in response_headers.items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(response_body)))
            self.end_headers()
            if response_body:
                self.wfile.write(response_body)

        do_POST = _dispatch
        do_GET = _dispatch
        do_DELETE = _dispatch
        do_PUT = _dispatch
        do_OPTIONS = _dispatch

    server = Server((host, port), Handler)
    try:
        endpoint_holder['endpoint'] = McpHttpEndpoint(
            adapter, expected_port=server.server_port,
            allow_single_user_local=True, principal_provider=principal_provider)
    except Exception:
        # Configuration validation happens after binding because an ephemeral
        # port is part of the endpoint's Host check. Release the socket on every
        # invalid configuration path instead of leaving an unreachable listener.
        server.server_close()
        raise
    server.mcp_endpoint = endpoint_holder['endpoint']
    return server


def serve_http(adapter, *, port=8765,
               request_deadline_seconds=DEFAULT_HTTP_REQUEST_DEADLINE_SECONDS,
               allow_single_user_local=False):
    """Explicit server runner. It never chooses a non-loopback bind address."""
    server = make_http_server(adapter, host='127.0.0.1', port=port,
                              request_deadline_seconds=request_deadline_seconds,
                              allow_single_user_local=allow_single_user_local)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
