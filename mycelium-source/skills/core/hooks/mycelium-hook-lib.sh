#!/usr/bin/env bash

# Shared compatibility helpers for Claude Code and Codex hooks.

mycelium_prepare_state_dir() {
  local repo_root="$1"
  local mode="${2:-write}"
  local requested_state=""
  local living_dir=""
  local legacy_dir=""
  local legacy_session=""
  local unsafe_link=""
  local plugin_pointer=""
  local pointer_tmp=""

  repo_root=$(cd "$repo_root" 2>/dev/null && pwd -P) || return 1
  living_dir="$repo_root/.living"

  # These hooks run from a globally trusted plugin but operate on an
  # untrusted checkout. Never follow repository-controlled symlinks for state
  # or lifecycle output: doing so would turn a normal hook into an arbitrary
  # out-of-project writer.
  if [[ -L "$living_dir" ]]; then
    return 1
  fi
  if [[ -d "$living_dir" ]]; then
    unsafe_link=$(find "$living_dir" -type l -print -quit 2>/dev/null || true)
    if [[ -n "$unsafe_link" ]]; then
      return 1
    fi
  fi

  requested_state="${MYCELIUM_STATE_DIR:-$repo_root/.mycelium}"
  requested_state=$(python3 - "$repo_root" "$requested_state" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
candidate = Path(sys.argv[2])
if not candidate.is_absolute():
    candidate = root / candidate
candidate = Path(os.path.abspath(candidate))
try:
    relative = candidate.relative_to(root)
except ValueError:
    raise SystemExit(1)
if not relative.parts:
    raise SystemExit(1)

# Reject every existing symlink or non-directory component before mkdir. This
# validates the path before the shell can follow a repository-controlled link.
current = root
for part in relative.parts:
    current = current / part
    try:
        mode = current.lstat().st_mode
    except FileNotFoundError:
        continue
    except OSError:
        raise SystemExit(1)
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise SystemExit(1)

try:
    candidate.resolve(strict=False).relative_to(root)
except (OSError, RuntimeError, ValueError):
    raise SystemExit(1)
print(candidate)
PY
  ) || return 1
  if [[ "$mode" == "read-only" ]]; then
    [[ -d "$requested_state" ]] || return 1
  else
    mkdir -p "$requested_state" || return 1
  fi
  STATE_DIR=$(cd "$requested_state" 2>/dev/null && pwd -P) || return 1
  case "$STATE_DIR" in
    "$repo_root"/*) ;;
    *) return 1 ;;
  esac
  unsafe_link=$(find "$STATE_DIR" -type l -print -quit 2>/dev/null || true)
  if [[ -n "$unsafe_link" ]]; then
    return 1
  fi

  # Ownership preflight for host-identified PostToolUse events must happen
  # before state initialization, plugin-pointer refresh, or legacy migration.
  if [[ "$mode" == "read-only" ]]; then
    return 0
  fi

  if [[ ! -f "$STATE_DIR/.gitignore" ]]; then
    printf '*\n!.gitignore\n' > "$STATE_DIR/.gitignore"
  fi

  # Codex plugin hooks carry their live bundle root through the dispatcher.
  # Refresh the generated guidance pointer only after the shared state safety
  # checks above, and replace it atomically rather than following an existing
  # path with shell redirection.
  if [[ -n "${MYCELIUM_PLUGIN_ROOT:-}" ]]; then
    plugin_pointer="$STATE_DIR/plugin-root"
    if [[ -L "$plugin_pointer" \
      || ( -e "$plugin_pointer" && ! -f "$plugin_pointer" ) ]]; then
      return 1
    fi
    if [[ ! -f "$plugin_pointer" \
      || "$(cat "$plugin_pointer" 2>/dev/null || true)" != "$MYCELIUM_PLUGIN_ROOT" ]]; then
      pointer_tmp=$(mktemp "$STATE_DIR/.plugin-root.tmp.XXXXXX") || return 1
      if ! printf '%s\n' "$MYCELIUM_PLUGIN_ROOT" > "$pointer_tmp" \
        || ! mv -f "$pointer_tmp" "$plugin_pointer"; then
        rm -f "$pointer_tmp"
        return 1
      fi
    fi
  fi

  # Preserve cross-session context from projects initialized before v0.4.
  legacy_dir="$repo_root/.claude"
  legacy_session="$legacy_dir/last-session.md"
  if [[ ! -f "$STATE_DIR/last-session.md" \
    && -d "$legacy_dir" \
    && ! -L "$legacy_dir" \
    && -f "$legacy_session" \
    && ! -L "$legacy_session" ]]; then
    cp "$legacy_session" "$STATE_DIR/last-session.md"
  fi

  return 0
}

mycelium_read_active_log_marker() {
  local repo_root="$1"
  local marker_file="$2"
  local raw_path=""
  local owner_ts=""
  local ownership_format=""
  local line_count=""
  local safe_path=""

  [[ -f "$marker_file" && ! -L "$marker_file" ]] || return 1
  raw_path=$(sed -n '1p' "$marker_file" 2>/dev/null || true)
  owner_ts=$(sed -n '2p' "$marker_file" 2>/dev/null || true)
  ownership_format=$(sed -n '3p' "$marker_file" 2>/dev/null || true)
  line_count=$(awk 'END { print NR }' "$marker_file" 2>/dev/null || true)
  [[ -n "$raw_path" && "$owner_ts" =~ ^[0-9]{1,18}$ ]] || return 1
  if [[ "$line_count" == 2 ]]; then
    [[ -z "$ownership_format" ]] || return 1
  elif [[ "$line_count" == 3 ]]; then
    [[ "$ownership_format" == "owner-id-v1" ]] || return 1
  else
    return 1
  fi

  safe_path=$(python3 - "$repo_root" "$raw_path" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
raw = Path(sys.argv[2])
if not raw.is_absolute():
    raise SystemExit(1)
log_root = (root / ".living" / "log").resolve(strict=False)
candidate = Path(os.path.abspath(raw))
try:
    relative = candidate.resolve(strict=False).relative_to(log_root)
except (OSError, RuntimeError, ValueError):
    raise SystemExit(1)
if len(relative.parts) != 1:
    raise SystemExit(1)
try:
    mode = candidate.lstat().st_mode
except FileNotFoundError:
    pass
except OSError:
    raise SystemExit(1)
else:
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise SystemExit(1)
print(candidate)
PY
  ) || return 1
  [[ -n "$safe_path" ]] || return 1
  printf '%s\n%s\n' "$safe_path" "$owner_ts"
  if [[ -n "$ownership_format" ]]; then
    printf '%s\n' "$ownership_format"
  fi
}

mycelium_read_session_owner_id() {
  local owner_file="$1"
  local owner_id=""
  local line_count=""

  [[ -f "$owner_file" && ! -L "$owner_file" ]] || return 1
  owner_id=$(sed -n '1p' "$owner_file" 2>/dev/null || true)
  line_count=$(awk 'END { print NR }' "$owner_file" 2>/dev/null || true)
  [[ "$owner_id" =~ ^[A-Za-z0-9._-]+$ \
    && ${#owner_id} -le 200 \
    && "$line_count" == 1 ]] || return 1
  printf '%s\n' "$owner_id"
}

mycelium_payload_owns_active_session() {
  local repo_root="$1"
  local input="$2"
  local marker_file="$STATE_DIR/active-session-log.tmp"
  local owner_file="$STATE_DIR/active-session-owner-id.tmp"
  local marker=""
  local owner_format=""
  local owner_id=""
  local host_session_id=""
  local host_identified=false

  host_session_id=$(printf '%s' "$input" | mycelium_json_get 'session_id')
  if [[ -n "$host_session_id" ]]; then
    [[ "$host_session_id" =~ ^[A-Za-z0-9._-]+$ \
      && ${#host_session_id} -le 200 ]] || return 1
    host_identified=true
  fi

  # A host-identified event with no active transaction is a delayed event from
  # a completed/superseded task. Only payloads from legacy hosts that omit a
  # session identity may retain the pre-owner compatibility behavior.
  if [[ ! -f "$marker_file" ]]; then
    [[ "$host_identified" != true \
      && ! -e "$owner_file" && ! -L "$owner_file" ]]
    return
  fi
  if ! marker=$(mycelium_read_active_log_marker "$repo_root" "$marker_file"); then
    # A legacy/corrupt marker with no owner token cannot authorize a log write,
    # but it also must not disable independent activity enforcement. A claimed
    # host-owned transaction remains fail-closed.
    [[ "$host_identified" != true \
      && ! -e "$owner_file" && ! -L "$owner_file" ]]
    return
  fi
  owner_format=$(printf '%s\n' "$marker" | sed -n '3p')
  if [[ "$owner_format" != "owner-id-v1" \
    && ! -e "$owner_file" && ! -L "$owner_file" ]]; then
    return 0
  fi
  owner_id=$(mycelium_read_session_owner_id "$owner_file") || return 1
  [[ "$host_identified" == true && "$host_session_id" == "$owner_id" ]]
}

mycelium_active_transaction_present() {
  # True iff some session currently owns a LIVE transaction in this worktree,
  # i.e. a valid active-session marker exists. Unlike
  # mycelium_payload_owns_active_session this deliberately does NOT require the
  # calling payload to be that owner: a genuine concurrent FOREIGN writer must
  # still have its per-edit provenance recorded while a transaction is live
  # (the Stop hook later resolves the true last writer of each path). It rejects
  # only the late/delayed event that arrives when NO transaction is active at
  # all -- a completed or superseded task whose marker is already gone. The
  # marker is validated safely (symlinks and out-of-tree log paths rejected) by
  # mycelium_read_active_log_marker. Requires STATE_DIR to be set by a prior
  # mycelium_prepare_state_dir call.
  local repo_root="$1"
  mycelium_read_active_log_marker \
    "$repo_root" "$STATE_DIR/active-session-log.tmp" >/dev/null 2>&1
}

mycelium_prepare_post_tool_state() {
  local repo_root="$1"
  local input="$2"
  local host_session_id=""

  host_session_id=$(printf '%s' "$input" | mycelium_json_get 'session_id')
  if [[ -n "$host_session_id" ]]; then
    # An identified payload cannot create markerless state. Validate the
    # existing transaction without mutation, perform normal preparation only
    # for its owner, then revalidate to close the preparation race.
    mycelium_prepare_state_dir "$repo_root" read-only || return 1
    mycelium_payload_owns_active_session "$repo_root" "$input" || return 1
    mycelium_prepare_state_dir "$repo_root" || return 1
    mycelium_payload_owns_active_session "$repo_root" "$input" || return 1
  else
    # Identity-free payloads retain compatibility with hosts predating session
    # IDs, including their markerless activity enforcement.
    mycelium_prepare_state_dir "$repo_root" || return 1
    mycelium_payload_owns_active_session "$repo_root" "$input" || return 1
  fi
}

mycelium_registry_cell() {
  python3 -c '
import html
import sys
value = sys.stdin.read().replace("\r", " ").replace("\n", " ")
print(html.escape(" ".join(value.split()), quote=True).replace("|", "&#124;"))
'
}

mycelium_emit_stop_block() {
  local reason="$1"
  local escaped_reason=""
  escaped_reason=$(printf '%s' "$reason" | python3 -c \
    'import json, sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null) || return 1
  printf '{"decision": "block", "reason": %s}\n' "$escaped_reason"
}

mycelium_living_changed() {
  local repo_root="$1"
  local baseline_file="$2"
  local helper="$3"
  local reminder_ts="${4:-0}"
  local helper_status=2

  if [[ -f "$helper" && -f "$baseline_file" ]]; then
    if python3 "$helper" living-changed \
      --repo-root "$repo_root" \
      --baseline "$baseline_file" >/dev/null 2>&1; then
      return 0
    else
      helper_status=$?
    fi
    if [[ "$helper_status" -eq 1 ]]; then
      return 1
    fi
  fi

  # Rolling-upgrade fallback for sessions whose baseline predates content
  # fingerprints. Nanosecond mtimes avoid the old same-second false negative,
  # and every finding file is checked rather than only the directory mtime.
  python3 - "$repo_root" "$reminder_ts" <<'PY' >/dev/null 2>&1
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
try:
    threshold_ns = int(sys.argv[2]) * 1_000_000_000
except ValueError:
    threshold_ns = 0
living = root / ".living"
paths = [living / name for name in ("learnings.md", "decisions.md", "conventions.md")]
for directory_name in ("decisions", "findings"):
    directory = living / directory_name
    if directory.is_dir() and not directory.is_symlink():
        paths.extend(path for path in directory.rglob("*") if path.is_file())
for path in paths:
    try:
        if path.stat().st_mtime_ns > threshold_ns:
            raise SystemExit(0)
    except OSError:
        pass
raise SystemExit(1)
PY
}

mycelium_refresh_work_cycle() {
  local repo_root="$1"
  local helper="$2"
  local reminder_file="$STATE_DIR/mycelium-reminded.tmp"
  local baseline_file="$STATE_DIR/living-reminder-baseline.json"
  local reminder_ts=0
  local should_refresh=false

  if [[ ! -f "$reminder_file" ]]; then
    should_refresh=true
  else
    reminder_ts=$(head -1 "$reminder_file" 2>/dev/null || echo 0)
    if mycelium_living_changed \
      "$repo_root" "$baseline_file" "$helper" "$reminder_ts"; then
      should_refresh=true
    fi
  fi

  if [[ "$should_refresh" != true ]]; then
    return 1
  fi

  if [[ -f "$helper" ]]; then
    python3 "$helper" living-snapshot \
      --repo-root "$repo_root" \
      --output "$baseline_file" >/dev/null 2>&1 \
      || rm -f "$baseline_file"
  fi
  date +%s > "$reminder_file"
  return 0
}

mycelium_acquire_stop_lock() {
  local state_dir="$1"
  local attempts=0
  local max_attempts="${MYCELIUM_STOP_LOCK_MAX_ATTEMPTS:-600}"
  local owner_pid=""
  local owner_ts=""
  local now_ts=""
  local lock_mtime=0
  local owner_is_live=false
  local owner_is_dead=false

  if [[ ! "$max_attempts" =~ ^[1-9][0-9]*$ ]]; then
    max_attempts=600
  fi

  MYCELIUM_STOP_LOCK_DIR="$state_dir/mycelium-stop.lock"
  while ! mkdir "$MYCELIUM_STOP_LOCK_DIR" 2>/dev/null; do
    attempts=$((attempts + 1))
    owner_pid=""
    owner_ts=""
    owner_is_live=false
    owner_is_dead=false
    if [[ -f "$MYCELIUM_STOP_LOCK_DIR/owner" ]]; then
      read -r owner_pid owner_ts < "$MYCELIUM_STOP_LOCK_DIR/owner" || true
      if [[ "$owner_pid" =~ ^[0-9]+$ ]]; then
        if kill -0 "$owner_pid" 2>/dev/null; then
          owner_is_live=true
        else
          owner_is_dead=true
        fi
      fi
    fi
    now_ts=$(date +%s)
    lock_mtime=$(mycelium_file_mtime "$MYCELIUM_STOP_LOCK_DIR")
    # A recorded dead owner cannot still be in its critical section, so recover
    # immediately. Missing or malformed owner state may instead be the narrow
    # mkdir-to-owner publication window; retain the age guard for that case.
    if [[ "$owner_is_dead" == true ]] \
      || { [[ "$owner_is_live" != true && "$lock_mtime" =~ ^[0-9]+$ ]] \
        && (( now_ts - lock_mtime > 300 )); }; then
      rm -f "$MYCELIUM_STOP_LOCK_DIR/owner"
      rmdir "$MYCELIUM_STOP_LOCK_DIR" 2>/dev/null || true
      continue
    fi
    if (( attempts >= max_attempts )); then
      return 1
    fi
    sleep 0.05
  done
  printf '%s %s\n' "$$" "$(date +%s)" > "$MYCELIUM_STOP_LOCK_DIR/owner"
  return 0
}

mycelium_release_stop_lock() {
  if [[ -n "${MYCELIUM_STOP_LOCK_DIR:-}" ]]; then
    rm -f "$MYCELIUM_STOP_LOCK_DIR/owner"
    rmdir "$MYCELIUM_STOP_LOCK_DIR" 2>/dev/null || true
  fi
}

mycelium_knowledge_dir() {
  printf '%s\n' "${MYCELIUM_KNOWLEDGE_DIR:-$HOME/.mycelium/knowledge}"
}

mycelium_file_mtime() {
  local path="${1:-}"
  local value=""

  if [[ -z "$path" || ! -e "$path" ]]; then
    printf '0\n'
    return
  fi

  # GNU stat uses -c; BSD/macOS stat uses -f. GNU `stat -f "%m"` exits
  # successfully but prints a mount point, so validate the result before use.
  value=$(stat -c "%Y" "$path" 2>/dev/null || true)
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$value"
    return
  fi

  value=$(stat -f "%m" "$path" 2>/dev/null || true)
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$value"
    return
  fi

  printf '0\n'
}

mycelium_file_size() {
  local path="${1:-}"
  local value=""

  if [[ -z "$path" || ! -e "$path" ]]; then
    printf '0\n'
    return
  fi

  # As with mtimes, try GNU first and validate before falling back to BSD.
  # GNU `stat -f%z` can otherwise succeed with non-size filesystem output.
  value=$(stat -c "%s" "$path" 2>/dev/null || true)
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$value"
    return
  fi

  value=$(stat -f "%z" "$path" 2>/dev/null || true)
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$value"
    return
  fi

  printf '0\n'
}

mycelium_hook_host() {
  case "${MYCELIUM_HOOK_HOST:-}" in
    codex|claude) printf '%s\n' "$MYCELIUM_HOOK_HOST" ;;
    *) printf '%s\n' "claude" ;;
  esac
}

mycelium_json_get() {
  local dotted_path="$1"
  python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
    for key in sys.argv[1].split("."):
        value = value.get(key) if isinstance(value, dict) else None
    if value is not None and not isinstance(value, (dict, list)):
        print(value)
except Exception:
    pass
' "$dotted_path"
}

mycelium_bash_exit() {
  python3 -c '
import json, re, sys
try:
    response = json.load(sys.stdin).get("tool_response")
except Exception:
    response = None

keys = ("exit_code", "exitCode", "return_code", "returncode")

def find_structured(value):
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                return candidate
        for candidate in value.values():
            found = find_structured(candidate)
            if found is not None:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = find_structured(candidate)
            if found is not None:
                return found
    elif isinstance(value, str):
        # Code-mode local tools expose model-facing output as input_text blocks.
        # Their text can itself be the JSON serialization of the structured
        # command result, so recurse through that serialization.
        try:
            decoded = json.loads(value)
        except Exception:
            decoded = None
        if decoded is not None and decoded != value:
            found = find_structured(decoded)
            if found is not None:
                return found
    return None

def find_textual(value):
    if isinstance(value, dict):
        for candidate in value.values():
            found = find_textual(candidate)
            if found is not None:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = find_textual(candidate)
            if found is not None:
                return found
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except Exception:
            decoded = None
        if decoded is not None and decoded != value:
            found = find_textual(decoded)
            if found is not None:
                return found
        match = re.search(
            r"(?:exit(?:ed)?(?:[ _-]with)?(?:[ _-]code)?|return(?:[ _-]code)?)"
            r"[\":= ]+(-?\d+)",
            value,
            re.IGNORECASE,
        )
        if match:
            return int(match.group(1))
    return None

result = find_structured(response)
if result is None:
    result = find_textual(response)
if result is not None:
    print(result)
'
}

mycelium_emit_context() {
  local event="$1"
  local context="$2"
  local system_message="${3:-}"
  local host
  host=$(mycelium_hook_host)

  python3 - "$host" "$event" "$context" "$system_message" <<'PY'
import json
import sys

host, event, context, system_message = sys.argv[1:]
if host == "codex" and event == "Stop":
    payload = {"systemMessage": system_message or context}
else:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        }
    }
    if system_message:
        payload["systemMessage"] = system_message
print(json.dumps(payload))
PY
}

mycelium_record_provenance() {
  # Append genuine per-edit provenance to the shared, append-only ledger:
  # "<session_id> <relative-path>" per line, one line per touched path, in
  # call order. Runs for every root session that writes while a transaction is
  # LIVE, regardless of which session OWNS that transaction -- provenance is
  # "who actually wrote this path, when", not an ownership verdict, so gating it
  # on ownership would (as the prior foreign-only ledger did) leave a non-owner's
  # write completely unattributed. It IS gated on a live transaction existing at
  # all: a host-identified event with no active owner is a delayed event from a
  # completed/superseded task and must not create or mutate any .mycelium state
  # (the late-event gate below runs before any directory/baseline/cache/ledger
  # write). The Stop hook resolves, for each changed path, the LAST recorded
  # writer (append order = chronological order), so a later genuine edit by a
  # different session naturally reclaims that path instead of being permanently
  # excluded by an earlier touch.
  #
  # Usage:
  #   mycelium_record_provenance REPO_ROOT SESSION_ID PATH...
  #     Explicit paths already decoded from an Edit/Write/apply_patch
  #     payload (exact, no scan needed).
  #   mycelium_record_provenance REPO_ROOT SESSION_ID --scan HELPER
  #     Bash-originated writes: the exact touched paths are not decodable
  #     from arbitrary command text, so diff a rolling content-fingerprint
  #     snapshot (session_file_changes.py) against the last provenance scan
  #     (by any session) and attribute that delta to this invocation.
  local repo_root="$1"
  local session_id="$2"
  shift 2

  [[ -n "$session_id" && "$session_id" =~ ^[A-Za-z0-9._-]+$ ]] || return 0

  # Late-event gate (must run BEFORE any state creation): a host-identified
  # PostToolUse event with no active transaction is a delayed event from a
  # completed or superseded task, and must not create or mutate ANY .mycelium
  # state -- not the state directory, and not the baseline/cache/ledger. Probe
  # the existing transaction WITHOUT creating state first (read-only prepare
  # never mkdirs), then require a live transaction before proceeding. A live
  # transaction owned by a DIFFERENT (foreign) session still records here:
  # provenance is "who actually wrote this path", so a genuine concurrent
  # foreign writer is attributed, not dropped -- only the no-live-owner late
  # event is rejected. (Regression: test_late_host_post_tool_use_cannot_mutate_
  # without_an_active_transaction; positive neighbors: the active foreign-owner
  # probes in test_observer_writer_attribution.sh and
  # test_active_transaction_records_foreign_writer_provenance.)
  mycelium_prepare_state_dir "$repo_root" read-only >/dev/null 2>&1 || return 0
  mycelium_active_transaction_present "$repo_root" || return 0

  # A live transaction is present; prepare in write mode to fold in this
  # event's provenance rows.
  mycelium_prepare_state_dir "$repo_root" >/dev/null 2>&1 || return 0
  local ledger="$STATE_DIR/mycelium-foreign-activity.tmp"
  if [[ -e "$ledger" && ( ! -f "$ledger" || -L "$ledger" ) ]]; then
    return 0
  fi

  if [[ "${1:-}" == "--scan" ]]; then
    local helper="$2"
    [[ -n "$helper" && -f "$helper" ]] || return 0
    local baseline="$STATE_DIR/mycelium-provenance-baseline.json"
    local cache="$STATE_DIR/mycelium-fingerprint-cache.json"
    # Single-pass rolling scan (r2 provenance-latency fix): `scan` computes the
    # delta since the last baseline AND rewrites the shared baseline + private
    # fingerprint cache from ONE worktree read, reusing an unchanged path's
    # cached fingerprint instead of re-hashing. This replaces the prior
    # collect+snapshot pair, which content-hashed the whole dirty tree TWICE on
    # every Bash tool call (minutes on a large untracked data tree). `scan` also
    # appends this observing session's provenance rows to the shared ledger
    # INSIDE its locked baseline transaction (seq90), so a read-only observer
    # can never overtake a genuine explicit writer for a path -- the shell no
    # longer appends here. With no baseline yet, `scan` only establishes a
    # starting point and attributes nothing, so a first observation never
    # attributes the pre-existing tree to this session.
    python3 "$helper" scan \
      --repo-root "$repo_root" \
      --baseline "$baseline" \
      --cache "$cache" \
      --ledger "$ledger" \
      --session-id "$session_id" >/dev/null 2>&1 || true
  else
    # Explicitly-attributed writes (Edit/Write/apply_patch): advance-baseline
    # appends this writer's provenance rows to the shared ledger AND folds the
    # paths into the shared baseline + private cache INSIDE one locked
    # transaction (seq90). The shell no longer appends the ledger itself: doing
    # it outside the lock let a concurrent read-only observer scan interleave and
    # become the last recorded writer for a path this session created. The append
    # happens even before a baseline exists (so a pre-baseline write is still
    # attributed); the baseline/cache fold is a no-op until the first --scan
    # establishes a baseline. A genuine LATER writer still reclaims the path (its
    # scan/advance appends last). Best-effort; never blocks the caller.
    local _sfc_helper="${MYCELIUM_SESSION_CHANGES_HELPER:-${BASH_SOURCE[0]%/*}/../scripts/session_file_changes.py}"
    local _prov_baseline="$STATE_DIR/mycelium-provenance-baseline.json"
    local _prov_cache="$STATE_DIR/mycelium-fingerprint-cache.json"
    if [[ -f "$_sfc_helper" ]]; then
      local _adv_args=(advance-baseline --repo-root "$repo_root" \
        --baseline "$_prov_baseline" --cache "$_prov_cache" \
        --ledger "$ledger" --session-id "$session_id")
      local _prov_path
      for _prov_path in "$@"; do
        [[ -z "$_prov_path" ]] && continue
        _adv_args+=(--path "$_prov_path")
      done
      python3 "$_sfc_helper" "${_adv_args[@]}" >/dev/null 2>&1 || true
    fi
  fi
}

mycelium_archive_foreign_provenance() {
  # Preserve a deferred second-root-writer's per-edit provenance BEFORE the
  # accepting owner's Stop cleanup removes the transient ledger
  # (mycelium-foreign-activity.tmp). Accepted finding:
  # work/lifecycle-deferred-evidence-probe.json -- without this the owner's
  # cleanup deletes the ledger, destroying a concurrent writer's attribution
  # while its work file survives.
  #
  # BOUNDED DURABLE-JOURNAL HANDOFF (not a multi-owner finalization redesign).
  # The ONLY mutation is a single atomic rename of the ledger to a UNIQUELY
  # named durable journal (mycelium-foreign-activity.journal.XXXXXX). A journal
  # is NEVER unlinked and NEVER restored over the ledger path; every
  # session-tagged row is kept verbatim. A filtered foreign-only view is
  # DERIVED on demand from the journal(s), never the source of truth.
  #
  # Why an unconditional rename with no read, no filter, and no unlink:
  # mycelium_record_provenance appends with a plain `printf >> ledger` and
  # takes NO Stop lock. The redirection opens the inode, then the builtin
  # writes; ordinary scheduling can run this cleanup BETWEEN the open and the
  # write. Any design that reads-to-classify then conditionally unlinks/rm's
  # (even "owner-only") or restores loses that in-flight row -- reproduced in
  # the retained probes:
  #   work/archive-helper-review-probes.json (swallowed read failure; symlinked
  #     append target followed; append after snapshot lost),
  #   work/archive-handoff-review-probes.json (restore over a fresh ledger;
  #     unlinked claimed inode; reused-name claim clobbered),
  #   work/journal-owner-only-interleaving-probe-corrected.json (the owner-only
  #     unlink freed the inode a paused per-line append still targeted).
  # Renaming the whole inode to a name that is never removed preserves every
  # such in-flight write:
  #   - a redirection opened BEFORE this rename keeps writing into the SAME
  #     inode, now the durable journal (never unlinked) -- preserved;
  #   - a redirection opened AFTER this rename creates a fresh ledger at the
  #     original path, left untouched for the next cleanup -- preserved.
  # Retaining a small raw journal is deliberately preferred over losing
  # attribution; bounding journal growth is left to a separate, safe reaper and
  # is out of scope here.
  #
  # Owns disposal of the transient ledger; the caller MUST NOT delete it.
  # return 0 = handed off (or nothing present); nonzero = ledger left in place
  # for a later retry (failure-safe). Callers ignore the status.
  #
  # Usage: mycelium_archive_foreign_provenance STATE_DIR OWNER_SESSION_ID
  #   OWNER_SESSION_ID is accepted for signature stability and future derived
  #   foreign-only attribution; the raw handoff itself is owner-agnostic and
  #   preserves ALL rows, so it is not consulted here.
  local state_dir="$1"
  local ledger="$state_dir/mycelium-foreign-activity.tmp"
  [[ -e "$ledger" || -L "$ledger" ]] || return 0
  # A symlink or non-regular ledger is never followed, renamed, or deleted.
  [[ -L "$ledger" || ! -f "$ledger" ]] && return 1
  # Atomic durable handoff to a unique name (mktemp reserves it, so a prior
  # attempt's journal is never reused or clobbered). Never unlinked, never
  # restored. If the rename fails the ledger stays in place -> retry next time.
  local journal
  journal=$(mktemp "$state_dir/mycelium-foreign-activity.journal.XXXXXX" 2>/dev/null) || return 1
  if ! mv -f "$ledger" "$journal" 2>/dev/null; then
    rm -f "$journal" 2>/dev/null || true   # remove only the empty mktemp placeholder
    return 1
  fi
  return 0
}
