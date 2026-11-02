"""Offline evidence-boundary tests. No model, shell, scorer or service calls."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'src'),str(PROJECT/'scripts')]
from experiments import ExperimentStore, EvidenceError, KINDS, canonical, digest, safe_read, tool_definitions, validate_arguments
from experiment_catalog import build


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.j=self.root/'journal';self.j.mkdir();self.report=self.root/'report';self.report.mkdir()
        rows=[]
        for backend in ['vllm','sglang','strata']:
            for task,file in [('c_parse','c_parse.c'),('aarch64_sum','aarch64_sum.S')]:
                key=backend+'-'+task
                self.write(self.j/(key+'-intent.json'),{'backend':backend,'task':task,'payload':{'body':{'messages':[
                    {'role':'system','content':'fixed policy'}, {'role':'user','content':'public task '+task}],
                    'temperature':0,'seed':42,'max_tokens':1024}}})
                code='int candidate(void) { return 0; }\n' if task=='c_parse' else '.text\nret\n'
                answer='```\n'+code+'```'
                truncated=backend=='vllm' and task=='aarch64_sum'
                self.write(self.j/(key+'-response.json'),{'status':'COMPLETED','content':answer,'finish_reason':'length' if truncated else 'stop','private_raw_response':{'secret':'PRIVATE_RESPONSE_NEVER_EXPORT'}})
                if not truncated:
                    folder=self.j/(key+'-candidate');folder.mkdir();(folder/file).write_text(code);(folder/file).chmod(0o600)
                    output={'evaluator_version':'b4-v1','status':'FAIL','legacy_status':'FAIL','additional_status':'FAIL','steps':[
                        {'phase':'compile_candidate','returncode':0,'output':'candidate compiled\n'},
                        {'phase':'execute_tests','returncode':1,'output':'PRIVATE_FIXTURE_BODY_NEVER_EXPORT'}],
                        'additional_checks':[{'id':'null_contracts','status':'FAIL','steps':[]}],
                        'private_test':'PRIVATE_FIXTURE_BODY_NEVER_EXPORT'}
                    self.write(self.j/(key+'-grading.json'),{'backend':backend,'task':task,'candidate_sha256':digest(code.encode()),
                        'answer_sha256':digest(answer.encode()),'fixture_manifest_sha256':'a'*64,
                        'configuration':{'context':4096,'max_tokens':1024},'score':{'format':'PASS','code':'FAIL','overall':'FAIL','complete_output':True},
                        'private_raw_sandbox':{'output':json.dumps(output),'execution_exit_code':0,'limit_reason':None,
                            'host_config':{'secret':'PRIVATE_OPERATION_NEVER_EXPORT'}}})
                rows.append({'backend':backend,'task':task,'model':'Next AWQ4 c92cabfa' if backend!='strata' else 'Flash GSQ-RCO Q2_0',
                    'usage':{'completion_tokens':1024 if truncated else 50}})
        self.write(self.j/'scope.json',{'version':1,'max_tokens':1024})
        self.write(self.report/'FINAL.json',{'rows':rows})
        self.manifest=build(self.j,self.report);self.path=self.root/'manifest.json'
        self.store=self.load(release=True)

    def write(self,path,value):path.write_text(json.dumps(value));path.chmod(0o600)
    def load(self,release=False):
        self.write(self.path,self.manifest)
        store=ExperimentStore(self.path,digest(self.path.read_bytes()))
        if release:
            for run in self.manifest['runs']:
                _,values=store.inspect(run['id'])
                run['release']={k:digest(v) for k,v in values.items() if v is not None}
            self.write(self.path,self.manifest);store=ExperimentStore(self.path,digest(self.path.read_bytes()))
        return store
    def ident(self,backend='sglang',task='c_parse'):
        return next(r['id'] for r in self.manifest['runs'] if r['key']==backend+'-'+task)
    def artifact(self,kind='generated_code',**args):
        return self.store.dispatch('experiment_artifacts',{'run_id':self.ident(),'artifact_id':kind,**args})

    def test_four_readonly_schemas(self):
        self.assertEqual(len(tool_definitions()),4)
        for t in tool_definitions():self.assertTrue(t['annotations']['readOnlyHint']);self.assertFalse(t['inputSchema']['additionalProperties'])
    def test_catalog_builder_no_automatic_release(self):
        m=build(self.j,self.report)
        self.assertEqual(len(m['runs']),6);self.assertTrue(all(r['release']=={} for r in m['runs']))
    def test_raw_code_hash_and_bounded_reassembly(self):
        first=self.artifact(max_bytes=7)
        self.assertTrue(first['truncated']);self.assertEqual(first['returned_bytes'],7)
        out=b'';offset=0
        while True:
            a=self.artifact(offset=offset,max_bytes=7);out+=base64.b64decode(a['content_base64'])
            if a['next_offset'] is None:break
            offset=a['next_offset']
        self.assertEqual(digest(out),first['sha256']);self.assertIn(b'int candidate',out)
    def test_private_test_bodies_and_operational_payload_never_export(self):
        all_data=[]
        for run in self.manifest['runs']:
            all_data.append(self.store.dispatch('experiment_get',{'run_id':run['id']}))
            for kind in KINDS:
                a=self.store.dispatch('experiment_artifacts',{'run_id':run['id'],'artifact_id':kind})
                if 'content_base64' in a:all_data.append(base64.b64decode(a['content_base64']).decode())
        text=json.dumps(all_data)
        self.assertNotIn('PRIVATE_',text);self.assertNotIn(str(self.root),text)
        self.assertIn('null_contracts',text)
    def test_truncation_not_scored_failure(self):
        v=self.store.dispatch('experiment_get',{'run_id':self.ident('vllm','aarch64_sum')})
        self.assertEqual(v['classification'],'TOKEN_TRUNCATED_UNSCORED');self.assertFalse(v['scored'])
        a=self.store.dispatch('experiment_artifacts',{'run_id':v['run_id'],'artifact_id':'generated_code'})
        self.assertEqual(a['status'],'MISSING')
    def test_filters_pagination_and_cursor_context(self):
        one=self.store.dispatch('experiment_list',{'limit':1,'task':'c_parse'})
        two=self.store.dispatch('experiment_list',{'limit':1,'task':'c_parse','cursor':one['next_cursor']})
        self.assertNotEqual(one['items'],two['items']);self.assertEqual(one['total'],3)
        with self.assertRaises(EvidenceError):self.store.dispatch('experiment_list',{'cursor':one['next_cursor'],'task':'aarch64_sum'})
    def test_unknown_path_and_bad_input_rejected(self):
        for args in [{'run_id':'../secret'},{'run_id':self.ident(),'path':'/etc/passwd'}, {'run_id':self.ident(),'artifact_id':'private_raw_sandbox'},
                     {'run_id':self.ident(),'artifact_id':'generated_code','max_bytes':True},{'run_id':self.ident(),'offset':1}]:
            with self.subTest(args=args),self.assertRaises(EvidenceError):self.store.dispatch('experiment_artifacts',args)
        with self.assertRaises(EvidenceError):self.store.dispatch('experiment_compare',{'run_ids':[self.ident(),self.ident()]})
    def test_source_mutation_withholds_not_success(self):
        f=self.j/'sglang-c_parse-candidate/c_parse.c';f.write_text('stolen bytes')
        a=self.artifact();self.assertNotIn('content_base64',a);self.assertEqual(a['status'],'UNVERIFIED')
    def test_release_hash_required(self):
        self.manifest['runs'][0]['release']={};s=self.load()
        a=s.dispatch('experiment_artifacts',{'run_id':self.manifest['runs'][0]['id'],'artifact_id':'response_text'})
        self.assertEqual(a['status'],'UNREVIEWED_WITHHELD');self.assertNotIn('content_base64',a)
    def test_manifest_mutation_stale_pin_and_cursor(self):
        self.path.write_text('{}')
        with self.assertRaises(EvidenceError):self.store.dispatch('experiment_list',{})
        with self.assertRaises(EvidenceError):ExperimentStore(self.path,'0'*64)
    def test_symlink_leaf_parent_and_root_escapes_refused(self):
        target=self.j/'sglang-c_parse-candidate/c_parse.c';target.unlink();target.symlink_to('/etc/passwd')
        self.assertNotIn('content_base64',self.artifact())
        link=self.root/'link';link.symlink_to(self.j,target_is_directory=True)
        with self.assertRaises(OSError):safe_read(str(link),'scope.json')
        with self.assertRaises(EvidenceError):safe_read(str(self.j),'../report/FINAL.json')
    def test_hardlinks_special_files_and_source_size_refused(self):
        f=self.j/'x';f.write_text('a');os.link(f,self.j/'hard')
        with self.assertRaises(EvidenceError):safe_read(str(self.j),'x')
        fifo=self.j/'fifo';os.mkfifo(fifo)
        with self.assertRaises(EvidenceError):safe_read(str(self.j),'fifo')
        with self.assertRaises(EvidenceError):safe_read(str(self.j),'scope.json',maximum=1)
    def test_public_compile_log_only(self):
        a=self.artifact('compile_log');text=base64.b64decode(a['content_base64']).decode()
        self.assertEqual(text,'candidate compiled\n')
    def test_secret_and_fixture_path_log_withheld_after_release(self):
        f=self.j/'sglang-c_parse-grading.json';s=json.loads(f.read_text());e=json.loads(s['private_raw_sandbox']['output'])
        e['steps'][0]['output']='/suite/private_test.c: PRIVATE_BODY'
        s['private_raw_sandbox']['output']=json.dumps(e);self.write(f,s)
        run=next(r for r in self.manifest['runs'] if r['key']=='sglang-c_parse');run['sources']['grading']['sha256']=digest(f.read_bytes())
        self.store=self.load(release=True)
        a=self.artifact('compile_log');self.assertEqual(a['status'],'PRIVATE_OR_NON_TEXT_WITHHELD');self.assertNotIn('content_base64',a)
    def test_compare_does_not_attribute_model_difference_to_backend(self):
        out=self.store.dispatch('experiment_compare',{'run_ids':[self.ident(),self.ident('strata')]})
        self.assertEqual(out['backend_causal_attribution'],'NOT_ESTABLISHED')
        self.assertIn('model',out['differences_from_first'][0]['different'])
        self.assertEqual(out['counts_by_backend']['sglang']['SCORED_FAILURE'],1)
    def test_unknown_manifest_versions_and_arbitrary_source_names_refused(self):
        for key,value in [('schema',2)]:
            self.manifest[key]=value
            with self.assertRaises(EvidenceError):self.load()
        self.manifest['schema']=1;self.manifest['runs'][0]['sources']['response']['path']='controller.log'
        with self.assertRaises(EvidenceError):self.load()

    def test_environment_failure_remains_distinct_and_not_scored(self):
        run=next(r for r in self.manifest['runs'] if r['key']=='sglang-c_parse')
        f=self.j/'sglang-c_parse-response.json';s=json.loads(f.read_text());s['status']='FAILED';self.write(f,s)
        run['sources']['response']['sha256']=digest(f.read_bytes());run['sources']['grading']=None;run['sources']['candidate']=None
        self.store=self.load()
        v=self.store.dispatch('experiment_get',{'run_id':run['id']})
        self.assertEqual(v['classification'],'ENVIRONMENT_FAILURE');self.assertFalse(v['scored'])

    def test_missing_required_record_and_identity_conflict_unverified(self):
        run=next(r for r in self.manifest['runs'] if r['key']=='sglang-c_parse')
        (self.j/'sglang-c_parse-response.json').unlink()
        v=self.store.dispatch('experiment_get',{'run_id':run['id']})
        self.assertEqual(v['classification'],'UNVERIFIED');self.assertEqual(v['source_integrity']['response']['status'],'MISSING')

    def test_world_writable_sources_and_nonprivate_manifest_refused(self):
        f=self.j/'scope.json';f.chmod(0o666)
        with self.assertRaises(EvidenceError):safe_read(str(self.j),'scope.json')
        self.path.chmod(0o640)
        with self.assertRaises(EvidenceError):ExperimentStore(self.path,digest(self.path.read_bytes()))

    def test_directory_swap_never_reads_outside_open_dirfd(self):
        import experiments
        original=os.open;folder=self.j/'sglang-c_parse-candidate';moved=self.j/'moved';outside=self.root/'outside';outside.mkdir();(outside/'c_parse.c').write_text('PRIVATE_ESCAPE')
        swapped=False
        def opening(path,*args,**kwargs):
            nonlocal swapped
            if path=='c_parse.c' and not swapped:
                swapped=True;folder.rename(moved);folder.symlink_to(outside,target_is_directory=True)
            return original(path,*args,**kwargs)
        with patch('experiments.os.open',side_effect=opening):raw=safe_read(str(self.j),'sglang-c_parse-candidate/c_parse.c')
        self.assertNotIn(b'PRIVATE_ESCAPE',raw);self.assertIn(b'int candidate',raw)

    def test_failed_environment_or_drift_does_not_count_claimed_grade(self):
        run=next(r for r in self.manifest['runs'] if r['key']=='sglang-c_parse')
        f=self.j/'sglang-c_parse-response.json';s=json.loads(f.read_text());s['status']='FAILED';self.write(f,s)
        run['sources']['response']['sha256']=digest(f.read_bytes());self.store=self.load()
        v=self.store.dispatch('experiment_get',{'run_id':run['id']})
        self.assertEqual(v['classification'],'ENVIRONMENT_FAILURE');self.assertFalse(v['scored']);self.assertEqual(v['score']['code'],'UNASSESSED')
        s['status']='COMPLETED';self.write(f,s)
        v=self.store.dispatch('experiment_get',{'run_id':run['id']})
        self.assertEqual(v['classification'],'UNVERIFIED');self.assertFalse(v['scored']);self.assertEqual(v['score']['code'],'UNASSESSED')

    def test_free_form_phase_check_and_numeric_fields_never_export(self):
        run=next(r for r in self.manifest['runs'] if r['key']=='sglang-c_parse')
        f=self.j/'sglang-c_parse-grading.json';s=json.loads(f.read_text());e=json.loads(s['private_raw_sandbox']['output'])
        e['steps'][1].update(phase='private_fixture_sentinel',returncode='private_numeric_sentinel',wall_seconds='private_time_sentinel')
        e['additional_checks'][0]['id']='private_check_sentinel'
        s['private_raw_sandbox']['output']=json.dumps(e);self.write(f,s)
        run['sources']['grading']['sha256']=digest(f.read_bytes());self.store=self.load()
        v=self.store.dispatch('experiment_get',{'run_id':run['id']})
        text=json.dumps(v)
        for token in ['private_fixture_sentinel','private_numeric_sentinel','private_time_sentinel','private_check_sentinel']:self.assertNotIn(token,text)

    def test_incomplete_or_inconsistent_grading_never_claims_scored_pass(self):
        run=next(r for r in self.manifest['runs'] if r['key']=='sglang-c_parse')
        f=self.j/'sglang-c_parse-grading.json';s=json.loads(f.read_text())
        s['score'].update(code='PASS',overall='PASS');s.pop('private_raw_sandbox')
        self.write(f,s);run['sources']['grading']['sha256']=digest(f.read_bytes());self.store=self.load()
        v=self.store.dispatch('experiment_get',{'run_id':run['id']})
        self.assertEqual(v['classification'],'UNVERIFIED');self.assertFalse(v['scored']);self.assertEqual(v['score']['code'],'UNASSESSED')


class ExperimentAdapterTests(unittest.TestCase):
    write=ExperimentTests.write
    load=ExperimentTests.load
    ident=ExperimentTests.ident
    def setUp(self):
        ExperimentTests.setUp(self)
        (PROJECT/'.runtime').mkdir(exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=PROJECT/'.runtime',prefix='experiment-');self.addCleanup(temp.cleanup)
        self.runtime=Path(temp.name);(self.runtime/'logs').mkdir(mode=0o700)
        self.write(self.runtime/'experiment-read-policy.json',{'schema':1,'manifest':str(self.path),'sha256':digest(self.path.read_bytes())})

    def adapter(self,enabled=True):
        from mcp_adapter import Adapter
        a=Adapter(self.runtime,allow_experiments=enabled);self.addCleanup(a.audit.close);return a

    def call(self,adapter,name,args):
        from mcp_adapter import VERSION_META,REQUEST_PROTOCOL,CAPABILITIES_META
        return adapter.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args,
            '_meta':{VERSION_META:REQUEST_PROTOCOL,CAPABILITIES_META:{}}}})

    def test_disabled_existing_23_definitions_identical(self):
        from mcp_adapter import tool_definitions
        old=tool_definitions(False,True,True,True,True)
        same=tool_definitions(False,True,True,True,True,False)
        new=tool_definitions(False,True,True,True,True,True)
        self.assertEqual(old,same);self.assertEqual(len(old),23);self.assertEqual(new[:23],old);self.assertEqual(len(new),27)
        a=self.adapter(False)
        reply=self.call(a,'experiment_list',{})
        self.assertIn('error',reply)

    def test_adapter_schema_dispatch_audit_and_no_execution(self):
        a=self.adapter()
        with patch('subprocess.Popen',side_effect=AssertionError('No process may start')):
            r=self.call(a,'experiment_artifacts',{'run_id':self.ident(),'artifact_id':'compile_log'})
        self.assertFalse(r['result']['isError']);self.assertIn('content_base64',r['result']['structuredContent']['data'])
        a.audit.flush();log=''.join(p.read_text() for p in (self.runtime/'logs').iterdir())
        self.assertNotIn('candidate compiled',log);self.assertNotIn(str(self.root),log)

    def test_adapter_errors_are_fixed_and_no_private_exception(self):
        a=self.adapter()
        with patch.object(a.experiments,'dispatch',side_effect=OSError('PRIVATE_EXCEPTION_SECRET')):
            r=self.call(a,'experiment_get',{'run_id':self.ident()})
        self.assertTrue(r['result']['isError']);self.assertNotIn('PRIVATE_EXCEPTION_SECRET',json.dumps(r))

    def test_activation_requires_valid_private_policy(self):
        p=self.runtime/'experiment-read-policy.json';p.write_text('{}');p.chmod(0o600)
        with self.assertRaises(EvidenceError):self.adapter()

    def test_service_adapter_flag_only_and_login_pin_when_on(self):
        from poc_services import Services
        from poc_login import snapshot,LoginBlocked
        from experiments import experiment_snapshot
        from unittest.mock import Mock
        m=Services.__new__(Services)
        m.root=self.runtime;m.codex=Path('/fixture/codex');m.client=Path('/fixture/tunnel-client')
        m.key=Path('/fixture/key');m.health=self.runtime/'health'
        m.allow_local_llm=m.allow_local_models=m.allow_chat_mailbox=m.allow_mailbox_grants=True
        m.allow_codex_pipeline=False;m.allow_experiments=False
        before=m.commands();self.assertNotIn('--allow-experiments',' '.join(before['tunnel']))
        state={'local_llm_enabled':True,'local_models_enabled':True,'chat_mailbox_enabled':True,
            'mailbox_grants_enabled':True,'codex_file_reference':str(m.codex),'credential_file_reference':str(m.key)}
        m.read_state=lambda:state
        grant=Mock();grant.sha256.return_value='b'*64
        with patch('poc_login.Mailbox.policy_snapshot',return_value=(None,None,'a'*64)),patch('poc_login.load_runtime_grant',return_value=grant):
            _,old,oldpin=snapshot(m)
            self.assertNotIn('experiments_enabled',old)
            state['experiments_enabled']=False
            self.assertEqual(snapshot(m)[2],oldpin)
            m.allow_experiments=True
            commands=m.commands()
            self.assertEqual(commands['daemon'],before['daemon'])
            self.assertEqual(' '.join(commands['tunnel']).count('--allow-experiments'),1)
            state.update(experiments_enabled=True,experiment_read_snapshot=experiment_snapshot(self.runtime))
            self.assertNotEqual(snapshot(m)[2],oldpin)
            state['experiment_read_snapshot']['manifest_sha256']='0'*64
            with self.assertRaisesRegex(LoginBlocked,'EXPERIMENT_READ_PIN_MISMATCH'):snapshot(m)


if __name__=='__main__':unittest.main()
