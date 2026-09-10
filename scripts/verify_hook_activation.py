#!/usr/bin/env python3
"""Read-only native HOOK-ACTIVATION regression guard (mycelium-integration r2, seq150).

WHY THIS EXISTS. Installed-plugin *payload* PASS (identity + EXPORT_MANIFEST closure, checked by
verify-native-activation.sh / check_installed_plugin.py) is DISTINCT from runtime hook
trust/dispatch. The r2 rollout discovered that a byte-perfect installed plugin can still fail to
run its lifecycle hooks:
  * Codex (codex-cli 0.146): the plugin's hooks are DISCOVERED + enabled + parsed but land
    trustStatus=untrusted, so nothing dispatches until the six vetted hashes are persisted as
    hooks.state.<key>.trusted_hash via the supported native config/batchWrite interface.
  * Claude: the plugin's bundled hooks/hooks.json is intentionally the Codex adapter and declines
    on Claude; Claude core lifecycle is supplied by SIX per-project .claude/settings.local.json
    registrations pointing at the R2 runtime hooks, ALONGSIDE the natively-loaded coordination MCP.

This checker verifies the ACTIVATION prerequisite and reports actionable status. It NEVER writes
config, NEVER trusts a hook, and NEVER auto-accepts an unexpected/changed hash (a modified hook
surfaces as untrusted via the host and MUST be re-vetted by a human, not silently trusted here).

Exit: 0 all required activation checks pass; 1 any failed (fail closed); 2 usage/bootstrap error.
"""

from __future__ import annotations
import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time

CODEX = "/opt/homebrew/bin/codex"
CODEX_PLUGIN_ID = "mycelium@mycelium-r2"
# The six mycelium core hooks Claude activates per project, each pinned to its REQUIRED
# (event, matcher, hook-script-basename). A registration only counts when the hook appears under the
# correct event AND matcher AND its command resolves to a REAL executable hook script (not an echo,
# not a dead path, not the wrong event) — six basenames appearing anywhere is NOT sufficient.
CLAUDE_SIX_SPEC = [
    ("SessionStart", "", "mycelium-health.sh"),
    ("PostToolUse", "Edit|Write", "mycelium-activity-tracker.sh"),
    ("PostToolUse", "Bash", "mycelium-post-action.sh"),
    ("PostToolUse", "Bash", "mycelium-data-tracker.sh"),
    ("PostToolUse", "Read", "mycelium-read-tracker.sh"),
    ("Stop", "", "mycelium-stop-check.sh"),
]


_INTERPRETERS = {
    "bash",
    "sh",
    "zsh",
    "dash",
    "ksh",
    "python",
    "python3",
    "perl",
    "ruby",
    "node",
}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _resolve_hook_executable(cmd, basename):
    """Return an absolute, existing, executable path for `basename` ONLY when it is the actual
    COMMAND BEING EXECUTED — the executable in command position — not merely an argument to some
    other command. The neighbor bug (seq159) was that any token ending in the basename counted, so
    `echo /abs/.../mycelium-health.sh` under the correct event still resolved and wrongly passed.

    We accept exactly the grammar the deployed registrations use and fail closed on anything else:
      * direct invocation:              /abs/.../mycelium-health.sh [args]
      * env-assignment-prefixed direct: FOO=1 BAR=2 /abs/.../mycelium-health.sh
      * explicit interpreter:           bash|sh|python3|... /abs/.../mycelium-health.sh
    The command word (after stripping leading NAME=value assignments, and after an interpreter's own
    leading flags) MUST itself be the hook script. A basename that appears only as an ARGUMENT
    (echoed, piped, quoted, or handed to an unrelated command) returns None. There is no shell
    pipeline / redirection / substitution modeling — unsupported grammar fails closed."""
    import shlex

    try:
        tokens = shlex.split(cmd)
    except Exception:
        return None
    if not tokens:
        return None
    # Strip leading `NAME=value` environment-assignment prefixes.
    i = 0
    while i < len(tokens) and _ENV_ASSIGN.match(tokens[i]):
        i += 1
    if i >= len(tokens):
        return None
    head = tokens[i]
    if os.path.basename(head) in _INTERPRETERS:
        # Explicit interpreter form: the hook script MUST be the interpreter's IMMEDIATE operand.
        # We deliberately do NOT skip interpreter flags (seq161): a flag such as `-n` (bash noexec /
        # syntax-check-only) or `-c` (run a command string, not a file) means the hook script is
        # never actually executed, so any token between the interpreter and the script is
        # unsupported grammar and fails closed.
        if i + 1 >= len(tokens):
            return None
        cand = tokens[i + 1]
    else:
        # Direct invocation: the command word itself must be the hook script.
        cand = head
    if not cand.endswith(basename):
        return None
    p = os.path.expanduser(os.path.expandvars(cand))
    if os.path.isabs(p) and os.path.isfile(p) and os.access(p, os.X_OK):
        return p
    return None


def _appserver_hooks_list(cwd, deadline_s=45):
    errf = open("/tmp/verify_hook_activation.appserver.stderr.txt", "w")
    p = subprocess.Popen(
        [CODEX, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=errf,
        text=True,
        bufsize=1,
    )

    def send(o):
        p.stdin.write(json.dumps(o) + "\n")
        p.stdin.flush()

    send(
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "hook-activation-guard",
                    "title": "Activation guard",
                    "version": "1",
                },
                "capabilities": {"experimentalApi": True, "requestAttestation": False},
            },
        }
    )
    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + deadline_s
    result = None
    try:
        while time.monotonic() < deadline:
            if not sel.select(1):
                continue
            line = p.stdout.readline()
            if not line:
                break
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("id") == 1:
                if "error" in j:
                    raise RuntimeError(j["error"])
                send({"method": "initialized"})
                send({"id": 2, "method": "hooks/list", "params": {"cwds": [cwd]}})
            elif j.get("id") == 2:
                result = j
                break
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        errf.close()
    if result is None:
        raise SystemExit("no hooks/list response before bounded deadline")
    if "error" in result:
        raise SystemExit("hooks/list error: " + json.dumps(result["error"]))
    return [h for d in result["result"]["data"] for h in d.get("hooks", [])]


def check_codex(cwd):
    print("== Codex hook-activation (read-only hooks/list; never writes/trusts) ==")
    hooks = _appserver_hooks_list(cwd)
    myc = [h for h in hooks if h.get("pluginId") == CODEX_PLUGIN_ID]
    fail = 0
    if len(myc) != 6:
        print(
            f"FAIL : expected 6 {CODEX_PLUGIN_ID} hooks discovered, found {len(myc)} "
            f"(missing/disabled discovery — reinstall or re-enable the plugin)"
        )
        fail = 1
    for h in myc:
        key = h.get("key", "?")
        en = h.get("enabled")
        ts = h.get("trustStatus")
        if en and ts == "trusted":
            print(f"ok   : {key}  enabled+trusted  {h.get('currentHash', '')[:20]}")
        else:
            reason = []
            if not en:
                reason.append("disabled")
            if ts != "trusted":
                reason.append(f"trustStatus={ts}")
            print(
                f"FAIL : {key}  {' '.join(reason)}  currentHash={h.get('currentHash', '')[:20]}"
            )
            fail = 1
    if fail:
        print(
            "ACTION: persist the SIX vetted hashes as hooks.state.<key>.trusted_hash via the supported "
            "native config/batchWrite (do NOT auto-trust a changed hash — re-vet first)."
        )
    return fail


def check_claude(project_dir, installed_plugins_path=None, settings_json_path=None):
    """Verify Claude static activation: the six core hooks are each registered under the CORRECT
    (event, matcher) with a REAL executable command path, AND the mycelium plugin is installed,
    enabled, and DECLARES the coordination MCP server. Static registration is reported SEPARATELY
    from live MCP connectivity (which is proven independently by the native audit)."""
    print(f"== Claude hook-activation (read-only, static) project={project_dir} ==")
    fail = 0
    slp = os.path.join(project_dir, ".claude", "settings.local.json")
    if not os.path.isfile(slp):
        print(f"FAIL : missing {slp} (no per-project hook registration)")
        return 1
    try:
        cfg = json.load(open(slp))
    except Exception as e:
        print(f"FAIL : {slp} unreadable: {e}")
        return 1
    hooks = cfg.get("hooks") or {}
    for event, matcher, basename in CLAUDE_SIX_SPEC:
        found = None
        for g in hooks.get(event, []) or []:
            # matcher must match exactly ("" accepts "" or a missing matcher key)
            gm = g.get("matcher", "")
            if gm != matcher and not (matcher == "" and not gm):
                continue
            for h in g.get("hooks", []):
                # Only a real command hook can dispatch a script; ignore any other hook type.
                if h.get("type") != "command":
                    continue
                p = _resolve_hook_executable(h.get("command", ""), basename)
                if p:
                    found = p
                    break
            if found:
                break
        if found:
            print(
                f"ok   : {event}[{matcher or '*'}] -> {basename}  (executable {found})"
            )
        else:
            print(
                f"FAIL : {event}[{matcher or '*'}] -> {basename} NOT registered as a real "
                "executable hook under the correct event/matcher (wrong event, echoed name, or "
                "dead/relative path does NOT count)"
            )
            fail = 1
    # Plugin must be installed AND enabled AND declare the coordination MCP server (static decl).
    ip = installed_plugins_path or os.path.expanduser(
        "~/.claude/plugins/installed_plugins.json"
    )
    sj = settings_json_path or os.path.expanduser("~/.claude/settings.json")
    try:
        installed = json.load(open(ip))
        recs = (installed.get("plugins") or {}).get("mycelium@mycelium")
        rec = (
            recs[0]
            if isinstance(recs, list) and recs
            else (recs if isinstance(recs, dict) else None)
        )
        install_path = rec.get("installPath") if rec else None
        if not (install_path and os.path.isdir(install_path)):
            print("FAIL : mycelium@mycelium not installed (no valid installPath)")
            return fail or 1
        # enabled? settings.json enabledPlugins maps "<plugin>@<mkt>" -> bool; absent => enabled.
        enabled = True
        try:
            enabled = (json.load(open(sj)).get("enabledPlugins") or {}).get(
                "mycelium@mycelium", True
            ) is not False
        except Exception:
            pass
        if not enabled:
            print(
                "FAIL : mycelium@mycelium is DISABLED in settings.json enabledPlugins"
            )
            fail = 1
        # MCP server DECLARED (static): .mcp.claude.json declares mycelium-coord with a real command.
        mcp_decl = os.path.join(install_path, ".mcp.claude.json")
        declared = False
        if os.path.isfile(mcp_decl):
            try:
                servers = json.load(open(mcp_decl)).get("mcpServers") or {}
                srv = servers.get("mycelium-coord") or {}
                cmd = os.path.expandvars(
                    (srv.get("command") or "").replace(
                        "${CLAUDE_PLUGIN_ROOT}", install_path
                    )
                )
                declared = bool(srv) and os.path.isfile(cmd) and os.access(cmd, os.X_OK)
            except Exception:
                declared = False
        if declared and enabled:
            print(
                "ok   : mycelium plugin installed+enabled; coordination MCP server DECLARED "
                f"(static) version={rec.get('version')} "
                "[live MCP connectivity proven separately by the native audit]"
            )
        elif not declared:
            print(
                "FAIL : coordination MCP server NOT declared/executable in "
                f"{mcp_decl} (missing .mcp.claude.json or mycelium-coord entry, or dead command)"
            )
            fail = 1
    except Exception as e:
        print(
            f"FAIL : installed_plugins.json unreadable ({e}); cannot confirm plugin/MCP"
        )
        fail = 1
    return fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", choices=["codex", "claude", "both"], required=True)
    ap.add_argument("--cwd", default=os.getcwd(), help="cwd for Codex hooks/list")
    ap.add_argument(
        "--project", help="Claude project dir (with .claude/settings.local.json)"
    )
    a = ap.parse_args()
    fail = 0
    if a.host in ("codex", "both"):
        fail |= check_codex(a.cwd)
    if a.host in ("claude", "both"):
        if not a.project:
            print("FAIL : --project required for claude host")
            fail |= 1
        else:
            fail |= check_claude(a.project)
    print("----")
    print(
        "ALL ACTIVATION CHECKS PASS (read-only; nothing written/trusted)"
        if not fail
        else "ACTIVATION CHECKS FAILED (payload PASS is NOT sufficient — see ACTION above)"
    )
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
