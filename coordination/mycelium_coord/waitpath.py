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

MCP_HOST_YIELD_S = 30.0  # observed DEFAULT host yield for a direct MCP tool call (fallback only)
MCP_SAFE_MARGIN_S = 5.0  # headroom so the tool returns before the host yields
MCP_SAFE_WAIT_S = MCP_HOST_YIELD_S - MCP_SAFE_MARGIN_S  # 25 s: the truthful short FALLBACK
CLI_OUTER_MARGIN_S = 10.0  # shell wrapper allowance above the inner wait (50 s -> 60 s)
ROUTES = ("mcp", "cli")

# R5: the PREFERRED pattern is the one that already worked in the audit — ONE model request per
# 50 s of waiting: a 50 s inner wait under an explicit 60 s outer allowance. On the CLI route the
# outer allowance is the shell call's own timeout pragma; on the MCP route it is the host's
# tool-call yield, declared to the tool as host_yield_s=60 so the inner wait is not capped to 25 s.
# The 25 s fallback is only for a host whose yield cannot be raised; two 25 s waits cost two model
# requests per 50 s, the same count as the broken 50 s wait plus its follow-up, so the fallback is
# never a request-count saving and must not be reported as one.
PREFERRED_INNER_S = 50.0
PREFERRED_OUTER_S = 60.0


def model_requests(horizon_s: float, inner_s: float) -> int:
    """Model requests needed to cover ``horizon_s`` of waiting with ``inner_s`` waits (one request
    per wait call, ceil). Deterministic; used to compare patterns over the SAME elapsed horizon."""
    inner = float(inner_s)
    if inner <= 0:
        raise ValueError("inner_s must be positive")
    h = max(0.0, float(horizon_s))
    n = int(h // inner)
    return n + (1 if h - n * inner > 1e-9 else 0)


def preferred_pattern(route: str, *, task_id: str = "TASK", participant_id: str = "PARTICIPANT",
                      after_seq: int = 0, job_id: Optional[str] = None) -> dict:
    """The reusable exact pattern for a long wait on ``route`` (no per-interval planning call):
    inner 50 s / outer 60 s, with the literal call shape and the request count it costs over a
    50 s horizon (1) versus the 25 s fallback (2)."""
    route = str(route or "").strip().lower()
    if route not in ROUTES:
        raise ValueError("unknown wait route %r (expected one of %s)" % (route, ", ".join(ROUTES)))
    inner, outer = PREFERRED_INNER_S, PREFERRED_OUTER_S
    if route == "cli":
        cmd = ("mycelium-coord job-join %s --timeout %d" % (job_id, int(inner)) if job_id else
               "mycelium-coord wait %s %s --after %d --timeout %d" % (task_id, participant_id, after_seq, int(inner)))
        call = {"tool": "functions.exec", "timeout_ms": int(outer * 1000), "cmd": cmd,
                "note": "the outer timeout_ms pragma IS the 60 s allowance; the inner --timeout 50 runs inside it"}
    else:
        tool = "job_join" if job_id else "coord_wait"
        args = ({"job_id": job_id} if job_id else
                {"task_id": task_id, "participant_id": participant_id, "after_seq": after_seq})
        args.update({"timeout_s": inner, "host_yield_s": outer})
        call = {"tool": tool, "args": args,
                "note": "requires the host's MCP tool-call yield to be at least 60 s; host_yield_s=60 "
                        "declares it so the inner wait is not capped at the 25 s fallback"}
    plan = plan_wait(route, inner, host_yield_s=outer if route == "mcp" else None)
    return {
        "route": route, "inner_s": inner, "outer_s": outer, "call": call, "plan": plan,
        "model_requests_per_50s": model_requests(50.0, plan["applied_s"]),
        "fallback_inner_s": MCP_SAFE_WAIT_S,
        "fallback_model_requests_per_50s": model_requests(50.0, MCP_SAFE_WAIT_S),
        "preferred": True,
    }


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
    plan["model_requests_per_50s"] = model_requests(50.0, applied) if applied > 0 else None
    plan["preferred"] = (applied >= PREFERRED_INNER_S) or (requested < PREFERRED_INNER_S and not plan["clamped"])
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
