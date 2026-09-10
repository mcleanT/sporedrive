---
name: cmux-driver
description: Operate or supervise an existing Claude Code session running in cmux — bind the exact window/workspace/surface/session identity, relay one faithful task brief, observe progress through bounded text reads and the event stream, manage safe compaction near the 30–40% context boundary, and close with a receipt. Use for explicit requests to drive, operate, supervise, monitor, or babysit a Claude Code session. Do not use for ordinary code review, direct repository implementation, generic desktop or terminal interaction, or browser automation.
metadata:
  contract_version: codex-claude-workflow 1.2.0
  source: ~/tools/codex-claude-workflow/src/codex/skills/cmux-driver
---

# cmux-driver

You are the operator. Claude Code is the sole executor for the worktree it runs in. Your job is to
deliver the owner's task faithfully, watch without interfering, keep the session healthy, and report
what actually happened. State the contract version and the target identity once at the start of a
fresh or resumed controller task.

Read `references/cmux-runbook.md` before the first transport command in a task: it records the
installed cmux version, the verified commands, the access constraint on this host, and the
behaviors that were and were not proven. Read `references/checkpoint-packet.md` when you write a
brief or a checkpoint. Read `references/mycelium-coordination.md` before routing substantive content
(briefs, reviews, replies, checkpoints): those run over a shared Mycelium task (provider-neutral
task state that replaces ad hoc pointers), while the cmux bridge stays the short notification path.
That reference also covers per-host plugin registration and how to reach the protocol: prefer the
`coord_*` MCP tools when the plugin is loaded; for the bare CLI (which is not on PATH) use
`scripts/locate-mycelium-coord.sh`. Resolve the current checkpoint at use (`checkpoint-read TASK`
with no revision, or `resume TASK PARTICIPANT`) rather than trusting a hand-copied current pointer.
Read `references/integration-readiness.md` on demand, before a task's first live acceptance
cycle against a real provider or a representative multi-stage runtime path — it does not apply
to documentation or ordinary local edits, so most briefs never need it.

## Operating rules

1. **Bind once, revalidate on change.** Resolve host, window/workspace/surface UUIDs, the Claude
   session ID and PID, and the repository/worktree realpath. Use titles and `surface:N` refs only as
   display labels — ref numbers are reassigned when surfaces are created or closed, and an unknown
   `--workspace` ref can silently resolve to the caller's own workspace. Re-verify after a reconnect,
   a surface replacement, `/clear`, or a session switch. If identity is ambiguous, stop dependent
   input and resolve it; never guess.
2. **One writer.** Claude executes repository changes. You may inspect scoped evidence and prepare
   the brief; you must not implement in parallel in that worktree. Observing Claude's changes does
   not make you their owner.
3. **One faithful brief, one active revision.** Use the brief template. Preserve the user's
   substantive nouns, experimental dimensions, and named constraints; run the requirement map
   before sending (each user requirement appears once; each added gate or design choice is
   necessary or marked as an assumption). Record a request ID and text hash. A material correction
   supersedes the previous brief and says so; a small addition is a scoped amendment. Reconcile any
   work already started under a superseded revision.

   Fix the task's required outcomes and checks before sending the brief; routine implementation
   choices stay Claude's to make autonomously within that scope, and a new user requirement is an
   explicit amendment — never invent a new acceptance gate from your own reading of the task. State
   the review/repair allowance explicitly: one scoped review, then one verification of its
   repairs, is the default; a further substantive review needs an explicitly allocated additional
   allowance. A project's own ratified contract (additional stages, review panels) is carried into
   the brief explicitly and is never replaced by this ordinary default. Add this to every brief:

   ```text
   Acceptance: [observable outcomes and named checks]
   Work limits: [review allowance, owned review deadline, unattended expiry if applicable]
   Reopen only for: [new evidence of failure in required behavior]
   Other findings: [bounded backlog]
   On limit: Preserve results; report incomplete work; do not renew automatically.
   ```
4. **Reuse authorization.** Keep the owner's instruction and its scope in the checkpoint. Continue
   reversible in-scope work; ask only for unresolved material ambiguity or an action outside that
   authority. Ratified scientific gates remain binding; summaries and reviewer verdicts never
   create owner authorization.
5. **Observe through the most specific supported interface.** Prefer structured executor evidence
   (artifacts, result files, agent hook events in `cmux events`), then `read-screen` by surface
   UUID with a small `--lines` budget, then Computer Use only when visual evidence or recovery is
   necessary. Keep complete logs on disk; return deltas, not repeated screenshots.
6. **Never send competing input into conflicting work — but authorized queued steering is allowed.**
   Thinking, data acquisition, file mutations, and unreconciled writes block a *new task* and block
   compaction. An idle input prompt alone is not proof of safety; a stale monitor or unrelated
   detached job should not block forever, but classify it from ownership and job state, not from a
   guess. Unknown stays unknown, and unknown blocks dependent input.
   The owner has durably authorized **scoped review / checkpoint / drain steering** to be *queued*
   for the next safe boundary even while the executor is working. This uses Claude Code's native
   message queue — press **Enter to queue** (the current tool calls finish); never **Esc**, which
   interrupts — and the executor reads it at the next safe boundary. This narrow route does **not**
   authorize starting an overlapping task, a competing write, or compaction while owned jobs are
   active; those still require drained jobs. Queuing is not acceptance — see rule 7 and
   `references/bridge-mcp.md` for the queue route's acceptance evidence.
7. **Acknowledge delivery, and keep the delivery states apart.** Submit literal text only to a
   verified Claude input surface, then submit exactly once. The states are: *transport* (`send`
   returned `OK`; not delivery); *staged* (the text, or a collapsed `[Pasted text #N +M lines]`
   block, is visible in Claude's input editor after `❯`; nothing has been accepted; not delivery);
   *accepted* — exact payload correlation: the session's transcript
   (`~/.claude/projects/<slug>/<session>.jsonl`) contains a user message whose content hash equals
   the hash of the text you sent, with a timestamp after the send. An `agent.hook.UserPromptSubmit`
   event for that session ID and PID is corroboration only and never sufficient by itself — a
   different manual prompt typed into that same session produces the same event; *running*
   (`PreToolUse` events or the spinner line); *turn complete* — the `agent.hook.Stop` event after
   the accepted message (or the transcript's `turn_duration` entry); *task complete* — the
   executor's own evidence (a named file with expected content, written after acceptance), never
   the file's existence alone. A staged block is not a receipt: a timed-out paste once left a
   collapsed block staged but unsubmitted. Never promote staged to accepted; on an uncertain
   outcome, retain it and reconcile by the same request ID from the transcript and the event
   stream before any retry — never resend because an event is missing, replayed, or gapped, and
   never resend on top of staged text (clear or submit it deliberately). When the bridge MCP is
   registered (`references/bridge-mcp.md`), use its submit/wait tools, which do this correlation.
   For the owner-authorized queued-steering route specifically, acceptance is proven only by the
   executor transcript's `queue-operation remove` with `reason: absorbed_mid_turn` together with a
   human-origin `attachment.type: queued_command` carrying the exact prompt for the target session
   (or, failing that, the later ordinary user turn whose content hash matches). A plain
   `type:user`-only match misses this route, and an `enqueue` — or any removal or hook event — alone
   is never acceptance (a removal may instead be the owner recalling the message).
8. **Wait without repeated reasoning.** Use `cmux events --after <seq>` with a cursor, or a bounded
   `read-screen`, with a timeout. Supervise on meaningful events only: an empty, timed-out wait
   with unchanged last_action is reissued silently within the task's own deadline, and a terminal
   task (completed, paused, expired) ends the wait loop rather than being polled again.
   Acknowledgments and telemetry do not each require a reciprocal acknowledgment. Notify the user
   for meaningful progress, failure, a decision, or completion — never for unchanged state.
9. **Compact deliberately.** Track Codex context and Claude context separately. Mark Claude's
   compaction **due when its context-used meter reaches about 30%** (used, not remaining; the
   Claude Code footer shows `Ctx Used: NN%`); run it at the next safe checkpoint and **before 40%
   used**, the preferred upper boundary. While overdue, do not start an avoidably large new phase; ask Claude to
   reach a drain/checkpoint boundary (stop scheduling conflicting work, reconcile in-flight jobs,
   persist the checkpoint), then invoke the native `/compact`, then verify completion from the
   executor's own evidence: the transcript's `compact_boundary` entry after the `/compact` command
   message, not a generic `agent.hook.SessionStart` (`SessionStart` also fires for reasons other
   than compaction, and `PreCompact` is not relayed). A stale UI percentage is not proof of
   failure; do not `/clear` because a meter has not refreshed.
10. **Inspect evidence proportionally.** Targeted rereads after change, compaction, or a
    contradiction are correct. Use digests for bulk material and direct reads for decisive details.
    Tests and reviews answer specific questions; do not add them as ritual.
11. **Track progress and stalls by artifacts and elapsed time.** A live process is not progress.
    Retry only known-retryable failures within agreed bounds, preserving caches. Never kill a
    process because of its age alone; cancel only an owned process whose task is cancelled or
    proven stalled, and keep its output.
12. **Close with a receipt.** Completed work, artifact/revision, checks actually run with results,
    unresolved limitations, and whether owned jobs remain active. Release the target when done;
    never close the pane or terminate Claude as cleanup.

## Task flow

1. **Transport discovery.** If the `codex-claude-bridge` MCP is available, call `bridge_discover`
   first; it reports the access mode and the Claude targets. Otherwise run the discovery commands
   in the runbook. If the socket answers
   `Access denied — only processes started inside cmux can connect`, you are not a cmux-descended
   process; report that exact requirement to the user and do not attempt to broaden socket access
   yourself. Until access is resolved, observation may proceed by Computer Use, but rules 1, 6, and
   7 still apply to anything you type.
2. **Bind the target** (runbook §2): `list-workspaces --id-format both`, `tree --all`,
   `list-pane-surfaces --workspace <uuid> --id-format both`, and the Claude session identity from
   `cmux events` payloads or the sidebar status. Confirm the worktree path Claude is running in.
3. **Write the brief** from `references/checkpoint-packet.md`; run the requirement map; assign a
   request ID and hash; store the checkpoint packet under your own task state, not in the repo.
   Create or explicitly join the authorized **shared Mycelium task** with the exact bound native
   identities (`references/mycelium-coordination.md`), and send the brief through it
   (`kind=task|amendment|review_finding`, `--revision R`); the message id is the correlation handle.
   Publish the checkpoint packet as a versioned Mycelium checkpoint so older pointers refer to it.
4. **Check the safe boundary**, then submit (runbook §3, or `bridge_submit` + `bridge_wait`).
   Confirm acceptance, not staging.
5. **Observe and wait** (runbook §4). Update the checkpoint at each meaningful change. At each
   phase boundary check the shared task with a bounded `inbox`/`wait` (advance the cursor; truthful
   limits — no full-context re-read per tool). To actually nudge an idle executor use the bridge
   (`notify-via-bridge`); a stored Mycelium message never wakes an idle peer, and only a bridge
   *accepted* outcome is delivery.
6. **Schedule compaction** by rule 9 whenever Claude's context crosses 30%.
7. **Receipt and release** by rule 12.

## Out of scope

Browser automation, pane cleanup, workspace deletion, arbitrary RPC, starting a second process on
the same persisted Claude conversation, and any repository implementation. For a fresh bounded
batch or review that needs no existing session, prefer `claude -p` with JSON output over driving a
terminal.
