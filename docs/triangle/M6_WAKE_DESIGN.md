# M6 Wake — design draft (lead, 2026-11-23)

**Problem.** A message stored on the coord board does not reach an idle session until that session's next turn ("stored messages cannot wake idle parents", global rule). The triangle needs one honest wake route per host, or an explicit "no wake" outcome. It must never fabricate a delivery.

**Facts (evidence/m5-probes-codex.json, m1):**
- Claude: an existing cmux bridge route (`notify_via_bridge`) types one line into a Claude TUI pane. It is Claude-executor only, and checks binding identity, pause and expiry before the mutation.
- Codex: `codex exec resume <thread> <prompt>` runs one headless turn on the same thread (VERIFIED). `codex queue --thread` only stores to ~/.codex/queue_1.sqlite. Queued items drain when a process next loads the thread: an exec resume answered the first one in-turn, and the second turn was aborted at exit. Live-TUI pickup is CANNOT-EVALUATE.
- Antigravity: no verified wake. It has native send_message/manage_inbox tools (untested). It is advisory, and consults run on demand through agy_ask.

**Proposal.**
1. `wake.py` holds a registry keyed by recipient host kind. All routes share the same gates (recipient resolution, pause/expiry re-read immediately before the mutation, idempotent request id per task+message) and the same outcome vocabulary: `accepted | uncertain | unavailable | not_supported | refused:<reason>`. Only `accepted` is recorded as a delivery.
   - **claude → existing bridge**, behaviour unchanged (thin adapter over notify_via_bridge).
   - **codex → exec-resume adapter**, OFF unless the task opts in (`protocol_features` 'codex-exec-wake'), because every wake is a billed model turn. A live-thread guard refuses (`refused:thread_live`) when a running process holds the thread; detection to be probed (lsof on the rollout file vs process args). The prompt is fixed: "New coordination message <id> on task <t>; read it with coord_inbox and act within your role." It runs foreground with stdin </dev/null and a bounded timeout. The receipt carries exit, the thread rollout delta and CANNOT-EVALUATE on timeout.
   - **antigravity → not_supported (next-turn inbox)**, recorded honestly. The consult path (agy_ask) is the active route.
2. Probes, on scratch only:
   - (P1) live-thread detection: open a scratch Codex TUI in a scratch cmux pane, find which signal shows the thread is live, then close it;
   - (P2) does the live TUI pick up a `codex queue` item? If yes, queueing becomes the preferred Codex route for live threads (no extra turn);
   - (P3) a headless exec-resume wake end to end on a scratch store.
3. bridge_link.py is unchanged apart from the registry calling it.

**Open questions.** Is exec-resume wake worth its cost and risk versus "codex: next-turn only" plus the bridge for Claude? Should Codex wake ever fire for advisory or observer recipients?

## Frozen acceptance (after second opinion, 2026-11-23)
1. **Store isolation (blocking for any Codex wake probe).** A woken `codex exec resume` turn loads the user Codex config, which registers mycelium-coord. Every probe overrides that server, via `-c` for the run only, to the dev worktree code and a scratch store. The debug log must confirm the override before any message exists. The woken turn's MCP calls must be shown to be permitted. Exit 0 with denied tools is not success.
2. **Delivered = store evidence only.** A wake is `accepted` only if the store shows that message exposed past that recipient's cursor (delivered or acknowledged) during the wake. Anything else is `uncertain`: it stays pending and is never auto-retried, because a retry is another billed turn.
3. **Safe to resume is defined positively.** Codex exec-resume wake fires only for a recipient whose stored host record marks the thread as headless-owned. Anything else, or anything unknown, is refused. No liveness heuristics (lsof, process args). An interactive Codex (TUI or app) is next-turn only, unless probe P2 proves live queue pickup.
4. **Target and trigger.**
   - The thread id comes only from the recipient's stored session, which the selection guard protects; there is no caller-supplied target.
   - Wake is an explicit sender call. It is core-gated (authority_op) and refused in advisory processes.
   - Observer and advisory recipients are never woken.
   - Wake fires only for work-requesting kinds (task, review_request, consult_request), capped per task.
5. **Queue interaction.** Refuse a resume while the thread has queued items. Probe P2 cleans up its own queue rows.
6. **Order.**
   - The registry, the Claude adapter (existing bridge, behaviour identical, tests reconciled by id) and the honest `not_supported` outcomes are built regardless.
   - The Codex exec-resume route is built only if P3 shows store-evidenced delivery under isolation.
   - P2, the visible scratch TUI in cmux, runs last.

## Implementation (2026-11-23)
- Built as `coordination/mycelium_coord/wake.py` (registry keyed by recipient host kind; one entry `wake_recipient`),
  CLI op `wake`, MCP tool `coord_wake`, protocol feature `codex-exec-wake`. Behaviour is specified in FEATURES.md ("Wake (M6)").
- Frozen acceptance mapping: (1) isolation = env-injected extra args (`MYCELIUM_WAKE_CODEX_ARGS`) + deployment note; no bypass
  flag can be passed. (2) accepted only on store evidence after the run (ack/claim/complete or cursor past); anything else
  uncertain and never re-run (stored receipt + in-flight claim). (3) positive `wake: headless` host marker, no liveness
  heuristics. (4) target only from the stored participant; authority_op gate; observers and non-work kinds refused; per-task
  cap. (5) refuses while the thread has queued items (read-only sqlite; unknown schema fails closed). (6) Claude route is
  the unchanged bridge (bridge_link.py not modified), Antigravity not_supported.
- M7 repair, five positive rules: (1) thread id must be a UUID (`refused:invalid_thread_id`); (2) `MYCELIUM_WAKE_CODEX_ARGS`
  allowlist = `--ignore-user-config` and `-c mcp_servers.*=...` only (`refused:codex_args_not_allowed`); (3) the common
  pause/expiry/draining/closed gate is applied to every route right before its mutation (Claude included);
  (4) recipient re-read right before Popen: attached, codex, same session, `wake=headless` else `refused:recipient_changed`;
  (5) timeout finite in (0, 900] (default 240) else rejected before side effects, and any post-Popen exception kills the
  process group before recording uncertain.
- Choices made: a refused outcome is recorded but is not billed against the cap and may be retried later; wake is also
  refused when the message is already acknowledged (it would be a wasted turn); bridge arguments are required on the Claude
  route (they name a binding that the adapter verifies against the stored recipient, not a free target).
- Queue guard (fixed 2026-11-23 after a check on a scratch copy of the real DB, codex 0.171.0): only table `queued_items`
  with column `thread_id` counts (drained items are deleted). `queued_thread_revisions` keeps a permanent row per thread and
  is ignored. Any other layout, or an unreadable DB, gives `queue_unknown` (refused). Remaining limit: a row queued after the
  check and before launch is not seen (no cross-process lock with Codex's queue).
- Rule (4) closed after the Codex verification: the recipient re-read and `Popen` run together under the task lock
  (attach/detach/transition take the same lock); the lock is released right after launch so the woken turn's coord_ack is
  not blocked.
- Review repair (2026-11-23, F2 and F4): (a) per-thread run lock `codex-thread-<uuid>`, zero wait, taken before the task lock
  and held across Popen to communicate; busy -> `refused:thread_wake_in_progress` (retryable, unbilled, replaces the in-flight
  claim). Tested from a second thread, and with the original two-wake reproducer (no overlap, second wake succeeds later).
  (b) every codex receipt carries `queue: {state, detail}`; an absent default queue DB is `unknown` when another
  `~/.codex/queue_*.sqlite` exists, an absent env-override path stays `clear`. (c) the route table is built from
  `model.HOST_KINDS`. Tests use temp directories only (`coordination/tests/test_review_repairs.py`).
