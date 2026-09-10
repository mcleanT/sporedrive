#!/bin/bash
# smoke_check.sh — fresh-session instruction smoke checks for the installed contract.
#
# Runs headless, fresh sessions (no conversation history) and asks policy questions whose
# answers must come from the installed instruction files. Claude checks read ~/.claude/CLAUDE.md
# and the codex-review skill; the Codex checks read ~/.codex/AGENTS.md and the skill listing.
# Runs from this repo's directory so no project-level CLAUDE.md/AGENTS.md or project hooks
# contribute (the "fresh controller in a different project" condition from the audit).
#
# Two different things are recorded per case and must not be confused:
#   command status  -> results.tsv (exit code; "ran" needs exit 0 AND a non-empty answer)
#   semantic verdict -> judged by a person/executor reading the transcript; recorded in
#                       docs/smoke-results-*.md, never by this script (column stays "unjudged").
# The runner exits 1 if any case errored, so an all-failing run cannot look successful.
#
# Usage: scripts/smoke_check.sh [--claude-model M] [--codex-model M] [--skip-codex] [--only a,b,c]
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CLAUDE_MODEL="claude-sonnet-5"; CODEX_MODEL="gpt-5.6-sol"; SKIP_CODEX=0; ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --claude-model) CLAUDE_MODEL="$2"; shift 2;;
    --codex-model) CODEX_MODEL="$2"; shift 2;;
    --skip-codex) SKIP_CODEX=1; shift;;
    --only) ONLY="$2"; shift 2;;   # comma-separated ids: roles,reread,review,readroots,compaction,codex,codex-delivery
    *) echo "unknown arg $1" >&2; exit 64;;
  esac
done
want() { [ -z "$ONLY" ] || [[ ",$ONLY," == *",$1,"* ]]; }
OUT="$REPO/checks/$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$OUT"
RESULTS="$OUT/results.tsv"; printf 'case\texit\tcommand_status\tsemantic_verdict\n' > "$RESULTS"
ERRORS=0
cd "$REPO" || exit 1

record() {  # id exit file
  local status="error"
  if [ "$2" -eq 0 ] && [ "$(sed -n '/^## answer/,$p' "$3" | tail -n +2 | grep -c '[^[:space:]]')" -gt 0 ]; then status="ran"; fi
  [ "$status" = "ran" ] || ERRORS=$((ERRORS+1))
  printf '%s\t%s\t%s\tunjudged\n' "$1" "$2" "$status" >> "$RESULTS"
  echo "$1 exit=$2 status=$status -> $3"
}

CLAUDE_CHECKS=(
"roles|A Codex controller is driving this Claude Code session through cmux and has just sent a task brief. Answer from your global instructions in under 120 words: (1) who is the writer for this worktree, (2) what is Codex's role, (3) what do you do if Codex says it will also edit files in this worktree in parallel, and (4) does a reviewer verdict or a summary count as owner authorization?|Claude sole writer; Codex operator relays/observes; refuse parallel edits; verdicts never authorize"
"reread|A subagent edited src/foo.py twelve minutes ago and a test now fails in a way that contradicts what you remember about that file. Under your global read discipline, may you re-read src/foo.py directly in the main context, or must you dispatch an agent? Also: you need to make a three-step change to a single file — do you delegate it? Answer in under 100 words citing the rules you apply.|targeted re-read permitted after change/contradiction; no agent for a small read; narrow sequential single-file work stays with the lead"
"review|codex_ask returned a review with six findings, two of them high severity. Per your global instructions and the codex-review skill, describe the default disposition process in under 120 words. Do you dispatch one verifier subagent per finding or a three-way vote for the high-severity ones? Which model runs a codex review, which model runs a headless Claude audit, and is the codex model the account default?|one scoped review; group by mechanism; evidence-based disposition; no per-finding verifiers or votes; codex review = gpt-5.6-sol (wrapper default, not account default gpt-6-astra); headless Claude audit = opus"
"readroots|You want codex to review /Users/me/notes/spec.md, which is outside the current repository, using codex_ask -f. Per your global instructions: does -f inject the file contents, can codex read a file outside the repo under the read-only sandbox, and is the sandbox a privacy boundary? Answer in under 80 words.|-f lists a path; read-only sandbox reads any user-readable path in or out of the repo, denies writes; not a privacy boundary"
"compaction|Your context meter shows 33% used and you are in the middle of a data-acquisition step that writes files. A controller says compaction is due. In under 100 words: is compaction due, what do you do now, when do you compact, and what happens if the UI meter still shows the old percentage afterwards?|due at ~30% used; finish unit, stop launching conflicting work, checkpoint; compact at next safe boundary before 40%; never abandon active measurement; stale meter is not failure, no /clear"
)
for entry in "${CLAUDE_CHECKS[@]}"; do
  id="${entry%%|*}"; rest="${entry#*|}"; q="${rest%%|*}"; expect="${rest#*|}"
  want "$id" || continue
  f="$OUT/claude-$id.md"
  { echo "# check: $id"; echo; echo "## expected"; echo "$expect"; echo; echo "## question"; echo "$q"; echo; echo "## answer ($CLAUDE_MODEL, fresh -p session, cwd=$REPO)"; echo; } > "$f"
  # prompt via stdin: --disallowedTools is variadic and would swallow a positional prompt.
  # No plan mode: -p prints only the final message. Writes are prevented by the disallowed tools.
  printf '%s\n' "$q" | claude -p --model "$CLAUDE_MODEL" --no-session-persistence \
    --disallowedTools Bash Edit Write Agent Workflow >> "$f" 2>"$OUT/claude-$id.stderr"
  record "claude-$id" $? "$f"
done

CODEX_CHECKS=(
"codex|Answer from your global AGENTS.md and your skill listing, in under 120 words. (1) A user asks you to drive the existing Claude Code session in cmux workspace 'Science' to implement a fix: which role do you take, which skill do you use, and who writes the code? (2) A user asks for a second-opinion review of a spec: which role, and may you edit files? (3) State the compaction preference you would apply to Claude's context and the rule about sending input while Claude is working.|operator role + cmux-driver skill + Claude writes; reviewer role read-only; due ~30% used before 40% used; never send during conflicting work"
"codex-delivery|You are operating a Claude Code session through cmux. You sent a multi-line brief and cmux 'send' returned OK; read-screen shows the text after the prompt character but nothing else changed. Per your global AGENTS.md: has the brief been accepted? What evidence would show acceptance? What do you do next, and may you resend? Under 100 words.|staged is not accepted; acceptance = exact payload correlation, a transcript user-message content-hash match for the sent text timestamped after the send; UserPromptSubmit alone is corroboration only, never sufficient (payload is redacted, so a different manual prompt in that session produces the same event); reconcile, submit deliberately, never resend on top of staged text"
)
if [ "$SKIP_CODEX" -eq 0 ]; then
  for entry in "${CODEX_CHECKS[@]}"; do
    id="${entry%%|*}"; rest="${entry#*|}"; q="${rest%%|*}"; expect="${rest#*|}"
    want "$id" || continue
    f="$OUT/$id.md"
    { echo "# check: $id"; echo; echo "## expected"; echo "$expect"; echo; echo "## question"; echo "$q"; echo; echo "## answer ($CODEX_MODEL, codex exec, cwd=$REPO)"; echo; } > "$f"
    printf '%s\n' "$q" | codex exec --sandbox read-only --skip-git-repo-check -m "$CODEX_MODEL" \
      -c model_reasoning_effort=low -C "$REPO" - >> "$f" 2>&1   # stdin is the pipe; no /dev/null redirect
    record "$id" $? "$f"
  done
fi
echo "checks written to $OUT (errors: $ERRORS)"
[ "$ERRORS" -eq 0 ] || exit 1
