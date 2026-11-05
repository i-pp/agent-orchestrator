"""The single local OS-user principal used by the private loopback adapter."""
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class LocalPrincipal:
    """Process UID identity; never derived from MCP request metadata."""

    uid: int

    def __post_init__(self):
        if type(self.uid) is not int or self.uid != os.getuid():
            raise ValueError('Principal must match the current local UID')

    @property
    def identity(self):
        return f'local-uid:{self.uid}'


def current_local_principal():
    """Derive the sole principal from the current process credentials."""
    return LocalPrincipal(os.getuid())


def principal_identity(value):
    """Return the stable local identity or a legacy low-level fixture string."""
    if isinstance(value, LocalPrincipal):
        return value.identity
    if isinstance(value, str):
        return value
    return None
