#!/usr/bin/env bash
# codex_call.sh: off-bus, read-only, ephemeral Codex call for `mycelium-coord consult call` (design TWO-MODES v1.2 2.2).
# Usage: codex_call.sh --out FILE [--cwd DIR] [--timeout SEC] [--model M] [--effort L] < prompt
# Runs: codex exec --ignore-user-config --ephemeral --skip-git-repo-check --sandbox read-only --disable hooks --json
#       -C <cwd> -m M -c model_reasoning_effort=E -o <last> -      (prompt on stdin; direct binary, never the cmux shim)
# Env: CODEX_BIN overrides the binary (default /opt/homebrew/bin/codex; tests).
# Exit: 0 ANSWERED, 2 timeout, 3 empty answer, 4 host config changed on an otherwise ANSWERED run (answer+receipt kept,
#       flagged), 5 protocol/spawn/incomplete-run/arg error/missing binary, 6 refused unsafe cwd, 7 answer could not be
#       written to <out> (outcome ANSWER-WRITE-FAILED). Same meanings as claude_ask.sh / agy_ask.py (README).
# <out> exists only when ANSWERED; a CANNOT-EVALUATE receipt is written on every exit path once --out is known.
# Side files next to <out>: .stream.jsonl (raw --json stream), .stderr, .last (codex -o file), .receipt.json.
set -u
OUT=""; CWD="$PWD"; TIMEOUT=600; MODEL="${CODEX_CALL_MODEL:-gpt-5.6-sol}"; EFFORT="${CODEX_CALL_EFFORT:-medium}"
BADARG=""
_prev=""
for _a in "$@"; do
  if [ "$_prev" = "--out" ]; then OUT="$_a"; fi
  case "$_a" in --out=*) OUT="${_a#--out=}";; esac
  _prev="$_a"
done
while [ $# -gt 0 ]; do
  case "$1" in
    --out) [ $# -ge 2 ] || { BADARG="argument error: --out needs a value"; break; }; OUT="$2"; shift 2;;
    --out=*) OUT="${1#--out=}"; shift;;
    --cwd) [ $# -ge 2 ] || { BADARG="argument error: --cwd needs a value"; break; }; CWD="$2"; shift 2;;
    --timeout) [ $# -ge 2 ] || { BADARG="argument error: --timeout needs a value"; break; }; TIMEOUT="$2"; shift 2;;
    --model) [ $# -ge 2 ] || { BADARG="argument error: --model needs a value"; break; }; MODEL="$2"; shift 2;;
    --effort) [ $# -ge 2 ] || { BADARG="argument error: --effort needs a value"; break; }; EFFORT="$2"; shift 2;;
    --keep) shift;;
    *) BADARG="argument error: unknown arg $1"; break;;
  esac
done
if [ -z "$BADARG" ]; then
  case "$TIMEOUT" in
    [1-9]*) case "$TIMEOUT" in *[!0-9]*) BADARG="argument error: --timeout must be a positive integer, got: $TIMEOUT";; esac;;
    *) BADARG="argument error: --timeout must be a positive integer, got: $TIMEOUT";;
  esac
fi
START="$(python3 -c 'import datetime;print(datetime.datetime.now(datetime.timezone.utc).isoformat())')"
if [ -n "$OUT" ]; then
  mkdir -p "$(dirname "$OUT")" 2>/dev/null && OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
  rm -f "$OUT" "$OUT.receipt.json" "$OUT.partial" "$OUT.last"
fi
refuse() {
  echo "$2" >&2
  if [ -n "$OUT" ]; then
    python3 - "$OUT" "$START" "$2" "$1" "$EFFORT" "$MODEL" <<'PY'
import json,sys
out,start,reason,code,eff,model=sys.argv[1:7]
json.dump({"adapter":"codex_call","host":"codex","outcome":"CANNOT-EVALUATE","reason":reason,"exit":int(code),"started_at":start,
 "answer_chars":0,"files_read":[],"tool_calls":[],"effort_requested":eff,"model_requested":model,
 "host_config_unchanged":None,"usage":None,"effort_observed":None},open(out+".receipt.json","w"),indent=1)
PY
  fi
  exit "$1"
}
[ -z "$BADARG" ] || refuse 5 "$BADARG"
[ -n "$OUT" ] || { echo "--out required" >&2; exit 5; }
CODEX_BIN="${CODEX_BIN:-/opt/homebrew/bin/codex}"
[ -x "$CODEX_BIN" ] || refuse 5 "codex binary not found: $CODEX_BIN"
RAW="$OUT.stream.jsonl"; LAST="$OUT.last"; PROMPT="$(mktemp "${TMPDIR:-/tmp}/codex_call_prompt.XXXXXX")"; cat > "$PROMPT"
if [ ! -d "$CWD" ]; then rm -f "$PROMPT"; refuse 6 "cwd not a directory: $CWD"; fi
CWD="$(cd "$CWD" && pwd -P)"
case "$CWD" in "$HOME"|"$HOME/.claude"*|"$HOME/.codex"*|"$HOME/.gemini"*) rm -f "$PROMPT"; refuse 6 "refusing unsafe cwd $CWD";; esac
cfg_hash() { shasum -a 256 "$HOME/.codex/config.toml" "$HOME/.codex/hooks.json" 2>/dev/null | awk '{print $1}' | tr '\n' ','; }
H0="$(cfg_hash)"
( cd "$CWD" && perl -e 'alarm shift; exec @ARGV' -- "$TIMEOUT" "$CODEX_BIN" exec \
    --ignore-user-config --ephemeral --skip-git-repo-check --sandbox read-only --disable hooks --json \
    -C "$CWD" -m "$MODEL" -c "model_reasoning_effort=$EFFORT" -o "$LAST" - \
    < "$PROMPT" > "$RAW" 2> "$OUT.stderr" ); RC=$?
H1="$(cfg_hash)"
python3 - "$OUT" "$RAW" "$LAST" "$START" "$RC" "$MODEL" "$EFFORT" "$CWD" "$([ "$H0" = "$H1" ] && echo 1 || echo 0)" "$CODEX_BIN" <<'PY'
import json,sys,datetime,subprocess,os,re
out,raw,last,start,rc,model,effort,cwd,unchanged,cbin=sys.argv[1:11]; rc=int(rc)
thread=None; completed=False; usage=None; cmds=[]; errors=[]
try: lines=open(raw,errors="replace").read().splitlines()
except OSError: lines=[]
for l in lines:
    try: m=json.loads(l)
    except Exception: continue
    t=m.get("type")
    if t=="thread.started": thread=m.get("thread_id")
    elif t=="turn.completed":
        completed=True
        u=m.get("usage")
        if isinstance(u,dict):  # null (never 0) for a field the stream does not carry
            usage={k:u.get(k) for k in ("input_tokens","cached_input_tokens","output_tokens")}
    elif t in ("error","turn.failed"): errors.append(str(m.get("message") or m.get("error") or m)[:300])
    elif t=="item.completed":
        it=m.get("item") or {}
        if it.get("type")=="command_execution":
            cmds.append({"kind":"command_execution","title":str(it.get("command"))[:300],"status":it.get("status"),"exit_code":it.get("exit_code")})
try: ans=open(last,errors="replace").read()
except OSError: ans=""
try: err=open(out+".stderr",errors="replace").read()
except OSError: err=""
mm=re.search(r"^model:\s*(\S+)",err,re.M); ee=re.search(r"^reasoning effort:\s*(\S+)",err,re.M)
code=0; reason="ok"
if rc==142 or rc==-14: code=2; reason="timeout (SIGALRM)"
elif rc!=0 or not completed: code=5; reason="no successful turn (rc=%d, turn.completed=%s)"%(rc,"yes" if completed else "no")+((" | "+"; ".join(errors)) if errors else "")
elif not ans.strip(): code=3; reason="empty answer"
if unchanged!="1":
    if code==0: code=4; reason="host config changed: ~/.codex config.toml/hooks.json hash differs"
    else: reason+=" | host config changed"
answered = code in (0,4) and bool(ans.strip())
now=datetime.datetime.now(datetime.timezone.utc); s=datetime.datetime.fromisoformat(start)
try: ver=subprocess.run([cbin,"--version"],capture_output=True,text=True,timeout=20).stdout.strip() or None
except Exception: ver=None
outcome="ANSWERED" if answered else "CANNOT-EVALUATE"
try:
    if answered: open(out,"w").write(ans)
    elif ans.strip(): open(out+".partial","w").write(ans)
except OSError as e:
    reason=(reason or "")+" | write failed: "+str(e)
    if answered: outcome="ANSWER-WRITE-FAILED"; code=7
rec={"adapter":"codex_call","host":"codex","codex_version":ver,"model_reported":mm.group(1) if mm else None,"model_requested":model,
 "effort_requested":effort,"effort_observed":ee.group(1) if ee else None,"session_id":thread,"started_at":start,"ended_at":now.isoformat(),
 "duration_s":round((now-s).total_seconds(),3),"outcome":outcome,"reason":reason,"exit":code,"answer_chars":len(ans),
 "tool_calls":cmds,"host_config_unchanged":unchanged=="1","usage":usage,"cwd":cwd,"stream":raw,"last_message":last,
 "flags":"--ignore-user-config --ephemeral --skip-git-repo-check --sandbox read-only --disable hooks --json"}
json.dump(rec,open(out+".receipt.json","w"),indent=1)
print(json.dumps({"outcome":rec["outcome"],"exit":code,"receipt":out+".receipt.json"}))
sys.exit(code)
PY
CODE=$?; rm -f "$PROMPT"; exit $CODE
