#!/usr/bin/env bash
# Mycelium coordination attach hook (SessionStart: startup|resume|clear|compact).
#
# NATIVE CONTEXT ROUTING (MYCELIUM-INTEGRATION r1, D1/D5): when THIS native session has an explicit
# task selection (bound to its OWN session id in managed outside-repo state — never a shared cwd),
# expose that task's checkpoint + pending messages to the model as hookSpecificOutput.additionalContext
# so an attached supervisor/executor reattaches after startup/compaction without copying transcripts.
# READ-ONLY coordination state: it never claims the scientific lifecycle owner marker, never runs
# Science's write-side hooks, and changes no repository files.
#
# SELECTION is per native session: `mycelium-coord attach`/`select-session` records {task,participant}
# under this session's id; the hook resolves by the SessionStart input session_id, so two sessions in
# one worktree never resolve to each other. No selection for this session -> silent no-op.
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

TASK=""; PARTICIPANT=""; FROM_ENV=0
if [ -n "$SESSION_ID" ]; then
  SEL="$("$CLI" resolve-session "$HOST" "$SESSION_ID" 2>/dev/null || true)"
  read -r TASK PARTICIPANT < <(printf '%s' "$SEL" | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
print(d.get("task",""), d.get("participant",""))' 2>/dev/null || echo "")
fi
# Env fallback ONLY for a session explicitly launched with these (env cannot be set retroactively).
# The managed selector (resolve-session) already checks this session's live identity; the env hint
# has NOT been checked, so validate it below before trusting it.
if [ -z "$TASK" ] || [ -z "$PARTICIPANT" ]; then
  TASK="${MYCELIUM_COORD_TASK:-}"
  PARTICIPANT="${MYCELIUM_COORD_PARTICIPANT:-}"
  FROM_ENV=1
fi
{ [ -z "$TASK" ] || [ -z "$PARTICIPANT" ]; } && exit 0

# An env hint must prove THIS session's native identity (host + input session_id) matches the named
# participant, exactly like the managed selector — otherwise an unrelated or merely inherited-env
# session could silently impersonate a participant (session-transition review 2). A missing native
# session id fails verification and the hook stays silent.
if [ "$FROM_ENV" = "1" ]; then
  "$CLI" verify-session "$TASK" "$PARTICIPANT" "$HOST" "$SESSION_ID" >/dev/null 2>&1 || exit 0
fi

RESUME="$("$CLI" resume "$TASK" "$PARTICIPANT" --limit 20 2>/dev/null || true)"
[ -z "$RESUME" ] && exit 0
printf '%s' "$RESUME" | python3 "$HERE/_coord_context.py" "$TASK" "$PARTICIPANT" || true
exit 0
