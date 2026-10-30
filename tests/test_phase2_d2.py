import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from control_socket import request, endpoint, validate_socket, peer_uid
from runner import Runner
from storage import Store, rollback_database
from transport import proc_identity
import scheduler
from resources import Control, policy


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT / ".runtime", prefix="d2-unit-")
        self.root = Path(self.temp.name)
        self.processes = []
        self.outputs = []
        self.start()
        self.call("pause")
        seed = self.root / "fixtures/seed";seed.mkdir(parents=True)
        (seed / "evidence.txt").write_text("retained fixture\n")

    def start(self):
        output = tempfile.TemporaryFile(mode="w+")
        p = subprocess.Popen([sys.executable, "-B", str(PROJECT / "tests/daemon_fixture.py"), str(self.root)],
                             stdout=output, stderr=output)
        self.processes.append(p); self.outputs.append(output)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                state = self.call("status")
                self.daemon_pid = state["pid"]
                self.current = p
                return state
            except (OSError, ValueError):
                if p.poll() is not None:
                    output.seek(0); self.fail("Daemon failed to start: " + output.read()[-3000:])
                time.sleep(.03)
        self.fail("Daemon startup timed out")

    def tearDown(self):
        try:self.call("shutdown")
        except (OSError, RuntimeError, EOFError, ValueError):pass
        for process in self.processes:
            try:process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate();process.wait(timeout=45)
        for output in self.outputs:output.close()
        try:
            path = endpoint(self.root)
            if path.exists():
                # Only this test runtime's dead endpoint is removed.
                validate_socket(path);path.unlink()
        except OSError:pass
        self.temp.cleanup()

    def call(self, method, **params):return request(self.root, method, params)

    def submit(self, prompt="normal", key=None, timeout=15):
        return self.call("submit", prompt=prompt, request_key=key or str(time.time_ns()), timeout=timeout,
                         workspace={"fixture": "seed"}, acceptance={"artifacts": [{"path": "evidence.txt",
                         "sha256": hashlib.sha256(b'retained fixture\n').hexdigest()}]})["task_id"]

    def wait(self, condition, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = condition()
            if value:return value
            time.sleep(.04)
        self.fail("Condition was not reached")

    def state(self, task):return self.call("status", task_id=task)["state"]

    def test_socket_permissions_peer_uid_and_controls(self):
        path = endpoint(self.root)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(path));self.assertEqual(peer_uid(sock),os.getuid())
        self.assertEqual(self.call("list"), [])
        self.assertTrue(self.call("status")["paused"])
        self.assertFalse(self.call("resume_scheduler")["paused"])
        self.assertTrue(self.call("pause")["paused"])
        path.chmod(0o666)
        try:
            with self.assertRaises(ValueError):self.call("status")
        finally:path.chmod(0o600)

    def test_model_and_verifier_namespaces_cannot_reach_control_socket(self):
        from isolation import launch, isolated_launch
        from verification import verification_launch
        code="from pathlib import Path;print(Path("+repr(str(endpoint(self.root)))+").exists())"
        commands=[launch(self.root,sys.executable,['-B','-c',code]),
                  isolated_launch(self.root,sys.executable,['-B','-c',code],authenticate=False),
                  verification_launch(self.root,[sys.executable,'-B','-c',code])]
        for argv,env in commands:
            result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),'False')

    def test_fifo_two_sessions_and_no_duplicate_dispatch(self):
        ids = [self.submit() for _ in range(4)]
        with closing(sqlite3.connect(self.root/'tasks.sqlite')) as db, db:
            db.execute('UPDATE tasks SET created=created-10000 WHERE id=?',(ids[-1],))
        self.call("resume_scheduler")
        self.wait(lambda: len(self.call("status")["active_tasks"]) == 2)
        self.wait(lambda: all(self.state(t) == "COMPLETED" for t in ids), 25)
        with closing(sqlite3.connect(self.root / "tasks.sqlite")) as db:
            self.assertEqual([r[0] for r in db.execute("SELECT task_id FROM scheduler_dispatches ORDER BY created")], ids)
            self.assertEqual(db.execute("SELECT count(*) FROM executions").fetchone()[0],4)
            self.assertEqual(db.execute("SELECT count(DISTINCT thread_id) FROM executions").fetchone()[0],4)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")

    def test_pause_persists_and_shutdown_retains_unselected_queue(self):
        task = self.submit()
        self.call("shutdown");self.current.wait(timeout=15)
        self.assertEqual(self.current.returncode,0)
        self.assertTrue(self.start()["paused"])
        self.assertEqual(self.state(task),"QUEUED")
        self.call("resume_scheduler");self.wait(lambda:self.state(task)=="COMPLETED")

    def test_daemon_and_worker_effective_resource_limits(self):
        relative=Path('/proc/'+str(self.daemon_pid)+'/cgroup').read_text().strip().split('::')[1]
        group=Path('/sys/fs/cgroup')/relative.lstrip('/')
        self.assertEqual((group/'cpu.max').read_text().strip(),'50000 100000')
        self.assertEqual((group/'memory.max').read_text().strip(),str(512*1024**2))
        self.assertEqual((group/'pids.max').read_text().strip(),'64')
        task=self.submit();self.call('resume_scheduler')
        self.wait(lambda:self.state(task)=='COMPLETED')
        self.wait(lambda:not self.call('status')['active_tasks'])
        with closing(sqlite3.connect(self.root/'tasks.sqlite')) as db:
            data=json.loads(db.execute('SELECT cleanup FROM scheduler_dispatches WHERE task_id=?',(task,)).fetchone()[0])['resources']
        self.assertTrue(data['confirmed'] and data['released'])
        self.assertEqual(data['receipt_data']['effective']['memory.max'],str(256*1024**2))
        self.assertEqual(data['receipt_data']['effective']['pids.max'],'32')

    def test_sigterm_foreground_helper_gracefully_cancels_owned_task(self):
        task=self.submit('hang');waiting=self.submit()
        self.call('resume_scheduler')
        self.wait(lambda:self.call('status',task_id=task)['turn_id'])
        self.call('pause')
        # Both may have dispatched before pause; create an untouched queued item now.
        retained=self.submit()
        self.current.terminate();self.current.wait(timeout=25)
        self.assertEqual(self.current.returncode,0)
        store=Store(self.root/'tasks.sqlite')
        try:
            self.assertEqual(store.get(task)['state'],'CANCELLED')
            self.assertEqual(store.get(retained)['state'],'QUEUED')
        finally:store.close()

    def test_individual_cancel_and_graceful_stop(self):
        a,b,c = self.submit("hang"),self.submit("hang"),self.submit()
        self.call("resume_scheduler")
        self.wait(lambda:all(self.call("status",task_id=t)["turn_id"] for t in [a,b]))
        self.call("pause");self.call("cancel",task_id=a)
        self.wait(lambda:self.state(a)=="CANCELLED")
        self.assertEqual(self.state(b),"RUNNING")
        self.call("shutdown");self.current.wait(timeout=20)
        store=Store(self.root/'tasks.sqlite')
        try:
            self.assertEqual(store.get(b)["state"],"CANCELLED")
            self.assertEqual(store.get(c)["state"],"QUEUED")
        finally:store.close()

    def test_sigkill_restart_unknown_and_queue_preservation(self):
        a,b,c = self.submit("hang"),self.submit("hang"),self.submit()
        self.call("resume_scheduler")
        self.wait(lambda:all(self.call("status",task_id=t)["turn_id"] for t in [a,b]))
        self.call("pause")
        os.kill(self.daemon_pid,signal.SIGKILL)
        self.current.wait(timeout=10)
        self.start()
        self.wait(lambda:all(self.state(t)=="UNKNOWN" for t in [a,b]))
        self.assertEqual(self.state(c),"QUEUED")
        self.wait(lambda:not self.call("status")["blocked"])
        self.call("resume_scheduler")
        self.wait(lambda:self.state(c)=="COMPLETED")
        self.assertEqual([self.state(t) for t in [a,b]],["UNKNOWN","UNKNOWN"])
        with closing(sqlite3.connect(self.root/'tasks.sqlite')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM executions').fetchone()[0],3)

    def test_worker_sigkill_and_other_session_continues(self):
        a,b = self.submit("hang"),self.submit("hang")
        self.call("resume_scheduler")
        self.wait(lambda:all(self.call("status",task_id=t)["turn_id"] for t in [a,b]))
        with closing(sqlite3.connect(self.root/'tasks.sqlite')) as db:
            worker=json.loads(db.execute('SELECT worker FROM scheduler_dispatches WHERE task_id=?',(a,)).fetchone()[0])
        os.kill(worker['process']['pid'],signal.SIGKILL)
        self.wait(lambda:self.state(a)=="UNKNOWN")
        self.assertEqual(self.state(b),"RUNNING")
        self.call("cancel",task_id=b);self.wait(lambda:self.state(b)=="CANCELLED")

    def test_request_key_and_disconnected_submit_are_idempotent(self):
        params={"prompt":"normal", "request_key":"disconnect-stable", "workspace":{"fixture":"seed"},
                "acceptance":{"artifacts":[{"path":"evidence.txt","sha256":hashlib.sha256(b'retained fixture\n').hexdigest()}]},"timeout":15}
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
            sock.connect(str(endpoint(self.root)))
            sock.sendall(json.dumps({"id":"lost-ack","method":"submit","params":params}).encode()+b'\n')
        self.wait(lambda:len(self.call('list'))==1)
        first=request(self.root,'submit',params)['task_id']
        self.assertEqual(first,request(self.root,'submit',params)['task_id'])
        changed={**params,"timeout":20}
        with self.assertRaises(RuntimeError):request(self.root,'submit',changed)
        self.assertEqual(len(self.call('list')),1)

    def test_malformed_partial_and_oversized_frames_do_not_kill_daemon(self):
        for payload in [b'{bad}\n',b'[]\n',b'{"partial":',b'x'*140000]:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
                sock.connect(str(endpoint(self.root)))
                try:sock.sendall(payload)
                except BrokenPipeError:pass
        with self.assertRaises(RuntimeError):self.call('submit',prompt=True,request_key='invalid')
        self.assertTrue(self.call('status')['paused'])
        self.assertEqual(self.call('list'),[])
        # A client-side EOF is a structured CLI error, never a blind submit retry.
        scratch=self.root/'client-eof';scratch.mkdir()
        path=endpoint(scratch,create=True)
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server:
            server.bind(str(path));path.chmod(0o600);server.listen(1)
            def disconnect():
                connection,_=server.accept()
                with connection:connection.recv(4096)
            thread=threading.Thread(target=disconnect);thread.start()
            try:
                result=subprocess.run([sys.executable,'-B',str(PROJECT/'src/runner.py'),'--runtime',str(scratch),'status'],capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,1)
                self.assertIn('error',json.loads(result.stdout))
                self.assertNotIn('Traceback',result.stderr)
            finally:thread.join(timeout=3);path.unlink()

    def test_sqlite_timeout_and_retry_does_not_duplicate(self):
        db=sqlite3.connect(self.root/'tasks.sqlite');db.execute('BEGIN IMMEDIATE')
        result=[]
        def submit():
            try:self.submit(key='db-lock')
            except RuntimeError as exc:result.append(str(exc))
        thread=threading.Thread(target=submit);thread.start();thread.join(timeout=8)
        db.rollback();db.close();thread.join(timeout=3)
        self.assertTrue(result and 'locked' in result[0])
        first=self.submit(key='db-lock');self.assertEqual(first,self.submit(key='db-lock'))
        self.assertEqual(len(self.call('list')),1)

    def test_foreground_run_and_second_daemon_are_refused(self):
        task=self.submit()
        runner=Runner(self.root,sys.executable,launcher=lambda *_:None)
        try:
            with self.assertRaises(RuntimeError):runner.run(task,1)
        finally:runner.store.close()
        output=tempfile.TemporaryFile(mode='w+')
        other=subprocess.Popen([sys.executable,'-B',str(PROJECT/'tests/daemon_fixture.py'),str(self.root)],stdout=output,stderr=output)
        self.processes.append(other);self.outputs.append(output)
        other.wait(timeout=10)
        self.assertNotEqual(other.returncode,0)
        self.assertEqual(self.call('status')['pid'],self.daemon_pid)
        local=subprocess.run([sys.executable,'-B',str(PROJECT/'src/runner.py'),'--runtime',str(self.root),
                              'submit','must not silently ignore timeout','--timeout','1'],capture_output=True,text=True,timeout=10)
        self.assertEqual(local.returncode,1)
        self.assertIn('--via-daemon',json.loads(local.stdout)['error'])
        self.assertEqual(len(self.call('list')),1)

    def test_legacy_queued_task_is_serialized_with_isolated_tasks(self):
        store=Store(self.root/'tasks.sqlite')
        try:legacy=store.submit('legacy inspection')
        finally:store.close()
        other=self.submit()
        self.call('resume_scheduler')
        self.wait(lambda:self.state(legacy)=='WAITING_FOR_HUMAN')
        self.wait(lambda:self.state(other)=='COMPLETED')
        with closing(sqlite3.connect(self.root/'tasks.sqlite')) as db:
            a=db.execute('SELECT started,finished FROM executions WHERE task_id=?',(legacy,)).fetchone()
            b=db.execute('SELECT started,finished FROM executions WHERE task_id=?',(other,)).fetchone()
        self.assertLessEqual(a[1],b[0])

    def test_capacity_stops_new_dispatch_without_pruning(self):
        task=self.submit();path=self.root/'oversized-evidence'
        with path.open('wb') as stream:stream.truncate(3*1024**3)
        try:
            self.call('resume_scheduler')
            self.wait(lambda:'storage_budget' in self.call('status')['blocked'])
            self.assertEqual(self.state(task),'QUEUED')
            self.assertTrue(path.exists())
            self.call('pause')
        finally:
            # This newly-created sparse fault fixture is reset, not retained task evidence.
            path.write_bytes(b'fixture-reset')

    def test_unreleased_unknown_blocks_dispatch(self):
        unknown=self.submit();waiting=self.submit()
        store=Store(self.root/'tasks.sqlite')
        try:
            store.transition(unknown,'QUEUED','RUNNING','fixture untracked owner')
            store.recover(unknown)
        finally:store.close()
        self.call('resume_scheduler')
        self.wait(lambda:'unreleased_unknown' in self.call('status')['blocked'])
        self.assertEqual(self.state(waiting),'QUEUED')

    def test_repeated_queue_cycles_and_socket_churn(self):
        started=time.monotonic()
        before_fds=len(list(Path('/proc/'+str(self.daemon_pid)+'/fd').iterdir()))
        ids=[self.submit() for _ in range(24)]
        self.call('resume_scheduler')
        self.wait(lambda:all(t['state']=='COMPLETED' for t in self.call('list')),75)
        for _ in range(40):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
                sock.connect(str(endpoint(self.root)))
        self.assertEqual(len(self.call('list')),24)
        self.assertEqual(len(set(ids)),24)
        self.wait(lambda:not self.call('status')['active_tasks'])
        while time.monotonic()-started < 60:
            self.assertEqual(self.call('status')['task_counts'].get('COMPLETED'),24)
            time.sleep(.2)
        after_fds=len(list(Path('/proc/'+str(self.daemon_pid)+'/fd').iterdir()))
        self.assertLessEqual(after_fds,before_fds+8)


class SchedulerPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=PROJECT/'.runtime',prefix='d2-journal-')
        self.root=Path(self.temp.name)
        self.runner=Runner(self.root,sys.executable,launcher=lambda *_:None)

    def tearDown(self):self.runner.store.close();self.temp.cleanup()

    def test_additive_schema_preserves_core_and_refuses_destructive_rollback(self):
        task=self.runner.store.submit('preserved')
        before=self.runner.store.get(task)
        scheduler.initialize(self.runner.store)
        self.assertEqual(self.runner.store.get(task),before)
        self.assertEqual(self.runner.store.db.execute('PRAGMA user_version').fetchone()[0],3)
        self.assertTrue(list((self.root/'db-backups').glob('before-scheduler-v1-*.sqlite')))
        old=next((self.root/'db-backups').glob('phase2-ab-*.sqlite'))
        with self.assertRaisesRegex(ValueError,'Scheduler metadata'):rollback_database(self.root/'tasks.sqlite',old.name)

    def test_submission_timeout_is_atomic_and_idempotent(self):
        scheduler.initialize(self.runner.store)
        store=self.runner.store
        task=store.submit('persist timeout',workspace={},request_key='atomic',schedule_timeout=10)
        with self.assertRaises(ValueError):store.submit('persist timeout',workspace={},request_key='atomic',schedule_timeout=20)
        self.assertEqual(store.db.execute('SELECT count(*) FROM tasks').fetchone()[0],1)
        self.assertEqual(store.db.execute('SELECT timeout FROM scheduler_options WHERE task_id=?',(task,)).fetchone()[0],10)

    def test_socket_resume_inherits_workspace_policy(self):
        from types import SimpleNamespace
        from daemon import Daemon
        scheduler.initialize(self.runner.store)
        store=self.runner.store
        source=store.submit('stored session metadata fixture',workspace={})
        store.update(source,session_id='stored-fixture-thread')
        store.transition(source,'QUEUED','COMPLETED','Simulate stored metadata; no execution success inferred')
        stub=SimpleNamespace(store=store,stopping=False,settings={'task_timeout':120},event=lambda *_,**__:None)
        result=Daemon.handle(stub,'submit',{'prompt':'explicit continuation','request_key':'resume-key','resume_of':source})
        self.assertEqual(store.get(result['task_id'])['workspace_id'],store.get(source)['workspace_id'])
        self.assertEqual(store.get(result['task_id'])['state'],'QUEUED')

    def test_lost_unrecorded_handoff_is_unknown_and_blocks(self):
        scheduler.initialize(self.runner.store)
        task=self.runner.store.submit('lost reservation',workspace={})
        untouched=self.runner.store.submit('not reserved',workspace={})
        descriptor=Control(self.root,policy()).descriptor
        owner=scheduler.owner_identity();owner['boot_id']='previous-boot-fixture'
        with self.runner.store.db:
            self.runner.store.db.execute("INSERT INTO scheduler_dispatches VALUES(?,?,?,'RESERVED',NULL,NULL,?,NULL,?,NULL)",
                (task,'reservation-fixture',json.dumps(owner),json.dumps(descriptor),time.time()))
        self.assertEqual(scheduler.recover_dispatches(self.runner),[task])
        self.assertEqual(self.runner.store.get(task)['state'],'UNKNOWN')
        self.assertEqual(self.runner.store.get(untouched)['state'],'QUEUED')
        self.assertEqual(scheduler.blocked_unknown(self.runner)[0],[task])

    def test_dispatch_token_is_one_use_and_requires_live_owner(self):
        scheduler.initialize(self.runner.store)
        task=self.runner.store.submit('token fixture',workspace={})
        owner=scheduler.owner_identity();descriptor=Control(self.root,policy()).descriptor
        processes={str(os.getpid()):proc_identity(os.getpid())['start']}
        with self.runner.store.db:
            self.runner.store.db.execute("INSERT INTO scheduler_dispatches VALUES(?,?,?,'STARTED',?,NULL,?,NULL,?,NULL)",
                (task,'one-use-fixture',json.dumps(owner),json.dumps(processes),json.dumps(descriptor),time.time()))
        with scheduler.execution_admission(self.root,self.runner.store,task,'one-use-fixture'):pass
        with self.assertRaises(ValueError):scheduler.execution_admission(self.root,self.runner.store,task,'one-use-fixture')
        self.assertEqual(self.runner.store.get(task)['state'],'QUEUED')


if __name__=='__main__':unittest.main()
