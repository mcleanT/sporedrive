#!/usr/bin/env bash
# cmux socket access: status / dry-run / activate / rollback helper for password mode.
#
# Thin shim: all logic now lives in scripts/cmux_access_plan.py (a proper JSONC-safe parser,
# preimage-based rollback, and preflight ordering that a shell regex/`$()` pipeline can't give
# us safely -- see the module docstring for the four corrections this replaced). This shim exists
# only so the CLI entry point and env-var names stay the same as before.
#
# Sub-commands: status | dry-run | prepare-secret | activate --confirm | rollback --confirm
#
# Env vars (unchanged names, one corrected default + three new ones):
#   CMUX_SETTINGS_FILE          canonical file, default ~/.config/cmux/cmux.json (was wrongly
#                                defaulted to the legacy settings.json before this fix)
#   CMUX_LEGACY_SETTINGS_FILE   new: read-only fallback evidence, default ~/.config/cmux/settings.json
#   CMUX_BRIDGE_PASSWORD_FILE   secret file, default ~/.config/codex-claude-bridge/cmux-socket-password
#   CMUX_ACCESS_BACKUP_DIR      default ~/.config/codex-claude-bridge/backups
#   CMUX_ACCESS_RECEIPT_DIR     new: activation receipts, default ~/.config/codex-claude-bridge/access-receipts
#   CMUX_APP_SAVED_PASSWORD_FILE  new, read-only evidence: ~/Library/Application Support/cmux/socket-control-password
#   CMUX_BRIDGE_CLI              cmux CLI binary, used only for the best-effort live status probe
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PY="${PYTHON:-python3}"

exec "$PY" "$SCRIPT_DIR/cmux_access_plan.py" "$@"
