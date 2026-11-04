import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from isolation import prepare, binary_version, isolated_launch
import isolation_guard


class VersionIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT / '.runtime', prefix='version-')
        self.root, _ = prepare(Path(self.temp.name), sys.executable)

    def tearDown(self):
        self.temp.cleanup()

    def require_host_context(self):
        context = isolation_guard.environment()
        if context["state"] != "HOST_CONTEXT":
            self.skipTest("BLOCKED: isolation-required test needs normal host context")

    def test_system_version_works_in_private_namespace(self):
        self.require_host_context()
        self.assertTrue(binary_version(self.root, sys.executable).startswith('Python 3.14'))

    def test_installed_npm_codex_version_works_in_private_namespace(self):
        self.require_host_context()
        binary = shutil.which('codex')
        if not binary:
            self.skipTest('Codex CLI is not installed')
        self.assertRegex(binary_version(self.root, binary), r'^codex-cli \S+')

    def test_probe_environment_and_no_auth_are_mandatory(self):
        with patch('isolation.require_isolation_creation'), patch('isolation.subprocess.run') as run:
            run.return_value.stdout = 'fixture version\n'
            self.assertEqual(binary_version(self.root, sys.executable), 'fixture version')
            argv = run.call_args.args[0]
            env = run.call_args.kwargs['env']
            self.assertIn('--net', argv)
            self.assertIn('no-auth', argv)
            self.assertEqual(env['HOME'], str(self.root / 'codex-home'))
            self.assertEqual(env['CODEX_HOME'], env['HOME'])
            self.assertNotIn('OPENAI_API_KEY', env)
        with patch('isolation.require_isolation_creation'), \
                patch('isolation.subprocess.run', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError): binary_version(self.root, sys.executable)

    def test_no_auth_cannot_launch_installed_codex_other_commands(self):
        fake = Path('/opt/fixture/codex')
        with patch('isolation.require_isolation_creation'), patch('isolation.shutil.which', return_value=str(fake)):
            argv, env = isolated_launch(self.root, str(fake), ['--version'], authenticate=False)
            self.assertIn('--net', argv)
            with self.assertRaises(ValueError):
                isolated_launch(self.root, str(fake), ['app-server'], authenticate=False)

    def test_launch_passes_the_canonical_installed_binary_to_entry(self):
        binary = shutil.which('codex')
        if not binary:
            self.skipTest('Codex CLI is not installed')
        canonical = str(Path(binary).resolve(strict=True))
        node = shutil.which('node')
        which = lambda name: {'codex': binary, 'node': node}.get(name)
        if Path(canonical).name == 'codex.js' and not node:
            with patch('isolation.require_isolation_creation'), \
                    patch('isolation.shutil.which', side_effect=which):
                with self.assertRaisesRegex(ValueError, 'Node.js runtime'):
                    isolated_launch(self.root, binary, ['--version'], authenticate=False)
            return
        with patch('isolation.require_isolation_creation'), \
                patch('isolation.shutil.which', side_effect=which):
            argv, env = isolated_launch(self.root, binary, ['--version'], authenticate=False)
        entry_index = argv.index(str(Path(__file__).resolve().parents[1] / 'src/isolated_entry.py'))
        self.assertEqual(argv[entry_index + 4], canonical)
        if Path(canonical).name == 'codex.js':
            self.assertEqual(env['ACEIS_CODEX_NODE'], str(Path(node).resolve(strict=True)))
        else:
            self.assertNotIn('ACEIS_CODEX_NODE', env)

    def test_private_namespace_hides_global_config_and_socket(self):
        self.require_host_context()
        code = ('from pathlib import Path;import os;'
                'assert not Path(os.environ["CODEX_HOME"]+"/config.toml").exists();'
                'assert not Path(' + repr(str(Path.home() / '.codex/config.toml')) + ').exists();'
                'assert not Path("/run/user").exists();print("isolated")')
        argv, env = isolated_launch(self.root, sys.executable, ['-B', '-c', code], authenticate=False)
        env['HOME'] = env['CODEX_HOME']
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'isolated')

    def test_synthetic_auth_is_readonly_and_host_canaries_are_hidden(self):
        self.require_host_context()
        with tempfile.TemporaryDirectory(prefix='outer-isolation-canary-') as temp:
            home = Path(temp) / 'synthetic-home'
            codex_dir = home / '.codex'
            codex_dir.mkdir(parents=True)
            auth_path = codex_dir / 'auth.json'
            canary = 'SYNTHETIC_AUTH_CANARY_DO_NOT_USE'
            auth_path.write_text(canary)
            secret = home / '.ssh/id_ed25519'
            secret.parent.mkdir()
            secret.write_text('SYNTHETIC_HOST_SECRET_CANARY')
            socket_path = Path(temp) / 'daemon.sock'
            with socket.socket(socket.AF_UNIX) as host_socket:
                try:
                    host_socket.bind(str(socket_path))
                except PermissionError:
                    self.skipTest("Unix socket positive control is blocked by the host execution policy")
                self.assertTrue(socket_path.exists())
                code = ('from pathlib import Path;import os;'
                        'auth=Path(os.environ["CODEX_HOME"]+"/auth.json");'
                        f'canary={canary!r};'
                        'assert auth.read_text()==canary;'
                        'try_write=False\n'
                        'try:\n auth.write_text("changed")\n try_write=True\n'
                        'except OSError:\n pass\n'
                        'assert not try_write;assert auth.read_text()==canary;'
                        f'assert not Path({str(secret)!r}).exists();'
                        f'assert not Path({str(socket_path)!r}).exists();'
                        'assert not Path("/run/user").exists();print("canaries isolated")')
                compile(code, '<synthetic isolation canary>', 'exec')
                with patch.dict(os.environ, {'HOME': str(home)}):
                    argv, env = isolated_launch(self.root, sys.executable, ['-B', '-c', code], authenticate=True)
                    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'canaries isolated')
                self.assertEqual(auth_path.read_text(), canary)
