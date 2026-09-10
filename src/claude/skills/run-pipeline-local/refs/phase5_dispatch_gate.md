# Phase 5 Dispatch Card + Gate (Post-Processing) + Single-Team Simplifications

> Relocated verbatim from `SKILL.md` during the 2026-09-08 mode-router restructure (source lines 1407-1462 of the pre-restructure file, preserved in the snapshot at `~/tools/codex-claude-workflow/snapshots/skills-20260908T202743Z/run-pipeline-local/SKILL.md`). This is main-context execution-contract content (dispatch cards / gates / model requirements), not subagent-facing protocol text.

**Load when:** After Phase 4 gate passes; single-team mode notes apply throughout.

---

## Section 8: Phase 5 Dispatch Card (Orchestrator)

Phase 5 is orchestrated — a single sonnet subagent handles all post-processing.

```
Agent(
  model: "sonnet",
  description: "Phase 5: Post-Processing, Audit, Cleanup",
  max_turns: 20
)
```

**Include FULL text of ref file in the dispatch prompt:**
- `refs/phase5_orchestrator.md`

**Pass:**
- `experiment_dir` (full path)
- `stages_completed` (list of completed stage names)
- `pipeline_config` (run mode, audit trail enabled flag, cost budget)

**Expected outputs:**
- Audit PDFs in `experiment outputs/{name}/audit/`
- `experiment outputs/{name}/cleanup_manifest.json`
- `.living/` update confirmation

**Gate:**
```bash
[ -f "experiment outputs/{name}/cleanup_manifest.json" ] && \
  echo "PHASE5_GATE_PASS" || \
  echo "PHASE5_GATE_FAIL: cleanup_manifest.json missing"
```

**Auto-generate PDFs**: After Phase 5 completes, automatically generate PDF reports from each stage's output. Do not wait for the user to request this.

---

## Single-Team Mode Simplifications

When running with `--n-teams 1` (or `pipeline_mode: standard` without multi-team), apply these changes:

| Change | Detail |
|--------|--------|
| Team directory | `team_dir = shared/` — all per-team outputs use `experiment outputs/{name}/shared/` |
| Phase 1 output | Only `lit_review/synthesis.json` — no `team_assignments.json` |
| Phase 3 (cross-team) | **Skip entirely** — no `cross_team/` directory created |
| Section 5.7 Findings Dossier | **Skip** — Paper Gen receives `analysis_synthesis.json` and `.md` directly |
| Phase 4 | Paper Gen dispatched flat as opus (not orchestrator); Paper Descent + nodes 3–9 still run in sequence |
| All gates | Apply unchanged with `team_dir = shared/` |
| Analysis loop | Runs identically (Sections 4.1–4.9), `team_dir = shared/` |
| Adversarial review (Phase 3.5) | Reviewer team created with 5 personas (no overlap constraint needed). Per-round review has 1 team key (`shared`). Phase 3.5 uses `analysis_synthesis.json/.md` as input instead of `final/synthesis_paper.md`. No adversarial-mode team privilege. |
| Data cleanup (Phase 3.6) | Same behavior — runs after adversarial review resolution. Cleanup safety constraint applies identically. |

Single-team mode is the recommended mode for development validation: `--n-teams 1 --dry-run`.

---

