"""Single active task runner using dedicated Codex App Server stdio processes."""

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time
import uuid
import recovery
import resources
import development_poc
from common_policy import PolicyError, load_project_policy, developer_instructions
from isolation_guard import IsolationBlocked, check_command, require_isolation_creation

from isolation import identity, launch, prepare, sandbox_preflight, isolated_launch, isolated_preflight, binary_version
from concurrency import guard, recover_abandoned
import workspaces
from transport import Process, ProtocolError, RpcError, redact, proc_identity
from storage import Store, STATES, runtime_lock, rollback_database
from verification import workspace_snapshot, verify

PROJECT = Path(__file__).resolve().parents[1]
RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"outcome": {"type": "string", "enum": ["success", "failure"]},
                   "result": {"type": "string"}},
    "required": ["outcome", "result"],
}
RESULT_INSTRUCTIONS = ("Return the final result using the requested JSON schema. "
                       "outcome=success means the requested operation actually succeeded. "
                       "Use outcome=failure for a denied, failed, unavailable or unperformed operation, "
                       "even when the user asked you to report that failure. Never claim execution without evidence. "
                       "Inside Codex isolation, do not create additional isolation with bwrap, unshare or equivalent operations. "
                       "Keep isolation-required evaluations BLOCKED with executed=false; never fall back to unisolated execution.")



class Runner:
    def __init__(self, root, binary=None, *, launcher=launch, resource_limits=None, development=False):
        development_poc.configure(root, development)
        self.development = development
        allowed = PROJECT / ".runtime"
        candidate = Path(root).expanduser().resolve()
        if allowed.is_symlink() or (candidate != allowed.resolve() and not candidate.is_relative_to(allowed.resolve())):
            raise ValueError("Runtime must remain inside this project's dedicated .runtime area")
        self.root, self.binary = prepare(root, binary)
        self.store = Store(self.root / "tasks.sqlite")
        self.launcher = launcher
        self.resource_limits = resources.policy(resource_limits) if launcher is launch or resource_limits is not None else None
        self.signal_cancel = False
        self.startup_recovered = []
        self.startup_recovered = recover_abandoned(self.root, self.store)

    def resource_control(self, task, execution_id=None):
        if self.resource_limits is None:
            return None  # Only explicit protocol fixtures bypass production controls.
        control = resources.Control(self.root, self.resource_limits, execution_id)
        if execution_id:
            execution = self.store.execution(execution_id)
            owner = json.loads(execution["owner"])
            owner.setdefault("resource_scopes", []).append(control.descriptor)
            owner["resource_policy"] = self.resource_limits
            with self.store.db:
                self.store.db.execute("UPDATE executions SET owner=? WHERE id=?", (json.dumps(owner), execution_id))
        return control

    def workroot(self, task):
        return workspaces.layout(self.root, self.store, task)

    def launch_task(self, task, args):
        root = self.workroot(task)
        if self.development:
            development_poc.validate_task(task)
            return development_poc.launch(self.root, root, self.binary, args, access=task['access'], authenticate=True)
        if task["workspace_id"] and self.launcher is launch:
            return isolated_launch(root, self.binary, args, access=task["access"])
        return self.launcher(root, self.binary, args)

    def task_lock(self, task):
        return guard(self.root, task) if task["workspace_id"] else self.lock()

    def snapshot(self, task_id):
        with self.task_lock(self.store.get(task_id)):
            return workspaces.create_snapshot(self, task_id)

    def release(self, task_id):
        task = self.store.get(task_id)
        if not task["workspace_id"]:
            raise ValueError("Legacy workspace cannot be released")
        with self.task_lock(task):
            with self.store.db:
                self.store.db.execute("BEGIN IMMEDIATE")
                if self.store.db.execute("SELECT 1 FROM tasks WHERE workspace_id=? AND state IN ('QUEUED','RUNNING','VERIFYING','UNKNOWN')", (task["workspace_id"],)).fetchone():
                    raise ValueError("Queued, active or unresolved tasks still own this workspace")
                workspaces.layout(self.root, self.store, task)
                self.store.db.execute("UPDATE workspaces SET state='RELEASED',released=? WHERE id=?", (time.time(), task["workspace_id"]))
            return {"workspace_id": task["workspace_id"], "state": "RELEASED", "files_retained": True}

    def lock(self):
        held = runtime_lock(self.root)
        try:
            from codex_host import pipeline_held
            if pipeline_held(self.root): raise RuntimeError('Pipeline execution or cleanup remains unresolved')
            return held
        except BaseException:
            held.close(); raise

    def run(self, task_id, timeout=120, *, dispatch_token=None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be finite and positive")
        import scheduler
        with scheduler.execution_admission(self.root, self.store, task_id, dispatch_token), self.task_lock(self.store.get(task_id)):
            self.store.recover(task_id)
            task = self.store.get(task_id)
            workroot = self.workroot(task)
            if self.development: development_poc.validate_task(task)
            owner = {"boot_id": Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                     "process": proc_identity(os.getpid())}
            if self.development: owner["execution_mode"] = development_poc.MODE
            self.store.claim(task_id, owner, workspace_snapshot(workroot / "workspace"))
            return self._execute(task_id, timeout)

    def reconcile(self, task_id, offline=False):
        with self.task_lock(self.store.get(task_id)):
            self.store.recover(task_id)
            return recovery.reconcile(self, task_id, offline=offline)

    def resolve(self, task_id, reconciliation_id, kind, actor, reason, accept_side_effects=False):
        with self.task_lock(self.store.get(task_id)):
            self.store.recover(task_id)
            return recovery.decision(self, task_id, reconciliation_id, kind, actor, reason, accept_side_effects)

    def review(self, task_id, approve, actor, reason):
        with self.task_lock(self.store.get(task_id)):
            self.store.recover(task_id)
            return recovery.review(self, task_id, approve, actor, reason)

    def observe(self, execution_id, processes):
        execution = self.store.execution(execution_id)
        old = json.loads(execution["processes"]) if execution["processes"] else {}
        old.update({str(pid): start for pid, start in processes.items()})
        self.store.execution_update(execution_id, processes=json.dumps(old), launch_pending=0)

    def _verify_task(self, task_id, log, deadline=None):
        task = self.store.get(task_id)
        execution_id = task["active_execution_id"]
        self.store.execution_update(execution_id, phase="VERIFYING")
        self.store.update(task_id, verification_status="RUNNING")
        policy = json.loads(task["acceptance"]) if task["acceptance"] else {
            "artifacts": [], "tests": [], "conditions": [], "commands": {}, "human": True}
        evidence = verify(self.workroot(task), policy, task["result"] or "", isolated=bool(task["workspace_id"]),
                          launcher=(lambda argv: development_poc.launch(self.root, self.workroot(task), argv[0], argv[1:])) if self.development else None,
                          resource_factory=lambda: self.resource_control(task, execution_id),
                          cancelled=lambda: self.signal_cancel or bool(self.store.get(task_id)["cancel_requested"]),
                          observer=lambda ids: self.observe(execution_id, ids), log=log,
                          launching=lambda: self.store.execution_update(execution_id, launch_pending=1),
                          deadline=deadline or (time.monotonic() + 60),
                          context={"task_id": task_id, "execution_id": execution_id, "phase": "VERIFYING",
                                   "workspace_id": task["workspace_id"], "role": task["role"], "snapshot_id": task["snapshot_id"]})
        surviving = any(item["cleanup"].get("survivors") or (
            item["cleanup"].get("resources") and not item["cleanup"]["resources"]["released"])
            for item in evidence["tests"] if item.get("cleanup"))
        if surviving or self.store.execution(execution_id)["launch_pending"]:
            state, status = "UNKNOWN", "UNKNOWN"
        elif evidence.get('blocked'):
            state, status = 'WAITING_FOR_HUMAN', 'BLOCKED'
        elif evidence["stop_reason"]:
            state, status = evidence["stop_reason"], evidence["stop_reason"]
        elif not evidence["all_passed"]:
            state, status = "FAILED", "FAILED"
        elif evidence["human_required"]:
            state, status = "WAITING_FOR_HUMAN", "PASSED"
        else:
            state, status = "COMPLETED", "PASSED"
        self.store.finish_verification(task_id, evidence, state, status,
                                       "PENDING" if state == "WAITING_FOR_HUMAN" else "NOT_REQUIRED")
        return self.store.get(task_id)

    def _execute(self, task_id, timeout):
        task = self.store.get(task_id)
        execution_id = task["active_execution_id"]
        workroot = self.workroot(task)
        proc = None
        thread_id = None
        turn_id = None
        dispatched = False
        terminal = None
        stop_reason = None
        blocked = None
        cleanup = None
        failures = []
        final_messages = []
        usage = None
        historical_turns = set()
        if task["resume_of"]:
            source_thread = self.store.get(task["resume_of"])["session_id"]
            historical_turns = {r[0] for r in self.store.db.execute(
                "SELECT turn_id FROM executions WHERE thread_id=? AND turn_id IS NOT NULL AND id<>?",
                (source_thread, execution_id))}
        deadline = time.monotonic() + timeout
        log_path = self.root / "logs" / f"{execution_id}.jsonl"
        log_context = {"task_id": task_id, "execution_id": execution_id, "workspace_id": task["workspace_id"],
                       "role": task["role"], "snapshot_id": task["snapshot_id"]}

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("Task execution deadline exceeded")
            return min(15, value)

        def cancelled():
            return self.signal_cancel or bool(self.store.get(task_id)["cancel_requested"])

        def handle(message):
            nonlocal terminal, usage
            if "id" in message and "method" in message:
                method = message["method"]
                failures.append(f"Client request denied: {method}")
                if method in ["item/commandExecution/requestApproval", "item/fileChange/requestApproval"]:
                    proc.send({"id": message["id"], "result": {"decision": "decline"}})
                elif method == "item/permissions/requestApproval":
                    proc.send({"id": message["id"], "result": {"permissions": {}, "scope": "turn"}})
                else:
                    proc.send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported client request"}})
                return
            method = message.get("method")
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise ProtocolError("Event params must be an object")
            if params.get("threadId") not in [None, thread_id]:
                raise ProtocolError("Event belongs to an unexpected thread")
            if method == "thread/tokenUsage/updated" and params.get("turnId") in historical_turns:
                proc.record("historical_usage_ignored", {"threadId": thread_id, "turnId": params["turnId"]})
                return
            if (method == "item/completed" or method == "thread/tokenUsage/updated") and params.get("turnId") != turn_id:
                raise ProtocolError("Event belongs to an unexpected turn")
            if method == "thread/tokenUsage/updated":
                usage = params.get("tokenUsage")
            if method == "item/completed":
                item = params.get("item", {})
                if not isinstance(item, dict):
                    raise ProtocolError("Event item must be an object")
                if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                    final_messages.append(item.get("text", ""))
                if item.get("type") in ["commandExecution", "fileChange", "mcpToolCall"]:
                    if item.get("status") in ["failed", "declined"] or item.get("exitCode") not in [None, 0]:
                        failures.append(f"Tool failed or denied: {item.get('type')} {item.get('id')}")
            if method == "error" and not params.get("willRetry", False):
                failures.append("Codex reported a non-retryable error")
            if method == "turn/completed":
                candidate = params.get("turn", {})
                if candidate.get("id") != turn_id:
                    raise ProtocolError("Terminal event belongs to an unexpected turn")
                if candidate.get("status") not in ["completed", "failed", "interrupted"]:
                    raise ProtocolError("Unknown terminal turn status")
                terminal = candidate

        with log_path.open("x", encoding="utf-8") as stream:
            log = resources.BudgetLog(stream, self.resource_limits["log_bytes"]) if self.resource_limits else stream
            self.store.update(task_id, log_path=str(log_path))
            try:
                if self.launcher is launch or self.development:
                    # Before resume inspection can attempt its own launcher.
                    require_isolation_creation('codex-execution')
                # Materialize the canonical text before any server/model dispatch.
                # The child sandbox need not mount the policy repository.
                common_policy = load_project_policy(PROJECT)
                policy_metadata = common_policy.metadata()
                owner = json.loads(self.store.execution(execution_id)["owner"])
                owner["common_policy"] = policy_metadata
                with self.store.db:
                    self.store.db.execute("UPDATE executions SET owner=?,updated=? WHERE id=?",
                                          (json.dumps(owner), time.time(), execution_id))
                if task["resume_of"]:
                    source = self.store.get(task["resume_of"])
                    previous = json.loads(self.store.execution(source["active_execution_id"])["owner"]).get("common_policy")
                    if previous is not None:
                        if (not isinstance(previous, dict)
                                or previous.get("sha256") != common_policy.sha256
                                or previous.get("version") != common_policy.version):
                            raise PolicyError("Resume policy conflicts with the source execution; start a newly reviewed task")
                    else:
                        log_context["policy_resume_source"] = "legacy-unpinned"
                log_context["common_policy"] = policy_metadata
                log.write(json.dumps({**log_context, "time": time.time(), "direction": "monitor",
                                      "message": {"event": "common_policy_loaded", **policy_metadata}},
                                     ensure_ascii=False) + "\n")
                log.flush()
                # Inspect history before opening this task's server. A resume
                # must not temporarily double the number of live servers.
                if task["resume_of"]:
                    recovery.pre_resume(self, task, deadline=deadline)
                if self.launcher is launch and not self.development:
                    if task["workspace_id"]:
                        isolated_preflight(workroot, task["access"], remaining())
                    else:
                        sandbox_preflight(self.root, remaining())
                version = (development_poc.version(self.root, workroot, self.binary, remaining()) if self.development
                           else binary_version(workroot, self.binary, timeout=remaining()))
                self.store.update(task_id, binary=self.binary, version=version, log_path=str(log_path))
                argv, env = self.launch_task(task, ["app-server", "--listen", "stdio://"])
                check_command(argv)
                self.store.execution_update(execution_id, launch_pending=1)
                proc = Process(argv, cwd=workroot / "workspace", env=env, log=log,
                               on_observed=lambda ids: self.observe(execution_id, ids), context=log_context,
                               resources=self.resource_control(task, execution_id))
                proc.call("initialize", {"clientInfo": {"name": "agent_orchestrator_phase1", "version": "0.1.0"},
                                         "capabilities": {"experimentalApi": False}}, remaining())
                proc.send({"method": "initialized", "params": {}})
                account = proc.call("account/read", {"refreshToken": False}, remaining())
                if account.get("account", {}).get("type") != "chatgpt":
                    raise RuntimeError("ChatGPT subscription authentication required; no API fallback")
                params = {"cwd": str(workroot / "workspace"), "sandbox": task["access"], "approvalPolicy": "never",
                          "developerInstructions": developer_instructions(common_policy, RESULT_INSTRUCTIONS + (
                              " You are an independent Reviewer. Inspect the supplied frozen snapshot read-only. "
                              "Report findings with file/test evidence; do not trust an implementer's success claim. "
                              "Your result does not approve the source task." if task["role"] == "reviewer" else "")) }
                if task["resume_of"]:
                    params["threadId"] = self.store.get(task["resume_of"])["session_id"]
                    result = proc.call("thread/resume", params, remaining())
                    if result["thread"]["id"] != params["threadId"]:
                        raise ProtocolError("Resume returned a different thread")
                else:
                    params["ephemeral"] = False
                    result = proc.call("thread/start", params, remaining())
                thread_id = result["thread"]["id"]
                self.store.update(task_id, session_id=thread_id)
                self.store.execution_update(execution_id, thread_id=thread_id)
                log_context["thread_id"] = thread_id
                if (result.get("approvalPolicy") != "never" or result.get("sandbox", {}).get("type") != ("workspaceWrite" if task["access"] == "workspace-write" else "readOnly")
                        or result.get("sandbox", {}).get("networkAccess", False)):
                    raise RuntimeError("Effective sandbox or approval policy differs from the required policy")
                if cancelled():
                    stop_reason = "CANCELLED"
                else:
                    # Mark uncertainty before writing: a lost RPC response may still have started work.
                    dispatched = True
                    self.store.execution_update(execution_id, dispatched=1, codex_status="DISPATCHING")
                    self.store.update(task_id, codex_status="DISPATCHING")
                    result = proc.call("turn/start", {"threadId": thread_id,
                                       "input": [{"type": "text", "text": task["prompt"]}],
                                       "clientUserMessageId": execution_id,
                                       "outputSchema": RESULT_SCHEMA,
                                       "sandboxPolicy": ({"type": "externalSandbox", "networkAccess": "enabled"} if self.development else
                                                         ({"type": "workspaceWrite", "writableRoots": [str(workroot / "workspace")],
                                                          "networkAccess": False, "excludeSlashTmp": True, "excludeTmpdirEnvVar": True}
                                                         if task["access"] == "workspace-write" else {"type": "readOnly"}))}, remaining())
                    turn_id = result["turn"]["id"]
                    self.store.update(task_id, turn_id=turn_id)
                    self.store.execution_update(execution_id, turn_id=turn_id)
                    self.store.execution_update(execution_id, codex_status="RUNNING")
                    self.store.update(task_id, codex_status="RUNNING")
                    next_poll = time.monotonic()
                    while terminal is None:
                        if cancelled():
                            stop_reason = "CANCELLED"
                            break
                        if time.monotonic() >= deadline:
                            stop_reason = "TIMED_OUT"
                            break
                        message = proc.next_message(min(0.2, remaining()))
                        if message:
                            handle(message)
                        if time.monotonic() >= next_poll:
                            status = proc.call("thread/read", {"threadId": thread_id, "includeTurns": False}, remaining())
                            proc.record("monitor", {"threadId": thread_id, "status": status["thread"]["status"]})
                            proc.observe_children()
                            next_poll = time.monotonic() + 1
            except IsolationBlocked as exc:
                blocked = exc.evidence
                stop_reason = 'WAITING_FOR_HUMAN'
                # Every guard raises before launch; no uncertain checkpoint.
                self.store.execution_update(execution_id, launch_pending=0)
                log.write(json.dumps({'direction': 'isolation_blocked', 'payload': blocked}) + '\n')
            except (TimeoutError, subprocess.TimeoutExpired):
                stop_reason = "TIMED_OUT"
            except resources.ResourceExceeded as exc:
                failures.append(str(exc))
                stop_reason = "FAILED"
            except RpcError as exc:
                failures.append(str(exc))
                # An explicit server rejection of turn/start did not accept the turn.
                if turn_id is None:
                    dispatched = False
                stop_reason = "UNKNOWN" if dispatched and turn_id is not None else "FAILED"
            except (OSError, EOFError, ProtocolError, RuntimeError, KeyError, TypeError, AttributeError,
                    ValueError, subprocess.SubprocessError) as exc:
                failures.append(f"{type(exc).__name__}: {exc}")
                stop_reason = "UNKNOWN" if dispatched else "FAILED"
            finally:
                if proc:
                    proc.closing = True  # Cleanup RPCs must remain possible after a budget trip.
                    if stop_reason in ["TIMED_OUT", "CANCELLED"] and turn_id and terminal is None:
                        try:
                            proc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=2)
                            until = time.monotonic() + 2
                            while terminal is None and time.monotonic() < until:
                                message = proc.next_message(0.1)
                                if message:
                                    handle(message)
                        except (OSError, EOFError, TimeoutError, RpcError, ProtocolError, ValueError, KeyError, TypeError, AttributeError):
                            failures.append("Interrupt acknowledgement unavailable; process termination required")
                    if thread_id:
                        try:
                            proc.call("thread/unsubscribe", {"threadId": thread_id}, timeout=2)
                        except (OSError, EOFError, TimeoutError, RpcError, ProtocolError, ValueError, KeyError, TypeError, AttributeError):
                            failures.append("Unsubscribe acknowledgement unavailable")
                    cleanup = proc.close()

            resource_result = cleanup.get("resources", {}) if cleanup else {}
            if resource_result.get("exceeded") or (cleanup and cleanup.get("log_limit_exceeded")):
                stop_reason = "FAILED"
                failures.append("Resource limit termination: " + str(resource_result.get("exceeded") or "log_bytes"))
            elif resource_result and (not resource_result["confirmed"] or not resource_result["released"]):
                stop_reason = "UNKNOWN" if dispatched else "FAILED"
                failures.append("Resource supervision or release is unconfirmed")
            result_text = "\n".join(final_messages)
            outcome = None
            if terminal and terminal["status"] == "completed" and final_messages:
                try:
                    structured = json.loads(result_text)
                    if (not isinstance(structured, dict) or set(structured) != {"outcome", "result"}
                            or structured["outcome"] not in ["success", "failure"]
                            or not isinstance(structured["result"], str)):
                        raise ValueError("Invalid final result schema")
                    outcome = structured["outcome"]
                    result_text = structured["result"]
                    if outcome == "failure":
                        failures.append("Agent reported an unsuccessful operation")
                except (ValueError, TypeError):
                    failures.append("Final result did not satisfy the required JSON schema")
            if cleanup and cleanup["survivors"]:
                state = "UNKNOWN"
                failures.append("Live descendant survived cleanup")
            elif resource_result and not resource_result["released"]:
                state = "UNKNOWN"
                failures.append("Resource scope release is unconfirmed")
            elif self.store.execution(execution_id)["launch_pending"]:
                state = "UNKNOWN"
                failures.append("Process launch checkpoint remains uncertain")
            elif stop_reason:
                state = stop_reason
            elif terminal and terminal["status"] == "completed" and outcome == "success" and not failures:
                state = "COMPLETED" if cleanup and cleanup["exit_code"] == 0 else "UNKNOWN"
            elif terminal and terminal["status"] in ["completed", "failed"]:
                state = "FAILED"
            elif terminal and terminal["status"] == "interrupted":
                state = "CANCELLED"
            else:
                state = "UNKNOWN"
            if terminal and terminal.get("error"):
                failures.append(json.dumps(redact(terminal["error"]), ensure_ascii=False))
            codex_status = ("SUCCEEDED" if terminal and terminal["status"] == "completed" and cleanup
                            and cleanup["exit_code"] == 0 and not cleanup["survivors"]
                            else state)
            if blocked:
                codex_status = 'BLOCKED'
                self.store.update(task_id, verification_status='BLOCKED', human_status='PENDING')
            self.store.execution_update(execution_id, phase="VERIFYING" if state == "COMPLETED" else state,
                                        terminal=json.dumps(terminal), codex_status=codex_status, agent_outcome=outcome,
                                        cleanup=json.dumps(cleanup), finished=None if state == "COMPLETED" else time.time())
            self.store.update(task_id, result=result_text, error=json.dumps(blocked) if blocked else "; ".join(failures) or None, codex_status=codex_status,
                              usage=json.dumps(usage) if usage else None,
                              cleanup=json.dumps(cleanup) if cleanup else None)
            if state == "COMPLETED":
                self.store.transition(task_id, "RUNNING", "VERIFYING", "Codex finished; task acceptance still pending")
                return self._verify_task(task_id, log, deadline)
            self.store.transition(task_id, "RUNNING", state, "observed execution outcome; no automatic retry")
        return self.store.get(task_id)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Codex task runner (Linux, at most two isolated sessions)")
    parser.add_argument("--development-poc", action="store_true", help="Explicit disposable bwrap PoC mode")
    parser.add_argument("--runtime", type=Path, default=PROJECT / ".runtime")
    parser.add_argument("--codex", help="Absolute Codex binary path; default resolves PATH")
    parser.add_argument('--allow-codex-pipeline', action='store_true', help='Opt-in daemon-owned bounded native gateway')
    parser.add_argument("--via-daemon", action="store_true", help="Use the private daemon control socket")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("usage")
    sub.add_parser("daemon")
    for name in ["status", "list", "pause", "resume_scheduler", "shutdown"]:
        command_parser = sub.add_parser(name)
        if name == "list":
            command_parser.add_argument("--limit", type=int, default=100)
            command_parser.add_argument("--offset", type=int, default=0)
    submit = sub.add_parser("submit")
    submit.add_argument("prompt")
    submit.add_argument("--resume-of")
    submit.add_argument("--request-key")
    submit.add_argument("--acceptance", type=Path, help="Operator-defined acceptance JSON file")
    submit.add_argument("--resume-approval", help="Explicit recovery decision ID, when required")
    submit.add_argument("--timeout", type=float, help="Daemon scheduling timeout")
    submit.add_argument("--isolated", action="store_true")
    submit.add_argument("--fixture", help="Dedicated fixture name under runtime/fixtures")
    submit.add_argument("--access", choices=["read-only", "workspace-write"], default="read-only")
    submit.add_argument("--role", choices=["implementer", "reviewer"], default="implementer")
    submit.add_argument("--review-snapshot")
    for name in ["snapshot", "release"]:
        sub.add_parser(name).add_argument("task_id")
    sub.add_parser("cancel-all")
    pair = sub.add_parser("run-pair")
    pair.add_argument("task_ids", nargs=2)
    pair.add_argument("--timeout", type=float, default=120)
    run = sub.add_parser("run")
    run.add_argument("task_id")
    run.add_argument("--timeout", type=float, default=120)
    run.add_argument("--dispatch-token", help=argparse.SUPPRESS)
    for command in ["show", "cancel"]:
        sub.add_parser(command).add_argument("task_id")
    sub.add_parser("recover")
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("task_id")
    reconcile.add_argument("--offline", action="store_true", help="Inspect local evidence only; no session eligibility claim")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("task_id")
    resolve.add_argument("--reconciliation", required=True)
    resolve.add_argument("--kind", choices=["allow_resume", "verify", "fail"], required=True)
    resolve.add_argument("--actor", required=True)
    resolve.add_argument("--reason", required=True)
    resolve.add_argument("--accept-side-effects", action="store_true")
    review = sub.add_parser("review")
    review.add_argument("task_id")
    review.add_argument("--decision", choices=["approve", "reject"], required=True)
    review.add_argument("--actor", required=True)
    review.add_argument("--reason", required=True)
    rollback = sub.add_parser("db-rollback")
    rollback.add_argument("backup_name")
    args = parser.parse_args()
    # Keep all writable state inside this project's dedicated test/runtime area.
    if (PROJECT / ".runtime").is_symlink():
        parser.error("The project runtime directory must not be a symlink")
    allowed = (PROJECT / ".runtime").resolve()
    if args.development_poc:
        raw = args.runtime.expanduser().absolute()
        if any(p.is_symlink() for p in [raw, *raw.parents]):
            parser.error("PoC runtime ancestry must not contain symlinks")
    root = args.runtime.expanduser().resolve()
    if root != allowed and not root.is_relative_to(allowed):
        parser.error("--runtime must be .runtime or a child directory inside this project")
    runner = None
    try:
        if args.command == "daemon":
            from daemon import foreground
            return foreground(root, args.codex, development=args.development_poc, allow_codex_pipeline=args.allow_codex_pipeline)
        if args.via_daemon or args.command in ["status", "list", "pause", "resume_scheduler", "shutdown"]:
            from control_socket import request
            method, params = args.command, {}
            if args.command == "submit":
                key = args.request_key or str(uuid.uuid4())
                params = {"prompt": args.prompt, "request_key": key,
                          "acceptance": json.loads(args.acceptance.read_text()) if args.acceptance else None,
                          "workspace": {"fixture": args.fixture, "access": args.access, "role": args.role, "snapshot_id": args.review_snapshot},
                          "resume_of": args.resume_of, "resume_approval": args.resume_approval}
                if args.resume_of and not (args.isolated or args.fixture or args.access != "read-only" or args.role != "implementer" or args.review_snapshot):
                    params["workspace"] = None  # Existing resume policy is inherited unchanged.
                if args.timeout is not None: params["timeout"] = args.timeout
            elif args.command in ["show", "cancel"]:
                method = "status" if args.command == "show" else "cancel"
                params = {"task_id": args.task_id}
            elif args.command == "list":
                params = {"limit": args.limit, "offset": args.offset}
            elif args.command not in ["status", "list", "pause", "resume_scheduler", "shutdown"]:
                raise ValueError("This operation is not available through the daemon")
            try: result = request(root, method, params)
            except (OSError, EOFError, RuntimeError, ValueError) as exc:
                if args.command == "submit":
                    print(json.dumps({"error": redact(str(exc)), "request_key": key, "retry": "Reuse this key; do not create a blind duplicate"}))
                    return 1
                raise
            print(json.dumps(redact(result), ensure_ascii=False, indent=2))
            return 0
        if args.command == "db-rollback":
            print(json.dumps(rollback_database(root / "tasks.sqlite", args.backup_name), ensure_ascii=False, indent=2))
            return 0
        runner = Runner(root, args.codex, development=args.development_poc)
        if args.command == "usage":
            from retention import report
            result = report(root, runner.store)
        elif args.command == "check":
            result = identity(root, runner.binary)
        elif args.command == "submit":
            if args.timeout is not None:
                raise ValueError("submit --timeout requires --via-daemon; local runs use run --timeout")
            policy = json.loads(args.acceptance.read_text()) if args.acceptance else None
            workspace = None
            if args.isolated:
                workspace = {"fixture": args.fixture, "access": args.access, "role": args.role, "snapshot_id": args.review_snapshot}
            elif args.fixture or args.access != "read-only" or args.role != "implementer" or args.review_snapshot:
                raise ValueError("Workspace options require --isolated")
            result = {"task_id": runner.store.submit(args.prompt, resume_of=args.resume_of, request_key=args.request_key,
                                                      acceptance=policy, resume_approval=args.resume_approval, workspace=workspace)}
        elif args.command == "run":
            def cancel_signal(signum, frame):
                runner.signal_cancel = True
            signal.signal(signal.SIGINT, cancel_signal)
            signal.signal(signal.SIGTERM, cancel_signal)
            result = runner.run(args.task_id, args.timeout, dispatch_token=args.dispatch_token)
        elif args.command == "run-pair":
            from coordinator import run_pair
            result = run_pair(root, runner.binary, args.task_ids, args.timeout)
        elif args.command == "cancel-all":
            result = {"tasks": runner.store.cancel_all()}
        elif args.command == "snapshot":
            result = runner.snapshot(args.task_id)
        elif args.command == "release":
            result = runner.release(args.task_id)
        elif args.command == "show":
            result = runner.store.get(args.task_id)
            if result["active_execution_id"]:
                result["execution"] = runner.store.execution(result["active_execution_id"])
            result["startup_recovered"] = runner.startup_recovered
            for table, key, json_field in [("verifications", "verification", "evidence"),
                                           ("reconciliations", "reconciliation", "facts")]:
                row = runner.store.db.execute(f"SELECT * FROM {table} WHERE task_id=? ORDER BY created DESC LIMIT 1", (args.task_id,)).fetchone()
                if row:
                    result[key] = dict(row)
                    result[key][json_field] = json.loads(row[json_field])
            result["decisions"] = [dict(row) for row in runner.store.db.execute(
                "SELECT * FROM decisions WHERE task_id=? ORDER BY created DESC LIMIT 20", (args.task_id,))]
        elif args.command == "cancel":
            result = runner.store.cancel(args.task_id)
        elif args.command == "reconcile":
            result = runner.reconcile(args.task_id, args.offline)
        elif args.command == "resolve":
            result = runner.resolve(args.task_id, args.reconciliation, args.kind, args.actor, args.reason, args.accept_side_effects)
        elif args.command == "review":
            result = runner.review(args.task_id, args.decision == "approve", args.actor, args.reason)
        else:
            result = {"unknown_tasks": runner.startup_recovered + recover_abandoned(root, runner.store)}
        print(json.dumps(redact(result), ensure_ascii=False, indent=2))
        return (0 if all(t["state"] == "COMPLETED" for t in result["tasks"]) else 2) if args.command == "run-pair" else (0 if args.command != "run" or result["state"] == "COMPLETED" else 2)
    except (ValueError, OSError, EOFError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": redact(str(exc)), "type": type(exc).__name__}, ensure_ascii=False))
        return 1
    finally:
        if runner:
            runner.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
