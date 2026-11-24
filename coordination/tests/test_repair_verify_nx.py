"""Repair-verification probes (2026-11-23). Each test states the DESIRED behaviour; a failure is a reproducer.
Positive controls (unlock on real fixture pairs) are expected to pass."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(HERE))
import test_writeguard as T  # noqa: E402
from test_writeguard import wg, run_hook, is_deny, fixture, codex, agy, env  # noqa: E402,F401
from mycelium_coord import Coordinator, wake  # noqa: E402
from mycelium_coord.store import CoordStore  # noqa: E402

ADDF = "*** Begin Patch\n*** Add File: plan.md\n+REWRITE\n*** End Patch"
THREAD = "01a2090c-f749-72e0-a856-3dcad3d24574"


# ---------------------------------------------------------------- positive controls: deny -> read -> retry unlock
def test_ctl_codex_deny_cat_retry_unlocks_with_call_ids(env):
    proj, state = env
    (proj / "plan.md").write_text("v1\n")
    assert is_deny(
        "codex",
        run_hook("codex", "pre", codex("PreToolUse", ADDF, proj, sid="A"), state),
    )
    pre = T._codex_cmd(
        "PreToolUse", "cat plan.md", proj, sid="A", call_id="exec-unlock-1"
    )
    post = T._codex_cmd(
        "PostToolUse", "cat plan.md", proj, sid="A", call_id="exec-unlock-1"
    )
    post["tool_response"] = "Exit code: 0\nOutput:\nv1\n"
    run_hook("codex", "pre", pre, state)
    run_hook("codex", "post", post, state, pre=False)
    assert not is_deny(
        "codex",
        run_hook("codex", "pre", codex("PreToolUse", ADDF, proj, sid="A"), state),
    )


def test_ctl_agy_deny_read_file_retry_unlocks_on_real_fixture_pair(env):
    proj, state = env
    pre = fixture("agy_pre_read_file", proj)
    post = fixture("agy_post_read_file", proj)
    target = Path(pre["toolCall"]["args"]["Path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("v1\n")
    cid = pre["conversationId"]
    wpre = agy("PreToolUse", "write_file", target, proj, cid=cid)
    assert is_deny("antigravity", run_hook("antigravity", "pre", wpre, state))
    run_hook("antigravity", "pre", pre, state)
    run_hook("antigravity", "post", post, state, pre=False)
    assert not is_deny("antigravity", run_hook("antigravity", "pre", wpre, state))


def test_ctl_agy_run_command_cat_unlocks_on_real_ids(env):
    proj, state = env
    (proj / "plan.md").write_text("v1\n")
    pre = T._agy_cmd(
        "PreToolUse",
        "cat plan.md",
        proj,
        cid="conv-A",
        call_id="call_1978012942867814271",
    )
    post = T._agy_cmd(
        "PostToolUse",
        "cat plan.md",
        proj,
        cid="conv-A",
        call_id="call_1978012942867814271",
    )
    run_hook("antigravity", "pre", pre, state)
    run_hook("antigravity", "post", post, state, pre=False)
    w = agy("PreToolUse", "write_file", proj / "plan.md", proj, cid="conv-A")
    assert not is_deny("antigravity", run_hook("antigravity", "pre", w, state))


# ---------------------------------------------------------------- NX-1: pending snapshot dirs are never pruned
def test_nx1_abandoned_pending_snapshot_of_a_dead_session_is_pruned(env):
    proj, state = env
    (proj / "plan.md").write_text("v1\n")
    # session X: PRE with no POST (command denied by the host / interrupted / agy POST with error)
    run_hook(
        "codex",
        "pre",
        T._codex_cmd("PreToolUse", "cat plan.md", proj, sid="X", call_id="x1"),
        state,
    )
    # session X also has an ordinary record (so prune() has a sessions/ dir to compare with)
    run_hook(
        "codex",
        "pre",
        T._codex_cmd("PreToolUse", "cat plan.md", proj, sid="X", call_id="x2"),
        state,
    )
    run_hook(
        "codex",
        "post",
        T._codex_cmd("PostToolUse", "cat plan.md", proj, sid="X", call_id="x2"),
        state,
        pre=False,
    )
    old = time.time() - 30 * 86400
    for p in list(state.rglob("*")):
        os.utime(p, (old, old))
    # other sessions keep working; prune() runs (it is 1-in-200 per hook call in production)
    for i in range(3):
        run_hook(
            "codex",
            "pre",
            T._codex_cmd("PreToolUse", "cat plan.md", proj, sid="Y", call_id="y%d" % i),
            state,
        )
    os.environ["MYCELIUM_WRITE_GUARD_DIR"] = str(state)
    wg.prune()
    sess_x = state / "sessions" / "codex" / "X"
    pend_x = state / "pending" / "codex" / "X"
    left = (
        sorted(str(p.relative_to(state)) for p in pend_x.rglob("*"))
        if pend_x.exists()
        else []
    )
    print("NX1 sessions/codex/X exists after prune:", sess_x.exists())
    print("NX1 pending/codex/X exists after prune:", pend_x.exists(), "files:", left)
    assert not pend_x.exists(), (
        "30-day-old pending dir of a dead session survives prune(): %s" % left
    )


# ---------------------------------------------------------------- NX-2: fast set is checked by program name only
def test_nx2_tail_follow_is_not_a_fast_program(tmp_path):
    base = str(tmp_path.resolve())
    eff = wg.shell_effects("echo x > plan.md; tail -f build.log", base)
    print("NX2 shell_effects:", eff)
    assert eff is None or eff[1] == [], (
        "overwrite credited although `tail -f` never finishes on its own"
    )


def test_nx2_overwrite_then_tail_follow_does_not_credit_a_concurrent_edit(env):
    """Same sequence as test_f1_overwrite_then_unknown_program_does_not_credit_a_concurrent_edit, with
    `tail -f` (name in FAST_PROGS) instead of `make`."""
    proj, state = env
    f = proj / "plan.md"
    f.write_text("v1\n")
    (proj / "build.log").write_text("log\n")
    cmd = "echo x > plan.md; tail -f build.log"
    run_hook("codex", "pre", T.codex_bash("PreToolUse", cmd, proj, sid="A"), state)
    f.write_text("x\nB-LINES edited while tail -f was following the log\n")
    run_hook(
        "codex",
        "post",
        T.codex_bash("PostToolUse", cmd, proj, sid="A"),
        state,
        pre=False,
    )
    out = run_hook("codex", "pre", codex("PreToolUse", ADDF, proj, sid="A"), state)
    assert is_deny("codex", out), "A's Add File is allowed although A never saw B-LINES"


# ---------------------------------------------------------------- NX-3: reply_expected missing from full resume
def test_nx3_full_resume_renders_reply_expected(tmp_path):
    co = Coordinator(CoordStore(str(tmp_path / "s")))
    co.create_task(
        "t",
        project="p",
        worktree_realpath="/wt",
        revision=1,
        protocol_features=["triangle-kinds"],
    )
    co.attach(
        "t",
        "sup",
        role="supervisor",
        worktree_realpath="/o",
        host={"host": "claude", "session": "s1"},
    )
    co.attach(
        "t",
        "ex",
        role="executor",
        worktree_realpath="/wt",
        host={"host": "codex", "session": THREAD},
    )
    co.send(
        task_id="t",
        message_id="a",
        sender="sup",
        recipient="ex",
        kind="review_request",
        task_revision=1,
        text="x",
    )
    compact = {
        m["message_id"]: m
        for m in co.resume("t", "ex", compact=True)["pending_unacked"]
    }
    full = {
        m["message_id"]: m
        for m in co.resume("t", "ex", compact=False)["pending_unacked"]
    }
    print("NX3 compact resume a.reply_expected =", compact["a"].get("reply_expected"))
    print("NX3 full resume a.reply_expected =", full["a"].get("reply_expected"))
    assert compact["a"].get("reply_expected") is True
    assert full["a"].get("reply_expected") is True, (
        "full (non-compact) resume drops reply_expected"
    )


# ---------------------------------------------------------------- NX-4: launched receipt after a post-launch error
FAKE = """#!/bin/sh
cat > /dev/null
echo launched >> "{rec}/launched"
exit 0
"""


def test_nx4_launched_then_error_receipt_keeps_queue_state(tmp_path, monkeypatch):
    monkeypatch.delenv("MYCELIUM_COORD_ADVISORY", raising=False)
    monkeypatch.setenv(wake.ENV_QUEUE_DB, str(tmp_path / "no-queue.sqlite"))
    rec = tmp_path / "rec"
    rec.mkdir()
    exe = tmp_path / "fake-codex"
    exe.write_text(FAKE.format(rec=rec))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv(wake.ENV_CODEX_BIN, str(exe))
    monkeypatch.setenv(wake.ENV_CODEX_ARGS, json.dumps(["--ignore-user-config"]))
    co = Coordinator(CoordStore(str(tmp_path / "store")))
    co.create_task(
        "t",
        project="p",
        worktree_realpath="/wt",
        revision=1,
        protocol_features=["triangle-kinds", "codex-exec-wake"],
    )
    co.attach(
        "t",
        "sup",
        role="supervisor",
        worktree_realpath="/out",
        host={"host": "claude", "session": "s"},
    )
    co.attach(
        "t",
        "cx",
        role="executor",
        worktree_realpath="/wt",
        host={"host": "codex", "session": THREAD, "wake": "headless"},
    )
    co.send(
        task_id="t",
        message_id="m1",
        sender="sup",
        recipient="cx",
        kind="task",
        task_revision=1,
        text="x",
    )
    real = co.message_state
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if (
            calls["n"] >= 2
        ):  # the first call is the pre-route state_before; the next one is after launch
            raise RuntimeError("store read failed after launch")
        return real(*a, **k)

    monkeypatch.setattr(co, "message_state", flaky)
    r = wake.wake_recipient(co, "t", "m1", sender="sup", timeout_s=30)
    print(
        "NX4 launched:",
        (rec / "launched").exists(),
        "receipt:",
        json.dumps(r, default=str)[:600],
    )
    assert (rec / "launched").exists()
    assert "queue" in r, "a launched run's receipt lacks the queue state the guard saw"
