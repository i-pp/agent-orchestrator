"""Offline requirements for explicit models, role selection, and bounded fan-out."""
from copy import deepcopy
import ast
import importlib.util
import io
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT / 'src'), str(PROJECT / 'tests')]
from native_agent_profiles import (ROLES, MODELS, BUDGET_CEILINGS,
                                   configured_budgets, load_profile, recommend, role_registry)
from native_team import NativeTeam, registry
from native_dispatcher import NativeDispatcher
from native_parent_adapter import NativeParentAdapter, AdapterError
import test_native_parent_adapter as fixtures

loader = importlib.util.spec_from_file_location('plan_cli', PROJECT / 'scripts/plan_native_team.py')
cli = importlib.util.module_from_spec(loader)
loader.loader.exec_module(cli)


class RecommendationTests(unittest.TestCase):
    def test_three_implementation_levels_are_distinct_but_keep_reviews(self):
        for level, role in [('quick', 'ImplementerQuick'), ('standard', 'Implementer'),
                            ('advanced', 'ImplementerAdvanced')]:
            result = recommend('implementation', level)
            assignments = result['assignments']
            self.assertEqual(next(a['role'] for a in assignments if a['phase'] == 'work'), role)
            self.assertEqual([a['role'] for a in assignments if a['phase'] == 'review'],
                             ['IndependentReviewer', 'TestReviewer'])
            self.assertEqual(assignments[-1]['role'], 'Verifier')
            self.assertFalse(result['automatic_dispatch'])
            self.assertFalse(result['availability_verified'])

    def test_orchestrator_selected_only_when_it_can_help(self):
        self.assertNotIn('Orchestrator', [a['role'] for a in recommend('implementation')['assignments']])
        for args in [{'parallel_parts': 3}, {'cross_project': True}, {'complexity': 'advanced'}]:
            self.assertEqual(recommend('implementation', **args)['assignments'][0]['role'], 'Orchestrator')
        self.assertEqual(recommend('orchestration')['assignments'],
                         [{'phase': 'work', 'role': 'Orchestrator', 'count': 1}])

    def test_specialized_reviews_do_not_replace_correctness_or_verification(self):
        result = recommend('implementation', security_sensitive=True, performance_sensitive=True)
        self.assertEqual([a['role'] for a in result['assignments']],
                         ['Implementer', 'IndependentReviewer', 'TestReviewer',
                          'SecurityReviewer', 'PerformanceReviewer', 'Verifier'])
        docs = recommend('documentation')['assignments']
        self.assertEqual([a['role'] for a in docs],
                         ['ImplementerQuick', 'DocumentationReviewer', 'Verifier'])
        self.assertNotIn('Editor', ROLES)

    def test_fanout_bounded_by_dispatch_cost_not_just_concurrent_slots(self):
        result = recommend('implementation', parallel_parts=19)
        self.assertEqual(result['required_dispatches'], 24)
        self.assertEqual(result['parent_integration_dispatches'], 1)
        self.assertEqual(result['required_dispatches'],
                         sum(a['count'] for a in result['assignments']) + 1)
        self.assertEqual(result['suggested_concurrency'], 6)
        for value in [0, 25, True, 1.1]:
            with self.assertRaises(ValueError):
                recommend('implementation', parallel_parts=value)
        with self.assertRaisesRegex(ValueError, 'DISPATCH_BUDGET'):
            recommend('implementation', parallel_parts=20)
        with self.assertRaisesRegex(ValueError, 'DISPATCH_BUDGET'):
            recommend('implementation', parallel_parts=19, security_sensitive=True)

    def test_invalid_flags_and_unknown_tasks_are_rejected(self):
        for args in [{'purpose': 'unknown'}, {'purpose': 'research', 'parallel_parts': 2},
                     {'purpose': 'implementation', 'complexity': 'unknown'},
                     {'purpose': 'implementation', 'security_sensitive': 'false'}]:
            with self.assertRaises(ValueError):
                recommend(**args)

    def test_registry_and_budget_returns_do_not_share_mutable_defaults(self):
        first = registry()
        first['roles']['Implementer']['native_role'] = 'changed'
        first['budgets']['cloud_concurrency'] = 999
        self.assertEqual(registry()['roles'], role_registry())
        self.assertEqual(registry()['budgets'], BUDGET_CEILINGS)
        self.assertFalse(registry()['backends']['local']['dispatch_enabled'])

    def test_all_checked_in_profiles_explicitly_select_sol_max(self):
        expected = {native + '.toml' for name, (native, _) in ROLES.items() if name != 'Coordinator'}
        self.assertEqual({p.name for p in (PROJECT / '.codex/agents').glob('*.toml')}, expected)
        for role in set(ROLES) - {'Coordinator'}:
            _, value = load_profile(PROJECT, role)
            self.assertEqual(value['model'], 'gpt-6.1-sol')
            self.assertEqual(value['model_reasoning_effort'], 'max')
        project = tomllib.loads((PROJECT / '.codex/config.toml').read_text())['agents']
        self.assertEqual(project['max_concurrent_threads_per_session'], 6)
        self.assertEqual(project['default_subagent_model'], 'gpt-6.1-sol')
        self.assertEqual(project['default_subagent_reasoning_effort'], 'max')

    def test_cli_reads_actual_profile_settings_without_starting_a_run(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            code = cli.main(['--purpose', 'implementation', '--parallel-parts', '3', '--security-sensitive'])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result['automatic_dispatch'])
        self.assertFalse(result['automatic_retry'])
        self.assertFalse(result['availability_verified'])
        for item in result['assignments']:
            self.assertEqual(item['model'], 'gpt-6.1-sol')
            self.assertEqual(item['reasoning_effort'], 'max')
            self.assertEqual(len(item['profile_sha256']), 64)
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(['--purpose', 'implementation', '--parallel-parts', '24']), 2)
        self.assertNotIn(str(PROJECT), output.getvalue())


class ConcurrencySettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / 'config').mkdir()
        self.config = self.project / 'config/native-team.json'

    def write(self, value):
        self.config.write_text(json.dumps(value))

    def test_max_is_the_default_within_this_transport_ceiling(self):
        budgets, selection = configured_budgets(self.project)
        self.assertEqual(budgets, BUDGET_CEILINGS)
        self.assertEqual(selection['requested'], 'max')
        self.assertEqual(selection['effective'], 6)
        self.assertEqual(selection['selection_source'], 'profile_default')
        self.assertIsNone(selection['executor_limit'])
        self.assertFalse(selection['executor_limit_verified'])

    def test_project_setting_reaches_registry_and_recommendation(self):
        self.write({'schema_version': 1, 'cloud_concurrency': 3})
        self.assertEqual(registry(self.project)['budgets']['cloud_concurrency'], 3)
        result = recommend('implementation', parallel_parts=8, project=self.project)
        self.assertEqual(result['budgets']['cloud_concurrency'], 3)
        self.assertEqual(result['suggested_concurrency'], 3)
        self.assertEqual(result['concurrency']['selection_source'], 'project_config')

    def test_admission_guide_registry_call_preserves_the_project_setting(self):
        self.write({'schema_version': 1, 'cloud_concurrency': 1})
        guide = (PROJECT / 'docs/native-team-poc.md').read_text()
        calls = re.findall(r'`(native_team\.registry\([^`]*\))`', guide)
        self.assertTrue(calls, 'Admission guide must show project-aware registry construction')
        for expression in calls:
            call = ast.parse(expression, mode='eval').body
            forwards_project = (
                bool(call.args) and isinstance(call.args[0], ast.Name) and call.args[0].id == 'project'
            ) or any(key.arg == 'project' and isinstance(key.value, ast.Name)
                     and key.value.id == 'project' for key in call.keywords)
            # Resolve only this recognized path forwarding; never execute prose.
            selected = registry(self.project if forwards_project else None)
            self.assertEqual(selected['budgets']['cloud_concurrency'], 1, expression)

    def test_explicit_selection_and_reported_executor_limit_are_separate(self):
        self.write({'schema_version': 1, 'cloud_concurrency': 2})
        budgets, selection = configured_budgets(self.project, requested='max', executor_limit=4)
        self.assertEqual(budgets['cloud_concurrency'], 4)
        self.assertEqual(selection['configured'], 2)
        self.assertEqual(selection['requested'], 'max')
        self.assertEqual(selection['selection_source'], 'override')
        self.assertFalse(selection['executor_limit_verified'])
        self.assertEqual(configured_budgets(self.project, requested=5, executor_limit=2)[0]['cloud_concurrency'], 2)
        self.assertEqual(configured_budgets(self.project, executor_limit=100)[0]['cloud_concurrency'], 2)

    def test_invalid_or_oversized_settings_fail_instead_of_using_max(self):
        values = [None, {}, {'schema_version': True, 'cloud_concurrency': 'max'},
                  {'schema_version': 1, 'cloud_concurrency': True},
                  {'schema_version': 1, 'cloud_concurrency': 0},
                  {'schema_version': 1, 'cloud_concurrency': 7},
                  {'schema_version': 1, 'cloud_concurrency': 'automatic'},
                  {'schema_version': 1, 'cloud_concurrency': 'max', 'extra': 1}]
        for value in values:
            self.write(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                configured_budgets(self.project)
        self.config.write_text(' ' * 4097)
        with self.assertRaises(ValueError):
            configured_budgets(self.project)
        self.config.write_text('{"schema_version":1,"cloud_concurrency":1,"cloud_concurrency":6}')
        with self.assertRaises(ValueError):
            configured_budgets(self.project)
        self.config.write_text('[' * 1500 + '0' + ']' * 1500)
        with self.assertRaises(ValueError):
            configured_budgets(self.project)

    def test_invalid_overrides_and_executor_limits_fail(self):
        for value in [0, 7, True, 2.5, '6', 'unknown']:
            with self.subTest(requested=value), self.assertRaises(ValueError):
                configured_budgets(self.project, requested=value)
        for value in [0, -1, True, 2.5, '6']:
            with self.subTest(executor_limit=value), self.assertRaises(ValueError):
                configured_budgets(self.project, executor_limit=value)

    def test_symlink_settings_are_not_followed(self):
        target = self.project / 'external.json'
        target.write_text('{"schema_version":1,"cloud_concurrency":"max"}')
        self.config.symlink_to(target)
        with self.assertRaises(ValueError):
            configured_budgets(self.project)

    def test_cli_applies_project_max_and_lower_explicit_limit_without_starting_work(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            code = cli.main(['--purpose', 'implementation', '--parallel-parts', '8',
                             '--concurrency', 'max', '--executor-limit', '2'])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['budgets']['cloud_concurrency'], 2)
        self.assertEqual(result['suggested_concurrency'], 2)
        self.assertFalse(result['concurrency']['executor_limit_verified'])
        self.assertFalse(result['automatic_dispatch'])
        self.assertFalse(result['availability_verified'])


class ModelHandoffTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ParentAdapterTests('test_spawn_id_scope_and_duplicate_ack')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_selected_project_concurrency_controls_admission_and_stays_frozen(self):
        project = self.f.f.project
        path = project / 'config/native-team.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text('{"schema_version":1,"cloud_concurrency":1}')
        spec = deepcopy(self.f.spec)
        spec['registry'] = registry(project)
        dispatcher = NativeDispatcher.receive(self.f.f.root / 'configured-run', spec,
                                              'fixture', clock=self.f.clock)
        adapter = NativeParentAdapter.configure(dispatcher, '/root', step_limit=30)
        calls = adapter.step()['tool_calls']
        self.assertEqual(len([c for c in calls if c['tool'] == 'collaboration.spawn_agent']), 1)
        self.assertEqual(dispatcher.snapshot()['spec']['registry']['budgets']['cloud_concurrency'], 1)
        path.write_text('{"schema_version":1,"cloud_concurrency":3}')
        self.assertEqual(registry(project)['budgets']['cloud_concurrency'], 3)
        self.assertEqual(dispatcher.snapshot()['spec']['registry']['budgets']['cloud_concurrency'], 1)

    def new_adapter(self, role, suffix):
        spec = deepcopy(self.f.spec)
        spec['tasks'] = spec['tasks'][:1]
        spec['tasks'][0].update(role=role, allowed=[])
        dispatcher = NativeDispatcher.receive(self.f.f.root / ('roles-' + suffix), spec, suffix,
                                               clock=self.f.clock)
        return NativeParentAdapter.configure(dispatcher, '/root')

    def test_every_leaf_role_reaches_explicit_spawn_contract_and_fixed_context(self):
        for role in set(ROLES) - {'Coordinator'}:
            with self.subTest(role=role):
                adapter = self.new_adapter(role, role)
                call = adapter.step()['tool_calls'][0]
                self.assertEqual(call['arguments']['model'], 'gpt-6.1-sol')
                self.assertEqual(call['arguments']['reasoning_effort'], 'max')
                self.assertEqual(call['arguments']['fork_turns'], 'none')
                ref = call['context_ref']
                message = adapter.context(ref['key'], ref['sha256'])['message']
                prompt = json.loads(message.split('\nTASK\n')[1])
                self.assertEqual(prompt['role'], role)
                self.assertEqual(prompt['model'], 'gpt-6.1-sol')
                self.assertEqual(prompt['reasoning_effort'], 'max')

    def test_fallback_is_an_explicit_new_profile_choice_never_a_failed_spawn_retry(self):
        # Availability/permission is the parent's pre-dispatch responsibility.
        path = self.f.f.project / '.codex/agents/researcher.toml'
        path.write_text(path.read_text().replace('gpt-6.1-sol', 'gpt-6-luna'))
        adapter = self.new_adapter('Researcher', 'fallback')
        first = adapter.step()['tool_calls'][0]
        self.assertEqual(first['arguments']['model'], 'gpt-6-luna')
        self.assertEqual(first['arguments']['reasoning_effort'], 'max')
        again = adapter.step()
        self.assertEqual(again['tool_calls'], [])
        self.assertEqual(len(again['pending_reconciliation']), 1)
        self.assertEqual(adapter.team.snapshot()['plan']['dispatches'], 1)

    def test_model_or_effort_change_after_configuration_blocks_before_admission(self):
        adapter = self.new_adapter('Researcher', 'pinned')
        path = self.f.f.project / '.codex/agents/researcher.toml'
        before = adapter.team.snapshot()
        path.write_text(path.read_text().replace('gpt-6.1-sol', 'gpt-6-luna'))
        with self.assertRaisesRegex(AdapterError, 'ROLE_PIN'):
            adapter.step()
        self.assertEqual(adapter.team.snapshot(), before)

    def test_missing_unsupported_or_extra_settings_fail_closed(self):
        path = self.f.f.project / '.codex/agents/researcher.toml'
        source = path.read_text()
        for i, replacement in enumerate([
                source.replace('gpt-6.1-sol', 'gpt-6-astra'),
                source.replace('gpt-6.1-sol', 'gpt-6-sol'),
                source.replace('model_reasoning_effort = "max"', 'model_reasoning_effort = "high"'),
                source.replace('model = "gpt-6.1-sol"\n', ''),
                source + '\nsandbox_mode = "danger-full-access"\n']):
            path.write_text(replacement)
            with self.assertRaisesRegex(AdapterError, 'UNSUPPORTED_ROLE_SETTINGS'):
                self.new_adapter('Researcher', 'bad-' + str(i))
            path.write_text(source)

    def test_v3_run_is_not_silently_migrated_to_explicit_models(self):
        adapter = self.new_adapter('Researcher', 'old-v3')
        with adapter.team._change() as (data, _, _):
            data['dispatcher']['parent_adapter']['profile'] = 'collaboration-parent-v3'
        before = adapter.team.snapshot()
        with self.assertRaisesRegex(AdapterError, 'NOT_CONFIGURED'):
            NativeParentAdapter(adapter.dispatcher)
        self.assertEqual(adapter.team.snapshot(), before)

    def test_tool_contract_rejects_implicit_or_unapproved_models(self):
        call = self.new_adapter('Researcher', 'contract').step()['tool_calls'][0]
        for key, value in [('model', 'gpt-6-astra'), ('reasoning_effort', 'high'), ('fork_turns', 'all')]:
            args = deepcopy(call['arguments'])
            args[key] = value
            with self.assertRaises(AdapterError):
                NativeParentAdapter._call(call['tool'], args, call['action_ids'])
        args = deepcopy(call['arguments'])
        del args['model']
        with self.assertRaises(AdapterError):
            NativeParentAdapter._call(call['tool'], args, call['action_ids'])

    def test_six_workers_can_be_admitted_but_unknown_worker_does_not_free_a_slot(self):
        fixture = self.f.f
        spec = deepcopy(self.f.spec)
        spec['registry']['budgets']['cloud_concurrency'] = 6
        spec['tasks'] = []
        for index in range(7):
            workspace = fixture.root / ('worker-' + str(index))
            fixture.git(fixture.project, 'worktree', 'add', '--quiet', '--detach', str(workspace), 'HEAD')
            spec['tasks'].append({'id': str(index), 'role': 'Researcher', 'backend': 'cloud',
                                  'deps': [], 'workspace': str(workspace), 'instruction': 'Read only.', 'allowed': []})
        team = NativeTeam.create(fixture.root / 'six-workers', spec, clock=self.f.clock)
        for index in range(6):
            ticket = team.admit(str(index), 'parent')
            team.bind(str(index), ticket['dispatch_id'], 'agent-' + str(index))
        first = team.snapshot()['bindings']['0']
        team.unknown('0', first['dispatch_id'], 'Uncertain native observation')
        with self.assertRaisesRegex(ValueError, 'CONCURRENCY'):
            team.admit('6', 'parent')
        team.acknowledge_release('0', first['dispatch_id'], 'agent-0', 'Observed actual terminal')
        self.assertTrue(team.admit('6', 'parent')['dispatch_id'])
        self.assertEqual(team.snapshot()['plan']['tasks']['0']['state'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
