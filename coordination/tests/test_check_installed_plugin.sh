#!/usr/bin/env bash
# seq35 focused +/- checks for scripts/check_installed_plugin.py — installed-PLUGIN identity/closure
# verification. PRIMARY signal = the Codex plugin registry (codex plugin list --json); cache-closure
# verification is MANDATORY, bound to the EXACT registered marketplace/plugin/version namespace, and
# covers the COMPLETE recorded closure incl. exec bits. Closes the three reproduced false-PASS cases
# (codex-installed-plugin-closure-review-r1): omitted cache, launchers-only cache, foreign namespace.
# Hermetic: synthetic registry JSON + synthetic candidate/cache trees under $TMP. No real install.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHK="$REPO/scripts/check_installed_plugin.py"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0; pass=0
ok(){ pass=$((pass+1)); echo "PASS: $1"; }
no(){ fail=$((fail+1)); echo "FAIL: $1"; }
V="0.6.0+codex.TEST.coord.integration.r1"
MKT="mycelium"        # registered marketplace name
SHORT="mycelium"      # plugin short name (pluginId = mycelium@mycelium)

# A representative "full" closure: launchers (exec) + Python impl + native manifest + a hook (exec).
mk_candidate(){ local d="$1" v="$2"
  mkdir -p "$d/coordination/bin" "$d/coordination/mycelium_coord" "$d/.codex-plugin" "$d/skills/core/hooks"
  printf '#!/bin/sh\necho coord\n' > "$d/coordination/bin/mycelium-coord";       chmod +x "$d/coordination/bin/mycelium-coord"
  printf '#launcher\n'            > "$d/coordination/bin/mycelium-coord-mcp";     chmod +x "$d/coordination/bin/mycelium-coord-mcp"
  printf '#locator\n'             > "$d/coordination/bin/locate-mycelium-coord.sh"; chmod +x "$d/coordination/bin/locate-mycelium-coord.sh"
  printf '# python impl\n'        > "$d/coordination/mycelium_coord/coord.py"
  printf '{"name":"mycelium"}\n'  > "$d/.codex-plugin/plugin.json"
  printf '#!/bin/sh\n#hook\n'     > "$d/skills/core/hooks/mycelium-activity-tracker.sh"; chmod +x "$d/skills/core/hooks/mycelium-activity-tracker.sh"
  python3 - "$d" "$v" <<'PY'
import json,os,sys,hashlib
d,v=sys.argv[1],sys.argv[2]
files=["coordination/bin/mycelium-coord","coordination/bin/mycelium-coord-mcp",
       "coordination/bin/locate-mycelium-coord.sh","coordination/mycelium_coord/coord.py",
       ".codex-plugin/plugin.json","skills/core/hooks/mycelium-activity-tracker.sh"]
clo={}
for rel in files:
    p=os.path.join(d,rel)
    clo[rel]={"sha256":hashlib.sha256(open(p,"rb").read()).hexdigest(),"exec":os.access(p,os.X_OK)}
json.dump({"manifest_versions":{"codex":v,"claude":"x"},"closure":clo},open(os.path.join(d,"EXPORT_MANIFEST.json"),"w"))
PY
}
# Copy the FULL candidate tree into the registered namespace cache dir (preserving exec bits).
mk_cache_full(){ local root="$1" v="$2" from="$3"; local base="$root/$MKT/$SHORT/$v"
  mkdir -p "$base"; (cd "$from" && find . -type f ! -name EXPORT_MANIFEST.json -print0 | while IFS= read -r -d '' f; do
     mkdir -p "$base/$(dirname "$f")"; cp -p "$f" "$base/$f"; done); printf '%s' "$base"; }
reg(){ printf '%s' "$1" > "$TMP/reg.json"; }
POS_REG(){ echo "{\"installed\":[{\"pluginId\":\"$SHORT@$MKT\",\"marketplaceName\":\"$MKT\",\"installed\":true,\"enabled\":true,\"version\":\"$V\",\"source\":{\"source\":\"local\",\"path\":\"$CAND\"}}]}"; }

CAND="$TMP/cand"; mk_candidate "$CAND" "$V"

# ---- Test 1: FULL-closure positive (exact namespace) -> PASS ------------------------------------
CR="$TMP/cache1"; mk_cache_full "$CR" "$V" "$CAND" >/dev/null
reg "$(POS_REG)"
python3 "$CHK" --candidate "$CAND" --registry-json "$TMP/reg.json" --cache-root "$CR" >/dev/null 2>&1
[ $? -eq 0 ] && ok "full-closure positive (exact namespace) -> PASS" || no "full-closure positive"

# ---- Test 2: NOT installed -> FAIL --------------------------------------------------------------
reg '{"installed":[]}'
python3 "$CHK" --candidate "$CAND" --registry-json "$TMP/reg.json" --cache-root "$CR" >/dev/null 2>&1
[ $? -eq 1 ] && ok "not-installed -> FAIL" || no "not-installed"

# ---- Test 3: installed but DISABLED -> FAIL -----------------------------------------------------
reg "{\"installed\":[{\"pluginId\":\"$SHORT@$MKT\",\"marketplaceName\":\"$MKT\",\"installed\":true,\"enabled\":false,\"version\":\"$V\",\"source\":{\"path\":\"$CAND\"}}]}"
out="$(python3 "$CHK" --candidate "$CAND" --registry-json "$TMP/reg.json" --cache-root "$CR" 2>&1)"; rc=$?
{ [ $rc -eq 1 ] && printf '%s' "$out" | grep -qi "not enabled"; } && ok "disabled -> FAIL" || no "disabled (rc=$rc)"

# ---- Test 4: WRONG (old) version -> FAIL --------------------------------------------------------
reg "{\"installed\":[{\"pluginId\":\"$SHORT@$MKT\",\"marketplaceName\":\"$MKT\",\"installed\":true,\"enabled\":true,\"version\":\"0.6.0+codex.20260802225518\",\"source\":{\"path\":\"$CAND\"}}]}"
out="$(python3 "$CHK" --candidate "$CAND" --registry-json "$TMP/reg.json" --cache-root "$CR" 2>&1)"; rc=$?
{ [ $rc -eq 1 ] && printf '%s' "$out" | grep -qi "version mismatch"; } && ok "old version -> FAIL" || no "old version (rc=$rc)"

# ---- Test 5: WRONG source path -> FAIL ----------------------------------------------------------
reg "{\"installed\":[{\"pluginId\":\"$SHORT@$MKT\",\"marketplaceName\":\"$MKT\",\"installed\":true,\"enabled\":true,\"version\":\"$V\",\"source\":{\"path\":\"/Users/mst36/tools/mycelium-main\"}}]}"
out="$(python3 "$CHK" --candidate "$CAND" --registry-json "$TMP/reg.json" --cache-root "$CR" 2>&1)"; rc=$?
{ [ $rc -eq 1 ] && printf '%s' "$out" | grep -qi "source path mismatch"; } && ok "wrong source -> FAIL" || no "wrong source (rc=$rc)"

# ---- Test 6 (review 1): OMITTED cache (empty cache root, and default) -> FAIL --------------------
reg "$(POS_REG)"
out="$(python3 "$CHK" --candidate "$CAND" --registry-json "$TMP/reg.json" --cache-root "$TMP/empty" 2>&1)"; rc=$?
# and with NO --cache-root at all, defaulting to an empty CODEX_HOME:
out2="$(CODEX_HOME="$TMP/emptyhome" python3 "$CHK" --candidate "$CAND" --registry-json "$TMP/reg.json" 2>&1)"; rc2=$?
{ [ $rc -eq 1 ] && printf '%s' "$out" | grep -qi "ABSENT" && [ $rc2 -eq 1 ]; } \
  && ok "review1: registry-only with no backing cache -> FAIL (cache mandatory)" \
  || no "review1 omitted cache (rc=$rc rc2=$rc2)"

# ---- Test 7 (review 2): LAUNCHERS-ONLY cache -> FAIL (full closure required) ---------------------
CR7="$TMP/cache7"; base7="$CR7/$MKT/$SHORT/$V"; mkdir -p "$base7/coordination/bin"
cp -p "$CAND"/coordination/bin/* "$base7/coordination/bin/"
out="$(python3 "$CHK" --candidate "$CAND" --registry-json "$TMP/reg.json" --cache-root "$CR7" 2>&1)"; rc=$?
{ [ $rc -eq 1 ] && printf '%s' "$out" | grep -qi "recorded plugin closure"; } \
  && ok "review2: launchers-only cache -> FAIL (impl/manifest/hook missing)" \
  || no "review2 launchers-only (rc=$rc)"

# ---- Test 8 (review 3): FOREIGN namespace only -> FAIL -------------------------------------------
CR8="$TMP/cache8"; base8="$CR8/unrelated-marketplace/$SHORT/$V"; mkdir -p "$base8"
(cd "$CAND" && find . -type f ! -name EXPORT_MANIFEST.json -print0 | while IFS= read -r -d '' f; do
   mkdir -p "$base8/$(dirname "$f")"; cp -p "$f" "$base8/$f"; done)   # full closure, WRONG namespace
out="$(python3 "$CHK" --candidate "$CAND" --registry-json "$TMP/reg.json" --cache-root "$CR8" 2>&1)"; rc=$?
{ [ $rc -eq 1 ] && printf '%s' "$out" | grep -qi "ABSENT"; } \
  && ok "review3: full closure only under foreign namespace -> FAIL (exact namespace required)" \
  || no "review3 foreign namespace (rc=$rc)"

# ---- Test 9: exec-bit mismatch in full cache -> FAIL --------------------------------------------
CR9="$TMP/cache9"; base9="$(mk_cache_full "$CR9" "$V" "$CAND")"
chmod -x "$base9/coordination/bin/mycelium-coord"     # recorded exec=true, now not executable
reg "$(POS_REG)"
out="$(python3 "$CHK" --candidate "$CAND" --registry-json "$TMP/reg.json" --cache-root "$CR9" 2>&1)"; rc=$?
{ [ $rc -eq 1 ] && printf '%s' "$out" | grep -qi "exec-bit"; } && ok "exec-bit mismatch -> FAIL" || no "exec-bit mismatch (rc=$rc)"

# ---- Test 10: byte tamper in full cache -> FAIL ------------------------------------------------
CR10="$TMP/cache10"; base10="$(mk_cache_full "$CR10" "$V" "$CAND")"
printf 'TAMPERED\n' > "$base10/coordination/mycelium_coord/coord.py"
out="$(python3 "$CHK" --candidate "$CAND" --registry-json "$TMP/reg.json" --cache-root "$CR10" 2>&1)"; rc=$?
{ [ $rc -eq 1 ] && printf '%s' "$out" | grep -qi "hash-mismatch"; } && ok "byte tamper -> FAIL" || no "byte tamper (rc=$rc)"

# ---- Test 11: empty/malformed closure metadata in candidate -> FAIL (rc2) ------------------------
BADCAND="$TMP/badcand"; mkdir -p "$BADCAND"
printf '{"manifest_versions":{"codex":"%s"},"closure":{}}' "$V" > "$BADCAND/EXPORT_MANIFEST.json"
reg "$(POS_REG)"
python3 "$CHK" --candidate "$BADCAND" --registry-json "$TMP/reg.json" --cache-root "$CR" >/dev/null 2>&1
[ $? -eq 2 ] && ok "empty closure metadata -> refuse (rc2)" || no "empty closure guard"

# ---- Test 12 (--plugin-id, additive personal marketplace): mycelium@personal positive -> PASS ----
# Separate cache namespace ("personal" marketplace, not "mycelium"): registry pluginId
# "mycelium@personal", marketplaceName "personal" -> cache dir root/personal/mycelium/<version>.
PCAND="$TMP/pcand"; mk_candidate "$PCAND" "$V"
PCACHE="$TMP/pcache"; PBASE="$PCACHE/personal/mycelium/$V"; mkdir -p "$PBASE"
(cd "$PCAND" && find . -type f ! -name EXPORT_MANIFEST.json -print0 | while IFS= read -r -d '' f; do
   mkdir -p "$PBASE/$(dirname "$f")"; cp -p "$f" "$PBASE/$f"; done)
PPOS_REG='{"installed":[
  {"pluginId":"mycelium@personal","marketplaceName":"personal","installed":true,"enabled":true,"version":"'"$V"'","source":{"path":"'"$PCAND"'"}},
  {"pluginId":"mycelium@mycelium","marketplaceName":"mycelium","installed":true,"enabled":false,"version":"0.6.0+codex.OLD","source":{"path":"/Users/mst36/tools/mycelium-main"}}
]}'
reg "$PPOS_REG"
python3 "$CHK" --candidate "$PCAND" --registry-json "$TMP/reg.json" --cache-root "$PCACHE" --plugin-id "mycelium@personal" >/dev/null 2>&1
[ $? -eq 0 ] && ok "namespace-aware: mycelium@personal full-closure positive -> PASS" || no "mycelium@personal positive"

# Sanity: the SAME registry, checked with the DEFAULT --plugin-id (mycelium@mycelium, disabled+old
# version+different source here), must still FAIL — confirms the two namespaces are not conflated.
out="$(python3 "$CHK" --candidate "$CAND" --registry-json "$TMP/reg.json" --cache-root "$CR" 2>&1)"; rc=$?
[ $rc -eq 1 ] && ok "namespace-aware: default --plugin-id unaffected by personal registry entry" \
  || no "namespace-aware default plugin-id (rc=$rc)"

# ---- Test 13 (--plugin-id): mycelium@personal VERSION MISMATCH -> FAIL ---------------------------
BADV="0.6.0+codex.PERSONAL-OLD"
reg "{\"installed\":[{\"pluginId\":\"mycelium@personal\",\"marketplaceName\":\"personal\",\"installed\":true,\"enabled\":true,\"version\":\"$BADV\",\"source\":{\"path\":\"$PCAND\"}}]}"
out="$(python3 "$CHK" --candidate "$PCAND" --registry-json "$TMP/reg.json" --cache-root "$PCACHE" --plugin-id "mycelium@personal" 2>&1)"; rc=$?
{ [ $rc -eq 1 ] && printf '%s' "$out" | grep -qi "version mismatch"; } \
  && ok "namespace-aware: mycelium@personal version mismatch -> FAIL" \
  || no "mycelium@personal version mismatch (rc=$rc)"

echo "----"; echo "$pass passed, $fail failed"
[ "$fail" = "0" ]
