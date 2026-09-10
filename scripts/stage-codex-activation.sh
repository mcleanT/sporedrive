#!/usr/bin/env bash
# STAGED Codex native activation for r1 — a concrete, runnable, SAFE repoint/enable with rollback.
# It repoints [marketplaces.mycelium].source to the COMPLETE candidate (NOT ~/tools/mycelium-main)
# and enables [plugins."mycelium@mycelium"], editing ONLY those owned keys in place so unrelated
# concurrent config edits and comments are preserved. It DOES NOT run `codex plugin add` or load any
# plugin — that global native switch stays staged for a clean boundary + owner.
#
#   stage-codex-activation.sh                  # DRY-RUN: print the exact targeted changes, write nothing
#   stage-codex-activation.sh --apply          # snapshot(receipt) + apply the owned-key edits (atomic)
#   stage-codex-activation.sh --rollback FILE  # owned-key, conflict-aware rollback from a receipt
#
# The TOML surgery is delegated to scripts/toml_owned_edit.py (seq36 repair): a TOML-aware,
# owned-key editor validated with tomllib (fail-closed), which fixes the two reproduced defects of
# the earlier naive helper — a whole-file `cp` rollback that clobbered an unrelated intervening edit,
# and a missed commented table header ("[marketplaces.mycelium] # local registry") that produced an
# invalid DUPLICATE table. apply writes a receipt; rollback restores ONLY the owned keys to their
# pre-apply state and REFUSES (no clobber) if an owned key changed since apply (unless --force).
#
# After --apply, the operator runs (still a deliberate, staged step): `codex plugin add mycelium@mycelium`
# then verifies with `verify-native-activation.sh --installed`. Default is DRY-RUN by design.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EDIT="$HERE/toml_owned_edit.py"
CAND="${MYCELIUM_CANDIDATE:-$HOME/tools/mycelium-integration-wfi}"
# Target the SAME Codex profile that `codex plugin add` will use. Precedence: explicit CODEX_CONFIG
# (highest) > $CODEX_HOME/config.toml (the profile codex itself resolves) > ~/.codex/config.toml.
# Ignoring CODEX_HOME here would edit the default profile while the plugin install hit a private one
# (seq47 wrong-profile risk).
CFG="${CODEX_CONFIG:-${CODEX_HOME:-$HOME/.codex}/config.toml}"

MODE="dryrun"; SNAP=""
case "${1:-}" in
  --apply) MODE=apply ;;
  --rollback) MODE=rollback; SNAP="${2:-}" ;;
  ""|--dry-run) MODE=dryrun ;;
  *) echo "usage: $0 [--apply | --rollback RECEIPT]" >&2; exit 2 ;;
esac

[ -f "$EDIT" ] || { echo "missing helper: $EDIT" >&2; exit 1; }
if [ "$MODE" != rollback ]; then
  [ -f "$CFG" ] || { echo "config not found: $CFG" >&2; exit 1; }
fi

case "$MODE" in
  dryrun)
    python3 "$EDIT" dryrun "$CFG" "$CAND"
    rc=$?
    [ "$rc" = 0 ] && echo "DRY-RUN: no file written. Re-run with --apply at a clean boundary to stage activation."
    exit $rc
    ;;
  apply)
    ts="$(date +%Y%m%dT%H%M%S)"
    receipt="${CFG}.pre-r1-activation.${ts}.receipt.json"
    python3 "$EDIT" apply "$CFG" "$CAND" --receipt "$receipt"
    exit $?
    ;;
  rollback)
    [ -n "$SNAP" ] || { echo "usage: $0 --rollback RECEIPT" >&2; exit 2; }
    [ -f "$SNAP" ] || { echo "receipt not found: $SNAP" >&2; exit 1; }
    python3 "$EDIT" rollback "$CFG" --receipt "$SNAP"
    exit $?
    ;;
esac
