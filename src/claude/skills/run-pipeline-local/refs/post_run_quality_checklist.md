# Post-Run Quality Checklist (MANDATORY)

> Relocated verbatim from `SKILL.md` during the 2026-09-08 mode-router restructure (source lines 1700-1752 of the pre-restructure file, preserved in the snapshot at `~/tools/codex-claude-workflow/snapshots/skills-20260908T202743Z/run-pipeline-local/SKILL.md`). This is main-context execution-contract content (dispatch cards / gates / model requirements), not subagent-facing protocol text.

**Load when:** After the final paper PDF is generated, before declaring the pipeline run complete.

---

## Section 8: Post-Run Quality Checklist (MANDATORY)

After the final paper PDF is generated and before declaring the pipeline complete, run the quality checklist from `memory/post_run_quality_checklist.md`.

**Dispatch:**
```
Agent(
  model: "sonnet",
  description: "Post-run quality checklist",
  prompt: "You are a post-run quality auditor. Read the paper at {run_dir}/final/paper_v*.md 
           (latest version) and the team findings at {run_dir}/team_*/round*/findings.json.
           
           Score EACH of these 10 items as PASS / PARTIAL / FAIL with a one-line justification:
           
           1. Claims match evidence type (no causal language for associations, no spatial claims from expression-space)
           2. Underpowered analyses flagged not claimed (N<50 or failed power check = 'exploratory')
           3. Abstract matches body evidence strength (no upgrades from 'suggestive' to 'reveals')
           4. Novelty claims distinguish integrative vs genuinely new
           5. Known biology acknowledged as known (canonical pathways = 'confirmed', not 'discovered')
           6. Zero dashboard panels in main figures (no hypothesis status, triage, evidence trajectory)
           7. All figures use real coordinate systems (spatial = tissue coords, not PCA/UMAP)
           8. Tier 1 reanalysis executed if applicable (code_executed: true for all computation concerns)
           9. Domain analysis skills loaded (selected_skills.json exists per team)
           10. Paper PDF is WeasyPrint not matplotlib (check creator metadata)
           
           Write scorecard to {run_dir}/quality_checklist.json with format:
           {\"items\": [{\"id\": 1, \"name\": \"...\", \"grade\": \"PASS\", \"justification\": \"...\"}], 
            \"pass_count\": N, \"summary\": \"one-line overall assessment\"}
           
           Also print a brief summary to stdout."
)
```

**Gate:**
```bash
python3 -c "
import json
with open('${RUN_DIR}/quality_checklist.json') as f:
    data = json.load(f)
passes = data.get('pass_count', 0)
total = len(data.get('items', []))
fails = [i for i in data.get('items', []) if i.get('grade') == 'FAIL']
print(f'Quality checklist: {passes}/{total} PASS')
if fails:
    print('FAILURES:')
    for f_item in fails:
        print(f'  [{f_item[\"id\"]}] {f_item[\"name\"]}: {f_item[\"justification\"]}')
if passes < 5:
    print('WARNING: <5 items passed — review before shipping')
" 2>&1
```

This is an informational gate (does not block), but FAIL items MUST be reported to the user before the run is declared complete.
