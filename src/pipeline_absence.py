"""Truthful no-durable-rollout resolution; never represents a native archive."""
import re,time
from codex_pipeline import PipelineError,digest,HELD
KIND='NO_DURABLE_ROLLOUT_CONFIRMED'
NOT_FOUND='NATIVE_THREAD_NOT_FOUND_CONFIRMED'
def exact_missing_reason(error,remote_id):
 if error.code==-32600 and error.server_message=='thread '+remote_id+' not found':return 'OWNED_THREAD_NOT_FOUND'
 return None
def eligible(s,tid):
 t=s['threads'].get(tid)
 if (not t or t.get('archived') or t.get('active_task') or not t.get('remote_id')
     or any(v['thread_id']==tid for v in s['tasks'].values())):raise PipelineError('ABSENCE_REQUIRES_UNUSED_OWNED_CONTEXT')
 if (t.get('native_creation_count')!=1 or t.get('replacement_attempts',0)!=0
     or t.get('native_replacements') or t.get('original_remote_id')
     or t.get('native_context_state','READY') not in ('READY',KIND,NOT_FOUND)):
  raise PipelineError('ABSENCE_REPLACEMENT_LINEAGE_UNCERTAIN')
 if any(o.get('context',{}).get('thread_id')==tid and o.get('kind') not in ('thread_start','archive') for o in s['ops'].values()):
  raise PipelineError('ABSENCE_RELATED_OPERATION_UNCERTAIN')
 if any(o.get('kind')=='archive' and o.get('context',{}).get('thread_id')==tid and o['context'].get('remote_id')!=t['remote_id'] for o in s['ops'].values()):
  raise PipelineError('ABSENCE_ARCHIVE_IDENTITY_MISMATCH')
 births=[o for o in s['ops'].values() if o.get('kind')=='thread_start' and o.get('context',{}).get('thread_id')==tid]
 if len(births)!=1 or births[0]['status']!='SUCCEEDED':raise PipelineError('ABSENCE_CREATION_UNCERTAIN')
 if any(v['state'] in HELD for v in s['tasks'].values()):raise PipelineError('ABSENCE_REQUIRES_IDLE_EXECUTOR')
 return t

def validated(s,tid):
 try:
  t=eligible(s,tid);r=t['absence_resolution'];p=r['proof']
  return (isinstance(p,dict) and t.get('native_context_state')==r.get('kind') and r.get('kind')==(KIND if p.get('reason')=='NO_ROLLOUT' else NOT_FOUND) and not t.get('closing')
   and set(r)=={'kind','proof','sha256','native_archive_confirmed'} and r['kind'] in (KIND,NOT_FOUND)
   and r['native_archive_confirmed'] is False and digest(p)==r['sha256']
   and set(p)=={'thread_id','remote_id_sha256','policy_hash','observed_at','method','rpc_error_code','reason'}
   and p['thread_id']==tid and p['remote_id_sha256']==digest(t['remote_id'])
   and isinstance(p['policy_hash'],str) and re.fullmatch('[a-f0-9]{64}',p['policy_hash']) is not None
   and type(p['observed_at']) in (int,float) and 0<p['observed_at']<10**12
   and p['method'] in ('thread/read','thread/resume') and (p['method']!='thread/resume' or p['reason']=='NO_ROLLOUT') and p['rpc_error_code']==-32600 and p['reason'] in ('NO_ROLLOUT','OWNED_THREAD_NOT_FOUND'))
 except (KeyError,TypeError,ValueError):return False

def resolved_archive(s,op):
 tid=op.get('context',{}).get('thread_id')
 return (op.get('kind')=='archive' and op.get('status')=='UNKNOWN' and validated(s,tid)
         and op.get('context',{}).get('remote_id')==s['threads'][tid]['remote_id']
         and op.get('absence_resolution')=={'thread_id':tid,'sha256':s['threads'][tid]['absence_resolution']['sha256']})

def record(s,proofs):
 # Called only with fresh server receipts held in the owning gateway, never client-supplied proofs.
 for p in proofs:
  t=eligible(s,p['thread_id'])
  if p['policy_hash']!=s['policy_hash'] or p['remote_id_sha256']!=digest(t['remote_id']):raise PipelineError('ABSENCE_PROOF_STALE')
 for p in proofs:
  tid=p['thread_id'];t=s['threads'][tid]
  kind=KIND if p['reason']=='NO_ROLLOUT' else NOT_FOUND
  t.update(native_context_state=kind,closing=False,absence_resolution={'kind':kind,'proof':dict(p),'sha256':digest(p),'native_archive_confirmed':False})
  if not validated(s,tid):raise PipelineError('INVALID_ABSENCE_PROOF')
  for op in s['ops'].values():
   if op.get('kind')=='archive' and op.get('context',{}).get('thread_id')==tid and op.get('status')=='UNKNOWN':
    op['absence_resolution']={'thread_id':tid,'sha256':digest(p)} # Original UNKNOWN/result retained.
  s['events'].append({'seq':len(s['events'])+1,'kind':'native_context_unavailable_confirmed','time':time.time(),'thread_id':tid,'proof_sha256':digest(p),'native_archive_confirmed':False})
 return {'status':'RESOLVED_NATIVE_CONTEXT_UNAVAILABLE','thread_ids':[p['thread_id'] for p in proofs],'native_archive_confirmed':False,'original_operation_outcomes_preserved':True}
