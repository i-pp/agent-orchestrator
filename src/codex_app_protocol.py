"""Offline-first boundary for codex-cli 0.165.0 app-server JSON messages.

No socket, authentication or default transport is provided. StdioTransport is
opt-in and starts only when start() is explicitly called. The host owns
one connection, serialization and deadlines. Its injected exchange(message,
timeout_s) sends exactly once and returns the raw {id, result}/{id, error}
envelope, or None for initialized. It may call receive() for intervening server
messages. It MUST bound IO by timeout_s; this client cannot preempt host code.
Schema inspection and fake exchanges do not establish a live connection.
"""

import json
import math
import os
import re
import selectors
import subprocess
import time
from collections.abc import Callable


SCHEMA_VERSION = "codex-cli 0.165.0"
# Installed service binary was also inspected with generate-json-schema only.
# Across 26 relevant schemas, differences are confined to CodexErrorInfo
# variants, which this boundary does not interpret or export.
COMPATIBLE_SCHEMA_VERSIONS = ('0.165.0', '0.164.0-alpha.11.1')


def schema_version_supported(user_agent):
    return isinstance(user_agent, str) and any(re.search(r'(?<![A-Za-z0-9])' + re.escape(v)
        + r'(?![A-Za-z0-9.\-])', user_agent) for v in COMPATIBLE_SCHEMA_VERSIONS)
# SHA-256 of generated artifacts inspected locally, not a runtime dependency.
SCHEMA_SHA256 = {
    "ClientRequest.json": "9b89e2459362d20cec67a429ef9430fe6b0e9fb8bd87d445d4c9a6acccd87450",
    "JSONRPCResponse.json": "4796738c04c74288213a08fb8d820c7b4df19e0977cdcd35b65ffcb43cfc93ab",
    "JSONRPCNotification.json": "c2b43f26880db331393fe09f34bdd76dfeb4dc30d8418d1c26e18db166d6c8c8",
    "ServerRequest.json": "f339be472737a0003efa25fba2e6e6c9237e621cd065b6d6995c51256e9dc1fb",
    "ServerNotification.json": "7e8324c4c95da9057611ffe40487c21db40eed69c247a1508d0651e89715e23a",
    "v1/InitializeParams.json": "b55124e7c0f36a1789396daa5578217a92be67f053903696236686e59f6f212e",
    "v1/InitializeResponse.json": "62ad689c2cb6379913c1d72749cfd8de5089d35760214123518eb92eef11acc9",
    "v2/ModelListResponse.json": "2fb387a2ed258fc74258828ea342d68f89ce4e8e22cd0329ca59d2b423f9108b",
    "v2/ThreadStartParams.json": "e9c6d3cc18d049bfbc0249add3808fb8e5a27e1a0b5a423767aafbc3859a9428",
    "v2/TurnSteerParams.json": "857e7a2b061ae46ed6aedea6a5fddd3a6ce14f0334bf7eafaf10d4cbe68ec78b",
    "v2/GetAccountParams.json": "30b545275f60975b55bd6fbaafd70f15505801d14168ee82d3931b6e93c78aab",
    "v2/GetAccountResponse.json": "03fa0cbf2d7483fdb96eee6e4c8cb3692404cefd125b24187ca4d2e63fd7fbda",
}
# (params schema, result schema, required params); supported fields are a strict
# subset. Future schema fields require deliberate review before being exposed.
METHOD_SCHEMAS = {
    "initialize": ("v1/InitializeParams.json", "v1/InitializeResponse.json", ("clientInfo",)),
    "model/list": ("v2/ModelListParams.json", "v2/ModelListResponse.json", ()),
    "account/read": ("v2/GetAccountParams.json", "v2/GetAccountResponse.json", ()),
    "thread/start": ("v2/ThreadStartParams.json", "v2/ThreadStartResponse.json", ()),
    "thread/resume": ("v2/ThreadResumeParams.json", "v2/ThreadResumeResponse.json", ("threadId",)),
    "thread/read": ("v2/ThreadReadParams.json", "v2/ThreadReadResponse.json", ("threadId",)),
    "thread/list": ("v2/ThreadListParams.json", "v2/ThreadListResponse.json", ()),
    "thread/archive": ("v2/ThreadArchiveParams.json", "v2/ThreadArchiveResponse.json", ("threadId",)),
    "turn/start": ("v2/TurnStartParams.json", "v2/TurnStartResponse.json", ("input", "threadId")),
    "turn/steer": ("v2/TurnSteerParams.json", "v2/TurnSteerResponse.json", ("expectedTurnId", "input", "threadId")),
    "turn/interrupt": ("v2/TurnInterruptParams.json", "v2/TurnInterruptResponse.json", ("threadId", "turnId")),
}
ALLOWED_METHODS = frozenset(METHOD_SCHEMAS)
_THREAD_FIELDS = {"model", "cwd", "baseInstructions", "developerInstructions",
                  "approvalPolicy", "approvalsReviewer", "sandbox"}
_FIELDS = {
    "initialize": {"clientInfo"},
    "model/list": {"cursor", "limit", "includeHidden"},
    "account/read": {"refreshToken"},
    "thread/start": _THREAD_FIELDS | {"ephemeral"},
    "thread/resume": _THREAD_FIELDS | {"threadId", "excludeTurns"},
    "thread/read": {"threadId", "includeTurns"},
    "thread/list": {"cursor", "limit", "archived", "cwd", "searchTerm", "sortDirection", "sortKey"},
    "thread/archive": {"threadId"},
    "turn/start": {"threadId", "input", "clientUserMessageId", "model", "effort", "cwd",
                   "approvalPolicy", "approvalsReviewer", "sandboxPolicy"},
    "turn/steer": {"threadId", "expectedTurnId", "input", "clientUserMessageId"},
    "turn/interrupt": {"threadId", "turnId"},
}
APPROVAL_METHODS = frozenset({
    "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
    "item/permissions/requestApproval", "applyPatchApproval", "execCommandApproval",
})
_SERVER_REQUESTS = APPROVAL_METHODS | {
    "item/tool/requestUserInput", "mcpServer/elicitation/request", "item/tool/call",
    "account/chatgptAuthTokens/refresh", "attestation/generate",
}
# Only interpreted lifecycle notifications. Others are forwarded as unknown.
_NOTIFICATIONS = frozenset({"thread/started", "thread/status/changed", "thread/archived",
    "thread/closed", "turn/started", "turn/completed", "error", "serverRequest/resolved",
    "item/started", "item/completed", "item/agentMessage/delta", "account/updated",
    "account/gatewayOAuth/changed", "account/rateLimits/updated", "account/login/completed"})


def _json_type(value):
    return {dict: 'object', list: 'array', str: 'string', int: 'integer',
            float: 'number', bool: 'boolean', type(None): 'null'}.get(type(value), 'other')


def frame_shape(value):
    """Fixed allowlisted key/type metadata. No account, trace or error values."""
    def members(obj, allowed):
        if type(obj) is not dict: return {'type': _json_type(obj)}
        known = set(obj) & set(allowed)
        return {'type': 'object', 'fields': {k: _json_type(obj[k]) for k in sorted(known)},
                'other_field_count': len(set(obj) - known)}
    shape = members(value, ('id', 'method', 'params', 'result', 'error', 'jsonrpc', 'trace', '_meta'))
    if type(value) is not dict: return shape
    if 'method' in value:
        method = value['method']
        shape['method_class'] = method if isinstance(method, str) and method in (_SERVER_REQUESTS | _NOTIFICATIONS) else 'OTHER'
    for key, allowed in [('trace', ('traceparent', 'tracestate')),
                         ('result', ('account', 'requiresOpenaiAuth', 'data', 'nextCursor')),
                         ('error', ('code', 'message', 'data'))]:
        if key in value: shape[key] = members(value[key], allowed)
    result = value.get('result')
    if isinstance(result, dict) and 'account' in result:
        shape['account'] = members(result['account'], ('type', 'email', 'planType'))
    return shape


def _server_message(message):
    allowed = {'id', 'method', 'params', 'jsonrpc'}
    request = isinstance(message, dict) and 'id' in message
    if request:
        allowed.add('trace')
    elif type(message) is dict:
        # The installed generic JSONRPCNotification schema permits extension
        # fields. Discard them before delivery; do not interpret them as RPCs.
        # Keep mixed response/event envelopes invalid rather than swallowing a
        # result or error as an ignored notification extension.
        if 'result' in message or 'error' in message:
            raise ProtocolError('mixed server message')
        message = {k: v for k, v in message.items() if k in allowed}
    _object(message, allowed, {'method'})
    if (type(message['method']) is not str or not message['method']
            or message.get('jsonrpc', '2.0') != '2.0'
            or ('id' in message and not _valid_id(message['id']))):
        raise ProtocolError('invalid server message')
    if 'trace' in message and message['trace'] is not None:
        trace = message['trace']
        if type(trace) is not dict or any(k in trace and trace[k] is not None and type(trace[k]) is not str
                                         for k in ('traceparent', 'tracestate')):
            raise ProtocolError('invalid server trace context')
    # Generic JSONRPCRequest schema permits optional trace; lifecycle consumers
    # need neither its values nor its future fields. Never forward/store it.
    return {k: v for k, v in message.items() if k != 'trace'}


class ProtocolError(RuntimeError):
    """Rejected before sending, or invalid incoming event."""
    status = "REJECTED"


class ProtocolUnknown(ProtocolError):
    """The peer may have acted; reconcile explicitly and never auto-resend."""
    status = "UNKNOWN"


class RpcError(ProtocolError):
    status = "FAILED"

    def __init__(self, code, message):
        # Keep untrusted server messages out of implicit logs/exception rendering.
        super().__init__(f"app-server rejected request (code {code})")
        self.code = code
        self.server_message = message


def _object(value, allowed, required=()):
    if type(value) is not dict or set(value) - set(allowed) or set(required) - set(value):
        raise ProtocolError("missing or unsupported object fields")


def _json_copy(value, limit):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > limit:
            raise ProtocolError("message exceeds byte limit")
        return json.loads(raw)
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise ProtocolError("message must be finite JSON") from exc


def _valid_id(value):
    return type(value) is str or (type(value) is int and -(2**63) <= value < 2**63)


def _params(method, params):
    _object(params, _FIELDS[method], METHOD_SCHEMAS[method][2])
    if method == "account/read":
        if params != {"refreshToken": False} or type(params.get("refreshToken")) is not bool:
            raise ProtocolError("account/read requires refreshToken=false")
        return
    for key, value in params.items():
        if key in {"clientInfo", "input", "sandboxPolicy"}:
            continue
        if key in {"includeHidden", "ephemeral", "excludeTurns", "includeTurns", "archived"}:
            if type(value) is not bool:
                raise ProtocolError("boolean parameter required")
        elif key == "limit":
            if type(value) is not int or not 0 <= value < 2**32:
                raise ProtocolError("limit must be uint32")
        elif type(value) is not str or not value:
            raise ProtocolError("nonempty string parameter required")
    for key, allowed in {
        "approvalPolicy": {"untrusted", "on-request"}, "approvalsReviewer": {"user"},
        "sandbox": {"read-only", "workspace-write"},
        "sortDirection": {"asc", "desc"}, "sortKey": {"created_at", "updated_at"},
    }.items():
        if key in params and params[key] not in allowed:
            raise ProtocolError("unsupported parameter value")
    if "clientInfo" in params:
        info = params["clientInfo"]
        _object(info, {"name", "title", "version"}, {"name", "version"})
        if any(type(v) is not str or not v for v in info.values()):
            raise ProtocolError("invalid clientInfo")
    if "input" in params:
        # Text-only task posting; no attachment paths, tool output or dynamic RPC.
        if type(params["input"]) is not list or not params["input"]:
            raise ProtocolError("nonempty input list required")
        for item in params["input"]:
            _object(item, {"type", "text", "text_elements"}, {"type", "text"})
            if item["type"] != "text" or type(item["text"]) is not str:
                raise ProtocolError("only text input is supported")
            if "text_elements" in item and item["text_elements"] != []:
                raise ProtocolError("text elements are unsupported")
    if "sandboxPolicy" in params:
        # Read-only is enough for this boundary; workspace profiles belong to a
        # separately reviewed host policy, not arbitrary nested configuration.
        if params["sandboxPolicy"] != {"type": "readOnly"}:
            raise ProtocolError("only readOnly sandboxPolicy is supported")


def _result(method, result):
    if type(result) is not dict:
        raise ProtocolError("result must be an object")
    if method == "account/read":
        if type(result.get("requiresOpenaiAuth")) is not bool:
            raise ProtocolError("invalid account result")
        account = result.get("account")
        if account is not None:
            if (type(account) is not dict or type(account.get("type")) is not str
                    or account["type"] not in {"chatgpt", "apiKey", "amazonBedrock", "gcp"}):
                raise ProtocolError("unknown account type")
            account = {"type": account["type"]}
        # Deliberately do not return email, account ids, plan metadata or future
        # auth fields through the public client API.
        return {"requiresOpenaiAuth": result["requiresOpenaiAuth"], "account": account}
    required = {
        "initialize": {"codexHome": str, "platformFamily": str, "platformOs": str, "userAgent": str},
        "model/list": {"data": list}, "thread/list": {"data": list},
        "thread/start": {"thread": dict, "approvalPolicy": (str, dict), "approvalsReviewer": str,
                         "cwd": str, "model": str, "modelProvider": str, "sandbox": dict},
        "thread/read": {"thread": dict}, "turn/start": {"turn": dict},
        "turn/steer": {"turnId": str},
    }
    required["thread/resume"] = required["thread/start"]
    if any(key not in result or not isinstance(result[key], kind)
           for key, kind in required.get(method, {}).items()):
        raise ProtocolError("invalid result shape")
    if "thread" in result and (type(result["thread"].get("id")) is not str
                               or not result["thread"]["id"]):
        raise ProtocolError("thread id missing")
    if "turn" in result:
        turn = result["turn"]
        if (type(turn.get("id")) is not str or not turn["id"]
                or type(turn.get("items")) is not list or type(turn.get("status")) is not str):
            raise ProtocolError("invalid turn shape")
    if method == "model/list":
        model_fields = {"id": str, "model": str, "displayName": str, "description": str,
                        "hidden": bool, "isDefault": bool, "defaultReasoningEffort": str,
                        "supportedReasoningEfforts": list}
        for model in result["data"]:
            if (type(model) is not dict or any(type(model.get(k)) is not t
                                              for k, t in model_fields.items())):
                raise ProtocolError("invalid model catalog entry")
    if method == "thread/list" and any(type(t) is not dict or type(t.get("id")) is not str
                                       or not t["id"] for t in result["data"]):
        raise ProtocolError("invalid thread list entry")
    # Preserve raw schema-shaped catalog/threads; availability is server-owned.
    for key in ("nextCursor", "backwardsCursor"):
        if key in result and result[key] is not None and type(result[key]) is not str:
            raise ProtocolError("invalid pagination cursor")
    return result


class ProtocolClient:
    def __init__(self, exchange: Callable | None = None, event_sink: Callable | None = None,
                 *, timeout_s=30, max_payload_bytes=262144, max_response_bytes=1048576):
        if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0
                or any(type(n) is not int or n <= 0 for n in (max_payload_bytes, max_response_bytes))):
            raise ProtocolError("positive timeout and byte limits required")
        if exchange is not None and not callable(exchange):
            raise ProtocolError("exchange must be callable")
        if event_sink is not None and not callable(event_sink):
            raise ProtocolError("event_sink must be callable")
        self.exchange = exchange
        self.event_sink = event_sink
        self.timeout_s = timeout_s
        self.max_payload_bytes = max_payload_bytes
        self.max_response_bytes = max_response_bytes
        self.initialized = False
        self._init_attempted = False
        self._sequence = 0
        self._busy = False
        self._diag = {'last_method': None, 'stage': 'constructed', 'failure_code': None,
                      'initialize_acknowledged': False, 'initialized_notification_written': False}

    def diagnostics(self):
        return {**self._diag, 'initialized': self.initialized, 'initialize_attempted': self._init_attempted,
                'request_sequence': self._sequence}

    def _exchange(self, message, *, notification=False):
        if self.exchange is None:
            raise ProtocolError("no transport configured")
        message = _json_copy(message, self.max_payload_bytes)
        self._diag.update(last_method=message['method'], stage='exchange', failure_code=None)
        for key in ('rpc_error_code', 'last_response_shape', 'last_server_message_shape'):
            self._diag.pop(key, None)
        start = time.monotonic()
        try:
            response = self.exchange(message, self.timeout_s)
        except Exception as exc:
            self._diag['failure_code'] = 'TRANSPORT_UNKNOWN'
            raise ProtocolUnknown("exchange failed; reconcile before any manual retry") from exc
        if time.monotonic() - start > self.timeout_s:
            self._diag['failure_code'] = 'EXCHANGE_TIMEOUT'
            raise ProtocolUnknown("exchange deadline exceeded; outcome unknown")
        try:
            self._diag['stage'] = 'response_validation'
            if notification:
                if response is not None:
                    raise ProtocolError("notification must not receive a response")
                return None
            response = _json_copy(response, self.max_response_bytes)
            self._diag['last_response_shape'] = frame_shape(response)
            _object(response, {"id", "result", "error", "jsonrpc"}, {"id"})
            if (not _valid_id(response["id"]) or type(response["id"]) is not type(message["id"])
                    or response["id"] != message["id"] or response.get("jsonrpc", "2.0") != "2.0"
                    or ("result" in response) == ("error" in response)):
                raise ProtocolError("uncorrelated or malformed response")
            if "error" in response:
                error = response["error"]
                _object(error, {"code", "message", "data"}, {"code", "message"})
                if type(error["code"]) is not int or type(error["message"]) is not str:
                    raise ProtocolError("invalid RPC error")
                raise RpcError(error["code"], error["message"])
            self._diag['stage'] = 'result_validation'
            result = _result(message["method"], response["result"])
            self._diag['stage'] = 'result_verified'
            return result
        except RpcError as exc:
            self._diag['failure_code'] = 'SERVER_REJECTED'
            self._diag['rpc_error_code'] = exc.code
            raise
        except ProtocolError as exc:
            self._diag['failure_code'] = 'INVALID_RESULT_SCHEMA' if self._diag['stage'] == 'result_validation' else 'INVALID_RESPONSE_ENVELOPE'
            raise ProtocolUnknown("invalid acknowledgement; outcome unknown") from exc

    def initialize(self, client_info=None):
        if self.initialized or self._init_attempted:
            raise ProtocolError("initialize already attempted on this connection")
        if self.exchange is None:
            raise ProtocolError("no transport configured")
        params = {"clientInfo": client_info if client_info is not None else
                  {"name": "chat-codex-pipeline", "version": "1"}}
        _params("initialize", params)
        _json_copy(params, self.max_payload_bytes)
        self._init_attempted = True
        result = self._request("initialize", params)
        self._diag['initialize_acknowledged'] = True
        self._exchange({"method": "initialized"}, notification=True)
        self._diag['initialized_notification_written'] = True
        self.initialized = True
        return result

    def request(self, method, params=None):
        if type(method) is not str or method not in ALLOWED_METHODS or method == "initialize":
            raise ProtocolError("method is not exposed")
        if not self.initialized:
            raise ProtocolError("initialize handshake required")
        params = {} if params is None else params
        if method == "account/read" and params == {}:
            params = {"refreshToken": False}
        _params(method, params)
        return self._request(method, params)

    def _request(self, method, params):
        if self._busy:
            raise ProtocolError("only one in-flight request is supported")
        self._sequence += 1
        self._busy = True
        try:
            return self._exchange({"id": self._sequence, "method": method, "params": params})
        finally:
            self._busy = False

    def model_catalog(self, **params):
        """Return one raw model/list page; no implicit pagination or model probe."""
        return self.request("model/list", params)

    def receive(self, message):
        """Forward server messages without answering, granting or replaying them.

        Unknown methods are delivered with known=False for host diagnostics.
        No response-writing or approval-resolution capability is exposed here.
        """
        message = _json_copy(message, self.max_response_bytes)
        self._diag['last_server_message_shape'] = frame_shape(message)
        message = _server_message(message)
        request = "id" in message
        method = message["method"]
        event = {"kind": "server_request" if request else "notification",
                 "known": method in (_SERVER_REQUESTS if request else _NOTIFICATIONS),
                 "approval_required": request and method in APPROVAL_METHODS,
                 "message": message}
        if self.event_sink is not None:
            self.event_sink(event)
        return event


class StdioTransport:
    """Explicit local app-server child, with no argv/config/provider/env overrides.

    Construction is inert. Call start() only with authorization to start the
    existing Codex executor. Existing Codex configuration/auth are inherited;
    this class does not log in, refresh credentials or choose a billing route.
    bind_client(client) routes raw server events through client.receive(). The
    host calls poll() between requests to observe asynchronous notifications.
    A timeout/protocol failure poisons this connection; close() reaps the child.
    """

    COMMAND = ("codex", "app-server", "--listen", "stdio://")

    def __init__(self, event_sink=None, *, max_frame_bytes=1048576, shutdown_timeout_s=2):
        if event_sink is not None and not callable(event_sink):
            raise ProtocolError("event_sink must be callable")
        if (type(max_frame_bytes) is not int or max_frame_bytes <= 0
                or type(shutdown_timeout_s) not in (int, float)
                or not math.isfinite(shutdown_timeout_s) or shutdown_timeout_s <= 0):
            raise ProtocolError("positive frame and shutdown limits required")
        self.event_sink = event_sink
        self.max_frame_bytes = max_frame_bytes
        self.shutdown_timeout_s = shutdown_timeout_s
        self._proc = None
        self._started = False
        self._closed = False
        self._broken = False
        self._active = False
        self._buffer = b""
        self._read_selector = None
        self._write_selector = None
        self._stderr_tail = b''
        self._diag = {'stage': 'constructed', 'request_count': 0, 'stdout_bytes': 0,
                      'stderr_bytes': 0, 'frames_received': 0, 'stderr_categories': [],
                      'failure_code': None, 'termination_requested': False,
                      'exit_code_before_close': None, 'reaped': False}

    def diagnostics(self):
        """Only fixed stages/counts/categories; never raw streams, paths or env."""
        return json.loads(json.dumps(self._diag))

    def _failure(self, code):
        self._diag['failure_code'] = code

    def _stderr(self):
        try:
            data = os.read(self._proc.stderr.fileno(), 4096)
        except BlockingIOError:
            return
        if not data:
            for selector in (self._read_selector, self._write_selector):
                try:
                    selector.unregister(self._proc.stderr)
                except KeyError:
                    pass
            return
        self._diag['stderr_bytes'] += len(data)
        # Classify a bounded moving window, then discard all raw diagnostics.
        sample = (self._stderr_tail + data).lower()
        for marker, category in [(b'read-only file system', 'READ_ONLY_FILESYSTEM'),
                (b'permission denied', 'PERMISSION_DENIED'), (b'error loading config', 'CONFIG_LOAD_ERROR'),
                (b'address already in use', 'ADDRESS_IN_USE')]:
            if marker in sample and category not in self._diag['stderr_categories']:
                self._diag['stderr_categories'].append(category)
        self._stderr_tail = sample[-128:]
        if self._diag['stderr_bytes'] > 65536:
            self._failure('STDERR_CAPACITY')
            raise ProtocolUnknown('stderr diagnostic capacity exceeded')

    def bind_client(self, client):
        if self._active or self._started:
            raise ProtocolError("bind client before starting transport")
        if not isinstance(client, ProtocolClient):
            raise ProtocolError("ProtocolClient required")
        self.event_sink = client.receive
        return self

    def start(self):
        if self._started or self._closed:
            raise ProtocolError("transport cannot be restarted")
        self._started = True
        try:
            self._proc = self._spawn()
            os.set_blocking(self._proc.stdout.fileno(), False)
            os.set_blocking(self._proc.stdin.fileno(), False)
            os.set_blocking(self._proc.stderr.fileno(), False)
            self._read_selector = selectors.DefaultSelector()
            self._read_selector.register(self._proc.stdout, selectors.EVENT_READ, 'stdout')
            self._read_selector.register(self._proc.stderr, selectors.EVENT_READ, 'stderr')
            self._write_selector = selectors.DefaultSelector()
            self._write_selector.register(self._proc.stdin, selectors.EVENT_WRITE, 'stdin')
            self._write_selector.register(self._proc.stderr, selectors.EVENT_READ, 'stderr')
            self._diag['stage'] = 'started'
        except Exception as exc:
            self._failure('PROCESS_START')
            self.close()
            raise ProtocolError("could not start fixed stdio executor") from exc
        return self

    def _spawn(self):
        return subprocess.Popen(list(self.COMMAND), shell=False,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, close_fds=True)

    def _ready(self):
        if self._proc is None or self._closed or self._broken:
            raise ProtocolError("stdio transport is not available")

    def _write(self, message, deadline):
        self._diag['stage'] = 'writing'
        data = (json.dumps(_json_copy(message, self.max_frame_bytes), ensure_ascii=False,
                           allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        sent = 0
        while sent < len(data):
            remaining = deadline - time.monotonic()
            ready = self._write_selector.select(max(0, remaining)) if remaining > 0 else []
            if not ready:
                self._failure('WRITE_TIMEOUT')
                raise ProtocolUnknown("stdio write deadline exceeded")
            for key, _ in ready:
                if key.data == 'stderr': self._stderr()
            if not any(key.data == 'stdin' for key, _ in ready): continue
            try:
                count = os.write(self._proc.stdin.fileno(), data[sent:])
            except BlockingIOError:
                continue
            if count <= 0:
                self._failure('STDIN_CLOSED')
                raise ProtocolUnknown("stdio write disconnected")
            sent += count

    def _read(self, deadline):
        self._diag['stage'] = 'reading'
        while True:
            if b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                if len(line) > self.max_frame_bytes:
                    self._failure('FRAME_CAPACITY')
                    raise ProtocolUnknown("stdio frame exceeds byte limit")
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("duplicate JSON key")
                        result[key] = value
                    return result
                try:
                    message = json.loads(line, object_pairs_hook=unique,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
                except (ValueError, UnicodeError, RecursionError) as exc:
                    self._failure('INVALID_JSON_FRAME')
                    raise ProtocolUnknown("invalid stdio JSON frame") from exc
                if type(message) is not dict:
                    self._failure('INVALID_FRAME_SHAPE')
                    raise ProtocolUnknown("stdio frame must be an object")
                self._diag['frames_received'] += 1
                self._diag['stage'] = 'frame_received'
                self._diag['last_frame_shape'] = frame_shape(message)
                return message
            if len(self._buffer) > self.max_frame_bytes:
                self._failure('FRAME_CAPACITY')
                raise ProtocolUnknown("stdio frame exceeds byte limit")
            remaining = max(0, deadline - time.monotonic())
            ready = self._read_selector.select(remaining)
            if not ready:
                return None
            # Drain stderr before stdout EOF so startup failures retain safe facts.
            for key, _ in ready:
                if key.data == 'stderr': self._stderr()
            for key, _ in ready:
                if key.data != 'stdout': continue
                try:
                    data = os.read(self._proc.stdout.fileno(), min(65536, self.max_frame_bytes + 1))
                except BlockingIOError:
                    continue
                if not data:
                    self._failure('STDOUT_EOF')
                    raise ProtocolUnknown("stdio disconnected before acknowledgement")
                self._diag['stdout_bytes'] += len(data)
                self._buffer += data

    def _event(self, message):
        self._diag['stage'] = 'server_message_validation'
        try: message = _server_message(message)
        except ProtocolError:
            self._failure('SERVER_MESSAGE_ENVELOPE')
            raise
        if self.event_sink is None:
            raise ProtocolUnknown("server event has no configured sink")
        self._diag['stage'] = 'server_event_delivery'
        try: self.event_sink(message)
        except Exception:
            self._failure('SERVER_EVENT_HANDLER')
            raise

    def __call__(self, message, timeout_s):
        self._ready()
        if self._active:
            raise ProtocolError("only one stdio operation may be active")
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ProtocolError("positive timeout required")
        _object(message, {"id", "method", "params"}, {"method"})
        method = message["method"]
        notification = method == "initialized"
        if notification:
            _object(message, {"method"}, {"method"})
        else:
            if type(method) is not str or method not in ALLOWED_METHODS or not _valid_id(message.get("id")):
                raise ProtocolError("method or request id is not exposed")
            _params(method, message.get("params", {}))
        _json_copy(message, self.max_frame_bytes)
        deadline = time.monotonic() + timeout_s
        self._diag['request_count'] += 1
        self._diag['last_method'] = method
        self._active = True
        try:
            self._write(message, deadline)
            if notification:
                return None
            while True:
                if time.monotonic() >= deadline:
                    self._failure('RESPONSE_TIMEOUT')
                    raise ProtocolUnknown("stdio acknowledgement deadline exceeded")
                response = self._read(deadline)
                if response is None:
                    self._failure('RESPONSE_TIMEOUT')
                    raise ProtocolUnknown("stdio acknowledgement deadline exceeded")
                if "method" in response:
                    self._event(response)
                    continue
                self._diag['stage'] = 'response_envelope_validation'
                try: _object(response, {"id", "result", "error", "jsonrpc"}, {"id"})
                except ProtocolError:
                    self._failure('RESPONSE_ENVELOPE')
                    raise
                if (type(response["id"]) is not type(message["id"]) or response["id"] != message["id"]
                        or response.get("jsonrpc", "2.0") != "2.0"
                        or ("result" in response) == ("error" in response)):
                    self._failure('RESPONSE_CORRELATION')
                    raise ProtocolUnknown("unexpected stdio acknowledgement")
                self._diag['stage'] = 'response_received'
                return response
        except Exception as exc:
            if self._diag['failure_code'] is None: self._failure('EXCHANGE_' + self._diag['stage'].upper())
            self._broken = True
            raise ProtocolUnknown("stdio exchange failed; outcome unknown, no retry") from exc
        finally:
            self._active = False

    def poll(self, timeout_s=0):
        """Deliver at most 100 pending events; never answer a server request."""
        self._ready()
        if self._active:
            raise ProtocolError("cannot poll during an exchange")
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s < 0:
            raise ProtocolError("nonnegative poll timeout required")
        deadline = time.monotonic() + timeout_s
        self._active = True
        count = 0
        try:
            while count < 100:
                frame = self._read(deadline)
                if frame is None:
                    return count
                self._event(frame)
                count += 1
                if time.monotonic() >= deadline:
                    return count
            return count
        except Exception as exc:
            if self._diag['failure_code'] is None: self._failure('EVENT_STREAM')
            self._broken = True
            raise ProtocolUnknown("stdio event stream failed; outcome unknown") from exc
        finally:
            self._active = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stderr_tail = b''
        if self._proc is not None:
            self._diag['exit_code_before_close'] = self._proc.poll()
        for selector in (self._read_selector, self._write_selector):
            if selector is not None:
                selector.close()
        if self._proc is None:
            return
        for stream in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if self._proc.poll() is None:
            self._diag['termination_requested'] = True
            self._proc.terminate()
            try:
                self._proc.wait(timeout=self.shutdown_timeout_s)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=self.shutdown_timeout_s)
        else:
            self._proc.wait(timeout=self.shutdown_timeout_s)
        self._diag['reaped'] = self._proc.poll() is not None
