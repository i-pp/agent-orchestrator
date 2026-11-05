"""Small fail-closed OAuth JWT resource-server verifier for HTTP MCP mode.

The authorization server is an existing external IdP; this module does not
issue tokens, fetch keys, or accept OpenAI client metadata as an identity.
Operators provision a pinned public JWKS and exact issuer/audience/scope
contract; verified issuer/subject pairs map to provider-independent AceIS IDs.
"""
import base64
import binascii
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlsplit

from local_principal import OAuthPrincipal


MAX_TOKEN_BYTES = 16_384
MAX_JWKS_BYTES = 65_536
MAX_JWKS_KEYS = 16
SCOPES = frozenset({'mcp:access', 'mcp:events'})


class OAuthError(ValueError):
    """Generic auth failure; callers must not disclose token validation detail."""


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise OAuthError('INVALID_JSON')
        result[key] = value
    return result


def _decode_json(raw):
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=lambda _value: (_ for _ in ()).throw(OAuthError('INVALID_JSON')))
    except (UnicodeError, ValueError, RecursionError, json.JSONDecodeError):
        raise OAuthError('INVALID_JSON') from None
    if not isinstance(value, dict):
        raise OAuthError('INVALID_JSON')
    return value


def _b64url(raw):
    if (not isinstance(raw, str) or not raw or len(raw) > 12_000
            or re.fullmatch(r'[A-Za-z0-9_-]+', raw) is None):
        raise OAuthError('INVALID_TOKEN')
    try:
        return base64.b64decode(raw + '=' * (-len(raw) % 4), altchars=b'-_', validate=True)
    except (ValueError, binascii.Error):
        raise OAuthError('INVALID_TOKEN') from None


def _integer_claim(value):
    return type(value) in (int, float) and value == value and abs(value) < 10**15


def _https_url(value):
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except (TypeError, ValueError):
        return False
    return (parsed.scheme == 'https' and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment)


def _load_jwks(path):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise ValueError('JWKS unavailable') from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JWKS_BYTES
                or info.st_uid not in (0, os.getuid()) or stat.S_IMODE(info.st_mode) & 0o222
                or info.st_nlink != 1):
            raise ValueError('Unsafe JWKS file')
        data = bytearray()
        while len(data) <= MAX_JWKS_BYTES:
            chunk = os.read(fd, min(8192, MAX_JWKS_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_JWKS_BYTES:
            raise ValueError('JWKS file too large')
        return _decode_json(bytes(data))
    finally:
        os.close(fd)


class OAuthJWTVerifier:
    """Provider-neutral RS256 OIDC access-token verifier using pinned issuer keys."""

    def __init__(self, *, issuer, audience, allowed_subject=None, allowed_subjects=None, jwks,
                 required_scopes=('mcp:access',), clock=time.time, leeway_seconds=30):
        if allowed_subjects is None:
            allowed_subjects = None if allowed_subject is None else (allowed_subject,)
        elif isinstance(allowed_subjects, str):
            allowed_subjects = (allowed_subjects,)
        if (allowed_subjects is not None
                and (not isinstance(allowed_subjects, (tuple, list, set, frozenset))
                     or not 1 <= len(allowed_subjects) <= 32
                     or any(not isinstance(subject, str) or not 1 <= len(subject) <= 255
                            for subject in allowed_subjects)
                     or len(set(allowed_subjects)) != len(allowed_subjects))):
            raise ValueError('Invalid OAuth subject allowlist')
        if (not _https_url(issuer) or not isinstance(audience, str)
                or not audience or len(audience) > 512
                or type(leeway_seconds) not in (int, float)
                or not 0 <= leeway_seconds <= 120 or not callable(clock)):
            raise ValueError('Invalid OAuth verifier configuration')
        if (not isinstance(required_scopes, (tuple, list, set, frozenset))
                or 'mcp:access' not in required_scopes
                or not set(required_scopes) <= SCOPES):
            raise ValueError('Invalid OAuth scopes')
        if not isinstance(jwks, dict) or set(jwks) != {'keys'}:
            raise ValueError('Invalid JWKS')
        keys = jwks['keys']
        if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_JWKS_KEYS:
            raise ValueError('Invalid JWKS')
        try:
            from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
        except ImportError:
            raise ValueError('RSA verifier unavailable') from None
        public_keys = {}
        for key in keys:
            if (not isinstance(key, dict) or key.get('kty') != 'RSA'
                    or key.get('use') not in (None, 'sig')
                    or key.get('alg') not in (None, 'RS256')
                    or any(secret in key for secret in ('d', 'p', 'q', 'dp', 'dq', 'qi', 'oth'))):
                raise ValueError('Invalid JWKS key')
            kid, n, e = key.get('kid'), key.get('n'), key.get('e')
            if (not isinstance(kid, str) or not 1 <= len(kid) <= 128 or kid in public_keys
                    or not isinstance(n, str) or not isinstance(e, str)):
                raise ValueError('Invalid JWKS key')
            try:
                modulus = int.from_bytes(_b64url(n), 'big')
                exponent = int.from_bytes(_b64url(e), 'big')
                if modulus.bit_length() < 2048 or modulus.bit_length() > 8192 or exponent < 3 or exponent % 2 == 0:
                    raise ValueError
                public_keys[kid] = RSAPublicNumbers(exponent, modulus).public_key()
            except Exception:
                raise ValueError('Invalid JWKS key') from None
        self.issuer = issuer
        self.audience = audience
        self.allowed_subjects = (None if allowed_subjects is None
                                 else frozenset(allowed_subjects))
        self.required_scopes = frozenset(required_scopes)
        self.clock = clock
        self.leeway_seconds = float(leeway_seconds)
        self._public_keys = public_keys
        # Kept for compatibility with offline fixtures that opt into an exact
        # subject allowlist. Normal OIDC operation maps each verified `sub` at
        # request time and does not require pre-enumerating users.
        self.principals = (tuple(OAuthPrincipal(issuer, subject)
                                 for subject in sorted(self.allowed_subjects))
                           if self.allowed_subjects is not None else ())
        self.principal = self.principals[0] if len(self.principals) == 1 else None

    @classmethod
    def from_environment(cls, environ=None):
        environment = os.environ if environ is None else environ
        required = ('ACEIS_OAUTH_ISSUER', 'ACEIS_OAUTH_AUDIENCE',
                    'ACEIS_OAUTH_JWKS_FILE')
        if any(not isinstance(environment.get(name), str) or not environment[name]
               for name in required):
            raise ValueError('OAuth configuration incomplete')
        if environment.get('ACEIS_OAUTH_ALLOWED_SUBJECTS_JSON'):
            try:
                subjects = json.loads(environment['ACEIS_OAUTH_ALLOWED_SUBJECTS_JSON'],
                                      object_pairs_hook=_pairs)
            except (ValueError, json.JSONDecodeError):
                raise ValueError('OAuth subject allowlist is invalid') from None
        else:
            subjects = environment.get('ACEIS_OAUTH_ALLOWED_SUBJECT')
        path = Path(environment['ACEIS_OAUTH_JWKS_FILE'])
        if not path.is_absolute() or any(parent.is_symlink() for parent in [path, *path.parents]):
            raise ValueError('Unsafe JWKS path')
        jwks = _load_jwks(path)
        scopes = environment.get('ACEIS_OAUTH_REQUIRED_SCOPES', 'mcp:access').split()
        return cls(issuer=environment['ACEIS_OAUTH_ISSUER'],
                   audience=environment['ACEIS_OAUTH_AUDIENCE'],
                   allowed_subjects=subjects,
                   jwks=jwks, required_scopes=scopes)

    def verify(self, authorization):
        if (not isinstance(authorization, str) or len(authorization) > MAX_TOKEN_BYTES + 7
                or not authorization.startswith('Bearer ')):
            raise OAuthError('UNAUTHORIZED')
        token = authorization[7:]
        if len(token) > MAX_TOKEN_BYTES:
            raise OAuthError('UNAUTHORIZED')
        parts = token.split('.')
        if len(parts) != 3:
            raise OAuthError('UNAUTHORIZED')
        try:
            header = _decode_json(_b64url(parts[0]))
            claims = _decode_json(_b64url(parts[1]))
            signature = _b64url(parts[2])
            if (set(header) - {'alg', 'typ', 'kid'} or header.get('alg') != 'RS256'
                    or header.get('typ') not in (None, 'JWT')
                    or not isinstance(header.get('kid'), str)):
                raise OAuthError('UNAUTHORIZED')
            key = self._public_keys.get(header['kid'])
            if key is None:
                raise OAuthError('UNAUTHORIZED')
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding
            key.verify(signature, (parts[0] + '.' + parts[1]).encode('ascii'),
                       padding.PKCS1v15(), hashes.SHA256())
            now = float(self.clock())
            exp, nbf, issued = claims.get('exp'), claims.get('nbf'), claims.get('iat')
            subject = claims.get('sub')
            if (claims.get('iss') != self.issuer
                    or not isinstance(subject, str) or not 1 <= len(subject) <= 255
                    or self.allowed_subjects is not None and subject not in self.allowed_subjects
                    or not _integer_claim(exp) or exp <= now - self.leeway_seconds
                    or nbf is not None and (not _integer_claim(nbf) or nbf > now + self.leeway_seconds)
                    or issued is not None and (not _integer_claim(issued) or issued > now + self.leeway_seconds)):
                raise OAuthError('UNAUTHORIZED')
            audience = claims.get('aud')
            if not (audience == self.audience or isinstance(audience, list)
                    and self.audience in audience and all(isinstance(item, str) for item in audience)):
                raise OAuthError('UNAUTHORIZED')
            raw_scopes = claims.get('scope', claims.get('scp', ''))
            scopes = (set(raw_scopes.split()) if isinstance(raw_scopes, str)
                      else set(raw_scopes) if isinstance(raw_scopes, list)
                      and all(isinstance(item, str) for item in raw_scopes) else set())
            if not self.required_scopes <= scopes:
                raise OAuthError('UNAUTHORIZED')
            return OAuthPrincipal(self.issuer, subject)
        except OAuthError:
            raise OAuthError('UNAUTHORIZED') from None
        except Exception:
            raise OAuthError('UNAUTHORIZED') from None

    def protected_resource_metadata(self, *, resource_url, authorization_server):
        if not _https_url(resource_url) or not _https_url(authorization_server):
            raise ValueError('OAuth resource metadata requires HTTPS URLs')
        return {'resource': resource_url,
                'authorization_servers': [authorization_server],
                'scopes_supported': sorted(self.required_scopes),
                'bearer_methods_supported': ['header']}


class OIDCVerifierSet:
    """Provider-neutral MCP resource verifier for one or more pinned OIDC issuers.

    Each issuer must issue an RS256 JWT access token whose audience and scopes
    target this MCP resource. This does not implement an authorization server
    or treat an OIDC ID token as an MCP access token.
    """

    def __init__(self, verifiers, *, authorization_servers=None):
        if (not isinstance(verifiers, (tuple, list)) or not 1 <= len(verifiers) <= 8
                or any(not isinstance(item, OAuthJWTVerifier) for item in verifiers)):
            raise ValueError('Invalid OIDC verifier set')
        if len({item.issuer for item in verifiers}) != len(verifiers):
            raise ValueError('Duplicate OIDC issuer')
        if len({item.audience for item in verifiers}) != 1:
            raise ValueError('OIDC issuers must target the same MCP audience')
        if authorization_servers is None:
            authorization_servers = tuple(item.issuer for item in verifiers)
        elif isinstance(authorization_servers, str):
            authorization_servers = (authorization_servers,)
        if (not isinstance(authorization_servers, (tuple, list))
                or len(authorization_servers) != len(verifiers)
                or any(not _https_url(value) for value in authorization_servers)):
            raise ValueError('Invalid OIDC authorization-server list')
        scope_sets = {item.required_scopes for item in verifiers}
        if len(scope_sets) != 1:
            raise ValueError('OIDC issuers must share the MCP scope contract')
        self.verifiers = tuple(verifiers)
        self.issuers = tuple(item.issuer for item in verifiers)
        self.authorization_servers = tuple(authorization_servers)
        self.audience = verifiers[0].audience
        self.required_scopes = verifiers[0].required_scopes
        self.issuer = self.issuers[0] if len(self.issuers) == 1 else None

    @classmethod
    def from_environment(cls, environ=None):
        environment = os.environ if environ is None else environ
        raw = environment.get('ACEIS_OIDC_PROVIDERS_JSON')
        if raw:
            if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_JWKS_BYTES:
                raise ValueError('OIDC provider configuration is too large')
            try:
                entries = json.loads(raw, object_pairs_hook=_pairs)
            except (ValueError, json.JSONDecodeError):
                raise ValueError('OIDC provider configuration is invalid') from None
            if not isinstance(entries, list) or not 1 <= len(entries) <= 8:
                raise ValueError('OIDC provider configuration is invalid')
            verifiers, auth_servers = [], []
            for entry in entries:
                if (not isinstance(entry, dict)
                        or set(entry) - {'issuer', 'audience', 'jwks_file',
                                         'authorization_server', 'required_scopes'}
                        or not {'issuer', 'audience', 'jwks_file'} <= set(entry)):
                    raise ValueError('OIDC provider configuration is invalid')
                path = Path(entry['jwks_file'])
                if not path.is_absolute() or any(parent.is_symlink()
                                                 for parent in [path, *path.parents]):
                    raise ValueError('Unsafe JWKS path')
                scopes = entry.get('required_scopes', ['mcp:access'])
                jwks = _load_jwks(path)
                verifiers.append(OAuthJWTVerifier(
                    issuer=entry['issuer'], audience=entry['audience'], jwks=jwks,
                    required_scopes=scopes))
                auth_servers.append(entry.get('authorization_server', entry['issuer']))
            return cls(verifiers, authorization_servers=auth_servers)

        verifier = OAuthJWTVerifier.from_environment(environment)
        authorization_server = environment.get('ACEIS_OAUTH_AUTHORIZATION_SERVER',
                                               verifier.issuer)
        return cls((verifier,), authorization_servers=(authorization_server,))

    def verify(self, authorization):
        for verifier in self.verifiers:
            try:
                return verifier.verify(authorization)
            except OAuthError:
                continue
        raise OAuthError('UNAUTHORIZED')

    def protected_resource_metadata(self, *, resource_url):
        if not _https_url(resource_url):
            raise ValueError('OAuth resource metadata requires HTTPS URLs')
        return {'resource': resource_url,
                'authorization_servers': list(self.authorization_servers),
                'scopes_supported': sorted(self.required_scopes),
                'bearer_methods_supported': ['header']}
