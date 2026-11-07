"""Validate an operator-fixed project root and its private runtime directory."""
from pathlib import Path
import os
import stat


PROJECT = Path(__file__).resolve().parents[1]


def resolve_project_root(project_root=None):
    raw = Path(PROJECT if project_root is None else project_root).expanduser().absolute()
    if any(path.is_symlink() for path in [raw, *raw.parents]):
        raise ValueError('Unsafe project ancestry')
    resolved = raw.resolve(strict=True)
    try:
        info = resolved.stat()
        runtime = resolved / '.runtime'
        runtime_info = runtime.stat()
    except OSError:
        raise ValueError('Project runtime anchor is unavailable') from None
    if (not resolved.is_dir() or info.st_uid != os.getuid()
            or runtime.is_symlink() or not runtime.is_dir()
            or runtime_info.st_uid != os.getuid()
            or stat.S_IMODE(runtime_info.st_mode) != 0o700
            or not (resolved / 'src' / 'daemon.py').is_file()):
        raise ValueError('Project root is not an operator-owned runtime anchor')
    return resolved


def runtime_path(root, *, project_root=None):
    project = resolve_project_root(project_root)
    raw = Path(root).expanduser().absolute()
    if any(path.is_symlink() for path in [raw, *raw.parents]):
        raise ValueError('Unsafe runtime ancestry')
    resolved = raw.resolve(strict=True)
    if not resolved.is_relative_to(project / '.runtime'):
        raise ValueError('Runtime must be inside the selected project .runtime')
    for path in [resolved, resolved / 'logs']:
        info = path.stat()
        if (path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError('Runtime and logs must be private operator-owned directories')
    return resolved
