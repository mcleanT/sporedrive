"""One correct wait path (request-reduction v1, item 2).

Observed failure: 50-second direct MCP waits (``coord_wait`` / ``job_join``) were issued under a
host tool-call yield of roughly 30 seconds (Codex ``functions.exec`` / MCP call default), so the
host returned before the in-tool wait finished and the model saw a truncated, non-terminal result.
The CLI route worked because its shell wrapper carried a 60-second OUTER allowance around a
50-second inner wait.

This module is the single, pure, documented adapter that both routes call BEFORE waiting:

* ``route="mcp"`` — the inner wait is capped at ``host_yield_s - MCP_SAFE_MARGIN_S`` (25 s under
  the default 30 s yield) unless the caller declares a larger host yield explicitly. The result
  reports ``applied_s`` and ``clamped`` truthfully so a caller never mistakes a short wait for a
  long one.
* ``route="cli"`` — the inner wait is honored (up to the hard maximum) and the plan reports the
  OUTER shell allowance the wrapper must pass (inner + ``CLI_OUTER_MARGIN_S``, e.g. 60 s for 50 s).

Everything here is deterministic and clock-free. It never sleeps, never polls, and never claims a
stored message can wake an idle host: a wait is a bounded in-tool poll, nothing more.
"""

from __future__ import annotations

from typing import Optional

MCP_HOST_YIELD_S = 30.0  # observed default host yield for a direct MCP tool call
MCP_SAFE_MARGIN_S = 5.0  # headroom so the tool returns before the host yields
MCP_SAFE_WAIT_S = MCP_HOST_YIELD_S - MCP_SAFE_MARGIN_S  # 25 s
CLI_OUTER_MARGIN_S = 10.0  # shell wrapper allowance above the inner wait (50 s -> 60 s)
ROUTES = ("mcp", "cli")


def plan_wait(
    route: str,
    requested_s: float,
    *,
    host_yield_s: Optional[float] = None,
    hard_max_s: float = 600.0,
) -> dict:
    """Compute the safe inner wait for ``route`` and report what was applied.

    Returns a dict with ``route``, ``requested_s``, ``applied_s``, ``clamped`` (True when the
    inner wait is shorter than requested), ``reason`` (``mcp_host_yield`` / ``hard_max`` /
    ``none``), ``host_yield_s`` (mcp only) and ``outer_allowance_s`` (cli only — the wrapper
    timeout the caller must supply). Raises ``ValueError`` on an unknown route.
    """
    route = str(route or "").strip().lower()
    if route not in ROUTES:
        raise ValueError("unknown wait route %r (expected one of %s)" % (route, ", ".join(ROUTES)))
    try:
        requested = float(requested_s)
    except (TypeError, ValueError):
        raise ValueError("requested_s must be a number")
    if requested != requested or requested < 0:  # NaN or negative
        raise ValueError("requested_s must be a non-negative number")
    hard_max = max(0.0, float(hard_max_s))
    applied = min(requested, hard_max)
    reason = "hard_max" if applied < requested else "none"
    plan = {
        "route": route,
        "requested_s": requested,
        "applied_s": applied,
        "clamped": False,
        "reason": reason,
        "wakes_idle_host": False,
    }
    if route == "mcp":
        yield_s = MCP_HOST_YIELD_S if host_yield_s is None else float(host_yield_s)
        if yield_s != yield_s or yield_s <= 0:
            raise ValueError("host_yield_s must be a positive number")
        safe = max(0.0, yield_s - MCP_SAFE_MARGIN_S)
        if applied > safe:
            applied = safe
            reason = "mcp_host_yield"
        plan.update({
            "applied_s": applied,
            "reason": reason,
            "host_yield_s": yield_s,
            "safe_margin_s": MCP_SAFE_MARGIN_S,
        })
    else:
        plan.update({
            "outer_allowance_s": applied + CLI_OUTER_MARGIN_S,
            "outer_margin_s": CLI_OUTER_MARGIN_S,
        })
    plan["clamped"] = applied < requested
    return plan


def check_wait(route: str, inner_s: float, outer_s: Optional[float] = None, *,
               host_yield_s: Optional[float] = None) -> dict:
    """Check a caller's chosen inner/outer pair against the plan. ``ok`` is True only when the
    inner wait is within the safe cap (mcp) or the outer allowance covers inner + margin (cli).
    Never raises for an unsafe pair — it reports it, with the corrected plan attached."""
    plan = plan_wait(route, inner_s, host_yield_s=host_yield_s)
    inner = float(inner_s)
    if plan["route"] == "mcp":
        ok = inner <= plan["applied_s"]
        problem = None if ok else (
            "inner wait %.1fs exceeds the safe MCP cap %.1fs (host yield %.1fs - %.1fs margin)"
            % (inner, plan["applied_s"], plan["host_yield_s"], plan["safe_margin_s"]))
    else:
        needed = inner + CLI_OUTER_MARGIN_S
        if outer_s is None:
            ok, problem = False, "cli route requires an explicit outer allowance (>= %.1fs)" % needed
        else:
            ok = float(outer_s) >= needed
            problem = None if ok else (
                "outer allowance %.1fs is below inner %.1fs + %.1fs margin"
                % (float(outer_s), inner, CLI_OUTER_MARGIN_S))
    return {"ok": ok, "problem": problem, "plan": plan}
