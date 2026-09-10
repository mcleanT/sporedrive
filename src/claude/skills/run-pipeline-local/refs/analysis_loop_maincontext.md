# Analysis Loop Protocol — Main-Context Control (Never Delegated)

> Relocated verbatim from `SKILL.md` during the 2026-09-08 mode-router restructure (source lines 284-680 of the pre-restructure file, preserved in the snapshot at `~/tools/codex-claude-workflow/snapshots/skills-20260908T202743Z/run-pipeline-local/SKILL.md`). This is main-context execution-contract content (dispatch cards / gates / model requirements), not subagent-facing protocol text.

**Load when:** Every analysis round. Distinct from refs/analysis_loop.md, which is the per-round subagent-facing protocol included IN the dispatch prompt; this file is what the MAIN CONTEXT itself must do to drive and gate each round.

---

## Section 4: Analysis Loop Protocol (Main Context Controls — Never Delegated)

**THIS IS THE MOST CRITICAL SECTION.**

The analysis loop MUST be implemented in the main context as explicit sequential dispatches. **It must never be wrapped into a single "run the analysis loop" orchestrator dispatch.** An orchestrator receiving a loop protocol will compress it into a single pass and skip validators, roundtables, and enrichments. The main context controls the loop.

### 4.1 Loop Pseudocode

```
ALGORITHM: Analysis Loop with Reviewer Feedback

team_round = 0
max_rounds = config.max_analysis_rounds  # default 5
min_rounds = config.min_analysis_rounds  # default: discovery=4, validation=3, methods=5

WHILE team_round < max_rounds:
  team_round += 1

  # Dispatch all 3 teams' Round N in parallel (existing behavior)
  DISPATCH: Agent(team_1 Round N) + Agent(team_2 Round N) + Agent(team_3 Round N)
    Each team internally runs Steps 1-5:
      Step 1: Analysis agent → findings.json, code.py, figures/, round_summary.md
      Step 2: Validators (batch) → validator_results.json
      Step 2a-c: VLM, contradiction search, quick corroboration (conditional)
      Step 3: PI interpretation → pi_interpretation.json
      Step 4: Analysis roundtable → roundtable_verdict.json (now includes strongest_objection)
      Step 5: Adversarial assessment → adversarial_assessment.json (carries forward unresolved critiques)

  MASTER GATE: Verify ALL round artifacts for all 3 teams (existing behavior)
  IF ANY REQUIRED FILE MISSING: Re-dispatch responsible step (max 2 attempts)

  # >>> Branch allocation (if branching_analysis_enabled) — per team, after master gate <<<
  STEP 4b: For each team, read roundtable_verdict.next_hypotheses
           IF new sub-hypotheses AND active_branches < max_analysis_branches AND branch_budget > 0:
             Spawn new branch (track hypothesis, parent, round=1)
           Apply branch decisions: continue / prune / merge / converge
           Write {team}/branch_status.json with active branches, costs, narrative scores
           See refs/analysis_loop.md §13 for branch decision logic and narrative coherence test.

  # >>> Reviewer batch review (if adversarial review enabled) <<<
  DISPATCH: Agent(reviewer_team batch review of all 3 teams' Round N outputs)
  GATE: reviewer_team/round{N}/review.json exists; per_team_concerns has keys for all teams

  CONVERGENCE CHECK:
    IF team_round < min_rounds: CONTINUE (ignore convergence vote)
    ELIF ALL teams' roundtable_verdict.converged == true
         AND no CRITICAL validator flags
         AND no unresolved major reviewer concerns from round N
         AND adversarial_assessment.risk_score <= 0.7 (if present)
         AND all active branches converged/pruned/merged: BREAK
    ELIF team_round >= max_rounds: BREAK (force-converge remaining branches)
    ELSE: CONTINUE
      Pass to each team's Round N+1:
        - roundtable verdict (next_hypotheses, next_approach, strongest_objection)
        - reviewer_team/round{N}/review.json → per_team_concerns[team_id]
        - {team}/branch_status.json → active branches for parallel dispatch
      IF multiple active branches per team: dispatch parallel analysis agents (one per branch)
      Each branch gets its own iteration_context with branch-specific findings
```

**Cross-team interleaving (multi-team runs):** Dispatch ALL teams' Round N in a single message (3 Agent calls), gate all 3 before dispatching Round N+1. This cuts wall time by ~3× without additional cost.

```
Message: Agent(team_1 Round 1) + Agent(team_2 Round 1) + Agent(team_3 Round 1)
→ Gate all 3 teams' Round 1 master gate
Message: Agent(team_1 Round 2) + Agent(team_2 Round 2) + Agent(team_3 Round 2)
→ Gate all 3 teams' Round 2 master gate
...
```

**Within each round's Agent call**: Steps 1–4 run **sequentially inside the agent**. The parallelism is at the team level, not within per-round steps. Each team's round agent dispatches Steps 1–4 in sequence, verifies artifacts internally, and returns a round summary to the main context.

**Within-round parallelism** (inside the round agent, after Step 1 completes):
```
analysis_agent (Step 1) → DONE
    ↓
[validators (Step 2) + VLM interpreter (Step 2a) + contradiction search (Step 2b)]  ← parallel
    ↓
PI interpretation (Step 3)   ← depends on validators
    ↓
roundtable (Step 4)          ← depends on PI interpretation
    ↓
adversarial assessment (Step 5)  ← depends on roundtable
```

### 4.1.1 MANDATORY Pre-Dispatch: Domain Skill Selection

**Before dispatching ANY Round 1 analysis, the main context MUST select domain skills.**

This step runs ONCE per team before Round 1. The selected skills are included in every
subsequent round dispatch for that team.

**Procedure:**
1. Read the team's `approach.json` and `analysis_plan.json`
2. Read the team's `data_manifest.json` (check dataset types and file formats)
3. Match keywords against the table in `refs/analysis_loop.md` §4:

| Keywords in question/hypothesis/approach/data | Skill file |
|-----------------------------------------------|------------|
| spatial / Visium / MERFISH / Moran / neighborhood | `spatial_analysis.md` |
| RNA-seq / differential expression / count matrix / DEG | `bulk_rnaseq.md` |
| scRNA-seq / single-cell / 10x / h5ad / pseudobulk / Leiden | `scrna_seq.md` |
| survival / Kaplan-Meier / Cox regression / hazard ratio | `survival_analysis.md` |
| protein interaction / PPI / STRING / hub gene / network | `network_pathway.md` |
| somatic mutation / TMB / oncoplot / driver gene / VAF | `mutation_analysis.md` |
| clustering / cell type / UMAP / t-SNE / PCA | `clustering_analysis.md` |
| treatment vs control / condition comparison | `cross_condition.md` |
| time series / longitudinal / trajectory | `timeseries.md` |
| multi-omics / proteomics / metabolomics / MOFA | `multi_omics.md` |
| GWAS / Manhattan plot / SNP / eQTL | `gwas_genetic.md` |
| scL / L metric / Lorenz curve | `scl_metric.md` |

4. Read EACH matched skill file from `prompts/skills/{name}.md`
5. Log the selected skills: `{team}/selected_skills.json` with format:
   `{"skills": ["spatial_analysis", "survival_analysis", ...], "match_reasons": {...}}`

**Gate:** `{team}/selected_skills.json` exists with ≥1 skill selected. If the approach
involves biological data analysis and 0 skills match, something is wrong — re-check
with broader keyword search on the full approach text.

**Known failure mode (2026-04-03 liver_tme run):** All 3 teams ran 4 rounds of analysis
without ANY domain skills. The teams had spatial transcriptomics (needs `spatial_analysis.md`),
scRNA-seq (needs `scrna_seq.md`), survival data (needs `survival_analysis.md`), STRING
PPI networks (needs `network_pathway.md`), and mutation data (needs `mutation_analysis.md`).
None of these skills were included because skill selection was documented but not enforced.

**Round 1 dispatch**: Include full skill file content under `## DOMAIN ANALYSIS PROTOCOL`
**Round 2+ dispatch**: Condense each skill to critical rules only (lines containing
"must", "never", "always", "critical", "mandatory", "required", "forbidden" — max 15 rules per skill)

### 4.2 Step 1: Analysis Round

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifacts | `{team}/round{N}/findings.json`, `{team}/round{N}/code.py`, `{team}/round{N}/figures/` (≥1 PNG), `{team}/round{N}/round_summary.md` |
| Gate | All 4 artifacts exist; `findings.json` has `findings` array with ≥1 entry; `figures/` directory is non-empty |
| On failure | Re-dispatch with: "Missing: {list of missing files}. All 4 artifacts are mandatory. Do not declare success until all exist." |
| Prompt inputs (Round 1) | Column inventory, analysis plan, **ALL selected domain skill files from `selected_skills.json`** (full content), `refs/analysis_loop.md`, `refs/science_focus.md` |
| Prompt inputs (Round 2+) | All Round 1 inputs PLUS: prior round findings, PI interpretation, roundtable verdict, `next_hypotheses`, `next_approach` from prior roundtable, `reviewer_team/round{N-1}/review.json` → `per_team_concerns[team_id]` (if adversarial review enabled). Domain skills condensed to critical rules only. |
| Max tokens in dispatch | ~20K |

**Figure standards for all analysis code (include in every dispatch):**
- Colorblind-safe palette: `["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#F0E442", "#56B4E9", "#E69F00"]` or viridis/plasma for sequential
- Arial/Helvetica font; 12pt axis labels (bold); 10pt tick labels; 14pt titles (bold)
- 300 DPI minimum; prefer vector formats (PDF/SVG)
- `constrained_layout=True`; error bars on means; jitter/swarm for N<30
- `from autonomous_science.analytics import *` — analytics toolkit auto-applies theme
- See `refs/figure_standards.md` for full requirements

### 4.3 Step 2: Validators (Batched)

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/round{N}/validator_results.json` |
| Gate | File exists AND contains ALL 9 required keys (see table below). Additionally, `statistical.null_sensitivity_checks` MUST exist as a non-null array. If pathway analysis was performed: also `pathway` key required. |
| On failure | Count keys present. Re-dispatch with: "You produced {N}/9 validators. Missing: {list}. Run the missing validators and merge results into `validator_results.json`. Also ensure `statistical.null_sensitivity_checks` is present as an array." Maximum 2 re-dispatch attempts before logging partial results and continuing. |
| Prompt inputs | Round findings JSON, `code.py`, analysis plan |
| Max tokens in dispatch | ~20K |

**Required validator keys:**

| Key | Validator | Required Checks |
|-----|-----------|-----------------|
| `statistical` | Statistical validity | p-value distribution, multiple testing, effect sizes, assumptions; MUST include `null_sensitivity_checks` sub-key (array of null model / permutation tests) |
| `causal` | Causal inference | Confounders, effect direction, mechanistic plausibility |
| `embedding` | Embedding quality | Dimensionality stability, cluster separation, batch effects |
| `automl` | AutoML benchmark | Predictive performance, feature importance consistency |
| `evidence_typing` | Evidence typing | Data modality vs. claim type match |
| `comparator` | Comparator audit | Appropriate comparators, baseline validity |
| `novelty` | Novelty assessment | Literature overlap, incremental vs. novel findings |
| `synthetic_data` | Synthetic data | Data authenticity, distribution checks |
| `pathway` | Pathway enrichment | Gene set membership, enrichment calibration — **conditional**: required only if pathway analysis performed |

**Content gate (Bash):**
```bash
python3 -c "
import json
with open('${TEAM_DIR}/round${N}/validator_results.json') as f:
    data = json.load(f)
required = ['statistical', 'causal', 'embedding', 'automl', 'evidence_typing',
            'comparator', 'novelty', 'synthetic_data']
missing = [k for k in required if k not in data]
assert not missing, f'Missing validator keys: {missing}'
stat = data['statistical']
assert 'null_sensitivity_checks' in stat and stat['null_sensitivity_checks'] is not None, \
    'statistical.null_sensitivity_checks missing or null'
print('GATE_PASS')
" 2>&1
```

### 4.4 Step 2a: VLM Figure Interpretation

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/round{N}/figure_interpretations.json` |
| Gate | File exists AND has ≥1 entry per figure in `{team}/round{N}/figures/` |
| On failure | Skip (optional enrichment) but log: "WARNING: VLM interpretation unavailable for round {N}. Adding to roundtable context as absent." |
| Condition | Only dispatched if `figures/` directory is non-empty |
| Prompt inputs | Figure PNG paths (passed as image inputs to the agent), round findings JSON |
| Max tokens in dispatch | ~10K + image inputs |

### 4.5 Step 2b: Contradiction Search

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/round{N}/contradiction_search.json` |
| Gate | File exists (soft gate — failure is logged as warning but does NOT block the pipeline) |
| On failure | Log warning; add `"contradiction_search_unavailable": true` flag to roundtable context. Rationale: MCP search tools may be rate-limited. Missing contradiction data reduces quality but is not pipeline-blocking. |
| Condition | Only dispatched in round 2+ (round 1 findings may not be stable enough) |
| Prompt inputs | Key findings from round findings JSON, MCP tools available |
| MCP tools | `search_pubmed`, `search_semantic_scholar`, `search_openalex` |
| Max tokens in dispatch | ~15K |

### 4.6 Step 2c: Quick Corroboration

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/round{N}/quick_corroboration.json` |
| Gate | File exists (proceed without if absent — full corroboration happens post-loop) |
| On failure | Proceed without; log that full corroboration will be required in post-analysis |
| Condition | Only dispatched if `validator_results.json` → `evidence_typing.violations` is non-empty |
| Prompt inputs | Evidence typing violations list, key gene list from findings |
| MCP tools | `get_hpa_gene_info`, `search_open_targets`, `get_protein_interactions` |
| Max tokens in dispatch | ~12K |

### 4.7 Step 3: PI Interpretation Roundtable

| Field | Value |
|-------|-------|
| Model | opus |
| Artifact | `{team}/round{N}/pi_interpretation.json` |
| Gate | File exists AND has keys: `mechanism_explanations` (array), `sub_hypotheses` (array), `panel_agreement` (float 0–1), `surprise_findings` (array) |
| On failure | Re-dispatch once. If second failure: proceed with synthetic stub `{"panel_agreement": 0.5, "mechanism_explanations": [], "sub_hypotheses": [], "surprise_findings": [], "error": "roundtable_failed"}` and log warning |
| Prompt inputs | Round findings JSON, validator results, VLM interpretations (if available), team PI personas JSON, standing archetypes from `personas/standing_pis.py` |
| Max tokens in dispatch | ~25K |

**Content gate (Bash):**
```bash
python3 -c "
import json
with open('${TEAM_DIR}/round${N}/pi_interpretation.json') as f:
    data = json.load(f)
for k in ['mechanism_explanations', 'sub_hypotheses', 'panel_agreement', 'surprise_findings']:
    assert k in data, f'Missing key: {k}'
assert isinstance(data['panel_agreement'], (int, float)), 'panel_agreement must be a float'
print('GATE_PASS')
" 2>&1
```

### 4.8 Step 4: Analysis Roundtable

| Field | Value |
|-------|-------|
| Model | opus |
| Artifact | `{team}/round{N}/roundtable_verdict.json` AND (conditionally) `{team}/round{N}/dataset_recommendations.json` |
| Gate | `roundtable_verdict.json` exists AND has keys: `converged` (bool), `next_hypotheses` (array), `next_approach` (string), `critical_flags` (array). If `approach.json` → `data_needs` has entries with `priority: "critical"`: also verify `dataset_recommendations.json` exists. |
| On failure | Re-dispatch once. If second failure: synthetic NOT_CONVERGED verdict: `{"converged": false, "next_hypotheses": [], "next_approach": "retry_prior_analysis", "critical_flags": ["roundtable_failed"]}` |
| Prompt inputs | Round findings, PI interpretation, validator results, figure interpretations (if available), prior round verdicts (if round 2+), `refs/science_focus.md` content, branch status summary (if branching active — see `refs/analysis_loop.md` §13) |
| Branch allocation | When `branching_analysis_enabled` is True and branches exist, append the branch allocation prompt to the roundtable dispatch. The roundtable decides per-branch: continue / prune / merge / converge. New branches spawn from `next_hypotheses` that need independent data or methods. Write decisions to `{team}/branch_status.json`. |
| Max tokens in dispatch | ~25K |

**Content gate (Bash):**
```bash
python3 -c "
import json
with open('${TEAM_DIR}/round${N}/roundtable_verdict.json') as f:
    data = json.load(f)
for k in ['converged', 'next_hypotheses', 'next_approach', 'critical_flags']:
    assert k in data, f'Missing key: {k}'
assert isinstance(data['converged'], bool), 'converged must be bool'
assert 'strongest_objection' in data, 'Missing strongest_objection'
assert len(data.get('strongest_objection', '')) > 50, 'strongest_objection too brief — must be specific and testable'
print('GATE_PASS')
" 2>&1
```

### 4.8a Step 5: Adversarial Assessment

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `{team}/round{N}/adversarial_assessment.json` |
| Gate | File exists; `finding_critiques` array has entries for each finding; `risk_score` is float in [0, 1] |
| On failure | Proceed without — wrapped in error handler. Log warning in `gate_log.json`. Pipeline never halts on adversarial failure. |
| Prompt inputs | Round findings JSON, roundtable verdict, PI interpretation, prior round critiques (if round 2+), `prompts/adversarial_review.md` content |
| Max tokens in dispatch | ~15K |

**Behavior:**
- Produces per-finding critiques with evidence sufficiency assessment, alternative explanations, and overclaim detection
- Detects causal language ("causes", "drives", "induces") against evidence strength
- `risk_score` > 0.7 blocks convergence (forces another round)
- Unresolved critiques carry forward as `prior_adversarial_critiques` context in next round
- Wrapped in try/except — adversarial failure NEVER halts the pipeline

**Content gate (Bash):**
```bash
python3 -c "
import json
with open('${TEAM_DIR}/round${N}/adversarial_assessment.json') as f:
    data = json.load(f)
assert isinstance(data.get('finding_critiques'), list), 'finding_critiques must be array'
assert isinstance(data.get('risk_score'), (int, float)), 'risk_score must be numeric'
assert 0 <= data['risk_score'] <= 1, 'risk_score must be in [0, 1]'
print('GATE_PASS: risk_score={:.2f}, {} overclaimed findings'.format(
    data['risk_score'], len(data.get('overclaimed_findings', []))))
" 2>&1
```

### 4.9 Master Gate (After Each Round)

After completing Steps 1–4, the main context runs Bash `[ -f ]` checks for ALL required round artifacts before proceeding to convergence check or next round. **This is mandatory — do not skip.**

```bash
TEAM_DIR="experiment outputs/{name}/{team}"
N="{round_number}"
missing_files=()

# Required artifacts:
for f in \
  "${TEAM_DIR}/round${N}/findings.json" \
  "${TEAM_DIR}/round${N}/code.py" \
  "${TEAM_DIR}/round${N}/round_summary.md" \
  "${TEAM_DIR}/round${N}/validator_results.json" \
  "${TEAM_DIR}/round${N}/pi_interpretation.json" \
  "${TEAM_DIR}/round${N}/roundtable_verdict.json"
do
  [ -f "$f" ] || missing_files+=("$f")
done

# figures/ directory non-empty:
[ "$(ls -A "${TEAM_DIR}/round${N}/figures/" 2>/dev/null)" ] || \
  missing_files+=("${TEAM_DIR}/round${N}/figures/ (empty)")

# Conditional required (round 2+):
if [ "${N}" -gt 1 ]; then
  [ -f "${TEAM_DIR}/round${N}/contradiction_search.json" ] || \
    missing_files+=("${TEAM_DIR}/round${N}/contradiction_search.json (required round 2+)")
fi

# Conditional required (if data_needs has critical entries):
# Check approach.json for critical data_needs and gate accordingly

if [ ${#missing_files[@]} -gt 0 ]; then
  echo "MASTER_GATE_FAILURE: ${missing_files[*]}"
  # Log to gate_log.json and re-dispatch responsible step
else
  echo "MASTER_GATE_PASS: round ${N} complete"
fi
```

**Optional artifacts** (log if absent, do NOT block):
- `{team}/round{N}/figure_interpretations.json`
- `{team}/round{N}/quick_corroboration.json` (conditional on evidence_typing violations)
- `{team}/round{N}/adversarial_assessment.json` (wrapped in try/except — log if absent but never block)

**Gate failure response:**
1. Log missing file(s) to `experiment outputs/{name}/gate_log.json` (see Appendix B for schema)
2. Re-dispatch the responsible step with explicit callout: "GATE FAILURE: The following files were not produced: {list}. These files are MANDATORY and must be produced before you return."
3. Maximum 2 re-dispatch attempts per gate failure
4. After 2 failures: log `status: "permanent_failure"` in `gate_log.json`, proceed with warning, mark stage in audit PDF as FAILED

See `refs/gate_protocol.md` for reusable Bash gate check patterns.

### 4.10 Reviewer Batch Review (Per-Round, if adversarial review enabled)

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `reviewer_team/round{N}/review.json` |
| Gate | File exists; `per_team_concerns` has keys for all analysis teams |
| On failure | Re-dispatch once. If second failure: proceed without reviewer feedback for this round, log warning in `gate_log.json`. |
| Prompt inputs | All teams' `round{N}/findings.json` + `round_summary.md` + `validator_results.json`, prior round reviews (`reviewer_team/round{1..N-1}/review.json`), reviewer personas from `team_assignment.json`, `refs/reviewer_batch_review.md` content |
| Condition | Only dispatched if `adversarial_review` is enabled in config AND a reviewer team exists in `team_assignment.json` |
| Max tokens in dispatch | ~20K |

**Content gate (Bash):**
```bash
python3 -c "
import json
with open('${RUN_DIR}/reviewer_team/round${N}/review.json') as f:
    data = json.load(f)
teams = [t['team_id'] for t in json.load(open('${RUN_DIR}/shared/team_assignment.json')).get('teams', []) if t.get('role', 'analysis') == 'analysis']
for t in teams:
    assert t in data.get('per_team_concerns', {}), f'Missing concerns for {t}'
print('GATE_PASS')
" 2>&1
```

**Downstream**: Each team's Round N+1 analysis agent receives the reviewer concerns for their team as mandatory context. See `refs/reviewer_batch_review.md` for the full prompt template and `refs/analysis_loop.md` for how concerns are integrated into the Round 2+ dispatch format.

---

