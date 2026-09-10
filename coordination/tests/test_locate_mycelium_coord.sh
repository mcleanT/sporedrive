#!/usr/bin/env bash
# Focused checks for coordination/bin/locate-mycelium-coord.sh (MYCELIUM-INTEGRATION r1 adoption).
# Covers the two reproduced selection defects (codex-locator-selection-review-r1):
#   1) explicit MYCELIUM_COORD_BIN override must win BEFORE self-location;
#   2) a newer PARTIAL cache (launcher without the mycelium_coord package) must NOT hide an older
#      COMPLETE install — every candidate is filtered by the same full-usability check.
# Plus self-location and truthful not-found. No network, no real plugin install.
set -uo pipefail
CCW="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOC="$CCW/coordination/bin/locate-mycelium-coord.sh"
SByte=$(shasum -a256 "$LOC" | cut -d' ' -f1)
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0; pass=0
ok(){ pass=$((pass+1)); echo "PASS: $1"; }
no(){ fail=$((fail+1)); echo "FAIL: $1"; }

# Build a COMPLETE install tree at $1 (launcher + sibling mycelium_coord/coord.py).
mk_complete(){ local d="$1"; mkdir -p "$d/coordination/bin" "$d/coordination/mycelium_coord";
  printf '#!/bin/sh\necho "$d/coordination/bin/mycelium-coord"\n' >"$d/coordination/bin/mycelium-coord"
  chmod +x "$d/coordination/bin/mycelium-coord"; printf '# pkg\n' >"$d/coordination/mycelium_coord/coord.py"; }
# Build a PARTIAL install tree at $1 (launcher only, NO package).
mk_partial(){ local d="$1"; mkdir -p "$d/coordination/bin";
  printf '#!/bin/sh\necho partial\n' >"$d/coordination/bin/mycelium-coord"; chmod +x "$d/coordination/bin/mycelium-coord"; }

# A copy of the locator placed where it has NO sibling launcher (so self-location can't short-circuit).
NOWHERE="$TMP/nowhere"; mkdir -p "$NOWHERE"; cp "$LOC" "$NOWHERE/locate.sh"; chmod +x "$NOWHERE/locate.sh"
# A clean HOME with no ~/.claude/plugins and no ~/tools dev checkouts.
CLEANHOME="$TMP/home"; mkdir -p "$CLEANHOME"

# ---- Test 1: explicit override wins before self-location -------------------------------------------
# Run the REAL in-tree locator (its HERE has a sibling launcher -> self-location would return it),
# but pin MYCELIUM_COORD_BIN to a different complete launcher; expect the pinned one.
mk_complete "$TMP/pinned"
PIN="$TMP/pinned/coordination/bin/mycelium-coord"
got="$(MYCELIUM_COORD_BIN="$PIN" bash "$LOC" 2>/dev/null)"
[ "$got" = "$PIN" ] && ok "override honored before self-location" || no "override-first (got: $got, want: $PIN)"

# ---- Test 2: self-location when no override -------------------------------------------------------
got="$(env -u MYCELIUM_COORD_BIN bash "$LOC" 2>/dev/null)"
[ "$got" = "$CCW/coordination/bin/mycelium-coord" ] && ok "self-location beside script" || no "self-location (got: $got)"

# ---- Test 3: newer PARTIAL cache must not hide older COMPLETE (Codex cache scan) ------------------
CH="$TMP/codexhome"; base="$CH/plugins/cache/mycelium/mycelium"
mk_complete "$base/0.0.1-complete"           # older, complete
mk_partial  "$base/0.0.2-partial"            # newer, partial (launcher, no package)
want="$base/0.0.1-complete/coordination/bin/mycelium-coord"
got="$(env -i HOME="$CLEANHOME" PATH="/usr/bin:/bin" CODEX_HOME="$CH" bash "$NOWHERE/locate.sh" 2>/dev/null)"
[ "$got" = "$want" ] && ok "partial newer cache skipped for older complete (Codex)" || no "partial-skip Codex (got: $got, want: $want)"

# ---- Test 4: newer COMPLETE cache wins over older COMPLETE ----------------------------------------
mk_complete "$base/0.0.3-complete"           # newest, complete
want3="$base/0.0.3-complete/coordination/bin/mycelium-coord"
got="$(env -i HOME="$CLEANHOME" PATH="/usr/bin:/bin" CODEX_HOME="$CH" bash "$NOWHERE/locate.sh" 2>/dev/null)"
[ "$got" = "$want3" ] && ok "newest complete cache selected" || no "newest-complete (got: $got, want: $want3)"

# ---- Test 6: Claude cache route, newest COMPLETE, with a SPACE in the path (no word-split) --------
# Clean HOME so only the Claude-plugins route can resolve; a space-containing dir + a newer partial.
CH2HOME="$TMP/home2"; cp="$CH2HOME/.claude/plugins/marketplaces/my space mkt/mycelium"
mk_complete "$cp/0.0.1-complete"
mk_partial  "$cp/0.0.2-partial"
wantc="$cp/0.0.1-complete/coordination/bin/mycelium-coord"
got="$(env -i HOME="$CH2HOME" PATH="/usr/bin:/bin" CODEX_HOME="$TMP/none" bash "$NOWHERE/locate.sh" 2>/dev/null)"
[ "$got" = "$wantc" ] && ok "Claude cache: space-path, newest complete (no word-split)" || no "Claude-route space-path (got: [$got], want: [$wantc])"

# ---- Test 7: not found -> exit 3, truthful (no fabrication) ---------------------------------------
out="$(env -i HOME="$CLEANHOME" PATH="/usr/bin:/bin" CODEX_HOME="$TMP/empty" bash "$NOWHERE/locate.sh" 2>&1)"; rc=$?
{ [ "$rc" = "3" ] && printf '%s' "$out" | grep -q "not found"; } && ok "not-found exits 3 with guidance" || no "not-found (rc=$rc)"

echo "----"; echo "locator sha under test: $SByte"; echo "$pass passed, $fail failed"
[ "$fail" = "0" ]
