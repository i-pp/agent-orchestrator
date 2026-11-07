"""Local delivery boundaries: recovery, deduplication and uncertain results."""
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from desktop_notifications import DesktopNotificationQueue, FreedesktopSender
from notification_outbox import NotificationError

TASK = '00000000-0000-4000-8000-000000000999'


class DesktopNotificationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'pipeline.sqlite3'
        self.destination = self.root / 'desktop.sqlite3'
        self.now = [1_800_000_000.0]
        self.events = []
        self.write()
        self.queue = self.make_queue()
        self.sender = Mock(return_value={'status': 'DELIVERED', 'desktop_id': 7})

    def tearDown(self):
        self.tmp.cleanup()

    def make_queue(self):
        return DesktopNotificationQueue(self.source, self.destination, clock=lambda: self.now[0])

    def write(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY, body TEXT)')
            db.execute('INSERT OR REPLACE INTO ledger VALUES (1,?)',
                (json.dumps({'events': self.events, 'tasks': {TASK: {'prompt': 'SECRET_PROMPT'}}}),))
            db.commit()
        os.chmod(self.source, 0o600)

    def append(self, kind, **extra):
        self.events.append({'seq': len(self.events) + 1, 'kind': kind, 'task_id': TASK,
                            'time': self.now[0], 'log': 'SECRET_LOG', **extra})
        self.write()

    def test_baseline_skips_history_then_all_five_states_are_sanitized(self):
        self.append('task_completed')
        self.queue.run_once(self.sender)
        self.sender.assert_not_called()
        for kind in ('task_completed', 'task_failed', 'approval_waiting', 'task_cancelled', 'task_blocked'):
            self.append(kind, reason_code='RESUME_REJECTED')
        before = self.source.read_bytes()
        result = self.queue.run_once(self.sender)
        self.assertEqual(result['delivery_states'], {'DELIVERED': 5})
        self.assertEqual({call.args[0]['data']['state'] for call in self.sender.call_args_list},
                         {'COMPLETED', 'FAILED', 'WAITING_APPROVAL', 'CANCELLED', 'BLOCKED'})
        self.assertNotIn('SECRET', str(self.sender.call_args_list))
        self.assertNotIn(b'SECRET', self.destination.read_bytes())
        self.assertEqual(before, self.source.read_bytes())

    def test_restart_recovers_missed_events_and_repeated_poll_does_not_duplicate(self):
        self.queue.run_once(self.sender)
        for _ in range(8):
            self.append('task_completed')
        self.make_queue().run_once(self.sender)
        self.make_queue().run_once(self.sender)
        self.assertEqual(self.sender.call_count, 8)
        self.assertEqual(self.make_queue().summary()['delivery_states'], {'DELIVERED': 8})

    def test_known_not_sent_failure_retries_with_backoff_across_restart(self):
        self.queue.run_once(self.sender)
        self.append('task_failed')
        down = Mock(return_value={'status': 'RETRY'})
        self.queue.run_once(down)
        self.queue.run_once(down)
        self.assertEqual(down.call_count, 1)
        self.now[0] += 1
        self.make_queue().run_once(down)
        self.now[0] += 1
        self.make_queue().run_once(self.sender)
        self.sender.assert_not_called()
        self.now[0] += 1
        result = self.make_queue().run_once(self.sender)
        self.assertEqual(result['delivery_states'], {'DELIVERED': 1})
        self.assertEqual(result['last_latency_ms'], 3000)

    def test_uncertain_delivery_is_not_resent_or_reexecuted(self):
        self.queue.run_once(self.sender)
        self.append('task_completed')
        uncertain = Mock(side_effect=TimeoutError())
        self.queue.run_once(uncertain)
        self.now[0] += 1000
        self.make_queue().run_once(self.sender)
        self.sender.assert_not_called()
        self.assertEqual(self.queue.summary()['delivery_states'], {'UNKNOWN': 1})

    def test_interrupted_send_is_recovered_as_unknown(self):
        self.queue.run_once(self.sender)
        self.append('task_cancelled')
        self.queue._sync()
        with closing(sqlite3.connect(self.destination)) as db:
            db.execute("UPDATE desktop_deliveries SET status='SENDING'")
            db.commit()
        self.make_queue().run_once(self.sender)
        self.sender.assert_not_called()
        self.assertEqual(self.queue.summary()['delivery_states'], {'UNKNOWN': 1})

    def test_cursor_regression_does_not_lose_existing_queue(self):
        self.queue.run_once(self.sender)
        self.append('task_completed')
        self.queue.run_once(self.sender)
        self.events = []
        self.write()
        with self.assertRaisesRegex(NotificationError, 'DESKTOP_SOURCE_CHANGED'):
            self.queue.run_once(self.sender)
        self.assertEqual(self.queue.summary()['delivery_states'], {'DELIVERED': 1})

    def test_sender_distinguishes_preflight_failure_from_unknown_notify_result(self):
        payload = {'data': {'task_id': TASK, 'state': 'COMPLETED'}, 'timestamp': '2026-11-06T00:00:00Z'}
        run = Mock(side_effect=subprocess.TimeoutExpired('gdbus', 5))
        self.assertEqual(FreedesktopSender(run=run)(payload)['status'], 'RETRY')
        ready = subprocess.CompletedProcess([], 0, '(server,)', '')
        run = Mock(side_effect=[ready, subprocess.TimeoutExpired('gdbus', 5)])
        self.assertEqual(FreedesktopSender(run=run)(payload)['status'], 'UNKNOWN')
        run = Mock(side_effect=[ready, subprocess.CompletedProcess([], 0, '(uint32 42,)\n', '')])
        self.assertEqual(FreedesktopSender(run=run)(payload), {'status': 'DELIVERED', 'desktop_id': 42})
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('--method') + 1], 'org.freedesktop.Notifications.Notify')

    def test_status_is_read_only_and_does_not_create_a_missing_queue(self):
        self.queue.initialize()
        before = self.destination.read_bytes()
        self.assertEqual(DesktopNotificationQueue.read_summary(self.destination)['cursor'], 0)
        self.assertEqual(before, self.destination.read_bytes())
        missing = self.root / 'missing.sqlite3'
        with self.assertRaises((OSError, NotificationError)):
            DesktopNotificationQueue.read_summary(missing)
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
