"""Dedicated desktop CLI and runtime boundaries, using only temporary fixtures."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from desktop_notifications import DesktopNotificationQueue
from runtime_scope import resolve_project_root, runtime_path

spec = importlib.util.spec_from_file_location(
    'desktop_cli', PROJECT / 'scripts/desktop_notifications_worker.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class DesktopWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / 'project'
        (self.project / 'src').mkdir(parents=True)
        (self.project / 'src' / 'daemon.py').write_text('# fixture\n')
        (self.project / '.runtime').mkdir(mode=0o700)
        self.runtime = self.project / '.runtime' / 'fixture'
        self.runtime.mkdir(mode=0o700)
        (self.runtime / 'logs').mkdir(mode=0o700)
        self.source = self.runtime / 'codex-pipeline.sqlite3'
        self.destination = self.runtime / 'desktop-notifications.sqlite3'
        with sqlite3.connect(self.source) as db:
            db.execute('CREATE TABLE ledger (id INTEGER PRIMARY KEY,body TEXT)')
            db.execute('INSERT INTO ledger VALUES (1,?)', (json.dumps({'events': []}),))
        os.chmod(self.source, 0o600)
        self.argv = ['worker', '--runtime', str(self.runtime),
                     '--runtime-project-root', str(self.project)]

    def call(self, *extra):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', self.argv + list(extra)), \
                redirect_stdout(output), redirect_stderr(errors):
            result = cli.main()
        return result, output.getvalue(), errors.getvalue()

    def test_status_does_not_create_a_queue_or_sender(self):
        with patch.object(cli, 'FreedesktopSender') as sender:
            result, _, error = self.call('--status')
        self.assertEqual(result, 1)
        self.assertIn('DESKTOP_CONFIGURATION_INVALID', error)
        self.assertFalse(self.destination.exists())
        sender.assert_not_called()

    def test_existing_status_is_read_only_without_delivery_or_baseline_change(self):
        queue = DesktopNotificationQueue(self.source, self.destination)
        queue.initialize()
        before = {path: path.read_bytes() for path in [self.source, self.destination]}
        with patch.object(cli, 'FreedesktopSender') as sender, \
                patch.object(cli.DesktopNotificationQueue, 'initialize') as initialize:
            result, output, error = self.call('--status')
        self.assertEqual((result, error), (0, ''))
        self.assertEqual(json.loads(output)['cursor'], 0)
        self.assertFalse(json.loads(output)['chat_wake'])
        sender.assert_not_called()
        initialize.assert_not_called()
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_private_explicit_runtime_does_not_allow_another_project(self):
        self.assertEqual(resolve_project_root(self.project), self.project)
        self.assertEqual(runtime_path(self.runtime, project_root=self.project), self.runtime)
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir(mode=0o700)
        (outside / 'logs').mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, 'inside the selected project'):
            runtime_path(outside, project_root=self.project)

    def test_symlinks_and_non_private_runtime_anchors_are_rejected(self):
        linked = self.project / '.runtime' / 'linked'
        linked.symlink_to(self.runtime, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Unsafe runtime ancestry'):
            runtime_path(linked, project_root=self.project)
        os.chmod(self.project / '.runtime', 0o750)
        with self.assertRaises(ValueError):
            resolve_project_root(self.project)
        os.chmod(self.project / '.runtime', 0o700)
        os.chmod(self.runtime / 'logs', 0o750)
        with self.assertRaises(ValueError):
            runtime_path(self.runtime, project_root=self.project)

    def test_run_establishes_baseline_and_exits_without_webhook_or_task_work(self):
        stopped = threading.Event()
        stopped.set()
        before = self.source.read_bytes()
        with patch.object(cli.threading, 'Event', return_value=stopped), \
                patch.object(cli.signal, 'signal'), \
                patch.object(cli, 'FreedesktopSender', return_value=Mock()) as sender:
            result, output, error = self.call()
        self.assertEqual((result, error), (0, ''))
        self.assertIn('Desktop worker stopped', output)
        sender.return_value.assert_not_called()
        self.assertEqual(before, self.source.read_bytes())
        self.assertEqual(DesktopNotificationQueue.read_summary(self.destination)['cursor'], 0)


if __name__ == '__main__':
    unittest.main()
