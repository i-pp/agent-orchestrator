"""Small Linux subprocess/JSONL transport. No Codex state database access."""

from collections import deque
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time
from isolation_guard import check_command


class ProtocolError(RuntimeError):
    pass


class RpcError(RuntimeError):
    pass


def redact(value):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if re.fullmatch(
            r"(?i)(access.?token|refresh.?token|id.?token|api.?key|authorization|password|secret|private.?key|email|(?:chatgpt)?account.?id)", k
        ) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
                       "[REDACTED PRIVATE KEY]", value, flags=re.S)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}", "[REDACTED KEY]", value)
        value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED JWT]", value)
        return re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
    return value


def proc_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "ppid": int(fields[1]), "pgrp": int(fields[2]), "state": fields[0], "start": fields[19]}
    except (OSError, ValueError, IndexError):
        return None


class Process:
    """Own one process group; drain both pipes and bound JSONL frame size."""

    def __init__(self, argv, *, cwd, env, log=None, raw_output=False, on_observed=None, context=None, resources=None):
        check_command(argv)
        self.log = log
        self.resources = resources
        self.closing = False
        if resources:
            argv, env = resources.wrap(argv, env)
        self.raw_output = raw_output
        self.stdout_tail = b""
        self.stdout_bytes = 0
        self.on_observed = on_observed
        self.context = context if context is not None else {}
        guarded = [sys.executable, str(Path(__file__).with_name("child_entry.py")), str(os.getpid()), *argv]
        self.proc = subprocess.Popen(guarded, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     start_new_session=True)
        root_identity = proc_identity(self.proc.pid)
        self.root_start = root_identity["start"] if root_identity else None
        self.selector = selectors.DefaultSelector()
        for pipe, name in [(self.proc.stdout, "stdout"), (self.proc.stderr, "stderr")]:
            os.set_blocking(pipe.fileno(), False)
            self.selector.register(pipe, selectors.EVENT_READ, name)
        self.buffer = b""
        self.frames = deque()
        self.stderr_bytes = 0
        self.stderr_tail = b""
        self.seen = {}
        self.seq = 0
        self.notifications = deque()
        self.closed = False
        try:
            self.observe_children()
        except BaseException:
            self.close(grace=0)
            raise

    def record(self, direction, payload):
        if self.log:
            self.log.write(json.dumps({**self.context, "time": time.time(), "direction": direction,
                                       "message": redact(payload)}, ensure_ascii=False) + "\n")
            self.log.flush()
            if direction == "cleanup" or (isinstance(payload, dict) and payload.get("method") in ["turn/start", "turn/completed"]):
                os.fsync(self.log.fileno())

    def observe_children(self, persist=True):
        before = self.seen.copy()
        identities = [x for p in Path("/proc").iterdir() if p.name.isdigit()
                      if (x := proc_identity(int(p.name))) is not None]
        parents = {x["pid"] for x in identities if self.seen.get(x["pid"]) == x["start"]}
        if any(x["pid"] == self.proc.pid and x["start"] == self.root_start for x in identities):
            parents.add(self.proc.pid)
        while True:
            children = {x["pid"] for x in identities if x["ppid"] in parents}
            if children <= parents:
                break
            parents |= children
        for x in identities:
            if x["pid"] in parents:
                self.seen.setdefault(x["pid"], x["start"])
        if persist and self.on_observed and before != self.seen:
            self.on_observed(dict(self.seen))

    def send(self, message):
        if self.resources and not self.closing:
            self.resources.check()
        self.record("send", message)
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        self.proc.stdin.flush()

    def next_frame(self, timeout=0.2):
        deadline = time.monotonic() + timeout
        while True:
            if self.resources and not self.closing:
                self.resources.check()
            if self.frames:
                line = self.frames.popleft()
                try:
                    value = json.loads(line)
                except (UnicodeError, ValueError) as exc:
                    self.record("invalid", {"error": "Invalid JSONL", "bytes": len(line)})
                    raise ProtocolError("Invalid JSONL") from exc
                if not isinstance(value, dict):
                    raise ProtocolError("JSONL frame must be an object")
                self.record("receive", value)
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            for key, _ in self.selector.select(remaining):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    if key.data == "stdout" and self.buffer:
                        # A valid-looking JSON object without its newline may be a torn event.
                        self.record("truncated", {"bytes": len(self.buffer)})
                        self.buffer = b""
                        raise ProtocolError("Truncated JSONL frame at EOF")
                    continue
                if key.data == "stderr":
                    # Hold a bounded diagnostic in memory; redact it before persistence.
                    self.stderr_bytes += len(chunk)
                    self.stderr_tail = (self.stderr_tail + chunk)[-131072:]
                    continue
                if self.raw_output:
                    self.stdout_bytes += len(chunk)
                    self.stdout_tail = (self.stdout_tail + chunk)[-131072:]
                    continue
                self.buffer += chunk
                if len(self.buffer) > 4 * 1024 * 1024:
                    raise ProtocolError("JSONL frame exceeds 4 MiB")
                while b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    if line.strip():
                        self.frames.append(line)
            if not self.selector.get_map() and not self.frames:
                raise EOFError("Process output closed")

    def call(self, method, params, timeout=30):
        self.seq += 1
        request_id = self.seq
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.next_frame(min(0.2, max(0, deadline - time.monotonic())))
            if message is None:
                continue
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    raise RpcError(json.dumps(redact(message["error"]), ensure_ascii=False))
                return message["result"]
            self.notifications.append(message)
        raise TimeoutError(f"RPC timeout: {method}")

    def next_message(self, timeout=0.2):
        return self.notifications.popleft() if self.notifications else self.next_frame(timeout)

    def close(self, grace=2):
        if self.closed:
            return self.cleanup
        self.closing = True
        self.observe_children(persist=False)
        if not self.proc.stdin.closed:
            try:
                self.proc.stdin.close()
            except BrokenPipeError:
                pass
        deadline = time.monotonic() + grace
        while self.proc.poll() is None and time.monotonic() < deadline:
            try:
                self.next_frame(0.05)
            except (EOFError, ProtocolError):
                time.sleep(0.01)
        # Also stop descendants when a parent exited without releasing its pipes.
        for sig in [signal.SIGTERM, signal.SIGKILL]:
            owned_group = any((actual := proc_identity(pid)) and actual["start"] == start
                              and actual["pgrp"] == self.proc.pid for pid, start in self.seen.items())
            if owned_group:
                try:
                    os.killpg(self.proc.pid, sig)
                except ProcessLookupError:
                    pass
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                continue
            if sig == signal.SIGTERM:
                time.sleep(0.05)
        self.proc.wait(timeout=2)
        # Namespace teardown and reparenting are asynchronous. Check owned identities,
        # stop any observed detached descendant, then allow bounded time for exit.
        until = time.monotonic() + 2
        while True:
            survivors = []
            for pid, start in self.seen.items():
                current = proc_identity(pid)
                if current and current["start"] == start and current["state"] not in ["Z", "X"]:
                    survivors.append(pid)
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if not survivors or time.monotonic() >= until:
                break
            time.sleep(0.02)
        self.cleanup = {"exit_code": self.proc.returncode, "stderr_bytes": self.stderr_bytes,
                        "stderr_redacted": redact(self.stderr_tail.decode(errors="replace")),
                        "observed_pids": sorted(self.seen), "survivors": survivors}
        if self.raw_output:
            self.cleanup.update(stdout_bytes=self.stdout_bytes,
                                stdout_redacted=redact(self.stdout_tail.decode(errors="replace")))
        if self.resources:
            self.cleanup["resources"] = self.resources.finish()
        if getattr(self.log, "full", False):
            self.cleanup["log_limit_exceeded"] = True
        self.record("cleanup", self.cleanup)
        self.selector.close()
        self.proc.stdout.close()
        self.proc.stderr.close()
        self.closed = True
        return self.cleanup
