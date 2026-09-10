#!/usr/bin/env python3
"""Focused negative/positive tests for verify_hook_activation.check_claude (seq157 false-green fix).

Reproduces root's negative probe (all six basenames echoed under the wrong event must FAIL) plus
neighbors: dead/echo path, disabled plugin, missing MCP declaration. Also a true-positive that
mirrors the real per-project registration. Run: python3 scripts/test_verify_hook_activation.py
"""

import copy
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_hook_activation as V  # noqa: E402

RT = "/Users/mst36/.claude/mycelium-runtime-wfi-v2/skills/core/hooks"
REAL_CACHE = (
    "/Users/mst36/.claude/plugins/cache/mycelium/mycelium/0.6.0-coord.integration.r2"
)


def _proj(tmp, hooks):
    os.makedirs(tmp, exist_ok=True)
    d = os.path.join(tmp, "proj")
    os.makedirs(os.path.join(d, ".claude"), exist_ok=True)
    json.dump(
        {"hooks": hooks}, open(os.path.join(d, ".claude", "settings.local.json"), "w")
    )
    return d


def _installed(tmp, install_path):
    os.makedirs(tmp, exist_ok=True)
    p = os.path.join(tmp, "installed_plugins.json")
    json.dump(
        {
            "version": 1,
            "plugins": {
                "mycelium@mycelium": [
                    {
                        "installPath": install_path,
                        "version": "0.6.0+coord.integration.r2",
                    }
                ]
            },
        },
        open(p, "w"),
    )
    return p


def _settings(tmp, enabled_map):
    os.makedirs(tmp, exist_ok=True)
    p = os.path.join(tmp, "settings.json")
    json.dump({"enabledPlugins": enabled_map}, open(p, "w"))
    return p


CORRECT_HOOKS = {
    "SessionStart": [
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": f"{RT}/mycelium-health.sh"}],
        }
    ],
    "PostToolUse": [
        {
            "matcher": "Edit|Write",
            "hooks": [
                {"type": "command", "command": f"{RT}/mycelium-activity-tracker.sh"}
            ],
        },
        {
            "matcher": "Bash",
            "hooks": [
                {"type": "command", "command": f"{RT}/mycelium-post-action.sh"},
                {"type": "command", "command": f"{RT}/mycelium-data-tracker.sh"},
            ],
        },
        {
            "matcher": "Read",
            "hooks": [{"type": "command", "command": f"{RT}/mycelium-read-tracker.sh"}],
        },
    ],
    "Stop": [
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": f"{RT}/mycelium-stop-check.sh"}],
        }
    ],
}

# root's negative probe: all six basenames only echoed under Stop
ALL_ECHOED_UNDER_STOP = {
    "Stop": [
        {
            "matcher": "",
            "hooks": [
                {"type": "command", "command": f"echo {b}"}
                for b in [
                    "mycelium-health.sh",
                    "mycelium-activity-tracker.sh",
                    "mycelium-post-action.sh",
                    "mycelium-data-tracker.sh",
                    "mycelium-read-tracker.sh",
                    "mycelium-stop-check.sh",
                ]
            ],
        }
    ],
}

# correct events but dead/echo commands
CORRECT_EVENTS_DEAD_PATHS = {
    "SessionStart": [
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": "echo mycelium-health.sh"}],
        }
    ],
    "PostToolUse": [
        {
            "matcher": "Edit|Write",
            "hooks": [
                {"type": "command", "command": "echo mycelium-activity-tracker.sh"}
            ],
        },
        {
            "matcher": "Bash",
            "hooks": [
                {"type": "command", "command": "echo mycelium-post-action.sh"},
                {"type": "command", "command": "echo mycelium-data-tracker.sh"},
            ],
        },
        {
            "matcher": "Read",
            "hooks": [{"type": "command", "command": "echo mycelium-read-tracker.sh"}],
        },
    ],
    "Stop": [
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": "echo mycelium-stop-check.sh"}],
        }
    ],
}


def _map_commands(spec, fn):
    """Deep-copy a CORRECT_HOOKS-shaped spec, applying fn(command)->command to every hook command."""
    out = copy.deepcopy(spec)
    for groups in out.values():
        for g in groups:
            for h in g["hooks"]:
                h["command"] = fn(h["command"])
    return out


def _set_type(spec, t):
    """Deep-copy and set every hook's type to `t`."""
    out = copy.deepcopy(spec)
    for groups in out.values():
        for g in groups:
            for h in g["hooks"]:
                h["type"] = t
    return out


# seq159 neighbor repro: the SIX real absolute executables under the CORRECT events, but each is only
# an ARGUMENT to echo (`echo /abs/.../mycelium-health.sh`). The old guard passed because any token
# ending in the basename counted; command-position validation must now FAIL this.
ABSOLUTE_ECHO_ARGUMENT = _map_commands(CORRECT_HOOKS, lambda c: f"echo {c}")
# Correct events + real executables, but the hook is not a `command` hook -> nothing dispatches.
WRONG_HOOK_TYPE = _set_type(CORRECT_HOOKS, "webhook")
# Supported deployed grammars that MUST still PASS: env-assignment-prefixed direct, and interpreter.
ENV_PREFIXED_DIRECT = _map_commands(CORRECT_HOOKS, lambda c: f"MYCELIUM_AUDIT=1 {c}")
INTERPRETER_FORM = _map_commands(CORRECT_HOOKS, lambda c: f"bash {c}")
# seq161: `bash -n /abs/hook.sh` is bash noexec (syntax-check only) — the hook never runs. The script
# is NOT the interpreter's immediate operand, so this unsupported grammar must FAIL closed.
INTERPRETER_NOEXEC_FLAG = _map_commands(CORRECT_HOOKS, lambda c: f"bash -n {c}")


def run(name, project, installed_p, settings_p, expect_pass):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = V.check_claude(
            project, installed_plugins_path=installed_p, settings_json_path=settings_p
        )
    ok = (rc == 0) == expect_pass
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: rc={rc} expect_pass={expect_pass}")
    if not ok:
        print("  --- checker output ---")
        print("  " + buf.getvalue().replace("\n", "\n  "))
    return ok


def main():
    allok = True
    with tempfile.TemporaryDirectory() as tmp:
        good_installed = _installed(tmp, REAL_CACHE)
        good_settings = _settings(tmp, {})  # mycelium not disabled
        # P: correct registrations + installed + enabled + MCP declared -> PASS
        allok &= run(
            "P.correct",
            _proj(tmp + "/p", CORRECT_HOOKS),
            good_installed,
            good_settings,
            True,
        )
        # N1: root's repro — six echoed under Stop -> FAIL
        allok &= run(
            "N1.all_echoed_under_stop",
            _proj(tmp + "/n1", ALL_ECHOED_UNDER_STOP),
            good_installed,
            good_settings,
            False,
        )
        # N2: correct events but dead/echo paths -> FAIL
        allok &= run(
            "N2.correct_events_dead_paths",
            _proj(tmp + "/n2", CORRECT_EVENTS_DEAD_PATHS),
            good_installed,
            good_settings,
            False,
        )
        # N3: correct hooks but plugin DISABLED -> FAIL
        allok &= run(
            "N3.plugin_disabled",
            _proj(tmp + "/n3", CORRECT_HOOKS),
            good_installed,
            _settings(tmp + "_d", {"mycelium@mycelium": False}),
            False,
        )
        # N4: correct hooks but installPath has NO .mcp.claude.json (missing MCP decl) -> FAIL
        empty_ip = os.path.join(tmp, "empty_installpath")
        os.makedirs(empty_ip, exist_ok=True)
        allok &= run(
            "N4.missing_mcp_decl",
            _proj(tmp + "/n4", CORRECT_HOOKS),
            _installed(tmp + "_e", empty_ip),
            good_settings,
            False,
        )
        # N5 (seq159 repro): real absolute executables but only as an ARGUMENT to echo -> FAIL
        allok &= run(
            "N5.absolute_echo_argument",
            _proj(tmp + "/n5", ABSOLUTE_ECHO_ARGUMENT),
            good_installed,
            good_settings,
            False,
        )
        # N6: correct events + real executables but hook type != "command" -> FAIL
        allok &= run(
            "N6.wrong_hook_type",
            _proj(tmp + "/n6", WRONG_HOOK_TYPE),
            good_installed,
            good_settings,
            False,
        )
        # P2: env-assignment-prefixed direct invocation is a supported deployed grammar -> PASS
        allok &= run(
            "P2.env_prefixed_direct",
            _proj(tmp + "/p2", ENV_PREFIXED_DIRECT),
            good_installed,
            good_settings,
            True,
        )
        # P3: explicit interpreter form (bash /abs/hook.sh) is a supported grammar -> PASS
        allok &= run(
            "P3.interpreter_form",
            _proj(tmp + "/p3", INTERPRETER_FORM),
            good_installed,
            good_settings,
            True,
        )
        # N7 (seq161): `bash -n /abs/hook.sh` is noexec (syntax-check only) — hook never runs -> FAIL
        allok &= run(
            "N7.interpreter_noexec_flag",
            _proj(tmp + "/n7", INTERPRETER_NOEXEC_FLAG),
            good_installed,
            good_settings,
            False,
        )
    print("----")
    print("ALL GUARD TESTS PASS" if allok else "GUARD TESTS FAILED")
    sys.exit(0 if allok else 1)


if __name__ == "__main__":
    main()
