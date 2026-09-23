#!/usr/bin/env python3
"""Offline tests for the combined operation receipt (ExecutionManager.receipt) and the bounded
artifact read (ExecutionManager.evidence). Both are read-only; the record is never mutated.

Run: python3 -m pytest coordination/tests/test_execution_receipt.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import ProtocolError  # noqa: E402
from mycelium_coord.execution import ExecutionManager, STATUS_COMPLETED  # noqa: E402
from mycelium_coord.store import CoordStore  # noqa: E402
from mycelium_coord import views  # noqa: E402

AUTH = "/auth/user-instruction.md"
SCOPE = "/scope/frozen.md"


def _mgr(root, policy_path=None):
    return ExecutionManager(CoordStore(root), policy_path=policy_path)


def _open(mgr, task_id="t1", *, manifest=None, overrides=None, expires_at=None):
    return mgr.open_execution(
        task_id,
        execution_id=f"exec-{task_id}",
        scope_ref=SCOPE,
        authorization_ref=AUTH,
        acceptance_manifest=manifest,
        limits_overrides=overrides,
        expires_at=expires_at,
    )["execution"]


def _tmp():
    return tempfile.TemporaryDirectory()


def _policy(d, work_dispatches: int) -> str:
    """A maintained policy file with a larger work allowance (the default policy caps it at 4 and
    an open override may only lower it)."""
    p = Path(d) / "policy.json"
    p.write_text(json.dumps({"policy_version": 1, "defaults": {
        "work_dispatches": work_dispatches, "review_launches": 2, "review_deadline_seconds": 900,
        "expires_at": None, "technical_calls": None, "acceptance_calls": None}}))
    return str(p)


def _artifact(d, name, payload: bytes):
    p = Path(d) / name
    p.write_bytes(payload)
    return str(p), hashlib.sha256(payload).hexdigest()


def _size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


# --------------------------------------------------------------------------- receipt shape
def test_receipt_full_shape_fits_budget_and_carries_pointers():
    with _tmp() as d:
        m = _mgr(d)
        _open(m, "t1", manifest={"c1": {"description": "impl"}, "c2": {"description": "review"}})
        path, digest = _artifact(d, "ev.txt", b"evidence " * 100)
        m.reserve("t1", action_id="a1", kind="work_dispatch", criterion_ref="c1")
        m.settle("t1", action_id="a1", outcome="success", evidence_ref=path, criterion_ref="c1")
        m.record_evidence("t1", criterion_id="c1", evidence_ref=path, accepted_by="claude")
        before = m.read_execution("t1")
        r = m.receipt("t1")
        assert m.read_execution("t1") == before  # read-only
        assert r["receipt"] == "execution" and r["task_id"] == "t1"
        assert r["execution_id"] == "exec-t1"
        assert r["changed"] is True and "suppressed" not in r
        assert r["state_version"] == before["state_version"]
        assert r["status"] == "active" and r["phase"]
        assert r["terminal"] is False and r["stop"] is False
        assert r["usage"]["work_dispatches"] == 1 and "work_dispatches" in r["limits"]
        assert r["coverage"] == {"required": 2, "accepted": 1, "complete": False}
        acc = r["acceptance"]
        assert acc["accepted"]["c1"]["evidence_ref"] == path
        assert acc["accepted"]["c1"]["evidence_sha256"] == digest
        assert acc["pending"] == ["c2"]
        assert r["completion"] is None
        assert r["checks"][0]["action_id"] == "a1"
        assert r["checks"][0]["outcome"] == "success"
        assert r["checks"][0]["evidence_ref"] == path
        assert r["returned"] == 1 and r["omitted"] == 0 and r["truncated"] is False
        assert r["next_cursor"] is None and r["budget_bytes"] == 4096
        assert _size(r) <= 4096 and views.encoded_size(r) <= 4096
        assert m.receipt("missing") is None


def test_receipt_suppresses_unchanged_after_version_and_reports_change():
    with _tmp() as d:
        m = _mgr(d)
        _open(m, "t1", manifest={"c1": {"description": "impl"}})
        r1 = m.receipt("t1")
        r2 = m.receipt("t1", after_version=r1["state_version"])
        assert r2 == {
            "receipt": "execution",
            "task_id": "t1",
            "execution_id": "exec-t1",
            "state_version": r1["state_version"],
            "changed": False,
            "stop": False,
            "expired": False,
            "terminal": False,
            "suppressed": True,
        }
        assert _size(r2) < 240
        # any persisted mutation bumps the cursor -> changed again
        m.reserve("t1", action_id="a1", kind="work_dispatch")
        r3 = m.receipt("t1", after_version=r1["state_version"])
        assert r3["changed"] is True and r3["state_version"] > r1["state_version"]
        assert r3["open_reservations"] == ["a1"]
        m.settle("t1", action_id="a1", outcome="unknown")
        r4 = m.receipt("t1", after_version=r3["state_version"])
        assert r4["changed"] is True and r4["checks"][0]["outcome"] == "unknown"


def test_receipt_checks_bounded_newest_first():
    with _tmp() as d:
        m = _mgr(d, policy_path=_policy(d, 10))
        _open(m, "t1")
        for i in range(6):
            m.reserve("t1", action_id=f"a{i}", kind="work_dispatch")
            m.settle("t1", action_id=f"a{i}", outcome="success", evidence_ref=f"/e/{i}")
        r = m.receipt("t1", checks_limit=3)
        ids = [c["action_id"] for c in r["checks"]]
        assert ids == ["a5", "a4", "a3"]
        assert r["checks_total"] == 6 and r["returned"] == 3
        assert r["truncated"] is False  # bounding by checks_limit is not a budget cut
        assert r["checks"][0]["settled_at"] >= r["checks"][-1]["settled_at"]
        r0 = m.receipt("t1", checks_limit=0)
        assert r0["checks"] == [] and r0["checks_total"] == 6


def test_receipt_budget_truncates_truthfully_and_keeps_refs():
    with _tmp() as d:
        m = _mgr(d, policy_path=_policy(d, 40))
        _open(m, "t1")
        for i in range(30):
            m.reserve("t1", action_id=f"a{i:02d}", kind="work_dispatch")
            m.settle("t1", action_id=f"a{i:02d}", outcome="success", evidence_ref="/e/" + ("x" * 200))
        r = m.receipt("t1", checks_limit=30)
        assert r["truncated"] is True and r["omitted"] > 0
        assert r["returned"] + r["omitted"] == 30
        assert r["next_cursor"] == r["checks"][-1]["action_id"]
        assert views.encoded_size(r) <= 4096
        assert all(c["evidence_ref"] == "/e/" + ("x" * 200) for c in r["checks"])
        assert views._is_reference("evidence_ref") and views._is_reference("completion_ref")


def test_receipt_terminal_and_stop_after_completion():
    with _tmp() as d:
        m = _mgr(d)
        _open(m, "t1", manifest={"c1": {"description": "impl"}})
        path, _ = _artifact(d, "ev.txt", b"done")
        m.record_evidence("t1", criterion_id="c1", evidence_ref=path, accepted_by="claude")
        r = m.receipt("t1")
        assert r["status"] == "closed" and r["stop"] is True and r["terminal"] is False
        m.record_completion("t1", completion_ref="/receipt.md", accepted_by="owner")
        r = m.receipt("t1")
        assert r["status"] == STATUS_COMPLETED and r["terminal"] is True and r["stop"] is True
        assert r["completion"]["completion_ref"] == "/receipt.md"
        assert r["completion"]["accepted_by"] == "owner" and r["completion"]["at"]
        assert r["acceptance"]["pending"] == []


# --------------------------------------------------------------------------- evidence paging
def test_evidence_pages_recorded_artifact_with_sha256():
    with _tmp() as d:
        m = _mgr(d)
        _open(m, "t1", manifest={"c1": {"description": "impl"}})
        payload = b"".join(("line %04d héllo\n" % i).encode("utf-8") for i in range(600))
        assert len(payload) > 8192
        path, digest = _artifact(d, "big.txt", payload)
        m.record_evidence("t1", criterion_id="c1", evidence_ref=path, accepted_by="claude")
        p1 = m.evidence("t1", criterion_id="c1", offset=0, limit=4096, budget=0)
        assert p1["criterion_id"] == "c1" and p1["exists"] is True
        assert p1["bytes_total"] == len(payload)
        assert p1["truncated"] is True and p1["next_offset"] is not None
        assert p1["sha256"] == digest and p1["recorded_sha256"] == digest
        assert p1["sha256_match"] is True and p1["local"] is True
        assert p1["lossy"] is False
        # pages concatenate losslessly
        pages, off = [], 0
        while off is not None:
            pg = m.evidence("t1", criterion_id="c1", offset=off, limit=4096, budget=0)
            pages.append(pg["text"])
            off = pg["next_offset"]
        assert "".join(pages).encode("utf-8") == payload
        # default budget bounds the whole response
        pb = m.evidence("t1", criterion_id="c1", offset=0, limit=65536)
        assert views.encoded_size(pb) <= 4096 and pb["truncated"] is True
        # tail read
        pt = m.evidence("t1", criterion_id="c1", limit=64, tail=True, budget=0)
        assert pt["offset"] > 0 and pt["next_offset"] is None and pt["truncated"] is True
        assert payload.decode("utf-8").endswith(pt["text"])
        # whole file in one page is not truncated
        pw = m.evidence("t1", criterion_id="c1", limit=65536, budget=0)
        assert pw["truncated"] is False and pw["next_offset"] is None
        # a modified artifact is detected
        Path(path).write_bytes(payload + b"tampered")
        pm = m.evidence("t1", criterion_id="c1", limit=10, budget=0)
        assert pm["sha256_match"] is False and pm["sha256"] != digest


def test_evidence_selectors_and_refusals():
    with _tmp() as d:
        m = _mgr(d)
        _open(m, "t1", manifest={"c1": {"description": "impl"}})
        path, _ = _artifact(d, "ev.txt", b"abc")
        m.reserve("t1", action_id="a1", kind="work_dispatch")
        m.settle("t1", action_id="a1", outcome="success", evidence_ref=path)
        m.reserve("t1", action_id="a2", kind="work_dispatch")
        m.settle("t1", action_id="a2", outcome="unknown")  # no evidence ref
        with pytest.raises(ProtocolError) as ei:
            m.evidence("t1", criterion_id="nope")
        assert ei.value.code == "unknown_criterion"
        with pytest.raises(ProtocolError) as ei:
            m.evidence("t1", criterion_id="c1", action_id="a1")
        assert ei.value.code == "invalid_evidence_selector"
        with pytest.raises(ProtocolError) as ei:
            m.evidence("t1")
        assert ei.value.code == "invalid_evidence_selector"
        with pytest.raises(ProtocolError) as ei:
            m.evidence("t1", action_id="zzz")
        assert ei.value.code == "unknown_reservation"
        with pytest.raises(ProtocolError) as ei:
            m.evidence("t1", action_id="a2")
        assert ei.value.code == "missing_evidence"
        with pytest.raises(ProtocolError) as ei:
            m.evidence("t1", completion=True)
        assert ei.value.code == "missing_evidence"
        with pytest.raises(ProtocolError) as ei:
            m.evidence("absent", completion=True)
        assert ei.value.code == "execution_not_found"
        ra = m.evidence("t1", action_id="a1", budget=0)
        assert ra["action_id"] == "a1" and ra["text"] == "abc" and ra["truncated"] is False
        assert ra["recorded_sha256"] is None and ra["sha256_match"] is None
        # a semantic (non-local) reference is reported truthfully, never read as a path
        m.record_evidence("t1", criterion_id="c1", evidence_ref="review passed in PR #7",
                          attestation="codex verdict", accepted_by="codex")
        m.record_completion("t1", completion_ref="/no/such/receipt", accepted_by="owner")
        rs = m.evidence("t1", criterion_id="c1")
        assert rs["exists"] is False and rs["local"] is False and rs["sha256"] is None
        assert rs["text"] is None and rs["truncated"] is False
        rc = m.evidence("t1", completion=True)
        assert rc["completion"] is True and rc["exists"] is False


def test_views_is_reference_protects_ref_suffix():
    rec = {"evidence_ref": "x" * 5000, "note": "y" * 5000}
    out = views.shrink_tails(rec, budget=512, keys=("note",))
    assert out["evidence_ref"] == "x" * 5000
    assert len(out["note"]) < 5000


def test_r1_expiry_is_never_hidden_by_an_unchanged_cursor():
    """R1: time can expire a task without bumping state_version; the receipt must still expose it."""
    from datetime import datetime, timedelta, timezone
    import time
    with _tmp() as d:
        m = _mgr(d)
        soon = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        _open(m, "t1", expires_at=soon)
        r1 = m.receipt("t1")
        assert r1["changed"] is True and r1["expired"] is False and r1["stop"] is False
        r2 = m.receipt("t1", after_version=r1["state_version"])
        assert r2["suppressed"] is True and r2["expired"] is False  # compact unchanged ACTIVE receipt kept
        time.sleep(1.2)
        r3 = m.receipt("t1", after_version=r1["state_version"])
        assert r3["state_version"] == r1["state_version"]  # stored version unchanged...
        assert r3["changed"] is True and "suppressed" not in r3  # ...yet the deadline transition is exposed
        assert r3["expired"] is True and r3["stop"] is True and r3["version_changed"] is False
