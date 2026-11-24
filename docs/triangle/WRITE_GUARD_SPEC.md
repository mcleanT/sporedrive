# Write guard — spec (fixed before implementation, 2026-11-22)

**Owner request:** "prevent agents from 2 sessions from overwriting eachother".

**Design:** refuse STALE whole-file overwrites (optimistic concurrency). This mirrors Claude Code's built-in rule. It is not file ownership. Decision record: Science/.living/decisions.md [2026-11-22].

**Evidence:**
- evidence/m2-probes.json: Claude 6 runs, Codex 6, agy 5.
- Pre-checks: scratchpad/m2pre, 4 runs; summarised in BUILD.md.

## Verified host facts this spec relies on
| Host | Whole-file overwrite tool | Native staleness check | Hook payload path | Read signal |
|---|---|---|---|---|
| Claude 2.1.330 | Write (also Edit) | YES. Refuses if changed since Read, and runs BEFORE hooks | tool_input.file_path (abs) | Read |
| Codex 0.171.0 | apply_patch `*** Add File:` over an existing path, and `*** Move to:` an existing path | Update File: YES (context lines). **Add File over existing: NO — silent overwrite (verified)** | tool_input.command = raw patch text. Paths are relative to payload.cwd or absolute. Regex `^\*\*\* (Add File\|Update File\|Delete File): (.+)$` and `^\*\*\* Move to: (.+)$` | shell only (`cat`, `sed -n`, …) |
| agy 1.5.14 | write_file (args Path, Content, Overwrite) | **NO — lost update verified** | .toolCall.args.Path (abs) | read_file (Path), hooks fire Pre+Post (verified) |
| all | shell `>` / `tee` / `cp` / `mv` | NO (Codex clobber verified) | command string only | — |

**Identity per host:**
- Claude: `session_id`. Subagents carry the parent's id plus `agent_id`. Resume keeps the id.
- Codex: `session_id`, which is the thread id. Resume keeps the id.
- agy: `conversationId`. Resume keeps the id.

**Deny output per host (all verified to block, and the model sees the reason):**
- Claude: stdout `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":R}}`.
- Codex: the same JSON, which works on apply_patch.
- agy: stdout `{"decision":"deny","reason":R}`; allow is EMPTY stdout on PreToolUse (`{}` on PostToolUse).
  Live probe 2026-11-24 (agy 1.5.x, ACP and `-p`, scratch workspaces, host config unchanged): a PreToolUse hook that
  prints `{}` BLOCKS the call ("denied by pre-tool hook") on both routes; one that prints nothing leaves agy's own
  permission flow in place (the write landed after an approval on ACP and directly on `-p`). agy documents
  `{"decision":"allow"}` as "Automatically allow the tool execution", i.e. a grant, which the guard must never make
  (Mycelium's own agy adapter installs no PreToolUse handler for the same reason). On ACP an allow still reached the
  permission prompt; the IDE/App were not probed.

**agy is FAIL-CLOSED.** Verified 2026-11-24 on `-p`: a hook whose interpreter is missing exits 127 and the matched
tool call is blocked. The template therefore matches only the tools the guard handles. Non-JSON output and timeouts
were not probed separately; treat them as blocking.

## Behaviour
- **Signature** of a path: `(size, mtime_ns, sha256)`. sha256 is computed only when size ≤ 16 MiB; otherwise it is `None` and size+mtime_ns are compared. Paths are keyed by `os.path.realpath`. For a path that does not exist yet, resolve the realpath of its parent and append the basename. Relative paths resolve against the payload cwd.
- **Session record:** `$MYCELIUM_WRITE_GUARD_DIR` (default `~/.local/state/mycelium/write-guard`), then `sessions/<host>/<session_id>/<sha256(realpath)>.json` = `{path, sig, event, tool, at}`.
  - Writes are atomic: tmp file plus `os.replace`.
  - The store is outside the coordination store; it never touches `~/.local/state/mycelium/coordination`.
- **Last-writer ledger** (only for naming the other session in messages): `last_writer/<sha256(realpath)>.json` = `{path, host, session_id, at}`. It is updated on successful writes.
- **Record (PostToolUse):** after a read tool or a write tool, store the path's current signature in the session record.
  - Claude: Read, Write, Edit.
  - Codex: every path in an apply_patch, plus shell read commands.
  - agy: read_file, write_file, edit_file.
  - Shell read commands are best-effort: argument files of `cat`, `head`, `tail`, `sed -n`, `nl`, `wc`, `less`, `bat`.
- **Check (PreToolUse)** applies to whole-file overwrites only: agy write_file, and Codex Add File or Move-to onto an EXISTING path.
  - If the target does not exist → allow.
  - Else if the current signature equals the session's recorded signature → allow.
  - Else → **deny**, with reason: `WRITE GUARD: <path> has changed since this session last read or wrote it (or this session never read it)<; last written by <host> session <id8> at <HH:MM ET>>. Read the current file again and make your change on top of it — do not overwrite another session's work.`
  - For Codex, add: `Prefer an Update File hunk, which applies on top of the current content.`
  - A multi-file apply_patch is all-or-nothing: if any path is denied, deny the call and list the paths.
- **Not checked (rely on native checks):** Claude Write/Edit, Codex Update File, agy edit_file (substitution; a stale target fails natively). Their Post events still record.
- **Shell overwrite** (`>` but not `>>`, `tee` without `-a`, `cp`/`mv` onto an existing destination) is detected best-effort from the command string.
  - It is **never denied**.
  - Claude and Codex get a warning via additionalContext. agy only logs it.
  - Shell writes are reported as UNCOVERED.
  - The PRE warning is loose and never denies. The POST branch, however, records freshness AND the last-writer ledger for shell reads and shell overwrite targets, so it is gated: see "Round 3 fixes".
- **Modes:** `MYCELIUM_WRITE_GUARD=enforce` (default), `warn` (never deny, warn or log instead), `off` (exit immediately with allow).
- **Fail-open-and-logged inside the script.** Every path is wrapped. On any internal exception, emit the host's ALLOW output and append a line to `<dir>/errors.log`. Internal soft deadline: 2 s, so skip hashing when a single file read would exceed it. The script is stdlib only, has no package imports, and its startup time is under 150 ms. Registrations must set an explicit hook timeout of 10 s. agy fail-closed then applies only if python itself cannot start, so registrations use an absolute interpreter path.
- **Housekeeping:** on about 1 in 200 invocations, prune session dirs not touched for 14 days, at most 50 dirs per run.
- **CLI:**
  - `python3 writeguard.py hook --host claude|codex|antigravity --event pre|post` reads the payload from stdin.
  - `python3 writeguard.py status [--path P]` shows the session records and last writer for a path.
  - `python3 writeguard.py selftest` runs offline checks.

## Location
`coordination/mycelium_coord/writeguard.py` must be runnable standalone. Tests go in `coordination/tests/test_writeguard.py`. Payload fixtures are taken from the real probe logs and sanitised:
- scratchpad/m2/{claude,codex,antigravity}
- scratchpad/m2pre/{agy,codex}

Registration templates are NOT installed:
- `docs/triangle/proposed/writeguard-hooks/claude-settings-hooks.json`: PreToolUse matcher `Bash` (warn, and the Round 5 PRE snapshot); PostToolUse matcher `Bash|Read|Write|Edit|MultiEdit` (record).
- `.../codex-hooks.json`: PreToolUse matcher `apply_patch|Bash`; PostToolUse matcher `apply_patch|Bash` (needs trust).
- `.../antigravity-hooks.json`: PreToolUse and PostToolUse matcher `read_file|view_file|write_file|write_to_file|edit_file|replace_file_content|multi_replace_file_content|run_command` (top-level `writeguard` group). Narrowed from `*` on 2026-11-24: agy blocks a call when a hook cannot run, so a broken interpreter under `*` would block every tool.
- Every template registers a PRE hook for the host's shell tool (Claude `Bash`, Codex `Bash`, agy `run_command`). Since Round 5 this is load-bearing: the deny, `cat`, retry unlock only works because the PRE hook snapshots the file before the `cat` runs. A test (`test_templates_register_a_pre_hook_for_the_shell_tool`) reads the three templates and checks it.

## Acceptance (checked by file content)
1. **Unit tests on Betty:**
   - path extraction per host from real fixtures (incl. relative paths, Move to, multi-file);
   - the signature rules (a)–(c) below;
   - deny formats;
   - modes;
   - fail-open on an injected exception;
   - shell read and overwrite parsing (`>>` never flagged);
   - two concurrent writers of the same session record stay valid JSON.
2. **Live smokes on the Mac** (scratch, hooks registered per run or per workspace, ≤6 model runs):
   - (a) agy reads c.txt; an external writer appends EXTERNAL-LINE; agy write_file is **denied**, and the file still has EXTERNAL-LINE. After it re-reads, its write lands and **both** lines are present.
   - (b) agy write_file to a new file is allowed. Rewriting its own just-written file is allowed.
   - (c) Codex Add File over an existing, unread file is **denied** and the file is unchanged. After `cat` of the unchanged file, Add File is allowed.
   - (d) A Codex shell `printf > f` over a changed file is NOT denied, and a warning is logged. Report it as uncovered.
3. Full suite on Betty: no new failures vs baseline.

## Results
Implemented 2026-11-22. Nothing was installed or registered globally. Hooks were registered per run or per workspace only.

| Item | Result | Evidence |
|---|---|---|
| 1. Unit tests on Betty (venv-fastmcp) | PASS, 46 passed | evidence/betty/writeguard-unit.txt |
| 2a. agy stale overwrite denied, re-read, both lines present | PASS | evidence/m2-smoke/{c.after_external.txt,c.txt,events.log,turn2.out} |
| 2b. agy new file allowed, own rewrite allowed | PASS | evidence/m2-smoke/{new.txt (v2),turn1.out}; no deny logged for turn 1 |
| 2c. Codex Add File over existing unread file denied and unchanged, then allowed after cat | PASS | evidence/m2-smoke/{run1.jsonl,last.txt,existing.before,existing.txt,events.log} |
| 2d. Codex shell `>` over a changed file not denied, warning logged, reported as UNCOVERED | PASS (uncovered, by design) | evidence/m2-smoke/{g.txt,events.log} |
| 3. Full suite on Betty | PASS, 303 passed, 2 skipped, 0 failed | evidence/betty/writeguard-full-suite.txt |

Notes:
- 2a: agy conversation 6ed2602a-e644-421f-840c-c2acff61e5a0, turns 1 and 2 (resumed with --conversation). Between the turns EXTERNAL-LINE was appended outside agy. write_file was denied, the file kept EXTERNAL-LINE, and after a re-read the write landed with line-one, EXTERNAL-LINE and AGY-LINE. The deny is in events.log.
- 2c: Codex session 01a20629-f2f4-7c41-9239-7ec8ba859098. `cat existing.txt` after the refusal printed `original-existing`, so the file was unchanged at denial time. The retry then wrote NEW-CONTENT.
- 2d: the "external" change was simulated inside the same Codex session with a shell `>>` append, which the guard does not record as a read or write. It is not a second real session. The overwrite command was not blocked, and an `uncovered_shell_overwrite` line was logged.
- Model runs used: 3 of 6 (agy 2, Codex 1).
- Bug found by the unit tests and fixed: the patch parser returned the operation name "Add" instead of "Add File".
- The full-suite count (303) cannot be reconciled with the 292 baseline, because the test files differ from the baseline run. The result is 0 failed.
- Registration templates: docs/triangle/proposed/writeguard-hooks/. The agy template uses the verified top-level group shape with matcher `*`. The Codex and Claude matchers (`apply_patch|Bash`, `Bash|Read|Write|Edit|MultiEdit`) are standard regexes, not separately verified. The Codex smoke did use `apply_patch|Bash` with explicit `timeout=10`, and it fired.

## Round 2 fixes

Status: implemented and tested. The Betty run of 2026-11-23 passed (see Round 3: 483 passed, and docs/triangle/evidence/betty/ for the later full suites). An earlier draft of this line said the tests had not run because Betty was unreachable; that no longer holds.

- W1 fail-open on unwritable store: before any deny (enforce or warn), `store_writable()` writes and removes a probe file in the session's record dir. On OSError the hook allows, logs to errors.log if possible, and for claude/codex adds additionalContext "write guard inactive: state dir not writable". `record()` never raises. Tests: `test_unwritable_store_regular_file_allows`, `test_unwritable_store_readonly_dir_allows` (skipped as root).
- W2 shell parsing: `tokens()` is now a quote-aware tokenizer; unquoted newlines split commands like `;`; a quoted `>` (or any quoted operator word) is never an operator, so it neither flags an overwrite nor updates the last-writer ledger. `>>` is still never flagged. Tests: `test_shell_reads`, `test_shell_overwrites` additions, `test_quoted_gt_and_newline_do_not_record`.
- W3 cd tracking: `shell_reads(cmd, cwd)` now returns resolved paths and follows literal `cd DIR` across segments; a non-literal cd ($VAR, ~user, -, command substitution, globs) or pushd/popd stops recording reads for the rest of the command. Shell overwrite detection did not track cd in round 2; superseded in round 3: overwrite targets now resolve against the same effective cwd as reads (the post branch records freshness and the ledger, so it is not warn-only). Tests: parametrized `test_shell_reads`, `test_cd_tracking_records_resolved_paths`.
- W4 unreadable existing file: `signature()` returns `{size, mtime_ns, sha256: None}` when stat works but open fails; `same_sig` compares size + mtime_ns. Test: `test_signature_unreadable_file` (skipped as root).

## Round 3 fixes (2026-11-23)
Governing rule: WHEN UNSURE, RECORD NOTHING. A missed record only causes a later "re-read first" deny (safe); a wrong record grants false freshness. No heredoc modelling, no subshell scoping.
- `tokens()` splits each punctuation run by longest match against a fixed operator list (`;;& &>> <<< <<- ;; ;& && || |& >> >| >& <& &> <> <<` and the single `< > ; & | ( )`). A run that does not split, or is implausible (a redirection directly followed by another operator other than `(`), becomes an UNKNOWN operator token.
- A backslash-escaped character counts as quoted: an escaped operator is a literal NUL-prefixed word, never an operator. A quoted `{` or `}` is protected the same way.
- New `shell_effects(cmd, cwd) -> None | (reads, overwrites)` returns RESOLVED paths. It returns None (record nothing for the WHOLE command) for: an unquoted `(` or `)` token, a standalone `{` or `}` word, a backtick or `$(` anywhere outside single quotes, a heredoc or herestring operator (`<<`, `<<-`, `<<<`), an UNKNOWN operator token, a segment whose first word (after `NAME=val` prefixes) is builtin, command, eval, source, `.`, exec, pushd or popd, or a non-literal cd. A literal `cd DIR` tracks the effective cwd across segments, for reads and for overwrite targets alike; a literal cd combined with `|`, `|&`, `&` or `||` is not modelled and also gates the command.
- The PostToolUse shell branch records reads and overwrite targets ONLY from `shell_effects`; if it returns None nothing is recorded. The pre-event warning (`shell_overwrites`) stays loose and never denies.
- Counter-check: every real captured shell fixture (codex_*_bash_*, claude_*_bash*, agy_*run_command*) records exactly what it recorded before (test `test_real_shell_fixtures_record_exactly_what_they_did_before`); none contains a gated construct. Common Codex forms (`cat f`, `sed -n '1,200p' f`, `nl -ba f | sed -n '1,80p'`, `head -n 50 f && tail -n 5 g`, `cd sub && cat f`) still record.
- Tests restated as safety properties (recording nothing is acceptable): subshell cd leak, untested clauses, heredoc body, glued operators.
- Betty result (2026-11-23): targeted test_writeguard.py + test_triangle_advisory.py 181 passed (plus one test of mine that was wrong and removed); full suite `coordination/tests tests`: 483 passed, 3 skipped, 1 failed. The 1 failure is the known pre-existing flake `test_jobs.py::test_join_stops_on_paused_execution_and_on_stop_policy_is_honored`. Output: evidence/betty/round3-full-suite.txt.

## Round 4: trusted command shapes (2026-11-23)
Four review rounds each found a new shell form that slipped past a DENYLIST of gated constructs. Round 4 replaces it with a POSITIVE definition: `shell_effects(cmd, cwd)` trusts a command only if it matches the grammar below in full; anything else records NOTHING. A missed record only causes a later "re-read first" deny (safe); a wrong record grants false freshness (the bug). No new denylist entries are the mechanism.

Grammar (all of it must hold, else `None`):
1. Raw-text pre-gate, before tokenizing: no `$` or backtick outside single quotes (so none inside double quotes either), no backslash anywhere (also none inside single quotes), no control characters other than tab and newline, no unterminated quote.
2. Operators: only the separators `;`, `&&`, `||`, `|` and the newline separator, and the redirects overwrite `>`, `>|`, `&>`, `2>`; append `>>`, `&>>`, `2>>`; input `<`; fd-dup `>&` and `2>&` only to a digit or `-`. Any other operator (`<>`, `<&`, `|&`, `&`, `(`, `)`, `<<`, `<<<`, `;;`, `;&`, UNKNOWN) gives None. A digit word glued to a redirect is an fd prefix only for `2`. A redirect needs a literal target word. Empty segments, a leading or doubled `;`, and a dangling `&&`, `||` or `|` are syntax errors and give None; a newline after `&&`, `||`, `|` is a line continuation.
3. Each segment: drop leading `NAME=value` words (unquoted, no special characters, NAME not one of PATH, IFS, HOME, CDPATH, ... see `_DANGEROUS_ASSIGN`); the first word must match `^[A-Za-z0-9_./+-]+$`, not be an unquoted special word, not be a reserved word (`if then elif else fi do done while until for in case esac select time ! coproc function { } [[ ]]`) and not be a state-changing or flow-changing builtin (`GATED_BUILTINS`: builtin command eval source . exec pushd popd alias trap set shopt setopt, plus exit return logout break continue export unset read declare ... so that `exit 0; cat f` or `set -e` records nothing). A segment with only redirects or only assignments gives None. Unquoted `* ? [ ] { } ~ !`, a word-initial `=` (zsh) and a word-initial `#` (comment) are not literal words: in an argument position the argument is skipped, as a redirect target or program the command gives None (word-initial `#` always gives None).
4. `cd` is trusted ONLY as a leading chain: segments 0..k that are each exactly `cd <literal dir>` (no `..` component, no leading `-`, no redirect, no assignment) and each followed by `&&`. Any other `cd` (after `;`, after `||`, in a pipe, later, with a non-literal target, inside a reserved-word construct) gives None. Paths are resolved against the effective cwd of that chain.
5. Conditional execution: post payloads of all three hosts carry no exit status (verified in the fixtures), so after the leading cd chain reads and overwrite targets are recorded ONLY from the segments up to and including the first one followed by `&&` or `||`. `;`, `|` and newline do not stop recording.
6. Reads come only from segments whose program is exactly one of READ_CMDS (cat head tail sed nl wc less bat; no path, so `/bin/cat` and `./cat` record nothing), with per-program option tables (`_READ_OPTS`): an unknown option, any `--long` option, an option after the first file, `sed` with anything but `-n -E -r` and a script of the form `Np`, `N,Mp`, `$p`, `d`, `q`, `=`, make that segment record no read. A segment with any file redirect (input, append, or more than one) records no read, because a failing redirect stops the command. The target word of every redirect is never a read, and a read of a path that a redirect elsewhere in the same command may create is dropped. Overwrite targets come only from a segment with exactly one file redirect that is an overwrite redirect (`/dev/null` and fd-dups do not count); append targets are not overwrites; `tee`, `cp`, `mv` record nothing. The post branch records an overwrite only for an existing, writable regular file.
7. The PostToolUse shell branch records only from `shell_effects`. The pre-event warning (`shell_overwrites`) stays loose and never denies.
8. Deny reason names the unlock form. Codex: "To unlock: run a standalone read such as `cat <path>` (on its own, not after && or ||), then retry." Antigravity: "To unlock: call read_file on <path>, then retry."

Real-shell differential oracle (`coordination/tests/test_writeguard_oracle.py`), the closure mechanism:
- A seeded generator (default 1500 commands per shell, seed 20261123; `WG_ORACLE_N`, `WG_ORACLE_SEED`) draws from programs cat/head/tail/sed -n/nl/wc/echo/printf/true/false/cd plus gated probes (exit, set -e, export, unset PATH, eval, source), the five files and two dirs, the five separators, all redirects including `<>`, `&>`, `&>>`, `2>&1`, quoting variants (plain, single, double, `$'..'`, backslash), reserved words (if/for/!/time), parens, braces, heredocs, `$(..)` and backticks. Each command runs in a fresh temp dir inside a real shell.
- Read oracle: wrapper scripts for every READ_CMD sit first on PATH, log the realpath of each non-option argument that is an existing file (resolved from their own cwd), then exec the real binary. Every recorded read that is a file after the run must be in the log. Overwrite oracle: files hold unique markers; every recorded overwrite that is a file after the run must have been created or changed.
- Run under bash and under `zsh -f` (Codex runs zsh); a missing zsh is an explicit skip with a reason. The test asserts violations == 0 and coverage (fraction of commands with at least one recorded path) > 0.15, so a gate that returns None for everything cannot pass. A meta-test runs the same corpus against the old loose parser and requires violations > 0, proving the oracle can fail. Grep the summary line `WG_ORACLE shell=... violations=N coverage=F`.
- Found by the oracle during round 4 and fixed: a read of a path the same command later creates by a redirect (`cat missing.txt; true >> missing.txt`).

UNCOVERED (these record NOTHING now, by design; each only costs a later "re-read first"):
- anything after the first `&&` or `||` (for example `cat f && cat g`: only f; `echo x > b && cat b`: only the overwrite);
- any heredoc, herestring, subshell, brace group, process substitution, `$(..)`, backtick, backslash, `$'..'`, `&`, `|&`, `<>`, `<&`;
- `if`, `for`, `while`, `case`, `time`, `!`, functions, `[[`;
- any `cd` that is not a leading `cd <dir> && ...` chain, `cd ..`, and every cd inside a larger construct;
- reads with a file redirect (`cat f > g`), `--long` options, `sed -i`, `sed` scripts beyond the simple forms, `tee`/`cp`/`mv` overwrites, `/bin/cat`, glob/brace/tilde arguments;
- commands with `exit`, `export`, `set`, `eval` and the other state-changing builtins, or with `PATH`/`IFS`/`HOME` assignment prefixes.
Accepted residual: a non-redirect creator (`cat m.txt; touch m.txt`) of a path read earlier in the same command records a read of a file the command itself created (the oracle alphabet cannot create files any other way). `CDPATH` set in the host environment could redirect `cd <dir>`; the oracle unsets it.

## Round 5: freshness only for content the session saw (review 2026-11-23, F1, F3)
Why: in Rounds 1 to 4 a shell read was recorded at POST with the file's signature at that moment. `cat plan.md && make test` therefore credited every edit another session made while the test ran, and a later whole-file write silently dropped it. Two directions, both reproduced on Betty, both now closed:
- **Read direction.** At PRE, for a shell tool (Codex `Bash`, Claude `Bash`, agy `run_command`), the hook computes `shell_effects` and snapshots the signature of each candidate read path (under `<dir>/pending/<host>/<session>/<key>.json`). At POST a shell read is recorded only if a snapshot exists for the call AND the file's signature is unchanged; what is recorded is the snapshot signature. No snapshot (no PRE hook ran, or the command was not parsable) records nothing. A later whole-file write is then denied, as for any unread file.
- **Overwrite direction.** `echo x > f; make` credited an edit made during `make`. A shell overwrite is now recorded only when EVERY program in the command is in the known fast set (`FAST_PROGS`: the read commands `cat head tail sed nl wc less bat`, plus `echo printf true false :`). Anything else (make, pytest, sleep, ...) records no overwrite. The remaining window is hook latency for a command that is fast by construction.
- **Native read tools** (agy `read_file`, Claude `Read`): the same PRE and POST equality applies when a PRE snapshot exists; without one the plain POST record is kept (Claude's matcher has no PRE hook for `Read`).
- **Keying.** The fixtures show the same call id in PRE and POST for every host (Codex and Claude `tool_use_id`, agy `toolCall.id`; test `test_f1_fixtures_carry_the_same_call_id_in_pre_and_post`), so a snapshot is keyed by that id. A payload without an id falls back to a hash of (host, session, tool, command or paths). Two concurrent calls of one command therefore never share a snapshot.
- **Bounds and cleanup.** The snapshot is deleted when POST consumes it. A later PRE for the same session drops snapshots older than one day and keeps at most 64 (`PENDING_TTL_S`, `PENDING_MAX`). A snapshot failure is logged and never blocks (fail-open is unchanged).
- **F3 (Codex apply_patch).** The POST records only when `tool_response` is a string that starts with `Exit code: 0` and contains `Success` (as in the captured fixture). A failed patch, an empty or missing response, or any other shape records nothing.
- **Hook host list.** `writeguard.HOSTS` stays a copy (the script is standalone); a test asserts it equals `model.HOST_KINDS` (F14).
- **Oracle.** `test_writeguard_oracle.py` still drives `shell_effects` directly (`WG_ORACLE ...`; its coverage floor of 0.15 is unchanged). A new hook-level test drives the real hook: PRE, a real shell, POST (`WG_ORACLE_HOOK ...`). Session S1 is posted at once (control: reads must be credited), session S2 after every file was rewritten by "another session" (no read may be credited).
- Betty results: see docs/triangle/evidence/betty/review-repair-full-suite.txt and BUILD.md.

## Running alongside Mycelium (compat pass, 2026-11-24)
Scope: every Mycelium function on Claude Code, Codex and Antigravity, checked against Mycelium main 0.9.7 and the
hook registrations of the 0.9.3 to 0.9.6 builds. That covers all 11 skills, every hook script, and every helper script
and tool. Scripts that write no shared file were checked by a scan of their write sites rather than read in full. Coordination tools are covered by the
advisory allowlist, which refuses any tool not on it.

Fixed in this pass:
- agy PreToolUse allow is empty stdout. `{}` denies and `{"decision":"allow"}` grants (see "Verified host facts").
- The guard knows the agy IDE tool names `view_file` and `write_to_file` (offline-tested only).
- Mycelium's stamp files `.last-run` and `.last-audit` no longer raise the shell-overwrite warning.
- `export_coordination.py` refuses the canonical core-overlay on any Mycelium other than 0.6.0, because the overlay
  replaces whole core hook files written for 0.6.0.
- The Codex template says to append hook groups, because Codex keys hook trust by group position.

Why Mycelium and the guard do not fight:
- Mycelium mostly appends, and the guard never refuses an append.
- Mycelium's shared registries and indexes are rewritten by its own locked scripts, not by agent tool calls, so the guard
  never sees them.
- The handoff and the optional knowledge-audit `INDEX.md` are whole-file writes by the agent. The guard can deny one of
  them once, and a re-read clears it. No loop was found.
- Mycelium's hooks emit no permission decisions, and the guard's state lives outside every repository.

Known limits (not fixable inside sporedrive):
- **Codex with Mycelium 0.9.7 or later.** 0.9.7 adds an io-observer PreToolUse on `apply_patch`. When the guard denies
  that call, no PostToolUse runs, so Mycelium's observation interval stays open for up to 24 h. Later file-change records
  in that repository are then attributed "shared" instead of "sole". The changes are still recorded, and Mycelium already
  accepts this for any denied Codex call. Builds before 0.9.7 do not observe `apply_patch` before the call. Not tested:
  whether Codex runs Mycelium's PreToolUse at all when another hook denies.
- **Mycelium's own cross-session races** (pre-existing, Mycelium repository). In upstream Mycelium (0.9.x, including 0.9.7),
  concurrent SessionStarts can dispatch the knowledge audit and transfer twice. The in-flight ledger that
  prevents this, `housekeeping_ledger.py`, comes from this repository's `core-overlay/` and is present only in
  builds that carry it. Manually run scripts rewrite files without a lock: `init_repo`, `init_knowledge`,
  `install_convention`, `migrate_existing_repos`, `sync_html_report`, `check_linter_versions --record` and the
  knowledge-map build.
- **Not live-tested:** the agy IDE and App routes, the Claude `Bash` guard hook, and Codex hook ordering when one hook
  denies.
