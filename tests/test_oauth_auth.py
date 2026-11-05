"""Offline RS256 resource-server tests using disposable in-memory keys."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from local_principal import OAuthPrincipal
from oauth_auth import OAuthError, OAuthJWTVerifier


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


class OAuthJWTVerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = cls.private_key.public_key().public_numbers()
        cls.jwks = {'keys': [{'kty': 'RSA', 'use': 'sig', 'alg': 'RS256', 'kid': 'fixture-key',
                              'n': b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, 'big')),
                              'e': b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, 'big'))}]}

    def setUp(self):
        self.now = time.time()
        self.verifier = OAuthJWTVerifier(
            issuer='https://issuer.example.test', audience='https://mcp.example.test/mcp',
            allowed_subject='owner-opaque-id', jwks=self.jwks, clock=lambda: self.now)

    def sign(self, claims=None, header=None, key=None):
        selected_header = {'alg': 'RS256', 'typ': 'JWT', 'kid': 'fixture-key'}
        selected_header.update(header or {})
        selected_claims = {'iss': 'https://issuer.example.test', 'sub': 'owner-opaque-id',
                           'aud': 'https://mcp.example.test/mcp', 'exp': self.now + 60,
                           'iat': self.now, 'scope': 'mcp:access'}
        selected_claims.update(claims or {})
        signing_input = (b64url(json.dumps(selected_header, separators=(',', ':')).encode()) + '.'
                         + b64url(json.dumps(selected_claims, separators=(',', ':')).encode()))
        signature = (key or self.private_key).sign(signing_input.encode('ascii'),
            padding.PKCS1v15(), hashes.SHA256())
        return signing_input + '.' + b64url(signature)

    def test_valid_token_returns_only_the_allowlisted_issuer_subject(self):
        principal = self.verifier.verify('Bearer ' + self.sign())
        self.assertEqual(principal, OAuthPrincipal('https://issuer.example.test', 'owner-opaque-id'))

    def test_multiple_users_receive_distinct_verified_principals(self):
        verifier = OAuthJWTVerifier(
            issuer='https://issuer.example.test', audience='https://mcp.example.test/mcp',
            allowed_subjects=('owner-opaque-id', 'second-opaque-id'), jwks=self.jwks,
            clock=lambda: self.now)
        principal = verifier.verify('Bearer ' + self.sign({'sub': 'second-opaque-id'}))
        self.assertEqual(principal.subject, 'second-opaque-id')
        self.assertEqual({item.subject for item in verifier.principals},
                         {'owner-opaque-id', 'second-opaque-id'})

    def test_environment_loader_requires_read_only_pinned_jwks_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'jwks.json'
            path.write_text(json.dumps(self.jwks), encoding='utf-8')
            path.chmod(0o400)
            environ = {'ACEIS_OAUTH_ISSUER': 'https://issuer.example.test',
                       'ACEIS_OAUTH_AUDIENCE': 'https://mcp.example.test/mcp',
                       'ACEIS_OAUTH_ALLOWED_SUBJECTS_JSON': '["owner-opaque-id","second-opaque-id"]',
                       'ACEIS_OAUTH_JWKS_FILE': str(path)}
            verifier = OAuthJWTVerifier.from_environment(environ)
            self.assertEqual(len(verifier.principals), 2)
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                OAuthJWTVerifier.from_environment(environ)

    def test_wrong_issuer_subject_audience_expiry_scope_and_algorithm_fail_closed(self):
        cases = [
            ({'iss': 'https://other.example.test'}, {}),
            ({'sub': 'other-user'}, {}),
            ({'aud': 'https://other.example.test'}, {}),
            ({'exp': self.now - 60}, {}),
            ({'scope': 'mcp:events'}, {}),
            ({}, {'alg': 'none'}),
            ({}, {'kid': 'unknown'}),
        ]
        for claims, header in cases:
            with self.subTest(claims=claims, header=header):
                with self.assertRaises(OAuthError):
                    self.verifier.verify('Bearer ' + self.sign(claims, header))

    def test_signature_and_duplicate_json_keys_are_rejected(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(OAuthError):
            self.verifier.verify('Bearer ' + self.sign(key=other))
        token = self.sign().split('.')
        token[1] = b64url(b'{"iss":"https://issuer.example.test","iss":"https://evil.example"}')
        with self.assertRaises(OAuthError):
            self.verifier.verify('Bearer ' + '.'.join(token))

    def test_jwks_rejects_private_material_and_weak_keys(self):
        with self.assertRaises(ValueError):
            OAuthJWTVerifier(issuer='https://issuer.example.test', audience='resource',
                allowed_subject='owner', jwks={'keys': [{**self.jwks['keys'][0], 'd': 'private'}]})

    def test_metadata_requires_https_resource_and_authorization_server(self):
        with self.assertRaises(ValueError):
            self.verifier.protected_resource_metadata(
                resource_url='http://mcp.example.test/mcp',
                authorization_server='https://issuer.example.test')
        metadata = self.verifier.protected_resource_metadata(
            resource_url='https://mcp.example.test/mcp',
            authorization_server='https://issuer.example.test')
        self.assertEqual(metadata['resource'], 'https://mcp.example.test/mcp')


if __name__ == '__main__':
    unittest.main()
