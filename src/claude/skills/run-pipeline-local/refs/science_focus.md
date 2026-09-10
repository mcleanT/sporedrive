# Science Focus & Domain Skill Injection
> Read this file before dispatching Stage 6 Round 1 analysis subagents.
> Domain skill injection is MANDATORY — failure to inject causes reimplementation of algorithms.

---

## Science Focus Prompt Injection

Before dispatching each stage subagent, the main context MUST inject the mode-specific
prompt block from `src/autonomous_science/orchestrator/science_focus.py`.

### How to Inject

1. At pipeline start, read `src/autonomous_science/orchestrator/science_focus.py`
2. Extract the `_BLOCKS["{mode}"]["{stage}"]` entry for the configured mode and stage
3. Prepend this block to the subagent's system prompt (before task-specific instructions)
4. If the stage's system prompt contains `{focus_block}`, replace it with the extracted block

### Affected Stages (all 8 that have `{focus_block}` placeholders)

`hypothesis_gen`, `approach_gen`, `analysis`, `analysis_planner`,
`analysis_roundtable`, `scientific_interpreter`, `figure_composition`, `paper_gen`

### Quick Reference Table (read actual blocks from `science_focus.py` for full prompt text)

| Mode | Stage | Key Emphasis |
|------|-------|-------------|
| discovery | hypothesis_gen | Mechanistic novelty, pathway implications, penalise purely descriptive |
| discovery | approach_gen | Maximise mechanistic resolution, include pathway enrichment step |
| discovery | analysis | Rounds: landscape → pathway enrichment → mechanistic sub-hypotheses |
| discovery | analysis_planner | Phase 1 QC → Phase 2 differential → Phase 3 enrichment → Phase 4 network |
| discovery | analysis_roundtable | Convergence = mechanistic explanation + pathway evidence + sub-hypothesis tested |
| discovery | scientific_interpreter | Focus on new mechanisms, pathways, paradigm-shifting aspects |
| discovery | figure_composition | Panels: landscape → discovery → enrichment → network diagram |
| discovery | paper_gen | Discovery narrative, iterative deepening as methodological contribution |
| validation | hypothesis_gen | Precisely falsifiable, null models, effect-size expectations |
| validation | approach_gen | Pre-register tests, held-out cohort, multiple-testing correction at design stage |
| validation | analysis | BH correction, effect sizes + CIs, held-out replication, permutation nulls |
| validation | analysis_roundtable | Convergence = FDR survives + replication done + no p-hacking evidence |
| validation | paper_gen | Rigorous validation study, effect sizes before p-values, replication section |
| methods | hypothesis_gen | Motivate methodological innovation, benchmarking hypotheses |
| methods | approach_gen | ≥3 datasets, ablation/parameter sweep, baseline comparison, performance metrics |
| methods | analysis | ≥3 datasets, parameter sweeps, benchmark baselines, runtime/memory profiling |
| methods | analysis_roundtable | Convergence = benchmark + parameter sweep + generalisation + failure modes |
| methods | paper_gen | Methods paper structure, algorithmic description, parameter sensitivity section |

### Scoring Weights Per Mode

Include in roundtable/evaluation dispatches:
- `discovery`: biological_insight (2×), discovery_value (2×), all others 1×
- `validation`: statistical_validity (2×), reproducibility (2×), all others 1×
- `methods`: novelty (2×), coverage (2×), all others 1×

Include in the Analysis Roundtable dispatch prompt:
> "Score findings using these dimension weights: {weights for configured mode}.
> Dimensions with weight 2.0 are the PRIMARY convergence criteria for this mode."

### Minimum Iteration Floors Per Mode

| Mode | Min Rounds | Round Progression |
|------|-----------|------------------|
| discovery | 4 | landscape → enrichment → mechanisms → corroboration |
| validation | 3 | primary test → replication → sensitivity |
| methods | 5 | baseline → sweep → benchmark → generalise → edge cases |

Early convergence before the mode-specific minimum is NEVER acceptable:
- `discovery`: Rounds 1-3 convergence BLOCKED
- `validation`: Rounds 1-2 convergence BLOCKED
- `methods`: Rounds 1-4 convergence BLOCKED

---

## MANDATORY: Domain Skill Injection for Analysis Subagents

Before dispatching each Round 1 analysis subagent, the main context MUST:

### Step 1 — Select the Matching Skill

Scan the research question, hypothesis, and approach for domain keywords:

| Keywords | Skill file |
|----------|-----------|
| scL / L metric / Lorenz curve / gene co-expression matrix / Triandafillou | `scl_metric.md` |
| RNA-seq / differential expression / count matrix / DEG | `bulk_rnaseq.md` |
| clustering / cell type / UMAP / t-SNE / PCA | `clustering_analysis.md` |
| treatment vs control / condition comparison | `cross_condition.md` |
| spatial / Visium / MERFISH / Moran / neighborhood | `spatial_analysis.md` |
| time series / longitudinal / trajectory / developmental stage | `timeseries.md` |
| multi-omics / proteomics / metabolomics / MOFA | `multi_omics.md` |
| survival / Kaplan-Meier / Cox regression / hazard ratio / OS_MONTHS | `survival_analysis.md` |
| protein interaction / PPI / network centrality / hub gene / STRING | `network_pathway.md` |
| somatic mutation / TMB / oncoplot / driver gene / VAF / CNA | `mutation_analysis.md` |
| GWAS / Manhattan plot / SNP / eQTL / Mendelian randomization | `gwas_genetic.md` |
| scRNA-seq / single-cell RNA / 10x Genomics / h5ad / pseudobulk / Leiden | `scrna_seq.md` |

If no domain skill matches (fewer than 2 keyword hits), proceed without skill injection.

### Step 2 — Read the Skill File

`Read prompts/skills/{matched_skill}.md` — get the FULL text.

### Step 3 — Include the Skill Text VERBATIM in the Subagent Prompt

Wrap as:
```
## DOMAIN ANALYSIS PROTOCOL — {Skill Name}

Follow this protocol for your analysis. It contains the exact steps,
required imports, quality checks, and common pitfalls for this analysis type.

{full skill file content}
```

### Step 4 — Placement

Place it AFTER the data context and analysis plan, BEFORE round-specific instructions.

### Round 1 vs Round 2+ Rules

**Round 1**: Include the FULL skill file verbatim. This is mandatory.

**Rounds 2+**: Inject CONDENSED critical rules only (not the full skill).
Extract lines containing "must", "never", "always", "critical", "mandatory",
"required", "forbidden" from the skill file (cap at 15 rules). Inject as:

```
## Domain Protocol Reminders
- [critical rule 1]
- [critical rule 2]
...
```

### Why This Is Mandatory

Failure to inject domain skills causes:
- Analysis agent reimplements algorithms that already exist in the toolkit
- Skill-specific import paths and function signatures are unknown to the agent
- "Do NOT reimplement" instructions are not received — leading to reproducibility failures
- Specific quality checks (e.g., normalization requirements for scRNA-seq) are skipped
- Common pitfalls documented in the skill (e.g., log-normalization before PCA) are not avoided

The domain skill file contains exact steps, required imports, quality checks, and common
pitfalls for the analysis type. Including it verbatim is the ONLY way to guarantee the
agent receives this information.
