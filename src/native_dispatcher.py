"""Durable parent-action dispatcher. No native API, subprocess launcher or service.

NativeTeam owns admission/leases/receipts. This adds a bounded action journal
inside its private atomic snapshot. The parent executes issued actions using
standard tools; this preparation contains no real tool executor.
"""
from copy import deepcopy
from pathlib import Path
import re
import uuid

from native_team import NativeTeam,sha
from native_team_plan import Plan
from native_team_artifacts import _no_symlinks
from storage import runtime_lock

KINDS={'SPAWN','COORDINATE','POLL','INTERRUPT','VERIFY'}

class DispatchError(ValueError):
    pass

class NativeDispatcher:
    def __init__(self,team):
        self.team=team
        state=team.snapshot()
        self._journal(state)
        self.lock_dir=_no_symlinks(team.root/'dispatcher-control')
        self.lock_dir.mkdir(mode=0o700,exist_ok=True)
        if self.lock_dir.stat().st_mode&0o777!=0o700:raise DispatchError('PRIVATE_CONTROL_DIRECTORY_REQUIRED')

    @classmethod
    def receive(cls,root,spec,submission_key,**kwargs):
        if not isinstance(submission_key,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',submission_key):
            raise DispatchError('INVALID_SUBMISSION_KEY')
        root=Path(root)
        if root.exists():
            team=NativeTeam(root,**kwargs);state=team.snapshot();journal=cls._journal(state)
            if state['spec_sha256']!=sha(spec) or journal['submission_key']!=submission_key:
                raise DispatchError('SUBMISSION_CONFLICT')
            return cls(team)
        team=NativeTeam.create(root,spec,**kwargs)
        with team._change() as (data,plan,now):
            data['dispatcher']={'schema':1,'submission_key':submission_key,'spec_sha256':data['spec_sha256'],
                'actions':{},'retired':[],'terminal':{},'interrupt_issued':[],
                'next_poll':{},'orphans':[],'admission_blocked':{},'events':[]}
        return cls(team)

    @staticmethod
    def _journal(data):
        j=data.get('dispatcher')
        if (not isinstance(j,dict) or j.get('schema')!=1 or j.get('spec_sha256')!=data['spec_sha256']):
            raise DispatchError('DISPATCH_JOURNAL_RECONCILIATION_REQUIRED')
        return j

    def _edit(self,fn):
        with self.team._change() as (data,plan,now):
            return fn(self._journal(data),data,now)

    def snapshot(self):return self.team.snapshot()

    def _event(self,j,kind,**fields):
        j['events'].append({'kind':kind,**fields});j['events']=j['events'][-32:]

    def _queue(self,kind,task,binding):
        def add(j,data,now):
            if any(a['task_id']==task['id'] and a['dispatch_id']==binding['dispatch_id'] and a['kind']==kind
                   for a in j['actions'].values()):return
            if kind=='INTERRUPT' and binding['dispatch_id'] in j['interrupt_issued']:return
            a={'action_id':str(uuid.uuid4()),'kind':kind,'task_id':task['id'],
               'dispatch_id':binding['dispatch_id'],'agent_id':binding['agent_id'],'status':'READY','created':now}
            if kind in ['SPAWN','COORDINATE']:
                a['task']={k:task[k] for k in ['role','workspace','instruction','allowed']}
                a['input_revision']=binding['input_revision'];a['policy']=data['spec']['policy']
            j['actions'][a['action_id']]=a
        self._edit(add)

    def _retire(self,action_id):
        def retire(j,data,now):
            a=j['actions'].pop(action_id)
            j['retired'].append({'action_id':action_id,'digest':a.get('observation_digest'),
                'kind':a['kind'],'task_id':a['task_id'],'dispatch_id':a['dispatch_id']})
            j['retired']=j['retired'][-32:]
            if a['kind']=='POLL':j['next_poll'][a['dispatch_id']]=now+1
            self._event(j,'ACK',action_id=action_id,task_id=a['task_id'],time=now)
        self._edit(retire)

    @staticmethod
    def _validate(a,e):
        expected={'SPAWN':{'agent_id'},'COORDINATE':{'agent_id','accepted','reason','verification'},
          'POLL':{'agent_id','state'},'INTERRUPT':{'agent_id','accepted'},
          'VERIFY':{'agent_id','accepted','reason','verification'}}[a['kind']]
        if not isinstance(e,dict) or set(e)!=expected:raise DispatchError('INVALID_OBSERVATION_FIELDS')
        if not isinstance(e['agent_id'],str) or not e['agent_id'].strip() or len(e['agent_id'])>200:
            raise DispatchError('INVALID_NATIVE_ID')
        if a['agent_id'] is not None and e['agent_id']!=a['agent_id']:raise DispatchError('NATIVE_ID_MISMATCH')
        if 'accepted' in e and type(e['accepted']) is not bool:raise DispatchError('INVALID_ACCEPTANCE')
        if 'state' in e and e['state'] not in ['RUNNING','SUCCEEDED','FAILED','CANCELLED','UNKNOWN']:
            raise DispatchError('INVALID_NATIVE_STATE')
        if 'verification' in e:
            v=e['verification']
            if (not isinstance(v,dict) or set(v)!={'commands','exit_codes'} or not isinstance(v['commands'],list)
                or not isinstance(v['exit_codes'],list)
                or not v['commands'] or len(v['commands'])!=len(v['exit_codes'])
                or any(not isinstance(x,str) or not x.strip() for x in v['commands'])
                or any(type(x) is not int for x in v['exit_codes'])
                or (e['accepted'] and any(v['exit_codes']))):raise DispatchError('INVALID_VERIFICATION')
            if not isinstance(e['reason'],str) or not e['reason'].strip() or len(e['reason'])>512:
                raise DispatchError('INVALID_REASON')

    def _apply(self,a):
        e=a['observation'];task_id=a['task_id'];ticket=a['dispatch_id'];agent=e['agent_id']
        state=self.team.snapshot();b=state['bindings'][task_id];task=state['plan']['tasks'][task_id]
        if b['dispatch_id']!=ticket:raise DispatchError('DISPATCH_TICKET_MISMATCH')
        if a['kind'] in ['SPAWN','COORDINATE']:
            if b['agent_id'] is None:self.team.bind(task_id,ticket,agent)
            elif b['agent_id']!=agent:raise DispatchError('NATIVE_ID_CONFLICT')
        if a['kind']=='POLL':
            if b['agent_id']!=agent:raise DispatchError('NATIVE_ID_MISMATCH')
            if e['state']=='UNKNOWN':
                if task['state']=='RUNNING':self.team.unknown(task_id,ticket,'NATIVE_STATUS_UNKNOWN')
            elif e['state']!='RUNNING':
                self._edit(lambda j,data,now:j['terminal'].update({ticket:{'agent_id':agent,'state':e['state']}}))
                if task['state'] in ['UNKNOWN','TIMED_OUT'] and not b['released']:
                    self.team.acknowledge_release(task_id,ticket,agent,'Native terminal observed; no quality adoption')
        elif a['kind'] in ['VERIFY','COORDINATE']:
            if a['kind']=='COORDINATE':native_success=True
            else:
                terminal=self._journal(state)['terminal'].get(ticket)
                if not terminal or terminal['agent_id']!=agent:raise DispatchError('NATIVE_TERMINAL_REQUIRED')
                native_success=terminal['state']=='SUCCEEDED'
            if b['released']:
                receipt=state['receipts'].get(task_id,{})
                if (receipt.get('agent_id')!=agent or receipt.get('dispatch_id')!=ticket
                    or receipt.get('verification')!=e['verification'] or receipt.get('reason')!=e['reason']):
                    raise DispatchError('COMPLETION_RECEIPT_CONFLICT')
            elif task['state'] in ['UNKNOWN','TIMED_OUT']:
                self.team.acknowledge_release(task_id,ticket,agent,'Parent terminal observation after deadline; not adopted')
            else:
                self.team.complete(task_id,ticket,agent,e['accepted'] and native_success,e['reason'],e['verification'])
        # INTERRUPT accepted is deliberately NOT a terminal/release acknowledgment.
        self._retire(a['action_id'])

    def _apply_or_block(self,a):
        try:self._apply(a)
        except ValueError:
            state=self.team.snapshot();task=state['plan']['tasks'][a['task_id']]
            if task['state']=='RUNNING':self.team.unknown(a['task_id'],a['dispatch_id'],'LOCAL_RESULT_REJECTED')
            self._edit(lambda j,data,now:j['actions'][a['action_id']].update(status='BLOCKED',code='LOCAL_RESULT_REJECTED'))
            return False
        return True

    def observe(self,action_id,event):
        with runtime_lock(self.lock_dir):
            state=self.team.snapshot();j=self._journal(state)
            old=next((x for x in j['retired'] if x['action_id']==action_id),None)
            if old:
                if old['digest']!=sha(event):raise DispatchError('OBSERVATION_CONFLICT')
                return {'duplicate':True}
            a=j['actions'].get(action_id)
            if not a or a['status'] not in ['ISSUED','OBSERVED']:raise DispatchError('ACTION_NOT_ISSUED')
            self._validate(a,event)
            if a.get('observation_digest') not in [None,sha(event)]:raise DispatchError('OBSERVATION_CONFLICT')
            def freeze(j,data,now):
                row=j['actions'][action_id];row.update(status='OBSERVED',observation=deepcopy(event),observation_digest=sha(event))
            self._edit(freeze)
            applied=self._apply_or_block(self._journal(self.team.snapshot())['actions'][action_id])
            return {'duplicate':False,'applied':applied}

    def take(self):
        """Persist ISSUED before handoff. Repeated calls never repeat a write action."""
        with runtime_lock(self.lock_dir):
            result=[]
            with self.team._change() as (data,plan,now):
                j=self._journal(data)
                for task_id in plan.expire(now):
                    data['bindings'][task_id]['interrupt_required']=True
                for a in j['actions'].values():
                    if a['status']!='READY':continue
                    if a['kind'] in ['SPAWN','COORDINATE'] and plan.tasks[a['task_id']]['state']!='RUNNING':
                        a.update(status='BLOCKED',code='ADMISSION_EXPIRED_BEFORE_HANDOFF')
                        continue
                    a.update(status='ISSUED',issued_at=now)
                    if a['kind']=='INTERRUPT':j['interrupt_issued'].append(a['dispatch_id'])
                    result.append(deepcopy(a))
            return result

    def advance(self):
        """One bounded scheduling step; parent chooses when to call again."""
        with runtime_lock(self.lock_dir):
            # Only local acknowledged operations are replayed; no tool call occurs here.
            for a in list(self._journal(self.team.snapshot())['actions'].values()):
                if a['status']=='OBSERVED':self._apply_or_block(a)
            self.team.expire();state=self.team.snapshot();j=self._journal(state)
            spec=state['spec'];deadlines=Plan(spec['tasks'],spec['registry']['roles'],spec['registry']['budgets'])
            deadlines.tasks=deepcopy(state['plan']['tasks']);deadlines.run_started=state['plan']['run_started']
            for task_id,b in state['bindings'].items():
                if b['released']:continue
                task=state['plan']['tasks'][task_id]
                pending=[a for a in j['actions'].values() if a['task_id']==task_id and a['dispatch_id']==b['dispatch_id']]
                if b['agent_id'] is None:
                    if not any(a['kind'] in ['SPAWN','COORDINATE'] for a in pending):
                        if task['state']=='RUNNING':self.team.unknown(task_id,b['dispatch_id'],'ORPHANED_ADMISSION_NO_REPLAY')
                        self._edit(lambda journal,data,now:journal['orphans'].append(task_id) if task_id not in journal['orphans'] else None)
                    continue
                if task['role']=='Coordinator':continue
                terminal=j['terminal'].get(b['dispatch_id'])
                if terminal:
                    if task['state'] in ['UNKNOWN','TIMED_OUT']:
                        self.team.acknowledge_release(task_id,b['dispatch_id'],b['agent_id'],'Native terminal already confirmed; no adoption')
                    else:self._queue('VERIFY',task,b)
                else:
                    if task['state']=='TIMED_OUT' or (task['state']=='UNKNOWN' and deadlines._expired(task,self.team.clock())):
                        self._queue('INTERRUPT',task,b)
                    if self.team.clock()>=j['next_poll'].get(b['dispatch_id'],0):self._queue('POLL',task,b)
            state=self.team.snapshot();spec=state['spec'];plan=Plan(spec['tasks'],spec['registry']['roles'],spec['registry']['budgets'])
            plan.tasks=deepcopy(state['plan']['tasks'])
            for task_id in plan.ready():
                try:ticket=self.team.admit(task_id,'native parent-action dispatcher')
                except (ValueError,RuntimeError):
                    self._edit(lambda j,data,now:j['admission_blocked'].update({task_id:'ADMISSION_NOT_GRANTED'}));continue
                task=state['plan']['tasks'][task_id]
                self._queue('COORDINATE' if task['role']=='Coordinator' else 'SPAWN',task,ticket)
            final=self.team.snapshot()
            for action in list(self._journal(final)['actions'].values()):
                if final['bindings'][action['task_id']]['released']:self._retire(action['action_id'])
            final=self.team.snapshot();states={k:t['state'] for k,t in final['plan']['tasks'].items()}
            held=sum(not b['released'] for b in final['bindings'].values())
            ready=sum(a['status']=='READY' for a in self._journal(final)['actions'].values())
            status=('COMPLETE' if all(v=='SUCCEEDED' for v in states.values()) and not held else
                    'READY' if ready else 'AWAITING_OBSERVATION' if held else 'BLOCKED')
            return {'status':status,'states':states,
                'unreleased':sum(not b['released'] for b in final['bindings'].values()),
                'ready_actions':ready,
                'parent_executor_required':True}
