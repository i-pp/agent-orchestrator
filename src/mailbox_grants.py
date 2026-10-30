"""Bounded workstream grants, enabled only by explicit operator configuration.

The constructor defaults OFF. All persistence is in a separate fixed ledger.
This module cannot create tasks, read arbitrary files, execute commands or wake Chat.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from chat_mailbox import MailboxError, TOOLS, validate_arguments, digest, _private, _json

DAY=86400
MAX_DB_BYTES=1024*1024
PURPOSE='agent-orchestrator-development'


class GrantError(MailboxError):
    pass


@dataclass(frozen=True)
class GrantPolicy:
    grant_id: str
    task_id: str
    purpose: str
    not_before: int
    expires_at: int
    request_ttl_seconds: int
    max_requests: int
    max_pending: int
    max_text_bytes: int
    retain_seconds: int
    audit_retention_seconds: int

    @classmethod
    def from_dict(cls,value):
        fields=set(cls.__dataclass_fields__)
        if not isinstance(value,dict) or set(value)!=(fields|{'schema_version'}) or type(value['schema_version']) is not int or value['schema_version']!=1:
            raise GrantError('INVALID_GRANT_POLICY')
        try:
            validate_arguments(TOOLS[1],{'task_id':value['task_id'],'request_id':value['grant_id'],'request_sha256':'0'*64})
        except MailboxError:raise GrantError('INVALID_GRANT_POLICY') from None
        if value['purpose']!=PURPOSE:raise GrantError('INVALID_GRANT_PURPOSE')
        ranges={'not_before':(0,2**53-1),'expires_at':(1,2**53-1),
                'request_ttl_seconds':(1,DAY),'max_requests':(1,32),'max_pending':(1,8),
                'max_text_bytes':(1,4096),'retain_seconds':(1,7*DAY),
                'audit_retention_seconds':(1,30*DAY)}
        for key,(low,high) in ranges.items():
            if type(value[key]) is not int or not low<=value[key]<=high:raise GrantError('INVALID_GRANT_LIMIT')
        if not 0<value['expires_at']-value['not_before']<=7*DAY or value['max_pending']>value['max_requests']:
            raise GrantError('INVALID_GRANT_LIMIT')
        return cls(**{key:value[key] for key in fields})

    def metadata(self):return {'schema_version':1,**{k:getattr(self,k) for k in self.__dataclass_fields__}}
    def sha256(self):return hashlib.sha256(json.dumps(self.metadata(),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def load_runtime_grant(runtime,*,require_enabled=True):
    """Read only the operator-fixed private configuration; never initialize a ledger."""
    root=Path(runtime).absolute()
    if any(p.is_symlink() for p in [root,*root.parents]):raise GrantError('UNSAFE_GRANT_RUNTIME')
    info=root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise GrantError('UNSAFE_GRANT_RUNTIME')
    path=root/'chat-mailbox-grant.json'
    try:_private(path,16384);config=_json(path.read_bytes())
    except MailboxError:raise GrantError('INVALID_GRANT_CONFIGURATION') from None
    if (not isinstance(config,dict) or set(config)!={'schema_version','enabled','policy','policy_sha256'}
            or type(config['schema_version']) is not int or config['schema_version']!=1
            or type(config['enabled']) is not bool):raise GrantError('INVALID_GRANT_CONFIGURATION')
    policy=GrantPolicy.from_dict(config['policy'])
    if config['policy_sha256']!=policy.sha256():raise GrantError('GRANT_POLICY_HASH_MISMATCH')
    if require_enabled and config['enabled'] is not True:raise GrantError('GRANTS_DISABLED')
    return policy


class GrantLedger:
    """Atomic bounded enforcement; no policy installation or public cancellation API."""
    def __init__(self,runtime,policy,*,enabled=False,clock=time.time,create=True):
        if enabled is not True:raise GrantError('GRANTS_DISABLED')
        if not isinstance(policy,GrantPolicy):raise GrantError('INVALID_GRANT_POLICY')
        self.policy=GrantPolicy.from_dict(policy.metadata())
        self.clock=clock
        self.root=Path(runtime).absolute()
        if any(p.is_symlink() for p in [self.root,*self.root.parents]):raise GrantError('UNSAFE_GRANT_RUNTIME')
        info=self.root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
            raise GrantError('UNSAFE_GRANT_RUNTIME')
        self.path=self.root/'mailbox-grants-prepared.sqlite'
        if not create:
            _private(self.path,MAX_DB_BYTES)
            with self._db() as db:
                if db.execute('PRAGMA user_version').fetchone()[0]!=1:raise GrantError('GRANT_SCHEMA_MISMATCH')
                self._state(db)
            return
        try:fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        except FileExistsError:pass
        else:os.close(fd)
        with self._db(True) as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version==0:
                if db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]:raise GrantError('GRANT_SCHEMA_MISMATCH')
                db.execute('CREATE TABLE grants (grant_id TEXT PRIMARY KEY, policy_sha256 TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)))')
                db.execute('''CREATE TABLE messages (grant_id TEXT NOT NULL,request_id TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,request TEXT NOT NULL,created_at INTEGER NOT NULL,
                    request_expires_at INTEGER NOT NULL,answer TEXT,answer_sha256 TEXT,answered_at INTEGER,
                    PRIMARY KEY(grant_id,request_id),CHECK((answer IS NULL)=(answer_sha256 IS NULL)),
                    CHECK((answer IS NULL)=(answered_at IS NULL)))''')
                db.execute('''CREATE TABLE audit (time INTEGER NOT NULL,grant_id TEXT NOT NULL,
                    request_id TEXT,event TEXT NOT NULL,request_sha256 TEXT,answer_sha256 TEXT)''')
                db.execute('PRAGMA user_version=1')
            elif version!=1:raise GrantError('GRANT_SCHEMA_MISMATCH')
            row=db.execute('SELECT policy_sha256 FROM grants WHERE grant_id=?',(policy.grant_id,)).fetchone()
            if row and row[0]!=policy.sha256():raise GrantError('GRANT_POLICY_CONFLICT')
            db.execute('INSERT OR IGNORE INTO grants(grant_id,policy_sha256) VALUES(?,?)',(policy.grant_id,policy.sha256()))
            db.commit()

    @contextmanager
    def _db(self,write=False):
        _private(self.path,MAX_DB_BYTES)
        db=sqlite3.connect(self.path,timeout=1,isolation_level=None)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=DELETE');db.execute('PRAGMA synchronous=FULL')
            size=db.execute('PRAGMA page_size').fetchone()[0]
            db.execute('PRAGMA max_page_count='+str(MAX_DB_BYTES//size))
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
        finally:db.close()

    def _now(self):
        value=self.clock()
        if type(value) not in [int,float] or not 0<=value<=2**53-1:raise GrantError('GRANT_CLOCK_UNKNOWN','UNKNOWN')
        return int(value)

    def _state(self,db):
        row=db.execute('SELECT * FROM grants WHERE grant_id=?',(self.policy.grant_id,)).fetchone()
        if not row or row['policy_sha256']!=self.policy.sha256():raise GrantError('GRANT_INTEGRITY_UNKNOWN','UNKNOWN')
        return row

    def _active(self,state,now):
        if state['revoked']:raise GrantError('GRANT_REVOKED')
        if now<self.policy.not_before:raise GrantError('GRANT_NOT_YET_ACTIVE')
        if now>=self.policy.expires_at:raise GrantError('GRANT_EXPIRED')

    def _grant_state(self,state,now):
        if state['revoked']:return 'REVOKED'
        if now<self.policy.not_before:return 'NOT_YET_ACTIVE'
        if now>=self.policy.expires_at:return 'EXPIRED'
        return 'ACTIVE'

    def _row(self,db,args):
        row=db.execute('SELECT * FROM messages WHERE grant_id=? AND request_id=?',(self.policy.grant_id,args['request_id'])).fetchone()
        if row:
            try:
                validate_arguments(TOOLS[0],{'task_id':self.policy.task_id,'request_id':row['request_id'],
                    'request_sha256':row['request_sha256'],'request':row['request']})
                if row['answer'] is not None:
                    validate_arguments(TOOLS[2],{'task_id':self.policy.task_id,'request_id':row['request_id'],
                        'request_sha256':row['request_sha256'],'answer':row['answer'],'answer_sha256':row['answer_sha256']})
                if (type(row['created_at']) is not int or type(row['request_expires_at']) is not int
                        or not self.policy.not_before<=row['created_at']<row['request_expires_at']<=self.policy.expires_at
                        or row['request_expires_at']-row['created_at']>self.policy.request_ttl_seconds
                        or len(row['request'].encode())>self.policy.max_text_bytes):
                    raise GrantError('GRANT_INTEGRITY_UNKNOWN','UNKNOWN')
                if row['answer'] is not None and (type(row['answered_at']) is not int
                        or not row['created_at']<=row['answered_at']<row['request_expires_at']
                        or len(row['answer'].encode())>self.policy.max_text_bytes):
                    raise GrantError('GRANT_INTEGRITY_UNKNOWN','UNKNOWN')
            except MailboxError:raise GrantError('GRANT_INTEGRITY_UNKNOWN','UNKNOWN') from None
            if row['request_sha256']!=args['request_sha256']:raise GrantError('REQUEST_HASH_CONFLICT')
        return row

    def _commit(self,db):db.commit()

    def dispatch(self,name,args):
        validate_arguments(name,args)
        if args['task_id']!=self.policy.task_id:raise GrantError('GRANT_TASK_NOT_ALLOWED')
        for key in ['request','answer']:
            if key in args and len(args[key].encode())>self.policy.max_text_bytes:raise GrantError('GRANT_TEXT_LIMIT')
        write=name in (TOOLS[0],TOOLS[2])
        try:
            with self._db(write) as db:
                now=self._now()  # Re-evaluate after acquiring the write lock, never before it.
                state=self._state(db);row=self._row(db,args);duplicate=False
                if write:self._active(state,now)
                if name==TOOLS[0]:
                    if row:
                        if row['request']!=args['request']:raise GrantError('REQUEST_CONTENT_CONFLICT')
                        duplicate=True
                    else:
                        total=db.execute('SELECT count(*) FROM messages WHERE grant_id=?',(self.policy.grant_id,)).fetchone()[0]
                        pending=db.execute('SELECT count(*) FROM messages WHERE grant_id=? AND answer IS NULL AND request_expires_at>?',
                            (self.policy.grant_id,now)).fetchone()[0]
                        if total>=self.policy.max_requests:raise GrantError('GRANT_TOTAL_LIMIT')
                        if pending>=self.policy.max_pending:raise GrantError('GRANT_PENDING_LIMIT')
                        db.execute('INSERT INTO messages(grant_id,request_id,request_sha256,request,created_at,request_expires_at) VALUES(?,?,?,?,?,?)',
                            (self.policy.grant_id,args['request_id'],args['request_sha256'],args['request'],now,
                             min(now+self.policy.request_ttl_seconds,self.policy.expires_at)))
                elif name==TOOLS[2]:
                    if not row:raise GrantError('REQUEST_NOT_FOUND')
                    if now>=row['request_expires_at']:raise GrantError('REQUEST_EXPIRED')
                    if row['answer'] is not None:
                        if row['answer']!=args['answer']:raise GrantError('ANSWER_CONTENT_CONFLICT')
                        duplicate=True
                    else:
                        db.execute('UPDATE messages SET answer=?,answer_sha256=?,answered_at=? WHERE grant_id=? AND request_id=?',
                            (args['answer'],digest(args['answer']),now,self.policy.grant_id,args['request_id']))
                row=self._row(db,args)
                status='REVOKED' if state['revoked'] else 'NOT_FOUND'
                readable=False
                if row:
                    if state['revoked']:status='REVOKED'
                    elif row['answer'] is not None:
                        readable=now<row['answered_at']+self.policy.retain_seconds
                        status='ANSWERED' if readable else 'EXPIRED'
                    else:
                        readable=now<row['request_expires_at'];status='PENDING' if readable else 'EXPIRED'
                result={k:args[k] for k in ['task_id','request_id','request_sha256']}
                result.update(status=status,persisted=bool(row),grant_id=self.policy.grant_id,
                    policy_sha256=self.policy.sha256(),automatic_retry=False,identity_verified=False,
                    grant_state=self._grant_state(state,now))
                if write:result['duplicate']=duplicate
                if row:
                    result['request_expires_at']=row['request_expires_at']
                    result['answer_saved']=row['answer'] is not None
                    if row['answer_sha256'] is not None:result['answer_sha256']=row['answer_sha256']
                    if name==TOOLS[1] and readable:result['request']=row['request']
                    if name==TOOLS[3] and status=='ANSWERED':
                        if args.get('expected_answer_sha256') and args['expected_answer_sha256']!=row['answer_sha256']:
                            raise GrantError('ANSWER_CONTENT_CONFLICT')
                        result['answer']=row['answer']
                if write:
                    current=self._now();self._active(state,current)
                    if current>=row['request_expires_at']:raise GrantError('REQUEST_EXPIRED')
                    db.execute('INSERT INTO audit VALUES(?,?,?,?,?,?)',(now,self.policy.grant_id,args['request_id'],
                        'register_duplicate' if name==TOOLS[0] and duplicate else 'register' if name==TOOLS[0] else
                        'answer_duplicate' if duplicate else 'answer',args['request_sha256'],row['answer_sha256']))
                    self._commit(db)
                return result
        except MailboxError:raise
        except (OSError,sqlite3.Error):
            raise GrantError('GRANT_WRITE_UNKNOWN' if write else 'GRANT_READ_UNKNOWN','UNKNOWN') from None

    def revoke(self):
        """Prepared owner operation, not an MCP tool. Revoke is durable and irreversible here."""
        try:
            with self._db(True) as db:
                state=self._state(db)
                if not state['revoked']:
                    db.execute('UPDATE grants SET revoked=1 WHERE grant_id=?',(self.policy.grant_id,))
                    db.execute('INSERT INTO audit VALUES(?,?,?,?,?,?)',(self._now(),self.policy.grant_id,None,'revoke',None,None))
                    self._commit(db)
                return {'status':'REVOKED','grant_id':self.policy.grant_id,'automatic_retry':False}
        except MailboxError:raise
        except (OSError,sqlite3.Error):raise GrantError('GRANT_WRITE_UNKNOWN','UNKNOWN') from None

    def status(self):
        """Owner reconciliation view, including durable cancellation; never includes bodies."""
        try:
            with self._db() as db:
                state=self._state(db);now=self._now()
                total=db.execute('SELECT count(*) FROM messages WHERE grant_id=?',(self.policy.grant_id,)).fetchone()[0]
                pending=db.execute('SELECT count(*) FROM messages WHERE grant_id=? AND answer IS NULL AND request_expires_at>?',
                    (self.policy.grant_id,now)).fetchone()[0]
                return {'status':self._grant_state(state,now),'grant_id':self.policy.grant_id,
                    'task_id':self.policy.task_id,'policy_sha256':self.policy.sha256(),
                    'total_requests':total,'pending_requests':pending,'max_requests':self.policy.max_requests,
                    'max_pending':self.policy.max_pending,'not_before':self.policy.not_before,
                    'expires_at':self.policy.expires_at,'automatic_retry':False}
        except MailboxError:raise
        except (OSError,sqlite3.Error):raise GrantError('GRANT_READ_UNKNOWN','UNKNOWN') from None

    def audit_metadata(self):
        """Logical 30-day view; no physical deletion and no body or external diagnostics."""
        try:
            with self._db() as db:
                self._state(db)
                return [dict(row) for row in db.execute('SELECT * FROM audit WHERE grant_id=? AND time>=? ORDER BY rowid',
                    (self.policy.grant_id,self._now()-self.policy.audit_retention_seconds))]
        except MailboxError:raise
        except (OSError,sqlite3.Error):raise GrantError('GRANT_READ_UNKNOWN','UNKNOWN') from None
