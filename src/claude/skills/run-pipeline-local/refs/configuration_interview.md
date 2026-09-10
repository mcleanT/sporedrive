# Configuration Interview Reference

> Relocated verbatim from `SKILL.md` during the 2026-09-08 mode-router restructure (source lines 24-70 of the pre-restructure file, preserved in the snapshot at `~/tools/codex-claude-workflow/snapshots/skills-20260908T202743Z/run-pipeline-local/SKILL.md`). This is main-context execution-contract content (dispatch cards / gates / model requirements), not subagent-facing protocol text.

**Load when:** Only when the mode router determines a real interview is needed (missing required config/authorization), never unconditionally.

---

## Section 1: Configuration Interview

Present as a single summary table with defaults. Confirm before proceeding.

| Setting | Default | Options |
|---------|---------|---------|
| Research question | (required) | — |
| Experiment name | auto-generated | — |
| Pipeline mode | standard | standard / cohesive / multi-team |
| N (parallel runs) | 3 | integer; data acquisition always 1 |
| Science focus mode | discovery | discovery / validation / methods |
| Iterative analysis | yes (max 5 rounds) | yes / no |
| Roundtable debates | yes (multi-team) | yes / no |
| PI Interpretation Roundtable | yes | yes / no |
| Min analysis iterations | mode default | discovery=4, validation=3, methods=5 |
| Algorithmic validators | yes (all 4) | statistical / causal / embedding / automl |
| Pathway validator | no | yes / no (requires network) |
| Audit trail PDFs | yes (mandatory) | always on |
| Quality gate threshold | 6.0 | float (Ollama: 4.5) |
| Top-K hypotheses | 3 | integer |
| Code exec timeout | 300s | integer |
| Run mode | local | local / api / distributed |
| Cost budget | unlimited | USD float or null |
| Adversarial review   | yes                 | yes / no                          |
| Reviewer team size   | 5                   | 3 / 5                            |
| Analysis branching | yes | yes / no (multi-hypothesis parallel branches) |
| Max analysis branches | 3 | 1–8 (including main thread) |
| Max branch rounds | 3 | 1–5 (per branch before forced convergence) |
| Branch cost budget | $2.00 | USD float (0 = disable branching via budget) |

**Science focus modes** (controls prompt blocks, convergence floor, scoring weights):
- `discovery`: min 4 rounds. Scoring: biological_insight ×2, discovery_value ×2.
- `validation`: min 3 rounds. Scoring: statistical_validity ×2, reproducibility ×2.
- `methods`: min 5 rounds. Scoring: novelty ×2, coverage ×2.

**Multi-team extras**: number of teams [2], personas per team [3], shared lit review [yes], reanalysis max tier [4], persona selection [auto], reasoning modes [auto: standard/thinking/adversarial cycle], per-team temperatures [null], reviewer team size [5], reviewer persona selection [auto].

**API key check** (check `os.environ` at startup, do NOT ask unless missing):
- `NCBI_API_KEY`/`NCBI_EMAIL` — 10 req/sec PubMed (vs 3)
- `PERPLEXITY_API_KEY` — required for Perplexity search; skip if absent
- `SEMANTIC_SCHOLAR_API_KEY` — higher S2 rate limit
- `ELSEVIER_API_KEY` / `SPRINGER_API_KEY` — enhances full-text retrieval (skip if absent)

**Note**: Analysis Planner and Figure Composition are ALWAYS enabled. Not toggleable.

---

