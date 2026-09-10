#!/usr/bin/env python3
"""Deterministic state tests for the SporeDrive execution core (ExecutionManager).

These answer the PLAN section-7 acceptance cases that are properties of the core record itself
(the CLI/MCP surfaces call the same functions, and Change B wires dispatch/deadline enforcement):

  * frozen-acceptance coverage -> automatic closure; extra work refused; complete once
  * limits are policy-configured and cannot be self-raised (only lowered, or raised via the
    authorization-linked change path)
  * missing acceptance evidence cannot evade the shared work-dispatch allowance
  * demonstrated required failure permits a bounded repair; unresolved status is retained
  * retry/reconnect/compaction preserve identity, usage, pause and accepted evidence; no dup reserve
  * exactly one reservation wins the last allowance under the shared lock
  * pause/expiry block new dispatch while reconciliation/read stay possible
  * a closed/completed task reads/records safely and refuses new work; backlog is non-actionable
  * an implementation change inside a reserved acceptance batch blocks calls and invalidates the
    attempt (no blended passing cycle)
  * a legacy/unsupported policy requires migration before managed mutation but allows read/status

Run: python3 -m pytest coordination/tests/test_execution_core.py
"""

from __future__ import annotations

import os

import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import ProtocolError  # noqa: E402
from mycelium_coord.execution import (  # noqa: E402
    ExecutionManager,
    STATUS_ACTIVE,
    STATUS_COMPLETED,
    STATUS_DRAINING,
    STATUS_EXHAUSTED,
    PHASE_ACCEPTANCE,
    PHASE_CLOSURE,
)
from mycelium_coord.store import CoordStore  # noqa: E402

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


# --------------------------------------------------------------------------- open / identity
def test_open_idempotent_and_identity_conflict():
    with _tmp() as d:
        m = _mgr(d)
        r1 = m.open_execution(
            "t1", execution_id="e1", scope_ref=SCOPE, authorization_ref=AUTH
        )
        assert r1["idempotent"] is False
        assert r1["execution"]["status"] == STATUS_ACTIVE
        assert r1["execution"]["state_version"] == 1
        # same execution_id -> idempotent
        r2 = m.open_execution(
            "t1", execution_id="e1", scope_ref=SCOPE, authorization_ref=AUTH
        )
        assert r2["idempotent"] is True
        # a different execution_id on the same task is a conflict (a new execution is a new task)
        with pytest.raises(ProtocolError) as ei:
            m.open_execution(
                "t1", execution_id="e2", scope_ref=SCOPE, authorization_ref=AUTH
            )
        assert ei.value.code == "execution_identity_conflict"


def test_open_requires_authorization():
    with _tmp() as d:
        m = _mgr(d)
        with pytest.raises(ProtocolError) as ei:
            m.open_execution(
                "t1", execution_id="e1", scope_ref=SCOPE, authorization_ref=""
            )
        assert ei.value.code == "missing_authorization"


def test_open_override_may_lower_not_raise():
    with _tmp() as d:
        m = _mgr(d)
        # lowering the shared work-dispatch allowance is allowed
        rec = _open(m, "low", overrides={"work_dispatches": 2})
        assert rec["limits"]["work_dispatches"] == 2
        # raising an initial supervisor-abuse limit is refused without the authorized change path
        with pytest.raises(ProtocolError) as ei:
            _open(m, "high", overrides={"work_dispatches": 99})
        assert ei.value.code == "limit_increase_requires_authorization"


def test_open_may_set_task_specific_call_allowances():
    with _tmp() as d:
        m = _mgr(d)
        rec = _open(m, "study", overrides={"technical_calls": 3, "acceptance_calls": 2})
        assert rec["limits"]["technical_calls"] == 3
        assert rec["limits"]["acceptance_calls"] == 2


# --------------------------------------------------------------------------- reserve / settle
def test_reserve_charges_and_is_idempotent():
    with _tmp() as d:
        m = _mgr(d)
        _open(m)
        r = m.reserve("t1", action_id="a1", kind="work_dispatch")
        assert r["idempotent"] is False
        assert r["execution"]["usage"]["work_dispatches"] == 1
        # replay of the same action_id never double-charges
        r2 = m.reserve("t1", action_id="a1", kind="work_dispatch")
        assert r2["idempotent"] is True
        assert m.status("t1")["usage"]["work_dispatches"] == 1


def test_review_launch_consumes_both_allowances():
    with _tmp() as d:
        m = _mgr(d)
        _open(m)
        r = m.reserve("t1", action_id="rev1", kind="review_launch")
        u = r["execution"]["usage"]
        assert u["work_dispatches"] == 1 and u["review_launches"] == 1


def test_settle_never_refunds_uncertain_stays_charged():
    with _tmp() as d:
        m = _mgr(d)
        _open(m)
        m.reserve("t1", action_id="a1", kind="work_dispatch")
        m.settle("t1", action_id="a1", outcome="unknown")
        # still charged; a later settle can update the outcome but never refunds
        assert m.status("t1")["usage"]["work_dispatches"] == 1
        m.settle("t1", action_id="a1", outcome="success", evidence_ref="/e")
        assert m.status("t1")["usage"]["work_dispatches"] == 1


def test_two_concurrent_requests_only_one_wins_last_allowance():
    with _tmp() as d:
        m = _mgr(d)
        _open(m, "race", overrides={"work_dispatches": 1})
        results = {}

        def grab(name):
            try:
                m.reserve("race", action_id=name, kind="work_dispatch")
                results[name] = "ok"
            except ProtocolError as e:
                results[name] = e.code

        threads = [threading.Thread(target=grab, args=(n,)) for n in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        oks = [k for k, v in results.items() if v == "ok"]
        exhausted = [k for k, v in results.items() if v == "work_dispatch_exhausted"]
        assert len(oks) == 1 and len(exhausted) == 1
        assert m.status("race")["usage"]["work_dispatches"] == 1
        assert m.status("race")["status"] == STATUS_EXHAUSTED


def test_missing_evidence_cannot_evade_work_limit():
    # No acceptance attestation is ever submitted; the supervisor keeps asking for more work.
    with _tmp() as d:
        m = _mgr(
            d,
        )
        _open(
            m,
            "t1",
            manifest={"c1": {"description": "required check"}},
            overrides={"work_dispatches": 3},
        )
        for i in range(3):
            m.reserve("t1", action_id=f"a{i}", kind="work_dispatch")
        with pytest.raises(ProtocolError) as ei:
            m.reserve("t1", action_id="a3", kind="work_dispatch")
        assert ei.value.code == "work_dispatch_exhausted"
        # criterion never accepted -> never closed; missing evidence did not evade the limit
        s = m.status("t1")
        assert s["status"] == STATUS_EXHAUSTED
        assert s["coverage"]["complete"] is False


# --------------------------------------------------------------------------- closure
def test_all_criteria_accepted_auto_close_refuse_extra_complete_once():
    with _tmp() as d:
        m = _mgr(d)
        _open(
            m,
            "t1",
            manifest={
                "impl": {"description": "implementation"},
                "review": {"kind": "review", "description": "required review"},
            },
        )
        m.record_evidence(
            "t1", criterion_id="impl", evidence_ref="/impl", accepted_by="claude"
        )
        # not closed yet: the required review criterion is still open
        assert m.status("t1")["status"] == STATUS_ACTIVE
        res = m.record_evidence(
            "t1", criterion_id="review", evidence_ref="/rev", accepted_by="codex"
        )
        assert res["closed"] is True
        s = m.status("t1")
        assert s["status"] == STATUS_COMPLETED and s["phase"] == PHASE_CLOSURE
        # a late extra work request is refused
        with pytest.raises(ProtocolError) as ei:
            m.reserve("t1", action_id="late", kind="work_dispatch")
        assert ei.value.code == "execution_closed"
        # the optional wording issue is recorded as non-actionable backlog
        b = m.add_backlog("t1", item="rename a heading", source="codex")
        assert b["backlog_count"] == 1
        assert m.read_execution("t1")["backlog"][0]["actionable"] is False
        # complete once: re-recording an accepted criterion does not re-close or add scope
        again = m.record_evidence("t1", criterion_id="impl", evidence_ref="/impl")
        assert again["closed"] is True
        assert m.status("t1")["status"] == STATUS_COMPLETED


def test_empty_manifest_never_auto_closes():
    with _tmp() as d:
        m = _mgr(d)
        _open(m)  # no manifest
        assert m.status("t1")["coverage"] == {
            "required": 0,
            "accepted": 0,
            "complete": False,
        }
        # a work reservation does not trigger closure with nothing frozen to satisfy
        m.reserve("t1", action_id="a1", kind="work_dispatch")
        assert m.status("t1")["status"] == STATUS_ACTIVE


def test_record_evidence_unknown_criterion_and_missing_evidence_refused():
    with _tmp() as d:
        m = _mgr(d)
        _open(m, "t1", manifest={"c1": {"description": "x"}})
        with pytest.raises(ProtocolError) as e1:
            m.record_evidence("t1", criterion_id="nope", evidence_ref="/e")
        assert e1.value.code == "unknown_criterion"
        with pytest.raises(ProtocolError) as e2:
            m.record_evidence("t1", criterion_id="c1", evidence_ref="")
        assert e2.value.code == "missing_evidence"


# --------------------------------------------------------------------------- blockers / repair
def test_demonstrated_failure_permits_repair_then_retains_unresolved():
    with _tmp() as d:
        m = _mgr(d)
        _open(
            m,
            "t1",
            manifest={"c1": {"kind": "check", "description": "behaviour"}},
            overrides={"work_dispatches": 1},
        )
        # a demonstrated required failure opens an evidenced blocker against the criterion
        m.open_blocker("t1", blocker_id="b1", criterion_id="c1", evidence_ref="/repro")
        # a repair is permitted while allowance remains (this consumes the last dispatch)
        m.reserve("t1", action_id="fix1", kind="repair", repair_blocker_id="b1")
        assert m.status("t1")["usage"]["work_dispatches"] == 1
        # allowance now spent: a further repair is refused and the blocker remains unresolved
        with pytest.raises(ProtocolError) as ei:
            m.reserve("t1", action_id="fix2", kind="repair", repair_blocker_id="b1")
        assert ei.value.code == "work_dispatch_exhausted"
        assert [b["blocker_id"] for b in m.status("t1")["blockers"]] == ["b1"]


def test_blocker_reopens_only_affected_criterion_after_closure():
    with _tmp() as d:
        m = _mgr(d)
        _open(
            m, "t1", manifest={"c1": {"description": "a"}, "c2": {"description": "b"}}
        )
        m.record_evidence("t1", criterion_id="c1", evidence_ref="/1")
        m.record_evidence("t1", criterion_id="c2", evidence_ref="/2")
        assert m.status("t1")["status"] == STATUS_COMPLETED
        # a late evidenced blocker reopens ONLY c1 for a bounded repair; usage/counters unchanged
        m.open_blocker(
            "t1", blocker_id="b1", criterion_id="c1", evidence_ref="/regression"
        )
        rec = m.read_execution("t1")
        assert rec["status"] == STATUS_ACTIVE
        assert rec["acceptance_manifest"]["c1"]["accepted"] is False
        assert rec["acceptance_manifest"]["c2"]["accepted"] is True
        # a bounded repair linked to that open blocker is now permitted
        m.reserve("t1", action_id="fix", kind="repair", repair_blocker_id="b1")
        # re-accepting c1 re-closes
        m.record_evidence("t1", criterion_id="c1", evidence_ref="/fixed")
        assert m.status("t1")["status"] == STATUS_COMPLETED


# --------------------------------------------------------------------------- reconnect / persistence
def test_reconnect_preserves_state_and_no_duplicate_reservation():
    with _tmp() as d:
        m1 = _mgr(d)
        _open(m1, "t1", manifest={"c1": {"description": "x"}})
        m1.reserve("t1", action_id="a1", kind="work_dispatch")
        m1.record_evidence("t1", criterion_id="c1", evidence_ref="/e")  # closes
        # a fresh manager instance (compaction/reconnect) reads the same durable record
        m2 = _mgr(d)
        s = m2.status("t1")
        assert s["usage"]["work_dispatches"] == 1
        assert s["status"] == STATUS_COMPLETED
        # replaying the same reservation id is idempotent -> no duplicate reservation
        r = m2.reserve("t1", action_id="a1", kind="work_dispatch")
        assert r["idempotent"] is True
        assert m2.status("t1")["usage"]["work_dispatches"] == 1


def test_state_version_conflict_on_stale_write():
    with _tmp() as d:
        m = _mgr(d)
        _open(m)
        sv = m.status("t1")["state_version"]
        m.reserve("t1", action_id="a1", kind="work_dispatch")  # bumps state_version
        with pytest.raises(ProtocolError) as ei:
            m.reserve(
                "t1", action_id="a2", kind="work_dispatch", expected_state_version=sv
            )
        assert ei.value.code == "state_version_conflict"


# --------------------------------------------------------------------------- pause / expiry
def test_pause_blocks_new_work_but_settle_and_read_still_work():
    with _tmp() as d:
        m = _mgr(d)
        _open(m)
        m.reserve("t1", action_id="a1", kind="work_dispatch")
        # open reservation in flight -> pause drains
        m.pause("t1", authorization_ref=AUTH, reason="user stop")
        assert m.status("t1")["status"] == STATUS_DRAINING
        # no NOT-yet-dispatched work may begin
        with pytest.raises(ProtocolError) as ei:
            m.reserve("t1", action_id="a2", kind="work_dispatch")
        assert ei.value.code == "execution_paused"
        # already-accepted work is still reconciled; read still works
        m.settle("t1", action_id="a1", outcome="success", evidence_ref="/e")
        assert (
            m.read_execution("t1")["usage"]["reservations"]["a1"]["status"] == "settled"
        )
        # unpause requires authorization then restores active
        with pytest.raises(ProtocolError):
            m.unpause("t1", authorization_ref="")
        m.unpause("t1", authorization_ref=AUTH)
        assert m.status("t1")["status"] == STATUS_ACTIVE


def test_expiry_blocks_new_work():
    with _tmp() as d:
        m = _mgr(d)
        _open(m, "t1", expires_at="2000-01-01T00:00:00+00:00")
        assert m.status("t1")["expired"] is True
        with pytest.raises(ProtocolError) as ei:
            m.reserve("t1", action_id="a1", kind="work_dispatch")
        assert ei.value.code == "execution_expired"


# --------------------------------------------------------------------------- completed / closed safety
def test_completed_rejects_work_via_reattach_but_backlog_is_nonactionable():
    with _tmp() as d:
        m = _mgr(d)
        _open(m, "t1", manifest={"c1": {"description": "x"}})
        m.record_evidence("t1", criterion_id="c1", evidence_ref="/e")
        # reattach via a fresh manager; a completed task refuses new work
        m2 = _mgr(d)
        with pytest.raises(ProtocolError) as ei:
            m2.reserve("t1", action_id="new", kind="work_dispatch")
        assert ei.value.code == "execution_closed"
        # non-actionable backlog text cannot generate a work reservation
        m2.add_backlog("t1", item="please also do X", source="supervisor")
        assert all(e["actionable"] is False for e in m2.read_execution("t1")["backlog"])
        # reading/recording remains safe without reopening
        assert m2.status("t1")["status"] == STATUS_COMPLETED


# --------------------------------------------------------------------------- adapter freeze identity
def test_acceptance_freeze_mismatch_blocks_and_invalidates_attempt():
    with _tmp() as d:
        m = _mgr(d)
        # two criteria so the batch is still mid-flight (not auto-closed) when the build changes
        _open(
            m,
            "t1",
            manifest={
                "acc": {"kind": "acceptance", "description": "fresh acceptance"},
                "final": {
                    "kind": "acceptance",
                    "description": "second acceptance check",
                },
            },
            overrides={"acceptance_calls": 4},
        )
        m.set_phase("t1", phase=PHASE_ACCEPTANCE)
        # first acceptance call fixes the freeze identity, then one criterion is accepted under it
        m.reserve_call(
            "t1", action_id="c1", phase=PHASE_ACCEPTANCE, freeze_identity="build-AAA"
        )
        m.settle_call("t1", action_id="c1", freeze_identity="build-AAA", outcome="ok")
        m.record_evidence("t1", criterion_id="acc", evidence_ref="/acc-under-AAA")
        assert m.read_execution("t1")["acceptance_manifest"]["acc"]["accepted"] is True
        assert (
            m.status("t1")["status"] == STATUS_ACTIVE
        )  # 'final' still open, not closed
        # the implementation changes mid-batch -> a new freeze identity blocks remaining calls
        with pytest.raises(ProtocolError) as ei:
            m.reserve_call(
                "t1",
                action_id="c2",
                phase=PHASE_ACCEPTANCE,
                freeze_identity="build-BBB",
            )
        assert ei.value.code == "freeze_identity_mismatch"
        # and the criterion proven under the stale build is invalidated (no blended passing cycle)
        rec = m.read_execution("t1")
        assert rec["acceptance_manifest"]["acc"]["accepted"] is False
        assert rec["acceptance_freeze"] is None
        assert len(rec["acceptance_invalidations"]) == 1


def test_settle_call_freeze_mismatch_invalidates_and_stays_charged():
    with _tmp() as d:
        m = _mgr(d)
        _open(m, "t1", overrides={"acceptance_calls": 2})
        m.set_phase("t1", phase=PHASE_ACCEPTANCE)
        m.reserve_call(
            "t1", action_id="c1", phase=PHASE_ACCEPTANCE, freeze_identity="AAA"
        )
        with pytest.raises(ProtocolError) as ei:
            m.settle_call("t1", action_id="c1", freeze_identity="BBB", outcome="ok")
        assert ei.value.code == "freeze_identity_mismatch"
        # the call stays charged (unknown/mismatched outcomes are not refunded)
        assert m.status("t1")["usage"]["acceptance_calls"] == 1
        assert (
            m.read_execution("t1")["usage"]["reservations"]["c1"]["status"]
            == "invalidated"
        )


def test_reserve_call_requires_configured_allowance_and_freeze_identity():
    with _tmp() as d:
        m = _mgr(d)
        _open(m)  # technical_calls/acceptance_calls default null
        with pytest.raises(ProtocolError) as e1:
            m.reserve_call(
                "t1", action_id="c1", phase=PHASE_ACCEPTANCE, freeze_identity="AAA"
            )
        assert e1.value.code == "no_call_allowance"
        _open(m, "t2", overrides={"acceptance_calls": 1})
        m.set_phase("t2", phase=PHASE_ACCEPTANCE)
        with pytest.raises(ProtocolError) as e2:
            m.reserve_call(
                "t2", action_id="c1", phase=PHASE_ACCEPTANCE, freeze_identity=""
            )
        assert e2.value.code == "missing_freeze_identity"


# --------------------------------------------------------------------------- change_limits
def test_change_limits_requires_auth_and_lifts_exhausted_without_altering_usage():
    with _tmp() as d:
        m = _mgr(d)
        _open(m, "t1", overrides={"work_dispatches": 1})
        m.reserve("t1", action_id="a1", kind="work_dispatch")
        with pytest.raises(ProtocolError):
            m.reserve("t1", action_id="a2", kind="work_dispatch")
        assert m.status("t1")["status"] == STATUS_EXHAUSTED
        # raising requires an authorization_ref
        with pytest.raises(ProtocolError) as ei:
            m.change_limits("t1", authorization_ref="", changes={"work_dispatches": 3})
        assert ei.value.code == "missing_authorization"
        # authorized raise lifts exhaustion; PAST USAGE unchanged
        m.change_limits(
            "t1", authorization_ref="/auth/extend.md", changes={"work_dispatches": 3}
        )
        s = m.status("t1")
        assert s["status"] == STATUS_ACTIVE
        assert s["usage"]["work_dispatches"] == 1  # not reset
        m.reserve("t1", action_id="a2", kind="work_dispatch")
        assert m.status("t1")["usage"]["work_dispatches"] == 2


# --------------------------------------------------------------------------- legacy / migration
def test_legacy_policy_requires_migration_but_allows_read():
    with _tmp() as d:
        m = _mgr(d)
        _open(m)
        # simulate a record written under a future/unsupported policy version
        store = m.store
        rel = "tasks/t1/execution.json"
        rec = store.read(rel)
        rec["policy_version"] = 99
        store.write(rel, rec)
        # managed mutation is refused with a clear migration message
        with pytest.raises(ProtocolError) as ei:
            m.reserve("t1", action_id="a1", kind="work_dispatch")
        assert ei.value.code == "policy_migration_required"
        # bounded read/status remains available
        assert m.read_execution("t1")["policy_version"] == 99
        assert m.status("t1")["policy_version"] == 99


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------- CLI / MCP parity
def _strip_volatile(obj):
    """Drop wall-clock fields so two runs of the same sequence compare structurally."""
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items()
                if not (k == "at" or k.endswith("_at"))}
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj


def test_cli_and_mcp_make_identical_state():
    """PLAN case 14: the native CLI and the MCP surface run the same operations through the same core
    and persist identical state (modulo wall-clock)."""
    import asyncio
    import json as _json

    from mycelium_coord import cli
    from mycelium_coord import mcp_server as ms

    manifest = {"impl": {"description": "implementation"},
                "review": {"kind": "review", "description": "required review"}}

    with _tmp() as dc, _tmp() as dm:
        # --- drive the sequence through the CLI ---
        def cli_run(*a):
            assert cli.run(["--root", dc, *a]) == 0

        cli_run("exec-open", "t1", "--execution-id", "e1", "--scope", SCOPE,
                "--authorization", AUTH, "--manifest", _json.dumps(manifest),
                "--limits", _json.dumps({"work_dispatches": 3}))
        cli_run("exec-reserve", "t1", "--action-id", "a1", "--kind", "work_dispatch")
        cli_run("exec-settle", "t1", "--action-id", "a1", "--outcome", "success", "--evidence", "/e")
        cli_run("exec-record-evidence", "t1", "--criterion", "impl", "--evidence", "/impl")
        cli_run("exec-backlog", "t1", "--item", "optional polish", "--source", "codex")

        # --- drive the identical sequence through the MCP tools (same core) ---
        os.environ["MYCELIUM_COORD_DIR"] = dm
        ms._em.cache_clear()
        try:
            asyncio.run(ms.execution_open("t1", "e1", SCOPE, AUTH, acceptance_manifest=manifest,
                                          limits_overrides={"work_dispatches": 3}))
            asyncio.run(ms.execution_reserve("t1", "a1", "work_dispatch"))
            asyncio.run(ms.execution_settle("t1", "a1", "success", evidence_ref="/e"))
            asyncio.run(ms.execution_record_evidence("t1", "impl", "/impl"))
            asyncio.run(ms.execution_backlog("t1", "optional polish", source="codex"))
        finally:
            os.environ.pop("MYCELIUM_COORD_DIR", None)
            ms._em.cache_clear()

        rec_cli = _json.loads((Path(dc) / "tasks/t1/execution.json").read_text())
        rec_mcp = _json.loads((Path(dm) / "tasks/t1/execution.json").read_text())
        assert _strip_volatile(rec_cli) == _strip_volatile(rec_mcp)
        # sanity: both are mid-flight (review criterion still open), one dispatch spent, backlog noted
        assert rec_cli["status"] == STATUS_ACTIVE
        assert rec_cli["usage"]["work_dispatches"] == 1
        assert rec_cli["backlog"][0]["actionable"] is False
