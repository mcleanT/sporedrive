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
BINDING_OK = {
    "binding_id": "b1",
    "role": "writer",
    "worktree_realpath": "/wt",
    "claude_session_id": "claude-ea7b12c1",
    "controller_id": "ctl",
}


def _mk(t, extra_obs=False):
    co = Coordinator(CoordStore(Path(t) / "coordination"))
    co.create_task("t1", project="p", worktree_realpath="/wt", revision=1)
    co.attach("t1", "c", role="supervisor", worktree_realpath="/out", host=HOST_C)
    co.attach("t1", "cl", role="executor", worktree_realpath="/wt", host=HOST_CL)
    if extra_obs:
        co.attach("t1", "obs", role="observer", worktree_realpath="/wt", host=HOST_OBS)
    co.send(
        message_id="m1",
        task_id="t1",
        sender="c",
        recipient="cl",
        kind="task",
        task_revision=1,
        text="do it",
        host=HOST_C,
    )
    return co


class _FakeBridgeError(Exception):
    def __init__(self, code, message="", **d):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = d


def _fake(status=None, err=None, spy=None, stage_hook=None):
    class FakeBridge:
        def submit(self, *a, **k):
            # model the bridge's own staging/waits: an optional hook fires HERE (a pause can land),
            # then the post-staging pre_enter_gate runs immediately before the real Enter/mutation.
            if stage_hook is not None:
                stage_hook()
            gate = k.get("pre_enter_gate")
            if gate is not None:
                gate()  # raises to withhold the Enter (nothing is sent)
            if spy is not None:
                spy["called"] = True  # the real cmux mutation (Enter) happened
            if err:
                raise _FakeBridgeError(err, "x")
            return {
                "status": status,
                "request_id": a[1],
                "delivered_text_sha256": "abc",
            }

    bridge_link._import_bridge = lambda: (FakeBridge, _FakeBridgeError)


def test_accepted_delivered_with_matching_binding():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        _fake(status="accepted")
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        r = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="m1",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
        )
        assert r["delivered"] and r["state"] == "delivered"
        assert co.message_state("t1", "cl", "m1") == "delivered"


def test_binding_worktree_mismatch_refused_before_send():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        spy = {"called": False}
        _fake(status="accepted", spy=spy)
        bad = dict(BINDING_OK)
        bad["worktree_realpath"] = "/somewhere/else"
        bridge_link._read_binding = lambda bid: bad
        try:
            bridge_link.notify_via_bridge(
                co,
                task_id="t1",
                message_id="m1",
                binding_id="b1",
                controller_id="ctl",
                expected_revision=0,
            )
            assert False
        except ProtocolError as e:
            assert e.code == "binding_identity_mismatch"
        assert spy["called"] is False, "must refuse BEFORE any send"
        assert co.message_state("t1", "cl", "m1") == "persisted"


def test_binding_session_mismatch_refused():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        spy = {"called": False}
        _fake(status="accepted", spy=spy)
        bad = dict(BINDING_OK)
        bad["claude_session_id"] = "claude-DIFFERENT"
        bridge_link._read_binding = lambda bid: bad
        try:
            bridge_link.notify_via_bridge(
                co,
                task_id="t1",
                message_id="m1",
                binding_id="b1",
                controller_id="ctl",
                expected_revision=0,
            )
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
        co.attach(
            "t1",
            "cl",
            role="executor",
            worktree_realpath="/wt",
            host={"host": "claude", "session": "e", "native_id": "pid-1"},
        )
        co.send(
            message_id="m1",
            task_id="t1",
            sender="c",
            recipient="cl",
            kind="task",
            task_revision=1,
            text="do it",
            host=HOST_C,
        )
        spy = {"called": False}
        _fake(status="accepted", spy=spy)
        bridge_link._read_binding = lambda bid: dict(
            BINDING_OK
        )  # full session ea7b12c1
        try:
            bridge_link.notify_via_bridge(
                co,
                task_id="t1",
                message_id="m1",
                binding_id="b1",
                controller_id="ctl",
                expected_revision=0,
            )
            assert False
        except ProtocolError as e:
            assert e.code == "binding_identity_mismatch"
        assert not spy["called"]


def test_controller_mismatch_refused():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        spy = {"called": False}
        _fake(status="accepted", spy=spy)
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        try:
            bridge_link.notify_via_bridge(
                co,
                task_id="t1",
                message_id="m1",
                binding_id="b1",
                controller_id="NOT-THE-WRITER",
                expected_revision=0,
            )
            assert False
        except ProtocolError as e:
            assert e.code == "controller_mismatch"
        assert not spy["called"]


def test_ambiguous_recipient_refused():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t, extra_obs=True)
        spy = {"called": False}
        _fake(status="accepted", spy=spy)
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        co.send(
            message_id="mall",
            task_id="t1",
            sender="c",
            recipient="all",
            kind="progress",
            task_revision=1,
            text="hey all",
            host=HOST_C,
        )
        try:
            bridge_link.notify_via_bridge(
                co,
                task_id="t1",
                message_id="mall",
                binding_id="b1",
                controller_id="ctl",
                expected_revision=0,
            )
            assert False
        except ProtocolError as e:
            assert e.code == "ambiguous_recipient"
        assert not spy["called"]


def test_busy_refusal_stays_pending():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        _fake(err="busy")
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        r = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="m1",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
        )
        assert not r["delivered"] and r["state"] == "pending" and r["refused"] == "busy"
        assert co.message_state("t1", "cl", "m1") == "persisted"


def test_bridge_unavailable():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        bridge_link._import_bridge = lambda: (None, None)
        r = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="m1",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
        )
        assert not r["delivered"] and r["state"] == "bridge_unavailable"
        assert co.store.exists("tasks/t1/correlations/m1.json")


def test_request_id_injective_and_namespaced():
    # per-task ids must not collide across tasks; delimiter concat must be injective
    assert bridge_link._bridge_request_id("a.b", "c") != bridge_link._bridge_request_id(
        "a", "b.c"
    )
    assert bridge_link._bridge_request_id("tA", "m1") != bridge_link._bridge_request_id(
        "tB", "m1"
    )
    # stable on retry
    assert bridge_link._bridge_request_id("t1", "m1") == bridge_link._bridge_request_id(
        "t1", "m1"
    )


if __name__ == "__main__":
    fns = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nbridge_link adapter: {len(fns)} tests PASS")


# ---- execution gate (PLAN section 3 / cases 7 & 12): recheck immediately before send ----
def _em(co):
    from mycelium_coord.execution import ExecutionManager

    em = ExecutionManager(co.store)
    em.open_execution("t1", execution_id="e1", scope_ref="/s", authorization_ref="/a")
    return em


def test_execution_gate_allows_open_reservation():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        _fake(status="accepted")
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        _em(co).reserve("t1", action_id="act1", kind="work_dispatch")
        r = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="m1",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
            execution_action_id="act1",
        )
        assert r["delivered"] and r["state"] == "delivered"


def test_execution_gate_refuses_paused_before_send():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        spy = {"called": False}
        _fake(status="accepted", spy=spy)
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        em = _em(co)
        em.reserve("t1", action_id="act1", kind="work_dispatch")
        em.pause("t1", authorization_ref="/a")  # a newer pause after the reservation
        r = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="m1",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
            execution_action_id="act1",
        )
        assert r["state"] == "refused_by_execution_gate"
        assert r["reason"] == "execution_paused"
        assert spy["called"] is False  # nothing was dispatched
        assert co.message_state("t1", "cl", "m1") != "delivered"


def test_execution_gate_refuses_stale_settled_reservation():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        spy = {"called": False}
        _fake(status="accepted", spy=spy)
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        em = _em(co)
        em.reserve("t1", action_id="act1", kind="work_dispatch")
        em.settle(
            "t1", action_id="act1", outcome="success"
        )  # already reconciled -> stale for dispatch
        r = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="m1",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
            execution_action_id="act1",
        )
        assert r["state"] == "refused_by_execution_gate"
        assert r["reason"] == "reservation_not_open"
        assert spy["called"] is False


def test_execution_gate_refuses_expired_before_send():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        spy = {"called": False}
        _fake(status="accepted", spy=spy)
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        from mycelium_coord.execution import ExecutionManager

        em = ExecutionManager(co.store)
        em.open_execution(
            "t1",
            execution_id="e1",
            scope_ref="/s",
            authorization_ref="/a",
            expires_at="2000-01-01T00:00:00+00:00",
        )
        # cannot reserve on an expired execution; the gate must still refuse the dispatch outright
        r = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="m1",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
            execution_action_id="act1",
        )
        assert r["state"] == "refused_by_execution_gate"
        assert r["reason"] == "execution_expired"
        assert spy["called"] is False


def test_unmanaged_task_notify_passes_without_reservation():
    """A task with NO execution record is UNMANAGED: managed-ness is the durable presence of the
    record, so an unmanaged notify passes without a reservation (legacy/manual compatibility)."""
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        _fake(status="accepted")
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        r = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="m1",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
        )
        assert r["delivered"] and r["state"] == "delivered"


# ---- review R1: a MANAGED work dispatch requires a reservation BOUND to one concrete dispatch ----
def test_managed_notify_without_reservation_refused():
    """Once an execution record exists the task is managed; a work dispatch (actionable kind) with NO
    reservation must fail closed — omitting the field never makes a managed task unmanaged."""
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        spy = {"called": False}
        _fake(status="accepted", spy=spy)
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        _em(co)  # opens the execution -> t1 is now managed
        r = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="m1",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
        )
        assert r["state"] == "refused_by_execution_gate"
        assert r["reason"] == "managed_dispatch_requires_reservation"
        assert spy["called"] is False
        assert co.message_state("t1", "cl", "m1") != "delivered"


def test_one_reservation_funds_one_dispatch_only():
    """A reservation binds to the message that CLAIMS it at publication (coord.send), and a different
    message cannot be funded by the same reservation at notify; a replay of the SAME message
    reconciles without a new charge (review R1 reservation_reuse probe). The binding now originates at
    send, so notify sees an already-bound reservation and only reconciles it."""
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        _fake(status="accepted")
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        em = _em(co)
        em.reserve("t1", action_id="a1", kind="work_dispatch")
        # publishing the actionable message under the reservation BINDS a1 -> m2 atomically (send).
        co.send(
            message_id="m2",
            task_id="t1",
            sender="c",
            recipient="cl",
            kind="task",
            task_revision=1,
            text="the work this reservation funds",
            host=HOST_C,
            execution_action_id="a1",
        )
        # notifying the bound message delivers (the claim is the idempotent same-identity reconcile).
        r_bound = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="m2",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
            execution_action_id="a1",
        )
        # a DIFFERENT message (m1, which never claimed a1) cannot be funded by a1 -> refused.
        r_other = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="m1",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
            execution_action_id="a1",
        )
        assert r_bound["delivered"] is True
        assert (
            r_other["delivered"] is False
            and r_other["reason"] == "dispatch_binding_mismatch"
        )
        # exactly one dispatch was charged (at reserve/claim), never two
        assert em.status("t1")["usage"]["work_dispatches"] == 1
        # replaying the SAME bound dispatch reconciles (idempotent), still delivered, still one charge
        r_bound_again = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="m2",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
            execution_action_id="a1",
        )
        assert r_bound_again["delivered"] is True
        assert em.status("t1")["usage"]["work_dispatches"] == 1


def test_pause_during_bridge_staging_withholds_the_enter():
    """Transport-level regression (review R1 amendment): a pause that lands DURING the bridge's own
    staging/waits — after bridge_link's early-out passed and submit was entered — must still withhold
    the real Enter. The bridge invokes the caller's pre_enter_gate immediately before the Enter, so
    the gate (a fresh execution recheck) refuses and NOTHING is sent."""
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        spy = {"called": False}
        em = _em(co)
        em.reserve("t1", action_id="a1", kind="work_dispatch")
        # the pause fires INSIDE submit's staging, before the post-staging pre_enter_gate runs
        _fake(
            status="accepted",
            spy=spy,
            stage_hook=lambda: em.pause("t1", authorization_ref="/a"),
        )
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        r = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="m1",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
            execution_action_id="a1",
        )
        assert r["state"] == "refused_by_execution_gate"
        assert r["reason"] == "execution_paused"
        assert spy["called"] is False  # the real Enter/mutation never happened
        assert co.message_state("t1", "cl", "m1") != "delivered"


def test_pre_enter_gate_passes_when_still_dispatchable():
    """Control: with no pause, the same post-staging gate passes and the Enter proceeds — the gate
    withholds only when the execution actually became non-dispatchable during staging."""
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        spy = {"called": False}
        em = _em(co)
        em.reserve("t1", action_id="a1", kind="work_dispatch")
        _fake(
            status="accepted", spy=spy
        )  # no stage_hook: nothing changes during staging
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        r = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="m1",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
            execution_action_id="a1",
        )
        assert r["delivered"] is True and spy["called"] is True


def test_coord_send_managed_gate():
    """coord.send is gated too (review R1): a NEW actionable work request on a managed task needs a
    valid reservation; a non-actionable record and an unmanaged task are unaffected."""
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        em = _em(co)  # t1 now managed
        # actionable work request without a reservation is refused at send
        try:
            co.send(
                message_id="w1",
                task_id="t1",
                sender="c",
                recipient="cl",
                kind="task",
                task_revision=1,
                text="do work",
                host=HOST_C,
            )
            assert False, "expected refusal"
        except ProtocolError as e:
            assert e.code == "managed_send_requires_reservation"
        # a non-actionable record (progress) passes without a reservation
        co.send(
            message_id="p1",
            task_id="t1",
            sender="c",
            recipient="cl",
            kind="progress",
            task_revision=1,
            text="status",
            host=HOST_C,
        )
        assert co.get_message("t1", "p1") is not None
        # with a valid open reservation the work request is accepted
        em.reserve("t1", action_id="a1", kind="work_dispatch")
        co.send(
            message_id="w2",
            task_id="t1",
            sender="c",
            recipient="cl",
            kind="task",
            task_revision=1,
            text="do work",
            host=HOST_C,
            execution_action_id="a1",
        )
        assert co.get_message("t1", "w2") is not None
