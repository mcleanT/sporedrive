# Optimization Loop Template (optimize.py)

Template for the main CLI entrypoint that orchestrates the iterative prompt optimization loop. Customize the sections marked with `{{placeholders}}`.

## Architecture Overview

```
optimize.py (this file)
    ├── Loads prompt + test inputs
    ├── Runs baseline evaluation
    └── For each iteration:
        ├── analyze_errors() → error patterns
        ├── run_optimizer_tournament() → N candidate prompts
        ├── Screen candidates on 1 test input (parallel)
        ├── Validate best candidate on 3 test inputs
        ├── Accept/reject based on composite score
        ├── Every 5 accepts: full evaluation on ALL inputs
        └── Check convergence / phase transition
```

## Template Script Structure

### Constants

```python
# Paths
PROMPT_PATH = Path("path/to/your/prompt.md")
VERSIONS_DIR = Path("prompt_versions/")
LOG_PATH = Path("optimize/optimization_log.json")
PROGRAM_PATH = Path("optimize/program.md")

# Optimization parameters
CONVERGENCE_THRESHOLD = 0.002   # Stop when improvement < this
MAX_CONSECUTIVE_REJECTS = 25    # Stop after this many rejects in a row
PHASE1_TARGET = 0.95            # Quality target to trigger Phase 2
PHASE2_QUALITY_FLOOR = 0.93     # Minimum quality during cost optimization
MAX_CHANGE_RATIO = 1.0          # Max fraction of lines that can change (1.0 = disabled)

# Model identity — example pairing, not a required one. Pick per task/budget,
# and record both in every result so scores stay comparable across iterations.
EXTRACTION_MODEL_NAME = "your-extraction-model"   # runs the prompt under test
OPTIMIZER_MODEL_NAME = "your-optimizer-model"     # proposes edits (reasoning-capable)
```

### score_all — Batch Scoring

```python
def score_all(
    outputs: list[dict], rapid: bool = False
) -> tuple[float | None, dict[str, float | None], dict]:
    """Average composite and per-metric across successfully-scored outputs.

    Outputs with an _error key are an infrastructure outcome, not a quality-zero
    sample: they are excluded from the average, but their count is tracked
    explicitly in the returned coverage dict — never silently dropped and never
    folded into the average as a 0.0 (that would drag the score down as if it
    were a genuinely bad response).

    Returns (composite_or_None, per_metric_dict, coverage). composite is None —
    not 0.0 — when nothing could be scored (CANNOT-EVALUATE). Callers MUST check
    for None before using the composite in any accept/reject or ranking
    comparison; treat None as "did not run", never as a losing score.
    """
    failed = [o for o in outputs if o.get("_error")]
    successful = [o for o in outputs if not o.get("_error")]
    coverage = {
        "n_total": len(outputs),
        "n_scored": len(successful),
        "n_failed": len(failed),
        "coverage": (len(successful) / len(outputs)) if outputs else 0.0,
    }
    if not successful:
        return None, {n: None for n in METRIC_WEIGHTS}, coverage

    composites, per_metric_accum = [], {n: [] for n in METRIC_WEIGHTS}
    for output in successful:
        composite, metrics = score_output(output, rapid=rapid)
        composites.append(composite)
        for name, val in metrics.items():
            per_metric_accum[name].append(val)

    return (
        sum(composites) / len(composites),
        {n: sum(v) / len(v) for n, v in per_metric_accum.items()},
        coverage,
    )


def is_comparable(coverage: dict, *, require_full: bool = True) -> bool:
    """Execution-validity gate: is this composite eligible for ranking/acceptance?

    `composite is not None` only means "at least one case scored" — a candidate
    with 1 success and 99 failures still produces a non-None composite (mean
    over the single survivor) with coverage 0.01, and a bare `is None` check
    will let it outrank or replace a complete baseline. That is an infrastructure
    outcome wearing a quality score, not a real win.

    Default policy: require FULL coverage (every expected case produced a
    scoreable output) before a composite may be compared to another composite
    at all — screen ranking, quick-reject, validation accept/reject, full-eval
    baseline update, and final acceptance must all call this before comparing.
    An incomplete evaluation is EXCLUDED from comparison here, not averaged in
    (that hides the failure inside the mean) and not silently dropped (that
    would erase the record of what happened) — log it as its own
    `incomplete_non_comparable` outcome at the call site instead.

    The one sanctioned alternative to `require_full=True` is an explicitly
    justified common matched set: score both sides only over the case IDs both
    produced, document why, and record that policy in the log entry. Never make
    that substitution silently or as a default.
    """
    if coverage["n_total"] == 0:
        return False
    if require_full:
        return coverage["n_scored"] == coverage["n_total"]
    return True
```

### call_optimizer — Single Optimizer Call

```python
def call_optimizer(
    current_prompt: str,
    metrics: dict[str, float],
    composite: float,
    errors: list,
    history: list[dict],
    phase: str = "quality",
    iteration: int = 0,
    consecutive_rejects: int = 0,
    strategy_index: int | None = None,
) -> tuple[str, str]:
    """Call the optimizer agent to propose structured edits.

    Returns (new_prompt, summary).
    """
    system_prompt = PROGRAM_PATH.read_text()

    # Serialize top 8 error patterns
    error_dicts = [
        {"category": e.category, "description": e.description,
         "severity": round(e.severity, 3), "frequency": e.frequency,
         "examples": e.examples[:2]}
        for e in errors[:8]
    ]

    # Build context
    context = {
        "composite_score": round(composite, 4),
        "per_metric_scores": {k: round(v, 4) for k, v in metrics.items()},
        "metric_weights": METRIC_WEIGHTS,
        "error_patterns": error_dicts,
        "edit_history": history[-5:],  # Last 5 iterations only
        "phase": phase,
    }

    # Select diversity lens
    strat_idx = strategy_index if strategy_index is not None else iteration
    strategy = DIVERSITY_STRATEGIES[strat_idx % len(DIVERSITY_STRATEGIES)]
    diversity_block = f"\n## Optimization Lens: {strategy['lens']}\n\n{strategy['hint']}\n"

    # Escalation on consecutive rejects
    if consecutive_rejects >= 5:
        recent_failures = [
            h.get("optimizer_summary", "unknown")
            for h in history[-consecutive_rejects:]
            if h.get("outcome", "").startswith("rejected")
        ]
        diversity_block += (
            f"\n## IMPORTANT: {consecutive_rejects} consecutive rejects\n\n"
            "The following approaches ALL FAILED — do NOT repeat them:\n"
            + "\n".join(f"  - {s}" for s in recent_failures[-5:])
            + "\n\nTry a fundamentally different strategy.\n"
        )

    # Compose user message
    user_message = (
        "## Current Prompt\n\n```markdown\n" + current_prompt + "\n```\n\n"
        "## Current Scores and Error Analysis\n\n```json\n"
        + json.dumps(context, indent=2) + "\n```\n"
        + diversity_block + "\n"
        "Propose 1-3 targeted find/replace edits. Output ONLY the JSON object."
    )

    # Call the optimizer model. A reasoning-capable model tends to propose
    # better edits, but the specific model is a per-task/budget choice, not
    # a fixed requirement — adapt this to your LLM calling pattern.
    result = call_llm(
        system_prompt=system_prompt,
        user_message=user_message,
        model=OPTIMIZER_MODEL_NAME,
    )

    # Parse JSON response (with brace-slice fallback)
    edits_obj = parse_json_with_fallback(result)
    edits = edits_obj.get("edits", [])
    summary = edits_obj.get("summary", "")

    if not edits:
        raise ValueError("Optimizer returned no edits.")

    # Apply edits
    new_prompt = current_prompt
    applied = 0
    for edit in edits:
        find_str = edit.get("find", "")
        replace_str = edit.get("replace", "")
        if find_str and find_str in new_prompt:
            new_prompt = new_prompt.replace(find_str, replace_str, 1)
            applied += 1

    if applied == 0:
        raise ValueError("No edits could be applied (find strings not found).")

    return new_prompt, summary
```

### run_optimizer_tournament — Parallel Candidates

```python
def run_optimizer_tournament(
    current_prompt, metrics, composite, errors, history,
    phase="quality", iteration=0, consecutive_rejects=0, n_candidates=5,
) -> list[dict]:
    """Run N optimizer calls in parallel with different lenses.

    Returns list of {new_prompt, summary, lens, strategy_index}.
    """
    n_strats = len(DIVERSITY_STRATEGIES)
    if n_candidates <= n_strats:
        indices = random.sample(range(n_strats), n_candidates)
    else:
        indices = list(range(n_strats)) + random.choices(
            range(n_strats), k=n_candidates - n_strats
        )

    candidates = []
    with ThreadPoolExecutor(max_workers=n_candidates) as executor:
        futures = {
            executor.submit(
                call_optimizer, current_prompt, metrics, composite,
                errors, history, phase, iteration, consecutive_rejects, idx
            ): idx
            for idx in indices
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                new_prompt, summary = future.result()
                candidates.append({
                    "new_prompt": new_prompt,
                    "summary": summary,
                    "lens": DIVERSITY_STRATEGIES[idx % n_strats]["lens"],
                    "strategy_index": idx,
                })
            except Exception:
                pass  # Log and skip failed candidates

    return candidates
```

### Main Loop — The Core Algorithm

```python
def main():
    # --- Configuration ---
    # Parse args: --max-iterations, --sample-size, --candidates, --rapid, etc.

    # --- Load test inputs ---
    all_inputs = load_test_inputs()  # Your loading function

    # --- Load current prompt ---
    current_prompt = PROMPT_PATH.read_text()

    # --- Baseline evaluation (on ALL inputs) ---
    baseline_outputs = run_batch(current_prompt, all_inputs)
    baseline_composite, baseline_metrics, baseline_coverage = score_all(baseline_outputs)
    if baseline_composite is None:
        raise RuntimeError(
            f"Baseline is CANNOT-EVALUATE (0/{baseline_coverage['n_total']} cases scored) — "
            "fix the harness before optimizing. Do not treat this as a 0.0 baseline."
        )
    if not is_comparable(baseline_coverage):
        raise RuntimeError(
            f"Baseline only scored {baseline_coverage['n_scored']}/{baseline_coverage['n_total']} "
            "cases — a partial baseline is not a valid anchor for every later accept/reject "
            "comparison. Fix the harness (or adopt an explicitly justified matched-set policy "
            "and re-derive every downstream comparison against it) before optimizing."
        )
    save_version(
        current_prompt, "v1.0_baseline", baseline_metrics, baseline_composite,
        model=EXTRACTION_MODEL_NAME, prompt_revision="v1.0_baseline", coverage=baseline_coverage,
    )

    # --- Tracking ---
    best_prompt = current_prompt
    best_composite = baseline_composite
    best_metrics = baseline_metrics
    last_outputs = baseline_outputs
    history = []
    accepted_count = 0
    consecutive_rejects = 0
    accepted_since_full_eval = 0

    # High-water mark (captures best prompt even across rejected iterations)
    hwm_composite = best_composite
    hwm_prompt = best_prompt
    hwm_iteration = 0

    phase = "quality"

    # --- Optimization loop ---
    for iteration in range(1, max_iterations + 1):

        # (a) Sample test inputs for this iteration
        random.shuffle(all_inputs)
        screen_input = all_inputs[0]
        validation_inputs = all_inputs[1:1 + sample_size]

        # (b) Analyze errors from last outputs
        errors = analyze_errors(last_outputs)
        if not errors:
            print("Converged — no error patterns detected.")
            break

        # (c) Run optimizer tournament
        candidates = run_optimizer_tournament(
            best_prompt, best_metrics, best_composite, errors,
            history, phase, iteration, consecutive_rejects, n_candidates,
        )
        if not candidates:
            consecutive_rejects += 1
            continue

        # (d) Screen: test each candidate on 1 input (parallel)
        screen_results = []
        n_incomplete_screens = 0
        with ThreadPoolExecutor(max_workers=len(candidates)) as executor:
            futures = {
                executor.submit(run_batch, c["new_prompt"], [screen_input]): i
                for i, c in enumerate(candidates)
            }
            for future in as_completed(futures):
                idx = futures[future]
                outputs = future.result()
                s_composite, s_metrics, s_coverage = score_all(outputs)
                if s_composite is None:
                    continue  # CANNOT-EVALUATE candidate: skip, don't score as a loss
                if not is_comparable(s_coverage):
                    # Non-None but incomplete (e.g. 1 success among many _errors):
                    # excluded from ranking, not averaged in and not silently
                    # dropped — tracked via n_incomplete_screens for the log.
                    n_incomplete_screens += 1
                    continue
                screen_results.append({
                    "idx": idx, "composite": s_composite,
                    "metrics": s_metrics, "coverage": s_coverage, "outputs": outputs,
                })

        # (e) Pick best screened candidate
        if not screen_results:
            # All candidates CANNOT-EVALUATE or incomplete-non-comparable this
            # round — an infra hiccup, not a quality rejection. Still counts
            # toward the reject-escalation counter so a persistently broken
            # harness surfaces rather than looping forever.
            consecutive_rejects += 1
            if n_incomplete_screens:
                history.append({
                    "iteration": iteration, "outcome": "incomplete_non_comparable",
                    "stage": "screen", "n_incomplete_screens": n_incomplete_screens,
                    "phase": phase,
                })
            continue
        screen_results.sort(key=lambda r: r["composite"], reverse=True)
        best_screen = screen_results[0]
        best_cand = candidates[best_screen["idx"]]

        # Quick reject: if screen score <= baseline, skip validation
        if best_screen["composite"] <= best_composite:
            consecutive_rejects += 1
            # Update HWM if screen was still the best we've seen
            if best_screen["composite"] > hwm_composite:
                hwm_composite = best_screen["composite"]
                hwm_prompt = best_cand["new_prompt"]
                hwm_iteration = iteration
            history.append({
                "iteration": iteration, "outcome": "rejected_no_improvement",
                "old_composite": best_composite,
                "new_composite": best_screen["composite"],
                "optimizer_summary": best_cand["summary"],
                "lens": best_cand["lens"], "phase": phase,
            })
            continue

        # (f) Validate: test winner on additional inputs
        val_outputs = run_batch(best_cand["new_prompt"], validation_inputs)
        val_composite, val_metrics, val_coverage = score_all(val_outputs)
        if val_composite is None:
            consecutive_rejects += 1  # CANNOT-EVALUATE: infra failure, not a quality rejection
            history.append({
                "iteration": iteration, "outcome": "cannot_evaluate",
                "coverage": val_coverage, "optimizer_summary": best_cand["summary"],
                "lens": best_cand["lens"], "phase": phase,
            })
            continue
        if not is_comparable(val_coverage):
            # Non-None but incomplete validation (e.g. 1/3 cases scored): this is
            # an infrastructure outcome, not a quality result — it must never
            # reach the accept/reject comparison against best_composite below.
            consecutive_rejects += 1
            history.append({
                "iteration": iteration, "outcome": "incomplete_non_comparable",
                "stage": "validation", "coverage": val_coverage,
                "optimizer_summary": best_cand["summary"],
                "lens": best_cand["lens"], "phase": phase,
            })
            continue

        # Update HWM
        if val_composite > hwm_composite:
            hwm_composite = val_composite
            hwm_prompt = best_cand["new_prompt"]
            hwm_iteration = iteration

        # (g) Accept/reject (phase-aware)
        accepted = False
        if phase == "quality":
            accepted = val_composite > best_composite
        else:  # cost phase
            if val_composite < PHASE2_QUALITY_FLOOR:
                accepted = False  # Quality dropped below floor
            elif new_cost < best_cost:
                accepted = True   # Cost improved
            elif val_composite > best_composite:
                accepted = True   # Quality improved, cost neutral

        if not accepted:
            consecutive_rejects += 1
            history.append({
                "iteration": iteration,
                "outcome": "rejected_no_improvement",
                "old_composite": best_composite,
                "new_composite": val_composite,
                "optimizer_summary": best_cand["summary"],
                "lens": best_cand["lens"], "phase": phase,
            })
            if consecutive_rejects >= MAX_CONSECUTIVE_REJECTS:
                break
            continue

        # (h) Accept!
        # Deterministic proof of this fix: references/fixtures/delta_baseline_fixture.py
        # (run: python3 references/fixtures/delta_baseline_fixture.py) — reproduces the
        # original bug's zero-delta shape for contrast, then asserts prev_composite/
        # accept_delta below are computed against the TRUE prior baseline.
        # BUG THIS FIXES: capture the prior baseline BEFORE overwriting
        # best_composite. Computing `delta = val_composite - best_composite`
        # (or `old_composite`) AFTER `best_composite = val_composite` always
        # yields 0 — every ordinary accepted iteration then looks like zero
        # improvement, and the convergence check below stops the loop after the
        # very first accept. `prev_composite` is the value that must be logged
        # as `old_composite` and used for `delta`; only after both are computed
        # does `best_composite` get updated to the new value.
        prev_composite = best_composite
        best_prompt = best_cand["new_prompt"]
        best_composite = val_composite
        best_metrics = val_metrics
        last_outputs = val_outputs
        accepted_count += 1
        accepted_since_full_eval += 1
        consecutive_rejects = 0

        save_version(
            best_prompt, f"v{iteration}", val_metrics, val_composite,
            model=EXTRACTION_MODEL_NAME, prompt_revision=f"v{iteration}", coverage=val_coverage,
        )

        accept_delta = val_composite - prev_composite  # against the PRIOR baseline
        history.append({
            "iteration": iteration, "outcome": "accepted",
            "old_composite": prev_composite,
            "new_composite": val_composite,
            "delta": accept_delta,
            "coverage": val_coverage,
            "optimizer_summary": best_cand["summary"],
            "lens": best_cand["lens"], "phase": phase,
            "execution_model": EXTRACTION_MODEL_NAME,
            "optimizer_model": OPTIMIZER_MODEL_NAME,
            "prompt_revision": f"v{iteration}",
        })

        # (i) Full evaluation checkpoint every 5 accepts
        if accepted_since_full_eval >= 5:
            # This re-scores the same tuning corpus the loop samples from all
            # along — a development-monitoring check for overfitting to the
            # per-iteration *sample*, NOT an independent/held-out validation pass.
            full_outputs = run_batch(best_prompt, all_inputs)
            full_composite, full_metrics, full_coverage = score_all(full_outputs)
            if full_composite is not None and is_comparable(full_coverage):
                best_composite = full_composite  # Full eval is authoritative, when it ran complete
                best_metrics = full_metrics
            # else: CANNOT-EVALUATE or incomplete-non-comparable checkpoint — keep
            # the prior best_composite rather than overwriting a real score with
            # a failed or partial one.
            accepted_since_full_eval = 0

            # Phase transition check
            if (
                phase == "quality" and full_composite is not None
                and is_comparable(full_coverage) and full_composite > PHASE1_TARGET
            ):
                phase = "cost"
                print(f"PHASE TRANSITION: Quality {PHASE1_TARGET} reached!")

        # (j) Convergence check
        # Reuse accept_delta from step (h) — do NOT recompute
        # `val_composite - best_composite` here. best_composite may already
        # equal val_composite (from the accept above) or have been further
        # overwritten by the full-eval checkpoint in step (i); either way that
        # recomputation reintroduces the exact zero-delta bug this template
        # fixes (see the comment at step (h)). accept_delta was computed against
        # the correct prior baseline before any of those updates happened.
        if accept_delta < CONVERGENCE_THRESHOLD and phase == "quality":
            print(f"Converged: delta {accept_delta:.4f} < {CONVERGENCE_THRESHOLD}")
            break

    # --- Final evaluation with HWM prompt ---
    # NOTE: still the tuning corpus. If a held-out set was reserved (see SKILL.md
    # Test Corpus > Held-out vs. tuning corpus), score it separately, once, for
    # the real validation claim — this final eval is not a substitute for that.
    final_outputs = run_batch(hwm_prompt, all_inputs)
    final_composite, final_metrics, final_coverage = score_all(final_outputs)

    if final_composite is None:
        print(
            f"Final evaluation is CANNOT-EVALUATE (0/{final_coverage['n_total']} scored) — "
            "report this as an infrastructure failure, not as a failed or improved prompt. "
            "Fix the harness and re-run rather than accepting a None/0.0 result."
        )
    elif not is_comparable(final_coverage):
        print(
            f"Final evaluation only scored {final_coverage['n_scored']}/{final_coverage['n_total']} "
            "cases — INCOMPLETE, not comparable to the complete baseline. Do not accept/reject "
            "based on this number; fix the harness and re-run the final evaluation."
        )
    elif final_composite > baseline_composite:
        PROMPT_PATH.write_text(hwm_prompt)
        print(f"Updated prompt. Improvement: {final_composite - baseline_composite:+.4f}")

    # Save optimization log (append to existing) — each entry carries coverage
    # plus the model/prompt-revision identifiers recorded above, so results stay
    # comparable across iterations.
    save_log(history, baseline_composite, final_composite, final_coverage)
```

## Key Design Decisions

Every number in this section is a MEASURED example from a specific past run, not
a general property of the technique. Provenance: `references/scoring-design.md`
history / the run's own append-only log (`old_composite`/`delta`/`coverage` per
iteration, per the fixed accept logic above) is the only source that can support
a number like these — quote a figure here only if you can point to that log.
Where no such log exists for the task at hand, don't restate the figure below;
describe the mechanism only.

### Why Tournament Selection?
Mechanism: N parallel candidates, each with a different diversity lens, let the
search explore several directions per iteration instead of one. In one observed
task-specific run, single-candidate hill climbing accepted ~15-20% of
iterations, and N=5 tournament selection raised that to ~40-50% for that same
task — reported here as a specific measured example, not an expected acceptance
rate for a new prompt or task. The added cost is Nx optimizer calls (cheap
relative to extraction — pick a reasoning-capable model that fits the budget)
but only 1x validation extractions (expensive — the actual prompt execution),
and even that added cost is only worth paying once real evaluations are
gated for completeness (see the `is_comparable` gate above) — a fast rejection
rate improvement built on incomplete evaluations is not a real improvement.

### Why Random Sampling Per Iteration?
Mechanism: running the full test corpus every iteration is expensive; sampling
a subset (e.g. 1 screen + N validation inputs) reduces per-iteration cost while
still producing a comparison signal, provided the sampled evaluation is itself
complete (see `is_comparable`) — a cheap sample that silently drops failed
cases is not "statistical signal," it's an uncontrolled coverage gap. In one
observed task-specific run this reduced per-iteration cost by roughly 80% versus
full-corpus scoring; treat that figure as an example from that run, not a
universal saving. Full evaluations every N accepts are development monitoring
against the same tuning corpus the loop already sampled from — they may surface
overfitting to the per-iteration *sample*, but they are not independent
validation (see "Held-out vs. tuning corpus" in SKILL.md).

### Why High-Water Mark?
The best prompt may appear in an iteration where screening passes but validation fails due to sampling noise. HWM captures this — the final evaluation uses the HWM prompt, not just the last accepted one. This is a structural argument (a rejected iteration can still hold the best-seen result), not a claim tied to any percentage.

### Why Separate Screen and Validation?
Mechanism: screening on a small input count is a cheap filter that eliminates
clearly bad candidates before committing to a more expensive multi-input
validation pass. In one observed task-specific run this cut per-iteration
validation cost by roughly 60%; that number is specific to that run's candidate
count and sample size, not a guaranteed saving for a different configuration.

### Why Append-Only Log?
Every iteration's outcome, metrics, coverage, wall-clock time, optimizer summary, and model/prompt-revision identifiers are logged. This enables post-hoc analysis: which lenses work best, where does the optimizer stagnate, how much of the run was unmeasured infrastructure failure vs. real rejections, what's the throughput curve. Never delete or overwrite log entries.

## CLI Arguments Template

```python
parser = argparse.ArgumentParser()
parser.add_argument("--max-iterations", type=int, default=200)
parser.add_argument("--sample-size", type=int, default=3,
                    help="Validation inputs per iteration")
parser.add_argument("--candidates", type=int, default=5,
                    help="Parallel optimizer candidates per tournament")
parser.add_argument("--rapid", action="store_true",
                    help="Aggressive input truncation for faster iterations")
parser.add_argument("--version-prefix", type=str, default="v2")
parser.add_argument("--skip-baseline", type=str, default=None,
                    help="Path to existing baseline scores JSON")
```
