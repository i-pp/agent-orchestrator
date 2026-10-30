"""Loopback-only synthetic control plane. Never logs auth headers or key data."""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import queue
import threading
import time
import uuid


class ControlPlane:
    def __init__(self):
        self.commands = queue.Queue()
        self.responses = {}
        self.condition = threading.Condition()
        self.disconnected = False
        self.polls = 0
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def send(self, status, value=None):
                body = b'' if value is None else json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try: self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError): pass
            def do_GET(self):
                if parent.disconnected:
                    self.send(503, {}); return
                if '/poll' in self.path:
                    parent.polls += 1
                    try: item = parent.commands.get(timeout=.1)
                    except queue.Empty: self.send(204); return
                    self.send(200, {'commands': [item]})
                else:
                    self.send(200, {'id': 'tunnel_22222222222222222222222222222222', 'name': 'isolated-fixture'})
            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                if length > 1024**2: self.send(413); return
                body = self.rfile.read(length)
                if self.path.endswith('/response'):
                    value = json.loads(body)
                    with parent.condition:
                        parent.responses[value['request_id']] = value
                        parent.condition.notify_all()
                self.send(200, {})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def call(self, message, timeout=8):
        identifier = str(uuid.uuid4())
        self.commands.put({'request_id': identifier, 'shard_token': 'fixture-routing-token',
            'command_type': 'jsonrpc', 'channel': 'main', 'created_at': datetime.now(timezone.utc).isoformat(),
            'headers': {}, 'jsonrpc': message})
        deadline = time.monotonic() + timeout
        with self.condition:
            while identifier not in self.responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise TimeoutError('Fixture MCP response deadline')
                self.condition.wait(remaining)
            return self.responses.pop(identifier)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)
