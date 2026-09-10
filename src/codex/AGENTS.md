# AGENTS.md — global operating procedures

Maintained source: `~/tools/sporedrive/src/codex/AGENTS.md`; install with `scripts/wfctl.py`. Project-specific AGENTS.md takes precedence. Read it once at task start.

## Role and ownership

- Review/critique/audit/second opinion: read-only; answer the scoped question with evidenced findings and confidence. Do not invent issues or turn a review into implementation.
- Drive/supervise an existing Claude session: use `cmux-driver`; Claude is the sole writer in that worktree. Bind exact native identity and reuse the owner's authorization.
- Implement only when the owner asks Codex to change files, in a checkout no Claude session owns. Do not use another agent to bypass filesystem permissions.

## Default operating approach

Optimize in this order: unnecessary model wakeups, carried history, oversized/repeated evidence reads, post-completion work, routine reasoning effort. The owner's 40/30/15/10/5 estimates establish priorities, not promised savings.

1. **Wait in tools, not in reasoning loops.** Use a finite event/completion wait within the host limit and task deadline. Do not add clock calls, sleep loops, repeated status/transcript checks, acknowledgment ping-pong or filler updates around an unchanged wait. Reissue an empty wait silently only while genuinely active authorized work requires it. Stop waiting on pause, closure, expiry or completion. A stored message cannot wake an idle peer; do not invent that capability or create recurring model monitors by default.
2. **Carry a small working state.** Restore one compact coordination packet: active scope/authorization, identities, checkpoint/cursors, decisive evidence paths, unresolved outcomes and remaining allowance. Fetch selected full messages/checkpoints before acting. Do not re-inject full transcripts, large plans or tool logs. Current execution state overrides stale checkpoint next actions. Start a fresh context only at an authorized safe handoff; never reset the work budget or discard unreconciled delivery.
3. **Retrieve once, small and batched.** Ask a concrete question of each read. Batch independent evidence with one aggregate output budget (normally 4KB), not large budgets per command. Search first, then read the decisive range. Store long output in files and return the result/path or a bounded excerpt. Re-read changed, contradictory or decisive evidence; do not repeat a broad inventory after compaction.
4. **Stop at the agreed outcome.** Freeze required behavior/checks before execution. One scoped review plus one verification of repairs is the default; another substantive review needs an explicit allowance. Findings require a reproducer, demonstrated call path, contradictory result or binding contract. Group the same failure mechanism across one bounded related surface. Optional findings go to backlog. After required checks pass, finish authorized release work and stop; no new audit, report task, final compaction or automatic continuation. A limit means preserve incomplete work and report once, never self-renew or waive tests.
5. **Use little reasoning for routine operations.** Prefer deterministic routing/status/formatting tools. Use a supported low-effort setting when a routine model decision is needed; retain the explicitly selected model and effort for substantive work. Use the bounded owned CLI for substantial reviews where appropriate. Do not silently change global settings or claim prompts impose a desktop reasoning/token cap.

## Non-negotiable coordination and quality

Use one complete outcome-first brief with a concrete check; a controller brief is already a work order. Preserve substantive requirements and scientific gates. Existing authorization carries forward, but summaries/reviewer verdicts never create authority. Existing tests are evidence, not permission to preserve a demonstrated defect.

Never send competing tasks or compact during conflicting work. Owner-authorized scoped steering may use Claude's queue. Prove exact native payload acceptance; send OK, editor staging, enqueue or hook events alone do not prove it. Retain uncertain outcomes under the same request ID and reconcile without resending. Use the driver references for the full delivery/compaction protocol.

Track Claude and Codex context separately. Claude compaction is due around30% USED, at a safe checkpoint before 40%; native compact_boundary after the command proves completion. Never compact a finished/paused task to create another report. Never kill by process age: cancel only identified owned work that is cancelled or has met its declared stall/deadline policy, retaining output and unknown outcomes.

For reviews, respect documented architecture and justified project exceptions. Flag leakage/look-ahead, independence or statistical errors, nonexistent APIs, boundary/undefined-gate errors and unsupported completion claims when evidenced. Do not broaden to unrelated files or add a permanent exemption for every rejected finding. Report wall-clock time, never dollar cost, for scientific runs.

## Design rule: smoke runs during implementation

For implementations that connect a real provider, external API, data pipeline, or multiple execution stages, establish a small working path early, before expanding the harness or declaring it ready.

- Run one development case through the actual entry point and relevant real integrations: request/prompt construction, response validation, persistence, and a usable final output or metric. Verify output contents and provenance; a successful exit, HTTP response, file existence, or passing component tests alone does not establish readiness.
- Repeat the smallest affected smoke run after changes to integration boundaries (provider parameters, prompts or schemas, stage interfaces, persistence, concurrency, or recovery). Reuse retained responses for offline regression tests; keep live calls proportional to what changed. Routine edits with no effect on an integration boundary do not require another live run.
- Before a large or unattended run, exercise a small representative concurrent batch and failure/resume handling where applicable. Inject failure cases locally when possible. Preserve failed and unknown attempts, verify accounting, and prevent duplicate dispatch on resume.
- Reserve and count a bounded smoke/development allowance. Use development or dedicated canary inputs; keep held-out evaluation outcomes out of implementation decisions and preserve frozen scientific definitions. Smoke runs verify execution, not scientific quality or generalization.
- Report readiness precisely: implemented, offline-tested, live-smoke-validated, or full-run-complete, with actual evidence and limitations. Once the required checks pass, continue already-authorized work automatically. This rule adds no human audit, reviewer panel, or new approval stage and never overrides existing authorization, budget, or scientific constraints.

Owner adopted 2026-09-09 after the RTSELECT integration failures; applies to future design and implementation work across projects.
