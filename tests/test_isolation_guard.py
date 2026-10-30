"""Offline guard tests: no namespace creation, models or authentication."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
import isolation_guard as guard
import isolation
import development_poc
import verification
import transport
import runner
import mcp_adapter

SANDBOX = {'state': 'SANDBOXED', 'signals': ['BWRAP_PROFILE']}
UNKNOWN = {'state': 'UNKNOWN', 'signals': ['INSUFFICIENT_ENVIRONMENT_EVIDENCE']}
HOST = {'state': 'HOST_CONTEXT', 'signals': ['HOST_PROCFS_OBSERVATIONS']}


class DetectionTests(unittest.TestCase):
    def host(self, **changes):
        return dict(label='unconfined', init='systemd', uid_map='0 0 4294967295',
                    status='NSpid:\t123\nNoNewPrivs:\t0\nSeccomp:\t0', **changes)

    def test_host_requires_combined_evidence(self):
        self.assertEqual(guard.classify(self.host())['state'], 'HOST_CONTEXT')
        for field in self.host():
            case = self.host(); case[field] = None
            self.assertEqual(guard.classify(case)['state'], 'UNKNOWN', field)

    def test_independent_sandbox_signals(self):
        for field, value in [('label', 'bwrap//&unpriv_bwrap (enforce)'),
                             ('init', 'codex'), ('uid_map', '0 1000 1'),
                             ('status', 'NSpid:\t123\t4')]:
            case = self.host(); case[field] = value
            self.assertEqual(guard.classify(case)['state'], 'SANDBOXED', field)

    def test_unfamiliar_or_conflicting_context_is_unknown(self):
        for field, value in [('label', 'other-profile (enforce)'), ('init', 'python'),
                             ('uid_map', 'malformed'), ('status', 'Seccomp:\t2'),
                             ('status', 'NSpid:\tbad\nNoNewPrivs:\t0\nSeccomp:\t0')]:
            case = self.host(); case[field] = value
            self.assertEqual(guard.classify(case)['state'], 'UNKNOWN', field)

    def test_procfs_read_failure_is_unknown_without_launch(self):
        with patch.object(Path, 'read_text', side_effect=PermissionError), \
                patch('subprocess.Popen') as popen, patch('subprocess.run') as run:
            self.assertEqual(guard.environment()['state'], 'UNKNOWN')
            popen.assert_not_called(); run.assert_not_called()


class LaunchTests(unittest.TestCase):
    def test_all_owned_launchers_refuse_before_any_subprocess(self):
        calls = [lambda: isolation.launch('/missing', '/usr/bin/true', []),
                 lambda: isolation.isolated_launch('/missing', '/usr/bin/true', []),
                 lambda: isolation.sandbox_preflight('/missing'),
                 lambda: isolation.isolated_preflight('/missing'),
                 lambda: isolation.binary_version('/missing', '/usr/bin/true'),
                 lambda: isolation.identity('/missing', '/usr/bin/true'),
                 lambda: verification.verification_launch('/missing', ['/usr/bin/true']),
                 lambda: development_poc.launch('/missing', '/missing', '/usr/bin/true', []),
                 lambda: development_poc.version('/missing', '/missing', '/usr/bin/true', 1)]
        for context, code in [(SANDBOX, 'NESTED_ISOLATION_FORBIDDEN'),
                              (UNKNOWN, 'ISOLATION_CONTEXT_UNKNOWN')]:
            for index, call in enumerate(calls):
                with self.subTest(state=context['state'], entry=index), \
                        patch.object(guard, 'environment', return_value=context), \
                        patch('subprocess.Popen') as popen, patch('subprocess.run') as run:
                    with self.assertRaises(guard.IsolationBlocked) as caught:
                        call()
                    self.assertEqual(caught.exception.evidence['reason_code'], code)
                    self.assertIs(caught.exception.evidence['executed'], False)
                    popen.assert_not_called(); run.assert_not_called()

    def test_direct_namespace_tools_refused_before_resources_wrap(self):
        for binary in ['/usr/bin/bwrap', '/usr/bin/unshare', '/usr/bin/nsjail', '/usr/bin/firejail']:
            with self.subTest(binary=binary), patch.object(guard, 'environment', return_value=SANDBOX), \
                    patch('subprocess.Popen') as popen:
                with self.assertRaises(guard.IsolationBlocked):
                    transport.Process([binary, '--version'], cwd='/', env={})
                popen.assert_not_called()

    def test_normal_commands_do_not_need_environment_decision(self):
        with patch.object(guard, 'environment', side_effect=AssertionError('unexpected detection')):
            guard.check_command(['/usr/bin/python3', '-B', '-c', 'print(1)'])
            guard.check_command(['/usr/bin/true'])

    def test_host_only_builds_existing_isolation_argv_without_execution(self):
        with patch.object(guard, 'environment', return_value=HOST), patch('subprocess.Popen') as popen:
            argv, _ = isolation.isolated_launch('/tmp/fixture', '/usr/bin/true', [], authenticate=False)
            self.assertEqual(argv[0], '/usr/bin/unshare')
            self.assertIn('--net', argv)
            argv, _ = verification.verification_launch('/tmp/fixture', ['/usr/bin/true'])
            self.assertEqual(Path(argv[0]).name, 'bwrap')
            popen.assert_not_called()


class TaskTests(unittest.TestCase):
    def setUp(self):
        (PROJECT / '.runtime').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT / '.runtime', prefix='guard-')
        self.root = Path(self.temp.name)
        self.runners = []

    def tearDown(self):
        for instance in self.runners:
            instance.store.close()
        self.temp.cleanup()

    def instance(self, fake=False):
        def fake_launcher(root, binary, args):
            return [sys.executable, str(PROJECT / 'tests/fake_codex.py'), 'normal',
                    str(root / 'child.pid')], {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        value = runner.Runner(self.root, sys.executable, **({'launcher': fake_launcher} if fake else {}))
        self.runners.append(value)
        return value

    def test_execution_block_is_durable_not_failed_and_not_dispatched(self):
        instance = self.instance()
        task_id = instance.store.submit('guard fixture')
        with patch.object(guard, 'environment', return_value=SANDBOX), \
                patch('subprocess.Popen') as popen, patch('subprocess.run') as run:
            task = instance.run(task_id, 3)
            popen.assert_not_called(); run.assert_not_called()
        self.assertEqual(task['state'], 'WAITING_FOR_HUMAN')
        self.assertEqual(task['codex_status'], 'BLOCKED')
        self.assertEqual(task['verification_status'], 'BLOCKED')
        block = json.loads(task['error'])
        self.assertEqual(block['quality_verdict'], 'UNASSESSED')
        execution = instance.store.execution(task['active_execution_id'])
        self.assertEqual(execution['dispatched'], 0)
        self.assertEqual(execution['launch_pending'], 0)
        self.assertIsNone(execution['agent_outcome'])
        self.assertIsNone(execution['turn_id'])
        with self.assertRaises(ValueError): instance.run(task_id, 3)
        with self.assertRaises(ValueError): instance.review(task_id, True, 'tester', 'cannot approve unexecuted task')
        view = mcp_adapter.task_view(task)
        self.assertEqual(view['codex_status'], 'BLOCKED')
        self.assertEqual(view['blocking_reason'], 'NESTED_ISOLATION_FORBIDDEN')
        self.assertNotIn('error', view)

    def test_unknown_context_uses_distinct_machine_reason(self):
        instance = self.instance()
        with patch.object(guard, 'environment', return_value=UNKNOWN), patch('subprocess.Popen') as popen:
            task = instance.run(instance.store.submit('unknown fixture'), 3)
            popen.assert_not_called()
        self.assertEqual(task['state'], 'WAITING_FOR_HUMAN')
        self.assertEqual(json.loads(task['error'])['reason_code'], 'ISOLATION_CONTEXT_UNKNOWN')

    def test_verification_block_preserves_successful_fake_peer_result(self):
        instance = self.instance(fake=True)
        registry = {'smoke': {'argv': ['/usr/bin/true'], 'timeout': 1}}
        registry_file = self.root / 'commands.json'
        registry_file.write_text(json.dumps(registry))
        with patch.object(verification, 'REGISTRY', registry_file), \
                patch.object(runner, 'binary_version', return_value='offline CPython fixture'), \
                patch.object(guard, 'environment', return_value=SANDBOX), \
                patch.object(verification, 'Process') as verification_process:
            task = instance.run(instance.store.submit('offline verification', acceptance={'tests': ['smoke']}), 3)
            verification_process.assert_not_called()
        self.assertEqual(task['state'], 'WAITING_FOR_HUMAN')
        self.assertEqual(task['codex_status'], 'SUCCEEDED')
        self.assertEqual(task['verification_status'], 'BLOCKED')
        self.assertEqual(json.loads(task['error'])['reason_code'], 'NESTED_ISOLATION_FORBIDDEN')
        row = instance.store.db.execute('SELECT status,evidence FROM verifications WHERE task_id=?', (task['id'],)).fetchone()
        evidence = json.loads(row['evidence'])
        self.assertEqual(row['status'], 'BLOCKED')
        self.assertIsNone(evidence['tests'][0]['passed'])
        self.assertIs(evidence['tests'][0]['executed'], False)
        self.assertEqual(evidence['quality_verdict'], 'UNASSESSED')
        self.assertFalse(evidence['all_passed'])
        with self.assertRaises(ValueError): instance.review(task['id'], True, 'tester', 'no fallback')
        # A blocked evaluation does not hold up the next independent task.
        (self.root / 'workspace/ordinary.txt').write_text('ordinary artifact')
        with patch.object(runner, 'binary_version', return_value='offline CPython fixture'), \
                patch.object(guard, 'environment', return_value=SANDBOX):
            ordinary = instance.run(instance.store.submit('independent ordinary check', acceptance={
                'artifacts': [{'path': 'ordinary.txt'}]}), 3)
        self.assertEqual(ordinary['state'], 'COMPLETED')

    def test_independent_ordinary_task_still_completes(self):
        instance = self.instance(fake=True)
        (self.root / 'workspace/fixture.txt').write_text('existing artifact')
        with patch.object(runner, 'binary_version', return_value='offline CPython fixture'), \
                patch.object(guard, 'environment', return_value=SANDBOX):
            task = instance.run(instance.store.submit('offline artifact check', acceptance={
                'artifacts': [{'path': 'fixture.txt'}],
                'conditions': [{'kind': 'result_equals', 'value': 'mock result'}]}), 3)
        self.assertEqual(task['state'], 'COMPLETED')
        self.assertEqual(task['verification_status'], 'PASSED')
        self.assertEqual(json.loads(task['cleanup'])['survivors'], [])


if __name__ == '__main__':
    unittest.main()
