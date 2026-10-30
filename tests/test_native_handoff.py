"""Incident-driven offline handoff tests. No native tool/model execution."""
from copy import deepcopy
from pathlib import Path
import json
import io
import hashlib
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
import sys
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT / 'src'), str(PROJECT / 'tests')]
import test_native_parent_adapter as fixtures
from native_dispatcher import NativeDispatcher
from native_parent_adapter import NativeParentAdapter
from native_tool_driver import NativeToolDriver
from native_handoff import HandoffStore, encoded


class MechanicalHandoffTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ParentAdapterTests('test_spawn_id_scope_and_duplicate_ack')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base = self.fixture.f
        self.clock = self.fixture.clock
        spec = deepcopy(self.fixture.spec)
        spec['tasks'] = spec['tasks'][:1]
        self.dispatcher = NativeDispatcher.receive(self.base.root / 'mechanical', spec, 'one', clock=self.clock)
        self.adapter = NativeParentAdapter.configure(self.dispatcher, '/root', 30)
        self.calls = []
        self.driver = NativeToolDriver(self.adapter, self.port)

    def state(self):
        return self.adapter.team.snapshot()

    def port(self, tool, arguments):
        self.calls.append((tool, deepcopy(arguments)))
        if tool.endswith('spawn_agent'):
            # Mechanically decode the short bootstrap's JSON, then load pinned
            # bytes; no policy/role/message transcription is performed here.
            reference = json.loads(next(line for line in arguments['message'].splitlines() if line.startswith('{')))
            context = self.adapter.context(**reference)
            self.assertIn(self.fixture.bundle.text, context['message'])
            return {'task_name': '/root/' + arguments['task_name']}
        if tool.endswith('list_agents'):
            binding = self.state()['bindings']['a']
            task = self.state()['plan']['tasks']['a']
            (Path(task['workspace']) / 'result.txt').write_text('requirement checked')
            return {'agents': [{'agent_name': binding['agent_id'],
                                'agent_status': {'completed': 'fixture result; no trustworthy completion timestamp'}}]}
        return {'unverified_interrupt_reply': True}

    def spawn(self):
        result = self.driver.tick()
        self.assertEqual(len(self.calls), 1)
        return result

    def test_missing_real_tool_port_fails_before_admission(self):
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'NOT_CONNECTED'):
            NativeToolDriver(self.adapter, None).tick()
        self.assertEqual(self.state(), before)
        self.assertEqual(self.calls, [])

    def test_policy_link_transcription_fault_is_rejected_before_tool_call(self):
        self.fixture.bundle.text = 'policy [orchestration](orchestration.md#operating-principles)'
        call = self.adapter.step()['tool_calls'][0]
        reference = call['context_ref']
        path = self.adapter.handoffs._path('task', reference['key'])
        raw = path.read_text().replace('(orchestration.md#', '(../docs/ai/orchestration.md#')
        path.write_text(raw)
        with self.assertRaisesRegex(ValueError, 'CONTENT_MISMATCH'):
            self.driver.execute(call)
        self.assertEqual(self.calls, [])

    def test_bootstrap_or_arguments_change_cannot_be_rehashed_by_caller(self):
        call = self.adapter.step()['tool_calls'][0]
        call['arguments']['message'] += ' changed'
        with self.assertRaisesRegex(ValueError, 'CANONICAL_CALL_CHANGED'):
            self.driver.execute(call)
        self.assertEqual(self.calls, [])

    def test_canonical_spawn_result_is_bound_without_id_transcription(self):
        result = self.spawn()
        binding = self.state()['bindings']['a']
        self.assertEqual(binding['agent_id'], '/root/' + self.calls[0][1]['task_name'])
        capture = self.driver.store.read(result['captures'][0]['capture_ref'])
        request = self.driver.store.read(capture['request'])
        self.assertEqual(request['arguments'], self.calls[0][1])
        self.assertIsNone(capture['provider_completed_at'])

    def test_context_rejects_wrong_hash_path_traversal_and_late_load(self):
        call = self.adapter.step()['tool_calls'][0]
        ref = call['context_ref']
        with self.assertRaises(ValueError):
            self.adapter.context(ref['key'], '0' * 64)
        with self.assertRaises(ValueError):
            self.adapter.context('../outside', ref['sha256'])
        self.base.clock[0] += 301
        with self.assertRaisesRegex(ValueError, 'DEADLINE'):
            self.adapter.context(ref['key'], ref['sha256'])

    def test_artifacts_reject_symlinks_size_changes_and_content_conflicts(self):
        store = self.adapter.handoffs
        ref = store.put('task', 'a' * 64, {'message': 'fixed'})
        with self.assertRaisesRegex(ValueError, 'CONTENT_MISMATCH'):
            store.put('task', 'a' * 64, {'message': 'changed'})
        with self.assertRaisesRegex(ValueError, 'SIZE_LIMIT'):
            store.put('task', 'b' * 64, {'message': 'x' * 65536})
        path = store._path('task', ref['key'])
        original = path.with_suffix('.original')
        path.rename(original)
        path.symlink_to(original)
        with self.assertRaises(ValueError):
            store.read(ref)

    def test_lost_native_return_is_unknown_and_never_spawns_twice(self):
        def lost(tool, arguments):
            self.calls.append(tool)
            raise OSError('private exception body must not be stored')
        self.driver.port = lost
        call = self.adapter.step()['tool_calls'][0]
        result = self.driver.execute(call)
        self.assertFalse(result['registration']['applied'])
        self.assertEqual(self.state()['plan']['tasks']['a']['state'], 'UNKNOWN')
        with self.assertRaises(ValueError):
            self.driver.execute(call)
        self.assertEqual(len(self.calls), 1)
        serialized = self.driver.store.read(result['capture_ref'])
        self.assertNotIn('private exception body', json.dumps(serialized))

    def test_executor_argument_mutation_is_not_adopted(self):
        def mutate(tool, arguments):
            self.calls.append(tool)
            arguments['message'] += ' injected'
            return {'task_name': '/root/' + arguments['task_name']}
        self.driver.port = mutate
        result = self.driver.tick()
        self.assertFalse(result['captures'][0]['registration']['applied'])
        self.assertIsNone(self.state()['bindings']['a']['agent_id'])
        self.assertEqual(self.state()['plan']['tasks']['a']['state'], 'UNKNOWN')

    def test_early_poll_return_with_delayed_registration_stays_timed_out(self):
        self.spawn()
        self.base.clock[0] = 1100
        ingest = self.driver.ingest
        def delayed(reference):
            self.base.clock[0] = 1400
            return ingest(reference)
        with patch.object(self.driver, 'ingest', side_effect=delayed):
            result = self.driver.tick()
        state = self.state()
        self.assertEqual(state['plan']['tasks']['a']['state'], 'TIMED_OUT')
        self.assertTrue(state['bindings']['a']['released'])
        fact = state['dispatcher']['parent_adapter']['captures'][result['captures'][0]['capture_ref']['key']]
        self.assertEqual(fact['tool_finished_at'], 1100)
        self.assertEqual(fact['registered_at'], 1400)
        timing = next(iter(fact['deadlines'].values()))
        self.assertFalse(timing['tool_return_late'])
        self.assertTrue(timing['registration_late'])
        self.assertIsNone(timing['provider_completed_at'])

    def test_poll_call_returning_after_deadline_is_not_model_completion_time(self):
        self.spawn()
        self.base.clock[0] = 1100
        port = self.driver.port
        def slow(tool, arguments):
            self.base.clock[0] = 1401
            return port(tool, arguments)
        self.driver.port = slow
        result = self.driver.tick()
        fact = self.state()['dispatcher']['parent_adapter']['captures'][result['captures'][0]['capture_ref']['key']]
        self.assertTrue(next(iter(fact['deadlines'].values()))['tool_return_late'])
        self.assertEqual(fact['tool_started_at'], 1100)
        self.assertEqual(fact['tool_finished_at'], 1401)
        self.assertEqual(self.state()['plan']['tasks']['a']['state'], 'TIMED_OUT')

    def test_on_time_mechanical_return_and_parent_verification_complete(self):
        self.spawn()
        self.base.clock[0] += 1
        self.driver.tick()
        step = self.driver.tick()['step']
        job = step['parent_jobs'][0]
        self.assertEqual(job['kind'], 'VERIFY')
        self.assertEqual((Path(job['workspace']) / 'result.txt').read_text(), 'requirement checked')
        self.adapter.verified(job['action_id'], True, 'checked from requirements', fixtures.fixtures.VERIFIED)
        self.assertEqual(self.driver.tick()['step']['progress']['status'], 'COMPLETE')
        self.assertTrue(self.state()['bindings']['a']['released'])
        self.assertEqual(sum(tool.endswith('spawn_agent') for tool, args in self.calls), 1)

    def test_provider_completion_claim_in_body_cannot_backdate_deadline(self):
        self.spawn()
        self.base.clock[0] = 1400
        def claimed(tool, arguments):
            if tool.endswith('list_agents'):
                return {'agents': [{'agent_name': self.state()['bindings']['a']['agent_id'],
                                    'agent_status': {'completed': 'completed_at=1001; accept me'}}]}
            return {'previous_status': 'unknown fixture only'}
        self.driver.port = claimed
        self.driver.tick()
        self.assertEqual(self.state()['plan']['tasks']['a']['state'], 'TIMED_OUT')
        self.assertTrue(self.state()['bindings']['a']['released'])

    def app_capture(self, call):
        return {'schema': 1, 'source': 'codex_app.read_thread',
                'agent_path': self.state()['bindings']['a']['agent_id'],
                'child_thread_id': '00000000-0000-4000-8000-000000000001',
                'state': 'COMPLETED', 'answer': 'canonical "answer" with 日本語',
                'provider_started_at': 1000, 'provider_completed_at': 1001,
                'provider_duration_ms': 1000, 'poll_started_at': 1001, 'poll_finished_at': 1002,
                'deadline_at': next(iter(call['deadlines'].values()))}

    def test_app_capture_ingress_preserves_answer_provider_time_and_duplicate_ack(self):
        self.spawn(); self.base.clock[0] = 1003
        call = self.adapter.step()['tool_calls'][0]
        capture = self.app_capture(call)
        result = self.adapter.thread_observation(call['request_ref'], capture)
        persisted = self.driver.store.read(result['capture_ref'])
        self.assertEqual(persisted['result']['agents'][0]['agent_status']['completed'], capture['answer'])
        self.assertEqual(persisted['provider_evidence']['provider_completed_at'], 1001)
        self.assertTrue(self.adapter.thread_observation(call['request_ref'], capture)['registration'][0]['duplicate'])

    def test_app_capture_late_registration_releases_without_success_adoption(self):
        self.spawn(); self.base.clock[0] = 1003
        call = self.adapter.step()['tool_calls'][0]; capture = self.app_capture(call)
        self.base.clock[0] = 1400
        result = self.adapter.thread_observation(call['request_ref'], capture)
        self.assertEqual(self.state()['plan']['tasks']['a']['state'], 'TIMED_OUT')
        self.assertTrue(self.state()['bindings']['a']['released'])
        timings = self.state()['dispatcher']['parent_adapter']['captures'][result['capture_ref']['key']]['deadlines']
        self.assertEqual(next(iter(timings.values()))['provider_completed_at'], 1001)

    def test_app_capture_wrong_agent_or_deadline_is_rejected_before_state_write(self):
        self.spawn(); self.base.clock[0] = 1003
        call = self.adapter.step()['tool_calls'][0]
        before = self.state()
        for field, value in [('agent_path', '/root/unrelated'), ('deadline_at', 9999)]:
            capture = self.app_capture(call);capture[field] = value
            with self.assertRaises(ValueError):
                self.adapter.thread_observation(call['request_ref'], capture)
            self.assertEqual(self.state(), before)

    def test_two_executors_cannot_repeat_the_same_native_call(self):
        call = self.adapter.step()['tool_calls'][0]
        def simple(tool, arguments):
            self.calls.append(tool)
            return {'task_name': '/root/' + arguments['task_name']}
        self.driver.port = simple
        def attempt():
            try:
                return self.driver.execute(call)
            except (ValueError, RuntimeError):
                return 'reconciliation required; no replay'
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: attempt(), [1, 2]))
        self.assertEqual(len(self.calls), 1)

    def test_cli_context_reads_exact_canonical_body_without_prompt_transcription(self):
        call = self.adapter.step()['tool_calls'][0]
        reference = call['context_ref']
        payload = json.dumps({'action_id': reference['key'], 'sha256': reference['sha256']}).encode()
        output = io.StringIO()
        with patch.object(fixtures.cli, 'NativeTeam', return_value=self.adapter.team):
            code = fixtures.cli.main(['context', '--run', str(self.adapter.team.root)], io.BytesIO(payload), output)
        self.assertEqual(code, 0)
        returned = json.loads(output.getvalue())['result']
        self.assertEqual(returned['context_sha256'], reference['sha256'])
        self.assertEqual(returned, self.adapter.context(reference['key'], reference['sha256']))

    def test_bind_from_app_relation_does_not_transcribe_a_native_id(self):
        call = self.adapter.step()['tool_calls'][0]
        self.base.clock[0] = 1003
        capture = self.app_capture(call)
        capture['agent_path'] = call['expected_agent_name']
        result = self.adapter.bind_from_thread(call['request_ref'], capture)
        self.assertTrue(result['registration']['applied'])
        self.assertEqual(self.state()['bindings']['a']['agent_id'], capture['agent_path'])
        self.assertEqual(self.state()['dispatcher']['parent_adapter']['native_threads'][
            self.state()['bindings']['a']['dispatch_id']], capture['child_thread_id'])

    def test_fixed_code_mode_caller_through_real_cli_without_any_model(self):
        """Real JS -> quoted shell -> CLI -> durable state, fake read_thread only."""
        from common_policy import load_project_policy
        project = self.base.project
        (project / 'config').mkdir()
        (project / 'policies').mkdir()
        body = '# AI開発オーケストレーション統合指示 v1.0\n\nFixture read-only policy.\n'
        digest = hashlib.sha256(body.encode()).hexdigest()
        (project / 'policies.md').write_text(body)
        (project / 'policies/manifest.json').write_text(json.dumps({
            'schema_version': 1, 'version': '1.0', 'file': 'policies.md', 'sha256': digest}))
        (project / 'config/policy.json').write_text(json.dumps({'schema_version': 1,
            'bundle_root': '.', 'expected_version': '1.0', 'expected_sha256': digest}))
        bundle = load_project_policy(project)
        script = r'''
const fs=require('node:fs'), cp=require('node:child_process');
const cfg=JSON.parse(fs.readFileSync(0,'utf8'));
const m=require(cfg.module);
let commands=0,reads=0,pauseCalls=0;
const port=m.makeCliPort(async args=>{
  commands++;
  const r=cp.spawnSync('/bin/bash',['-c',args.cmd],{encoding:'utf8',timeout:10000});
  return {exit_code:r.status,output:r.stdout};
}, cfg.project, cfg.run);
async function go(){
  const ready=await m.prepareReadonly({runLocalAdapter:port});
  // Fixture boundary ONLY: no collaboration API is called. The child loader
  // and metadata are simulated; every subsequent local CLI call is real.
  const ref=JSON.parse(ready.arguments.message.split('\n').find(s=>s.startsWith('{')));
  const context=(await port('context',ref)).result;
  const task=JSON.parse(context.message.split('\nTASK\n')[1]);
  const answer=JSON.stringify({context_sha256:context.context_sha256,
    policy_version:task.policy.version,policy_sha256:task.policy.sha256,
    role:cfg.mode==='bad-answer'?'Implementer':'Researcher'});
  const pid='00000000-0000-4000-8000-000000000001';
  const cid='00000000-0000-4000-8000-000000000002';
  const tid='00000000-0000-4000-8000-000000000003';
  const end=Date.now()/1000;
  const api={runLocalAdapter:port,clock:()=>Date.now()/1000,
    pause:async()=>{pauseCalls++;},readThread:async args=>{
      reads++;
      return args.threadId===pid?{schemaVersion:1,thread:{id:pid},turns:[{id:tid,items:[{
        type:'subAgentActivity',agentPath:ready.expected_agent_name,agentThreadId:cid}]}]}:
        {schemaVersion:1,thread:{id:cid},turns:[{id:tid,status:'completed',startedAt:end-0.01,
          completedAt:end,durationMs:10,error:null,items:[{type:'agentMessage',phase:'final_answer',text:answer}]}]};
    }};
  if(cfg.mode==='dirty')fs.writeFileSync(task.workspace+'/unexpected.txt','out of scope');
  if(['lost-ack','lost-poll-ack'].includes(cfg.mode)){
    api.runLocalAdapter=async(op,body)=>{
      const r=await port(op,body);
      if(op===(cfg.mode==='lost-ack'?'bind_from_thread':'thread_observation'))throw Error('FIXTURE_ACK_LOST');
      return r;
    };
    try {await m.finishHistoricalReadonly(api,{parentThreadId:pid,spawnRequestRef:ready.request_ref});}
    catch(e){if(e.message!=='FIXTURE_ACK_LOST')throw e;}
    api.runLocalAdapter=port;
  }
  let result=await m.finishHistoricalReadonly(api,{parentThreadId:pid,spawnRequestRef:ready.request_ref});
  // A completed caller is safe to re-enter; it reads the same durable receipt.
  if(result.status==='SUCCEEDED'){
    const again=await m.finishHistoricalReadonly(api,{parentThreadId:pid,spawnRequestRef:ready.request_ref});
    if(JSON.stringify(again.receipt)!==JSON.stringify(result.receipt))throw Error('RECEIPT_CHANGED');
  }
  console.log(JSON.stringify({result,commands,reads,pauseCalls,model_calls:0}));
}
go().catch(e=>{console.error(e.message);process.exitCode=1;});
'''
        for mode in ['success', 'bad-answer', 'dirty', 'lost-ack', 'lost-poll-ack']:
            with self.subTest(mode=mode):
                spec = deepcopy(self.fixture.spec)
                spec['tasks'] = spec['tasks'][:1]
                spec['tasks'][0].update(role='Researcher', allowed=[],
                    instruction='Return only JSON with context_sha256 from context loader, policy_version, policy_sha256 and role Researcher.')
                spec['policy'] = {'version': bundle.version, 'sha256': bundle.sha256}
                spec['registry']['budgets'].update(cloud_concurrency=1, max_dispatches=1,
                    max_retries=0, task_timeout_s=120, run_timeout_s=120)
                root = self.base.root / ("caller ' $(literal) " + mode)
                with patch('native_team.load_project_policy', return_value=bundle), patch(
                        'native_parent_adapter.load_project_policy', return_value=bundle):
                    adapter = NativeParentAdapter.configure(NativeDispatcher.receive(root, spec, mode), '/root', 100)
                proc = subprocess.run(['node', '-e', script], input=json.dumps({
                    'module': str(PROJECT / 'scripts/native_thread_capture.cjs'),
                    'project': str(PROJECT), 'run': str(root), 'mode': mode}),
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                result = json.loads(proc.stdout)
                snapshot = adapter.team.snapshot()
                self.assertEqual(result['model_calls'], 0)
                self.assertEqual(snapshot['plan']['dispatches'], 1)
                if mode in ['success', 'lost-ack', 'lost-poll-ack']:
                    self.assertEqual(result['result']['status'], 'SUCCEEDED')
                    self.assertTrue(result['result']['receipt']['native_terminal_acknowledged'])
                    self.assertTrue(snapshot['bindings']['a']['released'])
                elif mode == 'bad-answer':
                    self.assertEqual(snapshot['plan']['tasks']['a']['state'], 'FAILED')
                    self.assertTrue(snapshot['bindings']['a']['released'])
                else:
                    self.assertNotEqual(result['result']['status'], 'SUCCEEDED')
                    self.assertFalse(snapshot['bindings']['a']['released'])
                (self.base.workspaces['a'] / 'unexpected.txt').unlink(missing_ok=True)

    def test_fixed_readonly_scope_rejects_general_tasks_before_admission(self):
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'READONLY_SMOKE_SCOPE_REQUIRED'):
            self.adapter.readonly_prepare()
        self.assertEqual(self.state(), before)

    def test_parent_standard_returns_bind_poll_verify_without_thread_projection(self):
        spec = deepcopy(self.fixture.spec)
        spec['tasks'] = spec['tasks'][:1]
        spec['tasks'][0].update(role='Researcher', allowed=[])
        spec['registry']['budgets'].update(cloud_concurrency=1, max_dispatches=1,
            max_retries=0, task_timeout_s=120, run_timeout_s=120)
        adapter = NativeParentAdapter.configure(NativeDispatcher.receive(
            self.base.root / 'parent-return-run', spec, 'parent-return', clock=self.clock), '/root', 100)
        spawn = adapter.readonly_prepare()['tool_calls'][0]
        raw_spawn = {'task_name': spawn['expected_agent_name']}
        bound = adapter.parent_result(spawn['request_ref'], raw_spawn)
        self.assertTrue(bound['registration']['applied'])
        self.assertTrue(adapter.parent_result(spawn['request_ref'], raw_spawn)['registration']['duplicate'])
        self.base.clock[0] = 1001
        running = adapter.step()['tool_calls'][0]
        adapter.parent_result(running['request_ref'], {'agents': [{
            'agent_name': spawn['expected_agent_name'], 'agent_status': 'running'}]})
        self.base.clock[0] = 1003
        poll = adapter.step()['tool_calls'][0]
        answer = json.dumps({'context_sha256': spawn['context_ref']['sha256'],
            'policy_version': spec['policy']['version'], 'policy_sha256': spec['policy']['sha256'],
            'role': 'Researcher'})
        raw_poll = {'agents': [{'agent_name': spawn['expected_agent_name'],
                               'agent_status': {'completed': answer}},
                              {'agent_name': '/root/unrelated', 'agent_status': {'completed': 'PRIVATE'}}]}
        observed = adapter.parent_result(poll['request_ref'], raw_poll)
        self.assertTrue(observed['registration'][0]['applied'])
        persisted = adapter.handoffs.read(observed['capture_ref'])
        self.assertNotIn('PRIVATE', json.dumps(persisted))
        for name in ['tool_started_at', 'tool_finished_at', 'captured_at', 'provider_completed_at']:
            self.assertIsNone(persisted[name])
        job = adapter.step()['parent_jobs'][0]
        # Re-enter with no local capture_ref after a saved POLL ACK was lost.
        verified = adapter.readonly_verified(job['action_id'], None, spawn['request_ref'])
        self.assertEqual(verified['state'], 'SUCCEEDED')
        state = adapter.team.snapshot()
        self.assertTrue(state['bindings']['a']['released'])
        self.assertEqual(state['plan']['dispatches'], 1)
        self.assertEqual(state['receipts']['a']['agent_id'], raw_spawn['task_name'])
        clock = state['dispatcher']['parent_adapter']['captures'][observed['capture_ref']['key']]
        self.assertEqual(clock['registered_at'], 1003)
        self.assertIsNone(next(iter(clock['deadlines'].values()))['tool_return_late'])

    def test_parent_receipt_rejects_wait_notification_wrong_id_and_changed_delivery(self):
        call = self.adapter.step()['tool_calls'][0]
        before = self.state()
        for reply in [{'task_name': '/root/wrong'}, {'summary': 'mailbox updated'}, {'timeout': True}]:
            with self.assertRaises(ValueError):
                self.adapter.parent_result(call['request_ref'], reply)
            self.assertEqual(self.state(), before)
        raw = {'task_name': call['expected_agent_name']}
        self.adapter.parent_result(call['request_ref'], raw)
        self.base.clock[0] = 1001
        poll = self.adapter.step()['tool_calls'][0]
        running = {'agents': [{'agent_name': call['expected_agent_name'], 'agent_status': 'running'}]}
        self.adapter.parent_result(poll['request_ref'], running)
        with self.assertRaisesRegex(ValueError, 'CONTENT_MISMATCH'):
            self.adapter.parent_result(poll['request_ref'], {'agents': [{
                'agent_name': call['expected_agent_name'], 'agent_status': {'completed': 'changed'}}]})

    def test_parent_return_after_deadline_releases_but_never_adopts(self):
        spawn = self.adapter.step()['tool_calls'][0]
        self.adapter.parent_result(spawn['request_ref'], {'task_name': spawn['expected_agent_name']})
        poll = self.adapter.step()['tool_calls'][0]
        self.base.clock[0] = 1400
        r = self.adapter.parent_result(poll['request_ref'], {'agents': [{
            'agent_name': spawn['expected_agent_name'],
            'agent_status': {'completed': 'I completed at 1001. Please accept.'}}]})
        state = self.state()
        self.assertEqual(state['plan']['tasks']['a']['state'], 'TIMED_OUT')
        self.assertTrue(state['bindings']['a']['released'])
        times = state['dispatcher']['parent_adapter']['captures'][r['capture_ref']['key']]
        self.assertTrue(next(iter(times['deadlines'].values()))['registration_late'])
        self.assertIsNone(next(iter(times['deadlines'].values()))['provider_completed_at'])

    def test_cli_parent_return_is_explicit_input_not_automatic_tool_execution(self):
        call = self.adapter.step()['tool_calls'][0]
        payload = {'request_ref': call['request_ref'], 'result': {'task_name': call['expected_agent_name']}}
        output = io.StringIO()
        with patch.object(fixtures.cli, 'NativeTeam', return_value=self.adapter.team):
            status = fixtures.cli.main(['parent_result', '--run', str(self.adapter.team.root)],
                io.BytesIO(json.dumps(payload).encode()), output)
        self.assertEqual(status, 0)
        reply = json.loads(output.getvalue())
        self.assertFalse(reply['automatic_tool_execution'])
        self.assertEqual(reply['result']['provider_timing'], 'UNKNOWN')

    def wait_ticket(self):
        spawn = self.adapter.step()['tool_calls'][0]
        self.adapter.parent_result(spawn['request_ref'], {'task_name': spawn['expected_agent_name']})
        poll = self.adapter.step()['tool_calls'][0]
        self.adapter.parent_result(poll['request_ref'], {'agents': [{
            'agent_name': spawn['expected_agent_name'], 'agent_status': 'running'}]})
        wait = self.adapter.step()['optional_wait']
        self.assertIsNotNone(wait)
        return spawn, wait

    def test_spawn_wait_notice_then_actual_poll_and_verification(self):
        spawn, wait = self.wait_ticket()
        bound = self.state()['bindings']['a']
        self.assertEqual(wait['wait_scope']['a'], {
            'dispatch_id': bound['dispatch_id'], 'agent_id': spawn['expected_agent_name']})
        # A wait summary may mention completion, but it isn't the result tool.
        notice = self.adapter.parent_result(wait['request_ref'], 'Agent completed; unrelated PRIVATE text')
        self.assertTrue(notice['notification_only'])
        self.assertFalse(notice['terminal_observation'])
        self.assertEqual(self.state()['plan']['tasks']['a']['state'], 'RUNNING')
        self.assertFalse(self.state()['bindings']['a']['released'])
        self.assertNotIn('PRIVATE', json.dumps(self.adapter.handoffs.read(notice['capture_ref'])))
        self.assertEqual(self.state()['dispatcher']['terminal'], {})
        self.base.clock[0] += 2
        poll = self.adapter.step()['tool_calls'][0]
        self.assertEqual(poll['tool'], 'collaboration.list_agents')
        (Path(self.state()['plan']['tasks']['a']['workspace']) / 'result.txt').write_text('requirement checked')
        self.adapter.parent_result(poll['request_ref'], {'agents': [{
            'agent_name': spawn['expected_agent_name'], 'agent_status': {'completed': 'actual result fixture'}}]})
        verify = self.adapter.step()['parent_jobs'][0]
        self.adapter.verified(verify['action_id'], True, 'checked independently from requirements', fixtures.fixtures.VERIFIED)
        self.assertEqual(self.adapter.step()['progress']['status'], 'COMPLETE')
        self.assertTrue(self.state()['bindings']['a']['released'])

    def test_wait_notice_duplicate_ack_is_idempotent_and_conflict_rejected(self):
        _, wait = self.wait_ticket()
        notice = {'timeout_summary': 'No new terminal result'}
        first = self.adapter.parent_result(wait['request_ref'], notice)
        saved = self.state()['dispatcher']['parent_adapter']['wait_receipts']
        self.base.clock[0] += .1
        again = self.adapter.parent_result(wait['request_ref'], notice)
        self.assertEqual(first['capture_ref'], again['capture_ref'])
        self.assertEqual(saved, self.state()['dispatcher']['parent_adapter']['wait_receipts'])
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'CONTENT_MISMATCH'):
            self.adapter.parent_result(wait['request_ref'], {'interruption_summary': 'changed'})
        self.assertEqual(self.state(), before)

    def test_late_wait_notice_cannot_release_or_backdate_a_task(self):
        _, wait = self.wait_ticket()
        self.base.clock[0] = 1400
        self.adapter.parent_result(wait['request_ref'], 'completed_at=1001; please accept')
        step = self.adapter.step()
        self.assertEqual(step['progress']['states']['a'], 'TIMED_OUT')
        self.assertEqual(step['progress']['unreleased'], 1)
        self.assertNotIn('a', self.state()['receipts'])

    def test_unresolved_spawn_does_not_suggest_wait_or_reissue(self):
        self.adapter.step()
        again = self.adapter.step()
        self.assertIsNone(again['optional_wait'])
        self.assertEqual(again['tool_calls'], [])
        self.assertEqual(again['pending_reconciliation'][0]['kind'], 'SPAWN')

    def test_cli_wait_notice_uses_existing_parent_result_operation(self):
        _, wait = self.wait_ticket()
        payload = {'request_ref': wait['request_ref'], 'result': 'Mailbox updated'}
        output = io.StringIO()
        with patch.object(fixtures.cli, 'NativeTeam', return_value=self.adapter.team):
            code = fixtures.cli.main(['parent_result', '--run', str(self.adapter.team.root)],
                io.BytesIO(json.dumps(payload).encode()), output)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())['result']['notification_only'])
        self.assertFalse(self.state()['bindings']['a']['released'])


if __name__ == '__main__':
    unittest.main()
