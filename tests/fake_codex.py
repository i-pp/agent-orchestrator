"""Deterministic persisted protocol peer; no Codex/model calls."""
import json
import os
import time
from pathlib import Path
import subprocess
import sys
import uuid

mode = sys.argv[1]
Path(sys.argv[2]).with_name("server.pid").write_text(str(os.getpid()))
history_path = Path(sys.argv[2]).with_name("fake-history.json")
history = json.loads(history_path.read_text()) if history_path.exists() else {}
thread_id = None
turn_id = None

def save():
    history_path.write_text(json.dumps(history))

def emit(value):
    print(json.dumps(value), flush=True)

def notification(method, params):
    emit({"method": method, "params": params})

for line in sys.stdin:
    message = json.loads(line)
    method = message["method"]
    params = message.get("params", {})
    if "id" not in message:
        continue
    rid = message["id"]
    if method == "initialize":
        result = {"userAgent": "offline-test"}
    elif method == "account/read":
        result = {"account": {"type": "apiKey" if mode == "api-key" else "chatgpt"}}
    elif method in ["thread/start", "thread/resume"]:
        if mode == "rpc-error":
            emit({"id": rid, "error": {"code": -32602, "message": "invalid thread parameters"}})
            continue
        thread_id = params.get("threadId") or "mock-session-" + str(uuid.uuid4())
        history.setdefault(thread_id, [])
        save()
        if mode == "previous-usage" and method == "thread/resume" and history[thread_id]:
            notification("thread/tokenUsage/updated", {"threadId":thread_id, "turnId":history[thread_id][0]["id"], "tokenUsage":{"last":{"totalTokens":9999}}})
        result = {"thread": {"id": thread_id}, "sandbox": {"type": "workspaceWrite" if params.get("sandbox") == "workspace-write" else "readOnly"}, "approvalPolicy": "never"}
    elif method == "turn/start":
        thread_id = params["threadId"]
        turn_id = "mock-turn-" + str(uuid.uuid4())
        turn = {"id": turn_id, "status": "inProgress", "items": [], "error": None}
        history[thread_id].insert(0, turn)
        save()
        emit({"id": rid, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
        if mode == "resource-cpu":
            while True: pass
        if mode == "resource-memory":
            values = []
            while True: values.append(bytearray(4 * 1024 * 1024))
        if mode == "resource-pids":
            children = []
            try:
                for _ in range(40):
                    children.append(subprocess.Popen([sys.executable, "-c", "import time;time.sleep(15)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            except OSError:
                pass
            time.sleep(15)
            continue
        if mode == "resource-disk":
            for n in range(100):
                (Path.cwd() / ("disk-" + str(n))).write_bytes(b"x" * 65536)
            time.sleep(15)
            continue
        if mode == "bad-shape":
            notification("item/completed", 7)
            continue
        if mode in ["malformed", "truncated", "truncated-valid"]:
            if mode == "malformed": print("{invalid-json", flush=True)
            else:
                sys.stdout.write('{"method":"turn/completed"' if mode == "truncated" else json.dumps({"method":"turn/completed","params":{"threadId":thread_id,"turn":{"id":turn_id,"status":"completed"}}}))
                sys.stdout.flush()
                sys.exit(0)
            continue
        if mode == "disconnect":
            sys.exit(0)
        if mode == "crash":
            sys.exit(17)
        if mode in ["hang-no-child", "complete-on-interrupt"]:
            continue
        if mode in ["hang", "hang-detached"]:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=mode == "hang-detached")
            Path(sys.argv[2]).write_text(str(child.pid))
            continue
        if mode == "slow-normal":
            time.sleep(1)
        if mode == "denied":
            item = {"type":"commandExecution","id":"cmd-1","status":"declined","exitCode":1}
            turn["items"].append(item)
            notification("item/completed", {"threadId":thread_id,"turnId":turn_id,"item":item})
        item = {"type":"agentMessage","id":"msg-1","phase":"final_answer", "text":json.dumps({
            "outcome":"failure" if mode == "reported-failure" else "success", "result":"mock result"})}
        turn["items"].append(item)
        turn["status"] = "completed"
        save()
        if mode == "disconnect-after-complete":
            sys.exit(0)
        notification("item/completed", {"threadId":thread_id,"turnId":"unexpected-turn" if mode == "wrong-turn" else turn_id,"item":item})
        notification("thread/tokenUsage/updated", {"threadId":thread_id,"turnId":turn_id,"tokenUsage":{"last":{"totalTokens":7}}})
        notification("turn/completed", {"threadId":thread_id,"turn":{ "id":turn_id,"status":"completed"}})
        continue
    elif method == "thread/read":
        thread_id = params["threadId"]
        if thread_id not in history:
            emit({"id":rid,"error":{"code":-32602,"message":"thread not found"}})
            continue
        rows = history[thread_id]
        result = {"thread":{"id":thread_id,"status":{"type":"active" if rows and rows[0]["status"] == "inProgress" else "idle"}}}
    elif method == "thread/turns/list":
        result = {"data":history.get(params["threadId"],[]),"nextCursor":None}
    elif method == "turn/interrupt":
        if mode == "complete-on-interrupt":
            item = {"type":"agentMessage","id":"race-msg","phase":"final_answer",
                    "text":json.dumps({"outcome":"success","result":"completed at cancellation boundary"})}
            history[thread_id][0].update(status="completed",items=[item])
            save()
            emit({"id":rid,"result":{}})
            notification("item/completed", {"threadId":thread_id,"turnId":turn_id,"item":item})
            notification("turn/completed", {"threadId":thread_id,"turn":{"id":turn_id,"status":"completed"}})
            continue
        if thread_id in history and history[thread_id]:
            history[thread_id][0]["status"] = "interrupted"
            save()
        emit({"id":rid,"result":{}})
        notification("turn/completed", {"threadId":thread_id,"turn":{"id":turn_id,"status":"interrupted"}})
        continue
    elif method == "thread/unsubscribe":
        result = {"status":"unsubscribed"}
    else:
        emit({"id":rid,"error":{"code":-32601,"message":"unknown method"}})
        continue
    emit({"id":rid,"result":result})
