#!/usr/bin/env bash
# Focused +/- checks for the seq36 config-helper repair: scripts/toml_owned_edit.py (TOML-aware
# owned-key apply/rollback) and the stage-codex-activation.sh wrapper. Covers the two reproduced
# defects plus the owned-key/conflict model. All checks use DISPOSABLE configs under $TMP; the real
# ~/.codex/config.toml is never touched (asserted by a sha sentinel at the end).
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="$REPO/scripts/toml_owned_edit.py"
WRAP="$REPO/scripts/stage-codex-activation.sh"
CAND="/Users/mst36/tools/mycelium-integration-wfi"
CAND2="/Users/mst36/tools/some-other-candidate"
OLD="/Users/mst36/tools/mycelium-main"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0; pass=0
ok(){ pass=$((pass+1)); echo "PASS: $1"; }
no(){ fail=$((fail+1)); echo "FAIL: $1"; }
REAL="$HOME/.codex/config.toml"
REAL_SHA_BEFORE="$( [ -f "$REAL" ] && shasum -a256 "$REAL" | cut -d' ' -f1 || echo none )"

parses(){ python3 -c "import tomllib;tomllib.load(open('$1','rb'));print('ok')" 2>/dev/null; }
val_of(){ python3 -c "import tomllib;d=tomllib.load(open('$1','rb'));print(d$2)" 2>/dev/null; }
ntables(){ python3 - "$1" <<PY
import importlib.util
spec=importlib.util.spec_from_file_location("toe","$PY"); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
import sys
text=open("$1").read(); _,roles=m.line_roles(text)
print(sum(1 for r in roles if r==("header",("marketplaces","mycelium"))))
PY
}

# --- Test 1: apply positive (plain header) ---------------------------------------------------------
C="$TMP/t1.toml"; R="$TMP/t1.receipt.json"
cat > "$C" <<EOF
[marketplaces.mycelium]
last_updated = "2026-08-04T22:26:56Z"
source_type = "local"
source = "$OLD"

[plugins."mycelium@mycelium"]
enabled = true
EOF
python3 "$PY" apply "$C" "$CAND" --receipt "$R" >/dev/null 2>&1
{ [ "$(val_of "$C" "['marketplaces']['mycelium']['source']")" = "$CAND" ] \
  && [ "$(val_of "$C" "['marketplaces']['mycelium']['last_updated']")" = "2026-08-04T22:26:56Z" ] \
  && [ "$(val_of "$C" "['marketplaces']['mycelium']['source_type']")" = "local" ] \
  && [ "$(parses "$C")" = "ok" ] && [ "$(ntables "$C")" = "1" ]; } \
  && ok "apply positive: source flipped, siblings preserved, valid, single table" \
  || no "apply positive"

# --- Test 2: commented-header (defect b): edit in place, NO duplicate table ------------------------
C="$TMP/t2.toml"; R="$TMP/t2.receipt.json"
cat > "$C" <<EOF
[marketplaces.mycelium] # local registry
source_type = "local"
source = "$OLD"
EOF
python3 "$PY" apply "$C" "$CAND" --receipt "$R" >/dev/null 2>&1
{ [ "$(val_of "$C" "['marketplaces']['mycelium']['source']")" = "$CAND" ] \
  && [ "$(parses "$C")" = "ok" ] && [ "$(ntables "$C")" = "1" ] \
  && grep -q '# local registry' "$C"; } \
  && ok "commented-header edited in place, no duplicate table, comment kept" \
  || no "commented-header (defect b)"

# --- Test 3: intervening-edit rollback (defect a): unrelated edit survives -------------------------
C="$TMP/t3.toml"; R="$TMP/t3.receipt.json"
cat > "$C" <<EOF
[marketplaces.mycelium]
source_type = "local"
source = "$OLD"
EOF
python3 "$PY" apply "$C" "$CAND" --receipt "$R" >/dev/null 2>&1
# operator makes an UNRELATED intervening edit after apply:
cat >> "$C" <<EOF

[some.unrelated]
added_after_apply = 42
EOF
python3 "$PY" rollback "$C" --receipt "$R" >/dev/null 2>&1
{ [ "$(val_of "$C" "['marketplaces']['mycelium']['source']")" = "$OLD" ] \
  && [ "$(val_of "$C" "['some']['unrelated']['added_after_apply']")" = "42" ] \
  && [ "$(parses "$C")" = "ok" ]; } \
  && ok "rollback restores owned key AND preserves unrelated intervening edit (defect a)" \
  || no "intervening-edit rollback (defect a)"

# --- Test 4: conflict rollback: owned key changed since apply -> refuse; --force overrides ---------
C="$TMP/t4.toml"; R="$TMP/t4.receipt.json"
cat > "$C" <<EOF
[marketplaces.mycelium]
source_type = "local"
source = "$OLD"
EOF
python3 "$PY" apply "$C" "$CAND" --receipt "$R" >/dev/null 2>&1
# someone changes the OWNED source to a THIRD value after apply:
python3 - "$C" "$CAND2" <<'PY'
import sys,re
c,new=sys.argv[1],sys.argv[2]
t=open(c).read().replace('source = "','SRC',1)
# simplest: rewrite source line
import io
lines=open(c).read().splitlines(True)
for i,l in enumerate(lines):
    if l.strip().startswith('source ='):
        lines[i]=f'source = "{new}"\n'; break
open(c,'w').write(''.join(lines))
PY
sha_before_rb="$(shasum -a256 "$C" | cut -d' ' -f1)"
python3 "$PY" rollback "$C" --receipt "$R" >/dev/null 2>&1; rc=$?
sha_after_rb="$(shasum -a256 "$C" | cut -d' ' -f1)"
{ [ "$rc" = "1" ] && [ "$sha_before_rb" = "$sha_after_rb" ] \
  && [ "$(val_of "$C" "['marketplaces']['mycelium']['source']")" = "$CAND2" ]; } \
  && ok "conflict rollback REFUSED (rc1), config unchanged" \
  || no "conflict rollback refuse (rc=$rc)"
python3 "$PY" rollback "$C" --receipt "$R" --force >/dev/null 2>&1; rc=$?
{ [ "$rc" = "0" ] && [ "$(val_of "$C" "['marketplaces']['mycelium']['source']")" = "$OLD" ]; } \
  && ok "conflict rollback --force restores pre-image" \
  || no "conflict rollback --force (rc=$rc)"

# --- Test 5: absent-section apply + rollback removes exactly the created section -------------------
C="$TMP/t5.toml"; R="$TMP/t5.receipt.json"
cat > "$C" <<EOF
[other.keep]
x = 1
EOF
python3 "$PY" apply "$C" "$CAND" --receipt "$R" >/dev/null 2>&1
created_ok=0
{ [ "$(val_of "$C" "['marketplaces']['mycelium']['source']")" = "$CAND" ] \
  && [ "$(val_of "$C" "['plugins']['mycelium@mycelium']['enabled']")" = "True" ] \
  && [ "$(parses "$C")" = "ok" ]; } && created_ok=1
python3 "$PY" rollback "$C" --receipt "$R" >/dev/null 2>&1
{ [ "$created_ok" = "1" ] \
  && [ "$(val_of "$C" "['other']['keep']['x']")" = "1" ] \
  && [ -z "$(val_of "$C" "['marketplaces']['mycelium']['source']")" ] \
  && [ "$(ntables "$C")" = "0" ] && [ "$(parses "$C")" = "ok" ]; } \
  && ok "absent-section: apply creates valid tables, rollback removes exactly them" \
  || no "absent-section apply/rollback"

# --- Test 6: absent-key add within existing section + rollback deletes only that key ---------------
C="$TMP/t6.toml"; R="$TMP/t6.receipt.json"
cat > "$C" <<EOF
[marketplaces.mycelium]
source_type = "local"

[plugins."mycelium@mycelium"]
enabled = true
EOF
python3 "$PY" apply "$C" "$CAND" --receipt "$R" >/dev/null 2>&1
add_ok=0
{ [ "$(val_of "$C" "['marketplaces']['mycelium']['source']")" = "$CAND" ] \
  && [ "$(val_of "$C" "['marketplaces']['mycelium']['source_type']")" = "local" ]; } && add_ok=1
python3 "$PY" rollback "$C" --receipt "$R" >/dev/null 2>&1
{ [ "$add_ok" = "1" ] \
  && [ -z "$(val_of "$C" "['marketplaces']['mycelium']['source']")" ] \
  && [ "$(val_of "$C" "['marketplaces']['mycelium']['source_type']")" = "local" ] \
  && [ "$(ntables "$C")" = "1" ] && [ "$(parses "$C")" = "ok" ]; } \
  && ok "absent-key: apply adds source, rollback deletes only it, section+siblings kept" \
  || no "absent-key add/rollback"

# --- Test 7: idempotent apply (already at target) -> no changes, file unchanged --------------------
C="$TMP/t7.toml"; R="$TMP/t7.receipt.json"
cat > "$C" <<EOF
[marketplaces.mycelium]
source_type = "local"
source = "$CAND"

[plugins."mycelium@mycelium"]
enabled = true
EOF
sha_b="$(shasum -a256 "$C" | cut -d' ' -f1)"
out="$(python3 "$PY" apply "$C" "$CAND" --receipt "$R" 2>&1)"; rc=$?
sha_a="$(shasum -a256 "$C" | cut -d' ' -f1)"
{ [ "$rc" = "0" ] && [ "$sha_b" = "$sha_a" ] && printf '%s' "$out" | grep -qi "no changes" && [ ! -f "$R" ]; } \
  && ok "idempotent apply: no changes, file+receipt untouched" \
  || no "idempotent apply (rc=$rc)"

# --- Test 8: invalid-input guard -> apply refuses, nothing written --------------------------------
C="$TMP/t8.toml"; R="$TMP/t8.receipt.json"
printf '[marketplaces.mycelium]\nsource = "unterminated\n' > "$C"
sha_b="$(shasum -a256 "$C" | cut -d' ' -f1)"
python3 "$PY" apply "$C" "$CAND" --receipt "$R" >/dev/null 2>&1; rc=$?
sha_a="$(shasum -a256 "$C" | cut -d' ' -f1)"
{ [ "$rc" = "1" ] && [ "$sha_b" = "$sha_a" ] && [ ! -f "$R" ]; } \
  && ok "invalid-input: apply refuses (rc1), config+receipt untouched" \
  || no "invalid-input guard (rc=$rc)"

# --- Test 9: duplicate-table input -> apply refuses at input validation ----------------------------
C="$TMP/t9.toml"; R="$TMP/t9.receipt.json"
cat > "$C" <<EOF
[marketplaces.mycelium]
source = "$OLD"
[marketplaces.mycelium]
source_type = "local"
EOF
sha_b="$(shasum -a256 "$C" | cut -d' ' -f1)"
python3 "$PY" apply "$C" "$CAND" --receipt "$R" >/dev/null 2>&1; rc=$?
sha_a="$(shasum -a256 "$C" | cut -d' ' -f1)"
{ [ "$rc" = "1" ] && [ "$sha_b" = "$sha_a" ] && [ ! -f "$R" ]; } \
  && ok "duplicate-table input: apply refuses (rc1), nothing written" \
  || no "duplicate-table input guard (rc=$rc)"

# --- Test 10: quoted-key table flip enabled=false -> true -----------------------------------------
C="$TMP/t10.toml"; R="$TMP/t10.receipt.json"
cat > "$C" <<EOF
[marketplaces.mycelium]
source_type = "local"
source = "$CAND"

[plugins."mycelium@mycelium"]
enabled = false
EOF
python3 "$PY" apply "$C" "$CAND" --receipt "$R" >/dev/null 2>&1
{ [ "$(val_of "$C" "['plugins']['mycelium@mycelium']['enabled']")" = "True" ] \
  && [ "$(parses "$C")" = "ok" ]; } \
  && ok "quoted-key table: enabled false->true, valid" \
  || no "quoted-key flip"

# --- Test 11: wrapper integration (dry-run default writes nothing; apply via wrapper) --------------
C="$TMP/t11.toml"; export MYCELIUM_CANDIDATE="$CAND"; export CODEX_CONFIG="$C"
cat > "$C" <<EOF
[marketplaces.mycelium]
source_type = "local"
source = "$OLD"

[plugins."mycelium@mycelium"]
enabled = true
EOF
sha_b="$(shasum -a256 "$C" | cut -d' ' -f1)"
"$WRAP" >/dev/null 2>&1; rc_dry=$?
sha_after_dry="$(shasum -a256 "$C" | cut -d' ' -f1)"
"$WRAP" --apply >/dev/null 2>&1; rc_apply=$?
applied_ok=0; [ "$(val_of "$C" "['marketplaces']['mycelium']['source']")" = "$CAND" ] && applied_ok=1
{ [ "$rc_dry" = "0" ] && [ "$sha_b" = "$sha_after_dry" ] && [ "$rc_apply" = "0" ] && [ "$applied_ok" = "1" ]; } \
  && ok "wrapper: dry-run writes nothing, --apply flips source (guard removed)" \
  || no "wrapper integration (rc_dry=$rc_dry rc_apply=$rc_apply)"
unset MYCELIUM_CANDIDATE CODEX_CONFIG

# --- Test 12 (defect 1): receipt is an existing dir -> refuse BEFORE mutation ---------------------
C="$TMP/t12.toml"; RD="$TMP/t12_receipt_dir"; mkdir -p "$RD"
cat > "$C" <<EOF
[marketplaces.mycelium]
source_type = "local"
source = "$OLD"
EOF
sha_b="$(shasum -a256 "$C" | cut -d' ' -f1)"
python3 "$PY" apply "$C" "$CAND" --receipt "$RD" >/dev/null 2>&1; rc=$?
sha_a="$(shasum -a256 "$C" | cut -d' ' -f1)"
{ [ "$rc" = "1" ] && [ "$sha_b" = "$sha_a" ]; } \
  && ok "defect1: receipt-is-dir refused before mutation, config unchanged" \
  || no "defect1 receipt-before-mutation (rc=$rc)"

# --- Test 13 (defect 2): wrong-config rollback refused (receipt for A must not change B) -----------
A="$TMP/t13a.toml"; RA="$TMP/t13a.receipt.json"
cat > "$A" <<EOF
[marketplaces.mycelium]
source_type = "local"
source = "$OLD"

[plugins."mycelium@mycelium"]
enabled = true
EOF
python3 "$PY" apply "$A" "$CAND" --receipt "$RA" >/dev/null 2>&1
B="$TMP/t13b.toml"
cat > "$B" <<EOF
[marketplaces.mycelium]
source_type = "local"
source = "$CAND"

[plugins."mycelium@mycelium"]
enabled = true
EOF
sha_b="$(shasum -a256 "$B" | cut -d' ' -f1)"
python3 "$PY" rollback "$B" --receipt "$RA" >/dev/null 2>&1; rc=$?
sha_a="$(shasum -a256 "$B" | cut -d' ' -f1)"
{ [ "$rc" = "1" ] && [ "$sha_b" = "$sha_a" ]; } \
  && ok "defect2: wrong-config rollback refused, unrelated config B unchanged" \
  || no "defect2 wrong-config rollback (rc=$rc)"

# --- Test 14 (defect 3): intervening edit before commit -> apply aborts, edit kept, receipt gone ---
C="$TMP/t14.toml"; R="$TMP/t14.receipt.json"
cat > "$C" <<EOF
[marketplaces.mycelium]
source_type = "local"
source = "$OLD"
EOF
R1ACT_TEST_INJECT_BEFORE_COMMIT=$'\n# concurrent edit\n' python3 "$PY" apply "$C" "$CAND" --receipt "$R" >/dev/null 2>&1; rc=$?
{ [ "$rc" = "1" ] && grep -q '# concurrent edit' "$C" \
  && [ "$(val_of "$C" "['marketplaces']['mycelium']['source']")" = "$OLD" ] \
  && [ ! -f "$R" ] && [ "$(parses "$C")" = "ok" ]; } \
  && ok "defect3: intervening edit aborts apply (drift), edit preserved, stale receipt removed" \
  || no "defect3 apply drift guard (rc=$rc)"

# --- Test 15 (defect 4): unique staging is symlink-safe; symlinked config preserved ----------------
D="$TMP/t15"; mkdir -p "$D"
REALCFG="$D/real.toml"
cat > "$REALCFG" <<EOF
[marketplaces.mycelium]
source_type = "local"
source = "$OLD"
EOF
CFG15="$D/config.toml"; ln -s "$REALCFG" "$CFG15"          # symlinked config
VICTIM="$D/victim.txt"; echo "DO NOT TOUCH" > "$VICTIM"
ln -s "$VICTIM" "$D/.real.toml.r1act-staging"             # hostile fixed-name staging symlink
ln -s "$VICTIM" "$D/.config.toml.r1act-staging"
R="$TMP/t15.receipt.json"
python3 "$PY" apply "$CFG15" "$CAND" --receipt "$R" >/dev/null 2>&1; rc=$?
{ [ "$rc" = "0" ] \
  && [ "$(cat "$VICTIM")" = "DO NOT TOUCH" ] \
  && [ -L "$CFG15" ] && [ "$(readlink "$CFG15")" = "$REALCFG" ] \
  && [ "$(val_of "$REALCFG" "['marketplaces']['mycelium']['source']")" = "$CAND" ] \
  && [ "$(parses "$REALCFG")" = "ok" ]; } \
  && ok "defect4: unique staging not aliased to victim; symlinked config kept; real target updated" \
  || no "defect4 symlink-safe staging (rc=$rc)"

# --- Test 16 (seq47): wrapper honors CODEX_HOME; explicit CODEX_CONFIG still wins -----------------
# (a) CODEX_HOME set, CODEX_CONFIG unset -> the private-profile config is edited, NOT the real one.
PH="$TMP/t16_home"; mkdir -p "$PH"
cat > "$PH/config.toml" <<EOF
[marketplaces.mycelium]
source_type = "local"
source = "$OLD"

[plugins."mycelium@mycelium"]
enabled = true
EOF
real_b="$( [ -f "$REAL" ] && shasum -a256 "$REAL" | cut -d' ' -f1 || echo none )"
( unset CODEX_CONFIG; MYCELIUM_CANDIDATE="$CAND" CODEX_HOME="$PH" "$WRAP" --apply ) >/dev/null 2>&1; rc=$?
receipt="$(ls "$PH"/config.toml.pre-r1-activation.*.receipt.json 2>/dev/null | head -1)"
home_edited=0; [ "$(val_of "$PH/config.toml" "['marketplaces']['mycelium']['source']")" = "$CAND" ] && home_edited=1
( unset CODEX_CONFIG; CODEX_HOME="$PH" "$WRAP" --rollback "$receipt" ) >/dev/null 2>&1; rb=$?
real_a="$( [ -f "$REAL" ] && shasum -a256 "$REAL" | cut -d' ' -f1 || echo none )"
{ [ "$rc" = "0" ] && [ "$home_edited" = "1" ] && [ "$rb" = "0" ] \
  && [ "$(val_of "$PH/config.toml" "['marketplaces']['mycelium']['source']")" = "$OLD" ] \
  && [ "$real_b" = "$real_a" ]; } \
  && ok "seq47: CODEX_HOME profile edited+rolled back; real ~/.codex untouched" \
  || no "seq47 CODEX_HOME honored (rc=$rc home_edited=$home_edited rb=$rb)"

# (b) explicit CODEX_CONFIG overrides CODEX_HOME (highest precedence).
OV="$TMP/t16_override.toml"
cat > "$OV" <<EOF
[marketplaces.mycelium]
source_type = "local"
source = "$OLD"
EOF
( MYCELIUM_CANDIDATE="$CAND" CODEX_HOME="$PH" CODEX_CONFIG="$OV" "$WRAP" --apply ) >/dev/null 2>&1
{ [ "$(val_of "$OV" "['marketplaces']['mycelium']['source']")" = "$CAND" ] \
  && [ "$(val_of "$PH/config.toml" "['marketplaces']['mycelium']['source']")" = "$OLD" ]; } \
  && ok "seq47: explicit CODEX_CONFIG wins over CODEX_HOME" \
  || no "seq47 explicit override precedence"

# --- Sentinel: the REAL ~/.codex/config.toml was never touched ------------------------------------
REAL_SHA_AFTER="$( [ -f "$REAL" ] && shasum -a256 "$REAL" | cut -d' ' -f1 || echo none )"
[ "$REAL_SHA_BEFORE" = "$REAL_SHA_AFTER" ] \
  && ok "real ~/.codex/config.toml UNTOUCHED ($REAL_SHA_AFTER)" \
  || no "real config changed! before=$REAL_SHA_BEFORE after=$REAL_SHA_AFTER"

echo "----"; echo "$pass passed, $fail failed"
[ "$fail" = "0" ]
