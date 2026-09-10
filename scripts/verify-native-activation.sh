#!/usr/bin/env bash
# Read-only verification for the r1 native-activation receipt. NEVER writes host config, NEVER loads
# a plugin. Two DISTINCT modes (the artifact under test differs — do not conflate them):
#
#   --candidate [DIR]   verify the COMPLETE candidate's identity AND its EXPORT_MANIFEST *closure*
#                       (every recorded file hashed; exec bits checked). FAILS CLOSED on a missing/
#                       malformed manifest, a missing closure field, or any hash/exec mismatch.
#                       Default DIR = ~/tools/mycelium-integration-wfi.
#   --installed         verify the actual INSTALLED postimage, NOT the candidate: the installed
#                       cmux-driver (~/.codex/skills/cmux-driver) must carry the coordination
#                       reference + locator BYTE-IDENTICAL to the repo source (fail closed if not).
#                       The global plugin cache is REPORTED as staged/live (never a silent pass).
#
# Exit: 0 all required checks pass; 1 any required check failed (fail closed).
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="candidate"; CAND="${MYCELIUM_CANDIDATE:-$HOME/tools/mycelium-integration-wfi}"; REGJSON=""
PLUGIN_ID="mycelium@mycelium"
# --installed and --candidate both name a mode, but --installed selects the STRONGER
# installed-postimage check and must win regardless of option order (seq74 fix): a
# trailing `--candidate DIR` supplies the pinned dir WITHOUT silently downgrading an
# explicit --installed to the weaker candidate-only pass. So track the request as a flag
# and resolve MODE after parsing; --candidate only sets MODE when --installed was absent.
INSTALLED_REQUESTED=0
args=("$@"); i=0
while [ $i -lt ${#args[@]} ]; do
  case "${args[$i]}" in
    --installed) INSTALLED_REQUESTED=1 ;;
    --candidate) i=$((i+1)); [ -n "${args[$i]:-}" ] && CAND="${args[$i]}"; [ "$INSTALLED_REQUESTED" = 1 ] || MODE="candidate" ;;
    --registry-json) i=$((i+1)); REGJSON="${args[$i]:-}" ;;
    --plugin-id) i=$((i+1)); [ -n "${args[$i]:-}" ] && PLUGIN_ID="${args[$i]}" ;;
    "" ) : ;;
    * ) echo "usage: $0 [--candidate DIR | --installed [--registry-json FILE] [--candidate DIR] [--plugin-id ID]]" >&2; exit 2 ;;
  esac
  i=$((i+1))
done
[ "$INSTALLED_REQUESTED" = 1 ] && MODE="installed"
fail=0; ok(){ echo "ok   : $1"; }; no(){ echo "FAIL : $1"; fail=1; }

verify_candidate() {
  [ -d "$CAND" ] || { no "candidate missing: $CAND"; return; }
  ok "candidate exists: $CAND"
  # Identity + FULL closure verification, fail-closed, in one python pass.
  if python3 - "$CAND" <<'PY'
import json,sys,hashlib,os
cand=sys.argv[1]; man=os.path.join(cand,"EXPORT_MANIFEST.json")
def die(m): print("FAIL : "+m); sys.exit(1)
try:
    m=json.load(open(man))
except Exception as e:
    die(f"EXPORT_MANIFEST.json unreadable/malformed: {e}")
cs=m.get("canonical_source"); mv=m.get("manifest_versions"); clo=m.get("closure")
if not isinstance(cs,dict) or not cs.get("commit"): die("canonical_source.commit missing")
if not isinstance(mv,dict) or not mv.get("claude") or not mv.get("codex"): die("manifest_versions incomplete")
if not isinstance(clo,dict) or not clo: die("closure missing/empty")
print(f"ok   : canonical_source.commit = {cs['commit']} (dirty={cs.get('dirty')})")
print(f"ok   : versions claude={mv['claude']} codex={mv['codex']}")
bad=0; checked=0
for rel,meta in clo.items():
    p=os.path.join(cand,rel)
    if not os.path.isfile(p): print(f"FAIL : closure file missing: {rel}"); bad+=1; continue
    want=meta.get("sha256") if isinstance(meta,dict) else None
    if not want: print(f"FAIL : closure entry has no sha256: {rel}"); bad+=1; continue
    h=hashlib.sha256(open(p,"rb").read()).hexdigest()
    if h!=want: print(f"FAIL : closure hash mismatch: {rel}"); bad+=1; continue
    if isinstance(meta,dict) and "exec" in meta:
        is_exec=os.access(p,os.X_OK)
        if bool(meta["exec"])!=is_exec: print(f"FAIL : exec-bit mismatch ({meta['exec']} vs {is_exec}): {rel}"); bad+=1; continue
    checked+=1
if bad: die(f"{bad} closure mismatch(es); {checked} verified")
print(f"ok   : EXPORT_MANIFEST closure verified: {checked} files match")
sys.exit(0)
PY
  then :; else no "candidate identity/closure verification failed (fail-closed)"; fi
  # Coordination overlay presence (belt-and-suspenders; closure already covers bytes).
  for f in coordination/bin/mycelium-coord coordination/bin/mycelium-coord-mcp coordination/bin/locate-mycelium-coord.sh; do
    [ -x "$CAND/$f" ] && ok "candidate executable: $f" || no "candidate missing/!exec: $f"
  done
}

verify_installed() {
  # The INSTALLED cmux-driver postimage must match the repo source byte-for-byte for the two new files.
  local drv="$HOME/.codex/skills/cmux-driver"
  [ -d "$drv" ] || { no "installed cmux-driver missing: $drv"; return; }
  ok "installed cmux-driver present: $drv"
  for rel in references/mycelium-coordination.md scripts/locate-mycelium-coord.sh; do
    local src="$REPO/src/codex/skills/cmux-driver/$rel" ins="$drv/$rel"
    if [ ! -f "$ins" ]; then no "installed postimage missing $rel (stale install?)"; continue; fi
    if [ ! -f "$src" ]; then no "repo source missing $rel"; continue; fi
    if cmp -s "$src" "$ins"; then ok "installed postimage matches source: $rel"; else no "installed postimage DIFFERS from source: $rel"; fi
  done
  [ -x "$drv/scripts/locate-mycelium-coord.sh" ] && ok "installed locator is executable" || no "installed locator not executable"

  # (2) Installed PLUGIN identity/closure vs the pinned candidate (seq35) — DISTINCT from the driver
  # check above, and the AUTHORITATIVE gate. PRIMARY signal is the Codex plugin registry
  # (note codex-native-installed-registry-locator-r1): a matching cache directory alone is NOT
  # native installation. Pre-activation this correctly FAILS (old version staged); it passes only
  # after `codex plugin add` installs+enables the pinned candidate.
  echo "---- installed PLUGIN identity vs pinned candidate (seq35; registry-primary) ----"
  local ch="${CODEX_HOME:-$HOME/.codex}" reg="$REGJSON" tmpreg=""
  # Derive the registry fetch scope from the SELECTED plugin id's marketplace (seq74 fix):
  # PLUGIN_ID is "<plugin>@<marketplace>", so the registry must be read for THAT marketplace
  # (e.g. mycelium@personal -> --marketplace personal), not a hardcoded "mycelium".
  local mkt="${PLUGIN_ID##*@}"
  if [ -z "$reg" ]; then
    if command -v codex >/dev/null 2>&1; then
      tmpreg="$(mktemp)"
      if codex plugin list --marketplace "$mkt" --json > "$tmpreg" 2>/dev/null; then reg="$tmpreg"
      else no "could not run 'codex plugin list' to read the installed registry"; fi
    else
      no "no registry available (codex CLI absent and no --registry-json); cannot verify installed plugin"
    fi
  fi
  if [ -n "$reg" ]; then
    if python3 "$REPO/scripts/check_installed_plugin.py" --candidate "$CAND" --registry-json "$reg" --cache-root "$ch/plugins/cache" --plugin-id "$PLUGIN_ID"; then
      ok "installed plugin ($PLUGIN_ID) matches the pinned candidate (registry identity + cache closure)"
    else
      no "installed plugin ($PLUGIN_ID) does NOT match the pinned candidate (old/missing/mismatched/disabled) — reasons above"
    fi
  fi
  [ -n "$tmpreg" ] && rm -f "$tmpreg"

  # (3) Cache directory scan — INFORMATIONAL corroboration only (a cache dir alone is not install).
  echo "---- global plugin cache dirs (reported; corroboration only, NOT the gate) ----"
  local ch="${CODEX_HOME:-$HOME/.codex}" found=0
  for d in "$ch"/plugins/cache/*/mycelium/* "$ch"/plugins/cache/mycelium/*/*; do
    [ -d "$d" ] || continue; found=1
    if [ -x "$d/coordination/bin/mycelium-coord" ] && [ -f "$d/coordination/mycelium_coord/coord.py" ]; then
      echo "report: Codex cache COMPLETE (coordination overlay): $d"
    else echo "report: Codex cache PRE-COORDINATION (no coordination/bin) — staged, not activated: $d"; fi
  done
  [ "$found" = 0 ] && echo "report: no Codex mycelium plugin cache installed (staged activation not yet performed)"
  if find "$HOME/.claude/plugins" -path '*coordination/bin/mycelium-coord' -type f 2>/dev/null | grep -q .; then
    echo "report: Claude plugin cache has a coordination-complete install"
  else echo "report: Claude plugin cache has no coordination-complete install (staged)"; fi
}

echo "== verify-native-activation ($MODE) =="
[ "$MODE" = candidate ] && verify_candidate || verify_installed
echo "----"; [ "$fail" = 0 ] && echo "ALL REQUIRED CHECKS PASS ($MODE; read-only, nothing activated)" || echo "REQUIRED CHECKS FAILED ($MODE)"
exit $fail
