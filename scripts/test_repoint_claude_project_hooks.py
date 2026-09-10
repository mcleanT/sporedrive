"""Tests for repoint_claude_project_hooks.py.

Covers the seq81 whole-tree pre/post rollback contract (no per-handler/ordinal
resolver) and the seq98 truthful current-state-recovery reconcile (require +
verify --from-receipt; --recovery never bypasses it; never claims a historical
reconstruction).

The module is loaded from its file path so the suite runs regardless of cwd.
"""

import argparse
import importlib.util
import json
import os
import pathlib

import pytest

_MOD = pathlib.Path(__file__).resolve().parent / "repoint_claude_project_hooks.py"
_spec = importlib.util.spec_from_file_location("repoint_hooks_under_test", _MOD)
repoint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repoint)

OLD = "/opt/rt/mycelium-runtime/skills/core/hooks/"
NEW = "/opt/rt/mycelium-runtime-wfi/skills/core/hooks/"

OWNED_ORDER = [
    "mycelium-health.sh",
    "mycelium-activity-tracker.sh",
    "mycelium-data-tracker.sh",
    "mycelium-post-action.sh",
    "mycelium-read-tracker.sh",
    "mycelium-stop-check.sh",
]


def _config(prefix, *, drop=None):
    """A representative settings config with 6 owned hooks + non-owned neighbors.

    `drop` optionally omits one owned hook by name (to model an owned-set that
    no longer matches a legacy receipt).
    """

    def owned(name):
        return {"type": "command", "command": prefix + name}

    session = []
    if drop != "mycelium-health.sh":
        session.append(owned("mycelium-health.sh"))
    stop_hooks = []
    if drop != "mycelium-stop-check.sh":
        stop_hooks.append(owned("mycelium-stop-check.sh"))
    stop_hooks.append({"type": "command", "command": "/proj/.claude/hooks/scribe.sh"})

    edit_hooks = [{"type": "command", "command": "/proj/.claude/hooks/lint.sh"}]
    if drop != "mycelium-activity-tracker.sh":
        edit_hooks.append(owned("mycelium-activity-tracker.sh"))
    bash_hooks = []
    if drop != "mycelium-data-tracker.sh":
        bash_hooks.append(owned("mycelium-data-tracker.sh"))
    if drop != "mycelium-post-action.sh":
        bash_hooks.append(owned("mycelium-post-action.sh"))
    read_hooks = []
    if drop != "mycelium-read-tracker.sh":
        read_hooks.append(owned("mycelium-read-tracker.sh"))

    return {
        "model": "opus",
        "permissions": {"allow": ["Bash"]},
        "hooks": {
            "SessionStart": [{"matcher": "", "hooks": session}],
            "PostToolUse": [
                {"matcher": "Edit|Write", "hooks": edit_hooks},
                {"matcher": "Bash", "hooks": bash_hooks},
                {"matcher": "Read", "hooks": read_hooks},
            ],
            "Stop": [{"matcher": "", "hooks": stop_hooks}],
        },
    }


def _write_json(path, obj, *, trailing_newline=True):
    path.write_text(json.dumps(obj, indent=2) + ("\n" if trailing_newline else ""))


def _apply_ns(config, receipt, *, expect=None, dry_run=False, old=OLD, new=NEW):
    return argparse.Namespace(
        config=str(config),
        old_prefix=old,
        new_prefix=new,
        receipt=str(receipt),
        expect=expect,
        dry_run=dry_run,
    )


def _rollback_ns(receipt, *, force=False):
    return argparse.Namespace(receipt=str(receipt), force=force)


def _reconcile_ns(
    config, receipt, from_receipt, *, expect=None, recovery=False, old=OLD, new=NEW
):
    return argparse.Namespace(
        config=str(config),
        old_prefix=old,
        new_prefix=new,
        receipt=str(receipt),
        from_receipt=None if from_receipt is None else str(from_receipt),
        recovery=recovery,
        expect=expect,
    )


def _owned_commands(cfg_dict, prefix):
    return sorted(
        h["command"]
        for _, _, _, _, h in repoint._iter_handlers(cfg_dict)
        if repoint._owned_script(h["command"], prefix)
    )


def _legacy_receipt(config_realpath, *, old=OLD, new=NEW, changed=None):
    return {
        "config_path": config_realpath,
        "old_prefix": old,
        "new_prefix": new,
        "pre_sha256": "0" * 64,
        "changed_commands": changed
        if changed is not None
        else [old + name for name in OWNED_ORDER],
        "mode": "0o644",
    }


# --------------------------------------------------------------------------- apply


def test_apply_repoints_owned_only_and_writes_schema2_receipt(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(OLD))
    os.chmod(cfg, 0o600)
    receipt = tmp_path / "receipt.json"

    assert repoint.cmd_apply(_apply_ns(cfg, receipt, expect=6)) == 0

    result = json.loads(cfg.read_text())
    # all 6 owned hooks now new-prefixed; none left at old prefix
    assert len(_owned_commands(result, NEW)) == 6
    assert _owned_commands(result, OLD) == []
    # non-owned neighbors untouched
    all_cmds = [h["command"] for _, _, _, _, h in repoint._iter_handlers(result)]
    assert "/proj/.claude/hooks/lint.sh" in all_cmds
    assert "/proj/.claude/hooks/scribe.sh" in all_cmds
    # mode preserved
    assert oct(os.stat(cfg).st_mode & 0o777) == "0o600"

    rec = json.loads(receipt.read_text())
    assert rec["schema"] == 2 and rec["kind"] == "apply"
    assert rec["pre_hooks"] == _config(OLD)["hooks"]
    assert rec["post_hooks"] == result["hooks"]
    assert len(rec["changed_commands"]) == 6


def test_apply_noop_when_no_owned_command_uses_old_prefix(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(NEW))  # already at NEW
    before = cfg.read_text()
    receipt = tmp_path / "receipt.json"

    assert repoint.cmd_apply(_apply_ns(cfg, receipt)) == 0
    assert cfg.read_text() == before
    assert not receipt.exists()


def test_apply_expect_mismatch_refuses_without_writing(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(OLD))
    before = cfg.read_text()
    receipt = tmp_path / "receipt.json"

    with pytest.raises(SystemExit):
        repoint.cmd_apply(_apply_ns(cfg, receipt, expect=5))
    assert cfg.read_text() == before
    assert not receipt.exists()


def test_apply_dry_run_writes_nothing(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(OLD))
    before = cfg.read_text()
    receipt = tmp_path / "receipt.json"

    assert repoint.cmd_apply(_apply_ns(cfg, receipt, dry_run=True)) == 0
    assert cfg.read_text() == before
    assert not receipt.exists()


# ------------------------------------------------------------------------ rollback


def test_rollback_restores_pre_when_current_is_post(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(OLD))
    receipt = tmp_path / "receipt.json"
    assert repoint.cmd_apply(_apply_ns(cfg, receipt, expect=6)) == 0

    assert repoint.cmd_rollback(_rollback_ns(receipt)) == 0
    restored = json.loads(cfg.read_text())
    assert restored["hooks"] == _config(OLD)["hooks"]
    # other top-level fields survive
    assert restored["model"] == "opus"
    assert restored["permissions"] == {"allow": ["Bash"]}


def test_rollback_noop_when_current_is_pre(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(OLD))
    receipt = tmp_path / "receipt.json"
    assert repoint.cmd_apply(_apply_ns(cfg, receipt, expect=6)) == 0
    assert repoint.cmd_rollback(_rollback_ns(receipt)) == 0
    after_first = cfg.read_text()
    # second rollback: current already == pre-image -> idempotent no-op
    assert repoint.cmd_rollback(_rollback_ns(receipt)) == 0
    assert cfg.read_text() == after_first


def test_rollback_conflict_on_any_third_tree(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(OLD))
    receipt = tmp_path / "receipt.json"
    assert repoint.cmd_apply(_apply_ns(cfg, receipt, expect=6)) == 0

    # An unrelated later change to the hooks tree invalidates the whole-tree
    # receipt: rollback must refuse, writing nothing.
    drifted = json.loads(cfg.read_text())
    drifted["hooks"]["SessionStart"].append(
        {"matcher": "", "hooks": [{"type": "command", "command": "/proj/new.sh"}]}
    )
    _write_json(cfg, drifted)
    before = cfg.read_text()

    with pytest.raises(SystemExit):
        repoint.cmd_rollback(_rollback_ns(receipt))
    assert cfg.read_text() == before  # untouched


def test_rollback_conflict_refuses_even_with_force(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(OLD))
    receipt = tmp_path / "receipt.json"
    assert repoint.cmd_apply(_apply_ns(cfg, receipt, expect=6)) == 0
    drifted = json.loads(cfg.read_text())
    drifted["hooks"]["Stop"][0]["hooks"].append(
        {"type": "command", "command": "/proj/extra.sh"}
    )
    _write_json(cfg, drifted)
    before = cfg.read_text()
    with pytest.raises(SystemExit):
        repoint.cmd_rollback(_rollback_ns(receipt, force=True))
    assert cfg.read_text() == before


def test_rollback_refuses_legacy_schema1_receipt(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(NEW))
    legacy = tmp_path / "legacy.json"
    _write_json(legacy, _legacy_receipt(os.path.realpath(str(cfg))))
    with pytest.raises(SystemExit):
        repoint.cmd_rollback(_rollback_ns(legacy))


# ----------------------------------------------------------------------- reconcile


def test_reconcile_writes_truthful_current_state_recovery(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(NEW))  # current live state is the post-image (R1)
    legacy = tmp_path / "legacy.json"
    _write_json(legacy, _legacy_receipt(os.path.realpath(str(cfg))))
    out = tmp_path / "recovery.json"

    assert repoint.cmd_reconcile(_reconcile_ns(cfg, out, legacy, expect=6)) == 0
    rec = json.loads(out.read_text())
    assert rec["schema"] == 2
    assert rec["kind"] == "current-state-recovery"  # never historical-upgrade
    assert rec["kind"] != "historical-upgrade"
    assert rec["reconciled_from"] == str(legacy)
    assert rec["verified_against_legacy"] == {
        "config_path_realpath_match": True,
        "prefix_match": True,
        "owned_command_whole_tree_match": True,
    }
    assert "current-state" in rec["note"]
    # pre_hooks reconstructed to OLD prefix; post_hooks == current NEW tree
    assert _owned_commands({"hooks": rec["pre_hooks"]}, OLD) == sorted(
        OLD + name for name in OWNED_ORDER
    )
    assert rec["post_hooks"] == _config(NEW)["hooks"]


def test_reconcile_requires_from_receipt(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(NEW))
    out = tmp_path / "recovery.json"
    with pytest.raises(SystemExit):
        repoint.cmd_reconcile(_reconcile_ns(cfg, out, None))
    assert not out.exists()


def test_reconcile_from_receipt_nonexistent_refuses(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(NEW))
    out = tmp_path / "recovery.json"
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(SystemExit):
        repoint.cmd_reconcile(_reconcile_ns(cfg, out, missing))
    assert not out.exists()


def test_recovery_flag_does_not_bypass_from_receipt(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(NEW))
    out = tmp_path / "recovery.json"
    # --recovery with no --from-receipt still refuses
    with pytest.raises(SystemExit):
        repoint.cmd_reconcile(_reconcile_ns(cfg, out, None, recovery=True))
    assert not out.exists()
    # --recovery with a nonexistent --from-receipt still refuses
    missing = tmp_path / "nope.json"
    with pytest.raises(SystemExit):
        repoint.cmd_reconcile(_reconcile_ns(cfg, out, missing, recovery=True))
    assert not out.exists()


def test_reconcile_refuses_config_path_mismatch(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(NEW))
    legacy = tmp_path / "legacy.json"
    _write_json(legacy, _legacy_receipt("/some/other/settings.local.json"))
    out = tmp_path / "recovery.json"
    with pytest.raises(SystemExit):
        repoint.cmd_reconcile(_reconcile_ns(cfg, out, legacy))
    assert not out.exists()


def test_reconcile_refuses_prefix_mismatch(tmp_path):
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(NEW))
    legacy = tmp_path / "legacy.json"
    _write_json(legacy, _legacy_receipt(os.path.realpath(str(cfg))))
    out = tmp_path / "recovery.json"
    with pytest.raises(SystemExit):
        repoint.cmd_reconcile(_reconcile_ns(cfg, out, legacy, old="/wrong/old/hooks/"))
    assert not out.exists()


def test_reconcile_refuses_owned_set_mismatch(tmp_path):
    cfg = tmp_path / "settings.local.json"
    # current config is missing one owned hook, so the reconstructed old-prefix
    # set can no longer match the legacy receipt's 6 changed_commands.
    _write_json(cfg, _config(NEW, drop="mycelium-read-tracker.sh"))
    legacy = tmp_path / "legacy.json"
    _write_json(legacy, _legacy_receipt(os.path.realpath(str(cfg))))
    out = tmp_path / "recovery.json"
    with pytest.raises(SystemExit):
        repoint.cmd_reconcile(_reconcile_ns(cfg, out, legacy))
    assert not out.exists()


def test_reconcile_receipt_is_a_valid_rollback_basis(tmp_path):
    """The schema-2 recovery receipt reconcile writes must drive a real rollback.

    current (NEW) == recovery.post_hooks -> rollback restores recovery.pre_hooks
    (OLD-prefixed), i.e. the R1 recovery receipt genuinely reverts R1.
    """
    cfg = tmp_path / "settings.local.json"
    _write_json(cfg, _config(NEW))
    legacy = tmp_path / "legacy.json"
    _write_json(legacy, _legacy_receipt(os.path.realpath(str(cfg))))
    out = tmp_path / "recovery.json"
    assert repoint.cmd_reconcile(_reconcile_ns(cfg, out, legacy)) == 0

    assert repoint.cmd_rollback(_rollback_ns(out)) == 0
    restored = json.loads(cfg.read_text())
    assert _owned_commands(restored, OLD) == sorted(OLD + name for name in OWNED_ORDER)
    assert _owned_commands(restored, NEW) == []
    assert restored["model"] == "opus"  # non-hook fields preserved
