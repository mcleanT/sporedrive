# Brief template and checkpoint packet (contract 1.2.0)

## Task brief (send this shape to Claude)

```text
Task: [observable desired outcome]
Context: [only facts needed for this work; exact input/spec/revision references]
Constraints: [hard boundaries and applicable authorization; preserve unrelated work]
Done when: [behavior plus the exact test/build/check, or ask Claude to identify the
           repository's canonical check if it is not yet known]
Autonomy: Make routine in-scope decisions and complete the authorized work.
          Escalate only material ambiguity or actions outside that authority.
Receipt: Result, changed artifacts/revision, commands and outcomes, caveats.
Revision: <request-id> [supersedes <previous-request-id> | amends <request-id>]
```

Do not invent a test command or fill missing facts with plausible-looking values. For a formal
scientific handoff, attach the exact contract and review record required by that project. For a
narrow repair, keep the brief short. Do not add "think step by step", "double-check everything",
"use a verification subagent", or a `no subagents` constraint unless the user asked for it or a
concrete shared-state reason exists.

### Requirement map (run before sending)

| User requirement (their words) | Where it appears in the brief | Added by me? | Necessary or marked as assumption? |
|---|---|---|---|

Every user requirement appears exactly once. Every gate or design choice you added is either
necessary or explicitly labeled as an assumption. This is a local check, not another agent.

## Checkpoint packet (keep under your task state; update at each meaningful change)

```yaml
contract_version: codex-claude-workflow 1.2.0
controller_task_id: <id>
request_id: <id>
request_revision: <revision>
request_text_sha256: <hash of the brief as sent>
target:
  host: <hostname>
  window_uuid: <uuid>
  workspace_uuid: <uuid>
  surface_uuid: <uuid>
  claude_pid: <pid>
  claude_session_id: <uuid>
  repository_realpath: <path>
  worktree_realpath: <path>
  bound_at: <timestamp>
objective: <current requested outcome>
accepted_scope: <current boundaries, including exact user corrections>
authorization:
  source: <user message or retained ratification reference>
  scope: <covered actions and exact scientific gate IDs when applicable>
completed: <artifacts and revision/hash references>
validation: <checks actually run, outcome, evidence path>
active_jobs: <owned job IDs, state, output location, restart/reconcile rule>
context:
  codex: {observed_at: <ts>, used: <value|unknown>, limit: <value+definition|unknown>}
  claude: {observed_at: <ts>, used_pct: <value|unknown>, compaction_due: <bool>, deferred_reason: <text|null>}
delivery: {request_id: <id>, state: <not_sent|staged|accepted|running|turn_complete|task_complete|uncertain>, evidence: <transcript content-hash match (accepted) / Stop event or turn_duration entry (turn_complete) / named-file evidence written after acceptance (task_complete) / compact_boundary entry (compaction) — UserPromptSubmit alone is corroboration only>}
events_cursor: {boot_id: <uuid>, last_seq: <n>}
open_issues: <unresolved questions or blockers>
next_action: <one concrete action>
```

Store observations, not hidden reasoning. Preserve exact high-impact constraints and IDs; link large
supporting material. Write it atomically under the owning controller task, never to a
repository-global scratch file.

## Compaction schedule (owner preference, global)

- `compaction_due` becomes true when Claude's own context-**used** meter reaches ~30% (used, not
  remaining).
- Run at the next safe checkpoint, and before 40% used. Safe = no thinking, no acquisition, no
  unreconciled writes, active jobs recorded with owners.
- While overdue: no avoidably large new phase; ask Claude for a drain boundary.
- Verify with executor evidence: the transcript's `compact_boundary` entry after the `/compact`
  command message, not a generic `SessionStart` event (`SessionStart` also fires for reasons other
  than compaction); clear the flag only on confirmed success. A meter that still shows the old
  percentage right after a confirmed compaction is stale, not a failure. Do not `/clear` because a
  meter is stale.
- A more specific run contract or an active measurement overrides the percentage.
