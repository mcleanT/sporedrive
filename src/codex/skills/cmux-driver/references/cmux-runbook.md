# cmux runbook — installed-version facts (verified 2026-09-08; contract 1.2.0)

Installed: cmux **0.64.17 (97) [9ed29d81a]**, bundle `com.cmuxterm.app`, CLI at
`/Applications/cmux.app/Contents/Resources/bin/cmux` (not on the default shell PATH outside cmux;
inside cmux terminals it is on PATH). Socket protocol `cmux-socket` v2 at
`~/.local/state/cmux/cmux-502.sock` (per-uid path; `~/.local/state/cmux/last-socket-path` names the
live one; the older `cmux.sock` there is stale and refuses connections). Everything below was run on
this host; commands not listed were not verified. The upstream `docs/cli-contract.md` on `main` is
ahead of this build (it documents `sessions`, `vault`, `comments`, `read-selection`, `local-tmux`
that this CLI does not have) — trust `cmux --help` of the installed binary over the web contract.

Set `CMUX_QUIET=1` to silence alias notices (`list-workspaces` is an alias of `workspace list`).

## 0. Open a fresh executor session with an explicit model (verified 2026-09-20)

**Fable is planning-only.** An implementation session is launched with an explicit `--model`
(Opus by default; Sonnet or Haiku when the brief names one) — never through the bare `clauded`
alias (`claude --dangerously-skip-permissions`, which inherits the `model` in
`~/.claude/settings.json`, currently `claude-fable-5-1[1m]`), never by binding whatever session is
focused or sits in the primary window, and never by reusing the owner's planning session. Window
placement and focus are display choices, not identity: identity is the workspace UUID, surface
UUID, Claude pid, session UUID and worktree realpath, exactly as §2 requires. Launch with the skill
script (stdlib Python; no bridge needed):

```bash
python3 ~/.codex/skills/cmux-driver/scripts/executor_session.py launch \
  --cwd '<ABSOLUTE_AUTHORIZED_WORKTREE>' --name '<TITLE>' \
  [--model opus|sonnet|haiku|<claude-* id>] [--effort low|medium|high|xhigh|max] \
  [--window <ref|uuid>] [--focus false] [--receipt <path>]
```

**Effort (owner rule, 2026-09-22).** `--effort` is the main control over how much an Opus session
thinks, and Opus 5.5 thinks more per level than Opus 5 did, so an implementation launch names it
rather than inheriting a session default: no `--effort` on an **Opus implementation** launch means
`--effort medium`. An explicit `--effort` always wins — `low` for mechanical work, `high` for hard
work or after demonstrated failures, `xhigh`/`max` only on an explicit request or measured benefit.
A Sonnet/Haiku launch, or a `--purpose planning` Fable launch, that names no effort passes no
`--effort` at all and keeps its own default. An unsupported level is refused before any cmux call.
The level is forwarded to the real `claude` CLI in the workspace command and recorded in the launch
receipt as the **requested** setting (`requested.effort`, `requested.effort_source`); the launcher
reads the footer for the model only, so it never claims the running session's effort was verified.
Changing effort on a live executor is the `effort` subcommand below (adaptive effort); the launcher
never types `/effort <level>` and never edits settings files.

**Adaptive effort on a live executor (owner rule, 2026-09-22).**

```bash
python3 ~/.codex/skills/cmux-driver/scripts/executor_session.py effort \
  --surface <surface-uuid> --pid <claude pid> --session-id <uuid> --cwd <session cwd> \
  --to low|medium|high --request-id <stable id> --authority policy|owner [--owner-ref <owner ref>] \
  [--pin|--release-pin] --reason "<evidence>" --checkpoint <checkpoint/phase id> --attest-drained \
  [--expected-model opus]
```

Native path (observed on v2.1.280): bare `/effort` + Enter opens a slider (`low medium high xhigh max
ultracode`, marker `▲`, hint `s for this session only`); `←/→` move one level; `s` commits for this
session only. Enter on the slider and `/effort <level>` save a persistent default — never used.
Gates before any key: exact pid ↔ `--session-id` argv, the process's own `CMUX_SURFACE_ID` equals
`--surface` and its cwd equals `--cwd` (unknown or different = refused), footer model passes the purpose policy,
empty input line (a draft is never touched), no dialog, Claude ≥ 2.1.257, target in low/medium/high,
current level readable and inside low/medium/high for `policy` authority, no retained owner pin for
`policy` authority, supervisor attestation that the session is drained at an authorized checkpoint
(recorded, not measured), one lock per surface held for the whole request (an empty/partial lock is
treated as held, never as stale), and a readable receipt chain (a torn/malformed chain is
`refused/history_damaged`, never reset or repaired). Then: marker must sit on the current level
before arrows and on the target after them (otherwise Esc, `refused/state_mismatch`); `s`; footer
`Thinking: <target>` and a clean prompt. Saved-settings hashes are compared before/after and a change
is reported (`settings_changed`), never repaired.

A durable `pending` record is written before the first key, so an interrupted switch replays as a
read-only reconcile (`unknown`) and is never resent. Outcomes (receipt chain
`$CMUX_BRIDGE_STATE_DIR/launches/effort/<session>.jsonl`): `no_op`,
`applied_ui` (UI changed; runtime pending), `applied` (the FIRST assistant transcript record after the
commit has `effort` AND `perTurnEffort` = target), `runtime_mismatch`, `refused/<code>`, `unknown`.
Re-running the SAME `--request-id` with the same content never types again: it re-checks identity and
reconciles `applied_ui` / `unknown` / `pending` read-only from screen + transcript. A same id with different content is refused. Exit
codes: 0 applied/applied_ui/no_op, 3 refused, 5 unknown, 6 runtime_mismatch. The helper spends no
model call: confirm runtime by re-running the same request after the executor's next real reply.

What it does, in order: resolves the policy (`--model fable`/`claude-fable-*` is refused with
exit 3 *before* any cmux call; no `--model` means `opus`); pre-generates the session UUID so the
exact native identity is known before the process exists; runs `new-workspace --cwd … --command
'<absolute claude> --dangerously-skip-permissions --model <m> --session-id <uuid> [--effort <e>]' --focus false
--env CLAUDE_CODE_SUBAGENT_MODEL=sonnet --env SPOREDRIVE_SESSION_PURPOSE=implementation …` (the
subagent variable keeps the executor's own workers off Fable too); resolves the new workspace and
surface UUIDs (`tree --all --json`, `list-pane-surfaces --id-format both --json`); waits, bounded,
for the Claude prompt and footer; and reports the REQUESTED model next to the RESOLVED footer model
(`Model: Opus 5 | …`), with `ok: true` only when the families match and a prompt is visible. It
writes a launch receipt (`~/.local/state/codex-claude-bridge/launches/<session>.json` by default)
carrying command, UUIDs, pid, argv and footer evidence. Exit 4 = launched but not verified (a
folder-trust dialog is showing — a human confirms it — or the footer disagrees with the request):
do not bind or send until `verify` passes. Nothing is ever typed into the session by the launcher.

Reuse an existing session for implementation only after the same policy verifies it:

```bash
python3 ~/.codex/skills/cmux-driver/scripts/executor_session.py verify \
  --surface <SURFACE_UUID> --pid <CLAUDE_PID> --cwd <WORKTREE> [--expected-model opus]
```

Exit 0 = usable for implementation. Exit 3 = refused: `model_policy` (footer shows Fable, i.e. a
bare launch inherited the settings default), `planning_session` (the process argv or the
transcript shows Fable — the owner's planning session, even after a `/model opus` switch),
`model_mismatch` (not the model you asked for). Exit 4 = `model_unverified` (no readable footer:
shell, dialog, editor). The bridge's `bridge_bind(purpose="implementation")` and every
`bridge_submit` apply the same rule once the installed bridge carries it (`bridge-mcp.md`).

Facts (this host, 2026-09-20): `claude --dangerously-skip-permissions --model opus --session-id
<uuid>` started from a login zsh shows `Model: Opus 5` in the footer, records
`message.model: "claude-opus-5"` on every assistant transcript entry, and `ps -o command=` on the
pid shows the explicit `--model opus` — the three independent readings the verifier uses.
`--session-id` fixes the transcript path (`~/.claude/projects/<slug>/<uuid>.jsonl`) before the
first prompt. `new-workspace` may return only `OK workspace:N`, even with `--json`; UUIDs must be
resolved before anything else (refs are reassigned). A `--focus false` workspace is readable and
writable by UUID without touching the owner's focused surface. An owned, trusted checkout may show
a folder-trust dialog first; the launcher reports it as `modal`. Model and effort otherwise follow
Claude's configuration — which is exactly why the flag is mandatory. Opening a session does not
itself assign work or resume a paused coordination task.

Manual fallback (only if the script cannot run): the same command line with the absolute `claude`
path, `--model <m>` and `--session-id <uuid>` typed into a fresh terminal surface — never `clauded`,
never a bare `claude`, never `--window`/`current-window` as the way to pick a target.

## 1. Historical access-mode restriction (2026-09-08; superseded on this host)

The original `cmux capabilities` probe reported `"access_mode": "cmuxOnly"`; the newer successful
launch above reports `password`. Treat the following as historical diagnostics, not a current
blocker. Setting: `automation.socketControlMode`
in `~/.config/cmux/cmux.json` (Settings > Automation). Enum on this build:
`off, cmuxOnly (default), automation, password, allowAll, openAccess, fullOpenAccess, notifications, full`.
`automation.socketPassword` holds the password for `password` mode; CLI auth order is
`--password` > `CMUX_SOCKET_PASSWORD` > password saved in Settings. `CMUX_SOCKET_MODE` is an
environment override read by the **app** at launch (documented values `cmuxOnly|allowAll|off`).

Proof of the ancestry check (probe launched under launchd, parent PID 1, not a cmux descendant):

```
$ cmux ping                → Error: Failed to write to socket (Broken pipe, errno 32)
$ cmux list-workspaces     → same broken pipe
$ CMUX_SOCKET_PASSWORD=x cmux ping → same broken pipe (cmuxOnly ignores passwords)
raw unix socket: connect ok, send ok, recv:
  ERROR: Access denied — only processes started inside cmux can connect
```

So the two symptoms seen from Codex are two different layers: **EPERM** = the Codex sandbox denied
the unix-socket connect; **broken pipe** (escalated) = the connect succeeded and cmux closed the
connection because the Codex process is not descended from the cmux app. Neither is a CLI bug.

**Paths considered during the original restriction (password access now works on this host):**

1. Run the controller's shell *inside* a cmux terminal (a surface whose process tree descends from
   the app). This is what makes the CLI work for Claude Code sessions launched by cmux. Not
   available to the Codex desktop app's own shell.
2. Owner switches `automation.socketControlMode` to `password` in cmux Settings and provides the
   password to the controller through `CMUX_SOCKET_PASSWORD` (never on the command line, never in
   a repo). This is the route the accepted release's live host smoke drove end to end: in `password`
   mode a launchd-spawned (non-cmux-descended) process authenticates with the password instead of the
   `cmuxOnly` ancestry check, so the bridge reaches the surface. The launch in §0 verifies the same
   access from the Codex desktop shell with the existing password configuration; do not change
   access settings merely to launch a session. Under `cmuxOnly` the ancestry blocker above still
   applies to any non-cmux process.
3. `automation` mode — semantics **unknown** (not documented on the web docs page); test before use.
4. `allowAll` / `openAccess` / `fullOpenAccess` — any local process; **do not use** on a shared
   machine and not what the owner asked for.

TCP is not involved anywhere (the `CMUX_PORT` variables are per-workspace port reservations for
user apps); do not add a TCP listener.

## 2. Identity (verified)

```
cmux identify --json                   # caller (this terminal) + focused surface; both carry UUIDs
cmux list-workspaces --id-format both  # "workspace:3 <UUID>  Science"
cmux tree --all                        # windows > workspaces > panes > surfaces, titles, ttys
cmux list-pane-surfaces --workspace <UUID> --id-format both
cmux surface-health --workspace <UUID> # in_window=false surfaces cannot be read
```

Facts:
- `identify` distinguishes **caller** (the surface whose process runs the command) from
  **focused** (what the user is looking at). Reads and sends target a surface explicitly; they do
  not depend on focus. Verified: reading and writing an unfocused surface in an unselected
  workspace left the focused surface unchanged.
- `surface:N` / `workspace:N` refs are display labels: after creating and closing one surface,
  `close-surface` echoed a different ref number than `new-surface` had returned for the same UUID.
  Bind by **UUID** only.
- `--surface <unknown UUID>` → `not_found: Workspace not found` (exit 1); `--surface surface:999` →
  `not_found: Surface not found for the given surface_id` (exit 1); bogus workspace UUID →
  `not_found` (exit 1). **But** `--workspace workspace:99` with no `--surface` silently fell back to
  the caller's own surface (exit 0). Always pass `--surface <UUID>`; never rely on `--workspace` alone.
- `internal_error: Failed to read terminal text` (seen on a hibernated workspace's surface) means
  UNKNOWN state, not empty output. `surface-health` reports `in_window=false` for every surface of
  a workspace that is not the focused one, including readable ones, so that flag alone does not
  predict readability; the read result does.
- `new-surface --json` returns refs only (`surface:N`); resolve the UUID immediately from
  `list-pane-surfaces --workspace <UUID> --id-format both` before doing anything else with it.
- `tree` tty numbers repeat across surfaces; they are not identities.
- Claude session identity comes from the event stream: `agent.hook.*` payloads carry
  `_opencode_request_id: claude-<claude-session-uuid>-<Event>-<ms>` and `_ppid: <claude pid>`;
  `sidebar.metadata.updated` carries `claude_code Running --tab=<workspace uuid>
  --panel=<surface uuid> --pid=<claude pid>`. Bind {workspace UUID, surface UUID, claude pid,
  claude session uuid, worktree realpath} together; a mismatch means re-bind.

## 3. Send and receipt (verified on a disposable terminal surface only)

```
cmux new-surface --type terminal --workspace <UUID> --focus false --json   # disposable probe target
cmux send --surface <UUID> "<literal text>"     # OK surface:N workspace:N ; text arrives literally, NOT submitted
cmux send-key --surface <UUID> Enter            # submits once
cmux read-screen --surface <UUID> --lines 6     # bounded read of the last N rows (default: visible screen)
cmux read-screen --surface <UUID> --scrollback --lines 200   # include scrollback, still bounded
cmux close-surface --surface <UUID> --workspace <UUID>       # only for surfaces you created
```

Facts:
- `send` delivered `$HOME`, backticks, single/double quotes, and Unicode literally, without
  submitting. A **trailing or embedded `\n` inside the sent text submits** (the shell executed the
  line). For a multi-line brief to Claude's prompt this is not yet proven safe: Claude Code
  distinguishes pasted multi-line text from typed newlines, and `send` was only tested against a
  shell. Until a disposable *Claude* session proves the behavior, deliver multi-line briefs as a
  file path plus a one-line instruction, or as a single line, and submit with `send-key Enter`.
- `send` returns `OK` on transport success only — that is not delivery. Text visible after `❯` in
  `read-screen` is **staged**, not accepted, and staging is not delivery either. Acceptance means
  exact payload correlation: the session's transcript (`~/.claude/projects/<slug>/<session>.jsonl`)
  contains a user message whose content hash equals the hash of the text you sent, with a
  timestamp after the send. An `agent.hook.UserPromptSubmit` event for that session ID and PID
  after the submit is corroboration only and never sufficient by itself — the event carries only
  `tool_input_length`/`context_length` (the prompt text itself is redacted), so a different manual
  prompt typed into that same session produces an indistinguishable event.
- Raw bytes pass through `send` unchanged, including bracketed-paste markers (`\x1b[200~ … \x1b[201~`),
  and a real LF byte submits in a ready shell. Sending right after `new-surface` races shell
  startup (the paste landed before zsh initialised); read the state before every send.
- Focus did not change; the user's active surface was untouched throughout.
- **Never send to a surface you have not bound and read first.** Verify the prompt is Claude's
  (`❯` with the status footer) and not a shell, a permission dialog, an editor, or a selector.
- There is no Claude-specific "submit prompt" method on this build besides typed input;
  `workspace.prompt_submit` exists in the method list but was not exercised.

## 4. Observe and wait (verified)

```
cmux events --after <seq> --limit <n> --no-heartbeat [--no-ack]  # replay retained events after seq (NDJSON)
cmux events --cursor-file <path> --reconnect                     # follow with a persisted cursor
cmux events --category agent --name agent.hook.UserPromptSubmit   # filters are repeatable
```

Facts:
- The ack frame carries `boot_id`, `latest_seq`, `next_seq`, `oldest_seq`, `gap`, and a
  `heartbeat_interval_seconds` (15). Use `boot_id` to detect an app restart; a changed `boot_id`
  or `gap: true` means re-bind and reconcile from artifacts.
- Claude hook events relayed on this build (280-frame window, 2026-09-08): `agent.hook.PreToolUse`,
  `agent.hook.Stop`, `agent.hook.UserPromptSubmit`, `agent.hook.SessionStart` (also after
  `/compact`, same session ID and PID), `agent.hook.SessionEnd`, `agent.hook.SubagentStop`,
  `agent.hook.Notification`. **`PreCompact` is not relayed.** Every payload carries
  `session_id: claude-<uuid>`, `_ppid: <claude pid>`, `cwd`, `workspace_id`, `occurred_at`;
  `surface_id` is null, and `tool_input`/`context` are redacted (lengths only). The surface ↔ PID
  link comes from `sidebar.metadata.updated` (`claude_code Running … --tab=<workspace uuid>
  --panel=<surface uuid> --pid=<pid>`). Other names: `feed.item.received/completed`,
  `workspace.prompt.submitted` (cmux's own prompt box; preview only), `surface.*`, `notification.*`.
- Replay from `--after 0` is slow (~25 frames/s); use `--after <recent seq> --limit n` under a
  subprocess timeout.
- Waiting: poll `events --after <cursor> --limit ...` with a timeout, or `read-screen --lines 5`
  for the status footer (`Ctx Used: NN%` and `Session:` appear in the Claude Code footer on this
  host), and reconcile against files on disk. The footer percentage is Claude's own accounting;
  do not compare it with Codex's meter.

## 5. Unknown-state and reconnect behavior (verified)

- `--socket /nonexistent` → `Socket not found at ...` (exit 1).
- Stale socket file → `Failed to connect ... (Connection refused, errno 61)` (exit 1).
- Access denied → `Broken pipe, errno 32` from the CLI; the raw response is the sentence in §1.
- App restart changes `boot_id` and resets event sequences; `events --after` from an old cursor
  reports `gap`.
- Any of these means state is UNKNOWN: stop dependent mutation, re-bind, reconcile from artifacts.

## 6. Not verified here (do before relying on it)

- `automation` socket mode; password access from Codex is now verified in §0.
- Behavior with the Mac locked, on network disconnect, or across a cmux update.
- Permission dialogs inside Claude's prompt (the bridge classifies them as `modal` and refuses
  input; the dialog itself was not exercised).
- The disabled third-party `using-cmux` plugin (Japanese-language, broader, older CLI surface); do
  not enable it as a substitute for this runbook.
