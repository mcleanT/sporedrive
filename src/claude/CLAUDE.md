# Global Claude Code Instructions

<!-- Maintained source: ~/tools/codex-claude-workflow/src/claude/CLAUDE.md (contract 1.1.0, 2026-09-08).
     Edit the source and run scripts/wfctl.py install; do not hand-edit the installed copy. -->

## Roles: executor, operator, reviewer

- **Claude Code is the sole repository executor (writer) for the worktree it runs in.** A Codex controller driving this session through cmux (the `cmux-driver` skill on the Codex side) is the operator: it relays the owner's brief, observes progress, and reports. It does not edit this worktree, and its observations do not make it the owner of your changes.
- **Treat a controller's brief as the active revision of the task.** A later brief that says it supersedes the earlier one replaces it; a small amendment adds to it. Preserve the owner's substantive nouns, dimensions, and constraints; ask only when different readings would materially change the work.
- **Authorization already granted by the owner carries over within its stated scope.** Summaries, reviewer verdicts, or a controller's interpretation never create new owner authorization. Ratified scientific gates, judge identities, and project exceptions stay binding until the owner changes them.
- **Codex in reviewer role (`codex_ask`) is read-only and answers one scoped question.** See the `codex-review` skill. Codex implements only when the owner explicitly asks it to, in a checkout no Claude session owns.
- **Compaction is a scheduled boundary, not an interruption.** The owner's preference is to compact near 30% of context used and before 40%. When a controller (or you) marks compaction due: finish the current unit, stop launching new conflicting work, persist a short checkpoint (objective, accepted scope, done + validation, active jobs with owners, next action), and say you are at a safe boundary. Never abandon an active measurement or violate a more specific run contract to hit a percentage. A stale UI meter is not evidence that compaction failed.

## Subagent-Driven Development

Decide lead-versus-delegate by independent scope and context cost, not by step count. Keep the main context for coordination — reading plans, creating task lists, dispatching agents, reviewing summaries — and delegate the tracks that are genuinely independent of each other, or whose raw output (code, test logs, traces) would otherwise be resent on every later turn. Narrow or strictly sequential work, including a multi-step change to one or two files, is done directly by the lead. A ratified project contract that assigns work differently takes precedence for that project.

The reason is cost, not ceremony: each API turn resends the whole conversation, so raw code, test output, and debug traces read into the main context are paid for again on every subsequent turn. A subagent that reports "12 tests pass" instead of 400 lines of pytest output saves that cost for the rest of the session.

Dispatch when work is parallelizable, or when doing it inline would pull large volumes of code or tool output into the main context. Batch independent tasks into a single message so they run concurrently, then wait for results before launching work that depends on them. Work inline for single-file changes, pure research and exploration, and whenever the user asks you to. Delegation is proportional: the minimum team that covers the genuinely independent tracks, not a fleet created to re-verify work you already verified.

### Post-Batch Crystallization

After a significant task (3+ subagent batches, or equivalent solo work) completes, record any
substantive decisions/learnings to `.living/`. The actual executor — whoever did the work, main
loop or subagent — may record it directly; dispatch a separate sonnet crystallization subagent only
when delegation adds value (e.g. the raw context is large and would otherwise be resent on later
turns). If `.living/` doesn't exist, skip this step (not a mycelium-enabled repo). Where a Stop hook
enforces that a significant task leaves a `.living/` record, it checks that the record exists, not
that a specific dispatched agent produced it — no new enforcement claim beyond what the hook itself
checks.

### Subagent Token Discipline

Each API turn resends the full conversation. Cost = O(turns × peak_context). Minimize both.

**Turn cap guidelines** (scoping guidance, not runtime-enforced limits):

| Task Type | max_turns | When to exceed |
|-----------|-----------|----------------|
| Implementation (create/edit files) | 15 | Only if blocked by test failures requiring debug |
| Code review / audit | 10 | Split scope if >10 turns needed; a truthful partial result beats claimed full coverage |
| Validation / test runner | 5 | Never |
| Exploration / research | 20 | Only for genuinely complex cross-cutting investigations |

**Dispatch templates**:

- **SCOPED** (default): Provide the agent with an explicit file list, clear task description, and the turn cap from the table above. The agent should NOT explore or read files beyond the provided list.
- **BROAD** (opt-in for cross-cutting work): Provide entry-point files + "may explore related files" permission + pointer to `memory/deep-reference.md` for architectural context. Use 20-turn cap.

**Read discipline (freshness-aware)**:
1. Don't re-read a file that is already in context and has not changed since — scroll up
2. Use `offset`/`limit` for files >300 lines — read the section you need, not the whole file
3. A targeted re-read is the right move after an edit by another agent, after compaction, when a test or result contradicts what you remember, or when the file is the decisive evidence for a decision. Prefer bounded excerpts over whole files
4. Don't spawn an agent just to look at ten lines; read them

**Main context discipline**:
- Main context = coordination: create tasks, dispatch agents, review summaries, log decisions — and read the decisive evidence directly
- Use digests for bulk material (long reports, many figures); read the exact section, chart, or contradictory result that determines a decision yourself
- QA of subagent work: read the decisive evidence yourself (the diff hunk, the failing assertion, the number). Dispatch a review subagent only when the question is independent and the material is bulky enough that reading it inline would cost more than the dispatch; a read count is not the criterion

---

## MANDATORY: GitHub PRs and Issues Must Match Repo Templates

Before creating a PR or issue via `gh` CLI:

1. **Read the repo's templates first**:
   - `gh api repos/OWNER/REPO/contents/.github/PULL_REQUEST_TEMPLATE.md` or read locally
   - `ls .github/ISSUE_TEMPLATE/` for issue templates
2. **Match every section** in the template — Type of Change, Checklist, Testing, Context, etc.
3. **Verify labels exist** before referencing them — `gh label list`. Create missing ones with `gh label create`.
4. **Apply labels** to both PRs (`enhancement`, `bug`, etc.) and issues (template-specified labels).
5. **Testing section**: describe what was *actually tested* and results, not just a future plan.

GitHub silently drops non-existent labels from `gh issue create --label`. Never assume a label exists.

---

## Subagent Model Selection

| Task | Model | Rationale |
|------|-------|-----------|
| File search, simple validation, test runner | `haiku` | Fast, cheap |
| Code generation, implementation, data analysis | `sonnet` | Good reasoning + speed balance |
| Architecture planning, complex debugging, paper analysis | `opus` | Deep reasoning |

Default to **sonnet**. Drop to `haiku` only for mechanical work with no judgment in it — file search, running a test command, checking whether a file exists. Anything that involves deciding, classifying, writing, or summarizing gets sonnet or better; haiku's savings are not worth a bad judgment call you then have to find and undo.

## Model tiering: Fable plans, cheaper models build (set 2026-09-01; revised 2026-09-08 per the workflow audit)

**Why this exists.** On 2026-09-01 a session hit the Fable session limit after ~95M tokens (80M cache reads, 14M cache writes, 577K output): 25 subagents, 20 of them on Fable, 730 agent turns, main context at ~185K per turn. The cause was not output volume but turn count x context size on the top tier. A bare `agent()` or Agent call inherits the main-loop model, so every unlabelled stage silently ran on Fable. This is a measured resource strategy, retained until a small comparison justifies a change; it is not evidence that Fable can never implement efficiently.

**The three-phase default (user-set 2026-09-01):**

- **Phase 1, discuss: Fable at high effort, main loop only.** Brainstorming, design, adjudication, synthesis, verdicts. No workflows, no implementation subagents. Keep the main context lean by dispatching sonnet digests for bulk plans, reports, or figure sets, while still reading the decisive section, chart, or contradictory result directly.
- **Phase 2, handoff: the current lead writes a concise checkpoint / work order.** An ordinary Write by the lead is the default; no separate fork is required. The handoff must carry everything an implementer would otherwise spend turns discovering: file allowlists, parquet schemas and column names, numbers files, exact commands and flags, model tier and turn cap per task, stop criteria. Verify the few facts it depends on (a column name, a flag, a path) so it carries no unverified assumption. Use an independent synthesis (a `fork` at higher effort) only when it adds material value — for example a long discussion whose decisions need reconciling — and then give it a bounded mission and compare its output to the authoritative decisions. A brief that arrives from a Codex controller is already the work order: execute it, do not regenerate a handoff or a second plan for it.
- **Phase 3, implement: a fresh Opus context at the task boundary, ultracode on, Fable structurally absent.** Write the checkpoint first, then `/model opus`. Start a fresh context (`/clear` or a new session) when the discussion context is no longer needed for the implementation, which is the usual case right after a handoff; do not clear coherent, continuing work merely to satisfy the phase label (the Cost Awareness rule below says when a clear is worth it). The implementation session runs with Opus as the session model so a forgotten `model:` resolves to Opus, never Fable. Fable does no implementation, figure rendering, data extraction, file edits, test runs, or headless file audits by default. The owner can explicitly authorize Fable to execute a named task or session (as in owner-directed workflow-tooling sessions); that authorization does not carry over to other sessions.

**Rules (hard, not defaults):**

1. **Fable = main-loop planner at high effort** (saved as the Fable default in `~/.claude/settings.json` `modelSettings`). Never inside an implementation fleet.
2. **Every `agent()` and every Agent call sets `model` explicitly**, even in an Opus session. Tiers: `haiku` + `low` for mechanical work with no judgment; `sonnet` for implementation, extraction, figure building, summarization, single-lens review, structured-output stages; `opus` + `high` for adversarial verify, judge panels, architecture, ambiguous debugging. Fable inside a subagent only when the user asks for it by name or authorizes the session for it.
3. **State a turn cap in every dispatch prompt as scoping guidance.** Readers <= 20 tool calls, builders <= 15, reviewers <= 10, validators <= 5. The API has no max_turns; scope the task so it fits. An agent that cannot finish within its cap returns a truthful partial result or a rescoping note rather than claiming full coverage.
4. **Fleet shape.** `~/.claude/settings.json` has `workflowSizeGuideline: "medium"` (verified present) — the harness reads this as the size guideline it surfaces to workflow-tool authoring; this repo has not independently verified whether the harness also mechanically rejects an over-limit workflow, so treat that part as unconfirmed rather than asserting a hard technical block. Either way, the RETAINED USER POLICY is to plan against "under 15 agents per workflow" as the ceiling — "under 15" is a count of whole agents, i.e. **at most 14**, not 15. Plan proportionally below that: 2-3 proposers, not 4; one critic per proposal; a shared knowledge map larger than ~5K tokens is written to a file once, not duplicated into every prompt; the synthesizer gets a digest, not the full corpus of proposals. Concurrency (how many agents run at once), total agents per work item, and spawn depth (hops from the top-level dispatch) are three separate quantities — the 14-agent ceiling bounds the total, not concurrency, and a long serial fleet is not made small by low concurrency. If a task genuinely needs more than 14, stop and ask first, stating the agent count, what the extra fleet buys, and the rough token cost. Do not raise the guideline, pass a `large`/`unrestricted` override, or split one oversized job across back-to-back workflows to slip past it. `log()` anything a cap forced you to drop; a silent cap reads as full coverage when it wasn't.
5. **Evidence discipline.** Read decisive evidence directly: the exact section, figure, or contradictory result that determines the decision. Use <= 400-word sonnet digests for bulk plans, reports, and figure sets. Re-read after a change, a compaction, or a contradiction. Keep the main context lean (about 100K tokens is a working target, not a rule that forbids reading the thing that decides the question); when it grows past that, prefer digests and dispatch for bulk work.
6. **Headless Claude audits** (`claude -p` file audits) run on `opus` at high with an explicit file allowlist. **Codex reviews** go through `codex_ask` and use the wrapper's configured reviewer model (`gpt-5.6-sol`); the Opus rule does not apply to them. Run one external review per question, not both, unless the user asks for a second opinion. Project contracts that ratify a specific reviewer or judge (for example ClaimGraph's formal-handoff Fable audit) take precedence for that project until the owner changes them.
7. **Before launching any workflow**, state the estimated fleet (agents x expected turns) in one line. If the estimate exceeds ~2M tokens, ask the user to check `/usage` first. On heavy tasks pass a `+Nk` output-budget directive so the harness enforces a hard ceiling.
8. **Ultracode** belongs to Phase 3 only. In a Fable discussion session, workflows run only on explicit request ("use a workflow", "fan out agents") even if the harness reports ultracode on. Verify the ultracode state from the system reminder on the first turn of each phase; it went off at medium effort and is expected back at high.

## Cost Awareness

- For routine work, prefer keeping extended thinking to roughly 10K tokens — a stated preference,
  not a runtime-enforced cap; no demonstrated harness control forces a hard ceiling on thinking-token
  spend. This is distinct from the reasoning-**effort** setting (low/medium/high/xhigh/...), which is
  a separate model parameter, and distinct from **visible-output length**, which some harness
  surfaces do let you bound explicitly (e.g. a `+Nk` output-budget directive per rule 7 above) —
  that budget directive is the actual enforced ceiling available today, not this preference.
- Batch file reads in parallel instead of sequential
- Avoid re-reading files that are already in context and unchanged
- Use `/clear` at a meaningful task boundary (new workstream, substantially changed spec, a long series of abandoned attempts), not as a mechanical step before every implementation

## MANDATORY: Measurement Integrity — a harness failure must never be representable as a result

In any evaluation, benchmark, judging, or scoring harness, **infrastructure failure and measurement artifact must be structurally distinguishable from a scientific finding.** The recurring bug is not "the code broke" — it is that the breakage was encoded in the *same value space* as a real result, so "we failed to measure it" became indistinguishable from "it measured badly." These runs exit 0, log "complete", and produce a confidently wrong verdict.

**Preflight — verify sources and dependencies BEFORE spending a run.** A long job that starts against the wrong input or an unavailable service produces a complete-looking result built on nothing.

- **Assert inputs resolve to real content**, not merely that an argument was supplied. A *default* pointing at the wrong corpus is more dangerous than a missing one, because it never errors. Fail with a distinct exit code naming the unresolved input.
- **A placeholder is never an acceptable substitute for missing input.** Sentinels like `[MISSING SOURCE TEXT]`, `""`, or a fallback directory silently turn "input absent" into "input judged and found wanting."
- **Verify correspondence between paired inputs by count and by key** — corpus ↔ gold ↔ config ↔ registry — and require zero orphans in *both* directions before starting.
- **Probe external dependencies first** (VPN route, API health, auth, disk), and **re-probe at checkpoints**: a multi-hour run outlives its preconditions, and a mid-run VPN drop or token expiry is indistinguishable from bad data in the output.
- **For metered resources, check headroom and serialize.** Never run two heavy consumers against one quota/subscription concurrently to save wall-clock — it converts a slow success into a total loss.
- **Record what was verified into the run artifact**, so provenance shows the preconditions actually held rather than being assumed.

Enforce these invariants whenever writing or reviewing measurement code:

1. **Absence is not a negative result.** Never let missing / failed / unjudged / not-yet-run collapse into a falsy value that enters a numerator or denominator. `bool(None) == False` silently converts "unmeasured" into "measured, and it failed." Keep a distinct null, propagate it, and **track coverage as a first-class metric** (`n_unmeasured`, `coverage`, `metrics_complete`).
2. **Refuse to rule on incomplete data.** A gate/verdict needs a third outcome — `CANNOT-EVALUATE` — distinct from pass and fail. Reporting a shortfall as a *failure* is as wrong as reporting it as a pass.
3. **Transient ≠ terminal.** Quota, rate limit, 429, network, and capacity errors are retryable and must be their own error class: **abort and preserve the cache**, never mark items permanently-failed. Burning the remaining budget marking work "failed" destroys the run and looks identical to completion.
4. **Exit code is not evidence.** A run that did zero work must not exit 0. Verify by counting artifacts on disk, never by `$?` or a subagent's self-report. An unrecognized filter/sentinel that matches nothing must be a hard error, not a clean no-op.
5. **Measure the artifact the system actually stores.** Judging a reduced projection (e.g. bare text when the record also carries structured context) understates quality and produces artifactual "failures."
6. **Check what a metric conflates before gating on it.** A metric that penalizes correct behavior is a spec bug. Decompose it and verify it isolates the property you care about.
7. **Never silently drop a unit of work.** An item absent from a registry, config tuple, or arm list gets skipped without appearing in the output — verify every expected unit is present in the result, by count.

**Triage rule:** when a result looks like a finding — *especially* bad news about the thing under evaluation — first ask whether it is an artifact. Fix the measurement before adjusting a threshold, relabeling, or accepting the conclusion. Quantify the artifact's size and report the decomposition rather than the raw number.

**When any of this bites, fix the harness first and re-measure.** Do not negotiate the threshold to accommodate a number you have reason to believe is wrong.

## FastMCP Server Generation Rules

When generating MCP servers using FastMCP, follow the rules in `~/.claude/memory/fastmcp-rules.md`. Key requirements:

1. **Three-layer architecture**: Tool class (`tools/*.py`) → MCP server (`mcp_servers/*.py`) → Client adapter. Agents never import tool classes directly.
2. **Every tool MUST have** `title` (Title Case) and `annotations` (`ToolAnnotations(readOnlyHint=True)` for reads, `readOnlyHint=False` for writes).
3. **ToolError passthrough pattern**: Wrap every tool body in `try / except ToolError: raise / except Exception as e: raise ToolError(f"tool_name failed: {e}") from e`.
4. **Input validation at MCP boundary**: Clamp unbounded numerics (`min(max_results, CAP)`), validate required param combinations, prevent path traversal on write tools.
5. **Lazy singletons**: `@lru_cache(maxsize=1)` factory with lazy imports inside the function body.
6. **Async-only**: All MCP tools are `async def`. Wrap sync calls with `asyncio.to_thread()`.
7. **Return types**: `list[dict]` for searches, `dict | None` for lookups, never raw strings or Pydantic models.
8. **Docstrings = schema**: First line is tool description, Google-style Args section generates `inputSchema`.
9. **Update routing table**: New tools require updates in tool class + MCP server + client routing table.

Full template, anti-patterns, clamping defaults, and testing checklist: `~/.claude/memory/fastmcp-rules.md`

---

## Python Conventions

- Python 3.11+, type hints on function signatures
- Pydantic for data structures crossing module boundaries
- Async by default for I/O-bound operations
- Structured logging over `print()` statements (use `structlog`)
- Retry with exponential backoff on external API calls
- `matplotlib.use('Agg')` for non-interactive rendering in scripts
- Publication figures: vector format (PDF/SVG), colorblind-safe palettes

## Codex / GPT Calls (Cross-Project)

All codex/GPT calls go through the global wrapper `codex_ask` (`~/.claude/tools/codex_ask.sh`, symlinked to `~/bin/codex_ask`). It auto-injects session context (git branch + recent commits + `.living/last-session.md` tail if present + the *paths* of `-f` files under review) and relies on codex auto-loading AGENTS.md. Golden rules are baked in (cd repo root, `--sandbox read-only`, prompt via stdin, output direct-to-file — no tail/head pipe).

- `codex_ask [-m gpt-5.6-sol] [-e low|medium|high|xhigh|ultra|max] [-f FILE]... [-o OUTFILE] [-n] "QUESTION"` — `-n` dry-runs (prints the assembled prompt). Launch in the background for long reviews; the script prints the output-file path on its last line.
- **`-f` lists a path; it does not inject file contents.** The wrapper adds `Files specifically under review (read these from disk)` with each path, and codex reads the file itself under the read-only sandbox. Paths must resolve from the repo root or be absolute. The `--sandbox read-only` policy (verified 2026-09-08 on codex-cli 0.146.0) reads any path the user can read, in or out of the repo and cwd, and denies every write, `/tmp` included. So `-f` works for out-of-repo files, and the sandbox is not a privacy boundary: never point a review at directories that hold secrets.
- **Model: `gpt-5.6-sol`** is the wrapper's default and the deliberately chosen reviewer model. It is **not** the account or app default: `~/.codex/config.toml` currently selects `gpt-6-astra` at `xhigh` for interactive Codex, and the two are tuned separately. **Do NOT use `gpt-5.6` or `gpt-5-6-thinking`** — Codex-with-a-ChatGPT-account REJECTS both ("model is not supported"). `gpt-5.5` is a fallback only. Keep Sol for reviews unless a later benchmark justifies a change.
- **Effort for deep reviews**: use `-e xhigh`; `low`/`medium` for quick checks. The wrapper default is `medium`. The Codex CLI accepts `low|medium|high|xhigh|ultra|max`.
- **Verify the model actually resolved and the run completed**: the codex output file contains the whole injected prompt followed by the run; stdout and stderr are merged, and the wrapper exits with codex's status. There is no separate receipt yet, so check the exit status, grep the output for `ERROR:` / `not supported`, and confirm the requested section headers are present before trusting a review — a rejected model or an auth failure still exits and writes a file.
- **Static context layers**: `~/.codex/AGENTS.md` (universal, every call) + each repo's root `AGENTS.md` (project architecture). codex reads AGENTS.md, NOT CLAUDE.md — keep a repo-root AGENTS.md in sync with the project's canonical architecture.
- New project? Scaffold its AGENTS.md from `~/.claude/tools/AGENTS.template.md`.
- ALWAYS add task-specific framing in the QUESTION arg; auto-injected context is generic grounding only.
- GPT only for second opinions — never ollama/open-source models. The Anthropic API stays off-limits for evaluation.
- One scoped review per question. Dispose of findings on evidence (reproducer, call path, counterexample, contract citation), grouped by failure mechanism; do not spawn a verifier per finding. Details in the `codex-review` skill.

## Design rule: smoke runs during implementation

For implementations that connect a real provider, external API, data pipeline, or multiple execution stages, establish a small working path early, before expanding the harness or declaring it ready.

- Run one development case through the actual entry point and relevant real integrations: request/prompt construction, response validation, persistence, and a usable final output or metric. Verify output contents and provenance; a successful exit, HTTP response, file existence, or passing component tests alone does not establish readiness.
- Repeat the smallest affected smoke run after changes to integration boundaries (provider parameters, prompts or schemas, stage interfaces, persistence, concurrency, or recovery). Reuse retained responses for offline regression tests; keep live calls proportional to what changed. Routine edits with no effect on an integration boundary do not require another live run.
- Before a large or unattended run, exercise a small representative concurrent batch and failure/resume handling where applicable. Inject failure cases locally when possible. Preserve failed and unknown attempts, verify accounting, and prevent duplicate dispatch on resume.
- Reserve and count a bounded smoke/development allowance. Use development or dedicated canary inputs; keep held-out evaluation outcomes out of implementation decisions and preserve frozen scientific definitions. Smoke runs verify execution, not scientific quality or generalization.
- Report readiness precisely: implemented, offline-tested, live-smoke-validated, or full-run-complete, with actual evidence and limitations. Once the required checks pass, continue already-authorized work automatically. This rule adds no human audit, reviewer panel, or new approval stage and never overrides existing authorization, budget, or scientific constraints.

Owner adopted 2026-09-09 after the RTSELECT integration failures; applies to future design and implementation work across projects.
