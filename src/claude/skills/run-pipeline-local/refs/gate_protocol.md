# Gate Check Patterns — Reusable Bash Reference

> Copy-paste patterns for the main context to verify that subagent dispatches
> produced all required files before advancing to the next pipeline stage.
> For the gate-checker subagent protocol (haiku, reads files and returns JSON),
> see `gate_checker.md`.

---

## 1. File-Existence Gate Pattern

Generic function that checks a list of required files and exits non-zero on failure.

```bash
# Usage: pass required file paths as positional arguments
# Returns: "GATE_PASS" or "GATE_FAILURE: <space-separated missing files>"
missing_files=()
for f in "$@"; do
  [ -f "$f" ] || missing_files+=("$f")
done
if [ ${#missing_files[@]} -gt 0 ]; then
  echo "GATE_FAILURE: ${missing_files[*]}"
  exit 1
fi
echo "GATE_PASS"
```

---

## 2. Directory Non-Empty Gate Pattern

Check that a directory exists and contains at least one file.

```bash
# Usage: DIR="/path/to/dir"
[ -d "$DIR" ] && [ "$(ls -A "$DIR")" ] && echo "GATE_PASS" || echo "GATE_FAILURE: $DIR is empty or missing"
```

---

## 3. JSON Content Validation Patterns

All patterns use `python3 -c` — no extra dependencies required.

### Array minimum count

```bash
# Check that a JSON array at TOP LEVEL has >= N entries
python3 -c "
import json, sys
data = json.load(open('$FILE'))
arr = data if isinstance(data, list) else data.get('$KEY', [])
count = len(arr)
print('GATE_PASS' if count >= $N else f'GATE_FAILURE: {count} entries (need >= $N)')
"
```

### Required keys present in a JSON object

```bash
python3 -c "
import json, sys
data = json.load(open('$FILE'))
required = $KEYS_LIST   # e.g. ['summary', 'hypotheses', 'coverage_score']
missing = [k for k in required if k not in data]
print('GATE_PASS' if not missing else f'GATE_FAILURE: missing keys {missing}')
"
```

### Nested key check

Check that a deeply nested key is present (e.g., `validator_results.json` must contain
`statistical.null_sensitivity_checks`).

```bash
python3 -c "
import json
data = json.load(open('$FILE'))
try:
    val = data['statistical']['null_sensitivity_checks']
    print('GATE_PASS')
except KeyError as e:
    print(f'GATE_FAILURE: missing nested key {e}')
"
```

### File size minimum

Check that a file is larger than N bytes (use for PDFs, figures, and other binary outputs).

```bash
SIZE=$(stat -f%z "$FILE" 2>/dev/null || stat -c%s "$FILE")
[ "$SIZE" -gt "$MIN_BYTES" ] && echo "GATE_PASS" || echo "GATE_FAILURE: $FILE is ${SIZE}B (need > ${MIN_BYTES}B)"
```

### Word count minimum

Check that a markdown or text file meets a minimum word count.

```bash
WORDS=$(wc -w < "$FILE")
[ "$WORDS" -ge "$MIN_WORDS" ] && echo "GATE_PASS" || echo "GATE_FAILURE: $FILE has ${WORDS} words (need >= ${MIN_WORDS})"
```

### IMRAD section check

Verify a markdown paper contains all four required section headers.

```bash
python3 -c "
import re
text = open('$FILE').read().lower()
sections = ['introduction', 'methods', 'results', 'discussion']
missing = [s for s in sections if not re.search(r'#+\s*' + s, text)]
print('GATE_PASS' if not missing else f'GATE_FAILURE: missing sections {missing}')
"
```

### Hypothesis coverage check

Verify that every hypothesis ID in `hypotheses.json` appears in `hypothesis_status`.

```bash
python3 -c "
import json
hyps = json.load(open('$HYPOTHESES_FILE'))
status = json.load(open('$STATUS_FILE'))
hyp_ids = {h['id'] for h in hyps}
covered = set(status.get('hypothesis_status', {}).keys())
missing = hyp_ids - covered
print('GATE_PASS' if not missing else f'GATE_FAILURE: uncovered hypothesis IDs: {missing}')
"
```

### Rasterization check

Confirm a figure script does NOT use rasterization shortcuts.

```bash
grep -q 'imread\|imshow' "$FILE" && echo "GATE_FAILURE: rasterization detected in $FILE" || echo "GATE_PASS"
```

---

## 4. Gate Failure Response Protocol

When a gate check returns `GATE_FAILURE`:

**Step 1 — Log to gate_log.json**

Append the failure record immediately (see schema in Section 5).

```bash
python3 -c "
import json, datetime, os
log_path = '$RUN_DIR/gate_log.json'
entry = {
    'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',
    'stage': '$STAGE',
    'team': '$TEAM',
    'round': $ROUND,   # use null if not applicable
    'required_files': $REQUIRED_FILES,
    'missing_files': $MISSING_FILES,
    'status': 'retry_1',
    'retry_prompt': None
}
log = json.load(open(log_path)) if os.path.exists(log_path) else {'gate_checks': []}
log['gate_checks'].append(entry)
json.dump(log, open(log_path, 'w'), indent=2)
print('logged')
"
```

**Step 2 — Re-dispatch with explicit callout**

Prepend the following to the re-dispatch prompt (fill in the actual missing paths):

```
GATE FAILURE: The following files were not produced by your previous run:

  - {path1}
  - {path2}

These files are MANDATORY. The pipeline cannot advance without them.
Produce every file in the required outputs list before returning.
Do not summarize or skip any file — write each one to disk.
```

**Step 3 — Track attempts and escalate**

- Attempt 1 failure: re-dispatch with callout, update log `status` to `"retry_1"`.
- Attempt 2 failure: re-dispatch once more, update log `status` to `"retry_2"`.
- After 2 failures: update log `status` to `"permanent_failure"`, proceed to the next
  stage with a warning, and mark the stage as `FAILED` in the audit manifest.

```bash
# After permanent failure — write to audit manifest
python3 -c "
import json, os
manifest_path = '$RUN_DIR/audit/audit_manifest.json'
manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {'stages': {}}
manifest['stages']['$STAGE'] = {
    'status': 'FAILED',
    'reason': 'permanent_gate_failure',
    'missing_files': $MISSING_FILES
}
os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
json.dump(manifest, open(manifest_path, 'w'), indent=2)
print('audit updated')
"
```

---

## 5. Gate Log JSON Schema

```json
{
  "gate_checks": [
    {
      "timestamp": "2026-03-24T12:00:00Z",
      "stage": "analysis_round_2",
      "team": "team_a",
      "round": 2,
      "required_files": [
        "outputs/run_001/analysis/round2/code.py",
        "outputs/run_001/analysis/round2/findings.json",
        "outputs/run_001/analysis/round2/round_summary.md"
      ],
      "missing_files": [
        "outputs/run_001/analysis/round2/round_summary.md"
      ],
      "status": "retry_1",
      "retry_prompt": "GATE FAILURE: round_summary.md was not produced. This file is MANDATORY."
    }
  ]
}
```

**`status` values**: `"pass"` | `"retry_1"` | `"retry_2"` | `"permanent_failure"`

---

## 6. Composite Gate Examples

Ready-to-run Bash snippets for the most common multi-file gates. Set `RUN_DIR` before using.

### Pre-analysis gate (hypotheses.json validation)

```bash
RUN_DIR="/path/to/run"
HYP="$RUN_DIR/hypotheses.json"
missing_files=()
[ -f "$HYP" ] || missing_files+=("$HYP")
if [ ${#missing_files[@]} -eq 0 ]; then
  python3 -c "
import json
data = json.load(open('$HYP'))
hyps = data if isinstance(data, list) else data.get('hypotheses', [])
checks = []
checks.append(('count>=3', len(hyps) >= 3))
checks.append(('has_title', all('title' in h for h in hyps)))
checks.append(('has_predictions', all('predictions' in h for h in hyps)))
checks.append(('has_feasibility', all('feasibility_score' in h for h in hyps)))
failed = [c[0] for c in checks if not c[1]]
print('GATE_PASS' if not failed else f'GATE_FAILURE: {failed}')
"
else
  echo "GATE_FAILURE: ${missing_files[*]}"
fi
```

### Analysis round master gate (7 required + 2 conditional)

```bash
RUN_DIR="/path/to/run"
ROUND=1
RDIR="$RUN_DIR/analysis/round${ROUND}"
# 7 required files
required=(
  "$RDIR/code.py"
  "$RDIR/findings.json"
  "$RDIR/round_summary.md"
  "$RDIR/stats_summary.json"
  "$RDIR/figures/fig1.pdf"
  "$RDIR/execution_log.txt"
  "$RDIR/biological_interpretation.json"
)
missing_files=()
for f in "${required[@]}"; do
  [ -f "$f" ] || missing_files+=("$f")
done
# 2 conditional: required when round > 1
if [ "$ROUND" -gt 1 ]; then
  for f in "$RDIR/prior_round_response.md" "$RDIR/open_questions_addressed.json"; do
    [ -f "$f" ] || missing_files+=("$f")
  done
fi
[ ${#missing_files[@]} -eq 0 ] && echo "GATE_PASS" || echo "GATE_FAILURE: ${missing_files[*]}"
```

### Post-analysis gate (synthesis outputs)

```bash
RUN_DIR="/path/to/run"
required=(
  "$RUN_DIR/analysis/analysis_synthesis.json"
  "$RUN_DIR/analysis/analysis_synthesis.md"
)
missing_files=()
for f in "${required[@]}"; do
  [ -f "$f" ] || missing_files+=("$f")
done
if [ ${#missing_files[@]} -eq 0 ]; then
  python3 -c "
import json
data = json.load(open('$RUN_DIR/analysis/analysis_synthesis.json'))
checks = []
checks.append(('has_findings', 'findings' in data and len(data['findings']) >= 1))
checks.append(('has_biological_summary', 'biological_summary' in data))
failed = [c[0] for c in checks if not c[1]]
print('GATE_PASS' if not failed else f'GATE_FAILURE: {failed}')
"
else
  echo "GATE_FAILURE: ${missing_files[*]}"
fi
```

### Cross-team gate (meta_comparison.json with >= 20 claims)

```bash
RUN_DIR="/path/to/run"
META="$RUN_DIR/cross_team/meta_comparison.json"
if [ -f "$META" ]; then
  python3 -c "
import json
data = json.load(open('$META'))
claims = data.get('claims', data) if isinstance(data, dict) else data
n_claims = len(claims) if isinstance(claims, list) else 0
has_divergences = isinstance(data, dict) and 'divergences' in data
checks = []
checks.append(('claims>=20', n_claims >= 20))
checks.append(('has_divergences', has_divergences))
failed = [c[0] for c in checks if not c[1]]
print('GATE_PASS' if not failed else f'GATE_FAILURE: {failed} (claims={n_claims})')
"
else
  echo "GATE_FAILURE: $META missing"
fi
```

### Figure composition rasterization gate

```bash
RUN_DIR="/path/to/run"
FIG_SCRIPT="$RUN_DIR/figure_composition/compose.py"
COMPOSITE_DIR="$RUN_DIR/figures/composites"
issues=()
# Check rasterization in composition script
[ -f "$FIG_SCRIPT" ] && grep -q 'imread\|imshow' "$FIG_SCRIPT" && issues+=("rasterization_in_script")
# Check composite directory is non-empty
[ -d "$COMPOSITE_DIR" ] && [ "$(ls -A "$COMPOSITE_DIR")" ] || issues+=("empty_composite_dir")
# Check at least one PDF composite exists
PDF_COUNT=$(find "$COMPOSITE_DIR" -name "*.pdf" 2>/dev/null | wc -l | tr -d ' ')
[ "$PDF_COUNT" -gt 0 ] || issues+=("no_pdf_composites")
[ ${#issues[@]} -eq 0 ] && echo "GATE_PASS" || echo "GATE_FAILURE: ${issues[*]}"
```

---

## Phase 3.5 Adversarial Review Gates

### Checklist Gate
```bash
python3 -c "
import json
with open('\${RUN_DIR}/adversarial_review/checklist_results.json') as f:
    data = json.load(f)
check_ids = {c['check_id'] for c in data['checks']}
required = {'FM-01','FM-02','FM-03','FM-04','FM-05','FM-06','FM-07','FM-08'}
missing = required - check_ids
assert not missing, f'Missing checks: {missing}'
for c in data['checks']:
    assert c.get('status') in ('pass', 'fail', 'partial', 'skipped', 'error'), f'Invalid status for {c[\"check_id\"]}'
print('GATE_PASS: checklist')
" 2>&1
```

### Panel Review Gate
```bash
python3 -c "
import json
with open('\${RUN_DIR}/adversarial_review/panel_review.json') as f:
    data = json.load(f)
assert 'editorial_synthesis' in data, 'Missing editorial_synthesis'
d = data['editorial_synthesis']
assert d.get('decision') in ('accept', 'minor_revisions', 'major_revisions'), f'Invalid decision: {d.get(\"decision\")}'
assert isinstance(d.get('unified_major_concerns'), list), 'unified_major_concerns must be array'
print('GATE_PASS: panel_review')
" 2>&1
```

### Resolution Gate
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
print('GATE_PASS: resolution')
" 2>&1
```

### Revised Synthesis Gate
```bash
python3 -c "
import json
with open('\${RUN_DIR}/adversarial_review/revised_synthesis.json') as f:
    data = json.load(f)
assert data.get('revised_synthesis_md'), 'revised_synthesis_md is empty'
assert len(data.get('revisions', [])) > 0 or data.get('added_limitations'), 'No revisions or limitations recorded'
print('GATE_PASS: revised_synthesis')
" 2>&1
```

### Reviewer Batch Review Gate (per-round)
```bash
python3 -c "
import json
with open('\${RUN_DIR}/reviewer_team/round\${N}/review.json') as f:
    data = json.load(f)
# Verify per_team_concerns has keys for all analysis teams
with open('\${RUN_DIR}/shared/team_assignment.json') as f:
    ta = json.load(f)
teams = [t['team_id'] for t in ta.get('teams', []) if t.get('role', 'analysis') == 'analysis']
for t in teams:
    assert t in data.get('per_team_concerns', {}), f'Missing concerns for {t}'
print('GATE_PASS: reviewer_round_\${N}')
" 2>&1
```
