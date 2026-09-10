#!/usr/bin/env python3
"""Focused shape/error regression for crossrun_view (rec 67 review).

Covers the case that previously raised rc1/AttributeError: a summary that is valid JSON but a bare
list `[]` (non-object). The scan must preserve it as an error row and keep processing the other
runs, and wall-clock duration must be None (not 0) when finish is missing/invalid.

Run: python3 scripts/test_crossrun_view.py   (also importable by pytest — functions named test_*)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crossrun_view as cv  # noqa: E402


def _mk(checks: Path, name: str, payload) -> None:
    d = checks / name
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps(payload))


def _fixture_repo(tmp: Path) -> Path:
    checks = tmp / "checks"
    checks.mkdir()
    # non-object summary (the regression): valid JSON, but a bare list
    _mk(checks, "live-20260101T000000Z", [])
    # a valid object run WITH start+finish -> real duration
    _mk(
        checks,
        "live-20260101T000100Z",
        {
            "contract": "x",
            "passed": 3,
            "total": 5,
            "failed": ["a", "b"],
            "started_at": "2026-01-01T00:01:00+00:00",
            "finished_at": "2026-01-01T00:01:12.5+00:00",
        },
    )
    # a valid object run with NO finish -> unknown duration (must be None, not 0)
    _mk(
        checks,
        "bridge-20260101T000200Z",
        {"contract": "x", "passed": 1, "at": "2026-01-01T00:02:00+00:00"},
    )
    return tmp


def test_nonobject_summary_is_error_row_and_scan_continues():
    with tempfile.TemporaryDirectory() as t:
        runs = cv.scan(_fixture_repo(Path(t)))
    live = {r["dir"].split("/")[-1]: r for r in runs["live"]}
    bad = live["live-20260101T000000Z"]
    good = live["live-20260101T000100Z"]
    assert bad["err"] and "non-object" in bad["err"], f"bad run not flagged: {bad}"
    assert bad["passed"] is None and bad["total"] is None, (
        "error row must not fabricate values"
    )
    assert good["err"] is None and good["passed"] == 3, (
        "valid run must still be processed"
    )
    assert good["failed"] == 2, "failed list must be counted"
    # render must not raise on the mixed set
    md = cv.render(Path("."), runs)
    assert "non-object summary" in md and "live-20260101T000100Z" in md


def test_duration_unknown_is_distinct_from_zero():
    with tempfile.TemporaryDirectory() as t:
        runs = cv.scan(_fixture_repo(Path(t)))
    good = next(r for r in runs["live"] if r["dir"].endswith("000100Z"))
    nofin = next(r for r in runs["bridge"] if r["dir"].endswith("000200Z"))
    assert good["dur_s"] == 12.5, f"expected 12.5s, got {good['dur_s']}"
    assert nofin["dur_s"] is None, "missing finish must be None (unknown), not 0"
    assert cv.wall_clock_s("bad", "also-bad") is None, "unparseable timestamps -> None"


def test_duration_neighbors_mixed_tz_and_reversed():
    # mixed naive/aware: the subtraction raises TypeError; must be caught -> None, never crash a row
    assert cv.wall_clock_s("2026-01-01T00:00:00", "2026-01-01T00:01:00+00:00") is None
    assert cv.wall_clock_s("2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00") is None
    # reversed interval (finish before start): invalid -> None, NOT -60.0
    assert (
        cv.wall_clock_s("2026-01-01T00:01:00+00:00", "2026-01-01T00:00:00+00:00")
        is None
    )
    # both-naive valid interval still works
    assert cv.wall_clock_s("2026-01-01T00:00:00", "2026-01-01T00:00:05") == 5.0


if __name__ == "__main__":
    test_nonobject_summary_is_error_row_and_scan_continues()
    test_duration_unknown_is_distinct_from_zero()
    test_duration_neighbors_mixed_tz_and_reversed()
    print(
        "crossrun_view regression: PASS (non-object error row; unknown/mixed-tz/reversed dur -> None, not 0/neg)"
    )
