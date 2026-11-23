#!/usr/bin/env python3
"""agy_ask: off-bus, read-only consult of Antigravity (agy) over ACP. Stdlib only.

Prompt on stdin. Exit codes: 0 ANSWERED, 2 timeout, 3 empty answer,
4 host config changed during an otherwise ANSWERED run (answer+receipt kept, flagged),
5 ACP/protocol error or stopReason other than end_turn, 6 refused unsafe dirs,
7 the answer could not be written to <out> (outcome ANSWER-WRITE-FAILED; no answer file exists).
A run with no complete answer never exits 0. A CANNOT-EVALUATE receipt is written on every
exit path once --out is known. <out> exists only when ANSWERED; partial text goes to <out>.partial.
"""
import argparse, datetime, glob, hashlib, json, os, re, shutil, subprocess, sys, tempfile, threading, time

GRANT_KINDS = {"read", "search"}
DENY_WORDS = {"write", "edit", "modify", "replace", "patch", "save", "create", "delete", "move", "rename",
              "send", "execute", "run", "command"}
AGY = os.environ.get("AGY_BIN") or shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
if not os.path.exists(AGY) and os.path.exists(os.path.expanduser("~/.gemini/bin/agy")):
    AGY = os.path.expanduser("~/.gemini/bin/agy")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def host_hashes():
    out = {}
    for p in sorted(glob.glob(os.path.expanduser("~/.gemini/config/*.json"))):
        try:
            out[p] = hashlib.sha256(open(p, "rb").read()).hexdigest()
        except OSError as e:
            out[p] = "ERR:" + str(e)
    return out


def title_words(title):
    return set(w for w in re.split(r"[^a-z0-9]+", str(title or "").lower()) if w)


def decide(tool_call, options):
    """Return (optionId or None, granted_bool). Allowlist by ACP kind only; deny words win."""
    kind = str((tool_call or {}).get("kind") or "").lower()
    ok = kind in GRANT_KINDS and not (title_words((tool_call or {}).get("title")) & DENY_WORDS)
    if ok:
        for o in options:
            if o.get("kind") == "allow_once":
                return o.get("optionId"), True
        return None, False  # no allow_once: never use allow_always
    for k in ("reject_once", "reject_always"):
        for o in options:
            if o.get("kind") == k:
                return o.get("optionId"), False
    return None, False  # caller sends cancelled


def unsafe_path(path):
    """True if path (existing or not) is, or would be, inside ~/.gemini. samefile walk + lowercase prefix."""
    home_g = os.path.expanduser("~/.gemini")
    rp = os.path.realpath(path)
    if rp.lower() == os.path.realpath(home_g).lower() or rp.lower().startswith(os.path.realpath(home_g).lower() + os.sep):
        return True
    cur = rp
    while True:
        if os.path.exists(cur) and os.path.exists(home_g):
            try:
                if os.path.samefile(cur, home_g):
                    return True
            except OSError:
                pass
        nxt = os.path.dirname(cur)
        if nxt == cur:
            return False
        cur = nxt


def main():
    t0 = time.time()
    out_arg = None
    for i, x in enumerate(sys.argv):
        if x == "--out" and i + 1 < len(sys.argv):
            out_arg = sys.argv[i + 1]
        elif x.startswith("--out="):
            out_arg = x.split("=", 1)[1]
    out = os.path.abspath(out_arg) if out_arg else None
    rcpt_path = (out + ".receipt.json") if out else None
    r = {"adapter": "agy_ask", "host": "antigravity", "agy_version": None, "model_reported": None,
         "session_id": None, "started_at": now(), "ended_at": None, "duration_s": None,
         "outcome": "CANNOT-EVALUATE", "reason": None, "answer_chars": 0, "stop_reason": None,
         "tool_calls": [], "files_read": [], "no_successful_read": True, "permissions_rejected": [],
         "permissions_granted": [], "host_config_unchanged": None, "gemini_dir": None, "kept": False}
    before = host_hashes()
    keep = [False]
    tcs = {}
    proc = [None]

    def finish(code, reason, answer=""):
        r["ended_at"] = now(); r["duration_s"] = round(time.time() - t0, 3)
        r["tool_calls"] = list(tcs.values())
        good = [e for e in tcs.values() if e["kind"] in GRANT_KINDS and e["status"] == "completed"]
        files = []
        for e in good:
            for pth in e["paths"]:
                if pth not in files:
                    files.append(pth)
        r["files_read"] = files
        r["no_successful_read"] = len(good) == 0
        after = host_hashes()
        r["host_config_unchanged"] = (after == before)
        reason = str(reason)
        if not r["host_config_unchanged"]:
            diff = {k: [before.get(k), after.get(k)] for k in set(before) | set(after) if before.get(k) != after.get(k)}
            r["host_config_diff"] = diff
            r["host_config_changed_files"] = sorted(diff)
            if code == 0:
                code = 4
            reason = "host config changed: " + reason
        answered = code in (0, 4) and bool(answer.strip())
        r["reason"] = reason; r["answer_chars"] = len(answer)
        r["outcome"] = "ANSWERED" if answered else "CANNOT-EVALUATE"
        if out:
            try:
                if answered:
                    open(out, "w").write(answer)
                elif answer.strip():
                    open(out + ".partial", "w").write(answer)
            except OSError as e:
                r["reason"] += " | write failed: " + str(e)
                if answered:
                    # contract: <out> exists only when ANSWERED. The answer could not be written, so
                    # the run is neither ANSWERED nor exit 0 (review F5).
                    r["outcome"] = "ANSWER-WRITE-FAILED"
                    code = 7
        r["kept"] = bool(keep[0]) and bool(r["gemini_dir"])
        if r["gemini_dir"] and not keep[0]:
            shutil.rmtree(r["gemini_dir"], ignore_errors=True)
        if rcpt_path:
            try:
                json.dump(r, open(rcpt_path, "w"), indent=1)
            except OSError as e:
                print("receipt write failed: %s" % e, file=sys.stderr)
        print(json.dumps({"outcome": r["outcome"], "exit": code, "receipt": rcpt_path}))
        sys.exit(code)

    class AP(argparse.ArgumentParser):
        def error(self, message):
            print(message, file=sys.stderr)
            finish(5, "argument error: " + message)

    ap = AP(allow_abbrev=False)  # the --out pre-scan must see the same option spelling argparse accepts
    ap.add_argument("--out", required=True)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--keep", action="store_true")
    if out:  # D5: no stale files may survive a failed run
        for pth in (out, out + ".receipt.json", out + ".partial"):
            try:
                os.unlink(pth)
            except FileNotFoundError:
                pass
            except OSError as e:
                print("cannot remove %s: %s" % (pth, e), file=sys.stderr)
    a = ap.parse_args()
    keep[0] = a.keep
    prompt = sys.stdin.read()
    if not os.path.isdir(a.cwd):
        finish(6, "cwd not a directory: " + a.cwd)
    # D6: fixed safe base, never $TMPDIR; refuse anything inside ~/.gemini (case-insensitive, samefile walk)
    base = os.environ.get("AGY_ASK_BASE") or "/private/tmp/agy_ask-%d" % os.getuid()  # AGY_ASK_BASE: test seam
    if unsafe_path(base):
        finish(6, "unsafe base dir: " + base)
    try:
        os.makedirs(base, mode=0o700, exist_ok=True)
        gdir = os.path.realpath(tempfile.mkdtemp(prefix="run_", dir=base))
    except OSError as e:
        finish(6, "cannot create gemini dir: %s" % e)
    if (not os.path.isabs(gdir)) or unsafe_path(gdir):
        shutil.rmtree(gdir, ignore_errors=True)
        finish(6, "unsafe gemini dir: " + gdir)
    r["gemini_dir"] = gdir
    try:
        r["agy_version"] = subprocess.run([AGY, "--version"], capture_output=True, text=True, timeout=30).stdout.strip() or None
    except Exception as e:
        r["agy_version"] = "ERR:" + str(e)
    cmd = [AGY, "acp", "--gemini-dir", gdir, "--app-data-dir", "appdata"]
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, cwd=gdir)
    except Exception as e:
        finish(5, "spawn failed: " + str(e))
    q = []; cv = threading.Condition()

    def rd():
        for l in p.stdout:
            try:
                m = json.loads(l)
            except Exception:
                continue
            with cv:
                q.append(m); cv.notify_all()
        with cv:
            q.append({"_eof": True}); cv.notify_all()

    def er():
        for _ in p.stderr:
            pass
    threading.Thread(target=rd, daemon=True).start(); threading.Thread(target=er, daemon=True).start()
    nid = [0]; deadline = t0 + a.timeout
    cursor = [0]  # global queue cursor: a later call never re-collects an earlier turn's messages

    def write(m):
        try:
            p.stdin.write(json.dumps(m) + "\n"); p.stdin.flush()
        except Exception:
            pass

    def note_tc(u):
        tid = u.get("toolCallId")
        e = tcs.setdefault(tid, {"kind": None, "title": None, "status": None, "paths": []})
        for k in ("kind", "title", "status"):
            if u.get(k):
                e[k] = u[k]
        for loc in u.get("locations") or []:
            if loc.get("path") and loc["path"] not in e["paths"]:
                e["paths"].append(loc["path"])
        ri = u.get("rawInput")
        if isinstance(ri, dict):
            for k in ("path", "file_path", "AbsolutePath", "absolute_path", "Path"):
                v = ri.get(k)
                if isinstance(v, str) and v not in e["paths"]:
                    e["paths"].append(v)

    def call(method, params, chunks=None):
        nid[0] += 1; i = nid[0]
        write({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        resp = None; linger = None
        while True:
            with cv:
                while cursor[0] < len(q):
                    m = q[cursor[0]]; cursor[0] += 1
                    if m.get("_eof"):
                        return {"error": {"message": "agy exited"}}
                    meth = m.get("method")
                    if meth == "session/update":
                        u = m["params"].get("update", {})
                        su = u.get("sessionUpdate")
                        if su == "agent_message_chunk" and chunks is not None:
                            chunks.append((u.get("content") or {}).get("text", ""))
                        elif su in ("tool_call", "tool_call_update"):
                            note_tc(u)
                    elif meth == "session/request_permission":
                        pr = m.get("params", {}); tc = pr.get("toolCall") or {}
                        note_tc(tc)
                        oid, granted = decide(tc, pr.get("options") or [])
                        rec = {"kind": tc.get("kind"), "title": tc.get("title")}
                        (r["permissions_granted"] if granted else r["permissions_rejected"]).append(rec)
                        res = {"outcome": {"outcome": "selected", "optionId": oid}} if oid else {"outcome": {"outcome": "cancelled"}}
                        write({"jsonrpc": "2.0", "id": m["id"], "result": res})
                    elif "method" in m and "id" in m:
                        write({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "unsupported"}})
                    elif m.get("id") == i:
                        resp = m
                if resp is not None:
                    # linger briefly so late chunks land in THIS turn, not the next
                    linger = linger or time.time() + 0.3
                    if time.time() >= linger:
                        return resp
                    cv.wait(0.1); continue
                rem = deadline - time.time()
                if rem <= 0:
                    return {"timeout": True}
                cv.wait(min(0.5, rem))

    def done(code, reason, ans=""):
        try:
            p.terminate(); p.wait(timeout=5)
        except Exception:
            try: p.kill()
            except Exception: pass
        finish(code, reason, ans)

    ini = call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    if ini.get("timeout"): done(2, "timeout in initialize")
    if "error" in ini or "result" not in ini: done(5, "initialize error: " + json.dumps(ini.get("error")))
    ns = call("session/new", {"cwd": os.path.abspath(a.cwd), "mcpServers": []})
    if ns.get("timeout"): done(2, "timeout in session/new")
    if "error" in ns or "result" not in ns: done(5, "session/new error: " + json.dumps(ns.get("error")))
    res = ns["result"]
    r["session_id"] = res.get("sessionId")
    mdl = res.get("models") or {}
    r["model_reported"] = mdl.get("currentModelId") or res.get("currentModelId")
    chunks = []
    pr = call("session/prompt", {"sessionId": r["session_id"], "prompt": [{"type": "text", "text": prompt}]}, chunks)
    if pr.get("timeout"): done(2, "timeout in session/prompt after %ss" % a.timeout, "".join(chunks))
    if "error" in pr: done(5, "session/prompt error: " + json.dumps(pr.get("error")), "".join(chunks))
    stop = (pr.get("result") or {}).get("stopReason")
    r["stop_reason"] = stop
    ans = "".join(chunks)
    second = os.environ.get("AGY_ASK_TEST_SECOND_PROMPT")  # test seam: second turn on the same session
    if second is not None:
        c2 = []
        pr2 = call("session/prompt", {"sessionId": r["session_id"], "prompt": [{"type": "text", "text": second}]}, c2)
        r["test_second_turn_text"] = "".join(c2)
        r["test_second_turn_stop"] = (pr2.get("result") or {}).get("stopReason")
    if stop != "end_turn":
        done(5, "stopReason=%s (not end_turn)" % stop, ans)
    if not ans.strip(): done(3, "empty answer (stopReason=%s)" % stop)
    done(0, "stopReason=%s" % stop, ans)


if __name__ == "__main__":
    main()
