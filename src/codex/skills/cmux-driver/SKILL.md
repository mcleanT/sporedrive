---
name: cmux-driver
description: Operate or supervise an existing Claude Code session running in cmux — bind the exact window/workspace/surface/session identity, relay one faithful task brief, observe progress through bounded text reads and the event stream, manage safe compaction near the 30–40% context boundary, and close with a receipt. Use for explicit requests to drive, operate, supervise, monitor, or babysit a Claude Code session. Do not use for ordinary code review, direct repository implementation, generic desktop or terminal interaction, or browser automation.
metadata:
  contract_version: codex-claude-workflow 1.2.0
  source: ~/tools/codex-claude-workflow/src/codex/skills/cmux-driver
---

# cmux driver

Operate the exact authorized Claude session; Claude alone writes its worktree. Read one compact Mycelium resume packet at attachment/resume. Current execution status outranks historical checkpoint instructions. Fetch the selected full brief with `coord_read_message` / `mycelium-coord read-message` before dispatching; previews are not execution instructions.

## Efficient default

- One outcome-first brief, one active revision, fixed acceptance and a bounded review/repair allowance. Reuse prior authorization; routine implementation decisions belong to Claude.
- Prefer direct MCP/CLI. Bind once, revalidate on identity/reconnect change, and retain request IDs/cursors. Use a single batched state read rather than separate inbox/checkpoint/status/screen/transcript calls.
- Wait for the required event or completion in a finite tool call, within the host/task deadline. Do not wrap unchanged waits in clock calls, recurring model wakeups, commentary or fresh reasoning. Terminal/pause/expiry returns stop the wait loop. A stored message is not a native wake.
- Read only a bounded delta or decisive excerpt. Keep raw logs and long documents on disk; cap the aggregate evidence output, normally 4KB. Fetch full records by ID only when needed.
- After compaction restore scope, identities, checkpoint/cursors, decisive evidence, unknown attempts and remaining allowance once. Do not reread the full collaboration history or regenerate the plan.
- Use deterministic code for routine observation and routing; use supported low effort for a necessary routine model decision. Substantial reviews use their explicit bounded settings. Do not claim a hard desktop reasoning cap.
- One scoped review and one verification, not a verifier per finding. Stop once required work and authorized release are complete. A pause/exhausted allowance means one short retained-state receipt, no further compaction or bookkeeping turn.

## Essential invariants

One writer per worktree. Exact native session identity, never title/focus guesses. No competing task or compaction while conflicting jobs/writes are active. Authorized scoped steering may queue; it does not start a competing task.

Send/stage/enqueue/hook success is not acceptance. Match the entire delivered payload in the exact native transcript; queued acceptance requires the correlated absorbed_mid_turn removal and human queued_command attachment, or a later exact user message. Unknown outcomes retain their request ID and are never auto-resent. Completion needs the executor's actual evidence, not file existence.

Claude context compaction is due at about 30% USED, at the next drained checkpoint before 40%. Track Codex separately. Verify a native compact_boundary after the actual command; a stale meter is not failure. Never compact after task completion/pause solely for a report. Cancel only identified owned work under its cancellation/deadline policy and retain output.

## Load details only for the operation being performed

- First transport access, or a changed capability: [cmux runbook](references/cmux-runbook.md). Historical socket restrictions are dated evidence; use current authenticated capability.
- Binding, sending, compaction or uncertain recovery: [driver protocol](references/driver-protocol.md) and the relevant section of [bridge MCP](references/bridge-mcp.md). Do not load every reference for a status read.
- First substantive coordination message: [Mycelium coordination](references/mycelium-coordination.md). Use compact resume/inbox and selected full message reads; message content_hash hashes its envelope, not an artifact file.
- Writing a handoff/checkpoint: [checkpoint packet](references/checkpoint-packet.md). Preserve substantive requirements and limits; keep raw history out.
- A real multi-stage/provider acceptance run only: [integration readiness](references/integration-readiness.md). It does not apply to ordinary documentation or status work.

For a fresh, bounded standalone review, prefer a tools-scoped `claude -p` invocation to driving an existing terminal. Never start a second process on an existing conversation identity.
