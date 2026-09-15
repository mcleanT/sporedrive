"""Owner-authorized recovery (request-reduction v1, item 6).

Run: python3 -m pytest coordination/tests/test_owner_recovery.py -q
"""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord.execution import ExecutionManager  # noqa: E402
from mycelium_coord.model import ProtocolError  # noqa: E402
from mycelium_coord.store import CoordStore  # noqa: E402

from test_execution_core import _open  # noqa: E402


def _mgr(tmp_path):
    return ExecutionManager(CoordStore(str(tmp_path)))


def test_unpause_records_amendment_preserves_usage_and_reports_exhausted(tmp_path):
    em = _mgr(tmp_path)
    _open(em, "t1", overrides={"work_dispatches": 1})
    em.reserve("t1", action_id="a1", kind="work_dispatch")
    em.pause("t1", authorization_ref="owner:pause")
    r = em.unpause("t1", authorization_ref="owner:continue", scope_amendment="owner extended scope")
    ex = r["execution"]
    assert ex["status"] == "exhausted"  # allowance already spent: truthful, not 'active'
    assert ex["usage"]["work_dispatches"] == 1  # past usage untouched
    rec = em.read_execution("t1")
    assert rec["scope_amendments"][-1]["via"] == "unpause"
    assert rec["scope_amendments"][-1]["note"] == "owner extended scope"
    assert ex["authorization"]["last_recovery"]["authorization_ref"] == "owner:continue"
    r2 = em.change_limits("t1", authorization_ref="owner:raise", changes={"work_dispatches": 3},
                          scope_amendment="one more dispatch")
    assert r2["execution"]["status"] == "active"
    assert len(em.read_execution("t1")["scope_amendments"]) == 2
    assert em.read_execution("t1")["usage"]["work_dispatches"] == 1


def test_unpause_refuses_expired_until_owner_extends(tmp_path):
    em = _mgr(tmp_path)
    past = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    _open(em, "t1", expires_at=past)
    em.pause("t1", authorization_ref="owner:pause")
    import time
    time.sleep(1.2)
    with pytest.raises(ProtocolError) as ei:
        em.unpause("t1", authorization_ref="owner:continue")
    assert ei.value.code == "execution_expired"
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    em.change_limits("t1", authorization_ref="owner:extend", changes={"expires_at": future})
    assert em.unpause("t1", authorization_ref="owner:continue")["execution"]["status"] == "active"


def test_recovery_never_touches_frozen_acceptance(tmp_path):
    em = _mgr(tmp_path)
    _open(em, "t1")
    before = em.read_execution("t1")
    frozen = copy.deepcopy({k: before.get(k) for k in ("acceptance_manifest", "accepted_evidence",
                                                        "acceptance_freeze")})
    em.change_limits("t1", authorization_ref="owner:raise", changes={"technical_calls": 9})
    after = em.read_execution("t1")
    assert {k: after.get(k) for k in frozen} == frozen
    assert not em.unpause.__doc__ is None
    with pytest.raises(ProtocolError):
        em.unpause("t1", authorization_ref="")


def test_r4_expired_and_paused_context_names_the_permitted_recovery(tmp_path):
    """R4: the startup context for an expired AND paused execution must state the permitted
    owner-authorized recovery explicitly (no confirmation deadlock), while keeping STOP."""
    import json
    import subprocess
    import time
    hook = Path(__file__).resolve().parents[1] / "hooks" / "_coord_context.py"
    em = _mgr(tmp_path)
    soon = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    _open(em, "t1", expires_at=soon)
    em.pause("t1", authorization_ref="owner:pause")
    time.sleep(1.2)
    from mycelium_coord.views import execution_view
    status = execution_view(em.status("t1"))
    assert status["expired"] is True and status["status"] == "paused"
    packet = {"task": {"task_id": "t1"}, "execution": status, "stop_waiting": True,
              "pending_unacked": [], "pending_count": 0}
    out = subprocess.run([sys.executable, str(hook), "t1", "cl"], input=json.dumps(packet), text=True,
                         capture_output=True, check=True).stdout
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "STOP: execution expired and paused" in ctx
    assert "PERMITTED RECOVERY" in ctx and "exec-change-limits" in ctx and "exec-unpause" in ctx
    assert "agent-authored reference never creates authority" in ctx
    # a recorded recovery is surfaced so the agent does not ask again
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    em.change_limits("t1", authorization_ref="owner:extend", changes={"expires_at": future})
    em.unpause("t1", authorization_ref="owner:extend")
    status2 = execution_view(em.status("t1"))
    packet2 = dict(packet, execution=status2, stop_waiting=False)
    out2 = subprocess.run([sys.executable, str(hook), "t1", "cl"], input=json.dumps(packet2), text=True,
                          capture_output=True, check=True).stdout
    ctx2 = json.loads(out2)["hookSpecificOutput"]["additionalContext"]
    assert "Owner-authorized recovery already recorded: unpause" in ctx2 and "STOP:" not in ctx2
