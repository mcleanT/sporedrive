# Appendix A/B — Master Gate Checklist, Gate Failure Log Format, Legacy Reference File Index

> Relocated verbatim from `SKILL.md` during the 2026-09-08 mode-router restructure (source lines 1463-1699 of the pre-restructure file, preserved in the snapshot at `~/tools/codex-claude-workflow/snapshots/skills-20260908T202743Z/run-pipeline-local/SKILL.md`). This is main-context execution-contract content (dispatch cards / gates / model requirements), not subagent-facing protocol text.

**Load when:** Consult any time a gate fails or you need the full list of expected artifacts across all phases; legacy reference-file index retained for cross-check against the new Loading Checklist in SKILL.md.

---

## Appendix A: Master Gate Checklist

Full list of ALL expected files at pipeline end for a **3-team multi-team run**. Use for final verification before declaring pipeline complete. See `refs/gate_protocol.md` for reusable Bash patterns.

### Phase 1 Outputs
```
experiment outputs/{name}/lit_review/synthesis.json
experiment outputs/{name}/team_assignments.json
```

### Per-Team Outputs (×3 teams: team_1, team_2, team_3)

**Pre-analysis:**
```
{team}/hypotheses.json
{team}/approach.json
{team}/data_manifest.json
{team}/data/  (≥1 file)
{team}/column_inventory.json
{team}/analysis_plan.json
{team}/data/preprocessed/  (≥1 .parquet file)
```

**Per analysis round (×N rounds):**
```
{team}/round{N}/findings.json
{team}/round{N}/code.py
{team}/round{N}/figures/  (non-empty)
{team}/round{N}/round_summary.md
{team}/round{N}/validator_results.json
{team}/round{N}/pi_interpretation.json
{team}/round{N}/roundtable_verdict.json
# Conditional:
{team}/round{N}/contradiction_search.json  (round 2+)
{team}/round{N}/dataset_recommendations.json  (if data_needs has critical entries)
# Optional:
{team}/round{N}/figure_interpretations.json
{team}/round{N}/quick_corroboration.json
```

**Post-analysis:**
```
{team}/modality_corroboration.json
{team}/prediction_plan.json
{team}/prediction_validation.json
{team}/validation_results.json  (conditional — if external validation data exists)
{team}/analysis_synthesis.json
{team}/analysis_synthesis.md
{team}/rebuild_composites.py
{team}/figures/composites/  (≥1 .pdf file)
{team}/findings_dossier.json
```

### Reviewer Team Outputs (per round)
```
reviewer_team/round{N}/review.json
```

### Phase 3 Cross-Team Outputs
```
cross_team/gene_panel_overlap.json
cross_team/meta_comparison.json
cross_team/convergence_validity.json
cross_team/dataset_reuse.json
cross_team/reanalysis_d{N}.json  (one per divergence)
cross_team/model_competition.json
cross_team/modality_harmonization.json
cross_team/claim_contracts.json
cross_team/novelty_assessment.json
final/synthesis_paper.md
```

### Phase 3.5 Adversarial Review Outputs
```
adversarial_review/checklist_results.json
adversarial_review/panel_review.json
adversarial_review/response/    (variable — tier0/tier1/tier2 artifacts per concern)
adversarial_review/resolution.json
adversarial_review/revised_synthesis.json
```

### Phase 4 Outputs
```
final/paper_draft.md
final/paper_v2.md
final/supplementary_tables.md
final/supplementary_figures.pdf
final/reference_validation.json
final/citation_provenance.json
final/sentence_claims.json
final/synthesis_ceiling.json
final/findings_network.json
final/graphical_abstract.pdf
final/audit_manifest.json
```

### Phase 5 Outputs
```
cleanup_manifest.json
audit/  (non-empty, ≥1 PDF)
```

### Final Verification (Bash)

```bash
RUN_DIR="experiment outputs/{name}"

# Count all expected JSON files:
find "${RUN_DIR}" -name "*.json" | wc -l

# Verify no required Phase 4 files missing:
for f in paper_v2.md supplementary_figures.pdf citation_provenance.json \
          sentence_claims.json synthesis_ceiling.json audit_manifest.json; do
  [ -f "${RUN_DIR}/final/${f}" ] || echo "MISSING: final/${f}"
done

# Verify per-team synthesis files:
for team in team_1 team_2 team_3; do
  [ -f "${RUN_DIR}/${team}/analysis_synthesis.json" ] || echo "MISSING: ${team}/analysis_synthesis.json"
  [ -f "${RUN_DIR}/${team}/analysis_synthesis.md" ] || echo "MISSING: ${team}/analysis_synthesis.md"
  [ -f "${RUN_DIR}/${team}/findings_dossier.json" ] || echo "MISSING: ${team}/findings_dossier.json"
done

# Verify Phase 3.5 adversarial review files (if enabled):
for f in checklist_results.json panel_review.json resolution.json revised_synthesis.json; do
  [ -f "${RUN_DIR}/adversarial_review/${f}" ] || echo "MISSING: adversarial_review/${f}"
done

echo "Checklist complete"
```

---

## Appendix B: Gate Failure Log Format

All gate failures are logged to `experiment outputs/{name}/gate_log.json`. Append a new entry for each failure. The main context writes this file directly via Bash tool.

**Schema:**

```json
{
  "gate_failures": [
    {
      "timestamp": "2026-03-24T14:32:00Z",
      "stage": "validators",
      "team": "team_1",
      "round": 2,
      "missing_files": [
        "experiment outputs/{name}/team_1/round2/validator_results.json"
      ],
      "missing_keys": ["causal", "embedding"],
      "attempt": 1,
      "status": "retrying",
      "redispatch_message": "GATE FAILURE: validator_results.json missing keys: causal, embedding. Run the missing validators and merge into validator_results.json."
    },
    {
      "timestamp": "2026-03-24T14:45:00Z",
      "stage": "validators",
      "team": "team_1",
      "round": 2,
      "missing_files": [],
      "missing_keys": [],
      "attempt": 2,
      "status": "permanent_failure",
      "redispatch_message": null,
      "note": "After 2 attempts, proceeding with partial validator_results.json. Stage marked FAILED in audit PDF."
    }
  ]
}
```

**Field definitions:**

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | string (ISO 8601) | When the failure was detected |
| `stage` | string | Stage name (e.g., `validators`, `pi_interpretation`, `analysis_synthesis`) |
| `team` | string | Team directory name (`team_1`, `team_2`, `team_3`, `shared`) |
| `round` | integer or null | Analysis round number; null for non-loop stages |
| `missing_files` | array of strings | Absolute paths of missing files |
| `missing_keys` | array of strings | Missing JSON keys (for content validation failures) |
| `attempt` | integer | Which attempt this is (1 or 2) |
| `status` | string | `retrying` (attempt 1) or `permanent_failure` (attempt 2) |
| `redispatch_message` | string or null | The exact message sent in the re-dispatch prompt |
| `note` | string or null | Human-readable context for permanent failures |

**Writing gate_log.json (Bash):**
```bash
# Append to gate_log.json (create if doesn't exist):
python3 -c "
import json, os, datetime

log_path = 'experiment outputs/{name}/gate_log.json'
if os.path.exists(log_path):
    with open(log_path) as f:
        log = json.load(f)
else:
    log = {'gate_failures': []}

log['gate_failures'].append({
    'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',
    'stage': '{stage}',
    'team': '{team}',
    'round': {round_or_null},
    'missing_files': {missing_files_list},
    'missing_keys': {missing_keys_list},
    'attempt': {attempt_number},
    'status': '{retrying_or_permanent_failure}',
    'redispatch_message': '{message_or_null}',
    'note': null
})

with open(log_path, 'w') as f:
    json.dump(log, f, indent=2)
print('gate_log.json updated')
"
```

---

## Reference File Index

| File | Used In | Required? |
|------|---------|-----------|
| `refs/phase1_orchestrator.md` | Phase 1 dispatch | NEVER skip |
| `refs/lit_review_protocol.md` | Phase 1 dispatch | NEVER skip |
| `refs/phase4_orchestrator.md` | Phase 4 dispatch | NEVER skip |
| `refs/phase5_orchestrator.md` | Phase 5 dispatch | NEVER skip |
| `refs/analysis_loop.md` | Step 1 (analysis round) dispatch | NEVER skip |
| `refs/science_focus.md` | Step 1 + Step 4 (roundtable) dispatch | NEVER skip |
| `refs/gate_protocol.md` | All Bash gate checks | Reference as needed |
| `refs/figure_standards.md` | Step 1 (analysis) + Fig Composition dispatch | Include in every analysis dispatch |
| `refs/adversarial_review.md` | Phase 3.5 dispatch (Section 6A) | NEVER skip (when adversarial review enabled) |
| `refs/reviewer_batch_review.md` | Section 4.10 (per-round reviewer) | NEVER skip (when adversarial review enabled) |

---

