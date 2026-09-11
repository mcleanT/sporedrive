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

## 0. Open a fresh Claude session with clauded (verified)

The owner uses `clauded` to launch new Claude sessions. It is a zsh alias in `~/.zshrc` for
`claude --dangerously-skip-permissions`, not a standalone executable. Invoke it through an
interactive zsh so the alias loads. Direct cmux CLI access from Codex succeeded with the existing
`password` access mode; Computer Use and changes to socket permissions were unnecessary.

When a new session is requested, choose the authorized project directory and use:

```bash
/Applications/cmux.app/Contents/Resources/bin/cmux current-window --json
/Applications/cmux.app/Contents/Resources/bin/cmux new-workspace \
  --window <WINDOW_UUID> --name '<TITLE>' --cwd '<ABSOLUTE_PROJECT_PATH>' \
  --command '/bin/zsh -lic clauded' --focus true --json
```

Creation may return only `OK workspace:N`, even with `--json`. Resolve the returned ref using
`identify --workspace <REF> --id-format both --json` and
`list-pane-surfaces --workspace <WORKSPACE_UUID> --id-format both --json`. Keep UUIDs for subsequent
operations; JSON output without `--id-format both` may omit them. Read the exact new surface with
`read-screen --workspace <WORKSPACE_UUID> --surface <SURFACE_UUID> --lines 18` to verify startup.
An owned, trusted checkout may show a folder-trust dialog first. A Claude prompt and status footer
establish readiness; the workspace creation acknowledgment alone does not.

Verified end to end in the SporeDrive release checkout: alias launch, folder-trust confirmation,
then an idle Claude prompt with bypass permissions enabled. Model and effort follow Claude's
configuration. Opening a session does not itself assign work or resume a paused coordination task.

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
   a repo). The later launch in §0 verifies access from the Codex desktop shell with the existing
   password configuration; do not change access settings merely to launch a session.
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
