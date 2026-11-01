"""Operator-run one-shot request/task/answer using the existing Pipeline.

No listener, service change, new auth, config overrides or automatic retry.
Inherited native MCP/plugins remain available; read-only is not a connector
write boundary. Run only after approval of this exact bounded host experiment.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT / 'src'), str(PROJECT / 'scripts')]
from codex_host_read_probe import preflight
from codex_host import HostTransport
from codex_app_protocol import ProtocolClient, schema_version_supported
from codex_pipeline import Pipeline, TERMINAL, HELD
from common_policy import load_project_policy, developer_instructions
from concurrency import lease
from poc_services import Services

MODEL = 'gpt-6.1-sol'
PROMPT = 'Reply with exactly PIPELINE_SMOKE_OK. Use only this message. Do not call tools, read files, access the network, change settings, or create subagents.'
EXPECTED = 'PIPELINE_SMOKE_OK'


class OneShotTransport(HostTransport):
    def __init__(self, binary, workspace):
        super().__init__(str(binary)); self.workspace = str(workspace)
        self.inference_attempts = 0

    def _spawn(self):
        return subprocess.Popen(list(self.COMMAND), shell=False, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, close_fds=True,
            start_new_session=True, cwd=self.workspace)

    def __call__(self, message, timeout_s):
        if message.get('method') == 'turn/start':
            if self.inference_attempts: raise ValueError('ONE_TURN_ONLY')
            self.inference_attempts += 1
        return super().__call__(message, timeout_s)


def _directory(path, private=False):
    p = Path(path)
    if (not p.is_absolute() or not p.is_dir() or p.stat().st_uid != os.getuid()
            or any(a.is_symlink() for a in (p, *p.parents))
            or (private and stat.S_IMODE(p.stat().st_mode) != 0o700)):
        raise ValueError('UNSAFE_DIRECTORY')
    return p


def _save(path, value):
    with path.open('w') as stream:
        json.dump(value, stream, indent=2); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    path.chmod(0o600)


def run(binary, binary_sha256, workspace, coordinator_runtime, run_dir, *, transport_factory=OneShotTransport):
    binary, context = preflight(binary, binary_sha256)
    workspace = _directory(workspace)
    coordinator = _directory(coordinator_runtime, private=True)
    target = Path(run_dir)
    _directory(target.parent)
    if not target.is_absolute() or target.is_symlink(): raise ValueError('UNSAFE_DIRECTORY')
    bundle = load_project_policy(workspace)
    # Existing coordinator's normal lock/idle evidence, without contacting or
    # restarting its services. No key or credential contents are opened.
    gate = Services.__new__(Services); gate.root = coordinator
    with lease(coordinator / 'runner.lock'):
        gate.require_idle_database()
        target.mkdir(mode=0o700)  # Existing/UNKNOWN run directory is never reused.
        _directory(target, private=True)
        policy = {'schema': 1, 'workspaces': {'development': str(workspace)}, 'models': [MODEL],
                  'max_threads': 1, 'max_tasks': 1, 'concurrency': 1, 'timeout_seconds': 120}
        _save(target / 'request.json', {'model': MODEL, 'prompt': PROMPT,
            'policy': policy, 'common_policy': bundle.metadata()})
        result = {'run_id': str(uuid.uuid4()), 'status': 'STARTING', 'context': context['state'],
                  'binary_sha256': binary_sha256, 'model': MODEL, 'automatic_retries': 0,
                  'inference_attempts': 0, 'inherited_tools_restricted': False,
                  'billing_verified': False, 'stop_requested': False, 'stop_confirmed': False}
        receipt = target / 'result.json'; _save(receipt, result)
        transport = transport_factory(binary, workspace)
        client = ProtocolClient(transport, timeout_s=10)
        pipeline = Pipeline(target, policy, client, instructions=developer_instructions(bundle), policy_sha256=bundle.sha256)
        transport.bind_client(client)
        pipeline.transport = transport
        stop = {'reason': None}; task = None
        def receive(event):
            pipeline.receive(event)
            message = event['message']
            if event['kind'] == 'server_request': stop['reason'] = 'SERVER_REQUEST_OR_APPROVAL'
            elif message['method'] in ('item/started', 'item/completed'):
                item = message.get('params', {}).get('item', {})
                if item.get('type') not in ('userMessage', 'agentMessage', 'reasoning', 'plan', 'contextCompaction'):
                    stop['reason'] = 'TOOL_USE_OBSERVED'
        client.event_sink = receive
        def interrupted(signum, frame):
            stop['reason'] = 'DEADLINE' if signum == signal.SIGALRM else 'USER_STOP'
            raise TimeoutError('ONE_SHOT_STOP')
        old = {s: signal.signal(s, interrupted) for s in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM)}
        started = time.monotonic(); signal.alarm(120)
        try:
            transport.start()
            initialized = client.initialize()
            if not schema_version_supported(initialized['userAgent']): raise ValueError('UNVERIFIED_SERVER_VERSION')
            thread = pipeline.dispatch('codex_thread_start', {'workspace': 'development', 'model': MODEL, 'request_key': 'once-thread'})
            if thread.get('status') != 'READY': raise ValueError('THREAD_NOT_READY')
            result['thread_id'] = thread['thread_id']
            if stop['reason']: raise ValueError('SERVER_REQUEST_OR_TOOL_OBSERVED')
            task = pipeline.dispatch('codex_task_submit', {'thread_id': thread['thread_id'], 'prompt': PROMPT, 'request_key': 'once-task'})
            if 'id' not in task: raise ValueError('TASK_NOT_BOUND')
            result['task_id'] = task['id']
            while task['state'] not in TERMINAL and not stop['reason']:
                transport.poll(timeout_s=.5)
                if stop['reason']: break
                task = pipeline.dispatch('codex_task_get', {'task_id': task['id']})
                if task['state'] in ('UNKNOWN', 'BLOCKED'): break
            result['status'] = 'RESULT_MISMATCH'
            if task['state'] == 'COMPLETED' and task.get('answer') == EXPECTED and not stop['reason']:
                result.update(status='PASS', answer=EXPECTED)
        except Exception as error:
            result.update(status='NOT_COMPLETED', exception_class=type(error).__name__)
        finally:
            # Ledger and disk errors must never bypass process cleanup or leave
            # signal handlers installed in the calling interpreter.
            for sig in old: signal.signal(sig, signal.SIG_IGN)
            signal.alarm(0)
            client.timeout_s = 2
            uncertain = False
            try:
                try:
                    if task is None:
                        tasks = pipeline._mutate(lambda state: list(state['tasks'].values()))
                        task = tasks[0] if tasks else None
                    if task is not None and task['state'] in HELD:
                        result['stop_requested'] = True
                        try:
                            pipeline.dispatch('codex_task_cancel', {'task_id': task['id'], 'request_key': 'once-stop'})
                            task = pipeline.dispatch('codex_task_get', {'task_id': task['id']})
                        except Exception: pass
                    if task is not None:
                        result.update(task_id=task['id'], task_state=task['state'],
                                      stop_confirmed=task['state'] in TERMINAL)
                except Exception as error:
                    uncertain = True; result['ledger_exception_class'] = type(error).__name__
                finally:
                    try: transport.close()
                    except Exception as error:
                        uncertain = True; result['cleanup_exception_class'] = type(error).__name__
                result.update(elapsed_seconds=round(time.monotonic() - started, 3),
                              inference_attempts=transport.inference_attempts,
                              stop_reason=stop['reason'], client=client.diagnostics(), transport=transport.diagnostics(),
                              cleanup_confirmed=bool(transport.cleanup_confirmed))
                try:
                    result['held_tasks'] = pipeline._mutate(lambda state: sum(t['state'] in HELD for t in state['tasks'].values()))
                except Exception as error:
                    uncertain = True; result.update(held_tasks=None, ledger_exception_class=type(error).__name__)
                if uncertain or result['held_tasks'] or not result['cleanup_confirmed']:
                    result.update(status='UNKNOWN', recovery='RECONCILE_THIS_RUN_AND_REMOTE_TURN_BEFORE_ANY_NEW_RUN')
                try: _save(receipt, result)
                except Exception as error:
                    result.update(status='UNKNOWN', receipt_exception_class=type(error).__name__,
                                  recovery='PRESERVE_EXISTING_RECEIPT_AND_RECONCILE_BEFORE_NEW_RUN')
            finally:
                for sig, handler in old.items(): signal.signal(sig, handler)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('binary', 'sha256', 'workspace', 'coordinator-runtime', 'run-dir'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    try: result = run(args.binary, args.sha256, args.workspace, args.coordinator_runtime, args.run_dir)
    except FileExistsError:
        result = {'status': 'BLOCKED', 'code': 'RUN_ALREADY_EXISTS_DO_NOT_REPLAY', 'automatic_retry': False}
    except Exception as error:
        result = {'status': 'UNKNOWN', 'exception_class': type(error).__name__,
                  'automatic_retry': False, 'recovery': 'CHECK_RUN_RECEIPT_BEFORE_ANY_NEW_RUN'}
    print(json.dumps(result))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__': raise SystemExit(main())
