---
name: codex-review
description: Use when the user wants an adversarial external review from OpenAI Codex — spec reviews, design critiques, plan audits, PR reviews, "get a second opinion from codex", "have codex look at this", "adversarial review". Also use for a different-model sanity check before a significant refactor or a substantive merge. Dispatches through the context-injecting `codex_ask` wrapper (read-only sandbox, prompt via stdin, output direct to file), then disposes of the findings on evidence in the executor: one scoped review, grouped by failure mechanism, a further specialist only for an unresolved consequential question.
---

# Codex Review

<!-- Maintained source: ~/tools/codex-claude-workflow/src/claude/skills/codex-review/SKILL.md (contract 1.1.0). -->

## Overview

Dispatch a spec, PR, design doc, or implementation plan to OpenAI Codex through `codex_ask` for an adversarial review, then return a critique the author can act on. Codex runs under the user's ChatGPT subscription — no API key, no metered billing. A different model offering independent critique is often more valuable than another self-review.

**Core principle:** review quality tracks (a) the structure of the prompt and (b) whether codex has the context the author does. A one-liner "review this" gets a mediocre review; a scoped prompt with a briefing, named weak sections, and an explicit output format gets a substantive critique.

**Every review answers one defined question and has a closure condition.** Record which question this review resolves (for example "is the gate logic in `eval/gates.py` sound before the AP-1 run?") and what counts as closed. Re-review only when the relevant semantics or evidence changed, not because a document has a new filename or a cosmetic edit.

> Codex reads `AGENTS.md`, not `CLAUDE.md`. Every project should carry a repo-root `AGENTS.md` describing architecture, conventions, and settled decisions not to flag. Scaffold from `~/.claude/tools/AGENTS.template.md` if one doesn't exist yet.

## When to Use

- "Have codex review this spec" / "get a second opinion from codex" / "adversarial review"
- Before committing to a significant refactor or merging a substantive change
- A formal scientific work order whose project contract calls for an external review

## When Not to Use

- Short questions or simple sanity checks — codex has ~30s startup; answer inline
- Live coding sessions — use subagents or `/ultrareview` instead
- Anything requiring the reviewer to MODIFY code (the wrapper enforces a read-only sandbox)
- A narrow implementation follow-up that has its own concrete check; run the check instead
- If `codex login status` shows unauthenticated — tell the user to run `codex login` first (interactive; cannot be automated from inside Claude Code)

## Prerequisites

1. **Auth:** `codex login status` → expect `Logged in using ChatGPT`.
2. **Owned processes only.** If a previous `codex exec` *that this session started* is still running for a task that was cancelled or has exceeded its deadline, cancel that PID and keep its output file. Never kill a codex process because of its age or its CPU reading alone — a valid long review, the desktop app, or someone else's task can look identical. If you suspect a lock problem, confirm it with evidence before acting: an error line in the output file (`database is locked`, `lock`, `another … in progress`), a stale `~/.codex/thread-writer-locks/<thread>.lock` whose owning process is gone, or no output growth past the task's own deadline. An empty output file and a process at 0% CPU also describe a model call waiting on the network; that observation alone is not proof.
3. **Materials.** Decide which files codex must read and whether it needs conversation context it cannot see (write a briefing doc for residual context only; see "Context Packaging").

## Calling Architecture — what `codex_ask` actually does

`codex_ask` (on PATH; source `~/.claude/tools/codex_ask.sh`) encapsulates the golden rules:

- **cd to the repo root**, so relative paths in the prompt resolve
- **`--sandbox read-only`**, so the review never modifies anything
- **prompt via stdin, output redirected directly to a file** — never through a `tail`/`head` pipe

```bash
codex_ask -o /tmp/codex_review_<slug>.txt \
  -f <path/to/primary-file> \
  -f <path/to/briefing-file> \
  "STRUCTURED REVIEW PROMPT"
```

| Flag | Behavior (verified against the wrapper, 2026-09-08) |
|---|---|
| `-f FILE` (repeatable) | Adds the path to a `Files specifically under review (read these from disk)` list. **Contents are not injected**; codex reads the file itself, so the path must resolve from the repo root or be absolute. The read-only sandbox reads any path the user can read (outside the repo and cwd included) and denies all writes; it is not a privacy boundary. |
| `-o OUTFILE` | Exact output path (stdout + stderr merged). Use a stable `/tmp/codex_review_<slug>.txt`. |
| `-m MODEL` | Default `gpt-5.6-sol`, the deliberately chosen reviewer model. This is *not* the account/app default (`~/.codex/config.toml` selects `gpt-6-astra`). `gpt-5.6` and `gpt-5-6-thinking` are rejected by Codex-with-ChatGPT; `gpt-5.5` is a fallback only. |
| `-e EFFORT` | Default `medium`; `xhigh` for deep reviews. Accepted: `low|medium|high|xhigh|ultra|max`. |
| `-n` | Dry run: print the assembled prompt, do not call codex. |

**Auto-injected `## Session context`:** repo name + branch, `git log -5`, the tail of `.living/last-session.md` if present (capped), and the `-f` path list. Codex also auto-loads `~/.codex/AGENTS.md` and the repo-root `AGENTS.md`. You do not need to package git state or architecture overviews by hand.

**Backgrounding:** the wrapper is synchronous. Launch it via the Bash tool with `run_in_background: true` (timeout 600000) and wait for the completion notification; the wrapper prints the output path on its last line.

**Receipt:** the wrapper writes one merged text file and exits with codex's status. There is no separate machine-readable receipt yet. Before trusting a review, check: exit status 0; no `ERROR:` / `not supported` / `not logged in` lines; the requested section headers are present. A rejected model, an expired login, or an empty prompt still exits and writes a file.

## Prompt Template

```
<MISSION — one line: "Adversarial spec review." / "Code review of changes in src/foo.py.">

<QUESTION THIS REVIEW RESOLVES — one sentence, plus what counts as closure.>

<CONTEXT: 1-3 paragraphs on what the project is, what was discussed, what's at stake. Point to the briefing doc rather than inlining everything.>

<AUTHOR'S KNOWN CONCERNS: weak sections to attack directly, e.g. "Spec §9 lists known weaknesses; challenge those and find issues the author did not anticipate.">

Evaluate:
A. <dimension 1 — a specific question, not "is this good">
B. <dimension 2>
...
<G. Unanticipated issues the author missed.>

Format:
## Summary verdict [one paragraph: proceed / revise-and-proceed / replace]
## Specific issues (ordered by severity) [numbered; each with section or file:line reference, the failure mechanism, the evidence you relied on, and a suggested fix]
## Things the doc does well [brief]
## Recommended revisions before <committing|merging|running>

Under <word limit — typically 1500>. Be direct; the author explicitly asked for challenge, not validation.
```

Ask codex to cite evidence for each issue (the line, the call path, the contradicting statement). Findings that come with evidence are cheap to dispose of; bare suspicions are not.

### Prompt design principles

- **Concrete dimensions, not "critique this."** Depth tracks the specificity of the questions.
- **Name the weak sections** so codex spends effort where it matters.
- **Ask for an output format.** Codex follows section headers closely.
- **Invite challenge.** Codex defaults to polite; say the author asked for it.

## Context Packaging

Write a briefing doc only for **residual context** codex cannot see: conversation-specific findings, decisions made mid-session, specific numbers or examples. Keep it short:

1. What prompted the work (one paragraph)
2. Session arc: prior findings, key numbers, specific examples
3. The proposal under review, in your own words if not obvious
4. Why this approach first, if non-obvious
5. Where the reviewer should focus (3–5 bullets)

Save it alongside the primary doc (e.g. `docs/specs/<date>-<name>-briefing.md`) and pass it with `-f`.

## Monitoring

- Wait for the harness's completion notification; don't poll.
- If asked whether it is still running, check `ls -la /tmp/codex_review_<slug>.txt`: a growing file means codex is streaming.

## Disposition (the executor decides, on evidence)

When the output arrives:

1. **Read the review section** (it starts at your format headers; the file also contains the echoed prompt and codex's `exec` traces, which show what it actually read).
2. **Group findings by failure mechanism**, not by count. Five comments about the same missing null check are one finding.
3. **Dispose of each group with disconfirming or confirming evidence you can point to:** a reproducer or failing test, the actual call path, a counterexample, or a citation of the contract or settled decision it contradicts. Read the cited code yourself; targeted reads are cheap and are the point. Three agents reading the same code and brief are not three independent measurements, so do not spawn a verifier per finding or a vote per contentious finding.
4. **Use a further specialist only for an unresolved, consequential question** — one agent, one question, with the specific evidence you want it to produce (run this reproducer, trace this path). A scientific judgment panel is still appropriate when a study's contract requires it; do not transfer that design to ordinary software comments.
5. **Present three buckets to the user:** actionable (with the evidence), rejected (with the one-line reason — never silently drop a finding), unresolved (what would settle it). Lead with the verdict and the 3–7 most consequential items; give the output path for the rest.
6. **AGENTS.md changes are rare and deliberate.** Add a "do not flag" rule only when a rejected finding reflects a stable, justified design decision; record rationale, scope, and revision. A false positive from stale source, a mistaken reviewer, or a missing test does not earn an exemption — fix the source or add the test.
7. **If the repo keeps a review ledger** (`tools/review_ledger.py`), append one record (artifact, model, findings, confirmed, rejected, unresolved). If it doesn't, skip this; don't scaffold one for a single review.

## Common Failures and Fixes

| Symptom | Cause | Fix |
|---|---|---|
| Output file stays empty longer than expected | Several causes look identical: a model call waiting on the network, an expired login, or a lock held by an earlier codex process | Look for an error line in the output file and a stale lock under `~/.codex/thread-writer-locks/` whose PID is gone; cancel only a process this session started whose task is cancelled or past its deadline, keep its output, then retry |
| `ERROR: ... not logged in` in the output | Session expired | User runs `codex login` |
| `ERROR: ... model is not supported` | Unsupported model slug (`gpt-5.6`, `gpt-5-6-thinking`) | Use `gpt-5.6-sol` (default) |
| Codex exits immediately with exit code 1 | Empty prompt or wrong file paths | Check the tail of the output file; verify paths resolve from the repo root |
| Codex cannot read a file | The path does not resolve (relative to the wrong root, a typo, or not readable by the user) | Pass an absolute path; the read-only sandbox reads anything the user can read, in or out of the repo |
| Review is generic or shallow | Underspecified prompt | Add dimensions, weak sections, output format, and the "be direct" instruction; rerun |
| Review exceeds the word limit | Codex treats limits as soft | Constrain structure ("stop at N sections", "each issue ≤ 3 sentences") |

## Post-Review Options

- **Apply the recommended revisions** and note the review date in the doc's revision history.
- **Save the review** alongside the doc as `<spec-filename>-codex-review-<date>.md` for auditability.
- **Second pass only if the semantics changed:** "This revision addresses the prior review; the key changes are X, Y, Z. Verify the revisions close the issues." Do not re-review a cosmetic edit.

## Why This Skill Exists

First-time codex dispatches used to fail on three traps — `tail` in the command hid streaming output, a stale config lock made new runs hang, and underspecified prompts produced shallow reviews. The wrapper removes the first two; this skill keeps the third from recurring and keeps review effort proportional to the decision it informs.
