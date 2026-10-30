"""Local JSON CLI for native-team admission and receipts; never dispatches tools."""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'src'))
from native_team import NativeTeam
from chat_mailbox import _json

MAX_INPUT=65536
OPERATIONS={
    'show':set(),
    'admit':{'task_id','actor'},
    'bind':{'task_id','dispatch_id','agent_id'},
    'complete':{'task_id','dispatch_id','agent_id','success','reason','verification'},
    'expire':set(),
    'unknown':{'task_id','dispatch_id','reason'},
    'release':{'task_id','dispatch_id','agent_id','reason'},
    'retry':{'task_id','cause_code'},
    'escalate':{'task_id','reason'},
}

class InputError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self,message):raise InputError('INVALID_CLI_ARGUMENTS')


def payload(stream,operation):
    raw=stream.read(MAX_INPUT+1)
    if len(raw)>MAX_INPUT:raise InputError('INPUT_SIZE_LIMIT')
    if not raw.strip():
        if operation in ['show','expire']:return {}
        raise InputError('JSON_OBJECT_REQUIRED')
    try:value=_json(raw)
    except ValueError:raise InputError('INVALID_JSON') from None
    if not isinstance(value,dict):raise InputError('JSON_OBJECT_REQUIRED')
    if operation!='init' and set(value)!=OPERATIONS[operation]:raise InputError('INVALID_OPERATION_FIELDS')
    return value


def invoke(operation,root,value):
    if operation=='init':return {'snapshot':NativeTeam.create(root,value).snapshot()}
    team=NativeTeam(root)
    if operation=='show':return {'snapshot':team.snapshot()}
    if operation=='admit':return {'ticket':team.admit(**value)}
    if operation=='bind':team.bind(**value);return {'bound':True}
    if operation=='complete':return {'receipt':team.complete(**value)}
    if operation=='expire':return {'expired':team.expire()}
    if operation=='unknown':team.unknown(**value);return {'recorded_unknown':True}
    if operation=='release':team.acknowledge_release(**value);return {'release_acknowledged':True}
    if operation=='retry':team.retry(**value);return {'queued':True}
    if operation=='escalate':team.escalate(**value);return {'waiting_for_human':True}
    raise InputError('UNKNOWN_OPERATION')


def main(argv=None,stdin=None,stdout=None):
    parser=Parser(description=__doc__)
    parser.add_argument('operation',choices=['init',*OPERATIONS])
    parser.add_argument('--run',type=Path,required=True,help='Dedicated private run directory, not a daemon runtime')
    stdin=stdin if stdin is not None else sys.stdin.buffer
    stdout=stdout if stdout is not None else sys.stdout
    operation='unparsed'
    try:
        args=parser.parse_args(argv)
        operation=args.operation
        value={} if operation in ['show','expire'] and stdin.isatty() else payload(stdin,operation)
        result={'status':'OK','operation':operation,'automatic_dispatch':False,'automatic_retry':False,
                **invoke(operation,args.run,value)}
        code=0
    except (ValueError,KeyError,TypeError,AttributeError,OSError,RuntimeError,sqlite3.Error,subprocess.SubprocessError) as exc:
        # Unknown external/file/JSON diagnostics can contain request bodies or paths.
        # Return only our fixed input codes; core rejection details remain local to API callers.
        error=str(exc) if isinstance(exc,InputError) else 'NATIVE_TEAM_OPERATION_REJECTED'
        if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',error):error='NATIVE_TEAM_OPERATION_REJECTED'
        # An operation may have been committed before an acknowledgement/fsync error.
        status='BLOCKED' if isinstance(exc,InputError) or operation=='show' else 'UNKNOWN'
        result={'status':status,'operation':operation,'code':error,'automatic_dispatch':False,
                'automatic_retry':False,'reconcile_required':status=='UNKNOWN'}
        code=2
    print(json.dumps(result,ensure_ascii=False),file=stdout)
    return code

if __name__=='__main__':raise SystemExit(main())
