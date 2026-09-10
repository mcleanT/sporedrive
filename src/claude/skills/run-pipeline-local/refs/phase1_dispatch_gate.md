# Phase 1 Dispatch Card + Gate (Main Context)

> Relocated verbatim from `SKILL.md` during the 2026-09-08 mode-router restructure (source lines 120-167 of the pre-restructure file, preserved in the snapshot at `~/tools/codex-claude-workflow/snapshots/skills-20260908T202743Z/run-pipeline-local/SKILL.md`). This is main-context execution-contract content (dispatch cards / gates / model requirements), not subagent-facing protocol text.

**Load when:** Phase 1 (lit review + team assignment) dispatch, immediately after mode router selects standard/cohesive/multi-team pipeline execution.

---

## Section 2: Phase 1 Dispatch Card (Orchestrator)

Phase 1 is orchestrated — a single sonnet subagent reads the full phase 1 protocol and coordinates the 7-phase lit review plus team assignment internally.

```
Agent(
  model: "sonnet",
  description: "Phase 1: Literature Review + Team Assignment",
  max_turns: 40
)
```

**Include FULL text of BOTH ref files in the dispatch prompt:**
- `refs/phase1_orchestrator.md`
- `refs/lit_review_protocol.md`

**Pass:**
- `research_question`
- `output_dir` → `experiment outputs/{name}/`
- Pipeline config (n_teams, personas_per_team, pipeline_mode)

**Expected outputs:**
- `{run_dir}/lit_review/synthesis.json`
- `{run_dir}/team_assignments.json` (multi-team only; omitted in single-team mode)

**Gate (main context verifies after dispatch returns):**

```bash
RUN_DIR="experiment outputs/{name}"

[ -f "${RUN_DIR}/lit_review/synthesis.json" ] || echo "GATE_FAIL: synthesis.json missing"

# Multi-team only:
python3 -c "
import json
with open('${RUN_DIR}/team_assignments.json') as f:
    data = json.load(f)
assert len(data.get('teams', [])) == N_TEAMS, f'Expected {N_TEAMS} teams, got {len(data[\"teams\"])}'
print('GATE_PASS')
" 2>&1
```

**On failure:** Re-dispatch with: "Phase 1 incomplete. `synthesis.json` is missing or team count is wrong. Check which of the 7 lit review phases completed and restart from the checkpoint."

**Single-team mode:** Only `synthesis.json` is gated. No `team_assignments.json` is produced.

---

