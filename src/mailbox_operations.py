"""Operator workflow over the existing four MCP tools; no service or policy writes."""
import hashlib
import json
import os
from pathlib import Path
import re
import select
import selectors
import subprocess
import sys
import time

from chat_mailbox import Mailbox, MailboxError, TOOLS, digest, validate_arguments, MAX_SLOTS, ID_PATTERN, HASH_PATTERN
from mcp_adapter import REQUEST_PROTOCOL, VERSION_META, CAPABILITIES_META, MAX_OUTPUT, decode

PROJECT=Path(__file__).resolve().parents[1]


class OperationError(RuntimeError):
    def __init__(self, code, status='UNKNOWN'):
        self.code,self.status=code,status
        super().__init__(code)


def prepare_request(draft, allowed):
    """Produce a reviewable candidate; never install it or expand live scope."""
    if not isinstance(draft,dict) or set(draft)!={'task_id','request_id','request'}:
        raise OperationError('INVALID_DRAFT','BLOCKED')
    try:bundle={**draft,'request_sha256':digest(draft['request'])} if isinstance(draft['request'],str) else {}
    except UnicodeError:raise OperationError('INVALID_DRAFT','BLOCKED') from None
    validate_arguments(TOOLS[0],bundle)
    pair=bundle['task_id'],bundle['request_id']
    if pair in allowed and allowed[pair]!=bundle['request_sha256']:
        raise OperationError('REQUEST_CONTENT_CONFLICT','BLOCKED')
    expanded=pair not in allowed
    if expanded and len(allowed)>=MAX_SLOTS:raise OperationError('MAILBOX_SLOT_LIMIT','BLOCKED')
    candidate={**allowed,pair:bundle['request_sha256']}
    return {'status':'PENDING_SCOPE_APPROVAL' if expanded else 'ALREADY_ALLOWLISTED',
            'approval_required':expanded,'install_performed':False,'request_bundle':bundle,
            'candidate_policy':{'schema':1,'allowed':[
                {'task_id':t,'request_id':r,'request_sha256':h} for (t,r),h in sorted(candidate.items())]}}


class StdioMailbox:
    """Bounded standard MCP transport. This is explicitly local, not hosted dot access."""
    def __init__(self, runtime, timeout=5):
        if type(timeout) not in [int,float] or not .05<=timeout<=10:
            raise OperationError('INVALID_TIMEOUT','BLOCKED')
        sys.path.insert(0,str(PROJECT/'scripts'))
        from poc_services import Services
        manager=Services(runtime=runtime)
        state=manager.read_state()
        if not state or state['phase']!='READY' or state.get('chat_mailbox_enabled') is not True:
            raise OperationError('MAILBOX_NOT_READY','BLOCKED')
        self.timeout,self.counter,self.buffer=timeout,0,bytearray()
        argv=[sys.executable,'-B',str(PROJECT/'src/mcp_adapter.py'),'--runtime',str(manager.root)]
        for name,flag in [('local_llm_enabled','--allow-local-llm'),
                          ('local_models_enabled','--allow-local-models'),
                          ('chat_mailbox_enabled','--allow-chat-mailbox')]:
            if type(state.get(name)) is not bool:raise OperationError('INVALID_INVOCATION_STATE','BLOCKED')
            if state[name]:argv.append(flag)
        grants=state.get('mailbox_grants_enabled',False)
        if type(grants) is not bool:raise OperationError('INVALID_INVOCATION_STATE','BLOCKED')
        if grants:argv.append('--allow-mailbox-grants')
        self.process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            env={'PATH':'/usr/bin:/bin','HOME':str(Path.home()),'LANG':'C.UTF-8'})
        os.set_blocking(self.process.stdin.fileno(),False)
        os.set_blocking(self.process.stdout.fileno(),False)
        self.selector=selectors.DefaultSelector();self.selector.register(self.process.stdout,selectors.EVENT_READ)

    def exchange(self,name,args):
        if name not in TOOLS:raise OperationError('UNKNOWN_MAILBOX_OPERATION','BLOCKED')
        validate_arguments(name,args)
        self.counter+=1
        message={'jsonrpc':'2.0','id':self.counter,'method':'tools/call','params':{
            '_meta':{VERSION_META:REQUEST_PROTOCOL,CAPABILITIES_META:{}},'name':name,'arguments':args}}
        raw=(json.dumps(message)+'\n').encode()
        deadline=time.monotonic()+self.timeout
        def remaining():
            value=deadline-time.monotonic()
            if value<=0:raise OperationError('MCP_TIMEOUT')
            return value
        try:
            sent=0
            while sent<len(raw):
                if not select.select([],[self.process.stdin],[],remaining())[1]:raise OperationError('MCP_TIMEOUT')
                try:sent+=os.write(self.process.stdin.fileno(),raw[sent:sent+4096])
                except BlockingIOError:continue
            while b'\n' not in self.buffer:
                if not self.selector.select(remaining()):raise OperationError('MCP_TIMEOUT')
                try:part=os.read(self.process.stdout.fileno(),4096)
                except BlockingIOError:continue
                if not part:raise OperationError('MCP_CONNECTION_CLOSED')
                self.buffer.extend(part)
                if len(self.buffer)>MAX_OUTPUT:raise OperationError('MCP_RESPONSE_BOUND')
            line,_,self.buffer=self.buffer.partition(b'\n')
            response=decode(line)
            if not isinstance(response,dict) or response.get('jsonrpc')!='2.0':
                raise OperationError('MCP_INVALID_ENVELOPE')
            if type(response.get('id')) is not int or response['id']!=self.counter:raise OperationError('MCP_ID_MISMATCH')
            if ('result' in response)==('error' in response):raise OperationError('MCP_INVALID_ENVELOPE')
            if 'error' in response:
                if not isinstance(response['error'],dict):raise OperationError('MCP_INVALID_ENVELOPE')
                code=response['error'].get('code')
                raise OperationError('MCP_INVALID_REQUEST' if code==-32602 else 'MCP_REMOTE_ERROR',
                                     'BLOCKED' if code==-32602 else 'UNKNOWN')
            result=response['result']
            if not isinstance(result,dict) or type(result.get('isError')) is not bool:
                raise OperationError('MCP_INVALID_ENVELOPE')
            structured=result.get('structuredContent')
            if (not isinstance(structured,dict) or set(structured)!={'call_id','data'}
                    or not isinstance(structured['data'],dict)
                    or not isinstance(structured['call_id'],str) or not re.fullmatch(ID_PATTERN,structured['call_id'])):
                raise OperationError('MCP_INVALID_ENVELOPE')
            data=result['structuredContent']['data']
            if result.get('isError'):
                code=data.get('code','MCP_REMOTE_ERROR')
                if not isinstance(code,str) or not re.fullmatch(r'[A-Z0-9_]{1,64}',code):code='MCP_REMOTE_ERROR'
                raise OperationError(code,'BLOCKED' if data.get('status')=='BLOCKED' else 'UNKNOWN')
            return {'call_id':result['structuredContent']['call_id'],'data':data}
        except OperationError:raise
        except (OSError,ValueError,KeyError,TypeError,AttributeError):
            raise OperationError('MCP_TRANSPORT_OR_RESPONSE_ERROR') from None

    def close(self):
        try:self.process.stdin.close()
        except OSError:pass
        try:self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=1)
        self.selector.close();self.process.stdout.close()


class MailboxWorkflow:
    def __init__(self,factory):
        self.factory,self.connection=factory,None
        self.calls=[];self.write_attempts=0;self.reconnections=0
        self.policy_identity=None;self.last_grant_state=None;self.last_status=None
        self.last_persisted=False;self.last_answer_hash=None

    def close(self):
        if self.connection:self.connection.close();self.connection=None

    def _call(self,name,args):
        start=time.monotonic();item={'tool':name}
        self.calls.append(item)
        try:
            if self.connection is None:self.connection=self.factory()
            envelope=self.connection.exchange(name,args)
            if (not isinstance(envelope,dict) or set(envelope)!={'call_id','data'}
                    or not isinstance(envelope.get('call_id'),str)
                    or not re.fullmatch(ID_PATTERN,envelope['call_id']) or not isinstance(envelope.get('data'),dict)):
                raise OperationError('MCP_INVALID_ENVELOPE')
            item['call_id']=envelope['call_id'];data=envelope['data']
            for key in ['task_id','request_id','request_sha256']:
                if data.get(key)!=args[key]:raise OperationError('MCP_SLOT_MISMATCH')
            if data.get('status') not in ['NOT_FOUND','PENDING','ANSWERED','EXPIRED','REVOKED']:
                raise OperationError('MCP_INVALID_STATE')
            if (data.get('identity_verified') is not False or data.get('automatic_retry') is not False
                    or not isinstance(data.get('policy_sha256'),str)
                    or not re.fullmatch(HASH_PATTERN,data['policy_sha256'])):
                raise OperationError('MCP_INVALID_STATE')
            grant='grant_id' in data or 'grant_state' in data
            terminal=data['status'] in ['EXPIRED','REVOKED']
            if grant:
                if (not isinstance(data.get('grant_id'),str) or not re.fullmatch(ID_PATTERN,data['grant_id'])
                        or data.get('grant_state') not in ['ACTIVE','NOT_YET_ACTIVE','EXPIRED','REVOKED']
                        or type(data.get('persisted')) is not bool
                        or (data['status']=='REVOKED') != (data['grant_state']=='REVOKED')
                        or (data['status']=='NOT_FOUND' and data['persisted'])
                        or (data['status'] in ['PENDING','ANSWERED','EXPIRED'] and not data['persisted'])
                        or (data['status']=='PENDING' and data['grant_state']!='ACTIVE')
                        or (data['status'] in ['ANSWERED','EXPIRED'] and data['grant_state']=='NOT_YET_ACTIVE')):
                    raise OperationError('MCP_INVALID_STATE')
                if data['persisted']:
                    if (type(data.get('request_expires_at')) is not int or data['request_expires_at']<=0
                            or type(data.get('answer_saved')) is not bool
                            or ('answer_sha256' in data) is not data['answer_saved']
                            or (data['status']=='ANSWERED' and not data['answer_saved'])
                            or (data['status']=='PENDING' and data['answer_saved'])):
                        raise OperationError('MCP_INVALID_STATE')
                elif any(k in data for k in ['request_expires_at','answer_saved','answer_sha256']):
                    raise OperationError('MCP_INVALID_STATE')
            elif terminal or any(k in data for k in ['request_expires_at','answer_saved']):
                raise OperationError('MCP_INVALID_STATE')
            elif data.get('persisted') is not (data['status']!='NOT_FOUND'):
                raise OperationError('MCP_INVALID_STATE')
            if ('answer_sha256' in data and (not isinstance(data['answer_sha256'],str)
                    or not re.fullmatch(HASH_PATTERN,data['answer_sha256']))):
                raise OperationError('MCP_INVALID_CONTENT_STATE')
            if ((terminal and any(k in data for k in ['request','answer']))
                    or (data['status']!='ANSWERED' and ('answer' in data or ('answer_sha256' in data and not terminal)))
                    or (name!=TOOLS[3] and 'answer' in data)
                    or ('request' in data and (name!=TOOLS[1] or data['status']=='NOT_FOUND'))):
                raise OperationError('MCP_INVALID_CONTENT_STATE')
            if name==TOOLS[1] and data['status'] in ['PENDING','ANSWERED']:
                validate_arguments(TOOLS[0],{**args,'request':data.get('request')})
            if name==TOOLS[3] and data['status']=='ANSWERED':
                validate_arguments(TOOLS[2],{k:args[k] for k in ['task_id','request_id','request_sha256']} |
                                   {'answer':data.get('answer'),'answer_sha256':data.get('answer_sha256')})
                if args.get('expected_answer_sha256') and data['answer_sha256']!=args['expected_answer_sha256']:
                    raise OperationError('ANSWER_CONTENT_CONFLICT','BLOCKED')
            identity=(data['policy_sha256'],data.get('grant_id'))
            if self.policy_identity is not None and identity!=self.policy_identity:
                raise OperationError('MCP_POLICY_MISMATCH')
            if grant and self.last_grant_state is not None:
                order={'NOT_YET_ACTIVE':0,'ACTIVE':1,'EXPIRED':2,'REVOKED':3}
                if order[data['grant_state']]<order[self.last_grant_state]:
                    raise OperationError('MCP_STATE_INCONSISTENT')
            if self.last_status is not None:
                allowed={'NOT_FOUND':{'NOT_FOUND','PENDING','ANSWERED','EXPIRED','REVOKED'},
                         'PENDING':{'PENDING','ANSWERED','EXPIRED','REVOKED'},
                         'ANSWERED':{'ANSWERED','EXPIRED','REVOKED'},
                         'EXPIRED':{'EXPIRED','REVOKED'},'REVOKED':{'REVOKED'}}
                if data['status'] not in allowed[self.last_status]:raise OperationError('MCP_STATE_INCONSISTENT')
            if (self.last_persisted and not data['persisted']) or (self.last_answer_hash is not None
                    and data.get('answer_sha256')!=self.last_answer_hash):
                raise OperationError('MCP_STATE_INCONSISTENT')
            self.policy_identity=identity;self.last_status=data['status']
            self.last_grant_state=data.get('grant_state')
            self.last_persisted=data['persisted'];self.last_answer_hash=data.get('answer_sha256')
            item['status']=data['status'];return data
        except MailboxError as exc:
            item.update(status='UNKNOWN',code='MCP_INVALID_CONTENT')
            raise OperationError('MCP_INVALID_CONTENT') from None
        except OperationError as exc:
            item.update(status=exc.status,code=exc.code);raise
        except (KeyError,TypeError,AttributeError,OSError,ValueError,RuntimeError):
            item.update(status='UNKNOWN',code='MCP_INVALID_ENVELOPE')
            raise OperationError('MCP_INVALID_ENVELOPE') from None
        finally:item['elapsed_ms']=round((time.monotonic()-start)*1000,3)

    def _receipt(self,status,**extra):
        return {'status':status,'automatic_retry':False,'write_attempts':self.write_attempts,
                'reconnections':self.reconnections,'transport':'local MCP stdio',
                'dot_direct_call_verified':False,'calls':self.calls,**extra}

    @staticmethod
    def _metadata(data):
        return {k:data[k] for k in ['grant_id','grant_state','policy_sha256',
                'request_expires_at','answer_saved','answer_sha256'] if k in data}

    def _registered(self,data,bundle,**extra):
        key={k:bundle[k] for k in ['task_id','request_id','request_sha256']}
        if not data['persisted']:
            return self._receipt('UNKNOWN',code='WRITE_NOT_YET_RECONCILED',**key)
        if data['status'] not in ['EXPIRED','REVOKED'] and data['request']!=bundle['request']:
            raise OperationError('REQUEST_CONTENT_CONFLICT','BLOCKED')
        return self._receipt(data['status'],persisted=True,**extra,**self._metadata(data),**key)

    def register(self,bundle):
        validate_arguments(TOOLS[0],bundle)
        key={k:bundle[k] for k in ['task_id','request_id','request_sha256']}
        uncertain_write=False
        try:
            existing=self._call(TOOLS[1],key)
            inactive=existing.get('grant_state') in ['NOT_YET_ACTIVE','EXPIRED','REVOKED']
            if existing['status'] in ['EXPIRED','REVOKED'] or (existing['status']=='NOT_FOUND' and inactive):
                code={'NOT_YET_ACTIVE':'GRANT_NOT_YET_ACTIVE','EXPIRED':'GRANT_EXPIRED',
                      'REVOKED':'GRANT_REVOKED'}.get(existing.get('grant_state'),'REQUEST_EXPIRED')
                return self._receipt('BLOCKED',code=code,observed_status=existing['status'],
                    persisted=existing['persisted'],**self._metadata(existing),**key)
            if existing['status']!='NOT_FOUND':
                return self._registered(existing,bundle,reused_existing=True)
            self.write_attempts=1
            try:self._call(TOOLS[0],bundle)
            except OperationError as exc:
                if exc.status=='BLOCKED':raise
                uncertain_write=True
                self.close();self.reconnections+=1
                # Reconnect for a getter only. A missing row cannot prove that a timed-out write will not commit later.
                reconciled=self._call(TOOLS[1],key)
                return self._registered(reconciled,bundle,reconciled=True)
            uncertain_write=True  # A later getter failure must not erase the known write attempt.
            confirmed=self._call(TOOLS[1],key)
            return self._registered(confirmed,bundle,reused_existing=False)
        except OperationError as exc:return self._receipt('UNKNOWN' if uncertain_write else exc.status,code=exc.code,**key)
        finally:self.close()

    def inspect(self,key):
        validate_arguments(TOOLS[3],key)
        request_key={k:key[k] for k in ['task_id','request_id','request_sha256']}
        try:
            for attempt in range(2):
                try:
                    question=self._call(TOOLS[1],request_key)
                    answer=self._call(TOOLS[3],key)
                    if question['status']=='NOT_FOUND' and answer['status']!='NOT_FOUND':
                        question=self._call(TOOLS[1],request_key)
                        if question['status'] in ['EXPIRED','REVOKED']:
                            answer=question
                    if answer['status']=='NOT_FOUND' and question['status']!='NOT_FOUND':
                        raise OperationError('MCP_STATE_INCONSISTENT')
                    if question['status']=='ANSWERED' and answer['status'] not in ['EXPIRED','REVOKED'] and (answer['status']!='ANSWERED'
                            or question.get('answer_sha256')!=answer.get('answer_sha256')):
                        raise OperationError('MCP_STATE_INCONSISTENT')
                    result=self._receipt(answer['status'],persisted=answer['persisted'],**self._metadata(answer),**request_key)
                    if answer['status'] in ['EXPIRED','REVOKED']:return result
                    if question['status']!='NOT_FOUND':result['request']=question['request']
                    if answer['status']=='ANSWERED':
                        result['answer']=answer['answer'];result['answer_sha256']=answer['answer_sha256']
                    return result
                except OperationError as exc:
                    if attempt or exc.status=='BLOCKED' or exc.code in ['MCP_POLICY_MISMATCH','MCP_STATE_INCONSISTENT']:raise
                    self.close();self.reconnections+=1  # Read-only reconnect at most once.
        except OperationError as exc:return self._receipt(exc.status,code=exc.code,**request_key)
        finally:self.close()
