"""Regression suite for scripts/wfctl.py v2.

Every wfctl invocation here runs in a subprocess with WFCTL_HOME / WFCTL_REPO pointed at
pytest tmp dirs, so the real home directory is never a candidate target (asserted in T11).

Run from the repo root:  python3 -m pytest tests/test_wfctl.py -q
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WFCTL = REPO_ROOT / "scripts" / "wfctl.py"
REAL_HOME = Path.home()


def _dir_baseline(p: Path):
    """Baseline of a runtime state directory that a clean release CLONE legitimately omits.

    Returns ``None`` when the directory is ABSENT and a sorted list of entry names when it exists
    (``[]`` for present-but-empty). Keeping absent (``None``) distinct from created-empty (``[]``)
    lets T11 assert the exact baseline is unchanged: a suite that created the directory — even empty —
    would flip ``None`` -> ``[]`` and fail, while a clean clone that never had ``snapshots/`` or
    ``installed/`` collects and passes instead of raising ``FileNotFoundError`` at import (seq235)."""
    if not p.is_dir():
        return None
    return sorted(q.name for q in p.iterdir())


# state of the real repo at import time — T11 asserts it is untouched
REAL_SNAPSHOTS_AT_IMPORT = _dir_baseline(REPO_ROOT / "snapshots")
REAL_INSTALLED_AT_IMPORT = _dir_baseline(REPO_ROOT / "installed")
REAL_RECEIPTS_EXISTED = (REPO_ROOT / "receipts").exists()

# every subprocess invocation is recorded here for the T11 audit
CALLS: list[dict] = []
TMP_ROOTS: list[Path] = []

SRC_FILES = {
    "src/claude/CLAUDE.md": "SRC: claude md v2\n",
    "src/claude/skills/codex-review/SKILL.md": "SRC: codex-review skill\n",
    "src/claude/skills/codex-review/references/global-conventions.md": "SRC: optional conventions\n",
    "src/claude/tools/codex_ask.sh": "#!/bin/sh\necho 'SRC codex_ask'\n",
    "src/claude/tools/codex_launch.py": "#!/usr/bin/env python3\n# SRC codex_launch stub\n",
    "src/claude/tools/sporedrive_review_guard.py": (
        "#!/usr/bin/env python3\n# SRC sporedrive_review_guard stub\n"
    ),
    "src/claude/skills/prompt-optimizer/SKILL.md": "SRC: prompt-optimizer skill\n",
    "src/claude/skills/prompt-optimizer/references/scoring-design.md": (
        "SRC: prompt-optimizer scoring design\n"
    ),
    "src/claude/skills/run-pipeline-local/SKILL.md": "SRC: run-pipeline-local skill\n",
    "src/claude/skills/run-pipeline-local/refs/model_selection.md": (
        "SRC: run-pipeline-local model selection\n"
    ),
    "src/codex/AGENTS.md": "SRC: agents md\n",
    "src/codex/skills/cmux-driver/SKILL.md": "SRC: cmux driver skill\n",
    "src/codex/skills/cmux-driver/references/x.md": "SRC: cmux reference x\n",
}
SETTINGS_FIELDS = {"skillOverrides": {"offer-k-dense-web": "off"}}

DEST_ORIG = {
    ".claude/CLAUDE.md": "HOME: claude md original\n",
    ".claude/skills/codex-review/SKILL.md": "HOME: codex-review original\n",
    ".claude/tools/codex_ask.sh": "#!/bin/sh\necho 'HOME codex_ask original'\n",
    ".claude/skills/prompt-optimizer/SKILL.md": "HOME: prompt-optimizer original\n",
    ".claude/skills/prompt-optimizer/references/scoring-design.md": (
        "HOME: prompt-optimizer scoring design original\n"
    ),
    ".claude/skills/run-pipeline-local/SKILL.md": "HOME: run-pipeline-local original\n",
    ".claude/skills/run-pipeline-local/refs/model_selection.md": (
        "HOME: run-pipeline-local model selection original\n"
    ),
    ".codex/AGENTS.md": "HOME: agents original\n",
    ".codex/skills/cmux-driver/SKILL.md": "HOME: cmux driver original\n",
    ".codex/skills/cmux-driver/references/x.md": "HOME: cmux reference original\n",
}
DEST_MODES = {
    ".claude/CLAUDE.md": 0o640,
    ".claude/tools/codex_ask.sh": 0o755,
}
HOOK_FILES = {
    ".claude/hooks/session-resume.sh": "#!/bin/sh\necho resume\n",
    ".claude/hooks/session-save-reminder.sh": "#!/bin/sh\necho save\n",
    ".claude/hooks/cost-tracker.sh": "#!/bin/sh\necho cost\n",
}
SETTINGS_DOC = {
    "model": "claude-fable-5-1[1m]",
    "enabledPlugins": {"mycelium@local": True},
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {"type": "command", "command": "~/.claude/hooks/session-resume.sh"}
                ]
            }
        ]
    },
    "skillOverrides": {"analytics-audit": "off", "auto-research": "off"},
}

FILE_TARGETS = [
    (".claude/CLAUDE.md", "src/claude/CLAUDE.md"),
    (".claude/skills/codex-review/SKILL.md", "src/claude/skills/codex-review/SKILL.md"),
    (
        ".claude/skills/codex-review/references/global-conventions.md",
        "src/claude/skills/codex-review/references/global-conventions.md",
    ),
    (".claude/tools/codex_ask.sh", "src/claude/tools/codex_ask.sh"),
    (".claude/tools/codex_launch.py", "src/claude/tools/codex_launch.py"),
    (
        ".claude/tools/sporedrive_review_guard.py",
        "src/claude/tools/sporedrive_review_guard.py",
    ),
    (".codex/AGENTS.md", "src/codex/AGENTS.md"),
]
DIR_TARGET = (".codex/skills/cmux-driver", "src/codex/skills/cmux-driver")
DIR_TARGET_PROMPT_OPT = (
    ".claude/skills/prompt-optimizer",
    "src/claude/skills/prompt-optimizer",
)
DIR_TARGET_RUN_PIPELINE = (
    ".claude/skills/run-pipeline-local",
    "src/claude/skills/run-pipeline-local",
)


def _write(p: Path, text: str, mode: int | None = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if mode is not None:
        os.chmod(p, mode)


class Env:
    def __init__(self, home: Path, repo: Path):
        self.home = home
        self.repo = repo

    # -- invocation ---------------------------------------------------
    def run(
        self, *args: str, extra_env: dict | None = None
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("WFCTL_FAIL_AT", None)
        env["WFCTL_HOME"] = str(self.home)
        env["WFCTL_REPO"] = str(self.repo)
        if extra_env:
            env.update(extra_env)
        CALLS.append(
            {
                "args": list(args),
                "WFCTL_HOME": env.get("WFCTL_HOME"),
                "WFCTL_REPO": env.get("WFCTL_REPO"),
            }
        )
        return subprocess.run(
            [sys.executable, str(WFCTL), *args],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(self.repo),
        )

    # -- inspection ---------------------------------------------------
    def dest_text(self, rel: str) -> str:
        return (self.home / rel).read_text()

    def dest_state(self) -> dict[str, str]:
        return {rel: (self.home / rel).read_text() for rel in DEST_ORIG}

    def mode(self, rel: str) -> int:
        return stat.S_IMODE((self.home / rel).stat().st_mode)

    def settings(self) -> dict:
        return json.loads((self.home / ".claude/settings.json").read_text())

    def snap(self, label: str) -> Path:
        return self.repo / "snapshots" / label


@pytest.fixture
def env(tmp_path: Path) -> Env:
    home = (tmp_path / "home").resolve()
    repo = (tmp_path / "repo").resolve()
    TMP_ROOTS.append(tmp_path.resolve())
    for rel, text in SRC_FILES.items():
        _write(repo / rel, text, 0o644)
    os.chmod(repo / "src/claude/tools/codex_ask.sh", 0o755)
    _write(
        repo / "src/claude/settings.fields.json",
        json.dumps(SETTINGS_FIELDS, indent=2) + "\n",
    )
    for rel, text in DEST_ORIG.items():
        _write(home / rel, text, DEST_MODES.get(rel, 0o644))
    for rel, text in HOOK_FILES.items():
        _write(home / rel, text, 0o755)
    _write(
        home / ".claude/settings.json", json.dumps(SETTINGS_DOC, indent=2) + "\n", 0o600
    )
    return Env(home, repo)


def src_hash(env: Env, rel: str) -> str:
    import hashlib

    return hashlib.sha256((env.repo / rel).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# T1 — preflight rejects a missing source before touching anything
# ---------------------------------------------------------------------------


def test_t1_preflight_missing_source_mutates_nothing(env: Env):
    before = env.dest_state()
    before_settings = env.settings()
    (env.repo / "src/claude/skills/codex-review/SKILL.md").unlink()

    r = env.run("install", "--label", "L1")

    assert r.returncode != 0, r.stdout
    assert "missing source file" in r.stderr
    assert "nothing was changed" in r.stderr
    assert env.dest_state() == before
    assert env.settings() == before_settings
    assert not env.snap("L1").exists()
    assert (
        not (env.repo / "snapshots").exists()
        or list((env.repo / "snapshots").iterdir()) == []
    )


def test_t1b_preflight_rejects_stale_staging_and_bad_settings(env: Env):
    stale = env.home / ".claude/.CLAUDE.md.wfctl-staging"
    _write(stale, "leftover\n")
    r = env.run("install", "--label", "L1b")
    assert r.returncode == 2
    assert "stale staging artifact" in r.stderr
    stale.unlink()

    (env.home / ".claude/settings.json").write_text("[1, 2, 3]\n")
    r = env.run("install", "--label", "L1b")
    assert r.returncode == 2
    assert "not a JSON object" in r.stderr

    (env.home / ".claude/settings.json").write_text(
        '{"skillOverrides": "not-a-dict"}\n'
    )
    r = env.run("install", "--label", "L1b")
    assert r.returncode == 2
    assert "parent is not a dict" in r.stderr
    assert not env.snap("L1b").exists()


# ---------------------------------------------------------------------------
# T2 — runtime failure mid-commit recovers to the pre-install state
# ---------------------------------------------------------------------------


def test_t2_fault_injection_recovers(env: Env):
    before = env.dest_state()
    before_settings = env.settings()

    r = env.run("install", "--label", "L2", extra_env={"WFCTL_FAIL_AT": "step-3"})

    assert r.returncode != 0
    assert "install FAILED" in r.stderr
    assert env.dest_state() == before, (
        "destinations must equal their originals after recovery"
    )
    assert env.settings() == before_settings

    receipt = json.loads((env.repo / "installed/transaction-L2.json").read_text())
    assert receipt["status"] == "failed"
    assert receipt["recovered"] is True
    by_id = {s["id"]: s for s in receipt["steps"]}
    assert (
        by_id["step-1"]["status"] == "rolled-back"
        and by_id["step-1"]["recovered"] is True
    )
    assert (
        by_id["step-2"]["status"] == "rolled-back"
        and by_id["step-2"]["recovered"] is True
    )
    assert by_id["step-3"]["status"] == "not-run"
    assert by_id["settings"]["status"] == "not-run"
    # no staging artifacts left behind
    leftovers = [
        p
        for p in env.home.rglob("*")
        if p.name.endswith((".wfctl-staging", ".wfctl-tmp", ".wfctl-old"))
    ]
    assert leftovers == []
    # a failed install records no postimage
    assert not (env.snap("L2") / "postimage.json").exists()
    assert not (env.repo / "installed/history.jsonl").exists()


# ---------------------------------------------------------------------------
# T3 — successful install
# ---------------------------------------------------------------------------


def test_t3_install_success(env: Env):
    r = env.run("install", "--label", "L3")
    assert r.returncode == 0, r.stderr

    for dest_rel, src_rel in FILE_TARGETS:
        assert env.dest_text(dest_rel) == SRC_FILES[src_rel]
    assert (
        env.dest_text(".codex/skills/cmux-driver/SKILL.md")
        == SRC_FILES["src/codex/skills/cmux-driver/SKILL.md"]
    )
    assert (
        env.dest_text(".claude/skills/prompt-optimizer/SKILL.md")
        == SRC_FILES["src/claude/skills/prompt-optimizer/SKILL.md"]
    )
    assert (
        env.dest_text(".claude/skills/run-pipeline-local/SKILL.md")
        == SRC_FILES["src/claude/skills/run-pipeline-local/SKILL.md"]
    )

    post = json.loads((env.snap("L3") / "postimage.json").read_text())
    for dest_rel, src_rel in FILE_TARGETS:
        assert post["targets"][str(env.home / dest_rel)]["hash"] == src_hash(
            env, src_rel
        )
    dir_entry = post["targets"][str(env.home / DIR_TARGET[0])]["hash"]
    assert dir_entry == {
        "SKILL.md": src_hash(env, "src/codex/skills/cmux-driver/SKILL.md"),
        "references/x.md": src_hash(
            env, "src/codex/skills/cmux-driver/references/x.md"
        ),
    }
    dir_entry_po = post["targets"][str(env.home / DIR_TARGET_PROMPT_OPT[0])]["hash"]
    assert dir_entry_po == {
        "SKILL.md": src_hash(env, "src/claude/skills/prompt-optimizer/SKILL.md"),
        "references/scoring-design.md": src_hash(
            env, "src/claude/skills/prompt-optimizer/references/scoring-design.md"
        ),
    }
    dir_entry_rpl = post["targets"][str(env.home / DIR_TARGET_RUN_PIPELINE[0])]["hash"]
    assert dir_entry_rpl == {
        "SKILL.md": src_hash(env, "src/claude/skills/run-pipeline-local/SKILL.md"),
        "refs/model_selection.md": src_hash(
            env, "src/claude/skills/run-pipeline-local/refs/model_selection.md"
        ),
    }
    assert post["leaves"]["skillOverrides.offer-k-dense-web"] == {
        "present": True,
        "value": "off",
    }

    assert env.settings()["skillOverrides"]["offer-k-dense-web"] == "off"
    leaves = json.loads((env.snap("L3") / "owned-leaves.json").read_text())
    assert leaves["skillOverrides.offer-k-dense-web"]["present"] is False
    assert leaves["skillOverrides.offer-k-dense-web"]["parent_created"] is False

    assert env.mode(".claude/settings.json") == 0o600
    assert env.mode(".claude/CLAUDE.md") == 0o640
    assert env.mode(".claude/tools/codex_ask.sh") == 0o755

    assert env.run("verify").returncode == 0
    hist = (env.repo / "installed/history.jsonl").read_text().strip().splitlines()
    assert len(hist) == 1 and json.loads(hist[0])["label"] == "L3"


# ---------------------------------------------------------------------------
# T4 — rollback restores only what wfctl owns
# ---------------------------------------------------------------------------


def test_t4_rollback_restores_only_owned(env: Env):
    assert env.run("install", "--label", "L4").returncode == 0

    s = env.settings()
    s["enabledPlugins"]["newplugin"] = True
    s["model"] = "claude-opus-5[1m]"
    s["skillOverrides"]["other"] = "on"
    _write(env.home / ".claude/settings.json", json.dumps(s, indent=2) + "\n", 0o600)
    _write(
        env.home / ".claude/hooks/cost-tracker.sh", "#!/bin/sh\necho EDITED\n", 0o755
    )

    r = env.run("rollback", "L4")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "evidence-only (not restored)" in r.stdout

    assert env.dest_state() == {rel: text for rel, text in DEST_ORIG.items()}
    assert not (env.home / ".claude/skills/codex-review/references/global-conventions.md").exists()
    out = env.settings()
    assert "offer-k-dense-web" not in out["skillOverrides"]  # absent, not null
    assert out["skillOverrides"]["other"] == "on"
    assert out["skillOverrides"]["analytics-audit"] == "off"
    assert out["enabledPlugins"]["newplugin"] is True
    assert out["model"] == "claude-opus-5[1m]"
    assert env.dest_text(".claude/hooks/cost-tracker.sh") == "#!/bin/sh\necho EDITED\n"

    receipts = sorted((env.repo / "receipts").glob("rollback-L4-*.json"))
    assert len(receipts) == 1
    rec = json.loads(receipts[0].read_text())
    assert rec["force"] is False
    assert (
        len(rec["restored"]) == 10
    )  # 5 original targets + codex_launch.py + review guard + prompt-optimizer + run-pipeline-local dirs + optional conventions
    assert any("cost-tracker.sh" in p for p in rec["skipped_evidence_only"])
    assert rec["settings_leaves"][0]["action"] == "delete"


# ---------------------------------------------------------------------------
# T5 — conflicts
# ---------------------------------------------------------------------------


def test_t5_rollback_conflict_needs_force(env: Env):
    assert env.run("install", "--label", "L5").returncode == 0
    _write(env.home / ".claude/CLAUDE.md", "USER EDIT after install\n", 0o640)

    r = env.run("rollback", "L5")
    assert r.returncode == 1
    assert "CONFLICT" in r.stderr and "diverged" in r.stderr
    assert env.dest_text(".claude/CLAUDE.md") == "USER EDIT after install\n"
    assert env.settings()["skillOverrides"]["offer-k-dense-web"] == "off"

    r = env.run("rollback", "L5", "--force")
    assert r.returncode == 0, r.stderr
    assert env.dest_text(".claude/CLAUDE.md") == DEST_ORIG[".claude/CLAUDE.md"]
    saved = list((env.snap("L5") / "conflicts").rglob(".claude/CLAUDE.md"))
    assert len(saved) == 1
    assert saved[0].read_text() == "USER EDIT after install\n"
    assert stat.S_IMODE(saved[0].stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# T6 — tampered snapshot copy
# ---------------------------------------------------------------------------


def test_t6_tampered_snapshot_refused(env: Env):
    assert env.run("install", "--label", "L6").returncode == 0
    copy = env.snap("L6") / "files/.claude/CLAUDE.md"
    copy.write_text(copy.read_text() + "tampered\n")

    r = env.run("rollback", "L6")
    assert r.returncode != 0
    assert "snapshot bytes altered" in (r.stderr + r.stdout)
    assert env.dest_text(".claude/CLAUDE.md") == SRC_FILES["src/claude/CLAUDE.md"]

    r = env.run("rollback", "L6", "--force")
    assert r.returncode != 0
    assert "snapshot bytes altered" in (r.stderr + r.stdout)


# ---------------------------------------------------------------------------
# T7 — privacy
# ---------------------------------------------------------------------------


def test_t7_privacy_modes_receipt_and_redaction(env: Env):
    assert env.run("install", "--label", "L7").returncode == 0
    snap = env.snap("L7")
    assert stat.S_IMODE(snap.stat().st_mode) == 0o700
    for p in snap.rglob("*"):
        want = 0o700 if p.is_dir() else 0o600
        assert stat.S_IMODE(p.stat().st_mode) == want, f"{p} mode"

    receipt_text = (env.repo / "receipts/L7.json").read_text()
    for text in list(DEST_ORIG.values()) + list(HOOK_FILES.values()):
        assert text.strip() not in receipt_text, "receipt must not carry file contents"
    receipt = json.loads(receipt_text)
    assert receipt["owned"][0]["sha256"]
    assert (
        receipt["owned_leaves"]["skillOverrides.offer-k-dense-web"]["present"] is False
    )
    assert "settings_full" not in receipt

    # a snapshot whose owned-leaves carries the scrubbed placeholder is not restorable
    assert env.run("snapshot", "L7b").returncode == 0
    leaves_p = env.snap("L7b") / "owned-leaves.json"
    leaves = json.loads(leaves_p.read_text())
    leaves["skillOverrides.offer-k-dense-web"] = {
        "present": True,
        "value": "<redacted>",
        "segments": ["skillOverrides", "offer-k-dense-web"],
        "parent_created": False,
    }
    leaves_p.write_text(json.dumps(leaves, indent=2) + "\n")
    r = env.run("rollback", "L7b", "--force")
    assert r.returncode == 5
    assert "REFUSING" in r.stderr
    assert env.settings()["skillOverrides"]["offer-k-dense-web"] == "off"


# ---------------------------------------------------------------------------
# T8 — absent vs null
# ---------------------------------------------------------------------------


def test_t8_null_leaf_restored_as_null(env: Env):
    s = env.settings()
    s["skillOverrides"]["offer-k-dense-web"] = None
    _write(env.home / ".claude/settings.json", json.dumps(s, indent=2) + "\n", 0o600)

    assert env.run("install", "--label", "L8").returncode == 0
    assert env.settings()["skillOverrides"]["offer-k-dense-web"] == "off"
    leaves = json.loads((env.snap("L8") / "owned-leaves.json").read_text())
    assert leaves["skillOverrides.offer-k-dense-web"] == {
        "present": True,
        "value": None,
        "segments": ["skillOverrides", "offer-k-dense-web"],
        "parent_created": False,
    }

    assert env.run("rollback", "L8").returncode == 0
    out = env.settings()
    assert "offer-k-dense-web" in out["skillOverrides"]
    assert out["skillOverrides"]["offer-k-dense-web"] is None


# ---------------------------------------------------------------------------
# T9 — install refuses drift
# ---------------------------------------------------------------------------


def test_t9_install_refuses_drift(env: Env):
    assert env.run("install", "--label", "L9").returncode == 0
    _write(env.home / ".claude/CLAUDE.md", "USER EDIT before second install\n", 0o640)

    r = env.run("install", "--label", "L9b")
    assert r.returncode == 2
    assert "diverged from every recorded wfctl postimage" in r.stderr
    assert env.dest_text(".claude/CLAUDE.md") == "USER EDIT before second install\n"
    assert not env.snap("L9b").exists()

    r = env.run("install", "--label", "L9b", "--accept-drift")
    assert r.returncode == 0, r.stderr
    assert env.dest_text(".claude/CLAUDE.md") == SRC_FILES["src/claude/CLAUDE.md"]
    # the user's edit survives in the pre-install snapshot
    assert (env.snap("L9b") / "files/.claude/CLAUDE.md").read_text() == (
        "USER EDIT before second install\n"
    )


# ---------------------------------------------------------------------------
# T10 — old-format snapshot
# ---------------------------------------------------------------------------


def test_t10_old_format_snapshot(env: Env):
    import hashlib

    old_claude = "OLD FORMAT snapshot claude md\n"
    old_hook = "#!/bin/sh\necho OLD hook\n"
    snap = env.snap("old-format")
    _write(snap / "files/.claude/CLAUDE.md", old_claude)
    _write(snap / "files/.claude/hooks/cost-tracker.sh", old_hook)
    manifest = {
        "label": "old-format",
        "taken_at": "2026-09-08T16:22:43.375946+00:00",
        "repo_head": "500dedc",
        "host": "test",
        "entries": [
            {
                "path": str(env.home / ".claude/CLAUDE.md"),
                "exists": True,
                "kind": "file",
                "sha256": hashlib.sha256(old_claude.encode()).hexdigest(),
                "size": len(old_claude),
                "snapshot_copy": "files/.claude/CLAUDE.md",
            },
            {
                "path": str(env.home / ".claude/hooks/cost-tracker.sh"),
                "exists": True,
                "kind": "file",
                "sha256": hashlib.sha256(old_hook.encode()).hexdigest(),
                "size": len(old_hook),
                "snapshot_copy": "files/.claude/hooks/cost-tracker.sh",
            },
        ],
        "inspected_only": [],
    }
    _write(snap / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    _write(
        snap / "settings.fields.json",
        json.dumps(
            {"model": "old-model", "skillOverrides": {"analytics-audit": "off"}},
            indent=2,
        )
        + "\n",
    )

    # no history at all -> every owned target is unverifiable, so --force is required
    r = env.run("rollback", "old-format")
    assert r.returncode == 1
    assert "no wfctl postimage on record" in r.stderr
    assert env.dest_text(".claude/CLAUDE.md") == DEST_ORIG[".claude/CLAUDE.md"]

    r = env.run("rollback", "old-format", "--force")
    assert r.returncode == 0, r.stderr
    assert env.dest_text(".claude/CLAUDE.md") == old_claude
    assert "evidence-only (not restored)" in r.stdout
    assert "cost-tracker.sh" in r.stdout
    # the evidence hook was NOT restored
    assert (
        env.dest_text(".claude/hooks/cost-tracker.sh")
        == HOOK_FILES[".claude/hooks/cost-tracker.sh"]
    )
    # untouched owned targets that are absent from the old manifest stay as they were
    assert env.dest_text(".codex/AGENTS.md") == DEST_ORIG[".codex/AGENTS.md"]
    # the model key is evidence only and is never restored from an old snapshot
    assert env.settings()["model"] == SETTINGS_DOC["model"]

    r = env.run("rollback", "old-format", "--force")
    assert r.returncode == 0
    rec = json.loads(
        sorted((env.repo / "receipts").glob("rollback-old-format-*.json"))[
            0
        ].read_text()
    )
    assert rec["snapshot_format"] == 1
    assert any("cost-tracker.sh" in p for p in rec["skipped_evidence_only"])


def test_t10b_list_reports_postimage_and_mode(env: Env):
    assert env.run("install", "--label", "LA").returncode == 0
    assert env.run("snapshot", "LB").returncode == 0
    out = env.run("list").stdout
    assert "LA" in out and "LB" in out
    la = [ln for ln in out.splitlines() if ln.startswith("LA")][0]
    lb = [ln for ln in out.splitlines() if ln.startswith("LB")][0]
    assert "postimage=yes" in la and "mode=0o700" in la
    assert "postimage=no" in lb


# ---------------------------------------------------------------------------
# T11 — the suite never touches the real home or the real repo
# ---------------------------------------------------------------------------


def test_t11_real_home_untouched():
    assert CALLS, "no wfctl invocations were recorded"
    real_home = str(REAL_HOME)
    for call in CALLS:
        assert call["WFCTL_HOME"], f"call without WFCTL_HOME: {call['args']}"
        assert call["WFCTL_REPO"], f"call without WFCTL_REPO: {call['args']}"
        assert call["WFCTL_HOME"] != real_home
        assert not Path(call["WFCTL_HOME"]).is_relative_to(REAL_HOME / ".claude")
        assert any(
            Path(call["WFCTL_HOME"]).is_relative_to(root) for root in TMP_ROOTS
        ), call["WFCTL_HOME"]
        assert any(Path(call["WFCTL_REPO"]).is_relative_to(root) for root in TMP_ROOTS)
    # the real home is not inside any directory the fixtures wrote to
    for root in TMP_ROOTS:
        assert not REAL_HOME.is_relative_to(root)
        assert not REPO_ROOT.is_relative_to(root)
    # the real repo's snapshot/install state is unchanged and no receipts dir appeared. The baseline
    # keeps absent (None) distinct from created-empty ([]), so this catches a suite that created a
    # previously-absent state dir while a clean clone (dir never present) still passes.
    assert _dir_baseline(REPO_ROOT / "snapshots") == REAL_SNAPSHOTS_AT_IMPORT
    assert _dir_baseline(REPO_ROOT / "installed") == REAL_INSTALLED_AT_IMPORT
    assert (REPO_ROOT / "receipts").exists() == REAL_RECEIPTS_EXISTED
