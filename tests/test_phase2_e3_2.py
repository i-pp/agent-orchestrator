"""Explicit PoC boundaries; no real authentication or model calls."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'src'))
import development_poc as poc
from runner import Runner


class FakeRunner(Runner):
    def launch_task(self,task,args):
        source=(PROJECT/'tests/fake_codex.py').read_text()
        return poc.launch(self.root,self.workroot(task),sys.executable,
            ['-B','-c',source,'normal','/tmp/child.pid'],access=task['access'])


class DevelopmentModeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=PROJECT/'.runtime',prefix='development-poc-test-')
        self.root=Path(self.temp.name);self.runner=None

    def tearDown(self):
        if self.runner:self.runner.store.close()
        self.temp.cleanup()

    def setup_runner(self):
        self.runner=FakeRunner(self.root,sys.executable,development=True)
        seed=self.root/'fixtures/seed';seed.mkdir(parents=True)
        for name in ['expected.txt','result.txt']:(seed/name).write_bytes(b'fixed fixture\n')
        return self.runner

    def task(self,access='read-only',wrong_hash=False):
        return self.runner.store.submit('synthetic PoC fixture',workspace={'fixture':'seed','access':access},
            acceptance={'artifacts':[{'path':'result.txt','sha256':hashlib.sha256(b'wrong' if wrong_hash else b'fixed fixture\n').hexdigest()}],
                        'tests':['workspace-result-check']})

    def test_explicit_opt_in_is_required_on_every_runner(self):
        self.setup_runner()
        with self.assertRaisesRegex(ValueError,'explicit'):Runner(self.root,sys.executable)
        self.assertEqual(json.loads((self.root/poc.MARKER).read_text())['mode'],poc.MODE)

    def test_existing_data_cannot_be_converted(self):
        (self.root/'existing.sqlite').write_bytes(b'preserved')
        with self.assertRaisesRegex(ValueError,'convert'):poc.configure(self.root,True)
        self.assertEqual((self.root/'existing.sqlite').read_bytes(),b'preserved')
        self.assertFalse((self.root/poc.MARKER).exists())

    def test_runtime_prefix_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=PROJECT/'.runtime',prefix='ordinary-') as other:
            with self.assertRaises(ValueError):poc.configure(Path(other),True)
        link=self.root.parent/('development-poc-link-'+uuid.uuid4().hex)
        link.symlink_to(self.root,target_is_directory=True)
        try:
            with self.assertRaises(ValueError):poc.configure(link,True)
            result=subprocess.run([sys.executable,'-B',str(PROJECT/'src/runner.py'),'--runtime',str(link),
                                   '--development-poc','submit','must not create state'],capture_output=True,text=True,timeout=5)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('symlinks',result.stderr)
            self.assertFalse((self.root/poc.MARKER).exists())
        finally:link.unlink()

    def test_invalid_or_shared_marker_is_rejected(self):
        poc.configure(self.root,True);marker=self.root/poc.MARKER
        marker.chmod(0o644)
        with self.assertRaises(ValueError):poc.configure(self.root,True)
        marker.chmod(0o600);marker.write_text('{}')
        with self.assertRaises(ValueError):poc.configure(self.root,True)

    def test_nonfixture_tasks_and_resume_are_rejected(self):
        self.setup_runner();task=self.runner.store.submit('legacy')
        with self.assertRaisesRegex(ValueError,'fixtures'):self.runner.run(task)
        self.assertEqual(self.runner.store.get(task)['state'],'QUEUED')
        value=self.runner.store.get(self.task());value['resume_of']=str(uuid.uuid4())
        with self.assertRaises(ValueError):poc.validate_task(value)

    def test_workspace_escape_and_runtime_executable_are_rejected(self):
        self.setup_runner()
        with self.assertRaises(ValueError):poc.launch(self.root,self.root,sys.executable,[])
        root=self.runner.workroot(self.runner.store.get(self.task()))
        executable=self.root/'untrusted';executable.write_text('not executable')
        with self.assertRaises(ValueError):poc.launch(self.root,root,executable,[])

    def test_real_systemd_write_boundary_and_affinity(self):
        self.setup_runner();task=self.runner.store.get(self.task('workspace-write'))
        root=self.runner.workroot(task)
        code=("from pathlib import Path; import os; Path('created.txt').write_text('ok'); "
              "assert len(os.sched_getaffinity(0))==1; "
              f"assert not Path({str(Path.home()/'.codex')!r}).exists(); "
              f"assert not Path({str(self.root/'tasks.sqlite')!r}).exists(); "
              "print('boundary ok')")
        argv,env=poc.launch(self.root,root,sys.executable,['-B','-c',code],access='workspace-write')
        unit='agentorchestrator-e32-boundary-'+uuid.uuid4().hex+'.service'
        result=subprocess.run(['systemd-run','--user','--quiet','--wait','--pipe','--collect','--unit',unit,*argv],
                              capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('boundary ok',result.stdout)
        self.assertEqual((root/'workspace/created.txt').read_text(),'ok')
        self.assertNotIn('CONTROL_PLANE_API_KEY',env)

    def test_readonly_execution_cannot_create_workspace_files(self):
        self.setup_runner();root=self.runner.workroot(self.runner.store.get(self.task()))
        argv,env=poc.launch(self.root,root,sys.executable,['-B','-c',"from pathlib import Path; Path('forbidden').write_text('no')"])
        result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((root/'workspace/forbidden').exists())

    def test_missing_boundary_never_falls_back(self):
        self.setup_runner();task=self.task()
        with patch('development_poc.version',side_effect=FileNotFoundError('boundary missing')),patch('runner.isolated_launch') as old:
            result=self.runner.run(task)
            self.assertEqual(result['state'],'FAILED');old.assert_not_called()
        execution=self.runner.store.execution(result['active_execution_id'])
        self.assertEqual(execution['dispatched'],0)

    def test_mock_execution_and_independent_verification_are_persisted(self):
        self.setup_runner();task=self.task();result=self.runner.run(task,20)
        self.assertTrue(result['completion_verified'],result['error'])
        execution=self.runner.store.execution(result['active_execution_id'])
        self.assertEqual(json.loads(execution['owner'])['execution_mode'],poc.MODE)
        log=Path(result['log_path']).read_text()
        self.assertIn('externalSandbox',log)
        self.assertNotIn('dangerFullAccess',log)
        self.runner.store.close();self.runner=FakeRunner(self.root,sys.executable,development=True)
        self.assertEqual(self.runner.store.get(task)['state'],'COMPLETED')

    def test_mock_codex_success_does_not_override_bad_artifact(self):
        self.setup_runner();result=self.runner.run(self.task(wrong_hash=True),20)
        self.assertEqual(result['codex_status'],'SUCCEEDED')
        self.assertEqual(result['state'],'FAILED')
        self.assertEqual(result['verification_status'],'FAILED')
