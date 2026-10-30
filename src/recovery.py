"""Reconciliation observes owned sessions; it never starts a model turn or reruns a task."""

import json
from pathlib import Path
import time
import uuid

from transport import Process, ProtocolError, RpcError, proc_identity, redact
from verification import workspace_snapshot


def resource_facts(execution, task):
    cleanup = json.loads(execution["cleanup"] or "null") if execution else json.loads(task["cleanup"] or "null")
    if not execution:
        if cleanup and "survivors" in cleanup and not cleanup["survivors"]:
            return {"released": True, "basis": "legacy_durable_cleanup_record", "live": []}
        return {"released": False, "basis": "legacy_resources_untracked", "live": []}
    if execution.get("launch_pending"):
        return {"released": False, "basis": "process_launch_checkpoint_missing", "live": []}
    identities = json.loads(execution["processes"] or "{}")
    owner = json.loads(execution["owner"])
    current_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if owner["boot_id"] != current_boot:
        return {"released": True, "basis": "kernel_boot_changed", "live": []}
    live = []
    for pid, start in identities.items():
        actual = proc_identity(int(pid))
        if actual and actual["start"] == start and actual["state"] not in ["Z", "X"]:
            live.append(actual)
    scope_states = []
    for scope in owner.get("resource_scopes", []):
        from resources import scope_released
        scope_states.append(scope_released(scope))
    return {"released": bool(identities) and not live and all(scope_states), "resource_scopes_released": scope_states,
            "basis": "recorded_pid_and_start_time" if identities else "launch_gap_untracked",
            "live": live}


def log_facts(root, task):
    if not task["log_path"]:
        return {"available": False}
    path = Path(task["log_path"])
    if path.is_symlink() or path.resolve().parent != (root / "logs").resolve():
        return {"available": False, "reason": "unsafe_log_path"}
    try:
        with path.open("rb") as stream:
            data = stream.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            return {"available": False, "reason": "log_size_limit"}
        complete = data.split(b"\n")[:-1]
        bad = 0
        for line in complete:
            try:
                if line.strip():
                    json.loads(line)
            except (ValueError, UnicodeError):
                bad += 1
        return {"available": True, "complete_lines": len(complete), "invalid_lines": bad,
                "truncated_tail_bytes": len(data.rsplit(b"\n", 1)[-1])}
    except OSError:
        return {"available": False, "reason": "log_unavailable"}


def inspect_session(runner, task, execution, log, probe_execution_id=None, deadline=None):
    thread_id = execution["thread_id"] if execution else task["session_id"]
    turn_id = execution["turn_id"] if execution else task["turn_id"]
    facts = {"thread_id": thread_id, "turn_id": turn_id, "exists": False, "ended": False,
             "codex_succeeded": False, "latest_turn_matches": False}
    if not thread_id:
        facts["reason"] = "thread_id_not_recorded"
        return facts
    proc = None
    def budget():
        remaining = 10 if deadline is None else min(10, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("Resume inspection deadline exceeded")
        return remaining
    try:
        argv, env = runner.launch_task(task, ["app-server", "--listen", "stdio://"])
        if probe_execution_id:
            runner.store.execution_update(probe_execution_id, launch_pending=1)
        proc = Process(argv, cwd=runner.workroot(task) / "workspace", env=env, log=log,
                       on_observed=(lambda ids: runner.observe(probe_execution_id, ids)) if probe_execution_id else None,
                       context={"task_id": task["id"], "execution_id": task["active_execution_id"],
                                "workspace_id": task["workspace_id"], "snapshot_id": task["snapshot_id"],
                                "probe_execution_id": probe_execution_id, "operation": "reconcile"},
                       resources=runner.resource_control(task, probe_execution_id))
        proc.call("initialize", {"clientInfo": {"name": "agent_orchestrator_phase2", "version": "0.2.0"},
                                 "capabilities": {"experimentalApi": False}}, budget())
        proc.send({"method": "initialized", "params": {}})
        account = proc.call("account/read", {"refreshToken": False}, budget())
        if (account.get("account") or {}).get("type") != "chatgpt":
            raise RuntimeError("ChatGPT authentication is required")
        thread = proc.call("thread/read", {"threadId": thread_id, "includeTurns": False}, budget())["thread"]
        facts["exists"] = thread.get("id") == thread_id
        facts["runtime_status"] = thread.get("status")
        if thread.get("cwd") and Path(thread["cwd"]).resolve() != (runner.workroot(task) / "workspace").resolve():
            raise RuntimeError("Stored session working directory does not match this workspace")
        cursor = None
        found = None
        for page in range(10):
            params = {"threadId": thread_id, "itemsView": "full", "limit": 20}
            if cursor:
                params["cursor"] = cursor
            response = proc.call("thread/turns/list", params, budget())
            rows = response["data"]
            if page == 0:
                facts["latest_turn_matches"] = bool(rows and rows[0].get("id") == turn_id)
            found = next((turn for turn in rows if turn.get("id") == turn_id), None)
            cursor = response.get("nextCursor")
            if found or not cursor:
                break
        if found:
            facts["turn"] = {k: found.get(k) for k in ["id", "status", "error", "startedAt", "completedAt"]}
            facts["ended"] = found.get("status") in ["completed", "failed", "interrupted"]
            items = found.get("items", [])
            messages = [x.get("text", "") for x in items if x.get("type") == "agentMessage" and x.get("phase") != "commentary"]
            failed_tool = any(x.get("type") in ["commandExecution", "fileChange", "mcpToolCall"] and (
                x.get("status") in ["failed", "declined"] or x.get("exitCode") not in [None, 0]) for x in items)
            facts["failed_tool"] = failed_tool
            try:
                result = json.loads(messages[-1]) if messages else None
                valid = isinstance(result, dict) and set(result) == {"outcome", "result"} and isinstance(result["result"], str)
                if valid:
                    facts["agent_result"] = result
                facts["codex_succeeded"] = bool(found.get("status") == "completed" and not found.get("error")
                                                and valid and result["outcome"] == "success" and not failed_tool)
            except (ValueError, TypeError):
                pass
        else:
            facts["reason"] = "recorded_turn_not_found_or_id_missing"
    except (OSError, EOFError, TimeoutError, ProtocolError, RpcError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
        facts["error"] = f"{type(exc).__name__}: {redact(str(exc))}"
    finally:
        if proc:
            facts["probe_cleanup"] = proc.close()
            if facts["probe_cleanup"]["survivors"]:
                facts["ended"] = False
    return redact(facts)


def reconcile(runner, task_id, *, offline=False, probe_execution_id=None, deadline=None):
    task = runner.store.get(task_id)
    if task["state"] in ["QUEUED", "RUNNING", "VERIFYING"]:
        raise ValueError("Reconciliation requires a stopped or recovered task")
    execution = runner.store.execution(task["active_execution_id"]) if task["active_execution_id"] else None
    current = workspace_snapshot(runner.workroot(task) / "workspace")
    before = json.loads(execution["workspace_before"]) if execution and execution["workspace_before"] else None
    effects = ("UNKNOWN" if not before or before["problems"] or current["problems"] else
               "UNCHANGED" if current["fingerprint"] == before["fingerprint"] else "CHANGED")
    reconciliation_id = str(uuid.uuid4())
    with (runner.root / "logs" / f"reconcile-{reconciliation_id}.jsonl").open("x") as log:
        session = ({"thread_id": task["session_id"], "exists": False, "ended": False,
                    "reason": "offline_only", "codex_succeeded": False} if offline else
                   inspect_session(runner, task, execution, log, probe_execution_id, deadline))
    resources = resource_facts(execution, task)
    facts = {"task_state": task["state"], "execution_id": task["active_execution_id"],
             "workspace_id": task["workspace_id"], "snapshot_id": task["snapshot_id"], "access": task["access"], "role": task["role"],
             "resources": resources, "side_effects": effects, "workspace": current,
             "side_effect_scope": "Workspace regular-file manifest only; external and transient effects are not established",
             "session": session, "log": log_facts(runner.root, task), "previous_error": task["error"]}
    facts["resume_eligible"] = bool(resources["released"] and session.get("exists") and session.get("ended")
                                    and session.get("latest_turn_matches") and not current["problems"])
    with runner.store.db:
        runner.store.db.execute("INSERT INTO reconciliations VALUES(?,?,?,?,?)", (
            reconciliation_id, task_id, task["active_execution_id"], json.dumps(redact(facts), ensure_ascii=False), time.time()))
    return {"reconciliation_id": reconciliation_id, **facts}


def decision(runner, task_id, reconciliation_id, kind, actor, reason, accept_side_effects=False):
    if kind not in ["allow_resume", "verify", "fail"] or not actor.strip() or not reason.strip():
        raise ValueError("Recovery decision requires a supported kind, actor and reason")
    task = runner.store.get(task_id)
    if kind in ["verify", "fail"] and task["state"] not in ["UNKNOWN", "WAITING_FOR_HUMAN"]:
        raise ValueError("This recovery decision requires UNKNOWN or WAITING_FOR_HUMAN")
    row = runner.store.db.execute("SELECT * FROM reconciliations WHERE id=? AND task_id=?",
                                  (reconciliation_id, task_id)).fetchone()
    if not row or row["execution_id"] != task["active_execution_id"]:
        raise ValueError("A reconciliation for the current execution is required")
    facts = json.loads(row["facts"])
    current = workspace_snapshot(runner.workroot(task) / "workspace")
    if current["fingerprint"] != facts["workspace"]["fingerprint"]:
        raise ValueError("Workspace changed after reconciliation; reconcile again")
    execution = runner.store.execution(task["active_execution_id"]) if task["active_execution_id"] else None
    if not resource_facts(execution, task)["released"]:
        raise ValueError("Resources are not proven released")
    if kind == "allow_resume" and (not facts["resume_eligible"] or (
        facts["side_effects"] != "UNCHANGED" and not accept_side_effects
    )):
        raise ValueError("Resume needs an ended current session and explicit acknowledgement of uncertain/changed side effects")
    if kind == "verify" and not facts["session"].get("codex_succeeded"):
        raise ValueError("No confirmed successful Codex result; verification cannot infer execution success")
    decision_id = str(uuid.uuid4())
    with runner.store.db:
        runner.store.db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?)", (
            decision_id, task_id, task["active_execution_id"], kind, redact(actor),
            redact(reason), reconciliation_id, time.time()))
    if kind == "fail":
        runner.store.transition(task_id, task["state"], "FAILED", f"Recovery decision {decision_id}")
    elif kind == "verify":
        if not task["active_execution_id"]:
            raise ValueError("Legacy execution needs a manual review path; no execution identity may be invented")
        runner.store.update(task_id, codex_status="SUCCEEDED", result=facts["session"]["agent_result"]["result"])
        runner.store.execution_update(task["active_execution_id"], codex_status="SUCCEEDED", agent_outcome="success",
                                      terminal=json.dumps(facts["session"]["turn"]), phase="VERIFYING")
        runner.store.transition(task_id, task["state"], "VERIFYING", f"Verify saved result only; decision {decision_id}")
        with (runner.root / "logs" / f"verify-{decision_id}.jsonl").open("x") as log:
            runner._verify_task(task_id, log)
    return {"decision_id": decision_id, "kind": kind, "task_state": runner.store.get(task_id)["state"]}


def pre_resume(runner, task, deadline=None):
    source = runner.store.get(task["resume_of"])
    facts = reconcile(runner, source["id"], probe_execution_id=task["active_execution_id"], deadline=deadline)
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("Resume inspection deadline exceeded")
    if not facts["resume_eligible"]:
        raise RuntimeError("Stored session is not safely resumable; reconciliation is required")
    if task["resume_approval"]:
        row = runner.store.db.execute("SELECT * FROM decisions WHERE id=? AND task_id=? AND kind='allow_resume'",
                                      (task["resume_approval"], source["id"])).fetchone()
        if not row or row["execution_id"] != source["active_execution_id"]:
            raise RuntimeError("Resume decision does not match the source execution")
        approved = runner.store.db.execute("SELECT facts FROM reconciliations WHERE id=?",
                                           (row["reconciliation_id"],)).fetchone()
        if not approved or json.loads(approved[0])["workspace"]["fingerprint"] != facts["workspace"]["fingerprint"]:
            raise RuntimeError("Resume approval is stale; workspace must be reconciled again")
    elif facts["side_effects"] != "UNCHANGED":
        raise RuntimeError("Changed or untracked side effects require an explicit recovery decision")
    return facts


def review(runner, task_id, approve, actor, reason):
    if not actor.strip() or not reason.strip():
        raise ValueError("Human review requires actor and reason")
    task = runner.store.get(task_id)
    row = runner.store.db.execute("SELECT * FROM verifications WHERE task_id=? ORDER BY created DESC LIMIT 1", (task_id,)).fetchone()
    if task["state"] != "WAITING_FOR_HUMAN" or not row or row["execution_id"] != task["active_execution_id"]:
        raise ValueError("Task is not waiting for review of its current verification")
    evidence = json.loads(row["evidence"])
    current = workspace_snapshot(runner.workroot(task) / "workspace")
    if not evidence["all_passed"] or current["problems"] or current["fingerprint"] != row["workspace_fingerprint"]:
        raise ValueError("Verification is incomplete or stale; human approval cannot override it")
    decision_id = str(uuid.uuid4())
    state = "COMPLETED" if approve else "FAILED"
    with runner.store.db:
        runner.store.db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?)", (
            decision_id, task_id, task["active_execution_id"], "approve" if approve else "reject",
            redact(actor), redact(reason), None, time.time()))
        runner.store.db.execute("UPDATE tasks SET state=?,human_status=?,error=?,updated=? WHERE id=? AND state='WAITING_FOR_HUMAN'",
                                (state, "APPROVED" if approve else "REJECTED", None if approve else "Human review rejected: " + redact(reason), time.time(), task_id))
        runner.store._transition_log(task_id, "WAITING_FOR_HUMAN", state, f"Human decision {decision_id}", task["active_execution_id"])
        runner.store.db.execute("UPDATE executions SET phase=?,updated=? WHERE id=?",
                                (state, time.time(), task["active_execution_id"]))
    return {"decision_id": decision_id, "state": state}
