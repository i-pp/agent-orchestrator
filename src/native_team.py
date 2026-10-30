"""Durable admission/receipt adapter for standard native tools; never a launcher.

Reuses the previously tested Plan and artifact adoption modules. A caller must
admit before spawn, bind the returned native ID, and acknowledge completion.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

from native_team_plan import Plan
from native_team_artifacts import fingerprint,_paths,_no_symlinks
from storage import runtime_lock
from chat_mailbox import _json,_private
from common_policy import load_project_policy

MAX_STATE=1024*1024

def git(path,*args):
    return subprocess.run(['git','--no-optional-locks','-c','core.fsmonitor=false','-C',str(path),*args],
                          capture_output=True,check=True,timeout=10).stdout

def revision(path):return git(path,'rev-parse','HEAD').decode().strip()
def common_dir(path):
    value=Path(git(path,'rev-parse','--git-common-dir').decode().strip())
    return str((path/value).resolve()) if not value.is_absolute() else str(value.resolve())
def sha(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def registry():
    return {'roles':{'Implementer':{'native_role':'implementer','writes':'own worktree only'},
        'Verifier':{'native_role':'verifier','writes':'verification outputs only'},
        'Researcher':{'native_role':'researcher','writes':'none'},'Coordinator':{'native_role':'parent','writes':'integration only'}},
        'backends':{'cloud':{'dispatch_enabled':True,'provider':'inherited'},
                    'local':{'dispatch_enabled':False,'reason':'Separate approval and real API verification required'}},
        'budgets':{'cloud_concurrency':2,'local_concurrency':1,'max_dispatches':6,'max_retries':2,
                   'task_timeout_s':300,'run_timeout_s':900}}

class NativeTeam:
    def __init__(self,root,clock=time.time):
        self.root=_no_symlinks(root);self.clock=clock
        s=self.root.stat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid!=os.getuid() or stat.S_IMODE(s.st_mode)!=0o700:
            raise ValueError('PRIVATE_RUN_DIRECTORY_REQUIRED')
        self.path=self.root/'native-team.json'

    @classmethod
    def create(cls,root,spec,clock=time.time):
        spec=deepcopy(spec)
        if set(spec)!={'schema','goal','requirements','project','policy','registry','tasks'} or type(spec['schema']) is not int or spec['schema']!=1:
            raise ValueError('INVALID_TEAM_SPEC')
        if (not isinstance(spec['goal'],str) or not spec['goal'].strip() or not isinstance(spec['requirements'],list)
            or not spec['requirements'] or any(not isinstance(x,str) or not x.strip() for x in spec['requirements'])):
            raise ValueError('REQUIREMENTS_REQUIRED')
        project=_no_symlinks(spec['project']);bundle=load_project_policy(project)
        if spec['policy']!={'version':bundle.version,'sha256':bundle.sha256}:raise ValueError('POLICY_PIN_MISMATCH')
        reg=spec['registry']
        if set(reg)!={'roles','backends','budgets'}:raise ValueError('INVALID_REGISTRY')
        p=Plan(spec['tasks'],reg['roles'],reg['budgets'])
        for key,limit in {'cloud_concurrency':2,'local_concurrency':1,'max_dispatches':6,'max_retries':2,
                          'task_timeout_s':300,'run_timeout_s':900}.items():
            if reg['budgets'][key]>limit:raise ValueError('PROJECT_BUDGET_CEILING')
        if set(reg['backends'])!={'cloud','local'} or reg['backends']['local'].get('dispatch_enabled') is not False:
            raise ValueError('LOCAL_BACKEND_NOT_AUTHORIZED')
        seen=[]
        for task in spec['tasks']:
            if not isinstance(task['allowed'],list):raise ValueError('INVALID_ARTIFACT_ALLOWLIST')
            if task['allowed']:_paths(task['allowed'])
            workspace=_no_symlinks(task['workspace'])
            if any(workspace==other or workspace in other.parents or other in workspace.parents for other in seen):
                raise ValueError('WORKSPACES_MUST_BE_DISTINCT_NON_NESTED')
            seen.append(workspace)
        root=_no_symlinks(root);root.mkdir(mode=0o700,parents=False,exist_ok=False)
        obj=cls(root,clock);now=obj._now()
        data={'schema':1,'spec':spec,'spec_sha256':sha(spec),'plan':p.snapshot(),
              'common_dir':common_dir(project),'bindings':{},'receipts':{},'decisions':[],
              'last_time':now,'credit_cost':'unmeasured','automatic_dispatch':False}
        with runtime_lock(obj.root):obj._save(data)
        return obj

    def _now(self):
        now=self.clock()
        if not Plan._number(now) or now<0:raise ValueError('CLOCK_UNKNOWN')
        return now

    def _read(self):
        _private(self.path,MAX_STATE);envelope=_json(self.path.read_bytes())
        if set(envelope)!={'sha256','data'} or sha(envelope['data'])!=envelope['sha256']:
            raise ValueError('STATE_HASH_MISMATCH')
        data=envelope['data'];spec=data['spec']
        if data['schema']!=1 or data['spec_sha256']!=sha(spec):raise ValueError('STATE_SPEC_MISMATCH')
        p=Plan(spec['tasks'],spec['registry']['roles'],spec['registry']['budgets']);saved=data['plan']
        if set(saved)!=set(p.snapshot()) or set(saved['tasks'])!=set(p.tasks):raise ValueError('INVALID_SNAPSHOT')
        for k,task in p.tasks.items():
            if any(saved['tasks'][k].get(f)!=task[f] for f in ['id','role','backend','deps','workspace','instruction','allowed']):
                raise ValueError('IMMUTABLE_TASK_CHANGED')
            if saved['tasks'][k]['state'] not in ['QUEUED','RUNNING','SUCCEEDED','FAILED','TIMED_OUT','WAITING_FOR_HUMAN','UNKNOWN']:
                raise ValueError('INVALID_TASK_STATE')
        p.tasks=deepcopy(saved['tasks']);p.events=deepcopy(saved['events']);p.budgets=deepcopy(saved['budgets'])
        if p.budgets!=spec['registry']['budgets'] or saved['registry']!=spec['registry']['roles']:
            raise ValueError('BUDGET_OR_REGISTRY_CHANGED')
        p.registry=deepcopy(saved['registry']);p.run_started=saved['run_started'];p.dispatches=saved['dispatches']
        return data,p

    def _save(self,data):
        raw=(json.dumps({'data':data,'sha256':sha(data)},sort_keys=True,allow_nan=False)+'\n').encode()
        if len(raw)>MAX_STATE:raise ValueError('STATE_SIZE_LIMIT')
        temp=self.root/('state-'+uuid.uuid4().hex+'.tmp')
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
            os.replace(temp,self.path)
            fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        finally:
            if temp.exists():temp.unlink()

    @contextmanager
    def _change(self):
        with runtime_lock(self.root):
            data,p=self._read();now=self._now()
            if now<data['last_time']:raise ValueError('CLOCK_REGRESSED')
            yield data,p,now
            data.update(plan=p.snapshot(),last_time=now);self._save(data)

    def snapshot(self):
        with runtime_lock(self.root):return deepcopy(self._read()[0])

    def admit(self,task_id,actor):
        with self._change() as (data,p,now):
            task=p._task(task_id);reg=data['spec']['registry']
            if reg['backends'].get(task['backend'],{}).get('dispatch_enabled') is not True:
                raise ValueError('BACKEND_DISABLED')
            held=[p.tasks[k] for k,v in data['bindings'].items() if not v['released']]
            if sum(t['backend']==task['backend'] for t in held)>=p.budgets[task['backend']+'_concurrency']:
                raise ValueError('UNRELEASED_CONCURRENCY_BUDGET')
            workspace=_no_symlinks(task['workspace'])
            if str(workspace)!=git(workspace,'rev-parse','--show-toplevel').decode().strip() or common_dir(workspace)!=data['common_dir']:
                raise ValueError('EXPECTED_GIT_WORKTREE_REQUIRED')
            if git(workspace,'status','--porcelain','-z','--untracked-files=all'):raise ValueError('DIRTY_WORKTREE_REQUIRES_RECONCILIATION')
            if any(t['workspace']==str(workspace) for t in held):raise ValueError('WORKSPACE_UNRELEASED')
            bundle=load_project_policy(Path(data['spec']['project']))
            if data['spec']['policy']!={'version':bundle.version,'sha256':bundle.sha256}:raise ValueError('POLICY_PIN_MISMATCH')
            p.start(task_id,actor,now)
            ticket={'dispatch_id':str(uuid.uuid4()),'actor':actor,'agent_id':None,'released':False,
                    'input_revision':revision(workspace),'admitted_at':now}
            data['bindings'][task_id]=ticket
        return deepcopy(ticket)

    def bind(self,task_id,dispatch_id,agent_id):
        if not isinstance(agent_id,str) or not agent_id.strip() or len(agent_id)>200:raise ValueError('NATIVE_ID_REQUIRED')
        with self._change() as (data,p,now):
            b=self._ticket(data,task_id,dispatch_id)
            if b['released'] or b['agent_id'] not in [None,agent_id]:raise ValueError('NATIVE_ID_CONFLICT')
            b['agent_id']=agent_id
            data['decisions'].append({'action':'bind','task':task_id,'agent_id':agent_id,'time':now})

    @staticmethod
    def _ticket(data,task_id,dispatch_id):
        b=data['bindings'][task_id]
        if b['dispatch_id']!=dispatch_id:raise ValueError('DISPATCH_TICKET_MISMATCH')
        return b

    def complete(self,task_id,dispatch_id,agent_id,success,reason,verification):
        if (type(success) is not bool or set(verification)!={'commands','exit_codes'}
            or not verification['commands'] or len(verification['commands'])!=len(verification['exit_codes'])
            or any(not isinstance(x,str) or not x for x in verification['commands'])
            or any(type(x) is not int for x in verification['exit_codes'])
            or (success and any(verification['exit_codes']))):raise ValueError('VERIFICATION_RECEIPT_REQUIRED')
        with self._change() as (data,p,now):
            b=self._ticket(data,task_id,dispatch_id);task=p._task(task_id)
            if b['released'] or not b['agent_id'] or b['agent_id']!=agent_id:raise ValueError('UNCONFIRMED_NATIVE_TERMINAL')
            workspace=Path(task['workspace'])
            if task['role']!='Coordinator' and revision(workspace)!=b['input_revision']:raise ValueError('WORKER_COMMIT_NOT_ALLOWED')
            changed=set(x.decode() for x in git(workspace,'diff','--no-ext-diff','--no-textconv','--name-only','-z',b['input_revision'],'--').split(b'\0') if x)
            changed.update(x.decode() for x in git(workspace,'ls-files','--others','--exclude-standard','-z').split(b'\0') if x)
            if changed-set(task['allowed']):raise ValueError('OUT_OF_SCOPE_CHANGE')
            hashes=fingerprint(workspace,task['allowed']) if success and task['allowed'] else {}
            receipt={'agent_id':agent_id,'dispatch_id':dispatch_id,'reason':reason,'verification':verification,
                     'artifacts':hashes,'input_revision':b['input_revision'],'output_revision':revision(workspace),
                     'native_terminal_acknowledged':True,'time':now}
            p.finish(task_id,success,now,reason,receipt);b['released']=True;data['receipts'][task_id]=receipt
        return deepcopy(receipt)

    def expire(self):
        with self._change() as (data,p,now):
            expired=p.expire(now)
            for key in expired:data['bindings'][key]['interrupt_required']=True
        return expired

    def unknown(self,task_id,dispatch_id,reason):
        if not isinstance(reason,str) or not reason.strip():raise ValueError('REASON_REQUIRED')
        with self._change() as (data,p,now):
            b=self._ticket(data,task_id,dispatch_id);task=p._task(task_id)
            if b['released'] or task['state']!='RUNNING':raise ValueError('NO_UNRESOLVED_DISPATCH')
            task.update(state='UNKNOWN',reason=reason,ended=now);p._record('unknown',task,now)

    def acknowledge_release(self,task_id,dispatch_id,agent_id,reason):
        if not isinstance(reason,str) or not reason.strip():raise ValueError('REASON_REQUIRED')
        with self._change() as (data,p,now):
            b=self._ticket(data,task_id,dispatch_id);task=p._task(task_id)
            if task['state'] not in ['UNKNOWN','TIMED_OUT'] or not b['agent_id'] or b['agent_id']!=agent_id:
                raise ValueError('NATIVE_TERMINAL_RECONCILIATION_REQUIRED')
            b.update(released=True,release_reason=reason,release_time=now)
            data['decisions'].append({'action':'release','task':task_id,'agent_id':agent_id,'reason':reason,'time':now})

    def retry(self,task_id,cause_code):
        if not isinstance(cause_code,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',cause_code):raise ValueError('STABLE_CAUSE_CODE_REQUIRED')
        with self._change() as (data,p,now):
            if not data['bindings'][task_id]['released']:raise ValueError('NATIVE_RELEASE_UNCONFIRMED')
            p.retry(task_id,cause_code,now)

    def escalate(self,task_id,reason):
        with self._change() as (data,p,now):p.escalate(task_id,reason,now)
