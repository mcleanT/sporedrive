"""Owner-directed follow-up must not be locked by a completed run (owner-followup v1).

Reproduces the Autoscience incident (completed execution + genuine owner approval -> refusal) and
verifies the ONE supported recovery operation ``owner_request`` from the core, the CLI and the MCP
entry points. Run: python3 -m pytest coordination/tests/test_owner_followup.py -q
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord.execution import ExecutionManager  # noqa: E402
from mycelium_coord.model import ProtocolError  # noqa: E402
from mycelium_coord.store import CoordStore  # noqa: E402
from mycelium_coord.views import stops_wait  # noqa: E402

from test_execution_core import AUTH, SCOPE, _open  # noqa: E402

OWNER = "/owner/2026-09-15T21:48Z-unlock-this-and-proceed.md"  # the ACTUAL owner instruction
MANIFEST_V1 = {"figure_v1": {"description": "figure v1 assembled", "kind": "deliverable"}}
MANIFEST_V2 = {"figure_v2": {"description": "figure revised per owner list", "kind": "deliverable"},
               "review_v2": {"description": "one scoped review", "kind": "review"}}


def _mgr(tmp_path):
    return ExecutionManager(CoordStore(str(tmp_path)))


def _complete(em, task="t1", *, overrides=None, expires_at=None):
    """Drive a run to the TERMINAL completed state the way the real figure run reached it."""
    _open(em, task, manifest=MANIFEST_V1, overrides=overrides, expires_at=expires_at)
    em.reserve(task, action_id="d1", kind="work_dispatch")
    em.settle(task, action_id="d1", outcome="success", evidence_ref="/e/d1")
    em.record_evidence(task, criterion_id="figure_v1", evidence_ref="/e/figure-v1.svg",
                       attestation="v1 delivered", accepted_by="codex-supervisor")
    assert em.status(task)["status"] == "closed"
    r = em.record_completion(task, completion_ref="/e/receipt-v1.md", accepted_by="codex-supervisor")
    assert r["execution"]["status"] == "completed"
    return em.read_execution(task)


def _followup(em, task="t1", rid="owner-req-1", **kw):
    kw.setdefault("authorization_ref", OWNER)
    kw.setdefault("scope_ref", "/scope/figure-v2-revisions.md")
    kw.setdefault("acceptance_manifest", MANIFEST_V2)
    return em.owner_request(task, request_id=rid, **kw)


# ------------------------------------------------------------------ the incident, reproduced
def test_completed_plus_genuine_owner_approval_was_refused_before_the_fix_paths(tmp_path):
    em = _mgr(tmp_path)
    _complete(em)
    # the two supported recovery operations that exist(ed): change_limits records authority but
    # cannot reopen; unpause refuses with not_paused; new work is refused as execution_completed.
    em.change_limits("t1", authorization_ref=OWNER, changes={"work_dispatches": 8})
    with pytest.raises(ProtocolError) as ei:
        em.unpause("t1", authorization_ref=OWNER)
    assert ei.value.code == "not_paused" and "owner_request" in str(ei.value)
    with pytest.raises(ProtocolError) as ei:
        em.reserve("t1", action_id="d2", kind="work_dispatch")
    assert ei.value.code == "execution_completed" and "owner_request" in str(ei.value)
    assert stops_wait(em.status("t1")) is True


# ------------------------------------------------------------------ the supported recovery
def test_completed_owner_followup_opens_run_2_preserving_history_and_usage(tmp_path):
    em = _mgr(tmp_path)
    before = _complete(em)
    r = _followup(em, add_limits={"work_dispatches": 2})
    assert r["idempotent"] is False and r["run"] == 2 and r["prior_run"] == 1
    ex = r["execution"]
    assert ex["status"] == "active" and ex["run"] == 2 and ex["run_id"] == "exec-t1/run-2"
    assert ex["closure"] is None and ex["shutdown"] is None and ex["shutdown_pending"] is False
    assert stops_wait(ex) is False
    # new scope has its OWN acceptance state; the old accepted criterion cannot satisfy it
    assert ex["coverage"] == {"required": 2, "accepted": 0, "complete": False}
    rec = em.read_execution("t1")
    assert sorted(rec["acceptance_manifest"]) == ["figure_v2", "review_v2"]
    assert all(not c["accepted"] for c in rec["acceptance_manifest"].values())
    # prior run archived immutably: receipt, acceptance evidence, usage and limits at end
    assert len(rec["runs"]) == 1
    prior = rec["runs"][0]
    assert prior["run"] == 1 and prior["status"] == "completed"
    assert prior["completion"]["completion_ref"] == "/e/receipt-v1.md"
    assert prior["acceptance_manifest"]["figure_v1"]["accepted"] is True
    assert prior["acceptance_manifest"] == before["acceptance_manifest"]
    assert prior["closure"] == before["closure"] and prior["shutdown"] == before["shutdown"]
    assert prior["usage_at_end"]["work_dispatches"] == 1 and prior["ended_by"] == "owner-req-1"
    # cumulative usage survives; the owner's addition is ADDED, recorded, never a reset
    assert ex["usage"]["work_dispatches"] == 1
    assert ex["limits"]["work_dispatches"] == before["limits"]["work_dispatches"] + 2
    lc = rec["limit_changes"][-1]
    assert lc["owner_request_id"] == "owner-req-1" and lc["changes"]["work_dispatches"]["added"] == 2
    assert lc["authorization_ref"] == OWNER
    # the recovery is recorded as owner authority, visible in a fresh read
    assert ex["authorization"]["last_recovery"]["via"] == "owner_request"
    assert ex["authorization"]["last_recovery"]["authorization_ref"] == OWNER
    assert ex["last_owner_request"]["request_id"] == "owner-req-1" and ex["owner_requests"] == 1
    assert rec["scope_ref"] == "/scope/figure-v2-revisions.md" and rec["authorization_ref"] == AUTH
    # and new work proceeds under the new run
    em.reserve("t1", action_id="d2", kind="work_dispatch")
    assert em.status("t1")["usage"]["work_dispatches"] == 2


def test_expired_and_paused_owner_followups_succeed_and_exhausted_stays_truthful(tmp_path):
    em = _mgr(tmp_path)
    # expired: requires a future deadline with the request (no silent extension)
    soon = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    _open(em, "tx", manifest=MANIFEST_V1, expires_at=soon)
    time.sleep(1.2)
    assert em.status("tx")["expired"] is True
    with pytest.raises(ProtocolError) as ei:
        _followup(em, "tx")
    assert ei.value.code == "owner_request_requires_deadline"
    with pytest.raises(ProtocolError) as ei:
        _followup(em, "tx", expires_at=soon)  # a past deadline is not a recovery
    assert ei.value.code == "owner_request_requires_deadline"
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    ex = _followup(em, "tx", expires_at=future)["execution"]
    assert ex["status"] == "active" and ex["expired"] is False and ex["run"] == 2
    # paused
    _open(em, "tp", manifest=MANIFEST_V1)
    em.pause("tp", authorization_ref="owner:pause")
    ex = _followup(em, "tp")["execution"]
    assert ex["status"] == "active" and ex["run"] == 2 and em.read_execution("tp")["pause"] is None
    assert em.read_execution("tp")["runs"][0]["pause"]["authorization_ref"] == "owner:pause"
    # exhausted without an added allowance stays exhausted truthfully (usage is never reset)
    _open(em, "te", manifest=MANIFEST_V1, overrides={"work_dispatches": 1})
    em.reserve("te", action_id="d1", kind="work_dispatch")
    with pytest.raises(ProtocolError):
        em.reserve("te", action_id="d2", kind="work_dispatch")  # the attempt that flags exhaustion
    assert em.status("te")["status"] == "exhausted"
    ex = _followup(em, "te")["execution"]
    assert ex["status"] == "exhausted" and ex["run"] == 2 and ex["usage"]["work_dispatches"] == 1
    ex = _followup(em, "te", rid="owner-req-2", add_limits={"work_dispatches": 1})["execution"]
    assert ex["status"] == "active" and ex["run"] == 3 and ex["limits"]["work_dispatches"] == 2
    # closed (coverage complete, not yet completed)
    _open(em, "tc", manifest=MANIFEST_V1)
    em.record_evidence("tc", criterion_id="figure_v1", evidence_ref="/e/v1", attestation="ok",
                       accepted_by="sup")
    assert em.status("tc")["status"] == "closed"
    assert _followup(em, "tc")["execution"]["status"] == "active"


def test_no_owner_request_stays_stopped_and_agent_reference_is_only_recorded(tmp_path):
    em = _mgr(tmp_path)
    _complete(em)
    # nothing but an explicit owner request changes the stopped state: reads, questions, backlog
    em.add_backlog("t1", item="could the figure use a legend?", source="codex")
    assert em.status("t1")["status"] == "completed" and stops_wait(em.status("t1")) is True
    with pytest.raises(ProtocolError) as ei:
        _followup(em, authorization_ref="")
    assert ei.value.code == "missing_authorization"
    with pytest.raises(ProtocolError) as ei:
        _followup(em, scope_ref="")
    assert ei.value.code == "missing_scope"
    assert em.status("t1")["status"] == "completed" and em.read_execution("t1").get("runs") is None


def test_identical_replay_is_idempotent_and_conflicting_replay_is_refused(tmp_path):
    em = _mgr(tmp_path)
    _complete(em)
    first = _followup(em, add_limits={"work_dispatches": 2})
    v = first["execution"]["state_version"]
    again = _followup(em, add_limits={"work_dispatches": 2})
    assert again["idempotent"] is True and again["run"] == 2
    assert again["execution"]["state_version"] == v  # no write, no second run, no charge
    assert again["execution"]["limits"]["work_dispatches"] == first["execution"]["limits"]["work_dispatches"]
    assert len(em.read_execution("t1")["runs"]) == 1
    with pytest.raises(ProtocolError) as ei:
        _followup(em, add_limits={"work_dispatches": 5})
    assert ei.value.code == "owner_request_conflict"
    with pytest.raises(ProtocolError) as ei:
        _followup(em, scope_ref="/scope/other.md", add_limits={"work_dispatches": 2})
    assert ei.value.code == "owner_request_conflict"
    assert em.read_execution("t1")["state_version"] == v


def test_old_receipts_and_stale_shutdown_cannot_close_or_stop_the_new_run(tmp_path):
    em = _mgr(tmp_path)
    _complete(em)
    _followup(em)
    # the old completion receipt is stale for run 2
    with pytest.raises(ProtocolError) as ei:
        em.record_completion("t1", completion_ref="/e/receipt-v1.md", accepted_by="codex-supervisor")
    assert ei.value.code == "stale_completion_receipt"
    # a fresh receipt cannot complete run 2 either while its own coverage is incomplete
    with pytest.raises(ProtocolError) as ei:
        em.record_completion("t1", completion_ref="/e/receipt-v2.md", accepted_by="codex-supervisor")
    assert ei.value.code == "not_in_closure"
    # the archived run's shutdown intent does not exist on run 2: nothing to reconcile, no stop
    with pytest.raises(ProtocolError) as ei:
        em.reconcile_shutdown("t1", outcome="paused")
    assert ei.value.code == "no_pending_shutdown"
    assert em.note_expiry_shutdown("t1") == {"noted": False, "reason": "not_expired"}
    st = em.status("t1")
    assert st["status"] == "active" and st["shutdown_pending"] is False and stops_wait(st) is False
    # old accepted evidence cannot satisfy the new criteria: only run-2 evidence closes run 2
    em.record_evidence("t1", criterion_id="figure_v2", evidence_ref="/e/v2.svg", attestation="v2",
                       accepted_by="sup")
    assert em.status("t1")["coverage"] == {"required": 2, "accepted": 1, "complete": False}
    em.record_evidence("t1", criterion_id="review_v2", evidence_ref="/e/review-v2.md", attestation="r",
                       accepted_by="sup")
    assert em.status("t1")["status"] == "closed"
    r = em.record_completion("t1", completion_ref="/e/receipt-v2.md", accepted_by="codex-supervisor")
    assert r["execution"]["status"] == "completed" and r["execution"]["run"] == 2
    rec = em.read_execution("t1")
    assert rec["runs"][0]["completion"]["completion_ref"] == "/e/receipt-v1.md"
    assert rec["completion"]["completion_ref"] == "/e/receipt-v2.md"


def test_live_conflicting_work_and_unreconciled_automation_shutdown_are_refused(tmp_path):
    em = _mgr(tmp_path)
    _complete(em)
    with pytest.raises(ProtocolError) as ei:
        _followup(em, live_work=["job-figure-render-7"])
    assert ei.value.code == "owner_request_live_work" and "job-figure-render-7" in str(ei.value)
    assert em.status("t1")["status"] == "completed"
    # draining = identified in-flight owned work
    _open(em, "td", manifest=MANIFEST_V1)
    em.reserve("td", action_id="d1", kind="work_dispatch")
    em.claim_dispatch("td", action_id="d1", dispatch_identity="brief-1")
    em.pause("td", authorization_ref="owner:pause")
    assert em.status("td")["status"] == "draining"
    with pytest.raises(ProtocolError) as ei:
        _followup(em, "td")
    assert ei.value.code == "owner_request_live_work"
    # an unattended run whose automation shutdown is still pending is never force-unlocked
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    em.open_execution("tu", execution_id="exec-tu", scope_ref=SCOPE, authorization_ref=AUTH,
                      acceptance_manifest=MANIFEST_V1, expires_at=future, attended=False,
                      automation_ref="sched:figure-nightly")
    em.request_shutdown("tu", reason="operator")
    with pytest.raises(ProtocolError) as ei:
        _followup(em, "tu")
    assert ei.value.code == "owner_request_unreconciled_shutdown"
    em.reconcile_shutdown("tu", outcome="paused")
    assert _followup(em, "tu")["execution"]["status"] == "active"


def test_legacy_completed_record_without_run_fields_reads_truthfully_and_recovers(tmp_path):
    em = _mgr(tmp_path)
    rec = _complete(em)
    assert "run" not in rec and "runs" not in rec and "owner_requests" not in rec  # legacy shape
    st = em.status("t1")
    assert st["run"] == 1 and st["run_id"] == "exec-t1" and st["runs_archived"] == 0
    assert st["owner_requests"] == 0 and st["last_owner_request"] is None
    assert _followup(em)["execution"]["run"] == 2


# ------------------------------------------------------------------ actual CLI and MCP entry points
def test_cli_and_mcp_owner_request_make_identical_state():
    from mycelium_coord import cli
    import asyncio
    ms = pytest.importorskip("mycelium_coord.mcp_server")  # fastmcp lives only in the MCP interpreter

    def seed(root):
        em = ExecutionManager(CoordStore(root))
        _complete(em)

    with tempfile.TemporaryDirectory() as dc, tempfile.TemporaryDirectory() as dm:
        seed(dc); seed(dm)
        assert cli.run(["--root", dc, "exec-owner-request", "t1", "--request-id", "owner-req-1",
                        "--authorization", OWNER, "--scope", "/scope/figure-v2-revisions.md",
                        "--manifest", json.dumps(MANIFEST_V2), "--add-limits",
                        json.dumps({"work_dispatches": 2}), "--note", "owner: unlock this and proceed"]) == 0
        # identical replay through the CLI: idempotent, exit 0
        assert cli.run(["--root", dc, "exec-owner-request", "t1", "--request-id", "owner-req-1",
                        "--authorization", OWNER, "--scope", "/scope/figure-v2-revisions.md",
                        "--manifest", json.dumps(MANIFEST_V2), "--add-limits",
                        json.dumps({"work_dispatches": 2}), "--note", "owner: unlock this and proceed"]) == 0
        # conflicting replay through the CLI: non-zero
        assert cli.run(["--root", dc, "exec-owner-request", "t1", "--request-id", "owner-req-1",
                        "--authorization", OWNER, "--scope", "/scope/figure-v2-revisions.md",
                        "--add-limits", json.dumps({"work_dispatches": 9})]) != 0
        os.environ["MYCELIUM_COORD_DIR"] = dm
        ms._em.cache_clear(); ms._jm.cache_clear()
        try:
            r = asyncio.run(ms.execution_owner_request(
                "t1", request_id="owner-req-1", authorization_ref=OWNER,
                scope_ref="/scope/figure-v2-revisions.md", acceptance_manifest=MANIFEST_V2,
                add_limits={"work_dispatches": 2}, note="owner: unlock this and proceed"))
            assert r["result"]["run"] == 2 if "result" in r else r["run"] == 2
        finally:
            os.environ.pop("MYCELIUM_COORD_DIR", None)
            ms._em.cache_clear(); ms._jm.cache_clear()
        a = ExecutionManager(CoordStore(dc)).read_execution("t1")
        b = ExecutionManager(CoordStore(dm)).read_execution("t1")
        volatile = {"updated_at", "opened_at", "run_opened_at", "at"}

        def strip(o):
            if isinstance(o, dict):
                return {k: strip(v) for k, v in o.items()
                        if k not in volatile and not k.endswith("_at") and k != "fingerprint"}
            if isinstance(o, list):
                return [strip(x) for x in o]
            return o
        assert strip(a) == strip(b)
        assert a["run"] == 2 and len(a["runs"]) == 1 and a["limits"]["work_dispatches"] == b["limits"]["work_dispatches"]


def test_generated_context_distinguishes_stopped_run_from_stopped_conversation(tmp_path):
    """Brief item 3: a completed run's context says the RUN cannot autonomously continue, keeps
    discussion possible, and names the owner-request path; after the request it reports run 2."""
    import importlib.util
    import subprocess
    from mycelium_coord.coord import Coordinator
    from mycelium_coord.views import execution_view
    hook = Path(__file__).resolve().parents[1] / "hooks" / "_coord_context.py"
    spec = importlib.util.spec_from_file_location("_coord_context", hook)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    em = _mgr(tmp_path)
    _complete(em)
    st = em.status("t1")
    view = execution_view(st)
    assert view["run"] == 1 and view["completion_ref"] == "/e/receipt-v1.md" and view["shutdown_pending"] is True
    ctx = mod.render({"execution": view, "stop_waiting": stops_wait(st)}, "t1", "claude-executor") \
        if hasattr(mod, "render") else None
    if ctx is None:
        r = subprocess.run([sys.executable, str(hook), "t1", "claude-executor"],
                           input=json.dumps({"execution": view, "stop_waiting": stops_wait(st)}),
                           text=True, capture_output=True)
        assert r.returncode == 0, r.stderr
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "STOP (autonomous work): run 1 of this execution is completed (completion receipt /e/receipt-v1.md)" in ctx
    assert "STILL PERMITTED in this conversation: read-only discussion" in ctx
    assert "exec-owner-request" in ctx and "execution_owner_request" in ctx and "opens run 2" in ctx
    assert "do not open a new Codex/Mycelium task" in ctx
    assert "questions, reviews, peer summaries, stale checkpoints or an agent-authored reference never restart work" in ctx
    # after the owner request: the fresh view is run 2, not stopped
    _followup(em)
    st2 = em.status("t1")
    v2 = execution_view(st2)
    assert v2["run"] == 2 and v2["runs_archived"] == 1 and v2["completion_ref"] is None
    assert v2["last_owner_request"]["request_id"] == "owner-req-1" and stops_wait(st2) is False
