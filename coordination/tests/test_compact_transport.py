"""Transport regressions: bounded evidence, exact retrieval and terminal waits, without model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import Coordinator, ProtocolError
from mycelium_coord.execution import ExecutionManager
from mycelium_coord.store import CoordStore


@pytest.fixture
def co(tmp_path):
    c = Coordinator(CoordStore(tmp_path / "store"))
    c.create_task("t", project="p", worktree_realpath="/project", revision=1)
    for pid, role, host in (("c", "supervisor", "codex"), ("cl", "executor", "claude"),
                            ("o", "observer", "codex")):
        c.attach("t", pid, role=role, worktree_realpath="/project" if pid == "cl" else "/outside",
                 host={"host": host, "session": pid, "native_id": pid})
    return c


def send(co, message_id, text="x" * 7900):
    return co.send(task_id="t", message_id=message_id, sender="c", recipient="cl",
                   kind="progress", task_revision=1, text=text)["message"]


def checkpoint(co):
    co.publish_checkpoint("t", revision=1, participant_id="cl",
                          checkpoint={"next_action": "DO_OLD_WORK", "history": "z" * 100_000})


def test_compact_page_retains_cursor_full_body_and_ack_separation(co):
    originals = [send(co, f"m{i}") for i in range(12)]
    first = co.inbox("t", "cl", limit=10, compact=True)
    second = co.inbox("t", "cl", after_seq=first["next_after_seq"], compact=True)
    assert [m["message_id"] for m in first["messages"] + second["messages"]] == [f"m{i}" for i in range(12)]
    assert first["truncated"] and first["next_after_seq"] == 10
    assert "text" not in first["messages"][0] and first["messages"][0]["summary_only"]
    assert co.read_message("t", "cl", "m0") == originals[0]
    assert first["messages"][0]["content_hash"] == originals[0]["content_hash"]
    assert co.get_cursor("t", "cl") == 0
    assert co.message_state("t", "cl", "m0") == "persisted"
    with pytest.raises(ProtocolError):
        co.read_message("t", "o", "m0")


def test_compact_resume_has_bounded_context_and_selective_checkpoint(co):
    checkpoint(co)
    for i in range(12):
        send(co, f"m{i}")
    full = co.resume("t", "cl")
    small = co.resume("t", "cl", limit=10, compact=True)
    assert len(json.dumps(small).encode()) < 7000
    assert len(json.dumps(small)) < len(json.dumps(full)) / 10
    assert small["pending_count"] == 12 and small["pending_truncated"]
    repeat = co.resume("t", "cl", compact=True, after_checkpoint_revision=1)
    assert repeat["checkpoint"]["unchanged"]
    assert "checkpoint" not in repeat["checkpoint"]
    assert co.read_checkpoint("t")["checkpoint"]["history"] == "z" * 100_000


@pytest.mark.parametrize("state", ["paused", "draining", "completed", "closed", "exhausted", "expired"])
def test_terminal_wait_returns_without_sleep_or_old_messages(co, monkeypatch, state):
    send(co, "old")
    status = {"status": state if state != "expired" else "active", "expired": state == "expired"}
    monkeypatch.setattr(ExecutionManager, "status", lambda self, task: status)
    monkeypatch.setattr("mycelium_coord.coord.time.sleep", lambda _: pytest.fail("terminal task waited"))
    result = co.wait("t", "cl", timeout_s=600, compact=True)
    assert result["stop_waiting"] and not result["timed_out"]
    assert result["messages"] == [] and result["next_after_seq"] == 0
    with pytest.raises(ProtocolError):
        co.wait("t", "missing", timeout_s=600)


def test_pause_during_wait_is_observed_without_new_mail(co, monkeypatch):
    manager = ExecutionManager(co.store)
    manager.open_execution("t", execution_id="e", scope_ref="/scope", authorization_ref="/owner")
    sleeps = []
    def pause(_):
        sleeps.append(1)
        manager.pause("t", authorization_ref="/owner", reason="user pause")
    monkeypatch.setattr("mycelium_coord.coord.time.sleep", pause)
    result = co.wait("t", "cl", timeout_s=10)
    assert result["reason"] == "task_paused" and len(sleeps) == 1


def test_cli_and_session_start_suppress_stale_checkpoint_when_paused(co):
    checkpoint(co)
    send(co, "m", "A complete brief that remains retrievable")
    manager = ExecutionManager(co.store)
    manager.open_execution("t", execution_id="e", scope_ref="/scope", authorization_ref="/owner")
    manager.pause("t", authorization_ref="/owner", reason="user pause")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "PYTHONDONTWRITEBYTECODE": "1"}
    args = [sys.executable, "-m", "mycelium_coord", "--root", str(co.store.root)]
    small = subprocess.run(args + ["resume", "t", "cl"], env=env, text=True, capture_output=True, check=True)
    state = json.loads(small.stdout)
    assert state["stop_waiting"] and state["execution"]["status"] == "paused"
    assert state["pending_unacked"][0]["summary_only"]
    full = subprocess.run(args + ["read-message", "t", "cl", "m"], env=env, text=True, capture_output=True, check=True)
    assert json.loads(full.stdout)["text"] == "A complete brief that remains retrievable"
    hook = Path(__file__).resolve().parents[1] / "hooks/_coord_context.py"
    rendered = subprocess.run([sys.executable, str(hook), "t", "cl"], input=small.stdout,
                              env=env, text=True, capture_output=True, check=True)
    context = json.loads(rendered.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "STOP:" in context and "DO_OLD_WORK" not in context
    # Full/legacy-format packets get the same current-state precedence.
    legacy = co.resume("t", "cl", compact=False)
    rendered = subprocess.run([sys.executable, str(hook), "t", "cl"], input=json.dumps(legacy),
                              env=env, text=True, capture_output=True, check=True)
    assert "DO_OLD_WORK" not in rendered.stdout


def test_timeout_is_unchanged_and_unicode_preview_is_not_full_evidence(co):
    result = co.wait("t", "cl", timeout_s=0, compact=True)
    assert result["unchanged"] and result["timed_out"] and not result["stop_waiting"]
    send(co, "unicode", "😀" * 2000)
    short = co.inbox("t", "cl", compact=True)["messages"][0]
    assert len(short["preview"].encode()) <= 243
    assert co.read_message("t", "cl", "unicode")["text"] == "😀" * 2000


def test_actual_mcp_entrypoints_default_to_compact_and_fetch_exact_body(co, monkeypatch):
    import asyncio
    from fastmcp import Client
    from mycelium_coord import mcp_server

    original = send(co, "m")
    checkpoint(co)
    monkeypatch.setattr(mcp_server, "_co", lambda: co)

    async def exercise():
        async with Client(mcp_server.mcp) as client:
            inbox = (await client.call_tool("coord_inbox", {"task_id": "t", "participant_id": "cl"})).data
            assert inbox["messages"][0]["summary_only"]
            full = (await client.call_tool("coord_read_message", {
                "task_id": "t", "participant_id": "cl", "message_id": "m"})).data
            assert full == original
            snapshot = (await client.call_tool("coord_resume", {
                "task_id": "t", "participant_id": "cl", "after_checkpoint_revision": 1})).data
            assert snapshot["checkpoint"]["unchanged"]
            wait = (await client.call_tool("coord_wait", {
                "task_id": "t", "participant_id": "cl", "timeout_s": 0})).data
            assert wait["messages"][0]["summary_only"]

    asyncio.run(exercise())
