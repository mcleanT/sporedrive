# Conditional project conventions

Read only the relevant section when doing that work. Current owner scope, permissions and the global stopping rules still govern.

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
- **Verify the model actually resolved and the run completed**: the codex output file contains the whole injected prompt followed by the run; stdout and stderr are merged, and the wrapper exits with codex's status. Read the adjacent `.receipt.json` and final review first; inspect raw errors only when completion or identity is unresolved — a rejected model or an auth failure still exits and writes a file.
- **Static context layers**: `~/.codex/AGENTS.md` (universal, every call) + each repo's root `AGENTS.md` (project architecture). codex reads AGENTS.md, NOT CLAUDE.md — keep a repo-root AGENTS.md in sync with the project's canonical architecture.
- New project? Scaffold its AGENTS.md from `~/.claude/tools/AGENTS.template.md`.
- ALWAYS add task-specific framing in the QUESTION arg; auto-injected context is generic grounding only.
- GPT only for second opinions — never ollama/open-source models. The Anthropic API stays off-limits for evaluation.
- One scoped review per question. Dispose of findings on evidence (reproducer, call path, counterexample, contract citation), grouped by failure mechanism; do not spawn a verifier per finding. Details in the `codex-review` skill.
