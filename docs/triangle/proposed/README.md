# Proposed codex_ask off-bus variant (NOT installed)

`codex_ask.sh` here is a copy of the installed `~/.claude/tools/codex_ask.sh` with ONE change: the
`codex exec` argv gains `--ignore-user-config` (codex-cli: "Do not load $CODEX_HOME/config.toml; auth
still uses CODEX_HOME"). A review run then loads no plugins, MCP servers (so no mycelium-coord), notify
hook or hook state, so a read-only consult cannot attach to or message the coordination bus.
Checked what the script relies on from config.toml: nothing. `-m`, `-c model_reasoning_effort=`,
`--sandbox read-only` and `--disable hooks` are already explicit; the config's own model/effort/
approvals_reviewer/service_tier are intentionally not inherited. No extra `-c` was needed.
Not run here; the lead smoke-tests it (check the banner shows the intended model/effort and no MCP).
Drift: the installed copy differs from BOTH source copies (`~/tools/codex-claude-workflow/src/claude/tools/codex_ask.sh`
and `src/claude/tools/codex_ask.sh` in this repo), and those two differ from each other (159 diff
lines); re-base any adoption on the installed copy and reconcile the sources deliberately.
