# Phase 3.5 Adversarial Review Dispatch Contracts (Main Context)

> Relocated verbatim from `SKILL.md` during the 2026-09-08 mode-router restructure (source lines 964-1314 of the pre-restructure file, preserved in the snapshot at `~/tools/codex-claude-workflow/snapshots/skills-20260908T202743Z/run-pipeline-local/SKILL.md`). This is main-context execution-contract content (dispatch cards / gates / model requirements), not subagent-facing protocol text.

**Load when:** When adversarial review is enabled, after Phase 3 cross-team synthesis. Distinct from refs/adversarial_review.md, which is the subagent-facing FM checklist/protocol content included IN these dispatch prompts.

---

## Section 6A: Phase 3.5 Adversarial Review Dispatch Contracts

**Multi-team and single-team.** These stages run AFTER Phase 3 (Section 6) completes — specifically after `final/synthesis_paper.md` passes its gate. They run sequentially. All dispatches reference `refs/adversarial_review.md` for full prompt templates.

Single-team mode: Phase 3 is skipped but Phase 3.5 still runs, using `analysis_synthesis.json` and `.md` as input instead of `final/synthesis_paper.md`. The reviewer team reviews the single team's work.

### 6A.1 Failure Mode Checklist

| Field | Value |
|-------|-------|
| Model | 10× haiku (FM-01,02,04,05,07,08,09,10,11,12,14) + 4× sonnet (FM-03,06,13,16) + 1× haiku (FM-15) |
| Artifact | `adversarial_review/checklist_results.json` |
| Gate | File exists; all 16 check IDs present (`skipped` status counts as present) |
| On failure | Re-dispatch missing checks only (max 2 attempts). After 2 failures per check: include with `status: "error"`. |
| Prompt inputs | Per-check inputs specified in `refs/adversarial_review.md` §1. FM-03/FM-06/FM-13/FM-16 require MCP tools (search_pubmed, search_semantic_scholar, get_pathways_for_gene, get_functional_enrichment). |
| Parallelization | All 16 checks dispatched in a single message (16 Agent calls) |
| MCP fallback | FM-03 and FM-06 produce `status: "skipped"` with `reason: "mcp_unavailable"` if MCP tools fail. Reviewer panel (6A.2) is informed. |
| Max tokens per check | ~5K in, ~1K out |

**Content gate (Bash):**
```bash
python3 -c "
import json
with open('\${RUN_DIR}/adversarial_review/checklist_results.json') as f:
    data = json.load(f)
check_ids = {c['check_id'] for c in data['checks']}
required = {'FM-01','FM-02','FM-03','FM-04','FM-05','FM-06','FM-07','FM-08','FM-09','FM-10','FM-11','FM-12','FM-13','FM-14','FM-15','FM-16'}
missing = required - check_ids
assert not missing, f'Missing checks: {missing}'
for c in data['checks']:
    assert c.get('status') in ('pass', 'fail', 'partial', 'skipped', 'error'), f'Invalid status for {c[\"check_id\"]}'
print('GATE_PASS: checklist ({} passed, {} failed, {} skipped)'.format(
    sum(1 for c in data['checks'] if c['status'] == 'pass'),
    sum(1 for c in data['checks'] if c['status'] == 'fail'),
    sum(1 for c in data['checks'] if c['status'] == 'skipped')
))
" 2>&1
```

### 6A.2 Reviewer Panel Assessment

| Field | Value |
|-------|-------|
| Model | opus |
| Artifact | `adversarial_review/panel_review.json` |
| Gate | File exists; `editorial_synthesis.decision` is present and valid; `unified_major_concerns` is an array |
| On failure | Re-dispatch once. If second failure: synthetic verdict `{"editorial_synthesis": {"decision": "major_revisions", "unified_major_concerns": [], "unified_minor_concerns": [], "priority_order": []}, "reviewers": []}` |
| Prompt inputs | Synthesis text (`final/synthesis_paper.md` or `analysis_synthesis.md`), `checklist_results.json`, all `reviewer_team/round{1..N}/review.json`, findings from adversarial-mode team, reviewer personas from `team_assignment.json`, `refs/adversarial_review.md` §2 |
| Max tokens | ~25K in, ~5K out |

**Editorial decision behavioral mapping:**

| Decision | Behavior |
|----------|----------|
| `accept` | Skip tiered response (6A.3) entirely. Proceed to resolution (6A.4) with all concerns marked informational. |
| `minor_revisions` | Execute Tier 0 + Tier 1 only. Tier 2 is blocked regardless of Tier 1 results. |
| `major_revisions` | Execute all tiers. Tier 2 eligible if Tier 1 changes major conclusions. |

**Content gate (Bash):**
```bash
python3 -c "
import json
with open('\${RUN_DIR}/adversarial_review/panel_review.json') as f:
    data = json.load(f)
assert 'editorial_synthesis' in data, 'Missing editorial_synthesis'
d = data['editorial_synthesis']
assert d.get('decision') in ('accept', 'minor_revisions', 'major_revisions'), f'Invalid decision: {d.get(\"decision\")}'
assert isinstance(d.get('unified_major_concerns'), list), 'unified_major_concerns must be array'
print('GATE_PASS: panel_review (decision={}, {} major, {} minor concerns)'.format(
    d['decision'], len(d['unified_major_concerns']), len(d.get('unified_minor_concerns', []))
))
" 2>&1
```

### 6A.3 Tiered Response

**Dispatch scope controlled by `editorial_synthesis.decision`:**
- `accept` → skip 6A.3 entirely, proceed to 6A.4
- `minor_revisions` → Tier 0 + Tier 1 only (Tier 2 blocked)
- `major_revisions` → all tiers eligible

#### 6A.3a Tier 0 — Language Fixes

| Field | Value |
|-------|-------|
| Model | haiku (parallel per concern) |
| Artifact | `adversarial_review/response/tier0_{concern_id}.json` per concern |
| Gate | One file per `resolution_type == "language"` concern |
| Condition | Only concerns with `resolution_type == "language"` from `panel_review.json` |
| Prompt inputs | Concern description, affected claim text, section, `refs/adversarial_review.md` §3a |
| Max tokens | ~2K per concern |

#### 6A.3b Tier 1 — Targeted Reanalysis

| Field | Value |
|-------|-------|
| Model | sonnet (parallel per concern) |
| Artifact | `adversarial_review/response/tier1_{concern_id}.json` + `_code.py` + `_figures/` |
| Gate | One JSON per `resolution_type == "computation"` concern; `code_executed: true` in each |
| Condition | Only concerns with `resolution_type == "computation"` from `panel_review.json` |
| Prompt inputs | Concern description, suggested analysis, preprocessed data file paths, original analysis code paths, `refs/adversarial_review.md` §3b |
| Data access | Preprocessed parquet files from team `data/preprocessed/` directories |
| Max tokens | ~10K per concern |

**CRITICAL — Tier 1 is NOT optional for `minor_revisions` or `major_revisions`.**
Creating a planning artifact with `"status": "pending"` does NOT satisfy this gate.
Each concern with `resolution_type == "computation"` MUST be dispatched as a sonnet
subagent that executes code and writes results. The main context MUST verify
`code_executed: true` in every response JSON before proceeding to 6A.3c.

**Known failure mode (2026-04-03 liver_tme run):** The main context created
`tier1_targeted_reanalysis.json` listing 4 reanalyses as "pending" and then
proceeded to 6A.4 Resolution without dispatching any of them. This produced a
paper with unaddressed computational concerns. This gate prevents that.

**Content gate (Bash) — HARD GATE, blocks 6A.3c:**
```bash
python3 -c "
import json, glob, sys

with open('\${RUN_DIR}/adversarial_review/panel_review.json') as f:
    panel = json.load(f)

decision = panel.get('editorial_synthesis', {}).get('decision', 'accept')
if decision == 'accept':
    print('GATE_PASS: decision=accept, no Tier 1 needed')
    sys.exit(0)

# Collect all computation-type concerns
ed = panel.get('editorial_synthesis', {})
computation_concerns = []
for c in ed.get('unified_major_concerns', []) + ed.get('unified_minor_concerns', []):
    if c.get('resolution_type') == 'computation':
        computation_concerns.append(c['concern_id'])

if not computation_concerns:
    print('GATE_PASS: no computation-type concerns')
    sys.exit(0)

# Verify each has a response with code_executed: true
missing = []
not_executed = []
for cid in computation_concerns:
    path = f'\${RUN_DIR}/adversarial_review/response/tier1_{cid}.json'
    try:
        with open(path) as f:
            data = json.load(f)
        if not data.get('code_executed'):
            not_executed.append(cid)
    except FileNotFoundError:
        missing.append(cid)

if missing or not_executed:
    msg = []
    if missing:
        msg.append(f'Missing response files: {missing}')
    if not_executed:
        msg.append(f'code_executed != true: {not_executed}')
    print('GATE_FAIL: ' + '; '.join(msg))
    sys.exit(1)

print(f'GATE_PASS: all {len(computation_concerns)} Tier 1 reanalyses executed')
" 2>&1
```

**On failure:** Dispatch a sonnet subagent per missing/unexecuted concern. Max 2 re-dispatch
attempts per concern. If a concern cannot be computationally resolved after 2 attempts,
mark it as `"disposition": "flagged_as_limitation"` in the response JSON (with
`code_executed: false, failure_reason: "..."`) and proceed — but the gate script above
must be re-run to confirm all concerns are now accounted for.

#### 6A.3c Tier Escalation Check

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `adversarial_review/response/tier_escalation.json` |
| Gate | File exists; `escalate_to_tier2` is boolean |
| Condition | Dispatched after ALL Tier 1 responses complete |
| Prompt inputs | All Tier 1 response JSONs, original synthesis claims, `refs/adversarial_review.md` §3c |
| Decision | Escalate if: finding confidence drops to preliminary/open_question, reanalysis contradicts claim, or projected correction now executed with >30% effect size change |
| Blocked | If `editorial_synthesis.decision == "minor_revisions"` → `escalate_to_tier2: false` regardless |

#### 6A.3d Tier 2 — Full Response Round (max 1, hard cap)

| Field | Value |
|-------|-------|
| Model | sonnet (analysis) + sonnet (validators) |
| Artifact | `adversarial_review/response/tier2_round/{findings.json, code.py, figures/, round_summary.md, validator_results.json}` |
| Gate | All 5 artifacts exist; `findings.json` has `findings` array |
| Condition | Only if `tier_escalation.json` → `escalate_to_tier2: true` |
| NOT a re-entry | PI interpretation skipped. Reviewer team evaluates findings (not analysis roundtable). |
| Prompt inputs | Unresolved concerns, Tier 1 results that changed conclusions, data paths, `refs/adversarial_review.md` §3d |
| Max tokens | ~20K |

### 6A.4 Review Resolution

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `adversarial_review/resolution.json` |
| Gate | File exists; all concern IDs from `panel_review.json` accounted for in `resolved_concerns` or `unresolved_concerns` |
| On failure | Re-dispatch once. If second failure: synthetic resolution marking all concerns as `flagged_as_limitation`. |
| Prompt inputs | All tier response artifacts, original `panel_review.json` concerns, `refs/adversarial_review.md` §4 |
| Max tokens | ~10K in, ~2K out |

**Content gate (Bash):**
```bash
python3 -c "
import json
with open('\${RUN_DIR}/adversarial_review/resolution.json') as f:
    res = json.load(f)
with open('\${RUN_DIR}/adversarial_review/panel_review.json') as f:
    rev = json.load(f)
# Use deduplicated editorial concerns (not per-reviewer, which may have duplicates)
ed = rev.get('editorial_synthesis', {})
all_concerns = set()
for c in ed.get('unified_major_concerns', []) + ed.get('unified_minor_concerns', []):
    all_concerns.add(c['concern_id'])
resolved_ids = {c['concern_id'] for c in res.get('resolved_concerns', [])}
unresolved_ids = {c['concern_id'] for c in res.get('unresolved_concerns', [])}
accounted = resolved_ids | unresolved_ids
missing = all_concerns - accounted
assert not missing, f'Unaccounted concerns: {missing}'
print('GATE_PASS: resolution ({} resolved, {} unresolved)'.format(len(resolved_ids), len(unresolved_ids)))
" 2>&1
```

### 6A.5 Synthesis Revision

| Field | Value |
|-------|-------|
| Model | sonnet |
| Artifact | `adversarial_review/revised_synthesis.json` |
| Gate | File exists; `revised_synthesis_md` is non-empty; `revisions` array accounts for all concerns from `resolution.json` |
| On failure | Re-dispatch once with explicit instruction: "revised_synthesis_md must contain the COMPLETE revised synthesis as markdown text, not a summary or diff." |
| Prompt inputs | Original synthesis, `resolution.json`, all tier response artifacts, `revised_claim_confidence` map, `refs/adversarial_review.md` §5 |
| Max tokens | ~25K in, ~8K out |

**Content gate (Bash):**
```bash
python3 -c "
import json
with open('\${RUN_DIR}/adversarial_review/revised_synthesis.json') as f:
    data = json.load(f)
assert data.get('revised_synthesis_md'), 'revised_synthesis_md is empty'
assert len(data['revised_synthesis_md']) > 1000, 'revised_synthesis_md too short — must be complete synthesis'
revisions = data.get('revisions', [])
limitations = data.get('added_limitations', [])
assert len(revisions) > 0 or len(limitations) > 0, 'No revisions or limitations recorded'
print('GATE_PASS: revised_synthesis ({} revisions, {} limitations, {} chars)'.format(
    len(revisions), len(limitations), len(data['revised_synthesis_md'])
))
" 2>&1
```

### 6A.6 Data Cleanup (Phase 3.6)

| Field | Value |
|-------|-------|
| Model | N/A (deterministic Bash execution, no LLM) |
| Artifact | `cleanup_manifest.json` |
| Gate | File exists (or cleanup was legitimately skipped — see safety constraint) |
| Safety constraint | If `resolution.json` has ANY `unresolved_concerns` with `disposition: "requires_further_analysis"`, cleanup is SKIPPED entirely and a warning is logged. Also skipped if `config.run_mode == "distributed"`. |
| Behavior | Same deletion rules as previous cleanup: raw extensions (`.h5ad`, `.mtx`, `.tsv`, `.txt`, `.rds`, `.tar`, `.gz`, `.zip`) deleted; preprocessed (`.parquet`, `.json`, `.csv`, `.py`) kept. Multi-team: clean all team `data/` directories. |

**Safety check before cleanup (Bash):**
```bash
python3 -c "
import json
with open('\${RUN_DIR}/adversarial_review/resolution.json') as f:
    res = json.load(f)
needs_further = [c for c in res.get('unresolved_concerns', [])
                 if c.get('disposition') == 'requires_further_analysis']
if needs_further:
    print('CLEANUP_SKIPPED: {} concerns require further analysis'.format(len(needs_further)))
else:
    print('CLEANUP_ELIGIBLE')
" 2>&1
```

If output is `CLEANUP_SKIPPED`: do not run cleanup, log the skip reason to `gate_log.json`, proceed to Phase 4.
If output is `CLEANUP_ELIGIBLE`: run cleanup using the same logic as the previous `_post_analysis_cleanup_node` (delete raw extensions, keep preprocessed, write `cleanup_manifest.json`).

### Pre-Phase 4 Hard Gate — Adversarial Review Completeness

**THIS GATE MUST PASS BEFORE DISPATCHING THE PHASE 4 ORCHESTRATOR.**

This gate verifies that the adversarial review pipeline actually completed — not just
that artifacts were created with placeholder/pending statuses. It catches the known
failure mode where Tier 1 reanalyses are planned but never executed.

```bash
python3 -c "
import json, sys

RUN_DIR = '\${RUN_DIR}'

# 1. revised_synthesis.json must exist and be non-trivial
try:
    with open(f'{RUN_DIR}/adversarial_review/revised_synthesis.json') as f:
        rs = json.load(f)
    assert rs.get('revised_synthesis_md'), 'revised_synthesis_md empty'
    assert len(rs['revised_synthesis_md']) > 1000, 'revised_synthesis_md too short'
except (FileNotFoundError, AssertionError) as e:
    print(f'GATE_FAIL: revised_synthesis.json — {e}')
    sys.exit(1)

# 2. If decision was not 'accept', verify Tier 1 reanalyses actually ran
with open(f'{RUN_DIR}/adversarial_review/panel_review.json') as f:
    panel = json.load(f)
decision = panel.get('editorial_synthesis', {}).get('decision', 'accept')

if decision != 'accept':
    ed = panel.get('editorial_synthesis', {})
    computation_concerns = [
        c['concern_id']
        for c in ed.get('unified_major_concerns', []) + ed.get('unified_minor_concerns', [])
        if c.get('resolution_type') == 'computation'
    ]
    for cid in computation_concerns:
        path = f'{RUN_DIR}/adversarial_review/response/tier1_{cid}.json'
        try:
            with open(path) as f:
                data = json.load(f)
            if not data.get('code_executed') and data.get('disposition') != 'flagged_as_limitation':
                print(f'GATE_FAIL: tier1_{cid}.json has code_executed=false without limitation flag')
                sys.exit(1)
        except FileNotFoundError:
            print(f'GATE_FAIL: tier1_{cid}.json missing — Tier 1 reanalysis never dispatched')
            sys.exit(1)

# 3. resolution.json must exist
try:
    with open(f'{RUN_DIR}/adversarial_review/resolution.json') as f:
        json.load(f)
except FileNotFoundError:
    print('GATE_FAIL: resolution.json missing')
    sys.exit(1)

print('GATE_PASS: adversarial review complete, Phase 4 may proceed')
" 2>&1
```

**On failure:** Do NOT proceed to Phase 4. Return to the failed substage:
- Missing `revised_synthesis.json` → re-dispatch 6A.5
- Tier 1 reanalyses not executed → re-dispatch 6A.3b per-concern, then re-run 6A.3c through 6A.5
- Missing `resolution.json` → re-dispatch 6A.4

---

