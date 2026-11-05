"""Local, externally authenticated, and internal AceIS principal types.

HTTP bearer tokens are verified into `OAuthPrincipal` and then mapped to an
opaque `AcePrincipal` before task or notification authorization.
"""
from dataclasses import dataclass
import hashlib
import os
import re
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
    """External OIDC issuer/subject identity from a verified OAuth access token."""

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
        issuer = self.issuer.encode('utf-8')
        subject = self.subject.encode('utf-8')
        pair = (len(issuer).to_bytes(4, 'big') + issuer
                + len(subject).to_bytes(4, 'big') + subject)
        digest = hashlib.sha256(b'aceis-oidc-session:v1\0' + pair).hexdigest()
        return 'oidc-session:v1:' + digest


@dataclass(frozen=True)
class AcePrincipal:
    """Provider-independent AceIS identity; contains no external claims or email."""

    principal_id: str

    def __post_init__(self):
        if (not isinstance(self.principal_id, str)
                or re.fullmatch(r'pid_v1_[0-9a-f]{32}', self.principal_id) is None):
            raise ValueError('Invalid AceIS principal ID')

    @property
    def identity(self):
        return 'aceis-principal:' + self.principal_id


def current_local_principal():
    """Derive a principal for local stdio use from the adapter's OS credentials."""
    return LocalPrincipal(os.getuid())


def principal_identity(value):
    """Return a stable identity for internal outbox storage APIs.

    Low-level outbox tests retain string fixtures; the MCP Events service
    accepts typed local, internal AceIS, or verified OIDC principals at their
    respective boundaries.
    """
    if isinstance(value, LocalPrincipal):
        return value.identity
    if isinstance(value, OAuthPrincipal):
        return value.identity
    if isinstance(value, AcePrincipal):
        return value.identity
    if isinstance(value, str):
        return value
    return None
