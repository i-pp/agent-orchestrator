"""Offline collaboration contract fixtures, with no real model or service calls."""
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT / 'src'), str(PROJECT / 'tests')]
import test_native_team as fixtures
from native_dispatcher import NativeDispatcher
from native_parent_adapter import NativeParentAdapter, AdapterError, TOOLS
from native_team import NativeTeam
from native_team_artifacts import integrate

loader = importlib.util.spec_from_file_location('parent_cli', PROJECT / 'scripts/native_parent.py')
cli = importlib.util.module_from_spec(loader)
loader.loader.exec_module(cli)


class ParentAdapterTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.NativeTeamTests('test_admission_precedes_spawn_and_reload_preserves_native_binding')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        roles = self.f.project / '.codex/agents'
        roles.mkdir(parents=True)
        for role in ['implementer', 'verifier', 'researcher']:
            (roles / (role + '.toml')).write_text(
                'name = "' + role + '"\ndescription = "fixture role"\n'
                'developer_instructions = "Do only the assigned role. Never spawn children."\n')
        self.bundle = SimpleNamespace(**fixtures.POLICY, text='verified fixture common policy')
        policy = patch('native_parent_adapter.load_project_policy', return_value=self.bundle)
        policy.start()
        self.addCleanup(policy.stop)
        self.spec = deepcopy(self.f.spec)
        self.spec['tasks'] = self.spec['tasks'][:2]
        self.clock = lambda: self.f.clock[0]
        self.d = NativeDispatcher.receive(self.f.root / 'parent-run', self.spec, 'fixture', clock=self.clock)
        self.adapter = NativeParentAdapter.configure(self.d, '/root', step_limit=30)

    def state(self):
        return self.d.snapshot()

    def bind(self, step=None):
        step = self.adapter.step() if step is None else step
        calls = [c for c in step['tool_calls'] if c['tool'] == 'collaboration.spawn_agent']
        for call in calls:
            self.adapter.spawned(call['action_ids'][0], '/root/' + call['arguments']['task_name'])
        return calls

    def inventory(self, state='running', step=None):
        step = self.adapter.step() if step is None else step
        call = next(c for c in step['tool_calls'] if c['tool'] == 'collaboration.list_agents')
        names = [b['agent_id'] for b in self.state()['bindings'].values() if not b['released']]
        result = {'agents': [{'agent_name': name, 'agent_status': state} for name in names]}
        self.adapter.inventory(call['action_ids'], result)
        return call, result

    def test_descriptors_match_current_tool_arguments_without_provider_or_custom_agent_keys(self):
        step = self.adapter.step()
        self.assertEqual(len(step['tool_calls']), 2)
        self.assertFalse(step['automatic_tool_execution'])
        self.assertFalse(step['wake_after_parent_turn'])
        for call in step['tool_calls']:
            self.assertEqual(set(call['arguments']), TOOLS[call['tool']])
            self.assertEqual(call['arguments']['fork_turns'], 'none')
            self.assertEqual(self.state()['dispatcher']['parent_adapter']['fork_turns'], 'none')
            message = call['arguments']['message']
            self.assertIn(self.bundle.text, message)
            self.assertIn('Never spawn children', message)
            self.assertIn(self.spec['requirements'][0], message)
            self.assertIn('input_revision', message)
            self.assertNotIn('model', call['arguments'])
            self.assertEqual(call['not_after'], self.f.clock[0] + 300)
            self.assertEqual(self.state()['dispatcher']['actions'][call['action_ids'][0]]['status'], 'ISSUED')

    def test_repeated_step_never_reissues_spawn_and_reload_has_reconciliation(self):
        first = self.adapter.step()
        again = NativeParentAdapter(NativeDispatcher(NativeTeam(self.d.team.root, clock=self.clock))).step()
        self.assertFalse(again['tool_calls'])
        self.assertEqual(len(again['pending_reconciliation']), 2)
        self.assertTrue(all(not a['reissue'] for a in again['pending_reconciliation']))
        self.assertEqual({a['action_id'] for a in again['pending_reconciliation']},
                         {c['action_ids'][0] for c in first['tool_calls']})

    def test_spawn_id_scope_and_duplicate_ack(self):
        call = self.adapter.step()['tool_calls'][0]
        action = call['action_ids'][0]
        before = self.state()
        for wrong in ['/root/unrelated', '/other/' + call['arguments']['task_name']]:
            with self.assertRaises(AdapterError):
                self.adapter.spawned(action, wrong)
        self.assertEqual(self.state(), before)
        name = '/root/' + call['arguments']['task_name']
        self.adapter.spawned(action, name)
        self.assertTrue(self.adapter.spawned(action, name)['duplicate'])

    def test_configuration_cannot_change_parent_or_step_budget(self):
        for parent, limit in [('/root/elsewhere', 30), ('/root', 31), ('/root/../x', 30), ('/root', True)]:
            before = self.state()
            with self.assertRaises(AdapterError):
                NativeParentAdapter.configure(self.d, parent, limit)
            self.assertEqual(self.state(), before)

    def test_legacy_context_fork_profile_is_not_resumed_silently(self):
        with self.d.team._change() as (data, plan, now):
            data['dispatcher']['parent_adapter']['profile'] = 'collaboration-parent-v1'
            del data['dispatcher']['parent_adapter']['fork_turns']
        before = self.state()
        with self.assertRaisesRegex(AdapterError, 'NOT_CONFIGURED'):
            NativeParentAdapter(self.d)
        self.assertEqual(self.state(), before)
        self.assertEqual(before['plan']['dispatches'], 0)

    def test_missing_policy_or_role_change_blocks_before_admission(self):
        before = self.state()
        self.bundle.sha256 = 'b' * 64
        with self.assertRaisesRegex(AdapterError, 'POLICY_PIN'):
            self.adapter.step()
        self.bundle.sha256 = fixtures.POLICY['sha256']
        role = self.f.project / '.codex/agents/implementer.toml'
        role.write_text(role.read_text().replace('fixture role', 'changed role'))
        with self.assertRaisesRegex(AdapterError, 'ROLE_PIN'):
            self.adapter.step()
        self.assertEqual(self.state(), before)

    def test_unsupported_role_model_setting_is_not_ignored(self):
        role = self.f.project / '.codex/agents/implementer.toml'
        role.write_text(role.read_text() + 'model = "fixture-unspecified-model"\n')
        before = self.state()
        with self.assertRaisesRegex(AdapterError, 'UNSUPPORTED_ROLE_SETTINGS'):
            self.adapter.step()
        self.assertEqual(self.state(), before)

    def test_one_inventory_call_matches_exact_bindings_and_does_not_store_answer_body(self):
        self.bind()
        call, result = self.inventory({'completed': 'private-response-sentinel'})
        self.assertEqual(len(call['action_ids']), 2)
        self.assertEqual(call['arguments'], {'path_prefix': '/root'})
        self.assertNotIn('private-response-sentinel', self.d.team.path.read_text())
        self.assertTrue(all(not b['released'] for b in self.state()['bindings'].values()))
        self.assertTrue(all(t['state'] == 'RUNNING' for t in self.state()['plan']['tasks'].values()))
        self.assertEqual(len(self.adapter.step()['parent_jobs']), 2)

    def test_unknown_inventory_missing_duplicate_or_future_status_holds_slot(self):
        for mode in ['missing', 'duplicate', 'future']:
            with self.subTest(mode=mode):
                # Each independent fixture variant uses its own run directory.
                run = NativeDispatcher.receive(self.f.root / ('variant-' + mode), self.spec, mode, clock=self.clock)
                adapter = NativeParentAdapter.configure(run, '/root')
                calls = adapter.step()['tool_calls']
                for call in calls:
                    adapter.spawned(call['action_ids'][0], '/root/' + call['arguments']['task_name'])
                poll = next(c for c in adapter.step()['tool_calls'] if c['tool'].endswith('list_agents'))
                names = [b['agent_id'] for b in run.snapshot()['bindings'].values()]
                rows = [] if mode == 'missing' else [
                    {'agent_name': n, 'agent_status': 'idle'} for n in names]
                if mode == 'duplicate':
                    rows += deepcopy(rows)
                adapter.inventory(poll['action_ids'], {'agents': rows})
                state = run.snapshot()
                self.assertTrue(all(t['state'] == 'UNKNOWN' for t in state['plan']['tasks'].values()))
                self.assertTrue(all(not b['released'] for b in state['bindings'].values()))

    def test_inventory_checks_all_ids_before_partial_mutation_and_rejects_changed_schema(self):
        self.bind()
        poll = next(c for c in self.adapter.step()['tool_calls'] if c['tool'].endswith('list_agents'))
        before = self.state()
        for ids, result in [(poll['action_ids'] + ['absent'], {'agents': []}),
                            (poll['action_ids'], {'agents': [], 'new_field': True})]:
            with self.assertRaises(AdapterError):
                self.adapter.inventory(ids, result)
            self.assertEqual(self.state(), before)

    def test_partial_inventory_ack_loss_replays_only_identical_local_observations(self):
        self.bind()
        call = next(c for c in self.adapter.step()['tool_calls'] if c['tool'].endswith('list_agents'))
        result = {'agents': [{'agent_name': b['agent_id'], 'agent_status': 'running'}
                             for b in self.state()['bindings'].values()]}
        original = self.d.observe
        calls = []
        def fail_second(action_id, event):
            calls.append(action_id)
            if len(calls) == 2:
                raise OSError('fixture ACK loss')
            return original(action_id, event)
        with patch.object(self.d, 'observe', side_effect=fail_second):
            with self.assertRaises(OSError):
                self.adapter.inventory(call['action_ids'], result)
        acknowledgments = self.adapter.inventory(call['action_ids'], result)
        self.assertTrue(acknowledgments[0]['duplicate'])
        self.assertTrue(acknowledgments[1]['applied'])
        self.assertEqual(self.state()['plan']['dispatches'], 2)

    def test_expired_ready_spawn_is_blocked_before_parent_handoff(self):
        self.d.advance()
        self.f.clock[0] += 301
        step = self.adapter.step()
        self.assertEqual(step['tool_calls'], [])
        self.assertEqual(step['progress']['status'], 'BLOCKED')
        self.assertTrue(all(a['status'] == 'BLOCKED' for a in step['pending_reconciliation']))

    def test_interrupt_descriptor_is_exact_and_ack_is_not_release(self):
        self.bind()
        self.f.clock[0] += 301
        step = self.adapter.step()
        interrupts = [c for c in step['tool_calls'] if c['tool'].endswith('interrupt_agent')]
        self.assertEqual(len(interrupts), 2)
        for call in interrupts:
            self.assertEqual(set(call['arguments']), {'target'})
            self.adapter.interrupted(call['action_ids'][0], True)
        self.assertTrue(all(not b['released'] for b in self.state()['bindings'].values()))
        self.inventory({'completed': 'interrupted turn has ended'}, step)
        self.assertTrue(all(b['released'] for b in self.state()['bindings'].values()))
        self.assertTrue(all(t['state'] == 'TIMED_OUT' for t in self.state()['plan']['tasks'].values()))

    def test_step_limit_and_near_deadline_wait_are_bounded(self):
        with self.d.team._change() as (data, plan, now):
            data['dispatcher']['parent_adapter']['steps'] = 29
        self.bind()
        before = self.state()
        with self.assertRaisesRegex(AdapterError, 'STEP_LIMIT'):
            self.adapter.step()
        self.assertEqual(self.state(), before)

    def test_wait_is_only_parent_hint_and_never_used_near_deadline(self):
        self.bind()
        self.inventory()
        hint = self.adapter.step()['optional_wait']
        self.assertEqual(hint['tool'], 'collaboration.wait_agent')
        self.assertEqual(hint['arguments'], {'timeout_ms': 10000})
        self.f.clock[0] += 299
        self.assertIsNone(self.adapter.step()['optional_wait'])

    def test_cli_strict_input_and_unknown_error_do_not_echo_private_diagnostics(self):
        for data in [b'{"unexpected":"private-cli-sentinel"}', b'{"x":1,"x":2}', b'x' * 65537]:
            out = io.StringIO()
            code = cli.main(['step', '--run', str(self.d.team.root)], io.BytesIO(data), out)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out.getvalue())['status'], 'BLOCKED')
            self.assertNotIn('private-cli-sentinel', out.getvalue())
        out = io.StringIO()
        with patch.object(cli, 'invoke', side_effect=OSError('private-diagnostic-sentinel')):
            cli.main(['step', '--run', str(self.d.team.root)], io.BytesIO(b'{}'), out)
        reply = json.loads(out.getvalue())
        self.assertEqual(reply['status'], 'UNKNOWN')
        self.assertNotIn('private-diagnostic-sentinel', out.getvalue())

    def test_cli_real_dispatcher_handoff_does_not_invoke_tools(self):
        out = io.StringIO()
        code = cli.main(['step', '--run', str(self.d.team.root)], io.BytesIO(b'{}'), out)
        self.assertEqual(code, 0)
        reply = json.loads(out.getvalue())
        self.assertEqual(len(reply['result']['tool_calls']), 2)
        self.assertFalse(reply['automatic_tool_execution'])

    def test_parent_driven_four_node_contract_loop_completes_without_real_spawns(self):
        spec = deepcopy(self.f.spec)
        for task in spec['tasks']:
            if task['id'] in ['a', 'b']:
                task['allowed'] = [task['id'] + '.txt']
            elif task['id'] == 'merge':
                task.update(role='Coordinator', allowed=['a.txt', 'b.txt'])
            else:
                task.update(role='Verifier', deps=['merge'], allowed=['report.txt'])
        run = NativeDispatcher.receive(self.f.root / 'full-run', spec, 'full', clock=self.clock)
        adapter = NativeParentAdapter.configure(run, '/root')
        fake_spawns = []
        for _ in range(12):
            step = adapter.step()
            if step['progress']['status'] == 'COMPLETE':
                break
            for call in step['tool_calls']:
                if call['tool'].endswith('spawn_agent'):
                    fake_spawns.append(call)
                    adapter.spawned(call['action_ids'][0], '/root/' + call['arguments']['task_name'])
                elif call['tool'].endswith('list_agents'):
                    rows = []
                    state = run.snapshot()
                    for task_id, binding in state['bindings'].items():
                        if not binding['released'] and binding['agent_id']:
                            task = state['plan']['tasks'][task_id]
                            if task['role'] == 'Verifier':
                                for dep in ['a', 'b']:
                                    self.assertEqual((self.f.workspaces['merge'] / (dep + '.txt')).read_text(),
                                                     'verified fixture ' + dep)
                            for file in task['allowed']:
                                (Path(task['workspace']) / file).write_text('verified fixture ' + task_id)
                            rows.append({'agent_name': binding['agent_id'], 'agent_status': {'completed': 'fixture'}})
                    adapter.inventory(call['action_ids'], {'agents': rows})
            for job in step['parent_jobs']:
                task = run.snapshot()['plan']['tasks'][job['task_id']]
                if job['kind'] == 'COORDINATE':
                    receipts = run.snapshot()['receipts']
                    self.assertEqual(set(job['inputs']), {'a', 'b'})
                    inputs = [{'root': self.f.workspaces[n], 'allowed': [n + '.txt'],
                               'expected': receipts[n]['artifacts']} for n in ['a', 'b']]
                    integrate(inputs, self.f.workspaces['merge'])
                self.assertTrue(all((Path(task['workspace']) / f).exists() for f in task['allowed']))
                adapter.verified(job['action_id'], True, 'fixture checked from requirements', fixtures.VERIFIED)
            self.f.clock[0] += 1.1
        self.assertEqual(step['progress']['status'], 'COMPLETE')
        self.assertEqual(len(fake_spawns), 3)
        self.assertEqual(len({c['arguments']['task_name'] for c in fake_spawns}), 3)
        self.assertTrue(all(b['released'] for b in run.snapshot()['bindings'].values()))


if __name__ == '__main__':
    unittest.main()
