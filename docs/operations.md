# Operations

How managed work is bounded, how sessions wait and resume, and what to check when something goes
wrong.

- For how to drive the workflow, see the
  [driver protocol](../src/codex/skills/cmux-driver/references/driver-protocol.md) and the
  [cmux runbook](../src/codex/skills/cmux-driver/references/cmux-runbook.md).
- For the protocol reference, see the
  [coordination skill](../coordination/skill/SKILL.md).

## Managed and unmanaged tasks

- **Plain coordination** means create a task, attach, send, acknowledge and checkpoint. It works
  without any execution record, and nothing gates it.
- **A managed task** adds an execution record. You open it with `mycelium-coord exec-open`, or the
  `execution_open` MCP tool.
  - Every actionable dispatch must first **reserve** from the task's allowance.
  - The reservation must then be **claimed** against one concrete dispatch identity.
  - A reservation cannot fund a second, different dispatch.
  - A review launch cannot be spent as a work dispatch.

Whether a message is actionable is a **trusted-agent disposition**: a boolean the sender sets
honestly. It is not parsed from free text. The store fails closed on a corrupt managed record.

## Bounded execution

The defaults live in `coordination/mycelium_coord/execution_policy.json`:

- 4 work dispatches: initial brief, one review, one repair brief, one repair verification;
- 2 review launches;
- a 900-second wall-clock deadline for each owned reviewer process.

A brief may only lower these limits. Raising or extending a limit, or unpausing a task, needs a
user authorization reference (see [Owner follow-up and recovery](#owner-follow-up-and-recovery)).

Lifecycle gates:

- **Pause:** the task enters a *draining* state. Claims and bridge deliveries are then refused with
  `execution_paused`.
- **Expiry and closure:** these are refused in the same way, with `execution_expired` or
  `execution_closed`. Refused work is reported, never silently dropped.
- **Closure:** when the frozen required criteria have accepted evidence, the task closes
  automatically. There is no separate approval step.
  - After closure, only an **evidenced repair** can dispatch: one linked to an open,
    evidence-backed acceptance blocker.
  - Optional improvements go to a backlog.
- **Technical readiness versus acceptance:** "the code runs and tests pass" is technical readiness.
  The owner's frozen acceptance criteria define "done", and the executor does not renegotiate them.

All of this state lives on disk. A session that compacts or restarts re-reads the same record.

What is **not** bounded:

- total tokens, reasoning effort or cost inside a desktop session;
- arbitrary host tools;
- the host's own file and shell permissions.

## Waiting, resuming and receipts

- **Resume and read.** `resume` returns a compact packet: execution status, stop state, checkpoint
  identity, participants and summaries of pending messages. Read a full message only when you act
  on it, with `read-message TASK PARTICIPANT MESSAGE_ID` or `coord_read_message`.
  - `--full` (`compact=false` in MCP) returns complete records.
  - Compact views do not acknowledge messages or advance cursors.
- **Waiting.** A wait returns early with `stop_waiting=true` on pause, draining, exhaustion,
  closure, completion or expiry. A plain timeout returns `unchanged=true`.
  - `wait-plan --preferred` prints the recommended call shape for each host: a 50-second inner wait
    under a 60-second outer allowance.
  - Waiting is a bounded local poll, not a wake-up. A stored message never wakes an idle host.
- **Receipts.** `exec-receipt TASK [--after-version N]` is the single bounded read between steps.
  An unchanged state version returns `changed=false`. `exec-evidence` pages a recorded artifact
  and reports truncation honestly.
- **Local jobs and schedules.**
  - `job-run` launches owned local work under one managed reservation. The request is immutable
    and the output stays on disk.
    - Launching and controlling a job is available only from the CLI.
    - Reading it (`job_join`, `job_status`, `job_list`, `job_output`) is also available as MCP
      tools.
  - `sched-plan` / `sched-verify` plan a schedule in Eastern time and check the scheduler's
    persisted `next_run_at`. They never write the scheduler's store.
- **Routine workers.** `worker-run` / `worker-result` launch one bounded Codex worker with an
  explicitly chosen profile, from a compact prompt file. These are available only from the CLI.
  - A failed or invalid result writes one `escalation.json`.
  - A failed result is never retried with another model.
  - The profile's model must be available to your Codex account.

## Owner follow-up and recovery

A completion receipt is a record. It does not forbid the owner from asking for more work later.

- **Recovering the same run.** `exec-change-limits` and `exec-unpause` recover a paused, exhausted
  or expired run in place.
  - Both require `--authorization <owner instruction ref>`.
  - They never reset past usage, and they append a `scope_amendments` entry.
  - `unpause` is refused on an expired run: extend `expires_at` first.
- **Opening a new run.** For a completed or closed run, a new owner instruction opens the **next
  run** of the same execution:

  ```bash
  mycelium-coord exec-owner-request TASK --request-id ID \
    --authorization <owner instruction ref> --scope <scope/acceptance ref> \
    [--add-limits JSON] [--expires-at ISO]
  ```

  - The prior run, its receipt and its usage are archived unchanged.
  - The new scope gets fresh acceptance state.
  - Only the allowance the owner added becomes available.
  - Replaying the same request id is idempotent.

An agent never grants itself this authority. Questions, reviews and stale checkpoints never restart
work.

## Hosts and sessions

- **Fresh sessions only.** Installing or re-exporting affects fresh sessions only.
- **Implementation model.** Implementation runs on an explicitly launched model.
  `src/codex/skills/cmux-driver/scripts/executor_session.py launch` starts a dedicated executor
  with an explicit `--model` and a pre-generated `--session-id`. `verify` checks an existing
  session before it is reused.
- **Direct messaging.** Short opaque `sd:` handles resolve only within the addressed task,
  participant and native session. Per-session status comes from the native hooks. There is no
  dispatcher, daemon or listener.
- **Codex desktop idle wake is experimental.** A read-only discovery probe and a guarded wake entry
  point (`wake-peer`) exist. No delivered wake has been validated, so the capability reports
  `not_established`.

## Minimal message exchange

This is an **unmanaged** round trip, with no `exec-open` and no reserve or claim. Use a scratch
store, never a shared one.

```bash
BIN=coordination/bin/mycelium-coord
ROOT=/path/to/scratch/coord-state     # --root
WT=/path/to/an/existing/worktree      # must match between create-task and the executor attach

$BIN --root "$ROOT" create-task demo-task --project demo --worktree "$WT"

# Each participant attaches with its role and native host identity (--host is required).
$BIN --root "$ROOT" attach demo-task sup-1  --role supervisor --worktree "$WT" \
  --host host=codex  --host session=sup-session-1
$BIN --root "$ROOT" attach demo-task exec-1 --role executor  --worktree "$WT" \
  --host host=claude --host session=exec-session-1

# --kind is one of: task, amendment, review_finding, progress, blocker, question,
# checkpoint_request, checkpoint_ready, completion_receipt, acknowledgment.
$BIN --root "$ROOT" send demo-task --id msg-1 --from sup-1 --to exec-1 \
  --kind task --revision 0 --text "start the smoke run"
$BIN --root "$ROOT" inbox demo-task exec-1 --after 0
$BIN --root "$ROOT" ack   demo-task exec-1 msg-1 --note "starting now"

$BIN --root "$ROOT" send demo-task --id msg-2 --from exec-1 --to sup-1 \
  --kind progress --revision 0 --text "smoke run started" --reply-to msg-1
$BIN --root "$ROOT" inbox demo-task sup-1 --after 0
$BIN --root "$ROOT" ack   demo-task sup-1 msg-2 --note "seen"

$BIN --root "$ROOT" checkpoint-publish demo-task --revision 0 --by exec-1 --checkpoint '{"status":"started"}'
$BIN --root "$ROOT" checkpoint-read    demo-task
```

Each command prints one JSON object. The same operations exist as `coord_*` MCP tools: the Claude
server is `plugin:mycelium:mycelium-coord` and the Codex server is `mycelium-coord`.

## Lifecycle hook behavior

In an initialized repository, the hooks do the following:

- **SessionStart** opens a session log and injects context.
- **PostToolUse** records activity, and script lineage for recognized analysis commands.
- **Stop** asks for `.living/` updates and a five-heading handoff before it accepts a stop.

Stop and housekeeping rules:

- **Stop prompts:** automatic Stop blocks are limited to two per session. Unresolved state is kept
  in `.mycelium/stop-retry-*.json` for repair, not discarded. A real repair clears the budget.
- **Stop-lock contention:** this produces at most one actionable block per unresolved condition.
  The hooks never force-unlock a live owner.
- **Housekeeping:** knowledge audit and transfer dispatch go through a durable attempt ledger, with
  bounded retries and cooldown.
- **Late events:** a host-identified PostToolUse event that arrives with no active transaction does
  not create or change state. Legacy payloads without a session identity keep their earlier
  markerless behavior, for compatibility.

## Troubleshooting

| Symptom | Likely cause and check |
|---|---|
| `--verify-only` reports mismatches you did not expect | `--source mycelium-source` was omitted, so it verified against the default maintainer path. |
| No Mycelium context at session start (Claude) | The repository was never initialized, or the session predates the install. Initialize it and restart. |
| Codex hooks never fire | They are not trusted in `/hooks`, or Codex was not restarted after trusting them. |
| Bridge refuses to bind | Session identity mismatch, or a Fable/default-model session bound for implementation. Launch an explicit-model executor. |
| Delivery refused with `execution_paused` / `execution_closed` | The managed task is paused or closed. Use an owner-authorized unpause or owner request; don't resend. |
| `attach` fails with `invalid_participant` | `--host host=… --host session=…` is missing. |
| `send` fails with `invalid_kind` | Use one of the ten protocol kinds listed above. `brief` is not one of them. |
| Bridge password ignored | The password file is group- or world-readable. Run `chmod 600`. |
| Installed files show DRIFT | Edit `src/` and reinstall, or `install --accept-drift`. The snapshot keeps your edit. |

## Live checks (deliberate, not routine)

- **`scripts/smoke_check.sh`** runs fresh `claude -p` and `codex exec` instruction smokes. It makes
  model calls.
- **`bridge/run_live.sh`** and **`bridge/live_acceptance.py`** need a real cmux session, a
  disposable directory, network access and both CLIs.
- **The lifecycle-audit protocol** is in
  [`mycelium-source/skills/lifecycle-audit/references/audit-protocol.md`](../mycelium-source/skills/lifecycle-audit/references/audit-protocol.md).
  It describes a black-box host audit in a disposable repository.
