#!/usr/bin/env bash
# Offline regression for the core-overlay hooks (request-reduction v1 items 3 and 4):
#   (b) duplicate housekeeping triggers deduplicate, failures are retained, exhaustion notifies once;
#   (c) a busy Stop lock yields exactly ONE actionable block, a repeat is silent, and the lock is
#       reconciled once the live owner releases it.
# No model, no network. Runs under the host bash + python3.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OVERLAY="$REPO/core-overlay/skills/core"
LEDGER_PY="$OVERLAY/scripts/housekeeping_ledger.py"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/core-overlay-test.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
jget() { python3 -c 'import json,sys; d=json.load(sys.stdin); v=d
for k in sys.argv[1].split("."): v=v.get(k) if isinstance(v,dict) else None
print("" if v is None else v)' "$1"; }

# ---------------------------------------------------------------- (b) ledger
L="$TMP/ledger.json"; T0=1000000
d=$(python3 "$LEDGER_PY" decide knowledge-audit --ledger "$L" --now $T0 --last-success-ts 0)
[ "$(printf '%s' "$d" | jget action)" = dispatch ] || fail "first decide should dispatch: $d"
A1=$(printf '%s' "$d" | jget attempt_id)
d=$(python3 "$LEDGER_PY" decide knowledge-audit --ledger "$L" --now $((T0+60)) --last-success-ts 0)
[ "$(printf '%s' "$d" | jget reason)" = in_flight ] || fail "duplicate trigger must skip in_flight: $d"
# in-flight TTL (120 min) elapsed -> recorded as a failure, then a fresh attempt
d=$(python3 "$LEDGER_PY" decide knowledge-audit --ledger "$L" --now $((T0+7300)) --last-success-ts 0 --cooldown-minutes 0)
[ "$(printf '%s' "$d" | jget action)" = dispatch ] || fail "after TTL a new attempt is allowed: $d"
[ "$(printf '%s' "$d" | jget attempts_before)" = 1 ] || fail "timed-out attempt must count as failure: $d"
A2=$(printf '%s' "$d" | jget attempt_id)
[ "$A1" != "$A2" ] || fail "attempt ids must differ"
# explicit failures up to exhaustion (max 3): attempts = 1 (timeout) + 2 fails -> exhausted
python3 "$LEDGER_PY" fail knowledge-audit --ledger "$L" --attempt-id "$A2" --reason "worker died" --now $((T0+7400)) >/dev/null
d=$(python3 "$LEDGER_PY" decide knowledge-audit --ledger "$L" --now $((T0+7401)) --last-success-ts 0 --cooldown-minutes 0)
[ "$(printf '%s' "$d" | jget action)" = dispatch ] || fail "third attempt allowed: $d"
A3=$(printf '%s' "$d" | jget attempt_id)
python3 "$LEDGER_PY" fail knowledge-audit --ledger "$L" --attempt-id "$A3" --reason "worker died again" --now $((T0+7500)) >/dev/null
d=$(python3 "$LEDGER_PY" decide knowledge-audit --ledger "$L" --now $((T0+7501)) --last-success-ts 0)
[ "$(printf '%s' "$d" | jget reason)" = exhausted ] || fail "must be exhausted after 3 failures: $d"
[ "$(printf '%s' "$d" | jget notify)" = True ] || fail "exhaustion notifies once: $d"
d=$(python3 "$LEDGER_PY" decide knowledge-audit --ledger "$L" --now $((T0+7502)) --last-success-ts 0)
[ "$(printf '%s' "$d" | jget notify)" = False ] || fail "second exhausted decide must not notify: $d"
[ "$(python3 "$LEDGER_PY" status knowledge-audit --ledger "$L" | jget record.failures | python3 -c 'import sys,ast; print(len(ast.literal_eval(sys.stdin.read())))')" = 3 ] || fail "failure evidence retained"
# completion resets attempts and preserves success time; a fresh success skips
python3 "$LEDGER_PY" complete knowledge-audit --ledger "$L" --attempt-id "$A3" --now $((T0+8000)) >/dev/null
d=$(python3 "$LEDGER_PY" decide knowledge-audit --ledger "$L" --now $((T0+8001)) --last-success-ts 0)
[ "$(printf '%s' "$d" | jget reason)" = fresh ] || fail "after completion the kind is fresh: $d"
# legacy success marker newer than the ledger is honored
d=$(python3 "$LEDGER_PY" decide knowledge-audit --ledger "$L" --now $((T0+200000)) --last-success-ts $((T0+199000)))
[ "$(printf '%s' "$d" | jget reason)" = fresh ] || fail "legacy marker honored: $d"
# corrupt ledger never dispatches twice in a row and is preserved beside a fresh one
echo '{not json' > "$L"
d=$(python3 "$LEDGER_PY" decide knowledge-audit --ledger "$L" --now $((T0+300000)) --last-success-ts 0)
[ "$(printf '%s' "$d" | jget action)" = dispatch ] || fail "fresh ledger after corruption: $d"
ls "$L".corrupt-* >/dev/null 2>&1 || fail "corrupt ledger must be preserved"
echo "ok (b) ledger accounting"

# ------------------------------------------------- (b2) health hook uses the ledger
export HOME="$TMP/home"; mkdir -p "$HOME/.mycelium/knowledge"
echo "1 daily-audit" > "$HOME/.mycelium/knowledge/.last-audit"
R="$TMP/repo"; mkdir -p "$R"; git -C "$R" init -q; git -C "$R" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
HOOK_IN='{"session_id":"sess-A","source":"startup","cwd":"'"$R"'"}'
out1=$(cd "$R" && printf '%s' "$HOOK_IN" | bash "$OVERLAY/hooks/mycelium-health.sh" 2>/dev/null || true)
printf '%s' "$out1" | grep -q "KNOWLEDGE AUDIT DUE" || fail "first startup dispatches the audit: $out1"
printf '%s' "$out1" | grep -q "attempt knowledge-audit-" || fail "dispatch carries an attempt id: $out1"
out2=$(cd "$R" && printf '%s' "$HOOK_IN" | bash "$OVERLAY/hooks/mycelium-health.sh" 2>/dev/null || true)
printf '%s' "$out2" | grep -q "KNOWLEDGE AUDIT DUE" && fail "second startup must NOT re-dispatch while in flight: $out2"
LEDGER_HOME="$HOME/.mycelium/knowledge/.housekeeping-ledger.json"
[ -f "$LEDGER_HOME" ] || fail "ledger written beside the knowledge dir"
[ "$(python3 "$LEDGER_PY" status knowledge-audit --ledger "$LEDGER_HOME" | jget record.state)" = in_flight ] || fail "ledger state in_flight"
echo "ok (b2) health hook dedup"

# ------------------------------------------------- (c) Stop lock contention
S="$R/.living/.state"; mkdir -p "$S"
# find the state dir the lib actually uses
STATE_DIR=$(cd "$R" && bash -c 'source "'"$OVERLAY"'/hooks/mycelium-hook-lib.sh"; mycelium_prepare_state_dir "'"$R"'" >/dev/null; printf "%s" "$STATE_DIR"')
[ -n "$STATE_DIR" ] || fail "state dir resolved"
sleep 300 & OWNER=$!
mkdir -p "$STATE_DIR/mycelium-stop.lock"; printf '%s %s\n' "$OWNER" "$(date +%s)" > "$STATE_DIR/mycelium-stop.lock/owner"
STOP_IN='{"session_id":"sess-A","stop_hook_active":false,"cwd":"'"$R"'"}'
s1=$(cd "$R" && MYCELIUM_STOP_LOCK_MAX_ATTEMPTS=3 bash -c 'printf "%s" "$0" | bash "$1"' "$STOP_IN" "$OVERLAY/hooks/mycelium-stop-check.sh" 2>/dev/null || true)
printf '%s' "$s1" | grep -q '"decision": "block"' || fail "first busy Stop blocks once: $s1"
printf '%s' "$s1" | grep -q "mycelium-stop-lock-contention.json" || fail "block names the evidence path: $s1"
STOP_IN2='{"session_id":"sess-A","stop_hook_active":true,"cwd":"'"$R"'"}'
s2=$(cd "$R" && MYCELIUM_STOP_LOCK_MAX_ATTEMPTS=3 bash -c 'printf "%s" "$0" | bash "$1"' "$STOP_IN2" "$OVERLAY/hooks/mycelium-stop-check.sh" 2>/dev/null || true)
[ -z "$s2" ] || fail "repeat busy Stop must be silent (no second retry prompt): $s2"
[ "$(jget repeats < "$STATE_DIR/mycelium-stop-lock-contention.json")" = 1 ] || fail "repeat counted in sentinel"
[ "$(jget state < "$STATE_DIR/mycelium-stop-lock-contention.json")" = unresolved ] || fail "sentinel unresolved while owner live"
[ -d "$STATE_DIR/mycelium-stop.lock" ] || fail "live owner was never force-unlocked"
kill "$OWNER" 2>/dev/null; wait "$OWNER" 2>/dev/null || true
s3=$(cd "$R" && MYCELIUM_STOP_LOCK_MAX_ATTEMPTS=3 bash -c 'printf "%s" "$0" | bash "$1"' "$STOP_IN" "$OVERLAY/hooks/mycelium-stop-check.sh" 2>/dev/null || true)
printf '%s' "$s3" | grep -q "lock remained busy" && fail "dead owner must be reclaimed: $s3"
[ "$(jget state < "$STATE_DIR/mycelium-stop-lock-contention.json")" = reconciled ] || fail "sentinel reconciled after acquire"
[ ! -d "$STATE_DIR/mycelium-stop.lock" ] || fail "lock released after Stop"
echo "ok (c) stop lock contention: one block, silent repeat, reconciled"
echo "PASS test_core_overlay"
