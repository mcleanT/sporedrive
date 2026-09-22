#!/usr/bin/env python3
"""Codex desktop idle-wake ORCHESTRATOR — offline tests (criterion D product integration, R2/R4/R6).

The entry point ties an ADDRESSED coordination message to the guarded IPC seam: it resolves one codex
recipient, applies the SAME managed-execution gate as notify_via_bridge (pause/expiry/closure), runs
read-only existing-owner + app-build guards, carries the short sd: handle as correlation, and — by
default un-armed — PREPARES the wake without firing. These tests use a fake adapter (no socket, no
app.asar read) and assert: the default prepared_not_armed plan, owner/build guard refusals, the
managed gate refusals, the no-active-root-wake guard, and the retained unknown-send identity.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import Coordinator  # noqa: E402
from mycelium_coord import codex_wake_link as L  # noqa: E402
from mycelium_coord.codex_ipc_wake import WakeError  # noqa: E402
from mycelium_coord.store import CoordStore  # noqa: E402

HOST_CODEX = {"host": "codex", "session": "codex-thread-1", "native_id": "t-01"}
HOST_CLAUDE = {"host": "claude", "session": "cl-1", "native_id": "pid-1"}
ROOT = next(iter(L.PROTECTED_CONVERSATIONS))


def _mk(t, *, kind="checkpoint_ready", recipient="c"):
    co = Coordinator(CoordStore(Path(t) / "coordination"))
    co.create_task("t1", project="p", worktree_realpath="/wt", revision=1)
    co.attach("t1", "c", role="supervisor", worktree_realpath="/out", host=HOST_CODEX)
    co.attach("t1", "cl", role="executor", worktree_realpath="/wt", host=HOST_CLAUDE)
    co.send(
        message_id="m1",
        task_id="t1",
        sender="cl",
        recipient=recipient,
        kind=kind,
        task_revision=1,
        text="review-ready; please read boundary-inbox",
        host=HOST_CLAUDE,
    )
    return co


class FakeAdapter:
    """Duck-typed CodexIpcWakeAdapter: no socket, no file. All I/O outcomes are injected so the
    orchestrator's own branching is what is under test."""

    def __init__(
        self,
        *,
        build_verified=True,
        probe_status="owner_discovered",
        owner="owner-client-9",
        start_result="not_armed",
    ):
        self._build_verified = build_verified
        self._probe_status = probe_status
        self._owner = owner
        self._start_result = start_result
        self.calls = []

    def verify_app_build(self):
        return {
            "verified": self._build_verified,
            "actual_sha256": "x" if self._build_verified else None,
        }

    def capability_probe(self, conversation_id, *, timeout_s=12.0):
        self.calls.append(("probe", conversation_id))
        return {
            "status": self._probe_status,
            "owner_client_id": self._owner
            if self._probe_status == "owner_discovered"
            else None,
            "app_build": self.verify_app_build(),
            "elapsed_seconds": 0.01,
        }

    def build_start_turn_frame(self, conversation_id, turn_start, owner_client_id):
        return {
            "requestId": "sporedrive-wake-req-1",
            "method": "thread-follower-start-turn",
            "targetClientId": owner_client_id,
            "params": {"conversationId": conversation_id, "turnStart": turn_start},
        }

    def start_turn(
        self, conversation_id, turn_start, owner_client_id, *, armed=False, dry_run=True
    ):
        self.calls.append(("start_turn", armed, dry_run))
        if self._start_result == "not_armed":
            raise WakeError("wake_not_armed", "guarded seam")
        if self._start_result == "dry_run":
            return {
                "sent": False,
                "dry_run": True,
                "frame": {"requestId": "sporedrive-wake-req-1"},
            }
        if self._start_result == "uncertain":
            return {
                "sent": True,
                "dry_run": False,
                "request_id": "sporedrive-wake-req-1",
                "response": None,
                "status": "uncertain",
            }
        if self._start_result == "confirmed":
            return {
                "sent": True,
                "dry_run": False,
                "request_id": "sporedrive-wake-req-1",
                "response": {"type": "response", "resultType": "success"},
            }
        raise AssertionError("unknown start_result")


def test_prepared_not_armed_is_the_default_and_records_correlation():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        fa = FakeAdapter()
        r = L.wake_peer(
            co, task_id="t1", message_id="m1", controller_id="cl", adapter=fa
        )
        assert r["state"] == "prepared_not_armed" and r["prepared"] is True
        assert r["wake_status"] == "not_established"
        assert r["recipient"] == "c"
        # conversation defaults to the addressed participant's native session (never guessed)
        assert r["conversation_id"] == "codex-thread-1"
        assert r["conversation_id_source"] == "recipient_native_session"
        # the short sd: handle is the correlation carried into the woken turn
        assert r["handle"] and r["handle"].startswith("sd:")
        assert r["handle"] in r["notification_line"]
        assert r["request_id"] == "sporedrive-wake-req-1"
        assert "ready/arm/yield" in r["arm_procedure"]
        # a durable receipt is retained even for a prepared-only plan
        rec = co.store.read("tasks/t1/wake/m1.json")
        assert rec["state"] == "prepared_not_armed" and rec["handle"] == r["handle"]


def test_owner_not_discovered_is_unavailable_no_send():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        fa = FakeAdapter(probe_status="owner_not_discovered")
        r = L.wake_peer(co, task_id="t1", message_id="m1", adapter=fa)
        assert (
            r["state"] == "wake_unavailable" and r["reason"] == "owner_not_discovered"
        )
        assert r["wake_status"] == "not_established"
        assert ("start_turn",) not in [
            (c[0],) for c in fa.calls
        ]  # never attempted a send


def test_app_build_unverified_is_unavailable():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        fa = FakeAdapter(build_verified=False)
        r = L.wake_peer(co, task_id="t1", message_id="m1", adapter=fa)
        assert (
            r["state"] == "wake_unavailable" and r["reason"] == "app_build_unverified"
        )


def test_armed_wake_to_protected_root_refuses_without_handshake():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        fa = FakeAdapter()
        r = L.wake_peer(
            co,
            task_id="t1",
            message_id="m1",
            conversation_id=ROOT,
            armed=True,
            dry_run=False,
            adapter=fa,
        )
        assert r["state"] == "refused_active_root_wake" and r["prepared"] is True
        assert r["conversation_id_source"] == "caller"
        assert (
            "start_turn",
            True,
            False,
        ) not in fa.calls  # the seam was never armed against root


def test_armed_dry_run_prepares_without_firing():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        fa = FakeAdapter(start_result="dry_run")
        r = L.wake_peer(
            co, task_id="t1", message_id="m1", armed=True, dry_run=True, adapter=fa
        )
        assert (
            r["state"] == "prepared_armed_dry_run"
            and r["wake_status"] == "not_established"
        )


def test_retained_unknown_send_identity_never_upgrades_to_delivered():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t)
        fa = FakeAdapter(start_result="uncertain")
        r = L.wake_peer(
            co, task_id="t1", message_id="m1", armed=True, dry_run=False, adapter=fa
        )
        assert r["state"] == "sent_uncertain" and r["sent"] is True
        # an uncertain outcome stays unknown under the same request id — route NOT established
        assert r["wake_status"] == "not_established"
        assert r["request_id"] == "sporedrive-wake-req-1"


def test_managed_actionable_without_reservation_refused_no_wake():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(t, kind="task")  # an actionable kind
        from mycelium_coord.execution import ExecutionManager

        ExecutionManager(co.store).open_execution(
            "t1", execution_id="e1", scope_ref="/s", authorization_ref="/a"
        )
        fa = FakeAdapter()
        r = L.wake_peer(co, task_id="t1", message_id="m1", adapter=fa)
        assert r["state"] == "refused_by_execution_gate"
        assert r["reason"] == "managed_dispatch_requires_reservation"
        assert fa.calls == []  # gate refused before any probe/send


def test_managed_paused_dispatch_refused_before_send():
    with tempfile.TemporaryDirectory() as t:
        co = _mk(
            t
        )  # a NON-actionable message; the dispatch identity comes from the caller hint
        from mycelium_coord.execution import ExecutionManager

        em = ExecutionManager(co.store)
        em.open_execution(
            "t1", execution_id="e1", scope_ref="/s", authorization_ref="/a"
        )
        em.reserve("t1", action_id="act1", kind="work_dispatch")
        em.pause("t1", authorization_ref="/a")  # a newer pause after the reservation
        fa = FakeAdapter()
        r = L.wake_peer(
            co, task_id="t1", message_id="m1", execution_action_id="act1", adapter=fa
        )
        assert (
            r["state"] == "refused_by_execution_gate"
            and r["reason"] == "execution_paused"
        )
        assert fa.calls == []  # a pause withholds the wake before any owner probe
