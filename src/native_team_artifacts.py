"""Explicit artifact adoption for the single integration coordinator.

The caller must serialize integration. This is filesystem validation, not a
security boundary against an actor concurrently changing directories.
"""
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat


def _paths(allowed):
    if not isinstance(allowed, list) or not allowed:
        raise ValueError('allowed must be a nonempty list')
    result = []
    for name in allowed:
        if (not isinstance(name, str) or not name or '\x00' in name
                or '\\' in name or name.startswith('/')
                or any(p in ('', '.', '..') for p in name.split('/'))):
            raise ValueError('ordinary relative paths required')
        result.append(name)
    if len(result) != len(set(result)):
        raise ValueError('duplicate artifact path')
    return result


def _no_symlinks(path):
    """Check existing ancestors without resolving away a symlink."""
    path = Path(os.path.abspath(path))
    for current in [*reversed(path.parents), path]:
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError('symlink path rejected')
        if current != path and not stat.S_ISDIR(mode):
            raise ValueError('nondirectory ancestor')
    return path


def _read(root, allowed):
    root = _no_symlinks(root)
    if not root.is_dir():
        raise ValueError('root must be an existing directory')
    result = {}
    for name in _paths(allowed):
        path = _no_symlinks(root / name)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('regular files required')
        result[name] = path.read_bytes()
    return result


def _hashes(contents):
    return {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}


def fingerprint(root, allowed):
    """Return SHA-256 only for the explicit regular-file allowlist."""
    return _hashes(_read(root, allowed))


def integrate(sources, destination):
    """Validate every source and destination before copying new files.

    Source contents are frozen during validation and written from those bytes;
    later source changes cannot silently enter the integrated result.
    """
    if not isinstance(sources, list) or not sources:
        raise ValueError('sources must be a nonempty list')
    contents = {}
    for source in sources:
        if not isinstance(source, dict) or set(source) != {'root', 'allowed', 'expected'}:
            raise ValueError('invalid source specification')
        data = _read(source['root'], source['allowed'])
        if _hashes(data) != source['expected']:
            raise ValueError('source fingerprint differs from expected')
        if set(data) & set(contents):
            raise ValueError('duplicate output path')
        contents.update(data)
    names = set(contents)
    for name in names:
        if any(str(p) in names for p in PurePosixPath(name).parents if str(p) != '.'):
            raise ValueError('file/directory output collision')
    destination = _no_symlinks(destination)
    if destination.exists() and not destination.is_dir():
        raise ValueError('destination must be a directory')
    for name in names:
        target = _no_symlinks(destination / name)
        if target.exists():
            raise ValueError('existing destination artifact')

    created_files, created_dirs = [], []

    def mkdirs(path):
        missing = []
        while not path.exists():
            missing.append(path)
            path = path.parent
        for directory in reversed(missing):
            directory.mkdir()
            created_dirs.append(directory)

    try:
        for name, data in contents.items():
            target = destination / name
            mkdirs(target.parent)
            with target.open('xb') as stream:
                created_files.append(target)
                stream.write(data)
        actual = fingerprint(destination, list(contents))
        if actual != _hashes(contents):
            raise ValueError('copied artifact fingerprint mismatch')
        return actual
    except Exception:
        for path in reversed(created_files):
            path.unlink()
        for path in reversed(created_dirs):
            path.rmdir()
        raise


def verify_manifest(root, expected):
    """Return False for invalid manifests, absent files, or changed content."""
    try:
        if not isinstance(expected, dict) or not expected:
            return False
        return fingerprint(root, list(expected)) == expected
    except (OSError, ValueError, TypeError):
        return False
