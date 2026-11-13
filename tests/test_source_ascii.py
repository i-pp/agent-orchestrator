"""ASCII gate fixtures use disposable repositories, not private runtime files."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('source_ascii', PROJECT / 'scripts/check_source_ascii.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CONFIG_FIXTURES = (
    '.gitignore',
    'config/fixture.settings',
    'examples/fixture.service',
    'examples/fixture.service.example',
    'examples/fixture.toml.example',
    'systemd/apparmor/fixture.draft',
    'systemd/fixture.example.service',
    'systemd/fixture.service',
    'systemd/fixture.service.example',
)


class SourceAsciiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '--quiet', '--template=')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args])

    def stage(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        self.git('add', '--', name)
        return path

    def test_unicode_escapes_and_literal_ascii_pass_for_code_and_role_files(self):
        self.stage('src/fixture.py', 'text = "\\u3042"\n')
        self.stage('.codex/agents/fixture.toml', 'description = "English role"\n')
        result = module.check(self.root)
        self.assertTrue(result['passed'])
        self.assertEqual(result['checked_sources'], 2)

    def test_literal_unicode_is_rejected_without_echoing_its_value(self):
        value = chr(0x3042)
        self.stage('src/fixture.py', '# first line\ntext = "' + value + '"\n')
        result = module.check(self.root)
        self.assertFalse(result['passed'])
        self.assertEqual(result['findings'], [{'path': 'src/fixture.py', 'line': 2, 'rule': 'non_ascii_source'}])
        self.assertNotIn(value, str(result))

    def test_markdown_historical_bytes_are_not_rewritten_or_flagged(self):
        self.stage('src/fixture.py', '# ASCII source\n')
        path = self.stage('docs/original_jp.md', chr(0x3042) + '\n')
        before = path.read_bytes()
        self.assertTrue(module.check(self.root)['passed'])
        self.assertEqual(path.read_bytes(), before)

    def test_staged_blob_is_authoritative_not_an_unstaged_replacement(self):
        path = self.stage('src/fixture.py', 'text = "' + chr(0x3042) + '"\n')
        path.write_text('# unstaged ASCII replacement\n')
        self.assertFalse(module.check(self.root)['passed'])

    def test_ascii_systemd_and_configuration_examples_pass(self):
        for name in CONFIG_FIXTURES:
            self.stage(name, '# ASCII configuration fixture\n')
        result = module.check(self.root)
        self.assertTrue(result['passed'])
        self.assertEqual(result['checked_sources'], len(CONFIG_FIXTURES))

    def test_unicode_systemd_and_configuration_examples_are_rejected_without_values(self):
        value = chr(0x3042)
        for name in CONFIG_FIXTURES:
            self.stage(name, '# ASCII header\n# ' + value + '\n')
        result = module.check(self.root)
        self.assertFalse(result['passed'])
        self.assertEqual(result['checked_sources'], len(CONFIG_FIXTURES))
        self.assertEqual(result['findings'], [
            {'path': name, 'line': 2, 'rule': 'non_ascii_source'}
            for name in sorted(CONFIG_FIXTURES)
        ])
        self.assertNotIn(value, str(result))

    def test_unstaged_configuration_replacements_cannot_hide_index_findings(self):
        for name in CONFIG_FIXTURES:
            path = self.stage(name, '# ' + chr(0x3042) + '\n')
            path.write_text('# unstaged ASCII replacement\n')
        result = module.check(self.root)
        self.assertFalse(result['passed'])
        self.assertEqual(result['checked_sources'], len(CONFIG_FIXTURES))
        self.assertEqual(result['findings'], [
            {'path': name, 'line': 1, 'rule': 'non_ascii_source'}
            for name in sorted(CONFIG_FIXTURES)
        ])

    def test_case_variant_source_suffixes_and_example_markers_are_guarded(self):
        names = ('src/module.PY', 'examples/check.YML',
                 'examples/fixture.SERVICE.EXAMPLE',
                 'examples/fixture.TOML.ExAmPlE.EXAMPLE')
        for name in names:
            self.stage(name, '# ASCII fixture\n')
        result = module.check(self.root)
        self.assertTrue(result['passed'])
        self.assertEqual(result['checked_sources'], len(names))
        for name in names:
            self.stage(name, '# ' + chr(0x3042) + '\n')
        result = module.check(self.root)
        self.assertFalse(result['passed'])
        self.assertEqual(result['checked_sources'], len(names))
        self.assertEqual(result['findings'], [
            {'path': name, 'line': 1, 'rule': 'non_ascii_source'}
            for name in sorted(names)
        ])

    def test_unstaged_unicode_cannot_replace_ascii_configuration_index(self):
        for name in CONFIG_FIXTURES:
            path = self.stage(name, '# ASCII configuration fixture\n')
            path.write_text('# unstaged ' + chr(0x3042) + '\n')
        result = module.check(self.root)
        self.assertTrue(result['passed'])
        self.assertEqual(result['checked_sources'], len(CONFIG_FIXTURES))

    def test_markdown_in_configuration_roots_and_examples_stays_excluded(self):
        self.stage('src/fixture.py', '# ASCII source\n')
        names = (
            'config/original_jp.md',
            'config/original_jp.markdown.example',
            'config/history/original_jp.MD',
            'systemd/original_jp.md.example',
            'systemd/original_jp.markdown',
            'systemd/history/original_jp.MARKDOWN',
            'systemd/history/original_jp.MD.EXAMPLE',
        )
        paths = [self.stage(name, chr(0x3042) + '\n') for name in names]
        before = [path.read_bytes() for path in paths]
        result = module.check(self.root)
        self.assertTrue(result['passed'])
        self.assertEqual(result['checked_sources'], 1)
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_symlink_configuration_examples_are_not_followed(self):
        names = ('examples/fixture.service.example', 'systemd/apparmor/fixture.draft')
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to('/nonexistent-private-fixture')
            self.git('add', '--', name)
        result = module.check(self.root)
        self.assertFalse(result['passed'])
        self.assertEqual(result['checked_sources'], len(names))
        self.assertEqual(result['findings'], [
            {'path': name, 'rule': 'nonregular_or_conflicted_source'}
            for name in sorted(names)
        ])

    def test_symlink_source_is_not_followed(self):
        path = self.root / 'fixture.py'
        path.symlink_to('/nonexistent-private-fixture')
        self.git('add', '--', path.name)
        result = module.check(self.root)
        self.assertFalse(result['passed'])
        self.assertEqual(result['findings'][0]['rule'], 'nonregular_or_conflicted_source')

    def test_empty_source_inventory_is_not_a_pass(self):
        self.stage('README.md', '# Documentation only\n')
        result = module.check(self.root)
        self.assertFalse(result['passed'])
        self.assertEqual(result['checked_sources'], 0)


if __name__ == '__main__':
    unittest.main()
