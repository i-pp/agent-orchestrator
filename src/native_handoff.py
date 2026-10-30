"""Private, immutable task/call artifacts. No tool, model, URL or shell executor."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from chat_mailbox import _json, _private
from native_team_artifacts import _no_symlinks
from storage import runtime_lock

MAX_BYTES = 65536
MAX_PER_KIND = 128
KINDS = {'task', 'request', 'started', 'result'}


def encoded(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > MAX_BYTES:
        raise ValueError('HANDOFF_SIZE_LIMIT')
    return raw


class HandoffStore:
    def __init__(self, run):
        self.root = _no_symlinks(Path(run) / 'handoff-artifacts')
        self.root.mkdir(mode=0o700, exist_ok=True)
        info = self.root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('PRIVATE_HANDOFF_DIRECTORY_REQUIRED')

    def _path(self, kind, key):
        if kind not in KINDS or not isinstance(key, str) or not re.fullmatch(r'[a-f0-9-]{32,64}', key):
            raise ValueError('INVALID_HANDOFF_REFERENCE')
        return self.root / (key + '.' + kind + '.json')

    def put(self, kind, key, value, *, exclusive=False):
        with runtime_lock(self.root):
            return self._put(kind, key, value, exclusive=exclusive)

    def _put(self, kind, key, value, *, exclusive=False):
        path = self._path(kind, key)
        raw = encoded(value)
        digest = hashlib.sha256(raw).hexdigest()
        reference = {'kind': kind, 'key': key, 'sha256': digest}
        if path.exists() or path.is_symlink():
            if exclusive:
                raise ValueError('NATIVE_CALL_ALREADY_STARTED_RECONCILE')
            self.read(reference)
            return reference
        if len(list(self.root.glob('*.' + kind + '.json'))) >= MAX_PER_KIND:
            raise ValueError('HANDOFF_COUNT_LIMIT')
        temp = self.root / (uuid.uuid4().hex + '.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp, path)  # No replacement of a concurrent writer.
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temp.unlink(missing_ok=True)
        return reference

    def read(self, reference):
        if not isinstance(reference, dict) or set(reference) != {'kind', 'key', 'sha256'}:
            raise ValueError('INVALID_HANDOFF_REFERENCE')
        digest = reference['sha256']
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('INVALID_HANDOFF_HASH')
        path = self._path(reference['kind'], reference['key'])
        _private(path, MAX_BYTES)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('HANDOFF_CONTENT_MISMATCH')
        return _json(raw)
