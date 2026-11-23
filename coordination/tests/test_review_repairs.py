"""Review repairs 2026-11-23 (docs/triangle/evidence/review-2026-11-23): F1-F14 + reply_expected.

Ports every adversarial-verifier reproducer (verify-SEC, verify-SP, verify-LLM). Each scenario is kept;
only the expected refusal code/shape follows the repair (noted per test). Run on Betty only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
ROOT = PKG.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(HERE))
from mycelium_coord import Coordinator, ProtocolError, model, wake  # noqa: E402
from mycelium_coord.store import CoordStore  # noqa: E402
import test_writeguard as T  # noqa: E402

env = T.env  # fixture re-export for the SP ports
run_hook, is_deny, agy, codex, codex_bash = T.run_hook, T.is_deny, T.agy, T.codex, T.codex_bash

_spec = importlib.util.spec_from_file_location("wg_repairs", PKG / "mycelium_coord" / "writeguard.py")
wg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wg)

ADV = "MYCELIUM_COORD_ADVISORY"
THREAD = "01a2090c-f749-72e0-a856-3dcad3d24574"
EXTRA = ["--ignore-user-config", "-c", 'mcp_servers.x.command="y"']
CONSULT = ROOT / "src" / "consult"
FIXC = HERE / "fixtures" / "consult"
ADD = "*** Begin Patch\n*** Add File: plan.md\n+REWRITE\n*** End Patch"


# ============================================================ verify-SEC: SEC-1 / SEC-5 (write guard)
@pytest.fixture
def wgenv(tmp_path, monkeypatch):
    state = tmp_path / "state"
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("MYCELIUM_WRITE_GUARD_DIR", str(state))
    monkeypatch.delenv("MYCELIUM_WRITE_GUARD", raising=False)
    return proj


def _cx(ev, tool, cmd, cwd, sid, resp=None):
    p = {
        "session_id": sid,
        "cwd": str(cwd),
        "hook_event_name": ev,
        "tool_name": tool,
        "tool_input": {"command": cmd},
    }
    if resp is not None:
        p["tool_response"] = resp
    return p


def _agy_write_post(path, cwd, cid):
    return {
        "conversationId": cid,
        "cwd": str(cwd),
        "hookEventName": "PostToolUse",
        "error": "",
        "toolCall": {"name": "write_file", "args": {"Path": str(path)}, "id": "x"},
    }


def _deny(out):
    return bool(out) and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_info_sec1_grammar_accepts_long_running_tails(wgenv):
    proj = wgenv
    (proj / "notes.md").write_text("v_A\n")
    for cmd in (
        "cat notes.md && pytest -q",
        "sed -n 1,400p notes.md && make test",
        "cat notes.md; make test",
        "cat notes.md && sleep 120",
    ):
        eff = wg.shell_effects(cmd, str(proj))
        assert eff is not None and str((proj / "notes.md").resolve()) in eff[0], cmd


def test_control_sec1_add_file_without_record_is_denied(wgenv):
    proj = wgenv
    (proj / "notes.md").write_text("v_A\n")
    patch = "*** Begin Patch\n*** Add File: notes.md\n+A full rewrite\n*** End Patch"
    out, _ = wg.handle("codex", "pre", _cx("PreToolUse", "apply_patch", patch, proj, "A"), "enforce")
    assert _deny(out)


def test_sec1_edit_during_long_shell_read_must_not_unlock_stale_overwrite(wgenv):
    """Original scenario, with the PRE hook the host really fires before the Bash call."""
    proj = wgenv
    f = proj / "notes.md"
    f.write_text("v_A\n")
    cmd = "cat notes.md && pytest -q"
    wg.handle("codex", "pre", _cx("PreToolUse", "Bash", cmd, proj, "A"), "enforce")  # A's cat prints v_A at t0
    f.write_text("v_B written by another session\n")  # ... while pytest runs
    wg.handle("antigravity", "post", _agy_write_post(f, proj, "B"), "enforce")
    wg.handle("codex", "post", _cx("PostToolUse", "Bash", cmd, proj, "A", resp="v_A\n...passed\n"), "enforce")
    patch = "*** Begin Patch\n*** Add File: notes.md\n+rewrite built from v_A\n*** End Patch"
    out, _ = wg.handle("codex", "pre", _cx("PreToolUse", "apply_patch", patch, proj, "A"), "enforce")
    assert _deny(out), "stale whole-file overwrite allowed: A never saw v_B but its record says it did"


def test_sec1_without_a_pre_snapshot_the_read_is_not_recorded(wgenv):
    """The same POST with no PRE (the original reproducer's exact shape) now records nothing."""
    proj = wgenv
    f = proj / "notes.md"
    f.write_text("v_A\n")
    f.write_text("v_B written by another session\n")
    wg.handle("codex", "post", _cx("PostToolUse", "Bash", "cat notes.md && pytest -q", proj, "A", resp="x"), "enforce")
    patch = "*** Begin Patch\n*** Add File: notes.md\n+rewrite\n*** End Patch"
    out, _ = wg.handle("codex", "pre", _cx("PreToolUse", "apply_patch", patch, proj, "A"), "enforce")
    assert _deny(out)


def test_sec5_failed_apply_patch_post_must_not_record_freshness(wgenv):
    proj = wgenv
    f = proj / "f.txt"
    f.write_text("v_B written by another session\n")
    upd = "*** Begin Patch\n*** Update File: f.txt\n@@\n-v_A\n+v_A2\n*** End Patch"
    wg.handle(
        "codex",
        "post",
        _cx("PostToolUse", "apply_patch", upd, proj, "A",
            resp="Exit code: 1\nWall time: 0.1 seconds\nOutput:\nFailed to find expected lines in f.txt\n"),
        "enforce",
    )
    add = "*** Begin Patch\n*** Add File: f.txt\n+rewrite built from v_A\n*** End Patch"
    out, _ = wg.handle("codex", "pre", _cx("PreToolUse", "apply_patch", add, proj, "A"), "enforce")
    assert _deny(out)


# ============================================================ verify-SEC: SEC-2 overlapping codex wakes (F2)
FAKE = """#!/bin/sh
d="{rec}"
n=$(cat "$d/count" 2>/dev/null || echo 0); echo $((n+1)) > "$d/count"
cat > /dev/null
if mkdir "$d/running" 2>/dev/null; then :; else echo overlap >> "$d/overlap"; fi
sleep 3
rmdir "$d/running" 2>/dev/null
exit 0
"""


@pytest.fixture
def wk(tmp_path, monkeypatch):
    monkeypatch.delenv(ADV, raising=False)
    monkeypatch.setenv(wake.ENV_QUEUE_DB, str(tmp_path / "no-queue.sqlite"))
    rec = tmp_path / "rec"
    rec.mkdir()
    exe = tmp_path / "fake-codex"
    exe.write_text(FAKE.format(rec=rec))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv(wake.ENV_CODEX_BIN, str(exe))
    monkeypatch.setenv(wake.ENV_CODEX_ARGS, json.dumps(EXTRA))
    co = Coordinator(CoordStore(str(tmp_path / "store")))
    co.create_task("t", project="p", worktree_realpath="/wt", revision=1,
                   protocol_features=["triangle-kinds", "codex-exec-wake"])
    co.attach("t", "sup", role="supervisor", worktree_realpath="/out", host={"host": "claude", "session": "supsess"})
    co.attach("t", "rv", role="observer", worktree_realpath="/out", host={"host": "claude", "session": "rvsess"})
    co.attach("t", "cx", role="executor", worktree_realpath="/wt",
              host={"host": "codex", "session": THREAD, "wake": "headless"})
    return co, rec


def _count(rec):
    f = rec / "count"
    return int(f.read_text()) if f.exists() else 0


def _send(co, mid, kind="task", text="do it"):
    return co.send(task_id="t", message_id=mid, sender="sup", recipient="cx", kind=kind,
                   task_revision=1, text=text)


def test_control_sec2_single_wake_reaches_launch(wk):
    co, rec = wk
    _send(co, "m1")
    r = wake.wake_recipient(co, "t", "m1", sender="sup", timeout_s=30)
    assert _count(rec) == 1 and r["route"] == "codex"
    assert r["queue"]["state"] == "clear"  # F4: the launched receipt shows what the guard saw


def test_sec2_two_wakes_to_one_thread_must_not_overlap(wk):
    """Original scenario. Repair: the second wake is refused (retryable, unbilled), then succeeds later."""
    co, rec = wk
    _send(co, "m1")
    _send(co, "m2", kind="review_request", text="also review this")
    res = {}

    def go(mid):
        res[mid] = wake.wake_recipient(co, "t", mid, sender="sup", timeout_s=30)

    t1 = threading.Thread(target=go, args=("m1",))
    t1.start()
    time.sleep(1.0)
    t2 = threading.Thread(target=go, args=("m2",))
    t2.start()
    t1.join(60)
    t2.join(60)
    assert _count(rec) == 1
    assert not (rec / "overlap").exists()
    assert res["m2"]["outcome"] == "refused:thread_wake_in_progress", res["m2"]
    # retryable and unbilled: once the thread is idle the same message can be woken
    r = wake.wake_recipient(co, "t", "m2", sender="sup", timeout_s=30)
    assert r["outcome"] in ("accepted", "uncertain") and _count(rec) == 2
    assert not (rec / "overlap").exists()


def test_f2_busy_thread_is_refused_from_a_second_thread_and_never_raises(wk):
    co, rec = wk
    _send(co, "m1")
    out = {}

    def second():
        try:
            out["r"] = wake.wake_recipient(co, "t", "m1", sender="sup", timeout_s=30)
        except BaseException as e:  # noqa: BLE001
            out["exc"] = repr(e)

    with co.store.lock("codex-thread-" + THREAD, timeout_s=0):  # a run holds the thread
        t = threading.Thread(target=second)
        t.start()
        t.join(30)
    assert "exc" not in out, out
    assert out["r"]["outcome"] == "refused:thread_wake_in_progress"
    assert _count(rec) == 0
    assert wake._count_billed(co, "t") == 0
    # retryable: the refusal is stored, not a permanent uncertain
    r = wake.wake_recipient(co, "t", "m1", sender="sup", timeout_s=30)
    assert _count(rec) == 1 and not r.get("idempotent_replay")


# ============================================================ F4 queue guard
def test_f4_absent_default_queue_db_is_unknown_when_another_queue_db_exists(tmp_path, monkeypatch):
    monkeypatch.delenv(wake.ENV_QUEUE_DB, raising=False)
    d = tmp_path / "codexhome"
    d.mkdir()
    monkeypatch.setattr(wake, "DEFAULT_QUEUE_DB", str(d / "queue_1.sqlite"))
    assert wake._queue_state(THREAD)[0] == "clear"  # nothing there at all
    (d / "queue_2.sqlite-shm").write_text("x")
    (d / "queue_2.sqlite-wal").write_text("x")
    assert wake._queue_state(THREAD)[0] == "clear"  # sidecars are not queue databases
    (d / "queue_2.sqlite").write_text("x")
    st, detail = wake._queue_state(THREAD)
    assert st == "unknown" and detail["other_queue_dbs"] == 1


def test_f4_absent_env_override_stays_clear(tmp_path, monkeypatch):
    d = tmp_path / "codexhome"
    d.mkdir()
    (d / "queue_2.sqlite").write_text("x")
    monkeypatch.setattr(wake, "DEFAULT_QUEUE_DB", str(d / "queue_1.sqlite"))
    monkeypatch.setenv(wake.ENV_QUEUE_DB, str(tmp_path / "renamed" / "queue_1.sqlite"))
    st, detail = wake._queue_state(THREAD)
    assert st == "clear" and detail == {"db": "absent"}


def test_sl2_missing_override_queue_db_shape(tmp_path, monkeypatch):
    """verify-SP SL-2 reproducer. The repair keeps an absent env-override as 'clear' (owner-fixed rule);
    what changed is that every launched receipt now records the state (test_control_sec2 above)."""
    monkeypatch.setenv(wake.ENV_QUEUE_DB, str(tmp_path / "renamed" / "queue_1.sqlite"))
    st, detail = wake._queue_state("01a20629-f2f4-7c41-9239-7ec8ba859098")
    assert (st, detail) == ("clear", {"db": "absent"})


def test_f4_launched_receipt_records_unknown_queue_state_is_refused_not_launched(wk, tmp_path, monkeypatch):
    co, rec = wk
    d = tmp_path / "codexhome"
    d.mkdir()
    (d / "queue_2.sqlite").write_text("x")
    monkeypatch.delenv(wake.ENV_QUEUE_DB, raising=False)
    monkeypatch.setattr(wake, "DEFAULT_QUEUE_DB", str(d / "queue_1.sqlite"))
    _send(co, "m1")
    r = wake.wake_recipient(co, "t", "m1", sender="sup", timeout_s=30)
    assert r["outcome"] == "refused:queue_unknown" and _count(rec) == 0
    assert r["queue"] == {"state": "unknown", "detail": {"db": "absent", "other_queue_dbs": 1}}


# ============================================================ verify-SEC: SEC-3 advisory identity (F12)
@pytest.fixture
def adv(tmp_path, monkeypatch):
    monkeypatch.delenv(ADV, raising=False)
    co = Coordinator(CoordStore(str(tmp_path / "store")))
    co.create_task("t", project="p", worktree_realpath="/wt", revision=1, protocol_features=["triangle-kinds"])
    co.attach("t", "sup", role="supervisor", worktree_realpath="/out", host={"host": "claude", "session": "supsess"})
    co.attach("t", "cx", role="executor", worktree_realpath="/wt", host={"host": "codex", "session": THREAD})
    co.send(task_id="t", message_id="m1", sender="sup", recipient="cx", kind="task", task_revision=1, text="do it")
    monkeypatch.setenv(ADV, "1")
    co.attach("t", "agy", role="observer", worktree_realpath="/wt", host={"host": "antigravity", "session": "agy-sess-1"})
    return co


def test_control_sec3_advisory_env_is_active(adv):
    with pytest.raises(ProtocolError) as e:
        adv.attach("t", "agy2", role="executor", worktree_realpath="/wt",
                   host={"host": "antigravity", "session": "agy-sess-2"})
    assert e.value.code == "advisory_process_refused"


def test_sec3_advisory_process_must_not_ack_for_executor(adv):
    with pytest.raises(ProtocolError) as e:
        adv.ack("t", "cx", "m1")
    assert e.value.code == "advisory_process_refused"
    assert adv.message_state("t", "cx", "m1") != "acknowledged"


def test_sec3_advisory_process_must_not_move_executor_cursor(adv):
    with pytest.raises(ProtocolError) as e:
        adv.set_cursor("t", "cx", 999)
    assert e.value.code == "advisory_process_refused"
    assert adv.get_cursor("t", "cx") == 0


def test_sec3_advisory_process_must_not_send_as_supervisor(adv):
    with pytest.raises(ProtocolError) as e:
        adv.send(task_id="t", message_id="q1", sender="sup", recipient="cx", kind="question",
                 task_revision=1, text="forged question")
    assert e.value.code == "advisory_process_refused"
    assert adv.get_message("t", "q1") is None


def test_f12_advisory_observer_may_ack_move_cursor_and_send_as_itself(adv):
    adv.send(task_id="t", message_id="q3", sender="agy", recipient="sup", kind="question", task_revision=1, text="ok")
    assert adv.set_cursor("t", "agy", 5)["after_seq"] == 5


def test_f12_detached_observer_is_refused(adv):
    adv.detach("t", "agy")
    with pytest.raises(ProtocolError) as e:
        adv.set_cursor("t", "agy", 1)
    assert e.value.code == "advisory_process_refused"


def test_f12_without_the_advisory_env_nothing_changes(adv, monkeypatch):
    monkeypatch.delenv(ADV, raising=False)
    assert adv.ack("t", "cx", "m1")["participant_id"] == "cx"
    assert adv.set_cursor("t", "cx", 3)["after_seq"] == 3


# ============================================================ verify-SP
def test_sl1c_edit_during_long_shell_command_is_not_credited(env):
    proj, state = env
    f = proj / "plan.md"
    f.write_text("v1\n")
    cmd = "cat plan.md; make build"
    run_hook("codex", "pre", codex_bash("PreToolUse", cmd, proj, sid="A"), state)
    f.write_text("v1\nB-LINES\n")
    run_hook("codex", "post", codex_bash("PostToolUse", cmd, proj, sid="A"), state, pre=False)
    out = run_hook("codex", "pre", codex("PreToolUse", ADD, proj, sid="A"), state)
    assert is_deny("codex", out), "A never saw B-LINES, yet its whole-file Add File is allowed"


def test_sl1c_control_agy_read_file_post_after_change(env):
    """Native read tool: the change lands between the tool's read and its PostToolUse hook. With the PRE
    hook a host fires, the POST no longer credits it."""
    proj, state = env
    f = proj / "plan.md"
    f.write_text("v1\n")
    run_hook("antigravity", "pre", agy("PreToolUse", "read_file", f, proj), state)
    f.write_text("v1\nB-LINES\n")
    run_hook("antigravity", "post", agy("PostToolUse", "read_file", f, proj), state)
    out = run_hook("antigravity", "pre", agy("PreToolUse", "write_file", f, proj), state)
    assert is_deny("antigravity", out)


def test_sl1a_hunk_edit_records_whole_file_freshness_by_design(env):
    """SL-1(a) is specified behaviour (a partial edit relies on the host's own context check and records
    freshness afterwards; WRITE_GUARD_SPEC Key decisions). Pinned, not changed: the scenario is kept and
    the expectation states the documented rule."""
    proj, state = env
    f = proj / "plan.md"
    f.write_text("top\nmid\n")
    run_hook("antigravity", "post", agy("PostToolUse", "read_file", f, proj), state)
    f.write_text("top\nmid\nB-LINES\n")
    f.write_text("TOP\nmid\nB-LINES\n")
    run_hook("antigravity", "post", agy("PostToolUse", "edit_file", f, proj), state)
    out = run_hook("antigravity", "pre", agy("PreToolUse", "write_file", f, proj), state)
    assert not is_deny("antigravity", out)


def test_sl1b_wc_counts_as_a_read_by_design(env):
    """SL-1(b) is specified behaviour (partial and metadata reads count, as in Claude's native rule).
    Pinned with the PRE hook a host fires: the unchanged file is credited."""
    proj, state = env
    f = proj / "plan.md"
    f.write_text("v1\nB-LINES\n")
    run_hook("antigravity", "post", agy("PostToolUse", "run_command", f, proj, CommandLine="wc -l plan.md"), state)
    out = run_hook("antigravity", "pre", agy("PreToolUse", "write_file", f, proj), state)
    assert not is_deny("antigravity", out)


# ============================================================ verify-LLM
def home_with_gemini():
    h = tempfile.mkdtemp()
    os.makedirs(os.path.join(h, ".gemini", "config"))
    open(os.path.join(h, ".gemini", "config", "c.json"), "w").write("{}")
    return h


def test_agy_answer_write_failure_is_not_answered_exit0():
    d = tempfile.mkdtemp()
    out = os.path.join(d, "ans.md")
    os.mkdir(out)  # <out> cannot be written as a file
    e = dict(os.environ, AGY_BIN=str(FIXC / "fake_agy"), FAKE_AGY_MODE="normal", HOME=home_with_gemini(),
             AGY_ASK_BASE=os.path.join(tempfile.mkdtemp(), "base"))
    p = subprocess.run([sys.executable, str(CONSULT / "agy_ask.py"), "--out", out, "--cwd", d, "--timeout", "20"],
                       input="review", text=True, capture_output=True, env=e, timeout=60)
    rc = json.load(open(out + ".receipt.json"))
    assert not os.path.isfile(out)
    assert rc["outcome"] == "ANSWER-WRITE-FAILED" and p.returncode == 7
    assert "write failed" in rc["reason"]
    assert json.loads(p.stdout)["outcome"] == "ANSWER-WRITE-FAILED"


def test_claude_answer_write_failure_still_writes_receipt():
    d = tempfile.mkdtemp()
    out = os.path.join(d, "ans.md")
    os.mkdir(out)
    e = dict(os.environ, CLAUDE_BIN=str(FIXC / "fake_claude"), HOME=tempfile.mkdtemp())
    p = subprocess.run(["bash", str(CONSULT / "claude_ask.sh"), "--out", out, "--cwd", d, "--timeout", "20"],
                       input="review", text=True, capture_output=True, env=e, timeout=60)
    assert p.returncode != 0
    assert os.path.exists(out + ".receipt.json"), "no receipt on the answer-write failure path"


def _claude_timeout(t):
    d = tempfile.mkdtemp()
    out = os.path.join(d, "a.md")
    e = dict(os.environ, CLAUDE_BIN=str(FIXC / "fake_claude"), HOME=tempfile.mkdtemp(), FAKE_CLAUDE_MODE="timeout")
    p = subprocess.Popen(["bash", str(CONSULT / "claude_ask.sh"), "--out", out, "--cwd", d, "--timeout", t],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         env=e, start_new_session=True)
    try:
        so, se = p.communicate("x", timeout=12)
        return p.returncode, se, out
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        return None, "still running after 12s", out


@pytest.mark.parametrize("t", ["0", "abc", "-3", "", "1.5", "007x", "+4"])
def test_timeout_must_be_a_positive_integer_refused_before_launch(t):
    code, se, out = _claude_timeout(t)
    assert code == 5, (t, code, se[-200:])
    assert "positive integer" in se
    rcpt = json.load(open(out + ".receipt.json"))
    assert rcpt["outcome"] == "CANNOT-EVALUATE" and rcpt["exit"] == 5


def test_a_positive_integer_timeout_still_bounds_the_run():
    code, se, out = _claude_timeout("2")
    assert code == 2, (code, se[-200:])  # SIGALRM path: timeout, not refused


# ============================================================ F13 / F14 / F11 / reply_expected / docs
def test_f13_dead_denylist_is_gone():
    assert not hasattr(model, "ADVISORY_DENIED_SEND_KINDS")


def test_f11_stray_empty_module_is_gone():
    assert not (PKG / "mycelium_coord" / "riteguard.py").exists()


def test_f14_host_kinds_have_one_definition():
    from mycelium_coord import waitpath

    assert model.HOST_KINDS == ("claude", "codex", "antigravity")
    assert waitpath.HOSTS is model.HOST_KINDS or tuple(waitpath.HOSTS) == model.HOST_KINDS
    assert tuple(wake.ROUTES) == model.HOST_KINDS
    assert not hasattr(Coordinator, "_HOST_KINDS")
    assert tuple(wg.HOSTS) == model.HOST_KINDS
    src = (PKG / "mycelium_coord" / "cli.py").read_text()
    assert "choices=list(waitpath.HOSTS)" in src


def test_f14_wake_route_for_a_future_host_defaults_to_not_supported(monkeypatch):
    monkeypatch.setattr(wake, "HOST_KINDS", model.HOST_KINDS)
    assert wake.ROUTES["antigravity"] is wake._route_unsupported
    assert wake.ROUTES["codex"] is wake._route_codex and wake.ROUTES["claude"] is wake._route_claude


def test_reply_expected_is_derived_in_every_rendering_and_never_stored(tmp_path):
    co = Coordinator(CoordStore(str(tmp_path / "s")))
    co.create_task("t", project="p", worktree_realpath="/wt", revision=1, protocol_features=["triangle-kinds"])
    co.attach("t", "sup", role="supervisor", worktree_realpath="/o", host={"host": "claude", "session": "s1"})
    co.attach("t", "ex", role="executor", worktree_realpath="/wt", host={"host": "codex", "session": THREAD})
    for mid, kind in (("a", "review_request"), ("b", "consult_request"), ("c", "task"), ("d", "report"),
                      ("e", "progress")):
        co.send(task_id="t", message_id=mid, sender="sup", recipient="ex", kind=kind, task_revision=1, text="x")
    want = {"a": True, "b": True}
    for compact in (True, False):
        for m in co.inbox("t", "ex", compact=compact)["messages"]:
            assert m.get("reply_expected", False) is want.get(m["message_id"], False), (compact, m["message_id"])
            if m["message_id"] not in want:
                assert "reply_expected" not in m
    assert co.read_message("t", "ex", "a")["reply_expected"] is True
    assert co.read_message("t", "ex", "b")["reply_expected"] is True
    assert "reply_expected" not in co.read_message("t", "ex", "c")
    assert "reply_expected" not in co.read_message("t", "ex", "d")
    assert "reply_expected" not in co.get_message("t", "a")  # the stored record is untouched
    # the reply it asks for is a report replying to the request
    r = co.send(task_id="t", message_id="r1", sender="ex", recipient="sup", kind="report", task_revision=1,
                text="done", reply_to="a")
    stored = co.get_message("t", "r1")
    assert stored["reply_to"] == "a" and stored["kind"] == "report"


def test_f7_model_facing_docstrings_name_the_triangle_kinds_and_wake_marker():
    src = (PKG / "mycelium_coord" / "mcp_server.py").read_text()
    assert "review_request|consult_request|report" in src
    assert "kind_requires_feature" in src and "codex-exec-wake" in src
    assert '"wake": "headless"' in src
    assert "reply_expected" in src
    cli = (PKG / "mycelium_coord" / "cli.py").read_text()
    assert "wake=headless" in cli and "codex-exec-wake" in cli
