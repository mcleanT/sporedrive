#!/usr/bin/env python3
"""Guarded repoint of the OWNED Mycelium core-hook command paths in a Claude
project settings.local.json (r2 seq65 Claude activation; seq81 whole-tree
fail-closed baseline supersedes the earlier per-handler/ordinal resolver).

It rewrites ONLY the owned core-hook command paths from an OLD runtime prefix to
a NEW one (e.g. `~/.claude/mycelium-runtime` -> `mycelium-runtime-wfi`), preserving
every other hook, key and value, and the file's permission mode.

WHOLE-TREE BASELINE (seq81; supersedes the seq75/seq77 per-handler resolver):

`apply` records, in the receipt, the ENTIRE `hooks` subtree exactly as it was
BEFORE the mutation (`pre_hooks`) and exactly as it was AFTER (`post_hooks`).
There is NO per-handler identity, NO ordinal position, NO "find the edited
handler" resolver of any kind. `rollback` compares the CURRENT `hooks` subtree,
as a whole, against those two snapshots:

  * current == post_hooks  -> restore: write `pre_hooks` back verbatim.
  * current == pre_hooks   -> idempotent no-op (already restored), exit 0.
  * anything else          -> CONFLICT: refuse, write nothing, exit non-zero.
    This includes a later unrelated hook being added, a group being prepended
    or reordered, or the owned hook drifting to a third value -- ANY change to
    the hooks tree since apply invalidates the receipt. That is deliberate:
    the narrowed contract refuses rather than guessing which handler is "the"
    owned one (seq75/seq77/seq79 were all misattribution bugs in that guess).

`reconcile` upgrades a legacy (schema-1, pre-seq81) flat receipt -- which only
ever recorded `changed_commands` (a flat list of pre-image command strings), no
tree snapshot -- into a schema-2 receipt. Because schema-1 never captured a
tree, a historical `pre_hooks` tree can never be VERIFIED from it -- so
`reconcile` never claims to. It always writes a truthfully-labeled
`"kind": "current-state-recovery"` receipt: `pre_hooks`/`post_hooks` are built
from the CURRENT config (current owned hooks translated back to old_prefix /
as they currently stand), not a reconstructed history. `--from-receipt` is
REQUIRED (a `--recovery` flag, if passed, does NOT skip this -- there is no
way to bypass opening and verifying the legacy receipt) and is VERIFIED before
anything is written:
  * realpath(config) == receipt.config_path
  * old_prefix/new_prefix on the CLI match the legacy receipt's
  * the current config's currently-owned new-prefix hooks, translated back to
    old-prefix form, are EXACTLY (as a multiset) the legacy receipt's
    `changed_commands` -- and re-applying old->new to the reconstructed tree
    exactly reproduces the current tree (whole-tree round-trip)
The receipt records exactly which of these were verified (`verified_against_legacy`)
and a `note` stating plainly that the recovery basis is current state, not a
reconstructed history. If any check fails, `reconcile` refuses -- no writes,
non-zero exit -- whether or not `--recovery` was passed.

Guards (mirroring scripts/additive_plugin_toggle.py):
  * mode-preserving: st_mode captured BEFORE mutation, os.chmod() AFTER os.replace().
  * symlink-safe: os.path.realpath() resolves the target; the link is left in place.
  * atomic: uniquely-created temp (mkstemp O_EXCL) in the same dir, fsync, os.replace.
  * drift guard (apply): the live file must still equal the pre-image at commit.
  * receipt: schema 2, written before mutation, records the whole-tree baseline.

Subcommands: apply | rollback | reconcile (upgrade a legacy receipt to schema 2).
Exit: 0 ok / no-op | 1 conflict/validation/usage failure (nothing written).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import tempfile

OWNED_HOOKS = frozenset(
    {
        "mycelium-activity-tracker.sh",
        "mycelium-data-tracker.sh",
        "mycelium-health.sh",
        "mycelium-post-action.sh",
        "mycelium-read-tracker.sh",
        "mycelium-stop-check.sh",
    }
)
RECEIPT_SCHEMA = 2


def _sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _owned_script(cmd: str, prefix: str) -> str | None:
    """Return the owned core-hook script name if cmd uses prefix for one, else None."""
    if not isinstance(cmd, str) or prefix not in cmd:
        return None
    tail = cmd.split(prefix, 1)[1]
    script = (tail.split()[0] if tail else "").strip("\"'")
    return script if script in OWNED_HOOKS else None


def _iter_handlers(data: dict):
    """Yield (event, group_index, hook_index, matcher, hook_dict) for every hook.

    `data` is a config-shaped dict with a top-level "hooks" key. Used only to
    locate and rewrite OWNED commands (apply / reconcile reconstruction); it is
    NOT used for rollback identity -- rollback compares whole trees, not
    individual handlers.
    """
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for gi, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher")
            for hi, h in enumerate(group.get("hooks", [])):
                if isinstance(h, dict) and isinstance(h.get("command"), str):
                    yield event, gi, hi, matcher, h


def _hooks_tree(data: dict) -> dict:
    """The bare `hooks` subtree of a config-shaped dict (never None)."""
    hooks = data.get("hooks", {})
    return hooks if isinstance(hooks, dict) else {}


def _diff_event_keys(cur_hooks: dict, other_hooks: dict) -> list[str]:
    """Event keys whose value differs between two hooks trees (for CONFLICT reports)."""
    keys = set(cur_hooks) | set(other_hooks)
    return sorted(k for k in keys if cur_hooks.get(k) != other_hooks.get(k))


def _apply_prefix(data: dict, old_prefix: str, new_prefix: str) -> list[dict]:
    """Rewrite owned commands old->new in-place; return per-handler change records.

    The returned records are used for --expect counting, printing, and
    reconstructing a historical tree in `reconcile` -- never for rollback
    identity (rollback is whole-tree, see module docstring).
    """
    changes = []
    for event, gi, hi, matcher, h in _iter_handlers(data):
        cmd = h["command"]
        if _owned_script(cmd, old_prefix) is not None:
            post = cmd.replace(old_prefix, new_prefix)
            h["command"] = post
            changes.append(
                {
                    "event": event,
                    "matcher": matcher,
                    "group_index": gi,
                    "hook_index": hi,
                    "pre": cmd,
                    "post": post,
                }
            )
    return changes


def _reverse_check(pre: dict, post: dict, old_prefix: str, new_prefix: str) -> None:
    """Reversing new->old in owned commands of `post` must reproduce `pre` exactly."""
    rev = copy.deepcopy(post)
    _apply_prefix(rev, new_prefix, old_prefix)
    if rev != pre:
        raise SystemExit(
            "VALIDATION FAILED: a non-owned change was detected; refusing to write"
        )


def _atomic_write(real: str, text: str) -> None:
    st_mode = None
    try:
        st_mode = os.stat(real).st_mode & 0o777
    except FileNotFoundError:
        pass
    d = os.path.dirname(real) or "."
    fd, staging = tempfile.mkstemp(prefix=".repoint.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(staging, real)
        if st_mode is not None:
            os.chmod(real, st_mode)  # PRESERVE original mode across the atomic replace
    finally:
        if os.path.exists(staging):
            os.unlink(staging)


def _write_receipt_exclusive(path: str, payload: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def _serialize(post: dict, raw: str) -> str:
    return json.dumps(post, indent=2) + ("\n" if raw.endswith("\n") else "")


def cmd_apply(args) -> int:
    real = os.path.realpath(args.config)
    with open(real, encoding="utf-8") as fh:
        raw = fh.read()
    pre = json.loads(raw)
    pre_hooks_snapshot = copy.deepcopy(_hooks_tree(pre))
    post = copy.deepcopy(pre)
    changes = _apply_prefix(post, args.old_prefix, args.new_prefix)
    if not changes:
        print(f"NO-OP: no owned core-hook command uses {args.old_prefix!r} in {real}")
        return 0
    if args.expect is not None and len(changes) != args.expect:
        raise SystemExit(
            f"EXPECT MISMATCH: found {len(changes)} owned commands, expected {args.expect}; refusing"
        )
    _reverse_check(pre, post, args.old_prefix, args.new_prefix)
    new_text = _serialize(post, raw)
    post_reloaded = json.loads(new_text)
    _reverse_check(pre, post_reloaded, args.old_prefix, args.new_prefix)
    post_hooks_snapshot = copy.deepcopy(_hooks_tree(post_reloaded))

    if args.dry_run:
        print(f"DRY-RUN: would repoint {len(changes)} owned commands in {real}")
        for c in changes:
            print(
                f"  - {c['event']}[matcher={c['matcher']!r}] {c['post'].split(args.new_prefix, 1)[1]}"
            )
        return 0

    pre_sha = _sha(real)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "kind": "apply",
        "config_path": real,
        "old_prefix": args.old_prefix,
        "new_prefix": args.new_prefix,
        "pre_sha256": pre_sha,
        "mode": oct(os.stat(real).st_mode & 0o777),
        "pre_hooks": pre_hooks_snapshot,
        "post_hooks": post_hooks_snapshot,
        "changed_commands": [
            c["pre"] for c in changes
        ],  # audit only; NOT used by rollback
    }
    _write_receipt_exclusive(args.receipt, receipt)  # BEFORE mutation
    with open(real, encoding="utf-8") as fh:
        live = json.load(fh)
    if live != pre:
        raise SystemExit(
            "DRIFT: config changed since read; aborting (receipt written, config untouched)"
        )
    _atomic_write(real, new_text)
    print(f"OK: repointed {len(changes)} owned commands in {real}")
    print(f"receipt: {args.receipt} (schema {RECEIPT_SCHEMA})")
    print(
        f"pre_sha={pre_sha[:16]} post_sha={_sha(real)[:16]} mode={oct(os.stat(real).st_mode & 0o777)}"
    )
    return 0


def cmd_rollback(args) -> int:
    with open(args.receipt, encoding="utf-8") as fh:
        rec = json.load(fh)
    if (
        rec.get("schema") != RECEIPT_SCHEMA
        or "pre_hooks" not in rec
        or "post_hooks" not in rec
    ):
        raise SystemExit(
            "LEGACY RECEIPT: this receipt predates the whole-tree baseline (schema 2). "
            "Run `reconcile --from-receipt <this receipt>` to record a verified "
            "current-state-recovery receipt against the current config before rollback."
        )
    real = os.path.realpath(rec["config_path"])
    with open(real, encoding="utf-8") as fh:
        raw = fh.read()
    cur = json.loads(raw)
    cur_hooks = _hooks_tree(cur)
    pre_hooks = rec["pre_hooks"]
    post_hooks = rec["post_hooks"]

    if cur_hooks == pre_hooks:
        print(
            f"NO-OP rollback: hooks tree in {real} already matches the recorded pre-image"
        )
        return 0

    if cur_hooks != post_hooks:
        diff_post = _diff_event_keys(cur_hooks, post_hooks)
        diff_pre = _diff_event_keys(cur_hooks, pre_hooks)
        raise SystemExit(
            "CONFLICT: current hooks tree matches neither the recorded pre-image nor "
            "post-image; refusing rollback (no writes). Whole-tree identity means ANY "
            "later insertion, deletion, reorder, or drift invalidates the receipt.\n"
            f"  differs from post-image at event(s): {diff_post or '(none)'}\n"
            f"  differs from pre-image at event(s):  {diff_pre or '(none)'}"
        )

    reverted = copy.deepcopy(cur)
    reverted["hooks"] = copy.deepcopy(pre_hooks)
    new_text = _serialize(reverted, raw)
    _atomic_write(real, new_text)
    print(
        f"ROLLED BACK whole-tree hooks config in {real} ({rec['new_prefix']} -> {rec['old_prefix']})"
    )
    if args.force:
        print(
            "  note: --force accepted for CLI compatibility; whole-tree rollback has "
            "no partial/force mode (a conflict always refuses, force or not)"
        )
    return 0


def _reconstruct_pre(cur: dict, new_prefix: str, old_prefix: str):
    """From a config-shaped `cur`, reverse-apply new->old on currently-owned hooks.

    Returns (sorted pre-form command list, reconstructed bare pre-hooks tree,
    change records). Used by both the validated `reconcile` upgrade path and
    the `--recovery` path -- the only difference is whether the result is
    checked against a legacy receipt before being written.
    """
    candidate = copy.deepcopy(cur)
    changes = _apply_prefix(candidate, new_prefix, old_prefix)
    # NOTE: _apply_prefix's OWN "pre"/"post" are relative to ITS call direction
    # (old_prefix=new_prefix, new_prefix=old_prefix here) -- so c["pre"] is the
    # ORIGINAL new-prefixed command and c["post"] is the reconstructed
    # old-prefixed command. We want the reconstructed old-prefixed list.
    return sorted(c["post"] for c in changes), _hooks_tree(candidate), changes


def cmd_reconcile(args) -> int:
    """Upgrade a legacy schema-1 receipt to a TRUTHFULLY-labeled schema-2 receipt.

    seq98 correction: a legacy (schema-1) receipt never captured a hooks tree --
    only a flat `changed_commands` list -- so no historical tree can ever be
    VERIFIED from it, only the CURRENT state can. This function therefore always
    writes `"kind": "current-state-recovery"` (never "historical-upgrade") and
    always requires + verifies `--from-receipt` before writing anything.
    `--recovery` does NOT bypass this verification -- it has no effect on
    behavior; it is accepted only so callers may say explicitly that they know
    this is a recovery, not a guaranteed historical reconstruction.
    """
    real = os.path.realpath(args.config)
    with open(real, encoding="utf-8") as fh:
        cur = json.load(fh)

    if not args.from_receipt:
        raise SystemExit(
            "RECONCILE: --from-receipt is required -- a legacy receipt must be opened "
            "and verified before any receipt is written. --recovery does not bypass "
            "this; there is no way to skip opening and verifying --from-receipt."
        )
    if not os.path.isfile(args.from_receipt):
        raise SystemExit(
            f"RECONCILE: --from-receipt {args.from_receipt!r} does not exist; refusing"
        )
    with open(args.from_receipt, encoding="utf-8") as fh:
        legacy = json.load(fh)

    reasons = []
    config_path_ok = real == legacy.get("config_path")
    if not config_path_ok:
        reasons.append(
            f"config_path mismatch: {real!r} != receipt {legacy.get('config_path')!r}"
        )
    prefix_ok = args.old_prefix == legacy.get(
        "old_prefix"
    ) and args.new_prefix == legacy.get("new_prefix")
    if not prefix_ok:
        reasons.append(
            f"prefix mismatch: cli(old={args.old_prefix!r}, new={args.new_prefix!r}) != "
            f"receipt(old={legacy.get('old_prefix')!r}, new={legacy.get('new_prefix')!r})"
        )

    pre_list, pre_hooks_tree, changes = _reconstruct_pre(
        cur, args.new_prefix, args.old_prefix
    )
    legacy_list = sorted(legacy.get("changed_commands") or [])
    owned_set_ok = pre_list == legacy_list
    roundtrip_ok = False
    if not owned_set_ok:
        extra = sorted(set(pre_list) - set(legacy_list))
        missing = sorted(set(legacy_list) - set(pre_list))
        reasons.append(
            "owned-set mismatch: currently-owned hooks (translated to old_prefix) != "
            f"receipt.changed_commands; extra={extra} missing={missing}"
        )
    else:
        # whole-tree round-trip: re-applying old->new to the reconstructed pre-tree
        # must exactly reproduce the current tree (nothing but the owned commands
        # differs between the reconstructed pre-image and the live config).
        roundtrip = {"hooks": copy.deepcopy(pre_hooks_tree)}
        _apply_prefix(roundtrip, args.old_prefix, args.new_prefix)
        roundtrip_ok = roundtrip["hooks"] == _hooks_tree(cur)
        if not roundtrip_ok:
            reasons.append(
                "whole-tree round-trip failed: reconstructed pre-image does not "
                "reproduce the current tree when old->new is re-applied"
            )

    if reasons:
        lines = [
            "RECONCILE REFUSED: prior receipt/baseline relationship did not verify "
            "(--recovery does not change this -- --from-receipt is always required "
            "and verified):"
        ]
        lines.extend(f"  - {r}" for r in reasons)
        raise SystemExit("\n".join(lines))

    if args.expect is not None and len(changes) != args.expect:
        raise SystemExit(
            f"RECONCILE EXPECT MISMATCH: found {len(changes)}, expected {args.expect}"
        )

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "kind": "current-state-recovery",
        "config_path": real,
        "old_prefix": args.old_prefix,
        "new_prefix": args.new_prefix,
        "pre_sha256": legacy.get("pre_sha256"),
        "mode": oct(os.stat(real).st_mode & 0o777),
        "pre_hooks": pre_hooks_tree,
        "post_hooks": _hooks_tree(cur),
        # c["post"]: the reconstructed OLD-prefixed command (see _reconstruct_pre note)
        "changed_commands": [c["post"] for c in changes],
        "reconciled_from": args.from_receipt,
        "verified_against_legacy": {
            "config_path_realpath_match": config_path_ok,
            "prefix_match": prefix_ok,
            "owned_command_whole_tree_match": owned_set_ok and roundtrip_ok,
        },
        "note": (
            "current-state recovery: the legacy (schema-1) receipt never captured a "
            "hooks tree, so no historical tree can be verified or reconstructed from "
            "it -- only the CURRENT state can. What WAS verified against the legacy "
            "receipt (see verified_against_legacy): config_path (via realpath), the "
            "old/new runtime prefixes, and that the CURRENT owned hooks (translated "
            "back to old_prefix) exactly match its recorded changed_commands as a "
            "whole tree. pre_hooks/post_hooks below are built from the CURRENT "
            "config, not a reconstructed history."
        ),
    }
    _write_receipt_exclusive(args.receipt, receipt)
    print(
        f"RECOVERED (current-state, verified against legacy receipt): wrote "
        f"schema-{RECEIPT_SCHEMA} receipt {args.receipt} recording {len(changes)} "
        "owned handlers"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Guarded Mycelium core-hook path repoint (r2 seq65/seq81 whole-tree)."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("apply")
    p.add_argument("config")
    p.add_argument("--old-prefix", required=True)
    p.add_argument("--new-prefix", required=True)
    p.add_argument("--receipt", required=True)
    p.add_argument(
        "--expect",
        type=int,
        default=None,
        help="required number of owned commands to change",
    )
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("rollback")
    p.add_argument("--receipt", required=True)
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "accepted for CLI compatibility; whole-tree rollback has no partial-force "
            "mode -- a conflict always refuses, with or without --force"
        ),
    )
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("reconcile")
    p.add_argument("config")
    p.add_argument("--old-prefix", required=True)
    p.add_argument("--new-prefix", required=True)
    p.add_argument(
        "--receipt", required=True, help="path to write the upgraded schema-2 receipt"
    )
    p.add_argument(
        "--from-receipt",
        default=None,
        help=(
            "legacy schema-1 receipt to upgrade; ALWAYS required (--recovery does not "
            "waive this). Verified against the current config before being trusted; "
            "refused on any mismatch."
        ),
    )
    p.add_argument(
        "--recovery",
        action="store_true",
        help=(
            "no effect on behavior -- --from-receipt is always required and verified "
            "regardless. Accepted so callers can say explicitly that they know the "
            "result is a current-state recovery, not a guaranteed historical "
            "reconstruction (which is always the case; the output is always labeled "
            "current-state-recovery)."
        ),
    )
    p.add_argument("--expect", type=int, default=None)
    p.set_defaults(func=cmd_reconcile)

    args = ap.parse_args()
    try:
        return args.func(args)
    except SystemExit as e:
        if isinstance(e.code, str):
            sys.stderr.write(e.code + "\n")
            return 1
        return e.code or 0


if __name__ == "__main__":
    raise SystemExit(main())
