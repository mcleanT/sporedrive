# Phase 5 Orchestrator — Post-Processing
> Dispatch prompt for a **sonnet** subagent that orchestrates post-processing tasks:
> per-stage audit PDF generation, supplementary CSV compilation, run summary generation,
> data cleanup, .living/ update, and final INDEX.pdf.
>
> NOTE: The orchestrator itself runs as **sonnet** (coordination and .living/ semantic updates
> require reasoning above haiku level). It internally dispatches **haiku** sub-subagents for
> mechanical tasks: PDF generation, data cleanup, file inventory, and INDEX.pdf compilation.
> The .living/ update (Task 5) dispatches a separate **sonnet** sub-subagent as required by
> project conventions for knowledge updates.
>
> This phase runs AFTER all scientific work is complete. It never touches scientific content —
> it only generates reports, cleans up raw data, and updates living documentation.

---

## Your Role

You are the Phase 5 Post-Processing Orchestrator. You receive:
- `experiment_dir` — absolute path to the experiment directory
- `experiment_name` — human-readable run name
- `pipeline_mode` — "standard" | "cohesive" | "multi-team"
- `run_mode` — "local" | "api" | "distributed"
- `stages_completed` — list of stage names that actually completed (from phase orchestrators)
- `living_dir` — path to `.living/` directory (e.g., `{project_dir}/.living/`)
- `skill_scripts_path` — absolute path to pipeline PDF generation scripts
- `team_dirs` — list of per-team output directories (may be empty for single-team)
- `cleanup_enabled` — boolean (true for local/api modes, false for distributed)
- `phase_summaries` — JSON summaries returned by Phase 1, 2, 3, 4 orchestrators

You MUST complete all 6 tasks below. None may be skipped.

---

## Task 1: Per-Stage Audit PDF Generation

**Model**: haiku (you run this yourself via Bash tool)
**Purpose**: Generate a PDF audit report for every pipeline stage that completed.

For each stage in `stages_completed`, dispatch a background haiku sub-subagent:

```bash
python3 {skill_scripts_path}/generate_pdf.py summary \
  --stage "{stage_name}" \
  --input "{stage_output_json_path}" \
  --output "{experiment_dir}/audit/{NN}_{stage_name}.pdf"
```

Stage number (`NN`) and output JSON path mapping:
```
01_literature_review          → shared/lit_review.json
02_hypotheses_{team}          → {team_dir}/hypotheses.json
03_approach_{team}            → {team_dir}/approach.json
04_data_acquisition_{team}    → {team_dir}/data_manifest.json
05_analysis_planner_{team}    → {team_dir}/column_inventory.json
06_analysis_round{N}_{team}   → {team_dir}/round{N}/findings.json  (one per round)
07_figure_composition_{team}  → {team_dir}/analysis_synthesis.json
08_findings_dossier_{team}    → {team_dir}/findings_dossier.json (MT only)
11_gene_panel_overlap         → cross_team/gene_panel_overlap.json (MT only)
12_meta_comparison            → cross_team/meta_comparison.json (MT only)
13_reanalysis_loop            → cross_team/ (MT only)
14_final_synthesis            → final/synthesis_paper.md (MT only)
15_paper_gen                  → final/paper.md
16_paper_descent              → final/paper_final.md
17_supplementary_tables       → final/supplementary/ (MT only)
18_supplementary_figures      → final/supplementary_figures_index.json
19_reference_validation       → final/reference_validation.json
20_citation_provenance        → final/citation_provenance.json
21_sentence_claims            → final/sentence_claims.json
22_synthesis_ceiling          → final/synthesis_ceiling.json
23_findings_network           → final/findings_network.json
24_graphical_abstract         → final/graphical_abstract.pdf
25_audit_manifest             → final/audit_manifest.json
26_adversarial_checklist      → adversarial_review/checklist_results.json
27_adversarial_panel_review   → adversarial_review/panel_review.json
28_adversarial_resolution     → adversarial_review/resolution.json
29_adversarial_tier_response  → adversarial_review/response/ (all tier artifacts)
30_adversarial_revised_synthesis → adversarial_review/revised_synthesis.json
```

Use `run_in_background: true` for all PDF generation — do NOT wait for individual PDFs.
After dispatching ALL audit PDF sub-subagents, collect their completions and verify:
- At least one PDF exists in `{experiment_dir}/audit/`
- No PDF generation crashed (check for error returns)

---

## Task 2: INDEX.pdf

After all audit PDFs are generated (wait for completion), dispatch a haiku sub-subagent to:

1. List all `.pdf` files in `{experiment_dir}/audit/` sorted by filename
2. Generate `{experiment_dir}/audit/INDEX.pdf`:
   - Cover page: "Pipeline Audit Trail — {experiment_name}"
   - Table of contents: each stage listed with file size and completion status
   - Append all per-stage PDFs in numerical order into one combined PDF

Use `pymupdf` (or `pypdf` as fallback) for PDF merging.

**Gate**:
- `{experiment_dir}/audit/INDEX.pdf` exists and is >50KB (indicates non-empty)
- At least one per-stage PDF was merged

---

## Task 3: Run Summary

Dispatch a haiku sub-subagent to generate the run summary:

**Inputs**: All phase orchestrator summaries from `phase_summaries` + pipeline config

**Output files**:
- `{experiment_dir}/run_summary.md` — human-readable summary
- `{experiment_dir}/run_summary.pdf` — PDF version (convert from MD)
- `{experiment_dir}/run_metadata.json` — machine-readable metadata

### run_summary.md structure:

```markdown
# Pipeline Run Summary: {experiment_name}

**Date**: {date}
**Pipeline Mode**: {pipeline_mode}
**Science Focus**: {science_focus_mode}
**Run Mode**: {run_mode}

## Research Question

{research_question}

## Stages Completed

| Stage | Status | Key Metrics |
|-------|--------|-------------|
| Literature Review | ✓ Complete | {screened_papers} papers, {themes} themes, coverage {coverage_score} |
| Hypothesis Gen | ✓ Complete | {n_hypotheses} hypotheses |
| Approach Gen | ✓ Complete | {n_datasets} datasets, {n_categories} source categories |
| Data Acquisition | ✓ Complete | {n_downloaded}/{n_attempted} datasets downloaded |
| Analysis Planner | ✓ Complete | {n_columns} columns verified |
| Analysis Loop | ✓ Complete | {n_rounds} rounds, converged={converged} |
| Figure Composition | ✓ Complete | {n_composites} composite figures |
| [MT] Meta-Comparison | ✓ Complete | {n_claims} claims, {n_divergences} divergences |
| [MT] Reanalysis | ✓ Complete | {n_resolved}/{n_divergences} resolved |
| [MT] Final Synthesis | ✓ Complete | {confidence_tier_breakdown} |
| Paper Generation | ✓ Complete | {word_count} words, {n_figures} figures |

## Key Findings

{extract top 5 validated_findings from analysis_synthesis.json, one sentence each}

## Output Files

- Paper: {final_dir}/paper_final.pdf
- Supplementary Figures: {final_dir}/supplementary_figures.pdf
- Findings Network: {final_dir}/findings_network.pdf
- Graphical Abstract: {final_dir}/graphical_abstract.pdf
- Audit Trail: {experiment_dir}/audit/INDEX.pdf

## Warnings

{list any warnings from phase_summaries}
```

### run_metadata.json schema:

```json
{
  "experiment_name": "...",
  "experiment_dir": "...",
  "research_question": "...",
  "pipeline_mode": "standard|multi-team",
  "science_focus_mode": "discovery|validation|methods",
  "run_mode": "local|api|distributed",
  "completed_at": "2026-03-24T...",
  "stages_completed": [...],
  "stages_failed": [...],
  "teams": [...],
  "analysis_rounds_per_team": {...},
  "datasets_downloaded": 3,
  "datasets_attempted": 5,
  "validated_findings_count": 12,
  "paper_word_count": 5847,
  "total_figures": 23,
  "main_figures": 5,
  "supplementary_figures": 18,
  "warnings_count": 2,
  "output_files": {
    "paper_pdf": "final/paper_final.pdf",
    "supplementary_figures_pdf": "final/supplementary_figures.pdf",
    "findings_network": "final/findings_network.pdf",
    "graphical_abstract": "final/graphical_abstract.pdf",
    "audit_index": "audit/INDEX.pdf"
  }
}
```

**Gate**:
- `run_summary.md` exists and is non-empty
- `run_summary.pdf` exists (>10KB indicates non-empty)
- `run_metadata.json` exists and is valid JSON

---

## Task 4: Verify Data Cleanup (moved to Phase 3.6)

Data cleanup now runs as Phase 3.6 (after adversarial review resolution), NOT in Phase 5.
Phase 5 verifies:
1. If cleanup was expected (run_mode != "distributed"):
   - Check `cleanup_manifest.json` exists → log cleanup stats in run summary
   - OR verify cleanup was skipped due to `requires_further_analysis` → log skip reason
2. If run_mode == "distributed": skip verification (no cleanup expected)

Include cleanup status in the run summary output.

---

## Task 5: .living/ Update

**Dispatch a SEPARATE sonnet sub-subagent for this task.**
(Haiku is insufficient — knowledge updates require semantic judgment about what is worth recording.)

**Do NOT read or write .living/ files yourself in this orchestrator.**
**Do NOT use Bash to append to .living/ files directly.**
**Dispatch a sonnet sub-subagent with max_turns: 10.**

Sub-subagent task:

```
You are updating the living documentation for the Autonomous Science project after a pipeline run.

Pipeline run: {experiment_name}
Research question: {research_question}
Science focus mode: {science_focus_mode}
Pipeline mode: {pipeline_mode}
Analysis rounds completed: {n_rounds}
Converged: {converged}
Key findings count: {n_validated_findings}
Warnings: {warnings_list}
Blocking failures: {blocking_failures_list}

Please update the following .living/ files by APPENDING (not replacing) using printf >> or similar:

1. {living_dir}/decisions.md — Log pipeline configuration decisions:
   - What science focus mode was used and why
   - Whether iterative deepening converged and after how many rounds
   - Any quality gate failures and how they were resolved

2. {living_dir}/learnings.md — Log any new learnings from this run:
   - Did any common failure modes occur? (lit review collapse, analysis loop delegation, etc.)
   - Were there unexpected successes or novel patterns?
   - Any new failure modes not in refs/common_failures.md?

3. {living_dir}/ANALYSIS_MANIFEST.md — Append a one-line run summary:
   "| {date} | {experiment_name} | {n_validated_findings} findings | {n_rounds} rounds | {status} |"

Format: Use the existing entry format in each file. Check the last few entries before appending
to match the style. Append only — NEVER overwrite existing content.

Return a one-line summary: "Updated decisions.md (+2 entries), learnings.md (+1 entry), ANALYSIS_MANIFEST.md (+1 row)"
```

**Gate**:
- Sonnet sub-subagent returns a one-line summary (indicates completion)
- Do NOT verify .living/ file contents in this orchestrator (would cause context bloat)

---

## Task 6: Final File Inventory

Dispatch a haiku sub-subagent to generate the complete file inventory:

**Task**: Walk `{experiment_dir}` and produce a sorted list of all output files with sizes.

**Output**: `{experiment_dir}/file_inventory.txt`

```
experiment outputs/my_experiment/
├── shared/                     (5 files, 2.3 MB)
│   ├── lit_review.json         (1.2 MB)
│   ├── lit_review.md           (45 KB)
│   └── ...
├── team_1/                     (47 files, 156 MB)
│   ├── hypotheses.json         (8 KB)
│   ├── approach.json           (12 KB)
│   ├── data/                   (4 files, 142 MB)
│   ├── round1/                 (6 files, 2.1 MB)
│   └── ...
├── final/                      (18 files, 8.4 MB)
│   ├── paper_final.pdf         (3.2 MB)
│   ├── supplementary_figures.pdf (2.1 MB)
│   └── ...
├── audit/                      (12 files, 4.8 MB)
│   ├── INDEX.pdf               (3.1 MB)
│   └── ...
├── run_summary.pdf             (142 KB)
└── run_metadata.json           (4 KB)

Total: 147 files, 178 MB
```

---

## Return Schema

```json
{
  "status": "success|partial|failed",
  "tasks_completed": [
    "audit_pdfs",
    "index_pdf",
    "run_summary",
    "data_cleanup",
    "living_update",
    "file_inventory"
  ],
  "metrics": {
    "audit_pdfs_generated": 18,
    "audit_pdfs_missing": 0,
    "bytes_freed_by_cleanup": 847293847,
    "total_output_files": 147,
    "total_output_mb": 178
  },
  "output_files": {
    "audit_index": "audit/INDEX.pdf",
    "run_summary_md": "run_summary.md",
    "run_summary_pdf": "run_summary.pdf",
    "run_metadata": "run_metadata.json",
    "cleanup_manifest": "cleanup_manifest.json",
    "file_inventory": "file_inventory.txt"
  },
  "warnings": [
    "Cleanup skipped for team_1/data/raw/GSE99999_matrix.h5 — no preprocessed Parquet found"
  ],
  "blocking_failures": []
}
```

**`status` values**:
- `"success"` — all 6 tasks completed
- `"partial"` — ≥4 tasks completed (audit PDFs + run summary are highest priority)
- `"failed"` — run summary not generated, or cleanup deleted protected files

**The parent reports this summary to the user as the final pipeline completion message.
Include the key output file paths (paper PDF, graphical abstract, audit index) prominently.**

---

## What to Report Back to Parent

When returning the summary, also include a human-readable completion message for the parent
to relay to the user:

```
Pipeline run "{experiment_name}" complete.

Key outputs:
  Paper:              {final_dir}/paper_final.pdf ({word_count} words, {n_main_figures} main figures)
  Supplementary:      {final_dir}/supplementary_figures.pdf ({n_supp_figures} figures)
  Findings Network:   {final_dir}/findings_network.pdf
  Graphical Abstract: {final_dir}/graphical_abstract.pdf
  Audit Trail:        {experiment_dir}/audit/INDEX.pdf ({n_stages} stages)
  Run Summary:        {experiment_dir}/run_summary.pdf

Analysis: {n_rounds} rounds, {n_validated_findings} validated findings, converged={converged}
Data: {n_downloaded} datasets downloaded, {mb_freed} MB raw data cleaned up
Warnings: {n_warnings} (see run_summary.md for details)
```
