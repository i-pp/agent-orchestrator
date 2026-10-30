"""Owner-only fixed-ledger status and durable cancellation; never an MCP tool."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

PROJECT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PROJECT/'src'),str(PROJECT/'scripts')]
from chat_mailbox import MailboxError
from mailbox_grants import load_runtime_grant,GrantLedger
from poc_services import Services,Refused,RUNTIME

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['status','revoke'])
    parser.add_argument('--runtime',type=Path,default=RUNTIME)
    parser.add_argument('--grant-id',required=True)
    parser.add_argument('--policy-sha256',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.apply and args.operation!='revoke':raise ValueError('invalid apply')
        manager=Services(runtime=args.runtime)
        policy=load_runtime_grant(manager.root,require_enabled=False)
        if args.grant_id!=policy.grant_id or args.policy_sha256!=policy.sha256():
            print(json.dumps({'status':'BLOCKED','code':'GRANT_OWNER_CONFIRMATION_MISMATCH','automatic_retry':False}));return 2
        if args.operation=='revoke' and not args.apply:
            print(json.dumps({'status':'REQUIRES_APPLY','grant_id':policy.grant_id,
                'policy_sha256':policy.sha256(),'permanent':True,'automatic_retry':False}));return 2
        ledger=GrantLedger(manager.root,policy,enabled=True,create=False)
        if args.operation=='revoke':ledger.revoke()  # No retry on an uncertain acknowledgement.
        print(json.dumps(ledger.status()));return 0
    except MailboxError as exc:
        print(json.dumps({'status':exc.status,'code':exc.code,'automatic_retry':False}));return 2
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error,Refused):
        print(json.dumps({'status':'BLOCKED','code':'GRANT_OWNER_REFERENCE_ERROR','automatic_retry':False}));return 2

if __name__=='__main__':raise SystemExit(main())
