"""Local operator CLI; stdout plans require approval before scope changes."""
import argparse
import json
from pathlib import Path
import sys

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'src'),str(PROJECT/'scripts')]
from chat_mailbox import Mailbox,MailboxError,TOOLS,validate_arguments
from mailbox_operations import MailboxWorkflow,StdioMailbox,OperationError,prepare_request
from mcp_adapter import decode
from poc_services import Services,Refused,RUNTIME
from mailbox_grants import load_runtime_grant

def grant_for(manager,task_id):
    state=manager.read_state()
    if not state or state.get('mailbox_grants_enabled',False) is not True:return None
    policy=load_runtime_grant(manager.root)
    return policy if policy.task_id==task_id else None

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['prepare','register','inspect'])
    parser.add_argument('--runtime',type=Path,default=RUNTIME)
    parser.add_argument('--timeout',type=float,default=5)
    parser.add_argument('--include-content',action='store_true',help='Explicit non-secret body output for inspect only')
    args=parser.parse_args()
    try:
        if args.include_content and args.operation!='inspect':raise OperationError('INVALID_CONTENT_OPTION','BLOCKED')
        if not .05<=args.timeout<=10:raise OperationError('INVALID_TIMEOUT','BLOCKED')
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise OperationError('INPUT_BOUND','BLOCKED')
        value=decode(raw)
        manager=Services(runtime=args.runtime)
        _,allowed,_=Mailbox.policy_snapshot(manager.root)
        if args.operation=='prepare':
            if isinstance(value,dict) and (policy:=grant_for(manager,value.get('task_id'))):
                from chat_mailbox import digest
                if set(value)!={'task_id','request_id','request'} or not isinstance(value['request'],str):
                    raise OperationError('INVALID_DRAFT','BLOCKED')
                bundle={**value,'request_sha256':digest(value['request'])}
                validate_arguments(TOOLS[0],bundle)
                result={'status':'PREPARED_IN_GRANT_NAMESPACE','approval_required':False,
                    'install_performed':False,'write_permission_verified':False,
                    'grant_id':policy.grant_id,'policy_sha256':policy.sha256(),'request_bundle':bundle}
            else:result=prepare_request(value,allowed)
        else:
            validate_arguments(TOOLS[0] if args.operation=='register' else TOOLS[3],value)
            if (allowed.get((value['task_id'],value['request_id']))!=value['request_sha256']
                    and grant_for(manager,value['task_id']) is None):
                raise OperationError('MAILBOX_SLOT_NOT_ALLOWED','BLOCKED')
            flow=MailboxWorkflow(lambda:StdioMailbox(manager.root,args.timeout))
            result=flow.register(value) if args.operation=='register' else flow.inspect(value)
            if not args.include_content:
                result.pop('request',None);result.pop('answer',None)
        print(json.dumps(result,ensure_ascii=False))
        return 2 if result['status'] in ['UNKNOWN','BLOCKED'] else 0
    except (MailboxError,OperationError) as exc:
        print(json.dumps({'status':getattr(exc,'status','BLOCKED'),'code':exc.code,'automatic_retry':False}))
    except (OSError,ValueError,TypeError,KeyError,Refused):
        print(json.dumps({'status':'BLOCKED','code':'LOCAL_OPERATOR_INPUT_OR_REFERENCE_ERROR','automatic_retry':False}))
    return 2

if __name__=='__main__':raise SystemExit(main())
