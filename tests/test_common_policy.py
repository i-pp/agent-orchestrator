"""Unit checks for exact policy snapshots, pins and transport-neutral assembly."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'src'))
from common_policy import (PolicyError, load_policy, load_project_policy,
                           developer_instructions, chat_messages, context_packet)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='common-policy-unit-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root/'common'
        (self.bundle/'policies').mkdir(parents=True)
        self.raw = (PROJECT/'policies.md').read_bytes()
        self.hash = hashlib.sha256(self.raw).hexdigest()
        (self.bundle/'policies.md').write_bytes(self.raw)
        self.manifest = {'schema_version': 1, 'version': '1.0',
                         'file': 'policies.md', 'sha256': self.hash}
        self.write_manifest()

    def write_manifest(self):
        (self.bundle/'policies/manifest.json').write_text(json.dumps(self.manifest))

    def project(self):
        project = self.root/'project'; (project/'config').mkdir(parents=True)
        config = {'schema_version': 1, 'bundle_root': '../common',
                  'expected_version': '1.0', 'expected_sha256': self.hash}
        (project/'config/policy.json').write_text(json.dumps(config))
        return project, config

    def test_exact_bytes_and_immutable_bundle(self):
        value = load_policy(self.bundle)
        self.assertEqual(value.text.encode('utf-8'), self.raw)
        self.assertEqual(value.sha256, self.hash)
        self.assertEqual(value.version, '1.0')
        self.assertEqual(value.source, str(self.bundle/'policies.md'))
        with self.assertRaises(AttributeError): value.text = 'modified'
        metadata = value.metadata(); metadata['version'] = 'changed'
        self.assertEqual(value.metadata()['version'], '1.0')
        self.assertNotIn('text', value.metadata())

    def test_caller_pins_reject_conflicts(self):
        self.assertEqual(load_policy(self.bundle, expected_version='1.0', expected_sha256=self.hash).sha256, self.hash)
        for args in [{'expected_version': '1.1'}, {'expected_sha256': '0'*64}]:
            with self.subTest(args=args), self.assertRaises(PolicyError): load_policy(self.bundle, **args)

    def test_relocation_and_separate_project_config(self):
        project, _ = self.project()
        self.assertEqual(load_project_policy(project).sha256, self.hash)
        moved = self.root/'moved'; shutil.copytree(self.root/'common', moved/'common')
        shutil.copytree(project, moved/'project')
        self.assertEqual(load_project_policy(moved/'project').text.encode(), self.raw)

    def test_missing_files(self):
        for relative in ['policies.md', 'policies/manifest.json']:
            path = self.bundle/relative; saved = path.read_bytes(); path.unlink()
            with self.subTest(path=relative), self.assertRaises(PolicyError): load_policy(self.bundle)
            path.write_bytes(saved)

    def test_tamper_and_line_ending_changes(self):
        for raw in [self.raw+b'\n', self.raw.replace(b'\n', b'\r\n'), self.raw.replace(b'v1.0', b'v2.0', 1)]:
            (self.bundle/'policies.md').write_bytes(raw)
            with self.assertRaises(PolicyError): load_policy(self.bundle)

    def test_version_heading_mismatch_even_with_new_hash(self):
        raw = self.raw.replace(b'v1.0', b'v1.1', 1)
        (self.bundle/'policies.md').write_bytes(raw)
        self.manifest['sha256'] = hashlib.sha256(raw).hexdigest(); self.write_manifest()
        with self.assertRaises(PolicyError): load_policy(self.bundle)

    def test_invalid_utf8_even_with_matching_hash(self):
        raw = b'\xff\xfe'; (self.bundle/'policies.md').write_bytes(raw)
        self.manifest['sha256'] = hashlib.sha256(raw).hexdigest(); self.write_manifest()
        with self.assertRaises(PolicyError): load_policy(self.bundle)

    def test_manifest_schema_and_fields(self):
        for changed in [dict(self.manifest, schema_version=True), dict(self.manifest, schema_version=2),
                        dict(self.manifest, extra='unknown'), dict(self.manifest, file='../private'),
                        dict(self.manifest, file='/private'), dict(self.manifest, version='1.0\n'),
                        dict(self.manifest, sha256='not-a-hash')]:
            (self.bundle/'policies/manifest.json').write_text(json.dumps(changed))
            with self.subTest(changed=changed), self.assertRaises(PolicyError): load_policy(self.bundle)

    def test_invalid_json_and_duplicate_keys(self):
        for raw in ['{', '[]', '{"schema_version":1,"schema_version":1}',
                    json.dumps(self.manifest).replace('"schema_version": 1', '"schema_version": NaN')]:
            (self.bundle/'policies/manifest.json').write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(PolicyError): load_policy(self.bundle)

    def test_project_config_rejects_missing_conflicting_and_unknown_fields(self):
        project, config = self.project(); path = project/'config/policy.json'
        for changed in [dict(config, bundle_root='/absolute'), dict(config, bundle_root=''),
                        dict(config, bundle_root='\x00'), dict(config, bundle_root='\ud800'),
                        dict(config, expected_version='2.0'), dict(config, expected_sha256='0'*64),
                        dict(config, extra='unknown'), {k:v for k,v in config.items() if k!='expected_sha256'}]:
            path.write_text(json.dumps(changed))
            with self.subTest(changed=changed), self.assertRaises(PolicyError): load_project_policy(project)

    def test_symlink_roots_files_and_ancestors(self):
        alias = self.root/'alias'; alias.symlink_to(self.bundle, target_is_directory=True)
        with self.assertRaises(PolicyError): load_policy(alias)
        link = self.bundle/'policies.md'; link.unlink(); link.symlink_to(PROJECT/'policies.md')
        with self.assertRaises(PolicyError): load_policy(self.bundle)
        link.unlink(); link.write_bytes(self.raw)
        manifest = self.bundle/'policies/manifest.json'; manifest.unlink()
        manifest.symlink_to(PROJECT/'policies/manifest.json')
        with self.assertRaises(PolicyError): load_policy(self.bundle)

    def test_parent_symlink_with_dotdot_is_not_resolved_away(self):
        alias = self.root/'alias'; alias.symlink_to(self.bundle, target_is_directory=True)
        with self.assertRaises(PolicyError): load_policy(alias/'..'/'common')

    def test_directory_in_place_of_canonical_file(self):
        path = self.bundle/'policies.md'; path.unlink(); path.mkdir()
        with self.assertRaises(PolicyError): load_policy(self.bundle)

    def test_invalid_path_values_fail_as_policy_error(self):
        for path in ['invalid\x00path', '\ud800']:
            with self.subTest(path=repr(path)), self.assertRaises(PolicyError): load_policy(path)

    def test_context_assembly_rechecks_bundle_integrity(self):
        value = load_policy(self.bundle)
        for changed in [value._replace(text='modified'), value._replace(version='2.0'),
                        value._replace(source='')]:
            with self.subTest(changed=changed.version), self.assertRaises(PolicyError):
                developer_instructions(changed)

    def test_full_text_once_and_role_coexistence(self):
        bundle = load_policy(self.bundle)
        text = developer_instructions(bundle, 'Return verified JSON; reviewer must stay read-only.')
        self.assertEqual(text.count(bundle.text), 1)
        self.assertIn('Return verified JSON', text)
        self.assertIn(self.hash, text)
        self.assertIn('ユーザーの明示的制約が優先', text)
        self.assertIn('権限や承認を追加しない', text)
        with self.assertRaises(PolicyError): developer_instructions(bundle, bundle.text)

    def test_messages_and_handoffs_have_body_without_network(self):
        bundle = load_policy(self.bundle)
        messages = chat_messages(bundle, 'original task', 'role only')
        self.assertEqual(messages[1], {'role':'user', 'content':'original task'})
        self.assertEqual(messages[0]['content'].count(bundle.text), 1)
        for target in ['chatgpt','dot','codex','local']:
            packet = context_packet(bundle, 'fixed task', target)
            self.assertEqual(packet['developer_instructions'].count(bundle.text), 1)
            self.assertEqual(packet['policy']['sha256'], self.hash)
            self.assertEqual(packet['target'], target)
            json.dumps(packet, ensure_ascii=False)
        with self.assertRaises(PolicyError): context_packet(bundle, 'task', 'unknown')


if __name__ == '__main__':
    unittest.main(verbosity=2)
