"""Audit actual staged blobs in disposable Git fixtures, never the live index."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

PROJECT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('project_git_audit',PROJECT/'scripts/git_audit.py')
audit_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit_module)


class GitAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='git-audit-')
        self.root=Path(self.temp.name)
        self.git('init','--quiet','--template=')

    def tearDown(self):
        self.temp.cleanup()

    def git(self,*args,input=None):
        return subprocess.run(['git','-c','core.hooksPath=/dev/null','-c','core.attributesFile=/dev/null',
                               '-c','core.excludesFile=/dev/null',*args],cwd=self.root,
                              input=input,capture_output=True,check=True).stdout

    def write(self,name,content,stage=True):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content,str) else content)
        if stage:self.git('add','-f','--',name)
        return path

    def result(self):
        return audit_module.audit(self.root)

    def test_all_twelve_actual_portable_policy_role_skill_files_pass(self):
        self.assertEqual(len(audit_module.PORTABLE_CONFIG_FILES),12)
        for name in audit_module.PORTABLE_CONFIG_FILES:
            self.write(name,(PROJECT/name).read_bytes())
        result=self.result()
        self.assertTrue(result['passed'],result['findings'])
        self.assertEqual(set(result['staged_files']),audit_module.PORTABLE_CONFIG_FILES)

    def test_source_examples_and_empty_secret_environment_template_pass(self):
        for name in ['src/module.py','tests/fixtures/example.sql','docs/example.md',
                     'systemd/aceis-mcp-tunnel.example.service','systemd/aceis-mcp-tunnel.example.yaml',
                     '.env.example','examples/.env.development.example']:
            self.write(name,'# portable example\n')
        self.assertTrue(self.result()['passed'])

    def test_local_paths_stay_forbidden_including_tracked_operator_configs(self):
        names=['.runtime/tasks.sqlite','.codex/auth.json','.codex/config.local.toml',
               '.codex/agents/researcher.local.toml','.agents/skills/common-policy/auth.json',
               '.serena/project.yml','.vscode/settings.json','.aws/credentials',
               'config/policy.local.json','examples/host.private/.env.example',
               '.env.production','examples/private.key','docs/session.jsonl',
               'docs/runner.pid','docs/runner.socket','docs/local.env',
               'docs/project.code-workspace','docs/.coverage','src/pkg.egg-info/PKG-INFO',
               'artifacts/result.json','worktrees/fixture/src.py',
               *sorted(audit_module.LOCAL_ONLY_FILES)]
        for name in names:
            with self.subTest(name=name):self.assertFalse(audit_module.path_allowed(name))
        for name in audit_module.LOCAL_ONLY_FILES:self.write(name,'# no secret; still operator-local\n')
        result=self.result()
        self.assertFalse(result['passed'])
        self.assertEqual({r['path'] for r in result['findings']},audit_module.LOCAL_ONLY_FILES)

    def test_unknown_assistant_state_cannot_use_the_source_exceptions(self):
        for name in ['.codex/history.txt','.codex/agents/unknown.toml',
                     '.agents/skills/common-policy/cache.json','config/credentials.toml']:
            with self.subTest(name=name):self.assertFalse(audit_module.path_allowed(name))

    def test_secret_detection_also_runs_inside_allowed_policy_and_env_example(self):
        secret='sk-'+'A'*32
        self.write('policies.md',secret+'\n')
        self.write('.env.example','SERVICE_PASSWORD='+'B'*32+'\n')
        result=self.result()
        self.assertFalse(result['passed'])
        self.assertIn('api_key',{x['rule'] for x in result['findings']})
        self.assertIn('unquoted_env_credential',{x['rule'] for x in result['findings']})
        self.assertNotIn(secret,json.dumps(result))

    def test_github_token_key_and_jwt_are_reported_without_values(self):
        contents=['ghp_'+'C'*32,'-----BEGIN ENCRYPTED PRIVATE KEY-----\nfixture',
                  'eyJ'+'D'*20+'.'+'E'*20+'.'+'F'*20,'password="'+'J'*32+'"']
        for index,value in enumerate(contents):self.write(f'docs/fixture-{index}.md',value)
        result=self.result();self.assertFalse(result['passed'])
        self.assertTrue({'github_token','private_key','jwt','credential_assignment'}<={x['rule'] for x in result['findings']})
        for value in contents:self.assertNotIn(value,json.dumps(result))

    def test_staged_secret_cannot_be_hidden_by_clean_working_file(self):
        path=self.write('src/module.py','key="'+'sk-'+'G'*32+'"')
        path.write_text('# clean unstaged content\n')
        self.assertFalse(self.result()['passed'])

    def test_unstaged_content_does_not_replace_the_scanned_blob(self):
        path=self.write('src/module.py','# safe staged content\n')
        path.write_text('sk-'+'H'*32)
        self.assertTrue(self.result()['passed'])

    def test_binary_and_oversized_blobs_stay_rejected(self):
        self.write('tests/fixtures/binary.dat',b'fixture\0data')
        self.write('docs/large.md',b'x'*(2*1024**2+1))
        result=self.result();self.assertFalse(result['passed'])
        self.assertTrue({'binary_blob','blob_size'}<={x['rule'] for x in result['findings']})

    def test_symlink_cannot_expose_a_source_exemption(self):
        path=self.root/'.codex/config.toml';path.parent.mkdir();path.symlink_to('/nonexistent-fixture')
        self.git('add','-f','--','.codex/config.toml')
        self.assertIn('nonregular_or_conflicted_index',{x['rule'] for x in self.result()['findings']})

    def test_unmerged_index_is_rejected(self):
        self.write('src/module.py','# fixture\n')
        blob=self.git('rev-parse',':src/module.py').decode().strip()
        self.git('update-index','--force-remove','src/module.py')
        self.git('update-index','--index-info',input=f'100644 {blob} 1\tsrc/module.py\n'.encode())
        self.assertIn('nonregular_or_conflicted_index',{x['rule'] for x in self.result()['findings']})

    def test_empty_index_is_not_a_successful_audit(self):
        self.assertFalse(self.result()['passed'])


if __name__=='__main__':unittest.main()
