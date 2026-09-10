"""Deterministic fixture: proves the accept-block delta in runner-template.md
is computed against the PRIOR baseline, not against itself after the update.

Bug this guards (optimizer-followup-review.md, finding 2): the loop used to do

    best_composite = val_composite          # update baseline first
    delta = val_composite - best_composite  # then "compute" delta -> always 0

which made every ordinary accepted iteration report zero improvement and
convergence-stop the loop after the first accept. The fix in
references/runner-template.md step (h)/(j) captures `prev_composite =
best_composite` BEFORE the update, computes `accept_delta = val_composite -
prev_composite`, and reuses that same `accept_delta` at the convergence check
in step (j) instead of recomputing it against the (already updated, and
possibly full-eval-overwritten) `best_composite`.

This fixture is pure, offline, and deterministic — no LLM calls, no I/O beyond
stdout. Run: python3 delta_baseline_fixture.py
"""

from __future__ import annotations

CONVERGENCE_THRESHOLD = 0.002


def buggy_accept_step(best_composite: float, val_composite: float) -> dict:
    """Reproduces the ORIGINAL bug for contrast. Do not use this shape."""
    best_composite = val_composite  # baseline overwritten first (the bug)
    delta = val_composite - best_composite  # always 0.0 — comparing val to itself
    return {
        "old_composite": best_composite,
        "delta": delta,
        "best_composite": best_composite,
    }


def fixed_accept_step(best_composite: float, val_composite: float) -> dict:
    """Mirrors runner-template.md step (h): capture prev_composite BEFORE
    updating best_composite, compute delta against that prior value."""
    prev_composite = best_composite  # <-- captured before the update
    best_composite = val_composite  # update happens AFTER capture
    accept_delta = val_composite - prev_composite
    return {
        "old_composite": prev_composite,
        "delta": accept_delta,
        "best_composite": best_composite,
    }


def fixed_convergence_check(accept_delta: float, phase: str) -> bool:
    """Mirrors runner-template.md step (j): reuse accept_delta, do not
    recompute `val_composite - best_composite` here."""
    return accept_delta < CONVERGENCE_THRESHOLD and phase == "quality"


def main() -> None:
    baseline = 0.70
    val_composite = 0.80  # a genuine, non-trivial improvement

    buggy = buggy_accept_step(baseline, val_composite)
    fixed = fixed_accept_step(baseline, val_composite)

    print(f"baseline={baseline}  val_composite={val_composite}")
    print(f"buggy  : old_composite={buggy['old_composite']}  delta={buggy['delta']}")
    print(f"fixed  : old_composite={fixed['old_composite']}  delta={fixed['delta']}")

    # 1. The bug reproduces exactly as described: delta collapses to 0.0
    #    because best_composite was overwritten before the subtraction.
    assert buggy["delta"] == 0.0, "expected the reproduced bug to yield delta==0.0"
    assert buggy["old_composite"] == val_composite, (
        "expected the reproduced bug's 'old_composite' to equal val_composite "
        "(it logs the NEW value under the OLD-value key)"
    )

    # 2. The fix reports the true, non-zero improvement against the real prior
    #    baseline, and logs the correct old_composite.
    expected_delta = round(val_composite - baseline, 10)
    assert abs(fixed["delta"] - expected_delta) < 1e-12, (
        f"expected fixed delta == {expected_delta}, got {fixed['delta']}"
    )
    assert fixed["old_composite"] == baseline, (
        f"expected fixed old_composite == {baseline}, got {fixed['old_composite']}"
    )
    assert fixed["best_composite"] == val_composite, (
        "best_composite must still update to the new value"
    )

    # 3. Convergence check: with the bug, delta==0.0 would immediately trigger
    #    convergence (0.0 < CONVERGENCE_THRESHOLD) on the FIRST accepted
    #    iteration, regardless of the true improvement size. With the fix, a
    #    real 0.10 improvement must NOT trigger convergence.
    assert fixed_convergence_check(buggy["delta"], "quality") is True, (
        "sanity check: the buggy zero-delta value would spuriously converge"
    )
    assert fixed_convergence_check(fixed["delta"], "quality") is False, (
        "the fixed delta (0.10 improvement) must NOT be treated as converged"
    )

    # 4. Small genuine improvement (below threshold) still correctly converges
    #    under the FIXED logic — this proves the fix doesn't merely disable
    #    convergence, it computes it correctly against the prior baseline.
    small_fixed = fixed_accept_step(0.90, 0.9005)
    assert fixed_convergence_check(small_fixed["delta"], "quality") is True, (
        "a genuinely small improvement (0.0005 < 0.002 threshold) must still "
        "converge under the fixed logic"
    )

    print("\nAll assertions passed:")
    print(" - buggy shape reproduces delta==0.0 / old_composite==val_composite")
    print(" - fixed shape reports delta=0.10 against the true prior baseline (0.70)")
    print(" - fixed shape does NOT spuriously converge on a real improvement")
    print(" - fixed shape DOES converge on a genuinely small improvement")


if __name__ == "__main__":
    main()
