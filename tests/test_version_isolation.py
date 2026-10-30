import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from isolation import prepare, binary_version, isolated_launch


class VersionIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT / '.runtime', prefix='version-')
        self.root, _ = prepare(Path(self.temp.name), sys.executable)

    def tearDown(self):
        self.temp.cleanup()

    def test_system_version_works_in_private_namespace(self):
        self.assertTrue(binary_version(self.root, sys.executable).startswith('Python 3.14'))

    def test_probe_environment_and_no_auth_are_mandatory(self):
        with patch('isolation.subprocess.run') as run:
            run.return_value.stdout = 'fixture version\n'
            self.assertEqual(binary_version(self.root, sys.executable), 'fixture version')
            argv = run.call_args.args[0]
            env = run.call_args.kwargs['env']
            self.assertIn('--net', argv)
            self.assertIn('no-auth', argv)
            self.assertEqual(env['HOME'], str(self.root / 'codex-home'))
            self.assertEqual(env['CODEX_HOME'], env['HOME'])
            self.assertNotIn('OPENAI_API_KEY', env)
        with patch('isolation.subprocess.run', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError): binary_version(self.root, sys.executable)

    def test_no_auth_cannot_launch_installed_codex_other_commands(self):
        fake = Path('/opt/fixture/codex')
        with patch('isolation.shutil.which', return_value=str(fake)):
            argv, env = isolated_launch(self.root, str(fake), ['--version'], authenticate=False)
            self.assertIn('--net', argv)
            with self.assertRaises(ValueError):
                isolated_launch(self.root, str(fake), ['app-server'], authenticate=False)

    def test_private_namespace_hides_global_config_and_socket(self):
        code = ('from pathlib import Path;import os;'
                'assert not Path(os.environ["CODEX_HOME"]+"/config.toml").exists();'
                'assert not Path(' + repr(str(Path.home() / '.codex/config.toml')) + ').exists();'
                'assert not Path("/run/user").exists();print("isolated")')
        argv, env = isolated_launch(self.root, sys.executable, ['-B', '-c', code], authenticate=False)
        env['HOME'] = env['CODEX_HOME']
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'isolated')
