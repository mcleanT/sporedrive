#!/usr/bin/env bash
# codex_ask.sh — GLOBAL canonical entrypoint for codex (GPT, via ChatGPT subscription) calls.
# Symlinked to ~/bin/codex_ask (on PATH) — callable as `codex_ask "..."` from ANY repo.
# Maintained source: ~/tools/codex-claude-workflow/src/claude/tools/codex_ask.sh (contract 1.1.0).
#
# Sends grounded context so codex reviews are pointed, not blind. Three layers reach codex:
#   1. ~/.codex/AGENTS.md   — UNIVERSAL conventions, auto-loaded by codex on every call.
#   2. <repo>/AGENTS.md     — PROJECT architecture/conventions, auto-loaded from the repo root.
#   3. "## Session context" — DYNAMIC block this script builds: branch + recent commits +
#                             tail of .living/last-session.md (if present) + the PATHS of any
#                             -f files (codex reads them from disk; contents are not injected).
#   ...plus your QUESTION. Put task-specific framing/decisions right in the question for max signal.
#
# Project-agnostic: auto-detects the git repo root; the .living/ tail is included only if it exists,
# so this works in any repo (or none). Golden rules baked in (cd repo root, --sandbox read-only,
# prompt via stdin, output redirected DIRECT to a file — never piped through tail/head).
#
# Usage:
#   codex_ask [-m MODEL] [-e low|medium|high|xhigh|ultra|max] [-f FILE]... [-n] "QUESTION"
#   -m  model        (default: gpt-5.6-sol — the deliberately chosen reviewer model for this wrapper.
#                     It is NOT the account/app default: ~/.codex/config.toml sets the interactive
#                     Codex model separately (gpt-6-astra as of 2026-09-08). Plain "gpt-5.6" and
#                     "gpt-5-6-thinking" are REJECTED by Codex-with-ChatGPT; use the -sol slug.)
#   -e  reasoning    (default: medium; use xhigh for deep reviews. Accepted by the Codex CLI:
#                     low|medium|high|xhigh|ultra|max)
#   -f  file         a path codex should read as specifically under review (repeatable). Listed by
#                     path in the prompt; NOT injected. Must resolve from the repo root or be absolute.
#                     The read-only sandbox reads any path the user can read (in or out of the repo)
#                     and denies all writes; it is not a privacy boundary.
#   -o  outfile      write codex output to this exact path (else an auto-named temp file)
#   -n  dry-run      assemble and print the prompt only; do NOT call codex
# Env: CODEX_ASK_OUTDIR overrides the temp dir for prompt/output files.
# Output: codex's stdout AND stderr are merged into the output file (BYTE-IDENTICAL to before this
#   receipt was added); this script still exits with codex's exit status; stdout still prints only
#   the output-file path. That contract is unchanged.
#
# Result receipt (additive, does not change the above): every real invocation (not -n/dry-run) also
# writes "${OUT_FILE}.receipt.json" next to the output file — exit code, wall-clock duration,
# requested/resolved model (from banner text; null where unavailable), served model (see below),
# effort, session id (best effort, null if absent), the raw-log + prompt + final-artifact paths,
# output size, a parse_status/error_class pair, any nonfatal warnings, and a boolean `complete`.
#
# Completeness mechanism (installed codex-cli 0.146.0, confirmed via `codex exec --help`): every
# real invocation also passes `-o/--output-last-message <FILE>` so the CLI itself writes ONLY its
# genuine final agent message to a CHECKED, UNIQUE, per-invocation artifact created fresh with
# `mktemp` — never a path derived from OUT_FILE. Freshness is guaranteed by construction: the file
# is brand-new and empty for THIS call, so a prior run's leftover file at a predictable path can
# never be mistaken for this run's answer (the earlier "rm before the call" scheme failed silently
# when the output directory was not writable, leaving a stale artifact that falsely marked the run
# complete). Two concurrent `-o SAME` calls also cannot collide. If a fresh artifact cannot be
# created anywhere writable, freshness cannot be established and the run reports complete:false
# (error_class=freshness_unverified) rather than trusting any pre-existing file.
# `complete` is true IFF the process exited 0 AND that artifact exists AND is non-empty after
# trimming whitespace — nothing else counts as final-answer evidence: not raw log length, not a
# startup banner, not a tool-execution/prompt echo, not sentence punctuation. A nonfatal
# models-cache warning next to a real artifact still reports complete:true (warning listed only).
# Auth failure, model rejection, any other nonzero exit, a missing/empty artifact (empty_output), a
# nonzero exit with a partial artifact (partial_output), and a timeout (rc=124 — the `timeout`
# command's own convention — or a signal death, rc>128) are each their own parse_status/error_class
# and always report complete:false. `model_served` is intentionally always null on this CLI route:
# it does not expose structured served-model metadata, and quoted/bareword "model: X" text found
# anywhere in the raw log (banner or answer prose) is never trusted as served identity or used to
# manufacture a mismatch — only `model_resolved`, read from the CLI's own startup banner, is
# populated. See tests/test_codex_ask_receipt.py (deterministic fixtures) for the exact contract.
# Callers can still fall back to the old manual check (grep the output file for "ERROR:" /
# "not supported" / "not logged in") — the receipt is a supplement, not a replacement requirement.
# For long reviews, launch this script in the background; it prints the output-file path last.

set -uo pipefail

MODEL="gpt-5.6-sol"   # deliberately chosen reviewer model; independent of ~/.codex/config.toml. Override with -m.
EFFORT="medium"
DRY_RUN=0
OUTFILE=""
FILES=()
OUTDIR="${CODEX_ASK_OUTDIR:-${TMPDIR:-/tmp}}"
OUTDIR="${OUTDIR%/}"

usage() {
  echo 'Usage: codex_ask [-m MODEL] [-e low|medium|high|xhigh|ultra|max] [-f FILE]... [-o OUTFILE] [-n] "QUESTION"' >&2
  exit 64
}

# --- Result-receipt helpers (additive; never alter raw output, stdout contract, or exit code) ----
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  s="${s//$'\r'/}"
  s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}

json_str_or_null() {
  if [ -z "$1" ]; then printf 'null'; else printf '"%s"' "$(json_escape "$1")"; fi
}

# Best-effort extraction of a model/session identity the CLI itself printed. Confirmed against a
# real invocation (codex-cli 0.146.0, 2026-09-08): plain `codex exec --sandbox read-only` text
# output DOES print "model: <slug>" and "session id: <uuid>" lines in its startup banner, and this
# detector reads both. Still null whenever the CLI's output format changes or omits them — this is
# a text-pattern heuristic, not a guaranteed contract with the CLI.
detect_field() {
  local file="$1" labels="$2"
  grep -Eio "\"(${labels})\"[[:space:]]*:[[:space:]]*\"[A-Za-z0-9._-]+\"|\\b(${labels})[[:space:]]*[:=][[:space:]]*[A-Za-z0-9._-]+\\b" \
    "$file" 2>/dev/null | head -1 | grep -Eio '[A-Za-z0-9._-]+"?$' | tr -d '"'
}

# Same extraction as detect_field(), but scanning an in-memory TEXT slice rather than a whole
# file. Used to keep "resolved" identity detection scoped to the trusted startup-banner region
# only, instead of scanning every byte of the combined stdout+stderr file.
extract_identity_from_text() {
  local text="$1" pattern="$2"
  [ -z "$text" ] && return 0
  printf '%s\n' "$text" | grep -Eio "$pattern" 2>/dev/null \
    | head -1 | grep -Eio '[A-Za-z0-9._-]+"?$' | tr -d '"'
}

# Classify one completed invocation and write "${out_file}.receipt.json". Never raises: a failure
# to write the receipt is reported to stderr by the caller and does not change $RC or stdout.
#
# Completeness mechanism: `last_msg_file` is the path passed to codex's own
# `-o/--output-last-message <FILE>` flag, a UNIQUE mktemp'd per-invocation path (empty on entry,
# so non-empty afterward proves THIS call wrote it). `fresh_established` is 1 when that fresh
# artifact was actually created, 0 when no writable location was available (then completeness is
# refused as freshness_unverified rather than falling back to any pre-existing file). The artifact
# is the ONLY signal used for final-answer evidence — no raw-log byte-length floor, no
# banner/warning text parsing, no punctuation heuristic (review finding 1, round 2: none of those
# distinguish a banner, a warning, echoed prompt/tool-log text, or a short-but-real token like
# "READY" from an actual answer; only the CLI's own last-message artifact can).
write_receipt() {
  local out_file="$1" rc="$2" duration_s="$3" invoked_at="$4" model_req="$5" effort="$6" prompt_file="$7" last_msg_file="$8" fresh_established="${9:-1}"
  local receipt_file="${out_file}.receipt.json"
  local out_size=0 out_nonblank="" parse_status="ok" error_class="" complete="true"
  local -a warnings=()

  if [ -f "$out_file" ]; then
    out_size="$(wc -c < "$out_file" 2>/dev/null | tr -d ' ')"
    out_nonblank="$(tr -d '[:space:]' < "$out_file" 2>/dev/null)"
  fi
  out_size="${out_size:-0}"

  # --- Final-answer artifact (the sole completeness signal) --------------------------------------
  local artifact_exists=0 artifact_nonblank=""
  if [ -n "$last_msg_file" ] && [ -f "$last_msg_file" ]; then
    artifact_exists=1
    artifact_nonblank="$(tr -d '[:space:]' < "$last_msg_file" 2>/dev/null)"
  fi
  local final_artifact=""
  [ "$artifact_exists" -eq 1 ] && [ -n "$artifact_nonblank" ] && final_artifact="$last_msg_file"

  # --- "resolved" identity: read ONLY from the CLI's own startup banner (a trusted region) -------
  # codex-cli's `exec --sandbox read-only` banner (confirmed live, 0.146.0) opens with
  # "OpenAI Codex v<ver>" and is delimited by a "--------" separator line; when present, everything
  # through the LAST such separator in the banner's first ~20 lines is startup config.
  local banner_lines=0 banner_text="" first_line="" model_resolved="" session_id=""
  if [ -f "$out_file" ]; then
    first_line="$(head -n 1 "$out_file" 2>/dev/null)"
    if printf '%s' "$first_line" | grep -Eiq '^OpenAI[[:space:]]+Codex\b'; then
      banner_lines="$(head -n 20 "$out_file" 2>/dev/null | grep -n '^-\{3,\}[[:space:]]*$' | tail -1 | cut -d: -f1)"
      banner_lines="${banner_lines:-0}"
      [ "$banner_lines" -gt 0 ] && banner_text="$(head -n "$banner_lines" "$out_file" 2>/dev/null)"
    fi
    [ -n "$out_nonblank" ] && session_id="$(detect_field "$out_file" 'session_id|session id|conversation_id')"
  fi
  local resolved_pattern='"(model|model_slug)"[[:space:]]*:[[:space:]]*"[A-Za-z0-9._-]+"|\bmodel[[:space:]]*[:=][[:space:]]*[A-Za-z0-9._-]+\b'
  [ -n "$banner_text" ] && model_resolved="$(extract_identity_from_text "$banner_text" "$resolved_pattern")"

  # "served" is intentionally ALWAYS null on this CLI route: plain-text `codex exec` (with or
  # without -o/--output-last-message) exposes no structured served-model metadata. Quoted or
  # bareword "model: X" text found anywhere in the raw log — banner OR answer prose — is never
  # trusted as served identity and never used to manufacture a mismatch (review finding 3 / round
  # 2 case: example JSON inside valid answer prose must not be read as response metadata).
  local model_served=""

  # --- Trusted status-channel checks (review finding 2) ------------------------------------------
  # auth/model-rejection phrases are only ever consulted below when the process itself reported a
  # nonzero exit AND no final-answer artifact evidence exists — the CLI's own exit code (and the
  # last-message artifact) are the channels we trust; a valid rc=0 final answer that merely
  # discusses these phrases as prose is never scanned as if it were a status line.
  local auth_hit="" reject_hit="" warn_hit=""
  if [ -f "$out_file" ]; then
    grep -Eiq 'not logged in|not authenticated|unauthorized|401 unauthorized|invalid api key|run `?codex login`?' "$out_file" 2>/dev/null && auth_hit=1
    grep -Eiq 'model is not supported|not supported (for|by) this|unsupported model|unknown model' "$out_file" 2>/dev/null && reject_hit=1
    grep -Eiq 'models? cache|failed to refresh models|stale model list|could not update model list' "$out_file" 2>/dev/null && warn_hit=1
  fi

  if [ "$rc" -eq 124 ]; then
    parse_status="timeout"; error_class="timeout"; complete="false"
  elif [ "$rc" -gt 128 ]; then
    # Signal death (128+N). Most commonly an external timeout/watchdog killing a hung call, but we
    # cannot prove that from here, so this gets its own class rather than being folded into "timeout".
    parse_status="timeout"; error_class="process_terminated"; complete="false"
  elif [ "$rc" -ne 0 ]; then
    if [ "$artifact_exists" -eq 1 ] && [ -n "$artifact_nonblank" ]; then
      # The CLI wrote real (if possibly incomplete) final-message content but still exited
      # nonzero — kept distinct from a bare nonzero exit with no artifact evidence at all.
      parse_status="partial_output"; error_class="partial_output"
    elif [ -n "$auth_hit" ]; then
      parse_status="auth_failure"; error_class="auth_failure"
    elif [ -n "$reject_hit" ]; then
      parse_status="model_rejected"; error_class="model_rejected"
    else
      parse_status="nonzero_exit"; error_class="nonzero_exit"
    fi
    complete="false"
  elif [ "$fresh_established" -eq 0 ]; then
    # We could not create a fresh unique artifact anywhere writable, so completeness cannot be
    # established for this run — refuse rather than trust any pre-existing file (stale-artifact
    # cleanup-failure case). Distinct from a run that had a fresh artifact but produced no answer.
    parse_status="freshness_unverified"; error_class="freshness_unverified"; complete="false"
  elif [ "$artifact_exists" -eq 0 ] || [ -z "$artifact_nonblank" ]; then
    # exit 0 but no non-empty last-message artifact: a banner, a warning, an echoed prompt, a
    # tool-execution log, or truly empty output — none of that is final-answer evidence, no
    # matter how many raw-log bytes it occupies (review finding 1, round 2).
    parse_status="empty_output"; error_class="empty_output"; complete="false"
  else
    parse_status="ok"; error_class=""; complete="true"
  fi

  # A nonfatal models-cache warning next to a real answer does NOT flip parse_status/complete —
  # it is surfaced in `warnings` only, per the "warning + valid final answer is not a failed review" rule.
  [ -n "$warn_hit" ] && warnings+=("models_cache_nonfatal")

  local warnings_json="[]"
  if [ "${#warnings[@]}" -gt 0 ]; then
    local first=1 w
    warnings_json="["
    for w in "${warnings[@]}"; do
      [ "$first" -eq 1 ] || warnings_json+=","
      warnings_json+="$(json_str_or_null "$w")"
      first=0
    done
    warnings_json+="]"
  fi

  {
    printf '{\n'
    printf '  "schema_version": "1.0.0",\n'
    printf '  "tool": "codex_ask",\n'
    printf '  "invoked_at": %s,\n' "$(json_str_or_null "$invoked_at")"
    printf '  "exit_code": %s,\n' "$rc"
    printf '  "duration_s": %s,\n' "$duration_s"
    printf '  "model_requested": %s,\n' "$(json_str_or_null "$model_req")"
    printf '  "model_resolved": %s,\n' "$(json_str_or_null "$model_resolved")"
    printf '  "model_served": %s,\n' "$(json_str_or_null "$model_served")"
    printf '  "effort": %s,\n' "$(json_str_or_null "$effort")"
    printf '  "session_id": %s,\n' "$(json_str_or_null "$session_id")"
    printf '  "artifact_path": %s,\n' "$(json_str_or_null "$out_file")"
    printf '  "prompt_path": %s,\n' "$(json_str_or_null "$prompt_file")"
    printf '  "final_artifact": %s,\n' "$(json_str_or_null "$final_artifact")"
    printf '  "output_bytes": %s,\n' "$out_size"
    printf '  "parse_status": %s,\n' "$(json_str_or_null "$parse_status")"
    printf '  "error_class": %s,\n' "$(json_str_or_null "$error_class")"
    printf '  "warnings": %s,\n' "$warnings_json"
    printf '  "complete": %s\n' "$complete"
    printf '}\n'
  } > "$receipt_file"
}

while getopts ":m:e:f:o:nh" opt; do
  case "$opt" in
    m) MODEL="$OPTARG" ;;
    e) EFFORT="$OPTARG" ;;
    f) FILES+=("$OPTARG") ;;
    o) OUTFILE="$OPTARG" ;;
    n) DRY_RUN=1 ;;
    h) usage ;;
    \?) echo "Unknown option -$OPTARG" >&2; usage ;;
    :) echo "Option -$OPTARG needs an argument" >&2; usage ;;
  esac
done
shift $((OPTIND - 1))
[ "$#" -ge 1 ] || usage
QUESTION="$*"

command -v codex >/dev/null 2>&1 || { echo "[codex_ask] codex CLI not found on PATH" >&2; exit 69; }

# Resolve repo root so codex cd's correctly and relative paths resolve (fall back to cwd if not a repo).
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT" || { echo "[codex_ask] cannot cd to $REPO_ROOT" >&2; exit 70; }

# --- Assemble the DYNAMIC session-context block ---------------------------------
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '(no git)')"
COMMITS="$(git log --oneline -5 2>/dev/null || echo '(none)')"
LASTSESSION=""
for f in ".living/last-session.md" ".living/log/last-session.md"; do
  if [ -f "$f" ]; then LASTSESSION="$(tail -n 25 "$f")"; break; fi
done
# Cap to keep the prompt focused — a long last-session entry can otherwise dominate the prompt
# with low-signal noise. Keep the most-recent tail (the file is chronological, newest at the end).
LS_CAP="${CODEX_ASK_LASTSESSION_CAP:-1600}"
if [ -n "$LASTSESSION" ] && [ "${#LASTSESSION}" -gt "$LS_CAP" ]; then
  LASTSESSION="...(older context truncated; showing most recent ${LS_CAP} chars)...
$(printf '%s' "$LASTSESSION" | tail -c "$LS_CAP")"
fi

PROMPT_FILE="$(mktemp "${OUTDIR}/codex_prompt.XXXXXX")"
{
  echo "## Session context (auto-injected)"
  echo "Repo: $(basename "$REPO_ROOT")   Branch: ${BRANCH}"
  echo
  echo "Recent commits:"
  echo "${COMMITS}"
  if [ -n "$LASTSESSION" ]; then
    echo
    echo "Current work (tail of .living/last-session.md):"
    echo "${LASTSESSION}"
  fi
  if [ "${#FILES[@]}" -gt 0 ]; then
    echo
    echo "Files specifically under review (read these from disk):"
    for f in "${FILES[@]}"; do echo "  - ${f}"; done
  fi
  echo
  echo "Project architecture + conventions are in AGENTS.md (already loaded by you)."
  echo
  echo "## Question"
  echo "${QUESTION}"
} > "$PROMPT_FILE"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "----- assembled prompt ($PROMPT_FILE) -----" >&2
  cat "$PROMPT_FILE"
  exit 0
fi

# --- Output destination ---------------------------------------------------------
if [ -n "$OUTFILE" ]; then
  OUT_FILE="$OUTFILE"
else
  SLUG="$(printf '%s' "$QUESTION" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | cut -c1-40)"
  SLUG="${SLUG%-}"
  OUT_FILE="$(mktemp "${OUTDIR}/codex_${SLUG:-ask}.XXXXXX.txt")"
fi

# --- Invoke codex: prompt via stdin, output DIRECT to file (no tail/head pipe) ---
# LAST_MSG_FILE: a CHECKED, UNIQUE per-invocation artifact for codex's own -o/--output-last-message
# flag (confirmed supported: `codex exec --help` lists "-o, --output-last-message <FILE>"). Created
# fresh with mktemp — NOT derived from OUT_FILE — so freshness is guaranteed by construction: the
# file is brand-new and empty for this call, and a stale artifact from a prior run at a predictable
# path can never mark a later run complete (the old "rm before the call" scheme failed silently in
# a read-only output directory and left the stale file behind). Prefer a name adjacent to OUT_FILE
# for discoverability; fall back to the writable OUTDIR when OUT_FILE's directory is not writable;
# if neither works, freshness cannot be established and completeness is refused downstream. This is
# additive: the raw merged stdout+stderr log (OUT_FILE), stdout contract (prints only OUT_FILE),
# and exit-code contract are all unchanged.
LAST_MSG_FILE=""
FRESH_ARTIFACT=0
if LAST_MSG_FILE="$(mktemp "${OUT_FILE}.lastmsg.XXXXXX" 2>/dev/null)"; then
  FRESH_ARTIFACT=1
elif LAST_MSG_FILE="$(mktemp "${OUTDIR}/codex_lastmsg.XXXXXX" 2>/dev/null)"; then
  FRESH_ARTIFACT=1
else
  LAST_MSG_FILE=""
fi
echo "[codex_ask] model=${MODEL} effort=${EFFORT} prompt=${PROMPT_FILE} out=${OUT_FILE}" >&2
INVOKED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "")"
SECONDS=0
CODEX_ARGS=(exec --sandbox read-only -m "$MODEL" -c model_reasoning_effort="$EFFORT")
[ -n "$LAST_MSG_FILE" ] && CODEX_ARGS+=(-o "$LAST_MSG_FILE")
CODEX_ARGS+=(-)
cat "$PROMPT_FILE" | codex "${CODEX_ARGS[@]}" > "$OUT_FILE" 2>&1
RC=$?
DURATION_S="$SECONDS"

# --- Adjacent structured JSON result receipt (additive-only; see header comment) ----------------
write_receipt "$OUT_FILE" "$RC" "$DURATION_S" "$INVOKED_AT" "$MODEL" "$EFFORT" "$PROMPT_FILE" "$LAST_MSG_FILE" "$FRESH_ARTIFACT" 2>/dev/null \
  || echo "[codex_ask] warning: failed to write result receipt (non-fatal, raw output at ${OUT_FILE} is unaffected)" >&2

echo "$OUT_FILE"
exit "$RC"
