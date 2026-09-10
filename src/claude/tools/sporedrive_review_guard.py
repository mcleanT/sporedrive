#!/usr/bin/env python3
"""sporedrive_review_guard.py — bundled helper that enforces the R3 managed-review launch bound.

Why this exists: ``codex_ask.sh``'s ``-x``/``-a``/``-T`` options used to only LABEL the receipt
(schema 1.1.0 ``execution_ref``/``action_ref`` fields); a MANAGED invocation (any of ``-x``, ``-a``,
``-T`` given) could launch codex with no allowance and no finite deadline at all. This guard is the
runtime/module-discovery shim the shell wrapper calls, in a subprocess, BEFORE it ever starts codex:

* ``claim``  — atomically claims the caller's existing review-launch reservation
  (:meth:`mycelium_coord.execution.ExecutionManager.claim_review`) and reports the effective (policy)
  deadline. A managed launch with no valid reservation refuses here, so codex never starts.
* ``settle`` — reconciles a claimed review launch after codex returns
  (:meth:`mycelium_coord.execution.ExecutionManager.settle_review`). Never refunds, never relaunches.

This module is intentionally small and dependency-light: it is bundled alongside ``codex_ask.sh``
(installed by ``scripts/wfctl.py``) so an installed copy of the wrapper can still resolve the
coordination package even when it is not checked out relative to the installed tools directory.
Legacy (unmanaged) launches never invoke this module, so this discovery dependency never affects them.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_REFUSED = 3
EXIT_COORD_PKG_NOT_FOUND = 70


def _discover_coord_pkg() -> None:
    """Make ``import mycelium_coord`` succeed, trying (in order): an explicit
    ``$SPOREDRIVE_COORD_PKG`` override, an already-importable package, then a few paths relative to
    this file's own location (covers the maintained-repo checkout layout, plus a couple of common
    install layouts). Exits with a clear message on ``sys.stderr`` if none of that works.
    """
    env_pkg = os.environ.get("SPOREDRIVE_COORD_PKG")
    if env_pkg:
        sys.path.insert(0, env_pkg)
        try:
            import mycelium_coord  # noqa: F401

            return
        except ImportError:
            pass  # fall through to the other discovery strategies below

    try:
        import mycelium_coord  # noqa: F401

        return
    except ImportError:
        pass

    guard_dir = Path(__file__).resolve().parent
    candidates = (
        guard_dir.parent.parent.parent
        / "coordination",  # repo layout: src/claude/tools/../../../coordination
        guard_dir.parent
        / "coordination",  # tools/ and coordination/ installed as siblings
        Path.home()
        / "tools"
        / "sporedrive"
        / "coordination",  # maintained-checkout default
    )
    for candidate in candidates:
        if (candidate / "mycelium_coord").is_dir():
            sys.path.insert(0, str(candidate))
            try:
                import mycelium_coord  # noqa: F401

                return
            except ImportError:
                continue

    sys.stderr.write(
        "sporedrive_review_guard: cannot locate the mycelium_coord package "
        "(set SPOREDRIVE_COORD_PKG to its parent directory)\n"
    )
    sys.exit(EXIT_COORD_PKG_NOT_FOUND)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="sporedrive_review_guard.py", description=__doc__)
    sub = ap.add_subparsers(dest="op", required=True)

    p = sub.add_parser("claim", help="claim an open review-launch reservation")
    p.add_argument("--task", required=True, dest="task_id")
    p.add_argument("--execution", required=True, dest="execution_id")
    p.add_argument("--action", required=True, dest="action_id")
    p.add_argument("--launch-identity", required=True, dest="launch_identity")
    p.add_argument(
        "--deadline", type=float, default=None, dest="caller_deadline_seconds"
    )

    p = sub.add_parser("settle", help="reconcile a claimed review launch")
    p.add_argument("--task", required=True, dest="task_id")
    p.add_argument("--action", required=True, dest="action_id")
    p.add_argument("--outcome", required=True)
    p.add_argument("--launch-identity", default=None, dest="launch_identity")
    p.add_argument("--response", default=None, dest="response_ref")

    return ap.parse_args(argv)


def _claim(args: argparse.Namespace) -> int:
    from mycelium_coord.execution import ExecutionManager
    from mycelium_coord.store import CoordStore, StoreError

    em = ExecutionManager(CoordStore())
    try:
        result = em.claim_review(
            args.task_id,
            execution_id=args.execution_id,
            action_id=args.action_id,
            launch_identity=args.launch_identity,
            caller_deadline_seconds=args.caller_deadline_seconds,
        )
    except StoreError as e:
        sys.stderr.write(f"REFUSED={e.code}\n")
        return EXIT_REFUSED

    if not result.get("ok"):
        sys.stderr.write(f"REFUSED={result.get('reason')}\n")
        return EXIT_REFUSED
    print(f"EFFECTIVE_DEADLINE={result['effective_deadline_seconds']}")
    return EXIT_OK


def _settle(args: argparse.Namespace) -> int:
    from mycelium_coord.execution import ExecutionManager
    from mycelium_coord.store import CoordStore, StoreError

    em = ExecutionManager(CoordStore())
    try:
        em.settle_review(
            args.task_id,
            action_id=args.action_id,
            outcome=args.outcome,
            launch_identity=args.launch_identity,
            response_ref=args.response_ref,
        )
    except StoreError as e:
        # Settlement is best-effort from the shell caller's side (non-fatal warning, like the
        # receipt write); still surface the reason for anyone reading stderr directly.
        sys.stderr.write(f"SETTLE_FAILED={e.code}\n")
        return 1
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    _discover_coord_pkg()
    if args.op == "claim":
        return _claim(args)
    return _settle(args)


if __name__ == "__main__":
    raise SystemExit(main())
