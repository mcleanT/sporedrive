# M7 — triangle drill and review (lead plan, 2026-11-23)

All drill steps run on a scratch store with dev code. Nothing is installed and nothing touches the live store. Per-run hooks and grants only.

## Drill (real hosts)
- **D1 Claude → Codex review request, woken.**
  - Setup: task t-tri with features `triangle-kinds` and `codex-exec-wake`; lead (claude, supervisor); cx (codex executor, a NEW headless scratch thread, `wake=headless`); a small file `sample.py` in the scratch repo with one planted bug.
  - Lead sends `review_request` to cx ("review sample.py; reply to lead with kind report") and calls `wake`.
  - Pass: wake `accepted`; lead's inbox holds a `report` from cx, reply_to the request, that names the planted bug. Codex runs ≤2.
- **D2 Antigravity advisory reply on its next turn (live re-run of the advisory attach path).**
  - One `agy -p` run with session-only per-tool grants (attach, inbox, send, ack), MYCELIUM_COORD_ADVISORY=1 in the server env, and per-conversation observer id `agy-<conv8>`.
  - It attaches as an observer, reads a `consult_request` from lead, acks it, and replies with kind `report`.
  - Pass: store shows the ack and the report. A second probe in the same run tries to attach as observer with cx's host/session and gets `advisory_process_refused`, with the selection still pointing to cx. agy runs ≤2.
- **D3 Antigravity stale-write deny text (live re-run of the write guard agy path on round-4 code).**
  - agy reads c.txt; an external append follows; agy's write_file is denied with the read_file unlock hint; after a re-read the write lands with both lines. agy runs ≤2.
- **D4 Wake refusals live (no model run).**
  - `wake` to the agy observer returns `refused:observer_not_wakeable`.
  - `wake` of a `progress` message returns `refused:kind_not_wakeable`.
  - `wake` from an advisory process is refused.
- **Host config hashes:** before and after for ~/.gemini/config/config.json, ~/.codex/config.toml, ~/.claude/settings.json (all unchanged).

## Review
- One scoped Codex review (codex-review skill, read-only, one question): correctness of `wake.py`, the only new module with no independent adversarial check, and its CLI/MCP surfaces against M6_WAKE_DESIGN.md "Frozen acceptance".
- Then one targeted verification of any repairs. Optional findings go to the backlog.

## Report
- Plain-language owner report: what works, by host and app/CLI; what is next-turn only; limits; evidence.
- Decisions for the owner:
  - live Antigravity registration;
  - installs: hooks, plugin copies, Codex config approving coord tools for wake;
  - commit/PR;
  - the optional P2 visible TUI probe;
  - the Antigravity observer-id policy (lead recommends per-conversation ids).
