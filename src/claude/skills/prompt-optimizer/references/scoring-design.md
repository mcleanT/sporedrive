# Scoring Function Design Guide

How to design a composite scoring function that drives meaningful prompt optimization.

## Table of Contents
1. [Core Principles](#core-principles)
2. [Metric Types](#metric-types)
3. [Weight Assignment](#weight-assignment)
4. [Common Patterns by Prompt Type](#common-patterns-by-prompt-type)
5. [Implementation Patterns](#implementation-patterns)
6. [Anti-Patterns](#anti-patterns)

---

## Core Principles

### Score what matters downstream
Your scoring function should measure what the *consumer* of the output cares about, not what's easy to measure. If the output feeds into a knowledge graph, score graph utility (connectivity, evidence linkage). If it feeds into a report, score information completeness and accuracy.

### Every metric must be automatically computable
No human-in-the-loop scoring. If you can't write a function to compute it, it's not a metric — it's a vibe. Vibes are valuable for manual review but useless for autonomous optimization.

### Bounded [0, 1] for composability
Every metric returns a float in [0, 1]. The composite score is a weighted sum of individual metrics. Weights must sum to 1.0.

### Return both composite and per-metric breakdown
The optimizer needs per-metric scores to know which aspect of the prompt to improve. A single composite number isn't enough.

### Support rapid mode
Some metrics are expensive (they require parsing References sections, computing embeddings, etc.). Support a `rapid` mode that excludes expensive metrics for fast iteration, with full metrics reserved for checkpoint evaluations.

---

## Metric Types

### Density Metrics
Measure whether the output produces the right *amount* of content.

```python
def _metric_density(n: int, target_min: int = 25, target_max: int = 50) -> float:
    """Score output count against a target range.

    Below target_min: linear ramp from 0 to 1
    Within range: 1.0
    Above target_max: linear penalty back to 0
    """
    if n == 0:
        return 0.0
    if n < target_min:
        return n / target_min
    if n <= target_max:
        return 1.0
    return max(0.0, 1.0 - (n - target_max) / target_max)
```

**When to use:** Any prompt that should produce N items (claims, bullet points, entities, code blocks). Always specify both min and max — "more is better" is never true.

### Coverage Metrics
Measure the fraction of items with a required field populated.

```python
def _metric_field_coverage(items: list[dict], field: str) -> float:
    """Fraction of items where field is non-empty."""
    return sum(1 for item in items if item.get(field)) / len(items)
```

**When to use:** Any structured output where fields should be populated. Good for catching prompts that produce skeleton structures without filling in details.

### Validity Metrics
Measure compliance with controlled vocabularies.

```python
VALID_VALUES: frozenset[str] = frozenset({"option_a", "option_b", "option_c"})

def _metric_validity(items: list[dict], field: str) -> float:
    """Fraction of items using valid vocabulary values."""
    return sum(1 for item in items if item.get(field) in VALID_VALUES) / len(items)
```

**When to use:** Any output with enum-like fields. Note: if a post-processing coercion step fixes invalid values, weight this metric LOW — don't optimize for what's already handled.

### Diversity Metrics
Measure whether the output uses the full range of available categories.

```python
import math

def _shannon_entropy_normalized(counts: dict[str, int]) -> float:
    """Normalized Shannon entropy in [0, 1].

    0.0 = all items use one category
    1.0 = perfectly uniform distribution across all categories
    """
    total = sum(counts.values())
    if total == 0 or len(counts) <= 1:
        return 0.0
    entropy = 0.0
    for count in counts.values():
        if count > 0:
            p = count / total
            entropy -= p * math.log2(p)
    max_entropy = math.log2(len(counts))
    return entropy / max_entropy if max_entropy > 0 else 0.0
```

**When to use:** When a prompt should use varied predicates/categories/types but tends to overuse one. Catches the "induces everything" problem.

### Completeness Metrics
Measure how thoroughly individual items are populated.

```python
def _metric_completeness(items: list[dict], required_fields: list[str]) -> float:
    """Average fraction of required fields populated per item."""
    scores = []
    for item in items:
        populated = sum(1 for f in required_fields if item.get(f))
        scores.append(populated / len(required_fields))
    return sum(scores) / len(scores)
```

**When to use:** Items that should have multiple sub-fields (e.g., evidence units with result_summary, readout, key_figure).

### Linkage Metrics
Measure cross-referencing between output components.

```python
def _metric_linkage(items: list[dict], link_field: str) -> float:
    """Fraction of items with non-empty cross-references."""
    return sum(1 for item in items if item.get(link_field)) / len(items)

def _metric_link_depth(items: list[dict], link_field: str, target: int = 2) -> float:
    """Average link count per item, scored against a target.

    0 links → penalty, 1 link → baseline (0.0), target+ → 1.0
    """
    total = 0.0
    for item in items:
        n_links = len(item.get(link_field) or [])
        total += min(1.0, max(-0.5, (n_links - 1) / (target + 1)))
    return max(0.0, total / len(items))
```

**When to use:** Outputs with multiple components that should reference each other (claims→evidence, entities→sources).

### Consistency Metrics
Measure whether the output uses consistent naming/formatting.

```python
from collections import defaultdict

def _metric_name_consistency(items: list[dict], name_fields: list[str]) -> float:
    """Detect entity name fragmentation (same entity, different spellings)."""
    name_groups: dict[str, set[str]] = defaultdict(set)
    for item in items:
        for field in name_fields:
            name = (item.get(field) or {}).get("name", "")
            if name:
                name_groups[name.lower()].add(name)
    if not name_groups:
        return 1.0
    fragmented = sum(1 for variants in name_groups.values() if len(variants) > 1)
    return 1.0 - fragmented / len(name_groups)
```

**When to use:** Outputs that reference the same entities multiple times. Catches "BMP4" vs "Bmp4" vs "BMP-4" problems.

### Quantitative Context Metrics
Measure whether outputs include actual data, not just assertions.

```python
import re
_HAS_NUMBER = re.compile(r"\d")

def _metric_quantitative(items: list[dict], field: str) -> float:
    """Fraction of items where a field contains actual numbers (not just text)."""
    def has_number(item):
        val = item.get(field)
        if not val:
            return False
        return any(_HAS_NUMBER.search(str(v)) for v in (val.values() if isinstance(val, dict) else [val]) if v)
    return sum(1 for item in items if has_number(item)) / len(items)
```

**When to use:** Outputs that should cite specific numbers (concentrations, p-values, timepoints) rather than vague descriptions.

---

## Weight Assignment

### The 55/25/12/8 Template

This weight distribution worked well for structured extraction and generalizes to most prompt types:

| Category | Weight | What it covers |
|----------|--------|---------------|
| Primary quality | 50-60% | The core job — what the output is FOR |
| Secondary quality | 20-30% | Supporting quality — grounds claims in evidence |
| Connectivity/context | 10-15% | Cross-references, source attribution |
| Format compliance | 5-10% | Schema validity — lowest priority if coercion exists |

### Weight Assignment Process

1. List all metrics
2. Group by category (primary, secondary, connectivity, format)
3. Assign category-level weights based on downstream importance
4. Distribute within-category weights proportionally to metric importance
5. Verify sum = 1.0
6. Sanity check: run scorer on a known-good and known-bad output. The good output should score >0.85, the bad output <0.40. If not, rebalance.

---

## Common Patterns by Prompt Type

### Structured Extraction (JSON output)
Primary: density, field coverage, quantitative context, conditions
Secondary: evidence linkage, evidence completeness
Connectivity: DOI coverage, citation contexts, cross-references
Format: vocabulary validity, schema compliance

### Text Summarization
Primary: information coverage (key facts present), conciseness (within length target)
Secondary: factual accuracy (no hallucinated facts), coherence
Format: structure compliance (sections present), length bounds

### Classification/Labeling
Primary: accuracy (against known labels if available), confidence calibration
Secondary: reasoning quality (explanation present and relevant)
Format: label validity, schema compliance

### Code Generation
Primary: correctness (passes test cases), completeness (handles edge cases)
Secondary: readability, efficiency
Format: syntax validity, style compliance

### Creative/Generative
Warning: these are harder to score automatically. Consider:
Primary: constraint satisfaction (length, style markers, required elements)
Secondary: vocabulary diversity, structural variety
Format: format compliance

---

## Implementation Patterns

### The score_output Function

```python
def score_output(
    data: dict,
    rapid: bool = False,
) -> tuple[float, dict[str, float]]:
    items = data.get("items") or []
    n = len(items)

    if n == 0:
        return 0.0, {name: 0.0 for name in METRIC_WEIGHTS}

    # Compute all metrics
    metrics = {
        "density": _metric_density(n),
        "coverage_x": _metric_field_coverage(items, "x"),
        "diversity_y": _metric_diversity(items, "y"),
        # ... all metrics ...
    }

    # Composite (rapid-aware)
    if rapid:
        active = {k: v for k, v in METRIC_WEIGHTS.items() if k not in RAPID_EXCLUDE}
        total_weight = sum(active.values())
        composite = sum(active[k] * metrics[k] for k in active) / total_weight
    else:
        composite = sum(METRIC_WEIGHTS[k] * metrics[k] for k, v in metrics.items())

    return composite, metrics
```

### The score_batch Function

Failed/errored outputs (an `_error` key) are an infrastructure outcome, not a quality-zero
sample. They must never be folded into the average as 0.0 (which drags the score down as if
it were a genuinely bad response) and never silently excluded from the denominator without
being counted (which inflates the average by only scoring successes). Return a coverage
count alongside the composite, and return `None` — not `0.0` — when nothing could be scored,
so a CANNOT-EVALUATE case can never be compared as if it were a real (bad) score.

```python
def score_batch(
    outputs: list[dict],
    rapid: bool = False,
) -> tuple[float | None, dict[str, float | None], dict]:
    """Average composite and per-metric across successfully-scored outputs.

    Returns (composite_or_None, per_metric_dict, coverage_info). `coverage_info`
    carries {n_total, n_scored, n_failed, coverage} — always report it alongside
    the composite; a score computed at partial coverage is a different claim than
    one at full coverage.
    """
    failed = [o for o in outputs if o.get("_error")]
    successful = [o for o in outputs if not o.get("_error")]
    coverage_info = {
        "n_total": len(outputs),
        "n_scored": len(successful),
        "n_failed": len(failed),
        "coverage": (len(successful) / len(outputs)) if outputs else 0.0,
    }

    if not successful:
        # CANNOT-EVALUATE: distinct from a real 0.0 quality score. Callers must
        # refuse to accept/reject/rank on a None composite, not treat it as bad.
        return None, {name: None for name in METRIC_WEIGHTS}, coverage_info

    composites = []
    per_metric_accum = {name: [] for name in METRIC_WEIGHTS}
    for output in successful:
        composite, metrics = score_output(output, rapid=rapid)
        composites.append(composite)
        for name, value in metrics.items():
            per_metric_accum[name].append(value)

    return (
        sum(composites) / len(composites),
        {name: sum(vals) / len(vals) for name, vals in per_metric_accum.items()},
        coverage_info,
    )
```

---

## Anti-Patterns

### "More is better" density
Don't use `return n / 100` — this incentivizes the prompt to produce 100 items regardless of input. Use target ranges with penalties for overshooting.

### Scoring format before function
If 60% of your weights are on format validity, the optimizer will make the prompt produce perfectly formatted garbage. Weight substance over form.

### Metrics that can't distinguish good from bad
If a metric returns 0.95+ on both good and bad outputs, it's not discriminating. Remove or redesign it.

### Hard cutoffs instead of continuous scoring
Don't use `return 1.0 if n >= 25 else 0.0`. Use continuous functions so the optimizer gets gradient signal. A score of 0.0 for 24 items and 1.0 for 25 items tells the optimizer nothing about whether 15 → 20 was progress.

### Per-metric regression guards
Don't reject edits that improve the composite but drop one metric. The composite score is the optimization target. Per-metric regression guards over-constrain the search and cause excessive rejections.

### Ignoring the production pipeline
If production applies coercion/normalization/filtering, apply it before scoring. Otherwise you optimize for problems that don't exist in production.

### Silently dropping failed/errored outputs from the average
Excluding `_error` outputs from the denominator without tracking how many were excluded turns "infrastructure failed to run" into "this scored well" by shrinking the sample size unnoticed. Always return a coverage count alongside the composite (see `score_batch` above), and treat 0% coverage as CANNOT-EVALUATE (`None`), never as `0.0`.

### Scoring a malformed output on the quality scale
An empty/unparseable/schema-invalid output is a validity-gate failure, not a low quality score. If a metric like density naturally returns 0.0 for `n == 0`, that's fine for a well-formed-but-empty output — but a parse failure or schema violation should be tagged `_error`/`invalid` upstream and routed through the coverage accounting above, not scored 0.0 alongside genuinely weak-but-valid outputs.
