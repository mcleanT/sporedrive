#!/usr/bin/env python3
"""Fake agy: `--version` and `acp ...` JSON-RPC server.
FAKE_AGY_MODE: normal | hang | empty | kind_other | read_write_title | max_tokens | refusal |
               twoturn | failed_read | partial_hang
FAKE_AGY_TOUCH=<path>: rewrite that file at session/new (simulates a host-config change).
FAKE_AGY_LOG=<path>: write argv as JSON.
FAKE_AGY_ENVDUMP=<path>: write the process environment as JSON (env-allowlist test)."""
import json, os, sys, time
_d = os.environ.get("FAKE_AGY_ENVDUMP")
if _d:
    json.dump(dict(os.environ), open(_d, "w"))
if "--version" in sys.argv:
    print("fake-agy 0.0"); sys.exit(0)
mode = os.environ.get("FAKE_AGY_MODE", "normal")
log = os.environ.get("FAKE_AGY_LOG")
if log:
    open(log, "w").write(json.dumps(sys.argv[1:]))
def send(m): sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
def upd(u, sid="S1"): send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})
def chunk(t): upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": t}})
def status(tid, st): upd({"sessionUpdate": "tool_call_update", "toolCallId": tid, "status": st})
rid = [100]
def ask_perm(kind, title, path):
    rid[0] += 1
    tid = "t%d" % rid[0]
    send({"jsonrpc": "2.0", "id": rid[0], "method": "session/request_permission", "params": {"sessionId": "S1",
          "toolCall": {"toolCallId": tid, "kind": kind, "title": title, "locations": [{"path": path}]},
          "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}, {"optionId": "rej", "name": "Reject", "kind": "reject_once"}]}})
    resp = json.loads(sys.stdin.readline())
    sel = resp.get("result", {}).get("outcome", {}).get("optionId")
    status(tid, "completed" if sel == "allow" else "failed")
    return sel
def end(m, stop="end_turn"): send({"jsonrpc": "2.0", "id": m["id"], "result": {"stopReason": stop}})
turn = [0]
for line in sys.stdin:
    m = json.loads(line)
    meth = m.get("method")
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"agentInfo": {"name": "fake"}}})
    elif meth == "session/new":
        t = os.environ.get("FAKE_AGY_TOUCH")
        if t:
            open(t, "w").write("changed-%s" % time.time())
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"sessionId": "S1", "models": {"currentModelId": "fake-model"}}})
    elif meth == "session/prompt":
        turn[0] += 1
        if mode == "hang":
            time.sleep(600)
        elif mode == "partial_hang":
            chunk("PARTIAL"); time.sleep(600)
        elif mode == "empty":
            end(m)
        elif mode == "kind_other":
            a = ask_perm("other", "Modify review_me.py", "/work/review_me.py")
            b = ask_perm("other", "apply_patch review_me.py", "/work/review_me.py")
            chunk("O|A=%s|B=%s" % (a, b)); end(m)
        elif mode == "read_write_title":
            a = ask_perm("read", "Write file x", "/work/x")
            chunk("RW|A=%s" % a); end(m)
        elif mode in ("max_tokens", "refusal"):
            ask_perm("read", "Read review_me.py", "/work/review_me.py")
            chunk("some text"); end(m, mode)
        elif mode == "twoturn":
            chunk("T%d-a" % turn[0]); chunk("T%d-b" % turn[0]); end(m)
            time.sleep(0.05); chunk("T%d-late" % turn[0])
        elif mode == "failed_read":
            upd({"sessionUpdate": "tool_call", "toolCallId": "tf", "kind": "read", "title": "Read missing.py",
                 "status": "pending", "locations": [{"path": "/work/missing.py"}]})
            status("tf", "failed")
            chunk("could not read"); end(m)
        else:  # normal
            chunk("A")
            w = ask_perm("edit", "Edit edit_target.py", "/work/edit_target.py")
            chunk("B")
            r = ask_perm("read", "Read review_me.py", "/work/review_me.py")
            chunk("|W=%s|R=%s|" % (w, r))
            end(m)
            time.sleep(0.05); chunk("LATE")
