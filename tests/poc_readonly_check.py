"""No models: real tunnel-client + loopback control-plane fixture + read-only MCP.

This does NOT test the hosted OpenAI Tunnel. No real runtime key is needed.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'src'));sys.path.insert(0,str(PROJECT/'scripts'))
from development_poc import configure
from control_socket import request
from service_wait import tunnel_ready
from systemd_fixture import ControlPlane
from transport import Process


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime',type=Path,required=True);p.add_argument('--task-id',required=True)
    args=p.parse_args();root=args.runtime.absolute()
    if not (root/'development-poc.json').is_file():p.error('An existing PoC runtime is required')
    configure(root,True)
    with sqlite3.connect('file:'+str(root/'tasks.sqlite')+'?mode=ro',uri=True) as db:
        if db.execute("select count(*) from tasks where state in ('QUEUED','RUNNING','VERIFYING')").fetchone()[0]:
            p.error('No queued/active tasks are allowed in this read-only diagnostic')
    facts={'hosted_tunnel_verified':False,'loopback_control_plane':True,'model_turns':0,'passed':False}
    evidence=PROJECT/'.runtime/phase2-e3-2-evidence'
    output=evidence/('readonly-tunnel-'+uuid.uuid4().hex[:8]+'.json')
    unit='agentorchestrator-e32-readonly-'+uuid.uuid4().hex+'.service'
    child=None;plane=ControlPlane();started=False
    client=Path.home()/'.local/opt/tunnel-client/v0.0.18/tunnel-client'
    binary=json.loads((evidence/'baseline.json').read_text())['codex']
    def call(message):return plane.call(message)['resp_json']['result']
    try:
        try:request(root,'status');raise RuntimeError('An existing daemon owns this runtime; not replacing it')
        except (OSError,ValueError):pass
        post=shlex.join([sys.executable,'-B',str(PROJECT/'scripts/service_wait.py'),'daemon','--runtime',str(root),'--unit',unit,'--timeout','20'])
        started=True
        subprocess.run(['systemd-run','--user','--quiet','--unit',unit,'--service-type=exec',
            '-pUMask=0077','-pKillMode=mixed','-pTimeoutStopSec=60','-pExecStartPost='+post,
            sys.executable,'-B',str(PROJECT/'src/runner.py'),'--runtime',str(root),'--codex',binary,'--development-poc','daemon'],
            check=True,capture_output=True,text=True,timeout=30)
        with tempfile.TemporaryDirectory(dir=evidence,prefix='readonly-fixture-') as tmp:
            tmp=Path(tmp);key=tmp/'synthetic-key';key.write_text('public-fixture-only-not-a-real-key');key.chmod(0o600)
            url=tmp/'health-url'
            adapter=shlex.join([sys.executable,'-B',str(PROJECT/'src/mcp_adapter.py'),'--runtime',str(root)])
            argv=[str(client),'run','--profile-file',str(PROJECT/'systemd/aceis-mcp-tunnel.yaml'),
                  '--control-plane.base-url='+plane.url,'--control-plane.tunnel-id=tunnel_22222222222222222222222222222222',
                  '--control-plane.api-key=file:'+str(key),'--control-plane.poll-timeout=1s',
                  '--control-plane.initial-poll-timeout=1s','--mcp.command=channel=main,command='+adapter,
                  '--health.listen-addr=127.0.0.1:0','--health.url-file='+str(url),'--log.level=warn','--log.http-raw-unsafe=false']
            os.umask(0o077)
            child=Process(argv,cwd=PROJECT,env={'PATH':'/usr/bin:/bin','HOME':str(Path.home()),'LANG':'C.UTF-8'},raw_output=True)
            until=time.monotonic()+15
            while True:
                try:
                    if tunnel_ready(client,url):break
                except (OSError,ValueError):pass
                if time.monotonic()>until:raise TimeoutError('Loopback readiness deadline')
                time.sleep(.1)
            facts['revision']=call({'jsonrpc':'2.0','id':1,'method':'initialize','params':{
                'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'poc-readonly-check','version':'1'}}})['protocolVersion']
            facts['tools']=[t['name'] for t in call({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})['tools']]
            status=call({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'orchestrator_status','arguments':{}}})
            task=call({'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'orchestrator_get_task','arguments':{'task_id':args.task_id}}})
            local=request(root,'status')
            facts.update(status=status['structuredContent'],task=task['structuredContent'])
            facts['passed']=(facts['tools']==['orchestrator_status','orchestrator_list_tasks','orchestrator_get_task']
                and status['structuredContent']['data']['task_counts']==local['task_counts']
                and task['structuredContent']['data']['state']=='COMPLETED' and not status['isError'] and not task['isError'])
            facts['cleanup']=child.close();child=None
    finally:
        if child:facts['cleanup']=child.close()
        if started:subprocess.run(['systemctl','--user','stop',unit],capture_output=True,timeout=65)
        plane.close();output.write_text(json.dumps(facts,indent=2))
    print(json.dumps({'evidence':str(output),'passed':facts['passed'],'hosted_tunnel_verified':False}))
    return 0 if facts['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
