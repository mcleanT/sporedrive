"""Coverage for codex_ask.sh's MANAGED review-launch bound (review R3) and the installer wiring
that ships its runtime dependencies (review R2).

A launch becomes MANAGED when any of -x/-a/-T is given; all three are then required, and codex must
never start without a valid, still-open review-launch reservation claimed through the bundled
sporedrive_review_guard.py. Uses the deterministic fake `codex` CLI stub
(tests/fixtures/fake_codex/codex, kind: simulated — the real codex/ChatGPT-subscription CLI is never
invoked here) plus a temporary $MYCELIUM_COORD_DIR so reservation setup/teardown is exercised
directly against mycelium_coord.execution.ExecutionManager.

Run from the repo root:  python3 -m pytest tests/test_codex_ask_managed_review.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "src" / "claude" / "tools" / "codex_ask.sh"
COORD_PKG = REPO_ROOT / "coordination"
FAKE_CODEX_DIR = Path(__file__).resolve().parent / "fixtures" / "fake_codex"
FAKE_CODEX_BIN = FAKE_CODEX_DIR / "codex"

sys.path.insert(0, str(COORD_PKG))
from mycelium_coord.execution import ExecutionManager  # noqa: E402
from mycelium_coord.store import CoordStore  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _fake_codex_executable():
    mode = FAKE_CODEX_BIN.stat().st_mode
    FAKE_CODEX_BIN.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    yield


def _env(tmp_path: Path, coord_dir: Path, scenario: str) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{FAKE_CODEX_DIR}:{env.get('PATH', '')}"
    env["FAKE_CODEX_SCENARIO"] = scenario
    env["CODEX_ASK_OUTDIR"] = str(tmp_path)
    env["MYCELIUM_COORD_DIR"] = str(coord_dir)
    env["SPOREDRIVE_COORD_PKG"] = str(COORD_PKG)
    return env


def _open_and_reserve(coord_dir: Path, task_id: str, execution_id: str, action_id: str):
    em = ExecutionManager(CoordStore(coord_dir))
    em.open_execution(
        task_id, execution_id=execution_id, scope_ref="/s", authorization_ref="/a"
    )
    em.reserve(task_id, action_id=action_id, kind="review_launch")
    return em


def run_wrapper(
    tmp_path,
    coord_dir,
    scenario,
    *args,
    question="trivial fixture question",
    out_name="out.txt",
):
    out_file = tmp_path / out_name
    argv = ["bash", str(WRAPPER), "-o", str(out_file), *args, question]
    proc = subprocess.run(
        argv,
        cwd=REPO_ROOT,
        env=_env(tmp_path, coord_dir, scenario),
        capture_output=True,
        text=True,
        timeout=30,
    )
    receipt_file = out_file.with_name(out_file.name + ".receipt.json")
    receipt = json.loads(receipt_file.read_text()) if receipt_file.exists() else None
    return proc, out_file, receipt


# ---------------------------------------------------------------------------------------------
# VALID managed launch
# ---------------------------------------------------------------------------------------------


def test_valid_managed_launch_reaches_codex_and_settles(tmp_path):
    coord_dir = tmp_path / "coord"
    em = _open_and_reserve(coord_dir, "task1", "exec1", "action1")

    proc, out_file, receipt = run_wrapper(
        tmp_path, coord_dir, "success", "-x", "exec1", "-a", "action1", "-T", "task1"
    )

    assert proc.returncode == 0, proc.stderr
    assert receipt is not None and receipt["complete"] is True

    res = em.read_execution("task1")["usage"]["reservations"]["action1"]
    assert res["status"] == "settled"
    assert res["launch_identity"]
    assert res["outcome"] == "complete"


# ---------------------------------------------------------------------------------------------
# NO-SUCH reservation — the R3 probe: currently the bug is a wrongful rc0/complete=true
# ---------------------------------------------------------------------------------------------


def test_no_such_reservation_is_refused_before_codex_runs(tmp_path):
    coord_dir = tmp_path / "coord"

    proc, out_file, receipt = run_wrapper(
        tmp_path,
        coord_dir,
        "success",
        "-x",
        "no-such-execution",
        "-a",
        "no-such-action",
        "-T",
        "sometask",
    )

    assert proc.returncode != 0
    assert not out_file.exists() or out_file.stat().st_size == 0
    assert receipt is None  # no receipt.json for a launch that never reached codex


# ---------------------------------------------------------------------------------------------
# PARTIAL managed ref — must fail before codex, exit 65
# ---------------------------------------------------------------------------------------------


def test_partial_managed_ref_fails_before_codex(tmp_path):
    coord_dir = tmp_path / "coord"

    proc, out_file, receipt = run_wrapper(
        tmp_path, coord_dir, "success", "-x", "exec1", "-a", "action1"
    )

    assert proc.returncode == 65
    assert not out_file.exists()
    assert receipt is None


# ---------------------------------------------------------------------------------------------
# REPEATED action use — a second launch against the same reservation must be refused
# ---------------------------------------------------------------------------------------------


def test_repeated_action_use_is_refused(tmp_path):
    coord_dir = tmp_path / "coord"
    _open_and_reserve(coord_dir, "task2", "exec2", "action2")

    proc1, out1, receipt1 = run_wrapper(
        tmp_path,
        coord_dir,
        "success",
        "-x",
        "exec2",
        "-a",
        "action2",
        "-T",
        "task2",
        question="first launch",
        out_name="out1.txt",
    )
    assert proc1.returncode == 0, proc1.stderr
    assert receipt1["complete"] is True

    proc2, out2, receipt2 = run_wrapper(
        tmp_path,
        coord_dir,
        "success",
        "-x",
        "exec2",
        "-a",
        "action2",
        "-T",
        "task2",
        question="second launch",
        out_name="out2.txt",
    )
    assert proc2.returncode != 0
    assert not out2.exists() or out2.stat().st_size == 0
    assert receipt2 is None


# ---------------------------------------------------------------------------------------------
# MANAGED deadline capping: -t 1 + a slow reviewer must still be terminated at ~1s
# ---------------------------------------------------------------------------------------------


def test_managed_deadline_caps_and_times_out(tmp_path):
    coord_dir = tmp_path / "coord"
    em = _open_and_reserve(coord_dir, "task3", "exec3", "action3")

    t0 = time.monotonic()
    proc, out_file, receipt = run_wrapper(
        tmp_path,
        coord_dir,
        "slow",
        "-x",
        "exec3",
        "-a",
        "action3",
        "-T",
        "task3",
        "-t",
        "1",
    )
    elapsed = time.monotonic() - t0

    assert proc.returncode == 124
    assert (
        elapsed < 20
    )  # actually terminated near the 1s deadline, not the fixture's 30s sleep
    assert receipt is not None
    assert receipt["parse_status"] == "timeout"
    assert receipt["complete"] is False

    res = em.read_execution("task3")["usage"]["reservations"]["action3"]
    assert res["status"] == "settled"
    assert res["outcome"] == "timeout"


# ---------------------------------------------------------------------------------------------
# LEGACY (unmanaged) launch — byte-identical behaviour, no guard involvement
# ---------------------------------------------------------------------------------------------


def test_legacy_unmanaged_launch_unchanged(tmp_path):
    coord_dir = tmp_path / "coord"

    proc, out_file, receipt = run_wrapper(tmp_path, coord_dir, "success")

    assert proc.returncode == 0
    assert proc.stdout.strip() == str(out_file)
    assert receipt is not None
    assert receipt["complete"] is True


# ---------------------------------------------------------------------------------------------
# R2 — the installer must ship codex_launch.py and sporedrive_review_guard.py alongside
# codex_ask.sh, so an INSTALLED wrapper's -t path finds the launcher (previously exit 70).
# ---------------------------------------------------------------------------------------------


def test_r2_installed_wrapper_finds_launcher(tmp_path):
    # A fake repo whose src/ tree is an exact copy of this repo's src/ — every wfctl TARGETS
    # source file resolves (preflight requirement) without mutating the real repo's own
    # snapshots/installed state (those live under WFCTL_REPO, so a real WFCTL_REPO would pollute
    # this checkout).
    fake_repo = tmp_path / "fake_repo"
    shutil.copytree(REPO_ROOT / "src", fake_repo / "src")

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}\n")
    # wfctl's stage_all() copies each file target straight into its destination directory
    # without mkdir'ing parents (it assumes a real ~/.claude tree already has them from a prior
    # install) — pre-create every TARGETS destination's parent dir here to match that.
    wfctl_spec = importlib.util.spec_from_file_location(
        "_wfctl_for_targets", REPO_ROOT / "scripts" / "wfctl.py"
    )
    wfctl_mod = importlib.util.module_from_spec(wfctl_spec)
    wfctl_spec.loader.exec_module(wfctl_mod)
    for _kind, _src_rel, dest_rel in wfctl_mod.TARGETS:
        (home / dest_rel).parent.mkdir(parents=True, exist_ok=True)

    install_env = dict(os.environ)
    install_env.pop("WFCTL_FAIL_AT", None)
    install_env["WFCTL_HOME"] = str(home)
    install_env["WFCTL_REPO"] = str(fake_repo)
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "wfctl.py"),
            "install",
            "--label",
            "r2test",
        ],
        env=install_env,
        capture_output=True,
        text=True,
        cwd=str(fake_repo),
    )
    assert proc.returncode == 0, proc.stderr

    installed_wrapper = home / ".claude" / "tools" / "codex_ask.sh"
    installed_launcher = home / ".claude" / "tools" / "codex_launch.py"
    installed_guard = home / ".claude" / "tools" / "sporedrive_review_guard.py"
    assert installed_wrapper.is_file()
    assert installed_launcher.is_file()
    assert installed_guard.is_file()

    out_file = tmp_path / "installed_out.txt"
    run_env = dict(os.environ)
    run_env["PATH"] = f"{FAKE_CODEX_DIR}:{run_env.get('PATH', '')}"
    run_env["FAKE_CODEX_SCENARIO"] = "slow"
    run_env["CODEX_ASK_OUTDIR"] = str(tmp_path)
    run_proc = subprocess.run(
        [
            "bash",
            str(installed_wrapper),
            "-t",
            "1",
            "-o",
            str(out_file),
            "trivial question",
        ],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run_proc.returncode != 70, (
        run_proc.stderr
    )  # launcher present, not "not found"
    assert (
        run_proc.returncode == 124
    )  # reached the deadline path and timed out as expected
