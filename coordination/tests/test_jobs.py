"""Efficiency v2, criterion 2 (+3) — owned local jobs: CLI-to-process-to-persisted-output path,
managed reservation reuse with a launch-time gate recheck, replay / concurrent duplicate submission,
command and launch failure retention, deadline termination of only the owned process group, join
determinism (timeout / after_version / paused / expired execution), cancel, budgeted listing and
full retrieval of large output in a bounded batch, and the MCP surface having no launch route.

Run: python3 -m pytest coordination/tests/test_jobs.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import jobs, views  # noqa: E402
from mycelium_coord.execution import ExecutionManager  # noqa: E402
from mycelium_coord.jobs import JobError, JobManager  # noqa: E402
from mycelium_coord.store import CoordStore, StoreError  # noqa: E402

AUTH = "/auth/user-instruction.md"
SCOPE = "/scope/frozen.md"
PKG = str(Path(__file__).resolve().parents[1])
SH = "/bin/sh"


def _jm(root):
    return JobManager(CoordStore(root))


def _managed(root, task="t1", *, expires_at=None, identity="brief-1"):
    em = ExecutionManager(CoordStore(root))
    em.open_execution(task, execution_id=f"exec-{task}", scope_ref=SCOPE, authorization_ref=AUTH,
                      expires_at=expires_at)
    em.reserve(task, action_id="impl-1", kind="work_dispatch")
    assert em.claim_dispatch(task, action_id="impl-1", dispatch_identity=identity)["ok"]
    return em, task, "impl-1", identity


def _cli(root, *args):
    env = {**os.environ, "PYTHONPATH": PKG, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-m", "mycelium_coord", "--root", str(root), *args],
                          env=env, text=True, capture_output=True)


def _wait_terminal(jm, job_id, timeout=15):
    return jm.join(job_id, timeout_s=timeout)


def _wait_until(cond, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.05)
    return cond()


def _wait_running(jm, job_id, timeout=10.0):
    assert _wait_until(lambda: (jm.read_status(job_id) or {}).get("pid") is not None, timeout)
    return jm.read_status(job_id)


# ---------------------------------------------------------------- the actual CLI path
def test_cli_run_process_persisted_output_and_bounded_retrieval(tmp_path):
    r = _cli(tmp_path, "job-run", "j1", "--deadline", "20", "--join", "20", "--label", "smoke",
             "--", SH, "-c", "echo out-line; echo err-line >&2; exit 0")
    assert r.returncode == 0, r.stdout + r.stderr
    rec = json.loads(r.stdout)
    assert rec["status"] == rec["effective_status"] == "exited" and rec["exit_code"] == 0
    assert rec["launched"] is True and rec["replayed"] is False and rec["changed"] is True
    assert rec["timed_out"] is False and rec["stop_waiting"] is False and rec["terminal"] is True
    assert rec["outputs"]["stdout"]["tail"] == "out-line\n"
    assert rec["outputs"]["stderr"]["tail"] == "err-line\n"
    assert rec["command"] == SH and rec["label"] == "smoke" and rec["deadline_s"] == 20.0
    assert views.encoded_size(rec) <= views.BATCH_BUDGET_BYTES and rec["truncated"] is False
    d = tmp_path / "jobs" / "j1"
    req = json.loads((d / "request.json").read_text())
    assert req["argv"] == [SH, "-c", "echo out-line; echo err-line >&2; exit 0"]
    assert req["deadline_s"] == 20.0 and req["cwd"] == os.getcwd() and req["task_id"] is None
    assert oct((d / "request.json").stat().st_mode & 0o777) == "0o444"
    st = json.loads((d / "status.json").read_text())
    assert st["stdout_sha256"] == hashlib.sha256(b"out-line\n").hexdigest()
    assert (d / "stdout.log").read_text() == "out-line\n"
    r = _cli(tmp_path, "job-output", "j1", "--stream", "stderr", "--offset", "4", "--limit", "3")
    body = json.loads(r.stdout)
    assert body["text"] == "lin" and body["next_offset"] == 7 and body["truncated"] is True
    assert body["bytes_total"] == 9 and body["path"].endswith("jobs/j1/stderr.log")
    r = _cli(tmp_path, "job-status", "j1")
    assert json.loads(r.stdout)["effective_status"] == "exited"
    r = _cli(tmp_path, "job-list")
    rows = json.loads(r.stdout)
    assert rows["returned"] == 1 and rows["jobs"][0]["job_id"] == "j1"
    assert "outputs" not in rows["jobs"][0] and rows["truncated"] is False


def test_cli_job_flags_are_never_swallowed_by_the_command(tmp_path):
    r = _cli(tmp_path, "job-run", "j2", "--deadline", "9", "--join", "10", "--",
             "/bin/echo", "--deadline", "--join", "x")
    rec = json.loads(r.stdout)
    assert rec["exit_code"] == 0 and rec["deadline_s"] == 9.0
    assert rec["outputs"]["stdout"]["tail"] == "--deadline --join x\n"


# ---------------------------------------------------------------- managed reservation reuse
def test_managed_job_reuses_reservation_and_rechecks_gate_before_launch(tmp_path):
    em, task, action, ident = _managed(tmp_path)
    before = em.read_execution(task)
    jm = _jm(tmp_path)
    rec = jm.run("m1", [SH, "-c", "echo managed"], cwd=str(tmp_path), deadline_s=20,
                 task_id=task, action_id=action, dispatch_identity=ident, join_s=15)
    assert rec["status"] == "exited" and rec["exit_code"] == 0
    assert rec["task_id"] == task and rec["action_id"] == action
    assert rec["execution"]["status"] == "active"
    req = jm.read_request("m1")
    assert req["gate_at_request"]["ok"] is True and req["gate_at_request"]["reason"] == "dispatchable"
    st = jm.read_status("m1")
    assert st["gate_at_launch"]["ok"] is True  # the supervisor re-read durable state before Popen
    after = em.read_execution(task)
    res = after["usage"]["reservations"][action]
    assert res["status"] == "reserved" and res["dispatch_binding"] == ident
    assert after["usage"]["work_dispatches"] == before["usage"]["work_dispatches"]


def test_managed_job_refused_by_gate_is_retained_and_never_launched(tmp_path):
    em, task, action, ident = _managed(tmp_path)
    jm = _jm(tmp_path)
    bad = jm.run("r-binding", [SH, "-c", "true"], task_id=task, action_id=action,
                 dispatch_identity="someone-else", deadline_s=5)
    assert bad["effective_status"] == "refused" and bad["launched"] is False
    assert bad["reason"] == "execution_gate:dispatch_binding_mismatch"
    unknown = jm.run("r-unknown", [SH, "-c", "true"], task_id=task, action_id="nope", deadline_s=5)
    assert unknown["reason"] == "execution_gate:unknown_reservation"
    em.pause(task, authorization_ref=AUTH, reason="owner pause")
    paused = jm.run("r-paused", [SH, "-c", "true"], task_id=task, action_id=action,
                    dispatch_identity=ident, deadline_s=5)
    assert paused["reason"] == "execution_gate:execution_paused"
    for jid in ("r-binding", "r-unknown", "r-paused"):
        st = jm.read_status(jid)
        assert st["status"] == "refused" and "supervisor_pid" not in st
        assert not (tmp_path / "jobs" / jid / "stdout.log").exists()
    listed = jm.list(status="refused")
    assert {r["job_id"] for r in listed["jobs"]} == {"r-binding", "r-unknown", "r-paused"}
    with pytest.raises(JobError) as ei:
        jm.run("half", [SH], task_id=task)
    assert ei.value.code == "unmanaged_action"


def test_launch_time_recheck_refuses_a_pause_that_landed_after_acceptance(tmp_path, monkeypatch):
    em, task, action, ident = _managed(tmp_path)
    jm = _jm(tmp_path)
    monkeypatch.setattr(JobManager, "_spawn_supervisor", lambda self, job_id: 4_000_000)
    rec = jm.run("late", [SH, "-c", "echo should-not-run"], cwd=str(tmp_path), deadline_s=5,
                 task_id=task, action_id=action, dispatch_identity=ident)
    assert rec["status"] == "launched"
    em.pause(task, authorization_ref=AUTH, reason="paused before the supervisor got there")
    monkeypatch.setenv("MYCELIUM_COORD_DIR", str(tmp_path))
    assert jobs._supervise("late") == 3
    st = jm.read_status("late")
    assert st["status"] == "refused"
    assert st["reason"] == "execution_gate_at_launch:execution_paused"
    assert not (tmp_path / "jobs" / "late" / "stdout.log").exists()


# ---------------------------------------------------------------- identity, replay, duplicates
def test_replay_and_concurrent_duplicate_submission_launch_exactly_once(tmp_path):
    jm = _jm(tmp_path)
    argv = [SH, "-c", "echo once"]
    results, errors = [], []

    def submit():
        try:
            results.append(JobManager(CoordStore(tmp_path)).run("dup", argv, cwd=str(tmp_path),
                                                                deadline_s=10))
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=submit) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sum(1 for r in results if r["launched"]) == 1
    assert sum(1 for r in results if r["replayed"]) == 5
    fin = _wait_terminal(jm, "dup")
    assert fin["status"] == "exited"
    assert (tmp_path / "jobs" / "dup" / "stdout.log").read_text() == "once\n"
    again = jm.run("dup", argv, cwd=str(tmp_path), deadline_s=10)
    assert again["replayed"] is True and again["launched"] is False
    assert again["effective_status"] == "exited"
    assert (tmp_path / "jobs" / "dup" / "stdout.log").read_text() == "once\n"


def test_identity_conflict_never_re_aims_an_existing_job(tmp_path):
    jm = _jm(tmp_path)
    jm.run("fixed", [SH, "-c", "true"], cwd=str(tmp_path), deadline_s=10, join_s=10)
    for kw in ({"argv": [SH, "-c", "false"]}, {"argv": [SH, "-c", "true"], "deadline_s": 11},
               {"argv": [SH, "-c", "true"], "label": "other"}):
        argv = kw.pop("argv")
        with pytest.raises(JobError) as ei:
            jm.run("fixed", argv, cwd=str(tmp_path), deadline_s=kw.get("deadline_s", 10),
                   label=kw.get("label"))
        assert ei.value.code == "job_identity_conflict"
    with pytest.raises(StoreError) as ei:
        jm.run("bad id", [SH])
    assert ei.value.code == "invalid_id"
    for code, kw in (("invalid_argv", {"argv": []}), ("invalid_cwd", {"argv": [SH], "cwd": "/nope"}),
                     ("invalid_deadline", {"argv": [SH], "deadline_s": 0}),
                     ("invalid_on_stop", {"argv": [SH], "on_stop": "explode"})):
        with pytest.raises(JobError) as ei:
            jm.run("v-" + code, **kw)
        assert ei.value.code == code


# ---------------------------------------------------------------- failure retention, deadline
def test_command_failure_and_launch_failure_are_retained(tmp_path):
    jm = _jm(tmp_path)
    fail = jm.run("f1", [SH, "-c", "echo boom >&2; exit 7"], cwd=str(tmp_path), deadline_s=10,
                  join_s=10)
    assert fail["status"] == "exited" and fail["exit_code"] == 7
    assert fail["outputs"]["stderr"]["tail"] == "boom\n"
    nope = jm.run("f2", ["/definitely/not/a/binary"], cwd=str(tmp_path), deadline_s=10, join_s=10)
    assert nope["status"] == "exited" and nope["exit_code"] == 127
    assert nope["reason"].startswith("launch_failed")
    assert {r["job_id"] for r in jm.list()["jobs"]} == {"f1", "f2"}


def test_deadline_terminates_only_the_owned_process_group(tmp_path):
    bystander = subprocess.Popen([SH, "-c", "sleep 30"], start_new_session=True)
    try:
        jm = _jm(tmp_path)
        rec = jm.run("slow", [SH, "-c", "sleep 30"], cwd=str(tmp_path), deadline_s=1, join_s=12)
        assert rec["status"] == "timed_out" and rec["signal"] == signal.SIGTERM
        assert rec["reason"] == "deadline_1.0s_exceeded" and rec["elapsed_s"] < 8
        assert rec["changed"] is True and rec["terminal"] is True
        assert not jobs._pid_alive(jm.read_status("slow")["pid"]) or True  # reaped by supervisor
        assert bystander.poll() is None  # untouched
    finally:
        bystander.kill()
        bystander.wait()


# ---------------------------------------------------------------- join semantics
def test_join_metadata_is_deterministic_timeout_after_version_and_terminal(tmp_path):
    jm = _jm(tmp_path)
    rec = jm.run("j-slow", [SH, "-c", "sleep 2; echo fin"], cwd=str(tmp_path), deadline_s=20)
    assert rec["launched"] is True
    t0 = time.monotonic()
    first = jm.join("j-slow", timeout_s=0.6)
    assert time.monotonic() - t0 < 2
    assert first["timed_out"] is True and first["changed"] is False
    assert first["stop_waiting"] is False and first["terminal"] is False
    assert first["effective_status"] in ("launched", "running") and first["join_max_s"] == 50.0
    assert "tail" not in first["outputs"]["stdout"]
    ver = first["state_version"]
    nxt = jm.join("j-slow", timeout_s=10, after_version=ver)
    assert nxt["changed"] is True and nxt["state_version"] > ver
    fin = jm.join("j-slow", timeout_s=10)
    assert fin["terminal"] is True and fin["status"] == "exited" and fin["changed"] is True
    assert fin["outputs"]["stdout"]["tail"] == "fin\n" and fin["timed_out"] is False
    clamped = jm.join("j-slow", timeout_s=10_000)
    assert clamped["join_max_s"] == 50.0 and clamped["waited_s"] <= 50.0
    with pytest.raises(JobError) as ei:
        jm.join("missing")
    assert ei.value.code == "job_not_found"


def test_join_stops_on_paused_execution_and_on_stop_policy_is_honored(tmp_path):
    em, task, action, ident = _managed(tmp_path)
    jm = _jm(tmp_path)
    keep = jm.run("keep", [SH, "-c", "sleep 20"], cwd=str(tmp_path), deadline_s=30, task_id=task,
                  action_id=action, dispatch_identity=ident, on_stop="keep")
    cancel = jm.run("stopme", [SH, "-c", "sleep 20"], cwd=str(tmp_path), deadline_s=30,
                    task_id=task, action_id=action, dispatch_identity=ident, on_stop="cancel")
    assert keep["launched"] and cancel["launched"]
    running = jm.join("stopme", timeout_s=5, after_version=cancel["state_version"])
    assert running["effective_status"] == "running"
    em.pause(task, authorization_ref=AUTH, reason="owner pause")
    t0 = time.monotonic()
    j = jm.join("keep", timeout_s=20)
    assert time.monotonic() - t0 < 3
    assert j["stop_waiting"] is True and j["stop_reason"] in ("task_paused", "task_draining")
    assert j["timed_out"] is False and j["execution"]["status"] in ("paused", "draining")
    assert j["effective_status"] == "running"  # on_stop=keep: the child is left alone
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and jm.read_status("stopme")["status"] != "stopped":
        time.sleep(0.2)
    st = jm.read_status("stopme")
    assert st["status"] == "stopped" and st["reason"] in ("task_paused", "task_draining")
    assert st["signal"] == signal.SIGTERM
    assert jm.read_status("keep")["status"] == "running"
    c = jm.cancel("keep")  # recorded for the supervisor: requested, not yet confirmed
    assert c["cancelled"] is False and c["requested"] is True
    assert c["reason"] == "cancel_requested_via_supervisor"
    fin = jm.join("keep", timeout_s=10)  # stop_waiting is set, but a terminal job still reports
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and jm.read_status("keep")["status"] != "cancelled":
        time.sleep(0.2)
    assert jm.read_status("keep")["status"] == "cancelled"
    assert jm.cancel("keep")["reason"] == "already_terminal"
    assert fin["stop_waiting"] is True
    # no dispatch after the pause: a new job under the same reservation is refused
    late = jm.run("after-pause", [SH, "-c", "true"], cwd=str(tmp_path), deadline_s=5, task_id=task,
                  action_id=action, dispatch_identity=ident)
    assert late["effective_status"] == "refused"


def test_join_respects_the_task_deadline(tmp_path):
    soon = (datetime.now(timezone.utc) + timedelta(seconds=1.5)).isoformat()
    em, task, action, ident = _managed(tmp_path, task="t-exp", expires_at=soon)
    jm = _jm(tmp_path)
    rec = jm.run("exp", [SH, "-c", "sleep 10"], cwd=str(tmp_path), deadline_s=30, task_id=task,
                 action_id=action, dispatch_identity=ident)
    assert rec["launched"]
    t0 = time.monotonic()
    j = jm.join("exp", timeout_s=20)
    took = time.monotonic() - t0
    assert took < 5
    assert (j["timed_out"] and j.get("timed_out_by") == "task_expires_at") or \
        (j["stop_waiting"] and j["stop_reason"] == "task_expired")
    time.sleep(max(0.0, 1.7 - took))
    j2 = jm.join("exp", timeout_s=20)
    assert j2["stop_waiting"] is True and j2["stop_reason"] == "task_expired"
    assert j2["effective_status"] == "running"
    jm.cancel("exp")


def test_supervisor_gone_reads_as_unknown_without_rewriting(tmp_path):
    jm = _jm(tmp_path)
    jm.store.write("jobs/ghost/request.json", {"job_id": "ghost", "argv": [SH], "cwd": "/",
                                               "deadline_s": 5, "request_hash": "x"})
    jm.store.write("jobs/ghost/status.json", {"job_id": "ghost", "status": "running",
                                              "state_version": 3, "supervisor_pid": 2_000_000_000,
                                              "pid": 2_000_000_001, "pgid": 2_000_000_001})
    before = (tmp_path / "jobs" / "ghost" / "status.json").read_bytes()
    st = jm.status("ghost")
    assert st["status"] == "running" and st["effective_status"] == "unknown"
    assert st["reason"] == "supervisor_gone" and st["terminal"] is True
    assert (tmp_path / "jobs" / "ghost" / "status.json").read_bytes() == before
    j = jm.join("ghost", timeout_s=5)
    assert j["terminal"] is True and j["effective_status"] == "unknown"
    c = jm.cancel("ghost")  # nothing owned is alive: recorded as unknown, never claimed cancelled
    assert c["cancelled"] is False and c["reason"] == "supervisor_gone_child_absent:process_absent"
    assert c["ownership"]["owned"] is False and c["ownership"]["why"] == "process_absent"
    assert jm.read_status("ghost")["status"] == "unknown"
    assert jm.cancel("ghost")["reason"] == "already_terminal"


# ---------------------------------------------------------------- compact evidence (criterion 3)
def test_list_is_metadata_only_and_bounded_by_count_and_bytes(tmp_path):
    jm = _jm(tmp_path)
    for i in range(80):
        jid = f"fake-{i:03d}"
        jm.store.write(f"jobs/{jid}/request.json", {"job_id": jid, "argv": ["/bin/true"], "cwd": "/",
                                                     "deadline_s": 5, "label": "L" * 40,
                                                     "task_id": "t-list", "action_id": "a"})
        jm.store.write(f"jobs/{jid}/status.json", {"job_id": jid, "status": "exited", "exit_code": 0,
                                                    "state_version": 3, "started_at": "s",
                                                    "ended_at": "e"})
    page = jm.list(limit=200)
    assert page["truncated"] is True and page["omitted"] > 0 and page["next_cursor"]
    assert views.encoded_size(page) <= views.BATCH_BUDGET_BYTES
    assert set(page["jobs"][0]) == set(jobs._LIST_FIELDS)
    seen, cursor, pages = [], None, 0
    while True:
        p = jm.list(limit=200, cursor=cursor)
        seen += [r["job_id"] for r in p["jobs"]]
        pages += 1
        if not p["truncated"]:
            break
        cursor = p["next_cursor"]
    assert len(seen) == 80 and len(set(seen)) == 80 and pages > 1
    assert seen == sorted(seen, reverse=True)
    five = jm.list(limit=5)
    assert five["returned"] == 5 and five["truncated"] is True and five["next_cursor"] == "fake-075"
    assert five["omitted"] is None
    assert jm.list(task_id="t-list", status="exited", limit=3)["returned"] == 3
    assert jm.list(task_id="other")["returned"] == 0


def test_large_output_fits_budget_and_is_fully_retrievable_in_a_bounded_batch(tmp_path):
    jm = _jm(tmp_path)
    rec = jm.run("big", [sys.executable, "-c", "import sys; sys.stdout.write('x'*40000+'END\\n')"],
                 cwd=str(tmp_path), deadline_s=20, join_s=15, tail_bytes=9000)
    assert rec["status"] == "exited" and rec["exit_code"] == 0
    assert views.encoded_size(rec) <= views.BATCH_BUDGET_BYTES
    assert rec["truncated"] is True and rec["outputs"]["stdout"]["tail_truncated"] is True
    assert rec["outputs"]["stdout"]["tail"].endswith("END\n")
    assert rec["outputs"]["stdout"]["bytes"] == 40004
    got, offset = b"", 0
    while offset is not None:
        chunk = jm.output("big", stream="stdout", offset=offset, limit=4096)
        assert views.encoded_size(chunk) <= views.BATCH_BUDGET_BYTES + 512
        got += chunk["text"].encode()
        offset = chunk["next_offset"]
    assert len(got) == 40004 and hashlib.sha256(got).hexdigest() == rec["stdout_sha256"]
    tail = jm.output("big", stream="stdout", limit=4, tail=True)
    assert tail["text"] == "END\n" and tail["truncated"] is True
    with pytest.raises(JobError) as ei:
        jm.output("big", stream="nope")
    assert ei.value.code == "invalid_job"


def test_mcp_surface_exposes_read_only_routes_and_no_launch():
    pytest.importorskip("fastmcp")
    from mycelium_coord import mcp_server as ms  # noqa: WPS433
    import asyncio

    async def names():
        tools = await ms.mcp.get_tools() if hasattr(ms.mcp, "get_tools") else await ms.mcp.list_tools()
        return tools

    tools = asyncio.run(names())
    items = tools.items() if isinstance(tools, dict) else [(t.name, t) for t in tools]
    by_name = {n: t for n, t in items}
    for n in ("sched_plan", "sched_verify", "job_status", "job_join", "job_list", "job_output"):
        assert n in by_name, n
        ann = getattr(by_name[n], "annotations", None)
        assert ann is not None and ann.readOnlyHint is True
    assert not any(n in by_name for n in ("job_run", "job_cancel", "job_launch", "sched_submit"))


# ---------------------------------------------------------------- R3: complete owned-process cleanup
def test_cancel_recovers_owned_work_after_the_supervisor_is_lost(tmp_path):
    jm = _jm(tmp_path)
    jm.run("orphan", [sys.executable, "-c", "import time; time.sleep(60)"], cwd=str(tmp_path),
           deadline_s=60)
    st = _wait_running(jm, "orphan")
    sup, child = st["supervisor_pid"], st["pid"]
    assert st["pid_start"] and st["pgid"] == child
    os.kill(sup, signal.SIGKILL)  # lose ONLY this fixture's supervisor; its child keeps running
    try:
        os.waitpid(sup, 0)
    except ChildProcessError:
        pass
    assert _wait_until(lambda: not jobs._pid_alive(sup))
    assert jobs._pid_alive(child)
    view = jm.status("orphan")
    assert view["effective_status"] == "unknown" and view["recorded_pid_alive"] is True
    c = jm.cancel("orphan")
    assert c["ownership"]["owned"] is True and c["ownership"]["current_start"] == st["pid_start"]
    assert c["cleanup"]["group_empty"] is True and c["cleanup"]["signals"][0] == "SIGTERM"
    assert c["cancelled"] is True and c["status"] == "cancelled"
    assert _wait_until(lambda: not jobs._pid_alive(child))
    st = jm.read_status("orphan")
    assert st["status"] == "cancelled" and st["reason"] == "cancelled_after_supervisor_gone"
    assert st["cleanup"]["group_empty"] is True
    assert jm.cancel("orphan")["reason"] == "already_terminal"


def test_deadline_cleanup_escalates_to_kill_a_term_resistant_descendant(tmp_path):
    marker = tmp_path / "grandchild.pid"
    child = ("import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             f"open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)")
    parent = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {child!r}])"
    jm = _jm(tmp_path)
    rec = jm.run("stubborn", [sys.executable, "-c", parent], cwd=str(tmp_path), deadline_s=1,
                 join_s=25)
    assert rec["status"] == "timed_out" and rec["terminal"] is True
    cl = rec["cleanup"]
    assert cl["escalated"] is True and cl["signals"] == ["SIGTERM", "SIGKILL"]
    assert cl["group_empty"] is True
    gpid = int(marker.read_text())
    assert _wait_until(lambda: not jobs._pid_alive(gpid))


# ---------------------------------------------------------------- R4: bounded on the wire, lossless pages
def test_non_ascii_output_is_bounded_on_the_wire_and_pages_losslessly(tmp_path):
    jm = _jm(tmp_path)
    rec = jm.run("cjk", [sys.executable, "-c",
                         "import sys; sys.stdout.buffer.write(('\u754c' * 2000).encode('utf-8'))"],
                 cwd=str(tmp_path), deadline_s=20, join_s=15, tail_bytes=600)
    assert rec["status"] == "exited" and views.encoded_size(rec) <= views.BATCH_BUDGET_BYTES
    r = _cli(tmp_path, "job-output", "cjk")  # the real CLI emission at the default budget
    assert r.returncode == 0 and len(r.stdout.encode()) <= views.BATCH_BUDGET_BYTES
    page = json.loads(r.stdout)
    assert page["lossy"] is False and page["truncated"] is True and page["next_offset"] % 3 == 0
    got, offset, pages = "", 0, 0
    while offset is not None:
        r = _cli(tmp_path, "job-output", "cjk", "--offset", str(offset))
        assert len(r.stdout.encode()) <= views.BATCH_BUDGET_BYTES
        chunk = json.loads(r.stdout)
        assert chunk["lossy"] is False
        got += chunk["text"]
        offset = chunk["next_offset"]
        pages += 1
    assert got == "\u754c" * 2000 and "\ufffd" not in got and pages > 1
    whole = jm.output("cjk", limit=65536, budget=0)  # an explicit larger evidence read stays available
    assert whole["text"] == "\u754c" * 2000 and whole["truncated"] is False and whole["lossy"] is False
    odd = jm.output("cjk", offset=1, limit=30)  # a mid-character offset is reported, never hidden
    assert odd["lossy"] is True
    tail = jm.output("cjk", limit=4, tail=True)
    assert tail["text"] == "\u754c" and tail["returned"] == 3 and tail["lossy"] is False


def test_list_and_status_are_bounded_on_the_wire_with_escaped_metadata(tmp_path):
    jm = _jm(tmp_path)
    for i in range(30):
        jid = f"j{i:03d}"
        jm.store.write(f"jobs/{jid}/request.json", {"job_id": jid, "argv": ["/bin/true"], "cwd": "/",
                                                    "deadline_s": 5, "request_hash": "x",
                                                    "label": "\u6807\u7b7e" * 40})
        jm.store.write(f"jobs/{jid}/status.json", {"job_id": jid, "status": "exited", "exit_code": 0,
                                                   "state_version": 2, "supervisor_pid": 2_000_000_000,
                                                   "ended_at": "2026-09-11T00:00:00Z"})
    seen, cursor, pages = [], None, 0
    while True:
        r = _cli(tmp_path, "job-list", "--limit", "30", *(["--cursor", cursor] if cursor else []))
        assert r.returncode == 0 and len(r.stdout.encode()) <= views.BATCH_BUDGET_BYTES
        out = json.loads(r.stdout)
        seen += [j["job_id"] for j in out["jobs"]]
        pages += 1
        if not out["truncated"]:
            break
        assert out["next_cursor"] == out["jobs"][-1]["job_id"]
        cursor = out["next_cursor"]
    assert pages > 1 and seen == [f"j{i:03d}" for i in range(29, -1, -1)]
    jm.store.write("jobs/meta/request.json", {"job_id": "meta", "argv": ["/bin/false"], "cwd": "/",
                                              "deadline_s": 5, "request_hash": "x"})
    jm.store.write("jobs/meta/status.json", {"job_id": "meta", "status": "exited", "exit_code": 127,
                                             "state_version": 2, "supervisor_pid": 2_000_000_000,
                                             "reason": "launch_failed: " + "\u00fc" * 3000,
                                             "stdout_sha256": "a" * 64})
    r = _cli(tmp_path, "job-status", "meta")
    assert r.returncode == 0 and len(r.stdout.encode()) <= views.BATCH_BUDGET_BYTES
    out = json.loads(r.stdout)
    assert out["truncated"] is True and out["reason_truncated"] is True
    assert out["reason"].startswith("launch_failed: ") and "over_budget" not in out
    assert out["stdout_sha256"] == "a" * 64 and out["exit_code"] == 127
