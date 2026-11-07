"""Strict helpers for operator-owned private state files."""

import json
import os
from pathlib import Path
import stat


class PrivateStateError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _private(path, limit=None):
    path = Path(path)
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or (limit is not None and info.st_size > limit)):
        raise PrivateStateError('UNSAFE_PRIVATE_STATE_FILE')


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PrivateStateError('INVALID_PRIVATE_STATE_JSON')
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError):
        raise PrivateStateError('INVALID_PRIVATE_STATE_JSON') from None
