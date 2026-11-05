"""Typed principals for local-only and authenticated OAuth MCP requests.

`LocalPrincipal` describes the adapter process only. Remote HTTP requests must
use `OAuthPrincipal`, which is constructed by the verified JWT resource server.
"""
from dataclasses import dataclass
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class LocalPrincipal:
    uid: int

    def __post_init__(self):
        if type(self.uid) is not int or self.uid != os.getuid():
            raise ValueError('Principal must match the current local UID')

    @property
    def identity(self):
        return f'local-uid:{self.uid}'


@dataclass(frozen=True)
class OAuthPrincipal:
    """Identity derived only from a successfully verified OAuth access token."""

    issuer: str
    subject: str

    def __post_init__(self):
        if (not isinstance(self.issuer, str) or len(self.issuer) > 512
                or not isinstance(self.subject, str) or not 1 <= len(self.subject) <= 255
                or any(ord(char) < 0x21 or ord(char) > 0x7e for char in self.subject)):
            raise ValueError('Invalid OAuth principal')
        try:
            parsed = urlsplit(self.issuer)
        except ValueError:
            raise ValueError('Invalid OAuth principal') from None
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError('Invalid OAuth principal')

    @property
    def identity(self):
        return f'oauth:{self.issuer}:{self.subject}'


def current_local_principal():
    """Derive a principal for local stdio use from the adapter's OS credentials."""
    return LocalPrincipal(os.getuid())


def principal_identity(value):
    """Return a stable identity for internal outbox storage APIs.

    Low-level outbox tests retain string fixtures; the MCP Events service
    accepts only a typed local or verified OAuth principal.
    """
    if isinstance(value, LocalPrincipal):
        return value.identity
    if isinstance(value, OAuthPrincipal):
        return value.identity
    if isinstance(value, str):
        return value
    return None
