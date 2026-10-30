"""Actual runner/protocol assembly with an offline peer, never a real model."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'src'))
import runner as runner_module
from runner import Runner
from common_policy import load_project_policy, PolicyError


class PolicyRunnerTests(unittest.TestCase):
    def setUp(self):
        (PROJECT/'.runtime').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='policy-protocol-', dir=PROJECT/'.runtime')
        self.root = Path(self.temp.name)
        self.launches = []
        def launcher(root, binary, args):
            self.launches.append(args)
            return [sys.executable, '-B', str(PROJECT/'tests/fake_codex.py'),
                    'normal', str(root/'child.pid')], {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}
        self.runner = Runner(self.root, sys.executable, launcher=launcher)
        self.version = patch.object(runner_module, 'binary_version', return_value='offline metadata stub')
        self.version.start()
        self.bundle = load_project_policy(PROJECT)
        (self.root/'workspace/evidence.txt').write_text('verified')
        self.acceptance = {'artifacts':[{'path':'evidence.txt','sha256':hashlib.sha256(b'verified').hexdigest()}]}

    def tearDown(self):
        self.version.stop()
        self.runner.store.close()
        self.temp.cleanup()

    def logs(self, task):
        return [json.loads(line) for line in Path(task['log_path']).read_text().splitlines()]

    def thread_request(self, task, method):
        return next(row['message']['params'] for row in self.logs(task)
                    if row.get('direction')=='send' and row['message'].get('method')==method)

    def assert_policy(self, params):
        text = params['developerInstructions']
        self.assertEqual(text.count(self.bundle.text), 1)
        self.assertIn(self.bundle.sha256, text)
        self.assertIn(runner_module.RESULT_INSTRUCTIONS, text)
        self.assertEqual(params['approvalPolicy'], 'never')

    def test_fresh_thread_contains_full_policy_and_provenance(self):
        task = self.runner.run(self.runner.store.submit('original task',acceptance=self.acceptance),5)
        self.assertEqual(task['state'],'COMPLETED',task['error'])
        self.assert_policy(self.thread_request(task,'thread/start'))
        execution = self.runner.store.execution(task['active_execution_id'])
        metadata = json.loads(execution['owner'])['common_policy']
        self.assertEqual(metadata,self.bundle.metadata())
        self.assertNotIn('text',metadata)
        turn = next(row['message']['params'] for row in self.logs(task)
                    if row.get('direction')=='send' and row['message'].get('method')=='turn/start')
        self.assertEqual(turn['input'],[{'type':'text','text':'original task'}])

    def test_resume_reloads_same_full_policy(self):
        source = self.runner.run(self.runner.store.submit('first',acceptance=self.acceptance),5)
        task = self.runner.run(self.runner.store.submit('resume',resume_of=source['id'],acceptance=self.acceptance),5)
        self.assertEqual(task['state'],'COMPLETED',task['error'])
        self.assert_policy(self.thread_request(task,'thread/resume'))

    def test_policy_failure_prevents_process_dispatch(self):
        with patch.object(runner_module,'load_project_policy',side_effect=PolicyError('pinned digest mismatch')):
            task = self.runner.run(self.runner.store.submit('must not launch'),5)
        self.assertEqual(task['state'],'FAILED')
        self.assertIn('pinned digest mismatch',task['error'])
        self.assertEqual(self.launches,[])
        self.assertEqual(self.runner.store.execution(task['active_execution_id'])['dispatched'],0)

    def test_resume_conflict_stops_before_server_launch(self):
        source = self.runner.run(self.runner.store.submit('source',acceptance=self.acceptance),5)
        execution = self.runner.store.execution(source['active_execution_id'])
        owner = json.loads(execution['owner']);owner['common_policy']['sha256']='0'*64
        with self.runner.store.db:
            self.runner.store.db.execute('UPDATE executions SET owner=? WHERE id=?',(json.dumps(owner),execution['id']))
        before=len(self.launches)
        task = self.runner.run(self.runner.store.submit('resume',resume_of=source['id'],acceptance=self.acceptance),5)
        self.assertEqual(task['state'],'FAILED')
        self.assertIn('conflicts',task['error'])
        self.assertEqual(len(self.launches),before)

    def test_legacy_resume_is_labeled_and_receives_current_policy(self):
        source = self.runner.run(self.runner.store.submit('legacy',acceptance=self.acceptance),5)
        execution = self.runner.store.execution(source['active_execution_id'])
        owner = json.loads(execution['owner']);owner.pop('common_policy')
        with self.runner.store.db:
            self.runner.store.db.execute('UPDATE executions SET owner=? WHERE id=?',(json.dumps(owner),execution['id']))
        task = self.runner.run(self.runner.store.submit('resume legacy',resume_of=source['id'],acceptance=self.acceptance),5)
        self.assertEqual(task['state'],'COMPLETED',task['error'])
        self.assert_policy(self.thread_request(task,'thread/resume'))
        self.assertTrue(any(row.get('policy_resume_source')=='legacy-unpinned' for row in self.logs(task)))

    def test_reviewer_receives_policy_and_independence_instructions(self):
        fixture=self.root/'fixtures/seed';fixture.mkdir(parents=True)
        (fixture/'evidence.txt').write_text('verified')
        source_id=self.runner.store.submit('implement',acceptance=self.acceptance,
                    workspace={'fixture':'seed','access':'read-only','role':'implementer','snapshot_id':None})
        source=self.runner.run(source_id,5)
        self.assertEqual(source['state'],'COMPLETED',source['error'])
        snap=self.runner.snapshot(source_id)
        review_id=self.runner.store.submit('independent review',acceptance=self.acceptance,
                    workspace={'fixture':None,'access':'read-only','role':'reviewer','snapshot_id':snap['snapshot_id']})
        task=self.runner.run(review_id,5)
        self.assertEqual(task['state'],'COMPLETED',task['error'])
        params=self.thread_request(task,'thread/start');self.assert_policy(params)
        self.assertIn('independent Reviewer',params['developerInstructions'])
        self.assertEqual(params['sandbox'],'read-only')


class PolicyCliTests(unittest.TestCase):
    def test_packet_and_hash_rejection_without_network(self):
        bundle=load_project_policy(PROJECT)
        command=[sys.executable,'-B',str(PROJECT/'scripts/policy_context.py')]
        result=subprocess.run(command+['--format','packet','--target','dot','--task','inspect'],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        packet=json.loads(result.stdout)
        self.assertIn(bundle.text,packet['developer_instructions'])
        self.assertEqual(packet['policy']['sha256'],bundle.sha256)
        rejected=subprocess.run(command+['--expect-sha256','0'*64],capture_output=True,text=True,timeout=5)
        self.assertEqual(rejected.returncode,2)
        self.assertEqual(rejected.stdout,'')


class PolicyReferenceTests(unittest.TestCase):
    def test_all_native_roles_keep_their_instructions_beside_full_policy(self):
        from common_policy import developer_instructions
        bundle=load_project_policy(PROJECT)
        for role in ['implementer','verifier','researcher','independent_reviewer']:
            with self.subTest(role=role):
                definition=tomllib.loads((PROJECT/f'.codex/agents/{role}.toml').read_text())
                instructions=definition['developer_instructions']
                result=developer_instructions(bundle,instructions)
                self.assertEqual(result.count(bundle.text),1)
                self.assertIn(instructions,result)
                self.assertEqual(definition['name'],role)
                self.assertNotIn('sandbox_mode',definition)

    def test_indexes_and_skill_are_references_not_a_second_policy_body(self):
        bundle=load_project_policy(PROJECT)
        paths=[PROJECT/'AGENTS.md',PROJECT/'.agents/skills/common-policy/SKILL.md',
               *list((PROJECT/'policies').glob('*.md'))]
        for path in paths:
            with self.subTest(path=path):
                text=path.read_text()
                self.assertNotIn(bundle.text,text)
                self.assertIn('policies.md',text)
        skill=(PROJECT/'.agents/skills/common-policy/SKILL.md').read_text()
        self.assertIn('scripts/policy_context.py --format inspect',skill)


if __name__=='__main__':
    unittest.main(verbosity=2)
