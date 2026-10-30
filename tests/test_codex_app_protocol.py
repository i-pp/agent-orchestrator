"""Fake-peer tests only: no server, model, auth, process or network startup."""
import hashlib
import json
import os
import subprocess
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from codex_app_protocol import (ALLOWED_METHODS, METHOD_SCHEMAS, SCHEMA_SHA256,
    SCHEMA_VERSION, ProtocolClient, ProtocolError, ProtocolUnknown, RpcError, StdioTransport, schema_version_supported)

INITIALIZE = {"codexHome": "/offline/codex", "platformFamily": "unix",
              "platformOs": "linux", "userAgent": "codex/0.165.0"}
MODEL = {"id": "offline-model", "model": "offline-model", "displayName": "Fixture",
         "description": "Not a live model", "hidden": False, "isDefault": True,
         "defaultReasoningEffort": "medium", "supportedReasoningEfforts": []}
THREAD = {"id": "thread-1", "cliVersion": "0.165.0", "createdAt": 0, "updatedAt": 0,
          "cwd": "/offline", "ephemeral": False, "modelProvider": "openai", "preview": "",
          "projectId": None, "sessionId": "session-1", "source": "appServer",
          "status": {"type": "idle"}, "turns": []}
TURN = {"id": "turn-1", "items": [], "status": "inProgress"}
THREAD_RESULT = {"thread": THREAD, "approvalPolicy": "on-request", "approvalsReviewer": "user",
                 "cwd": "/offline", "model": "offline-model", "modelProvider": "openai",
                 "sandbox": {"type": "readOnly"}}
RESULTS = {"initialize": INITIALIZE, "model/list": {"data": [MODEL], "nextCursor": None},
           "account/read": {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True},
           "thread/start": THREAD_RESULT, "thread/resume": THREAD_RESULT,
           "thread/read": {"thread": THREAD}, "thread/list": {"data": [THREAD]},
           "thread/archive": {}, "turn/start": {"turn": TURN},
           "turn/steer": {"turnId": "turn-1"}, "turn/interrupt": {}}


class Peer:
    def __init__(self):
        self.calls = []
        self.answer = None

    def __call__(self, message, timeout_s):
        self.calls.append((message, timeout_s))
        if message["method"] == "initialized":
            return None
        if self.answer:
            return self.answer(message)
        return {"id": message["id"], "result": RESULTS[message["method"]]}


class ProtocolTests(unittest.TestCase):
    def test_only_inspected_schema_versions_are_accepted(self):
        for agent in ['codex/0.165.0', 'codex_cli_rs/0.164.0-alpha.11.1 (Linux)']:
            self.assertTrue(schema_version_supported(agent))
        for agent in ['codex/0.165.01', 'codex/0.165.0-alpha.2', 'codex/0.164.0-alpha.11.11', 'unknown', None]:
            self.assertFalse(schema_version_supported(agent))
    def client(self, **kwargs):
        peer = Peer()
        client = ProtocolClient(peer, **kwargs)
        client.initialize()
        return client, peer

    def test_default_transport_does_not_exist(self):
        with self.assertRaises(ProtocolError):
            ProtocolClient().initialize()

    def test_handshake_and_raw_catalog_page(self):
        client, peer = self.client(timeout_s=7)
        self.assertEqual(peer.calls, [({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "chat-codex-pipeline", "version": "1"}}}, 7),
            ({"method": "initialized"}, 7)])
        self.assertEqual(client.model_catalog(limit=5), RESULTS["model/list"])
        self.assertEqual(peer.calls[-1][0], {"id": 2, "method": "model/list", "params": {"limit": 5}})
        with self.assertRaises(ProtocolError):
            client.initialize()

    def test_not_initialized_rejects_without_exchange(self):
        peer = Peer()
        with self.assertRaises(ProtocolError):
            ProtocolClient(peer).request("thread/start", {})
        self.assertEqual(peer.calls, [])

    def test_all_allowed_operations_use_schema_field_names(self):
        client, peer = self.client()
        text = [{"type": "text", "text": "offline task", "text_elements": []}]
        params = {"model/list": {}, "account/read": {"refreshToken": False}, "thread/start": {"model": "offline-model"},
            "thread/resume": {"threadId": "thread-1", "excludeTurns": True},
            "thread/read": {"threadId": "thread-1", "includeTurns": True}, "thread/list": {},
            "thread/archive": {"threadId": "thread-1"},
            "turn/start": {"threadId": "thread-1", "input": text},
            "turn/steer": {"threadId": "thread-1", "expectedTurnId": "turn-1", "input": text},
            "turn/interrupt": {"threadId": "thread-1", "turnId": "turn-1"}}
        for method, value in params.items():
            with self.subTest(method=method):
                self.assertEqual(client.request(method, value), RESULTS[method])
                self.assertEqual(peer.calls[-1][0]["params"], value)
        self.assertEqual(set(params) | {"initialize"}, ALLOWED_METHODS)

    def test_account_read_never_refreshes_or_returns_identity(self):
        client, peer = self.client()
        for account in [{"type": "chatgpt", "email": "fixture@example.invalid", "planType": "plus",
                         "accessToken": "fixture-private"}, {"type": "apiKey"}, None]:
            peer.answer = lambda message: {"id": message["id"], "result": {
                "account": account, "requiresOpenaiAuth": True, "futureSecret": "fixture-private"}}
            result = client.request("account/read")
            self.assertEqual(result, {"account": None if account is None else {"type": account["type"]},
                                      "requiresOpenaiAuth": True})
            self.assertEqual(peer.calls[-1][0]["params"], {"refreshToken": False})
            self.assertNotIn("fixture-private", json.dumps(result))
        count = len(peer.calls)
        for params in [{"refreshToken": True}, {"refreshToken": 0}, {"refreshToken": False, "token": "x"}]:
            with self.assertRaises(ProtocolError):
                client.request("account/read", params)
        self.assertEqual(len(peer.calls), count)

    def test_unsafe_and_unknown_methods_never_reach_peer(self):
        client, peer = self.client()
        before = len(peer.calls)
        for method in ["initialize", "initialized", "command/exec", "process/spawn", "exec",
            "account/login/start", "account/chatgptAuthTokens/refresh", "config/value/write",
            "thread/settings/update", "anything/new", None]:
            with self.subTest(method=method), self.assertRaises(ProtocolError):
                client.request(method, {})
        self.assertEqual(len(peer.calls), before)

    def test_missing_unknown_and_unsafe_params_never_reach_peer(self):
        client, peer = self.client()
        bad = [("thread/read", {}), ("turn/steer", {"threadId": "t", "input": []}),
            ("turn/interrupt", {"threadId": "t"}), ("model/list", {"limit": True}),
            ("model/list", {"limit": -1}), ("thread/read", {"threadId": "t", "includeTurns": 1}),
            ("thread/start", {"config": {}}), ("thread/start", {"modelProvider": "api-route"}),
            ("thread/start", {"approvalPolicy": "never"}),
            ("thread/start", {"approvalsReviewer": "auto_review"}),
            ("thread/start", {"sandbox": "danger-full-access"}),
            ("turn/start", {"threadId": "t", "input": [{"type": "localImage", "path": "/x"}]}),
            ("turn/start", {"threadId": "t", "input": [{"type": "text", "text": "x", "command": "x"}]}),
            ("turn/start", {"threadId": "t", "input": [{"type": "text", "text": "x"}],
                            "sandboxPolicy": {"type": "dangerFullAccess"}})]
        before = len(peer.calls)
        for method, value in bad:
            with self.subTest(method=method, value=value), self.assertRaises(ProtocolError):
                client.request(method, value)
        self.assertEqual(len(peer.calls), before)

    def test_payload_utf8_byte_limit_prevents_dispatch(self):
        client, peer = self.client(max_payload_bytes=200)
        count = len(peer.calls)
        with self.assertRaises(ProtocolError) as caught:
            client.request("thread/start", {"developerInstructions": "字" * 100})
        self.assertEqual(caught.exception.status, "REJECTED")
        self.assertEqual(len(peer.calls), count)

    def test_no_automatic_retry_after_timeout_or_lost_ack(self):
        for failure in [TimeoutError("deadline"), ConnectionError("lost ACK")]:
            client, peer = self.client()
            def fail(message):
                raise failure
            peer.answer = fail
            before = len(peer.calls)
            with self.assertRaises(ProtocolUnknown) as caught:
                client.request("thread/archive", {"threadId": "thread-1"})
            self.assertEqual(caught.exception.status, "UNKNOWN")
            self.assertEqual(len(peer.calls), before + 1)

    def test_host_deadline_violation_is_unknown(self):
        client, peer = self.client(timeout_s=1)
        with patch("codex_app_protocol.time.monotonic", side_effect=[0, 2]):
            with self.assertRaises(ProtocolUnknown):
                client.request("thread/archive", {"threadId": "t"})
        self.assertEqual(len(peer.calls), 3)

    def test_invalid_acknowledgements_have_unknown_outcome(self):
        invalid = [None, [], {"id": 2}, {"id": True, "result": {}},
            {"id": "2", "result": {}}, {"id": 9, "result": {}},
            {"id": 2, "result": {}, "error": {"code": -1, "message": "x"}},
            {"id": 2, "result": {}, "jsonrpc": "1.0"},
            {"id": 2, "result": {}, "method": "unexpected"},
            {"id": 2, "error": {"code": True, "message": "x"}},
            {"id": 2, "result": {"data": float("nan")}}]
        for response in invalid:
            client, peer = self.client()
            peer.answer = lambda message: response
            with self.subTest(response=response), self.assertRaises(ProtocolUnknown):
                client.model_catalog()
            self.assertEqual(len(peer.calls), 3)

    def test_oversize_response_and_bad_result_shape_are_unknown(self):
        client, peer = self.client(max_response_bytes=300)
        for response in [{"data": ["x" * 301]}, {}, {"data": {}, "nextCursor": None},
                         {"data": ["invalid model"]}, {"data": [{"id": "missing fields"}]},
                         {"data": [], "nextCursor": 123}]:
            peer.answer = lambda message: {"id": message["id"], "result": response}
            with self.subTest(response=response), self.assertRaises(ProtocolUnknown):
                client.model_catalog()

    def test_explicit_rpc_error_is_failed_and_not_logged_in_exception(self):
        client, peer = self.client()
        peer.answer = lambda m: {"id": m["id"], "error": {"code": -32000, "message": "private detail"}}
        with self.assertRaises(RpcError) as caught:
            client.request("thread/archive", {"threadId": "t"})
        self.assertEqual(caught.exception.status, "FAILED")
        self.assertEqual(caught.exception.server_message, "private detail")
        self.assertNotIn("private detail", str(caught.exception))
        self.assertEqual(len(peer.calls), 3)

    def test_partial_initialize_is_not_retried_or_marked_initialized(self):
        peer = Peer()
        original = peer.__call__
        def exchange(message, timeout):
            if message["method"] == "initialized":
                raise ConnectionError("notification lost")
            return original(message, timeout)
        client = ProtocolClient(exchange)
        with self.assertRaises(ProtocolUnknown):
            client.initialize()
        self.assertFalse(client.initialized)
        with self.assertRaises(ProtocolError):
            client.initialize()
        self.assertEqual(len(peer.calls), 1)

    def test_events_unknown_notifications_and_approvals_only_forward(self):
        events = []
        client, peer = self.client(event_sink=events.append)
        frames = [{"method": "turn/completed", "params": {"threadId": "t", "turn": TURN}},
            {"method": "future/notification", "params": {"opaque": True}},
            {"id": "approval-1", "method": "item/commandExecution/requestApproval", "params": {}},
            {"id": 5, "method": "future/request", "params": {}}]
        before = len(peer.calls)
        for frame in frames:
            client.receive(frame)
        self.assertEqual([e["known"] for e in events], [True, False, True, False])
        self.assertEqual([e["approval_required"] for e in events], [False, False, True, False])
        self.assertEqual([e["message"] for e in events], frames)
        self.assertEqual(len(peer.calls), before)
        self.assertFalse(hasattr(client, "approve"))
        self.assertFalse(hasattr(client, "send"))

    def test_events_are_size_checked_and_do_not_alias_source(self):
        events = []
        client, peer = self.client(event_sink=events.append)
        frame = {"method": "turn/started", "params": {"threadId": "t"}}
        client.receive(frame)
        frame["params"]["threadId"] = "changed"
        self.assertEqual(events[0]["message"]["params"]["threadId"], "t")
        for frame in [{"method": ""}, {"method": "x", "id": True}, {"result": {}},
                      {"method": "x", "jsonrpc": "bad"}, {"method": "x", "params": "x" * 1048576}]:
            with self.subTest(frame_type=type(frame)), self.assertRaises(ProtocolError):
                client.receive(frame)
        self.assertEqual(len(events), 1)

    def test_notifications_may_arrive_during_synchronous_exchange(self):
        events = []
        client, peer = self.client(event_sink=events.append)
        def exchange(message):
            client.receive({"method": "turn/started", "params": {"threadId": "t", "turn": TURN}})
            client.receive({"method": "item/fileChange/requestApproval", "id": "server-2", "params": {}})
            return {"id": message["id"], "result": {"turn": TURN}}
        peer.answer = exchange
        result = client.request("turn/start", {"threadId": "t", "input": [{"type": "text", "text": "x"}]})
        self.assertEqual(result, {"turn": TURN})
        self.assertEqual(len(events), 2)
        self.assertTrue(events[1]["approval_required"])
        self.assertEqual(len(peer.calls), 3)

    def test_reentrant_requests_are_rejected_without_second_send(self):
        client, peer = self.client()
        def exchange(message):
            with self.assertRaises(ProtocolError):
                client.model_catalog()
            return {"id": message["id"], "result": {"data": []}}
        peer.answer = exchange
        client.model_catalog()
        self.assertEqual(len(peer.calls), 3)

    def test_constructor_rejects_unbounded_or_invalid_configuration(self):
        for kwargs in [{"timeout_s": 0}, {"timeout_s": float("inf")}, {"timeout_s": True},
            {"max_payload_bytes": -1}, {"max_response_bytes": True}, {"exchange": 1}, {"event_sink": 1}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ProtocolError):
                ProtocolClient(**kwargs)

    def test_generated_schema_provenance_when_available(self):
        schema = PROJECT.parent / "schema"
        if not schema.is_dir():
            self.skipTest("generated schema inspection artifacts not present; no live connection claimed")
        self.assertEqual(SCHEMA_VERSION, "codex-cli 0.165.0")
        for name, digest in SCHEMA_SHA256.items():
            self.assertEqual(hashlib.sha256((schema / name).read_bytes()).hexdigest(), digest, name)
        requests = json.loads((schema / "ClientRequest.json").read_text())
        supported = {v["properties"]["method"]["enum"][0]: v for v in requests["oneOf"]}
        for method, (params, result, required) in METHOD_SCHEMAS.items():
            self.assertIn(method, supported)
            self.assertEqual(set(json.loads((schema / params).read_text()).get("required", [])), set(required))
            self.assertTrue((schema / result).is_file())
        steer = json.loads((schema / "v2/TurnSteerParams.json").read_text())
        self.assertIn("expectedTurnId", steer["required"])
        read = json.loads((schema / "v2/ThreadReadParams.json").read_text())
        self.assertIn("includeTurns", read["properties"])
        self.assertNotIn("includeTurns", read["required"])


class PipeProcess:
    """Real in-memory OS pipes, fake Popen lifecycle: never launches a process."""
    def __init__(self, *, force_kill=False):
        self.input_read, input_write = os.pipe()
        output_read, self.output_write = os.pipe()
        stderr_read, self.stderr_write = os.pipe()
        self.stdin = os.fdopen(input_write, "wb", buffering=0)
        self.stdout = os.fdopen(output_read, "rb", buffering=0)
        self.stderr = os.fdopen(stderr_read, "rb", buffering=0)
        os.set_blocking(self.input_read, False)
        self.status = None
        self.force_kill = force_kill
        self.actions = []

    def emit(self, value):
        os.write(self.output_write, (json.dumps(value) + "\n").encode())

    def sent(self):
        try:
            return [json.loads(line) for line in os.read(self.input_read, 65536).splitlines()]
        except BlockingIOError:
            return []

    def poll(self):
        return self.status

    def terminate(self):
        self.actions.append("terminate")
        if not self.force_kill:
            self.status = -15

    def kill(self):
        self.actions.append("kill")
        self.status = -9

    def wait(self, timeout):
        self.actions.append(("wait", timeout))
        if self.status is None:
            raise subprocess.TimeoutExpired("fixture", timeout)
        return self.status

    def cleanup(self):
        self.stdin.close()
        self.stdout.close()
        self.stderr.close()
        for fd in (self.input_read, self.output_write, self.stderr_write):
            if fd is not None:
                os.close(fd)
        self.output_write = self.input_read = self.stderr_write = None


class StdioTests(unittest.TestCase):
    def transport(self, *, force_kill=False, **kwargs):
        process = PipeProcess(force_kill=force_kill)
        self.addCleanup(process.cleanup)
        transport = StdioTransport(**kwargs)
        self.addCleanup(transport.close)
        with patch("codex_app_protocol.subprocess.Popen", return_value=process) as popen:
            transport.start()
        self.assertEqual(popen.call_args.args, (["codex", "app-server", "--listen", "stdio://"],))
        self.assertEqual(popen.call_args.kwargs, {"shell": False, "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "bufsize": 0, "close_fds": True})
        return transport, process

    def test_constructing_client_and_transport_does_not_spawn(self):
        with patch("codex_app_protocol.subprocess.Popen") as popen:
            transport = StdioTransport()
            ProtocolClient(transport)
            with self.assertRaises(ProtocolError):
                transport({"id": 1, "method": "model/list", "params": {}}, 1)
            transport.close()
            popen.assert_not_called()

    def test_client_handshake_and_events_on_fake_stdio(self):
        process = PipeProcess()
        self.addCleanup(process.cleanup)
        events = []
        transport = StdioTransport()
        self.addCleanup(transport.close)
        client = ProtocolClient(transport, events.append)
        transport.bind_client(client)
        with patch("codex_app_protocol.subprocess.Popen", return_value=process):
            transport.start()
        process.emit({"id": 1, "result": INITIALIZE})
        self.assertEqual(client.initialize(), INITIALIZE)
        process.emit({"method": "future/notification", "params": {"x": 1}})
        process.emit({"id": "approval", "method": "item/permissions/requestApproval", "params": {}})
        process.emit({"id": 2, "result": {"data": [MODEL]}})
        self.assertEqual(client.model_catalog(), {"data": [MODEL]})
        sent = process.sent()
        self.assertEqual([m["method"] for m in sent], ["initialize", "initialized", "model/list"])
        self.assertEqual(len(events), 2)
        self.assertFalse(events[0]["known"])
        self.assertTrue(events[1]["approval_required"])
        self.assertFalse(any("result" in message for message in sent))

    def test_poll_forwards_events_without_writing(self):
        events = []
        transport, process = self.transport(event_sink=events.append)
        process.emit({"method": "turn/completed", "params": {"threadId": "t", "turn": TURN}})
        self.assertEqual(transport.poll(), 1)
        self.assertEqual(events[0]["method"], "turn/completed")
        self.assertEqual(process.sent(), [])
        self.assertEqual(transport.poll(), 0)

    def test_timeout_poisoned_connection_has_no_replay(self):
        transport, process = self.transport()
        request = {"id": 1, "method": "thread/archive", "params": {"threadId": "t"}}
        with self.assertRaises(ProtocolUnknown):
            transport(request, 0.001)
        self.assertEqual(process.sent(), [request])
        with self.assertRaises(ProtocolError):
            transport(request, 0.001)
        self.assertEqual(process.sent(), [])

    def test_disconnect_is_unknown(self):
        transport, process = self.transport()
        os.close(process.output_write)
        process.output_write = None
        with self.assertRaises(ProtocolUnknown):
            transport({"id": 1, "method": "model/list", "params": {}}, 0.1)

    def test_invalid_and_uncorrelated_frames_are_unknown(self):
        for frame in [b"{bad}\n", b'{"id":1,"id":1,"result":{}}\n',
                      b'{"id":1,"result":NaN}\n', b"[]\n",
                      b'{"id":2,"result":{}}\n', b'{"id":1,"result":{},"error":{}}\n']:
            with self.subTest(frame=frame):
                transport, process = self.transport()
                os.write(process.output_write, frame)
                with self.assertRaises(ProtocolUnknown):
                    transport({"id": 1, "method": "model/list", "params": {}}, 0.1)
                transport.close()

    def test_oversize_or_incomplete_frame_is_unknown(self):
        for frame in [b"x" * 201, b"x" * 201 + b"\n", b'{"id":1']:
            with self.subTest(length=len(frame)):
                transport, process = self.transport(max_frame_bytes=200)
                os.write(process.output_write, frame)
                with self.assertRaises(ProtocolUnknown):
                    transport({"id": 1, "method": "model/list", "params": {}}, 0.001)
                transport.close()

    def test_direct_transport_cannot_send_arbitrary_rpc_or_auth_refresh(self):
        transport, process = self.transport()
        for message in [{"id": 1, "method": "command/exec", "params": {}},
                        {"id": 1, "method": "account/read", "params": {"refreshToken": True}},
                        {"method": "initialized", "result": "approval"},
                        {"id": True, "method": "model/list", "params": {}}]:
            with self.assertRaises(ProtocolError):
                transport(message, 0.1)
        self.assertEqual(process.sent(), [])

    def test_missing_event_sink_does_not_silently_drop_server_request(self):
        transport, process = self.transport()
        process.emit({"method": "item/fileChange/requestApproval", "id": "a", "params": {}})
        with self.assertRaises(ProtocolUnknown):
            transport({"id": 1, "method": "model/list", "params": {}}, 0.1)
        self.assertEqual(len(process.sent()), 1)

    def test_close_reaps_child_and_escalates_to_kill_once(self):
        transport, process = self.transport(force_kill=True, shutdown_timeout_s=0.1)
        transport.close()
        self.assertEqual(process.actions, ["terminate", ("wait", 0.1), "kill", ("wait", 0.1)])
        transport.close()
        self.assertEqual(len(process.actions), 4)
        with self.assertRaises(ProtocolError):
            transport.start()

    def test_normal_close_and_start_failure_are_bounded(self):
        transport, process = self.transport()
        transport.close()
        self.assertEqual(process.actions, ["terminate", ("wait", 2)])
        other = StdioTransport()
        with patch("codex_app_protocol.subprocess.Popen", side_effect=OSError("offline fixture")):
            with self.assertRaises(ProtocolError):
                other.start()
        other.close()

    def test_unsolicited_response_during_poll_is_unknown(self):
        transport, process = self.transport(event_sink=lambda message: None)
        process.emit({"id": 1, "result": {}})
        with self.assertRaises(ProtocolUnknown):
            transport.poll()

    def test_startup_eof_retains_only_safe_stderr_and_exit_facts(self):
        transport, process = self.transport()
        os.write(process.stderr_write, b'WARNING: read-only file system; private-canary-value\n')
        os.close(process.output_write)
        process.output_write = None
        process.status = 23
        client = ProtocolClient(transport)
        with self.assertRaises(ProtocolUnknown): client.initialize()
        transport.close()
        facts = transport.diagnostics()
        self.assertEqual(facts['failure_code'], 'STDOUT_EOF')
        self.assertIn('READ_ONLY_FILESYSTEM', facts['stderr_categories'])
        self.assertEqual(facts['exit_code_before_close'], 23)
        self.assertTrue(facts['reaped'])
        self.assertFalse(facts['termination_requested'])
        self.assertFalse(client.diagnostics()['initialize_acknowledged'])
        self.assertNotIn('private-canary-value', json.dumps(facts))
        self.assertEqual([m['method'] for m in process.sent()], ['initialize'])

    def test_fragmented_utf8_response_partial_writes_and_stderr(self):
        transport, process = self.transport()
        result = {**INITIALIZE, 'codexHome': '/offline/例'}
        os.write(process.output_write, (json.dumps({'id': 1, 'result': result}, ensure_ascii=False) + '\r\n').encode())
        os.write(process.stderr_write, b'nonsecret warning\n')
        read, write = os.read, os.write
        def fragmented_read(fd, count): return read(fd, min(count, 2))
        def partial_write(fd, data): return write(fd, data[:5])
        client = ProtocolClient(transport)
        with patch('codex_app_protocol.os.read', side_effect=fragmented_read), patch('codex_app_protocol.os.write', side_effect=partial_write):
            self.assertEqual(client.initialize(), result)
        self.assertTrue(client.diagnostics()['initialize_acknowledged'])
        self.assertTrue(client.diagnostics()['initialized_notification_written'])
        self.assertEqual([m['method'] for m in process.sent()], ['initialize', 'initialized'])

    def test_valid_envelope_invalid_init_result_is_distinct_from_transport_eof(self):
        transport, process = self.transport()
        process.emit({'id': 1, 'result': {'userAgent': 'codex/0.165.0'}})
        client = ProtocolClient(transport)
        with self.assertRaises(ProtocolUnknown): client.initialize()
        self.assertEqual(client.diagnostics()['failure_code'], 'INVALID_RESULT_SCHEMA')
        self.assertEqual(transport.diagnostics()['stage'], 'response_received')
        self.assertEqual(transport.diagnostics()['frames_received'], 1)
        self.assertFalse(client.diagnostics()['initialize_acknowledged'])
        with self.assertRaises(ProtocolError): client.initialize()
        self.assertEqual([m['method'] for m in process.sent()], ['initialize'])

    def test_lsp_content_length_is_rejected_without_automatic_protocol_fallback(self):
        transport, process = self.transport()
        os.write(process.output_write, b'Content-Length: 2\r\n\r\n{}')
        client = ProtocolClient(transport)
        with self.assertRaises(ProtocolUnknown): client.initialize()
        self.assertEqual(transport.diagnostics()['failure_code'], 'INVALID_JSON_FRAME')
        self.assertEqual([m['method'] for m in process.sent()], ['initialize'])

    def test_stderr_capacity_stops_instead_of_unbounded_logging(self):
        transport, process = self.transport()
        transport._diag['stderr_bytes'] = 65530
        os.write(process.stderr_write, b'harmless fixture diagnostic\n')
        client = ProtocolClient(transport)
        with self.assertRaises(ProtocolUnknown): client.initialize()
        self.assertEqual(transport.diagnostics()['failure_code'], 'STDERR_CAPACITY')
        self.assertEqual(process.sent(), [])  # Refused while draining, before stdin delivery.

    def test_full_stderr_and_large_stdin_do_not_deadlock_write_phase(self):
        transport, process = self.transport()
        client = ProtocolClient(transport, timeout_s=1)
        process.emit({'id': 1, 'result': INITIALIZE})
        client.initialize()
        process.sent()  # Remove only the already confirmed handshake.
        errors, received = [], []
        os.set_blocking(process.input_read, True)
        def peer():
            try:
                warning = b'harmless warning\n' * 2048
                offset = 0
                while offset < len(warning): offset += os.write(process.stderr_write, warning[offset:])
                payload = b''
                while b'\n' not in payload: payload += os.read(process.input_read, 4096)
                request = json.loads(payload.split(b'\n', 1)[0])
                received.append(request['method'])
                process.emit({'id': request['id'], 'result': THREAD_RESULT})
            except (OSError, ValueError) as error:
                errors.append(type(error).__name__)
        worker = threading.Thread(target=peer, daemon=True)
        worker.start()
        try:
            result = client.request('thread/start', {'developerInstructions': 'x' * 40000})
            self.assertEqual(result['thread']['id'], 'thread-1')
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
            self.assertEqual(received, ['thread/start'])
            self.assertGreater(transport.diagnostics()['stderr_bytes'], 8192)
        finally:
            transport.close()


if __name__ == "__main__":
    unittest.main()
