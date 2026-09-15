"""One correct wait path (request-reduction v1, item 2): the pure adapter and both routes.

Run: python3 -m pytest coordination/tests/test_waitpath.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import waitpath  # noqa: E402
from mycelium_coord.coord import Coordinator  # noqa: E402
from mycelium_coord.store import CoordStore  # noqa: E402


def test_mcp_route_caps_50_to_25_and_reports_it():
    p = waitpath.plan_wait("mcp", 50)
    assert p["applied_s"] == 25.0 and p["clamped"] is True and p["reason"] == "mcp_host_yield"
    assert p["host_yield_s"] == 30.0 and p["wakes_idle_host"] is False


def test_mcp_route_honours_explicit_host_yield():
    p = waitpath.plan_wait("mcp", 50, host_yield_s=60)
    assert p["applied_s"] == 50.0 and p["clamped"] is False and p["reason"] == "none"
    assert waitpath.plan_wait("mcp", 50, host_yield_s=30)["applied_s"] == 25.0


def test_cli_route_reports_outer_allowance():
    p = waitpath.plan_wait("cli", 50, hard_max_s=50)
    assert p["applied_s"] == 50.0 and p["outer_allowance_s"] == 60.0 and p["clamped"] is False
    q = waitpath.plan_wait("cli", 10_000, hard_max_s=50)
    assert q["applied_s"] == 50.0 and q["clamped"] is True and q["reason"] == "hard_max"


def test_unknown_route_and_bad_numbers_rejected():
    with pytest.raises(ValueError):
        waitpath.plan_wait("shell", 5)
    with pytest.raises(ValueError):
        waitpath.plan_wait("mcp", -1)
    with pytest.raises(ValueError):
        waitpath.plan_wait("mcp", 5, host_yield_s=0)


def test_check_wait_names_the_problem():
    bad = waitpath.check_wait("mcp", 50)
    assert bad["ok"] is False and "exceeds the safe MCP cap" in bad["problem"]
    assert waitpath.check_wait("mcp", 25)["ok"] is True
    assert waitpath.check_wait("cli", 50)["ok"] is False  # outer allowance required
    assert waitpath.check_wait("cli", 50, 60)["ok"] is True
    assert waitpath.check_wait("cli", 50, 55)["ok"] is False


def _seed(root):
    co = Coordinator(CoordStore(str(root)))
    co.create_task("t1", project="projA", worktree_realpath="/wt/projA", revision=1)
    co.attach("t1", "cl", role="executor", worktree_realpath="/wt/projA",
              host={"host": "claude", "session": "s1", "native_id": "n1"})
    return co


def test_coord_wait_reports_wait_path_for_both_routes(tmp_path):
    co = _seed(tmp_path)
    r = co.wait("t1", "cl", timeout_s=0.2, route="cli")
    assert r["timed_out"] is True and r["wait_path"]["route"] == "cli"
    assert r["wait_path"]["outer_allowance_s"] == pytest.approx(10.2)
    m = co.wait("t1", "cl", timeout_s=50, host_yield_s=5.2, route="mcp")
    assert m["timed_out"] is True and m["wait_path"]["applied_s"] == pytest.approx(0.2)
    assert m["wait_path"]["clamped"] is True and m["wait_path"]["reason"] == "mcp_host_yield"


def test_r5_preferred_pattern_costs_one_request_per_50s_and_fallback_two():
    """R5: the 50 s inner / 60 s outer path is preferred; the 25 s fallback is not a saving."""
    assert waitpath.model_requests(50, 50) == 1 and waitpath.model_requests(50, 25) == 2
    assert waitpath.model_requests(300, 50) == 6 and waitpath.model_requests(300, 25) == 12
    cli = waitpath.preferred_pattern("cli", task_id="t1", participant_id="cl", after_seq=7)
    assert cli["call"]["tool"] == "functions.exec" and cli["call"]["timeout_ms"] == 60000
    assert cli["call"]["cmd"] == "mycelium-coord wait t1 cl --after 7 --timeout 50"
    assert cli["plan"]["applied_s"] == 50.0 and cli["plan"]["outer_allowance_s"] == 60.0
    assert cli["model_requests_per_50s"] == 1 and cli["fallback_model_requests_per_50s"] == 2
    mcp = waitpath.preferred_pattern("mcp", job_id="j1")
    assert mcp["call"]["tool"] == "job_join"
    assert mcp["call"]["args"] == {"job_id": "j1", "timeout_s": 50.0, "host_yield_s": 60.0}
    assert mcp["plan"]["applied_s"] == 50.0 and mcp["plan"]["clamped"] is False
    assert mcp["model_requests_per_50s"] == 1
    fallback = waitpath.plan_wait("mcp", 50)  # default 30 s yield -> 25 s cap
    assert fallback["applied_s"] == 25.0 and fallback["model_requests_per_50s"] == 2
    assert fallback["preferred"] is False and mcp["plan"]["preferred"] is True
