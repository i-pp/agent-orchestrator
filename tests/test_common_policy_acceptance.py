"""Independent acceptance checks from the public policy-loading contract.

No model, network, native user state, or live service is exercised here.
"""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from common_policy import (PolicyError, chat_messages, context_packet,
                           developer_instructions, load_policy, load_project_policy)

CANONICAL_SHA256 = '8fede5f946fd944485e7095ea27cb4923fb97ebf466d1e4b355ad21a50a00286'


class CommonPolicyAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='policy-acceptance-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        self.canonical = (PROJECT / 'policies.md').read_bytes()
        self.assertEqual(hashlib.sha256(self.canonical).hexdigest(), CANONICAL_SHA256)
        self.populate(self.root)

    def populate(self, root):
        (root / 'policies').mkdir(parents=True, exist_ok=True)
        (root / 'config').mkdir(parents=True, exist_ok=True)
        (root / 'policies.md').write_bytes(self.canonical)
        self.manifest(root)
        self.config(root)

    def manifest(self, root, **changes):
        data = dict(schema_version=1, version='1.0', file='policies.md', sha256=CANONICAL_SHA256)
        data.update(changes)
        (root / 'policies/manifest.json').write_text(json.dumps(data))

    def config(self, root, **changes):
        data = dict(schema_version=1, bundle_root='.', expected_version='1.0',
                    expected_sha256=CANONICAL_SHA256)
        data.update(changes)
        (root / 'config/policy.json').write_text(json.dumps(data))

    def bytes_tree(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file() and not p.is_symlink()}

    def rejects_without_rewriting(self, operation):
        before = self.bytes_tree()
        with self.assertRaises(PolicyError):
            operation()
        self.assertEqual(self.bytes_tree(), before)

    def test_user_canonical_body_and_pinned_hash_are_exact(self):
        loaded = load_project_policy(self.root)
        self.assertEqual(loaded.text.encode('utf-8'), self.canonical)
        self.assertEqual(loaded.sha256, CANONICAL_SHA256)
        self.assertEqual(loaded.version, '1.0')
        self.assertEqual(Path(loaded.source), self.root / 'policies.md')
        self.assertEqual(loaded.metadata()['sha256'], CANONICAL_SHA256)
        self.assertNotIn('text', loaded.metadata())

    def test_policy_bundle_cannot_be_mutated(self):
        loaded = load_project_policy(self.root)
        with self.assertRaises((AttributeError, TypeError)):
            loaded.text = 'changed'

    def test_developer_context_contains_whole_policy_once_and_role(self):
        loaded = load_project_policy(self.root)
        role = 'Return the required execution JSON; independent reviewer evidence required.'
        assembled = developer_instructions(loaded, role)
        self.assertEqual(assembled.count(loaded.text), 1)
        self.assertIn(role, assembled)
        self.assertIn(CANONICAL_SHA256, assembled)
        self.assertIn('1.0', assembled)
        self.assertIn(loaded.source, assembled)

    def test_openai_chat_payload_contains_policy_in_system_not_task(self):
        loaded = load_project_policy(self.root)
        task = 'Check the fixture only.'
        messages = chat_messages(loaded, task, 'Read-only verifier role.')
        self.assertEqual([m['role'] for m in messages], ['system', 'user'])
        self.assertEqual(messages[0]['content'].count(loaded.text), 1)
        self.assertIn('Read-only verifier role.', messages[0]['content'])
        self.assertEqual(messages[1]['content'], task)

    def test_each_environment_packet_contains_actual_body(self):
        loaded = load_project_policy(self.root)
        for target in ['chatgpt', 'dot', 'codex', 'local']:
            with self.subTest(target=target):
                packet = context_packet(loaded, 'fixed task', target)
                self.assertEqual(packet['schema_version'], 1)
                self.assertEqual(packet['target'], target)
                self.assertEqual(packet['task'], 'fixed task')
                self.assertEqual(packet['policy']['sha256'], CANONICAL_SHA256)
                self.assertEqual(packet['developer_instructions'].count(loaded.text), 1)
                json.dumps(packet, ensure_ascii=False)

    def test_unknown_environment_is_rejected(self):
        loaded = load_project_policy(self.root)
        with self.assertRaises(PolicyError):
            context_packet(loaded, 'task', 'unimplemented-api')

    def test_missing_policy_manifest_or_project_config_is_rejected(self):
        for relative in ['policies.md', 'policies/manifest.json', 'config/policy.json']:
            with self.subTest(relative=relative):
                path = self.root / relative
                original = path.read_bytes()
                path.unlink()
                try:
                    self.rejects_without_rewriting(lambda: load_project_policy(self.root))
                finally:
                    path.write_bytes(original)

    def test_changed_policy_does_not_silently_rehash_or_fallback(self):
        (self.root / 'policies.md').write_bytes(self.canonical + b'\nchanged\n')
        self.rejects_without_rewriting(lambda: load_project_policy(self.root))

    def test_manifest_rehashed_without_project_pin_update_is_rejected(self):
        new = self.canonical + b'\nchange\n'
        (self.root / 'policies.md').write_bytes(new)
        self.manifest(self.root, sha256=hashlib.sha256(new).hexdigest())
        self.rejects_without_rewriting(lambda: load_project_policy(self.root))

    def test_version_conflicts_and_explicit_hash_pin_are_rejected(self):
        self.manifest(self.root, version='2.0')
        self.rejects_without_rewriting(lambda: load_project_policy(self.root))
        self.manifest(self.root)
        self.config(self.root, expected_version='2.0')
        self.rejects_without_rewriting(lambda: load_project_policy(self.root))
        self.config(self.root)
        self.rejects_without_rewriting(lambda: load_policy(self.root, expected_sha256='0' * 64))

    def test_invalid_utf8_is_rejected_even_with_matching_hashes(self):
        invalid = b'\xff\xfe'
        digest = hashlib.sha256(invalid).hexdigest()
        (self.root / 'policies.md').write_bytes(invalid)
        self.manifest(self.root, sha256=digest)
        self.config(self.root, expected_sha256=digest)
        self.rejects_without_rewriting(lambda: load_project_policy(self.root))

    def test_duplicate_json_keys_are_rejected(self):
        for relative, body in [
            ('policies/manifest.json', '{"schema_version":1,"schema_version":1}'),
            ('config/policy.json', '{"schema_version":1,"schema_version":1}')]:
            with self.subTest(relative=relative):
                path = self.root / relative; original = path.read_bytes()
                path.write_text(body)
                try:
                    self.rejects_without_rewriting(lambda: load_project_policy(self.root))
                finally:
                    path.write_bytes(original)

    def test_unknown_project_settings_and_absolute_bundle_are_rejected(self):
        self.config(self.root, fallback_to_builtin=True)
        self.rejects_without_rewriting(lambda: load_project_policy(self.root))
        self.config(self.root, bundle_root=str(self.root))
        self.rejects_without_rewriting(lambda: load_project_policy(self.root))

    def test_manifest_cannot_redirect_to_another_document(self):
        (self.root / 'other.md').write_bytes(self.canonical)
        self.manifest(self.root, file='other.md')
        self.rejects_without_rewriting(lambda: load_project_policy(self.root))

    def test_policy_and_manifest_symlinks_are_rejected(self):
        for relative in ['policies.md', 'policies/manifest.json']:
            with self.subTest(relative=relative):
                path = self.root / relative
                original = path.read_bytes(); backing = self.root / 'backing'
                backing.write_bytes(original); path.unlink(); path.symlink_to(backing)
                try:
                    self.rejects_without_rewriting(lambda: load_project_policy(self.root))
                finally:
                    path.unlink(); path.write_bytes(original); backing.unlink()

    def test_common_bundle_can_move_outside_project_with_pins(self):
        shared = Path(self.temp.name) / 'shared'; shared.mkdir()
        self.populate(shared)
        shutil.rmtree(self.root / 'policies')
        (self.root / 'policies.md').unlink()
        self.config(self.root, bundle_root='../shared')
        loaded = load_project_policy(self.root)
        self.assertEqual(loaded.text.encode(), self.canonical)
        self.assertEqual(loaded.sha256, CANONICAL_SHA256)
        self.assertEqual(Path(loaded.source), shared / 'policies.md')


if __name__ == '__main__':
    unittest.main(verbosity=2)
