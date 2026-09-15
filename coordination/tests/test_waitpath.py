"""One correct wait path (request-reduction v1, item 2): the pure adapter and both routes.

Run: python3 -m pytest coordination/tests/test_waitpath.py -q
"""
from __future__ import annotations

import json
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


def _host_completes(outer_s, inner_s):
    """Deterministic host model: a bounded in-tool wait completes within one host call only when
    the inner wait plus the safe margin fits inside the outer yield the call actually declares."""
    return inner_s + waitpath.MCP_SAFE_MARGIN_S <= outer_s


def test_r5_preferred_pattern_costs_one_request_per_50s_and_fallback_two():
    """R5: the 50 s inner / 60 s outer path is preferred; the 25 s fallback is not a saving."""
    assert waitpath.model_requests(50, 50) == 1 and waitpath.model_requests(50, 25) == 2
    assert waitpath.model_requests(300, 50) == 6 and waitpath.model_requests(300, 25) == 12
    mcp = waitpath.preferred_pattern("mcp", job_id="j1")
    assert mcp["call"]["inner"] == {"tool": "job_join",
                                    "args": {"job_id": "j1", "timeout_s": 50.0, "host_yield_s": 60.0}}
    assert mcp["plan"]["applied_s"] == 50.0 and mcp["plan"]["clamped"] is False
    assert mcp["model_requests_per_50s"] == 1 and mcp["fallback_model_requests_per_50s"] == 2
    fallback = waitpath.plan_wait("mcp", 50)  # default 30 s yield -> 25 s cap
    assert fallback["applied_s"] == 25.0 and fallback["model_requests_per_50s"] == 2
    assert fallback["preferred"] is False and mcp["plan"]["preferred"] is True


def test_r5_mcp_pattern_declares_the_real_exec_pragma_and_the_pragma_covers_the_inner_wait():
    """R5 (verification repair): the Codex outer allowance is the FIRST-LINE
    `// @exec: {"yield_time_ms": ...}` pragma of a functions.exec script, not a timeout_ms
    argument. The test EXERCISES the generated shape: it parses the pragma the call declares,
    feeds that yield back through check_wait/plan_wait, and models the host completing the call."""
    mcp = waitpath.preferred_pattern("mcp", task_id="t1", participant_id="cl", after_seq=7)
    call = mcp["call"]
    assert call["tool"] == "functions.exec" and "timeout_ms" not in call
    script = call["script"]
    first, body = script.split("\n", 1)
    assert first == call["pragma"] == '// @exec: {"yield_time_ms": 60000}'
    parsed = waitpath.parse_exec_pragma(script)
    assert parsed["yield_time_ms"] == 60000 and parsed["yield_s"] == 60.0
    assert waitpath.declared_outer_s(call) == 60.0
    # the inner MCP call inside the script is the real tool with host_yield_s equal to the pragma
    inner = call["inner"]
    assert inner["tool"] == "coord_wait" and inner["args"]["timeout_s"] == 50.0
    assert inner["args"]["host_yield_s"] == parsed["yield_s"]
    assert body == "return await mycelium_coord.coord_wait(%s);" % json.dumps(inner["args"], sort_keys=True)
    assert json.loads(body[body.index("(") + 1:body.rindex(")")]) == inner["args"]
    # exercise it: the declared yield keeps the 50 s inner wait uncapped and within the safe cap
    chk = waitpath.check_wait("mcp", inner["args"]["timeout_s"], host_yield_s=parsed["yield_s"])
    assert chk["ok"] is True and chk["plan"]["applied_s"] == 50.0 and chk["plan"]["clamped"] is False
    assert _host_completes(parsed["yield_s"], inner["args"]["timeout_s"])
    # and the same inner wait under the default 30 s yield is truthfully clamped to 25 s
    dflt = waitpath.check_wait("mcp", 50.0)
    assert dflt["ok"] is False and dflt["plan"]["applied_s"] == 25.0 and dflt["plan"]["reason"] == "mcp_host_yield"
    assert not _host_completes(waitpath.MCP_HOST_YIELD_S, 50.0)
    # a malformed or missing pragma is refused, never read as a 60 s allowance
    for bad in ("return 1;", "// @exec: {}\nreturn 1;", '// @exec: {"yield_time_ms": 0}\nx',
                'x\n// @exec: {"yield_time_ms": 60000}'):
        with pytest.raises(ValueError):
            waitpath.parse_exec_pragma(bad)
    assert waitpath.exec_pragma(60, max_output_tokens=2000) == '// @exec: {"max_output_tokens": 2000, "yield_time_ms": 60000}'


def test_r5_cli_pattern_is_the_claude_bash_timeout_and_codex_exec_command_cannot_do_50s():
    cli = waitpath.preferred_pattern("cli", task_id="t1", participant_id="cl", after_seq=7)
    call = cli["call"]
    assert call["tool"] == "Bash" and call["host"] == "claude" and "timeout_ms" not in call
    assert call["cmd"] == "mycelium-coord wait t1 cl --after 7 --timeout 50"
    assert call["timeout"] == 60000 and waitpath.declared_outer_s(call) == 60.0
    chk = waitpath.check_wait("cli", 50.0, waitpath.declared_outer_s(call))
    assert chk["ok"] is True and chk["plan"]["outer_allowance_s"] == 60.0
    assert cli["plan"]["applied_s"] == 50.0 and cli["model_requests_per_50s"] == 1
    # Codex's exec_command initial yield cap (30000 ms) cannot hold a 50 s synchronous CLI wait
    codex = call["codex"]
    assert codex["tool"] == "exec_command" and codex["max_yield_time_ms"] == 30000
    assert codex["single_call_50s"] is False
    assert waitpath.check_wait("cli", 50.0, codex["max_yield_time_ms"] / 1000.0)["ok"] is False
    assert not _host_completes(codex["max_yield_time_ms"] / 1000.0, 50.0)
