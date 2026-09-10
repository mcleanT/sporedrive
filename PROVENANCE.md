# Provenance

## SporeDrive bundle

This directory ("SporeDrive") is a cleaned, portable release build of the Codex <-> Claude Code
coordination workflow. It was assembled by copying (never moving) files out of a private local
development repository into this isolated staging tree, then removing everything that was
run-specific, machine-specific, or otherwise not meant for distribution.

Top-level components carried over from the source repo (`codex-claude-workflow`):

- `bridge/` -- the `cmux_bridge` MCP server package, `live_acceptance.py`, `run_live.sh`, and its
  offline test suite (`bridge/tests/`).
- `coordination/` -- the coordination package (`mycelium_coord/`), its CLI (`bin/`), hook scripts
  (`hooks/`), skill instructions (`skill/SKILL.md`), and tests.
- `scripts/` -- installer/export tooling (`wfctl.py`, `export_coordination.py`, and related
  activation/verification helpers).
- `tests/` -- root-level offline unit tests.
- `src/` -- the actual instruction/skill payload that the workflow installs: `src/claude/` (the
  Claude-side `CLAUDE.md` fragment, the `codex-review` skill, the `codex_ask.sh` wrapper, and other
  bundled skills) and `src/codex/` (the Codex-side `AGENTS.md` fragment and the `cmux-driver` skill).
- `VERSION`, `.gitignore`.

Excluded from this release (present in the source repo but deliberately not copied):

- `docs/` -- every file in the source repo's `docs/` directory was an internal, dated engineering
  journal entry (checkpoints, migration notes, disposition matrices, smoke/crossrun results,
  guarded-activation records) referencing private brief paths under `~/Documents/Codex/...` and
  internal run identifiers. None qualified as portable, user-facing documentation, so the entire
  directory was excluded rather than partially redacted.
- `checks/`, `receipts/`, `snapshots/`, `installed/` -- live-run evidence, transcripts, and local
  install/rollback state.
- `.git/`, `.pytest_cache/`, `.ruff_cache/`, `.benchmarks/`, `__pycache__/`, `*.pyc`, `.DS_Store`.
- Any `*.jsonl` transcripts, run markers, or state-dir contents.

A handful of test fixtures and default-path references inside included source/test files (e.g.
`bridge/live_acceptance.py`, `bridge/run_live.sh`, a few files under `coordination/tests/` and
`mycelium-source/skills/core/`) still contain the original developer's local absolute path
(`/Users/mst36/...`) as a default or fixture value. These are functional code paths, not
documentation, and were left as-is; a downstream adopter should treat them as illustrative defaults
to override, not as configuration to trust blindly.

## Mycelium lifecycle source (`mycelium-source/`)

`mycelium-source/` is a full copy of the Mycelium lifecycle plugin source repository
(`mycelium-lifecycle-wfi-r2`) at commit:

```
f2b0083d1118caffb08612ffbbd677db0268558f
2026-09-09 12:57:44 -0400
```

This is the reproducible source/build input needed to rebuild the Mycelium integration candidate
that the coordination package (`coordination/`, `src/codex/skills/cmux-driver/`) talks to. Excluded
from the copy: `.git/` (no history was carried over -- only the working tree at the pinned commit),
and cache/build artifacts (`.pytest_cache/`, `.ruff_cache/`, `.benchmarks/`, `__pycache__/`, `*.pyc`).

`mycelium-source/` carries its own `LICENSE` and `README.md` from the upstream project; those apply
to that subtree. See the top-level `LICENSE` in this bundle for the license covering the
`codex-claude-workflow`-derived parts (`bridge/`, `coordination/`, `scripts/`, `tests/`, `src/`).

## Build identity and acceptance

The `codex-claude-workflow`-derived parts of this bundle correspond to these source revisions:
`bridge/live_acceptance.py` and `bridge/tests/test_live_runner_helpers.py` at `9d18c82` (the accepted
acceptance harness -- review246 case_r6 FIFO fixture + R11c valid-payload scoping),
`scripts/crossrun_view.py` at `ec49e21`, and the remaining `bridge/`, `coordination/`, `scripts/`,
`tests/`, `src/` files from the same `codex-claude-workflow` tree (canonical HEAD `2398fee` at export).
`mycelium-source/` is pinned at `f2b0083` (above).

The full acceptance suite passed **40/40** against a real cmux session (harness `9d18c82`; independent
proof `full40-second-independent-proof.json` SHA `d35ac382...`); the 297 canonical bridge/coordination
tests and the offline `tests/test_wfctl.py` clean-clone suite pass.

This release records its **own** rebuilt build identity. It does **not** claim byte-identity with the
installed Mycelium R3 **package** closure (`36d2b54`, export manifest `80aa927f`) or the wfctl
**workflow-instruction** manifest (`ae0d755`) on the developer's hosts -- those are separate installed
artifacts. This bundle is the reproducible source/build input: a downstream adopter rebuilds the native
Mycelium candidate from `mycelium-source/` + `coordination/` via `scripts/export_coordination.py`.
