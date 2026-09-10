# Post-Analysis Flat Dispatch Contracts

> Relocated verbatim from `SKILL.md` during the 2026-09-08 mode-router restructure (source lines 681-848 of the pre-restructure file, preserved in the snapshot at `~/tools/codex-claude-workflow/snapshots/skills-20260908T202743Z/run-pipeline-local/SKILL.md`). This is main-context execution-contract content (dispatch cards / gates / model requirements), not subagent-facing protocol text.

**Load when:** After the analysis loop converges/exits, before Phase 3 cross-team dispatch.

---

## Section 5: Post-Analysis Flat Dispatch Contracts

**Single-team mode**: Only prediction generation (5.2), prediction validation (5.3), and figure composition (5.6) run as separate stages. Modality corroboration, validation analysis, and analysis synthesis are handled internally by the analysis loop's iterative deepening. The findings dossier is multi-team only.

**Multi-team mode**: All stages 5.1–5.7 run sequentially per team.

All 3 teams' post-analysis stages can be dispatched **in parallel across teams**, but within a single team the stages run **sequentially**. Dispatch all 3 teams' stage N in one message, gate all 3, then proceed to stage N+1.

### 5.1 Modality Corroboration

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/modality_corroboration.json` |
| Gate | File exists; `corroboration_attempts` array covers ≥50% of expression-only findings from the analysis synthesis |
| On failure | Re-dispatch with: "You must attempt external corroboration for ALL expression-only findings. Currently missing {N} findings from the required list." |
| Prompt inputs | Analysis synthesis, all round findings JSONs, MCP tools available |
| MCP tools | `search_pubmed`, `get_hpa_gene_info`, `search_open_targets`, `get_protein_interactions`, `get_functional_enrichment` |
| Max tokens in dispatch | ~20K |

### 5.2 Prediction Generation

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/prediction_plan.json` |
| Gate | File exists; `predictions` array has ≥10 entries; entries span ≥4 categories (mechanistic, therapeutic, biomarker, pathway) |
| On failure | Re-dispatch with category coverage requirement explicitly stated: "You must produce ≥10 predictions spanning all 4 categories: mechanistic, therapeutic, biomarker, pathway." |
| Prompt inputs | Analysis synthesis, hypotheses, approach spec |
| Max tokens in dispatch | ~15K |

### 5.3 Prediction Validation

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/prediction_validation.json` |
| Gate | File exists; `validation_results` array covers ALL `high_priority` predictions from prediction plan |
| On failure | Re-dispatch with list of high-priority prediction IDs that are missing validation results |
| Prompt inputs | Prediction plan, analysis data files, available validation datasets |
| Max tokens in dispatch | ~20K |

### 5.4 Validation Analysis

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/validation_results.json` |
| Gate | File exists |
| On failure | Skip if no validation data available; log reason in `gate_log.json` |
| Condition | **Only dispatched** if external validation data is available in data manifest |
| Prompt inputs | Prediction validation results, validation dataset paths |
| Max tokens in dispatch | ~15K |

### 5.5 Analysis Synthesis

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/analysis_synthesis.json` AND `{team}/analysis_synthesis.md` |
| Gate | **BOTH** files exist; `validated_findings` array in JSON has ≥3 entries |
| On failure | Re-dispatch with: "Both `analysis_synthesis.json` and `analysis_synthesis.md` are required. You must produce both. The JSON must have ≥3 validated findings." |
| Prompt inputs | All round findings JSONs, all round validator results, modality corroboration, prediction validation, prediction plan |
| Max tokens in dispatch | ~25K |

**Content gate (Bash):**
```bash
[ -f "${TEAM_DIR}/analysis_synthesis.json" ] || echo "GATE_FAIL: analysis_synthesis.json missing"
[ -f "${TEAM_DIR}/analysis_synthesis.md" ] || echo "GATE_FAIL: analysis_synthesis.md missing"
python3 -c "
import json
with open('${TEAM_DIR}/analysis_synthesis.json') as f:
    data = json.load(f)
assert len(data.get('validated_findings', [])) >= 3, 'Need >=3 validated findings'
print('GATE_PASS')
" 2>&1
```

### 5.6 Figure Composition

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/rebuild_composites.py` + ≥1 composite PDF in `{team}/figures/composites/` |
| Gate | `rebuild_composites.py` exists; ≥1 `.pdf` file exists in `{team}/figures/composites/`; `rebuild_composites.py` does NOT contain `imread` or `imshow`; NO composite contains dashboard content |
| On failure | Re-dispatch with: "Do not use imread/imshow. Composites must be generated programmatically from source data, not by compositing images." |
| Prompt inputs | All round figure paths, analysis synthesis, figure standards from `refs/figure_standards.md`, `prompts/figure_composition.md` (which includes the dashboard exclusion rules) |
| Max tokens in dispatch | ~15K |

**Rasterization gate (Bash) — run immediately after `rebuild_composites.py` is produced:**
```bash
grep -q 'imread\|imshow' "${TEAM_DIR}/rebuild_composites.py" && \
  echo "GATE_FAIL: rasterization detected — re-dispatch immediately" || \
  echo "GATE_PASS"
```

If output is `GATE_FAIL`, re-dispatch immediately. Do not proceed to findings dossier.

**Dashboard content gate (Bash) — run after rasterization gate passes:**
```bash
python3 -c "
import re

with open('${TEAM_DIR}/rebuild_composites.py') as f:
    code = f.read().lower()

# Check for dashboard/meta-analysis indicators in function names, titles, and comments
dashboard_patterns = [
    r'hypothesis.{0,10}(status|dashboard|triage|validation)',
    r'evidence.{0,10}(quality|trajectory)',
    r'prediction.{0,10}(priority|summary)',
    r'round.{0,5}summary',
    r'(finding|claim).{0,10}(validation|status).{0,10}(table|panel|chart)',
    r'data.{0,10}acquisition.{0,10}impact',
]

violations = []
for pat in dashboard_patterns:
    matches = re.findall(pat, code)
    if matches:
        violations.append(f'{pat}: {matches[:2]}')

if violations:
    print('GATE_FAIL: dashboard content detected in composites:')
    for v in violations:
        print(f'  - {v}')
    print('Re-dispatch with: Every panel must show biological data (heatmaps, survival curves, scatter plots, etc). Remove hypothesis status dashboards, triage panels, and validation summaries.')
else:
    print('GATE_PASS: no dashboard content detected')
" 2>&1
```

If output is `GATE_FAIL`, re-dispatch with the error message. The figure composition prompt
(`prompts/figure_composition.md`) contains the full exclusion list — ensure it is included
in the dispatch prompt.

### 5.7 Findings Dossier (Multi-Team Only)

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/findings_dossier.json` |
| Gate | File exists; `hypothesis_status` object covers ALL hypothesis IDs from `hypotheses.json`; `validated_findings` array has ≥3 entries |
| On failure | Re-dispatch with: "Missing hypotheses in `hypothesis_status`: {list of missing IDs}." |
| Prompt inputs | Analysis synthesis, prediction validation, hypotheses, approach, modality corroboration |
| Max tokens in dispatch | ~20K |

**Content gate (Bash):**
```bash
python3 -c "
import json
with open('${TEAM_DIR}/findings_dossier.json') as f:
    dossier = json.load(f)
with open('${TEAM_DIR}/hypotheses.json') as f:
    hyp = json.load(f)
hyp_ids = {h['id'] for h in hyp.get('hypotheses', [])}
status_ids = set(dossier.get('hypothesis_status', {}).keys())
missing = hyp_ids - status_ids
assert not missing, f'Missing hypothesis IDs in status: {missing}'
assert len(dossier.get('validated_findings', [])) >= 3, 'Need >=3 validated findings'
print('GATE_PASS')
" 2>&1
```

**Single-team mode**: Skip findings dossier. Paper Gen receives `analysis_synthesis.json` and `analysis_synthesis.md` directly.

---

