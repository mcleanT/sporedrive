#!/usr/bin/env bash
# test_observer_writer_attribution.sh — regressions for the 2026-09-08
# lifecycle-audit fixes:
#   1. .living/decisions/**  is recognized as substantive reflection content
#      (not only the top-level decisions.md file).
#   2. An observer session's Stop must never be blamed for, or finalize,
#      another concurrent writer session's file — evidence of a different
#      session's write is attributed explicitly ("foreign"), not silently
#      folded into the stopping session's own transaction and not silently
#      dropped.
#   3. A genuine Bash-originated edit by the SAME (real) executor is still
#      tracked and still enforced at that executor's own Stop.
#   4. A repeated, identical Stop after acceptance is an idempotent no-op
#      (bounded: exactly one finalization).
#   5. A late PostToolUse event carrying a superseded/old session id, after
#      an accepted Stop already removed the transaction marker, must not
#      resurrect any lifecycle state.
#
# Fixture repos are freshly `git init`ed under mktemp and discarded after
# each test. Result classes are kept separate: PASS / FAIL / SKIP(not
# reached because setup failed) are never collapsed into each other.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STOP_HOOK="$HERE/mycelium-stop-check.sh"
HEALTH_HOOK="$HERE/mycelium-health.sh"
ACTIVITY_HOOK="$HERE/mycelium-activity-tracker.sh"
POST_ACTION_HOOK="$HERE/mycelium-post-action.sh"
DATA_TRACKER_HOOK="$HERE/mycelium-data-tracker.sh"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

pass() { echo -e "${GREEN}PASS${NC} — $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo -e "${RED}FAIL${NC} — $1"; echo -e "       ${YELLOW}$2${NC}"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
skip() { echo -e "${YELLOW}SKIP (not reached)${NC} — $1"; echo -e "       ${YELLOW}$2${NC}"; SKIP_COUNT=$((SKIP_COUNT + 1)); }

make_repo() {
  local dir
  dir=$(mktemp -d) || return 1
  git init -q "$dir" || return 1
  git -C "$dir" config user.email "test@test.com"
  git -C "$dir" config user.name "Test"
  touch "$dir/README.md"
  git -C "$dir" add README.md
  git -C "$dir" commit -q -m "init"
  mkdir -p "$dir/.living"
  : > "$dir/.living/learnings.md"
  : > "$dir/.living/decisions.md"
  : > "$dir/.living/conventions.md"
  mkdir -p "$dir/.living/log"
  cat > "$dir/.living/log/LOG_REGISTRY.md" << 'REGISTRY_EOF'
# Session Log Registry

| Date | Session ID | Project | Branch | Duration | Files Changed | Summary | Key Outputs | Status | Tags | Log |
|------|-----------|---------|--------|----------|---------------|---------|-------------|--------|------|-----|
REGISTRY_EOF
  mkdir -p "$dir/.mycelium"
  echo "$dir"
}

health_start() {
  # $1 repo, $2 session_id -> returns nonzero on ANY failure to establish
  # real SessionStart state, distinct from the hook's own always-0 exit.
  local repo="$1" sid="$2"
  local payload
  payload=$(printf '{"session_id": %s, "cwd": %s, "source": "startup"}' \
    "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$sid")" \
    "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$repo")")
  (cd "$repo" && printf '%s' "$payload" | bash "$HEALTH_HOOK" >/dev/null 2>&1)
  local hook_exit=$?
  [ "$hook_exit" -eq 0 ] && [ -d "$repo/.mycelium" ]
}

bash_write() {
  # $1 repo, $2 session_id, $3 relative file path, $4 content.
  # Represents an ordinary Bash tool call the host does not decode a
  # file_path for: writes the file directly (as the real Bash command
  # would), then fires the Bash-matcher PostToolUse hooks a real Bash
  # tool call triggers (post-action + data-tracker), NOT the Edit/Write
  # activity hook. Returns nonzero if either hook invocation failed.
  local repo="$1" sid="$2" relpath="$3" content="$4"
  printf '%s\n' "$content" > "$repo/$relpath" || return 1
  local payload
  payload=$(python3 - "$sid" "$relpath" "$repo" << 'PY'
import json, sys
sid, relpath, repo = sys.argv[1:4]
cmd = "python3 -c \"from pathlib import Path; Path(%r).write_text(%r)\"" % (relpath, "bash write")
print(json.dumps({
    "session_id": sid,
    "cwd": repo,
    "tool_name": "Bash",
    "tool_input": {"command": cmd},
    "tool_response": {"exit_code": 0, "stdout": "", "stderr": ""},
}))
PY
)
  (cd "$repo" && printf '%s' "$payload" | bash "$POST_ACTION_HOOK" >/dev/null 2>&1)
  local rc1=$?
  (cd "$repo" && printf '%s' "$payload" | bash "$DATA_TRACKER_HOOK" >/dev/null 2>&1)
  local rc2=$?
  [ "$rc1" -eq 0 ] && [ "$rc2" -eq 0 ]
}

activity_write() {
  # $1 repo, $2 session_id, $3 relative file path (already written to disk)
  local repo="$1" sid="$2" relpath="$3"
  local payload
  payload=$(python3 - "$repo" "$sid" "$relpath" << 'PY'
import json, sys
repo, sid, relpath = sys.argv[1:4]
print(json.dumps({
    "session_id": sid,
    "cwd": repo,
    "tool_input": {"file_path": relpath},
    "tool_response": {"success": True},
}))
PY
)
  (cd "$repo" && printf '%s' "$payload" | bash "$ACTIVITY_HOOK" >/dev/null 2>&1)
}

run_stop() {
  # $1 repo, $2 session_id, $3 stop_hook_active (optional, "true"/"false",
  # default "false") -> sets STOP_OUTPUT, STOP_EXIT, STOP_STDERR.
  # Capturing stderr (rather than discarding it) lets a caller tell a
  # genuine empty-output accept apart from a crashed hook that also
  # produced no stdout. stop_hook_active mirrors what a real host sets when
  # this Stop is itself a continuation of a prior Stop hook's output --
  # see https://code.claude.com/docs/en/hooks#stop-decision-control.
  local repo="$1" sid="$2" active="${3:-false}"
  local payload stderr_file
  payload=$(python3 -c 'import json,sys; print(json.dumps({"session_id": sys.argv[1], "stop_hook_active": sys.argv[2] == "true"}))' "$sid" "$active")
  stderr_file=$(mktemp)
  STOP_OUTPUT=$(cd "$repo" && printf '%s' "$payload" | bash "$STOP_HOOK" 2>"$stderr_file")
  STOP_EXIT=$?
  STOP_STDERR=$(cat "$stderr_file" 2>/dev/null)
  rm -f "$stderr_file"
}

registry_row_count() {
  # Total data rows in LOG_REGISTRY.md (the row's own "Session ID" column is
  # the derived per-day log id, e.g. 2026-09-08-001 — not the host session
  # id passed to the hook — so count all rows rather than matching by host id.
  local repo="$1"
  grep -c '^| [0-9][0-9][0-9][0-9]-' "$repo/.living/log/LOG_REGISTRY.md" 2>/dev/null || true
}

# ─────────────────────────────────────────────────────────────────
# TEST 1: .living/decisions/*.md satisfies Stop end-to-end (RED before the
# session_file_changes.py fix, GREEN after — see the paired pytest unit
# tests in test_session_file_changes.py for the isolated red/green cycle).
# ─────────────────────────────────────────────────────────────────
echo "TEST 1: substantive .living/decisions/*.md end-to-end at Stop"
REPO=$(make_repo) || { skip "T1" "make_repo failed"; }
if [ -n "${REPO:-}" ]; then
  health_start "$REPO" "writer-decisions-dir"
  echo "work" > "$REPO/analysis.py"
  activity_write "$REPO" "writer-decisions-dir" "analysis.py"
  mkdir -p "$REPO/.living/decisions"
  echo "### A substantive decision" > "$REPO/.living/decisions/topic.md"
  run_stop "$REPO" "writer-decisions-dir"
  if [[ "$STOP_OUTPUT" != *'"decision": "block"'* ]]; then
    pass "T1 — .living/decisions/topic.md satisfies Stop (no block)"
  else
    fail "T1 — .living/decisions/topic.md satisfies Stop (no block)" "got: $STOP_OUTPUT"
  fi
  rm -rf "$REPO"
fi

# ─────────────────────────────────────────────────────────────────
# TEST 2: observer must not be blamed for / finalize a concurrent writer's
# file (reproduces the manual fixture in lifecycle-migration-evidence.md).
# ─────────────────────────────────────────────────────────────────
echo "TEST 2: observer Stop is not blamed for a concurrent writer's file"
REPO=$(make_repo) || { skip "T2" "make_repo failed"; }
if [ -n "${REPO:-}" ]; then
  health_start "$REPO" "observer-A"
  health_start "$REPO" "writer-B"   # must NOT seize A's transaction
  echo "b work" > "$REPO/writer-b-output.txt"
  activity_write "$REPO" "writer-B" "writer-b-output.txt"
  run_stop "$REPO" "observer-A"
  if [[ "$STOP_OUTPUT" == *"writer-b-output.txt"* && "$STOP_OUTPUT" == *'"decision": "block"'* ]]; then
    fail "T2 — observer A's Stop is not blamed for writer B's file" \
      "A's Stop still cites B's file as A's own unmet reflection: $STOP_OUTPUT"
  else
    pass "T2 — observer A's Stop is not blamed for writer B's file (output: ${STOP_OUTPUT:-<empty>})"
  fi
  rm -rf "$REPO"
fi

# ─────────────────────────────────────────────────────────────────
# TEST 3 (positive neighbor to T2): a genuine Bash-originated edit by the
# SAME owning session is still tracked and still enforced at its own Stop.
# ─────────────────────────────────────────────────────────────────
echo "TEST 3: Bash-originated edit by the real owner is still enforced"
REPO=$(make_repo) || { skip "T3" "make_repo failed"; }
if [ -n "${REPO:-}" ]; then
  health_start "$REPO" "solo-owner-A"
  # Simulate a Bash tool writing a file with no Edit/Write/apply_patch hook
  # firing at all — only the git-worktree-diff fallback can see this.
  echo "own bash write" > "$REPO/a-own-file.txt"
  run_stop "$REPO" "solo-owner-A"
  if [[ "$STOP_OUTPUT" == *"a-own-file.txt"* && "$STOP_OUTPUT" == *'"decision": "block"'* ]]; then
    pass "T3 — owner's own Bash-originated edit still enforced at Stop"
  else
    fail "T3 — owner's own Bash-originated edit still enforced at Stop" "got: $STOP_OUTPUT"
  fi
  rm -rf "$REPO"
fi

# ─────────────────────────────────────────────────────────────────
# TEST 4: repeated identical Stop after acceptance is a bounded, idempotent
# no-op — exactly one registry row, no re-block, no re-finalization.
# ─────────────────────────────────────────────────────────────────
echo "TEST 4: repeated identical Stop does not double-finalize"
REPO=$(make_repo) || { skip "T4" "make_repo failed"; }
if [ -n "${REPO:-}" ]; then
  health_start "$REPO" "idempotent-A"
  echo "work" > "$REPO/thing.py"
  activity_write "$REPO" "idempotent-A" "thing.py"
  echo "### reflection" >> "$REPO/.living/decisions.md"
  run_stop "$REPO" "idempotent-A"
  FIRST_EXIT=$STOP_EXIT
  FIRST_ROWS=$(registry_row_count "$REPO")
  run_stop "$REPO" "idempotent-A"
  SECOND_EXIT=$STOP_EXIT
  SECOND_OUTPUT="$STOP_OUTPUT"
  SECOND_ROWS=$(registry_row_count "$REPO")
  if [ "$FIRST_EXIT" -eq 0 ] && [ "$SECOND_EXIT" -eq 0 ] \
    && [ "$FIRST_ROWS" = "1" ] && [ "$SECOND_ROWS" = "1" ] \
    && [[ "$SECOND_OUTPUT" != *'"decision": "block"'* ]]; then
    pass "T4 — repeated Stop is idempotent (one registry row, no re-block)"
  else
    fail "T4 — repeated Stop is idempotent" \
      "first_exit=$FIRST_EXIT second_exit=$SECOND_EXIT first_rows=$FIRST_ROWS second_rows=$SECOND_ROWS second_output=$SECOND_OUTPUT"
  fi
  rm -rf "$REPO"
fi

# ─────────────────────────────────────────────────────────────────
# TEST 5: a late PostToolUse event carrying the old (now-superseded) session
# id, delivered after an accepted Stop removed the marker, must not
# resurrect any lifecycle state.
# ─────────────────────────────────────────────────────────────────
echo "TEST 5: late event after accepted Stop does not resurrect state"
REPO=$(make_repo) || { skip "T5" "make_repo failed"; }
if [ -n "${REPO:-}" ]; then
  health_start "$REPO" "late-event-A"
  echo "work" > "$REPO/thing2.py"
  activity_write "$REPO" "late-event-A" "thing2.py"
  echo "### reflection" >> "$REPO/.living/decisions.md"
  run_stop "$REPO" "late-event-A"
  MARKER_GONE_BEFORE=1
  [ -f "$REPO/.mycelium/active-session-log.tmp" ] && MARKER_GONE_BEFORE=0
  echo "late write" > "$REPO/late.txt"
  activity_write "$REPO" "late-event-A" "late.txt"
  MARKER_RESURRECTED=0
  [ -f "$REPO/.mycelium/active-session-log.tmp" ] && MARKER_RESURRECTED=1
  ACTIVITY_RESURRECTED=0
  [ -f "$REPO/.mycelium/mycelium-session-activity.tmp" ] && ACTIVITY_RESURRECTED=1
  if [ "$MARKER_GONE_BEFORE" -eq 1 ] && [ "$MARKER_RESURRECTED" -eq 0 ] && [ "$ACTIVITY_RESURRECTED" -eq 0 ]; then
    pass "T5 — late event after accepted Stop does not resurrect state"
  else
    fail "T5 — late event after accepted Stop does not resurrect state" \
      "marker_gone_before=$MARKER_GONE_BEFORE marker_resurrected=$MARKER_RESURRECTED activity_resurrected=$ACTIVITY_RESURRECTED"
  fi
  rm -rf "$REPO"
fi

# ─────────────────────────────────────────────────────────────────
# TESTS 6-8: the three attribution neighbors from the 2026-09-08 follow-up
# review (lifecycle-followup-review.md). Each setup step is gated on its
# OWN real success (SKIP, distinct from a behavioral FAIL, when unreached);
# every Stop assertion checks STOP_EXIT explicitly so a crashed hook with
# empty stdout can never look like a genuine "did not block" accept.
# ─────────────────────────────────────────────────────────────────
echo "TEST 6: foreign_bash neighbor -- B's ordinary-Bash write must not be blamed on A"
REPO=$(make_repo) || { skip "T6" "make_repo failed"; }
if [ -n "${REPO:-}" ]; then
  if ! health_start "$REPO" "neighborA-observer"; then
    skip "T6" "observer SessionStart setup did not succeed -- case not reached"
  elif ! health_start "$REPO" "neighborA-writer"; then
    skip "T6" "writer SessionStart setup did not succeed -- case not reached"
  elif ! bash_write "$REPO" "neighborA-writer" "overlap.txt" "writer B"; then
    skip "T6" "B's ordinary-Bash hook setup did not succeed -- case not reached"
  else
    run_stop "$REPO" "neighborA-observer"
    if [ "$STOP_EXIT" -ne 0 ]; then
      fail "T6 -- foreign_bash: A's Stop must exit 0" "stop_exit=$STOP_EXIT stdout=$STOP_OUTPUT stderr=$STOP_STDERR"
    elif [[ "$STOP_OUTPUT" == *"overlap.txt"* && "$STOP_OUTPUT" == *'"decision": "block"'* ]]; then
      fail "T6 -- foreign_bash: A must not be blamed for B's ordinary-Bash write" "got: $STOP_OUTPUT"
    else
      pass "T6 -- foreign_bash: A's Stop is not blamed for B's ordinary-Bash write (exit=0, output: ${STOP_OUTPUT:-<empty>})"
    fi
  fi
  rm -rf "$REPO"
fi

echo "TEST 7: owner_bash_after_foreign_write neighbor -- A's later genuine edit must not be suppressed"
REPO=$(make_repo) || { skip "T7" "make_repo failed"; }
if [ -n "${REPO:-}" ]; then
  if ! health_start "$REPO" "neighborB-observer"; then
    skip "T7" "observer SessionStart setup did not succeed -- case not reached"
  elif ! health_start "$REPO" "neighborB-writer"; then
    skip "T7" "writer SessionStart setup did not succeed -- case not reached"
  else
    echo "writer B" > "$REPO/overlap.txt"
    if ! activity_write "$REPO" "neighborB-writer" "overlap.txt"; then
      skip "T7" "B's explicit Write activity setup did not succeed -- case not reached"
    elif ! bash_write "$REPO" "neighborB-observer" "overlap.txt" "owner A later edit"; then
      skip "T7" "A's later ordinary-Bash edit setup did not succeed -- case not reached"
    else
      run_stop "$REPO" "neighborB-observer"
      if [ "$STOP_EXIT" -ne 0 ]; then
        fail "T7 -- owner_bash_after_foreign_write: A's Stop must exit 0" "stop_exit=$STOP_EXIT stdout=$STOP_OUTPUT stderr=$STOP_STDERR"
      elif [[ "$STOP_OUTPUT" == *"overlap.txt"* && "$STOP_OUTPUT" == *'"decision": "block"'* ]]; then
        pass "T7 -- owner_bash_after_foreign_write: A's genuine later edit is retained/enforced, not suppressed"
      else
        fail "T7 -- owner_bash_after_foreign_write: A's genuine later edit must be retained (blocked pending reflection), not silently suppressed by the earlier foreign-path entry" \
          "got: ${STOP_OUTPUT:-<empty>} exit=$STOP_EXIT stderr=$STOP_STDERR"
      fi
    fi
  fi
  rm -rf "$REPO"
fi

echo "TEST 8: foreign_writer_stop neighbor -- B's Stop must SURFACE the deferred-multi-owner condition, not silently exit 0, and must never fabricate a block or a false finalization"
REPO=$(make_repo) || { skip "T8" "make_repo failed"; }
if [ -n "${REPO:-}" ]; then
  if ! health_start "$REPO" "neighborC-observer"; then
    skip "T8" "observer SessionStart setup did not succeed -- case not reached"
  elif ! health_start "$REPO" "neighborC-writer"; then
    skip "T8" "writer SessionStart setup did not succeed -- case not reached"
  else
    echo "writer B" > "$REPO/writer-c-output.txt"
    if ! activity_write "$REPO" "neighborC-writer" "writer-c-output.txt"; then
      skip "T8" "B's explicit Write activity setup did not succeed -- case not reached"
    else
      LEDGER="$REPO/.mycelium/mycelium-foreign-activity.tmp"
      LEDGER_HAS_B_BEFORE=0
      grep -q '^neighborC-writer writer-c-output.txt$' "$LEDGER" 2>/dev/null && LEDGER_HAS_B_BEFORE=1
      ROWS_BEFORE=$(registry_row_count "$REPO")
      # First Stop: stop_hook_active=false (a fresh Stop, not a continuation).
      run_stop "$REPO" "neighborC-writer" "false"
      FIRST_EXIT=$STOP_EXIT
      FIRST_OUTPUT="$STOP_OUTPUT"
      FIRST_STDERR="$STOP_STDERR"
      ROWS_AFTER=$(registry_row_count "$REPO")
      LEDGER_HAS_B_AFTER=0
      grep -q '^neighborC-writer writer-c-output.txt$' "$LEDGER" 2>/dev/null && LEDGER_HAS_B_AFTER=1
      # Second Stop: stop_hook_active=true, i.e. the host is re-invoking Stop
      # as a continuation of the first Stop's own output (this is exactly
      # what additionalContext triggers per
      # https://code.claude.com/docs/en/hooks#stop-decision-control -- it is
      # NOT a one-shot notice on its own). The same unresolved-ownership
      # condition must NOT re-emit here, or the diagnostic would repeat on
      # every continuation up to the host's cap.
      run_stop "$REPO" "neighborC-writer" "true"
      SECOND_EXIT=$STOP_EXIT
      SECOND_OUTPUT="$STOP_OUTPUT"
      # A full multi-owner finalization redesign is explicitly deferred (no
      # registry row for B is expected or required here). What must hold:
      # (1) both Stops exit 0 (never a hard failure); (2) the FIRST Stop's
      # stdout is a non-empty, explicit diagnostic -- NOT the prior silent
      # empty string, and NOT a fabricated "decision": "block"; (3) the
      # SECOND (continuation) Stop's stdout is EMPTY -- the diagnostic is a
      # bounded one-shot, not a repeating continuation; (4) B's own
      # provenance-ledger evidence is left exactly as it was across both
      # Stops -- neither deleted nor silently folded into a (nonexistent)
      # finalization.
      if [ "$FIRST_EXIT" -ne 0 ] || [ "$SECOND_EXIT" -ne 0 ]; then
        fail "T8 -- foreign_writer_stop: both Stops must exit 0" \
          "first_exit=$FIRST_EXIT second_exit=$SECOND_EXIT first_stdout=$FIRST_OUTPUT first_stderr=$FIRST_STDERR second_stdout=$SECOND_OUTPUT"
      elif [ -z "$FIRST_OUTPUT" ]; then
        fail "T8 -- foreign_writer_stop: the FIRST Stop (stop_hook_active=false) must not silently exit 0 with empty stdout -- the deferred multi-owner condition must be surfaced" \
          "stop_exit=$FIRST_EXIT stdout=<empty> stderr=$FIRST_STDERR"
      elif [[ "$FIRST_OUTPUT" == *'"decision": "block"'* ]]; then
        fail "T8 -- foreign_writer_stop: B's Stop must not fabricate a block -- blocking is not the intended semantics for a deferred capability" \
          "got: $FIRST_OUTPUT"
      elif [[ "$FIRST_OUTPUT" != *"multi-owner"* && "$FIRST_OUTPUT" != *"deferred"* ]]; then
        fail "T8 -- foreign_writer_stop: FIRST Stop output must explicitly name the deferred multi-owner condition, not just be any non-empty text" \
          "got: $FIRST_OUTPUT"
      elif [ -n "$SECOND_OUTPUT" ]; then
        fail "T8 -- foreign_writer_stop: the SECOND (continuation, stop_hook_active=true) Stop must be a SILENT one-shot bound -- additionalContext continues the conversation under the same cap as decision:block, so re-emitting here would repeat on every continuation" \
          "second_stdout=$SECOND_OUTPUT"
      elif [ "$ROWS_AFTER" != "$ROWS_BEFORE" ]; then
        fail "T8 -- foreign_writer_stop: B must not be falsely finalized (no registry row change expected while multi-owner finalization is deferred)" \
          "registry_rows_before=$ROWS_BEFORE registry_rows_after=$ROWS_AFTER"
      elif [ "$LEDGER_HAS_B_BEFORE" -ne 1 ] || [ "$LEDGER_HAS_B_AFTER" -ne 1 ]; then
        fail "T8 -- foreign_writer_stop: B's own tracked-edit evidence must be preserved in the provenance ledger across its Stop" \
          "ledger_has_before=$LEDGER_HAS_B_BEFORE ledger_has_after=$LEDGER_HAS_B_AFTER"
      else
        pass "T8 -- foreign_writer_stop: FIRST Stop (stop_hook_active=false) surfaces the deferred multi-owner condition explicitly (exit=0, non-empty, no fabricated block); SECOND Stop (stop_hook_active=true, a continuation) is a silent one-shot bound (empty stdout); B's evidence preserved unattributed/unfinalized (first output: $FIRST_OUTPUT)"
      fi
    fi
  fi
  rm -rf "$REPO"
fi

# ─────────────────────────────────────────────────────────────────
# TEST 9: setup-failure negative case. The hooks are designed to exit 0 even
# on an internal failure (never crash the host), so a harness that only
# checks the hook's shell exit code cannot tell "setup succeeded" from
# "setup silently did nothing". This asserts the harness's OWN setup-success
# check (health_start) correctly reports failure -- not a false pass -- when
# SessionStart cannot establish real state.
# ─────────────────────────────────────────────────────────────────
echo "TEST 9: setup-failure negative case -- a genuinely broken SessionStart must be detected, never silently treated as a pass"
REPO=$(make_repo) || { skip "T9" "make_repo failed"; }
if [ -n "${REPO:-}" ]; then
  rm -rf "$REPO/.mycelium"
  : > "$REPO/.mycelium"   # a FILE where the state dir must be a directory
  if health_start "$REPO" "setup-failure-A"; then
    fail "T9 -- setup-failure negative case" \
      "health_start reported success despite an unusable .mycelium state dir (a file, not a directory) -- the harness would silently treat unreached setup as a pass"
  else
    pass "T9 -- setup-failure negative case correctly detected as setup-not-reached (health_start returned failure, not a false pass)"
  fi
  rm -rf "$REPO"
fi

# ─────────────────────────────────────────────────────────────────
# TEST 10: Case C continued THROUGH the owner's completion. T8 stops at the
# deferred writer's own Stops; this extends the same fixture through the OWNER
# (observer) Stop, whose accepted cleanup removes the transient provenance
# ledger. The accepted finding (work/lifecycle-deferred-evidence-probe.json)
# is that this cleanup used to destroy the deferred writer's attribution while
# its work file survived. The fix hands the ledger off to a durable journal
# (mycelium_archive_foreign_provenance) BEFORE removing it, so the writer's
# provenance is preserved across the owner's completion.
# ─────────────────────────────────────────────────────────────────
echo "TEST 10: Case C through owner completion -- the deferred writer's provenance must survive the OWNER's accepted-Stop cleanup, not just the writer's own Stop"
REPO=$(make_repo) || { skip "T10" "make_repo failed"; }
if [ -n "${REPO:-}" ]; then
  if ! health_start "$REPO" "ownerC-observer"; then
    skip "T10" "observer SessionStart setup did not succeed -- case not reached"
  elif ! health_start "$REPO" "ownerC-writer"; then
    skip "T10" "writer SessionStart setup did not succeed -- case not reached"
  else
    echo "writer C" > "$REPO/writer-c-output.txt"
    if ! activity_write "$REPO" "ownerC-writer" "writer-c-output.txt"; then
      skip "T10" "writer's explicit Write activity setup did not succeed -- case not reached"
    else
      LEDGER="$REPO/.mycelium/mycelium-foreign-activity.tmp"
      LEDGER_HAS_WRITER_BEFORE=0
      grep -q '^ownerC-writer writer-c-output.txt$' "$LEDGER" 2>/dev/null && LEDGER_HAS_WRITER_BEFORE=1
      # Writer's deferred Stops (fresh, then continuation) -- same as T8.
      run_stop "$REPO" "ownerC-writer" "false"
      run_stop "$REPO" "ownerC-writer" "true"
      # Now the OWNER (observer) stops and completes: its accepted-Stop cleanup
      # removes the transient ledger. Provenance must be handed off first.
      run_stop "$REPO" "ownerC-observer" "false"
      OWNER_EXIT=$STOP_EXIT
      # Durable preservation = a journal file carrying the writer's row.
      JOURNAL_HAS_WRITER=0
      for jf in "$REPO"/.mycelium/mycelium-foreign-activity.journal.*; do
        [ -f "$jf" ] || continue
        if grep -q '^ownerC-writer writer-c-output.txt$' "$jf" 2>/dev/null; then
          JOURNAL_HAS_WRITER=1; break
        fi
      done
      WORK_FILE_PRESERVED=0
      [ "$(cat "$REPO/writer-c-output.txt" 2>/dev/null)" = "writer C" ] && WORK_FILE_PRESERVED=1
      if [ "$LEDGER_HAS_WRITER_BEFORE" -ne 1 ]; then
        skip "T10" "writer's provenance row was not recorded before Stop -- case not reached"
      elif [ "$OWNER_EXIT" -ne 0 ]; then
        fail "T10 -- owner completion: the owner's Stop must exit 0" \
          "owner_exit=$OWNER_EXIT stderr=$STOP_STDERR stdout=$STOP_OUTPUT"
      elif [ "$WORK_FILE_PRESERVED" -ne 1 ]; then
        fail "T10 -- owner completion: the writer's actual work file must survive the owner's cleanup" \
          "writer-c-output.txt content unexpected"
      elif [ "$JOURNAL_HAS_WRITER" -ne 1 ]; then
        fail "T10 -- owner completion: the deferred writer's provenance must be PRESERVED in a durable journal across the owner's accepted-Stop cleanup, not destroyed (the accepted finding's defect)" \
          "no journal carries 'ownerC-writer writer-c-output.txt'; ledger_now=$( [ -e "$LEDGER" ] && echo present || echo absent )"
      else
        pass "T10 -- owner completion: the owner's Stop completed (exit 0), the writer's work file survived, AND its provenance was preserved in a durable journal across the owner's cleanup (deferred-evidence finding fixed)"
      fi
    fi
  fi
  rm -rf "$REPO"
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "Results: ${GREEN}${PASS_COUNT} passed${NC} / ${RED}${FAIL_COUNT} failed${NC} / ${YELLOW}${SKIP_COUNT} not-reached${NC} / $((PASS_COUNT + FAIL_COUNT + SKIP_COUNT)) total"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

[ "$FAIL_COUNT" -eq 0 ]
