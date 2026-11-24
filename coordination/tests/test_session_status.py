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
