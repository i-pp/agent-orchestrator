"""Offline integration checks: no model requests, services or extra isolation."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'src'),str(PROJECT/'scripts')]
import mcp_adapter as mcp
from poc_services import Services

class LocalLlmMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        (self.root/'logs').mkdir(mode=0o700)
        self.helpers = self.root/'helpers'; self.helpers.mkdir()
        (self.helpers/'extension.py').write_text('# trusted fixture')
        (self.helpers/'selection_extension.py').write_text('# trusted fixture')
        (self.helpers/'.runtime').mkdir(mode=0o700)
        config=self.root/'local-llm.json';config.write_text(json.dumps({'schema':1,'helper_directory':str(self.helpers)}));config.chmod(0o600)
        with patch.object(mcp,'runtime_path',return_value=self.root):
            self.adapter=mcp.Adapter(self.root,allow_local_llm=True)
    def tearDown(self):
        self.adapter.audit.close();self.temp.cleanup()
    def test_default_is_still_three_readonly_tools(self):
        self.assertEqual(len(mcp.tool_definitions()),3)
        self.assertTrue(all(t['annotations']['readOnlyHint'] for t in mcp.tool_definitions()))
        self.assertEqual([t['name'] for t in self.adapter.tools], ['orchestrator_status','orchestrator_list_tasks','orchestrator_get_task','aceis_local_llm_generate'])
        self.assertFalse(self.adapter.tools[-1]['annotations']['readOnlyHint'])
        self.assertFalse(self.adapter.tools[-1]['annotations']['idempotentHint'])
    def test_invalid_arguments_do_not_launch_helper(self):
        for args in [dict(task='review',prompt='x',model='other'),dict(task='review',prompt='x',max_tokens=True),dict(task='review',prompt='x',max_tokens=1025),dict(task='review',prompt='あ'*6000),dict(task='exec',prompt='x')]:
            with patch.object(mcp.subprocess,'run') as run:
                with self.assertRaises(mcp.Invalid):self.adapter.call_tool('aceis_local_llm_generate',args,1)
                run.assert_not_called()
    def test_fixed_dispatch_and_forced_unverified_output(self):
        envelope={'isError':False,'structuredContent':{'content':'fixture','verification_status':'PASSED','code_executed':True}}
        with patch.object(mcp.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(envelope),'')) as run:
            result=self.adapter.call_tool('aceis_local_llm_generate',{'task':'review','prompt':'private-prompt'},1)
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['data']['verification_status'],'NOT_EXECUTED')
        self.assertFalse(result['structuredContent']['data']['code_executed'])
        self.assertTrue(result['structuredContent']['data']['untrusted_advisory'])
        self.assertEqual(run.call_args.kwargs['timeout'],90)
        self.assertEqual(run.call_args.args[0],['/usr/bin/python3','-B',str(self.helpers/'extension.py'),'--call'])
        self.assertNotIn('private-prompt',self.adapter.audit_path.read_text())
    def test_timeout_is_uncertain_without_retry(self):
        with patch.object(mcp.subprocess,'run',side_effect=subprocess.TimeoutExpired('helper',90)) as run:
            result=self.adapter.call_tool('aceis_local_llm_generate',{'task':'draft','prompt':'x'},1)
        self.assertTrue(result['isError']); self.assertEqual(run.call_count,1)
        self.assertIn('LOCAL_LLM_ERROR_OR_UNCERTAIN',result['content'][0]['text'])
    def test_optin_does_not_enable_submit(self):
        with self.assertRaises(mcp.Invalid): self.adapter.call_tool('orchestrator_submit_task',{},1)
    def test_service_command_requires_explicit_optin(self):
        service=Services.__new__(Services)
        service.root=Path('/tmp/public-fixture');service.codex=Path('/usr/bin/false')
        service.client=Path('/usr/bin/false');service.key=Path('/tmp/metadata-only')
        service.health=Path('/tmp/public-fixture/health')
        service.allow_local_llm=False
        before=service.commands()
        service.allow_local_llm=True
        after=service.commands()
        self.assertEqual(before['daemon'],after['daemon'])
        self.assertNotIn('--allow-local-llm',' '.join(before['tunnel']))
        self.assertIn('--allow-local-llm',' '.join(after['tunnel']))
        self.assertNotIn('--allow-writes',' '.join(after['tunnel']))
class SelectedMcpTests(unittest.TestCase):
    setUp = LocalLlmMcpTests.setUp
    tearDown = LocalLlmMcpTests.tearDown
    def test_seven_tools_are_independent_from_submit_cancel(self):
        names=[t['name'] for t in mcp.tool_definitions(False,True,True)]
        self.assertEqual(len(names),7)
        self.assertNotIn('orchestrator_submit_task',names)
        self.assertIn('aceis_local_model_prepare',names)
    def test_selected_validation_rejects_extra_authority(self):
        with patch.object(mcp,'runtime_path',return_value=self.root):selected=mcp.Adapter(self.root,allow_local_llm=True,allow_local_models=True)
        try:
            for args in [{'profile':'http://other'},{'profile':'qwen-coder-next','command':'x'}]:
                with patch.object(mcp.subprocess,'run') as run:
                    with self.assertRaises(mcp.Invalid):selected.call_tool('aceis_local_model_prepare',args,1)
                    run.assert_not_called()
        finally:selected.audit.close()
    def test_selected_bounds_and_same_private_helper(self):
        with patch.object(mcp,'runtime_path',return_value=self.root):selected=mcp.Adapter(self.root,allow_local_llm=True,allow_local_models=True)
        try:
            cases=[('aceis_local_model_status',{'profile':'gpt-oss-120b'},15),('aceis_local_model_prepare',{'profile':'qwen-coder-next'},130),('aceis_local_model_generate',{'profile':'gpt-oss-120b','task':'draft','prompt':'fixture'},250)]
            result={'isError':False,'structuredContent':{'status':'fixture'}}
            for name,args,bound in cases:
                with patch.object(mcp.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(result),'')) as run:
                    out=selected.call_tool(name,args,1)
                self.assertFalse(out['isError']);self.assertEqual(run.call_args.kwargs['timeout'],bound)
                self.assertEqual(run.call_args.args[0][2],str(self.helpers/'selection_extension.py'))
        finally:selected.audit.close()
    def test_helper_timeout_persists_uncertainty_without_retry(self):
        with patch.object(mcp,'runtime_path',return_value=self.root):selected=mcp.Adapter(self.root,allow_local_models=True)
        try:
            with patch.object(mcp.subprocess,'run',side_effect=subprocess.TimeoutExpired('fixture',130)) as run:
                out=selected.call_tool('aceis_local_model_prepare',{'profile':'gpt-oss-120b'},1)
            self.assertTrue(out['isError']);self.assertEqual(run.call_count,1)
            self.assertFalse(json.loads((self.helpers/'.runtime/UNCERTAIN.json').read_text())['automatic_retry'])
        finally:selected.audit.close()
    def test_config_not_private_is_refused_before_helper(self):
        (self.root/'local-llm.json').chmod(0o644)
        with patch.object(mcp,'runtime_path',return_value=self.root):
            with self.assertRaises(ValueError):mcp.Adapter(self.root,allow_local_models=True)

if __name__=='__main__':unittest.main()
