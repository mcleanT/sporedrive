# Phase 4 Dispatch Card + Gate (Paper Gen + QA)

> Relocated verbatim from `SKILL.md` during the 2026-09-08 mode-router restructure (source lines 1315-1406 of the pre-restructure file, preserved in the snapshot at `~/tools/codex-claude-workflow/snapshots/skills-20260908T202743Z/run-pipeline-local/SKILL.md`). This is main-context execution-contract content (dispatch cards / gates / model requirements), not subagent-facing protocol text.

**Load when:** After the Pre-Phase 4 Hard Gate (end of adversarial_review_dispatch.md) passes.

---

## Section 7: Phase 4 Dispatch Card (Orchestrator)

Phase 4 is orchestrated — a single opus subagent handles the full 9-node post-synthesis chain.

```
Agent(
  model: "opus",
  description: "Phase 4: Paper Generation + Post-Synthesis QA",
  max_turns: 40
)
```

**Include FULL text of ref file in the dispatch prompt:**
- `refs/phase4_orchestrator.md`

**Pass:**
- `experiment_dir`
- All team findings dossiers (multi-team) OR analysis synthesis (single-team)
- Cross-team comparison outputs (`cross_team/` directory) (multi-team only)
- Lit review synthesis path

**Expected outputs (9-node post-synthesis chain):**

| Node | Output | Gate |
|------|--------|------|
| 1. Paper Gen | `final/paper_draft.md` | File exists |
| 2. Paper Descent | `final/paper_v2.md` | File exists, all IMRAD sections present, word count ≥3000, citation count ≥10 |
| 3. Supplementary Tables | `final/supplementary_tables.md` | File exists |
| 4. Supplementary Figures | `final/supplementary_figures.pdf` | File exists, size >100KB |
| 5. Reference Validation | `final/reference_validation.json` | File exists (DOI resolution + retraction check) |
| 6. Citation Provenance | `final/citation_provenance.json` | File exists |
| 7. Sentence Claim Governance | `final/sentence_claims.json` | File exists |
| 8. Synthesis Claim Ceiling | `final/synthesis_ceiling.json` | File exists |
| 9. Findings Network + Graphical Abstract + Audit Manifest | `final/findings_network.json` + `final/graphical_abstract.pdf` + `final/audit_manifest.json` | All 3 files exist |

**Gate (main context verifies — ALL must pass):**

```bash
RUN_DIR="experiment outputs/{name}"
missing=()

[ -f "${RUN_DIR}/final/paper_v2.md" ] || missing+=("paper_v2.md")
[ -f "${RUN_DIR}/final/paper_v2.pdf" ] || missing+=("paper_v2.pdf")
[ -f "${RUN_DIR}/final/supplementary_figures.pdf" ] || missing+=("supplementary_figures.pdf")
[ -f "${RUN_DIR}/final/citation_provenance.json" ] || missing+=("citation_provenance.json")
[ -f "${RUN_DIR}/final/sentence_claims.json" ] || missing+=("sentence_claims.json")
[ -f "${RUN_DIR}/final/synthesis_ceiling.json" ] || missing+=("synthesis_ceiling.json")
[ -f "${RUN_DIR}/final/audit_manifest.json" ] || missing+=("audit_manifest.json")

# Word count check:
wc -w "${RUN_DIR}/final/paper_v2.md" | awk '{if ($1 < 3000) print "GATE_FAIL: paper < 3000 words (" $1 ")"}'

# Citation count check:
python3 -c "
import re
with open('${RUN_DIR}/final/paper_v2.md') as f:
    text = f.read()
sections = ['Introduction', 'Methods', 'Results', 'Discussion']
missing_s = [s for s in sections if s not in text]
assert not missing_s, f'Missing IMRAD sections: {missing_s}'
refs = re.findall(r'\[\d+\]|\[[@\w]+\]', text)
assert len(refs) >= 10, f'Need >=10 citations, found {len(refs)}'
print('GATE_PASS')
" 2>&1

# PDF size check (hard gate — paper_v2.pdf must exist and be >500KB):
python3 -c "
import os
size = os.path.getsize('${RUN_DIR}/final/paper_v2.pdf')
assert size > 500*1024, f'PDF too small: {size} bytes (need >500KB)'
print('GATE_PASS: PDF {:.0f}KB'.format(size/1024))
" 2>&1

# Supplementary figures size check:
python3 -c "
import os
size = os.path.getsize('${RUN_DIR}/final/supplementary_figures.pdf')
assert size > 100*1024, f'Supp figures PDF too small: {size} bytes (need >100KB)'
print('GATE_PASS')
" 2>&1

if [ ${#missing[@]} -gt 0 ]; then
  echo "PHASE4_GATE_FAILURE: ${missing[*]}"
else
  echo "PHASE4_GATE_PASS"
fi
```

**Single-team mode:** Paper Gen dispatched as a flat opus dispatch (not an orchestrator) receiving `analysis_synthesis.json` and `analysis_synthesis.md` directly. Paper Descent and the full 9-node post-synthesis chain (nodes 3–9) still run in sequence as normal inside the Phase 4 orchestrator.

---

