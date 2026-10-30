"""Opt-in: one real Codex turn in a disposable systemd user service, never installed."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'src'))
from runner import Runner
from control_socket import request
from transport import redact

CODE = b'def add(a, b):\n    return a + b\n'
TEST = b'''import unittest
from calculator import add

class CalculatorTests(unittest.TestCase):
    def test_positive(self): self.assertEqual(add(2, 3), 5)
    def test_negative(self): self.assertEqual(add(-2, -3), -5)
    def test_zero(self): self.assertEqual(add(0, 0), 0)
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    if not args.execute: parser.error('--execute is required: one subscription model turn')
    root=PROJECT/'.runtime'/('development-poc-e32-'+uuid.uuid4().hex[:8])
    evidence=PROJECT/'.runtime/phase2-e3-2-evidence';evidence.mkdir(exist_ok=True)
    runner=Runner(root,development=True);binary=runner.binary;runner.store.close()
    unit='agentorchestrator-e32-live-'+uuid.uuid4().hex+'.service'
    facts={'runtime':str(root),'unit':unit,'binary':binary,'passed':False,'model_turn_budget':1,'tasks_submitted':0,
           'persistent_units_installed':False,'execution_mode':'development-poc-bwrap'}
    out=evidence/('live-'+root.name+'.json')
    def save():out.write_text(json.dumps(redact(facts),indent=2,ensure_ascii=False))
    def wait(condition,seconds=240):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            value=condition()
            if value:return value
            time.sleep(.15)
        raise TimeoutError('PoC condition deadline')
    def prop():
        return subprocess.check_output(['systemctl','--user','show',unit,'-p','MainPID','-p','ControlGroup','-p','ActiveState','-p','Result'],text=True)
    try:
        fixture=root/'fixtures/calculator';fixture.mkdir(parents=True,mode=0o700)
        (fixture/'test_calculator.py').write_bytes(TEST)
        post=shlex.join([sys.executable,'-B',str(PROJECT/'scripts/service_wait.py'),'daemon','--runtime',str(root),'--unit',unit,'--timeout','20'])
        cmd=['systemd-run','--user','--quiet','--no-ask-password','--unit',unit,'--service-type=exec',
             '--working-directory='+str(PROJECT),'-pUMask=0077','-pKillMode=mixed','-pTimeoutStopSec=60','-pTimeoutStartSec=30',
             '-pCPUQuota=50%','-pCPUQuotaPeriodSec=100ms','-pMemoryMax=512M','-pMemorySwapMax=0','-pTasksMax=64',
             '-pExecStartPost='+post,sys.executable,'-B',str(PROJECT/'src/runner.py'),'--runtime',str(root),
             '--codex',binary,'--development-poc','daemon']
        subprocess.run(cmd,check=True,capture_output=True,text=True,timeout=35)
        facts['initial_status']=request(root,'status');facts['unit_properties']=prop();save()
        policy={'artifacts':[{'path':'calculator.py','sha256':hashlib.sha256(CODE).hexdigest()},
                             {'path':'test_calculator.py','sha256':hashlib.sha256(TEST).hexdigest()}],
                'tests':['poc-calculator-tests']}
        prompt=('Work only in this dedicated test workspace. Create calculator.py with exactly these bytes, '
                'including a final newline:\n'+CODE.decode()+'\nDo not modify test_calculator.py. '
                'Run /usr/bin/python3 -B -m unittest -v test_calculator and confirm all three tests pass. '
                'Do not inspect authentication or configuration files. Report the actual outcome using the required schema.')
        task_id=request(root,'submit',{'prompt':prompt,'request_key':'e32-one-real-turn','timeout':240,
            'workspace':{'fixture':'calculator','access':'workspace-write'},'acceptance':policy})['task_id']
        facts['task_id']=task_id;facts['tasks_submitted']=1;save()
        def finished():
            task=request(root,'status',{'task_id':task_id})
            if task['state'] in ['FAILED','TIMED_OUT','CANCELLED','UNKNOWN','WAITING_FOR_HUMAN']:
                facts['task']=task;save();raise RuntimeError('PoC task failed: '+str(task['error']))
            return task if task['completion_verified'] else False
        task=wait(finished);wait(lambda:not request(root,'status')['active_tasks'],15)
        facts['task']=task
        with sqlite3.connect(root/'tasks.sqlite') as db:
            db.row_factory=sqlite3.Row
            facts['execution']=dict(db.execute('select * from executions where task_id=?',(task_id,)).fetchone())
            facts['verification']=dict(db.execute('select * from verifications where task_id=? order by created desc limit 1',(task_id,)).fetchone())
        first_pid=request(root,'status')['pid']
        subprocess.run(['systemctl','--user','restart',unit],check=True,timeout=65)
        after=request(root,'status',{'task_id':task_id})
        facts['restart_preserved']=(request(root,'status')['pid']!=first_pid and after['state']=='COMPLETED' and after['completion_verified'])
        with sqlite3.connect(root/'tasks.sqlite') as db:
            facts['execution_count']=db.execute('select count(*) from executions').fetchone()[0]
            facts['integrity_check']=db.execute('pragma integrity_check').fetchone()[0]
        facts['passed']=bool(facts['restart_preserved'] and facts['execution_count']==1 and facts['integrity_check']=='ok')
    except Exception as exc:
        facts['error']=redact(str(exc));raise
    finally:
        subprocess.run(['systemctl','--user','stop',unit],capture_output=True,timeout=70)
        facts['final_unit_properties']=prop()
        journal=subprocess.check_output(['journalctl','--user-unit',unit,'--no-pager','-o','cat'],text=True)
        (evidence/(unit+'.journal.txt')).write_text(redact(journal))
        subprocess.run(['systemctl','--user','reset-failed',unit],capture_output=True,timeout=10)
        save();print(json.dumps({'evidence':str(out),'passed':facts['passed'],'runtime':str(root)}))
    return 0 if facts['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
