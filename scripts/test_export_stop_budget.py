#!/usr/bin/env python3
"""The exported runtime keeps the two-prompt Stop budget.

The core-overlay replaces the source Stop hooks at export time, so a budget that only
exists in mycelium-source is not what ships. These tests export into a temp dir with the
canonical overlay and drive the exported hooks.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    target = tmp_path_factory.mktemp("export") / "mycelium"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/export_coordination.py"),
         "--source", str(ROOT / "mycelium-source"), "--target", str(target),
         "--build-id", "test.stopbudget"],
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=300,
    )
    core = target / "skills/core"
    # the overlay really replaced the hooks, and the budget helper they call ships
    for rel in ("hooks/mycelium-stop-check.sh", "hooks/mycelium-hook-lib.sh"):
        assert (core / rel).read_bytes() == (ROOT / "core-overlay/skills/core" / rel).read_bytes()
    assert (core / "scripts/stop_retry_budget.py").is_file()
    return core


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    living = tmp_path / ".living"
    living.mkdir()
    for name in ("learnings.md", "decisions.md", "conventions.md"):
        p = living / name
        p.write_text("# Existing knowledge\n")
        os.utime(p, (time.time() - 7200,) * 2)
    state = tmp_path / ".mycelium"
    state.mkdir()
    (state / "mycelium-reminded.tmp").write_text(str(int(time.time()) - 3600))
    return state


def _stop(core, root, sid="review"):
    r = subprocess.run(
        ["bash", str(core / "hooks/mycelium-stop-check.sh")], cwd=root,
        input=json.dumps({"session_id": sid, "stop_hook_active": False}),
        text=True, capture_output=True, timeout=15,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else {}


def test_changing_reasons_share_one_budget(exported, tmp_path):
    state = tmp_path / ".mycelium"
    state.mkdir()
    script = (
        'source "$1"; STATE_DIR="$2"; HOST_SESSION_ID=review; '
        'mycelium_emit_stop_block "reason $3"'
    )
    outs = []
    for i in range(3):
        r = subprocess.run(
            ["bash", "-c", script, "x", str(exported / "hooks/mycelium-hook-lib.sh"),
             str(state), str(i)],
            text=True, capture_output=True, timeout=15,
        )
        assert r.returncode == 0, r.stderr
        outs.append(json.loads(r.stdout) if r.stdout.strip() else {})
    assert [o.get("decision") for o in outs] == ["block", "block", None]
    receipt = json.loads(next(state.glob("stop-retry-*.json")).read_text())
    assert receipt["prompts"] == 2 and receipt["status"] == "deferred"


def test_exported_stop_hook_caps_retains_and_clears(exported, tmp_path):
    state = _repo(tmp_path)
    original = (state / "mycelium-reminded.tmp").read_bytes()
    results = [_stop(exported, tmp_path) for _ in range(4)]
    assert [x.get("decision") for x in results] == ["block", "block", None, None]
    # unresolved state is retained for repair, not discarded
    assert (state / "mycelium-reminded.tmp").read_bytes() == original
    receipts = list(state.glob("stop-retry-*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["status"] == "deferred"
    # a real repair clears the budget
    (tmp_path / ".living/learnings.md").write_text("# Repaired\n")
    assert _stop(exported, tmp_path).get("decision") != "block"
    assert not list(state.glob("stop-retry-*.json"))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
