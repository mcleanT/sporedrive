# Multi-Team Pipeline — Complete Protocol
> This file is read by the main context before multi-team Phase 1 and re-read before Phase 3.
> Phase 3 steps are SEQUENTIAL and MANDATORY — the anti-skip enforcement checks below MUST pass.

## [MT] Team Assignment

**Prompt**: `prompts/team_assignment.md`  |  **Model**: sonnet

**Persona Selection**: If `persona_selection_strategy=auto` or `domain-specific`, first
dispatch a sonnet subagent with `prompts/persona_selection.md` to score each persona's
domain relevance to the research question. Then load top-scoring personas from
`personas/library/*.json` and assign to T teams balancing methodology + domain.
If `high-diversity` or `all`, skip relevance scoring and select for maximum methodological spread.

**Reasoning-Mode Diversity**: Each team's subagents use the team's assigned reasoning mode.
- `auto`: For N teams, assign ["standard", "thinking", "adversarial"] (cycles if N>3)
- `uniform`: All teams use "standard" (no diversity)
- Custom list: e.g., ["standard", "adversarial", "thinking"]

Three modes available:
- `standard` — normal reasoning
- `thinking` — extended thinking / chain-of-thought
- `adversarial` — critic framing ("Find flaws, question assumptions, challenge conclusions")

Include in each subagent dispatch prompt:
"You are operating in {mode} reasoning mode. {mode_instructions}"
where adversarial = "Find flaws, question assumptions, challenge conclusions"

---

## Multi-Team Pipeline Architecture

### Phase 1 (Shared)
- Team Assignment
- Shared Literature Review (1×, using MCP tools) → saved to `{experiment_dir}/shared/`

### Phase 2 — Per-Team Pipelines (T teams in parallel)
Each team runs these stages with:
- Persona injection at hypothesis, approach, analysis, dossier stages
- `other_team_focuses` passed to Data Acquisition for dataset complementarity
- Stage 6 iterative deepening uses FULL ROUNDTABLE with team personas between rounds
  (not single-agent reflection)
- Reasoning-mode diversity: each team's subagents use the team's assigned mode

Per-team stages:
```
Hypothesis Gen → Approach Gen → Data Acquisition →
  Analysis Planner → Analysis (iterative) → Figure Composition →
  Findings Dossier (structured JSON, replaces per-team Paper Gen)
```

### Phase 3 — Cross-Team Integration (MANDATORY SEQUENTIAL ORCHESTRATION)

**CRITICAL**: This phase was SKIPPED in a real run because the main context jumped from
meta-comparison directly to final synthesis, omitting tiered reanalysis entirely. The
divergences were "resolved" narratively — exactly the failure mode described below.

**The main context MUST execute these 4 steps IN ORDER. No step can be skipped.**

```
Step 1: Gene Panel Overlap
  → Save cross_team/gene_panel_overlap.json
  → GATE: file exists with pairwise Jaccard scores

Step 2: Meta-Comparison (opus subagent)
  → Save cross_team/meta_comparison.json
  → GATE: ≥20 atomic claims, divergences explicitly flagged
  → Extract divergence_list from output

Step 3: Reanalysis Loop (MANDATORY if divergence_list is non-empty)
  → For EACH divergence in divergence_list:
     dispatch reanalysis subagent (escalate through tiers)
  → Save cross_team/reanalysis_d{N}.json + figures per divergence
  → GATE: Python code executed + figures generated for EVERY divergence
  → BLOCKING: Final Synthesis CANNOT start until ALL divergences processed

Step 4: Final Synthesis (opus subagent)
  → Input MUST include reanalysis results (not just meta-comparison)
  → Save final/synthesis_paper.md
  → GATE: Confidence tiers assigned, reanalysis evidence cited
```

**Anti-skip enforcement**: Before dispatching Final Synthesis, the main context MUST verify:
1. `cross_team/gene_panel_overlap.json` exists
2. `cross_team/meta_comparison.json` exists with `divergences` array
3. For EACH divergence in the array: `cross_team/reanalysis_d{N}.json` exists
4. Each reanalysis file contains `code_executed: true` and `figures` array (non-empty)

If ANY check fails → DO NOT proceed to Final Synthesis. Dispatch the missing stage instead.

### Phase 3.5 — Adversarial Review (MANDATORY if adversarial_review enabled)

The main context MUST verify before Paper Generation (Phase 4):
1. `adversarial_review/checklist_results.json` exists with all 8 check IDs
2. `adversarial_review/panel_review.json` exists with valid editorial decision
3. `adversarial_review/resolution.json` exists with all concerns accounted for
4. `adversarial_review/revised_synthesis.json` exists with non-empty `revised_synthesis_md`
5. If ANY check fails: do NOT proceed to Phase 4. Dispatch the missing stage.

If `adversarial_review` is disabled in config: skip Phase 3.5 entirely and proceed to Phase 4 with `final/synthesis_paper.md` as input.

---

## [MT] Step 1: Gene Panel Overlap

Extract gene panels per team → pairwise Jaccard overlap → unique genes per team.
Save to `cross_team/gene_panel_overlap.json`.

---

## [MT] Step 2: Meta-Comparison

**Prompt**: `prompts/meta_comparison.md`  |  **Model**: opus

Input: All `team_{N}/findings_dossier.json` files (not papers — dossiers contain machine-
readable arrays). Atomic claims can be extracted directly from `validated_findings` and
`hypothesis_status` arrays, with full statistical evidence already structured. No
free-text claim extraction from prose is needed. The agent still classifies claim pairs
as CONVERGENT / DIVERGENT / UNIQUE / COMPLEMENTARY and flags divergences for reanalysis.

**Dispatch with**:
- All `team_{N}/findings_dossier.json` files
- `cross_team/gene_panel_overlap.json`
- Research question + lit review summary

**Output MUST include a `divergences` array** — each entry has:
- `divergence_id`: "D1", "D2", etc.
- `claim_team_a`: the claim from one team (reference by `finding_id` from dossier)
- `claim_team_b`: the contradicting claim from another team (reference by `finding_id`)
- `team_a_data_path`: path to Team A's data files
- `team_b_data_path`: path to Team B's data files
- `team_a_method`: description of Team A's analytical method (from dossier `methods_summary`)
- `team_b_method`: description of Team B's analytical method (from dossier `methods_summary`)
- `team_a_statistics`: key statistics from the dossier (copied from `key_statistics`)
- `team_b_statistics`: key statistics from the dossier (copied from `key_statistics`)

**Validation Gate**: ≥20 atomic claims extracted (directly from dossier arrays), divergences
explicitly flagged with data paths, method descriptions, and statistical details.

### After Meta-Comparison: Convergence Validity Classification

Call `classify_convergence_validity()` from `agents/convergence_validity_classifier.py`
with the convergent claims from meta-comparison + team dossiers.

Each convergence is classified as:
- **Type A (true independence)**: different datasets or modalities → strong confidence upgrade
- **Type B (partial independence)**: same data, different methods → moderate upgrade
- **Type C (pseudo-independence)**: same data + same priors → NO confidence upgrade

Save to `cross_team/convergence_validity.json`.
Feed to Final Synthesis: Type C convergences MUST NOT be cited as "robust" or
"independently confirmed." Language like "irrefutable" or "convergence establishes"
is BANNED for Type C claims.

### After Convergence Validity: Dataset Reuse Scoring

Call `score_dataset_reuse()` from `agents/dataset_reuse_scorer.py`
with convergent claims + team dossiers.
- Computes dataset independence score (Jaccard distance), platform independence,
  and reuse penalty (0-0.5) per convergent claim
- Claims with reuse_penalty > 0.2 get downgraded one confidence tier in synthesis
- Save to `cross_team/dataset_reuse.json`

---

## [MT] Step 3: Reanalysis (Tiered, WITH Code Execution)

Reanalysis MUST involve writing and executing Python code. Narrative-only reconciliation
is not acceptable — it's a recurring failure where divergences get "resolved" by reasoning
without computational verification. The reason code execution matters: logical explanations
can be wrong, and only re-running the analysis on swapped data can verify whether a
divergence is real or methodological.

**Orchestration protocol** (main context controls the loop):

```
divergences = read cross_team/meta_comparison.json → divergences array

for each divergence D in divergences:

  # Tier 1 — Method Swap
  1. Dispatch Method Swap subagent (sonnet) with:
     - D.claim_team_a, D.claim_team_b
     - D.team_a_data_path, D.team_b_data_path
     - D.team_a_method, D.team_b_method
     - Prompt: prompts/method_swap.md

  2. Subagent:
     - Loads Team B's data, applies Team A's method → writes+executes Python
     - Loads Team A's data, applies Team B's method → writes+executes Python
     - Generates comparison figures
     - Reports: resolved (true/false), resolution, quantitative_evidence

  3. If resolved → save cross_team/reanalysis_d{N}.json → next divergence

  # Tier 2 — Discriminating Tests (if Tier 1 unresolved)
  4. Dispatch Discriminating Test subagent (sonnet) with:
     - Tier 1 results + original divergence context
     - Prompt: prompts/discriminating_test.md

  5. Subagent:
     - Designs tests with mutually exclusive predictions
     - Writes+executes Python code
     - Generates figures
     - Reports: resolved, resolution, quantitative_evidence

  6. If resolved → save → next divergence

  # Tier 3 — Mini-Pipeline (if Tier 2 unresolved)
  7. Dispatch Mini-Pipeline subagent (opus) with:
     - Raw data paths only (no access to either team's code)
     - Fresh analysis from scratch

  8. Save cross_team/reanalysis_d{N}.json regardless of resolution
```

**Validation Gate per divergence**:
- [ ] Python code written AND executed (not just described)
- [ ] Quantitative results reported (statistics, not narrative)
- [ ] ≥1 figure generated per divergence
- [ ] Resolution classification: RESOLVED_TEAM_A / RESOLVED_TEAM_B / RESOLVED_BOTH_VALID /
      METHOD_DEPENDENT / UNRESOLVED — with computational evidence

---

## [MT] Step 4: Final Synthesis

**Prompt**: `prompts/final_synthesis.md`  |  **Model**: opus

### Before Dispatching Final Synthesis: Model Competition

Call `compete_models()` from `agents/model_competition.py` with
the convergent/unique claims from meta-comparison + evidence typing data.
- For each major claim, generates 2-6 competing global explanations:
  epiphenomenon, confound, composition artifact, shared upstream, reverse causation, null
- Scores by data_fit (0.5 weight), parsimony (0.2), external_support (0.3)
- Determines conclusion qualifier:
  * winning_margin > 0.3: "strongly supported"
  * 0.15-0.3: "best among alternatives"
  * 0.05-0.15: "marginally preferred"
  * <0.05: "indeterminate"
- Save to `cross_team/model_competition.json`
- Feed to Final Synthesis: each claim MUST use the qualifier from model competition.
  Claims with "indeterminate" qualifier MUST present multiple interpretations.
  Claims with "marginally preferred" MUST acknowledge alternatives in the Discussion.

### Final Synthesis Input (MUST include all of these)
- All `team_{N}/findings_dossier.json` files (structured findings, NOT per-team papers)
- Meta-comparison results (convergent, complementary, unique claims)
- **Reanalysis results for every divergence** (resolution + evidence + figures)
- Gene panel overlap data

Confidence tiers: ROBUST / SUPPORTED / METHOD-DEPENDENT / PRELIMINARY / OPEN QUESTION.
Must reference composite figures, embed as markdown images.
Divergence resolutions from reanalysis MUST be cited with their tier and evidence.

---

## Phase 3.5: Adversarial Review

After Final Synthesis passes its gate, the main context dispatches Phase 3.5.

**Full protocol**: See `refs/adversarial_review.md` (§1–§5)
**Dispatch cards**: See SKILL.md Section 6A

**Dispatch order (sequential — each gates before the next):**
1. Failure Mode Checklist (16 checks, parallel) → gate all 16 check IDs present
2. Reviewer Panel Assessment (1 opus) → gate editorial decision present
3. Tiered Response (per editorial decision scope) → gate per tier artifact
4. Review Resolution (1 sonnet) → gate all concerns accounted for
5. Synthesis Revision (1 sonnet) → gate revised_synthesis_md non-empty

**Phase 3.6: Data Cleanup** runs after Phase 3.5 resolution passes its gate. Uses the same deletion rules as before (raw extensions deleted, preprocessed kept). Safety constraint: if `resolution.json` contains ANY `unresolved_concerns` with `disposition: "requires_further_analysis"`, cleanup is SKIPPED and a warning is logged.

**Phase 4 input change**: Paper Generation receives `adversarial_review/revised_synthesis.json` as its primary synthesis input (instead of `final/synthesis_paper.md`). See `refs/phase4_orchestrator.md` for integration details.

---

## [MT] Stage 7b: Findings Dossier (Multi-Team Only)

**Model**: sonnet  |  **Replaces**: per-team Paper Generation in multi-team mode

In multi-team mode, each team produces a **Findings Dossier** instead of a paper after
Figure Composition. The dossier is a structured JSON document richer than a paper for
information exchange: it preserves statistical details, figure paths, round provenance,
and explicit cross-team comparison suggestions that prose loses. The Meta-Comparison
agent (Phase 3, Step 2) reads dossiers directly — no claim extraction from prose needed.

**CRITICAL**: This stage replaces per-team Paper Generation in multi-team mode only.
Single-team mode continues to use Stage 8 (Paper Generation) unmodified.

1. Dispatch sonnet subagent per team with:
   - `analysis_synthesis.json` (validated findings, refuted findings, open questions)
   - All round checkpoint files (`round{N}/findings.json`, `round{N}/roundtable_verdict.json`)
   - PI interpretations from all rounds (`pi_interpretations.json`)
   - Figure inventory (`figure_inventory.json`) and composite figure paths
   - Team persona profiles
   - Research question + lit review summary
   - Task: Produce `team_{N}/findings_dossier.json` per the schema below

2. Subagent writes `team_{N}/findings_dossier.json`:

```json
{
  "team_id": 1,
  "team_name": "...",
  "research_question": "...",

  "hypothesis_status": [
    {
      "hypothesis": "...",
      "status": "confirmed|falsified|inconclusive|refined",
      "evidence_summary": "2-3 sentences summarising the evidence",
      "key_statistics": [{"test": "...", "stat": 0.0, "p": 0.0, "effect_size": 0.0, "ci": [0.0, 0.0]}],
      "supporting_figures": ["team_1/figures/fig1.pdf"],
      "round_established": 2,
      "corroboration": "second method or data type that supports this"
    }
  ],

  "validated_findings": [
    {
      "finding_id": "F1",
      "description": "...",
      "biological_interpretation": "mechanism explanation",
      "statistical_evidence": {"test": "...", "stat": 0.0, "p": 0.0, "effect_size": 0.0, "fdr_q": 0.0},
      "figure_path": "team_1/figures/fig1.pdf",
      "round": 2,
      "corroborated_by": "second method or dataset that replicates this",
      "clinical_relevance": "...",
      "data_source": "GSE12345 / specific dataset"
    }
  ],

  "falsified_hypotheses": [
    {
      "hypothesis": "...",
      "evidence_against": "specific statistical evidence that contradicts this",
      "what_was_learned": "what this falsification reveals",
      "round_falsified": 3
    }
  ],

  "gene_panel": {
    "genes": ["NRL", "CRX"],
    "gene_evidence": {
      "NRL": {"effect_size": 0.8, "direction": "up", "context": "upregulated in photoreceptor differentiation"}
    }
  },

  "methods_summary": {
    "datasets_used": [{"accession": "GSE12345", "type": "scRNA-seq", "samples": 42}],
    "statistical_methods": ["Wilcoxon rank-sum", "BH FDR correction", "Kaplan-Meier"],
    "software": ["scanpy 1.9", "statsmodels 0.14", "autonomous_science.analytics"]
  },

  "open_questions": ["..."],
  "data_gaps": ["To publish finding F1, we would need longitudinal data showing X"],
  "suggested_cross_team_comparisons": [
    "Compare gene panel overlap with Team 2 — both investigate photoreceptor fate"
  ],

  "round_progression": [
    {"round": 1, "focus": "Differential expression landscape", "key_result": "...", "led_to": "..."},
    {"round": 2, "focus": "Pathway enrichment of DEGs", "key_result": "...", "led_to": "..."}
  ],

  "mechanistic_model": "integrated narrative from final PI interpretation roundtable"
}
```

**Validation Gate**:
- [ ] `team_{N}/findings_dossier.json` written for every team
- [ ] `hypothesis_status` array covers ALL hypotheses from Stage 2 (none omitted)
- [ ] `validated_findings` array contains ≥3 findings with full statistical evidence
- [ ] `falsified_hypotheses` array present (may be empty — must be explicit)
- [ ] `gene_panel.gene_evidence` populated for all listed genes
- [ ] `round_progression` reflects actual rounds completed (length matches analysis rounds)
- [ ] All figure paths in `supporting_figures` and `figure_path` fields exist on disk
- [ ] `suggested_cross_team_comparisons` non-empty (team must identify ≥1 comparison)

---

## [MT] Post-Synthesis Chain (ALL 9 Must Execute)

Paper Generation and Paper Descent now run here (post-synthesis) rather than per-team.
See Stage 8 in SKILL.md for the detailed Paper Generation protocol — in multi-team mode,
use the multi-team input list (synthesis paper + all dossiers + reanalysis results).

| Step | Model | Output |
|------|-------|--------|
| Paper Generation | opus | `final/paper.md` (single comprehensive paper using synthesis + dossiers) |
| Paper Descent | opus | `final/paper_final.md` + `final/paper_final.pdf` (section refinement) |
| Supplementary Tables S1-S6 | haiku | `final/supplementary/table_s*.csv` + `table_s*.pdf` |
| Figure Composition Plan | sonnet | `final/figure_plan.json` |
| **Supplementary Figures Document** | sonnet | `final/supplementary_figures.pdf` + `final/supplementary_figures_index.json` |
| Reference Validation | haiku | `final/reference_validation.json` |
| Citation Provenance QA | haiku | `final/citation_provenance.json` |
| Sentence Claim Governance | haiku | `final/sentence_claims.json` |
| Synthesis Claim Ceiling | haiku | `final/synthesis_ceiling.json` |
| Findings Network Diagram | sonnet | `final/findings_network.pdf` + `.png` preview |
| Graphical Abstract | sonnet | `final/graphical_abstract.pdf` + `.png` preview |
| Audit Manifest | haiku | `final/audit_manifest.json` |

### Supplementary Figures Document
**Model**: sonnet  |  **Runs after**: Figure Composition Plan

Dispatch a sonnet subagent with:
- All `{team_dir}/figures/`, `{team_dir}/round*/`, and `cross_team/reanalysis_*/figures/` paths
- `final/synthesis_paper.md` (to determine which figures ARE embedded in the main paper)
- `final/figure_plan.json`
- Each team's `round*_summary.json` or equivalent metadata for captions

**Task**: Compile a Supplementary Figures PDF from every analysis figure NOT embedded in `final/synthesis_paper.md`:
1. Identify figures already embedded in the synthesis paper (parse `![...]()` references) — exclude these
2. Collect all remaining PNG/PDF figures from team analysis rounds and cross-team reanalysis
3. Assign sequential figure numbers: S1, S2, S3...
4. For each figure extract a caption from the nearest `round_summary`, figure metadata, or filename
5. Organize by team, then by round within each team
6. Add a header page: "Supplementary Figures — {experiment name}" with table of contents
7. Render as `final/supplementary_figures.pdf` (one figure per page, caption below each)
8. Write `final/supplementary_figures_index.json`:
   ```json
   [
     {
       "figure_number": "S1",
       "source_path": "team_A/round2/sa03_volcano.png",
       "team": "team_A",
       "round": 2,
       "caption": "Volcano plot of differential expression..."
     }
   ]
   ```
9. Add a final line to `final/synthesis_paper.md`: "See Supplementary Figures Document (`supplementary_figures.pdf`) for additional analyses."

**Validation Gate**:
- [ ] `final/supplementary_figures.pdf` exists and is non-empty (>100KB indicates multiple figures)
- [ ] `final/supplementary_figures_index.json` exists with ≥1 entry
- [ ] No figure numbered S1+ is also embedded in the main synthesis paper (no overlap)

### Citation Provenance QA
**Model**: haiku  |  **Runs after**: Reference Validation

Dispatch with `final/paper_final.md` + `final/reference_validation.json`. Task:
1. Detect preprints cited as established evidence (bioRxiv/medRxiv DOIs, qualifying language check)
2. Detect reviews cited where primary data is needed (empirical claims backed only by reviews)
3. Check claim-reference alignment (does the citing sentence match what the reference shows?)
4. Flag missing DOIs
Save to `final/citation_provenance.json`.

**Validation Gate**:
- [ ] `final/citation_provenance.json` exists
- [ ] All references checked (references_checked == total_references)
- [ ] citation_quality_score present (0-1 float)
- [ ] Any preprints_cited_as_established flagged as CRITICAL issues

### Sentence-Level Claim Governance
**Model**: haiku  |  **Runs after**: Paper Descent (before supplementary tables)

Dispatch with `final/paper_final.md` + evidence-typing validator output + analysis synthesis findings.
Task: Call `govern_sentence_claims()` from `agents/sentence_claim_governor.py` to parse every
sentence in Results and Discussion, detect claim strength, map to source findings, and flag
sentences exceeding their ceiling. Save to `final/sentence_claims.json`.
If violations found: dispatch follow-up sonnet subagent to rewrite violated sentences using
`suggested_rewrite` field → `final/paper_governed.md`.

**Validation Gate**:
- [ ] `final/sentence_claims.json` exists
- [ ] claim_strength_score ≥ 0.8 (≤20% of claim sentences have violations)
- [ ] If violations found: `final/paper_governed.md` exists with rewrites applied
- [ ] No therapeutic_target claims without perturbation+clinical evidence

### Synthesis Claim Ceiling Enforcement
**Model**: haiku  |  **Runs after**: Sentence Claim Governance

Dispatch with `final/paper_governed.md` (or `final/paper_final.md` if no governance violations)
+ evidence-typing classifications from all teams + `cross_team/convergence_validity.json`.
Task: Call `enforce_synthesis_ceiling()` from `agents/synthesis_claim_ceiling.py`.
- Type C convergence DOWNGRADES the effective ceiling one tier
- Type A convergence ALLOWS one tier upgrade
Save to `final/synthesis_ceiling.json`.
If violations: produce `final/paper_ceiling_governed.md` with downgrades applied.

**Validation Gate**:
- [ ] `final/synthesis_ceiling.json` exists
- [ ] synthesis_claim_score ≥ 0.8
- [ ] No "irrefutable"/"definitive" language for Type C convergences
- [ ] No mechanism claims where all component ceilings are candidate_pathway

### Findings Network Diagram
**Model**: sonnet

Dispatch with `cross_team/meta_comparison.json` + `cross_team/reanalysis_d*.json` + `final/synthesis_paper.md`.
Task: Generate network graph:
- Nodes = atomic claims (colored by confidence tier: ROBUST=green, SUPPORTED=blue,
  METHOD-DEPENDENT=orange, PRELIMINARY=yellow, OPEN=red)
- Edges = relationships (CONVERGENT=solid, COMPLEMENTARY=dashed, DIVERGENT=red)
- Node size proportional to number of teams supporting the claim
- Edge labels show reanalysis resolution tier if applicable
Use matplotlib + networkx (or graphviz). Save as `final/findings_network.pdf` (vector) + `final/findings_network.png` (preview).
Also save `final/findings_network.json` (node-link format).

**Validation Gate**:
- [ ] PDF and PNG files exist and are non-empty
- [ ] JSON node-link file exists with ≥10 nodes
- [ ] Colorblind-safe palette used for confidence tiers

### Graphical Abstract
**Model**: sonnet

Dispatch with `final/synthesis_paper.md` + `final/findings_network.json` + team paper summaries.
Task: Generate multi-panel graphical abstract (single figure) showing:
- Panel A: Pipeline overview (question → N teams → synthesis)
- Panel B: Key convergent finding visualization (most significant result)
- Panel C: Findings network (simplified version of findings_network)
- Panel D: Confidence tier breakdown (bar chart or pie)
Use matplotlib with Nature analytics toolkit theme. Layout: 2×2 grid or horizontal strip, sized for journal submission (180mm wide).
Save as `final/graphical_abstract.pdf` (vector) + `final/graphical_abstract.png` (preview).

**Validation Gate**:
- [ ] PDF and PNG files exist
- [ ] Multi-panel layout (≥2 panels)
- [ ] Colorblind-safe, Arial/Helvetica, ≥300 DPI for PNG

### Post-Synthesis Chain Validation Gate (All Steps)
- [ ] `final/paper.md` and `final/paper_final.pdf` exist
- [ ] `final/supplementary/` contains `table_s*.csv` (≥4 files)
- [ ] `final/supplementary_figures.pdf` exists (>100KB)
- [ ] `final/supplementary_figures_index.json` exists
- [ ] `final/findings_network.pdf` and `.png` exist
- [ ] `final/graphical_abstract.pdf` and `.png` exist
- [ ] `final/reference_validation.json` exists
- [ ] `final/audit_manifest.json` exists
- [ ] `final/citation_provenance.json` exists
- [ ] `final/sentence_claims.json` exists
- [ ] `final/synthesis_ceiling.json` exists
