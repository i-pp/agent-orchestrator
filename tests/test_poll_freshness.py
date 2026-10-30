"""Offline poll-age regression: no live service, socket bind, key, or model."""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT / 'scripts'), str(PROJECT / 'src'), str(PROJECT / 'tests')]
import service_wait as wait
import poc_services as services
import poc_login as login
import test_grant_services as fixtures

NOW = 1800000000.0


def report(timestamp=NOW):
    return {'result': 'ok', 'healthz': {'ok': True}, 'readyz': {'ok': True},
            'control_plane_poll': {'ok': True, 'value': timestamp}}


class PollFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='poll-freshness-')
        self.addCleanup(self.temp.cleanup)
        self.url = Path(self.temp.name) / 'health-url'
        self.url.write_text('http://127.0.0.1:12345')
        self.url.chmod(0o600)

    def probe(self, value, now=NOW, code=0):
        result = SimpleNamespace(returncode=code, stdout=json.dumps(value))
        with patch.object(wait.subprocess, 'run', return_value=result), patch.object(wait.time, 'time', return_value=now):
            return wait.tunnel_ready(Path('/usr/bin/true'), self.url)

    def test_observed_incident_stale_success_with_all_green_flags_is_rejected(self):
        # Incident's last-success Unix seconds, evaluated ten minutes later.
        self.assertFalse(self.probe(report(1793366942), now=1793367542))

    def test_recent_success_and_exact_ninety_second_boundary(self):
        for age in [0, 1, 45, 89.999, 90]:
            with self.subTest(age=age):
                self.assertTrue(self.probe(report(NOW - age)))
        self.assertFalse(self.probe(report(NOW - 90.001)))

    def test_missing_timestamp_does_not_fall_back_to_ok_flag(self):
        value = report()
        del value['control_plane_poll']['value']
        self.assertFalse(self.probe(value))

    def test_non_numeric_non_finite_or_non_positive_timestamp_is_rejected(self):
        for timestamp in [None, True, False, '1800000000', 0, -1, float('nan'),
                          float('inf'), float('-inf'), 10 ** 400, [], {}]:
            with self.subTest(timestamp=timestamp):
                self.assertFalse(self.probe(report(timestamp)))

    def test_future_timestamp_is_not_treated_as_fresh(self):
        for timestamp in [NOW + .001, NOW + 1, NOW * 1000]:
            with self.subTest(timestamp=timestamp):
                self.assertFalse(self.probe(report(timestamp)))

    def test_invalid_wall_clock_cannot_establish_freshness(self):
        for now in [None, True, 0, -1, float('inf'), float('nan'), 10 ** 400]:
            with self.subTest(now=now):
                self.assertFalse(self.probe(report(), now=now))

    def test_each_readiness_flag_is_still_required(self):
        for field in ['healthz', 'readyz', 'control_plane_poll']:
            for wrong in [False, 1, 'true', None]:
                value = report()
                value[field]['ok'] = wrong
                self.assertFalse(self.probe(value))
        value = report()
        value['result'] = 'failed'
        self.assertFalse(self.probe(value))

    def test_malformed_report_containers_fail_without_attribute_errors(self):
        for value in [None, [], True, 'ok']:
            self.assertFalse(self.probe(value))
        for field in ['healthz', 'readyz', 'control_plane_poll']:
            for wrong in [None, [], True, 'ok']:
                value = report()
                value[field] = wrong
                self.assertFalse(self.probe(value))

    def test_failed_health_command_does_not_parse_its_output(self):
        with patch.object(wait.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='not json')):
            self.assertFalse(wait.tunnel_ready(Path('/usr/bin/true'), self.url))

    def test_invalid_json_remains_a_rejectable_value_error(self):
        with patch.object(wait.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='not json')):
            with self.assertRaises(ValueError):
                wait.tunnel_ready(Path('/usr/bin/true'), self.url)

    def test_probe_keeps_fixed_command_timeout_and_excludes_secret_environment(self):
        result = SimpleNamespace(returncode=0, stdout=json.dumps(report()))
        with patch.object(wait.subprocess, 'run', return_value=result) as run, \
                patch.object(wait.time, 'time', return_value=NOW):
            self.assertTrue(wait.tunnel_ready(Path('/usr/bin/true'), self.url, timeout=.75))
            args, kwargs = run.call_args
            self.assertEqual(args[0], ['/usr/bin/true', 'health', '--url-file', str(self.url),
                                       '--require-control-plane-poll', '--json'])
            self.assertEqual(kwargs['timeout'], .75)
            self.assertEqual(set(kwargs['env']), {'PATH', 'HOME', 'LANG'})
            self.assertNotIn('shell', kwargs)

    def test_cli_stale_health_exits_at_deadline_without_restarting(self):
        args = ['service_wait.py', 'tunnel', '--client', '/usr/bin/true', '--url-file', str(self.url), '--timeout', '.2']
        result = SimpleNamespace(returncode=0, stdout=json.dumps(report(NOW - 600)))
        out, err = io.StringIO(), io.StringIO()
        started = time.monotonic()
        with patch.object(sys, 'argv', args), patch.object(wait.subprocess, 'run', return_value=result), \
                patch.object(wait.time, 'time', return_value=NOW), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(wait.main(), 1)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn('Readiness deadline exceeded: tunnel', err.getvalue())
        self.assertEqual(out.getvalue(), '')


class PollFreshnessServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.GrantServiceTests('test_default_off_and_legacy_mock_have_no_grant_argument')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service
        self.fixture.service.health.write_text('http://127.0.0.1:12345')
        self.fixture.service.health.chmod(0o600)
        self.result = SimpleNamespace(returncode=0, stdout=json.dumps(report(NOW - 600)))
        patches = [patch.object(services, 'daemon_ready', return_value=True),
                   patch.object(wait.subprocess, 'run', return_value=self.result),
                   patch.object(wait.time, 'time', return_value=NOW),
                   patch.object(self.service, 'start', side_effect=AssertionError('No automatic start')),
                   patch.object(self.service, 'stop', side_effect=AssertionError('No automatic stop'))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_existing_status_downgrades_to_not_ready_without_state_or_scope_mutation(self):
        before = {p.name: p.read_bytes() for p in self.fixture.root.iterdir() if p.is_file()}
        result = self.service.status()
        self.assertEqual(result['phase'], 'NOT_READY')
        self.assertTrue(result['daemon_ready'])
        self.assertFalse(result['tunnel_ready'])
        self.assertEqual(self.service.read_state()['phase'], 'READY')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.fixture.root.iterdir() if p.is_file()
                                  and p.name != 'poc-services.lock'})

    def test_fresh_success_recovers_readiness_and_aging_downgrades_it_again(self):
        self.result.stdout = json.dumps(report())
        self.assertEqual(self.service.status()['phase'], 'READY')
        with patch.object(wait.time, 'time', return_value=NOW + 91):
            self.assertEqual(self.service.status()['phase'], 'NOT_READY')

    def test_login_restore_does_not_restart_existing_stale_units(self):
        pin = login.snapshot(self.service)[2]
        before = (self.fixture.root / services.STATE).read_bytes()
        with self.assertRaisesRegex(login.LoginBlocked, 'EXISTING_UNIT_REQUIRES_RECONCILIATION'):
            login.restore(self.service, pin)
        self.assertEqual((self.fixture.root / services.STATE).read_bytes(), before)

    def test_invalid_health_json_and_health_timeout_are_not_ready(self):
        self.result.stdout = 'invalid json'
        self.assertEqual(self.service.status()['phase'], 'NOT_READY')
        with patch.object(wait.subprocess, 'run', side_effect=subprocess.TimeoutExpired('health', .2)):
            self.assertEqual(self.service.status()['phase'], 'NOT_READY')


if __name__ == '__main__':
    unittest.main()
