# SporeDrive

**SporeDrive is a portable, supervised two-agent coding workflow.** It pairs an OpenAI **Codex**
session acting as *supervisor* with an Anthropic **Claude Code** session acting as *executor*: the
supervisor relays the owner's brief, reviews, and observes progress, while Claude Code is the single
agent that actually writes to the repository. The two never share a process -- they coordinate through
**Mycelium**, a local shared-state protocol (addressed messages, versioned checkpoints, and an
enforced execution policy), and the supervisor reaches the exact live executor session through a small
**cmux MCP bridge** that delivers a notification only to the one bound Claude session.

This repository is the installable release of that workflow: the instruction files, the
`codex-review` skill, the `codex_ask` wrapper, the Codex-side `cmux-driver` skill, the shared
`mycelium_coord` coordination package, the `cmux_bridge` MCP server, owned install/verify/rollback and
native-export tooling, and the Mycelium lifecycle source it integrates with. Cloning the repo installs
nothing on its own -- nothing here is wired into any running Claude Code or Codex session until you run
the installer (`scripts/wfctl.py install`) and export/activate the plugin against your own machine
(see [Install / change / rollback](#install--change--rollback-procedure) and
[Native install & setup](#native-install--setup-portable)).

## Who it is for, and what you get

SporeDrive is for someone running both Codex and Claude Code on a Mac who wants **one agent to
supervise another with real, auditable limits** rather than two chat windows and copy-paste. You get:

- **A clear division of authority.** Codex supervises and reviews; Claude Code is the sole writer of
  the worktree. A supervisor's brief is the task of record, but it never edits your repository itself.
- **Durable coordination that survives compaction.** Every task, message, checkpoint, and execution
  allowance is persisted to a local state directory, so a session that compacts or restarts resumes
  from the shared record instead of losing the thread.
- **Bounded execution.** A managed task carries a spendable budget -- how many work dispatches and
  reviewer launches it may make, a reviewer time limit, an optional expiry -- enforced atomically in
  code, not left to an agent's good intentions (see [Bounded execution](#bounded-execution)).
- **Exact-session delivery.** The bridge notifies the specific bound Claude session (matched by
  workspace, worktree, session id, and controller), so a message cannot be typed into the wrong
  window, and a delivery is refused outright if the task is paused or closed.
- **Owned, reversible installation.** `wfctl.py` snapshots what it replaces and can roll back byte for
  byte; the exporter builds a self-verifying native Mycelium plugin from bundled source and never
  touches the canonical source tree.

## How the pieces fit

- **Roles -- Codex supervisor, Claude executor.** The supervisor relays the owner's brief, runs scoped
  read-only reviews (`codex-review` / `codex_ask`), and watches progress; it holds no repository-write
  authority. The executor (the Claude Code session) implements, tests, and is the only writer. This
  mapping is carried in the coordination host identities (`host=codex` supervisor, `host=claude`
  executor).
- **Mycelium -- shared coordination state.** `coordination/mycelium_coord/` is a provider-neutral
  package exposing the same operations over a bundled CLI (`coordination/bin/mycelium-coord`) and, once
  the plugin is loaded, `coord_*` MCP tools on both hosts. Sessions **attach** to an authorized task,
  **send** addressed messages of ten fixed kinds, **ack** receipt, and publish/read versioned
  **checkpoints**. Message states are distinct and meaningful (persisted -> delivered -> acknowledged
  -> completion_claimed -> completed), and a completion is only *verified* when its artifact evidence
  (a file plus sha256) actually checks out -- a claim of "done" is not the same as done.
- **Execution policy -- the enforced part.** On top of plain messaging, a *managed* task opens an
  execution record that governs what work may be dispatched. Actions must **reserve** from a shared
  allowance and **claim** that reservation against one concrete dispatch identity before any work goes
  out; paused, expired, closed, or already-spent reservations are refused (see
  [Bounded execution](#bounded-execution)).
- **The cmux bridge -- exact-session delivery.** `bridge/cmux_bridge/` is a minimal
  `codex-claude-bridge` MCP server over the cmux CLI. The supervisor binds to a specific Claude session
  (verified by workspace, worktree realpath, session id, and controller id) and submits a
  notification; immediately before pressing Enter in that session the bridge re-checks the managed
  execution gate, so a delivery into a paused or closed task is withheld rather than typed. Its
  operator-side contract is `src/codex/skills/cmux-driver/references/bridge-mcp.md`.
- **Owned tooling -- install, verify, roll back, export.** `scripts/wfctl.py` installs the instruction
  files/skills/wrappers with a pre-install snapshot and a byte-exact rollback;
  `scripts/export_coordination.py` turns the bundled `mycelium-source/` plus this repo's
  `coordination/` and `bridge/` into one owned, self-verifying native Mycelium plugin candidate. Both
  are covered in detail under [Native install & setup](#native-install--setup-portable).

## Efficient supervision

The default is fewer model requests and less carried context: wait for meaningful events, restore
one small working-state packet, fetch only decisive evidence, stop at the required outcome, and use
low reasoning effort for routine operations where the host supports it. Detailed procedures are
loaded on demand. Existing reviews and scientific checks retain their explicit quality requirements.

The Mycelium CLI/MCP now defaults to compact `resume`, `inbox` and `wait` views. `resume` combines
current execution/stop state, checkpoint identity, participant and pending message summaries; it
replaces separate status/checkpoint/inbox reads. Fetch a selected full brief with
`mycelium-coord read-message TASK PARTICIPANT MESSAGE_ID` or `coord_read_message`. Use `--full`
(`compact=false` in MCP) when complete records are required. These views do not acknowledge messages,
change message hashes, advance cursors or prove delivery. Existing Python callers retain full views
unless they opt into `compact=True`.

Waits return immediately on pause, draining, exhaustion, closure, completion or expiry with
`stop_waiting=true`. An ordinary timeout is marked `unchanged=true`; it is not a reason for repeated
model-driven status narration. Startup context gives current execution state precedence over stale
checkpoint work. Waiting inside a tool is still a finite local poll, not a native wake-up or a hard
limit on desktop reasoning. Respect the host's timeout limits; no new background scheduler is added.

`codex_ask` keeps only three commit subjects in its automatic context and omits historical session
narrative unless `CODEX_ASK_INCLUDE_LASTSESSION=1` is explicitly set. Full logs remain on disk. These
changes reduce supplied context and unnecessary reads; they do not automatically shrink an already
running conversation, change its model effort, or establish a measured percentage saving.

**Efficiency v2 (scheduling, owned local jobs, compact evidence).** The coordination package now ships
three small owned helpers, exposed as CLI subcommands and read-only MCP tools: `sched-plan` /
`sched-verify` (a deterministic Eastern-time planner that emits the intended UTC instant plus one-shot
submission data, and a read-only verifier that compares the *persisted* `next_run_at`/status with the
intent — `match`, `mismatch` or `cannot_evaluate`; it never writes the scheduler's store), `job-run` /
`job-join` / `job-status` / `job-list` / `job-output` / `job-cancel` (local work launched by the CLI from
the host-authorized shell under one managed reservation, with an immutable request record, complete
output on disk, and an in-tool join of at most 50 s that stops on the task deadline or a paused/closed
execution — no MCP launch route), and a combined 4 KB batch budget for owned outputs with truthful
`truncated` flags and offset/cursor retrieval of the full evidence. See `coordination/skill/SKILL.md`.

## Bounded execution

A supervised workflow is only meaningful if "supervised" is something the code enforces. A managed
task's execution record (opened with `exec-open`) carries a spendable budget and a lifecycle, seeded
from the maintained defaults in `coordination/mycelium_coord/execution_policy.json`:

- **Persistent allowances and reservations.** Every work-producing action must first *reserve* from a
  shared allowance, and a reservation is **bound to one concrete dispatch identity** the first time it
  funds a real dispatch -- the same reservation can never fund a second, different dispatch, and a
  reviewer launch can never be spent as a work dispatch. Allowances and reservations live in the
  persisted record, so they hold across compaction and across both hosts.
- **Defaults: 4 work dispatches, 2 review launches, a 900-second reviewer deadline.** An ordinary task
  may make four work-producing dispatches (initial brief, one review, one repair brief, one repair
  verification) and two reviewer launches; each owned read-only reviewer process carries a hard 900 s
  wall-clock limit. A brief may only **lower** a limit -- raising one, extending it, or unpausing
  requires a user-authorization-linked change path, never something a supervisor composes for itself.
- **Acceptance closure, evidenced repairs, and a backlog.** When the required criteria have accepted
  evidence the task moves to *closure* automatically (no separate owner-approval step); after closure only an **evidenced repair** -- a reservation linked to a still-open,
  evidence-backed acceptance blocker -- may dispatch, and optional non-blocking improvements go to a
  backlog rather than reopening the task.
- **Pause and expiry gates.** A task can be paused (it enters a *draining* state) or given an expiry;
  while paused, expired, or closed, claims and bridge deliveries are refused with a distinct reason
  (`execution_paused` / `execution_closed`), not silently dropped.
- **Compaction preserves state.** Because all of this is on disk, a session that compacts or restarts
  reattaches and reads the same execution record, allowances, and reservations -- the boundary is a
  scheduled checkpoint, not a lost task.
- **Technical readiness vs. frozen acceptance.** The workflow separates "the code runs and the tests
  pass" (technical readiness, which the executor may continue on its own) from the owner's **frozen
  acceptance criteria** (what makes the task *done*), which the executor does not renegotiate. A smoke
  or repair dispatch verifies execution, not that a scientific result is correct.

## A task, end to end

A typical run: the owner gives the Codex supervisor a brief. The supervisor **creates a managed task**
and opens its execution record, then **sends** the brief as an addressed, actionable message to the
Claude executor over Mycelium. The executor **acks**, does the work as the sole writer, runs tests,
and **publishes a checkpoint** plus progress messages the supervisor reads back. If a review is
warranted the supervisor spends a **review launch** on a scoped, read-only `codex_ask` review (bounded
by the 900 s reviewer deadline); the executor repairs against the named findings. Once the task's
already-frozen required criteria have accepted evidence, it moves to **closure** automatically -- there
is no separate owner-approval gate -- and the executor sends a **completion_receipt** carrying artifact
evidence (a file path plus sha256) that Mycelium verifies. Completion *is* that verified receipt, not a
new human sign-off. Throughout, the supervisor reaches the executor's exact live session through the
cmux bridge, and every dispatch is drawn from -- and checked against -- the task's persisted allowance.
A runnable, command-by-command example of the underlying **unmanaged** message exchange (attach / send /
ack / checkpoint) is in
[Minimal cross-session exchange example](#4-minimal-cross-session-exchange-example) below -- that
example shows plain coordination only; a *managed* task additionally opens an execution record
(`exec-open`) and reserves/claims each actionable dispatch, which the example does not.

## What SporeDrive does and does not do

- **Enforcement applies to *managed* tasks.** The allowance/reservation/gate machinery governs a task
  that has an execution record. Plain coordination messaging still works without one, and whether a
  managed message is *actionable* (and so subject to reserve/claim gating) is a **trusted-agent
  disposition** the sending agent supplies -- a boolean it sets honestly, not something parsed out of
  free text. Unmanaged and legacy messages hash and behave exactly as before and are not gated; the
  system fails closed on a corrupt managed record rather than treating it as legacy.
- **No hard desktop token or reasoning cap.** SporeDrive bounds *coordination* -- dispatches, reviews,
  reviewer wall-clock, and task lifecycle. It does **not** impose a hard token or reasoning-effort
  ceiling on the desktop Claude/Codex sessions themselves; per-model token and effort preferences are
  policy and guidance, not a runtime mechanism this repo enforces. The one hard time limit it does
  enforce is the reviewer deadline on an owned reviewer process.
- **Shared storage is local, not a cross-machine bus.** The coordination store is a directory on one
  machine's filesystem (`--root` / `MYCELIUM_COORD_DIR`). Two sessions coordinate by sharing that local
  path; it is not a network service or an arbitrary cross-machine message bus.
- **macOS + cmux, and activation is for fresh sessions.** The live path targets macOS with the cmux app
  (either `cmuxOnly` ancestry or password socket-control mode) and the live `codex`/`claude` CLIs.
  Installing or re-exporting activates the runtime for **fresh** Claude/Codex sessions; a session that
  was already running keeps the modules it loaded until it restarts.
- **It coordinates; it does not reason for the models.** SporeDrive manages the *lifecycle* -- who may
  write, what may be dispatched, when a task is done -- and the durable record around it. The quality
  of the actual code and analysis is still the models' own reasoning; this layer makes that work
  bounded, addressable, and auditable, not smarter.

## Layout

```
LICENSE                       MIT license (this bundle's codex-claude-workflow-derived parts)
PROVENANCE.md                 what was included/excluded and why; Mycelium source commit pin
VERSION                       contract version string
src/claude/CLAUDE.md          -> $HOME/.claude/CLAUDE.md
src/claude/skills/codex-review/SKILL.md -> $HOME/.claude/skills/codex-review/SKILL.md
src/claude/tools/codex_ask.sh           -> $HOME/.claude/tools/codex_ask.sh  ($HOME/bin/codex_ask can symlink to it)
src/claude/settings.fields.json         merge into $HOME/.claude/settings.json (skillOverrides only)
src/codex/AGENTS.md                     -> $HOME/.codex/AGENTS.md
src/codex/skills/cmux-driver/           -> $HOME/.codex/skills/cmux-driver/
scripts/wfctl.py               snapshot | install | verify | rollback | list
scripts/export_coordination.py export the coordination package for installation
scripts/smoke_check.sh         fresh-session instruction smoke checks (claude -p, codex exec)
bridge/                        cmux_bridge MCP server (Codex <-> cmux bridge) + offline test suite
coordination/                  mycelium_coord package: CLI, hooks, MCP server, skill instructions
tests/, bridge/tests/          offline unit tests (no network, no live cmux/codex/claude calls)
mycelium-source/                reproducible Mycelium lifecycle plugin source (see PROVENANCE.md)
```

## Identity: what would be installed, and from where

Once installed against a real `$HOME`, you can check drift and provenance with:

```
python3 scripts/wfctl.py verify      # OK/DRIFT per target; exit 1 on drift
cat installed/manifest.json          # repo commit + src/dest sha256 at install time (created on install)
head -3 $HOME/.claude/CLAUDE.md      # each installed file names its maintained source and contract version
```

## Install / change / rollback procedure

1. Set `$REPO` to wherever you place this bundle, e.g. `export REPO=/path/to/sporedrive-release-stage`.
2. Edit files under `$REPO/src/` if you need to customize them. Never hand-edit the installed
   copies under `$HOME/.claude` or `$HOME/.codex` directly (a later `verify` will report DRIFT).
3. `cd "$REPO" && python3 scripts/wfctl.py install` -- takes a `pre-install-<UTC>` snapshot of
   whatever is currently at the destination paths, copies `src/` into place atomically, merges the
   settings fields, and writes `installed/manifest.json`.
4. `python3 scripts/wfctl.py verify`.
5. Commit your own fork/copy of this bundle if you're tracking it under version control.

Rollback: `python3 scripts/wfctl.py list`, then
`python3 scripts/wfctl.py rollback <label> --dry-run` and, once the plan reads right,
`python3 scripts/wfctl.py rollback <label>`. Files present in the snapshot are restored byte for
byte; targets that were absent when the snapshot was taken (for example `$HOME/.codex/skills/cmux-driver`)
are removed; the tracked settings fields (`skillOverrides`, `enabledPlugins`, `modelSettings`,
`effortLevel`, `model`) are restored to their snapshot values without touching other fields.

## What is deliberately never copied by the installer

`$HOME/.codex/auth.json`, `$HOME/.claude/.credentials*`, `.env` files, messaging tokens, and
`$HOME/.codex/config.toml` beyond its top-level model fields (extracted as text, never the file).
`wfctl.py snapshot` scrubs key-like fields and aborts if a token-shaped string appears anywhere in
the snapshot.

## Bridge (cmux <-> Codex <-> Claude)

`bridge/cmux_bridge/` is a minimal `codex-claude-bridge` MCP server (a small set of bounded tools
over the cmux CLI); the operator-side contract for using it lives in
`src/codex/skills/cmux-driver/references/bridge-mcp.md`. Offline simulated test suite:
`cd bridge && python3 -m pytest tests -q`. A **live** acceptance run (`bridge/run_live.sh`,
`bridge/live_acceptance.py`) requires a real cmux session, a disposable working directory, network
access, and the live `codex`/`claude` CLIs -- it is **not** exercised as part of preparing or
inspecting this bundle, and should only be run deliberately, by a human, in an environment set up
for it.

## Mycelium lifecycle source

`mycelium-source/` bundles the Mycelium lifecycle plugin source this workflow integrates with, at a
pinned commit. See `PROVENANCE.md` for the exact commit hash, what was excluded from the copy
(`.git/`, caches), and licensing notes for that subtree.

**Build identity is more than one thing.** This bundle's `mycelium-source/` commit pin is the
*release bundle's* source input; it is not automatically the same commit as (a) an already-installed
plugin candidate's frozen manifest, or (b) an acceptance harness that exercised it. Do not assume this
release is byte-identical to whatever full-source tree is already installed on a given machine, and do
not reinstall or re-export an existing candidate solely because this bundle's docs or other
non-runtime files changed -- a release can instead be rebuilt from its own final bundled inputs and
record its own manifest identity (`EXPORT_MANIFEST.json` / the `+<build-id>` suffix in `plugin.json`),
distinct from both of the above. The concrete commit, manifest, and cache identities for the current
accepted release are recorded in `PROVENANCE.md` (committed) and, for the local validation run, in the
accepted release receipt `checks/RELEASE-RECEIPT-20260910.json` -- a local artifact from the `664de18`
release run, not committed to the repository and absent from a GitHub clone.

## Native install & setup (portable)

A from-scratch checklist for getting this bundle wired into a real `$HOME` on a new machine, beyond
the generic `wfctl.py install` steps above.

### 0. Initialize the target project, and the per-host hook distinction

Installing the plugin (step 2 below) and initializing a *project* to use it are two different
actions, and Claude and Codex differ in what initialization does for hooks (from the bundled source,
`mycelium-source/README.md`'s "Initialize your project" section and its hooks table):

- **Claude Code:** hooks are registered **per repository** by `init_repo.py`
  (`mycelium-source/skills/core/scripts/init_repo.py`), not globally by installing the plugin. Open
  Claude Code in the target project and say "Set up mycelium" (or "Initialize living repo"); this
  scaffolds `.living/`, `MYCELIUM.md`, and the project's `CLAUDE.md`/`AGENTS.md` adapters, and is what
  actually wires the lifecycle hooks into *that* repository. Installing the plugin alone does not
  register Claude hooks in a project you haven't initialized this way.
- **Codex:** hooks are bundled once with the plugin in `hooks/hooks.json` and dispatched via
  `PLUGIN_ROOT` -- there is no per-repository hook-registration step. Trust them once via `/hooks` (all
  six in this integrated export, five in upstream Mycelium -- see the health check's hooks item
  below). They are then available in every Codex session where the plugin is enabled, but the Mycelium
  dispatcher **no-ops outside a Mycelium (initialized) repository** (bundled source
  `mycelium-source/README.md`), so the target project must still be initialized as a living project for
  the hooks to act there.

**Both hosts:** after installing the plugin (step 2), initialize the target repository as a living
project before relying on it -- Claude via "Set up mycelium" (`init_repo.py`, which also registers the
per-repo lifecycle hooks), Codex by enabling the plugin in that repository so the dispatcher sees a
Mycelium repo. Installing the plugin alone does not turn an arbitrary repository into a coordinating
living project on either host.

Plugin-level facts (the `mycelium-coord` MCP server registered and answering; Codex hooks trusted)
and the project-level fact (Claude lifecycle hooks registered in *this specific* repository via
`init_repo.py`) are independent and must each be checked on their own: a session where the
coordination MCP answers fine can still be a Claude repository that was never initialized, and so has
no lifecycle hooks firing there, and vice versa.

### 1. Install the bundled Mycelium source via the exporter

`mycelium-source/` bundles a full copy of the Mycelium lifecycle plugin source (see "Mycelium
lifecycle source" above). `scripts/export_coordination.py` turns that copy plus this bundle's
`coordination/` and `bridge/` packages into one owned, self-verifying native plugin candidate
directory -- it copies the source tree, overlays `coordination/` and `bridge/`, merges the
coordination MCP registration into both plugin manifests, merges a SessionStart coordination-attach
hook into the existing hooks, and writes a cross-verified `EXPORT_MANIFEST.json`:

```bash
python3 scripts/export_coordination.py \
  --source mycelium-source \
  --target <native-mycelium-plugin-dir> \
  --build-id <semver>
```

Flags: `--source` (defaults to `~/tools/mycelium-lifecycle-wfi`; **always pass
`--source mycelium-source` explicitly** to use the copy bundled in this release -- see the warning
below), `--target` (the owned candidate directory to create/refresh -- refused if it aliases the
source/canonical repo, or if non-empty without this exporter's ownership marker, unless `--force`),
`--build-id` (a dot-separated semver-style build identifier appended to the plugin version),
`--force` (replace a non-empty, unmarked target), `--keep-stage` (keep the intermediate staging dir
on validation failure, for debugging). Re-run with `--verify-only` to re-check an existing target
against its manifest, **passing the same `--source mycelium-source`**:

```bash
python3 scripts/export_coordination.py --source mycelium-source --target <native-mycelium-plugin-dir> --verify-only
```

**Wrong-source verification bug, confirmed by actually running both forms against the same freshly
exported target in an isolated scratch dir:** omitting `--source` on `--verify-only` silently falls
back to the default `~/tools/mycelium-lifecycle-wfi` instead of erroring, and if that default path
exists with different content on your machine the check reports false failures against the *wrong*
source tree:

```
$ python3 scripts/export_coordination.py --source mycelium-source --target "$T" --verify-only
{"verified": true, "problems": []}                                    # exit 0 -- correct

$ python3 scripts/export_coordination.py --target "$T" --verify-only  # --source omitted
{"verified": false, "problems": ["retained != source: skills/core/hooks/mycelium-activity-tracker.sh", ...]}
                                                                        # exit 1 -- false negative
```

Every export or verify invocation in this document, and every one you run yourself against the
bundled source, MUST include `--source mycelium-source` explicitly. The export performs no network
or git writes and never touches the source tree.

### 2. Host plugin selection, hook trust, and health check

**Do not install the upstream `arjunrajlaboratory/mycelium` marketplace for this bundle.** Upstream
Mycelium has no coordination subsystem, no bridge, and only five Codex hooks -- it is not this
exported, integrated candidate. Instead, point your host's plugin marketplace at the **local**
directory the exporter just produced (step 1's `<native-mycelium-plugin-dir>`), which carries its own
`.claude-plugin/marketplace.json` / `.codex-plugin/plugin.json` copied and re-versioned from the
bundled Mycelium source. This local-build-selection flow is confirmed by reading the live
registration this machine already carries (`codex mcp list` shows `mycelium-coord` running from a
`~/.codex/plugins/cache/...` path with a `+coord.integration.rN`-suffixed version -- a local cache
entry, never the upstream repo):

**Claude Code:**
```bash
claude plugin marketplace add <native-mycelium-plugin-dir>   # LOCAL path, not a GitHub repo
claude plugin install mycelium@mycelium
```
(or `claude --plugin-dir <native-mycelium-plugin-dir>` for a single-session dev load of the exported
candidate, per the same pattern `mycelium-source/README.md` documents for the upstream repo). Restart
Claude Code after re-running the exporter and `claude plugin update mycelium@mycelium` so a refreshed
build's skills/hooks/MCP registration are picked up.

**Codex:**
```bash
codex plugin marketplace add <native-mycelium-plugin-dir>    # LOCAL path, not a GitHub repo
codex plugin add mycelium@mycelium
```
Before the first task, launch a current Codex CLI (not the desktop app), open `/hooks`, and trust
**all SIX** command hooks this integrated build registers -- upstream Mycelium alone ships five
(`mycelium-health.sh`, `mycelium-post-action.sh`, `mycelium-activity-tracker.sh`,
`mycelium-data-tracker.sh`, `mycelium-stop-check.sh`); the exporter's `merge_hooks()` step adds a
sixth SessionStart entry, `mycelium-coord-attach.sh` (the coordination auto-attach hook), without
dropping any of the five. Confirmed by actually exporting a candidate into an isolated scratch
directory and counting the SessionStart/PostToolUse/Stop hook entries in its `hooks/hooks.json`: six
total, matching six distinct scripts. Run `codex update` first if `/hooks` isn't exposed. Fully exit
and restart Codex afterward so the approved `SessionStart` hooks are present from process startup --
Codex deliberately skips untrusted command hooks.

**Health check -- verifying the exported candidate's files is not the same as verifying a host
session actually selected and is running it.** The checks below cover both; each is marked
`[shell-verifiable]` (run it yourself, right now, no live session needed) or `[LIVE host session
required]` (only observable from inside an actual Claude Code or Codex session).

1. **[shell-verifiable] Candidate files are the integrated build, not upstream.** `python3
   scripts/export_coordination.py --source mycelium-source --target <dir> --verify-only` reports
   `{"verified": true, ...}`; independently, `cat <dir>/.claude-plugin/plugin.json` (or
   `.codex-plugin/plugin.json`) shows a version with a `+<build-id>` suffix, e.g.
   `0.6.0+coord.integration.r1` -- upstream's plugin.json version has no such suffix.

2. **[LIVE host session required] The session actually selected this candidate, not upstream or a
   stale cache.** File identity (1) says nothing about what a fresh session loads:
   - Claude Code: `claude plugin list --json` (or the interactive `/plugin` command inside the
     session) and confirm `mycelium@mycelium` is enabled and was installed from the local
     marketplace you added in step 2 above (`claude plugin marketplace add <native-mycelium-plugin-dir>`),
     not a GitHub-fetched copy.
   - Codex: `codex plugin list --json` and confirm the listed `mycelium` plugin's version carries the
     same `+<build-id>` suffix as (1), sourced from the local marketplace snapshot added in step 2
     above.

3. **[LIVE host session required, read-only] The coordination MCP server that answers is this
   candidate's bundled package**, not merely "present" in config. `codex mcp list` / `claude mcp get
   mycelium-coord` only show registration, not that the right code is behind it. From inside the
   session, call the `coord_list_tasks` MCP tool (no required arguments -- an empty task list is a
   pass, it only reads the task registry and creates nothing) and confirm it returns rather than
   erroring.

4. **Six trusted hooks, enumerated from this candidate's own `hooks/hooks.json`, checked separately
   from (1)-(3).** Five are inherited unchanged from `mycelium-source/hooks/hooks.json`; the exporter's
   `merge_hooks()` step adds a sixth:
   - SessionStart: `mycelium-health.sh` (inherited)
   - PostToolUse / `Bash`: `mycelium-post-action.sh`, `mycelium-data-tracker.sh` (inherited)
   - PostToolUse / `apply_patch`: `mycelium-activity-tracker.sh` (inherited)
   - Stop: `mycelium-stop-check.sh` (inherited)
   - SessionStart: `mycelium-coord-attach.sh` (added by the exporter -- the coordination auto-attach hook)

   **[shell-verifiable]** count six hook-script references across `SessionStart`/`PostToolUse`/`Stop`
   in the exported candidate's `hooks/hooks.json`. **[LIVE host session required, Codex only]** open
   `/hooks` in a current Codex CLI session and confirm all six show as trusted -- Codex will not
   dispatch an untrusted command hook. Claude Code has no separate hook-trust step: its **core
   lifecycle hooks are registered in the project's settings by `init_repo.py`** (per repository,
   step 0), while the coordination auto-attach hook is plugin-provided.

5. **[LIVE host session required] The `mycelium-health.sh` SessionStart hook itself is firing.** It
   loads session-resume context, refreshes `.living/INDEX.md` counts, and injects knowledge summaries
   on every session start. A clean session start with that context visible is evidence for *that one
   hook*, on *that one repository* (on Claude, only if the repository was initialized per step 0
   above) -- it is not, by itself, evidence that the other five hooks or the coordination MCP are
   working. Check those separately via (1)-(4) above. Together, these five checks establish plugin
   **selection**, hook **registration/trust**, a **read-only MCP** response, and **SessionStart**
   firing -- they do **not**, on their own, exercise the **PostToolUse/Stop** hooks, which fire only on
   real edits/bash and real Stop events in a live session. This bundle does not ship a separate
   standalone health-check binary beyond these checks.

### 3. Bridge config and password prerequisites

The cmux bridge (`bridge/cmux_bridge/`) needs, before any live use:

- a running cmux session (the process either descends from the cmux app in `cmuxOnly` mode, or the
  owner has set `automation.socketControlMode=password` in cmux Settings > Automation);
- `CMUX_BRIDGE_PASSWORD_FILE` set to the path of an owner-only password file for that password mode
  -- the CLI reads the file only if its mode bits carry no group/other permissions at all
  (`stat().st_mode & 0o077 == 0`, i.e. `chmod 600`); a group- or other-readable file is silently
  ignored, not an error. **Never print, echo, or embed the password itself** -- only its file path is
  ever a legitimate thing to mention;
- the live `codex` and `claude` CLIs installed and on `PATH`.

The full operator-side contract (tool list, refusal codes, bind/submit/wait flow) lives in
`src/codex/skills/cmux-driver/references/bridge-mcp.md`. Offline (no live cmux/CLIs needed):
`cd bridge && python3 -m pytest tests -q`.

**Python dependencies.** Both MCP servers (`bridge/cmux_bridge/`, `coordination/mycelium_coord/`)
need `fastmcp` and `mcp` on the interpreter that launches them:

```bash
python3 -m pip install "fastmcp==3.1.0" "mcp==1.26.0"
```

Verified with `pip install --dry-run` against this bundle's tested versions (exit 0, all
requirements already satisfied on the reference machine); and both server modules import cleanly end
to end without starting a live server or touching any cmux socket:

Run each from the bundle root in its own subshell -- `cd` persists across lines in the same shell, so
chaining these with a bare `&&` would resolve the second `cd` relative to the first (`bridge/coordination`,
which doesn't exist) and fail:

```bash
( cd bridge       && PYTHONPATH="$(pwd)" python3 -c "from cmux_bridge.server import mcp; print(mcp.name)" )   # -> codex-claude-bridge, exit 0
( cd coordination && PYTHONPATH="$(pwd)" python3 -c "from mycelium_coord.mcp_server import mcp; print(mcp.name)" )  # -> mycelium-coord, exit 0
```

**Bridge MCP server registration (Codex).** `mycelium-coord` registers itself automatically -- it's
merged into the plugin manifest by the exporter (step 1) and Codex picks it up from the active
plugin's `.mcp.json`. The standalone bridge (`codex-claude-bridge`) is a separate MCP server and is
**not** part of the Mycelium plugin manifest, so it needs its own explicit registration, confirmed
against the installed `codex mcp add --help` (codex-cli 0.146.0) and matching the live registration
already present on the reference machine (`codex mcp list` shows a `codex-claude-bridge` row with
this exact command/args/env shape):

```bash
codex mcp add codex-claude-bridge \
  --env CMUX_BRIDGE_PASSWORD_FILE=<path-to-owner-only-password-file> \
  --env PYTHONPATH=<path-to-this-bundle>/bridge \
  -- python3 -m cmux_bridge
```

This registers the server entry in Codex's own MCP config (`codex mcp list` / `codex mcp get
codex-claude-bridge` to confirm) without starting or connecting to it -- it will not touch cmux until
a session actually calls `bridge_discover`/`bridge_bind`/etc. Registration was verified by inspecting
the flag surface and an already-live equivalent entry on this machine, without re-running or
re-registering the bridge itself, so as not to touch the live cmux bridge.

### 4. Minimal cross-session exchange example

The coordination protocol (`coordination/mycelium_coord/`) is provider-neutral: the same operations
back both the bundled CLI (`coordination/bin/mycelium-coord`, confirmed subcommands `create-task`,
`attach`, `send`, `inbox`, `ack`, `checkpoint-publish`, `checkpoint-read`, ...) and, once the plugin
is loaded, the `coord_*` MCP tools (Claude server `plugin:mycelium:mycelium-coord`, Codex server
`mycelium-coord`) -- `coord_attach`, `coord_send`, `coord_inbox`, `coord_ack`,
`coord_checkpoint_publish`, `coord_checkpoint_read` map one-to-one onto the CLI ops below. This is an
**unmanaged messaging example**: it creates a task, attaches, and sends/acks plain coordination messages
and a checkpoint; it does **not** `exec-open` an execution record or reserve/claim dispatches, so it is
not the bounded/managed form of the workflow. A *managed* task additionally opens an execution record and
reserves then claims each actionable dispatch (see [Bounded execution](#bounded-execution)). Smallest
concrete round trip between two sessions attached to the same task:

```bash
BIN=coordination/bin/mycelium-coord
ROOT=/path/to/an/isolated/coord-state-dir   # --root; use a scratch dir, never a shared store, to try this
WT=/path/to/a/worktree                      # any existing directory; must be identical for create-task
                                             # and the executor's attach (executor worktree must equal
                                             # the task's canonical worktree)

# 1. Either session creates the task once.
$BIN --root "$ROOT" create-task demo-task --project demo --worktree "$WT"

# 2. Each session attaches under its own role, participant id, and NATIVE HOST IDENTITY
#    (--host host=<claude|codex> session=<your session id> -- required; omitting --host fails
#    with "invalid_participant: host.host (native host kind) required"). Normal mapping for this
#    workflow: supervisor = codex, executor = claude.
$BIN --root "$ROOT" attach demo-task sup-1  --role supervisor --worktree "$WT" \
  --host host=codex  --host session=sup-session-1
$BIN --root "$ROOT" attach demo-task exec-1 --role executor  --worktree "$WT" \
  --host host=claude --host session=exec-session-1

# 3. Supervisor sends a message to the executor. --kind must be one of the ten protocol kinds
#    (task, amendment, review_finding, progress, blocker, question, checkpoint_request,
#    checkpoint_ready, completion_receipt, acknowledgment) -- "brief" is NOT one of them and
#    fails with exit 3 "invalid_kind".
$BIN --root "$ROOT" send demo-task --id msg-1 --from sup-1 --to exec-1 \
  --kind task --revision 0 --text "start the smoke run"

# 4. Executor reads its inbox and acknowledges.
$BIN --root "$ROOT" inbox demo-task exec-1 --after 0
$BIN --root "$ROOT" ack   demo-task exec-1 msg-1 --note "starting now"

# 5. Executor replies to the supervisor (reply-to the original message) and the supervisor acks it back.
$BIN --root "$ROOT" send demo-task --id msg-2 --from exec-1 --to sup-1 \
  --kind progress --revision 0 --text "smoke run started" --reply-to msg-1
$BIN --root "$ROOT" inbox demo-task sup-1 --after 0
$BIN --root "$ROOT" ack   demo-task sup-1 msg-2 --note "seen"

# 6. Executor publishes a checkpoint; supervisor reads it back.
$BIN --root "$ROOT" checkpoint-publish demo-task --revision 0 --by exec-1 --checkpoint '{"status":"started"}'
$BIN --root "$ROOT" checkpoint-read    demo-task
```

This whole eleven-command sequence was run end to end, with the supervisor/executor host mapping
shown above (supervisor `host=codex`, executor `host=claude`), against a freshly created, isolated
`--root` state dir and worktree (no production coordination store touched) -- every command returned
exit 0 and the expected JSON (the supervisor's inbox read shows `msg-2`; `checkpoint-read` returns
`{"status": "started"}`). Two constraints to keep in mind, both documented inline above: (a) `attach`
without `--host` fails `invalid_participant`, and (b) `--kind` must be one of the ten protocol kinds
-- `brief` is not one of them and fails `invalid_kind`.

### 5. Tested versions

Verified on (not "requires exactly" -- other compatible versions may work):

- Python 3.13.11
- fastmcp 3.1.0
- mcp 1.26.0
- cmux app **0.64.17 (build 97)**, password socket-control mode -- note "v2" elsewhere in this
  ecosystem names the coordination **protocol**'s schema-version-family, not the cmux application's
  own version
- Claude Code 2.1.263 originally, with fixtures re-run on 2.1.267
- native Codex audit on codex-cli 0.146.0
- contract `codex-claude-workflow 1.2.0` (matches this bundle's `VERSION` file)
- macOS (Darwin)

This matches what is installed on the reference machine today: Python 3.13.11, fastmcp 3.1.0, mcp
1.26.0, cmux 0.64.17 (97), Claude Code 2.1.267, codex-cli 0.146.0.

## Attribution / license

The `codex-claude-workflow`-derived parts of this bundle (`bridge/`, `coordination/`, `scripts/`,
`tests/`, `src/`, `VERSION`, `.gitignore`) are released under the MIT License -- see `LICENSE`
(copyright mcleanT, 2026).

`mycelium-source/` bundles Mycelium lifecycle source at commit `f2b0083`; that subtree carries its
own upstream `LICENSE` and `README.md`, unmodified. See `PROVENANCE.md` for the full breakdown of
what was included, what was excluded, and why.

## Status

This is the **authorized private release** of the SporeDrive coordination bundle, and it is complete
and accepted. The maintained runtime source is the bounded-execution build at commit `664de18` on
branch `sporedrive-bounded-execution` (bridge `cmux_bridge/core.py` sha256 `54cac34e...`, 18 pre-enter
gate sites); the bundled Mycelium lifecycle source is pinned at `f2b0083` (see `PROVENANCE.md`). The
concrete commit, cache, and manifest identities are recorded in the accepted release receipt
`checks/RELEASE-RECEIPT-20260910.json`. That receipt and the `checks/live-*` run directories cited below
are **local validation artifacts from the tested `664de18` release run -- they are not committed to the
repository and are absent from a GitHub clone**; the results they attest are dated to that release.

**Validation (tested `664de18` release):**

- **346 offline tests pass** -- coordination 87 (including 5 repair-verification tests), package tests
  45, bridge 214 -- plus `py_compile` and a clean-clone-shaped `tests/test_wfctl.py` (no `snapshots/`,
  `installed/`, or `receipts/`, the shape a fresh release clone actually has). `ruff` format is clean
  on changed sources (five pre-existing `E741` in `live_acceptance.py` left unchanged).
- **Live host smoke: 40 / 40 PASS, 0 FAIL** against a real cmux session over password-mode socket
  access, on a `--focus false` disposable surface with focus preserved and every runner-owned surface
  closed (`checks/live-20260910T091929Z`, head `664de18`).
- **Fresh-host execution verified.** Two fresh native sessions (a `claude -p` and an isolated
  `codex exec`, with distinct PIDs, session ids, and plugin caches) loaded the installed package and
  exercised shared managed pause/resume/closure and evidenced-repair gates; the real disposable-cmux
  managed gate was driven through the actual `notify_via_bridge` transport (paused and closed
  deliveries refused with no Enter, the resumed delivery accepted exactly once).
- **Review budget: 2 / 2 consumed** (a primary review plus one repair verification); no further
  reviews are allocated for this release.

**Honest limitations (carried forward).** A Claude or Codex session that was already running keeps its
previously-loaded modules until it restarts; the repaired runtime is what **fresh** clients load. In
the real bridge-gate run, delivering the resumed notification left the disposable target on a modal at
teardown, so the harness could not confirm a graceful `SessionEnd` and correctly refused to press
Enter onto a modal -- cleanup was instead reconciled out of band (owned surface closed, owned process
and descendants exited, writer binding released). The compaction-efficiency and effort-versus-output
comparisons are descriptive experiments, not yet run. Delegation, token, and effort controls are
installed *policy*, not a runtime-enforced mechanism. The native lifecycle audit used fixture and
injected state -- no real scientific repository was mutated.
