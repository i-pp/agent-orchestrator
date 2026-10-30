"""Small Linux Unix-domain JSONL control protocol, authenticated by peer UID."""

import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time
import uuid

MAX_REQUEST = 131072
MAX_RESPONSE = 4 * 1024**2


def endpoint(root, create=False):
    base = Path('/run/user') / str(os.getuid())
    for parent in [base, *base.parents]:
        if parent.is_symlink(): raise ValueError("Unsafe runtime directory ancestry")
    info = base.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("User runtime directory must be owned by this UID with mode 0700")
    directory = base / "agent-orchestrator"
    if create: directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink(): raise ValueError("Socket directory must not be a symlink")
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Socket directory must be private (0700)")
    name = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:24]
    return directory / (name + ".sock")


def peer_uid(connection):
    return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1]


def validate_socket(path):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("Control socket must be owned by this UID with mode 0600")
    return info


def request(root, method, params=None, timeout=10):
    path = endpoint(root)
    validate_socket(path)
    identifier = str(uuid.uuid4())
    message = json.dumps({"id": identifier, "method": method, "params": params or {}}, ensure_ascii=False).encode() + b"\n"
    if len(message) > MAX_REQUEST: raise ValueError("Control request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        deadline = time.monotonic() + timeout
        while True:
            try:
                connection.connect(str(path)); break
            except BlockingIOError:
                # AF_UNIX backlog pressure occurs before any request bytes;
                # waiting here cannot duplicate a task submission.
                if time.monotonic() >= deadline: raise TimeoutError("Control socket backlog remained full")
                time.sleep(.02)
        if peer_uid(connection) != os.getuid(): raise ValueError("Unexpected daemon peer UID")
        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0: raise TimeoutError("Control request deadline exceeded")
            connection.settimeout(seconds)
        remaining()
        connection.sendall(message)
        buffer = bytearray()
        while b"\n" not in buffer:
            remaining()
            data = connection.recv(65536)
            if not data: raise EOFError("Daemon connection closed before a complete response; do not blindly resubmit")
            buffer.extend(data)
            if len(buffer) > MAX_RESPONSE: raise ValueError("Control response exceeds limit")
        response = json.loads(buffer.split(b"\n", 1)[0])
        if response.get("id") != identifier: raise ValueError("Control response ID mismatch")
        if "error" in response: raise RuntimeError(response["error"])
        return response["result"]
