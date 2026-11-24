# Installation and activation

Cloning this repository installs nothing. Getting SporeDrive working takes four separate steps, and
each one changes something different:

| Step | Command | What it changes |
|---|---|---|
| 1. Global instruction files | `scripts/wfctl.py install` | `~/.claude` and `~/.codex` instruction files, skills and tool wrappers, plus the owned Claude settings leaves |
| 2. Integrated plugin | `scripts/export_coordination.py` | A plugin directory you choose (nothing global) |
| 3. Host activation | `claude plugin …`, `codex plugin …`, `/hooks`, restart | Each host's plugin registry and hook trust |
| 4. Project initialization | "Set up mycelium" (`init_repo.py`) | The target repository |

Prerequisites (macOS, cmux, the CLIs, Python packages) are listed in the
[README](../README.md#prerequisites-and-platform).

## 1. Install the global instruction files (`wfctl`)

`wfctl.py` installs the files under `src/` into their global locations. This is the complete
`TARGETS` list in `scripts/wfctl.py`, which is the authority:

| Source | Destination | Kind |
|---|---|---|
| `src/claude/CLAUDE.md` | `~/.claude/CLAUDE.md` | file |
| `src/claude/skills/codex-review/SKILL.md` | `~/.claude/skills/codex-review/SKILL.md` | file |
| `src/claude/skills/codex-review/references/global-conventions.md` | `~/.claude/skills/codex-review/references/global-conventions.md` | file |
| `src/claude/tools/codex_ask.sh` | `~/.claude/tools/codex_ask.sh` | file |
| `src/claude/tools/codex_launch.py` | `~/.claude/tools/codex_launch.py` | file |
| `src/claude/tools/sporedrive_review_guard.py` | `~/.claude/tools/sporedrive_review_guard.py` | file |
| `src/claude/skills/prompt-optimizer/` | `~/.claude/skills/prompt-optimizer/` | directory |
| `src/claude/skills/run-pipeline-local/` | `~/.claude/skills/run-pipeline-local/` | directory |
| `src/codex/AGENTS.md` | `~/.codex/AGENTS.md` | file |
| `src/codex/skills/cmux-driver/` | `~/.codex/skills/cmux-driver/` | directory |

It also merges the settings leaves listed in `src/claude/settings.fields.json` into
`~/.claude/settings.json`. Those leaves are the only settings `wfctl` owns.

**Review and customize `src/` first.** Installation *replaces* those managed files with this
repository's versions, including the role and model policy. Edit the files under `src/`, not the
installed copies. A later `verify` reports hand edits to installed copies as drift.

```bash
export REPO=/path/to/sporedrive
cd "$REPO"
python3 scripts/wfctl.py install            # snapshot, then install every target
python3 scripts/wfctl.py verify             # OK/DRIFT per target; exit 1 on drift
```

What `install` does:

- **Snapshots first.** It records a `pre-install-<UTC>` snapshot of everything currently at the
  destinations. It then copies `src/` into place atomically and writes `installed/manifest.json`
  with the repository commit and source/destination sha256.
- **Refuses to clobber your edits.** If an installed target diverged from every version `wfctl`
  ever installed, `install` stops. `--accept-drift` installs over it; your edit is still kept in
  the snapshot.
- **Scoped installs.** `install --only <src path>` (repeatable) installs one target, one file inside
  a directory target, or the literal `settings`.
  - Other installed files keep whatever local drift they have.
  - The snapshot is still the full one.
  - The manifest records `scope` and `scoped_files`.
- **No dry-run mode.** To see what would change, run `verify` first. The dry-run option is on
  `rollback`.

To roll back:

```bash
python3 scripts/wfctl.py list
python3 scripts/wfctl.py rollback <label> --dry-run    # show the plan
python3 scripts/wfctl.py rollback <label>
```

What `rollback` restores:

- owned target files in the snapshot come back byte for byte;
- owned targets that were absent at snapshot time are removed;
- the owned settings leaves from `settings.fields.json` are reset to their snapshot values. Other
  settings are left alone.

Other captured model and config fields are evidence only. `rollback` reports them as
"evidence-only (not restored)" and never restores them.

If an owned target or leaf has diverged from every version `wfctl` recorded, `rollback` refuses.
It changes nothing and exits with the drift code. `rollback <label> --force` first archives the
diverged copies, then restores.

The installer never copies credentials: `~/.codex/auth.json`, `~/.claude/.credentials*`, `.env`
files and tokens. `wfctl.py snapshot` scrubs key-like fields, and it aborts if a token-shaped string
appears.

Check provenance after installing:

```bash
cat installed/manifest.json        # commit + sha256 at install time
head -3 ~/.claude/CLAUDE.md        # names its maintained source and contract version
```

## 2. Export the integrated plugin

`scripts/export_coordination.py` builds one self-contained plugin directory, in this order:

1. It copies the bundled `mycelium-source/`.
2. It replaces hook files from `core-overlay/`, the repository-owned hook versions.
3. It adds `coordination/` and `bridge/`.
4. It registers the `mycelium-coord` MCP server in both plugin manifests.
5. It adds the coordination hook registrations.
6. It writes a cross-checked `EXPORT_MANIFEST.json`.

It performs no network or git writes, and it never changes the source trees.

```bash
python3 scripts/export_coordination.py \
  --source mycelium-source \
  --target <plugin-dir> \
  --build-id <semver-build-id>

python3 scripts/export_coordination.py --source mycelium-source --target <plugin-dir> --verify-only
```

**Always pass `--source mycelium-source` explicitly, including with `--verify-only`.** The default
source is a maintainer path, `~/tools/mycelium-lifecycle-wfi`. If that path exists on your machine
with other content, verifying against it reports false mismatches instead of an error.

Other flags:

- **`--target`:** refused if it aliases the source. It is also refused if it is non-empty and lacks
  the exporter's ownership marker, unless you pass `--force`.
- **`--build-id`:** appended to the plugin version, for example `0.6.0+<build-id>`.
- **`--keep-stage`:** keeps the staging directory when validation fails.
- **`--overlay <dir>`** and **`--no-overlay`:** choose a different hook overlay directory, or
  export the source hooks unchanged.

About the core overlay:

- **Why it exists:** the files in `core-overlay/skills/core/` carry SporeDrive's own runtime changes
  to the Mycelium hooks, such as housekeeping attempt accounting and one-shot Stop-lock contention.
- **What ships:** the overlay replaces source files at the same paths, so the overlay is what ships.
  An overlay built on an older source can silently hide newer source behavior.
- **The guard:** `scripts/test_export_stop_budget.py` exports with the overlay and fails if an
  overlay hook drops any function its source defines.

**Build identity.** Three identities can differ:

- this repository's `mycelium-source/` pin;
- an exported candidate's `EXPORT_MANIFEST.json` and `+<build-id>` version;
- whatever a given host already has installed.

Do not assume they match. A change that touches only docs does not require a re-export.

## 3. Activate on each host

Do not install the upstream `arjunrajlaboratory/mycelium` marketplace. It has no coordination
subsystem, no bridge, and only five Codex hooks. Point each host at the **local** export directory
instead.

**Claude Code**

```bash
claude plugin marketplace add <plugin-dir>     # local path
claude plugin install mycelium@mycelium
```

- **Single-session load:** `claude --plugin-dir <plugin-dir>` loads the export for one session.
- **After a re-export:** run `claude plugin update mycelium@mycelium` and restart.
- **Hooks:** Claude Code's lifecycle hooks are registered per repository by project
  initialization (step 4), not by installing the plugin. The coordination attach hook comes from the
  plugin.

**Codex**

```bash
codex plugin marketplace add <plugin-dir>      # local path
codex plugin add mycelium@mycelium
```

Then open `/hooks` in a current Codex CLI session and trust **all seven hook registrations**. Run
`codex update` first if `/hooks` is missing. Codex skips untrusted command hooks. Fully restart
Codex afterwards, so SessionStart hooks are present from process start.

The seven registrations use six distinct scripts:

| Event | Matcher | Script | Origin |
|---|---|---|---|
| SessionStart | startup/resume/clear/compact | `mycelium-health.sh` | bundled Mycelium, replaced by `core-overlay/` |
| SessionStart | startup/resume/clear/compact | `mycelium-coord-attach.sh` | exporter |
| PostToolUse | `Bash` | `mycelium-post-action.sh` | bundled Mycelium |
| PostToolUse | `Bash` | `mycelium-data-tracker.sh` | bundled Mycelium |
| PostToolUse | `apply_patch` | `mycelium-activity-tracker.sh` | bundled Mycelium |
| PostToolUse | `Bash` | `mycelium-coord-attach.sh` | exporter |
| Stop | (all) | `mycelium-stop-check.sh` | bundled Mycelium, replaced by `core-overlay/` |

Codex dispatches hooks through `PLUGIN_ROOT`, and there is no per-repository registration. The
Mycelium dispatcher does nothing outside an initialized Mycelium repository.

### Register the cmux bridge (Codex)

`mycelium-coord` registers itself through the plugin manifest. The `codex-claude-bridge` server is
separate, and needs its own entry:

```bash
codex mcp add codex-claude-bridge \
  --env CMUX_BRIDGE_PASSWORD_FILE=<path-to-owner-only-password-file> \
  --env PYTHONPATH=<path-to-this-repo>/bridge \
  -- python3 -m cmux_bridge
```

Registration does not contact cmux. Nothing happens until a session calls `bridge_discover` or
`bridge_bind`. Bridge prerequisites:

- **A running cmux session.** Either the process descends from the cmux app (`cmuxOnly`), or
  *Settings > Automation* sets `automation.socketControlMode=password`.
- **A password file for password mode.** `CMUX_BRIDGE_PASSWORD_FILE` must point to an owner-only
  file (`chmod 600`).
  - A group- or world-readable file is silently ignored.
  - Never print or paste the password itself.
- **The live `codex` and `claude` CLIs** on `PATH`.
- **Python packages:** both MCP servers need `fastmcp` and `mcp` installed on the interpreter that
  runs them. Python 3.13 is the tested interpreter; it is not a proven exact-version requirement.

```bash
python3 -m pip install "fastmcp==3.1.0" "mcp==1.26.0"
( cd bridge       && PYTHONPATH="$(pwd)" python3 -c "from cmux_bridge.server import mcp; print(mcp.name)" )
( cd coordination && PYTHONPATH="$(pwd)" python3 -c "from mycelium_coord.mcp_server import mcp; print(mcp.name)" )
```

The two import checks should print `codex-claude-bridge` and `mycelium-coord`. The full tool list,
refusal codes and bind/submit/wait flow are in the
[bridge MCP contract](../src/codex/skills/cmux-driver/references/bridge-mcp.md).

## 4. Initialize each target repository

Installing the plugin does not turn a repository into a Mycelium project. In the target
repository, open Claude Code and say "Set up mycelium", which runs
`skills/core/scripts/init_repo.py --target-dir <repo>`.

Initialization creates:

- `.living/`, `MYCELIUM.md`, and the `CLAUDE.md`/`AGENTS.md` adapters;
- on Claude Code, the project-level hook registrations in `.claude/settings.local.json`.

A repository that was never initialized has no Claude lifecycle hooks, even if the coordination MCP
answers. On Codex, the bundled hooks act only once the repository is initialized.

## Health checks

Checking the exported files is not the same as checking what a running session loaded. Checks
marked *(shell)* can run anywhere; checks marked *(session)* need a fresh host session.

1. *(shell)* `--verify-only` prints `{"verified": true, "problems": []}`. The plugin versions in
   `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json` carry your `+<build-id>` suffix.
   Upstream versions have none.
2. *(session)* The host selected this build.
   - Claude: `claude plugin list --json` shows `mycelium@mycelium` enabled from your local
     marketplace.
   - Codex: `codex plugin list --json` shows the same `+<build-id>` version.
3. *(session)* The coordination MCP answers: `coord_list_tasks` returns. It only reads, so an empty
   list is a pass.
4. *(shell)* `hooks/hooks.json` has seven registrations of six scripts, as in the table above.
   *(session, Codex)* `/hooks` shows all seven as trusted.
5. *(session)* The SessionStart health context appears in an initialized repository. This proves
   only that one hook works in that one repository. PostToolUse and Stop fire only on real tool
   use and a real Stop.

A session that was already running keeps what it loaded at startup. Restart it after any install or
re-export.

## Tested versions

Verified on the maintainer's machine. Other versions may work.

| Component | Version |
|---|---|
| Operating system | macOS (Darwin) |
| Python | 3.13 (tested interpreter) |
| fastmcp | 3.1.0 |
| mcp | 1.26.0 |
| cmux | 0.64.17, password socket-control mode |
| Instruction contract | see `VERSION` |

Claude Code and codex-cli change often. Check the plugin, `/hooks` and MCP commands above against
your installed `--help` output.
