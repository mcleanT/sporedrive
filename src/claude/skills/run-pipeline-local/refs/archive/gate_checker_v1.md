# Gate-Checker Subagent Protocol

> This protocol is included in haiku gate-checker subagent dispatches.
> The gate-checker verifies stage outputs without the main context reading files directly.

## Instructions

You are verifying outputs for a completed pipeline stage. Check every criterion listed
below for the specified stage. Read the actual files, count entries, verify structure.

Return a JSON verdict:
{
  "stage": "<stage_name>",
  "passed": true/false,
  "checks": [
    {"name": "<check_name>", "passed": true/false, "value": "<actual_value>", "required": "<threshold>"}
  ],
  "blocking_failures": [],
  "metrics": {}
}

## Per-Stage Gate Criteria

### Literature Review
1. queries.json: exists, ≥8 entries, each has target_api
2. raw_candidates.json: exists, ≥100 unique papers, results from ≥3 APIs
3. screened_papers.json: exists, ≥40 papers (or ≥25 with lowered threshold), each has title/authors/year/DOI/score
4. full_texts.json: exists, ≥35 papers with text
5. doi_validation.json: exists, ≥40 papers with doi_verified=true
6. extractions.json: exists, ≥30 entries, each has ≥2 key_findings
7. themes.json: exists, ≥4 themes with ≥3 papers each
8. gaps.json: exists, ≥3 gaps with ≥1 MAJOR
9. lit_review.json: exists, has summary (≥400 words), screened_papers, themes, gaps, coverage_score
10. No papers with unverified DOIs in lit_review.json

### Hypothesis Generation
1. Output JSON exists with ≥3 hypotheses
2. Each has: title, description, predictions, feasibility_score
3. Predictions are specific and falsifiable

### Approach Generation
1. Output JSON exists with real dataset accessions (GSE*, E-MTAB-*, etc.)
2. Statistical plan defined with tests and thresholds
3. ≥2 data source categories used
4. Validation dataset specified

### Data Acquisition
1. data_manifest.json exists with attempted/successful/failed arrays
2. ≥1 PRIMARY dataset from approach in successful array
3. Each successful entry has non-empty files array
4. Downloaded files exist on disk and are non-empty
5. If fallback_used=true: flagged with reason

### Analysis Planner
1. Preprocessed data files exist (Parquet or CSV)
2. column_inventory.json exists with verified columns
3. Sub-analysis specs defined

### Analysis (per round)
1. round{N}/code.py exists
2. round{N}/findings.json exists with ≥1 finding
3. Each finding has: description, statistical_evidence (test, stat, p, effect_size)
4. round{N}/figures/ contains ≥1 PDF or PNG file
5. round{N}/round_summary.md exists

### Figure Composition
1. ≥1 composite multi-panel figure PDF exists
2. Figure metadata JSON exists
3. PDF is vector format (file size ≤300KB for composites)

### Paper Generation
1. Paper markdown exists with all IMRAD sections
2. figure_inventory.json exists with unique paths
3. ≥1 figure referenced via ![caption](path) syntax
4. No duplicate figure paths
5. ≥10 citations total (Intro ≥8, Methods ≥2, Results ≥3, Discussion ≥10)
6. Word count ≥3000
7. PDF exists with embedded figures (file size >500KB)

### [MT] Gene Panel Overlap
1. cross_team/gene_panel_overlap.json exists with pairwise Jaccard scores

### [MT] Meta-Comparison
1. cross_team/meta_comparison.json exists
2. ≥20 atomic claims extracted
3. divergences array present with data_paths and methods per divergence

### [MT] Reanalysis (per divergence)
1. cross_team/reanalysis_d{N}.json exists
2. code_executed=true
3. figures array is non-empty
4. resolution classification present

### [MT] Final Synthesis
1. final/synthesis_paper.md exists
2. Confidence tiers assigned (ROBUST/SUPPORTED/METHOD-DEPENDENT/PRELIMINARY/OPEN)
3. Reanalysis evidence cited

### [MT] Post-Synthesis Chain
1. final/paper.md and final/paper_final.pdf exist
2. final/supplementary/ contains table_s*.csv (≥4 files)
3. final/supplementary_figures.pdf exists (>100KB)
4. final/supplementary_figures_index.json exists
5. final/findings_network.pdf and .png exist
6. final/graphical_abstract.pdf and .png exist
7. final/reference_validation.json exists
8. final/audit_manifest.json exists
