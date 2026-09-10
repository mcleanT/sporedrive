---
name: run-pipeline-local
description: >
  Run THIS skill's own autonomous-science pipeline locally using Claude Code subagents as the
  LLM backend (Phase 1 lit review through Phase 5 post-processing; flat-dispatched science
  stages; ratified per-stage model assignments; hard stage gates). Use when the caller wants to
  execute a run of this specific legacy flat pipeline and either supplies research
  question/config directly or says "run locally", "run the pipeline", "run in this terminal",
  "no API calls", "run without API". Runs a short MODE ROUTER first — it does NOT present an
  unconditional configuration interview when the caller already supplied enough config and
  authorization. Do NOT use for generic autonomous-science-pipeline work that belongs to a
  DIFFERENT project's own contract (e.g. a project's own
  docs/plans/*multi-team-pipeline-design.md, or any pipeline whose config/output_dir points
  outside this skill's own experiment-outputs tree) — route that to the owning project's
  contract instead. Match on the caller's actual target project/config path, never on loose
  keyword presence like "pipeline", "autonomous", or "multi-team" alone.
---

# Run Pipeline Local — Execution Contract

This is a **hard execution contract**. Every stage listed here MUST execute unless the user explicitly skips it. Claude Code IS the LLM — do NOT run `autosci run` or `python main.py`. Each stage executes via subagent dispatch. The main context coordinates only: read plans, dispatch subagents, verify gates, proceed.

**Zero-tolerance for stage skipping.** Known failure modes:
- **#1**: Silently collapsing multi-phase stages (lit review, analysis loop) into a single-pass subagent that omits expensive phases.
- **#2**: Skipping mandatory stages entirely. Every stage below has a gate. Verify it.
- **#3**: Treating validators as optional enrichments. Every round's validator gate is a hard gate — validators are NOT optional even if the analysis agent declares findings complete.
- **#4**: Using a phase orchestrator for science stages (hypothesis gen through findings dossier). Science stages MUST be flat-dispatched. This architecture exists because orchestrators compress these stages.

**Architecture change (2026-03-24)**: Science stages (hypothesis gen through findings dossier) are now **flat-dispatched** — each is a separate main-context dispatch producing exactly one artifact, verified by a file-existence gate before proceeding. Only Phase 1 (lit review), Phase 4 (paper gen + QA), and Phase 5 (post-processing) use orchestrators. The analysis loop MUST be controlled by the main context, never delegated.

**2026-09-08 restructure note**: the full execution contract (dispatch cards, gates, ratified model table) previously lived inline in this file below the interview. It has been relocated verbatim into `refs/` — see the Loading Checklist below — so this entrypoint stays a lean router. Nothing was deleted; see "Gate-preservation verification" in `.living/` history / the audit report for the grep evidence. Original pre-restructure file preserved at `~/tools/codex-claude-workflow/snapshots/skills-20260908T202743Z/run-pipeline-local/SKILL.md`.

## Operating mode: this is a scientific protocol, not an ordinary review

Three distinct operating modes exist and each has different stopping rules; this pipeline is
the third one:

- **Ordinary design discussion** — one defined question, one critique round, one synthesis by
  default. Not what this skill runs.
- **Computational optimization** — objective, max calls/evaluations, elapsed-time window,
  improvement criterion and a stagnation/patience rule fixed before the run, sized to the
  task. Not what this skill runs (see the `prompt-optimizer` skill for that mode).
- **Scientific protocol — this pipeline.** The ratified minimum rounds, per-stage model
  assignments, stage gates, validator requirements and held-out/reporting boundaries carried
  in the Loading Checklist below and its `refs/` files are the mandated content of this mode.
  **Do not apply an ordinary one-review-then-close software cap to shorten them** — e.g. do
  not collapse a required analysis round, skip a validator gate, or treat adversarial review
  (Phase 3.5) as an optional single pass, on the theory that "one review is enough." Any
  reduction in required rounds/gates needs an explicit, scoped project amendment, not a
  general review-proportionality default.

---

## MODE ROUTER (run this FIRST — before any interview, before any dispatch)

**Step 0 — Is this actually THIS skill's pipeline, or a different project's own contract?**

Route OUT (do not import this skill's legacy flat pipeline) when the request is generic "autonomous science pipeline" work that names or targets a **different project's own** pipeline design/config — e.g. it references another project's `docs/plans/...multi-team-pipeline-design.md`, another project's own orchestration code, or an `output_dir`/config that is not this skill's `experiment outputs/{name}/` tree. **Match on the caller's actual target project and config path, not on keyword presence** ("pipeline", "autonomous", "multi-team", "run" are not sufficient signals by themselves — this legacy pipeline and a project-owned pipeline can both use those words). If ambiguous, ask exactly one clarifying question: *"Should this run use run-pipeline-local's own legacy flat pipeline, or does \[project\] have its own pipeline contract this should follow instead?"* Do not guess silently in either direction.

**Step 1 — Does the caller already have sufficient configuration + authorization?**

Check what was already supplied against the schema in `refs/configuration_interview.md`, minimally: research question, pipeline mode (standard/cohesive/multi-team), N, and an explicit go-ahead to dispatch now (e.g. "run it", "go ahead", "execute this").

- **Fully specified + authorized** → skip the interview entirely. Silently fill any remaining settings from the documented defaults in `refs/configuration_interview.md` (do not re-ask about anything already given or already defaulted). Proceed straight to Phase 1 dispatch.
- **Partially specified** → do NOT re-run the full interview. Ask only about the specific missing choices that would **materially change behavior** — pipeline mode (fan-out width, team count), adversarial review on/off (whether Phase 3.5 executes at all), cost budget / branch budget (whether analysis branching can run), run mode (local/api/distributed). Never ask about settings that have a safe, well-documented default (quality gate threshold, max_turns knobs, reviewer team size, etc.) — apply the default and state it once in the summary.
- **Nothing supplied** → present the full interview table from `refs/configuration_interview.md` as a single summary table with defaults, and confirm before proceeding. This is the fallback path, not the default path.

**Step 2 — Route to the loading checklist below for the confirmed mode, and begin Phase 1 dispatch.**

---

## Loading Checklist — which refs/ file(s) to load, per phase/mode

Load ONLY what the current phase needs; do not front-load the whole contract. Every row below is main-context execution-contract content unless marked *(subagent-facing)*.

| Phase / situation | MUST load |
|---|---|
| Router Step 1 (interview needed, or filling defaults) | `refs/configuration_interview.md` |
| Before ANY subagent dispatch, any phase | `refs/model_selection.md` (ratified — do not deviate) |
| Phase 1 dispatch (lit review + team assignment) | `refs/phase1_dispatch_gate.md` + `refs/phase1_orchestrator.md` *(subagent-facing)* + `refs/lit_review_protocol.md` *(subagent-facing)* |
| Pre-analysis flat dispatch (hypothesis, approach, data acquisition, analysis planner) | `refs/pre_analysis_dispatch.md` + `refs/data_acquisition.md` *(subagent-facing)* |
| Every analysis round (main context driving the loop) | `refs/analysis_loop_maincontext.md` + `refs/analysis_loop.md` *(subagent-facing, goes IN the dispatch prompt)* + `refs/science_focus.md` *(subagent-facing)* + `refs/figure_standards.md` *(subagent-facing)* + `refs/common_failures.md` |
| Per-round reviewer batch review (adversarial review enabled) | `refs/reviewer_batch_review.md` *(subagent-facing)* |
| Post-analysis flat dispatch (modality corrob., predictions, synthesis, figure composition, findings dossier) | `refs/post_analysis_dispatch.md` |
| Phase 3 cross-team dispatch (multi-team only) | `refs/phase3_cross_team_dispatch.md` + `refs/multi_team.md` *(subagent-facing)* |
| Phase 3.5 adversarial review (enabled) | `refs/adversarial_review_dispatch.md` + `refs/adversarial_review.md` *(subagent-facing FM checklist/protocol)* |
| Phase 4 dispatch (paper gen + QA) | `refs/phase4_dispatch_gate.md` + `refs/phase4_orchestrator.md` *(subagent-facing)* |
| Phase 5 dispatch (post-processing) | `refs/phase5_dispatch_gate.md` + `refs/phase5_orchestrator.md` *(subagent-facing)* |
| Any gate check, any phase | `refs/gate_protocol.md` (reusable Bash patterns) + `refs/master_gate_appendix.md` (full expected-artifact checklist + gate-failure log format) |
| After final paper PDF, before declaring the run complete | `refs/post_run_quality_checklist.md` |

**Do not skip a row above for the phase you are entering.** This checklist is the authoritative index; the legacy index inside `refs/master_gate_appendix.md` is retained for historical cross-check only.

---

## Single-Team Mode Simplifications, Appendices, Gate Log Format

See `refs/phase5_dispatch_gate.md` (single-team simplifications) and `refs/master_gate_appendix.md` (Appendix A: Master Gate Checklist; Appendix B: Gate Failure Log Format).
