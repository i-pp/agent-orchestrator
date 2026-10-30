"""Only UUID private PoC runtimes, synthetic key files, and transient fixture units."""

import fcntl
import json
import os
from pathlib import Path
import pty
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'scripts'))
sys.path.insert(0, str(PROJECT/'src'))
from poc_services import Services, Refused, STATE, private_file, ENV
from development_poc import configure
from control_socket import request
from poc_services_driver import FixtureServices
from systemd_fixture import ControlPlane
from mcp_adapter import VERSION_META, CAPABILITIES_META, REQUEST_PROTOCOL

SENTINEL = 'public-synthetic-poc-service-fixture-value'


class ServiceFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT/'.runtime', prefix='development-poc-services-')
        self.root = Path(self.temp.name)
        configure(self.root, True)
        (self.root/'logs').mkdir(mode=0o700)
        self.keys = tempfile.TemporaryDirectory(prefix='poc-service-synthetic-')
        self.key = Path(self.keys.name)/'key'
        self.key.write_text(SENTINEL); self.key.chmod(0o600)
        self.services = Services(self.root, sys.executable, self.key)

    def tearDown(self):
        self.keys.cleanup(); self.temp.cleanup()

    def absent(self):
        return {'LoadState': 'not-found', 'ActiveState': 'inactive'}


class MetadataTests(ServiceFixture):
    def test_plan_reads_only_metadata_and_creates_no_state(self):
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('No secret reads')):
            plan = self.services.plan()
        self.assertTrue(plan['key_metadata']['acceptable'])
        self.assertFalse((self.root/STATE).exists())
        self.assertFalse((self.root/'poc-services.lock').exists())
        self.assertNotIn(SENTINEL, json.dumps(plan))

    def test_missing_unsafe_and_symlink_key_fail_before_launch(self):
        for mode in [0o664, 0o640, 0o644]:
            self.key.chmod(mode)
            with patch.object(self.services, 'show', return_value=self.absent()), patch.object(self.services, 'launch') as launch:
                with self.assertRaises(Refused): self.services.start()
                launch.assert_not_called()
        self.key.chmod(0o600)
        alias = self.key.parent/'alias'; alias.symlink_to(self.key)
        with self.assertRaises(Refused): private_file(alias)
        self.key.unlink()
        with patch.object(self.services, 'show', return_value=self.absent()), patch.object(self.services, 'launch') as launch:
            with self.assertRaises(OSError): self.services.start()
            launch.assert_not_called()

    def test_commands_use_poc_file_reference_and_read_only_adapter(self):
        commands = self.services.commands()
        self.assertIn('--development-poc', commands['daemon'])
        self.assertIn('--control-plane.api-key=file:'+str(self.key), commands['tunnel'])
        text = json.dumps(commands)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn('--allow-writes', text)
        self.assertNotIn('danger-full-access', text)
        self.assertNotIn('file:%d', text)

    def test_explicit_apply_required_without_launch(self):
        run = subprocess.run([sys.executable, '-B', str(PROJECT/'scripts/poc_services.py'), 'start', '--runtime', str(self.root)],
                             capture_output=True, text=True, timeout=3)
        self.assertEqual(run.returncode, 2)
        self.assertIn('--apply', run.stderr)
        self.assertFalse((self.root/STATE).exists())

    def test_parallel_operation_lock_is_not_force_released(self):
        with self.services.locked():
            with self.assertRaises(Refused): self.services.status()
        self.assertTrue((self.root/'poc-services.lock').exists())

    def test_stale_state_refuses_start_until_explicit_stop(self):
        state = self.state()
        self.services.write_state(state)
        with patch.object(self.services, 'show', return_value=self.absent()), patch.object(self.services, 'launch') as launch:
            with self.assertRaises(Refused): self.services.start()
            launch.assert_not_called()
            self.assertEqual(self.services.status()['phase'], 'STALE_OR_STOPPED')
            self.assertEqual(self.services.stop()['phase'], 'STOPPED')

    def state(self):
        return {'schema': 1, 'runtime': str(self.root), 'token': 'a'*32, 'units': self.services.units,
                'invocations': {'daemon': 'original-d', 'tunnel': 'original-t'}, 'phase': 'READY'}

    def test_replaced_unit_or_invocation_is_never_stopped(self):
        state = self.state(); self.services.write_state(state)
        props = {'LoadState': 'loaded', 'ActiveState': 'active', 'Transient': 'yes',
                 'Description': self.services.description(state, 'daemon'), 'InvocationID': 'reused-name-new-generation'}
        with patch.object(self.services, 'show', return_value=props), patch.object(self.services, 'call') as call:
            with self.assertRaises(Refused): self.services.stop()
            call.assert_not_called()
        with patch.object(self.services, 'show', return_value=props):
            with self.assertRaises(Refused): self.services.start()

    def test_missing_record_and_foreign_unit_are_never_adopted(self):
        with patch.object(self.services, 'show', return_value={'LoadState': 'loaded', 'ActiveState': 'active'}), patch.object(self.services, 'call') as call:
            with self.assertRaises(Refused): self.services.start()
            with self.assertRaises(Refused): self.services.stop()
            call.assert_not_called()

    def test_launch_uses_fixed_user_bus_and_no_parent_secret_environment(self):
        state = self.state()
        props = {'Description': self.services.description(state, 'daemon'), 'InvocationID': 'fixture-generation'}
        with patch.dict(os.environ, {'CONTROL_PLANE_API_KEY': SENTINEL, 'XDG_RUNTIME_DIR': '/invalid',
                                    'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/invalid'}), \
             patch.object(self.services, 'call', return_value='') as call, \
             patch.object(self.services, 'show', return_value=props):
            self.services.launch(state, 'daemon', self.services.commands()['daemon'])
        argv = call.call_args.args[0]
        clean = argv[argv.index('--')+1:]
        self.assertEqual(clean[:2], ['/usr/bin/env', '-i'])
        self.assertIn('XDG_RUNTIME_DIR=/run/user/'+str(os.getuid()), clean)
        self.assertNotIn(SENTINEL, json.dumps(argv))
        self.assertNotIn('/invalid', json.dumps(argv))
        self.assertIn('--expand-environment=no', argv)

    def test_real_user_bus_query_works_without_parent_bus_environment(self):
        with patch.dict(os.environ, {'XDG_RUNTIME_DIR': '/invalid', 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/invalid'}):
            props = self.services.show(self.services.units['daemon'])
        self.assertEqual(props['LoadState'], 'not-found')

    def test_pending_work_is_not_dispatched_by_service_start(self):
        with sqlite3.connect(self.root/'tasks.sqlite') as db:
            db.execute('CREATE TABLE tasks(state TEXT)')
            db.execute("INSERT INTO tasks VALUES('QUEUED')")
        with patch.object(self.services, 'show', return_value=self.absent()), patch.object(self.services, 'launch') as launch:
            with self.assertRaises(Refused): self.services.start()
            launch.assert_not_called()
        with sqlite3.connect(self.root/'tasks.sqlite') as db:
            self.assertEqual(db.execute('SELECT state FROM tasks').fetchone()[0], 'QUEUED')


class DetachedServiceTests(ServiceFixture):
    def setUp(self):
        super().setUp()
        self.plane = ControlPlane()
        self.services = FixtureServices(self.root, self.key, self.plane.url)

    def tearDown(self):
        try:
            if (self.root/STATE).exists(): self.services.stop()
        finally:
            self.plane.close()
            super().tearDown()

    def test_terminal_exit_keeps_owned_services_and_readonly_mcp(self):
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen([sys.executable, '-B', str(PROJECT/'tests/poc_services_driver.py'),
                                        str(self.root), str(self.key), self.plane.url],
                                       stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                                       env={'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'LANG': 'C.UTF-8'})
            process.wait(timeout=20)
            self.assertEqual(process.returncode, 0)
        finally: os.close(slave); os.close(master)
        status = self.services.status()
        self.assertEqual(status['phase'], 'READY')
        self.assertTrue(all(u['ownership'] == 'owned' for u in status['units'].values()))
        self.assertFalse(request(self.root, 'status')['active_tasks'])
        self.assertFalse(request(self.root, 'status')['task_counts'])
        minimal = {'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'LANG': 'C.UTF-8'}
        cli = [sys.executable, '-B', str(PROJECT/'scripts/poc_services.py')]
        probe = subprocess.run([*cli, 'status', '--runtime', str(self.root)], env=minimal,
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(probe.returncode, 0)
        self.assertEqual(json.loads(probe.stdout)['phase'], 'READY')
        for role, name in self.services.units.items():
            props = self.services.show(name)
            pid = int(props['MainPID'])
            info = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
            self.assertEqual(int(info[4]), 0)  # tty_nr: no controlling terminal
            self.assertNotEqual(int(info[1]), process.pid)
        def rpc(method, extra=None):
            return {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': {
                '_meta': {VERSION_META: REQUEST_PROTOCOL, CAPABILITIES_META: {}}, **(extra or {})}}
        self.assertIn(REQUEST_PROTOCOL, self.plane.call(rpc('server/discover'))['resp_json']['result']['supportedVersions'])
        listed = self.plane.call(rpc('tools/list'))['resp_json']['result']['tools']
        self.assertEqual([t['name'] for t in listed], ['orchestrator_status', 'orchestrator_list_tasks', 'orchestrator_get_task'])
        result = self.plane.call(rpc('tools/call', {'name': 'orchestrator_status'}))['resp_json']['result']
        self.assertFalse(result['isError'])
        with self.assertRaises(Refused): self.services.start()
        # A sentinel environment must not be inherited from a user manager or shell.
        for role in self.services.units:
            pid = self.services.show(self.services.units[role])['MainPID']
            argv = Path('/proc', pid, 'cmdline').read_bytes()
            self.assertNotIn(SENTINEL.encode(), argv)
        for path in (self.root/'logs').glob('poc-service-*.log'):
            self.assertNotIn(SENTINEL.encode(), path.read_bytes())
        stopped = subprocess.run([*cli, 'stop', '--runtime', str(self.root), '--apply'], env=minimal,
                                 capture_output=True, text=True, timeout=15)
        self.assertEqual(stopped.returncode, 0)
        self.assertEqual(json.loads(stopped.stdout)['phase'], 'STOPPED')
        self.assertTrue(all(u['ownership'] == 'absent' for u in self.services.status()['units'].values()))

    def test_tunnel_readiness_failure_stops_only_created_units(self):
        self.plane.disconnected = True
        with self.assertRaises(Refused): self.services.start(tunnel_timeout=.4)
        self.assertEqual(self.services.read_state()['phase'], 'FAILED')
        self.assertTrue(all(u['ownership'] == 'absent' for u in self.services.status()['units'].values()))

    def test_daemon_exit_does_not_launch_tunnel(self):
        commands = self.services.commands()
        commands['daemon'] = [sys.executable, '-c', 'raise SystemExit(7)']
        with patch.object(self.services, 'commands', return_value=commands), \
             patch.object(self.services, 'launch', wraps=self.services.launch) as launch:
            with self.assertRaises(Refused): self.services.start(daemon_timeout=.5, tunnel_timeout=.5)
        self.assertEqual([c.args[1] for c in launch.call_args_list], ['daemon'])
        self.assertEqual(self.services.read_state()['phase'], 'FAILED')
        self.assertTrue(all(u['ownership'] == 'absent' for u in self.services.status()['units'].values()))


if __name__ == '__main__': unittest.main()
