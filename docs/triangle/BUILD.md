# Triangle build — scope, pass conditions, checkpoint

## Scope and authorization (owner, 2026-11-22)
Owner's reply to the proposal (`~/tools/codex-claude-workflow/docs/proposals/triangle-coord-2026-11-22.md`):
"1. okay, we should add a mechanism to prevent agents from 2 sessions from overwriting eachother. 2. ok. 3. soudns good. go ahead and build and test"

- Build the triangle on mycelium-coord, one step at a time, starting with the Antigravity experiment.
- Antigravity's role: reviewer and adviser only, never a writer.
- **Amendment:** a mechanism that stops agents in two sessions from overwriting each other. Interpreted as file-level, because a folder-level lock would block the owner's routine multi-session use of the Science root. Owner may redirect.
- **Owner holds (ask first):**
  - registering Antigravity on the LIVE store;
  - installing into live plugin copies or hook settings (any host);
  - commit or push.
- **Rules:**
  - Test suites run on Betty (genoa, sbatch). Live host smokes must run on the Mac, because the hosts live there.
  - No installed copies and no live store (`~/.local/state/mycelium/coordination`) are touched.
  - One writer in this dev folder at a time.

Dev folder: `~/tools/sporedrive-triangle`, branch `feat/triangle-coord`, from `sporedrive-release` 91546b3. Same code as the installed coord on Claude and Codex.

## Milestones and fixed pass conditions
**M1 Antigravity experiment.** Uses the installed launcher, a scratch store and a temporary registration; serialized, run by the lead.
- **Pre:**
  - No SCF or other `agy` job running.
  - Back up `~/.gemini/config/mcp_config.json` (0 bytes).
  - Record the live store's file count.
  - Hand-launch the server with the same env and the scratch dir, and get a tool list. If that fails, the result is CANNOT-EVALUATE, not "agy can't see the tools".
- **Pass — tool access.** One `agy -p` stream-json run. Report "tools listed" and "tool called" separately, then three outcomes: sees + calls, does not, or cannot evaluate. An approval it cannot give is recorded, not bypassed.
- **Pass — round trip.** The lead creates a task and question on the scratch store via the CLI. The agy run attaches as `antigravity`, reads the question and replies with reply_to. The scratch store shows 2 messages with the right host and reply_to.
- **Restore.** Remove the registration and confirm `mcp_config.json` is byte-identical to the backup. No leftover server process. The live store's file count is unchanged.
- **App / IDE.** One bounded attempt, or a paste-in prompt for the owner. Unknown if not done; this does not hold M2.

**M2 Overwrite guard.**
- First reproduce lost updates with no guard, per pair and tool:
  - Claude Edit/Write vs Claude;
  - Codex apply_patch vs Claude;
  - shell write.
  The reproduced cases become acceptance tests.
- Verify with a scratch logging hook per host that each host's pre-tool hook fires for its write tool. Correct the earlier claim where it does not.
- Check the session id a subagent's tool call carries, and behaviour on resume.
- **Design constraints:**
  - File-level, real-path keyed.
  - Constant time per write with a hard timeout.
  - A deliberate fail-open-and-logged choice.
  - An explicit rule for shared append files (living docs, memory).
  - Tested with Code Mode on via `--settings`.
- **Pass:**
  - Every reproduced cross-session overwrite on a hooked tool is refused with a message naming the holder.
  - Different files in the same worktree are never blocked.
  - A session's own subagents are not blocked.
  - Shell writes are reported as uncovered.
  - Unit tests pass on Betty.

**M3 Hardening.**
- Authority guard for advisory processes; owner-facing sessions are not blocked.
- `codex_ask` off-bus flags, checked by a run whose events show zero MCP servers.
- Find a scoped agy registration.

**M4 Core patches.**
- Host-aware prefix strip.
- Kinds: review_request, consult_request, report.
- Reply metering.
- Version floor.
- agy waitpath fallback.
- Backward compatible with the installed copies.

**M5 Consult adapters.**
- agy via clean-dir acp, with positive and negative memory controls.
- claude_ask.
- Receipts with coverage evidence and CANNOT-EVALUATE.

**M6 Wake.**
- Registry refactor.
- Codex queue probe.
- Claude socket probe.
- agy next-turn inbox.

**M7 Triangle drill.**
- One scoped Codex review of the diff.
- Report.

Baseline: the existing suite runs on Betty at 91546b3 before any change.

## Checkpoint log
- 2026-11-22: worktree created. M1 starting.
- 2026-11-22 M1 DONE (2 agy runs, both `agy -p`, model reported "Gemini 4 Neon"). Evidence: evidence/m1/.
  - `agy mcp add` writes ONLY ~/.gemini/config/mcp_config.json (hash diff of 17 config files).
  - The CLI exposes MCP tools by writing per-tool schema files to ~/.gemini/antigravity-cli/mcp/<server>/ (51 files incl. instructions.md); the model reads them with read_file and calls them through a generic `call_mcp_tool {ServerName, ToolName, Arguments}`.
  - Run 1 (no grant): headless auto-DENIED the "mcp" permission. The CLI exited 0, the result status was SUCCESS and the response was empty, with `denied_actions:[mcp]` → exit code/status are not evidence.
  - Run 2: `--settings '{"permissions":{"allow":["mcp(<server>/<tool>)",...]}}'` (session-only) granting 4 tools → attach, inbox and send all succeeded. The scratch store holds 2 messages: agy-observer→claude-lead, kind progress, reply_to m1-q1, correlation m1-corr, text "PINEAPPLE-42 — running as Gemini 4 Neon". The CLI log shows the permissions parsed. Per-tool grants are a host-side least-privilege control (advisory agy can be denied execution_* tools).
  - Desktop App: its language_server (pid 1394) spawned the coord server from the global config (pid 38945, same python and env) and cached the schemas in ~/.gemini/antigravity/mcp/<server>/ → App loads the global registration (VERIFIED at the process level). Model use inside the App is UNTESTED. Removing the registration did NOT stop the App-spawned server (stopped by hand).
  - IDE not running → UNKNOWN.
  - The CLI starts its own language server per run (cli log: "Starting language server process with pid 38816"), not the App's.
  - Restore: mcp_config.json byte-identical to the backup. `agy mcp remove` leaves `{"mcpServers": {}}` (23 B), so it was restored from the backup. Schema caches moved to scratch. Live store 13871 → 13871 files.
  - Side finding for M6: agy has native `send_message` / `manage_inbox` tools (candidate Antigravity wake route; untested).
- 2026-11-22 Baseline on Betty at 91546b3: 266 passed, 4 skipped, 2 failed (both `ModuleNotFoundError: fastmcp` on Betty — env, not code). evidence/betty/baseline-91546b3.txt.
- 2026-11-22 RUNNING (owner of both: this lead session 6cb0c997):
  - workflow wp2zexa9l (wf_dfd2e1ea-fc4) — M2 host hook probes (3 × sonnet/medium; scratch only, ≤6 model runs per host) → scratchpad/m2/<host>/.
  - workflow wwdbghwkf (wf_60563f62-c93) — M3+M4 core patches (sonnet/medium implementer, sole writer in this worktree, tests via betty_test.sh) + opus/high adversarial verify.
  - Next: read the probe contracts → design and implement the write guard (writer starts only after M3/M4 finishes: one writer).
- Model-tier hook enforces: implementation = sonnet/medium; opus/high only for verify/judge/synthesis.

## M2 revised (2026-11-22, after host probes; supersedes the M2 "claims/holder" pass conditions above)
Probe evidence: evidence/m2-probes.json (Claude 6 runs, Codex 6, agy 5).
- Claude Edit/Write already refuse a write when the file changed since the session's last read. The check runs BEFORE hooks, so no new Claude hook is needed for those tools.
- Codex apply_patch Update File checks its context lines against the current file, so no loss occurs on overlapping hunks. Add File over an existing file has not been run yet.
- agy write_file has NO staleness check. A lost update was VERIFIED: an external EXTERNAL-LINE was overwritten after a resumed conversation called write_file.
- Shell whole-file redirects clobber a concurrent change on every host (VERIFIED on Codex).
- **Design decision: refuse STALE writes (optimistic concurrency), not file ownership.**
  - For each (host, session), record the content signature of each real path the session last read or wrote.
  - Refuse a write to an existing file whose current signature differs from the session's record, or that the session has never read or written. This mirrors Claude's built-in rule.
  - Session-id ambiguity (subagents, resume) costs at most a re-read, never a lockout.
  - No TTL, no liveness tracking, no exemption list.
  - State lives OUTSIDE the coordination store: ~/.local/state/mycelium/write-guard (MYCELIUM_WRITE_GUARD_DIR).
- **Pass (checked by file content):**
  - (a) The reproduced agy overwrite is refused with a re-read instruction; after a re-read, both changes survive.
  - (b) A write after a fresh read, a new file, and a rewrite of the session's own last-written file are never blocked.
  - (c) Shell appends are never refused. Shell overwrites are reported as UNCOVERED (best-effort only).
  - (d) Fail-open-and-logged is used only if the host's own hook-failure behaviour leaves us the choice (verified first).
  - (e) Unit tests pass on Betty.
- **Pre-checks before code:**
  - Does agy fire a hook for read_file/view_file carrying a path?
  - What does agy do when a hook crashes or times out?
  - Does Codex apply_patch refuse Add File over an existing file?
- 2026-11-22 M3+M4 implemented (sonnet): 292 passed / 2 node-skips on the Betty venv (/vast/projects/arjunraj/auto-science/mycelium-ci/venv-fastmcp). Old-reader compatibility test passes against the vendored base package. The opus verifier found 2 important + 3 minor defects (evidence/m3-m4-result.json): the per_sender budget can be bypassed via actionable=True; advisory containment covered only 5 ops; the agy check_wait cap is not enforced on the MCP route; per_correlation refuses lifecycle messages; some tests are weak.
- 2026-11-22 RUNNING: workflow wuv2fzo8f (wf_023c1dd1-62f), owned by this lead. Stages, run strictly sequentially so there is a single writer:
  1. Repairs R1–R5. Advisory mode becomes an allowlist: observer-only attach, non-actionable sends, reads; every execution mutator is refused.
  2. Targeted repair check (opus).
  3. Write-guard implementation, Betty tests and live smokes (a)–(d), ≤6 model runs, scratchpad/m2guard.
  4. Write-guard adversarial verify (opus).
- 2026-11-22 wuv2fzo8f DONE (evidence/repair-writeguard-result.json).
  - Repairs R1–R5: the repair check closed all 5 named findings. Betty venv run: 302 passed, 2 node-skips.
  - New important finding: advisory detach plus observer re-attach can take over the executor id. Minor findings: advisory backlog writes bump state_version; the agy host_yield cannot lower the cap.
  - Write guard built: 46 unit tests on Betty. Live smokes (a)–(d) PASS by file content, using 3 model runs (agy conversation 6ed2602a-…, Codex session 01a20629-…).
  - Guard verify: 3 important findings: an unwritable state dir leaves agy fail-closed forever; multi-line or quoted shell is misparsed; the full-suite run omitted tests/. 2 minor findings: cd is ignored; an unreadable file is treated as new.
- 2026-11-22 Betty UNREACHABLE (ssh port 22 timeout; the route goes via the home gateway and only Tailscale is up, so the Penn VPN looks disconnected). Tests are held until the owner reconnects.
- 2026-11-22 RUNNING: w1cidwlt7 (wf_4aefb468-9ee), round-2 fixes A1–A3 and W1–W5 (sonnet; sole writer). If Betty is down it runs no tests: py_compile only, and tests are reported as CANNOT-EVALUATE. Afterwards: Betty full suite (coordination/tests + tests), then one opus verify of the round-2 changes.
- 2026-11-22 w1cidwlt7 DONE: round-2 fixes written (A1–A3, W1–W4) and compiled; py_compile passes. Tests are NOT RUN (CANNOT-EVALUATE, Betty unreachable). The implementer admitted one small local helper call, which breaks the owner rule; nothing was saved from it.
  - Held until Betty is back:
    - full suite `coordination/tests tests`;
    - the two triangle files;
    - one opus verify of round 2.
- 2026-11-22 RUNNING: wfzj6xui0 (wf_be3b883d-a11), Mac-only host probes, scratch only, no worktree writes except evidence JSON.
  - Codex agent: Q1 does the installed codex_ask load MCP servers; Q2 does the proposed --ignore-user-config variant load zero; Q3 Codex wake or resume route.
  - agy agent: Q4 ACP clean-dir consult; Q5 memory controls, positive and negative, plus a default-dir cross-conversation leak check; Q6 the native send_message/manage_inbox schemas.
- 2026-11-22 wfzj6xui0 DONE. Mac probes: Codex 5 runs, agy 8 runs. Evidence: evidence/m5-probes-{codex,agy}.json.
  - **Q1 VERIFIED (now observed, no longer inferred).** The installed codex_ask starts 6 MCP servers, including mycelium-coord and codex-claude-bridge (RUST_LOG=debug; --json shows no MCP events).
  - **Q2 VERIFIED.** The proposed `--ignore-user-config` copy starts no user or bus servers and still answers (gpt-5.6-sol, low, read-only). cmux-cua and codex_apps are still listed; cmux-cua is injected by the cmux codex shim (`-c mcp_servers.cmux-cua.*`, plus `--dangerously-bypass-hook-trust`). `codex queue` rejects that flag, so call /opt/homebrew/bin/codex directly.
  - **Q3 Codex wake.**
    - `codex exec resume <id> <prompt>` runs a headless turn on the same thread (recalled LANTERN).
    - `codex queue --thread --message` only stores to ~/.codex/queue_1.sqlite; with no daemon, no turn runs. Queued items drain on the next exec resume.
    - Live-TUI or daemon pickup: CANNOT-EVALUATE.
    - Scratch thread 01a20643-58b5-78b1-aa76-5ea8d95a94fc.
  - **Q4 agy consult route: VERIFIED.** `agy acp --gemini-dir <ABS scratch> --app-data-dir <relative>` with ACP initialize → session/new (mcpServers=[]) → session/prompt:
    - it answered (gemini-4-neon-high);
    - it needed no auth files;
    - it wrote NOTHING under ~/.gemini.
    An absolute --app-data-dir is rejected. **Variant (i), relative app-data-dir with the default gemini dir, is unsafe:** it wrote ~/.gemini/m5probe-appdata and ~/.gemini/private, and REWROTE ~/.gemini/config/config.json, adding plugins.lab-figures.enabled and plugins.mycelium.enabled = true. The lead restored config.json byte-identical from the M1 backup (sha 627dff77…) and moved both dirs to scratchpad/m5probe/leftovers.
  - **Q5 memory.**
    - Same ACP session recalls: positive control OK.
    - A new clean-dir session gives UNKNOWN: negative control OK.
    - A default-dir `agy -p` showed no cross-conversation leak in 1 trial.
    - The client must drain chunks per turn.
  - **Q6.** agy send_message(Recipient = conversation id, Message) reaches other agy conversations only, and gives no evidence of waking an idle or external session. There is no manage_inbox tool. Antigravity stays next-turn only.
  - agy conversation ids left behind: 3 default-dir -p runs (f2ab34a3, ebe76216, 92d1830c) in ~/.gemini/antigravity-cli; 4 ACP runs in scratch.
- 2026-11-22 wd0uzfvrs DONE. Re-smokes on the ROUND-2 writeguard.py (sha256 c14345d3…), checked by file content; evidence/m2-smoke-round2.json. All four PASS:
  - **R-a:** agy's stale write_file was denied, EXTERNAL-LINE was intact at the deny, and after a re-read the final file has all 3 lines (conversation 5bcfa0b9…).
  - **R-u:** with the state dir as a regular file, agy's write over an unread file LANDS. Fail-open is confirmed live; an offline control shows deny when the store is a real dir.
  - **R-c:** Codex Add File over an unread existing file was denied, the file was unchanged, and the write was allowed after cat (session 01a2064a…).
  - **R-q:** the proposed codex_ask calling /opt/homebrew/bin/codex directly with --ignore-user-config lists only the built-in codex_apps (streamable_http) and spawns no stdio MCP server. M3 off-bus pass condition MET for the direct-binary route; the cmux shim route adds cmux-cua.
  - The ~/.gemini config hashes were unchanged throughout. 5 model runs.
- The guard is now live-smoke-validated on its current code. Unit tests and the full suite are still CANNOT-EVALUATE until Betty is back.
- 2026-11-22 Betty still unreachable. RUNNING: wklec7zqs (wf_9af42a6c-0bd), the M5 consult adapters. It writes NEW files only (src/consult/, test_consult_adapters.py, evidence/m5-adapters/) and is the sole writer. It builds:
  - agy_ask.py: ACP client with a fresh absolute gemini dir per call, read-only permissions, receipts, non-zero exit on CANNOT-EVALUATE;
  - claude_ask.sh: off-bus, read-only, hooks disabled.
  Live smokes (a)–(d) on the Mac, then an opus verify. coord.py and writeguard.py stay frozen until the Betty verify of round 2.
- 2026-11-22 wklec7zqs DONE (evidence/m5-adapters-result.json): agy_ask.py, claude_ask.sh, README, fake-host tests (not run). Smokes:
  - (a) agy review: PASS.
  - (b) agy write attempt: no write was attempted; the rejected `ls` led to exit 3.
  - (c) claude review: PASS, mcp [] and tools Read/Grep/Glob.
  - (d) claude write: PASS, file unchanged.

  The opus verify found 2 blocking defects:
  - the agy permission filter matches read words as substrings, so a "review" title grants write-capable tools;
  - claude_ask returns ANSWERED on a crash or truncated stream.

  It also found 9 important and 3 minor defects, among them:
  - stopReason is ignored;
  - outputs are stale or partial;
  - the refusal path creates a dir in ~/.gemini;
  - hook evidence is vacuous without --include-hook-events;
  - project settings can re-add plugins;
  - files_read overstates;
  - agy's edit-permission prompt is UNPROVEN live;
  - the tests don't constrain the flags.
- 2026-11-22 RUNNING: wj4oslybd (wf_69a02a1e-1a9), adapter repairs D1–D8 (new files only) and live smokes:
  - L1 decisive: does agy ask permission before an edit in a fresh gemini dir;
  - L2;
  - L3/L4 Claude hook positive and negative control with --safe-mode;
  - L5.
  Then a targeted opus re-check.
- 2026-11-22 wj4oslybd DONE (evidence/m5-adapters-r2-result.json, evidence/m5-adapters-r2/). Repairs D1–D8 done; the opus re-check closed all 14 findings and found 2 minor arg-order gaps, which the lead fixed:
  - claude_ask pre-scans --out;
  - agy_ask uses allow_abbrev=False;
  - 2 tests were added, not run.

  Live smokes, all PASS with host hashes unchanged (5 runs):
  - **L1, decisive:** in a fresh gemini dir, agy sends session/request_permission kind=edit (title "File <relpath>", no tool name) BEFORE editing. agy_ask rejects it and the file is byte-identical. agy_ask is read-only in practice; the policy relies on ACP kind alone.
  - **L2:** agy review ANSWERED, end_turn, files_read set.
  - **L3:** a project SessionStart hook fires under plain `claude -p --setting-sources project`; positive control, 6 hook events.
  - **L4:** the same workspace under claude_ask (`--safe-mode --restricted --strict-mcp-config --mcp-config {} --tools Read,Grep,Glob --include-hook-events`): no hook, no MCP, builtin plugins only.
  - **L5:** claude review ANSWERED.
- **M5 status:** adapters live-smoke-validated. Unit tests are CANNOT-EVALUATE until Betty.
- **M6 HOLD:** wiring wake routes touches bridge_link.py, frozen until the Betty run plus the opus verify of round 2. Remaining unknown: does a live Codex TUI pick up `codex queue` items? This needs a scratch Codex TUI in a cmux pane, which is a visible action in the owner's terminal.

## [2026-11-23] Betty back: round-2 full suite run and reconciled by test id
- Betty reachable again (ssh exit 0). Full suite `coordination/tests tests` on venv-fastmcp: **402 passed, 1 failed, 3 skipped** (evidence/betty/round2-full-suite.txt).
- Reconciled by test id against `--collect-only` of base 91546b3 (evidence/betty/base-91546b3-collected-ids.txt, 272 ids; result in evidence/betty/round2-reconcile.txt):
  - every base id is present; 269 pass, 2 skip (waitpath node tests, node not installed — same skip as base), 1 fails (below);
  - 133 new ids (test_writeguard 63, test_triangle_advisory 37, test_consult_adapters 33), **all pass**, including the 2 lead-added arg-order tests and the W1–W4 tests; 1 consult test skips on Linux by design (macOS fixed-base check).
- The 1 failure, `test_jobs.py::test_join_stops_on_paused_execution_and_on_stop_policy_is_honored` (line 292, `'launched' == 'running'`), is a **pre-existing race in the base test**, not a regression: 10 reruns each on Betty gave base 91546b3 8 pass / 2 fail and round-2 9 pass / 1 fail, same assertion (evidence/betty/flaky-base-91546b3-x10.txt, flaky-round2-x10.txt). Cause: the test waits for job `stopme` to reach running but never for `keep`, so a fast pause can see `keep` still `launched`. Our only jobs.py change is the `authority_op` gate (one env read before launch). Backlog: fix the test to wait for both jobs; not in triangle scope.
- Unit tests and full suite for round 2 + adapters: **PASS (offline-tested)**. Next: one opus/high targeted verify of round-2 repairs (A1–A3, W1–W4) and the adapter tests, then M6.

## [2026-11-23] Round-2 targeted verify (opus/high, workflow w3sjhn2l3, run wf_a405c417-407)
- Result: evidence/round2-verify-result.json; reproducers + Betty outputs: evidence/round2-verify/ (21 probes; run 2: 12 failed = defects reproduced, 9 passed = controls/closed items).
- Closed: A1, A2, A3, W1, W3, W4 (W5 by the 11-23 full run). **W2 partial**: quoted operators and newlines fixed; backslash-escaped operators (`echo \> f`, `\;`) still parsed as operators.
- New, same mechanisms (all reproduced on Betty against a deny control):
  - N1 important: an attach with a FRESH observer id but another participant's host/session overwrites the native-session selection (coord.py:567-575 `_write_selection` is unconditional; the :522 guard keys on participant id only). The executor's SessionStart/compaction hook then resolves to the observer. Works cross-task and repeats after the executor re-attaches. Pre-existing in non-advisory mode too.
  - N2 important: shell tokenizer misses command boundaries for glued operator runs (`$(cat a);git add f`, `(cat a)&&…`), heredoc bodies (newline→';' parses them as commands), and backslash escapes. Effect: false freshness, so a stale overwrite of a never-read file is allowed.
  - N3 minor: shell-overwrite post-records resolve against payload cwd, ignoring cd; a cd inside `( … )` leaks to later commands.
  - Note: FEATURES.md:34 says antigravity "ignores" a caller host_yield_s — stale since A3.

## [2026-11-23] Round 3 repairs
- W2/N2/N3 (writeguard.py): `tokens()` splits punctuation runs by longest match against a fixed operator list, unsplittable/implausible runs become an UNKNOWN operator; backslash-escaped characters count as quoted (escaped operators are literal words). New gated `shell_effects(cmd, cwd)` returns resolved (reads, overwrites) or None (record nothing) for subshells/`$(`, braces, backticks, heredocs/herestrings, UNKNOWN operators, builtin/command/eval/source/./exec/pushd/popd, non-literal cd, and literal cd combined with `|`, `&`, `||`. The PostToolUse shell branch records only from `shell_effects`; overwrite targets use the same effective cwd as reads. The pre-warning stays loose.
- N1 (coord.py attach): in an advisory process, an attach whose (host, session) is selected by another live participant is refused with `advisory_process_refused` before any write, also for fresh participant ids and across tasks; exceptions for a missing/detached/moved holder and for the same advisory observer identity. Non-advisory unchanged.
- Docs: FEATURES.md (agy min yield, exact A1 guarantee, non-advisory limit), WRITE_GUARD_SPEC.md (real post-branch behaviour, "Round 3 fixes").
- Tests: verifier reproducers ported into test_writeguard.py and test_triangle_advisory.py; four exact-list asserts restated as safety properties; per-fixture record test added.
- Betty: full suite 483 passed, 3 skipped, 1 failed = the known test_jobs flake (evidence/betty/round3-full-suite.txt). Not committed. Live smoke by the lead next.
- [2026-11-23] Lead check of round 3 (workflow wfx5ukdh0, result evidence/round3-repair-result.json): full suite reconciled by id (evidence/betty/round3-reconcile.txt): 483 passed, 1 failed = the known pre-existing jobs race, 3 skipped (2 node, 1 macOS-only); all 403 round-2 ids present; 81 new ids, all pass. Live re-smoke R-c on round-3 writeguard.py (evidence/m3-smoke/, 1 Codex run, inline per-run hooks): first Add File denied ("Command blocked by PreToolUse hook: WRITE GUARD"), `cat existing.txt` showed original-existing, retry landed NEW-CONTENT — the shell read signal still unblocks Codex after the gate. UNCOVERED (accepted, missed or conditional records only): `cd sub && cat f | head` records nothing; `false && cat b` still records b. Next: one narrow opus re-check of W2 + N1–N3.

## [2026-11-23] Round-3 targeted verify (opus/high, workflow wtjzyxvuz, run wf_3b76c79d-f53)
- Result: evidence/round3-verify-result.json; reproducers + Betty output: evidence/round3-verify/ (18 failed = defects reproduced, 181 existing passed).
- W2, N2, N3 closed against their literal requirements. **N1 partial.**
- New important (introduced by the round-3 check): N1 bypass. `still_on` (coord.py:560-563) compares the holder with the CALLER's host/session, not with the stored selection's key. (a) `session='h.<sha16>'` (the hashed file name of a victim whose session id contains '/') captures natively on Betty; the Mac store holds 6 such hashed codex selections. (b) host kind 'Claude' on the case-insensitive Mac store erases, then a fresh id captures (emulated on Betty).
- New minor, left from round 2, all false-freshness direction: cd behind if/then, for/do, time, ! or after a failed &&; `&>>`/`<>` redirect targets recorded as reads, syntax-error commands record; `$'..\'..'` desynchronises quote state.
- Weakened assertion: the round-2 W4 hook-chain reproducer (unread chmod-0 file denied; read-then-chmod-0 allowed) was not ported.
- Process note: the verifier ran a read-only `ls -d` on the live store's sessions directory to check case-insensitivity. That is a read, not a write, but the rule is not to touch the live store; future verifier briefs must name the case-sensitivity probe target (a scratch dir).

## [2026-11-23] Round 4 repairs
- Why: four review rounds each found a new shell form that slipped past a denylist. Round 4 replaces it with a positive definition of a trusted command; anything else records nothing (docs/triangle/WRITE_GUARD_SPEC.md, "Round 4: trusted command shapes", including the UNCOVERED list).
- writeguard.py: `shell_effects` is now an allowlist validator (raw-text pre-gate; separators `; && || |` + newline; redirects `> >| &> 2> >> &>> 2>> < >&`; simple commands with a literal program word; reserved words and state-changing builtins gated; `cd` only as a leading `cd <dir> &&` chain; reads/overwrites recorded only up to the first `&&`/`||`; reads only from READ_CMDS segments with no file redirect; overwrites only from a single overwrite redirect). Deny reasons now carry the unlock form (Codex: standalone `cat <path>`, not after && or ||; Antigravity: read_file). The post branch also requires a writable file before recording an overwrite.
- Real-shell differential oracle: coordination/tests/test_writeguard_oracle.py (seeded, 1500 commands per shell, bash and `zsh -f`, wrapper scripts log real reads, content snapshots check real writes). Result: 0 violations, coverage 0.289 (floor 0.15), 372 read checks and 88 overwrite checks per shell; the same corpus against the old loose parser gives 134 violations (the oracle can fail). Extra seeds 11/12/13 at 5000 commands per shell: 0 violations. The oracle found one real defect during development (a read of a path the same command later creates by a redirect); fixed. Output: evidence/betty/round4-oracle.txt.
- coord.py: one helper, `_advisory_selection_guard`, runs in advisory processes before any write in BOTH `attach` and `select_session`: host kind must be exactly claude/codex/antigravity; the exact selection file and every `_sid_match` fallback record are examined; a record held by another live participant is refused unless the holder is missing, detached, or off THAT RECORD's own host/session, or is an observer on the caller's host+session. Non-advisory behaviour unchanged.
- Tests: the 18 round-3 reproducers restated as safety properties in test_writeguard.py / test_triangle_advisory.py; N1 hashed-name and host-case captures are native tests; the round-2 W4 hook chain is ported; unlock-hint tests; trusted-shape and outside-grammar tables.
- Betty (2026-11-23): targeted writeguard + advisory 319 passed; full suite `coordination/tests tests` 627 passed, 3 skipped, 0 failed (the known test_jobs flake did not fire this run). Output: evidence/betty/round4-full-suite.txt. Not committed. Live smoke by the lead next.
- Note for the lead: one zsh oracle command in an extra 5000-command run hit the 10 s timeout (1 of 5000, not a violation).
- [2026-11-23] Lead check of round 4 (workflow wghft0zf6, result evidence/round4-repair-result.json): full suite reconciled by id (evidence/betty/round4-reconcile.txt): 627 passed, 0 failed, 3 skipped; all 484 round-3 ids present; 143 new ids. Oracle (evidence/betty/round4-oracle.txt): bash and zsh -f, seed 20261123 ×1500 plus seeds 11/12/13 ×5000: 0 violations, coverage 0.287–0.297, 1 zsh timeout (unchecked, not counted as pass); loose round-3 parser on the same corpus: 134 violations. Live re-smoke on round-4 writeguard.py (evidence/m4-smoke/, 1 Codex run): Add File denied with the unlock hint ("To unlock: run a standalone read such as `cat <path>` …"), `cat` showed original-existing, retry landed NEW-CONTENT. **Write guard and advisory selection guard: offline-tested and oracle-checked; live-smoke-validated on round-4 code for the Codex path only.** The Antigravity stale-write path (deny text changed) and the advisory attach path (selection guard changed) were last run live on round-2 code; they are re-run in the M7 drill before any owner claim of live validation. No fifth adversarial round; the M7 Codex diff review is the remaining independent look. Backlog: deny reason ends "retry.." (double period); the jobs-test race; generator retuned once to clear the 0.15 coverage floor (violation check untouched).

## [2026-11-23] M6 probe P3: headless Codex wake (workflow wa5li091i + lead run 3; evidence/m6-p3/, m6-p3-result.json)
- Isolation proven from the debug log before any message existed and on every wake: `--ignore-user-config` plus `-c mcp_servers.mycelium-coord.{command=<dev launcher>, env.MYCELIUM_COORD_DIR=<scratch>, default_tools_approval_mode="approve"}` → spawned servers "codex_apps, mycelium-coord" (dev launcher only); no plugin coord server, no bridge. Narrow per-server approval; no bypass flag.
- Run 2 (prompt "read with coord_inbox and act"): the woken turn read the message but the store stayed `persisted` — **a read via coord_inbox leaves no store trace**, so it is NO_DELIVERY under the frozen criterion.
- Run 3 (prompt adds "acknowledge it with coord_ack"): woken turn called coord_inbox then coord_ack; store state `acknowledged`. **VERIFIED_DELIVERY.** 3 of 3 Codex runs used.
- Consequence for the design: the wake prompt REQUIRES coord_ack, and `accepted` = the store shows acknowledged (or a cursor past the message) for that recipient after the wake. The Codex exec-resume route is built.

## [2026-11-23] M6 wake build
- Added wake.py (registry, gates, claude/codex/antigravity routes, receipts), CLI `wake`, MCP `coord_wake`, feature `codex-exec-wake` (model.py), tests coordination/tests/test_wake.py (33 tests, fake codex executable, no real codex/agy/claude run), `coord_wake` added to the advisory-hidden tool assertions in test_triangle_advisory.py. bridge_link.py and writeguard.py not touched by M6. Docs: FEATURES.md "Wake (M6)", M6_WAKE_DESIGN.md "Implementation". Not committed.
- Betty (2026-11-23): test_wake.py 33 passed; bridge + advisory + wake 114 passed; full suite `coordination/tests tests` 660 passed, 3 skipped (2 node, 1 macOS-only), 0 failed (627 round-4 + 33 new). Output: evidence/betty/m6-full-suite.txt. The known test_jobs flake did not fire.
- Not done by design: no live Codex run (lead's smoke); real Codex queue DB schema unverified (guard fails closed as queue_unknown).
- [2026-11-23] Lead check of M6 build (workflow wsl3eopd4, result evidence/m6-build-result.json): suite reconciled by id (evidence/betty/m6-reconcile.txt): 660 passed, 0 failed, 3 skipped; all 627 round-4 ids present; 33 new (test_wake.py). Live smoke via the new code path (evidence/m6-smoke/, 1 Codex run): fresh scratch task t-wake3 created with `--feature codex-exec-wake`, cx attached with host wake=headless, message m-wake3 (kind task); `mycelium-coord wake` → outcome **accepted**, route codex, state persisted → acknowledged, exit 0, 13.5 s; a second wake returned the stored receipt (idempotent_replay, no second run). The queue guard read the real ~/.codex/queue_1.sqlite read-only and passed. Isolation for this run rests on args identical to P3 (proven there from the debug log) plus the ack landing in the scratch store; wake.py records only stderr byte counts, so this run has no debug excerpt (backlog: keep a bounded stderr tail in the receipt). **M6 status: Claude route = existing bridge (unchanged); Codex headless route live-smoke-validated; Antigravity = not_supported; interactive Codex = next-turn only.** P2 (live-TUI queue pickup, a visible scratch Codex window in the owner's cmux) is deferred to an owner OK in the final report: it can only upgrade interactive Codex from next-turn to wakeable.
- [2026-11-23] **M6 queue-guard fix (lead, single writer; evidence/m6-queue-check/).** A check on a scratch COPY of the real ~/.codex/queue_1.sqlite (real schema saved as real-queue-schema.sql) showed the guard counted rows in every table with a thread column. That includes `queued_thread_revisions`, which keeps a permanent row per thread after its items drain, so any thread that had ever queued anything was refused forever (fail-closed false refusal; the earlier "pass" on the real DB was absence, not evidence). Fix: positive rule — only table `queued_items` with column `thread_id` counts; any other layout → `queue_unknown` (refused). The test fixture now mirrors the real schema and triggers, with a regression test for a drained thread and one for an unrecognised layout. Betty: test_wake.py 34 passed (evidence/betty/m6-queuefix-test_wake.txt). Scratch re-check with a stub codex: drained thread → wake proceeds (stub invoked once, outcome uncertain, no ack); queued row → `refused:thread_has_queued_items`, stub not invoked. Other M6 checks: a `review_request` renders `actionable: true` to the recipient (so a woken Codex should reply); `consult_request` and `report` are non-actionable by design, so a woken recipient may not answer a consult (reply-expectation gap → owner report). Codex runs with cwd = the recipient's stored worktree (wake.py:308).

## [2026-11-23] M7 drill + Codex review (workflow wp7s6vw5w; evidence/m7-drill/, m7-drill-result.json, m7-codex-review/, m7-codex-review-result.json)
- **Drill: all PASS on a scratch store with dev code; host config hashes unchanged; Codex 2/3, agy 3/5 runs.**
  - D1: Codex wake accepted (30.5 s); cx's report to lead (reply_to set) names the planted off-by-one at sample.py:4; cx also sent agy-drill a consult_request.
  - D2: Antigravity, via ACP with the dev coord MCP in advisory mode, attached as observer, acked both consults, and replied to lead and to cx. All 8 coord-tool permission requests were granted narrowly, matched by tool name: they arrive as `mcp_mycelium-coord_coord_<tool>`, kind `other`, so agy_ask's kind allowlist would reject them. agy had no file access, so its answers prove the plumbing, not the review.
  - D2b: advisory attach of a fresh observer on cx's session → advisory_process_refused (names the holder); host 'Antigravity' → refused; selection unchanged.
  - D3: agy stale write_file denied on round-4 code with the read_file unlock hint; after a re-read all three lines are present. **The Antigravity write-guard path and the advisory attach path are now live-validated on current code.**
  - D4: observer_not_wakeable, kind_not_wakeable, and the advisory-process refusal, all as designed.
  - **All six directed edges have a message in the store:** claude→codex m-rev1, codex→claude m-rev1-cx-report, codex→agy m-rev1-cx-consult, claude→agy m-cons1, agy→claude m-cons1-agy-report, agy→codex m-cons2-agy-report.
- **Codex review of wake.py (off-bus codex_ask, gpt-5.6-sol medium, complete). Disposition:**
  - (1) a stored thread id can be read as a Codex option — ACCEPTED: positive thread-id format;
  - (2) MYCELIUM_WAKE_CODEX_ARGS substring filter allows equivalent bypasses — ACCEPTED: allowlist the extra-arg shape;
  - (3) the Claude route skips the wrapper's pause/expiry gate for non-actionable kinds — ACCEPTED: apply the gate to every route;
  - (4) the participant record goes stale between checks and launch — ACCEPTED: re-read right before Popen;
  - (5) the timeout is unbounded or non-finite and the child is not killed on non-timeout exceptions — ACCEPTED: finite range, killpg on any exception.
  - Noted, not repaired: the queue check-to-launch race (no cross-process sync with Codex; documented limit); Claude `accepted` = bridge delivered record (consistent with frozen "delivered").

- M7 repair (2026-11-23): five accepted Codex findings fixed in wake.py (UUID thread id, args allowlist, execution gate on every route, recipient re-read before Popen, timeout range + killpg on exception); tests added in test_wake.py; Betty evidence docs/triangle/evidence/betty/m7-repair-full-suite.txt. writeguard.py and bridge_link.py untouched.
- [2026-11-23] **M7 repair closed (workflow wznwm1elr + lead).**
  - Sonnet writer repaired all 5 findings as positive rules, with tests. Betty full suite: 690 passed, 0 failed.
  - One off-bus Codex verification (evidence/m7-codex-verify/): findings 1, 2, 3 and 5 are CLOSED. Finding 4 was OPEN: the recipient check was not the last step before launch, because `get_cursor` ran after it.
  - Lead fix: the recipient re-read and `Popen` now run together under the task lock. That lock is re-entrant per thread, and attach/detach take it. It is released right after launch.
  - New tests:
    - the verifier's exact reproducer (the participant changes during `get_cursor`: detached, session or wake marker), refused with zero launches;
    - a lock test that probes from a second thread, because the lock is re-entrant within a thread.
  - Betty full suite: **694 passed, 3 skipped, 0 failed** (evidence/betty/m7-lockfix-full-suite.txt). Reconciled by id against m7-repair: none lost, 4 new.
  - **Live post-repair smoke** (evidence/m7-postrepair-smoke/), same scratch thread and drill isolation args, which all fit the new allowlist:
    - wake `accepted` in 17.8 s, state acknowledged;
    - cx report m-post1-cx-report with reply_to m-post1;
    - the replay returned the stored receipt (`idempotent_replay: true`) with no second run;
    - host config hashes unchanged.
  - Codex runs used in M7: 3 of 3.
  - Finding 4's closure rests on the reproducer test plus this smoke, not on a second Codex verification. The default allowance is one review and one verification.
- [2026-11-23] **Advisor checks before the owner report (lead).**
  - A task-lock timeout before launch raised StoreError, which is not a ProtocolError. The outer handler recorded it as permanent `uncertain`, so the message could never be woken again even though nothing ran. Fixed: it is now retryable `unavailable` with error task_lock_timeout, and the second wake is accepted. Test added.
  - The lock test now asserts the `lock_timeout` code, not any exception.
  - attach and detach are the only writers of participant records, and both take the task lock.
  - FEATURES.md queue text was corrected to the positive rule.
  - Evidence scrub (token shapes, owner email) over the m6-queue-check and m7-* evidence: clean.
  - Betty full suite: **695 passed, 3 skipped, 0 failed** (evidence/betty/m7-lockfix2-full-suite.txt). Reconciled by id: none lost, 1 new.
  - The happy path of the launch is unchanged since the post-repair live smoke, so no new live run was made.
- **Status by surface, for the owner report.** The surfaces actually run:
  - Claude: the dev CLI. As a wake recipient, offline only.
  - Codex: headless exec / exec resume.
  - Antigravity: an ACP driver in a clean gemini dir.
  - No desktop app, IDE or interactive TUI was run.
  - Write guard: Codex and agy are live on current code. Claude's Write/Edit rely on its native stale check (m2-probes). The Claude Bash hook is a template and was never run live.
  - D2 pre-attached agy-drill, so the per-conversation observer id is a proposal, not tested.
  - Finding 4 was closed by the lead and not re-verified by Codex.
- [2026-11-23] **Review repair: Mycelium review of the working tree, findings F1 to F14 (single writer; report .living/outputs/reviews/2026-11-23-triangle-working-tree.md).**
  - F1 (major, write guard): freshness only for content the session saw. A shell tool's PRE hook snapshots each candidate read path (keyed by the host's call id, which the fixtures show in both PRE and POST; fallback hash of host, session, tool, command); POST records a shell read only if a snapshot exists and the file is unchanged, and records the snapshot signature. No snapshot records nothing. Shell overwrites are recorded only when every program in the command is in the fast set. Native reads use the same equality when a PRE snapshot exists. Snapshots are consumed at POST, expire after a day and are capped at 64 per session; failures stay fail-open. All three hook templates register a PRE hook for the shell tool (tested). Spec: WRITE_GUARD_SPEC.md "Round 5", matcher text now quotes the templates (closes F9).
  - F2 per-thread run lock (`refused:thread_wake_in_progress`); F3 Codex patch records only on success; F4 queue state in every codex receipt and an absent default DB is unknown when a sibling queue_*.sqlite exists; F5 agy_ask and claude_ask answer-write failure is `ANSWER-WRITE-FAILED` exit 7; F6 claude_ask `--timeout` must be a positive integer; F7 MCP/CLI docstrings; F8 stale "never ran on Betty" text replaced; F10 README; F11 riteguard.py deleted (nothing referenced it); F12 advisory ack/set_cursor/send require an attached observer; F13 dead list removed; F14 one `model.HOST_KINDS`.
  - Owner decision implemented: `reply_expected: true` is derived for review_request and consult_request in inbox, resume and read_message (never stored). Reply with kind report, reply_to the request.
  - F12 check: no src/consult adapter uses the coordination core. Two advisory tests sent a setup message as the supervisor inside an advisory process; they now send it before the advisory env is set (setup only, no product flow).
  - Ported every verifier reproducer into coordination/tests/test_review_repairs.py. SL-1(a) and SL-1(b) are specified behaviour (review Key decisions), so their ports pin that behaviour; the SL-2 port keeps the owner-fixed "absent override is clear" and checks the receipt instead.
  - Oracle: `WG_ORACLE` coverage 0.289 before and after (1500 commands, 0 violations, floor 0.15 unchanged). New hook-level oracle drives PRE, a real shell, POST: 106 reads credited when unchanged, 0 credited after another session's rewrite (bash and zsh).
  - Betty full suite `coordination/tests tests`: **769 passed, 3 skipped, 0 failed** (evidence/betty/review-repair-full-suite.txt). Reconciled by id against m7-lockfix2 (695): 0 lost, 74 new (evidence/betty/review-repair-reconcile.txt). A first full run had 1 failure in a new test (my wrong assertion on a send result); fixed and rerun. The known flake did not fire. bridge_link.py untouched by this repair.

## [2026-11-23] Mycelium review → repair → commit (owner: "run a mycelium review … use your judgement for the decisions")
- **Review:** Science/.living/outputs/reviews/2026-11-23-triangle-working-tree.md (validator OK). 5 finders + 5 adversarial verifiers. 17 raw → 14 findings: **1 major** (F1: a shell read was credited at POST time, so an edit made by another session during a long command counted as seen) and 13 minor. Evidence: evidence/review-2026-11-23/.
- **Repair** (workflow w4oz3t7df; sonnet writer, opus targeted verify, live smoke):
  - all 14 repaired, plus the owner-delegated `reply_expected` flag;
  - Betty full suite 769 passed, 0 failed, 0 ids lost;
  - verifier: 14/15 CLOSED, with a revert check on F1, F2, F3 and F12;
  - live smoke (Codex 2 runs, agy 2 runs, host config hashes unchanged): S1 Codex deny → cat → retry PASS; **S2 live F1 race PASS** (`cat notes.md && sleep 25` with an external append during the sleep; the later Add File was denied and the append survived); S3 agy stale write_file denied, then landed after a re-read, PASS.
- **Lead follow-up** on the verifier's open item and new issues (verifier reproducers ported as coordination/tests/test_repair_verify_nx.py):
  - NX3: full `resume` renders reply_expected;
  - NX1: prune() expires abandoned PRE snapshots and removes emptied pending dirs;
  - NX2: `tail -f/-F/--follow` is not a fast program;
  - NX4: a launched-then-error wake receipt keeps the queue state.
  - Betty full suite: **777 passed, 3 skipped, 0 failed**, 0 ids lost, 8 new (evidence/betty/review-nx-full-suite.txt). Oracle bash/zsh 0 violations, coverage 0.289; hook oracle credited_changed=0.
- **Root cause of the stray files.** The empty `riteguard.py` and `ake.py` came from BSD `sed -i 's/…/…/' writeguard.py`: without `''`, the script is taken as the backup suffix and the file name as the script, whose `w` command creates `riteguard.py`. Both files were removed.
- **Decisions taken under owner delegation:**
  - commit LOCAL on feat/triangle-coord, no push, raw evidence left unstaged;
  - reply_expected: done;
  - P2 visible-TUI probe: not run;
  - live Antigravity registration: no, because the proposal's step-3 send capability token is not built;
  - live install: not this pass. It needs a merge into ~/tools/sporedrive-release, owner trust approval of each new Codex hook (hooks.state), and a per-tool Codex approval for wake (a server-wide `approve` would open the limit/unpause tools to every Codex session).
