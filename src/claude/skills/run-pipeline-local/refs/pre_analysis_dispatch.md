# Pre-Analysis Flat Dispatch Contracts (Hypothesis / Approach / Data Acquisition / Analysis Planner)

> Relocated verbatim from `SKILL.md` during the 2026-09-08 mode-router restructure (source lines 168-283 of the pre-restructure file, preserved in the snapshot at `~/tools/codex-claude-workflow/snapshots/skills-20260908T202743Z/run-pipeline-local/SKILL.md`). This is main-context execution-contract content (dispatch cards / gates / model requirements), not subagent-facing protocol text.

**Load when:** After Phase 1 gate passes, before entering the analysis loop.

---

## Section 3: Pre-Analysis Flat Dispatch Contracts

Each stage below is a single dispatch per team. **All 3 teams are dispatched in parallel** (3 Agent tool calls in one message) within each stage. Each stage **blocks on the prior stage's gate passing** before dispatching.

Dispatch pattern:
```
Message: Agent(team_1 {stage}) + Agent(team_2 {stage}) + Agent(team_3 {stage})
→ Gate ALL 3 teams before proceeding to next stage
```

### 3.1 Hypothesis Generation

| Field | Value |
|-------|-------|
| Model | opus |
| Artifact | `{team}/hypotheses.json` |
| Gate | File exists; `hypotheses` array has ≥3 entries; each entry has a `predictions` array |
| On failure | Re-dispatch once with: "Your output is missing or malformed. Produce `hypotheses.json` with ≥3 hypotheses, each containing a `predictions` array." |
| Prompt inputs | Research question, lit review synthesis, team assignment, `prompts/hypothesis_gen.md` content |
| Max tokens in dispatch | ~15K |

**Content gate (Bash):**
```bash
python3 -c "
import json, sys
with open('${TEAM_DIR}/hypotheses.json') as f:
    data = json.load(f)
assert len(data.get('hypotheses', [])) >= 3, 'Need >=3 hypotheses'
for h in data['hypotheses']:
    assert 'predictions' in h, f'Missing predictions in hypothesis {h.get(\"id\")}'
print('GATE_PASS')
" 2>&1
```

### 3.2 Approach Generation

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/approach.json` |
| Gate | File exists; `accessions` array non-empty with real identifiers (GSE\*/E-MTAB-\*/TCGA-\*); `source_categories` has ≥2 distinct categories |
| On failure | Re-dispatch with: "Missing `approach.json` or insufficient data sources. You must specify ≥2 source categories (expression, clinical, proteomic, etc.)." |
| Prompt inputs | Hypotheses JSON, lit review synthesis, `prompts/approach_gen.md` content, dataset catalog search results |
| Max tokens in dispatch | ~20K |

**Content gate (Bash):**
```bash
python3 -c "
import json
with open('${TEAM_DIR}/approach.json') as f:
    data = json.load(f)
assert len(data.get('accessions', [])) >= 1, 'No accessions'
cats = data.get('source_categories', [])
assert len(set(cats)) >= 2, f'Need >=2 source categories, got {cats}'
print('GATE_PASS')
" 2>&1
```

### 3.3 Data Acquisition

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/data_manifest.json` + `{team}/data/` directory with ≥1 file |
| Gate | `data_manifest.json` exists; `datasets` array non-empty; at least one `local_path` file exists on disk |
| On failure | Re-dispatch with explicit list of missing files and the accessions to retry |
| Prompt inputs | Approach spec, run mode sizing context, `prompts/data_acquisition.md` content |
| Max tokens in dispatch | ~15K |

**Run mode sizing context to include:**
- `local`: Max 200 MB/file, 500 MB total
- `api`: Max 1 GB/file, 2 GB total
- `distributed`: Max 15 GB/file, no total limit

**Content gate (Bash):**
```bash
python3 -c "
import json, os
with open('${TEAM_DIR}/data_manifest.json') as f:
    data = json.load(f)
datasets = data.get('datasets', [])
assert len(datasets) >= 1, 'No datasets in manifest'
found = [d for d in datasets if os.path.exists(d.get('local_path', ''))]
assert len(found) >= 1, 'No local_path files exist on disk'
print('GATE_PASS')
" 2>&1
```

### 3.4 Analysis Planner

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/column_inventory.json` + `{team}/analysis_plan.json` + preprocessed Parquet files in `{team}/data/preprocessed/` |
| Gate | Both JSON files exist; `column_inventory.json` has `verified_columns` key (populated by actual code execution); ≥1 `.parquet` file in `{team}/data/preprocessed/` |
| On failure | Re-dispatch with: "You must execute Python to verify columns. `column_inventory.json` must contain columns from actual code execution, not from guessing." |
| Prompt inputs | Data manifest, approach spec, `prompts/analysis_planner.md` content |
| Max tokens in dispatch | ~15K |

**Content gate (Bash):**
```bash
python3 -c "
import json
with open('${TEAM_DIR}/column_inventory.json') as f:
    data = json.load(f)
assert 'verified_columns' in data, 'verified_columns key missing — must be from actual execution'
assert len(data['verified_columns']) > 0, 'verified_columns is empty'
print('GATE_PASS')
" 2>&1

ls "${TEAM_DIR}/data/preprocessed/"*.parquet 2>/dev/null | wc -l | \
  awk '{if ($1 >= 1) print "GATE_PASS"; else print "GATE_FAIL: no parquet files"}'
```

---

