#!/usr/bin/env bash
# claude_ask.sh: off-bus, read-only Claude consult for Codex/agy callers.
# Usage: claude_ask.sh --out FILE [--cwd DIR] [--timeout SEC] [--model M] [--effort L] < prompt
# Exit: 0 ANSWERED, 2 timeout, 3 empty, 4 host/hook/mcp/plugin/tool breach on an otherwise ANSWERED run
#       (answer+receipt kept, flagged), 5 protocol/spawn/incomplete-run/arg error (incl. a --timeout that is not a positive
#       integer), 6 refused unsafe dir, 7 answer could not be written to <out> (outcome ANSWER-WRITE-FAILED).
# <out> exists only when ANSWERED; a CANNOT-EVALUATE receipt is written on every exit path once --out is known.
set -u
OUT=""; CWD="$PWD"; TIMEOUT=600; MODEL="${CLAUDE_ASK_MODEL:-opus}"; EFFORT="${CLAUDE_ASK_EFFORT:-high}"
BADARG=""
# Pre-scan for --out so cleanup and the refuse receipt happen whatever the argument order.
_prev=""
for _a in "$@"; do
  if [ "$_prev" = "--out" ]; then OUT="$_a"; fi
  case "$_a" in --out=*) OUT="${_a#--out=}";; esac
  _prev="$_a"
done
while [ $# -gt 0 ]; do
  case "$1" in
    --out) [ $# -ge 2 ] || { BADARG="--out needs a value"; break; }; OUT="$2"; shift 2;;
    --out=*) OUT="${1#--out=}"; shift;;
    --cwd) [ $# -ge 2 ] || { BADARG="--cwd needs a value"; break; }; CWD="$2"; shift 2;;
    --timeout) [ $# -ge 2 ] || { BADARG="--timeout needs a value"; break; }; TIMEOUT="$2"; shift 2;;
    --model) [ $# -ge 2 ] || { BADARG="--model needs a value"; break; }; MODEL="$2"; shift 2;;
    --effort) [ $# -ge 2 ] || { BADARG="--effort needs a value"; break; }; EFFORT="$2"; shift 2;;
    --keep) shift;;
    *) BADARG="unknown arg $1"; break;;
  esac
done
# --timeout must be a positive integer: 0, a negative or a non-numeric value would reach perl's alarm and
# silently remove the deadline (review F6).
if [ -z "$BADARG" ]; then
  case "$TIMEOUT" in
    [1-9]*) case "$TIMEOUT" in *[!0-9]*) BADARG="--timeout must be a positive integer, got: $TIMEOUT";; esac;;
    *) BADARG="--timeout must be a positive integer, got: $TIMEOUT";;
  esac
fi
START="$(python3 -c 'import datetime;print(datetime.datetime.now(datetime.timezone.utc).isoformat())')"
if [ -n "$OUT" ]; then
  mkdir -p "$(dirname "$OUT")" 2>/dev/null && OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
  rm -f "$OUT" "$OUT.receipt.json" "$OUT.partial"
fi
# refuse(code, reason): CANNOT-EVALUATE receipt (when --out is known), then exit
refuse() {
  echo "$2" >&2
  if [ -n "$OUT" ]; then
    python3 - "$OUT" "$START" "$2" "$1" "$EFFORT" "$MODEL" <<'PY'
import json,sys
out,start,reason,code,eff,model=sys.argv[1:7]
json.dump({"adapter":"claude_ask","host":"claude","outcome":"CANNOT-EVALUATE","reason":reason,"exit":int(code),"started_at":start,
 "answer_chars":0,"no_successful_read":True,"files_read":[],"tool_calls":[],"effort_requested":eff,"model_requested":model,
 "host_config_unchanged":None,"usage":None,"effort_observed":None},open(out+".receipt.json","w"),indent=1)
PY
  fi
  exit "$1"
}
[ -z "$BADARG" ] || refuse 5 "$BADARG"
[ -n "$OUT" ] || { echo "--out required" >&2; exit 5; }
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude)}"
[ -n "$CLAUDE_BIN" ] || refuse 5 "claude binary not found"
RAW="$OUT.stream.jsonl"; PROMPT="$(mktemp "$(dirname "$OUT")/.claude_ask_prompt.XXXXXX")"; cat > "$PROMPT"
if [ ! -d "$CWD" ]; then rm -f "$PROMPT"; refuse 6 "cwd not a directory: $CWD"; fi
CWD="$(cd "$CWD" && pwd -P)"
UNSAFE=0
case "$CWD" in "$HOME"|"$HOME/.claude"*|"$HOME/.codex"*|"$HOME/.gemini"*) UNSAFE=1;; esac
# the same dirs by their REAL path: a symlinked ~/.claude (dotfiles style) must not let a call run inside its target
for D in "$HOME" "$HOME/.claude" "$HOME/.codex" "$HOME/.gemini" "$HOME/.local/state/mycelium"; do
  RD="$(cd "$D" 2>/dev/null && pwd -P)"
  [ -n "$RD" ] || continue
  if [ "$D" = "$HOME" ]; then [ "$CWD" != "$RD" ] || UNSAFE=1; else case "$CWD" in "$RD"|"$RD"/*) UNSAFE=1;; esac; fi
done
if [ "$UNSAFE" = 1 ]; then rm -f "$PROMPT"; refuse 6 "refusing unsafe cwd $CWD"; fi
# Isolation (D7): --safe-mode turns off plugins, hooks, MCP, CLAUDE.md, skills; --restricted ignores
# user/project/local settings and confines file tools to the working directory; empty strict MCP config;
# read-only tools; hook events included in the stream so a breach is observable.
cfg_hash() { shasum -a 256 "$HOME/.claude/settings.json" "$HOME/.claude/settings.local.json" 2>/dev/null | awk '{print $1}' | tr '\n' ','; }
H0="$(cfg_hash)"
( cd "$CWD" && perl -e 'alarm shift; exec @ARGV' -- "$TIMEOUT" "$CLAUDE_BIN" -p \
    --model "$MODEL" --effort "$EFFORT" \
    --output-format stream-json --verbose --include-hook-events \
    --safe-mode --restricted \
    --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
    --tools "Read,Grep,Glob" --allowedTools "Read,Grep,Glob" \
    --permission-mode default --no-session-persistence --disable-slash-commands \
    < "$PROMPT" > "$RAW" 2> "$OUT.stderr" ); RC=$?
H1="$(cfg_hash)"
python3 - "$OUT" "$RAW" "$START" "$RC" "$MODEL" "$EFFORT" "$CWD" "$([ "$H0" = "$H1" ] && echo 1 || echo 0)" "${CLAUDE_BIN}" <<'PY'
import json,sys,datetime,subprocess,os
out,raw,start,rc,model,effort,cwd,unchanged,cbin=sys.argv[1:10]; rc=int(rc)
init=None; res=None; text=[]; tcs={}; denials=[]; hookev=[]; sid=None; used_model=None
for l in open(raw,errors="replace"):
    try: m=json.loads(l)
    except Exception: continue
    t=m.get("type"); st=m.get("subtype")
    if t=="system" and st=="init": init=m; sid=m.get("session_id"); used_model=m.get("model")
    elif t=="system" and "hook" in str(st).lower(): hookev.append({"subtype":st,"name":m.get("hook_name") or m.get("hook_event")})
    elif t=="assistant":
        for c in (m.get("message") or {}).get("content") or []:
            if c.get("type")=="text": text.append(c["text"])
            elif c.get("type")=="tool_use":
                inp=c.get("input") or {}; p=inp.get("file_path") or inp.get("path")
                tcs[c["id"]]={"kind":c.get("name"),"title":json.dumps(inp)[:200],"status":None,"paths":[p] if p else []}
    elif t=="user":
        for c in (m.get("message") or {}).get("content") or []:
            if isinstance(c,dict) and c.get("type")=="tool_result" and c.get("tool_use_id") in tcs:
                tcs[c["tool_use_id"]]["status"]="error" if c.get("is_error") else "completed"
    elif t=="result": res=m
ok_result = res is not None and res.get("subtype")=="success" and res.get("is_error") is False and rc==0
ans = ((res or {}).get("result") or "") if ok_result else ""   # never from narration
partial = "".join(text)
if res: denials=res.get("permission_denials") or []
files=[]; good=0
for e in tcs.values():
    if e["kind"] in ("Read","Grep","Glob") and e["status"]=="completed":
        good+=1
        for p in e["paths"]:
            p=p if os.path.isabs(p) else os.path.join(cwd,p)
            if p not in files: files.append(p)
mcp=(init or {}).get("mcp_servers"); tools=(init or {}).get("tools")
plugins=(init or {}).get("plugins")
nonbuiltin=[p for p in (plugins or []) if not (isinstance(p,dict) and (p.get("path")=="builtin" or str(p.get("source","")).endswith("@builtin")))]
sock=(init or {}).get("messaging_socket_path")
# Nullable usage / observed effort: null (never 0) when the stream does not carry them.
_u=(res or {}).get("usage") if isinstance((res or {}).get("usage"),dict) else None
usage=None if _u is None else {k:_u.get(k) for k in ("input_tokens","cache_read_input_tokens","cache_creation_input_tokens","output_tokens")}
effort_obs=(init or {}).get("effort") or (init or {}).get("reasoning_effort") or None
mcp_ok = init is not None and mcp==[]
tools_ok = init is not None and set(tools or [])=={"Read","Grep","Glob"}
code=0; reason="ok"
if rc==142 or rc==-14: code=2; reason="timeout (SIGALRM)"
elif init is None: code=5; reason="no init event in stream (spawn/auth failure?)"
elif not ok_result: code=5; reason="no successful result event (rc=%d, result subtype=%s, is_error=%s)"%(rc,(res or {}).get("subtype"),(res or {}).get("is_error"))
elif not ans.strip(): code=3; reason="empty answer"
breach=[]
if init is not None and not mcp_ok: breach.append("mcp_servers not empty: %s"%json.dumps(mcp))
if init is not None and not tools_ok: breach.append("tools not exactly Read/Grep/Glob: %s"%json.dumps(tools))
if nonbuiltin: breach.append("non-builtin plugin at init: %s"%json.dumps(nonbuiltin)[:200])
if hookev: breach.append("hook events observed: %s"%json.dumps(hookev)[:200])
if unchanged!="1": breach.append("~/.claude settings hash changed")
if breach and code==0: code=4; reason="; ".join(breach)
elif breach: reason+=" | "+"; ".join(breach)
answered = code in (0,4) and bool(ans.strip())
now=datetime.datetime.now(datetime.timezone.utc)
s=datetime.datetime.fromisoformat(start)
try: ver=subprocess.run([cbin,"--version"],capture_output=True,text=True,timeout=20).stdout.strip()
except Exception: ver=None
outcome="ANSWERED" if answered else "CANNOT-EVALUATE"
try:
    if answered: open(out,"w").write(ans)
    elif (ans or partial).strip(): open(out+".partial","w").write(ans or partial)
except OSError as e:
    reason=(reason or "")+" | write failed: "+str(e)
    if answered:  # <out> exists only when ANSWERED: an unwritable answer is neither ANSWERED nor exit 0 (review F5)
        outcome="ANSWER-WRITE-FAILED"; code=7
rec={"adapter":"claude_ask","host":"claude","claude_version":ver,"model_reported":used_model,"model_requested":model,"effort_requested":effort,
 "session_id":sid,"started_at":start,"ended_at":now.isoformat(),"duration_s":round((now-s).total_seconds(),3),
 "outcome":outcome,"reason":reason,"exit":code,"answer_chars":len(ans),
 "tool_calls":list(tcs.values()),"files_read":files,"no_successful_read":good==0,
 "permissions_rejected":[{"kind":d.get("tool_name"),"title":json.dumps(d.get("tool_input"))[:200]} for d in denials],
 "permissions_granted":[],"host_config_unchanged":unchanged=="1","gemini_dir":None,"kept":True,
 "mcp_servers_at_init":mcp,"tools_at_init":tools,"plugins_at_init":plugins,"messaging_socket_path":sock,
 "isolation_flags":"--safe-mode --restricted --strict-mcp-config --mcp-config {} --tools Read,Grep,Glob --include-hook-events",
 "hook_events_seen":hookev,"cwd":cwd,"stream":raw,"usage":usage,"effort_observed":effort_obs}
json.dump(rec,open(out+".receipt.json","w"),indent=1)
print(json.dumps({"outcome":rec["outcome"],"exit":code,"receipt":out+".receipt.json"}))
sys.exit(code)
PY
CODE=$?; rm -f "$PROMPT"; exit $CODE
