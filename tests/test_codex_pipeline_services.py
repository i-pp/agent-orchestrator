"""Service/login wiring with fake host metadata and mocked service managers only."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shlex
import sys
import types
import unittest
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT/'src'), str(PROJECT/'scripts'), str(PROJECT/'tests')]
import poc_services as module
import poc_login as login
import test_grant_services as fixtures
from poc_services import Services, Refused, STATE

SNAPSHOT = dict(zip(('executor_policy_sha256', 'pipeline_policy_sha256',
                     'common_policy_sha256', 'binary_sha256'), ('a'*64, 'b'*64, 'c'*64, 'd'*64)))


class PipelineServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.GrantServiceTests('test_default_off_and_legacy_mock_have_no_grant_argument')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.key = self.fixture.root, self.fixture.key
        self.host = types.ModuleType('codex_host')
        self.host.executor_snapshot = Mock(return_value=dict(SNAPSHOT))
        self.host.pipeline_held = Mock(return_value=False)
        mocked = patch.dict(sys.modules, {'codex_host': self.host})
        mocked.start(); self.addCleanup(mocked.stop)
        # Deployment-local tunnel profile is intentionally absent in this
        # isolated worktree. Only its existence check is a fixture here.
        is_file = Path.is_file
        profile = PROJECT/'systemd/aceis-mcp-tunnel.yaml'
        available = patch.object(Path, 'is_file', autospec=True,
                                 side_effect=lambda path: path == profile or is_file(path))
        available.start(); self.addCleanup(available.stop)
        self.service = self.fixture.service

    def enabled(self):
        return Services(self.root, Path('/usr/bin/true'), self.key, True, True, True, True,
                        allow_codex_pipeline=True)

    @staticmethod
    def adapter(service):
        argument = next(v for v in service.commands()['tunnel'] if v.startswith('--mcp.command='))
        return shlex.split(argument.split('command=', 2)[-1])

    def enable_state(self, phase='READY'):
        self.fixture.state.update(codex_pipeline_enabled=True,
                                  codex_pipeline_snapshot=dict(SNAPSHOT), phase=phase)
        self.fixture.save()

    def absent(self):
        self.fixture.props = {role: {'LoadState': 'not-found', 'ActiveState': 'inactive'}
                              for role in self.service.units}

    def test_default_off_preserves_eleven_tools_and_no_executor_snapshot(self):
        self.assertFalse(self.service.plan()['codex_pipeline_enabled'])
        self.assertNotIn('--allow-codex-pipeline', self.service.commands()['daemon'])
        self.assertNotIn('--allow-codex-pipeline', self.adapter(self.service))
        checked = self.service.preflight(self.key)
        self.assertEqual(len(checked['tool_names']), 11)
        self.assertFalse(checked['codex_pipeline_enabled'])
        self.assertIsNone(checked['codex_pipeline_snapshot'])
        self.host.executor_snapshot.assert_not_called()
        del self.service.allow_codex_pipeline
        self.assertNotIn('--allow-codex-pipeline', self.service.commands()['daemon'])
        self.assertFalse(self.service.plan()['codex_pipeline_enabled'])

    def test_enabled_flag_reaches_both_daemon_and_adapter_once(self):
        service = self.enabled()
        self.assertEqual(service.commands()['daemon'].count('--allow-codex-pipeline'), 1)
        self.assertEqual(self.adapter(service).count('--allow-codex-pipeline'), 1)
        self.assertLess(service.commands()['daemon'].index('--allow-codex-pipeline'),
                        service.commands()['daemon'].index('daemon'))
        self.assertNotIn('--allow-writes', self.adapter(service))
        checked = service.preflight(self.key)
        self.assertTrue(checked['codex_pipeline_enabled'])
        self.assertEqual(checked['codex_pipeline_snapshot'], SNAPSHOT)
        self.assertEqual(len(checked['tool_names']), 23)
        self.host.executor_snapshot.assert_called_with(self.root, Path('/usr/bin/true'))
        self.assertEqual(service.plan()['codex_pipeline_snapshot'], SNAPSHOT)
        self.assertFalse((self.root/'codex-pipeline.sqlite3').exists())

    def test_pipeline_preflight_checks_both_command_paths(self):
        service = self.enabled()
        for role in ('daemon', 'tunnel'):
            command = service.commands()
            command[role] = [v.replace(' --allow-codex-pipeline', '')
                             for v in command[role] if v != '--allow-codex-pipeline']
            with self.subTest(role=role), patch.object(service, 'commands', return_value=command):
                with self.assertRaisesRegex(Refused, 'capability argument'):
                    service.preflight(self.key)

    def test_invalid_pipeline_flag_and_snapshot_are_rejected(self):
        for value in [1, 'true', None]:
            with self.subTest(flag=value), self.assertRaises(Refused):
                Services(self.root, Path('/usr/bin/true'), self.key, allow_codex_pipeline=value)
        service = self.enabled()
        for value in [{}, {**SNAPSHOT, 'extra': 'f'*64}, {**SNAPSHOT, 'binary_sha256': 'bad'}]:
            self.host.executor_snapshot.return_value = value
            with self.subTest(snapshot=value), self.assertRaisesRegex(Refused, 'snapshot'):
                service.preflight(self.key)

    def test_enabled_state_is_saved_with_snapshot_during_mocked_start(self):
        self.fixture.state['phase'] = 'STOPPED'; self.fixture.save(); self.absent()
        service = self.enabled()
        with patch.object(module, 'endpoint', side_effect=FileNotFoundError), \
             patch.object(Services, 'launch') as launch, patch.object(Services, 'wait_ready'):
            self.assertEqual(service.start()['phase'], 'READY')
        saved = service.read_state()
        self.assertTrue(saved['codex_pipeline_enabled'])
        self.assertEqual(saved['codex_pipeline_snapshot'], SNAPSHOT)
        self.assertEqual(saved['credential_file_reference'], str(self.key))
        self.assertEqual(saved['codex_file_reference'], '/usr/bin/true')
        self.assertEqual(launch.call_count, 2)

    def test_old_states_default_off_and_malformed_pipeline_records_refused(self):
        self.assertNotIn('--allow-codex-pipeline', self.service.recovery_plan()['commands']['start'])
        for changed in [{'codex_pipeline_enabled': 'true'}, {'codex_pipeline_enabled': True},
                        {'codex_pipeline_enabled': True, 'codex_pipeline_snapshot': {}},
                        {'codex_pipeline_enabled': True, 'codex_pipeline_snapshot': {**SNAPSHOT, 'binary_sha256': 1}}]:
            self.fixture.state.update(changed); self.fixture.save()
            with self.subTest(changed=changed), self.assertRaisesRegex(Refused, 'invalid'):
                self.service.read_state()
            self.fixture.state.pop('codex_pipeline_enabled', None)
            self.fixture.state.pop('codex_pipeline_snapshot', None)

    def test_recovery_preserves_pipeline_flags_and_saved_references(self):
        self.enable_state()
        before = (self.root/STATE).read_bytes()
        result = self.service.recovery_plan()
        self.assertEqual(len(result['preflight']['tool_names']), 23)
        for op in ('start', 'preflight'):
            argv = result['commands'][op]
            for flag in ['--allow-local-llm', '--allow-local-models', '--allow-chat-mailbox',
                         '--allow-mailbox-grants', '--allow-codex-pipeline']:
                self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index('--key-file')+1], str(self.key))
            self.assertEqual(argv[argv.index('--codex')+1], '/usr/bin/true')
        self.assertEqual(result['preflight']['codex_pipeline_snapshot'], SNAPSHOT)
        self.assertEqual(before, (self.root/STATE).read_bytes())
        self.assertFalse(result['automatic_apply'])

    def test_pipeline_rollback_removes_only_pipeline_and_keeps_eleven(self):
        self.enable_state()
        before = (self.root/STATE).read_bytes()
        result = self.service.recovery_plan(rollback_pipeline=True)
        self.assertEqual(result['operation'], 'pipeline-rollback-plan')
        self.assertEqual(len(result['preflight']['tool_names']), 11)
        argv = result['commands']['start']
        self.assertNotIn('--allow-codex-pipeline', argv)
        for flag in ['--allow-local-llm', '--allow-local-models', '--allow-chat-mailbox', '--allow-mailbox-grants']:
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--key-file')+1], str(self.key))
        self.assertEqual(before, (self.root/STATE).read_bytes())

    def test_changed_pipeline_hash_blocks_preflight_recovery_and_rollback(self):
        self.enable_state()
        for key in SNAPSHOT:
            self.host.executor_snapshot.return_value = {**SNAPSHOT, key: 'f'*64}
            for operation in [self.enabled().preflight, self.service.recovery_plan,
                              lambda: self.service.recovery_plan(rollback_pipeline=True)]:
                with self.subTest(hash=key), self.assertRaisesRegex(Refused, 'hash differs'):
                    operation()

    def test_held_or_unknown_pipeline_blocks_interruption_even_when_flag_off(self):
        self.host.pipeline_held.return_value = True
        for service in [self.service, self.enabled()]:
            with patch.object(Services, 'stop_owned') as stop, patch.object(Services, 'launch') as launch:
                for action in [service.require_idle_database, service.preflight, service.recovery_plan,
                               service.start, service.stop]:
                    with self.subTest(action=action.__name__), self.assertRaisesRegex(Refused, 'pipeline work'):
                        action()
                stop.assert_not_called(); launch.assert_not_called()

    def test_login_off_pin_bytes_match_legacy_snapshot_exactly(self):
        state, data, pin = login.snapshot(self.service)
        legacy = {'schema': 1, 'runtime': str(self.root),
            **{k: state[k] for k in ['credential_file_reference', 'codex_file_reference', *login.FLAGS]},
            'fixed_mailbox_policy_sha256': data['fixed_mailbox_policy_sha256'],
            'grant_policy_sha256': data['grant_policy_sha256']}
        self.assertEqual(pin, hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        self.fixture.state['codex_pipeline_enabled'] = False; self.fixture.save()
        self.assertEqual(login.snapshot(self.service)[2], pin)
        self.host.executor_snapshot.assert_not_called()

    def test_login_pipeline_pin_contains_flag_and_all_four_hashes(self):
        old_pin = login.snapshot(self.service)[2]
        self.enable_state()
        _, data, pin = login.snapshot(self.service)
        self.assertNotEqual(pin, old_pin)
        self.assertTrue(data['codex_pipeline_enabled'])
        self.assertEqual(data['codex_pipeline_snapshot'], SNAPSHOT)
        configured = login.configured(self.root)
        self.assertTrue(configured.allow_codex_pipeline)
        self.assertEqual(configured.codex, Path('/usr/bin/true'))
        self.assertEqual(configured.key, self.key)
        for key in SNAPSHOT:
            self.host.executor_snapshot.return_value = {**SNAPSHOT, key: 'f'*64}
            with self.subTest(hash=key), self.assertRaisesRegex(login.LoginBlocked, 'EXECUTOR_PIN_MISMATCH'):
                login.snapshot(self.service)

    def test_login_requires_twenty_three_tools_and_snapshot_for_pipeline(self):
        self.enable_state('STOPPED'); self.absent()
        service = self.enabled()
        pin = login.snapshot(service)[2]
        checked = service.preflight(self.key)
        with patch.object(service, 'status', return_value={'phase': 'STOPPED'}), \
             patch.object(login, 'endpoint', return_value=self.root/'absent-fixture.sock'), \
             patch.object(service, 'start') as start:
            for bad in [{**checked, 'tool_names': checked['tool_names'][:11]},
                        {**checked, 'codex_pipeline_snapshot': {**SNAPSHOT, 'binary_sha256': 'f'*64}}]:
                with patch.object(service, 'preflight', return_value=bad), self.assertRaises(login.LoginBlocked):
                    login.restore(service, pin)
            start.assert_not_called()

    def test_login_restores_pipeline_once_after_twenty_three_tool_preflight(self):
        self.enable_state('STOPPED'); self.absent()
        service = self.enabled()
        pin = login.snapshot(service)[2]
        with patch.object(service, 'status', side_effect=[{'phase': 'STOPPED'}, {'phase': 'READY'}]), \
             patch.object(login, 'endpoint', return_value=self.root/'absent-fixture.sock'), \
             patch.object(service, 'start') as start, patch.object(service, 'stop') as stop:
            result = login.restore(service, pin)
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(result['expected_invocation_sha256'], pin)
        self.assertFalse(result['automatic_retry'])
        start.assert_called_once_with(); stop.assert_not_called()

    def test_pipeline_cli_routes_only_to_planner_and_keeps_legacy_constructor_shape(self):
        for op, options in [('pipeline-rollback-plan', []), ('preflight', ['--allow-codex-pipeline'])]:
            with patch.object(sys, 'argv', ['poc_services.py', op, *options]), \
                 patch.object(module, 'Services') as factory, contextlib.redirect_stdout(io.StringIO()):
                factory.return_value.recovery_plan.return_value = {'automatic_apply': False}
                factory.return_value.preflight.return_value = {'passed': True}
                self.assertEqual(module.main(), 0)
                if op == 'pipeline-rollback-plan':
                    self.assertEqual(factory.call_args.kwargs, {})
                    factory.return_value.recovery_plan.assert_called_once_with(rollback_pipeline=True)
                else:
                    self.assertEqual(factory.call_args.kwargs, {'allow_codex_pipeline': True})
                factory.return_value.start.assert_not_called(); factory.return_value.stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
