from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
sys.path.insert(0, str(PROJECT / 'scripts'))
from control_socket import request, endpoint
from storage import Store
from transport import proc_identity
from service_wait import daemon_ready, tunnel_ready
from systemd_fixture import ControlPlane

CLIENT = Path.home() / '.local/opt/tunnel-client/v0.0.18/tunnel-client'
SENTINEL = 'public-synthetic-e3-fixture-not-a-real-key'


def command(argv): return shlex.join([str(v) for v in argv])


class ReadinessTests(unittest.TestCase):
    def test_missing_daemon_is_bounded_failure(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / '.runtime', prefix='e3-wait-') as temp:
            start = time.monotonic()
            run = subprocess.run([sys.executable, '-B', str(PROJECT/'scripts/service_wait.py'), 'daemon',
                '--runtime', temp, '--timeout', '.2'], capture_output=True, text=True, timeout=3)
            self.assertEqual(run.returncode, 1)
            self.assertLess(time.monotonic()-start, 1.5)
            self.assertNotIn('Traceback', run.stderr)

    def test_health_rejects_nonloopback_symlink_and_insecure_file(self):
        with tempfile.TemporaryDirectory(dir=PROJECT/'.runtime', prefix='e3-health-') as temp:
            path=Path(temp)/'url';path.write_text('https://example.com');path.chmod(0o600)
            with self.assertRaises(ValueError):tunnel_ready(CLIENT,path)
            path.write_text('http://127.0.0.1:1');path.chmod(0o644)
            with self.assertRaises(ValueError):tunnel_ready(CLIENT,path)
            path.unlink();path.symlink_to(PROJECT/'README.md')
            with self.assertRaises(ValueError):tunnel_ready(CLIENT,path)

    def test_health_requires_poll_and_does_not_pass_key_environment(self):
        with tempfile.TemporaryDirectory(dir=PROJECT/'.runtime', prefix='e3-health-') as temp:
            path=Path(temp)/'url';path.write_text('http://127.0.0.1:1');path.chmod(0o600)
            value={'result':'ok','healthz':{'ok':True},'readyz':{'ok':True},'control_plane_poll':{'ok':True,'value':time.time()}}
            with patch('service_wait.subprocess.run') as run:
                run.return_value.returncode=0;run.return_value.stdout=json.dumps(value)
                self.assertTrue(tunnel_ready(CLIENT,path))
                self.assertNotIn('CONTROL_PLANE_API_KEY',run.call_args.kwargs['env'])
                self.assertIn('--require-control-plane-poll',run.call_args.args[0])
                del value['control_plane_poll'];run.return_value.stdout=json.dumps(value)
                self.assertFalse(tunnel_ready(CLIENT,path))

    def test_units_use_file_credentials_mixed_stop_and_manual_only(self):
        a=(PROJECT/'systemd/agent-orchestrator.service').read_text()
        b=(PROJECT/'systemd/aceis-mcp-tunnel.service').read_text()
        for value in [a,b]:
            self.assertNotIn('[Install]',value);self.assertIn('KillMode=mixed',value)
        self.assertIn('LoadCredential=control-plane-api-key:',b)
        self.assertIn('--control-plane.api-key=file:%d/control-plane-api-key',b)
        self.assertIn('Type=exec',b);self.assertIn('ExecStartPre=',b);self.assertIn('ExecStartPost=',b)
        self.assertIn('BindsTo=agent-orchestrator.service',b)
        self.assertIn('PartOf=agent-orchestrator.service',b)
        self.assertNotIn('--allow-writes',b)
        self.assertNotIn('api_key:',(PROJECT/'systemd/aceis-mcp-tunnel.yaml').read_text())


class SystemdFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=PROJECT/'.runtime',prefix='e3-unit-')
        self.root=Path(self.temp.name);self.units=[];self.plane=None;self.measurements={}
        self.daemon_unit='agentorchestrator-e3-'+str(uuid.uuid4())+'.service'
        self.tunnel_unit='agentorchestrator-e3-'+str(uuid.uuid4())+'.service'
        self.credential=self.root/'dummy-key';self.credential.write_text(SENTINEL);self.credential.chmod(0o600)
        self.url_file=self.root/'health-url'

    def tearDown(self):
        e=PROJECT/'.runtime/phase2-e3-evidence';e.mkdir(exist_ok=True)
        if (self.root/'tasks.sqlite').exists():
            with closing(sqlite3.connect(self.root/'tasks.sqlite')) as db:
                self.measurements['task_diagnostics']=db.execute('select id,state,error,turn_id from tasks').fetchall()
        for unit in reversed(self.units):
            subprocess.run(['/usr/bin/systemctl','--user','stop',unit],capture_output=True,timeout=30)
            journal=subprocess.run(['/usr/bin/journalctl','--user-unit',unit,'--no-pager','-o','cat'],capture_output=True,text=True,timeout=10)
            (e/(unit+'.journal.txt')).write_text(journal.stdout.replace(SENTINEL,'[SYNTHETIC FIXTURE]'))
            subprocess.run(['/usr/bin/systemctl','--user','reset-failed',unit],capture_output=True,timeout=10)
        if self.plane:self.plane.close()
        (e/(self._testMethodName+'.json')).write_text(json.dumps(self.measurements,indent=2))
        self.temp.cleanup()

    def properties(self,unit):
        data=subprocess.check_output(['/usr/bin/systemctl','--user','show',unit,
            '-p','ActiveState','-p','SubState','-p','MainPID','-p','Result','-p','ControlGroup','-p','KillMode','-p','NRestarts'],text=True)
        return dict(l.split('=',1) for l in data.splitlines() if '=' in l)

    def start(self,unit,argv,properties):
        self.assertTrue(unit.startswith('agentorchestrator-e3-'));self.units.append(unit)
        args=['/usr/bin/systemd-run','--user','--quiet','--no-ask-password','--unit',unit,
              '--service-type=exec','--working-directory='+str(PROJECT),'-pUMask=0077',
              '-pKillMode=mixed','-pTimeoutStartSec=20','-pTimeoutStopSec=20',
              '-pCPUQuotaPeriodSec=100ms','-pMemorySwapMax=0']
        for key,value in properties.items():args+=['-p'+key+'='+str(value)]
        return subprocess.run([*args,*map(str,argv)],capture_output=True,text=True,timeout=30)

    def daemon(self, fixture=False):
        argv=([sys.executable,'-B',PROJECT/'tests/daemon_fixture.py',self.root] if fixture else
              [sys.executable,'-B',PROJECT/'src/runner.py','--runtime',self.root,'--codex',sys.executable,'daemon'])
        post=[sys.executable,'-B',PROJECT/'scripts/service_wait.py','daemon','--runtime',self.root,'--unit',self.daemon_unit,'--timeout','10']
        result=self.start(self.daemon_unit,argv,{'CPUQuota':'50%','MemoryMax':'512M','TasksMax':'64',
            'ExecStartPost':command(post),'Restart':'on-failure','RestartSec':'.2','StartLimitBurst':'3','StartLimitIntervalSec':'60'})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(daemon_ready(self.root,self.daemon_unit))

    def tunnel(self,extra=None,missing_key=False):
        self.plane=ControlPlane()
        pre=[sys.executable,'-B',PROJECT/'scripts/service_wait.py','daemon','--runtime',self.root,'--unit',self.daemon_unit,'--timeout','2']
        post=[sys.executable,'-B',PROJECT/'scripts/service_wait.py','tunnel','--client',CLIENT,'--url-file',self.url_file,'--timeout','12']
        argv=[CLIENT,'run','--profile-file',PROJECT/'systemd/aceis-mcp-tunnel.yaml',
            '--control-plane.base-url='+self.plane.url,'--control-plane.tunnel-id=tunnel_22222222222222222222222222222222',
            # systemd-run escapes percent specifiers in argv; path-only env expansion is equivalent.
            '--control-plane.api-key=file:${CREDENTIALS_DIRECTORY}/test-key','--control-plane.poll-timeout=1s',
            '--control-plane.initial-poll-timeout=1s','--mcp.command=channel=main,command='+
            command([sys.executable,'-B',PROJECT/'src/mcp_adapter.py','--runtime',self.root]),
            '--health.listen-addr=127.0.0.1:0','--health.url-file='+str(self.url_file),'--log.http-raw-unsafe=false','--log.level=warn']
        props={'CPUQuota':'25%','MemoryMax':'256M','TasksMax':'64','LoadCredential':'test-key:'+str(self.credential if not missing_key else self.root/'missing-key'),
            'UnsetEnvironment':'CONTROL_PLANE_API_KEY OPENAI_API_KEY OPENAI_ADMIN_KEY',
            'ExecStartPre':command(pre),'ExecStartPost':command(post),'BindsTo':self.daemon_unit,'After':self.daemon_unit,'PartOf':self.daemon_unit}
        if extra:props.update(extra)
        return self.start(self.tunnel_unit,argv,props)

    def wait(self,condition,seconds=8):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            if condition():return
            time.sleep(.05)
        self.fail('fixture condition deadline')

    def test_daemon_start_stop_restart_and_actual_cgroups(self):
        self.daemon();first=request(self.root,'status')['pid']
        prop=self.properties(self.daemon_unit);self.assertEqual(prop['KillMode'],'mixed')
        group=Path('/sys/fs/cgroup')/prop['ControlGroup'].lstrip('/')
        self.assertEqual((group/'cpu.max').read_text().strip(),'50000 100000')
        self.assertEqual((group/'memory.max').read_text().strip(),str(512*1024**2))
        self.assertEqual((group/'pids.max').read_text().strip(),'64')
        self.measurements['daemon_cgroup']={n:(group/n).read_text().strip() for n in ['cpu.max','memory.max','pids.max','memory.swap.max']}
        subprocess.run(['/usr/bin/systemctl','--user','restart',self.daemon_unit],check=True,timeout=25)
        self.assertNotEqual(request(self.root,'status')['pid'],first)
        subprocess.run(['/usr/bin/systemctl','--user','stop',self.daemon_unit],check=True,timeout=25)
        self.wait(lambda:not endpoint(self.root).exists())
        self.assertFalse(proc_identity(first) and proc_identity(first)['state'] not in ['Z','X'])

    def test_native_tunnel_credentials_mcp_forwarding_and_no_key_in_journal(self):
        self.daemon();result=self.tunnel();self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(tunnel_ready(CLIENT,self.url_file))
        init=self.plane.call({'jsonrpc':'2.0','id':1,'method':'initialize','params':{
            'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'systemd-fixture','version':'1'}}})
        self.assertEqual(init['resp_json']['result']['protocolVersion'],'2025-11-25')
        listed=self.plane.call({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
        tools=listed['resp_json']['result']['tools']
        self.assertEqual([t['name'] for t in tools],['orchestrator_status','orchestrator_list_tasks','orchestrator_get_task'])
        status=self.plane.call({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'orchestrator_status','arguments':{}}})
        self.assertFalse(status['resp_json']['result']['isError'])
        self.assertEqual(status['resp_json']['result']['structuredContent']['data']['task_counts'],{})
        prop=self.properties(self.tunnel_unit);pid=int(prop['MainPID'])
        env=Path('/proc/'+str(pid)+'/environ').read_bytes()
        self.assertNotIn(SENTINEL.encode(),env)
        self.assertNotIn(b'CONTROL_PLANE_API_KEY=',env)
        self.assertNotIn(SENTINEL.encode(),Path('/proc/'+str(pid)+'/cmdline').read_bytes())
        self.measurements['mcp']={'revision':init['resp_json']['result']['protocolVersion'],'tools':[t['name'] for t in tools],'status':status['resp_json']['result']['structuredContent']}
        self.measurements['polls']=self.plane.polls
        group=Path('/sys/fs/cgroup')/prop['ControlGroup'].lstrip('/')
        self.assertEqual((group/'cpu.max').read_text().strip(),'25000 100000')
        self.assertEqual((group/'memory.max').read_text().strip(),str(256*1024**2))
        self.measurements['tunnel_cgroup']={n:(group/n).read_text().strip() for n in ['cpu.max','memory.max','pids.max','memory.swap.max']}
        child_pids=[int(line.split()[0]) for line in subprocess.check_output(['ps','--ppid',str(pid),'-o','pid='],text=True).splitlines()]
        self.measurements['native_mcp_child_pids']=child_pids
        journal=subprocess.check_output(['/usr/bin/journalctl','--user-unit',self.tunnel_unit,'--no-pager','-o','cat'],text=True)
        self.assertNotIn(SENTINEL,journal)
        subprocess.run(['/usr/bin/systemctl','--user','stop',self.tunnel_unit],check=True,timeout=25)
        self.wait(lambda:not self.url_file.exists())
        self.assertEqual(self.properties(self.daemon_unit)['ActiveState'],'active')
        for child in [pid,*child_pids]:
            self.wait(lambda child=child:not proc_identity(child) or proc_identity(child)['state'] in ['Z','X'])
        self.measurements['released']=True

    def test_missing_credential_refuses_tunnel(self):
        self.daemon();result=self.tunnel(missing_key=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(self.url_file.exists())

    def test_daemon_stop_stops_bound_tunnel(self):
        self.daemon();result=self.tunnel();self.assertEqual(result.returncode,0,result.stderr)
        subprocess.run(['/usr/bin/systemctl','--user','stop',self.daemon_unit],check=True,timeout=25)
        self.wait(lambda:self.properties(self.tunnel_unit)['ActiveState']=='inactive')
        self.assertFalse(self.url_file.exists())

    def test_daemon_restart_propagates_tunnel_restart(self):
        self.daemon();result=self.tunnel();self.assertEqual(result.returncode,0,result.stderr)
        old=int(self.properties(self.tunnel_unit)['MainPID'])
        subprocess.run(['/usr/bin/systemctl','--user','restart',self.daemon_unit],check=True,timeout=30)
        self.wait(lambda:self.properties(self.tunnel_unit)['ActiveState']=='active' and int(self.properties(self.tunnel_unit)['MainPID'])!=old,15)
        self.assertTrue(tunnel_ready(CLIENT,self.url_file))

    def test_daemon_queued_unknown_survive_service_restart(self):
        self.daemon();request(self.root,'pause')
        store=Store(self.root/'tasks.sqlite')
        try:
            queued=store.submit('queued synthetic task',workspace={})
            unknown=store.submit('unknown synthetic task',workspace={})
            store.transition(unknown,'QUEUED','UNKNOWN','explicit isolated fault fixture')
        finally:store.close()
        subprocess.run(['/usr/bin/systemctl','--user','restart',self.daemon_unit],check=True,timeout=25)
        self.assertEqual(request(self.root,'status',{'task_id':queued})['state'],'QUEUED')
        self.assertEqual(request(self.root,'status',{'task_id':unknown})['state'],'UNKNOWN')
        with closing(sqlite3.connect(self.root/'tasks.sqlite')) as db:
            self.assertEqual(db.execute('select count(*) from executions').fetchone()[0],0)

    def test_tunnel_disconnect_does_not_requeue_or_create_tasks(self):
        self.daemon();result=self.tunnel();self.assertEqual(result.returncode,0,result.stderr)
        self.plane.disconnected=True;time.sleep(.4)
        self.assertEqual(request(self.root,'status')['task_counts'],{})
        result=subprocess.run(['/usr/bin/systemctl','--user','restart',self.tunnel_unit],timeout=30,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        # A down fixture cannot pass the post-start poll check. No task mutation follows.
        self.assertEqual(request(self.root,'status')['task_counts'],{})


    def task_fixture(self):
        self.daemon(fixture=True);request(self.root,'pause')
        seed=self.root/'fixtures/seed';seed.mkdir(parents=True)
        (seed/'evidence.txt').write_text('retained fixture\n')
        def submit(prompt):
            return request(self.root,'submit',{'prompt':prompt,'request_key':str(uuid.uuid4()),'timeout':60,
                'workspace':{'fixture':'seed'},'acceptance':{'artifacts':[{'path':'evidence.txt',
                'sha256':hashlib.sha256(b'retained fixture\n').hexdigest()}]}})['task_id']
        a,b,c=submit('hang'),submit('hang'),submit('normal')
        request(self.root,'resume_scheduler')
        self.wait(lambda:all(request(self.root,'status',{'task_id':t})['turn_id'] for t in [a,b]),15)
        request(self.root,'pause')
        return a,b,c

    def test_systemd_sigterm_cancels_running_tasks_and_releases_scopes(self):
        a,b,c=self.task_fixture()
        subprocess.run(['/usr/bin/systemctl','--user','stop',self.daemon_unit],check=True,timeout=30)
        store=Store(self.root/'tasks.sqlite')
        try:
            self.measurements['states']={t:store.get(t)['state'] for t in [a,b,c]}
            self.assertEqual([store.get(t)['state'] for t in [a,b,c]],['CANCELLED','CANCELLED','QUEUED'])
            cleanups=[json.loads(row[0]) for row in store.db.execute('select cleanup from scheduler_dispatches')]
            self.assertTrue(all(value['resources']['released'] for value in cleanups))
            self.measurements['resources_released']=True
        finally:store.close()
        self.assertFalse(endpoint(self.root).exists())

    def test_systemd_sigkill_restarts_and_reconciles_without_replay(self):
        a,b,c=self.task_fixture();old=request(self.root,'status')['pid']
        # Only this unique fixture unit's foreground controller is killed.
        subprocess.run(['/usr/bin/systemctl','--user','kill','--kill-whom=main','--signal=SIGKILL',self.daemon_unit],check=True,timeout=5)
        def recovered():
            try:
                status=request(self.root,'status')
                return status['pid']!=old and all(request(self.root,'status',{'task_id':t})['state']=='UNKNOWN' for t in [a,b])
            except (OSError,ValueError,RuntimeError,EOFError):return False
        self.wait(recovered,20)
        self.assertEqual(request(self.root,'status',{'task_id':c})['state'],'QUEUED')
        self.wait(lambda:not request(self.root,'status')['blocked'],20)
        with closing(sqlite3.connect(self.root/'tasks.sqlite')) as db:
            self.assertEqual(db.execute('select count(*) from executions').fetchone()[0],2)
            self.assertEqual(db.execute('pragma integrity_check').fetchone()[0],'ok')
        self.measurements.update({'states':{t:request(self.root,'status',{'task_id':t})['state'] for t in [a,b,c]},
            'execution_count':2,'integrity_check':'ok','NRestarts':self.properties(self.daemon_unit)['NRestarts']})
        self.assertGreaterEqual(int(self.properties(self.daemon_unit)['NRestarts']),1)

    def test_daemon_absent_prestart_refuses_native_tunnel(self):
        pre=[sys.executable,'-B',PROJECT/'scripts/service_wait.py','daemon','--runtime',self.root,'--timeout','.3']
        result=self.start(self.tunnel_unit,['/usr/bin/false'],{'ExecStartPre':command(pre)})
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(self.url_file.exists())

    def test_empty_credential_file_refused_by_specifier_prestart(self):
        self.credential.write_text('')
        # Tests exact %d expansion in unit property (unlike systemd-run argv).
        result=self.start(self.tunnel_unit,['/usr/bin/true'],{
            'LoadCredential':'test-key:'+str(self.credential),'ExecStartPre':'/usr/bin/test -s %d/test-key'})
        self.assertNotEqual(result.returncode,0)
