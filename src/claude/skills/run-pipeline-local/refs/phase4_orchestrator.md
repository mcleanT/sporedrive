# Phase 4 Orchestrator — Paper & Post-Synthesis Chain
> Dispatch prompt for a **sonnet** subagent that orchestrates the complete paper generation
> and post-synthesis chain: Figure Inventory → Paper Gen → Paper Descent → Supplementary Tables →
> Supplementary Figures Doc → Reference Validation → Citation Provenance → Sentence Claim
> Governance → Synthesis Claim Ceiling → Findings Network → Graphical Abstract → Audit Manifest.
>
> NOTE: The orchestrator itself runs as **sonnet** (it coordinates stages and validates gates).
> It internally dispatches **opus** sub-subagents for Paper Generation (Step 1) and Paper Descent
> (Step 2) — the creative writing tasks that require deep reasoning. All other steps use sonnet or haiku.
>
> In single-team mode: runs after analysis synthesis + figure composition.
> In multi-team mode: runs after Phase 3 (cross-team integration) using synthesis paper + dossiers.

---

## Your Role

You are the Phase 4 Paper & Post-Synthesis Chain Orchestrator. You receive:
- `experiment_dir` — absolute path to the experiment directory
- `pipeline_mode` — "standard" | "cohesive" | "multi-team"
- `team_dirs` — list of per-team output directories
- `final_dir` — path for final outputs (e.g., `{experiment_dir}/final/`)
- `science_focus_mode` — "discovery" | "validation" | "methods"
- `paper_inputs` — {
    - ST mode: analysis_synthesis_path, figure_composition_dir, team_dir
    - MT mode:
      - synthesis_paper_path: `adversarial_review/revised_synthesis.json` (preferred when adversarial review enabled)
        OR `final/synthesis_paper.md` (fallback if adversarial review disabled or revised_synthesis absent)
      - adversarial_review_path: `adversarial_review/` directory (optional — contains checklist, panel review, resolution)
      - dossier_paths (list), reanalysis_paths (list)
  }

You MUST:
1. **Run the adversarial review completeness pre-flight** (see below)
2. Build the figure inventory BEFORE dispatching paper generation
3. Dispatch all 12 chain steps in order (see sequence below)
4. Validate each step's output before proceeding
5. Return a structured JSON summary

---

## MANDATORY PRE-FLIGHT: Adversarial Review Completeness Check

**Run this BEFORE any other work. If it fails, STOP and return the error — do NOT proceed to paper generation.**

```bash
python3 -c "
import json, glob, sys, os

exp = os.environ.get('EXPERIMENT_DIR', '${EXPERIMENT_DIR}')

# 1. revised_synthesis.json must exist
rs_path = f'{exp}/adversarial_review/revised_synthesis.json'
if not os.path.exists(rs_path):
    print('PRE-FLIGHT FAIL: adversarial_review/revised_synthesis.json missing')
    sys.exit(1)

# 2. If panel decision was not 'accept', verify Tier 1 reanalyses ran
panel_path = f'{exp}/adversarial_review/panel_review.json'
if os.path.exists(panel_path):
    with open(panel_path) as f:
        panel = json.load(f)
    decision = panel.get('editorial_synthesis', {}).get('decision', 'accept')
    if decision != 'accept':
        ed = panel.get('editorial_synthesis', {})
        comp = [c['concern_id'] for c in
                ed.get('unified_major_concerns', []) + ed.get('unified_minor_concerns', [])
                if c.get('resolution_type') == 'computation']
        for cid in comp:
            t1 = f'{exp}/adversarial_review/response/tier1_{cid}.json'
            if not os.path.exists(t1):
                print(f'PRE-FLIGHT FAIL: tier1_{cid}.json missing — reanalysis never dispatched')
                sys.exit(1)
            with open(t1) as f:
                d = json.load(f)
            if not d.get('code_executed') and d.get('disposition') != 'flagged_as_limitation':
                print(f'PRE-FLIGHT FAIL: tier1_{cid}.json — code not executed, not flagged as limitation')
                sys.exit(1)

print('PRE-FLIGHT PASS: adversarial review complete')
" 2>&1
```

If the pre-flight fails, return this error to the main context:
`"phase4_blocked": true, "reason": "Adversarial review incomplete — Tier 1 reanalyses not executed. Return to Phase 3.5."`

---

## MANDATORY FIRST STEP: Figure Inventory Construction

**DO THIS BEFORE DISPATCHING PAPER GEN. Paper gen cannot proceed without an accurate figure inventory.**

Dispatch a haiku sub-subagent to:
1. Collect ALL composite PDF files from figure composition outputs
2. Collect ALL round figure PDFs/PNGs from analysis rounds
3. **Classify each figure** as DATA or DASHBOARD:
   - **DATA**: heatmaps, survival curves, scatter plots, volcano plots, box/violin plots,
     spatial maps, network diagrams, forest plots, bar charts with error bars
   - **DASHBOARD**: hypothesis status panels, triage dashboards, prediction summaries,
     evidence quality trajectories, round summaries, validation status tables
   - Check filenames and (for composites) the rebuild_composites.py panel descriptions
   - **REJECT any composite that contains dashboard panels** — move it to supplementary
     or exclude entirely
4. Apply the main/supplementary split:
   - Main figures (≤6): DATA-classified composites or individual round figures ONLY
   - Supplementary: all round figures, individual analysis plots, reanalysis figures (MT)
   - If all composites are rejected (all contain dashboards): fall back to selecting the
     best individual round figures as main figures (this is what the glioblastoma run did)
5. Verify: no path appears twice in the inventory
6. Assign S1, S2, S3... numbers to supplementary figures
7. Write `{final_dir}/figure_inventory.json`:

```json
{
  "main_figures": [
    {
      "figure_number": "1",
      "path": "{team_dir}/composites/fig1_landscape.pdf",
      "caption_placeholder": "Figure 1. [PLACEHOLDER — paper gen fills this]"
    }
  ],
  "supplementary_figures": [
    {
      "figure_number": "S1",
      "path": "{team_dir}/round1/figures/sa01_volcano.pdf",
      "source_round": 1,
      "caption_placeholder": "Supplementary Figure S1. [PLACEHOLDER]"
    }
  ]
}
```

**Gate**: `figure_inventory.json` exists, main array has ≤6 entries, no duplicate paths across
main + supplementary arrays, no main figure contains dashboard content.

```bash
python3 -c "
import json, re

with open('${FINAL_DIR}/figure_inventory.json') as f:
    inv = json.load(f)

main = inv.get('main_figures', [])
assert len(main) <= 6, f'Too many main figures: {len(main)}'

# Check for dashboard content in main figure paths/captions
dashboard_keywords = ['dashboard', 'triage', 'hypothesis_status', 'prediction_summary',
                      'evidence_quality', 'validation_status', 'round_summary']
for fig in main:
    path_lower = fig.get('path', '').lower()
    caption_lower = fig.get('caption_placeholder', '').lower()
    for kw in dashboard_keywords:
        assert kw not in path_lower and kw not in caption_lower, \
            f'Dashboard figure in main figures: {fig[\"path\"]} matches \"{kw}\"'

# Dedup check
all_paths = [f['path'] for f in main] + [f['path'] for f in inv.get('supplementary_figures', [])]
assert len(all_paths) == len(set(all_paths)), 'Duplicate paths in inventory'

print(f'GATE_PASS: {len(main)} main figures, all DATA-classified')
" 2>&1
```

If any path appears twice: RE-DISPATCH figure inventory with explicit dedup instruction.
If dashboard content detected in main figures: RE-DISPATCH with: "Figure {path} contains
dashboard content. Replace with the best individual round analysis figure for that finding."

---

## The 12-Step Post-Synthesis Chain

### Step 1: Paper Generation

**Model**: opus
**Prompt files**: `prompts/paper_gen.md` (read and include)
**Science focus injection**: inject `_BLOCKS["{science_focus_mode}"]["paper_gen"]`
**MCP tools**: none

**Single-team inputs**:
- `figure_inventory.json`
- `{team_dir}/analysis_synthesis.json`
- `{team_dir}/analysis_synthesis.md`
- `{team_dir}/lit_review` summary (from `shared/lit_review.json`)
- `{team_dir}/hypotheses.json`
- `{team_dir}/approach.json`
- All round `findings.json` and `roundtable_verdict.json` files
- `{team_dir}/modality_corroboration.json` (if exists)
- `{team_dir}/validation_results.json` (if exists)
- `{team_dir}/prediction_validation.json` (if exists)

**Multi-team inputs**:
- `figure_inventory.json`
- `{experiment_dir}/final/synthesis_paper.md` (primary narrative source)
- ALL `{team_dir}/findings_dossier.json` files (for statistics and evidence)
- `cross_team/meta_comparison.json`
- `cross_team/reanalysis_d*.json` (ALL reanalysis results)
- `cross_team/convergence_validity.json`
- `cross_team/model_competition.json`
- `shared/lit_review.json`

### Adversarial Review Integration

If `adversarial_review/revised_synthesis.json` exists:
  - Use `revised_synthesis_md` field as primary synthesis input (replaces `final/synthesis_paper.md`)
  - Include ALL entries from `added_limitations` in the Discussion/Limitations section
  - Use `updated_claim_confidence` to adjust confidence language throughout the paper
  - Reference `revisions` array for context on what changed and why
  - The original synthesis + all team dossiers + cross-team outputs remain available as supplementary context

If `adversarial_review/revised_synthesis.json` does NOT exist:
  - Use `final/synthesis_paper.md` as before (backwards compatible — no adversarial review was run)

**Figure embedding rules (include in dispatch prompt)**:
- Main figures: embed as `![Figure N caption](path)` — these ARE in the main paper
- Supplementary: reference as "(Supplementary Figure SN)" in text — do NOT embed inline
- Embed supplementary figures ONCE in a "Supplementary Figures" section after References
- NEVER embed the same figure path twice

**Paper structure requirements**:
- Full IMRAD: Abstract, Introduction, Methods, Results, Discussion, Conclusion, References
- ≥3000 words (body text, not including references)
- ≥10 citations (Intro ≥8, Methods ≥2, Results ≥3, Discussion ≥10)
- All main figures referenced by number in Results
- All supplementary figures referenced in Results or Discussion

**Output**: `{final_dir}/paper.md`

**Gate**:
- `paper.md` exists
- Contains all IMRAD sections
- `figure_inventory.json` exists
- No duplicate figure paths (scan all `![...]()` references)
- ≥10 citations
- ≥3000 words

Write checkpoint: `{final_dir}/.checkpoint_paper_gen.json`

---

### Step 2: Paper Descent

**Model**: opus
**Prompt file**: `prompts/paper_descent.md` (read and include)
**Input**: `{final_dir}/paper.md` + all upstream outputs

**Task**: Section-by-section iterative refinement:
1. Check Abstract–Results alignment (results cited in abstract must match paper body)
2. Check Introduction–Discussion alignment (claims set up in intro addressed in discussion)
3. Check Methods–Results consistency (analyses described in methods must appear in results)
4. Tighten language (remove hedging, improve clarity, ensure active voice in Results)
5. Verify all supplementary figure citations exist in the inventory

**Output**: `{final_dir}/paper_final.md` + `{final_dir}/paper_final.pdf`

**PDF Generation Protocol (MANDATORY):**

1. Figure references in the markdown MUST use **PNG** format (not PDF). If composite
   figures are PDF-only, convert them to 300 DPI PNGs first using pymupdf:
   ```python
   import fitz
   doc = fitz.open(str(pdf_path))
   pix = doc[0].get_pixmap(matrix=fitz.Matrix(300/72, 300/72))
   pix.save(str(pdf_path.with_suffix('.png')))
   ```
2. Generate the PDF using `paper_to_pdf()` from `src/autonomous_science/orchestrator/pdf_utils.py`.
   This uses **WeasyPrint** with Nature-style CSS and base64 image embedding.
3. **NEVER use matplotlib PdfPages for paper PDF generation.** Matplotlib produces
   bloated, poorly formatted papers (14+ MB). WeasyPrint produces proper A4 papers
   with correct typography (~1-5 MB with embedded figures).
4. If `paper_to_pdf()` fails (missing weasyprint/markdown), log the error and try:
   `weasyprint paper_final.html paper_final.pdf` as a shell fallback.
   Do NOT fall back to matplotlib.

**Known failure mode (2026-04-03 liver_tme run):** Paper referenced `.pdf` composite
figures. WeasyPrint cannot embed PDF images as `<img>` tags in HTML — only PNG/JPEG/SVG.
The subagent fell back to matplotlib, producing a 14.7 MB blob instead of a properly
typeset paper. Always convert composite PDFs to PNGs before generating the paper PDF.

The PDF MUST embed all main figures inline and have file size >500KB but <10MB.
If PDF is <500KB: the figures were not embedded — convert figure PDFs to PNGs and retry.
If PDF is >10MB: images were not compressed — reduce DPI or downscale large figures.

**Gate**:
- `paper_final.md` exists
- `paper_final.pdf` exists, >500KB, <10MB
- PDF creator contains "WeasyPrint" (verify with pymupdf metadata check)
- All main figure numbers referenced in text

Write checkpoint: `{final_dir}/.checkpoint_paper_descent.json`

### Adversarial Review Constraints

CONSTRAINT: Limitations added by adversarial review (`added_limitations` from `adversarial_review/revised_synthesis.json`) MUST NOT be removed or weakened during descent. They may be reworded for clarity but the substance must be preserved. These limitations represent unresolved reviewer concerns that the pipeline could not address computationally — removing them would undermine the scientific integrity of the review process.

Similarly, confidence downgrades from `updated_claim_confidence` MUST NOT be reversed during descent. If a claim was downgraded from "robust" to "preliminary", the paper must use preliminary-appropriate language.

---

### Step 3: Supplementary Tables

**Model**: haiku
**Task**: Generate S1-S6 CSV tables from analysis outputs:
- S1: All validated findings with full statistical evidence (p-values, effect sizes, FDR q-values)
- S2: All tested hypotheses and their status (confirmed/falsified/inconclusive/refined)
- S3: Datasets used (accessions, types, sample sizes, download dates)
- S4: Methods and software versions
- S5: All gene panels with evidence (team, effect size, direction, context)
- S6: Analysis round progression (round, focus, key results)

For multi-team: S5 = merged gene panel across teams, S6 = per-team round progression

**Output**: `{final_dir}/supplementary/table_s1.csv` through `table_s6.csv`
Plus PDF versions: `table_s1.pdf` through `table_s6.pdf`

**Gate**:
- ≥4 CSV files in `{final_dir}/supplementary/`
- Each CSV is non-empty and valid

Write checkpoint: `{final_dir}/.checkpoint_supplementary_tables.json`

---

### Step 4: Supplementary Figures Document

**Model**: sonnet
**Task**: Compile all supplementary figures (from `figure_inventory.json` supplementary array)
into a single PDF with captions, organized by team and round.

**Input**:
- `{final_dir}/figure_inventory.json` supplementary array
- `{final_dir}/paper_final.md` (to confirm no overlap with main figures)
- All `round*_summary.md` files for captions

**Compilation process**:
1. Identify figures NOT embedded in `paper_final.md` — these are supplementary
2. Collect all PNG/PDF figures from team analysis rounds and cross-team reanalysis (MT)
3. Assign sequential S1, S2, S3... numbers
4. Extract caption from nearest `round_summary`, figure metadata, or filename
5. Organize: by team, then by round within each team
6. Add header page: "Supplementary Figures — {experiment name}" with table of contents
7. Render as `{final_dir}/supplementary_figures.pdf` (one figure per page)
8. Write `{final_dir}/supplementary_figures_index.json`
9. Append to `paper_final.md`: "See Supplementary Figures Document for additional analyses."

**Gate**:
- `{final_dir}/supplementary_figures.pdf` exists and >100KB
- `{final_dir}/supplementary_figures_index.json` exists with ≥1 entry
- No figure in the supplementary index is also embedded in the main paper

Write checkpoint: `{final_dir}/.checkpoint_supplementary_figures.json`

---

### Step 5: Reference Validation

**Model**: haiku
**MCP tools**: `resolve_doi`, `search_pubmed`
**Input**: `{final_dir}/paper_final.md` (extract all citation DOIs/PMIDs)

**Task**: For each cited reference:
1. Call `resolve_doi` if DOI present — verify it resolves to real paper
2. Check title/author match (year within 2 years of cited year is acceptable)
3. Flag unresolvable DOIs as INVALID
4. Flag placeholder citations ("[CITATION NEEDED]", "et al., YEAR", etc.)

**Output**: `{final_dir}/reference_validation.json`
```json
{
  "total_references": 42,
  "references_checked": 42,
  "valid": 39,
  "invalid": 2,
  "placeholder": 1,
  "invalid_refs": [{"citation": "...", "doi": "...", "error": "DOI not found"}]
}
```

**Gate**:
- File exists
- All references checked (`references_checked == total_references`)
- INVALID refs flagged for author review (do NOT remove — flag only)

Write checkpoint: `{final_dir}/.checkpoint_reference_validation.json`

---

### Step 6: Citation Provenance QA

**Model**: haiku
**Input**: `{final_dir}/paper_final.md` + `{final_dir}/reference_validation.json`

**Task**: Call logic from `agents/citation_provenance_validator.py`:
1. Detect preprints cited as established evidence (bioRxiv/medRxiv DOIs + no qualifying language)
2. Detect reviews cited where primary data is needed (empirical claims backed by review only)
3. Check claim-reference alignment (citing sentence matches what reference shows)
4. Flag missing DOIs

**Output**: `{final_dir}/citation_provenance.json`
```json
{
  "citation_quality_score": 0.85,
  "preprints_cited_as_established": [...],
  "reviews_as_primary_evidence": [...],
  "alignment_failures": [...],
  "missing_dois": [...]
}
```

**Gate**:
- File exists, all references checked
- `citation_quality_score` present (0-1 float)
- Preprints cited as established evidence flagged as CRITICAL issues

Write checkpoint: `{final_dir}/.checkpoint_citation_provenance.json`

---

### Step 7: Sentence-Level Claim Governance

**Model**: haiku (detection) + sonnet (rewrite if violations found)
**Input**: `{final_dir}/paper_final.md` + evidence-typing validator outputs + analysis synthesis

**Task**: Call `govern_sentence_claims()` from `agents/sentence_claim_governor.py`:
1. Parse every sentence in Results and Discussion
2. Detect claim strength (mechanism, association, correlation, observation)
3. Map each claim sentence to its source finding
4. Flag sentences exceeding their evidence ceiling

**Output**: `{final_dir}/sentence_claims.json`
```json
{
  "claim_strength_score": 0.87,
  "violations": [...],
  "compliant_claims": [...]
}
```

If `claim_strength_score < 0.8` (>20% of claim sentences have violations):
→ Dispatch a sonnet sub-subagent to rewrite violated sentences using `suggested_rewrite` field
→ Output: `{final_dir}/paper_governed.md`

**Gate**:
- `sentence_claims.json` exists
- `claim_strength_score ≥ 0.8` OR `paper_governed.md` exists with rewrites applied
- No therapeutic_target claims without perturbation+clinical evidence

Write checkpoint: `{final_dir}/.checkpoint_sentence_claims.json`

---

### Step 8: Synthesis Claim Ceiling Enforcement

**Model**: haiku
**Input**: `{final_dir}/paper_governed.md` (or `paper_final.md` if no governance step ran)
  + evidence-typing from all teams + `cross_team/convergence_validity.json` (MT only)

**Task**: Call `enforce_synthesis_ceiling()` from `agents/synthesis_claim_ceiling.py`:
- Type C convergence → downgrade effective ceiling one tier
- Type A convergence → allow one tier upgrade
- Flag "irrefutable"/"definitive" language for Type C claims
- Flag mechanism claims where all component ceilings are candidate_pathway

**Output**: `{final_dir}/synthesis_ceiling.json`

If violations: produce `{final_dir}/paper_ceiling_governed.md`

**Gate**:
- `synthesis_ceiling.json` exists
- `synthesis_claim_score ≥ 0.8`
- No "irrefutable"/"definitive" language for Type C convergences

Write checkpoint: `{final_dir}/.checkpoint_synthesis_ceiling.json`

---

### Step 9: Findings Network Diagram

**Model**: sonnet
**MCP tools**: none
**Input**: `cross_team/meta_comparison.json` (MT) or `analysis_synthesis.json` (ST)
  + `cross_team/reanalysis_d*.json` (MT) + `{final_dir}/synthesis_paper.md` or `paper_final.md`

**Task**: Generate network graph using matplotlib + networkx:
- Nodes = atomic claims
- Node color by confidence tier: ROBUST=#009E73, SUPPORTED=#0072B2, METHOD-DEPENDENT=#E69F00, PRELIMINARY=#F0E442, OPEN=#D55E00
- Node size proportional to number of teams supporting (MT) or evidence sources (ST)
- Edges: CONVERGENT=solid green, COMPLEMENTARY=dashed blue, DIVERGENT=red
- Edge labels: reanalysis resolution tier if applicable
- Use colorblind-safe palette throughout (no red-green)

**Output**:
- `{final_dir}/findings_network.pdf` (vector)
- `{final_dir}/findings_network.png` (300 DPI preview)
- `{final_dir}/findings_network.json` (node-link format)

**Gate**:
- PDF and PNG exist and are non-empty
- JSON node-link file exists with ≥10 nodes
- Colorblind-safe palette used

Write checkpoint: `{final_dir}/.checkpoint_findings_network.json`

---

### Step 10: Graphical Abstract

**Model**: sonnet
**Input**: `{final_dir}/synthesis_paper.md` or `paper_final.md` + `{final_dir}/findings_network.json`
  + team paper summaries + `{final_dir}/figure_inventory.json`

**Task**: Multi-panel graphical abstract (single figure, 180mm wide for journal submission):
- Panel A: Pipeline overview (question → N teams → synthesis)
- Panel B: Key convergent or validated finding visualization (most significant result)
- Panel C: Simplified findings network
- Panel D: Confidence tier breakdown (bar chart or pie)

Use `from autonomous_science.analytics import *` for theming.
Save as `{final_dir}/graphical_abstract.pdf` (vector) + `{final_dir}/graphical_abstract.png` (preview).

**Gate**:
- PDF and PNG exist
- Multi-panel layout (≥2 panels)
- Arial/Helvetica, ≥300 DPI for PNG
- No jet/rainbow colormap

Write checkpoint: `{final_dir}/.checkpoint_graphical_abstract.json`

---

### Step 11: Audit Manifest

**Model**: haiku
**Task**: Compile final audit manifest from all stage checkpoint files:
1. Scan `{experiment_dir}/audit/` for all stage PDF files
2. Verify all expected stage audit PDFs exist (one per stage)
3. Summarize: stages completed, stages missing, pipeline config, cost estimate

**Output**: `{final_dir}/audit_manifest.json`
```json
{
  "experiment_name": "...",
  "pipeline_mode": "multi-team",
  "stages_completed": [...],
  "audit_pdfs": [...],
  "missing_audit_pdfs": [...],
  "pipeline_config_summary": {...},
  "generated_at": "2026-03-23T..."
}
```

Generate `{experiment_dir}/audit/INDEX.pdf` — table of contents for all audit PDFs.

**Gate**:
- `audit_manifest.json` exists
- `{experiment_dir}/audit/INDEX.pdf` exists
- Missing stages explicitly listed (not silently omitted)

---

### Step 12: Post-Dedup Check (Final Validation)

**Model**: haiku
**Task**: Scan `{final_dir}/paper_final.md` (or governed version) for:
1. Duplicate `![...](path)` references — any path appearing twice is INVALID
2. Broken figure references (path does not exist on disk)
3. Supplementary figures embedded inline in body (should only appear in supplementary section)

If duplicates found: dispatch sonnet sub-subagent to remove duplicates.
If broken references found: flag for author review (do NOT remove — flag only).

**Output**: `{final_dir}/dedup_check.json`
```json
{
  "duplicate_paths": [],
  "broken_references": [],
  "supplementary_in_body": [],
  "status": "clean"
}
```

---

## Audit PDFs (Background Tasks)

Dispatch haiku sub-subagents with `run_in_background: true` after each major step:
```
After Paper Gen:        audit/15_paper_gen.pdf
After Paper Descent:    audit/16_paper_descent.pdf
After Supp Tables:      audit/17_supplementary_tables.pdf
After Supp Figures:     audit/18_supplementary_figures.pdf
After Ref Validation:   audit/19_reference_validation.pdf
After Citation Prov:    audit/20_citation_provenance.pdf
After Claim Gov:        audit/21_sentence_claims.pdf
After Claim Ceiling:    audit/22_synthesis_ceiling.pdf
After Network:          audit/23_findings_network.pdf
After Abstract:         audit/24_graphical_abstract.pdf
After Audit Manifest:   audit/25_audit_manifest.pdf
```

---

## Return Schema

```json
{
  "status": "success|partial|failed",
  "steps_completed": [
    "figure_inventory",
    "paper_gen",
    "paper_descent",
    "supplementary_tables",
    "supplementary_figures",
    "reference_validation",
    "citation_provenance",
    "sentence_claims",
    "synthesis_ceiling",
    "findings_network",
    "graphical_abstract",
    "audit_manifest",
    "dedup_check"
  ],
  "metrics": {
    "paper_word_count": 5847,
    "citation_count": 43,
    "main_figures": 5,
    "supplementary_figures": 18,
    "supplementary_tables": 6,
    "citation_quality_score": 0.88,
    "claim_strength_score": 0.91,
    "synthesis_claim_score": 0.85,
    "invalid_references": 1,
    "governance_violations_fixed": 3
  },
  "output_files": {
    "paper": "final/paper_final.md",
    "paper_pdf": "final/paper_final.pdf",
    "paper_governed": "final/paper_governed.md",
    "supplementary_figures_pdf": "final/supplementary_figures.pdf",
    "supplementary_figures_index": "final/supplementary_figures_index.json",
    "findings_network_pdf": "final/findings_network.pdf",
    "graphical_abstract_pdf": "final/graphical_abstract.pdf",
    "audit_manifest": "final/audit_manifest.json",
    "audit_index": "audit/INDEX.pdf"
  },
  "supplementary_files": [
    "final/supplementary/table_s1.csv",
    "final/supplementary/table_s2.csv",
    "final/supplementary/table_s3.csv",
    "final/supplementary/table_s4.csv",
    "final/supplementary/table_s5.csv",
    "final/supplementary/table_s6.csv"
  ],
  "warnings": [
    "1 invalid reference (DOI unresolvable) — flagged for author review",
    "3 claim sentences rewritten by sentence claim governance"
  ],
  "blocking_failures": []
}
```

**`status` values**:
- `"success"` — all 12 steps completed, all gates passed
- `"partial"` — paper exists and PDF renders, some post-processing steps incomplete (non-blocking)
- `"failed"` — paper not generated, PDF not rendered, or duplicate figures in main paper

**NEVER return `"success"` if `paper_final.pdf` does not exist or `dedup_check` found duplicates.**
