# Analysis Round Reference

> Included in dispatch prompts sent to analysis agents and roundtable panels.
> Describes what each participant must produce and what quality standards apply.

---

## 1. Analysis Round Contract

Every analysis round must produce the following artifacts:

- **`findings.json`** — structured findings with statistics, effect sizes, biological interpretation, and figure references
- **`code.py`** — the executed Python script (full, reproducible, no omissions)
- **`figures/`** — one figure per finding minimum; vector PDF format + 300 DPI PNG preview
- **`round_summary.md`** — human-readable narrative: what was tested, what was found, what remains open

Every finding in `findings.json` must include: statistical test used, test statistic, p-value (raw and corrected), effect size, biological interpretation, and a reference to at least one figure. Findings without biological interpretation or without a corresponding figure are incomplete.

---

## 2. Analytics Toolkit

ALL analysis code MUST use the `autonomous_science.analytics` toolkit (25 modules, 100+ functions).

- **Import**: `from autonomous_science.analytics import *` — auto-applies the publication figure theme (colorblind-safe palette, Arial font, 300 DPI) and exports all analysis functions
- **API reference**: `prompts/api_reference.md` documents every function signature, parameter, and return type — consult it before writing code
- **Reimplementation is forbidden**: If the toolkit has a function for a statistical test, use it instead of raw scipy/statsmodels. The toolkit wraps scipy with built-in multiple testing correction, effect size computation, and standardized JSON output. Writing `from scipy.stats import ttest_ind` when `compare_two_groups()` exists is a reproducibility failure.

Key modules: `comparison` (t-tests, ANOVA), `correlation`, `survival` (KM, Cox), `enrichment` (Fisher, GSEA), `dimensionality` (PCA, UMAP), `clustering`, `regression`, `bootstrap`, `network`, `timeseries`, `bayesian`, `rnaseq`, `spatial`, `multiomics`, `roc`, `power`

---

## 3. Science Focus Modes

The `science_focus` mode determines the analytical emphasis and minimum rounds required.

### discovery
**Goal**: Find unexpected biology. Characterize broadly first, then pursue anomalies.

- Emphasize breadth in Round 1: multiple analysis angles, pathway enrichment on all significant gene lists, anomaly flagging
- Prioritize surprising findings over confirmatory ones in later rounds
- Minimum 4 rounds: landscape → enrichment → mechanisms → corroboration
- Convergence before Round 4 is never acceptable

### validation
**Goal**: Rigorously test a pre-specified hypothesis.

- Round 1 executes the primary test with full statistical rigor
- Round 2 replicates in a different subset, cohort, or method
- Round 3+ addresses sensitivity and boundary conditions
- Minimum 3 rounds: primary test → replication → sensitivity
- Convergence before Round 3 is never acceptable

### methods
**Goal**: Benchmark and characterize an analytical method across conditions.

- Round 1 establishes baseline performance
- Rounds 2–3 sweep parameters and compare alternatives
- Rounds 4–5 generalize across datasets and probe edge cases
- Minimum 5 rounds: baseline → sweep → benchmark → generalise → edge cases
- Convergence before Round 5 is never acceptable

---

## 4. Domain Skill References

When the research question matches a known domain, a domain skill protocol is included in the dispatch prompt under the heading `## DOMAIN ANALYSIS PROTOCOL`. Follow it exactly — it contains the required steps, imports, quality checks, and common pitfalls for that analysis type.

Domain matching:

| Keywords in question/hypothesis | Skill |
|--------------------------------|-------|
| scL / L metric / Lorenz curve / gene co-expression matrix | `scl_metric.md` |
| RNA-seq / differential expression / count matrix / DEG | `bulk_rnaseq.md` |
| clustering / cell type / UMAP / t-SNE / PCA | `clustering_analysis.md` |
| treatment vs control / condition comparison | `cross_condition.md` |
| spatial / Visium / MERFISH / Moran / neighborhood | `spatial_analysis.md` |
| time series / longitudinal / trajectory / developmental stage | `timeseries.md` |
| multi-omics / proteomics / metabolomics / MOFA | `multi_omics.md` |
| survival / Kaplan-Meier / Cox regression / hazard ratio | `survival_analysis.md` |
| protein interaction / PPI / network centrality / hub gene / STRING | `network_pathway.md` |
| somatic mutation / TMB / oncoplot / driver gene / VAF / CNA | `mutation_analysis.md` |
| GWAS / Manhattan plot / SNP / eQTL / Mendelian randomization | `gwas_genetic.md` |
| scRNA-seq / single-cell RNA / 10x Genomics / h5ad / pseudobulk / Leiden | `scrna_seq.md` |

In Round 2+ dispatches, the full skill is condensed to its critical rules only (lines containing "must", "never", "always", "critical", "mandatory", "required", "forbidden" — capped at 15 rules).

---

## 5. Round Progression

### Round 1 — Characterization and Enrichment

- Execute primary hypothesis tests from the analysis plan
- Compute descriptive statistics and profile all data inputs
- **Mandatory pathway enrichment on ALL significant gene lists** — do not defer this to later rounds; `get_functional_enrichment` (STRING) and `analyze_gene_list_pathways` (multi-DB) are the primary endpoints
- Annotate every finding biologically — what does it mean for the cell or organism?
- Flag anomalies: distributions, outliers, unexpected patterns
- Generate sub-hypotheses for follow-up

### Round 2 — Biological Investigation

Driven by the PI Interpretation Roundtable's `sub_hypotheses`, `proposed_followups`, and the Analysis Roundtable's `next_approach`:

- Test the sub-hypotheses generated in Round 1
- Pursue surprise findings from the PI panel
- Deepen mechanistic understanding: what pathway, what cell type, what regulatory logic?
- Cross-validate predictions against independent signals in the data

### Rounds 3+ — Hypothesis Testing and Corroboration

Each round must produce genuinely new biological insight, not just statistical sensitivity checks:

- Test mechanistic model predictions
- Validate findings across subgroups, conditions, or methods
- Pursue divergent threads flagged as DIVERGENT by prior roundtables
- Ensure key findings have corroborating evidence from at least one independent signal: a second data type (e.g., expression + clinical/pathway/protein), a second statistical method, or cross-validation in a different subset or cohort

**Round 2+ dispatch format — DUAL-TRACK**:

```
### PRIMARY INVESTIGATION (~70% of analysis code)
Hypothesis: [from roundtable's next_hypotheses or open_questions]
Expected analyses: [from next_approach]

### PERIPHERAL EXPLORATION (~30% of analysis code)
One tangential direction to ensure breadth:
[from PI roundtable's peripheral_suggestions]
Options: different cell type/tissue, different pathway, potential confounders
Peripheral explorations often yield the most novel discoveries.

### REVIEWER CONCERNS (must address ALL major concerns)
{reviewer_concerns_for_this_team}

For each concern from the reviewer team, you must either:
(a) Perform the suggested analysis and report results in your findings
(b) Provide evidence the concern is already addressed by existing findings
(c) Explicitly acknowledge as a limitation of the current analysis

Record your response to each concern in the `concern_responses` array of `findings.json`.
If this is Round 1 (no prior reviewer concerns exist), this section is empty and `concern_responses` should be an empty array.

### DO NOT REPEAT
[cumulative list of hypotheses already tested in prior rounds]
```

---

## 6. Code Execution Requirements

Analysis code runs in a sandboxed subprocess with:

- Environment sanitization (no API keys, no network access)
- Resource limits (CPU, memory)
- Filesystem scoping to the experiment output directory
- Output size cap (stdout/stderr)
- Timeout enforced

**Data by reference**: Never embed raw data in analysis code. Reference file paths from the data manifest. The analysis planner produces preprocessed Parquet files with a verified column inventory — use column names exactly as they appear in the inventory. Hallucinated column names cause execution failures.

**Adversarial robustness** — for top-3 findings, execute these 5 checks before reporting:

1. Cell-type stratification: does the finding hold within individual cell types?
2. Donor/batch blocking: does it persist after accounting for technical variation?
3. Alternative statistical method: does the direction and significance hold under a different test?
4. Matched negative control: does a plausible negative control show no effect?
5. Trajectory robustness: is the finding stable across reasonable parameter choices?

Findings failing 2 or more of these checks are downgraded to "preliminary".

**MCP tools** are available for pathway enrichment and gene annotation:
`get_functional_enrichment`, `get_protein_interactions`, `analyze_gene_list_pathways`,
`get_pathways_for_gene`, `search_reactome_pathways`, `get_reactome_pathway`,
`get_hpa_gene_info`, `get_tissue_expression`, `get_subcellular_location`,
`get_target_associations`, `get_target_drugs`, `get_chembl_target_by_gene`,
`search_open_targets`, `search_gwas_associations`

Using these tools for pathway enrichment is mandatory for any significant gene list. Relying on the LLM's training-time knowledge for pathway membership is unreliable and not acceptable.

---

## 7. Statistical Rigor

### Multiple testing
Apply Benjamini-Hochberg FDR correction across ALL p-values in the round. Use `correct_findings()` from `analytics.correction`. Report both raw and corrected p-values. A finding with only a raw p-value is incomplete.

### Effect sizes
Required for every comparison: Cohen's d for continuous outcomes, OR/RR for binary, hazard ratio for survival, R² for regression. Flag implausible effect sizes (Cohen's d > 2.0, |r| > 0.8) — these usually indicate data problems.

### Test assumptions
Verify normality before applying parametric tests. Use `check_normality()` from `analytics.descriptive`. Applying a t-test to a clearly non-normal distribution is a methodological error.

### Power
Minimum 0.8 power required to claim a null result is meaningful. Use `analytics.power` when interpreting non-significant findings.

### Null model sensitivity
For each finding with p < 0.05, test under 2–3 alternative statistical methods:
- If primary is parametric (t-test): also run Wilcoxon rank-sum + permutation test
- If primary is correlation (Pearson): also run Spearman + Kendall
- If primary is survival (log-rank): also run Cox regression + Gehan-Breslow

Classify sensitivity:
- **STABLE**: direction consistent, all p < 0.05
- **MODERATELY_SENSITIVE**: direction consistent, some p > 0.05 — report as WARNING
- **FRAGILE**: direction reverses in any method, or all alternative p > 0.10 — auto-downgrade finding to "preliminary"

Report `null_sensitivity_class` per finding in `findings.json`.

### Novelty null model
The novelty assessor's surprise score is grounded in a Monte Carlo null distribution computed during lit review: 100 random gene-set pairs sampled from the topic gene universe, Jaccard computed for each. The mean and standard deviation of this distribution form the null model. Observed Jaccard overlap between a finding's genes and canonical pathway genes is converted to a z-score, then to a pseudo-p-value via the normal CDF. This answers: "is this gene-set convergence more than expected by chance?"

Surprise score interpretation:
- `< 0.2`: consensus_recap — well-established biology
- `0.2–0.4`: incremental_advance — modest extension of known findings
- `0.4–0.7`: unexpected_convergence — warrants deeper investigation
- `> 0.7`: genuine_discovery — MUST be prioritized in follow-up rounds

### P-hacking
Do not report multiple threshold cuts (p < 0.05, p < 0.10) as if they are independent results. Pre-specify the primary test and threshold per sub-analysis.

---

## 8. Biological Interpretation

Every finding must include a biological interpretation — not just the statistical result. A finding that reports only "gene X is differentially expressed (p=0.003)" is incomplete.

Biological interpretation must address:

- **Mechanism**: what biological process does this finding implicate? What is the regulatory, signaling, or metabolic logic?
- **Pathway context**: what pathway does this connect to? What are the upstream and downstream partners?
- **Cell or tissue specificity**: does this occur in all cell types or specific populations?
- **Disease or developmental relevance**: how does this connect to the research question?
- **Surprise or novelty**: is this expected given the literature, or is it unexpected? If unexpected, why might it occur?

For gene lists, pathway enrichment is not optional — it is the minimum required biological annotation. Enrichment results must be interpreted (what does the enriched pathway mean for the phenotype?), not just listed.

Sub-hypotheses: every round should generate at least one testable sub-hypothesis derived from the findings, formatted as "If [mechanism], then [measurable prediction] in [specific data]."

---

## 9. Validator Expectations

Non-LLM algorithmic validators run after every round. Anticipate their checks:

**Statistical Validator**: BH FDR correction, effect size plausibility, p-hacking detection, test-assumption consistency, null model sensitivity. Produces `overall_reliability_score`.

**Causal Validator**: Detects causal language ("X causes Y", "X drives Y") and grades it. Expression-only evidence supports associative language, not causal. Flags findings that claim causation without perturbation evidence.

**Embedding Miner**: TF-IDF cosine similarity across findings. Flags redundant findings (similarity > 0.8) and uncovered sub-hypotheses (max cosine < 0.3). Avoid producing near-identical findings under different labels.

**AutoML Validator**: AST scan for feature selection, cross-validation, random seeds, effect size reporting, multiple comparison correction. All of these should be present in production analysis code.

**Evidence-Typing Validator**: Classifies each finding's evidence type and assigns a conclusion ceiling:
- Expression-only → max "candidate_pathway"
- Expression + chromatin → max "regulatory_association"
- Perturbation required for "mechanism" claims
- Perturbation + clinical/genetic required for "therapeutic_target"

Write conclusions that match the evidence. Claiming "mechanistic" conclusions from expression data alone will be flagged as a CRITICAL violation.

**Evidence Ceiling Feedback Loop**: When the evidence-typing validator flags ceiling violations, it generates resolution directives that are injected into the next round's dispatch as `evidence_ceiling_directives`. These are MANDATORY — the analysis agent must either collect additional evidence to raise the ceiling or downgrade claim language to match. Unresolved CRITICAL violations block convergence.

**Comparator Auditor**: Flags cross-species, cross-cell-type, and cross-developmental-stage comparisons when confounders are not acknowledged. Cross-species + cross-cell-type together is CRITICAL.

**Contradiction Retriever**: Generates negation-oriented literature search queries for top findings and scores contradiction potential (0-1 scale). In the API pipeline, runs per-iteration in plan-only mode and injects `contradiction_directives` into the next round. In the local pipeline, a dedicated subagent (Step 2b) runs full MCP-backed searches in round 2+. Findings with `contradiction_score > 0.3` receive confidence adjustment recommendations: `minor_downgrade` (0.2-0.4), `major_downgrade` (0.4-0.7), or `finding_challenged` (>0.7). The analysis agent must address each flagged finding — either qualify claim language or explain why counter-evidence doesn't apply.

**Novelty Assessor**: Two-layer novelty scoring. Layer 1: TF-IDF cosine similarity between findings and lit review key findings — flags overclaimed novelty language. Layer 2: Jaccard-based surprise score using a Monte Carlo null model from the lit review (see "Novelty null model" above). The z-score approach compares observed gene-set overlap against the random baseline distribution, producing a pseudo-p-value. Findings with `surprise_score > 0.7` are classified as `genuine_discovery` and injected into the next round as `high_surprise_findings` — these MUST be investigated in subsequent analysis rounds. Do not use "novel" or "first" language for findings with `surprise_score < 0.4`.

When algorithmic validator evidence contradicts an LLM finding, the algorithmic evidence takes precedence.

**Violation resolution**: If the evidence-typing validator flags a CRITICAL violation (conclusion exceeds evidence ceiling), the next round must address it. Resolution options in priority order: (1) collect additional evidence that raises the ceiling, (2) reframe the conclusion to match the current ceiling. Report outcome in `findings.json` as `"violation_resolved": "V{N}"` and `"resolution_method": "upgraded|downgraded|acknowledged"`.

---

## 10. PI Interpretation Panel

The PI Interpretation Roundtable is a panel of PI personas (team researchers + 3 standing archetypes: Mechanistic Biologist, Translational Researcher, Systems Biologist). They conduct a lab-meeting-style discussion about what the findings mean biologically.

The panel produces a `BiologicalInterpretation` object with:

- `mechanism_explanations`: for each major finding, the biological mechanism it implicates
- `surprise_findings`: results that were unexpected given prior hypotheses or the literature — these deserve priority investigation in the next round
- `sub_hypotheses`: testable sub-hypotheses with specific test proposals and expected outcomes
- `proposed_followups`: concrete analyses for the next round (method, data, expected result)
- `mechanistic_model`: an integrated narrative connecting the findings into a coherent biological story
- `panel_agreement`: float 0–1 quantifying consensus level across panelists
- `dissenting_views`: substantive disagreements among panelists (not token objections)
- `data_gaps`: list of strings in the format "To publish [X], we would need [Y] data showing [Z]"
- `peripheral_suggestions`: tangential directions for breadth exploration in the next round

The PI panel's `sub_hypotheses` and `proposed_followups` are REQUIRED inputs for the Analysis Roundtable. Without them, the roundtable lacks biological context and produces statistically-focused verdicts that miss biological discovery.

---

## 11. Analysis Roundtable

The Analysis Roundtable debates biological significance (primary) and statistical rigor (secondary). It receives the full round results, prior round results, the PI panel's `BiologicalInterpretation`, and the algorithmic validator results as ground-truth constraints.

The roundtable produces an `AnalysisRoundtableVerdict` with:

- `converged`: boolean — true only if all convergence criteria are met (see below)
- `validated_findings`: findings endorsed by the panel
- `refuted_findings`: findings rejected, with reasons
- `falsified_hypotheses`: hypotheses that were tested and disproven this round, each with `{hypothesis, evidence_against, what_was_learned}`. Falsification is a positive outcome — it narrows the hypothesis space and should be reported as such.
- `blocked_tests`: tests that could not be executed this round due to data, panel, or power limitations, each with `{test, reason_blocked, resolution}`. If resolution is achievable next round, flag as `"escalation": "CRITICAL_NEXT_ROUND"`.
- `open_questions`: list of unresolved questions
- `next_hypotheses`: list of Hypothesis objects for the next round
- `next_approach`: statistical plan for the next round
- `next_threads`: list of ThreadSpec objects, each classified as:
  - `"type": "CRITICAL_PATH"` — sequential dependency that gates downstream interpretation; must complete before divergent threads can be validated
  - `"type": "DIVERGENT"` — parallel exploration that can run independently
  - Never label sequential dependencies as "consensus" — use CRITICAL_PATH
- `cumulative_validated_findings`: all validated findings across all rounds
- `data_needs`: additional data that would strengthen findings, each with `{description, source_category, justification, priority ("critical"/"strengthening"), suggested_tool}`
- `strongest_objection`: the single strongest objection to the current findings — must be specific and testable, state what evidence would refute the team's primary claim, and be at least 50 characters long (e.g., "If compartment-restricted permutation of cell positions eliminates the L-metric co-localization signal for CAF-T_cell pairs, then the co-localization is an artifact of tissue geometry rather than biological interaction.")

### Self-Probe: Strongest Objection

Before voting on convergence, the roundtable must articulate the single strongest objection to the current findings in the `strongest_objection` field of `roundtable_verdict.json`. This must be:
- Specific and testable — not a vague caveat like "more data needed"
- State what evidence, if found, would refute the team's primary claim
- At least 50 characters long

Example: "If compartment-restricted permutation of cell positions eliminates the L-metric co-localization signal for CAF-T_cell pairs, then the co-localization is an artifact of tissue geometry rather than biological interaction."

The next round's analysis agent receives this objection and must address it as a primary investigation target.

### Convergence Criteria

The roundtable may vote CONVERGED only after the mode-specific minimum rounds AND only if ALL of the following are true:

1. Mechanistic explanation exists for all major findings and has been tested through sub-hypotheses
2. Pathway enrichment performed and interpreted for all significant gene lists
3. At least one surprising or novel finding identified and investigated
4. Findings connect into a coherent biological narrative that addresses the research question
5. Key findings have corroborating evidence from at least one independent signal: a second data type, a second method, or cross-validation in a different subset or cohort. A single uncorroborated finding from one method is not publishable.
6. Statistical rigor confirmed: appropriate tests, assumptions verified, multiple testing corrected, effect sizes reported
7. No CRITICAL flags remain from algorithmic validators that are unaddressed
8. No unresolved major reviewer concerns from the current round (if adversarial review is enabled and a reviewer team exists). Major concerns that were `disputed` or `acknowledged_limitation` without strong justification count as unresolved.

Evidence-typing violation resolution: if an unresolved CRITICAL violation directive exists (`priority: MUST_RESOLVE_THIS_ROUND`), convergence is blocked regardless of other criteria.

Early convergence is blocked by mode:
- `discovery`: Rounds 1–3 convergence blocked; Round 4+ may converge
- `validation`: Rounds 1–2 convergence blocked; Round 3+ may converge
- `methods`: Rounds 1–4 convergence blocked; Round 5+ may converge

---

## 12. Output Schemas

### findings.json

```json
{
  "round": 2,
  "findings": [
    {
      "index": 0,
      "description": "...",
      "test": "Wilcoxon rank-sum",
      "test_statistic": 423.5,
      "p_value_raw": 0.0021,
      "p_value_corrected": 0.0063,
      "effect_size": {"metric": "cohen_d", "value": 0.74},
      "null_sensitivity_class": "STABLE",
      "biological_interpretation": "...",
      "pathway_context": "...",
      "figure": "figures/fig_02_wilcoxon_comparison.pdf",
      "novelty": "incremental",
      "surprise_score": 0.35,
      "anomaly": false,
      "confidence_level": 0.82,
      "evidence_types": ["expression"],
      "conclusion_ceiling": "candidate_pathway",
      "violation": false,
      "sub_hypotheses": ["If X regulates Y, then silencing X reduces Z in data D"],
      "adversarial_checks_passed": 4,
      "adversarial_checks_total": 5
    }
  ],
  "methods_used": ["Wilcoxon rank-sum", "BH FDR correction", "STRING enrichment"],
  "open_questions": ["..."],
  "concern_responses": [
    {
      "concern_id": "R1-T1-01",
      "disposition": "addressed|disputed|acknowledged_limitation",
      "response": "Brief explanation of how this concern was handled",
      "evidence": "Reference to specific analysis result or figure that addresses it"
    }
  ]
}
```

### Concern Response Tracking

Each round's `findings.json` must include a `concern_responses` array:

```json
{
  "findings": [...],
  "concern_responses": [
    {
      "concern_id": "R1-T1-01",
      "disposition": "addressed|disputed|acknowledged_limitation",
      "response": "Brief explanation of how this concern was handled",
      "evidence": "Reference to specific analysis result or figure that addresses it"
    }
  ]
}
```

- Round 1: `concern_responses` is an empty array `[]` (no prior concerns exist)
- Round 2+: Must have one entry per concern from the prior round's reviewer feedback for this team
- Missing entries for major concerns will be flagged by the reviewer in the next round

### roundtable_verdict.json

```json
{
  "round": 2,
  "converged": false,
  "validated_findings": [0, 2],
  "refuted_findings": [{"index": 1, "reason": "effect reversed under batch correction"}],
  "falsified_hypotheses": [
    {"hypothesis": "...", "evidence_against": "...", "what_was_learned": "..."}
  ],
  "blocked_tests": [
    {"test": "...", "reason_blocked": "...", "resolution": "...", "escalation": "CRITICAL_NEXT_ROUND"}
  ],
  "open_questions": ["..."],
  "next_hypotheses": [{"text": "...", "rationale": "...", "testable_prediction": "..."}],
  "next_approach": {"primary_test": "...", "data_needed": "...", "expected_output": "..."},
  "next_threads": [
    {"description": "...", "type": "CRITICAL_PATH"},
    {"description": "...", "type": "DIVERGENT"}
  ],
  "cumulative_validated_findings": [0, 2],
  "data_needs": [
    {
      "description": "...",
      "source_category": "clinical",
      "justification": "...",
      "priority": "critical",
      "suggested_tool": "get_cbioportal_clinical_data"
    }
  ],
  "strongest_objection": "If compartment-restricted permutation of cell positions eliminates the L-metric co-localization signal for CAF-T_cell pairs, then the co-localization is an artifact of tissue geometry rather than biological interaction."
}
```

### pi_interpretation.json

```json
{
  "round": 2,
  "mechanism_explanations": [{"finding_index": 0, "mechanism": "..."}],
  "surprise_findings": [{"finding_index": 2, "description": "...", "why_surprising": "..."}],
  "sub_hypotheses": [
    {"text": "...", "test_proposal": "...", "expected_outcome": "..."}
  ],
  "proposed_followups": [{"analysis": "...", "data": "...", "expected_result": "..."}],
  "mechanistic_model": "...",
  "panel_agreement": 0.78,
  "dissenting_views": [{"persona": "Translational Researcher", "view": "..."}],
  "data_gaps": ["To publish X, we would need Y data showing Z"],
  "peripheral_suggestions": ["..."]
}
```

---

## 13. Analysis Branching (Multi-Hypothesis Parallel Investigation)

When `branching_analysis_enabled` is True (default), the analysis loop can pursue multiple sub-hypotheses as independent branches. The roundtable decides branching after each iteration.

### How it works

1. **Primary branch** is initialized from the main hypothesis
2. After Round 1, the roundtable may spawn new branches for surprise findings or sub-hypotheses
3. Each branch maintains its own cumulative findings, code, and iteration context
4. Active branches are dispatched in parallel via `per_run_inputs` to `_run_stage`
5. A shared roundtable evaluates ALL branches each iteration and decides: **continue**, **prune**, **merge**, or **converge**

### Branch decisions

- **continue**: Branch is making progress toward the narrative — run another round
- **prune**: Branch diverged from the central question — archive findings as supplementary
- **merge**: Branch findings strengthen another branch — fold findings into the merge target
- **converge**: Branch answered its sub-hypothesis — lock findings

### Narrative coherence test

Before spawning a new branch, the roundtable must answer: "How does this sub-hypothesis connect back to the main research question?" If it cannot articulate the connection in one sentence, the idea is logged as "future work" instead.

### Budget control

- **`branch_cost_budget`** (default $2.00): Maximum additional LLM cost for all branches combined
- **`max_analysis_branches`** (default 3): Maximum concurrent branches including the primary thread
- **`max_branch_rounds`** (default 3): Maximum rounds per branch before forced convergence
- When budget is exhausted, no new branches spawn but existing branches continue to their next convergence point

### For skill-orchestrated runs (run-pipeline-local)

When orchestrating via subagents (not the real Python pipeline), branching must be controlled at the dispatch level:

1. After the Round 1 roundtable verdict, check `verdict.next_hypotheses` for sub-hypotheses worth branching
2. If branching, dispatch parallel analysis subagents — one per branch — with separate iteration contexts
3. Feed all branch results into a single roundtable call that includes the branch allocation prompt
4. Apply the roundtable's `BranchAllocation` decisions before the next iteration
5. Write `{team}/branch_status.json` per team with active branches, costs, and narrative scores
6. Round N+1 dispatches one parallel analysis subagent per active branch; branch-specific `iteration_context` injected

The real Python pipeline (`autosci run --provider claude_code`) handles this automatically via `BranchManager` in `graph.py`.

### Branch status schema

`{team}/branch_status.json`:

```json
{
  "round": 2,
  "active_branches": [
    {
      "branch_id": "primary",
      "hypothesis": "Main research hypothesis text",
      "parent": null,
      "round_within_branch": 2,
      "cumulative_cost_usd": 0.73,
      "narrative_score": 0.82,
      "decision": "continue"
    },
    {
      "branch_id": "b1",
      "hypothesis": "Sub-hypothesis spawned from surprise finding in R1",
      "parent": "primary",
      "round_within_branch": 1,
      "cumulative_cost_usd": 0.31,
      "narrative_score": 0.71,
      "decision": "continue"
    }
  ],
  "pruned": [],
  "merged": [],
  "converged": [],
  "total_cost_usd": 1.04,
  "budget_remaining_usd": 0.96
}
```

### Interaction with convergence criteria

Convergence is only reached when:
- The roundtable votes converged for the team AND
- All active branches for that team are in `converged`, `pruned`, or `merged` state
- The `max_branch_rounds` cap has been respected for every branch
