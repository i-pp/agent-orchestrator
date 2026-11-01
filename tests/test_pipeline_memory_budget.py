"""Finite pipeline memory profile and accounting; no services, sockets or models."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT/'src'), str(PROJECT/'scripts')]
import daemon
import scheduler
import resources
from poc_services import Services

MIB = 1024**2


class PipelineMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'scheduler.json'
        self.config = json.loads((PROJECT/'scheduler-config.json').read_text())
        self.path.write_text(json.dumps(self.config))
        p = patch.object(scheduler, 'CONFIG', self.path)
        p.start(); self.addCleanup(p.stop)

    def test_only_explicit_pipeline_memory_changes(self):
        base = scheduler.configuration()
        enabled = scheduler.configuration(codex_pipeline=True)
        self.assertEqual(base['daemon']['memory_bytes'], 512*MIB)
        self.assertEqual(enabled['daemon']['memory_bytes'], 1024*MIB)
        expected = copy.deepcopy(base)
        expected['daemon']['memory_bytes'] = 1024*MIB
        expected['daemon']['pids'] = 512
        self.assertEqual(enabled, expected)
        self.assertEqual(scheduler.configuration(), base)

    def test_old_configuration_still_supports_non_pipeline_daemon(self):
        self.config.pop('pipeline_daemon_memory_bytes')
        self.path.write_text(json.dumps(self.config))
        self.assertEqual(scheduler.configuration()['daemon']['memory_bytes'], 512*MIB)
        with self.assertRaisesRegex(ValueError, 'explicitly configured'):
            scheduler.configuration(codex_pipeline=True)

    def test_malformed_budgets_never_silently_fall_back(self):
        for value in [None, True, False, '1073741824', 0, -1, 512*MIB-1, 1024.0]:
            self.config['pipeline_daemon_memory_bytes'] = value
            self.path.write_text(json.dumps(self.config))
            for enabled in [False, True]:
                with self.subTest(value=value, enabled=enabled), self.assertRaises(ValueError):
                    scheduler.configuration(codex_pipeline=enabled)

    def test_selector_is_not_truthy_permission(self):
        for value in [None, 0, 1, 'true']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                scheduler.configuration(codex_pipeline=value)

    def test_pipeline_kernel_task_budget_is_explicit_bounded_and_separate(self):
        self.assertEqual(scheduler.configuration()['daemon']['pids'],64)
        self.assertEqual(scheduler.configuration(codex_pipeline=True)['daemon']['pids'],512)
        service=Services.__new__(Services); service.allow_codex_pipeline=True
        self.assertEqual(service.kernel_task_budget(),{'daemon_scope_tasks':512,
            'supervisor_service_tasks':64,'tunnel_service_tasks':64,'separate_cgroups_total_tasks':640})
        for invalid in [None,True,False,'512',0,63,513,4096,512.0]:
            self.config['pipeline_daemon_pids']=invalid
            self.path.write_text(json.dumps(self.config))
            with self.subTest(value=invalid),self.assertRaises(ValueError):
                scheduler.configuration(codex_pipeline=True)

    def test_enabled_pipeline_never_falls_back_to_old_64_task_budget(self):
        self.config.pop('pipeline_daemon_pids')
        self.path.write_text(json.dumps(self.config))
        self.assertEqual(scheduler.configuration()['daemon']['pids'],64)
        with self.assertRaisesRegex(ValueError,'explicitly configured'):
            scheduler.configuration(codex_pipeline=True)

    def test_file_ceiling_remains_unchanged_in_pipeline_profile(self):
        for enabled in [False,True]:
            settings=scheduler.configuration(codex_pipeline=enabled)
            self.assertEqual(settings['daemon']['file_bytes'],64*MIB)
            self.assertEqual(settings['worker']['file_bytes'],64*MIB)
            self.assertEqual(settings['daemon']['runtime_bytes'],2*1024*MIB)
            self.assertEqual(settings['daemon']['log_bytes'],32*MIB)

    def test_outer_supervisor_tunnel_and_aggregate_are_explicit(self):
        service = Services.__new__(Services)
        service.allow_codex_pipeline = False
        base = service.memory_budget()
        service.allow_codex_pipeline = True
        enabled = service.memory_budget()
        self.assertEqual(base['daemon_scope_bytes'], 512*MIB)
        self.assertEqual(enabled['daemon_scope_bytes'], 1024*MIB)
        for value in [base, enabled]:
            self.assertEqual(value['supervisor_service_bytes'], 512*MIB)
            self.assertEqual(value['tunnel_service_bytes'], 256*MIB)
        self.assertEqual(base['separate_cgroups_total_bytes'], 1280*MIB)
        self.assertEqual(enabled['separate_cgroups_total_bytes'], 1792*MIB)

    def test_actual_launch_keeps_supervisor_and_tunnel_caps(self):
        service = Services.__new__(Services)
        service.root = self.root
        service.units = {role:'fixture-'+role+'.service' for role in ['daemon','tunnel']}
        service.allow_codex_pipeline = True
        state = {'token':'public-fixture', 'invocations':{}}
        for role, memory in [('daemon',512*MIB), ('tunnel',256*MIB)]:
            props = {'Description':service.description(state,role), 'InvocationID':'fixture-generation'}
            with self.subTest(role=role), patch.object(service,'call') as call, \
                 patch.object(service,'show',return_value=props), patch.object(service,'write_state'):
                service.launch(state,role,['/usr/bin/true'])
            command = call.call_args.args[0]
            self.assertIn('--property=MemoryMax='+str(memory),command)
            self.assertIn('--property=MemorySwapMax=0',command)
            self.assertIn('--property=TasksMax=64',command)
            self.assertIn('--property=Restart=no',command)
            self.assertIn('--property=LimitFSIZE=64M',command)

    def test_self_limit_check_accepts_only_the_selected_memory_cap(self):
        original = Path.read_text
        for enabled, memory in [(False,512*MIB),(True,1024*MIB)]:
            values = {'cpu.max':'50000 100000','memory.max':str(memory),
                      'pids.max':'512' if enabled else '64','memory.swap.max':'0'}
            def read(path,*args,**kwargs):
                if path==Path('/proc/self/cgroup'):return '0::/fixture.scope\n'
                if str(path).startswith('/sys/fs/cgroup/fixture.scope/'):
                    return values[path.name]
                return original(path,*args,**kwargs)
            with self.subTest(enabled=enabled), patch.object(Path,'read_text',autospec=True,side_effect=read):
                daemon.verify_self_limits(allow_codex_pipeline=enabled)
                values['memory.max']=str(memory+1)
                with self.assertRaisesRegex(RuntimeError,'verified cgroup limits'):
                    daemon.verify_self_limits(allow_codex_pipeline=enabled)

    def test_foreground_controller_receives_selected_inner_limit(self):
        (self.root/'logs').mkdir()
        for enabled, memory in [(False, 512*MIB), (True, 1024*MIB)]:
            control = Mock(unit='fixture.scope')
            process = Mock()
            process.proc.poll.return_value = 0
            process.close.return_value = {'exit_code': 0,
                'resources': {'confirmed': True, 'exceeded': None, 'released': True}}
            with self.subTest(enabled=enabled), \
                 patch('isolation.prepare', return_value=(self.root, '/fixture/codex')), \
                 patch('daemon.development_poc.configure'), \
                 patch('daemon.Control', return_value=control) as factory, \
                 patch('daemon.Process', return_value=process) as spawn:
                self.assertEqual(daemon.foreground(self.root, allow_codex_pipeline=enabled), 0)
                self.assertEqual(factory.call_args.args[1]['memory_bytes'], memory)
                self.assertEqual(factory.call_args.args[1]['pids'], 512 if enabled else 64)
                self.assertIs(spawn.call_args.kwargs['resources'], control)

    def test_inner_daemon_verifies_same_selected_limit(self):
        (self.root/'logs').mkdir()
        for enabled, memory in [(False, 512*MIB), (True, 1024*MIB)]:
            runner = Mock(root=self.root)
            with self.subTest(enabled=enabled), patch('daemon.Runner', return_value=runner), \
                 patch('daemon.lease'), patch('daemon.scheduler.initialize'), \
                 patch('daemon.scheduler.owner_identity', return_value={}):
                instance = daemon.Daemon(self.root, allow_codex_pipeline=enabled)
                self.assertEqual(instance.settings['daemon']['memory_bytes'], memory)
                instance.events_stream.close(); instance.selector.close()
                instance.lease.close(); instance.store.close()

    def test_cache_accounting_is_recorded_without_weakening_max_event(self):
        group = self.root/'cgroup'; group.mkdir()
        for name, content in {'cpu.stat':'usage_usec 1\n', 'memory.events':'max 58\noom 0\noom_kill 0\n',
                'pids.events':'max 0\n', 'cgroup.events':'populated 0\n',
                'memory.current':str(372543488), 'memory.peak':str(512*MIB),
                'pids.current':'0', 'pids.peak':'28',
                'memory.stat':'anon 123\nfile 456\ninactive_file 400\nslab_reclaimable 8\npgfault 999\n'}.items():
            (group/name).write_text(content)
        stats = resources.counters(group)
        self.assertEqual(stats['memory.stat'], {'anon':123,'file':456,'inactive_file':400,'slab_reclaimable':8})
        self.assertEqual(resources.violation(stats, scheduler.configuration(codex_pipeline=True)['daemon']), 'memory.max')


if __name__ == '__main__': unittest.main()
