#!/usr/bin/env bash
# seq74: public-wrapper regressions for scripts/verify-native-activation.sh
#  (1) --installed must win over --candidate regardless of option order: an absent/empty
#      registry can never PASS in either order (no silent downgrade to candidate-only).
#  (2) the installed registry fetch derives its marketplace from the selected --plugin-id,
#      not a hardcoded "mycelium".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
W="$REPO/scripts/verify-native-activation.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo '{"installed":[],"available":[]}' > "$TMP/empty.json"
# a minimal valid candidate (real one) for the arg to resolve; installed mode fails at registry
CAND="${MYCELIUM_CANDIDATE:-$HOME/plugins/mycelium}"
pass=0; fail=0
chk(){ if [ "$1" = "$2" ]; then pass=$((pass+1)); echo "  PASS $3"; else fail=$((fail+1)); echo "  FAIL $3 (got $1 want $2)"; fi; }

"$W" --installed --candidate "$CAND" --plugin-id mycelium@personal --registry-json "$TMP/empty.json" >/dev/null 2>&1
chk "$?" 1 "empty registry, order[--installed then --candidate] -> exit 1"
"$W" --candidate "$CAND" --installed --plugin-id mycelium@personal --registry-json "$TMP/empty.json" >/dev/null 2>&1
chk "$?" 1 "empty registry, order[--candidate then --installed] -> exit 1"
# Bug2 code-level: fetch derives marketplace from plugin-id, no hardcoded 'mycelium'
if grep -qE 'codex plugin list --marketplace "\$mkt" --json' "$W" && ! grep -qE 'codex plugin list --marketplace mycelium --json' "$W"; then
  pass=$((pass+1)); echo "  PASS registry fetch derives marketplace from plugin-id (no hardcoded mycelium)"
else fail=$((fail+1)); echo "  FAIL registry fetch still hardcodes marketplace"; fi
echo "wrapper-options: $pass passed, $fail failed"; [ "$fail" -eq 0 ]
