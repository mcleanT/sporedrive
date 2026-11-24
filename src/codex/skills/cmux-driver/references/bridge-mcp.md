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
| `bridge_discover(workspace_uuid?, max_targets≤50)` | read | ping + access mode + caller/focused identity + Claude targets `{workspace_uuid, surface_uuid, pid, alive, claude_session_id, cwd, model}` from sidebar/agent events; `model` = off-screen evidence `{argv, argv_family, transcript, transcript_family, fable_evidence}` (launch argv `--model` and the transcript's last assistant model) so a Fable planning session is visible before any bind — the live footer is read at bind |
| `bridge_bind(workspace_uuid, surface_uuid, claude_session_id, claude_pid, worktree_realpath, controller_id, role=writer|monitor, lease_ttl_s, purpose=implementation|planning, expected_model=None)` | state | verifies surface∈workspace, pid alive, sidebar links surface↔pid, cwd == worktree realpath, screen shows Claude; **purpose/model gate (owner rule 2026-09-20, Fable is planning-only):** for `purpose=implementation` (the default) a WRITER binding additionally requires the live footer model to be Opus/Sonnet/Haiku (`model_policy` on Fable, `model_unverified` on an unreadable footer) and neither the process argv nor the transcript to show Fable (`planning_session` — the owner's planning session, even after a `/model opus` switch); `expected_model` (alias, id or display name) must match the footer family (`model_mismatch`). `purpose=planning` admits any known model. A monitor records the verdict without enforcing it. The binding records `purpose` and `model {footer, argv, argv_present, transcript, family, expected, verified, code}`; window and focus play no part; for `role=writer` additionally requires an agent hook event linking the session id to the pid and cwd — without it the writer bind is refused (`bind_evidence_missing`); the caller may then bind explicitly as `role=monitor` (bounded observation, no writes). The bridge never downgrades a role silently. Writer leases are exclusive per surface and per worktree; a rebind by the same controller supersedes its own prior binding. Monitors coexist with any writer, and with each other. Refuses `bad_request` (refs instead of UUIDs), `identity`, `bind_evidence_missing`, `not_claude`, `lease_conflict` (a second writer on the session or worktree). Returns `binding_id`, `revision` (starts at 1), `boot_id`, `cursor_seq` |
| `bridge_observe(binding_id, lines=30, after_seq=None, max_events=200)` | read | screen state `prompt_idle | staged | running | modal | not_claude | unknown`, `ctx_used_pct`, events after `after_seq` up to `max_events` (session Stop/PreToolUse/UserPromptSubmit seqs), `compaction.due/overdue`. Never advances a shared cursor — the caller passes `after_seq` on each call; the result carries `next_after_seq` for the next one |
| `bridge_submit(binding_id, request_id, text, expected_revision, kind=task|reply, accept_timeout_s)` | write | writer only; refuses `stale_revision`, `busy` (anything but `prompt_idle`), `not_claude`, `unknown_state`, `staged_unverified`, `uncertain_foreign`, `lease_lost`, `identity_lost`, and — re-reading the footer model on every submit, before anything is typed or reserved — `model_policy` (the session switched to Fable), `model_changed` (any other switch since the bind: re-bind to re-verify), `model_unverified`. **Delivery is verifiable, not a raw paste.** A single-line payload ≤160 chars is staged inline; a multi-line or longer payload is written once to an immutable, bridge-owned **task file** (`tasks/<binding_id>/<request_id>.txt`, mode `0444` under a `0700` root) and the line actually staged is a short **reference** — `Task brief: <path>`. The bridge never pastes raw multi-line text: a paste marker's line count is not payload identity, and a raw bracketed paste auto-submits on this Claude build (captured under `checks/paste-probe-20260908T182911Z`). It verifies *staged* (refusing Enter if the reference wraps to a continuation row), submits Enter once, then confirms *accepted* by exact correlation of the **delivered line**: a user message for this session whose sha256 equals the delivered line's, at/after the send — the `UserPromptSubmit` event is corroboration only, never sufficient alone. **The receipt carries two deliberately distinct hashes:** `text_sha256` (the original brief; echoed as `payload.payload_sha256` for a task file) and `delivered_text_sha256` (the reference actually staged and matched). They differ for task-file deliveries, so acceptance proves the reference was accepted — **not that the full brief was read**; the executor's own `task_complete` evidence proves that. Same `request_id` again replays the persisted receipt, even against a now-stale `expected_revision` — it never resends; a genuinely different send needs a new `request_id` |
| `bridge_wait(binding_id, until, timeout_s, request_id=None, after_seq=None, evidence=None)` | read | `until ∈ accepted` (transcript hash match) `| turn_complete` (Stop after the accepted message, or the transcript's `turn_duration` entry) `| idle` (screen) `| task_complete | compaction_complete` (the transcript's `compact_boundary` entry after the `/compact` message, not a generic SessionStart). `task_complete` requires `evidence` shaped `{"file": path, "contains": text}` or `{"file": path, "sha256": hex}` — refused without it; the file's mere existence is not evidence. Outcomes: `satisfied | timeout | reconnect_gap | identity_lost | blocked_modal`. Always finite |
| `bridge_compact(binding_id, request_id, expected_revision, checkpoint, ctx_used_pct=None, reason=None, force=False, timeout_s=120)` | write | policy 30% used due / before 40% used. `checkpoint` uses the same evidence shape as `bridge_wait`'s `task_complete` and is required: a current, drained checkpoint written after the last accepted request. `not_due` → no action unless `force`. Busy → `deferred` with the given `reason` and `next_safe_boundary` (refuses `deferral_needs_reason` without one). Idle and due → `/compact` once, waits for the transcript's `compact_boundary` entry → `completed` with `meter_stale` (footer unchanged is not failure) or `uncertain` (nothing resent) |
| `bridge_release(binding_id)` | state | drops the leases; never closes the surface or the process |

## Flow

1. `bridge_discover` → pick the target by UUIDs, session id, pid, cwd (never by title or focus).
2. `bridge_bind(... role=writer, purpose=implementation, expected_model=<what you launched>)`; for
   a writer this requires an agent hook event linking the session id to the pid and cwd, else the
   writer bind is refused and you may bind as `role=monitor` explicitly, and the session's model
   must pass the purpose gate (a Fable session is never an implementation writer — launch a
   dedicated executor with `scripts/executor_session.py launch`, runbook §0). Keep `binding_id`,
   `revision` and the recorded `model.family` in the checkpoint packet.
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
`unknown_state`, `deferral_needs_reason`, `model_policy`, `planning_session`, `model_unverified`,
`model_mismatch`, `model_changed`, `transport_<kind>`
(`access_denied | socket_missing | connection_refused | not_found | internal_error | timeout`).

Model/purpose codes were added to the maintained bridge on 2026-09-20 (`cmux_bridge/model_policy.py`);
the installed Codex MCP entry runs the bridge from the release checkout named in
`~/.codex/config.toml` (`PYTHONPATH`), so until that checkout carries this change the same rule is
enforced by `scripts/executor_session.py launch|verify` in this skill, not by `bridge_bind`.


## Queued steering (owner-authorized, scoped — never a coalescing dispatcher)

Steering means adding one bounded, scoped message to an executor that is **already running a turn**,
so it is absorbed mid-turn instead of interrupting. Both authorized sources land in the same place —
the cmux input surface under the running spinner — never in a second queue service, never through an
Esc interrupt, and never through Claude Code's native message-queue API:

1. **Owner/operator keyboard.** The owner presses Enter to queue a scoped message while the executor
   works. This is outside the bridge; the bridge neither owns nor reports it.
2. **Bridge scoped `steer` submit (`bridge_submit` with `kind: steer`).** The bridge may type ONE
   single-line, owner-authorized payload into a running turn. `running` is the only non-idle screen
   state a submit may write into: `_require_writable` still refuses `modal`, `unknown`, `not_claude`,
   and a foreign `staged` draft, and every non-steer submit still needs a clean idle prompt — the
   identity, lease, and pre-Enter gates downstream are unchanged, so a steer relaxes only the idle
   precondition, not authorization. The payload is verified by `queued_is_ours` (the running-turn
   analogue of `staged_is_ours`: exact single line in the running editor's `input_text` — or a
   staged line with no continuation row — ours by handle or task-file reference) before Enter. A
   bounded confirm wait (default 20s, max 60s) that cannot see the payload absorbed returns
   `queued_unconfirmed` under the SAME request id; the bridge never auto-resends and never converts
   uncertainty, a foreign line, or human reassurance into an accepted delivery.

A queued steer is never reported as accepted from an `enqueue`, a removal, or a hook event alone. If
a receipt must record that a steer was carried out, match it by the executor transcript's
`queue-operation remove` with `reason: absorbed_mid_turn` plus the human-origin
`attachment.type: queued_command` carrying the exact prompt and target session id (or the later
ordinary user turn whose content hash matches). This route never authorizes an overlapping task, a
competing write, or compaction while owned jobs are active.
