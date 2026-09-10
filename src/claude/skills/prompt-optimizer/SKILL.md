---
name: prompt-optimizer
description: Systematically optimize any LLM prompt through autonomous iterative improvement. Use when the user wants to improve prompt quality, set up an optimization loop, tune a prompt for better outputs, benchmark prompt variants, or says "optimize my prompt", "improve this prompt", "my prompt isn't working well", "tune this prompt", "autoresearch loop", "prompt engineering". Also use when the user has a prompt producing inconsistent or low-quality outputs and wants a disciplined, metric-driven approach rather than ad-hoc tweaking.
---

# Prompt Optimizer

A methodology for autonomous, metric-driven prompt optimization using iterative refinement. Adapted from the Karpathy autoresearch pattern. Tournament selection with diversity lenses is one effective technique among several — not a universal guarantee. Any quoted improvement percentage reflects specific past runs, not an expected outcome for a new prompt or task; treat the templates below as a proven starting configuration to scale to the task at hand, not a fixed protocol.

## When to Use This

- A prompt produces inconsistent or low-quality outputs
- You need measurable improvement, not vibes-based tweaking
- The prompt is complex enough that manual iteration is slow
- You want to optimize for cost while maintaining quality
- You have (or can create) test cases to evaluate against

## Core Architecture: The Three-File Pattern

Every prompt optimization project uses three files:

| File | Role | Who writes it |
|------|------|---------------|
| `program.md` | Optimizer agent instructions — tells the LLM how to propose improvements | You (with this skill's template) |
| The artifact | The prompt being optimized | Exists already (user's prompt) |
| `optimize.py` | Runner script — orchestrates the loop, scoring, and version control | You (with this skill's template) |

**Supporting modules** (created alongside the runner):
- `scoring.py` — Composite scoring function (pure, no I/O)
- `error_analyzer.py` — Systematic error pattern detection (pure, no I/O)
- `experiment_runner.py` — I/O layer that executes the prompt on test cases

## Workflow

### Phase 0: Setup Interview

Before writing any code, establish these with the user:

1. **What is the prompt doing?** (extraction, generation, classification, summarization, etc.)
2. **What does "good" look like?** This determines your metrics. Push for specifics:
   - Not "accurate" → "extracts all entities with correct types"
   - Not "well-written" → "uses active voice, <200 words, includes specific examples"
3. **What test cases exist?** As a starting example, 5-25 representative inputs is a workable range (more improves statistical power; even 3 can bootstrap the process) — scale this to the task rather than treating it as a fixed floor or ceiling. If a genuine held-out validation claim matters for this task, reserve a subset up front that the tuning loop (screen/validate/full-eval) never samples from — see "Held-out vs. tuning corpus" under Test Corpus below.
4. **What model runs the prompt?** (determines cost estimation and context limits)
5. **Is there a production post-processing step?** If so, apply it before scoring — don't optimize for issues already handled downstream.
6. **What's the throughput budget?** Each iteration runs N extraction calls; plan and report in calls/iteration, iterations/hour, and wall-clock time — not a dollar estimate. Tournament selection with 5 candidates on 4 test cases (~20 LLM calls/iteration) is one example configuration; scale N and sample size to the task's actual scope and time budget.

### Phase 1: Define Metrics

This is the most important step. Bad metrics waste every subsequent iteration.

Read `references/scoring-design.md` for the full guide. Key principles:

**Score what matters for the downstream use case, not format compliance.**
If a post-processing step fixes formatting issues, don't waste optimization budget on format metrics. Weight claim quality > evidence quality > connectivity > format validity.

**Every metric must be:**
- Automatically computable, with no *live* human judgment in the loop — but the scoring function MAY take trusted inputs, reference/gold material, and previously-retained evaluator (e.g., LLM-judge) results as arguments. Scoring the final output in isolation from the context it was produced in and validated against throws away legitimate signal; use whatever trusted material is already available.
- Bounded [0, 1] for composability, and reserved for genuine quality judgments — not for infrastructure state (see "Validity gates vs. quality objectives" below)
- Weighted by importance to the use case

**Metric design template:**
```python
METRIC_WEIGHTS: dict[str, float] = {
    # --- Primary quality (50-60% of composite) ---
    "metric_a": 0.15,  # Most important aspect
    "metric_b": 0.12,  # Second most important
    # --- Secondary quality (20-30%) ---
    "metric_c": 0.08,
    # --- Format/structure (10-20%) ---
    "metric_d": 0.05,
}
# Weights MUST sum to 1.0
```

**Common metric patterns:**
- **Density**: Output count in target range (penalize both too few and too many)
- **Coverage**: Fraction of items with required field populated
- **Diversity**: Shannon entropy of categorical distribution (normalized to [0,1])
- **Validity**: Fraction matching controlled vocabulary
- **Completeness**: Average fraction of required sub-fields populated per item

**Validity gates are not quality metrics.** Before scoring quality, check whether the output is even well-formed and in-scope (parses as expected, matches the required schema, isn't empty/truncated/an API error). A malformed or invalid output is a gate FAILURE, not a low point on the same [0,1] quality scale as a valid-but-mediocre output — conflating the two lets a crash or schema violation masquerade as "just a bad score," and makes a broken output rank the same as a well-formed weak one. Track gate failures (e.g. `invalid` / `_error`) as their own category, separate from the composite quality score.

**Missing or failed evaluations are an infrastructure outcome, never a score.** If a case times out, errors, hits a quota, or otherwise never produces a scoreable output, that is `unmeasured`/`failed-to-run` — a distinct, explicitly tracked count (`n_failed`, `coverage`). It must never be folded into the quality average as a 0.0 (which drags the score down as if it were a genuinely bad response) and never silently dropped from the denominator (which inflates the average by counting only successes). Report `coverage = n_scored / n_total` alongside every composite; a composite at partial coverage is a different claim than one at full coverage and should be labeled as such, not presented as directly comparable.

### Phase 2: Build the Scoring Function

Create `scoring.py` as a pure function — no I/O, no side effects:

```python
def score_output(data: dict) -> tuple[float, dict[str, float]]:
    """Score a single prompt output against quality metrics.

    Returns:
        (composite_score, per_metric_dict) — both values in [0, 1].
    """
```

Key design rules:
- Return both composite AND per-metric breakdown (the optimizer needs per-metric to know what to fix)
- Support a `rapid` mode that excludes metrics requiring expensive computation
- Use target ranges with linear ramp-up and penalty for overshooting (not just "more is better")

### Phase 3: Build the Error Analyzer

Create `error_analyzer.py` — detects systematic patterns across a batch of outputs:

```python
@dataclass
class ErrorPattern:
    category: str       # e.g., "missing_field", "invalid_value", "low_density"
    description: str    # Human-readable description with statistics
    severity: float     # 0-1, how bad this pattern is
    frequency: int      # How many times it occurred
    examples: list[str] # 2-3 concrete examples from the outputs

def analyze_errors(outputs: list[dict]) -> list[ErrorPattern]:
    """Detect systematic errors, sorted by severity × frequency descending."""
```

The error analyzer bridges the gap between metrics (which say "what's wrong") and the optimizer (which needs to know "why it's wrong" to propose fixes). Include:
- Vocabulary violations (using invalid values)
- Missing required fields
- Distribution skew (one category dominating)
- Structural problems (missing linkages, incomplete records)
- Domain-specific anti-patterns

### Phase 4: Write the Optimizer Agent Prompt (program.md)

Read `references/program-template.md` for the full template. The optimizer agent receives:
1. The current prompt text
2. Composite score + per-metric breakdown
3. Ranked error patterns with examples
4. History of past edit attempts and outcomes

And outputs: **1-3 surgical find/replace edits** as JSON.

Critical design decisions baked into the template:
- **Structured edits, not regeneration.** LLMs cannot faithfully reproduce large prompts. Find/replace edits are verifiable and bounded.
- **Priority ordering.** The optimizer attacks the highest-impact metrics first.
- **History awareness.** Failed approaches are explicitly listed so the optimizer doesn't repeat them.
- **Diversity lenses.** Each iteration gets a different strategic lens to prevent stagnation. See `references/diversity-lenses.md`.

### Phase 5: Build the Experiment Runner

Create `experiment_runner.py` — thin I/O wrapper:

```python
def run_single(prompt_text: str, input_text: str, timeout: int = 600) -> dict:
    """Execute the prompt on one input and return parsed output."""

def run_batch(prompt_text: str, inputs: list[dict], max_workers: int = 5) -> list[dict]:
    """Run on all inputs in parallel via ThreadPoolExecutor."""
```

Key patterns:
- **Parallel execution** with `ThreadPoolExecutor` — each call is independent I/O
- **Graceful failure handling** — failed calls return empty results with `_error` key, never crash the batch
- **Token tracking** — track input/output tokens per call (this is the quantity Phase 2 optimizes against); report wall-clock latency and throughput to the user, not a dollar figure
- **JSON parsing with fallback** — direct parse → brace slice → truncation repair
- **Production coercion** — apply any post-processing pipeline before returning results
- **Timeout per call** — prevent hung calls from blocking the batch

### Phase 6: Wire the Optimization Loop

Read `references/runner-template.md` for the full template. The step counts below (5 candidates, 1 screen input, 3 validation inputs, every 5 accepts) are a proven starting configuration, not a fixed requirement — scale them to the test corpus size and time budget. The main loop:

```
for each iteration:
    1. Sample test cases (random subset per iteration to reduce cost)
    2. Analyze errors from last round's outputs
    3. Run optimizer tournament (N candidates, different lenses, parallel)
    4. Screen: test each candidate on 1 input (parallel)
    5. Pick best screened candidate
    6. Validate: test winner on 3 additional inputs
    7. Accept/reject based on composite improvement
    8. If accepted: update best prompt, reset reject counter
    9. Every 5 accepts: full evaluation on ALL test cases
    10. Check convergence / phase transition
```

**Tournament selection** is the key innovation over naive hill climbing:
- Launch N=5 optimizer candidates in parallel, each with a different diversity lens
- Screen all 5 on 1 test case (cheap — 5 LLM calls)
- Only validate the winner on 3 more test cases (expensive — 3 calls)
- Per-iteration cost: ~5 optimizer + 5 screen + 3 validation = 13 calls
- Every screen/validation result must pass an execution-validity gate (full coverage, or an explicitly justified matched set — see `references/runner-template.md`'s `is_comparable`) before it can rank or be accepted; an incomplete evaluation is excluded from comparison, not averaged in and not silently dropped
- In one observed task-specific run, this reduced the single-candidate rejection rate substantially (measured example, not a guarantee — see "Key Design Decisions" in `references/runner-template.md`)

**Two-phase optimization:**
- **Phase 1 (Quality):** Maximize composite score until target reached (e.g., > 0.95)
- **Phase 2 (Cost):** Minimize per-call cost while maintaining quality floor (e.g., > 0.93)

### Phase 7: Run and Monitor

```bash
python optimize.py \
    --max-iterations 200 \
    --sample-size 3 \
    --candidates 5 \
    --version-prefix v2
```

Monitor for:
- **Consecutive rejects > 10:** The optimizer is stuck. The loop auto-escalates diversity pressure, but if rejects hit 25, it stops.
- **Phase transition:** Quality target reached → switches to cost reduction automatically.
- **High-water mark:** The globally best prompt is tracked even across rejected iterations. The final output uses the HWM prompt, not just the last accepted one.
- **Full evaluation checkpoints:** Every 5 accepted iterations, runs on ALL test cases (not just the sample) — development monitoring that may help surface overfitting to the per-iteration sample; it is not independent validation, since the tuning loop has already seen and optimized against this same corpus (see "Held-out vs. tuning corpus" below).

### Phase 8: Analyze Results

After the loop completes:
1. Compare baseline vs final composite score
2. Review per-metric improvements (which metrics improved most?)
3. Check the optimization log for patterns (which lenses worked best?)
4. Run a final full evaluation on the HWM prompt (this remains a check on the tuning corpus; if a held-out set was reserved per Phase 0, score it separately — once — for the real validation claim)
5. If quality target not reached, consider: expanding test corpus, redesigning metrics, or restructuring the prompt architecture

## Best Practices (Hard-Won Lessons)

### Metric Design
- **Metric choice dominates.** Switching from word-overlap to embedding similarity changed scores 5x without changing the system. Invest time here.
- **Weight by downstream impact.** If 55% of value comes from claim quality, weight it 55%.
- **Don't score what coercion handles.** If a post-processing pipeline fixes invalid values, don't penalize the prompt for producing them.

### Prompt Editing
- **Structured edits > full regeneration.** LLMs hallucinate when asked to reproduce large prompts. Find/replace is verifiable.
- **1-3 edits per iteration.** More than that makes it impossible to attribute improvement to specific changes.
- **Add, don't remove.** Prefer inserting clarifications after existing text. Only modify text that's actively causing confusion.
- **Examples beat rules.** One concrete input→output example is worth three abstract instructions.

### Optimization Loop
- **Tournament selection is an effective pattern, not a mandate.** In observed runs, single-candidate hill climbing had a high (>80%) rejection rate, and N=5 candidates with different lenses reduced it substantially — treat N=5 as a starting example to size to the budget; a simpler single-candidate loop is legitimate when the task's scope doesn't warrant parallel tournament calls.
- **Random sampling per iteration.** Don't run on ALL test cases every iteration — sample 3-4 to reduce cost, but only once the sampled evaluation itself passes the execution-validity gate (full coverage on the sample, or a documented matched set). Full evaluation every 5 accepts is development monitoring that may help surface overfitting to the sample; it is not independent validation.
- **High-water mark tracking.** The best prompt may come from a rejected iteration (screen passed, validation failed due to sampling noise). Track it.
- **Consecutive reject escalation.** After 5 rejects, feed failed approaches back to the optimizer with "DO NOT repeat these." After 25, stop — the prompt needs structural changes, not more tweaking.

### Test Corpus
- **Filter to representative inputs.** Exclude edge cases that produce unrepresentative outputs (e.g., review papers when optimizing for primary research extraction).
- **~5 test cases is a starting-point floor, not a hard minimum.** Below that, per-iteration scores tend to get noisy — but the right number depends on observed variance for the task.
- **Include hard cases.** The optimizer should be tested against difficult inputs, not just easy ones.
- **Held-out vs. tuning corpus.** The tournament's screen/validate steps and the "full evaluation every 5 accepts" checkpoint all draw from the same tuning corpus (`all_inputs`). Repeatedly running the full corpus during tuning is development monitoring — useful for catching overfitting to the per-iteration *sample* — but it is not independent validation, because by then the loop has seen and optimized against every case in it. If a real held-out validation claim is needed, reserve a separate subset up front that never enters screen, validate, or full-eval during tuning, and score it once, after optimization is finished.

### Cost Management
- **Report wall-clock time and throughput, not dollar estimates.** Track iterations/hour and cases/hour so you (and the user) know how long a run will take. Per-call token count is a legitimate quality objective for Phase 2 (see above); a dollar total is a fragile guess that goes stale as pricing changes, so don't present it as a measurement.
- **A cheap-model-for-extraction / strong-model-for-optimization split is one workable example**, not a required pairing — e.g. the prompt under test on a fast/cheap model, the optimizer that proposes edits on a stronger reasoning model. Match the actual models to the task's budget and quality needs.
- **Rapid mode for early iterations.** Truncate inputs aggressively during exploration; switch to full inputs for final validation.
- **Batch API for production, where available.** Optimizer iterations use real-time API; production deployments may use a batch API at a discount — verify current pricing/availability rather than assuming a fixed rate.

### Infrastructure
- **Version every iteration, tagged with model identity.** Save prompt + scores as `v{N}.md` + `v{N}_scores.json`, and record which model executed the prompt and which model (if any) generated the edit for that version. A composite score is only comparable across iterations when both the prompt revision and the producing model are known — you will want to diff versions later.
- **Append-only optimization log.** JSON log of every iteration's outcome, metrics, coverage (n_scored/n_failed), wall-clock time, optimizer summary, and the model identifiers (execution model + optimizer model) and prompt revision id involved. The model/revision fields are required to keep results comparable if models are ever swapped mid-run. Essential for post-hoc analysis.
- **Unbuffered stdout.** Set `PYTHONUNBUFFERED=1` — overnight runs need real-time log visibility.
- **JSON output format from CLI.** Text output gets truncated on large responses; JSON output preserves full content.

## Scaffolding Generation

When the user is ready to start, generate the full file structure:

```
project/
├── optimize/
│   ├── __init__.py
│   ├── scoring.py          # Composite scoring (from Phase 2)
│   ├── error_analyzer.py   # Error pattern detection (from Phase 3)
│   ├── experiment_runner.py # I/O layer (from Phase 5)
│   └── program.md          # Optimizer agent prompt (from Phase 4)
├── optimize.py              # Main CLI entrypoint (from Phase 6)
├── prompt_versions/         # Auto-created: version snapshots
├── test_inputs/             # User's test cases
└── tests/
    └── test_optimizer/
        ├── test_scoring.py
        └── test_error_analyzer.py
```

Generate scoring.py and error_analyzer.py with domain-specific metrics based on the setup interview. Generate program.md from the template in `references/program-template.md`. Generate optimize.py from the template in `references/runner-template.md`.

## Reference Files

- `references/scoring-design.md` — Deep guide to metric design with examples for common prompt types
- `references/program-template.md` — Full template for the optimizer agent prompt
- `references/diversity-lenses.md` — Catalog of 10 diversity strategies with usage guidance
- `references/runner-template.md` — Full template for the optimization loop script
