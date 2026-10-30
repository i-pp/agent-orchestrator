"""Opt-in real daemon integration: three model turns, no service installation."""

import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from control_socket import request


def main():
    identifier = str(uuid.uuid4())[:8]
    root = PROJECT / ".runtime" / ("phase2-d2-live-" + identifier)
    evidence_dir = PROJECT / ".runtime/phase2-d2-evidence"
    evidence = {"runtime": str(root), "passed": False, "service_installed_or_started": False}
    children, streams = [], []
    path = evidence_dir / ("native-" + identifier + ".json")

    def save():path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    def call(method, **params):return request(root, method, params)
    def wait(condition, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = condition()
            if result:return result
            time.sleep(.1)
        raise TimeoutError("Native daemon condition not reached")
    def start():
        stream = (evidence_dir / ("native-daemon-" + identifier + "-" + str(len(children)) + ".txt")).open("w")
        streams.append(stream)
        child = subprocess.Popen([sys.executable, "-B", str(PROJECT / "src/runner.py"), "--runtime", str(root), "daemon"], stdout=stream, stderr=stream)
        children.append(child)
        def ready():
            try:return call("status")
            except (OSError, ValueError):
                if child.poll() is not None:raise RuntimeError("Daemon exited before socket readiness")
                return False
        return child,wait(ready,15)
    def submit(name, prompt):
        directory=root/'fixtures'/name;directory.mkdir(parents=True)
        data=("D2-"+name+"-"+identifier+"\n").encode()
        for n in ['expected.txt','result.txt']:(directory/n).write_bytes(data)
        return call('submit',prompt=prompt,request_key='native-'+name+'-'+identifier,timeout=180,
                    workspace={'fixture':name},acceptance={'artifacts':[{'path':'result.txt','sha256':hashlib.sha256(data).hexdigest()}],
                    'tests':['workspace-result-check']})['task_id']
    def status(t):return call('status',task_id=t)
    def command_started(t):
        item=status(t)
        if item['state'] not in ['QUEUED','RUNNING']:raise RuntimeError('Task ended before expected command: '+item['state']+' '+str(item['error']))
        if not item['log_path']:return False
        for line in Path(item['log_path']).read_text().splitlines():
            try:msg=json.loads(line)['message']
            except (ValueError,KeyError):continue
            command=msg.get('params',{}).get('item',{})
            if msg.get('method')=='item/started' and command.get('type')=='commandExecution' and 'sleep' in str(command.get('command','')):return True
        return False

    try:
        daemon,state=start();call('pause');evidence['first_daemon']=state
        a=submit('normal','Read expected.txt and result.txt. Run Python that sleeps 8 seconds, verifies exact byte equality, then prints the marker. Do not change files. Return the file marker.')
        b=submit('cancel','Read expected.txt then run a Python command that prints the marker and sleeps 90 seconds. Do not change files.')
        evidence['normal_task'],evidence['cancel_task']=a,b;call('resume_scheduler')
        wait(lambda:command_started(a) and command_started(b))
        evidence['parallel_started']=[status(a),status(b)];save()
        wait(lambda:status(a)['completion_verified'])
        call('pause');call('cancel',task_id=b);wait(lambda:status(b)['state']=='CANCELLED')
        evidence['normal_result'],evidence['cancel_result']=status(a),status(b)
        wait(lambda:not call('status')['active_tasks'])
        c=submit('crash','Read expected.txt and run Python that prints the marker and sleeps 90 seconds. Do not change files.')
        evidence['crash_task']=c;call('resume_scheduler');wait(lambda:command_started(c));call('pause')
        d=submit('queued','Read and verify expected.txt and result.txt. Do not modify files.')
        evidence['queued_task']=d;before=call('status');evidence['before_kill']=before;save()
        os.kill(before['pid'],signal.SIGKILL)  # Only our private daemon PID.
        daemon.wait(timeout=12)
        restarted,after=start()
        wait(lambda:status(c)['state']=='UNKNOWN' and not call('status')['pending_recovery'],15)
        # Ask status to tick through the durable old reservation and scope teardown.
        wait(lambda:not call('status')['blocked'],15)
        states=[status(t)['state'] for t in [a,b,c,d]]
        evidence['after_restart']=call('status');evidence['states']=states
        with closing(sqlite3.connect(root/'tasks.sqlite')) as db:
            evidence['executions']=db.execute('SELECT count(*) FROM executions').fetchone()[0]
            evidence['integrity']=db.execute('PRAGMA integrity_check').fetchone()[0]
            evidence['foreign_key_errors']=len(db.execute('PRAGMA foreign_key_check').fetchall())
            evidence['dispatch_order']=[r[0] for r in db.execute('SELECT task_id FROM scheduler_dispatches ORDER BY created')]
        time.sleep(.5)
        evidence['unknown_not_replayed']=status(c)['state']=='UNKNOWN' and status(d)['state']=='QUEUED'
        call('shutdown');restarted.wait(timeout=20)
        evidence['graceful_shutdown_exit']=restarted.returncode
        evidence['passed']=(states==['COMPLETED','CANCELLED','UNKNOWN','QUEUED'] and evidence['executions']==3
            and evidence['integrity']=='ok' and evidence['foreign_key_errors']==0 and evidence['unknown_not_replayed']
            and evidence['dispatch_order']==[a,b,c] and restarted.returncode==0
            and evidence['normal_result']['completion_verified'])
        evidence['model_turn_starts']=3
        save();print(json.dumps({'evidence':str(path),'passed':evidence['passed'],'states':states}))
        return 0 if evidence['passed'] else 1
    finally:
        try:call('shutdown')
        except (OSError,RuntimeError,ValueError,EOFError):pass
        for child in children:
            if child.poll() is None:
                child.terminate()
                try:child.wait(timeout=45)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
        for stream in streams:stream.close()
        save()


if __name__=='__main__':raise SystemExit(main())
