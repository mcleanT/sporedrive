#!/usr/bin/env python3
"""M6 wake: registry, gates, outcome vocabulary and the Codex exec-resume route.

A FAKE codex executable stands in for the real one (no real codex, agy or claude runs, the real
~/.codex queue DB is never read: every test points the queue at a fixture path). Run on Betty only.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import Coordinator, ProtocolError, bridge_link, wake  # noqa: E402
from mycelium_coord.execution import ExecutionManager  # noqa: E402
from mycelium_coord.store import CoordStore, StoreError  # noqa: E402

ADV = "MYCELIUM_COORD_ADVISORY"
THREAD = "01a2090c-f749-72e0-a856-3dcad3d24574"
EXTRA = ["--ignore-user-config", "-c", 'mcp_servers.x.command="y"']
PKG_ROOT = str(Path(__file__).resolve().parents[1])

FAKE = """#!/bin/sh
d="{rec}"
n=$(cat "$d/count" 2>/dev/null || echo 0); echo $((n+1)) > "$d/count"
printf '%s\\n' "$@" > "$d/argv.txt"
cat > "$d/stdin.txt"
case "$FAKE_MODE" in
  ack) PYTHONPATH="{pkg}" MYCELIUM_COORD_DIR="{store}" "{py}" -m mycelium_coord ack "$FAKE_TASK" "$FAKE_PID" "$FAKE_MID" >/dev/null ;;
  noack) : ;;
  sleep) sleep 30 ;;
  fail) exit 7 ;;
esac
exit 0
"""


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.delenv(ADV, raising=False)
    monkeypatch.setenv(
        wake.ENV_QUEUE_DB, str(tmp_path / "no-queue.sqlite")
    )  # absent -> no queued items
    monkeypatch.delenv(wake.ENV_CODEX_ARGS, raising=False)
    monkeypatch.delenv(wake.ENV_CODEX_BIN, raising=False)


def _fake(tmp_path, monkeypatch, mode, store_dir):
    rec = tmp_path / "rec"
    rec.mkdir(exist_ok=True)
    exe = tmp_path / "fake-codex"
    exe.write_text(
        FAKE.format(rec=rec, pkg=PKG_ROOT, store=store_dir, py=sys.executable)
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv(wake.ENV_CODEX_BIN, str(exe))
    monkeypatch.setenv(wake.ENV_CODEX_ARGS, json.dumps(EXTRA))
    monkeypatch.setenv("FAKE_MODE", mode)
    monkeypatch.setenv("FAKE_TASK", "t")
    monkeypatch.setenv("FAKE_PID", "cx")
    monkeypatch.setenv("FAKE_MID", "m1")
    return rec


def _calls(rec):
    f = rec / "count"
    return int(f.read_text()) if f.exists() else 0


def _mk(
    tmp_path,
    features=("triangle-kinds", "codex-exec-wake"),
    headless=True,
    cx_host="codex",
):
    sd = tmp_path / "store"
    co = Coordinator(CoordStore(str(sd)))
    co.create_task(
        "t",
        project="p",
        worktree_realpath="/wt",
        revision=1,
        protocol_features=list(features) if features else None,
    )
    co.attach(
        "t",
        "sup",
        role="supervisor",
        worktree_realpath="/out",
        host={"host": "claude", "session": "supsess"},
    )
    host = {"host": cx_host, "session": THREAD}
    if headless:
        host["wake"] = "headless"
    co.attach("t", "cx", role="executor", worktree_realpath="/wt", host=host)
    co.attach(
        "t",
        "obs",
        role="observer",
        worktree_realpath="/wt",
        host={"host": "codex", "session": "obs-thread", "wake": "headless"},
    )
    return co, sd


def _send(co, mid="m1", to="cx", kind="task"):
    return co.send(
        task_id="t",
        message_id=mid,
        sender="sup",
        recipient=to,
        kind=kind,
        task_revision=1,
        text="do it",
    )


def _wake(co, mid="m1", **kw):
    return wake.wake_recipient(co, "t", mid, sender="sup", **kw)


# ------------------------------------------------------------------ codex route outcomes
def test_codex_ack_in_store_is_accepted_and_gets_exact_prompt_and_args(
    tmp_path, monkeypatch
):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co)
    r = _wake(co)
    assert r["outcome"] == "accepted" and r["route"] == "codex"
    assert r["state_before"] == "persisted" and r["state_after"] == "acknowledged"
    assert (
        r["exit"] == 0
        and r["timed_out"] is False
        and r["request_id"].startswith("wake-")
    )
    assert co.message_state("t", "cx", "m1") == "acknowledged"
    assert (
        (rec / "stdin.txt").read_text()
        == wake.wake_prompt("t", "m1", "cx")
        == (
            "New coordination message m1 on task t for participant cx. Read it with coord_inbox (task t, "
            "participant cx), acknowledge it with coord_ack (task t, participant cx, message m1), then act "
            "within your role."
        )
    )
    argv = (rec / "argv.txt").read_text().split("\n")[:-1]
    assert argv == ["exec", "resume", "--json", *EXTRA, THREAD, "-"]
    assert not any("bypass" in a or "yolo" in a for a in argv)
    stored = co.store.read("tasks/t/wakes/m1.json")
    assert stored["outcome"] == "accepted"
    audit = (sd / "tasks/t/audit.jsonl").read_text()
    assert '"kind": "wake"' in audit and '"outcome": "accepted"' in audit


def test_codex_exit_zero_without_ack_is_uncertain(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    _fake(tmp_path, monkeypatch, "noack", sd)
    _send(co)
    r = _wake(co)
    assert (
        r["outcome"] == "uncertain"
        and r["exit"] == 0
        and r["state_after"] == "persisted"
    )


def test_codex_timeout_is_uncertain_timed_out(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    _fake(tmp_path, monkeypatch, "sleep", sd)
    _send(co)
    r = _wake(co, timeout_s=1.0)
    assert r["outcome"] == "uncertain" and r["timed_out"] is True


def test_codex_nonzero_exit_is_uncertain(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    _fake(tmp_path, monkeypatch, "fail", sd)
    _send(co)
    r = _wake(co)
    assert r["outcome"] == "uncertain" and r["exit"] == 7


def test_idempotent_second_call_returns_receipt_and_does_not_rerun(
    tmp_path, monkeypatch
):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "noack", sd)
    _send(co)
    first = _wake(co)
    assert first["outcome"] == "uncertain" and _calls(rec) == 1
    second = _wake(co)
    assert second["outcome"] == "uncertain" and second["idempotent_replay"] is True
    assert second["request_id"] == first["request_id"] and _calls(rec) == 1


def test_idempotent_accepted_not_rerun(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co)
    _wake(co)
    again = _wake(co)
    assert (
        again["outcome"] == "accepted"
        and again["idempotent_replay"]
        and _calls(rec) == 1
    )


# ------------------------------------------------------------------ gates
def test_opt_in_missing_refused_and_not_run(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path, features=("triangle-kinds",))
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co)
    assert (
        _wake(co)["outcome"] == "refused:codex_exec_wake_not_enabled"
        and _calls(rec) == 0
    )


def test_headless_marker_missing_refused(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path, headless=False)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co)
    assert _wake(co)["outcome"] == "refused:not_headless_owned" and _calls(rec) == 0


def test_headless_marker_must_be_exact(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    p = co.store.read("tasks/t/participants/cx.json")
    p["host"]["wake"] = "interactive"
    co.store.write("tasks/t/participants/cx.json", p)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co)
    assert _wake(co)["outcome"] == "refused:not_headless_owned" and _calls(rec) == 0


def test_observer_recipient_refused(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co, "m1", to="obs", kind="consult_request")
    assert _wake(co)["outcome"] == "refused:observer_not_wakeable" and _calls(rec) == 0


def test_non_work_kind_refused(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co, "m1", kind="progress")
    assert _wake(co)["outcome"] == "refused:kind_not_wakeable" and _calls(rec) == 0


def test_role_and_all_recipients_refused_as_fan_out(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co, "m1", to="executor")
    _send(co, "m2", to="all")
    assert _wake(co, "m1")["outcome"] == "refused:fan_out_recipient"
    assert (
        _wake(co, "m2")["outcome"] == "refused:fan_out_recipient" and _calls(rec) == 0
    )


def test_unknown_message_raises(tmp_path):
    co, _ = _mk(tmp_path)
    with pytest.raises(ProtocolError) as e:
        _wake(co, "nope")
    assert e.value.code == "unknown_message"


def test_already_acknowledged_message_is_not_woken(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co)
    co.ack("t", "cx", "m1")
    assert _wake(co)["outcome"] == "refused:already_acknowledged" and _calls(rec) == 0


def test_advisory_process_refused(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co)
    monkeypatch.setenv(ADV, "1")
    with pytest.raises(ProtocolError) as e:
        _wake(co)
    assert e.value.code == "advisory_process_refused" and _calls(rec) == 0


def test_per_task_cap_default_and_override(tmp_path, monkeypatch):
    assert (
        wake.DEFAULT_WAKE_CAP == 20
        and wake._wake_cap({}) == 20
        and wake._wake_cap({"wake_budget": 3}) == 3
    )
    co, sd = _mk(tmp_path)
    t = co.store.read("tasks/t/task.json")
    t["wake_budget"] = 1
    co.store.write("tasks/t/task.json", t)
    rec = _fake(tmp_path, monkeypatch, "noack", sd)
    _send(co, "m1")
    _send(co, "m2")
    assert _wake(co, "m1")["outcome"] == "uncertain"
    assert (
        _wake(co, "m2")["outcome"] == "refused:wake_budget_exhausted"
        and _calls(rec) == 1
    )


def test_refused_wake_is_not_billed_against_the_cap(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path, headless=False)
    t = co.store.read("tasks/t/task.json")
    t["wake_budget"] = 1
    co.store.write("tasks/t/task.json", t)
    _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co, "m1")
    _send(co, "m2")
    assert _wake(co, "m1")["outcome"] == "refused:not_headless_owned"
    assert _wake(co, "m2")["outcome"] == "refused:not_headless_owned"


def _set_participant(co, pid, **host_upd):
    rel = f"tasks/t/participants/{pid}.json"
    rec = co.store.read(rel)
    detached = host_upd.pop("_detached", None)
    rec["host"] = {**rec["host"], **host_upd}
    for k in [k for k, v in host_upd.items() if v is None]:
        rec["host"].pop(k)
    if detached is not None:
        rec["detached"] = detached
    co.store.write(rel, rec)


@pytest.mark.parametrize(
    "session",
    [
        "--dangerously-bypass-approvals-and-sandbox",
        "not-a-uuid",
        THREAD + " --yolo",
        THREAD[:-1],
    ],
)
def test_non_uuid_thread_id_is_refused_and_never_launched(
    tmp_path, monkeypatch, session
):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _set_participant(co, "cx", session=session)
    _send(co)
    assert _wake(co)["outcome"] == "refused:invalid_thread_id" and _calls(rec) == 0


def test_uppercase_uuid_thread_is_accepted(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _set_participant(co, "cx", session=THREAD.upper())
    _send(co)
    assert _wake(co)["outcome"] == "accepted" and _calls(rec) == 1


@pytest.mark.parametrize(
    "args",
    [
        ["--dangerously-bypass-approvals-and-sandbox"],
        ["--yolo"],
        ["-c", 'sandbox_mode="danger-full-access"'],
        ["-c", 'approval_policy="never"'],
        ["-c"],
        ["-c", "--yolo"],
        ["--ignore-user-config", "--json"],
        ["-cmcp_servers.x=1"],
    ],
)
def test_codex_args_outside_allowlist_are_refused(tmp_path, monkeypatch, args):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    monkeypatch.setenv(wake.ENV_CODEX_ARGS, json.dumps(args))
    _send(co)
    assert _wake(co)["outcome"] == "refused:codex_args_not_allowed" and _calls(rec) == 0


def test_isolation_flags_are_accepted(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    monkeypatch.setenv(
        wake.ENV_CODEX_ARGS,
        json.dumps(
            [
                "--ignore-user-config",
                "-c",
                'mcp_servers.mycelium-coord.command="x"',
                "-c",
                'mcp_servers.mycelium-coord.env={A="b"}',
            ]
        ),
    )
    _send(co)
    assert _wake(co)["outcome"] == "accepted" and _calls(rec) == 1


def test_claude_route_managed_paused_refused_and_bridge_not_called(
    tmp_path, monkeypatch
):
    co, sd = _mk(tmp_path, cx_host="claude")
    calls = []
    monkeypatch.setattr(
        bridge_link, "notify_via_bridge", lambda c, **kw: calls.append(kw) or {}
    )
    em = ExecutionManager(co.store)
    em.open_execution(
        "t",
        execution_id="exec-t",
        scope_ref="/scope/s.md",
        authorization_ref="/auth/a.md",
    )
    em.pause("t", authorization_ref="/auth/a.md", reason="test")
    _send(co, "m1", kind="consult_request")
    r = _wake(co, binding_id="b1", controller_id="ctl", expected_revision=1)
    assert r["outcome"].startswith("refused:execution_gate:") and calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        {"_detached": True},
        {"session": "11111111-2222-3333-4444-555555555555"},
        {"wake": None},
        {"host": "claude"},
    ],
)
def test_recipient_changed_before_launch_is_refused(tmp_path, monkeypatch, mutation):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    real = wake._queue_state

    def hooked(thread):
        out = real(thread)
        _set_participant(co, "cx", **dict(mutation))
        return out

    monkeypatch.setattr(wake, "_queue_state", hooked)
    _send(co)
    assert _wake(co)["outcome"] == "refused:recipient_changed" and _calls(rec) == 0


@pytest.mark.parametrize(
    "mutation",
    [{"_detached": True}, {"session": "00000000-0000-4000-8000-000000000000"}, {"wake": None}],
)
def test_recipient_changed_during_get_cursor_is_refused(
    tmp_path, monkeypatch, mutation
):
    # Codex verify reproducer (m7-codex-verify, finding 4): a transition triggered during
    # get_cursor must still be caught — the re-read now runs after it, under the task lock.
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    real = co.get_cursor

    def hooked(task_id, pid):
        out = real(task_id, pid)
        _set_participant(co, "cx", **dict(mutation))
        return out

    monkeypatch.setattr(co, "get_cursor", hooked)
    _send(co)
    assert _wake(co)["outcome"] == "refused:recipient_changed" and _calls(rec) == 0


def test_recipient_recheck_and_launch_hold_the_task_lock(tmp_path, monkeypatch):
    # the fake codex launch happens while the task lock is held: a non-blocking flock attempt from
    # inside Popen must fail
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    seen = {}
    real_popen = wake.subprocess.Popen

    def probe():
        # store.lock is re-entrant per THREAD, so probe from another thread (own flock fd)
        try:
            with co.store.lock("t", timeout_s=0.05):
                seen["lock_free"] = True
        except StoreError as e:
            seen["lock_free"] = False if e.code == "lock_timeout" else repr(e)

    def spy(*a, **kw):
        th = threading.Thread(target=probe)
        th.start()
        th.join()
        return real_popen(*a, **kw)

    monkeypatch.setattr(wake.subprocess, "Popen", spy)
    _send(co)
    assert _wake(co)["outcome"] == "accepted" and seen.get("lock_free") is False


@pytest.mark.parametrize(
    "bad", [float("inf"), float("nan"), 0, -1, 901, 900.5, True, "5", float("-inf")]
)
def test_timeout_outside_range_is_rejected_before_side_effects(
    tmp_path, monkeypatch, bad
):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co)
    with pytest.raises(ProtocolError) as ei:
        _wake(co, timeout_s=bad)
    assert ei.value.code == "invalid_timeout"
    assert _calls(rec) == 0 and co.store.read("tasks/t/wakes/m1.json") is None


def test_timeout_boundary_900_is_accepted(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co)
    assert _wake(co, timeout_s=900)["outcome"] == "accepted" and _calls(rec) == 1


def test_exception_after_launch_kills_process_group_and_stays_uncertain(
    tmp_path, monkeypatch
):
    import os
    import subprocess

    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "sleep", sd)
    pids = []

    class Boom(subprocess.Popen):
        def communicate(self, *a, **k):
            if not pids:
                pids.append(self.pid)
                raise RuntimeError("boom")
            return super().communicate(*a, **k)

    monkeypatch.setattr(wake.subprocess, "Popen", Boom)
    _send(co)
    r = _wake(co)
    assert r["outcome"] == "uncertain" and "boom" in r["error"]
    assert len(pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


def test_managed_paused_execution_refuses_before_run(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    em = ExecutionManager(co.store)
    em.open_execution(
        "t",
        execution_id="exec-t",
        scope_ref="/scope/s.md",
        authorization_ref="/auth/a.md",
    )
    em.pause("t", authorization_ref="/auth/a.md", reason="test")
    _send(co, "m1", kind="consult_request")
    r = _wake(co)
    assert r["outcome"].startswith("refused:execution_gate:") and _calls(rec) == 0


# ------------------------------------------------------------------ queue guard
# The REAL Codex queue schema (codex 0.171.0, captured from a scratch copy of ~/.codex/queue_1.sqlite;
# evidence/m6-queue-check/real-queue-schema.sql). Drained items are DELETED from queued_items, but the
# revision triggers leave a permanent queued_thread_revisions row per thread — which must NOT count.
_REAL_QUEUE_SCHEMA = """
CREATE TABLE queued_items (id TEXT PRIMARY KEY NOT NULL, thread_id TEXT NOT NULL, payload_json TEXT NOT NULL,
    queue_order INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL);
CREATE TABLE queued_thread_revisions (thread_id TEXT PRIMARY KEY NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
CREATE TRIGGER queued_items_revision_after_insert AFTER INSERT ON queued_items BEGIN
    INSERT INTO queued_thread_revisions (thread_id) VALUES (NEW.thread_id)
    ON CONFLICT(thread_id) DO UPDATE SET revision = (SELECT COALESCE(MAX(revision), 0) + 1 FROM queued_thread_revisions); END;
CREATE TRIGGER queued_items_revision_after_delete AFTER DELETE ON queued_items BEGIN
    INSERT INTO queued_thread_revisions (thread_id) VALUES (OLD.thread_id)
    ON CONFLICT(thread_id) DO UPDATE SET revision = (SELECT COALESCE(MAX(revision), 0) + 1 FROM queued_thread_revisions); END;
"""


def _queue_db(path, rows, with_thread_col=True, drained=()):
    con = sqlite3.connect(path)
    if with_thread_col:
        con.executescript(_REAL_QUEUE_SCHEMA)
        for i, r in enumerate(list(rows) + list(drained)):
            con.execute(
                "insert into queued_items (id, thread_id, payload_json, queue_order, created_at_ms, updated_at_ms)"
                " values (?,?,?,?,0,0)",
                (f"q{i}", r, "{}", i),
            )
        for r in drained:  # drained = inserted then deleted, as Codex does
            con.execute("delete from queued_items where thread_id = ?", (r,))
    else:
        con.execute("create table something (a text, b text)")
    con.commit()
    con.close()


def test_queued_items_for_the_thread_refuse(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    db = tmp_path / "q.sqlite"
    _queue_db(db, [THREAD])
    monkeypatch.setenv(wake.ENV_QUEUE_DB, str(db))
    _send(co)
    assert (
        _wake(co)["outcome"] == "refused:thread_has_queued_items" and _calls(rec) == 0
    )


def test_queue_with_other_threads_or_done_rows_is_clear(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    db = tmp_path / "q.sqlite"
    _queue_db(db, ["some-other-thread"])
    monkeypatch.setenv(wake.ENV_QUEUE_DB, str(db))
    _send(co)
    assert _wake(co)["outcome"] == "accepted" and _calls(rec) == 1
    # regression (lead, 2026-11-23): a DRAINED thread keeps a queued_thread_revisions row forever;
    # that row must not make the thread look queued (it would make every thread that ever queued
    # anything permanently unwakeable).
    db2 = tmp_path / "q2.sqlite"
    _queue_db(db2, [], drained=[THREAD])
    con = sqlite3.connect(db2)
    assert (
        con.execute(
            "select count(*) from queued_thread_revisions where thread_id = ?",
            (THREAD,),
        ).fetchone()[0]
        == 1
    )
    con.close()
    monkeypatch.setenv(wake.ENV_QUEUE_DB, str(db2))
    assert wake._queue_state(THREAD)[0] == "clear"


def test_queue_db_with_an_unrecognised_layout_is_unknown(tmp_path, monkeypatch):
    # a thread column in some OTHER table is not the known layout: fail closed
    db = tmp_path / "q.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "create table queue (id integer primary key, thread_id text, status text)"
    )
    con.commit()
    con.close()
    monkeypatch.setenv(wake.ENV_QUEUE_DB, str(db))
    assert wake._queue_state(THREAD)[0] == "unknown"


def test_unknown_schema_or_unreadable_db_refuses_queue_unknown(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    db = tmp_path / "q.sqlite"
    _queue_db(db, [], with_thread_col=False)
    monkeypatch.setenv(wake.ENV_QUEUE_DB, str(db))
    _send(co, "m1")
    assert _wake(co, "m1")["outcome"] == "refused:queue_unknown"
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"this is not a sqlite database at all" * 10)
    monkeypatch.setenv(wake.ENV_QUEUE_DB, str(junk))
    _send(co, "m2")
    assert _wake(co, "m2")["outcome"] == "refused:queue_unknown" and _calls(rec) == 0


# ------------------------------------------------------------------ other host kinds
def test_antigravity_and_unknown_host_are_not_supported(tmp_path, monkeypatch):
    co, sd = _mk(tmp_path, cx_host="antigravity")
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co)
    r = _wake(co)
    assert (
        r["outcome"] == "not_supported"
        and r["route"] == "antigravity"
        and _calls(rec) == 0
    )
    p = co.store.read("tasks/t/participants/cx.json")
    p["host"]["host"] = "somethingelse"
    co.store.write("tasks/t/participants/cx.json", p)
    _send(co, "m2")
    r2 = _wake(co, "m2")
    assert r2["outcome"] == "not_supported" and r2["route"] == "unknown"
    assert co.message_state("t", "cx", "m2") == "persisted"  # nothing fabricated


def test_claude_route_calls_notify_via_bridge_with_same_arguments(
    tmp_path, monkeypatch
):
    co, sd = _mk(tmp_path, cx_host="claude")
    seen = {}

    def fake_notify(c, **kw):
        seen["co"], seen["kw"] = c, kw
        return {
            "message_id": kw["message_id"],
            "bridge_request_id": "b-req",
            "delivered": True,
            "state": "delivered",
        }

    monkeypatch.setattr(bridge_link, "notify_via_bridge", fake_notify)
    _send(co)
    r = _wake(
        co,
        binding_id="b1",
        controller_id="ctl",
        expected_revision=4,
        accept_timeout_s=5.0,
        execution_action_id="act-1",
    )
    assert r["outcome"] == "accepted" and r["route"] == "claude"
    assert seen["co"] is co
    assert seen["kw"] == dict(
        task_id="t",
        message_id="m1",
        binding_id="b1",
        controller_id="ctl",
        expected_revision=4,
        accept_timeout_s=5.0,
        execution_action_id="act-1",
    )


@pytest.mark.parametrize(
    "bridge_out,expected",
    [
        ({"state": "uncertain", "delivered": False}, "uncertain"),
        ({"state": "bridge_unavailable", "delivered": False}, "unavailable"),
        (
            {"state": "pending", "delivered": False, "refused": "busy"},
            "refused:bridge_busy",
        ),
        (
            {
                "state": "refused_by_execution_gate",
                "delivered": False,
                "reason": "execution_paused",
            },
            "refused:execution_gate:execution_paused",
        ),
        ({"state": "unknown", "delivered": False}, "uncertain"),
    ],
)
def test_claude_route_outcome_mapping(tmp_path, monkeypatch, bridge_out, expected):
    co, _ = _mk(tmp_path, cx_host="claude")
    monkeypatch.setattr(
        bridge_link,
        "notify_via_bridge",
        lambda c, **kw: {"message_id": "m1", **bridge_out},
    )
    _send(co)
    assert (
        _wake(co, binding_id="b1", controller_id="ctl", expected_revision=0)["outcome"]
        == expected
    )


def test_claude_route_needs_bridge_arguments_and_maps_protocol_errors(
    tmp_path, monkeypatch
):
    co, _ = _mk(tmp_path, cx_host="claude")
    _send(co)
    assert _wake(co)["outcome"] == "refused:bridge_arguments_required"

    def boom(c, **kw):
        raise ProtocolError("binding_identity_mismatch", "b1", "no")

    monkeypatch.setattr(bridge_link, "notify_via_bridge", boom)
    r = _wake(co, binding_id="b1", controller_id="ctl", expected_revision=0)
    assert r["outcome"] == "refused:binding_identity_mismatch"


def test_claude_route_exception_is_uncertain_and_never_rerun(tmp_path, monkeypatch):
    co, _ = _mk(tmp_path, cx_host="claude")
    n = {"c": 0}

    def boom(c, **kw):
        n["c"] += 1
        raise RuntimeError("socket died after send")

    monkeypatch.setattr(bridge_link, "notify_via_bridge", boom)
    _send(co)
    kw = dict(binding_id="b1", controller_id="ctl", expected_revision=0)
    assert _wake(co, **kw)["outcome"] == "uncertain"
    assert _wake(co, **kw)["idempotent_replay"] is True and n["c"] == 1


# ------------------------------------------------------------------ surfaces
def test_cli_wake_op_records_and_is_refused_in_advisory(tmp_path, monkeypatch, capsys):
    from mycelium_coord import cli

    co, sd = _mk(tmp_path, cx_host="antigravity")
    _send(co)
    rc = cli.run(["--root", str(sd), "wake", "t", "m1", "--sender", "sup"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "not_supported"
    monkeypatch.setenv(ADV, "1")
    _send_adv = cli.run(["--root", str(sd), "wake", "t", "m1", "--sender", "sup"])
    assert _send_adv == 3
    assert json.loads(capsys.readouterr().out)["error"] == "advisory_process_refused"


def test_mcp_tool_is_not_advisory_allowed():
    pytest.importorskip("fastmcp")
    from mycelium_coord import mcp_server

    assert "coord_wake" not in mcp_server.ADVISORY_ALLOWED_TOOLS


def test_task_lock_timeout_before_launch_is_unavailable_and_retryable(tmp_path, monkeypatch):
    # advisor check of the lead's finding-4 fix: failing to take the task lock means nothing ran,
    # so the outcome must be retryable `unavailable`, never a permanent `uncertain`.
    co, sd = _mk(tmp_path)
    rec = _fake(tmp_path, monkeypatch, "ack", sd)
    _send(co)
    real_lock = co._lock

    def timeout_lock(task_id):
        raise StoreError("lock_timeout", "locks/t.lock", "could not acquire t lock in 5.0s")

    monkeypatch.setattr(co, "_lock", timeout_lock)
    r = _wake(co)
    assert r["outcome"] == "unavailable" and _calls(rec) == 0
    monkeypatch.setattr(co, "_lock", real_lock)
    r2 = _wake(co)
    assert r2["outcome"] == "accepted" and not r2.get("idempotent_replay") and _calls(rec) == 1
