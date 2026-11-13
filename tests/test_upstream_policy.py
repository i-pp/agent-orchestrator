"""Local pinned-source tests use tiny Git fixtures; no network, models or services."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'src'))
from common_policy import load_policy,load_project_policy,developer_instructions,PolicyError
from upstream_policy import FILES,GIT_URL,REPOSITORY,assemble,resolve_checkout

class UpstreamTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'project';self.clone=self.base/'source'
        (self.root/'config').mkdir(parents=True);(self.root/'policies').mkdir();self.clone.mkdir()
        for name in ['policies.md','policies/manifest.json']:shutil.copyfile(PROJECT/name,self.root/name)
        self.legacy=load_policy(self.root)
        self.config={'schema_version':1,'bundle_root':'.','expected_version':'1.0',
            'expected_sha256':self.legacy.sha256,'upstream_pin':'upstream-policy.json'}
        self.save_config()
        self.docs={name:('# Fixture '+name+'\nNo additional authority; verify bounded work.\n') for name in FILES}
        for name,text in self.docs.items():
            p=self.clone/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        self.git('init','-b','main');self.git('remote','add','origin',GIT_URL)
        self.git('add','--all');self.git('commit','-qm','Fixture policy')
        self.commit=self.git('rev-parse','HEAD').strip()
        self.pin={'schema_version':1,'repository':REPOSITORY,'git_url':GIT_URL,'commit':self.commit,
            'version':'git:'+self.commit,'checkout':'../source',
            'sha256':hashlib.sha256(assemble(self.docs).encode()).hexdigest(),'files':{
                name:{'sha256':hashlib.sha256(text.encode()).hexdigest(),
                      'git_blob_sha':hashlib.sha1(b'blob '+str(len(text.encode())).encode()+b'\0'+text.encode()).hexdigest()}
                for name,text in self.docs.items()}}
        self.save_pin()

    def git(self,*args):
        return subprocess.check_output(['git','-c','user.name=Policy Fixture','-c','user.email=fixture@example.invalid',
            '-c','commit.gpgsign=false','-c','core.hooksPath=/dev/null','-C',str(self.clone),*args],stderr=subprocess.DEVNULL,text=True)
    def save_pin(self):(self.root/'config/upstream-policy.json').write_text(json.dumps(self.pin))
    def save_config(self):(self.root/'config/policy.json').write_text(json.dumps(self.config))
    def override(self,**changes):
        value={'schema_version':1,'checkout':str(self.clone),'checkout_commit':self.commit};value.update(changes)
        (self.root/'config/policy-source.local.json').write_text(json.dumps(value))

    def test_real_git_loads_exact_committed_text_and_provenance_without_copy(self):
        loaded=load_project_policy(self.root)
        self.assertEqual(loaded.text,assemble(self.docs));self.assertEqual(loaded.sha256,self.pin['sha256'])
        metadata=loaded.metadata()['upstream']
        self.assertEqual(metadata['loaded_checkout_commit'],self.commit)
        self.assertFalse(metadata['checkout_dirty']);self.assertTrue(metadata['checkout_origin_verified'])
        self.assertFalse(metadata['legacy_body_injected']);self.assertFalse(metadata['automatic_network'])
        context=developer_instructions(loaded,'Keep reviewer independence.')
        for text in self.docs.values():self.assertEqual(context.count(text),1)
        self.assertNotIn(self.legacy.text,context);self.assertFalse((self.root/'.runtime').exists())
        for guard in ['explicit constraints take precedence','existing safety guards','no-nested-isolation restriction','grants no additional permissions or approval']:
            self.assertIn(guard,context)

    def test_missing_local_path_fails_without_fetch_clone_or_cache_fallback(self):
        self.clone.rename(self.base/'preserved-source')
        with self.assertRaises(PolicyError):load_project_policy(self.root)
        self.assertFalse(self.clone.exists());self.assertFalse((self.root/'.runtime').exists())

    def test_dirty_worktree_is_reported_but_never_loaded_or_reset(self):
        p=self.clone/FILES[0];changed='Uncommitted operator change\n';p.write_text(changed)
        loaded=load_project_policy(self.root)
        self.assertEqual(loaded.text,assemble(self.docs));self.assertTrue(loaded.metadata()['upstream']['checkout_dirty'])
        self.assertFalse(loaded.metadata()['upstream']['working_tree_body_used'])
        self.assertEqual(p.read_text(),changed)

    def test_newer_head_does_not_silently_change_the_selected_version(self):
        (self.clone/FILES[0]).write_text('New committed source\n');self.git('add','--all');self.git('commit','-qm','Next fixture')
        head=self.git('rev-parse','HEAD').strip();loaded=load_project_policy(self.root)
        self.assertNotEqual(head,self.commit);self.assertEqual(loaded.version,self.pin['version'])
        self.assertEqual(loaded.metadata()['upstream']['checkout_head_after'],head)
        self.assertEqual(loaded.metadata()['upstream']['loaded_checkout_commit'],self.commit)

    def test_unverified_origin_requires_explicit_local_commit_with_equal_hashes(self):
        self.git('remote','remove','origin')
        with self.assertRaises(PolicyError):load_project_policy(self.root)
        self.override();loaded=load_project_policy(self.root)
        self.assertFalse(loaded.metadata()['upstream']['checkout_origin_verified'])
        self.assertTrue(loaded.metadata()['upstream']['local_commit_override'])
        self.assertEqual(loaded.text,assemble(self.docs))
        (self.clone/FILES[0]).write_text('Different policy\n');self.git('add','--all');self.git('commit','-qm','Different fixture')
        self.override(checkout_commit=self.git('rev-parse','HEAD').strip())
        with self.assertRaises(PolicyError):load_project_policy(self.root)

    def test_tilde_and_relative_path_resolution_are_explicit(self):
        self.assertEqual(resolve_checkout(self.root,'../source'),self.clone.resolve())
        self.assertEqual(resolve_checkout(self.root,'~'),Path.home().resolve())
        for path in ['', 'invalid\x00path']:
            with self.assertRaises(PolicyError):resolve_checkout(self.root,path)

    def test_version_hash_and_git_blob_pin_conflicts_are_rejected(self):
        original=json.loads(json.dumps(self.pin))
        for field,value in [('version','git:'+'2'*40),('sha256','0'*64),('repository','other/repo')]:
            self.pin=json.loads(json.dumps(original));self.pin[field]=value;self.save_pin()
            with self.subTest(field=field),self.assertRaises(PolicyError):load_project_policy(self.root)
        self.pin=original;self.pin['files'][FILES[0]]['git_blob_sha']='0'*40;self.save_pin()
        with self.assertRaises(PolicyError):load_project_policy(self.root)

    def test_missing_selection_or_pin_is_not_a_silent_legacy_fallback(self):
        self.config.pop('upstream_pin');self.save_config()
        with self.assertRaises(PolicyError):load_project_policy(self.root)
        self.config['upstream_pin']='upstream-policy.json';self.save_config()
        (self.root/'config/upstream-policy.json').unlink()
        with self.assertRaises(PolicyError):load_project_policy(self.root)

    def test_symlink_subdirectory_and_config_redirection_are_rejected(self):
        alias=self.base/'alias';alias.symlink_to(self.clone,target_is_directory=True)
        self.override(checkout=str(alias))
        with self.assertRaises(PolicyError):load_project_policy(self.root)
        self.override(checkout=str(self.clone/'docs'))
        with self.assertRaises(PolicyError):load_project_policy(self.root)
        self.config['upstream_pin']='../other.json';self.save_config()
        with self.assertRaises(PolicyError):load_project_policy(self.root)

    def test_bundle_is_rechecked_before_context_assembly(self):
        loaded=load_project_policy(self.root)
        for changed in [loaded._replace(text='changed'),loaded._replace(text=None),
                        loaded._replace(version='git:'+'2'*40),loaded._replace(provenance_json='{}')]:
            with self.assertRaises(PolicyError):developer_instructions(changed)
        metadata=json.loads(loaded.provenance_json);metadata['files'][FILES[0]]['sha256']='0'*64
        with self.assertRaises(PolicyError):developer_instructions(loaded._replace(provenance_json=json.dumps(metadata)))

    def test_editorial_documents_are_required_and_cannot_fall_back_to_three_files(self):
        for name in ['docs/ai/editorial-policy.md','policies/editorial.md']:
            with self.subTest(name=name):
                pin=json.loads(json.dumps(self.pin));pin['files'].pop(name)
                (self.root/'config/upstream-policy.json').write_text(json.dumps(pin))
                with self.assertRaisesRegex(PolicyError,'document set differs'):
                    load_project_policy(self.root)

    def test_changed_editorial_git_objects_and_context_are_rejected(self):
        for name in ['docs/ai/editorial-policy.md','policies/editorial.md']:
            with self.subTest(name=name):
                # Select a real later commit, keeping the reviewed content pins.
                # Moving the ref must not make changed editorial text acceptable.
                path=self.clone/name;original=path.read_text()
                path.write_text(original+'Unreviewed editorial change\n')
                self.git('add',name);self.git('commit','-qm','Unreviewed editorial change')
                self.override(checkout_commit=self.git('rev-parse','HEAD').strip())
                with self.assertRaisesRegex(PolicyError,'document hash mismatch'):
                    load_project_policy(self.root)
                (self.root/'config/policy-source.local.json').unlink()
                loaded=load_project_policy(self.root)
                meta=json.loads(loaded.provenance_json)
                meta['files'][name]['sha256']='0'*64
                with self.assertRaisesRegex(PolicyError,'document hash mismatch'):
                    developer_instructions(loaded._replace(provenance_json=json.dumps(meta)))
                path.write_text(original)
                self.git('add',name);self.git('commit','-qm','Restore fixture editorial text')

    def test_missing_editorial_git_object_is_not_optional(self):
        for name in ['docs/ai/editorial-policy.md','policies/editorial.md']:
            with self.subTest(name=name):
                path=self.clone/name;original=path.read_text();path.unlink()
                self.git('add',name);self.git('commit','-qm','Fixture missing editorial file')
                self.override(checkout_commit=self.git('rev-parse','HEAD').strip())
                before=(self.root/'config/upstream-policy.json').read_bytes()
                with self.assertRaises(PolicyError):
                    load_project_policy(self.root)
                self.assertFalse(path.exists())
                self.assertEqual((self.root/'config/upstream-policy.json').read_bytes(),before)
                path.write_text(original)
                self.git('add',name);self.git('commit','-qm','Restore missing fixture file')

if __name__=='__main__':unittest.main()
