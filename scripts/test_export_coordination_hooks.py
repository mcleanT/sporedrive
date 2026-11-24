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
