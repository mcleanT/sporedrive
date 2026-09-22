#!/usr/bin/env bash
# Mycelium coordination native adapter (two-host, shared semantics).
#
# Fires on BOTH hosts (the exporter merges this ONE script into each host's hooks.json) at two kinds
# of boundary, and self-detects which:
#   * SessionStart (startup|resume|clear|compact): expose the task's checkpoint + bounded pending
#     messages to the model as hookSpecificOutput.additionalContext so an attached participant
#     reattaches after startup/compaction without copying transcripts.
#   * PostToolUse (active tool boundary): publish an up-to-date per-session status AND surface only
#     NEW addressed mail (bounded), so an ALREADY-RUNNING peer sees fresh mail without waiting for a
#     restart/compaction (PLAN v1 section 3 + PLAN-v2 R4/R5, acceptance criterion C).
#
# NATIVE CONTEXT ROUTING is per native session: `mycelium-coord attach`/`select-session` records
# {task,participant} under this session's id; resolution is by the input session_id, so two sessions
# in one worktree never resolve to each other. No selection for this session -> silent no-op.
#
# READ-ONLY toward the scientific tree: it never claims the lifecycle owner marker, never runs
# Science write-side hooks, and changes no repository files. Status publication is pure telemetry
# (a coordination-store write outside any repo): it never calls a model, demands an ack, writes a
# scientific tree, wakes a terminal task, or polls on a timer. Every coord call is best-effort so it
# can never block the context the model relies on.
set -euo pipefail

INPUT="$(cat 2>/dev/null || true)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI="$(cd "$HERE/../bin" && pwd)/mycelium-coord"
[ -x "$CLI" ] || exit 0

if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then HOST=claude; else HOST="${MYCELIUM_HOOK_HOST:-codex}"; fi
SESSION_ID="$(printf '%s' "$INPUT" | python3 -c 'import json,sys
try: v=json.load(sys.stdin)
except Exception: v={}
print(v.get("session_id") or v.get("session") or "")' 2>/dev/null || true)"
SOURCE_EVENT="$(printf '%s' "$INPUT" | python3 -c 'import json,sys
try: v=json.load(sys.stdin)
except Exception: v={}
print(v.get("source") or "")' 2>/dev/null || true)"
EVENT="$(printf '%s' "$INPUT" | python3 -c 'import json,sys
try: v=json.load(sys.stdin)
except Exception: v={}
print(v.get("hook_event_name") or v.get("hookEventName") or "")' 2>/dev/null || true)"
# host env override (a Codex adapter that cannot set hook_event_name may pass MYCELIUM_HOOK_EVENT)
[ -z "$EVENT" ] && EVENT="${MYCELIUM_HOOK_EVENT:-SessionStart}"

TASK=""; PARTICIPANT=""; FROM_ENV=0
if [ -n "$SESSION_ID" ]; then
  SEL="$("$CLI" resolve-session "$HOST" "$SESSION_ID" 2>/dev/null || true)"
  read -r TASK PARTICIPANT < <(printf '%s' "$SEL" | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
print(d.get("task",""), d.get("participant",""))' 2>/dev/null || echo "")
fi
# Env fallback ONLY for a session explicitly launched with these (env cannot be set retroactively).
if [ -z "$TASK" ] || [ -z "$PARTICIPANT" ]; then
  TASK="${MYCELIUM_COORD_TASK:-}"
  PARTICIPANT="${MYCELIUM_COORD_PARTICIPANT:-}"
  FROM_ENV=1
fi
{ [ -z "$TASK" ] || [ -z "$PARTICIPANT" ]; } && exit 0

# An env hint must prove THIS session's native identity (host + input session_id) matches the named
# participant, exactly like the managed selector — otherwise an unrelated or merely inherited-env
# session could silently impersonate a participant. A missing native session id fails verification.
if [ "$FROM_ENV" = "1" ]; then
  "$CLI" verify-session "$TASK" "$PARTICIPANT" "$HOST" "$SESSION_ID" >/dev/null 2>&1 || exit 0
fi

# Honest runtime state (never a synthesised idle): a SessionStart turn is (re)starting; a PostToolUse
# boundary is mid-turn active. Owned jobs and context are unobservable here and left unknown.
case "$EVENT" in
  PostToolUse) RUNTIME=active ;;
  *) case "$SOURCE_EVENT" in resume|compact) RUNTIME=resuming ;; *) RUNTIME=starting ;; esac ;;
esac

# Publish this session's status. --allocate-seq derives seq + binding generation from durable,
# lock-serialised coordinator state (NOT a host clock), strictly increasing across hook processes.
if [ -n "$SESSION_ID" ]; then
  "$CLI" status-publish "$TASK" "$PARTICIPANT" --session "$SESSION_ID" --allocate-seq \
    --source "$HOST.$EVENT.${SOURCE_EVENT:-tool}" --runtime-state "$RUNTIME" >/dev/null 2>&1 || true
fi

if [ "$EVENT" = "PostToolUse" ]; then
  # Active-boundary exposure: surface only the bounded NEW addressed mail after this participant's
  # notification cursor, then advance that cursor so the same item is not re-injected on every
  # subsequent tool. Ack is separate: unacked mail still reminds at the next startup/resume.
  BINBOX="$("$CLI" boundary-inbox "$TASK" "$PARTICIPANT" --limit 10 --max-bytes 4000 2>/dev/null || true)"
  [ -z "$BINBOX" ] && exit 0
  COORD_HOOK_EVENT=PostToolUse COORD_BOUNDARY_JSON="$BINBOX" \
    python3 "$HERE/_coord_context.py" "$TASK" "$PARTICIPANT" </dev/null || true
  read -r UNREAD NEXT < <(printf '%s' "$BINBOX" | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
print(d.get("unread_count",0), d.get("next_after_seq",0))' 2>/dev/null || echo "0 0")
  if [ "${UNREAD:-0}" != "0" ]; then
    "$CLI" set-cursor "$TASK" "$PARTICIPANT" "$NEXT" >/dev/null 2>&1 || true
  fi
  exit 0
fi

# SessionStart: emit the resume context (checkpoint + bounded pending) the model reattaches from.
RESUME="$("$CLI" resume "$TASK" "$PARTICIPANT" --limit 20 2>/dev/null || true)"
[ -z "$RESUME" ] && exit 0
printf '%s' "$RESUME" | COORD_HOOK_EVENT=SessionStart python3 "$HERE/_coord_context.py" "$TASK" "$PARTICIPANT" || true
exit 0
