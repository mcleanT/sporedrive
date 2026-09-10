"""Deterministic tests for codex_launch.py — the owned-process deadline launcher used by
codex_ask.sh -t. No real codex is involved; fake child commands stand in for the reviewer process.

Run from the repo root:  python3 -m pytest tests/test_codex_launch.py -q
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "src" / "claude" / "tools" / "codex_launch.py"


def _run(argv, stdin_text="", timeout=30):
    return subprocess.run([sys.executable, str(LAUNCHER), *argv], input=stdin_text,
                          capture_output=True, text=True, timeout=timeout)


def test_fast_passthrough_returns_child_rc_and_captures_output(tmp_path):
    out = tmp_path / "o.txt"
    proc = _run(["--deadline", "5", "--out", str(out), "--",
                 sys.executable, "-c", "import sys;print('GREETING');print('in:',sys.stdin.read().strip())"],
                stdin_text="PROMPTDATA")
    assert proc.returncode == 0
    raw = out.read_text()
    assert "GREETING" in raw and "in: PROMPTDATA" in raw  # stdin forwarded, stdout captured


def test_deadline_returns_124_and_preserves_partial_output(tmp_path):
    out = tmp_path / "o.txt"
    t0 = time.monotonic()
    proc = _run(["--deadline", "1", "--grace", "1", "--out", str(out), "--",
                 sys.executable, "-c", "import time;print('start',flush=True);time.sleep(30)"])
    assert proc.returncode == 124                       # GNU timeout convention
    assert time.monotonic() - t0 < 15                   # actually terminated, did not run 30s
    raw = out.read_text()
    assert "start" in raw                                # partial output preserved
    assert "deadline of" in raw                          # explicit deadline marker
    assert "NOT retried automatically" in raw            # truthful non-relaunch note


def test_deadline_kills_whole_owned_process_group(tmp_path):
    """SIGTERM/SIGKILL target the owned group, so a grandchild the reviewer spawned dies too."""
    out = tmp_path / "o.txt"
    marker = tmp_path / "grandchild_alive"
    # child spawns a grandchild that would create `marker` after 4s if it survived, then the child hangs.
    grandchild = (f"import os,subprocess,sys,time;"
                  f"subprocess.Popen([sys.executable,'-c',\"import time;time.sleep(4);open(r'{marker}','w').close()\"]);"
                  f"print('spawned',flush=True);time.sleep(30)")
    proc = _run(["--deadline", "1", "--grace", "1", "--out", str(out), "--",
                 sys.executable, "-c", grandchild])
    assert proc.returncode == 124
    time.sleep(5)  # wait past when the grandchild WOULD have created the marker
    assert not marker.exists(), "grandchild survived group termination"


def test_missing_command_reports_noexec(tmp_path):
    out = tmp_path / "o.txt"
    proc = _run(["--deadline", "5", "--out", str(out), "--", "definitely-not-a-real-binary-xyz"])
    assert proc.returncode == 126
    assert "not found" in out.read_text()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
