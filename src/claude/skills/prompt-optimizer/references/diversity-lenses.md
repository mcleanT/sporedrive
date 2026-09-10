# Diversity Lenses for Prompt Optimization

Diversity lenses are strategic perspectives assigned to optimizer candidates during tournament selection. Each candidate in a tournament round receives a different lens, forcing the optimizer to explore different approaches rather than proposing the same edit N times.

## Why Lenses Matter

Without diversity lenses, N parallel optimizer calls tend to converge on the same obvious edit (e.g., "add more examples for the weakest metric"). This wastes the tournament budget — you get 5 nearly identical candidates instead of 5 genuinely different approaches. Lenses solve this by constraining each candidate to a specific strategic frame.

## The 10 Standard Lenses

### 1. structural_rewrite
**Hint:** Try restructuring how the prompt organizes its instructions — reorder sections, merge related rules, or split overloaded rules into clearer sub-rules. Focus on making the instructions easier to follow rather than adding more content.

**When it wins:** The prompt has grown organically and has contradictory or redundant rules scattered across sections. Reorganization resolves confusion without adding tokens.

**Risk:** Can produce large diffs that are hard to validate. Cap at 5-10 lines changed.

### 2. example_driven
**Hint:** Add or improve concrete input→output EXAMPLES rather than adding more rules. Show the model exactly what a good output looks like for the weakest metrics. One good example beats three rules.

**When it wins:** The prompt has plenty of rules but the model doesn't follow them. A concrete example disambiguates what abstract rules cannot.

**Risk:** Examples add tokens. Keep them minimal — show the critical pattern, not a full output.

### 3. counter_example
**Hint:** Add COUNTER-EXAMPLES showing common mistakes and how to avoid them. Focus on the error patterns — show what wrong output looks like and contrast it with the correct version.

**When it wins:** The model consistently makes the same mistake despite rules against it. Seeing the wrong output explicitly helps the model recognize and avoid the pattern.

**Risk:** Counter-examples can be confusing if not clearly marked as "DON'T do this." Always pair with the correct version.

### 4. constraint_tightening
**Hint:** Make vague instructions more specific and actionable. Replace "should" with "MUST", add exact thresholds, specify minimum counts. Turn guidelines into hard constraints with clear pass/fail.

**When it wins:** The prompt uses soft language ("try to", "when possible", "ideally") and the model treats these as optional. Tightening language makes requirements non-negotiable.

**Risk:** Over-constraining can cause the model to produce rigid, unnatural outputs. Use sparingly and only on metrics that are consistently underperforming.

### 5. checklist_approach
**Hint:** Add a pre-output self-check or checklist that the model must run before producing its final output. Target the weakest metrics with specific verification steps.

**When it wins:** The model produces output that's close but misses specific details. A checklist at the end catches omissions before the model commits to its response.

**Risk:** Checklists add output tokens. Keep them to 5-8 items max, focused on the weakest metrics.

### 6. negative_space
**Hint:** Focus on what the prompt does NOT say. Identify implicit assumptions the model might make that lead to errors. Add explicit disambiguation for ambiguous cases.

**When it wins:** The model makes reasonable-but-wrong assumptions about edge cases that the prompt doesn't address. Making the implicit explicit resolves these.

**Risk:** Can lead to over-specification. Only address assumptions that are actually causing errors (check error_patterns).

### 7. cross_metric_synergy
**Hint:** Find edits that improve MULTIPLE weak metrics simultaneously. For example, better evidence instructions can improve both evidence_depth and evidence_completeness at once.

**When it wins:** Multiple metrics are weak for the same underlying reason. One well-placed edit creates a cascade of improvement.

**Risk:** Trying to optimize too many things at once can dilute the edit's impact. Focus on 2-3 synergistic metrics, not all weak ones.

### 8. simplification
**Hint:** The prompt may be too complex or contradictory. Try SIMPLIFYING or REMOVING instructions that may be confusing the model. Sometimes less is more — conflicting rules cause errors.

**When it wins:** The prompt has accumulated rules over many iterations and now contains contradictions or redundancies that confuse the model. Removing noise improves signal.

**Risk:** Removing the wrong instruction can cause regression. Only simplify rules that correlate with error patterns.

### 9. workflow_reframing
**Hint:** Restructure the prompt to guide the model through a specific WORKFLOW — e.g., "First scan all figures, then extract claims, then link evidence." A clear process can be more effective than a list of rules.

**When it wins:** The model processes the input in a suboptimal order (e.g., writing conclusions before reading all evidence). A prescribed workflow improves quality.

**Risk:** Overly rigid workflows can prevent the model from adapting to unusual inputs. Frame as "recommended approach" not "mandatory sequence."

### 10. weakest_link_focus
**Hint:** Ignore the top error patterns — they've likely been targeted already. Focus on the LOWEST-SCORING metric that hasn't been the target of recent edits. Sometimes gains come from unexpected places.

**When it wins:** The optimizer keeps targeting the same obvious issue while ignoring other metrics that have larger marginal improvement potential.

**Risk:** The weakest metric might have a low weight, making improvement there less impactful on the composite. Check the weight before committing.

---

## Implementation

### Python Configuration

```python
DIVERSITY_STRATEGIES = [
    {
        "lens": "structural_rewrite",
        "hint": "Try restructuring how the prompt organizes its instructions..."
    },
    {
        "lens": "example_driven",
        "hint": "Add or improve concrete input→output EXAMPLES..."
    },
    # ... all 10 lenses
]
```

### Tournament Selection

Each iteration:
1. If N candidates ≤ 10 lenses: sample N unique lenses (no replacement)
2. If N candidates > 10 lenses: use all 10 + random extras
3. Pass the lens hint to each optimizer call as an additional section
4. The optimizer is instructed to let the lens guide (not dictate) its approach

### Escalation on Consecutive Rejects

After 5 consecutive rejects, append to the diversity hint:

```
## IMPORTANT: {N} consecutive rejects

The following recent approaches ALL FAILED — do NOT repeat them:
  - {summary of failed approach 1}
  - {summary of failed approach 2}
  ...

You MUST try a fundamentally different strategy. Consider:
- Editing a completely different section of the prompt
- Using a different technique (examples vs rules vs checklists)
- Targeting a different metric than the obvious choice
- Simplifying or removing instructions instead of adding more
```

This prevents the optimizer from persisting on dead-end approaches.

---

## Lens Selection Strategy

### Early iterations (1-10)
Use all lenses — you're exploring the optimization landscape. Any direction could yield gains.

### Mid iterations (10-30)
Track which lenses produce accepted edits. Weight sampling toward successful lenses, but always include 1-2 random lenses for exploration.

### Late iterations (30+)
If a specific lens consistently produces rejects, consider removing it from the pool. But always keep at least 5 active lenses to maintain diversity.

### Phase 2 (Cost Reduction)
Simplification and structural_rewrite tend to dominate in cost reduction. Example_driven and counter_example are less useful (they add tokens). Adjust the pool accordingly.

---

## Custom Lenses

For domain-specific optimization, you can add custom lenses:

```python
{
    "lens": "domain_knowledge",
    "hint": (
        "Leverage your knowledge of {DOMAIN} to improve the prompt. "
        "Add domain-specific terminology, common patterns, or field "
        "conventions that would help the extraction model produce "
        "more accurate outputs."
    ),
}
```

Good custom lenses target domain-specific failure modes that generic lenses miss. Bad custom lenses are just renamed versions of existing ones.
