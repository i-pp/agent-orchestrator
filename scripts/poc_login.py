"""Prepare a persistent login entry and restore only absent approved PoC services.

No install/enable/linger operations. Existing Services owns both transient units,
readiness, credentials, resource limits and startup failure cleanup.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'src'),str(PROJECT/'scripts')]
from poc_services import Services,Refused,RUNTIME
from control_socket import endpoint
from chat_mailbox import Mailbox,MailboxError
from mailbox_grants import load_runtime_grant

FLAGS=['local_llm_enabled','local_models_enabled','chat_mailbox_enabled','mailbox_grants_enabled']

class LoginBlocked(RuntimeError):
    def __init__(self,code):self.code=code;super().__init__(code)


def snapshot(manager):
    state=manager.read_state()
    if not state:raise LoginBlocked('MISSING_APPROVED_STATE')
    if any(state.get(k) is not True for k in FLAGS):raise LoginBlocked('INVOCATION_SCOPE_CHANGED')
    refs={}
    for name in ['credential_file_reference','codex_file_reference']:
        value=state.get(name)
        if not isinstance(value,str) or not Path(value).is_absolute():raise LoginBlocked('INVOCATION_REFERENCE_MISSING')
        refs[name]=value
    _,_,static_hash=Mailbox.policy_snapshot(manager.root)
    grant=load_runtime_grant(manager.root)
    data={'schema':1,'runtime':str(manager.root),**refs,**{k:state[k] for k in FLAGS},
          'fixed_mailbox_policy_sha256':static_hash,'grant_policy_sha256':grant.sha256()}
    pin=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return state,data,pin


def configured(runtime):
    manager=Services(runtime=runtime);state,_,_=snapshot(manager)
    return Services(manager.root,Path(state['codex_file_reference']),Path(state['credential_file_reference']),
                    *[state[k] for k in FLAGS])


def unit_name(manager):return manager.units['daemon'].removesuffix('-daemon.service')+'-login.service'


def escape(value,*,execution=False):
    value=str(value)
    if not value or any(ord(c)<32 or ord(c)==127 for c in value):raise LoginBlocked('INVALID_UNIT_PATH')
    value=value.replace('\\','\\\\').replace('"','\\"').replace('%','%%')
    return value.replace('$','$$') if execution else value


def render_unit(project,runtime,pin):
    if not isinstance(pin,str) or not re.fullmatch(r'[0-9a-f]{64}',pin):raise LoginBlocked('INVALID_INVOCATION_PIN')
    project=Path(project);runtime=Path(runtime)
    if not project.is_absolute() or not runtime.is_absolute() or runtime.parent!=project/'.runtime':
        raise LoginBlocked('INVALID_UNIT_PATH')
    text=(PROJECT/'systemd/agent-orchestrator-login.example.service').read_text()
    values={'HOME_EXEC':escape(Path.home(),execution=True),
        'SCRIPT_EXEC':escape(project/'scripts/poc_login.py',execution=True),
        'RUNTIME_EXEC':escape(runtime,execution=True),'PIN':pin}
    for key,value in values.items():text=text.replace('@'+key+'@',value)
    if re.search(r'@[A-Z_]+@',text):raise LoginBlocked('UNRESOLVED_UNIT_TEMPLATE')
    return text


def plan(manager):
    _,data,pin=snapshot(manager)
    return {'status':'PREPARED','unit':unit_name(manager),'expected_invocation_sha256':pin,
        'snapshot':data,'installation_performed':False,'enable_performed':False,'stops_or_launches':0,
        'survives_logout':False,'requires_linger':False,'automatic_write_retries':0,
        'readiness_owner':'existing poc_services.Services','startup_preflight_required':True}


def restore(manager,expected_pin):
    if not isinstance(expected_pin,str) or not re.fullmatch(r'[0-9a-f]{64}',expected_pin):
        raise LoginBlocked('INVALID_INVOCATION_PIN')
    state,_,pin=snapshot(manager)
    if pin!=expected_pin:raise LoginBlocked('INVOCATION_PIN_MISMATCH')
    observed=manager.status()
    if observed['phase']=='READY':
        return {'status':'ALREADY_READY','stops_or_launches':0,'automatic_retry':False}
    if state['phase'] not in ['READY','STOPPED']:
        raise LoginBlocked('INCOMPLETE_OR_FAILED_STATE_REQUIRES_RECONCILIATION')
    # The existing lock also serializes the ordinary CLI. No blind stop of a unit
    # that could have appeared or acquired work since the first observation.
    with manager.locked():
        state,_,pin=snapshot(manager)
        if pin!=expected_pin:raise LoginBlocked('INVOCATION_PIN_MISMATCH')
        if state['phase'] not in ['READY','STOPPED']:
            raise LoginBlocked('INCOMPLETE_OR_FAILED_STATE_REQUIRES_RECONCILIATION')
        props=[manager.show(name) for name in manager.units.values()]
        if any(p.get('LoadState')!='not-found' for p in props):
            raise LoginBlocked('EXISTING_UNIT_REQUIRES_RECONCILIATION')
        try:socket_path=endpoint(manager.root)
        except FileNotFoundError:socket_path=None
        if socket_path is not None and (socket_path.exists() or socket_path.is_symlink()):
            raise LoginBlocked('EXISTING_SOCKET_REQUIRES_RECONCILIATION')
        if manager.health.exists() or manager.health.is_symlink():
            raise LoginBlocked('EXISTING_HEALTH_LOCATOR_REQUIRES_RECONCILIATION')
        checked=manager.preflight(Path(state['credential_file_reference']))
        if not checked['passed'] or len(checked['tool_names'])!=11:
            raise LoginBlocked('APPROVED_ELEVEN_TOOLS_NOT_CONFIRMED')
        # Only reconcile a lost manager's state; no stop(), file deletion, or
        # policy/ledger mutation. start() rechecks idle work under the same lock.
        if state['phase']=='READY':
            state.update(phase='STOPPED',updated=time.time());manager.write_state(state)
    # start() checks for a concurrent owner before launching; never stop/adopt it.
    try:manager.start()
    except (OSError,ValueError,RuntimeError,sqlite3.Error,subprocess.SubprocessError):
        try:phase=(manager.read_state() or {}).get('phase','UNKNOWN')
        except (OSError,ValueError,RuntimeError):phase='UNKNOWN'
        return {'status':'BLOCKED' if phase=='FAILED' else 'UNKNOWN','code':'START_FAILED_RECONCILE_BEFORE_RETRY',
                'phase':phase,'automatic_retry':False}
    try:health=manager.status()
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):
        return {'status':'UNKNOWN','code':'READINESS_NOT_CONFIRMED','automatic_retry':False}
    if health['phase']!='READY':
        return {'status':'UNKNOWN','code':'READINESS_NOT_CONFIRMED','automatic_retry':False}
    return {'status':'READY','unit':unit_name(manager),'service_health':health,
            'expected_invocation_sha256':expected_pin,'automatic_retry':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['plan','render','restore'])
    parser.add_argument('--runtime',type=Path,default=RUNTIME)
    parser.add_argument('--expected-invocation-sha256')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.operation=='restore' and not args.apply:raise LoginBlocked('EXPLICIT_APPLY_REQUIRED')
        if args.operation!='restore' and args.apply:raise LoginBlocked('INVALID_APPLY_OPTION')
        manager=configured(args.runtime)
        if args.operation=='restore':result=restore(manager,args.expected_invocation_sha256)
        elif args.operation=='plan':result=plan(manager)
        else:
            info=plan(manager)
            print(render_unit(PROJECT,manager.root,info['expected_invocation_sha256']),end='');return 0
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result['status'] in ['PREPARED','ALREADY_READY','READY'] else 2
    except LoginBlocked as exc:
        result={'status':'BLOCKED','code':exc.code,'automatic_retry':False}
    except (OSError,ValueError,RuntimeError,sqlite3.Error,MailboxError,Refused,subprocess.SubprocessError):
        result={'status':'BLOCKED','code':'LOGIN_REFERENCE_OR_SERVICE_CHECK_FAILED','automatic_retry':False}
    print(json.dumps(result));return 2

if __name__=='__main__':raise SystemExit(main())
