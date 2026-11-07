"""HostGateway worker wiring tests using in-memory worker doubles only."""
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))

from codex_host import HostGateway


class Worker:
    def __init__(self, source_path):
        self.outbox = type('Outbox', (), {'source_path': Path(source_path)})()
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        return True

    def stop(self, *, timeout=2.0):
        self.stopped = True
        return True


class Pipeline:
    def __init__(self, path, worker_check=None):
        self.db = Path(path)
        self.worker_check = worker_check

    def _dispatch(self):
        if self.worker_check is not None:
            self.worker_check()
        return {'status': 'DISPATCH_CHECKED'}


class NotificationHostBridgeTests(unittest.TestCase):
    def _gateway(self, *, enabled, factory, pipeline):
        gateway = object.__new__(HostGateway)
        gateway.pipeline = pipeline
        gateway.root = pipeline.db.parent
        gateway.failed = False
        gateway.release_idle = lambda: None
        gateway.notification_worker_factory = factory
        gateway.allow_external_notifications = enabled
        gateway.notification_worker = None
        gateway.notification_worker_started = False
        gateway.notification_worker_status = {'status': 'OFF'}
        return gateway

    def test_worker_starts_before_pipeline_dispatch_and_stops_on_close(self):
        with tempfile.TemporaryDirectory(prefix='notification-host-') as temp:
            source = Path(temp) / 'pipeline.sqlite3'
            made = []
            worker = Worker(source)
            pipeline = Pipeline(source, worker_check=lambda: self.assertTrue(worker.started))
            gateway = self._gateway(enabled=True,
                factory=lambda current: made.append(current) or worker, pipeline=pipeline)
            self.assertEqual(gateway.pump(), {'status': 'DISPATCH_CHECKED'})
            self.assertEqual(len(made), 1)
            self.assertEqual(gateway.notification_worker_status['status'], 'RUNNING')
            self.assertEqual(gateway._stop_notification_worker()['status'], 'STOPPED')
            self.assertTrue(worker.stopped)

    def test_default_off_does_not_construct_worker_or_block_pipeline(self):
        with tempfile.TemporaryDirectory(prefix='notification-host-off-') as temp:
            called = []
            pipeline = Pipeline(Path(temp) / 'pipeline.sqlite3')
            gateway = self._gateway(enabled=False,
                factory=lambda current: called.append(current), pipeline=pipeline)
            self.assertEqual(gateway.pump(), {'status': 'DISPATCH_CHECKED'})
            self.assertEqual(called, [])
            self.assertEqual(gateway.notification_worker_status['status'], 'DISABLED')

    def test_worker_source_must_be_the_same_pipeline_ledger(self):
        worker = Worker('/tmp/wrong-ledger.sqlite3')
        gateway = self._gateway(enabled=True, factory=lambda _: worker,
            pipeline=Pipeline('/tmp/pipeline.sqlite3'))
        self.assertEqual(gateway._start_notification_worker()['status'], 'UNAVAILABLE')
        self.assertFalse(worker.started)


if __name__ == '__main__':
    unittest.main()
