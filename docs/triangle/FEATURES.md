# Triangle coordination features (opt-in; old servers keep working)

- Advisory containment (allowlist): env MYCELIUM_COORD_ADVISORY=1|true|yes. Every refusal is the core error
  `advisory_process_refused` (shared by MCP and CLI; helpers `model.authority_op`, `model.require_advisory_allowed`).
  REFUSED: attach with a role other than observer, or with allow_transition; any send whose message is actionable
  (by kind, `actionable=true` or an execution_action_id) or whose kind is not one of question, progress,
  review_finding, acknowledgment, consult_request, report (so task, amendment, review_request, checkpoint_request,
  checkpoint_ready, completion_receipt and blocker are refused); checkpoint_publish; every ExecutionManager
  mutator (open, reserve, settle, claim_review, settle_review, record_evidence, record_completion, open_blocker,
  resolve_blocker, reserve_call, settle_call, request_shutdown, note_expiry_shutdown, reconcile_shutdown, pause,
  unpause, change_limits, owner_request, set_phase, claim_dispatch, add_backlog); notify_via_bridge; CLI job-run, job-cancel,
  worker-run (gate sits in JobManager.run/cancel and WorkerManager.run).
  ALLOWED: create_task (unmanaged), attach as plain observer, the non-actionable sends above, inbox, read_message,
  ack and set_cursor (only as an attached observer, see below), message_state, wait, participants, list_tasks/list_sessions/find_recipients, resume,
  checkpoint_read, execution_read/status/receipt/evidence, wait_plan, sched_plan/sched_verify,
  job_status/join/list/output, detach of an OBSERVER record only. execution_backlog / add_backlog is REFUSED
  (it writes state_version). Attach as observer is refused when a stored record for that participant_id has a
  different role/host/session (no takeover via detach + re-attach); detach of a non-observer is refused.
  check_wait for antigravity: a caller host_yield_s can only LOWER the 30 s MCP yield (min of the two).
  MCP: in an advisory process the server lists ONLY `mcp_server.ADVISORY_ALLOWED_TOOLS` (all other tools are removed
  when FastMCP allows: 2.x `remove_tool`, 3.x/4.x `local_provider`); the core refusal is the real gate and holds when
  removal is unavailable (tested with the tool left listed).
- Advisory identity (review F12): in an advisory process, `ack` and `set_cursor` require the NAMED participant, and `send`
  requires the SENDER, to be an attached, non-detached observer; otherwise `advisory_process_refused`. An adviser can no
  longer ack or move the cursor for an executor, or send as a supervisor. This is containment of an MCP-only process, not
  authentication (see Limits below). Outside an advisory process nothing changes.
- reply_expected (owner decision, 2026-11-23): a message of kind review_request or consult_request is rendered with
  `reply_expected: true` in compact and full inbox, resume and `coord_read_message`; every other kind carries no such
  field. It is derived at rendering time and never stored. Reply with kind `report`, `reply_to` the request's message_id
  (a report is non-actionable and needs the task's triangle-kinds feature).
- Host kinds (review F14): one list, `model.HOST_KINDS` (claude, codex, antigravity), used by attach validation, waitpath
  (and the CLI `--host` choices) and the wake route table (a host without a wake route is `not_supported`). The standalone
  writeguard.py keeps its own copy; a test asserts it equals `model.HOST_KINDS`.
- Message kinds review_request, consult_request, report: base-commit READERS tolerate them (kind is never
  validated on read; tested), but a base-commit SENDER refuses them, so sending needs the task opt-in
  `protocol_features: ["triangle-kinds"]` (create-task `--feature triangle-kinds`, MCP `protocol_features`).
  Without it: `kind_requires_feature`. consult_request/report are always non-actionable. MCP `coord_send`/`coord_create_task`/`coord_attach` and the CLI help now name these kinds, the opt-in, the `codex-exec-wake` feature and the `wake=headless` host key.
- Reply budget: task `message_budget` {"per_correlation": N, "per_sender": M} (`--message-budget JSON`).
  Enforced by new servers only (old senders ignore the field); `message_budget_exceeded`; supervisors exempt
  from per_correlation; idempotent replays are not re-counted. per_sender counts EVERY message by the sender
  (actionable or not). per_correlation does not count or refuse kinds acknowledgment and completion_receipt
  (they still count toward per_sender).
- Session ids: the "claude-" prefix strip applies only when the host kind is "claude".
- wait-plan / wait_plan `host=antigravity`: MCP 25 s fallback, shell route reported unmeasured. check_wait for
  antigravity takes the MINIMUM of a caller-supplied host_yield_s and 25 s (a caller can only lower it) and returns
  ok=False with a `problem` for any inner wait above that; result carries host, shell_route and note.
- Old-reader check: test_triangle_advisory.py::test_old_reader_reads_everything_a_new_server_writes runs the
  base package (docs/triangle/evidence/base-91546b3, a `git archive` of 91546b3) against a store written by the new code.

## Limits of advisory containment (lead note, 2026-11-22)
- MYCELIUM_COORD_ADVISORY is a property of the coord PROCESS: the MCP server or CLI started for that host. It is enforced only on that MCP or CLI surface.
- A host whose agent can run shell commands could bypass it. It could start the coord CLI without the variable, or write the store files directly, because the store is plain user-owned files.
- Containment therefore also depends on host-side grants. For headless agy (`agy -p`, `agy acp`), the client denies `command(...)` and grants only the allowed `mcp(...)` tools. The Antigravity App and IDE prompt the human for commands.
- Claim: "advisory processes can only watch, read and comment through the coordination tools". Do NOT claim "cannot affect tasks" for a host with unrestricted shell access.
- Antigravity's session value is model-supplied: M1 attached as host antigravity, session "agy-smoke-1". It is not hook-verified, so the A1 identity check does not authenticate Antigravity itself. What it guarantees, in an ADVISORY process only
  (MYCELIUM_COORD_ADVISORY set): (a) a stored participant record is not taken over (same participant id needs the
  same observer role, host and session); (b) round 4, N1, in BOTH selection writers (`attach` and `select_session`),
  before any write: the host kind must be exactly `claude`, `codex` or `antigravity` (case variants such as `Claude`
  are refused, so no case-insensitive store path is ever reached); the selection is checked at the EXACT file the
  write would use (including a hashed `h.<sha16>` name) plus every record the `_sid_match` fallback scan would match;
  a record held by a different (task, participant) refuses the call with `advisory_process_refused` unless that holder
  is missing, detached, or no longer on THAT RECORD's own host/session (compared with the stored record, never with
  the caller's claim), or is an observer whose stored host+session equal the caller's (the same advisory identity
  re-attaching or moving to another task, which moves its selection).
  NON-advisory processes are unchanged: last attach wins the native-session selection.
- Pending lead decision, after the Betty verify of round 2: per-conversation observer ids (`agy-<conv8>`), or allowing re-attach onto a DETACHED observer record of the same host.
- Non-advisory last-attach-wins (unchanged, round 3): outside an advisory process any attach with a host+session overwrites
  that native session's selection. Only advisory processes are protected against selection capture.

## Wake (M6): `wake` CLI op / `coord_wake` MCP tool (coordination/mycelium_coord/wake.py)
- One public entry, `wake_recipient(co, task, message, sender=...)`, core-gated by `authority_op("wake")` (refused in an
  advisory process; `coord_wake` is NOT in `ADVISORY_ALLOWED_TOOLS`). The route is picked from the RECIPIENT's stored host
  kind; there is no caller-supplied target. Outcomes for every route: `accepted | uncertain | unavailable | not_supported |
  refused:<reason>`. Only `accepted` is a delivery. A stored accepted OR uncertain receipt for (task, message) is returned
  as-is (`idempotent_replay`): a retry is another billed model turn, so nothing is auto-retried.
- Gates before any side effect: message exists; the recipient is ONE concrete participant (role/`all` ->
  `refused:fan_out_recipient`); attached and not detached; not the sender; role observer -> `observer_not_wakeable`; kind in
  task / review_request / consult_request else `kind_not_wakeable`; message not already acknowledged/claimed/completed
  (`already_acknowledged`); per-task cap of accepted+uncertain wakes (default 20, task.json integer `wake_budget`) else
  `wake_budget_exhausted`; managed-task pause/expiry re-read immediately before the Codex run (same `dispatch_check` the
  bridge uses; a work dispatch needs its bound reservation). An in-flight claim receipt is written before the run, so a crash
  leaves `uncertain`, never a re-run. Receipts: `tasks/<t>/wakes/<m>.json` plus an audit line (route, outcome, state
  before/after, exit, timed_out, duration, request id `wake-<hash>`).
- Five wake rules (M7 repair, positive rules): (1) the stored Codex thread (`host.session`) must fully match a UUID
  (case-insensitive) else `refused:invalid_thread_id`, codex never launched. (2) `MYCELIUM_WAKE_CODEX_ARGS` is an allowlist:
  only `--ignore-user-config` and `-c` followed by `mcp_servers.<...>=<value>`; anything else ->
  `refused:codex_args_not_allowed`. (3) The managed pause/expiry/draining/closed gate runs for EVERY route, immediately
  before the route's mutation (Claude: before `notify_via_bridge`; `bridge_link.py` unchanged). (4) Immediately before
  Popen the recipient participant is re-read and must still be attached, host codex, the same session, `wake == headless`,
  else `refused:recipient_changed`. (5) Timeout (CLI `--timeout`, MCP `timeout_s`) must be finite in (0, 900], default
  240, else a validation error (`invalid_timeout`) before any side effect; any exception after Popen kills the process
  group (killpg, then wait) before the outcome is recorded `uncertain`.
- Routes: **claude** = thin adapter over `bridge_link.notify_via_bridge` (needs `--binding-id --controller-id
  --expected-revision`; bridge states map: delivered -> accepted, uncertain/unknown -> uncertain, bridge_unavailable ->
  unavailable, pending+refused -> `refused:bridge_<code>`, execution gate/inbox-only -> refused). **antigravity** and any
  unknown host -> `not_supported` (next-turn inbox only). **codex** = `codex exec resume --json [extra] <thread> -`
  (prompt on stdin, foreground, timeout 240 s), only when the task has `protocol_features` `codex-exec-wake`
  (`create-task --feature codex-exec-wake`) AND the recipient's host record has `"wake": "headless"` (attach with
  `--host wake=headless`; set by the Codex participant, never inferred) else `refused:codex_exec_wake_not_enabled` /
  `refused:not_headless_owned`. The thread is the stored `host.session`. Refused when the Codex queue DB
  (`~/.codex/queue_1.sqlite`, env `MYCELIUM_WAKE_CODEX_QUEUE_DB`) has rows for the thread in table `queued_items`
  (`thread_has_queued_items`). Only that known layout counts (codex 0.171.0; drained items are deleted, and
  `queued_thread_revisions` is ignored). DB absent = none; unreadable or any other layout = `queue_unknown`.
  The recipient re-read and the launch run together under the task lock; a task-lock timeout before launch is
  `unavailable` (retryable, nothing ran).
  Per-THREAD run lock (review F2): one headless `codex exec resume` per thread at a time. The lock
  (`codex-thread-<uuid>` in the store's locks dir) is taken with zero wait BEFORE the task lock and held from before the
  queue check through the whole run (Popen to communicate). A busy thread returns `refused:thread_wake_in_progress`: retryable,
  unbilled, never a StoreError and never a permanent `uncertain`; the same message can be woken once the thread is idle.
  Queue state (review F4): every codex receipt, launched or refused for the queue, carries `queue: {state, detail}`
  (`clear` | `queued` | `unknown`). An absent DEFAULT DB (`~/.codex/queue_1.sqlite`) is `unknown` (refused `queue_unknown`) when
  another `~/.codex/queue_*.sqlite` exists (the `-shm`/`-wal` sidecars do not count); an absent env-override path stays
  `clear`.
  Env overrides: `MYCELIUM_WAKE_CODEX_BIN`, `MYCELIUM_WAKE_CODEX_ARGS` (JSON list of extra args, e.g. the isolation
  flags in evidence/m6-p3/run.sh; any bypass flag is refused). Fixed prompt: "New coordination message {mid} on task {tid}
  for participant {pid}. Read it with coord_inbox (task {tid}, participant {pid}), acknowledge it with coord_ack (task {tid},
  participant {pid}, message {mid}), then act within your role." Outcome is read from the store after the run:
  acknowledged / completion_claimed / completed, or the cursor moved past the message -> `accepted`; otherwise (clean exit
  without ack, nonzero exit, timeout) -> `uncertain`. Exit code alone never makes `accepted`.
- Deployment note (owner-held host change): a real Codex wake loads the user Codex config. The woken turn must be allowed to
  call the coord tools (`coord_inbox`, `coord_ack`) without a prompt, e.g. a per-server `default_tools_approval_mode =
  "approve"` for mycelium-coord, and must point at the intended store. P3 proved this with per-run `-c` overrides; a
  production setup needs the same in its own Codex config. This build changes no host config.

## Send capability (proposal step 3, 2026-11-23)
- Opt-in task feature `send-capability` (`create-task --feature send-capability`, MCP `protocol_features`). An advisory
  process (`MYCELIUM_COORD_ADVISORY=1`) is held to it on every task.
- Every attach mints a random capability that the attaching PROCESS keeps in memory (module `sendcap.py`). The
  participant record stores only its sha256 and the holder process (pid, ps start time, machine). Neither the
  capability nor the hash is ever returned by attach, participants, the MCP tools or the CLI.
- Enforced writes: send (sender), ack, set_cursor, checkpoint publish, detach, wake (sender), bridge notify
  (controller_id when it is a participant). Without the current capability: `send_capability_required` (this process
  never attached it) or `send_capability_stale` (another process attached it since). Nothing is written. An idempotent
  replay is refused too. Reads (inbox, read_message, resume, wait, participants) are not gated.
- Attach of an attached id: the holder keeps its capability. Another process gets `participant_held` while the holder
  process is still running (`participant_holder_unknown` when it cannot be checked); once the holder has exited, a
  same-identity attach mints a new capability (a restarted session, a woken headless Codex turn). A non-advisory process
  may take over a live-held id with `allow_transition` (audit `send_cap: takeover`); the old holder is then stale.
- CLI: each call is a new process, so `--cap-file PATH` (or `MYCELIUM_COORD_CAP_FILE`) keeps capabilities in a 0600
  JSON file; `attach` writes it (output `capability_file`), every other op loads the entries for its store root.
- Wake: on an enforced task the Codex wake prompt starts with a `coord_attach` step carrying the recipient's stored
  identity, so the woken process can ack. A woken turn that skips it cannot ack, so the wake stays `uncertain`.
- Limits: containment, not authentication (a shell can edit the store); execution_* tools carry no participant identity
  and stay covered by the advisory authority gate; processes still running older code ignore the feature until they
  are restarted with this code.
- The binding is per server PROCESS, not per conversation: sessions served by one MCP process (the Antigravity App runs
  one coord server for all its conversations) share capabilities, so one App conversation can act as an observer that
  another App conversation attached. Both are advisory observers with the same powers. No host passes a caller
  identity to the server, so a finer binding is not available.
- Bridge (Claude) wake: the controller capability is re-checked immediately before the Enter keystroke, outside the task
  lock, so a takeover landing in the moment between that check and the keystroke is not excluded (the same window the
  execution pause gate has). The Codex route re-checks under the task lock held through launch.
- Review: Codex scoped review (9 findings: 7 fixed, 1 accepted limit, 1 test gap fixed) and one targeted verification
  (7 closed; the two items above documented). Evidence: evidence/sendcap-review/, live smoke evidence/sendcap-smoke/.
