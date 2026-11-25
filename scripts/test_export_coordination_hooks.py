#!/usr/bin/env python3
"""merge_hooks registers the coordination adapter on BOTH the SessionStart and the PostToolUse
(active) boundaries for both hosts, idempotently, without dropping the source health/Stop hooks."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_coordination as ec  # noqa: E402

SOURCE = Path(__file__).resolve().parents[1] / "mycelium-source"


def _coord_groups(hooks, event):
    return [
        grp for grp in hooks["hooks"].get(event, [])
        if any("mycelium-coord-attach.sh" in h.get("command", "") for h in grp.get("hooks", []))
    ]


def test_merge_registers_both_boundaries_idempotently():
    h1 = ec.merge_hooks(SOURCE)
    assert len(_coord_groups(h1, "SessionStart")) == 1
    ptu = _coord_groups(h1, "PostToolUse")
    assert len(ptu) == 1 and ptu[0]["matcher"] == "Bash"
    # the coord command resolves either host's plugin root (two-host, shared script)
    cmd = ptu[0]["hooks"][0]["command"]
    assert "PLUGIN_ROOT" in cmd and "CLAUDE_PLUGIN_ROOT" in cmd
    # source health SessionStart + Stop are preserved, not clobbered
    assert any("mycelium-health.sh" in h.get("command", "")
               for grp in h1["hooks"]["SessionStart"] for h in grp.get("hooks", []))
    assert h1["hooks"].get("Stop")
    # idempotent: a second merge adds no duplicates
    h2 = ec.merge_hooks(SOURCE)
    assert len(_coord_groups(h2, "SessionStart")) == 1
    assert len(_coord_groups(h2, "PostToolUse")) == 1


if __name__ == "__main__":
    test_merge_registers_both_boundaries_idempotently()
    print("ok")


def test_codex_mcp_config_forwards_the_called_session_markers():
    # Codex passes only listed variables to a plugin MCP server. A called Codex session that loaded the plugin
    # would otherwise run an unrestricted server that could start calls of its own.
    env_vars = ec.build_mcp_json()["mcpServers"]["mycelium-coord"]["env_vars"]
    assert "MYCELIUM_COORD_ADVISORY" in env_vars and "MYCELIUM_CONSULT_TARGET" in env_vars
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "coordination"))
    from mycelium_coord import consult

    assert consult.ADVISORY_ENV in env_vars and consult.RECURSION_ENV in env_vars


def test_export_ships_the_call_mode_adapters_where_the_candidate_finds_them(tmp_path):
    # The first live MCP smoke of an exported build: every call ended adapter_missing, because the exporter shipped
    # coordination/ but not src/consult/, which consult.consult_dir() resolves from the exported package.
    stage, _ = ec._stage(SOURCE, tmp_path / "target", "test.adapters", None)
    adapters = ("claude_ask.sh", "codex_call.sh", "agy_ask.py")
    assert [n for n in adapters if not (stage / "src" / "consult" / n).is_file()] == []
    assert ec._validate_consult_adapters(stage) == []
    _, _, problems, _ = ec._cross_origin(stage, SOURCE, None)
    assert [p for p in problems if "src/consult" in p] == []
