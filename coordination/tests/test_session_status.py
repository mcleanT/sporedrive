#!/usr/bin/env python3
"""Criterion C: individual session status + bounded inbox, published NATURALLY on BOTH hosts through
the actual SessionStart/PostToolUse hook adapter, with persisted lock-serialised seq + binding
generation (PLAN v1 section 3, PLAN-v2 R4/R5).

Two layers:
  * pure coord: allocate path is strictly-increasing/persisted, bumps generation on a session change,
    and the explicit-seq path still refuses stale / replaced / out-of-order (R6 negatives, offline);
  * the real hook script: run for host=claude AND host=codex, SessionStart AND PostToolUse, asserting
    honest status is published and that a running peer sees NEW mail once at the active boundary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import Coordinator, ProtocolError  # noqa: E402
from mycelium_coord.store import CoordStore  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "coordination" / "hooks" / "mycelium-coord-attach.sh"


def _co(t):
    co = Coordinator(CoordStore(Path(t)))
    co.create_task("t", project="p", worktree_realpath="/wt", revision=1)
    return co


# ---------------------------------------------------------------- pure coord: allocate + refusals
def test_allocate_seq_is_persisted_and_strictly_increasing():
    with tempfile.TemporaryDirectory() as t:
        co = _co(t)
        co.attach("t", "cl", role="executor", worktree_realpath="/wt",
                  host={"host": "claude", "session": "s1", "native_id": "1"})
        r0 = co.publish_session_status("t", "cl", native_session_id="s1", seq=0,
                                       source="claude.SessionStart.startup", runtime_state="starting", allocate=True)
        r1 = co.publish_session_status("t", "cl", native_session_id="s1", seq=0,
                                       source="claude.PostToolUse.Bash", runtime_state="active", allocate=True)
        r2 = co.publish_session_status("t", "cl", native_session_id="s1", seq=0,
                                       source="claude.PostToolUse.Bash", runtime_state="active", allocate=True)
        assert (r0["generation"], r0["seq"]) == (0, 0)
        assert (r1["generation"], r1["seq"]) == (0, 1)
        assert (r2["generation"], r2["seq"]) == (0, 2)  # persisted across calls, strictly increasing
        assert r2["sequence_source"] == "coordinator_persisted"  # NOT a host clock (labelled)


def test_allocate_bumps_generation_when_a_different_session_takes_over():
    with tempfile.TemporaryDirectory() as t:
        co = _co(t)
        co.attach("t", "cl", role="executor", worktree_realpath="/wt",
                  host={"host": "claude", "session": "s1", "native_id": "1"})
        a = co.publish_session_status("t", "cl", native_session_id="s1", seq=0, source="x", allocate=True)
        b = co.publish_session_status("t", "cl", native_session_id="s2", seq=0, source="x", allocate=True)
        assert (a["generation"], a["seq"]) == (0, 0)
        assert b["generation"] == 1 and b["seq"] == 0  # replacement session -> strictly newer generation
        assert b["native_session_id"] == "s2"


def test_explicit_seq_refusals_still_hold():
    with tempfile.TemporaryDirectory() as t:
        co = _co(t)
        co.attach("t", "cl", role="executor", worktree_realpath="/wt",
                  host={"host": "claude", "session": "s1", "native_id": "1"})
        co.publish_session_status("t", "cl", native_session_id="s1", seq=5, generation=1, source="x")
        # out-of-order within a generation
        try:
            co.publish_session_status("t", "cl", native_session_id="s1", seq=5, generation=1, source="x")
            assert False
        except ProtocolError as e:
            assert e.code == "status_out_of_order"
        # generation went backwards for the same session
        try:
            co.publish_session_status("t", "cl", native_session_id="s1", seq=9, generation=0, source="x")
            assert False
        except ProtocolError as e:
            assert e.code == "status_stale_generation"
        # a different session that is NOT strictly newer cannot take over the slot
        try:
            co.publish_session_status("t", "cl", native_session_id="s2", seq=99, generation=1, source="x")
            assert False
        except ProtocolError as e:
            assert e.code == "status_replaced_session"


def test_read_status_freshness_and_missing_are_unknown_never_idle():
    with tempfile.TemporaryDirectory() as t:
        co = _co(t)
        co.attach("t", "cl", role="executor", worktree_realpath="/wt",
                  host={"host": "claude", "session": "s1", "native_id": "1"})
        co.publish_session_status("t", "cl", native_session_id="s1", seq=0, source="x",
                                  runtime_state="active", allocate=True)
        fresh = co.read_session_status("t", "cl", max_age_s=300)["status"]
        assert fresh["runtime_state"] == "active" and fresh["freshness"] == "fresh"
        stale = co.read_session_status("t", "cl", max_age_s=0)["status"]  # force stale
        assert stale["runtime_state"] == "unknown" and stale["freshness"] == "stale"
        missing = co.read_session_status("t", "cx")
        assert missing["present"] is False and missing["status"] is None


# ---------------------------------------------------------------- the real two-host hook adapter
def _run_hook(store_root, host, session_id, stdin_obj):
    env = dict(os.environ)
    env["MYCELIUM_COORD_DIR"] = str(store_root)
    if host == "claude":
        env["CLAUDE_PROJECT_DIR"] = "/tmp/claude-proj"
        env.pop("MYCELIUM_HOOK_HOST", None)
    else:
        env.pop("CLAUDE_PROJECT_DIR", None)
        env["MYCELIUM_HOOK_HOST"] = "codex"
    return subprocess.run([str(HOOK)], input=json.dumps(stdin_obj),
                          capture_output=True, text=True, env=env)


def _setup_host(co, host, part, session_id):
    co.attach("t", part, role="executor", worktree_realpath="/wt",
              host={"host": host, "session": session_id, "native_id": part})
    co.select_session("t", part, host_kind=host, session_id=session_id)


def test_hook_publishes_status_and_surfaces_active_mail_on_both_hosts():
    for host, part, sess in (("claude", "cl", "sc1"), ("codex", "cx", "sx1")):
        with tempfile.TemporaryDirectory() as t:
            co = _co(t)
            _setup_host(co, host, part, sess)

            # 1) SessionStart: publishes starting status + emits resume context
            r = _run_hook(t, host, sess, {"session_id": sess, "source": "startup",
                                          "hook_event_name": "SessionStart"})
            assert r.returncode == 0, r.stderr
            assert "attached to task t" in r.stdout
            st = co.read_session_status("t", part)["status"]
            assert st and st["runtime_state"] == "starting"
            assert st["evidence_source"] == f"{host}.SessionStart.startup"
            assert st["sequence_source"] == "coordinator_persisted"
            gen0, seq0 = st["generation"], st["seq"]

            # a peer sends addressed mail WHILE this session is already running
            co.attach("t", "peer", role="supervisor", worktree_realpath="/wt",
                      host={"host": "codex", "session": "peersess", "native_id": "peer"})
            co.send(message_id="mA", task_id="t", sender="peer", recipient=part, kind="progress",
                    task_revision=1, text="new mail for the running peer")

            # 2) PostToolUse (active boundary): publishes active status + surfaces the NEW mail once
            r2 = _run_hook(t, host, sess, {"session_id": sess, "hook_event_name": "PostToolUse",
                                           "tool_name": "Bash"})
            assert r2.returncode == 0, r2.stderr
            assert "mA" in r2.stdout and "new mail" in r2.stdout
            st2 = co.read_session_status("t", part)["status"]
            assert st2["runtime_state"] == "active"
            assert (st2["generation"], st2["seq"]) == (gen0, seq0 + 1)  # persisted seq advanced

            # 3) a SECOND active boundary: cursor advanced, so the same mail is NOT re-injected
            r3 = _run_hook(t, host, sess, {"session_id": sess, "hook_event_name": "PostToolUse",
                                           "tool_name": "Bash"})
            assert r3.returncode == 0, r3.stderr
            assert "mA" not in r3.stdout  # no re-spam of already-surfaced mail


# ---------------------------------------------------------------- boundary cursor: only what was shown
def _two_party(t):
    co = _co(t)
    co.attach("t", "cl", role="executor", worktree_realpath="/wt",
              host={"host": "claude", "session": "sc1", "native_id": "cl"})
    co.attach("t", "peer", role="supervisor", worktree_realpath="/wt",
              host={"host": "codex", "session": "peersess", "native_id": "peer"})
    return co


def _send(co, i, text=None):
    co.send(message_id=f"m{i:02d}", task_id="t", sender="peer", recipient="cl", kind="progress",
            task_revision=1, text=text if text is not None else f"mail {i}")


def test_boundary_inbox_cursor_lands_after_last_shown_message():
    with tempfile.TemporaryDirectory() as t:
        co = _two_party(t)
        for i in range(1, 18):  # 17 new messages, boundary shows at most 10
            _send(co, i)
        seen = []
        for _ in range(4):
            b = co.boundary_inbox("t", "cl", limit=10, max_bytes=4000)
            if not b["unread"]:
                break
            seqs = [m["seq"] for m in b["unread"]]
            assert b["next_after_seq"] == seqs[-1]  # cursor = last SHOWN seq, not the 100-item page end
            seen.extend(m["message_id"] for m in b["unread"])
            co.set_cursor("t", "cl", b["next_after_seq"])
        assert seen == [f"m{i:02d}" for i in range(1, 18)]  # nothing skipped, nothing repeated


def test_boundary_inbox_cursor_stops_at_byte_truncation():
    with tempfile.TemporaryDirectory() as t:
        co = _two_party(t)
        for i in range(1, 6):
            _send(co, i, text="x" * 1500)  # 4000-byte cap fits two, the third would exceed it
        b = co.boundary_inbox("t", "cl", limit=10, max_bytes=4000)
        assert [m["message_id"] for m in b["unread"]] == ["m01", "m02"]
        assert b["truncated_for_boundary"] is True
        assert b["next_after_seq"] == b["unread"][-1]["seq"]
        co.set_cursor("t", "cl", b["next_after_seq"])
        b2 = co.boundary_inbox("t", "cl", limit=10, max_bytes=4000)
        assert [m["message_id"] for m in b2["unread"]] == ["m03", "m04"]
        co.set_cursor("t", "cl", b2["next_after_seq"])
        b3 = co.boundary_inbox("t", "cl", limit=10, max_bytes=4000)
        assert [m["message_id"] for m in b3["unread"]] == ["m05"]
        assert b3["truncated_for_boundary"] is False


def test_boundary_inbox_nothing_unread_keeps_cursor():
    with tempfile.TemporaryDirectory() as t:
        co = _two_party(t)
        b = co.boundary_inbox("t", "cl", limit=10, max_bytes=4000)
        assert b["unread"] == [] and b["next_after_seq"] == b["after_seq"] == 0


def test_hook_post_tool_use_does_not_skip_unshown_mail():
    with tempfile.TemporaryDirectory() as t:
        co = _two_party(t)
        co.select_session("t", "cl", host_kind="claude", session_id="sc1")
        for i in range(1, 16):
            _send(co, i)
        ev = {"session_id": "sc1", "hook_event_name": "PostToolUse", "tool_name": "Bash"}
        r1 = _run_hook(t, "claude", "sc1", ev)
        assert r1.returncode == 0, r1.stderr
        assert "m10" in r1.stdout and "m11" not in r1.stdout
        r2 = _run_hook(t, "claude", "sc1", ev)
        assert "m11" in r2.stdout and "m15" in r2.stdout and "m10" not in r2.stdout
        r3 = _run_hook(t, "claude", "sc1", ev)
        assert "mail" not in r3.stdout and r3.stdout.strip() == ""


# ---------------------------------------------------------------- hook cost on a session with no selection
def _counting_env(store_root, counters, host="claude"):
    """Run-env whose `python3` (hook parsing + launcher) and the CLI python are counting shims."""
    shim = Path(counters) / "bin"
    shim.mkdir(parents=True, exist_ok=True)
    py3 = shim / "python3"
    py3.write_text('#!/bin/sh\necho x >> "%s/py3.count"\nexec "%s" "$@"\n' % (counters, sys.executable))
    py3.chmod(0o755)
    cli = shim / "coordpy"
    cli.write_text('#!/bin/sh\necho x >> "%s/cli.count"\nexec "%s" "$@"\n' % (counters, sys.executable))
    cli.chmod(0o755)
    env = dict(os.environ)
    env["MYCELIUM_COORD_DIR"] = str(store_root)
    env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
    env["MYCELIUM_COORD_PYTHON"] = str(cli)
    for k in ("MYCELIUM_COORD_TASK", "MYCELIUM_COORD_PARTICIPANT"):
        env.pop(k, None)
    if host == "claude":
        env["CLAUDE_PROJECT_DIR"] = "/tmp/claude-proj"
        env.pop("MYCELIUM_HOOK_HOST", None)
    else:
        env.pop("CLAUDE_PROJECT_DIR", None)
        env["MYCELIUM_HOOK_HOST"] = host
    return env


def _count(counters, name):
    p = Path(counters) / name
    return len(p.read_text().split()) if p.exists() else 0


def _hook(env, obj):
    return subprocess.run([str(HOOK)], input=json.dumps(obj), capture_output=True, text=True, env=env)


def test_hook_no_selection_is_one_python_and_never_starts_the_cli():
    ev = {"session_id": "nosel", "hook_event_name": "PostToolUse", "tool_name": "Bash"}
    for host in ("claude", "codex"):
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as c:
            r = _hook(_counting_env(t, c, host), ev)  # empty store: no selection can exist
            assert r.returncode == 0 and r.stdout == "" and r.stderr == "", (r.stdout, r.stderr)
            assert _count(c, "py3.count") <= 1
            assert _count(c, "cli.count") == 0
    # a store that has selections for OTHER sessions (and other hosts) still resolves nothing
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as c:
        co = _two_party(t)
        co.select_session("t", "cl", host_kind="claude", session_id="sc1")
        r = _hook(_counting_env(t, c, "claude"), ev)
        assert r.returncode == 0 and r.stdout == ""
        assert _count(c, "cli.count") == 1  # sessions exist for this host, so resolve-session really ran
        assert co.read_session_status("t", "cl")["present"] is False  # nothing published for "nosel"


def test_hook_with_selection_still_publishes_status_and_surfaces_mail():
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as c:
        co = _two_party(t)
        co.select_session("t", "cl", host_kind="claude", session_id="sc1")
        _send(co, 1)
        r = _hook(_counting_env(t, c, "claude"),
                  {"session_id": "sc1", "hook_event_name": "PostToolUse", "tool_name": "Bash"})
        assert r.returncode == 0, r.stderr
        assert "m01" in r.stdout and "mail 1" in r.stdout
        assert co.read_session_status("t", "cl")["status"]["runtime_state"] == "active"
        assert _count(c, "cli.count") >= 1


def test_hook_env_fallback_still_works_without_a_selection():
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as c:
        co = _two_party(t)  # participant attached as claude/sc1 but NO select_session
        _send(co, 1)
        env = _counting_env(t, c, "claude")
        env["MYCELIUM_COORD_TASK"] = "t"
        env["MYCELIUM_COORD_PARTICIPANT"] = "cl"
        ev = {"session_id": "sc1", "hook_event_name": "PostToolUse", "tool_name": "Bash"}
        r = _hook(env, ev)
        assert r.returncode == 0, r.stderr
        assert "m01" in r.stdout
        assert co.read_session_status("t", "cl")["status"]["runtime_state"] == "active"
        # an env hint for a DIFFERENT native session is still refused (verify-session)
        r2 = _hook(env, {**ev, "session_id": "intruder"})
        assert r2.returncode == 0 and r2.stdout == ""
