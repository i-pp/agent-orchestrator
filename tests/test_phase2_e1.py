import hashlib
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from mcp_adapter import Adapter, PROTOCOL, MAX_INPUT, AUDIT_LIMIT, task_view, decode
from control_socket import request, endpoint, validate_socket
from storage import Store


class Client:
    def __init__(self, root, writes=False, server_code=None):
        entry = ['-c', server_code] if server_code else [str(PROJECT/'src/mcp_adapter.py')]
        self.process = subprocess.Popen([sys.executable, '-B', *entry,
            '--runtime', str(root), *(['--allow-writes'] if writes else [])], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.selector = selectors.DefaultSelector();self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = bytearray()

    def send(self, value):
        self.raw(json.dumps(value).encode()+b'\n')

    def raw(self, value):
        self.process.stdin.write(value);self.process.stdin.flush()

    def receive(self, timeout=5):
        end=time.monotonic()+timeout
        while b'\n' not in self.buffer:
            if not self.selector.select(max(0,end-time.monotonic())):raise TimeoutError('fixture receive')
            part=os.read(self.process.stdout.fileno(),4096)
            if not part:raise EOFError('fixture EOF')
            self.buffer.extend(part)
        line,_,self.buffer=self.buffer.partition(b'\n')
        return json.loads(line)

    def init(self):
        self.send({'jsonrpc':'2.0','id':1,'method':'initialize','params':{
            'protocolVersion':PROTOCOL,'capabilities':{},'clientInfo':{'name':'fixture','version':'1'}}})
        reply=self.receive()
        self.send({'jsonrpc':'2.0','method':'notifications/initialized'})
        return reply

    def call(self, name, arguments=None, identifier=2):
        self.send({'jsonrpc':'2.0','id':identifier,'method':'tools/call','params':{'name':name,'arguments':arguments or {}}})
        return self.receive()

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:self.process.stdin.close()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:self.process.terminate();self.process.wait(timeout=5)
        self.selector.close();self.process.stdout.close();self.process.stderr.close()


class AdapterProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=PROJECT/'.runtime',prefix='mcp-unit-')
        self.root=Path(self.temp.name);(self.root/'logs').mkdir(mode=0o700)
        self.adapter=Adapter(self.root)
        self.clients=[]

    def tearDown(self):
        for client in self.clients:client.close()
        self.adapter.audit.close();self.temp.cleanup()

    def client(self):
        client=Client(self.root);self.clients.append(client);return client

    def init(self, adapter=None):
        a=adapter or self.adapter
        result=a.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{
            'protocolVersion':PROTOCOL,'capabilities':{},'clientInfo':{'name':'fixture','version':'1'}}})
        a.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        return result

    def call(self,name,args=None):
        return self.adapter.handle({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':name,'arguments':args or {}}})

    def test_initialization_tools_list_and_ping(self):
        self.assertEqual(self.init()['result']['protocolVersion'],PROTOCOL)
        reply=self.adapter.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})
        self.assertEqual(len(reply['result']['tools']),3)
        self.assertTrue(all(t['annotations']['readOnlyHint'] for t in reply['result']['tools']))
        self.assertTrue(all(t['inputSchema']['additionalProperties'] is False for t in reply['result']['tools']))
        self.assertEqual(self.adapter.handle({'jsonrpc':'2.0','id':4,'method':'ping'})['result'],{})

    def test_uninitialized_reinitialize_and_unknown_method(self):
        self.assertIn('error',self.call('orchestrator_status'))
        self.init();self.assertIn('error',self.init())
        self.assertEqual(self.adapter.handle({'jsonrpc':'2.0','id':4,'method':'shutdown'})['error']['code'],-32601)
        self.assertIsNone(self.adapter.handle({'jsonrpc':'2.0','method':'unsupported-notification'}))

    def test_read_only_rejects_write_even_when_called_directly(self):
        self.init()
        for name in ['orchestrator_submit_task','orchestrator_cancel_task','pause','shutdown']:
            with patch('mcp_adapter.request') as send:
                self.assertIn('error',self.call(name));send.assert_not_called()

    def test_schema_types_and_arbitrary_paths_are_rejected(self):
        self.init()
        cases=[('orchestrator_status',{'runtime':'/tmp/other'}),('orchestrator_list_tasks',{'limit':True}),
               ('orchestrator_list_tasks',{'limit':51}),('orchestrator_list_tasks',{'offset':-1}),
               ('orchestrator_get_task',{'task_id':'../../.codex/config.toml'}),
               ('orchestrator_get_task',{'task_id':str(uuid.uuid4()),'socket_path':'/tmp/x'})]
        with patch('mcp_adapter.request') as send:
            for name,args in cases:self.assertIn('error',self.call(name,args))
            send.assert_not_called()

    def test_projection_omits_text_credentials_config_paths_and_owner(self):
        task={'id':str(uuid.uuid4()),'state':'UNKNOWN','result':'private-result','error':'private-error',
              'prompt':'private-prompt','config':{'secret':'private-config'},'owner':'private-owner',
              'log_path':'private-path','human_status':'private-value','active_execution_id':str(uuid.uuid4())}
        view=task_view(task)
        self.assertNotIn('private-',json.dumps(view));self.assertIsNone(view['human_status'])
        self.init()
        with patch('mcp_adapter.request',return_value=task):
            reply=self.call('orchestrator_get_task',{'task_id':task['id']})
        self.assertFalse(reply['result']['isError'])
        logs=self.adapter.audit_path.read_text()
        self.assertNotIn('private-',logs);self.assertIn(task['active_execution_id'],logs)

    def test_daemon_error_text_is_not_exported(self):
        self.init()
        with patch('mcp_adapter.request',side_effect=RuntimeError('private-config-or-token')):
            result=self.call('orchestrator_status')
        self.assertTrue(result['result']['isError'])
        self.assertNotIn('private-',json.dumps(result)+self.adapter.audit_path.read_text())

    def test_audit_failure_prevents_daemon_operation(self):
        self.init()
        with patch.object(self.adapter,'record',side_effect=OSError('full')),patch('mcp_adapter.request') as send:
            self.assertIn('error',self.call('orchestrator_status'));send.assert_not_called()
        self.adapter.audit.seek(AUDIT_LIMIT-100)
        with patch('mcp_adapter.request') as send:
            self.assertIn('error',self.call('orchestrator_status'));send.assert_not_called()

    def test_bad_json_duplicate_keys_and_nonfinite_then_valid_request(self):
        c=self.client()
        for raw in [b'{no}\n',b'{"jsonrpc":"2.0","jsonrpc":"2.0"}\n',b'{"x":NaN}\n',
                    b'{"x":1e400}\n',b'{"x":"\\ud800"}\n','{}'.encode('utf-16')+b'\n']:
            c.raw(raw);self.assertEqual(c.receive()['error']['code'],-32700)
        self.assertEqual(c.init()['result']['protocolVersion'],PROTOCOL)
        c.send({'jsonrpc':'2.0','id':10,'method':'ping'});self.assertEqual(c.receive()['result'],{})
        c.raw(b'[]\n');self.assertEqual(c.receive()['error']['code'],-32600)
        c.send({'jsonrpc':'2.0','id':True,'method':'ping'});self.assertEqual(c.receive()['error']['code'],-32600)

    def test_oversized_frame_exits_bounded(self):
        c=self.client();c.raw(b'x'*(MAX_INPUT+1)+b'\n')
        self.assertEqual(c.receive()['error']['code'],-32700);c.process.wait(timeout=3)
        self.assertNotEqual(c.process.returncode,0)

    def test_partial_eof_and_partial_deadline(self):
        c=self.client();c.raw(b'{');c.process.stdin.close()
        self.assertEqual(c.receive()['error']['code'],-32700);c.process.wait(timeout=3)
        c=self.client();c.raw(b'{')
        self.assertEqual(c.receive(timeout=5)['error']['code'],-32700);c.process.wait(timeout=3)

    def test_stdout_disconnect_releases_adapter(self):
        c=self.client();c.process.stdout.close();c.send({'jsonrpc':'2.0','id':1,'method':'ping'})
        c.process.wait(timeout=3);self.assertNotEqual(c.process.returncode,0)

    def test_runtime_policy_and_symlink_boundaries(self):
        with self.assertRaises(ValueError):Adapter('/tmp')
        link=self.root/'link';link.symlink_to(self.root)
        with self.assertRaises(ValueError):Adapter(link)
        policy=self.root/'mcp-task-policy.json';policy.symlink_to(PROJECT/'README.md')
        with self.assertRaises(ValueError):Adapter(self.root,True)
        policy.unlink();policy.write_text('{}');policy.chmod(0o600)
        with self.assertRaises(ValueError):Adapter(self.root,True)

    def test_output_bound_and_unexpected_daemon_task(self):
        self.init()
        with patch('mcp_adapter.request',return_value={'id':'not-a-uuid','state':'COMPLETED'}):
            self.assertTrue(self.call('orchestrator_get_task',{'task_id':str(uuid.uuid4())})['result']['isError'])
        with patch('mcp_adapter.request',return_value=[{'id':str(uuid.uuid4()),'state':'QUEUED'}]*51):
            self.assertTrue(self.call('orchestrator_list_tasks',{'limit':50})['result']['isError'])

    def test_stdio_response_size_is_bounded(self):
        code = ('import sys;sys.path.insert(0,'+repr(str(PROJECT/'src'))+');'
                'import mcp_adapter as m;m.MAX_OUTPUT=1024;raise SystemExit(m.main())')
        c=Client(self.root,server_code=code);self.clients.append(c);c.init()
        c.send({'jsonrpc':'2.0','id':2,'method':'tools/list'})
        reply=c.receive();self.assertEqual(reply['error']['code'],-32603)
        self.assertLess(len(json.dumps(reply).encode()),1024)

    def test_adapter_daemon_timeout_returns_tool_error(self):
        path=endpoint(self.root);server=socket.socket(socket.AF_UNIX);server.bind(str(path));path.chmod(0o600);server.listen()
        def silent():
            conn,_=server.accept()
            with conn:
                conn.recv(4096);time.sleep(2.3)
        thread=threading.Thread(target=silent);thread.start()
        try:
            c=self.client();c.init();start=time.monotonic()
            reply=c.call('orchestrator_status')
            self.assertTrue(reply['result']['isError']);self.assertLess(time.monotonic()-start,3)
        finally:thread.join(timeout=4);server.close();path.unlink()

    def test_absolute_socket_timeout_survives_trickled_response(self):
        path=endpoint(self.root);server=socket.socket(socket.AF_UNIX);server.bind(str(path));path.chmod(0o600);server.listen()
        def trickle():
            conn,_=server.accept()
            with conn:
                conn.recv(4096)
                for _ in range(20):
                    try:conn.send(b' ')
                    except OSError:break
                    time.sleep(.05)
        thread=threading.Thread(target=trickle);thread.start();start=time.monotonic()
        try:
            with self.assertRaises(TimeoutError):request(self.root,'status',timeout=.2)
            self.assertLess(time.monotonic()-start,.6)
        finally:thread.join(timeout=3);server.close();path.unlink()


class AdapterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=PROJECT/'.runtime',prefix='mcp-integration-')
        self.root=Path(self.temp.name);self.clients=[];self.output=tempfile.TemporaryFile()
        self.process=subprocess.Popen([sys.executable,'-B',str(PROJECT/'tests/daemon_fixture.py'),str(self.root)],stdout=self.output,stderr=self.output)
        self.wait(lambda:self.call('status'),ready=True);self.call('pause')
        seed=self.root/'fixtures/seed';seed.mkdir(parents=True)
        for name in ['evidence.txt','expected.txt','result.txt']:(seed/name).write_bytes(b'MCP fixture\n')
        policy={'workspace':{'fixture':'seed','access':'read-only'},'acceptance':{
            'artifacts':[{'path':'evidence.txt','sha256':hashlib.sha256(b'MCP fixture\n').hexdigest()}],
            'tests':['workspace-result-check']},'timeout':15}
        path=self.root/'mcp-task-policy.json';path.write_text(json.dumps(policy));path.chmod(0o600)

    def call(self,method,**params):return request(self.root,method,params)

    def wait(self,condition,ready=False):
        end=time.monotonic()+12
        while time.monotonic()<end:
            try:
                value=condition()
                if value:return value
            except (OSError,ValueError):
                if not ready:raise
            time.sleep(.03)
        self.fail('fixture deadline exceeded')

    def client(self,writes=False):
        c=Client(self.root,writes);self.clients.append(c);c.init();return c

    def tearDown(self):
        for c in self.clients:c.close()
        try:self.call('shutdown')
        except (OSError,ValueError,RuntimeError,EOFError):pass
        try:self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:self.process.terminate();self.process.wait(timeout=45)
        self.output.close()
        path=endpoint(self.root)
        if path.exists():validate_socket(path);path.unlink()
        self.temp.cleanup()

    def test_local_stdio_submit_duplicate_execute_verify_and_correlate(self):
        c=self.client(True);args={'prompt':'normal','request_key':'mcp-same-key'}
        a=c.call('orchestrator_submit_task',args)['result'];self.assertFalse(a['isError']);tid=a['structuredContent']['data']['task_id']
        again=c.call('orchestrator_submit_task',args)['result'];self.assertEqual(again['structuredContent']['data']['task_id'],tid)
        self.assertEqual(len(self.call('list')),1)
        self.call('resume_scheduler');self.wait(lambda:self.call('status',task_id=tid)['completion_verified'])
        view=c.call('orchestrator_get_task',{'task_id':tid})['result']['structuredContent']['data']
        self.assertTrue(view['completion_verified']);self.assertEqual(view['state'],'COMPLETED')
        self.assertIsNotNone(view['active_execution_id'])
        records=[json.loads(line) for p in (self.root/'logs').glob('mcp-*.jsonl') for line in p.read_text().splitlines()]
        self.assertTrue(any(r.get('execution_id')==view['active_execution_id'] for r in records))
        self.assertTrue(any(r.get('task_id')==tid for r in records))
        self.assertFalse(any('prompt' in r or 'request_key' in r for r in records))
        c.send({'jsonrpc':'2.0','id':9,'method':'tools/list'});self.assertEqual(len(c.receive()['result']['tools']),5)

    def test_queue_cancel_and_client_cannot_change_policy(self):
        c=self.client(True)
        for extra in [{'workspace':{'access':'danger-full-access'}},{'acceptance':{}},{'resume_of':str(uuid.uuid4())},{'timeout':1}]:
            self.assertIn('error',c.call('orchestrator_submit_task',{'prompt':'normal','request_key':'bad',**extra}))
        response=c.call('orchestrator_submit_task',{'prompt':'normal','request_key':'cancel'})
        tid=response['result']['structuredContent']['data']['task_id']
        view=c.call('orchestrator_cancel_task',{'task_id':tid})['result']['structuredContent']['data']
        self.assertEqual(view['state'],'CANCELLED')
        bad=c.call('orchestrator_submit_task',{'prompt':'different','request_key':'cancel'})
        self.assertTrue(bad['result']['isError']);self.assertEqual(len(self.call('list')),1)

    def test_read_only_metadata_and_daemon_stopped_failure(self):
        c=self.client();self.assertFalse(c.call('orchestrator_status')['result']['isError'])
        self.assertEqual(c.call('orchestrator_list_tasks')['result']['structuredContent']['data']['tasks'],[])
        self.call('shutdown');self.process.wait(timeout=15)
        self.assertTrue(c.call('orchestrator_status')['result']['isError'])

    def test_unknown_is_visible_and_not_reexecuted(self):
        store=Store(self.root/'tasks.sqlite')
        try:
            tid=store.submit('unresolved fixture')
            store.transition(tid,'QUEUED','UNKNOWN','isolated injected uncertainty')
        finally:store.close()
        c=self.client(True)
        self.assertEqual(c.call('orchestrator_get_task',{'task_id':tid})['result']['structuredContent']['data']['state'],'UNKNOWN')
        queued=c.call('orchestrator_submit_task',{'prompt':'normal','request_key':'after-unknown'})['result']['structuredContent']['data']['task_id']
        self.call('resume_scheduler');time.sleep(.4)
        self.assertEqual(self.call('status',task_id=queued)['state'],'QUEUED')
        self.assertEqual(self.call('status',task_id=tid)['state'],'UNKNOWN')
        self.assertGreater(c.call('orchestrator_status')['result']['structuredContent']['data']['blocked_count'],0)

    def test_lost_submit_reply_reuses_key_after_adapter_disconnect(self):
        c=self.client(True);args={'prompt':'normal','request_key':'lost-ack'}
        c.send({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'orchestrator_submit_task','arguments':args}})
        c.process.stdout.close();c.process.wait(timeout=5)
        again=self.client(True).call('orchestrator_submit_task',args)['result']
        self.assertFalse(again['isError']);self.assertEqual(len(self.call('list')),1)
