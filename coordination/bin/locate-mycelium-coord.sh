#!/usr/bin/env bash
# Canonical locator for the shared coordination CLI launcher (`mycelium-coord`), shipped INSIDE the
# plugin at `<plugin-root>/coordination/bin/` beside the launchers. Works from ANY cwd (it
# self-locates via its own path, never a cwd-relative reference), so both hosts and both skills
# (Claude's `mycelium-coordinate`, Codex's `cmux-driver`) can call it by absolute path.
#
# WHY: the managed guidance uses the bare command `mycelium-coord`, but the CLI ships inside the
# plugin — NOT on PATH — so `command -v mycelium-coord` returns absent on a host that only installed
# the plugin. Prefer the `coord_*` MCP tools whenever the plugin is loaded (Claude server
# `plugin:mycelium:mycelium-coord`; Codex server `mycelium-coord`); use THIS locator for the bare CLI
# (shell/hook use, or when MCP is absent). It never invents a launcher: if nothing usable exists it
# exits non-zero — fall back to the bridge/brief path with truthful limits, never a fabricated call.
#
# Usage (invoke by ABSOLUTE path, e.g. "${CLAUDE_PLUGIN_ROOT}/coordination/bin/locate-mycelium-coord.sh"):
#   locate-mycelium-coord.sh                 # print the resolved absolute launcher path, or exit 3
#   locate-mycelium-coord.sh <op> [args...]  # exec the located CLI with these args (e.g. resume TASK P)
#   MYCELIUM_COORD_BIN=/path/to/mycelium-coord locate-mycelium-coord.sh ...   # explicit override
#
# Exit codes: 0 found (printed or exec'd); 3 not found anywhere.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# A candidate is usable only if the launcher exists AND its sibling package is present (a frozen
# pre-coordination plugin install has neither coordination/bin nor the mycelium_coord package).
_usable() { [ -n "${1:-}" ] && [ -x "$1" ] && [ -f "$(dirname "$1")/../mycelium_coord/coord.py" ]; }

# Newest COMPLETE version dir under a cache root. Selection uses the SAME full-usability check as
# everywhere else (launcher present AND sibling coord.py): a newer PARTIAL cache (launcher without
# the package) must NOT be chosen as winner and hide an older complete install (locator review 2).
_newest_under() {
  local root="$1" hit="" v c
  [ -d "$root" ] || return 0
  while IFS= read -r v; do
    c="$v/coordination/bin/mycelium-coord"; _usable "$c" && hit="$c"
  done < <(find "$root" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort)
  [ -n "$hit" ] && printf '%s\n' "$hit"
}

_resolve() {
  local c
  # 1) explicit override — honored FIRST; an explicit pin beats self-location (locator review 1).
  if [ -n "${MYCELIUM_COORD_BIN:-}" ] && _usable "$MYCELIUM_COORD_BIN"; then printf '%s\n' "$MYCELIUM_COORD_BIN"; return 0; fi
  # 2) self-location: the launcher sitting beside THIS script (the plugin coordination/bin case).
  c="$HERE/mycelium-coord"; if _usable "$c"; then printf '%s\n' "$c"; return 0; fi
  # 3) bare command on PATH (the form the guidance assumes; usually absent)
  if c="$(command -v mycelium-coord 2>/dev/null)" && _usable "$c"; then printf '%s\n' "$c"; return 0; fi
  # 4) plugin-root env vars the host/hooks export
  for root in "${CLAUDE_PLUGIN_ROOT:-}" "${MYCELIUM_PLUGIN_ROOT:-}" "${PLUGIN_ROOT:-}"; do
    c="$root/coordination/bin/mycelium-coord"; if [ -n "$root" ] && _usable "$c"; then printf '%s\n' "$c"; return 0; fi
  done
  # 5) Codex installed plugin cache: $CODEX_HOME/plugins/cache/<ns>/<plugin>/<version>/...
  local ch="${CODEX_HOME:-$HOME/.codex}"
  for nsp in "$ch"/plugins/cache/*/mycelium "$ch"/plugins/cache/mycelium/*; do
    c="$(_newest_under "$nsp")"; if _usable "$c"; then printf '%s\n' "$c"; return 0; fi
  done
  # 6) Claude installed plugins (any depth) — newest COMPLETE match, not merely newest (review 2).
  # Read find output line by line so launcher paths containing spaces are never word-split (review 3).
  local best=""
  while IFS= read -r c; do _usable "$c" && best="$c"; done \
    < <(find "$HOME/.claude/plugins" -path '*coordination/bin/mycelium-coord' -type f 2>/dev/null | sort)
  if [ -n "$best" ] && _usable "$best"; then printf '%s\n' "$best"; return 0; fi
  # 7) dev checkouts (exported candidate + source of truth) — last resort for a run on this host
  for c in "$HOME/tools/mycelium-integration-wfi/coordination/bin/mycelium-coord" \
           "$HOME/tools/codex-claude-workflow/coordination/bin/mycelium-coord"; do
    if _usable "$c"; then printf '%s\n' "$c"; return 0; fi
  done
  return 3
}

if CLI="$(_resolve)"; then
  if [ "$#" -gt 0 ]; then exec "$CLI" "$@"; fi
  printf '%s\n' "$CLI"; exit 0
fi

cat >&2 <<'EOF'
mycelium-coord CLI not found. It ships inside the plugin (<plugin-root>/coordination/bin/), not on PATH.
Options, in order of preference:
  1) Use the coord_* MCP tools instead (server plugin:mycelium:mycelium-coord on Claude, mycelium-coord on Codex).
  2) Install/enable the Mycelium plugin so a version WITH coordination/bin is present, then retry.
  3) Set MYCELIUM_COORD_BIN to an explicit launcher path.
Do NOT fabricate a coordination send/ack/checkpoint when the CLI and MCP are both unavailable — fall
back to the bridge/brief path with truthful limits.
EOF
exit 3
