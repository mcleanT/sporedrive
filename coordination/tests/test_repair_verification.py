#!/usr/bin/env python3
"""Repair-verification regressions (review R1/R4): the four exact seams the single repair
verification found in the first-pass R1/R4 work, driven through the ACTUAL CLI-equivalent core
paths (coord.send / ExecutionManager.claim_dispatch / bridge_link.notify_via_bridge) with a fake
bridge and disposable state — no real cmux contact.

1. native coord.send CLAIMS+binds a reservation at first managed actionable publication (one action
   cannot fund two messages by avoiding terminal notification);
2. reservation PURPOSES are not interchangeable (a technical-call reservation cannot fund a work
   dispatch) and managed messages carry an explicit actionable disposition;
3. CLOSURE is repair-only for the DISPATCH/CLAIM predicate too (a generic work reservation made
   before closure is refused after it);
4. a completed/closed NON-actionable notice does not wake an idle executor (bridge not pressed).
"""

from __future__ import annotations

import tempfile

import pytest
from test_bridge_link import (  # sibling fixture module (same tests dir)
    BINDING_OK,
    HOST_C,
    _fake,
    _mk,
    bridge_link,
)

from mycelium_coord import ProtocolError
from mycelium_coord.execution import ExecutionManager


def _setup(t, **kw):
    """A managed task with one required acceptance criterion, mirroring the verifier harness."""
    co = _mk(t)
    em = ExecutionManager(co.store)
    em.open_execution(
        "t1",
        execution_id="e1",
        scope_ref="scope",
        authorization_ref="user",
        acceptance_manifest={"c1": {"description": "required check"}},
        **kw,
    )
    return co, em


def _send(co, mid, aid=None, kind="task", actionable=None):
    return co.send(
        message_id=mid,
        task_id="t1",
        sender="c",
        recipient="cl",
        kind=kind,
        task_revision=1,
        text="fixture",
        host=HOST_C,
        execution_action_id=aid,
        actionable=actionable,
    )


def _close(em):
    em.record_evidence(
        "t1",
        criterion_id="c1",
        evidence_ref="external-ref",
        attestation="verified fixture",
        accepted_by="tester",
    )


# 1 -----------------------------------------------------------------------------------------------
def test_reservation_reuse_via_send_is_refused():
    """coord.send binds the reservation to the FIRST message that names it (inside the held task
    lock); a SECOND, different message naming the same action is refused at send and is NOT
    persisted, and the reservation is charged exactly once. Native inbox-read dispatch is therefore
    bounded even though notify_via_bridge is never called (review R1)."""
    with tempfile.TemporaryDirectory() as t:
        co, em = _setup(t)
        em.reserve("t1", action_id="a1", kind="work_dispatch")
        a = _send(co, "m2", "a1")
        # m2 binds a1 and persists with explicit managed identity on the message body
        assert a["message"]["message_id"] == "m2"
        assert a["message"]["actionable"] is True
        assert a["message"]["execution_action_id"] == "a1"
        # a different message trying to spend the SAME reservation is refused at publication
        with pytest.raises(ProtocolError) as ei:
            _send(co, "m3", "a1")
        assert ei.value.code == "refused_by_execution_gate"
        # m3 is NOT persisted; exactly one dispatch charged; a1 is bound to m2
        assert co.get_message("t1", "m3") is None
        rec = em.read_execution("t1")
        assert rec["usage"]["work_dispatches"] == 1
        assert rec["usage"]["reservations"]["a1"]["dispatch_binding"] == "m2"
        # an idempotent replay of the SAME message does NOT re-claim or re-charge
        again = _send(co, "m2", "a1")
        assert again["idempotent"] is True
        assert em.read_execution("t1")["usage"]["work_dispatches"] == 1


# 2 -----------------------------------------------------------------------------------------------
def test_call_reservation_cannot_fund_work_dispatch():
    """A technical-call reservation (reserve_call, kind='call') and a reviewer launch are DISTINCT
    purposes: neither can fund a work dispatch merely by naming its action id. send refuses, the
    direct claim refuses with reservation_purpose_mismatch, and the work-dispatch allowance is never
    charged (review R1 reservation-purpose interchange)."""
    with tempfile.TemporaryDirectory() as t:
        co, em = _setup(t, limits_overrides={"technical_calls": 1})
        em.reserve_call(
            "t1",
            action_id="call1",
            phase="technical_debug",
            freeze_identity="actual-fixture",
        )
        with pytest.raises(ProtocolError) as ei:
            _send(co, "m2", "call1")
        assert ei.value.code == "refused_by_execution_gate"
        assert co.get_message("t1", "m2") is None
        claim = em.claim_dispatch("t1", action_id="call1", dispatch_identity="m2")
        assert claim["ok"] is False
        assert claim["reason"] == "reservation_purpose_mismatch"
        assert claim["reservation_kind"] == "call"
        # the shared work-dispatch allowance stayed at zero
        assert em.read_execution("t1")["usage"]["work_dispatches"] == 0


# 3 -----------------------------------------------------------------------------------------------
def test_pre_reserved_generic_work_refused_after_closure():
    """Closure is repair-only for the DISPATCH predicate, not only at reserve time: a generic
    work_dispatch reserved while ACTIVE must not still fire once coverage is complete (closed).
    send and the direct claim both refuse with execution_closed (review R4 stale-queue)."""
    with tempfile.TemporaryDirectory() as t:
        co, em = _setup(t)
        em.reserve("t1", action_id="a1", kind="work_dispatch")  # reserved while active
        _close(em)  # last criterion accepted -> closed
        assert em.read_execution("t1")["status"] == "closed"
        with pytest.raises(ProtocolError) as ei:
            _send(co, "m2", "a1")
        assert ei.value.code == "refused_by_execution_gate"
        assert co.get_message("t1", "m2") is None
        claim = em.claim_dispatch("t1", action_id="a1", dispatch_identity="m2")
        assert claim["ok"] is False
        assert claim["reason"] == "execution_closed"


def test_evidenced_repair_still_dispatches_during_closure():
    """Positive control for the closure gate: a bounded REPAIR linked to a still-open, evidenced
    blocker IS allowed to dispatch during closure — the gate is repair-only, not repair-never."""
    with tempfile.TemporaryDirectory() as t:
        co, em = _setup(t)
        _close(em)  # coverage complete -> closed
        # a blocker invalidates the criterion and keeps the execution closed (repair-only)
        em.open_blocker(
            "t1", blocker_id="b-1", criterion_id="c1", evidence_ref="repro://x"
        )
        em.reserve("t1", action_id="r1", kind="repair", repair_blocker_id="b-1")
        claim = em.claim_dispatch("t1", action_id="r1", dispatch_identity="mr")
        assert claim["ok"] is True


# 4 -----------------------------------------------------------------------------------------------
def test_completed_nonactionable_notice_does_not_wake_executor():
    """A non-actionable notice (a late progress report) on a COMPLETED managed execution is NOT
    bridge-delivered: the bridge is never pressed, delivered is False, and the message keeps its
    explicit actionable=false metadata in the durable inbox for the executor to read on its own
    (review R1/R4 no-wake)."""
    with tempfile.TemporaryDirectory() as t:
        co, em = _setup(t)
        _close(em)
        em.record_completion(
            "t1",
            completion_ref="final-receipt",
            accepted_by="tester",
            attestation="done",
        )
        a = _send(co, "late-progress", kind="progress")
        assert a["message"]["actionable"] is False
        spy = {}
        _fake(status="accepted", spy=spy)
        bridge_link._read_binding = lambda bid: dict(BINDING_OK)
        r = bridge_link.notify_via_bridge(
            co,
            task_id="t1",
            message_id="late-progress",
            binding_id="b1",
            controller_id="ctl",
            expected_revision=0,
        )
        assert spy.get("called") is None  # the real cmux Enter/mutation never happened
        assert r["delivered"] is False
        assert r["state"] == "inbox_only_non_actionable"
        # the notice is preserved in the durable inbox (safe read still works)
        assert co.get_message("t1", "late-progress") is not None
