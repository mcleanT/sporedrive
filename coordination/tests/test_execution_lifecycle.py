#!/usr/bin/env python3
"""Lifecycle tests for the execution core: the contract repairs R4 (closure vs terminal completion,
attributable evidence) and R5 (fail-closed unattended expiry + crash-reconcilable shutdown).

These exercise properties the primary review demonstrated were missing:

  * coverage -> CLOSURE is NOT terminal completion; a blocker in closure funds ONLY a bounded repair,
    never generic work; the explicit final receipt is the separate transition to terminal completed,
    after which nothing reactivates the execution;
  * evidence must be attributable: a local artifact is validated by identity, external/semantic
    evidence requires an attestation + accepted_by; a bare unresolvable reference is never a pass;
  * an invalid/naive expiry and a negative/absurd limit are refused at open AND change; an unattended
    execution requires a finite expiry and a named automation; an unparseable stored expiry fails
    CLOSED; a terminal/expired execution records a durable pending shutdown that a host adapter
    reconciles by pausing the named automation — exactly once, even across a simulated crash.

Run: python3 -m pytest coordination/tests/test_execution_lifecycle.py -q
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import ProtocolError  # noqa: E402
from mycelium_coord.execution import (  # noqa: E402
    ExecutionManager,
    STATUS_CLOSED,
    STATUS_COMPLETED,
    reconcile_pending_shutdown,
)
from mycelium_coord.store import CoordStore  # noqa: E402

AUTH = "/auth/user-instruction.md"
SCOPE = "/scope/frozen.md"


def _mgr(root):
    return ExecutionManager(CoordStore(root))


def _future(seconds=3600):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _open(
    m, task="t1", *, manifest=None, attended=True, expires_at=None, automation_ref=None
):
    return m.open_execution(
        task,
        execution_id=f"e-{task}",
        scope_ref=SCOPE,
        authorization_ref=AUTH,
        acceptance_manifest=manifest,
        attended=attended,
        expires_at=expires_at,
        automation_ref=automation_ref,
    )["execution"]


# --------------------------------------------------------------- R4: attributable evidence
def test_local_artifact_evidence_validated_by_identity():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d)
        _open(m, manifest={"c1": {"description": "x"}})
        artifact = Path(d) / "result.txt"
        artifact.write_text("measured output\n")
        import hashlib

        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        # a local artifact is validated by identity; the computed digest is recorded (no attestation
        # needed for a verifiable artifact)
        res = m.record_evidence("t1", criterion_id="c1", evidence_ref=str(artifact))
        assert res["closed"] is True
        rec = m.read_execution("t1")
        assert rec["acceptance_manifest"]["c1"]["evidence_sha256"] == digest


def test_local_artifact_digest_mismatch_and_missing_are_refused():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d)
        _open(m, manifest={"c1": {"description": "x"}, "c2": {"description": "y"}})
        artifact = Path(d) / "r.txt"
        artifact.write_text("real\n")
        with pytest.raises(ProtocolError) as e1:
            m.record_evidence(
                "t1",
                criterion_id="c1",
                evidence_ref=str(artifact),
                evidence_sha256="deadbeef",
            )
        assert e1.value.code == "evidence_hash_mismatch"
        # a digest claimed for a nonexistent artifact cannot be validated
        with pytest.raises(ProtocolError) as e2:
            m.record_evidence(
                "t1",
                criterion_id="c2",
                evidence_ref="/no/such/file",
                evidence_sha256="deadbeef",
            )
        assert e2.value.code == "evidence_artifact_missing"


def test_external_evidence_requires_attributable_attestation():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d)
        _open(m, manifest={"c1": {"description": "x"}})
        # a bare unresolvable reference with no attestation is NEVER a pass (review R4 probe)
        with pytest.raises(ProtocolError) as ei:
            m.record_evidence(
                "t1", criterion_id="c1", evidence_ref="/nonexistent/evidence"
            )
        assert ei.value.code == "unattributable_evidence"
        # an attestation alone without an attributed identity is still not attributable
        with pytest.raises(ProtocolError):
            m.record_evidence(
                "t1",
                criterion_id="c1",
                evidence_ref="s3://ext/log",
                attestation="looks good",
            )
        # attestation + accepted_by is attributable -> accepted
        res = m.record_evidence(
            "t1",
            criterion_id="c1",
            evidence_ref="s3://ext/log",
            attestation="reviewer confirmed",
            accepted_by="codex",
        )
        assert res["closed"] is True


def test_completion_requires_closure_and_is_terminal():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d)
        _open(m, manifest={"c1": {"description": "x"}})
        # cannot finalize before coverage is complete
        with pytest.raises(ProtocolError) as e0:
            m.record_completion("t1", completion_ref="/r", accepted_by="user")
        assert e0.value.code == "not_in_closure"
        m.record_evidence(
            "t1",
            criterion_id="c1",
            evidence_ref="ext://ok",
            attestation="done",
            accepted_by="claude",
        )
        assert m.status("t1")["status"] == STATUS_CLOSED
        # a final receipt must be attributable
        with pytest.raises(ProtocolError) as e1:
            m.record_completion("t1", completion_ref="", accepted_by="user")
        assert e1.value.code == "unattributable_completion"
        m.record_completion("t1", completion_ref="/receipt", accepted_by="user")
        assert m.status("t1")["status"] == STATUS_COMPLETED
        # terminal: completion is idempotent and phase/pause cannot mutate it
        assert m.record_completion("t1", completion_ref="/receipt", accepted_by="user")[
            "idempotent"
        ]
        with pytest.raises(ProtocolError):
            m.set_phase("t1", phase="acceptance")


# --------------------------------------------------------------- R5: expiry / limit validation
def test_invalid_expiry_rejected_at_open_and_change():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d)
        with pytest.raises(ProtocolError) as e1:
            _open(m, "bad", expires_at="not-a-time")
        assert e1.value.code == "invalid_expiry"
        _open(m, "t1", expires_at=_future())
        with pytest.raises(ProtocolError) as e2:
            m.change_limits(
                "t1", authorization_ref="/a", changes={"expires_at": "still-not-a-time"}
            )
        assert e2.value.code == "invalid_expiry"


def test_invalid_numeric_limits_rejected():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d)
        _open(m, "t1")
        with pytest.raises(ProtocolError) as e1:
            m.change_limits(
                "t1", authorization_ref="/a", changes={"review_deadline_seconds": 0}
            )
        assert e1.value.code == "invalid_review_deadline"
        with pytest.raises(ProtocolError) as e2:
            m.change_limits(
                "t1", authorization_ref="/a", changes={"technical_calls": -1}
            )
        assert e2.value.code == "invalid_limit"


def test_unattended_requires_finite_expiry_and_automation():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d)
        with pytest.raises(ProtocolError) as e1:
            _open(m, "u1", attended=False)  # no expiry
        assert e1.value.code == "unattended_requires_expiry"
        with pytest.raises(ProtocolError) as e2:
            _open(m, "u2", attended=False, expires_at=_future())  # no automation
        assert e2.value.code == "unattended_requires_automation_ref"
        rec = _open(
            m,
            "u3",
            attended=False,
            expires_at=_future(),
            automation_ref="sched:overnight",
        )
        assert rec["attended"] is False and rec["automation_ref"] == "sched:overnight"
        # an unattended execution cannot later drop to a null expiry
        with pytest.raises(ProtocolError) as e3:
            m.change_limits("u3", authorization_ref="/a", changes={"expires_at": None})
        assert e3.value.code == "unattended_requires_expiry"


def test_unparseable_stored_expiry_fails_closed():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d)
        _open(m, "t1")
        # corrupt the durable expiry directly (bypassing validation) to prove defense-in-depth
        rel = "tasks/t1/execution.json"
        rec = m.store.read(rel)
        rec["limits"]["expires_at"] = "garbage-timestamp"
        m.store.write(rel, rec)
        assert m.status("t1")["expired"] is True  # fail closed
        with pytest.raises(ProtocolError) as ei:
            m.reserve("t1", action_id="a1", kind="work_dispatch")
        assert ei.value.code == "execution_expired"


# --------------------------------------------------------------- R5: crash-reconcilable shutdown
class FakeScheduler:
    """The supported automation tool, stubbed. Records which automations it paused."""

    def __init__(self):
        self.paused = []

    def pause(self, automation_ref):
        self.paused.append(automation_ref)
        return {"paused": automation_ref}


def test_completion_records_pending_shutdown_reconciled_by_host_adapter():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d)
        _open(
            m,
            "t1",
            manifest={"c1": {"description": "x"}},
            attended=False,
            expires_at=_future(),
            automation_ref="sched:overnight",
        )
        m.record_evidence(
            "t1",
            criterion_id="c1",
            evidence_ref="ext://ok",
            attestation="done",
            accepted_by="claude",
        )
        m.record_completion("t1", completion_ref="/r", accepted_by="user")
        assert m.status("t1")["shutdown_pending"] is True
        # simulate a CRASH between the terminal state and the scheduler pause: a fresh manager reads
        # the durable pending intent and the host adapter completes the pause exactly once.
        m2 = _mgr(d)
        sched = FakeScheduler()
        r = reconcile_pending_shutdown(m2, "t1", sched.pause)
        assert r["acted"] is True and sched.paused == ["sched:overnight"]
        assert m2.status("t1")["shutdown"]["status"] == "reconciled"
        # idempotent: a second sweep does nothing (no double pause, no extra work dispatched)
        r2 = reconcile_pending_shutdown(m2, "t1", sched.pause)
        assert r2["acted"] is False and sched.paused == ["sched:overnight"]
        assert m2.status("t1")["usage"]["work_dispatches"] == 0


def test_expiry_shutdown_intent_noted_and_reconciled():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d)
        _open(
            m,
            "t1",
            attended=False,
            expires_at=_future(2),
            automation_ref="sched:overnight",
        )
        # force expiry by rewriting to a past timestamp (still a VALID timestamp)
        rel = "tasks/t1/execution.json"
        rec = m.store.read(rel)
        rec["limits"]["expires_at"] = "2000-01-01T00:00:00+00:00"
        m.store.write(rel, rec)
        noted = m.note_expiry_shutdown("t1")
        assert noted["noted"] is True and noted["shutdown"]["reason"] == "expiry"
        sched = FakeScheduler()
        r = reconcile_pending_shutdown(m, "t1", sched.pause)
        assert r["acted"] is True and sched.paused == ["sched:overnight"]


def test_pause_failure_keeps_intent_pending_for_next_sweep():
    with tempfile.TemporaryDirectory() as d:
        m = _mgr(d)
        _open(
            m,
            "t1",
            attended=False,
            expires_at=_future(),
            automation_ref="sched:overnight",
        )
        m.request_shutdown("t1", reason="expiry")

        def flaky_pause(ref):
            raise RuntimeError("scheduler unreachable")

        r = reconcile_pending_shutdown(m, "t1", flaky_pause)
        assert r["acted"] is False and r["reason"] == "pause_failed"
        # the intent is STILL pending (not silently marked done) so a later sweep retries
        assert m.status("t1")["shutdown"]["status"] == "pending"
        good = FakeScheduler()
        r2 = reconcile_pending_shutdown(m, "t1", good.pause)
        assert r2["acted"] is True and good.paused == ["sched:overnight"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
