#!/usr/bin/env python3
"""Bridge-link adapter regression: delivery is recorded ONLY on a bridge accepted outcome; the
binding's native identity (worktree/session/controller/surface) must match the addressed recipient
BEFORE any send; request ids are namespaced by task AND message and are injective. Uses a FAKE bridge
and a stubbed binding read — no real cmux contact.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import Coordinator, ProtocolError  # noqa: E402
from mycelium_coord import bridge_link  # noqa: E402
from mycelium_coord.store import CoordStore  # noqa: E402

HOST_C = {"host": "codex", "session": "c1", "native_id": "t-01"}
HOST_CL = {"host": "claude", "session": "ea7b12c1", "native_id": "pid-1"}
HOST_OBS = {"host": "claude", "session": "obs9", "native_id": "pid-2"}
BINDING_OK = {"binding_id": "b1", "role": "writer", "worktree_realpath": "/wt",
              "claude_session_id": "claude-ea7b12c1", "controller_id": "ctl"}


def _mk(t, extra_obs=False):
    co = Coordinator(CoordStore(Path(t) / "coordination"))
    co.create_task("t1", project="p", worktree_realpath="/wt", revision=1)
    co.attach("t1", "c", role="supervisor", worktree_realpath="/out", host=HOST_C)
    co.attach("t1", "cl", role="executor", worktree_realpath="/wt", host=HOST_CL)
    if extra_obs:
        co.attach("t1", "obs", role="observer", worktree_realpath="/wt", host=HOST_OBS)
    co.send(message_id="m1", task_id="t1", sender="c", recipient="cl",
            kind="task", task_revision=1, text="do it", host=HOST_C)
    return co


class _FakeBridgeError(Exception):
    def __init__(self, code, message="", **d):
        super().__init__(f"{code}: {message}"); self.code = code; self.message = message; self.detail = d


def _fake(status=None, err=None, spy=None):
    class FakeBridge:
        def submit(self, *a, **k):
            if spy is not None:
                spy["called"] = True
            if err:
                raise _FakeBridgeError(err, "x")
            return {"status": status, "request_id": a[1], "delivered_text_sha256": "abc"}
    bridge_link._import_bridge = lambda: (FakeBridge, _FakeBridgeError)


def test_accepted_delivered_with_matching_binding():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t); _fake(status="accepted"); bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        r = bridge_link.notify_via_bridge(co, task_id="t1", message_id="m1", binding_id="b1",
                                          controller_id="ctl", expected_revision=0)
        assert r["delivered"] and r["state"] == "delivered"
        assert co.message_state("t1", "cl", "m1") == "delivered"


def test_binding_worktree_mismatch_refused_before_send():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t); spy = {"called": False}; _fake(status="accepted", spy=spy)
        bad = dict(BINDING_OK); bad["worktree_realpath"] = "/somewhere/else"
        bridge_link._read_binding = lambda bid: bad
        try:
            bridge_link.notify_via_bridge(co, task_id="t1", message_id="m1", binding_id="b1",
                                          controller_id="ctl", expected_revision=0)
            assert False
        except ProtocolError as e:
            assert e.code == "binding_identity_mismatch"
        assert spy["called"] is False, "must refuse BEFORE any send"
        assert co.message_state("t1", "cl", "m1") == "persisted"


def test_binding_session_mismatch_refused():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t); spy = {"called": False}; _fake(status="accepted", spy=spy)
        bad = dict(BINDING_OK); bad["claude_session_id"] = "claude-DIFFERENT"
        bridge_link._read_binding = lambda bid: bad
        try:
            bridge_link.notify_via_bridge(co, task_id="t1", message_id="m1", binding_id="b1",
                                          controller_id="ctl", expected_revision=0)
            assert False
        except ProtocolError as e:
            assert e.code == "binding_identity_mismatch"
        assert not spy["called"]


def test_short_session_is_not_authority():
    # a one-char abbreviated session must NOT satisfy identity even if it "prefixes" the real one
    with tempfile.TemporaryDirectory() as t:
        co = Coordinator(CoordStore(Path(t) / "coordination"))
        co.create_task("t1", project="p", worktree_realpath="/wt", revision=1)
        co.attach("t1", "c", role="supervisor", worktree_realpath="/out", host=HOST_C)
        co.attach("t1", "cl", role="executor", worktree_realpath="/wt",
                  host={"host": "claude", "session": "e", "native_id": "pid-1"})
        co.send(message_id="m1", task_id="t1", sender="c", recipient="cl",
                kind="task", task_revision=1, text="do it", host=HOST_C)
        spy = {"called": False}; _fake(status="accepted", spy=spy)
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)  # full session ea7b12c1
        try:
            bridge_link.notify_via_bridge(co, task_id="t1", message_id="m1", binding_id="b1",
                                          controller_id="ctl", expected_revision=0)
            assert False
        except ProtocolError as e:
            assert e.code == "binding_identity_mismatch"
        assert not spy["called"]


def test_controller_mismatch_refused():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t); spy = {"called": False}; _fake(status="accepted", spy=spy)
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        try:
            bridge_link.notify_via_bridge(co, task_id="t1", message_id="m1", binding_id="b1",
                                          controller_id="NOT-THE-WRITER", expected_revision=0)
            assert False
        except ProtocolError as e:
            assert e.code == "controller_mismatch"
        assert not spy["called"]


def test_ambiguous_recipient_refused():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t, extra_obs=True); spy = {"called": False}; _fake(status="accepted", spy=spy)
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        co.send(message_id="mall", task_id="t1", sender="c", recipient="all",
                kind="progress", task_revision=1, text="hey all", host=HOST_C)
        try:
            bridge_link.notify_via_bridge(co, task_id="t1", message_id="mall", binding_id="b1",
                                          controller_id="ctl", expected_revision=0)
            assert False
        except ProtocolError as e:
            assert e.code == "ambiguous_recipient"
        assert not spy["called"]


def test_busy_refusal_stays_pending():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t); _fake(err="busy"); bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        r = bridge_link.notify_via_bridge(co, task_id="t1", message_id="m1", binding_id="b1",
                                          controller_id="ctl", expected_revision=0)
        assert not r["delivered"] and r["state"] == "pending" and r["refused"] == "busy"
        assert co.message_state("t1", "cl", "m1") == "persisted"


def test_bridge_unavailable():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t); bridge_link._import_bridge = lambda: (None, None)
        r = bridge_link.notify_via_bridge(co, task_id="t1", message_id="m1", binding_id="b1",
                                          controller_id="ctl", expected_revision=0)
        assert not r["delivered"] and r["state"] == "bridge_unavailable"
        assert co.store.exists("tasks/t1/correlations/m1.json")


def test_request_id_injective_and_namespaced():
    # per-task ids must not collide across tasks; delimiter concat must be injective
    assert bridge_link._bridge_request_id("a.b", "c") != bridge_link._bridge_request_id("a", "b.c")
    assert bridge_link._bridge_request_id("tA", "m1") != bridge_link._bridge_request_id("tB", "m1")
    # stable on retry
    assert bridge_link._bridge_request_id("t1", "m1") == bridge_link._bridge_request_id("t1", "m1")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\nbridge_link adapter: {len(fns)} tests PASS")


# ---- execution gate (PLAN section 3 / cases 7 & 12): recheck immediately before send ----
def _em(co):
    from mycelium_coord.execution import ExecutionManager
    em = ExecutionManager(co.store)
    em.open_execution("t1", execution_id="e1", scope_ref="/s", authorization_ref="/a")
    return em


def test_execution_gate_allows_open_reservation():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t); _fake(status="accepted")
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        _em(co).reserve("t1", action_id="act1", kind="work_dispatch")
        r = bridge_link.notify_via_bridge(co, task_id="t1", message_id="m1", binding_id="b1",
                                          controller_id="ctl", expected_revision=0,
                                          execution_action_id="act1")
        assert r["delivered"] and r["state"] == "delivered"


def test_execution_gate_refuses_paused_before_send():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t); spy = {"called": False}; _fake(status="accepted", spy=spy)
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        em = _em(co)
        em.reserve("t1", action_id="act1", kind="work_dispatch")
        em.pause("t1", authorization_ref="/a")   # a newer pause after the reservation
        r = bridge_link.notify_via_bridge(co, task_id="t1", message_id="m1", binding_id="b1",
                                          controller_id="ctl", expected_revision=0,
                                          execution_action_id="act1")
        assert r["state"] == "refused_by_execution_gate"
        assert r["reason"] == "execution_paused"
        assert spy["called"] is False                      # nothing was dispatched
        assert co.message_state("t1", "cl", "m1") != "delivered"


def test_execution_gate_refuses_stale_settled_reservation():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t); spy = {"called": False}; _fake(status="accepted", spy=spy)
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        em = _em(co)
        em.reserve("t1", action_id="act1", kind="work_dispatch")
        em.settle("t1", action_id="act1", outcome="success")  # already reconciled -> stale for dispatch
        r = bridge_link.notify_via_bridge(co, task_id="t1", message_id="m1", binding_id="b1",
                                          controller_id="ctl", expected_revision=0,
                                          execution_action_id="act1")
        assert r["state"] == "refused_by_execution_gate"
        assert r["reason"] == "reservation_not_open"
        assert spy["called"] is False


def test_execution_gate_refuses_expired_before_send():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t); spy = {"called": False}; _fake(status="accepted", spy=spy)
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        from mycelium_coord.execution import ExecutionManager
        em = ExecutionManager(co.store)
        em.open_execution("t1", execution_id="e1", scope_ref="/s", authorization_ref="/a",
                          expires_at="2000-01-01T00:00:00+00:00")
        # cannot reserve on an expired execution; the gate must still refuse the dispatch outright
        r = bridge_link.notify_via_bridge(co, task_id="t1", message_id="m1", binding_id="b1",
                                          controller_id="ctl", expected_revision=0,
                                          execution_action_id="act1")
        assert r["state"] == "refused_by_execution_gate"
        assert r["reason"] == "execution_expired"
        assert spy["called"] is False


def test_unmanaged_notify_ignores_execution_gate():
    """A notify WITHOUT an execution_action_id is unmanaged transport and is unaffected by the gate
    (message delivery state stays separate from execution permission)."""
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t); _fake(status="accepted")
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        r = bridge_link.notify_via_bridge(co, task_id="t1", message_id="m1", binding_id="b1",
                                          controller_id="ctl", expected_revision=0)
        assert r["delivered"] and r["state"] == "delivered"
