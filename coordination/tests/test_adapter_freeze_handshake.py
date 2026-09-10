#!/usr/bin/env python3
"""Fake-provider fixtures for the adapter freeze-identity handshake (PLAN sections 4/7/8).

These represent incident 2 (the roundtable integration defects that were debugged INSIDE the strict
acceptance cycle) as offline fixtures, so the two failure patterns the workflow-audit brief names are
covered without any real provider:

  * a technical-debug probe that returns TRUNCATED/error JSON leaves readiness UNESTABLISHED (a smoke
    call used to change the implementation is debugging evidence, never acceptance evidence);
  * an implementation change WITHIN a reserved acceptance batch (a new build == a new freeze identity)
    blocks the remaining calls and invalidates the attempt, so old and new responses can never be
    blended into a passing cycle.

They also show the enforcement boundary the plan insists on: an adapter that reserves each live call
with a freeze identity computed from its ACTUAL config enforces phase/call limits; an observational
adapter that only reports totals afterward does not, and cannot claim it does.

Run: python3 -m pytest coordination/tests/test_adapter_freeze_handshake.py -q
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import ProtocolError  # noqa: E402
from mycelium_coord.execution import (  # noqa: E402
    PHASE_ACCEPTANCE,
    PHASE_TECHNICAL_DEBUG,
    STATUS_ACTIVE,
    STATUS_CLOSED,
    STATUS_COMPLETED,
    ExecutionManager,
)
from mycelium_coord.store import CoordStore  # noqa: E402

AUTH = "/auth/user-instruction.md"
SCOPE = "/scope/frozen.md"


class FakeProvider:
    """A stand-in provider. `build` models the actual implementation/config identity the adapter must
    read; changing it mid-run models patching the implementation between calls. Responses are canned."""

    def __init__(self, build: str, responses):
        self.build = build
        self._responses = list(responses)
        self._i = 0

    def call(self, _prompt: str) -> str:
        r = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return r


class EnforcingAdapter:
    """Minimal ENFORCING adapter: computes freeze identity from the provider's ACTUAL current build
    (not a copied constant), reserves each live call with phase+identity BEFORE issuing it, and binds
    the response by settling against that identity. A build change between reserve and settle, or
    across calls in the acceptance phase, is caught by the core handshake."""

    def __init__(self, em: ExecutionManager, task_id: str, provider: FakeProvider):
        self.em, self.task_id, self.provider = em, task_id, provider

    def _freeze(self) -> str:
        return hashlib.sha256(f"build={self.provider.build}".encode()).hexdigest()[:16]

    def call(self, action_id: str, prompt: str, *, phase: str = PHASE_ACCEPTANCE):
        self.em.reserve_call(
            self.task_id,
            action_id=action_id,
            phase=phase,
            freeze_identity=self._freeze(),
        )
        resp = self.provider.call(prompt)
        valid = _is_valid_json_answer(resp)
        # settle binds the response to the identity that is current NOW; a mid-call build change
        # raises freeze_identity_mismatch here.
        self.em.settle_call(
            self.task_id,
            action_id=action_id,
            freeze_identity=self._freeze(),
            outcome="ok" if valid else "invalid",
            response_ref=f"/resp/{action_id}",
        )
        return resp, valid


def _is_valid_json_answer(text: str) -> bool:
    """A truncated or error body is NOT a valid probe result (the roundtable's truncated-JSON-as-
    success bug): it must parse AND carry the expected 'answer' field."""
    try:
        obj = json.loads(text)
    except ValueError:
        return False
    return isinstance(obj, dict) and "answer" in obj and bool(obj["answer"])


def _open(root, *, manifest, calls):
    em = ExecutionManager(CoordStore(root))
    em.open_execution(
        "t1",
        execution_id="e1",
        scope_ref=SCOPE,
        authorization_ref=AUTH,
        acceptance_manifest=manifest,
        limits_overrides=calls,
    )
    return em


def test_enforcing_adapter_valid_acceptance_closes():
    with tempfile.TemporaryDirectory() as d:
        em = _open(
            d,
            manifest={"acc": {"kind": "acceptance", "description": "fresh acceptance"}},
            calls={"acceptance_calls": 3},
        )
        em.set_phase("t1", phase=PHASE_ACCEPTANCE)
        adapter = EnforcingAdapter(em, "t1", FakeProvider("AAA", ['{"answer": "ok"}']))
        _resp, valid = adapter.call("c1", "check?")
        assert valid is True
        # coverage complete -> CLOSURE (not terminal completion; that needs a final receipt, R4)
        em.record_evidence(
            "t1",
            criterion_id="acc",
            evidence_ref="/resp/c1",
            attestation="acceptance response validated by adapter",
            accepted_by="adapter",
        )
        s = em.status("t1")
        assert s["status"] == STATUS_CLOSED
        assert s["coverage"]["complete"] is True
        assert s["usage"]["acceptance_calls"] == 1
        # the explicit final completion receipt is the SEPARATE transition to terminal completed
        em.record_completion("t1", completion_ref="/receipt/final", accepted_by="user")
        assert em.status("t1")["status"] == STATUS_COMPLETED


def test_truncated_probe_leaves_readiness_unestablished():
    with tempfile.TemporaryDirectory() as d:
        em = _open(
            d,
            manifest={"acc": {"kind": "acceptance", "description": "fresh acceptance"}},
            calls={"technical_calls": 3, "acceptance_calls": 3},
        )
        em.set_phase("t1", phase=PHASE_TECHNICAL_DEBUG)
        # a technical-debug probe returns a compact TRUNCATED JSON body
        adapter = EnforcingAdapter(
            em, "t1", FakeProvider("AAA", ['{"answer": "ok'])
        )  # missing }
        _resp, valid = adapter.call("p1", "probe?", phase=PHASE_TECHNICAL_DEBUG)
        assert valid is False  # truncated body is not a valid result
        # the probe is debugging evidence, not acceptance evidence: no criterion is recorded
        s = em.status("t1")
        assert s["coverage"]["complete"] is False  # readiness remains UNESTABLISHED
        assert s["status"] == STATUS_ACTIVE
        assert s["usage"]["technical_calls"] == 1  # the probe WAS ledgered
        assert s["usage"]["acceptance_calls"] == 0  # nothing counted as acceptance


def test_build_change_within_acceptance_batch_invalidates_no_blended_cycle():
    with tempfile.TemporaryDirectory() as d:
        em = _open(
            d,
            manifest={
                "acc1": {"kind": "acceptance", "description": "first acceptance check"},
                "acc2": {
                    "kind": "acceptance",
                    "description": "second acceptance check",
                },
            },
            calls={"acceptance_calls": 4},
        )
        em.set_phase("t1", phase=PHASE_ACCEPTANCE)
        provider = FakeProvider("AAA", ['{"answer": "one"}', '{"answer": "two"}'])
        adapter = EnforcingAdapter(em, "t1", provider)
        adapter.call("c1", "check1?")
        em.record_evidence(
            "t1",
            criterion_id="acc1",
            evidence_ref="/resp/c1",
            attestation="acc1 validated",
            accepted_by="adapter",
        )
        assert (
            em.read_execution("t1")["acceptance_manifest"]["acc1"]["accepted"] is True
        )
        # someone patches the implementation mid-batch -> the provider's build changes
        provider.build = "BBB"
        with pytest.raises(ProtocolError) as ei:
            adapter.call("c2", "check2?")  # reserve_call sees a new freeze identity
        assert ei.value.code == "freeze_identity_mismatch"
        rec = em.read_execution("t1")
        # the criterion proven under the stale build is invalidated; no blended passing cycle
        assert rec["acceptance_manifest"]["acc1"]["accepted"] is False
        assert rec["status"] != STATUS_COMPLETED
        assert len(rec["acceptance_invalidations"]) == 1


def test_observational_adapter_cannot_claim_enforcement():
    """An adapter that skips the reservation handshake and just calls the provider consumes NO
    managed allowance and sets NO freeze identity — it is observational and cannot claim enforced
    phase/call limits, exactly as the plan states."""
    with tempfile.TemporaryDirectory() as d:
        em = _open(
            d,
            manifest={"acc": {"kind": "acceptance", "description": "x"}},
            calls={"acceptance_calls": 3},
        )
        em.set_phase("t1", phase=PHASE_ACCEPTANCE)
        provider = FakeProvider("AAA", ['{"answer": "ok"}', '{"answer": "ok"}'])
        # observational: no reserve_call/settle_call at all
        provider.call("a?")
        provider.call("b?")
        s = em.status("t1")
        assert (
            s["usage"]["acceptance_calls"] == 0
        )  # the core enforced nothing it never saw
        assert em.read_execution("t1")["acceptance_freeze"] is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
