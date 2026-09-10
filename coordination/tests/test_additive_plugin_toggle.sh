#!/usr/bin/env bash
# Focused +/- checks for scripts/additive_plugin_toggle.py — the durable, guarded ADDITIVE
# [plugins."<id>"].enabled toggle for the personal-marketplace activation method. Modeled on
# coordination/tests/test_stage_codex_activation.sh. All checks use DISPOSABLE configs under
# $TMP; the real ~/.codex/config.toml is never touched (asserted by a sha sentinel at the end),
# and this script never invokes `codex plugin ...`.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="$REPO/scripts/additive_plugin_toggle.py"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0; pass=0
ok(){ pass=$((pass+1)); echo "PASS: $1"; }
no(){ fail=$((fail+1)); echo "FAIL: $1"; }
REAL="$HOME/.codex/config.toml"
REAL_SHA_BEFORE="$( [ -f "$REAL" ] && shasum -a256 "$REAL" | cut -d' ' -f1 || echo none )"

parses(){ python3 -c "import tomllib;tomllib.load(open('$1','rb'));print('ok')" 2>/dev/null; }
val_of(){ python3 -c "import tomllib;d=tomllib.load(open('$1','rb'));print(d$2)" 2>/dev/null; }
mode_of(){ stat -f "%OLp" "$1" 2>/dev/null || stat -c "%a" "$1" 2>/dev/null; }

PID="mycelium@personal"
KEYPATH="['plugins']['$PID']['enabled']"

mk_cfg(){ local c="$1" enabled="$2"; cat > "$c" <<EOF
[marketplaces.mycelium]
source_type = "local"
source = "/Users/mst36/tools/mycelium-main"

[plugins."mycelium@mycelium"]
enabled = false

[plugins."$PID"] # additive personal namespace
enabled = $enabled
EOF
}

# --- Test 1: apply positive — flips only the owned key, siblings preserved, valid TOML ------------
C="$TMP/t1.toml"; R="$TMP/t1.receipt.json"
mk_cfg "$C" false
python3 "$PY" apply "$C" --plugin-id "$PID" --enabled true --receipt "$R" >/dev/null 2>&1; rc=$?
{ [ "$rc" = "0" ] && [ "$(val_of "$C" "$KEYPATH")" = "True" ] \
  && [ "$(val_of "$C" "['plugins']['mycelium@mycelium']['enabled']")" = "False" ] \
  && [ "$(val_of "$C" "['marketplaces']['mycelium']['source']")" = "/Users/mst36/tools/mycelium-main" ] \
  && [ "$(parses "$C")" = "ok" ] && grep -q '# additive personal namespace' "$C"; } \
  && ok "apply positive: owned key flipped, siblings + comment preserved, valid TOML" \
  || no "apply positive (rc=$rc)"

# --- Test 2: mode 0600 PRESERVED across apply (THE key requirement) -------------------------------
C="$TMP/t2.toml"; R="$TMP/t2.receipt.json"
mk_cfg "$C" false; chmod 600 "$C"
before_mode="$(mode_of "$C")"
python3 "$PY" apply "$C" --plugin-id "$PID" --enabled true --receipt "$R" >/dev/null 2>&1
after_mode="$(mode_of "$C")"
{ [ "$before_mode" = "600" ] && [ "$after_mode" = "600" ]; } \
  && ok "mode 0600 preserved across apply (before=$before_mode after=$after_mode)" \
  || no "mode NOT preserved (before=$before_mode after=$after_mode)"

# --- Test 3: only the ONE owned key changed (full-dict diff, owned key normalized) -----------------
C="$TMP/t3.toml"; R="$TMP/t3.receipt.json"
mk_cfg "$C" false
python3 - "$C" > "$TMP/t3.before.json" <<PY
import tomllib,json,sys
d=tomllib.load(open(sys.argv[1],'rb')); d['plugins']['$PID'].pop('enabled',None)
json.dump(d, sys.stdout, sort_keys=True)
PY
python3 "$PY" apply "$C" --plugin-id "$PID" --enabled true --receipt "$R" >/dev/null 2>&1
python3 - "$C" > "$TMP/t3.after.json" <<PY
import tomllib,json,sys
d=tomllib.load(open(sys.argv[1],'rb')); d['plugins']['$PID'].pop('enabled',None)
json.dump(d, sys.stdout, sort_keys=True)
PY
{ cmp -s "$TMP/t3.before.json" "$TMP/t3.after.json" && [ "$(val_of "$C" "$KEYPATH")" = "True" ]; } \
  && ok "only the owned key changed (rest of parsed config identical)" \
  || no "collateral change detected in non-owned config"

# --- Test 4: no-op when already at target (idempotent, no receipt overwrite surprise) --------------
C="$TMP/t4.toml"; R="$TMP/t4.receipt.json"
mk_cfg "$C" true
out="$(python3 "$PY" apply "$C" --plugin-id "$PID" --enabled true --receipt "$R" 2>&1)"; rc=$?
{ [ "$rc" = "0" ] && printf '%s' "$out" | grep -qi "no changes needed" && [ ! -f "$R" ]; } \
  && ok "apply no-op when already at target; no receipt written" \
  || no "apply no-op behavior (rc=$rc)"

# --- Test 5: missing table -> clear error, nothing written, no receipt -----------------------------
C="$TMP/t5.toml"; R="$TMP/t5.receipt.json"
cat > "$C" <<EOF
[marketplaces.mycelium]
source = "x"
EOF
sha_before="$(shasum -a256 "$C" | cut -d' ' -f1)"
out="$(python3 "$PY" apply "$C" --plugin-id "mycelium@absent" --enabled true --receipt "$R" 2>&1)"; rc=$?
sha_after="$(shasum -a256 "$C" | cut -d' ' -f1)"
{ [ "$rc" = "1" ] && printf '%s' "$out" | grep -qi "not found" && [ ! -f "$R" ] && [ "$sha_before" = "$sha_after" ]; } \
  && ok "missing table -> clear error, config untouched, no receipt" \
  || no "missing table handling (rc=$rc)"

# --- Test 6: DRIFT GUARD — owned key changed between read and commit -> refuse, nothing written ----
C="$TMP/t6.toml"; R="$TMP/t6.receipt.json"
mk_cfg "$C" false
sha_before="$(shasum -a256 "$C" | cut -d' ' -f1)"
out="$(APT_TEST_INJECT_TOGGLE_BEFORE_COMMIT=true python3 "$PY" apply "$C" --plugin-id "$PID" --enabled true --receipt "$R" 2>&1)"; rc=$?
{ [ "$rc" = "1" ] && printf '%s' "$out" | grep -qi "changed since it was read" && [ ! -f "$R" ] \
  && [ "$(val_of "$C" "$KEYPATH")" = "True" ]; } \
  && ok "drift guard: concurrent change caught, apply refused, receipt not left behind" \
  || no "drift guard apply (rc=$rc)"
# The injected concurrent value (true) must survive untouched (apply wrote NOTHING further).
[ "$(val_of "$C" "$KEYPATH")" = "True" ] && ok "drift guard: the concurrent writer's value survives" \
  || no "drift guard: concurrent value clobbered"

# --- Test 7: rollback restores the pre-apply value ---------------------------------------------------
C="$TMP/t7.toml"; R="$TMP/t7.receipt.json"
mk_cfg "$C" false
python3 "$PY" apply "$C" --plugin-id "$PID" --enabled true --receipt "$R" >/dev/null 2>&1
python3 "$PY" rollback "$C" --receipt "$R" >/dev/null 2>&1; rc=$?
{ [ "$rc" = "0" ] && [ "$(val_of "$C" "$KEYPATH")" = "False" ] && [ "$(parses "$C")" = "ok" ]; } \
  && ok "rollback restores pre-apply value" \
  || no "rollback restore (rc=$rc)"

# --- Test 8: rollback CONFLICT — owned key changed since apply -> refuse without --force -----------
C="$TMP/t8.toml"; R="$TMP/t8.receipt.json"
mk_cfg "$C" false
python3 "$PY" apply "$C" --plugin-id "$PID" --enabled true --receipt "$R" >/dev/null 2>&1
# Someone else flips it after apply, before rollback (hand-edit the specific line directly —
# this fixture must not depend on the tool-under-test to construct the conflict).
python3 - "$C" "$PID" <<'PY'
import sys
path, pid = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines(keepends=True)
out = []
in_tbl = False
target = f'[plugins."{pid}"]'
for ln in lines:
    if ln.strip().startswith('['):
        in_tbl = (ln.strip().split('#')[0].strip() == target)
    if in_tbl and ln.strip().startswith('enabled'):
        ln = 'enabled = false  # changed after apply\n'
    out.append(ln)
open(path, 'w').writelines(out)
PY
out="$(python3 "$PY" rollback "$C" --receipt "$R" 2>&1)"; rc=$?
{ [ "$rc" = "1" ] && printf '%s' "$out" | grep -qi "CONFLICT" && [ "$(val_of "$C" "$KEYPATH")" = "False" ]; } \
  && ok "rollback conflict refused without --force (post-apply edit preserved)" \
  || no "rollback conflict refusal (rc=$rc)"
# --force overrides the conflict and restores the pre-apply value anyway.
out="$(python3 "$PY" rollback "$C" --receipt "$R" --force 2>&1)"; rc=$?
{ [ "$rc" = "0" ] && [ "$(val_of "$C" "$KEYPATH")" = "False" ]; } \
  && ok "rollback --force overrides conflict and restores pre-apply value" \
  || no "rollback --force (rc=$rc)"

# --- Test 9: symlinked config — real target updated, symlink itself preserved ----------------------
D="$TMP/t9"; mkdir -p "$D"
REALCFG="$D/real.toml"; mk_cfg "$REALCFG" false; chmod 600 "$REALCFG"
LNK="$D/config.toml"; ln -s "$REALCFG" "$LNK"
VICTIM="$D/victim.txt"; echo "DO NOT TOUCH" > "$VICTIM"
ln -s "$VICTIM" "$D/.real.toml.apt-staging"      # hostile fixed-name staging symlink
ln -s "$VICTIM" "$D/.config.toml.apt-staging"
R="$TMP/t9.receipt.json"
python3 "$PY" apply "$LNK" --plugin-id "$PID" --enabled true --receipt "$R" >/dev/null 2>&1; rc=$?
{ [ "$rc" = "0" ] \
  && [ "$(cat "$VICTIM")" = "DO NOT TOUCH" ] \
  && [ -L "$LNK" ] && [ "$(readlink "$LNK")" = "$REALCFG" ] \
  && [ "$(val_of "$REALCFG" "$KEYPATH")" = "True" ] \
  && [ "$(mode_of "$REALCFG")" = "600" ] \
  && [ "$(parses "$REALCFG")" = "ok" ]; } \
  && ok "symlinked config: real target updated + mode preserved, symlink kept, no staging-symlink hijack" \
  || no "symlinked config handling (rc=$rc)"

# --- Test 10: apply with a nonexistent --receipt parent creates it; existing receipt refused --------
C="$TMP/t10.toml"; R="$TMP/nested/dir/t10.receipt.json"
mk_cfg "$C" false
python3 "$PY" apply "$C" --plugin-id "$PID" --enabled true --receipt "$R" >/dev/null 2>&1; rc=$?
{ [ "$rc" = "0" ] && [ -f "$R" ]; } && ok "apply creates nested receipt directory" || no "nested receipt dir (rc=$rc)"
C2="$TMP/t10b.toml"; mk_cfg "$C2" false
out="$(python3 "$PY" apply "$C2" --plugin-id "$PID" --enabled true --receipt "$R" 2>&1)"; rc=$?
{ [ "$rc" = "1" ] && printf '%s' "$out" | grep -qi "already exists"; } \
  && ok "apply refuses to overwrite an existing receipt path" \
  || no "existing receipt refusal (rc=$rc)"

# --- Sentinel: the REAL ~/.codex/config.toml was never touched, and no `codex` binary was invoked ---
REAL_SHA_AFTER="$( [ -f "$REAL" ] && shasum -a256 "$REAL" | cut -d' ' -f1 || echo none )"
[ "$REAL_SHA_BEFORE" = "$REAL_SHA_AFTER" ] \
  && ok "real ~/.codex/config.toml UNTOUCHED ($REAL_SHA_AFTER)" \
  || no "real config changed! before=$REAL_SHA_BEFORE after=$REAL_SHA_AFTER"

echo "----"; echo "$pass passed, $fail failed"
[ "$fail" = "0" ]
