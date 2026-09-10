# Phase 3 Cross-Team Dispatch Contracts

> Relocated verbatim from `SKILL.md` during the 2026-09-08 mode-router restructure (source lines 849-963 of the pre-restructure file, preserved in the snapshot at `~/tools/codex-claude-workflow/snapshots/skills-20260908T202743Z/run-pipeline-local/SKILL.md`). This is main-context execution-contract content (dispatch cards / gates / model requirements), not subagent-facing protocol text.

**Load when:** Multi-team mode only, after all teams complete post-analysis dispatch.

---

## Section 6: Phase 3 Cross-Team Dispatch Contracts

**Multi-team only.** These stages run after ALL teams have completed post-analysis (Section 5). Dispatched **sequentially** — each depends on prior output.

Single-team mode: Skip Phase 3 entirely. No `cross_team/` directory is created.

### 6.1 Gene Panel Overlap

| Field | Value |
|-------|-------|
| Model | haiku |
| Artifact | `cross_team/gene_panel_overlap.json` |
| Gate | File exists; `pairwise_jaccard` has entries for all team pairs |
| On failure | Re-dispatch once |
| Prompt inputs | All team findings dossiers |
| Max tokens in dispatch | ~10K |

### 6.2 Meta-Comparison

| Field | Value |
|-------|-------|
| Model | opus |
| Artifact | `cross_team/meta_comparison.json` |
| Gate | File exists; `atomic_claims` array has ≥20 entries; `divergences` array is present |
| On failure | Re-dispatch with: "You must extract ≥20 atomic claims. Currently at {N}." |
| Prompt inputs | All findings dossiers, gene panel overlap, lit review synthesis |
| Max tokens in dispatch | ~30K |

### 6.3 Convergence Validity Classification

| Field | Value |
|-------|-------|
| Model | haiku |
| Artifact | `cross_team/convergence_validity.json` |
| Gate | File exists; each divergence from meta-comparison has a Type A/B/C classification |
| On failure | Re-dispatch once |
| Prompt inputs | Meta-comparison JSON |
| Max tokens in dispatch | ~10K |

### 6.4 Dataset Reuse Scorer

| Field | Value |
|-------|-------|
| Model | haiku |
| Artifact | `cross_team/dataset_reuse.json` |
| Gate | File exists; `reuse_penalties` computed for all team pairs |
| On failure | Re-dispatch once |
| Prompt inputs | All team data manifests |
| Max tokens in dispatch | ~10K |

### 6.5 Reanalysis (Per Divergence, Parallelizable)

Each reanalysis dispatch is independent. **All divergences can be dispatched in parallel** (N Agent calls in one message).

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `cross_team/reanalysis_d{N}.json` per divergence |
| Gate | File exists; `code_executed: true`; `figures` array non-empty |
| On failure | Re-dispatch with specific divergence context |
| Prompt inputs | Meta-comparison divergence entry, relevant team analysis code, data file paths |
| Max tokens in dispatch | ~20K |

**Parallel dispatch pattern:**
```
Message: Agent(divergence_1 reanalysis) + Agent(divergence_2 reanalysis) + Agent(divergence_N reanalysis)
→ Gate all N reanalysis files before proceeding to Model Competition
```

### 6.6 Model Competition

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `cross_team/model_competition.json` |
| Gate | File exists; each reanalysis divergence has a `qualifier` (replicated / partial / contradicted / artifact) |
| On failure | Re-dispatch once |
| Prompt inputs | All reanalysis results, meta-comparison |
| Max tokens in dispatch | ~20K |

### 6.6a Modality Harmonization

| Field | Value |
|-------|-------|
| Model | haiku (rule-based, no LLM needed) |
| Artifact | `cross_team/modality_harmonization.json` |
| Gate | File exists; `n_bridge_required` is an integer |
| On failure | Proceed without — soft gate |
| Prompt inputs | All teams' typed_claims from analysis outputs |
| Max tokens | ~5K |

### 6.6b Claim Contract Enforcement

| Field | Value |
|-------|-------|
| Model | haiku (rule-based, no LLM needed) |
| Artifact | `cross_team/claim_contracts.json` |
| Gate | File exists; `n_downgraded` + `n_unchanged` equals total claims |
| On failure | Proceed without — soft gate |
| Prompt inputs | All teams' typed_claims, modality harmonization results |
| Max tokens | ~5K |

### 6.7 Final Synthesis

| Field | Value |
|-------|-------|
| Model | opus |
| Artifact | `final/synthesis_paper.md` |
| Gate | File exists; has ≥4 IMRAD sections (Introduction, Methods, Results, Discussion); `confidence_tiers` assigned to all major claims |
| On failure | Re-dispatch once with missing sections enumerated |
| Prompt inputs | Meta-comparison, model competition, all team findings dossiers, gene panel overlap |
| Max tokens in dispatch | ~30K |

---

