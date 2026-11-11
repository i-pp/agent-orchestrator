"""Plan generation cannot start a process or grant live authorization."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from codex_approval_trial_plan import build_trial_plan,TrialPlanError,COMMAND

class TrialPlanTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(dir="/var/tmp");self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);os.chmod(self.root,0o700)
 def plan(self):return build_trial_plan(self.root,'gpt-6-luna',approved_models=['gpt-6-luna'])
 def test_no_native_actions_or_credential_reads(self):
  with patch('subprocess.Popen',side_effect=AssertionError('no process allowed')):
   plan=self.plan()
  self.assertFalse(plan['executed']);self.assertFalse(plan['credentials_read'])
  self.assertEqual(plan['native_turns_started'],0);self.assertEqual(plan['app_server_processes_started'],0)
  self.assertEqual(list(self.root.iterdir()),[])
 def test_unknown_environment_never_becomes_permission(self):
  with patch('codex_approval_trial_plan.environment',return_value={'state':'UNKNOWN','signals':['test']}):p=self.plan()
  self.assertIn('NORMAL_HOST_CONTEXT_NOT_CONFIRMED',p['launch_blockers'])
  self.assertEqual(p['status'],'PREPARED_NOT_AUTHORIZED')
 def test_host_observation_still_needs_explicit_live_decision(self):
  with patch('codex_approval_trial_plan.environment',return_value={'state':'HOST_CONTEXT','signals':['test']}):p=self.plan()
  self.assertEqual(p['launch_blockers'],['EXPLICIT_NATIVE_TRIAL_AUTHORIZATION_REQUIRED']);self.assertFalse(p['executed'])
 def test_rpc_templates_preserve_readonly_user_and_untrusted(self):
  p=self.plan();t=p['rpc_templates']['thread/start'];u=p['rpc_templates']['turn/start']
  self.assertEqual(t['approvalPolicy'],'untrusted');self.assertEqual(t['approvalsReviewer'],'user');self.assertEqual(t['sandbox'],'read-only');self.assertFalse(t['ephemeral'])
  self.assertEqual(u['approvalPolicy'],'untrusted');self.assertEqual(u['sandboxPolicy'],{'type':'readOnly'})
  self.assertIn(COMMAND,u['input'][0]['text']);self.assertEqual(p['scope']['max_turns'],1)
  self.assertFalse(p['scope']['existing_service_changes']);self.assertFalse(p['scope']['global_policy_changes'])
 def test_unapproved_model_blocked(self):
  with self.assertRaises(TrialPlanError):build_trial_plan(self.root,'other-model',approved_models=['gpt-6-luna'])
 def test_nonempty_or_nonprivate_scratch_blocked(self):
  (self.root/'data').write_text('synthetic')
  with self.assertRaises(TrialPlanError):self.plan()
  (self.root/'data').unlink();os.chmod(self.root,0o755)
  with self.assertRaises(TrialPlanError):self.plan()
 def test_repository_or_symlink_blocked(self):
  (self.root/'.git').mkdir()
  with self.assertRaises(TrialPlanError):self.plan()
  (self.root/'.git').rmdir();link=self.root.parent/(self.root.name+'-link');link.symlink_to(self.root,target_is_directory=True);self.addCleanup(link.unlink)
  with self.assertRaises(TrialPlanError):build_trial_plan(link,'gpt-6-luna',approved_models=['gpt-6-luna'])

if __name__=='__main__':unittest.main()
