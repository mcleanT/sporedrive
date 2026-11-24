#!/usr/bin/env python3
"""Send capability (triangle proposal step 3, "send binding"): an identity-bearing write on a
send-capability task, or from any advisory process, must come from the process that attached that
participant. ``sendcap.forget()`` makes this test process look like a second process that never
attached; real second processes are CLI subprocesses.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import Coordinator, ProtocolError, sendcap, wake  # noqa: E402
from mycelium_coord.model import PROTOCOL_FEATURES  # noqa: E402
from mycelium_coord.store import CoordStore  # noqa: E402

ADV = "MYCELIUM_COORD_ADVISORY"
PKG_ROOT = str(Path(__file__).resolve().parents[1])
THREAD = "01a2090c-f749-72e0-a856-3dcad3d24574"
FEATS = ["triangle-kinds", "send-capability"]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv(ADV, raising=False)
    monkeypatch.delenv(sendcap.CAP_FILE_ENV, raising=False)
    sendcap.forget()
    yield
    sendcap.forget()


def _co(tmp_path, features=FEATS):
    co = Coordinator(CoordStore(str(tmp_path / "store")))
    co.create_task(
        "t",
        project="p",
        worktree_realpath="/wt",
        revision=1,
        protocol_features=list(features) if features else None,
    )
    return co


def _attach(co, pid, role="supervisor", host="codex", session=None, wt=None, **kw):
    return co.attach(
        "t",
        pid,
        role=role,
        worktree_realpath=wt or ("/wt" if role == "executor" else "/out"),
        host={"host": host, "session": session or f"{pid}-sess"},
        **kw,
    )


def _send(co, mid, sender, to="all", kind="progress"):
    return co.send(
        task_id="t",
        message_id=mid,
        sender=sender,
        recipient=to,
        kind=kind,
        task_revision=1,
        text="x",
    )


def _cli(store, *args, cap_file=None, env_extra=None):
    env = {**os.environ, "PYTHONPATH": PKG_ROOT}
    env.pop(ADV, None)
    env.pop(sendcap.CAP_FILE_ENV, None)
    env.update(env_extra or {})
    pre = ["--root", str(store)] + (["--cap-file", str(cap_file)] if cap_file else [])
    return subprocess.run(
        [sys.executable, "-m", "mycelium_coord", *pre, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _code(fn):
    with pytest.raises(ProtocolError) as e:
        fn()
    return e.value.code


# ------------------------------------------------------------------ feature + same-process flow
def test_feature_is_a_known_protocol_feature(tmp_path):
    assert "send-capability" in PROTOCOL_FEATURES
    co = _co(tmp_path)
    assert "send-capability" in co.get_task("t")["protocol_features"]


def test_attaching_process_acts_normally(tmp_path):
    co = _co(tmp_path)
    _attach(co, "sup")
    _attach(co, "ex", role="executor", host="claude")
    _send(co, "m1", "sup", to="ex", kind="task")
    co.ack("t", "ex", "m1")
    co.set_cursor("t", "ex", 1)
    co.publish_checkpoint("t", revision=1, participant_id="sup", checkpoint={"k": 1})
    # an idempotent re-attach by the holder keeps its capability
    again = _attach(co, "sup")
    assert again["send_cap"]["holder_pid"] == os.getpid()
    _send(co, "m2", "sup")


# ------------------------------------------------------------------ acceptance: no capability, no write
def test_every_identity_write_without_capability_is_refused(tmp_path):
    co = _co(tmp_path)
    _attach(co, "sup")
    _attach(co, "ex", role="executor", host="claude")
    _send(co, "m1", "sup", to="ex", kind="task")
    seq_before = co._max_seq("t")
    sendcap.forget()  # now a process that attached nobody
    assert (
        _code(lambda: _send(co, "m2", "sup", to="ex", kind="task"))
        == "send_capability_required"
    )
    assert _code(lambda: co.ack("t", "ex", "m1")) == "send_capability_required"
    assert _code(lambda: co.set_cursor("t", "ex", 1)) == "send_capability_required"
    assert (
        _code(
            lambda: co.publish_checkpoint(
                "t", revision=1, participant_id="sup", checkpoint={"k": 1}
            )
        )
        == "send_capability_required"
    )
    assert _code(lambda: co.detach("t", "ex")) == "send_capability_required"
    assert (
        _code(lambda: wake.wake_recipient(co, "t", "m1", sender="sup"))
        == "send_capability_required"
    )
    # nothing was written
    assert co._max_seq("t") == seq_before
    assert co.get_message("t", "m2") is None
    assert not co.is_acked("t", "ex", "m1")
    assert co.get_cursor("t", "ex") == 0
    assert co.read_checkpoint("t") is None
    assert not co.store.read("tasks/t/participants/ex.json").get("detached")


def test_idempotent_replay_from_a_foreign_process_is_refused_too(tmp_path):
    co = _co(tmp_path)
    _attach(co, "sup")
    _send(co, "m1", "sup")
    sendcap.forget()
    assert _code(lambda: _send(co, "m1", "sup")) == "send_capability_required"


def test_reattach_while_the_holder_is_alive_is_refused(tmp_path):
    """Advisor acceptance: another process re-attaches an attached id (same identity), then sends."""
    co = _co(tmp_path)
    _attach(co, "sup")
    sendcap.forget()
    assert _code(lambda: _attach(co, "sup")) == "participant_held"
    assert _code(lambda: _send(co, "m1", "sup")) == "send_capability_required"


def test_explicit_takeover_rotates_and_stales_the_old_holder(tmp_path):
    co = _co(tmp_path)
    _attach(co, "sup")
    old = sendcap.held(co.store, "t", "sup")
    sendcap.forget()
    _attach(co, "sup", allow_transition=True)  # non-advisory, explicit, audited
    _send(co, "m1", "sup")
    audit = [
        json.loads(x)
        for x in (Path(co.store.root) / "tasks/t/audit.jsonl").read_text().splitlines()
    ]
    assert any(
        r.get("kind") == "attach" and r.get("send_cap") == "takeover" for r in audit
    )
    # the original holder's capability no longer matches
    sendcap._CAPS[sendcap._key(co.store, "t", "sup")] = old
    assert _code(lambda: _send(co, "m2", "sup")) == "send_capability_stale"


def test_reattach_after_the_holder_exited_mints_a_new_capability(tmp_path):
    co = _co(tmp_path)
    r = _cli(
        co.store.root,
        "attach",
        "t",
        "cx",
        "--role",
        "executor",
        "--worktree",
        "/wt",
        "--host",
        "host=codex",
        "--host",
        f"session={THREAD}",
    )
    assert r.returncode == 0, r.stderr
    # the CLI process has exited: this process holds nothing yet
    assert _code(lambda: _send(co, "m1", "cx")) == "send_capability_required"
    co.attach(
        "t",
        "cx",
        role="executor",
        worktree_realpath="/wt",
        host={"host": "codex", "session": THREAD},
    )
    _send(co, "m1", "cx")


def test_detach_releases_the_capability(tmp_path):
    co = _co(tmp_path)
    _attach(co, "obs", role="observer")
    co.detach("t", "obs")
    assert sendcap.held(co.store, "t", "obs") is None
    _attach(co, "obs", role="observer")
    _send(co, "m1", "obs")


def test_record_from_older_code_needs_a_fresh_attach(tmp_path):
    co = _co(tmp_path)
    _attach(co, "sup")
    rec = co.store.read("tasks/t/participants/sup.json")
    rec.pop("send_cap")
    co.store.write(
        "tasks/t/participants/sup.json", rec
    )  # as an older server would leave it
    assert _code(lambda: _send(co, "m1", "sup")) == "send_capability_stale"
    _attach(co, "sup")  # no live holder recorded -> allowed, mints
    _send(co, "m1", "sup")


# ------------------------------------------------------------------ scope: legacy tasks, advisory processes
def test_task_without_the_feature_keeps_old_behaviour(tmp_path):
    co = _co(tmp_path, features=["triangle-kinds"])
    _attach(co, "sup")
    sendcap.forget()
    _send(co, "m1", "sup")
    _attach(co, "sup")  # re-attach from "another process": legacy semantics


def test_advisory_process_is_held_to_its_own_attach_on_every_task(
    tmp_path, monkeypatch
):
    co = _co(tmp_path, features=None)  # a legacy task
    _attach(co, "obs-a", role="observer", host="antigravity")
    sendcap.forget()
    monkeypatch.setenv(ADV, "1")
    assert _code(lambda: _send(co, "m1", "obs-a")) == "send_capability_required"
    assert (
        _code(lambda: _attach(co, "obs-a", role="observer", host="antigravity"))
        == "participant_held"
    )
    # its own fresh observer works
    _attach(co, "obs-b", role="observer", host="antigravity")
    _send(co, "m2", "obs-b", kind="progress")


# ------------------------------------------------------------------ outputs never carry the capability
def test_hash_and_capability_never_rendered(tmp_path):
    co = _co(tmp_path)
    out = _attach(co, "sup")
    cap = sendcap.held(co.store, "t", "sup")
    stored = co.store.read("tasks/t/participants/sup.json")["send_cap"]["sha256"]
    blob = json.dumps([out, co.list_participants("t")])
    assert stored not in blob and cap not in blob
    assert out["send_cap"]["holder_pid"] == os.getpid()


def test_cli_cap_file_round_trip(tmp_path):
    co = _co(tmp_path)
    store = co.store.root
    capf = tmp_path / "caps" / "lead.json"
    r = _cli(
        store,
        "attach",
        "t",
        "lead",
        "--role",
        "supervisor",
        "--worktree",
        "/out",
        "--host",
        "host=claude",
        "--host",
        "session=lead-sess",
        cap_file=capf,
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["capability_file"] == str(capf.resolve())
    assert stat.S_IMODE(capf.stat().st_mode) == 0o600
    cap = json.loads(capf.read_text())["caps"][0]["cap"]
    assert cap not in r.stdout
    ok = _cli(
        store,
        "send",
        "t",
        "--id",
        "m1",
        "--from",
        "lead",
        "--to",
        "all",
        "--kind",
        "progress",
        "--revision",
        "1",
        "--text",
        "hi",
        cap_file=capf,
    )
    assert ok.returncode == 0, ok.stderr
    bad = _cli(
        store,
        "send",
        "t",
        "--id",
        "m2",
        "--from",
        "lead",
        "--to",
        "all",
        "--kind",
        "progress",
        "--revision",
        "1",
        "--text",
        "hi",
    )
    assert bad.returncode != 0 and "send_capability_required" in (
        bad.stdout + bad.stderr
    )
    env_ok = _cli(
        store,
        "send",
        "t",
        "--id",
        "m3",
        "--from",
        "lead",
        "--to",
        "all",
        "--kind",
        "progress",
        "--revision",
        "1",
        "--text",
        "hi",
        env_extra={sendcap.CAP_FILE_ENV: str(capf)},
    )
    assert env_ok.returncode == 0, env_ok.stderr


def test_cap_file_from_another_store_is_ignored(tmp_path):
    co = _co(tmp_path)
    capf = tmp_path / "caps.json"
    capf.write_text(json.dumps({"version": 2, "caps": [
        {"store": "/other/root", "task": "t", "participant": "lead", "cap": "x" * 40}]}))
    capf.chmod(0o600)
    assert sendcap.load_file(str(capf), co.store) == 0


# ------------------------------------------------------------------ holder liveness
def test_holder_state():
    me = sendcap.self_holder()
    assert sendcap.holder_state(me) == sendcap.ALIVE
    assert sendcap.holder_state(None) == sendcap.DEAD
    assert (
        sendcap.holder_state({**me, "node": me["node"] + "-elsewhere"})
        == sendcap.UNKNOWN
    )
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert sendcap.holder_state({**me, "pid": p.pid}) == sendcap.DEAD
    q = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        live = {"pid": q.pid, "start": sendcap._start_time(q.pid), "node": me["node"]}
        assert sendcap.holder_state(live) == sendcap.ALIVE
        assert (
            sendcap.holder_state({**live, "start": "Thu Jan  1 00:00:00 1970"})
            == sendcap.DEAD
        )
        assert sendcap.holder_state({**live, "start": None}) == sendcap.UNKNOWN
    finally:
        q.kill()
        q.wait()


# ------------------------------------------------------------------ wake on a send-capability task
FAKE = """#!/bin/sh
d="{rec}"
cat > "$d/stdin.txt"
run() {{ PYTHONPATH="{pkg}" "{py}" -m mycelium_coord --root "{store}" --cap-file "$d/cap.json" "$@"; }}
case "$FAKE_MODE" in
  attach_ack) run attach t cx --role executor --worktree /wt --host host=codex --host session={thread} \
      --host wake=headless >/dev/null && run ack t cx m1 >/dev/null ;;
  ack_only) run ack t cx m1 >"$d/ack.err" 2>&1 ;;
esac
exit 0
"""


def _wake_setup(tmp_path, monkeypatch, mode):
    co = _co(
        tmp_path, features=["triangle-kinds", "codex-exec-wake", "send-capability"]
    )
    store = co.store.root
    _attach(co, "sup", host="claude")
    r = _cli(
        store,
        "attach",
        "t",
        "cx",
        "--role",
        "executor",
        "--worktree",
        "/wt",
        "--host",
        "host=codex",
        "--host",
        f"session={THREAD}",
        "--host",
        "wake=headless",
    )  # holder exits at once
    assert r.returncode == 0, r.stderr
    rec = tmp_path / "rec"
    rec.mkdir()
    exe = tmp_path / "fake-codex"
    exe.write_text(
        FAKE.format(
            rec=rec, pkg=PKG_ROOT, py=sys.executable, store=store, thread=THREAD
        )
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv(wake.ENV_CODEX_BIN, str(exe))
    monkeypatch.setenv(wake.ENV_CODEX_ARGS, json.dumps(["--ignore-user-config"]))
    monkeypatch.setenv(wake.ENV_QUEUE_DB, str(tmp_path / "no-queue.sqlite"))
    monkeypatch.setenv("FAKE_MODE", mode)
    _send(co, "m1", "sup", to="cx", kind="task")
    return co, rec


def test_woken_turn_attaches_then_acks_and_is_accepted(tmp_path, monkeypatch):
    co, rec = _wake_setup(tmp_path, monkeypatch, "attach_ack")
    out = wake.wake_recipient(co, "t", "m1", sender="sup", timeout_s=60)
    assert out["outcome"] == "accepted", out
    prompt = (rec / "stdin.txt").read_text()
    assert (
        "coord_attach" in prompt
        and "participant_id=cx" in prompt
        and '"wake": "headless"' in prompt
    )


def test_woken_turn_that_skips_attach_cannot_ack(tmp_path, monkeypatch):
    co, rec = _wake_setup(tmp_path, monkeypatch, "ack_only")
    out = wake.wake_recipient(co, "t", "m1", sender="sup", timeout_s=60)
    assert out["outcome"] == "uncertain", out
    assert "send_capability_required" in (rec / "ack.err").read_text()
    assert not co.is_acked("t", "cx", "m1")


def test_wake_prompt_unchanged_without_the_feature():
    base = wake.wake_prompt("t", "m1", "cx")
    assert "coord_attach" not in base
    withp = wake.wake_prompt(
        "t",
        "m1",
        "cx",
        attach={
            "role": "executor",
            "worktree_realpath": "/wt",
            "host": {"host": "codex", "session": THREAD},
        },
    )
    assert withp.endswith(base) and "coord_attach" in withp


# ------------------------------------------------------------------ Codex review repairs (2026-11-23)
def test_a_real_second_process_cannot_send_or_quietly_take_over(tmp_path):
    co = _co(tmp_path)
    _attach(co, "lead", host="claude", session="lead-sess")  # held by this (live) test process
    store = co.store.root
    snd = _cli(store, "send", "t", "--id", "m1", "--from", "lead", "--to", "all", "--kind", "progress",
               "--revision", "1", "--text", "hi")
    assert snd.returncode != 0 and "send_capability_required" in snd.stdout + snd.stderr
    att = _cli(store, "attach", "t", "lead", "--role", "supervisor", "--worktree", "/out",
               "--host", "host=claude", "--host", "session=lead-sess")
    assert att.returncode != 0 and "participant_held" in att.stdout + att.stderr
    assert co.get_message("t", "m1") is None
    _send(co, "m2", "lead")  # the holder is unaffected


def test_a_reused_pid_is_not_the_holder(tmp_path):
    co = _co(tmp_path)
    _attach(co, "sup")
    rec = co.store.read("tasks/t/participants/sup.json")
    rec["send_cap"]["holder"]["start"] = "Thu Jan  1 00:00:00 1970"  # same pid, different process
    co.store.write("tasks/t/participants/sup.json", rec)
    sendcap.forget()
    _attach(co, "sup")  # the recorded holder is gone: allowed, mints
    _send(co, "m1", "sup")


def test_store_spellings_share_capabilities(tmp_path, monkeypatch):
    """A relative and an absolute spelling of one store are one store (a symlinked root is refused by
    the store itself)."""
    co = _co(tmp_path)
    _attach(co, "sup")
    monkeypatch.chdir(tmp_path)
    rel = Coordinator(CoordStore("store"))
    rel.send(task_id="t", message_id="m1", sender="sup", recipient="all", kind="progress",
             task_revision=1, text="x")


def test_set_cursor_needs_an_attached_participant_on_an_enforced_task(tmp_path):
    co = _co(tmp_path)
    assert _code(lambda: co.set_cursor("t", "ghost", 5)) == "unknown_participant"
    assert not (Path(co.store.root) / "tasks/t/cursors/ghost.json").exists()
    legacy = Coordinator(CoordStore(str(tmp_path / "legacy")))
    legacy.create_task("t", project="p", worktree_realpath="/wt", revision=1)
    legacy.set_cursor("t", "ghost", 5)  # unchanged on a task without the feature


def test_detach_revokes_the_durable_capability(tmp_path):
    co = _co(tmp_path)
    store = co.store.root
    capf = tmp_path / "caps.json"
    assert _cli(store, "attach", "t", "obs", "--role", "observer", "--worktree", "/out",
                "--host", "host=antigravity", "--host", "session=o1", cap_file=capf).returncode == 0
    old = json.loads(capf.read_text())["caps"][0]["cap"]
    assert _cli(store, "detach", "t", "obs", cap_file=capf).returncode == 0
    assert json.loads(capf.read_text())["caps"] == []
    assert "send_cap" not in co.store.read("tasks/t/participants/obs.json")
    sendcap._CAPS[sendcap._key(co.store, "t", "obs")] = old  # a copy kept from before the detach
    assert _code(lambda: co.set_cursor("t", "obs", 1)) == "participant_detached"
    assert _code(lambda: _send(co, "m1", "obs")) == "participant_detached"


def test_an_insecure_cap_file_is_refused(tmp_path):
    co = _co(tmp_path)
    capf = tmp_path / "caps.json"
    assert _cli(co.store.root, "attach", "t", "lead", "--role", "supervisor", "--worktree", "/out",
                "--host", "host=claude", "--host", "session=s", cap_file=capf).returncode == 0
    capf.chmod(0o644)
    r = _cli(co.store.root, "send", "t", "--id", "m1", "--from", "lead", "--to", "all", "--kind",
             "progress", "--revision", "1", "--text", "hi", cap_file=capf)
    assert r.returncode != 0 and "cap_file_insecure" in r.stdout


def test_concurrent_cap_file_saves_keep_every_entry(tmp_path):
    import concurrent.futures as cf
    co = _co(tmp_path)
    ids = [f"p{i}" for i in range(24)]
    for pid in ids:
        _attach(co, pid, role="observer")
    capf = tmp_path / "caps.json"
    with cf.ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda pid: sendcap.save_to_file(str(capf), co.store, "t", pid), ids))
    got = {e["participant"] for e in json.loads(capf.read_text())["caps"]}
    assert got == set(ids)
    assert stat.S_IMODE(capf.stat().st_mode) == 0o600


def test_full_resume_never_renders_the_hash(tmp_path):
    co = _co(tmp_path)
    _attach(co, "sup")
    sha = co.store.read("tasks/t/participants/sup.json")["send_cap"]["sha256"]
    assert sha not in json.dumps(co.resume("t", "sup")) + json.dumps(co.resume("t", "sup", compact=True))


def test_wake_refused_without_launch_while_the_recipient_holder_runs(tmp_path, monkeypatch):
    co, rec = _wake_setup(tmp_path, monkeypatch, "attach_ack")
    sendcap.forget(co.store, "t", "cx")
    co.attach("t", "cx", role="executor", worktree_realpath="/wt",
              host={"host": "codex", "session": THREAD, "wake": "headless"})  # now held by a live process
    out = wake.wake_recipient(co, "t", "m1", sender="sup", timeout_s=60)
    assert out["outcome"] == "refused:recipient_held", out
    assert not (rec / "stdin.txt").exists()  # nothing was launched


def test_wake_refused_when_the_sender_is_taken_over_before_launch(tmp_path, monkeypatch):
    co, rec = _wake_setup(tmp_path, monkeypatch, "attach_ack")
    real_qs = wake._queue_state

    def takeover_then_check(thread):
        old = sendcap.held(co.store, "t", "sup")
        sendcap.forget(co.store, "t", "sup")
        _attach(co, "sup", host="claude", allow_transition=True)  # rotation lands mid-wake
        sendcap._CAPS[sendcap._key(co.store, "t", "sup")] = old
        return real_qs(thread)

    monkeypatch.setattr(wake, "_queue_state", takeover_then_check)
    out = wake.wake_recipient(co, "t", "m1", sender="sup", timeout_s=60)
    assert out["outcome"] == "refused:sender_send_capability_stale", out
    assert not (rec / "stdin.txt").exists()


def test_bridge_withholds_enter_when_the_controller_is_taken_over_during_staging(tmp_path):
    from mycelium_coord import bridge_link
    import test_bridge_link as tb
    co = Coordinator(CoordStore(str(tmp_path / "store")))
    co.create_task("t1", project="p", worktree_realpath="/wt", revision=1,
                   protocol_features=["send-capability"])
    co.attach("t1", "c", role="supervisor", worktree_realpath="/out", host=tb.HOST_C)
    co.attach("t1", "cl", role="executor", worktree_realpath="/wt", host=tb.HOST_CL)
    co.send(message_id="m1", task_id="t1", sender="c", recipient="cl", kind="task", task_revision=1,
            text="do it", host=tb.HOST_C)
    spy = {"called": False}

    def stage():
        old = sendcap.held(co.store, "t1", "c")
        sendcap.forget(co.store, "t1", "c")
        co.attach("t1", "c", role="supervisor", worktree_realpath="/out", host=tb.HOST_C,
                  allow_transition=True)
        sendcap._CAPS[sendcap._key(co.store, "t1", "c")] = old

    saved = (bridge_link._import_bridge, bridge_link._read_binding)
    try:
        tb._fake(status="accepted", spy=spy, stage_hook=stage)
        bridge_link._read_binding = lambda bid: dict(tb.BINDING_OK, controller_id="c")
        r = bridge_link.notify_via_bridge(co, task_id="t1", message_id="m1", binding_id="b1",
                                          controller_id="c", expected_revision=0)
    finally:
        bridge_link._import_bridge, bridge_link._read_binding = saved
    assert spy["called"] is False
    assert r["delivered"] is False and r.get("refused") == "refused_send_capability_stale", r
