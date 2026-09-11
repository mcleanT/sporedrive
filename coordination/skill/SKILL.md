---
name: mycelium-coordinate
description: >-
  Coordinate a paired Codex-supervisor / Claude-executor session through Mycelium: attach to an
  authorized task, exchange addressed messages with correlated replies, publish and read versioned
  checkpoints, and resume after compaction — WITHOUT relaying text through the user. Use this
  whenever two agent sessions (a supervisor and an executor, possibly in different repos) need to
  talk, whenever you must send/read/acknowledge a coordination message or review finding, whenever
  you need the current task checkpoint, or when reattaching a task after a compaction or restart.
  Provider-neutral: identical on both native Mycelium hosts.
---

# Mycelium session coordination

One shared, provider-neutral protocol lets a Codex **supervisor** and a Claude **executor** (and
optional **observers**) talk over an authorized task. The same operations are available two ways:

- **MCP tools** (server `mycelium-coord`): `coord_create_task`, `coord_attach`, `coord_send`,
  `coord_inbox`, `coord_read_message`, `coord_wait`, `coord_ack`, `coord_message_state`, `coord_set_cursor`,
  `coord_checkpoint_publish`, `coord_checkpoint_read`, `coord_resume`, `coord_participants`,
  `coord_detach`, `coord_notify_via_bridge`, and the bounded discovery reads `coord_list_tasks`,
  `coord_find_recipients`, `coord_list_sessions`.
- **CLI** (`mycelium-coord <op> …`) — the same operations for shell/hook use.

**Prefer the MCP `coord_*` tools** whenever the plugin is loaded — no path resolution needed. The
bare `mycelium-coord` CLI ships INSIDE the plugin (`<plugin-root>/coordination/bin/`), so it is
**not on PATH** — `command -v mycelium-coord` returns absent on a host that only installed the
plugin. When you need the CLI (shell/hook use, or the MCP server is absent), resolve it with the
shipped locator, invoked by ABSOLUTE path so it works from any cwd — never a cwd-relative
`scripts/...`:

```bash
# Claude (plugin hook/MCP env sets CLAUDE_PLUGIN_ROOT):
CLI="$("${CLAUDE_PLUGIN_ROOT}/coordination/bin/locate-mycelium-coord.sh")" || CLI=""
# Codex (plugin cache): use PLUGIN_ROOT/MYCELIUM_PLUGIN_ROOT, or let the locator search the cache:
#   "$PLUGIN_ROOT/coordination/bin/locate-mycelium-coord.sh" resume TASK PARTICIPANT
[ -n "$CLI" ] && "$CLI" resume TASK PARTICIPANT     # or exec directly: locate-...sh resume TASK P
```

The locator self-locates beside the launchers, then searches the plugin-root env and the
Codex/Claude plugin caches for a version that actually carries `coordination/bin`, and exits
non-zero if none exists. If neither the MCP tools nor the CLI resolve, do NOT fabricate a
send/ack/checkpoint — fall back to the bridge/brief path with truthful limits.

State lives outside every repository at `~/.local/state/mycelium/coordination/` (owner-only). It is
NOT scientific `.living/` knowledge and never triggers analysis bookkeeping.

## CLI quickstart (exact syntax)

The CLI takes **positional** task/participant ids — not `--task`/`--participant` flags. After a
compaction or restart, ONE bounded call restores you:

```
mycelium-coord resume TASK PARTICIPANT            # one compact execution/checkpoint/inbox read
mycelium-coord read-message TASK PARTICIPANT ID   # selected complete brief, no acknowledgment
```

Prefer that over re-reading pieces separately. The other common ops (positional ids, flags only for
options):

Resume/inbox/wait return summaries by default (10 messages, compact cap20). A summary is explicitly
incomplete: fetch a selected body with `read-message` / `coord_read_message` before acting. Use
`checkpoint-read` only for a relevant checkpoint; pass `--after-checkpoint REV` to resume to omit
unchanged checkpoint content. `--full` (MCP `compact=false`) is the explicit full-record escape hatch.
Retrieval does not acknowledge or advance a cursor. Save the returned cursor only for the page
actually exposed; pending messages remain reachable. `content_hash` hashes the coordination envelope,
not a referenced file's bytes; artifact verification uses its own hash.

Current execution state is included in resume/wait and outranks old checkpoint next actions.
`stop_waiting=true` means paused, draining, exhausted, closed, completed or expired: stop the wait loop.
Timeout with `unchanged=true` is not progress. Choose a finite wait within the host/task deadline and
let the tool do the waiting. Do not add clock calls, repeated screen/transcript reads or model-driven
heartbeat commentary; no recurring monitor is enabled by this protocol. A stored message still does
not wake an idle host. Exact delivery, authorization and completion rules below remain unchanged.

```
mycelium-coord inbox   TASK PARTICIPANT [--after SEQ] [--limit N] [--kind KIND]
mycelium-coord wait    TASK PARTICIPANT [--after SEQ] [--timeout S] [--kind KIND]   # bounded poll
mycelium-coord ack     TASK PARTICIPANT MESSAGE_ID [--note "…"]
mycelium-coord state   TASK PARTICIPANT MESSAGE_ID                                   # one message's state
mycelium-coord set-cursor TASK PARTICIPANT SEQ                                       # advance notify cursor
mycelium-coord send    TASK --id MSG_ID --from SENDER --to RECIPIENT --kind KIND \
                       --revision R (--text "…" | --artifact-ref PATH) \
                       [--artifact '{"file":"…","sha256":"…"}'] [--reply-to REQ_ID]
mycelium-coord checkpoint-read    TASK [--revision N]
mycelium-coord checkpoint-publish TASK --revision N --by PARTICIPANT --checkpoint '{…}'|-
```

`--to` is a participant id, a role, or `all`. `--artifact` values are JSON objects (repeat the flag
per artifact); a bare path string is not verifiable evidence.

### Owned local jobs, Eastern scheduling and output budgets (efficiency v2)

```
mycelium-coord job-run  JOB_ID --task TASK --action-id RES --dispatch-identity ID [--deadline S] \
                        [--on-stop keep|cancel] [--join S<=50] [--label "…"] -- CMD [ARGS…]   # CLI only
mycelium-coord job-join JOB_ID [--timeout S<=50] [--after-version N] [--tail BYTES]        # in-tool wait
mycelium-coord job-status JOB_ID | job-list [--task T] [--status S] [--limit N] [--cursor C]
mycelium-coord job-output JOB_ID [--stream stdout|stderr|supervisor] [--offset N] [--limit N] [--tail] [--budget B]
mycelium-coord job-cancel JOB_ID
mycelium-coord sched-plan   (--at "YYYY-MM-DD HH:MM" | --in 90m) [--tz America/New_York|UTC-05:00] [--now ISO]
mycelium-coord sched-verify --intended-utc ISO (--persisted-json FILE|- | --next-run-at ISO --active BOOL)
```

MCP (both hosts) exposes only the read-only routes: `job_status`, `job_join`, `job_list`, `job_output`,
`sched_plan`, `sched_verify`. There is no MCP launch route: a job is started by the owned CLI from the
host-authorized shell so its processes inherit that shell's permissions. A managed job runs under one
open work reservation (re-checked immediately before the actual launch), keeps an immutable request
record (identity, argv, cwd, deadline, reservation) and complete stdout/stderr on disk under the state
root; a replayed `job-run` with the same id and content never launches twice, and a different content
under an existing id is refused (`job_identity_conflict`). Refused, failed and unknown attempts stay
listed. In a paired session always pass the managed task, its open work reservation and the dispatch
identity it is bound to; without them the job is unmanaged local work and no task limit, deadline
or pause of the task applies to it. The wrapper terminates only its own child's process group, under
the job's declared deadline / cancel / `--on-stop` policy, with bounded SIGTERM then SIGKILL
escalation and the real outcome recorded under `cleanup` (`group_empty` false means owned work may
still be alive); a job whose supervisor was lost is recovered by `job-cancel` only after the recorded
process is re-identified as the owned one.

Join SOP: a synchronous or batched read needs NO join and NO host wait. For a running job, issue one
`job-join` (or `--join` on the run) per permitted interval; it waits inside the tool for at most 50 s
and returns `changed`, `timed_out`, `stop_waiting` (task deadline, paused/closed/expired/completed
execution) plus status/exit metadata — never a bare "still running" to interpret. Do not wrap it in
clock calls or sleep loops; a stored message still does not wake an idle host, and a local wrapper
cannot promise zero wake-ups for an indefinitely long desktop operation.

Owned outputs select fields before serialization and normally fit a combined 4 KB batch budget
(`truncated: true` with `next_cursor` / `next_offset` when cut; inline tails are trimmed before any
reference is dropped). Full evidence stays on disk and is retrievable with `job-output --offset/--limit`.
This caps only this package's outputs, never a host-native shell or file tool's output; `--full` /
`compact=false` and `read-message` remain the full-record escape hatches.

Scheduling: `sched-plan` is pure — it emits the intended UTC instant, the Eastern rendering and one-shot
submission data in two forms (America/New_York by default; fixed `UTC-05:00` only when explicitly
requested; gap and unresolved overlap wall times are refused; an `--at` value carrying its own offset is
honored as written). `scheduler.immediate.rrule` is DTSTART-free with explicit date/time/COUNT for the
app's ordinary immediate create route (it rejects DTSTART and schedules to the minute);
`scheduler.anchored.rrule` (also `rrule_utc`) carries DTSTART for `suggested_create` only. Create/update/pause the automation with the host's official
automation tool, then `sched-verify` the ACTUAL persisted `next_run_at`/status before any success claim:
`match` is the only pass, `cannot_evaluate` is neither pass nor fail, and `mismatch` on an active record
means `requires_native_pause` through that same official tool (the helper never writes the app store).

### Discovery (bounded, read-only)

Find work you do not already hold an id for, then join it with an ordinary `attach` — discovery
never joins, notifies, or broadcasts, and never crosses into another store. Every result is capped
by `--limit` with a truthful `truncated` flag.

```
mycelium-coord list-tasks [--project P] [--limit N] [--cursor TOKEN]     # tasks (scope by project)
mycelium-coord find-recipients TASK [--role R] [--host claude|codex] \
              [--exclude ID] [--include-detached] [--limit N] [--cursor TOKEN]   # who you may address
mycelium-coord list-sessions HOST [--live-only] [--limit N] [--cursor TOKEN]  # sessions -> task/participant
```

Each read is one page of at most `--limit` rows (clamped to a finite server maximum) in a stable
order. When more rows remain, `truncated` is true and `next_cursor` is an opaque token; pass it back
as `--cursor` to get the next page, and stop when `next_cursor` is null. This keeps every match
reachable within a fixed per-call budget — a small page size never strands later results.

- `list-tasks` returns lightweight task summaries only (never message/participant bodies); it is the
  only way to discover a task id you were not handed. **Joining stays a separate explicit step:** pick
  a `task_id` and `attach` to it.
- `find-recipients` reports the addressable participants of a KNOWN task, filtered by role/host,
  excluding yourself — so you address a concrete recipient instead of fanning out. Any `all`/role
  send stays your explicit choice.
- `list-sessions` carries `live` per record (true only while the bound participant is still attached
  on exactly that native session — the same authority `resolve-session` uses); a stale post-rebind
  record is `live:false` and is dropped under `--live-only`.

## Core rules (do not violate)

1. **Attach explicitly.** `coord_attach(task, participant, role, worktree_realpath, host)` — CLI:
   `attach TASK PARTICIPANT --role R --worktree WT --host host=codex|claude --host session=… --host
   native_id=…`. Give your own `worktree_realpath` (a supervisor's is outside the executor repo —
   that's expected) and a native identity. Attaching a supervisor grants **no** repository-write or
   lifecycle-owner authority. The executor's worktree must equal the task's.
2. **Messages are addressed and idempotent.** `coord_send(message_id, …)` — reusing a `message_id`
   with the same content is a no-op; with different content it is **rejected**. Pick a fresh id for a
   changed payload. Recipient is a participant id, a role, or `all` (this task only — never broadcast
   across tasks).
3. **Four distinct states, never collapsed:** `persisted` → `delivered` (past your cursor or
   bridge-notified **to you** — delivery is tracked per recipient, so a broadcast delivered to one
   recipient is not `delivered` for the others) → `acknowledged` (you called `coord_ack`, and only an
   addressed recipient may) → `completed`. `completed` requires a `completion_receipt` that replies
   to the request, matches its `task_revision`, and carries **verifiable** artifact evidence
   (`{file, sha256}` or `{file, contains}`) that actually checks out. An unverified claim reads as
   `completion_claimed` — a receipt that a message was read never means its action completed.
4. **Wait is bounded, not a wake-up.** `coord_wait(…, timeout_s)` polls with a finite timeout and may
   return `timed_out`. A stored message cannot wake an idle peer. For an idle Claude, a supervisor may
   nudge via `coord_notify_via_bridge` (reuses the cmux bridge; only a bridge *accepted* outcome marks
   the message delivered — a busy refusal stays pending-until-next-turn). Nothing here relaunches a
   session or claims exactly-once execution.
5. **Checkpoints are versioned + immutable.** `coord_checkpoint_publish(task, revision, checkpoint,
   authorization_ref)` — a revision is write-once. `authorization_ref` is recorded, never a grant.
6. **Resume after compaction** with `coord_resume(task, participant)`: it returns the current
   checkpoint + bounded unacknowledged messages, not a transcript. The SessionStart attach hook
   surfaces this automatically — but only for the native session that OWNS the selection. Selection
   is bound to **this native session id** (recorded by `coord_attach` / `select-session`, resolved by
   the session's own SessionStart id), never a shared repo/cwd marker, so two sessions in one worktree
   never resolve to each other and a controlled rebind (`attach … --allow-transition`) retires the old
   session's selection. An explicit `MYCELIUM_COORD_TASK`/`MYCELIUM_COORD_PARTICIPANT` env is only a
   hint: the hook still validates this session's native host+session against the participant before
   using it, so an unrelated or inherited-env session cannot impersonate a participant.

## Typical flow

1. Owner authorizes a task → `coord_create_task`. Supervisor and executor each `coord_attach`.
2. Supervisor `coord_send(kind=review_finding, task_revision=R, reply_to=…)`.
3. Executor `coord_wait` → `coord_ack` → does the work in its own repo → `coord_send(kind=
   completion_receipt, reply_to=<request>, task_revision=R, artifacts=[{file, sha256}])`. The
   completion receipt's `task_revision` MUST equal the request's revision **R** — a receipt whose
   revision differs does not complete the request. Bump the task revision only through an explicit
   amendment, never on the completion reply.
4. Supervisor `coord_wait` for the reply, `coord_message_state` → `completed`, resolves from evidence.

Repository edits stay owned by the executor. Coordination is talk + state; it changes no code.
