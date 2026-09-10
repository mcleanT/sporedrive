# Bridge MCP `codex-claude-bridge` — operator contract (contract 1.2.0)

Source: `~/tools/codex-claude-workflow/bridge/` (`python3 -m cmux_bridge`, stdio). Registered in
Codex as `codex-claude-bridge` (`codex mcp list`). It wraps the installed cmux CLI through argument
arrays only: no shell, no generic RPC, no pane deletion, no process cleanup. Its state (leases,
bindings, request records, receipts) lives owner-only under `~/.local/state/codex-claude-bridge/`.

Access: the server process must be able to open the cmux socket. In `cmuxOnly` mode that means a
cmux-descended process; from the Codex app it needs the owner's `password` mode with the password
in an owner-only file named by `CMUX_BRIDGE_PASSWORD_FILE` (never argv, never a repo). If
`bridge_discover` returns `transport.ok: false`, report `transport.requirement` verbatim and stop.

## Tools (all bounded)

| Tool | Kind | What it does / refuses |
|---|---|---|
| `bridge_discover(workspace_uuid?, max_targets≤50)` | read | ping + access mode + caller/focused identity + Claude targets `{workspace_uuid, surface_uuid, pid, alive, claude_session_id, cwd}` from sidebar/agent events |
| `bridge_bind(workspace_uuid, surface_uuid, claude_session_id, claude_pid, worktree_realpath, controller_id, role=writer|monitor, lease_ttl_s)` | state | verifies surface∈workspace, pid alive, sidebar links surface↔pid, cwd == worktree realpath, screen shows Claude; for `role=writer` additionally requires an agent hook event linking the session id to the pid and cwd — without it the writer bind is refused (`bind_evidence_missing`); the caller may then bind explicitly as `role=monitor` (bounded observation, no writes). The bridge never downgrades a role silently. Writer leases are exclusive per surface and per worktree; a rebind by the same controller supersedes its own prior binding. Monitors coexist with any writer, and with each other. Refuses `bad_request` (refs instead of UUIDs), `identity`, `bind_evidence_missing`, `not_claude`, `lease_conflict` (a second writer on the session or worktree). Returns `binding_id`, `revision` (starts at 1), `boot_id`, `cursor_seq` |
| `bridge_observe(binding_id, lines=30, after_seq=None, max_events=200)` | read | screen state `prompt_idle | staged | running | modal | not_claude | unknown`, `ctx_used_pct`, events after `after_seq` up to `max_events` (session Stop/PreToolUse/UserPromptSubmit seqs), `compaction.due/overdue`. Never advances a shared cursor — the caller passes `after_seq` on each call; the result carries `next_after_seq` for the next one |
| `bridge_submit(binding_id, request_id, text, expected_revision, kind=task|reply, accept_timeout_s)` | write | writer only; refuses `stale_revision`, `busy` (anything but `prompt_idle`), `not_claude`, `unknown_state`, `staged_unverified`, `uncertain_foreign`, `lease_lost`, `identity_lost`. **Delivery is verifiable, not a raw paste.** A single-line payload ≤160 chars is staged inline; a multi-line or longer payload is written once to an immutable, bridge-owned **task file** (`tasks/<binding_id>/<request_id>.txt`, mode `0444` under a `0700` root) and the line actually staged is a short **reference** — `Task brief: <path>`. The bridge never pastes raw multi-line text: a paste marker's line count is not payload identity, and a raw bracketed paste auto-submits on this Claude build (captured under `checks/paste-probe-20260908T182911Z`). It verifies *staged* (refusing Enter if the reference wraps to a continuation row), submits Enter once, then confirms *accepted* by exact correlation of the **delivered line**: a user message for this session whose sha256 equals the delivered line's, at/after the send — the `UserPromptSubmit` event is corroboration only, never sufficient alone. **The receipt carries two deliberately distinct hashes:** `text_sha256` (the original brief; echoed as `payload.payload_sha256` for a task file) and `delivered_text_sha256` (the reference actually staged and matched). They differ for task-file deliveries, so acceptance proves the reference was accepted — **not that the full brief was read**; the executor's own `task_complete` evidence proves that. Same `request_id` again replays the persisted receipt, even against a now-stale `expected_revision` — it never resends; a genuinely different send needs a new `request_id` |
| `bridge_wait(binding_id, until, timeout_s, request_id=None, after_seq=None, evidence=None)` | read | `until ∈ accepted` (transcript hash match) `| turn_complete` (Stop after the accepted message, or the transcript's `turn_duration` entry) `| idle` (screen) `| task_complete | compaction_complete` (the transcript's `compact_boundary` entry after the `/compact` message, not a generic SessionStart). `task_complete` requires `evidence` shaped `{"file": path, "contains": text}` or `{"file": path, "sha256": hex}` — refused without it; the file's mere existence is not evidence. Outcomes: `satisfied | timeout | reconnect_gap | identity_lost | blocked_modal`. Always finite |
| `bridge_compact(binding_id, request_id, expected_revision, checkpoint, ctx_used_pct=None, reason=None, force=False, timeout_s=120)` | write | policy 30% used due / before 40% used. `checkpoint` uses the same evidence shape as `bridge_wait`'s `task_complete` and is required: a current, drained checkpoint written after the last accepted request. `not_due` → no action unless `force`. Busy → `deferred` with the given `reason` and `next_safe_boundary` (refuses `deferral_needs_reason` without one). Idle and due → `/compact` once, waits for the transcript's `compact_boundary` entry → `completed` with `meter_stale` (footer unchanged is not failure) or `uncertain` (nothing resent) |
| `bridge_release(binding_id)` | state | drops the leases; never closes the surface or the process |

## Flow

1. `bridge_discover` → pick the target by UUIDs, session id, pid, cwd (never by title or focus).
2. `bridge_bind(... role=writer)`; for a writer this requires an agent hook event linking the
   session id to the pid and cwd, else the writer bind is refused and you may bind as `role=monitor` explicitly. Keep `binding_id`
   and `revision` in the checkpoint packet.
3. `bridge_observe` → require `prompt_idle` before any input. Pass `after_seq` yourself each call
   (it never advances a shared cursor) and carry forward the `next_after_seq` it returns.
4. `bridge_submit(request_id=<your request id>, expected_revision=<observed>)` → a short
   single-line brief is staged inline; a multi-line or long brief is delivered as an immutable
   task-file reference (`Task brief: <path>`, mode `0444`), never a raw paste. On `accepted` store
   the event seq — the transcript hash match is against the **delivered line**
   (`delivered_text_sha256`), which for a task file differs from the payload hash (`text_sha256`),
   so acceptance means the reference was accepted, not that the brief was read; prove the latter
   with your own `task_complete` evidence. On `uncertain` call the same submit again — it replays
   the persisted receipt even against a stale `expected_revision` — or `bridge_wait until=accepted`;
   never construct a new request id for the same text.
5. `bridge_wait until=turn_complete request_id=…`, then `until=task_complete` with `evidence`
   shaped `{"file": path, "contains": text}` or `{"file": path, "sha256": hex}` — the executor's
   own evidence, never the file's mere existence. Turn completion is not task completion.
6. When `ctx_used_pct ≥ 30`: `bridge_compact(request_id=…, checkpoint=<same evidence shape,
   drained and current>)`; if `deferred`, wait for the boundary it names and call it again before
   40%. Record `meter_stale` as an observation, not a failure.
7. `bridge_release` when done. Never close the pane or terminate Claude as cleanup.

## Refusal codes

`bad_request`, `identity`, `bind_evidence_missing`, `not_claude`, `lease_conflict`,
`lease_expired`, `lease_lost`, `released`, `not_writer`, `unknown_binding`, `unknown_request`,
`stale_revision`, `busy`, `identity_lost`, `staged_unverified`, `uncertain_foreign`,
`unknown_state`, `deferral_needs_reason`, `transport_<kind>`
(`access_denied | socket_missing | connection_refused | not_found | internal_error | timeout`).


## Queued steering (owner-authorized route — not a bridge transport)

The bridge delivers only through the cmux input surface (`bridge_submit`) and never through Claude
Code's native message queue. Queued review / checkpoint / drain steering is an *owner/operator*
action outside the bridge: the owner presses Enter to queue a scoped message while the executor
works (Esc, which interrupts, is never used). The bridge does not enqueue, does not own that route,
and must not report an enqueue as accepted. If a receipt must record that a queued steering message
was carried out, match it by the executor transcript's `queue-operation remove` with
`reason: absorbed_mid_turn` plus the human-origin `attachment.type: queued_command` carrying the
exact prompt and target session id (or the later ordinary user turn whose content hash matches) —
never from an `enqueue`, a removal, or a hook event alone. This route never authorizes an
overlapping task, a competing write, or compaction while owned jobs are active.
