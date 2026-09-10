# Phase 3.5 Adversarial Review Protocol

This file contains the full prompt templates and schemas for the adversarial review phase.
The main context reads this file and dispatches agents according to the templates below.
Dispatch cards (model, artifact, gate) are in SKILL.md Section 6A. This file has the prompts.

---

## §1 Failure Mode Checklist

Sixteen field-agnostic epistemological checks. Each check is an independent agent dispatch.
The main context dispatches all 16 in parallel (16 Agent calls in one message).
Output: All 16 results merged into `adversarial_review/checklist_results.json`.

The merging agent (haiku) collects the 16 individual check outputs and produces the merged file.
If a check times out or fails, include it with `status: "error"` and `reason`.

---

### FM-01: Evidence–Claim Escalation

**Model:** haiku

**Prompt:**
You are evaluating whether any scientific claims exceed what their evidence type supports.

Evidence type hierarchy (weakest to strongest):
- correlational / associational → supports "associated with", "co-occurs with"
- predictive → supports "predicts", "is a biomarker for"
- interventional / perturbational → supports "causes", "drives", "is required for"
- mechanistic (with pathway validation) → supports "mechanism", "cascade", "pathway"

For each claim in the synthesis, compare its language strength against its evidence type.
Flag any claim that uses language from a higher tier than its evidence supports.

**Input:** All team findings JSONs (`{team}/analysis_synthesis.json`) + synthesis text (`final/synthesis_paper.md` or `analysis_synthesis.md` in single-team mode)

**Output:** JSON with structure:
```json
{
  "check_id": "FM-01",
  "check_name": "evidence_claim_escalation",
  "status": "pass|fail|partial",
  "violations": [
    {
      "claim": "exact claim text from synthesis",
      "source_finding": "finding_id or description",
      "evidence_type": "correlational|predictive|interventional|mechanistic",
      "claim_strength": "associational|predictive|causal|mechanistic",
      "severity": "major|minor"
    }
  ]
}
```

---

### FM-02: Scope–N Mismatch

**Model:** haiku

**Prompt:**
You are evaluating whether generalization claims are supported by sufficient independent instances.

Scope thresholds:
- "pan-cancer" / "universal" / "conserved across" → requires ≥5 independent cancer types/systems
- "common" / "widespread" → requires ≥3 independent instances
- "in X and Y" (named) → requires evidence from each named instance
- No scope language → no flag needed

Count the distinct independent datasets/cancer types/model systems that support each scoped claim. Independent means different GEO accessions or different biological sources — not different analyses of the same data.

Flag any claim where scope language exceeds the instance count.

**Input:** Synthesis claims + dataset inventory from all team `data_manifest.json` files

**Output:** JSON with structure:
```json
{
  "check_id": "FM-02",
  "check_name": "scope_n_mismatch",
  "status": "pass|fail|partial",
  "violations": [
    {
      "claim": "exact claim text",
      "scope_language": "pan-cancer|universal|conserved|common|widespread",
      "instances_claimed": "implicit count from scope language",
      "instances_evidenced": 2,
      "datasets_supporting": ["GSE123", "GSE456"],
      "severity": "major|minor"
    }
  ]
}
```

---

### FM-03: Novelty Inflation (requires MCP)

**Model:** sonnet (needs reasoning for literature comparison)

**Prompt:**
You are evaluating whether "first", "novel", or "unprecedented" claims have adequate prior art support.

For each claim containing novelty language ("first", "novel", "unprecedented", "field-defining", "no prior study", "we demonstrate for the first time"):
1. Extract the core assertion being claimed as novel
2. Search PubMed and Semantic Scholar for prior work making the same or substantially similar assertion
3. If prior art exists that substantially overlaps, flag the claim
4. If prior art partially overlaps (same biology, different method), flag as "minor"

**MCP tools available:** `search_pubmed`, `search_semantic_scholar`

**Fallback:** If MCP tools are unavailable (timeout, rate limit, network error), produce:
```json
{"check_id": "FM-03", "check_name": "novelty_inflation", "status": "skipped", "reason": "mcp_unavailable", "violations": []}
```

**Input:** Claims containing novelty language extracted from synthesis text

**Output:** JSON with structure:
```json
{
  "check_id": "FM-03",
  "check_name": "novelty_inflation",
  "status": "pass|fail|partial|skipped",
  "reason": "only populated if skipped",
  "violations": [
    {
      "claim": "exact claim text",
      "novelty_language": "first|novel|unprecedented|field-defining",
      "prior_art": [
        {"doi": "10.1234/...", "title": "...", "overlap_description": "..."}
      ],
      "severity": "major|minor"
    }
  ]
}
```

---

### FM-04: Projected ≠ Executed

**Model:** haiku

**Prompt:**
You are evaluating whether corrections and validations described in the synthesis were actually computed on data, or only theoretically projected.

For each claim about a correction, validation, or reanalysis being "applied", "performed", "executed", or "computed":
1. Search the experiment directory for a corresponding code artifact (`.py` file) that implements this correction
2. Search for output files (figures, JSON results) that would be produced by executing the correction
3. Check if the correction is described with hedging language ("would be expected to", "projected to") vs. definitive language ("was applied", "confirmed that")

Flag any correction described with definitive language that has no execution evidence.
Flag any correction described with hedging language that is later cited as if it were definitive.

**Input:** Correction/validation claims from synthesis + file listing of `experiment outputs/{name}/` (use `ls -R` or glob)

**Output:** JSON with structure:
```json
{
  "check_id": "FM-04",
  "check_name": "projected_not_executed",
  "status": "pass|fail|partial",
  "violations": [
    {
      "claim": "exact claim text about the correction",
      "correction_type": "geometry null|batch correction|normalization|other",
      "language_used": "definitive|hedged",
      "expected_artifact": "path to expected .py or output",
      "artifact_found": true,
      "severity": "major|minor"
    }
  ]
}
```

---

### FM-05: Model ≠ Mechanism

**Model:** haiku

**Prompt:**
You are evaluating whether integrative models are incorrectly presented as validated causal mechanisms.

Key distinction:
- A **model** or **framework** is built from observational associations and computational synthesis. It is a hypothesis about how things work.
- A **mechanism** requires interventional evidence — perturbation, knockout, inhibition, or gain-of-function experiments that demonstrate causality.

Mechanism language includes: "drives", "causes", "cascade", "required for", "mediates", "activates", "inhibits", "triggers", "induces"
Model language includes: "suggests", "is consistent with", "model proposes", "framework describes", "associated with", "correlates with"

For each claim using mechanism language, check whether ANY finding in the evidence chain includes interventional evidence. If all evidence is observational/associational, the mechanism language is not supported.

**Input:** Synthesis text + evidence type inventory per finding from team `analysis_synthesis.json` files

**Output:** JSON with structure:
```json
{
  "check_id": "FM-05",
  "check_name": "model_not_mechanism",
  "status": "pass|fail|partial",
  "violations": [
    {
      "claim": "exact claim text using mechanism language",
      "mechanism_language": "drives|causes|cascade|etc",
      "evidence_types_available": ["correlational", "predictive"],
      "has_interventional_evidence": false,
      "severity": "major|minor"
    }
  ]
}
```

---

### FM-06: Unsurprising Convergence (requires MCP)

**Model:** sonnet (needs reasoning for pathway analysis)

**Prompt:**
You are evaluating whether convergent gene/marker sets reported as a key finding are biologically surprising or merely expected given the studied process.

For each convergent gene set reported in the synthesis:
1. Look up each gene's known pathway memberships
2. Calculate what fraction of the gene set belongs to canonical pathways for the studied process
3. If >70% of genes are canonical markers for the studied biology, the convergence is expected and should not be presented as a discovery

Example: In a study of fibroblast-mediated immune exclusion, convergence on VIM, FN1, TGFB1, ACTA2, COL1A1 is entirely expected (all are canonical fibrosis/EMT markers). The convergence validates methodology but is not a novel biological finding.

**MCP tools available:** `get_pathways_for_gene`, `get_functional_enrichment`

**Fallback:** If MCP tools are unavailable:
```json
{"check_id": "FM-06", "check_name": "unsurprising_convergence", "status": "skipped", "reason": "mcp_unavailable", "violations": []}
```

**Input:** Convergent gene/marker sets from `cross_team/gene_panel_overlap.json` (or single-team gene lists)

**Output:** JSON with structure:
```json
{
  "check_id": "FM-06",
  "check_name": "unsurprising_convergence",
  "status": "pass|fail|partial|skipped",
  "reason": "only populated if skipped",
  "violations": [
    {
      "gene_set": ["VIM", "FN1", "TGFB1"],
      "canonical_fraction": 0.85,
      "canonical_pathways": ["EMT", "TGF-beta signaling", "ECM organization"],
      "surprise_score": 0.15,
      "framing_in_synthesis": "presented as discovery|presented as validation|appropriately hedged",
      "severity": "major|minor"
    }
  ]
}
```

---

### FM-07: Validation Circularity

**Model:** haiku

**Prompt:**
You are evaluating whether validation analyses are truly independent of the discovery analyses they claim to validate.

Types of circularity:
1. **Data circularity**: Validation uses the same dataset (same GEO accession) as discovery
2. **Method circularity**: Validation uses the same statistical test as discovery (e.g., same correlation, same enrichment)
3. **Scale circularity**: Bulk RNA-seq survival associations claimed as validation of single-cell/spatial model (different data scale, but the bulk signature is derived from the spatial findings)

For each finding that claims "validation", "confirmation", or "independent support":
1. Identify the discovery data source and method
2. Identify the validation data source and method
3. Flag if any circularity type applies

**Input:** Per-finding validation sources and discovery sources from team `analysis_synthesis.json` and `prediction_validation.json`

**Output:** JSON with structure:
```json
{
  "check_id": "FM-07",
  "check_name": "validation_circularity",
  "status": "pass|fail|partial",
  "violations": [
    {
      "finding": "finding description",
      "discovery_source": "GSE123 + Ripley's L",
      "validation_source": "GSE123 + survival analysis",
      "circularity_type": "data|method|scale",
      "severity": "major|minor"
    }
  ]
}
```

---

### FM-08: Correction Propagation Gap

**Model:** haiku

**Prompt:**
You are evaluating whether important methodological corrections identified during analysis were applied to all affected downstream results.

For each correction identified in the analysis (look in validator results, roundtable verdicts, and cross-team reanalysis):
1. Identify when the correction was first identified (which round/team)
2. List all findings that were produced before or without the correction
3. Check if each affected finding was re-analyzed with the correction applied
4. Flag any finding that should have been corrected but was not

Common examples:
- A geometry-aware null model identified by one team but not applied to other teams' Ripley's L results
- A batch effect correction identified in round 3 but not retroactively applied to round 1-2 findings
- A multiple testing correction added to one analysis but not to parallel analyses using the same approach

**Input:** All team `round{N}/validator_results.json`, `roundtable_verdict.json`, and `cross_team/reanalysis_d{N}.json` files

**Output:** JSON with structure:
```json
{
  "check_id": "FM-08",
  "check_name": "correction_propagation_gap",
  "status": "pass|fail|partial",
  "violations": [
    {
      "correction": "description of the correction",
      "identified_in": "team_3/round2",
      "affected_findings": ["finding_1", "finding_2", "finding_3"],
      "corrected_findings": ["finding_1"],
      "uncorrected_findings": ["finding_2", "finding_3"],
      "severity": "major|minor"
    }
  ]
}
```

---

### FM-09: Resampling Stability

**Model:** haiku

**Prompt:**
You are evaluating whether the core finding is stable under resampling of the sample composition.

Bootstrap the primary test statistic for each finding 1000 times (sampling with replacement from the available observations). Report the 95% bootstrap confidence interval of the test statistic.

For each finding:
1. Identify the primary test statistic (effect size, correlation coefficient, enrichment score, etc.)
2. Run 1000 bootstrap iterations, resampling observations with replacement
3. Compute the 2.5th and 97.5th percentiles of the bootstrap distribution
4. Check whether the CI crosses zero (for effect sizes) or spans >2 orders of magnitude (for p-values)
5. Check whether the effect size sign flips in >5% of resamples

Flag any finding whose bootstrap CI crosses zero or whose effect size sign flips in >5% of resamples.

**Input:** Findings JSON + analysis code + preprocessed data paths

**Pass criteria:** 95% bootstrap CI does not cross zero (for effect sizes) or does not span >2 orders of magnitude (for p-values)

**Fail criteria:** Bootstrap CI crosses zero or effect size sign flips in >5% of resamples

**Output:** JSON with structure:
```json
{
  "check_id": "FM-09",
  "check_name": "resampling_stability",
  "status": "pass|fail|partial",
  "violations": [
    {
      "finding": "finding description",
      "test_statistic": "effect_size|correlation|enrichment_score",
      "bootstrap_ci_lower": -0.05,
      "bootstrap_ci_upper": 0.42,
      "ci_crosses_zero": true,
      "sign_flip_fraction": 0.08,
      "severity": "major|minor"
    }
  ]
}
```

---

### FM-10: Alternative Classifier Audit

**Model:** haiku

**Prompt:**
You are evaluating whether findings that depend on cell-type classifiers are robust to alternative classifier definitions.

A finding is classifier-dependent if it involves cell-type labels, cluster assignments, or any grouping variable derived from a classification algorithm.

For each classifier-dependent finding:
1. Check whether DefinitionSweepValidator results are available in the experiment outputs
2. If available, extract the `claim_invariance_score` for the finding
3. Flag any finding with `claim_invariance_score < 0.5`
4. If DefinitionSweepValidator results are not available, set status to "skipped"

A finding with low invariance score means the result changes substantially depending on which classifier operationalization was used — it is definition-specific, not general biology.

**Input:** Findings JSON + DefinitionSweepValidator results (if available in `adversarial_review/` or `cross_team/`)

**Pass criteria:** `claim_invariance_score >= 0.5` for all classifier-dependent findings

**Fail criteria:** Any finding with `claim_invariance_score < 0.5`

**Fallback:** If DefinitionSweepValidator results are not available:
```json
{"check_id": "FM-10", "check_name": "alternative_classifier_audit", "status": "skipped", "reason": "definition_sweep_validator_not_available", "violations": []}
```

**Output:** JSON with structure:
```json
{
  "check_id": "FM-10",
  "check_name": "alternative_classifier_audit",
  "status": "pass|fail|partial|skipped",
  "reason": "only populated if skipped",
  "violations": [
    {
      "finding": "finding description",
      "classifier_type": "leiden|louvain|seurat|manual|other",
      "claim_invariance_score": 0.35,
      "severity": "major|minor"
    }
  ]
}
```

**Links to:** Package B DefinitionSweepValidator

---

### FM-11: Alternative Null Model

**Model:** haiku

**Prompt:**
You are evaluating whether each finding is robust across multiple null model assumptions.

A null model defines what the data would look like if there were no true effect. A finding that is significant under one null but not another is sensitive to null model assumptions.

For each finding:
1. Identify the null model used in the original analysis
2. Identify at least 2 alternative null models appropriate for the data type:
   - Permutation null (shuffle labels)
   - Random spatial null (random point process for spatial data)
   - Matched-sample null (match on confounders)
   - Gene-set size null (permute gene sets of the same size)
3. Re-test the finding under each alternative null
4. Flag any finding that survives only 1 of the 3 tested nulls (original + 2 alternatives)

**Input:** Findings JSON + analysis code

**Pass criteria:** Finding survives ≥2 of 3 tested nulls

**Fail criteria:** Finding only survives 1 null model

**Output:** JSON with structure:
```json
{
  "check_id": "FM-11",
  "check_name": "alternative_null_model",
  "status": "pass|fail|partial",
  "violations": [
    {
      "finding": "finding description",
      "original_null": "permutation",
      "nulls_tested": ["permutation", "random_spatial", "matched_sample"],
      "nulls_survived": ["permutation"],
      "nulls_failed": ["random_spatial", "matched_sample"],
      "severity": "major|minor"
    }
  ]
}
```

---

### FM-12: Effect-Size Shrinkage Under Stricter Controls

**Model:** haiku

**Prompt:**
You are evaluating whether reported effect sizes shrink substantially when standard confounding variables are added as covariates.

For each finding:
1. Identify the primary effect size estimate and the variables in the original model
2. Examine the data column inventory for available covariates (batch, sample type, cell cycle phase, sequencing depth, tissue region, patient metadata)
3. Re-estimate the effect size with at least one additional covariate that was not in the original model
4. Compute the fractional shrinkage: (original_effect - adjusted_effect) / original_effect
5. Flag any finding where fractional shrinkage exceeds 0.5 (>50% reduction) or the sign changes

An effect size that shrinks >50% when adding standard confounders is likely substantially confounded and the original estimate is unreliable.

**Input:** Findings JSON + analysis code + data column inventory (from AnalysisPlan or data manifest)

**Pass criteria:** Effect size retains >50% of magnitude after adding covariates

**Fail criteria:** Effect size shrinks >50% or changes sign

**Output:** JSON with structure:
```json
{
  "check_id": "FM-12",
  "check_name": "effect_size_shrinkage",
  "status": "pass|fail|partial",
  "violations": [
    {
      "finding": "finding description",
      "original_effect_size": 0.62,
      "adjusted_effect_size": 0.19,
      "covariates_added": ["batch", "sequencing_depth"],
      "fractional_shrinkage": 0.69,
      "sign_changed": false,
      "severity": "major|minor"
    }
  ]
}
```

---

### FM-13: Leave-One-Dataset-Out

**Model:** sonnet

**Prompt:**
You are evaluating whether multi-dataset findings are robust to the removal of any single contributing dataset.

This check applies only to findings that draw on more than one dataset. For single-dataset findings, set status to "skipped".

For each multi-dataset finding:
1. Identify all datasets contributing to the finding
2. For each dataset, re-compute the finding with that dataset excluded
3. Check whether the finding still holds (same direction, p < 0.05, or whatever the original significance criterion was) when each dataset is removed
4. Flag any finding that disappears when any single dataset is removed

A finding driven by a single dataset is not robust convergence evidence — it means only one dataset supports the claim and the others are neutral or contradictory.

**Input:** Findings JSON + data manifest (to identify which datasets contribute to each finding)

**Pass criteria:** Finding survives with every single dataset removed in turn

**Fail criteria:** Finding disappears when any one dataset is removed

**Fallback:** If only one dataset is available:
```json
{"check_id": "FM-13", "check_name": "leave_one_dataset_out", "status": "skipped", "reason": "single_dataset_run", "violations": []}
```

**Output:** JSON with structure:
```json
{
  "check_id": "FM-13",
  "check_name": "leave_one_dataset_out",
  "status": "pass|fail|partial|skipped",
  "reason": "only populated if skipped",
  "violations": [
    {
      "finding": "finding description",
      "datasets_contributing": ["GSE123", "GSE456", "GSE789"],
      "dataset_removed": "GSE456",
      "finding_survives": false,
      "direction_change": false,
      "severity": "major|minor"
    }
  ]
}
```

---

### FM-14: Universal Circularity Audit

**Model:** haiku

**Prompt:**
You are performing a systematic check for any gene or variable that appears in both a grouping/classifier variable and a test/outcome variable.

This is the most critical failure mode check. Circularity occurs when the same gene is used to define groups AND to measure outcomes — for example, using TGFB1 expression to classify cells into "TGFβ-high" vs "TGFβ-low" groups, then testing whether TGFB1 is differentially expressed between those groups.

For each finding:
1. Extract all genes or variables used in the classifier, grouping variable, or cluster definition
2. Extract all genes or variables used in the test or outcome measurement
3. Check for overlap between the two sets
4. Flag any finding where any gene appears in both sets

Also check the analysis code directly: look for any variable defined using a gene X that is then tested for expression of gene X.

Circularity is the #1 retracted error type across pipeline runs (responsible for 2 of 8 historical retractions).

**Input:** Findings JSON + analysis code

**Pass criteria:** No gene appears in both the grouping variable and the test variable

**Fail criteria:** Any circularity detected — any gene in the classifier AND in the measured outcome

**Output:** JSON with structure:
```json
{
  "check_id": "FM-14",
  "check_name": "universal_circularity_audit",
  "status": "pass|fail|partial",
  "violations": [
    {
      "finding": "finding description",
      "classifier_genes": ["TGFB1", "FN1", "ACTA2"],
      "outcome_genes": ["TGFB1", "CD8A", "GZMB"],
      "offending_genes": ["TGFB1"],
      "circularity_type": "direct|indirect",
      "severity": "major|minor"
    }
  ]
}
```

---

### FM-15: Measurement Commensurability

**Model:** haiku

**Prompt:**
You are evaluating whether findings being synthesized or compared were measured using compatible measurement paradigms.

Different spatial and single-cell technologies measure fundamentally different biological quantities:
- Visium (10x Genomics): spot-level averages (~10-50 cells per spot), ~3,000 genes, no single-cell resolution
- CosMx / Xenium / MERFISH: true single-cell resolution, hundreds to thousands of targeted genes
- scRNA-seq: single-cell, whole transcriptome, no spatial coordinates
- Bulk RNA-seq: tissue-level averages, whole transcriptome

Synthesizing a Visium finding with a CosMx finding as if they measure the same quantity is methodologically invalid without a bridge model that accounts for the measurement difference (e.g., deconvolution, spot-to-cell mapping).

For each cross-team or cross-dataset synthesis claim:
1. Check whether ModalityHarmonizer results are available
2. If available, extract `bridge_required` and whether bridge evidence was documented for each synthesized pair
3. Flag any synthesized pair with `bridge_required=True` and no bridge evidence
4. If ModalityHarmonizer is not available, set status to "skipped"

**Input:** Cross-team findings + ModalityHarmonizer results (if available in `cross_team/`)

**Pass criteria:** All synthesized claim pairs have `bridge_required=False` OR bridge evidence is documented

**Fail criteria:** Any synthesized pair with `bridge_required=True` and no bridge evidence

**Fallback:** If ModalityHarmonizer results are not available:
```json
{"check_id": "FM-15", "check_name": "measurement_commensurability", "status": "skipped", "reason": "modality_harmonizer_not_available", "violations": []}
```

**Output:** JSON with structure:
```json
{
  "check_id": "FM-15",
  "check_name": "measurement_commensurability",
  "status": "pass|fail|partial|skipped",
  "reason": "only populated if skipped",
  "violations": [
    {
      "claim": "exact synthesized claim text",
      "modality_a": "Visium",
      "modality_b": "CosMx",
      "bridge_required": true,
      "bridge_documented": false,
      "severity": "major|minor"
    }
  ]
}
```

---

### FM-16: Novelty Verification

**Model:** sonnet

**Prompt:**
You are evaluating whether each finding's novelty classification is accurately reflected in the paper's language.

Novelty categories:
- `consensus_recap` — recovering well-established biology (e.g., TGFB1 in fibrosis, TP53 in cancer)
- `incremental_advance` — extending known biology to a new context or with a new dataset
- `genuine_novelty` — a finding that contradicts or substantially extends current understanding with strong evidence

For each finding:
1. Check whether NoveltyBaseline results are available (from lit review or NoveltyScorerAgent)
2. If available, extract the `novelty_category` for the finding
3. Check whether the paper language matches the novelty category:
   - `consensus_recap` findings must NOT use high-novelty verbs: "discovers", "reveals", "demonstrates for the first time", "novel", "unprecedented"
   - `incremental_advance` findings must NOT use novelty language equivalent to `genuine_novelty`
   - `genuine_novelty` findings may use strong language IF the evidence supports it
4. Flag any finding where paper language exceeds its documented novelty category
5. For each flagged finding, recommend a verb ceiling (the strongest verb the finding's evidence warrants)

If NoveltyBaseline is not available, set status to "skipped".

**Input:** Findings JSON + NoveltyBaseline results (if available from lit review stage) + NoveltyScorerAgent results (if available) + synthesis text

**Pass criteria:** Finding's `novelty_category` is documented and paper language matches (no high-novelty verbs for `consensus_recap` or `incremental_advance` findings)

**Fail criteria:** Paper uses high-novelty verbs for `consensus_recap` or `incremental_advance` findings

**Fallback:** If NoveltyBaseline and NoveltyScorerAgent are both unavailable:
```json
{"check_id": "FM-16", "check_name": "novelty_verification", "status": "skipped", "reason": "novelty_baseline_not_available", "violations": []}
```

**Output:** JSON with structure:
```json
{
  "check_id": "FM-16",
  "check_name": "novelty_verification",
  "status": "pass|fail|partial|skipped",
  "reason": "only populated if skipped",
  "violations": [
    {
      "finding": "finding description",
      "novelty_category": "consensus_recap|incremental_advance|genuine_novelty",
      "offending_verb": "discovers",
      "claim_text": "exact claim text using the offending verb",
      "recommended_verb_ceiling": "confirms|extends|is consistent with",
      "severity": "major|minor"
    }
  ]
}
```

---

### Checklist Merge Output Schema

After all 16 checks complete, the main context merges results into `adversarial_review/checklist_results.json`:

```json
{
  "checks": [
    {
      "check_id": "FM-01",
      "check_name": "evidence_claim_escalation",
      "status": "pass|fail|partial|skipped|error",
      "reason": "only if skipped or error",
      "violations": [...]
    }
  ],
  "summary": {
    "total_checks": 16,
    "passed": 0,
    "failed": 0,
    "partial": 0,
    "skipped": 0,
    "error": 0,
    "major_violations": 0,
    "minor_violations": 0
  }
}
```

The main context computes the summary by counting statuses and violation severities across all checks. Skipped checks (FM-03, FM-06 when MCP unavailable; FM-10, FM-13, FM-15, FM-16 when optional validators not available) count toward `skipped`, not `failed`.

---

## §2 Reviewer Panel Assessment

Single opus dispatch. The same 5 reviewer personas from the per-round batch review now evaluate the full synthesis.

**Prompt template:**

You are a panel of 5 independent scientific reviewers. You have been reviewing this research throughout the analysis loop. Now you are evaluating the final synthesis — the culmination of all teams'  work.

Your reviewer identities:
{reviewer_personas}

Your prior review history (from the analysis loop rounds):
{round_review_history}

The synthesis to review:
{synthesis_text}

Automated failure mode checklist results (already performed):
{checklist_results}

Privileged evidence from the adversarial-mode team:
{adversarial_team_findings}
NOTE: Corrections from the adversarial-mode team are binding constraints. Evaluate whether they were adequately integrated into the synthesis. If not, raise as a major concern.

### Instructions for each reviewer persona

1. Produce your review independently, from your area of expertise
2. Identify **major concerns** — issues that MUST be addressed before this work is publishable
3. Identify **minor concerns** — issues that SHOULD be addressed for quality
4. Note **commendations** — what was done well
5. For each concern, classify the resolution type:
   - `computation` — requires new analysis on data to resolve
   - `language` — can be resolved by rewriting claims/framing
   - `informational` — noted for transparency, no action needed
6. Identify concerns the automated checklist MISSED (novel failure modes not in the 16 standard checks)

### Editorial synthesis instructions

After all 5 reviewers produce their independent reviews:
1. Deduplicate concerns across reviewers into unified lists
2. Rank unified concerns by impact (highest impact first)
3. Produce an editorial decision:
   - `accept` — no actionable issues found (rare; checklist + reviewers agree everything is sound)
   - `minor_revisions` — only language fixes and targeted reanalysis needed (Tier 0 + Tier 1 eligible)
   - `major_revisions` — significant computational response needed (all tiers eligible, including Tier 2 full round)

### Output schema

Write `adversarial_review/panel_review.json`:

```json
{
  "reviewers": [
    {
      "persona": "Reviewer Name",
      "expertise_relevant_to": "methodology or domain area",
      "major_concerns": [
        {
          "concern_id": "PR-01",
          "description": "Specific concern text",
          "affected_claims": ["claim_id_1", "claim_id_2"],
          "resolution_type": "computation|language|informational",
          "suggested_analysis": "If computation: specific test to run. If language: specific reframing needed."
        }
      ],
      "minor_concerns": [],
      "commendations": ["What was done well"]
    }
  ],
  "editorial_synthesis": {
    "decision": "accept|minor_revisions|major_revisions",
    "unified_major_concerns": [
      {
        "concern_id": "PR-01",
        "description": "Deduplicated concern",
        "affected_claims": [],
        "resolution_type": "computation|language|informational",
        "suggested_analysis": "",
        "raised_by": ["Reviewer 1", "Reviewer 3"]
      }
    ],
    "unified_minor_concerns": [],
    "priority_order": ["PR-01", "PR-03", "PR-02"]
  }
}
```

---

## §3 Tiered Response Protocol

### §3a Tier 0 — Language Fixes

**Model:** haiku (one dispatch per concern, parallel)
**Condition:** Concerns with `resolution_type == "language"`

**Prompt template:**

You are revising a specific claim in the scientific synthesis based on a reviewer concern.

Reviewer concern: {concern_description}
Affected claim text: {original_text}
Section of paper: {section}

Revise the claim text to address the concern. Rules:
- Downgrade claim strength to match evidence type if escalation was flagged
- Replace mechanism language with model language where only observational evidence exists
- Add scope qualifiers where generalization exceeds evidence base
- Do NOT add new claims or findings — only revise existing text
- Do NOT weaken claims beyond what the concern requires
- Preserve scientific accuracy while being more precise about evidence strength

**Output:** `adversarial_review/response/tier0_{concern_id}.json`:
```json
{
  "concern_id": "PR-01",
  "original_text": "TGF-β drives immune exclusion through CAF activation",
  "revised_text": "TGF-β is spatially associated with immune exclusion in regions of CAF activation, consistent with a model in which...",
  "revision_type": "language_downgrade|scope_qualification|hedging_addition",
  "section": "Results|Discussion|Abstract"
}
```

### §3b Tier 1 — Targeted Reanalysis

**Model:** sonnet (one dispatch per concern, parallel)
**Condition:** Concerns with `resolution_type == "computation"`

**Prompt template:**

You are performing a targeted reanalysis to address a specific reviewer concern about the scientific synthesis.

Reviewer concern: {concern_description}
Suggested analysis: {suggested_test}
Available preprocessed data files: {data_file_paths}
Original analysis code (for reference): {original_code_path}

Your task:
1. Write Python code that directly addresses this concern
2. Execute the code on the available data
3. Report quantitative results
4. State whether the results support or undermine the original finding
5. Update the finding's confidence level based on the results

Use the analytics toolkit: `from autonomous_science.analytics import *`
Follow figure standards: colorblind-safe, 300 DPI, Arial font, constrained_layout=True

**Output:** `adversarial_review/response/tier1_{concern_id}.json`:
```json
{
  "concern_id": "PR-03",
  "code_executed": true,
  "code_path": "adversarial_review/response/tier1_PR-03_code.py",
  "results": {
    "test_performed": "compartment-restricted permutation of L-metric",
    "original_effect_size": 0.45,
    "corrected_effect_size": 0.31,
    "p_value": 0.003,
    "interpretation": "Effect size reduced by 31% but remains significant"
  },
  "original_confidence": "supported",
  "updated_confidence": "supported",
  "figures": ["adversarial_review/response/tier1_PR-03_figures/corrected_lmetric.png"],
  "conclusion": "Original finding survives geometry correction with reduced but significant effect"
}
```

Also write the executed code to `adversarial_review/response/tier1_{concern_id}_code.py`.

### §3c Tier Escalation Check

**Model:** sonnet (single dispatch after all Tier 1 responses complete)

**Prompt template:**

You are evaluating whether Tier 1 targeted reanalysis results require a full response round (Tier 2).

Tier 1 results:
{tier1_results_summary}

Original synthesis claims:
{synthesis_claims}

Tier 2 triggers if ANY of the following are true:
1. A finding's confidence drops from `robust` or `supported` to `preliminary` or `open_question`
2. A Tier 1 reanalysis produces a result that directly contradicts a claim in the synthesis
3. A correction that was previously "projected" is now "executed" and the corrected result differs substantially (>30% effect size change) from the projection

IMPORTANT: If the editorial decision was `minor_revisions`, Tier 2 is BLOCKED regardless of these criteria. In that case, report `escalate_to_tier2: false` with reason `"blocked_by_minor_revisions_decision"`.

**Output:** `adversarial_review/response/tier_escalation.json`:
```json
{
  "escalate_to_tier2": false,
  "reason": "No major conclusion changes from Tier 1 results",
  "tier1_summary": {
    "concerns_addressed": 3,
    "confidence_changes": [
      {"finding": "...", "from": "supported", "to": "supported", "changed": false}
    ],
    "contradictions_found": 0
  },
  "affected_conclusions": []
}
```

### §3d Tier 2 — Full Response Round

**Model:** sonnet (analysis) + sonnet (validators)
**Condition:** Only if tier escalation check returns `escalate_to_tier2: true`
**Hard cap:** Maximum 1 Tier 2 round per pipeline run

**This is NOT a re-entry into the main analysis loop.** It is a standalone response round with a modified structure:
- The analysis agent receives reviewer concerns as its directive (not `next_hypotheses`)
- All Tier 1 results are provided as context
- Validators (statistical, causal, embedding, automl) run on the new findings
- PI interpretation is SKIPPED — the reviewer team serves this evaluative role
- The analysis roundtable is REPLACED by the reviewer team evaluating the response

**Prompt template (analysis agent):**

You are performing a comprehensive reanalysis round in response to adversarial review concerns that were not fully resolved by targeted reanalysis.

Reviewer concerns requiring full investigation:
{unresolved_concerns}

Tier 1 results that changed major conclusions:
{tier1_changes}

Available data: {data_paths}
Prior analysis code (for reference): {prior_code_paths}

Produce a complete analysis round:
- findings.json with all new findings
- code.py with all executed code
- figures/ with all generated figures
- round_summary.md describing what was investigated and found

Focus exclusively on the reviewer concerns. Do not re-run prior analyses that were not challenged.

**Output:** `adversarial_review/response/tier2_round/`:
```
findings.json
code.py
figures/
round_summary.md
validator_results.json  (from validator dispatch on the new findings)
```

---

## §4 Review Resolution

Single sonnet dispatch. The reviewer team confirms whether each concern was adequately addressed by the tiered response.

**Prompt template:**

You are the reviewer panel confirming whether your concerns were adequately addressed by the authors' response.

Your original concerns:
{panel_review_concerns}

Response artifacts received:
- Tier 0 (language fixes): {tier0_results}
- Tier 1 (targeted reanalysis): {tier1_results}
- Tier 2 (full response round): {tier2_results_or_not_triggered}

For each concern (major and minor), determine:

1. **`adequately_addressed: true`** — The response resolves the concern. Provide a brief summary of how.

2. **`adequately_addressed: false`** — The concern remains. Choose a disposition:
   - `flagged_as_limitation` — The concern cannot be resolved with current data/methods. Include `limitation_text` that MUST appear in the paper's Discussion/Limitations section. This text should be factual and specific, not a generic caveat.
   - `requires_further_analysis` — The concern could potentially be resolved but requires analysis beyond what the current pipeline can perform (e.g., wet-lab validation, new dataset acquisition). Include `limitation_text` as above. NOTE: This disposition blocks data cleanup.

Also produce a `revised_claim_confidence` map: for each claim whose confidence changed based on the tiered response results, map `claim_id → new_confidence_tier`. Valid tiers: `robust`, `supported`, `method_dependent`, `preliminary`, `open_question`.

**Output schema:** Write `adversarial_review/resolution.json`:

```json
{
  "resolved_concerns": [
    {
      "concern_id": "PR-01",
      "resolution_tier": 0,
      "adequately_addressed": true,
      "resolution_summary": "Claim language downgraded from 'drives' to 'is associated with'"
    }
  ],
  "unresolved_concerns": [
    {
      "concern_id": "PR-03",
      "resolution_tier": 1,
      "adequately_addressed": false,
      "disposition": "flagged_as_limitation",
      "limitation_text": "The geometry-aware null model was applied to Team Alpha's CAF-T_cell co-localization finding but not to the remaining 4 findings that used standard Ripley's L. These findings should be interpreted with caution pending formal geometry correction."
    }
  ],
  "revised_claim_confidence": {
    "tgfb_caf_cascade": "supported",
    "pan_cancer_conservation": "preliminary"
  }
}
```

---

## §5 Synthesis Revision

Single sonnet dispatch. Integrates all response artifacts into a revised synthesis document that Phase 4 (paper generation) will use as its primary input.

**Prompt template:**

You are revising the scientific synthesis based on the results of adversarial review.

Original synthesis:
{original_synthesis_text}

Review resolution:
{resolution_json}

Tier 0 language fixes (revised claim text):
{tier0_responses}

Tier 1 reanalysis results (new quantitative findings):
{tier1_responses}

Tier 2 response round findings (if triggered):
{tier2_findings_or_not_triggered}

Updated claim confidence map:
{revised_claim_confidence}

### Revision instructions

1. **Apply all Tier 0 language fixes**: Replace original claim text with revised text from each `tier0_{concern_id}.json`
2. **Incorporate Tier 1 findings**: Where targeted reanalysis produced new results, add them to the appropriate Results section. If a reanalysis changed a finding's confidence, update the language accordingly.
3. **Incorporate Tier 2 findings**: If a full response round was triggered, integrate its findings into the synthesis.
4. **Add ALL limitations**: Every `limitation_text` from `unresolved_concerns` in `resolution.json` MUST appear in a Limitations subsection. Do not soften, generalize, or omit any limitation text.
5. **Update confidence language**: Use the `revised_claim_confidence` map to adjust language throughout:
   - `robust` → "strongly supported by", "demonstrated that"
   - `supported` → "supported by", "consistent with"
   - `method_dependent` → "observed using [method], pending replication with alternative approaches"
   - `preliminary` → "preliminary evidence suggests", "initial analysis indicates"
   - `open_question` → "remains an open question whether", "further investigation is needed to determine"
6. **Preserve IMRAD structure**: The revised synthesis must maintain Introduction, Methods, Results, Discussion sections.
7. **Maintain an audit trail**: Record every revision in the `revisions` array.

**Output schema:** Write `adversarial_review/revised_synthesis.json`:

```json
{
  "original_synthesis_path": "final/synthesis_paper.md",
  "revisions": [
    {
      "concern_id": "PR-01",
      "section": "Results",
      "original_text": "TGF-β drives immune exclusion through CAF activation",
      "revised_text": "TGF-β spatial co-localization with CAF activation markers is consistent with a model in which...",
      "revision_type": "language_downgrade"
    },
    {
      "concern_id": "PR-03",
      "section": "Discussion",
      "original_text": "",
      "revised_text": "The geometry-aware null model was applied to...",
      "revision_type": "limitation_added"
    }
  ],
  "added_limitations": [
    "The geometry-aware null model was applied to Team Alpha's CAF-T_cell co-localization finding but not to the remaining 4 findings that used standard Ripley's L."
  ],
  "updated_claim_confidence": {
    "tgfb_caf_cascade": "supported",
    "pan_cancer_conservation": "preliminary"
  },
  "new_findings": [
    "Tier 1 reanalysis of CAF-T_cell co-localization with compartment-restricted null: effect size reduced from 0.45 to 0.31 (p=0.003)"
  ],
  "revised_synthesis_md": "# Full revised synthesis markdown\n\n## Introduction\n..."
}
```

The `revised_synthesis_md` field must contain the COMPLETE revised synthesis as markdown text. Phase 4 paper generation uses this field as its primary input.

Valid `revision_type` values: `language_downgrade`, `scope_qualification`, `new_result_added`, `confidence_change`, `limitation_added`, `hedging_addition`
