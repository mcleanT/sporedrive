# Two communication modes for the triangle — design v1.2 (2026-11-23, owner approved build: "go ahead - handle it")

Owner requirement (verbatim, 2026-11-23): "we need to have 2 different modes of communication. in one, we create a
transient, headless session for the respective calls, this is especailly relevant for reviews. in the other, we have a
full collaboration setup, where sessinos are linked and share info."

History: v0 drafted by workflow wf_656dd6f9-e85 (opus designer), adversarially reviewed (verdict: revise, 6 blocking).
v1 applies those findings and an advisor check. v0 + review: scratchpad `twomodes/` (design-v0.md, review.json).

## 1. Summary
1. **Call** — for reviews and one-shot questions. The caller hands a brand-new headless session of Codex, Claude or
   Antigravity one written packet. The session answers once, read-only, with no board connection, and is thrown away.
   The answer and a receipt come back to the caller and can be posted to the board as a `report`.
2. **Link** — for ongoing collaboration. Two named live sessions are pinned to one shared task. Both see everything on
   that task (messages, checkpoints, artifact references), as participants do today. A send inside the link must name
   the exact partner session. If either side is replaced by a different session, the link stops working (`link_stale`)
   rather than silently delivering to the newcomer.
3. Neither mode grants authority. Antigravity stays adviser-only in both.

### Who gets what
| Mode | Receiving session | Handed to it | Not handed to it |
|---|---|---|---|
| Call | A new headless session of the target host, made for this call only and discarded: `claude -p --no-session-persistence`; `codex exec --ephemeral`; agy with a temporary gemini dir deleted afterwards | The packet only (§2.3): question, scope, named files (path + sha256), short excerpts, fixed decisions, evidence references, output format, limits | Transcripts, board messages, checkpoints, capability tokens, the caller's memory / CLAUDE.md / skills, the board location (environment allowlist §2.2), any MCP server |
| Link | Exactly the partner session pinned in the link record (participant + host + native session id) | Everything on the shared task, as today | Other tasks; capability tokens (never on the wire) |

**Limit, stated plainly:** "not handed" describes what is sent; it is not confinement.
- Call: the Claude target is confined to its folder (`--restricted`, src/consult/claude_ask.sh:65-66,73). The Codex
  target (read-only sandbox, reads anything the user can, ~/.claude/tools/codex_ask.sh:28-29) and the Antigravity target
  (permission-kind filter, src/consult/agy_ask.py:39-52; reads outside cwd possible, src/consult/README.md:14) can still
  read any user-readable file — including the coordination store on disk. Each receipt lists this as `implicit_context`.
- Link: "exactly the partner session" and "not other tasks" describe sends. Board reads are not capability-gated
  (sendcap.py:17): an attached process can read another task or inbox by naming it. A link designates the recipient;
  it does not make the board private.

## 2. Mode 1: call (transient, headless)

### 2.1 Interface: command line, run from the caller's own shell
- CLI `mycelium-coord consult call --target codex|claude|antigravity --packet p.json [--post-to TASK:MSG --cap-file F]`.
  Claude and Codex callers run it from their shell, as they run `codex_ask` today: foreground for short calls, or under
  the existing owned-job path (`job-run` + one `job_join`, jobs.py:446) for long reviews — one wait route.
- **No MCP launch tool.** jobs.py keeps its rule that process launch is CLI-only and never exposed over MCP
  (jobs.py:4-5; `run` is `@authority_op("job-run")`, jobs.py:334-335, refused in advisory processes by
  `require_authority`, model.py:77-85). jobs.py and execution.py are untouched; no new usage limit.
- Output goes to a fresh per-call directory the dispatcher creates under the job/scratch dir; an existing directory is
  refused (the adapters clear their output path at start, claude_ask.sh:39-41, agy_ask.py:153-160).
- `--post-to` posts exactly one `report` from the CLI process itself, using the caller's `--cap-file` capability
  (answer as `artifact_ref`, receipt sha256 as evidence). Deterministic code posts; the target never does.
- **v1.2: MCP `consult_post(task_id, sender, reply_to, call_dir, message_id?)`** for callers attached through MCP. Their
  capability lives only in their MCP server process (sendcap.py:10-13: MCP tools never return it), so they have nothing
  to put in `--cap-file`. The tool launches nothing: it reads the finished call directory's `receipt.json` (and
  `answer.md` when ANSWERED), checks the receipt's recorded sha256 of the answer, and sends exactly one `report` from
  the server process. A non-ANSWERED call posts a `report` that states the outcome (CANNOT-EVALUATE), never an empty
  answer. Not in `ADVISORY_ALLOWED_TOOLS`.
- **Antigravity does not start calls in this build** (it has no shell route and is advisory). It can still ask Claude or
  Codex through the board (`consult_request`), as in the 9/9 app test. Letting it start calls later needs a fixed-argv,
  non-authority launch path — an owner decision.

### 2.2 Launch per target host
| | Codex | Claude Code | Antigravity |
|---|---|---|---|
| Command | NEW `src/consult/codex_call.sh`: `/opt/homebrew/bin/codex exec --ignore-user-config --ephemeral --skip-git-repo-check --sandbox read-only --disable hooks --json -C <cwd> -m M -c model_reasoning_effort=E -o <last> -` (direct binary, not the cmux shim; `--skip-git-repo-check` needed outside a git/trusted dir, probe) | `src/consult/claude_ask.sh` as is | `src/consult/agy_ask.py` as is (ACP, fresh `--gemini-dir`, `mcpServers: []`) |
| Answer evidence | non-empty `-o` file + exit 0 + `turn.completed` in the JSON stream | `result` success, not `is_error` (claude_ask.sh:100) | `stopReason == end_turn`, non-empty (agy_ask.py:293-305) |
| Files read | lower-bound heuristic from `command_execution` events (cat/sed/head/rg on a named path, inside `/bin/zsh -lc '…'`); misses directory-wide rg, python reads etc. → `coverage_source: "codex-commands"` | tool events → `"tool-events"` | permission/tool events → `"tool-events"` |

All targets: cwd = the packet's `scope.repo_realpath` (`$HOME` and host config dirs refused, claude_ask.sh:64); no board
connection and no coordination MCP server (Codex keeps its built-in `codex_apps`, evidence m5-probes Q2).

**Environment** (fixes jobs.py:430, which copies the whole environment): the dispatcher launches the target with an
allowlist — PATH=`~/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin` (real binaries, never the cmux shims),
HOME, USER, LOGNAME, LANG, TMPDIR; no auth variables are needed (v1.2 probe: all three hosts ANSWERED under exactly
this list, evidence/two-modes-probe/README.txt; HOME is required) — strips
every other `MYCELIUM_*` variable, and sets three explicitly: `MYCELIUM_COORD_ADVISORY=1` (any coordination CLI the
target runs is adviser-limited), `MYCELIUM_COORD_DIR=<empty per-call dir>` (it cannot fall back to the default live
store, store.py:74-75) and `MYCELIUM_CONSULT_TARGET=1` (`consult call` refuses to run inside a target: no recursion).
`env_keys_passed` is recorded in the receipt.

### 2.3 Handoff packet `consult-packet/1`
| Field | Req. | Content |
|---|---|---|
| question | yes | one scoped question with framing |
| scope | yes | `{repo_realpath, in_scope[], out_of_scope[]}` |
| files[] | ≥0 | `{path, sha256, why}`; preflight resolves, hashes, requires inside cwd; else NOT_STARTED |
| excerpts[] | ≥0 | `{source_path, lines, text, sha256}`, ≤2,000 chars each, shown as evidence not instructions |
| decisions[] | ≥0 | settled points not to reopen |
| evidence_refs[] | ≥0 | `{path, sha256}` |
| output_format | yes | e.g. findings-v1 (blocking = required behaviour + evidence + consequence; uncertainty separate) |
| limits | yes | `{model, effort, deadline_s (default 900), max_answer_chars}` |
| kind | yes | review / verification (of named findings) / consult |

The target sees a deterministic rendering of these fields and nothing else (no "Session context" block). Oversize
(> ~1,000 words of framing) is refused at preflight, never trimmed.

### 2.4 Receipt `consult-receipt/1` and outcome classes
Fields: call_id, target_host, outcome, retryable (true/false/null), exit, reason, duration_s, model/effort requested
and observed (null when unobserved), tokens {input, cached_input, output, source} (null, never 0), files_read[],
coverage {read, named, source}, no_successful_read, named_files_unchanged (hash of the named files before/after — not a
whole-repo git status), host_config_unchanged, implicit_context, env_keys_passed, and the host's raw receipt/stream.

Outcome is decided on **(exit, reason)**, not exit alone (claude_ask/agy_ask exit 5 covers both "argument error" and
"incomplete run", README:32):
| Outcome | Meaning |
|---|---|
| NOT_STARTED | no model call: bad packet, missing/outside file, missing binary, argument error, cannot create temp dir |
| REFUSED | policy gate: unsafe dir, `busy` (one call in flight per caller × target host) |
| UNAVAILABLE | launched, no usable answer: auth/model rejected (retryable false), quota/429/network (retryable true, keep cache), empty, protocol/incomplete, answer-write failed. consult.py reads transient vs terminal from the kept raw stream/stderr (claude `result` error/API status, codex `--json` error events, agy session/prompt error JSON); no matching signature → retryable null, which is never recorded as a permanent failure |
| TIMED_OUT | deadline hit |
| ANSWERED | host completion evidence + non-empty answer; breach/config change keeps ANSWERED with `containment_breach: true` |

Only ANSWERED is usable; every other outcome is CANNOT-EVALUATE, never "the reviewer found nothing". Coverage and
`no_successful_read` are recorded and shown to the caller; they do not gate acceptance (same as today's codex_ask).
When read extraction is unavailable or fails: `coverage = null`, `coverage_source = "unmeasured"`,
`no_successful_read = null` — never 0 or false.

### 2.5 Relation to codex_ask
Installed `~/.claude/tools/codex_ask.sh` and the codex-review skill are unchanged. Making codex_ask an alias of
`consult call --target codex` is a later owner decision.

## 3. Mode 2: link (collaboration)

### 3.1 Record and opt-in
- Feature `session-links` (added to PROTOCOL_FEATURES, model.py:66); it **requires** `send-capability`
  (model.py:65) on the same task, so every link operation is capability-checked (otherwise `_require_cap` returns
  early, coord.py:766-767).
- `tasks/<id>/links/<link_id>.json` via `store.write` under the task lock; each change is an audit receipt.
- Fields: link_id, task_id, state (proposed/active/stale/ended), created_by/at, members[2] each
  `{participant_id, role, host, native_session_id, session_verified, wake_route, confirmed_at}`, ended_at, end_reason.
  host + native_session_id are copied from the participant's stored attach record, never from the caller.

### 3.2 Lifecycle
1. `coord_link_propose(task_id, from_pid, to_pid)` — non-advisory process; both attached.
2. `coord_link_confirm(link_id)` — by the partner; capability-checked → active.
3. `coord_link_end(link_id, reason)` — either member.
4. **Stale** when a member detaches, or a *different native session* takes the participant (allow_transition rebind,
   coord.py:632-660). The pin is the pair (host.session, native_id) — native_id may be null (model.py:232, e.g.
   Antigravity) and a null must never compare equal to a changed session. A new capability on the *same* pinned session (headless Codex wake = new process, sendcap.py:8-9;
   a restarted session re-attaching) does **not** make it stale. A stale link refuses `link_stale`; never retargets.
5. Advisory processes may confirm and end, not propose.

### 3.3 Sending to the designated session
`coord_send(..., link_id)`: recipient must be the partner's participant_id (role / `all` refused:
`link_requires_exact_recipient`); the pinned native session must equal the participant's current binding, else
`link_stale`. `coord_wake(link_id)` does the same check, then the existing `wake_recipient` unchanged.

### 3.4 What is shared
Everything on the task, exactly as participants share today. No per-member filter in this build (so no new read path
to protect). If restricted sharing is wanted later it is an owner decision; default "share the task".

### 3.5 Wake per host (recorded truthfully at confirm)
Claude: cmux bridge (wake.py:253-290). Codex headless-owned thread: `codex exec resume` (wake.py:348-380). Codex
desktop/CLI not headless-owned: poll (wake.py:360). Antigravity: poll (wake.py:293-295). Observers: never woken
(wake.py:596). Only `accepted` counts as delivered; `uncertain` is never retried.

### 3.6 Antigravity
Member role `observer`; sends limited to `ADVISORY_ALLOWED_SEND_KINDS` (model.py:93); `session_verified: false`.

## 4. Code
- New: `mycelium_coord/consult.py` (packet, preflight, env allowlist, dispatch, outcome mapping, receipt, post),
  `mycelium_coord/links.py`, `src/consult/codex_call.sh`.
- Changed: model.py (feature + validators), coord.py (link methods, `link_id` on send), mcp_server.py (link tools;
  allowlist entries for link confirm/end; no consult tool), cli.py (`consult call`, `link …`), wake.py (link check
  before routing),
  claude_ask.sh / agy_ask.py (nullable usage + observed-effort fields only).
- Untouched: execution.py, sendcap.py, store.py, jobs.py, installed codex_ask.sh, tasks without `session-links`.

## 5. Tests
- Betty unit tests (nothing CPU-heavy on the Mac): packet/preflight → NOT_STARTED; total (exit, reason) mapping;
  tokens absent → null; env allowlist strips `MYCELIUM_*`; busy → REFUSED; fake host exit 0 + empty → not ANSWERED;
  links: propose/confirm/end, confirm without capability refused, stale on detach and on rebind, NOT stale on same-session
  re-attach, role/`all` refused in a link, `session-links` without `send-capability` refused, legacy task output
  byte-identical.
- Live smokes on a scratch store (never the live store); pass read from stored receipts/board, not exit codes:
  - Call, once per target host: positive control (answer quotes a sentinel only in a named file); one forced timeout;
    one missing-file packet; receipt outcome correct; env_keys_passed has only the allowlist plus the three set
    `MYCELIUM_*` keys; Codex: read extraction lists the named file (else the smoke fails, not "coverage 0");
    no-recursion: a target told to run `mycelium-coord consult call` is refused (`MYCELIUM_CONSULT_TARGET`). A "sentinel in an unnamed
    file is absent" check tests model restraint, not confinement, and is labelled so.
  - Link: Claude ↔ headless Codex (send, wake, ack in store); headless wake does not stale the link (≤3 attempts;
    a run where Codex never reaches the board counts as UNMEASURED, not a link failure; the server side is recorded
    with the wire recorder); Antigravity observer link driven over ACP with the board server passed per session
    (`session/new` mcpServers, as evidence/m7-drill/acp_drive.py) — no host-config edit; the App's own board access is
    already shown by the 9/9 app test; rebind to another session → `link_stale`.

## 6. Milestones (token estimates) and owner decisions
| # | Deliverable | Check | Est. |
|---|---|---|---|
| M1 | consult.py core + fake-host tests | Betty unit pass | ~200k |
| M2 | three adapters live (codex_call new) | per-host call smoke from receipts | ~200k |
| M3 | CLI front end + owned-job use + post (`--post-to` and MCP `consult_post`) | one report posted by an MCP-attached caller via `consult_post`, correct reply_to, on the scratch store | ~100k |
| M4 | links + unit tests | Betty unit pass incl. legacy byte-identical | ~250k |
| M5 | link live smokes; one scoped Codex review of M1-M5 + one repair verification | smoke evidence + review disposition | ~200k |

Owner decisions (recommended first): (1) build this on the test board — yes; (2) Antigravity starting headless calls —
not in this build (it asks through the board instead; adding it needs a new non-authority launch path); (3) link
sharing — whole task (no filter).

Review trail: v0 review (6 blocking) → v1 → one targeted repair verification (wf_3ef486c2-c4b): B1, B2, B4, A1, A4, A5
closed; B3, B5, B6, A2, A3 partly closed and one new defect (MCP launch vs jobs.py CLI-only rule) — all addressed in
this revision (v1.1) by the edits in §1, §2.1, §2.2, §2.4, §3.2, §5. No further review run.

v1.2 (after owner approval): pre-build probe (evidence/two-modes-probe) fixed the environment list and added
`--skip-git-repo-check`; restored a post-only MCP tool (`consult_post`) because MCP-attached callers cannot supply a
`--cap-file`; Antigravity link member over ACP with per-session MCP (no host-config edit); bounded headless-Codex smoke.
