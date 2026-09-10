# Mycelium coordination for the driver

Substantive control between the supervisor (you) and the Claude executor runs over a **shared
Mycelium task** — a provider-neutral task/message/checkpoint record that replaces ad hoc pointers.
The cmux bridge stays the **short notification/control path** (submit/wait, queued-steering
acceptance); Mycelium carries the durable content: briefs, reviews, replies, and checkpoints. Read
this before routing substantive content. It never changes who owns repository writes.

The shared skill `mycelium-coordinate` (server `mycelium-coord`, or the `mycelium-coord` CLI) is the
protocol; this reference is only how the driver adopts it.

## Install and locate the coordination CLI + skill (per host)

The protocol ships inside the Mycelium plugin. Each host registers it from **its own manifest** — the
MCP command/env differ and must not be assumed identical (proven in `checks/native-plugin-proof-r1/`):

- **Claude executor.** Load the plugin natively — `claude --plugin-dir <candidate>` for a pinned
  checkout, or a normal installed plugin. The manifest `.claude-plugin/plugin.json` points at
  `./.mcp.claude.json` (a `${CLAUDE_PLUGIN_ROOT}`-expanded command). This connects the MCP server
  `plugin:mycelium:mycelium-coord` (the `coord_*` tools) and discovers the `mycelium:coordinate`
  skill. The SessionStart attach hook registers automatically — do NOT pass `--mcp-config`.
- **Codex supervisor.** Register through the marketplace, then enable: a `[marketplaces.<name>]
  source_type=local, source=<candidate>` entry, `codex plugin add mycelium@mycelium` (installs into
  `$CODEX_HOME/plugins/cache/<ns>/mycelium/<version>/`), and `[plugins."mycelium@mycelium"] enabled =
  true`. The manifest `.codex-plugin/plugin.json` points at `./.mcp.json` (bare-relative command +
  `cwd:"."` + `env_vars`) and connects the MCP server `mycelium-coord`. For an isolated trial use a
  disposable `CODEX_HOME`; never hot-switch a running session's loaded cache.

**Prefer the MCP `coord_*` tools** whenever the plugin is loaded — that is the most robust path and
needs no path resolution. Reach for the bare CLI only for shell/hook use.

**Locating the bare CLI (it is NOT on PATH).** The CLI lives at `<plugin-root>/coordination/bin/
mycelium-coord`, so `command -v mycelium-coord` returns absent on a host that only installed the
plugin (observed in Codex: absent at 05:01Z). A frozen pre-coordination plugin install has no
`coordination/bin` at all. Use the bundled locator — invoked by an **absolute path**, since a
cwd-relative `scripts/...` fails from an arbitrary working directory. The same self-locating locator
ships in two places: the plugin (both hosts) at `<plugin-root>/coordination/bin/
locate-mycelium-coord.sh` and this operator skill at `<skill-dir>/scripts/locate-mycelium-coord.sh`
(installed `~/.codex/skills/cmux-driver/scripts/…`):

```bash
LOC="${CLAUDE_PLUGIN_ROOT:-${MYCELIUM_PLUGIN_ROOT:-$HOME/.codex/skills/cmux-driver}}"
# plugin ships it under coordination/bin; the operator skill under scripts/ — try both by abs path:
for cand in "$LOC/coordination/bin/locate-mycelium-coord.sh" \
            "$HOME/.codex/skills/cmux-driver/scripts/locate-mycelium-coord.sh"; do
  [ -x "$cand" ] && { CLI="$("$cand")" && break; }
done
[ -n "${CLI:-}" ] && "$CLI" resume TASK PARTICIPANT   # or: "$cand" resume TASK PARTICIPANT (exec form)
```

The locator self-locates beside the launchers, then searches `$MYCELIUM_COORD_BIN`; PATH; the host's
plugin-root env (`CLAUDE_PLUGIN_ROOT` / `MYCELIUM_PLUGIN_ROOT` / `PLUGIN_ROOT`); the Codex cache
(`$CODEX_HOME/plugins/cache/*/mycelium/*`, newest version that actually carries `coordination/bin`);
`~/.claude/plugins`; then the dev checkouts. It skips installs that predate the coordination overlay
and exits non-zero with guidance if nothing usable exists — it never invents a launcher. Claude uses
the SAME locator through the shared `mycelium-coordinate` skill (see `coordination/skill/SKILL.md`).
If neither the MCP tools nor the CLI resolve, fall back to the bridge/brief path with truthful limits
(see **Authority and safe fallback**); do not fabricate a send/ack/checkpoint.

## Create or explicitly join the authorized task

1. The owner authorizes a task. Create it once (idempotent): `mycelium-coord create-task TASK
   --project P --worktree <executor worktree realpath> --revision R --authorization <brief path>`.
   `--authorization` is recorded, never a grant.
2. Attach BOTH sides with the **exact bound native identities** you resolved in operating rule 1 —
   not display labels:
   - Supervisor (you): `attach TASK <supervisor-id> --role supervisor --worktree <your own cwd,
     outside the executor repo> --host host=codex --host session=<controller session> --host
     native_id=<controller id>`. Attaching as supervisor grants no write or lifecycle-owner authority.
   - Executor: `attach TASK <executor-id> --role executor --worktree <Claude worktree realpath>
     --host host=claude --host session=<Claude session id> --host native_id=<pid-…>`. The executor's
     worktree must equal the task's.
   A genuine session replacement is a controlled transition (`--allow-transition`), which retires the
   old session's selection; never bind a foreign session to an existing participant.

## Route substantive content through Mycelium

- **Brief / amendment / review finding:** `send TASK --id <fresh id> --from <you> --to <executor>
  --kind task|amendment|review_finding --revision R --artifact-ref <brief/finding path>` (or
  `--text` for short content). Keep the requirement map and request id/hash exactly as in
  `references/checkpoint-packet.md`; the message id is the correlation handle.
- **Completion reply:** the executor replies `--kind completion_receipt --reply-to <request id>
  --revision R --artifact '{"file":"…","sha256":"…"}'`. The receipt's revision MUST equal the
  request's R; a differing revision does not complete it. `message_state` → `completed` only when the
  artifact verifies on disk — existence alone is not evidence.
- **Checkpoint:** publish the checkpoint packet as a versioned Mycelium checkpoint
  (`checkpoint-publish TASK --revision N --by <you> --checkpoint '{…}'`). A revision is write-once;
  the shared record keeps every prior body immutable and rederives a `latest` pointer on each
  publish. Do NOT copy the new revision's current-state narrative into other docs — point at the
  shared record instead (see **Stable current-state pointer** below).

## Stable current-state pointer (resolve current revision at use)

The shared task is the single source of current truth; the current revision is **resolved at use**,
never hand-copied into parallel "current" documents. This removes the recurring drift where a
compatibility narrative (docs/CURRENT, a session packet's `authoritative_current_pointer` /
`context.*.note`) kept pointing at an older checkpoint after the shared task advanced.

- **Resolve the current checkpoint** with `checkpoint-read TASK` (no `--revision` → the latest body)
  or, for a bound participant, `resume TASK PARTICIPANT` (current checkpoint + bounded unacked). The
  SessionStart attach hook already uses this path, so a native restore/compaction obtains the current
  `next_action` directly from the shared record.
- **Compatibility docs are pointers, not snapshots.** A durable doc names the **task id** and the
  exact resolve command as the authority; it does not restate the revision/next_action as current
  truth. Any embedded value is a clearly-labelled non-authoritative cache ("resolve for truth"), so
  publishing a new revision changes what the supported restore path shows **without editing those
  docs**. History stays immutable and readable; a specific `--revision N` still fetches an old body.
- **Unattached-session fallback preserved.** A session with no selection still reads the current
  state by task id via `checkpoint-read TASK`; nothing depends on a hand-synced local pointer.
- **Executor-owned decision linkage.** The link between durable repository decisions and the shared
  task is one-directional and stable: the doc points to the task; the checkpoint's `knowledge_refs`
  point to the immutable evidence. Preserve that linkage across adoption — do not duplicate a mutable
  current value on both ends where they can disagree.

## Bounded unread exposure at boundaries (not only startup)

Active hosts must see unread coordination messages at supported boundaries, without re-reading full
context on every tool call:

- At each phase boundary — before a new brief, before compaction, when waiting on the executor — run
  `inbox TASK PARTICIPANT --after <cursor>` or a bounded `wait TASK PARTICIPANT --timeout S`, and
  advance the cursor with `set-cursor`. Report deltas with truthful limits, not repeated dumps.
- On startup/compaction, the SessionStart attach hook surfaces the checkpoint + bounded unacked
  messages for the session that owns the selection (bound to its native session id). One
  `resume TASK PARTICIPANT` call is the same bounded restore by hand.
- **No fabricated wake-up.** A stored Mycelium message cannot wake an idle peer. To actually nudge an
  idle Claude, use `notify-via-bridge` (it reuses the cmux bridge and records the coordination
  message_id ↔ bridge request_id correlation); only a bridge **accepted** outcome is delivery — a
  busy refusal stays pending until the executor's next turn. This is the same acceptance evidence as
  `references/bridge-mcp.md`; delivery/acknowledged/completed stay distinct.

## Authority and safe fallback

The sole-executor rule and every ratified gate stand unchanged; Mycelium is talk + state. If the
coordination MCP/CLI is unavailable on a host, fall back to the bridge/brief path with truthful
limits — never fabricate a delivery, an acknowledgment, or a completion, and never hot-switch a
running session's loaded hooks or caches to force adoption. Adopt the shared task at the next clean
boundary instead.
