"""Offline regression for explicit invocation inheritance; no services or sockets."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'src'),str(PROJECT/'scripts')]
import poc_services as service_module
from poc_services import Services, Refused
from chat_mailbox import digest, MailboxError

class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.key=self.root/'existing-credential-reference';self.key.write_text('synthetic-fixture-only');self.key.chmod(0o600)
        helper=self.root/'helpers';helper.mkdir()
        for name in ['extension.py','selection_extension.py']:(helper/name).write_text('# offline fixture')
        config=self.root/'local-llm.json';config.write_text(json.dumps({'schema':1,'helper_directory':str(helper)}));config.chmod(0o600)
        self.slot={'task_id':'00000000-0000-4000-8000-000000000001',
                   'request_id':'00000000-0000-4000-8000-000000000002','request_sha256':digest('Non-secret fixture')}
        policy=self.root/'chat-mailbox-policy.json';policy.write_text(json.dumps({'schema':1,'allowed':[self.slot]}));policy.chmod(0o600)
        self.service=Services.__new__(Services);s=self.service
        s.root=self.root;s.key=self.key;s.codex=s.client=Path('/usr/bin/true');s.health=self.root/'health'
        s.allow_local_llm=s.allow_local_models=s.allow_chat_mailbox=True
        self.saved={'local_llm_enabled':True,'local_models_enabled':True}
        self.reader=patch.object(s,'read_state',return_value=self.saved);self.reader.start()
        # Host ancestry ownership is not part of invocation inheritance tests.
        # Keep private-file checks real; only substitute the host ancestry probe.
        self.metadata=patch.object(service_module,'credential_metadata',side_effect=service_module.private_file)
        self.metadata.start()
    def tearDown(self):self.metadata.stop();self.reader.stop();self.temp.cleanup()

    def test_valid_eleven_tools_without_stops_launches_or_database(self):
        with patch.object(self.service,'call',side_effect=AssertionError('No manager calls')):
            result=self.service.preflight(self.key)
        self.assertTrue(result['passed']);self.assertEqual(len(result['tool_names']),11)
        self.assertFalse((self.root/'chat-mailbox.sqlite').exists())
        self.assertEqual(result['mailbox_allowed_slots'],1)
        self.assertNotIn('orchestrator_submit_task',result['tool_names'])

    def test_missing_or_different_key_is_rejected_before_start(self):
        self.service.key=self.root/'missing-default'
        with self.assertRaises(Refused):self.service.preflight(self.key)
        with patch.object(self.service,'locked',return_value=contextlib.nullcontext()),patch.object(self.service,'launch') as launch,patch.object(self.service,'write_state') as write:
            with self.assertRaises(FileNotFoundError):self.service.start()
            launch.assert_not_called();write.assert_not_called()

    def test_saved_reference_catches_omitted_override_even_if_other_file_exists(self):
        self.saved['credential_file_reference']=str(self.key)
        other=self.root/'different-credential';other.write_text('different-synthetic-fixture');other.chmod(0o600)
        self.service.key=other
        with self.assertRaisesRegex(Refused,'differs'):self.service.preflight()

    def test_both_existing_capability_flags_must_be_inherited(self):
        for attr in ['allow_local_llm','allow_local_models']:
            with self.subTest(attribute=attr),patch.object(self.service,attr,False):
                with self.assertRaisesRegex(Refused,'omitted'):self.service.preflight(self.key)

    def test_command_builder_cannot_omit_credential_or_capability_argument(self):
        command=self.service.commands()
        bad={'daemon':command['daemon'],'tunnel':[v for v in command['tunnel'] if not v.startswith('--control-plane.api-key=')]}
        with patch.object(self.service,'commands',return_value=bad):
            with self.assertRaisesRegex(Refused,'Credential argument'):self.service.preflight(self.key)
        bad={'daemon':command['daemon'],'tunnel':[v.replace(' --allow-local-models','') for v in command['tunnel']]}
        with patch.object(self.service,'commands',return_value=bad):
            with self.assertRaisesRegex(Refused,'capability argument'):self.service.preflight(self.key)

    def test_policy_failure_and_missing_binary_fail_before_interruption(self):
        policy=self.root/'chat-mailbox-policy.json';policy.chmod(0o644)
        with self.assertRaises(MailboxError):self.service.preflight(self.key)
        policy.chmod(0o600);self.service.codex=self.root/'missing-binary'
        with self.assertRaisesRegex(Refused,'Executable'):self.service.preflight(self.key)

    def test_cli_preserves_explicit_reference_and_all_three_flags(self):
        argv=['poc_services.py','preflight','--key-file',str(self.key),'--expected-key-file',str(self.key),
              '--allow-local-llm','--allow-local-models','--allow-chat-mailbox']
        with patch.object(sys,'argv',argv),patch.object(service_module,'Services') as factory,contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.preflight.return_value={'passed':True}
            self.assertEqual(service_module.main(),0)
        self.assertEqual(factory.call_args.args[2],self.key)
        self.assertEqual(factory.call_args.args[3:],(True,True,True))
        factory.return_value.preflight.assert_called_once_with(self.key)
        factory.return_value.start.assert_not_called();factory.return_value.stop.assert_not_called()

if __name__=='__main__':unittest.main()
