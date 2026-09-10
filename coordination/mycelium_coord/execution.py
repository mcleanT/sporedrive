"""execution.py — one persistent, versioned, opt-in execution record per managed task.

This is SporeDrive's stopping/permission layer (PLAN sections 2-4). It is deliberately built ON TOP
of the existing coordination primitives rather than beside them:

* storage, atomic temp+rename writes, corruption-vs-absence distinction and the re-entrant per-task
  advisory ``flock`` all come from :class:`~mycelium_coord.store.CoordStore`;
* rejections are :class:`~mycelium_coord.model.ProtocolError` so both hosts see one error contract;
* message *delivery* state (persisted/delivered/acknowledged/completed) stays entirely in
  ``coord.py``. This module is about *permission to execute a task*, which is a distinct axis: a
  delivered progress message never becomes authority to spend a new work dispatch.

Design invariants (measurement-integrity + Fable F1-F6):

* Limits live in a maintained, versioned policy config (``execution_policy.json``), never in a
  model-composed brief. An open override may only *lower* the supervisor-abuse limits
  (work/review dispatches); *raising* an initial limit, extending it later, enabling it after open,
  or unpausing all require the user-authorization-linked ``change_limits``/``unpause`` path.
* Every work-producing action reserves the shared work-dispatch allowance atomically under the
  per-task lock with a stable ``action_id`` before dispatch; a replay of that id is idempotent and
  never double-charges; an uncertain action stays charged until reconciled (no blind refund/retry).
* The frozen acceptance manifest enumerates ALL required deliverables/checks/reviews. On each
  evidence settlement coverage is recomputed; once every required criterion is accepted the record
  enters closure automatically in the SAME conditional write. Missing/unknown evidence is never a
  pass. Completed is terminal for that execution.
* Reading, recording evidence, acknowledging, reporting completion and safe stopping remain possible
  after pause/exhaustion/closure — these are bounded closure operations, not new analysis.
* Adapter calls (technical-debug / frozen-acceptance) reserve against their own task-specific
  allowance with a freeze identity the ADAPTER computes from the actual implementation/config. An
  implementation change mid-batch (a new freeze identity) blocks remaining dispatch and invalidates
  the current acceptance attempt so old/new responses cannot be blended into a passing cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os

from .model import ProtocolError
from .store import CoordStore, require_id, utcnow

EXEC_SCHEMA_VERSION = "1.0.0"
SUPPORTED_POLICY_VERSIONS = (1,)

# user-facing execution status (distinct from message delivery state, and never a falsy value)
STATUS_ACTIVE = "active"
STATUS_WAITING = "waiting"
STATUS_DRAINING = "draining"  # pause requested; identified owned work still in flight
STATUS_PAUSED = "paused"
STATUS_EXHAUSTED = "exhausted"  # a work-dispatch allowance is spent; new work refused
STATUS_COMPLETED = "completed"  # coverage complete OR final receipt written; terminal
STATUSES = (
    STATUS_ACTIVE,
    STATUS_WAITING,
    STATUS_DRAINING,
    STATUS_PAUSED,
    STATUS_EXHAUSTED,
    STATUS_COMPLETED,
)

PHASE_IMPLEMENTATION = "implementation"
PHASE_TECHNICAL_DEBUG = "technical_debug"
PHASE_ACCEPTANCE = "acceptance"
PHASE_EXECUTION = "execution"
PHASE_CLOSURE = "closure"
PHASES = (
    PHASE_IMPLEMENTATION,
    PHASE_TECHNICAL_DEBUG,
    PHASE_ACCEPTANCE,
    PHASE_EXECUTION,
    PHASE_CLOSURE,
)

# reservation kinds that consume the shared work-dispatch allowance
WORK_KINDS = ("work_dispatch", "review_launch", "repair")

# adapter call phases -> the task-specific allowance field each consumes
CALL_PHASES = {
    PHASE_TECHNICAL_DEBUG: "technical_calls",
    PHASE_ACCEPTANCE: "acceptance_calls",
}

_DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "execution_policy.json"


def _load_policy(path=None) -> dict:
    p = Path(
        path or os.environ.get("SPOREDRIVE_EXECUTION_POLICY") or _DEFAULT_POLICY_PATH
    )
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        raise ProtocolError(
            "policy_config_missing",
            str(p),
            "maintained execution policy config not found",
        )
    except ValueError as e:
        raise ProtocolError(
            "policy_config_corrupt", str(p), f"unparseable policy config: {e}"
        )
    if not isinstance(data, dict) or "defaults" not in data:
        raise ProtocolError(
            "policy_config_corrupt", str(p), "policy config missing 'defaults'"
        )
    return data


class ExecutionManager:
    """The single core both the CLI and the MCP surface call, so the two hosts make identical gate
    decisions and persist identical state. Names here are an implementation choice; the *behaviour*
    is the contract."""

    # supervisor-abuse limits: an open override may only LOWER these, never raise them.
    LOWER_ONLY = ("work_dispatches", "review_launches", "review_deadline_seconds")
    # task-specific config an authorized open may set (default null == not applicable).
    TASK_SETTABLE = ("technical_calls", "acceptance_calls", "expires_at")

    def __init__(self, store: CoordStore | None = None, *, policy_path=None):
        self.store = store or CoordStore()
        self._policy_path = policy_path

    # ------------------------------------------------------------------ paths
    def _exec_rel(self, task_id: str) -> str:
        return f"tasks/{require_id(task_id, 'task_id')}/execution.json"

    def _audit_rel(self, task_id: str) -> str:
        return f"tasks/{task_id}/execution_audit.jsonl"

    # ------------------------------------------------------------------ read (always allowed)
    def read_execution(self, task_id: str):
        return self.store.read(self._exec_rel(task_id))

    def status(self, task_id: str):
        rec = self.read_execution(task_id)
        return None if rec is None else self._summary(rec)

    def dispatch_check(self, task_id: str, action_id: str) -> dict:
        """Read-only pre-dispatch gate: is ``action_id`` an OPEN reservation on a currently
        dispatchable execution? A managed transport (bridge notify / coord work-dispatch) calls this
        immediately before it sends, so a request that was queued before a newer pause/expiry cannot
        still fire. This is a FRESH read of durable state, deliberately not a lock held across the
        transport (a flock is never held across a network wait, store.py); a pause landing in the
        microseconds after this read but before the actual send is the documented residual window,
        not an atomicity guarantee this layer can make across cmux."""
        rec = self.read_execution(task_id)
        if rec is None:
            return {"ok": False, "reason": "execution_not_found"}
        if rec.get("policy_version") not in SUPPORTED_POLICY_VERSIONS:
            return {
                "ok": False,
                "reason": "policy_migration_required",
                "status": rec.get("status"),
            }
        if self._expired(rec):
            return {
                "ok": False,
                "reason": "execution_expired",
                "status": rec.get("status"),
            }
        status = rec.get("status")
        if status in (STATUS_PAUSED, STATUS_DRAINING):
            return {"ok": False, "reason": "execution_paused", "status": status}
        if status == STATUS_EXHAUSTED:
            return {"ok": False, "reason": "work_dispatch_exhausted", "status": status}
        if status == STATUS_COMPLETED or rec.get("phase") == PHASE_CLOSURE:
            return {"ok": False, "reason": "execution_closed", "status": status}
        res = rec.get("usage", {}).get("reservations", {}).get(action_id)
        if res is None:
            return {"ok": False, "reason": "unknown_reservation", "status": status}
        if res.get("status") != "reserved":
            return {
                "ok": False,
                "reason": "reservation_not_open",
                "status": status,
                "reservation_status": res.get("status"),
            }
        return {
            "ok": True,
            "reason": "dispatchable",
            "status": status,
            "state_version": rec.get("state_version"),
        }

    def _require(self, task_id: str) -> dict:
        rec = self.store.read(self._exec_rel(task_id))
        if rec is None:
            raise ProtocolError(
                "execution_not_found",
                task_id,
                "no execution record; open/attach before managed mutation",
            )
        return rec

    def _require_managed(self, task_id: str) -> dict:
        rec = self._require(task_id)
        pv = rec.get("policy_version")
        if pv not in SUPPORTED_POLICY_VERSIONS:
            raise ProtocolError(
                "policy_migration_required",
                task_id,
                f"policy_version {pv!r} unsupported; migrate before managed mutation "
                f"(bounded read-only/status remains available)",
            )
        return rec

    # ------------------------------------------------------------------ helpers
    def _write(self, task_id: str, rec: dict) -> dict:
        rec["state_version"] = int(rec.get("state_version", 0)) + 1
        rec["updated_at"] = utcnow()
        self.store.write(self._exec_rel(task_id), rec)
        return rec

    def _audit(self, task_id: str, kind: str, record: dict) -> None:
        self.store.append_receipt(
            self._audit_rel(task_id), kind, record, lock_name=task_id
        )

    @staticmethod
    def _check_version(rec: dict, expected) -> None:
        if expected is not None and int(rec["state_version"]) != int(expected):
            raise ProtocolError(
                "state_version_conflict",
                str(rec["state_version"]),
                f"expected state_version {expected}, found {rec['state_version']}",
            )

    @staticmethod
    def _expired(rec: dict) -> bool:
        exp = (rec.get("limits") or {}).get("expires_at")
        if not exp:
            return False
        try:
            dt = datetime.fromisoformat(exp)
        except (ValueError, TypeError):
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > dt

    def _summary(self, rec: dict) -> dict:
        limits = rec.get("limits", {})
        usage = rec.get("usage", {})
        manifest = rec.get("acceptance_manifest", {})
        total = len(manifest)
        accepted = sum(1 for c in manifest.values() if c.get("accepted"))
        reservations = usage.get("reservations", {})
        return {
            "execution_id": rec["execution_id"],
            "task_id": rec["task_id"],
            "status": rec["status"],
            "phase": rec["phase"],
            "state_version": rec["state_version"],
            "policy_version": rec["policy_version"],
            "limits": dict(limits),
            "usage": {
                k: usage.get(k, 0)
                for k in (
                    "work_dispatches",
                    "review_launches",
                    "technical_calls",
                    "acceptance_calls",
                )
            },
            "open_reservations": sorted(
                a for a, r in reservations.items() if r.get("status") == "reserved"
            ),
            "coverage": {
                "required": total,
                "accepted": accepted,
                "complete": total > 0 and accepted == total,
            },
            "expires_at": limits.get("expires_at"),
            "expired": self._expired(rec),
            "blockers": [
                b for b in rec.get("blockers", []) if b.get("status") == "open"
            ],
            "backlog_count": len(rec.get("backlog", [])),
        }

    def _freeze_manifest(self, manifest) -> dict:
        out = {}
        for cid, spec in (manifest or {}).items():
            require_id(cid, "criterion_id")
            spec = dict(spec or {})
            out[cid] = {
                "criterion_id": cid,
                "description": spec.get("description", ""),
                "evidence_requirements": spec.get("evidence_requirements", ""),
                # deliverable | check | review | release | acceptance
                "kind": spec.get("kind", "deliverable"),
                "accepted": False,
                "evidence_ref": None,
                "evidence_sha256": None,
                "attestation": None,
                "accepted_at": None,
                "accepted_by": None,
                "accepted_under_freeze": None,
            }
        return out

    def _apply_open_overrides(self, limits: dict, overrides: dict) -> dict:
        out = dict(limits)
        for k, v in overrides.items():
            if k in self.LOWER_ONLY:
                cur = out.get(k)
                if v is None or (cur is not None and int(v) > int(cur)):
                    raise ProtocolError(
                        "limit_increase_requires_authorization",
                        k,
                        f"open override may only lower {k} (policy default {cur}); "
                        f"raise it via change_limits with an authorization_ref",
                    )
                out[k] = int(v)
            elif k in self.TASK_SETTABLE:
                out[k] = v if (k == "expires_at" or v is None) else int(v)
            else:
                raise ProtocolError("unknown_limit", k, f"unknown limit field {k}")
        return out

    # ------------------------------------------------------------------ open (idempotent)
    def open_execution(
        self,
        task_id: str,
        *,
        execution_id: str,
        scope_ref: str,
        authorization_ref: str,
        acceptance_manifest=None,
        limits_overrides=None,
        expires_at=None,
        phase: str = PHASE_IMPLEMENTATION,
        previous_execution_id=None,
    ) -> dict:
        require_id(task_id, "task_id")
        require_id(execution_id, "execution_id")
        if not authorization_ref:
            raise ProtocolError(
                "missing_authorization",
                task_id,
                "opening a managed execution references the user instruction",
            )
        if phase not in PHASES:
            raise ProtocolError(
                "invalid_phase", phase, f"phase must be one of {PHASES}"
            )
        with self.store.lock(task_id):
            existing = self.store.read(self._exec_rel(task_id))
            if existing is not None:
                if existing.get("execution_id") == execution_id:
                    return {"idempotent": True, "execution": existing}
                raise ProtocolError(
                    "execution_identity_conflict",
                    task_id,
                    "a different execution_id already owns this task; a new execution is a new task",
                )
            policy = _load_policy(self._policy_path)
            pv = int(policy.get("policy_version"))
            if pv not in SUPPORTED_POLICY_VERSIONS:
                raise ProtocolError(
                    "policy_migration_required",
                    str(pv),
                    "policy config version is unsupported",
                )
            limits = dict(policy["defaults"])
            if limits_overrides:
                limits = self._apply_open_overrides(limits, limits_overrides)
            if expires_at is not None:
                limits["expires_at"] = expires_at
            rec = {
                "schema_version": EXEC_SCHEMA_VERSION,
                "execution_id": execution_id,
                "task_id": task_id,
                "policy_version": pv,
                "state_version": 0,
                "status": STATUS_ACTIVE,
                "phase": phase,
                "scope_ref": scope_ref,
                "authorization_ref": authorization_ref,
                "previous_execution_id": previous_execution_id,
                "limits": limits,
                "usage": {
                    "work_dispatches": 0,
                    "review_launches": 0,
                    "technical_calls": 0,
                    "acceptance_calls": 0,
                    "reservations": {},
                },
                "acceptance_manifest": self._freeze_manifest(acceptance_manifest),
                "accepted_evidence": {},
                "acceptance_freeze": None,
                "acceptance_invalidations": [],
                "blockers": [],
                "backlog": [],
                "limit_changes": [],
                "last_action": None,
                "pause": None,
                "closure": None,
                "opened_at": utcnow(),
            }
            rec = self._write(task_id, rec)  # state_version 0 -> 1
            self._audit(
                task_id,
                "open_execution",
                {"execution_id": execution_id, "policy_version": pv},
            )
            return {"idempotent": False, "execution": rec}

    # ------------------------------------------------------------------ new-work guards
    def _find_open_blocker(self, rec: dict, blocker_id):
        for b in rec.get("blockers", []):
            if (
                b.get("blocker_id") == blocker_id
                and b.get("status") == "open"
                and b.get("evidence_ref")
            ):
                return b
        return None

    def _guard_new_work(self, rec: dict, kind: str, repair_blocker_id) -> None:
        if self._expired(rec):
            raise ProtocolError(
                "execution_expired",
                rec["task_id"],
                "execution expired; new work refused (read/reconcile still allowed)",
            )
        status = rec["status"]
        if status in (STATUS_PAUSED, STATUS_DRAINING):
            raise ProtocolError(
                "execution_paused",
                rec["task_id"],
                "execution paused/draining; new work refused",
            )
        if status == STATUS_EXHAUSTED:
            raise ProtocolError(
                "work_dispatch_exhausted",
                rec["task_id"],
                "work-dispatch allowance exhausted",
            )
        if status == STATUS_COMPLETED or rec["phase"] == PHASE_CLOSURE:
            # a closed/completed execution refuses new work EXCEPT a bounded repair linked to an
            # open, evidenced blocker against an existing criterion.
            if kind != "repair" or not repair_blocker_id:
                raise ProtocolError(
                    "execution_closed",
                    rec["task_id"],
                    "closed/completed execution refuses new work except a bounded evidenced repair",
                )
            if self._find_open_blocker(rec, repair_blocker_id) is None:
                raise ProtocolError(
                    "no_open_blocker",
                    str(repair_blocker_id),
                    "repair must link to an open, evidenced blocker against an existing criterion",
                )

    def _charge_work(self, task_id: str, rec: dict, kind: str) -> None:
        limits = rec["limits"]
        usage = rec["usage"]
        wd_limit = limits.get("work_dispatches")
        if wd_limit is not None and usage.get("work_dispatches", 0) >= int(wd_limit):
            if rec["status"] != STATUS_EXHAUSTED:
                rec["status"] = STATUS_EXHAUSTED
                self._write(task_id, rec)
                self._audit(task_id, "exhausted", {"reason": "work_dispatches"})
            raise ProtocolError(
                "work_dispatch_exhausted",
                task_id,
                "shared work-dispatch allowance exhausted",
            )
        if kind == "review_launch":
            rl_limit = limits.get("review_launches")
            if rl_limit is not None and usage.get("review_launches", 0) >= int(
                rl_limit
            ):
                raise ProtocolError(
                    "review_launch_exhausted",
                    task_id,
                    "review-launch allowance exhausted",
                )
            usage["review_launches"] = usage.get("review_launches", 0) + 1
        # a review launch consumes BOTH the review allowance and the shared work-dispatch allowance.
        usage["work_dispatches"] = usage.get("work_dispatches", 0) + 1

    # ------------------------------------------------------------------ reserve / settle (work)
    def reserve(
        self,
        task_id: str,
        *,
        action_id: str,
        kind: str,
        purpose=None,
        criterion_ref=None,
        repair_blocker_id=None,
        expected_state_version=None,
    ) -> dict:
        require_id(action_id, "action_id")
        if kind not in WORK_KINDS:
            raise ProtocolError(
                "invalid_reservation_kind", kind, f"kind must be one of {WORK_KINDS}"
            )
        with self.store.lock(task_id):
            rec = self._require_managed(task_id)
            self._check_version(rec, expected_state_version)
            reservations = rec["usage"].setdefault("reservations", {})
            # idempotent replay: a retried/reconnected reserve of the same action_id never
            # double-charges, and is allowed even if the record has since paused/exhausted.
            if action_id in reservations:
                return {
                    "idempotent": True,
                    "reservation": reservations[action_id],
                    "execution": self._summary(rec),
                }
            self._guard_new_work(rec, kind, repair_blocker_id)
            self._charge_work(task_id, rec, kind)  # persists + raises on exhaustion
            reservations[action_id] = {
                "action_id": action_id,
                "kind": kind,
                "purpose": purpose,
                "criterion_ref": criterion_ref,
                "repair_blocker_id": repair_blocker_id,
                "status": "reserved",
                "outcome": None,
                "evidence_ref": None,
                "reserved_at": utcnow(),
            }
            rec["last_action"] = {
                "id": action_id,
                "kind": kind,
                "outcome": "reserved",
                "evidence_ref": None,
            }
            rec = self._write(task_id, rec)
            self._audit(task_id, "reserve", {"action_id": action_id, "kind": kind})
            return {
                "idempotent": False,
                "reservation": reservations[action_id],
                "execution": self._summary(rec),
            }

    def settle(
        self,
        task_id: str,
        *,
        action_id: str,
        outcome: str,
        evidence_ref=None,
        criterion_ref=None,
        expected_state_version=None,
    ) -> dict:
        require_id(action_id, "action_id")
        with self.store.lock(task_id):
            rec = self._require_managed(
                task_id
            )  # reconciliation allowed while paused/exhausted
            self._check_version(rec, expected_state_version)
            res = rec["usage"].get("reservations", {}).get(action_id)
            if res is None:
                raise ProtocolError(
                    "unknown_reservation",
                    action_id,
                    "no reservation with that action_id",
                )
            # settlement reconciles; it NEVER refunds. An uncertain outcome stays charged.
            res["status"] = "settled"
            res["outcome"] = outcome
            res["evidence_ref"] = evidence_ref
            if criterion_ref is not None:
                res["criterion_ref"] = criterion_ref
            res["settled_at"] = utcnow()
            rec["last_action"] = {
                "id": action_id,
                "kind": res["kind"],
                "outcome": outcome,
                "evidence_ref": evidence_ref,
            }
            rec = self._write(task_id, rec)
            self._audit(task_id, "settle", {"action_id": action_id, "outcome": outcome})
            return {"reservation": res, "execution": self._summary(rec)}

    # ------------------------------------------------------------------ acceptance evidence / close
    def record_evidence(
        self,
        task_id: str,
        *,
        criterion_id: str,
        evidence_ref: str,
        attestation=None,
        evidence_sha256=None,
        accepted_by=None,
        expected_state_version=None,
    ) -> dict:
        require_id(task_id, "task_id")
        with self.store.lock(task_id):
            rec = self._require_managed(
                task_id
            )  # a bounded closure op, allowed while paused
            self._check_version(rec, expected_state_version)
            crit = rec.get("acceptance_manifest", {}).get(criterion_id)
            if crit is None:
                raise ProtocolError(
                    "unknown_criterion",
                    criterion_id,
                    "evidence must reference a frozen acceptance criterion",
                )
            if not evidence_ref:
                raise ProtocolError(
                    "missing_evidence",
                    criterion_id,
                    "evidence_ref required; missing/unknown evidence is never a pass",
                )
            crit["accepted"] = True
            crit["evidence_ref"] = evidence_ref
            crit["evidence_sha256"] = evidence_sha256
            crit["attestation"] = attestation
            crit["accepted_at"] = utcnow()
            crit["accepted_by"] = accepted_by
            # bind an acceptance-phase pass to the current freeze so a later freeze change invalidates
            # exactly the criteria proven under the stale implementation (no blended passing cycle).
            crit["accepted_under_freeze"] = (
                rec.get("acceptance_freeze")
                if rec["phase"] == PHASE_ACCEPTANCE
                else None
            )
            rec.setdefault("accepted_evidence", {})[criterion_id] = {
                "evidence_ref": evidence_ref,
                "evidence_sha256": evidence_sha256,
                "accepted_at": crit["accepted_at"],
                "accepted_by": accepted_by,
            }
            rec["last_action"] = {
                "id": criterion_id,
                "kind": "evidence",
                "outcome": "accepted",
                "evidence_ref": evidence_ref,
            }
            closed = self._maybe_close(rec)
            rec = self._write(task_id, rec)
            self._audit(
                task_id,
                "record_evidence",
                {"criterion_id": criterion_id, "closed": closed},
            )
            return {"execution": self._summary(rec), "closed": closed}

    def _maybe_close(self, rec: dict) -> bool:
        if rec["status"] == STATUS_COMPLETED:
            return True
        manifest = rec.get("acceptance_manifest", {})
        if (
            not manifest
        ):  # nothing frozen to satisfy: never auto-close on an empty manifest
            return False
        if all(c.get("accepted") for c in manifest.values()):
            rec["phase"] = PHASE_CLOSURE
            rec["status"] = STATUS_COMPLETED
            rec["closure"] = {"closed_at": utcnow(), "reason": "coverage_complete"}
            return True
        return False

    # ------------------------------------------------------------------ blockers / backlog
    def open_blocker(
        self,
        task_id: str,
        *,
        blocker_id: str,
        criterion_id: str,
        evidence_ref: str,
        description=None,
        expected_state_version=None,
    ) -> dict:
        require_id(blocker_id, "blocker_id")
        with self.store.lock(task_id):
            rec = self._require_managed(task_id)
            self._check_version(rec, expected_state_version)
            crit = rec.get("acceptance_manifest", {}).get(criterion_id)
            if crit is None:
                raise ProtocolError(
                    "unknown_criterion",
                    criterion_id,
                    "a blocker must target an existing acceptance criterion",
                )
            if not evidence_ref:
                raise ProtocolError(
                    "missing_evidence",
                    criterion_id,
                    "a blocker must be evidenced (reproducer/result/contract)",
                )
            for b in rec.setdefault("blockers", []):
                if b.get("blocker_id") == blocker_id:
                    return {
                        "idempotent": True,
                        "blocker": b,
                        "execution": self._summary(rec),
                    }
            blk = {
                "blocker_id": blocker_id,
                "criterion_id": criterion_id,
                "evidence_ref": evidence_ref,
                "description": description or "",
                "status": "open",
                "opened_at": utcnow(),
            }
            rec["blockers"].append(blk)
            # a blocker may invalidate ONLY the affected accepted criterion; it cannot add scope,
            # reset counters or mint allowance.
            if crit.get("accepted"):
                crit["accepted"] = False
                crit["invalidated_at"] = utcnow()
                rec.get("accepted_evidence", {}).pop(criterion_id, None)
            # reopen a closed/completed execution just enough for the bounded repair; usage unchanged.
            if rec["status"] == STATUS_COMPLETED or rec["phase"] == PHASE_CLOSURE:
                rec["status"] = STATUS_ACTIVE
                rec["phase"] = PHASE_ACCEPTANCE
                rec["closure"] = None
            rec["last_action"] = {
                "id": blocker_id,
                "kind": "blocker",
                "outcome": "open",
                "evidence_ref": evidence_ref,
            }
            rec = self._write(task_id, rec)
            self._audit(
                task_id,
                "open_blocker",
                {"blocker_id": blocker_id, "criterion_id": criterion_id},
            )
            return {
                "idempotent": False,
                "blocker": blk,
                "execution": self._summary(rec),
            }

    def resolve_blocker(
        self,
        task_id: str,
        *,
        blocker_id: str,
        resolution_ref=None,
        expected_state_version=None,
    ) -> dict:
        with self.store.lock(task_id):
            rec = self._require_managed(task_id)
            self._check_version(rec, expected_state_version)
            found = next(
                (
                    b
                    for b in rec.get("blockers", [])
                    if b.get("blocker_id") == blocker_id
                ),
                None,
            )
            if found is None:
                raise ProtocolError("unknown_blocker", blocker_id, "no such blocker")
            found["status"] = "resolved"
            found["resolution_ref"] = resolution_ref
            found["resolved_at"] = utcnow()
            rec = self._write(task_id, rec)
            self._audit(task_id, "resolve_blocker", {"blocker_id": blocker_id})
            return {"blocker": found, "execution": self._summary(rec)}

    def add_backlog(
        self, task_id: str, *, item: str, source=None, expected_state_version=None
    ) -> dict:
        with self.store.lock(task_id):
            rec = self._require_managed(
                task_id
            )  # a late optional request is recorded, not executed
            self._check_version(rec, expected_state_version)
            entry = {
                "item": str(item)[:2000],
                "source": source,
                "actionable": False,
                "at": utcnow(),
            }
            rec.setdefault("backlog", []).append(entry)
            rec = self._write(task_id, rec)
            self._audit(task_id, "backlog", {"source": source})
            return {
                "execution": self._summary(rec),
                "backlog_count": len(rec["backlog"]),
            }

    # ------------------------------------------------------------------ adapter call handshake
    def _invalidate_acceptance(self, rec: dict, attempted_identity) -> None:
        rec.setdefault("acceptance_invalidations", []).append(
            {
                "at": utcnow(),
                "prior_freeze": rec.get("acceptance_freeze"),
                "attempted_freeze": attempted_identity,
            }
        )
        stale = rec.get("acceptance_freeze")
        for cid, crit in rec.get("acceptance_manifest", {}).items():
            if crit.get("accepted") and crit.get("accepted_under_freeze") == stale:
                crit["accepted"] = False
                crit["invalidated_at"] = utcnow()
                rec.get("accepted_evidence", {}).pop(cid, None)
        rec["acceptance_freeze"] = None

    def reserve_call(
        self,
        task_id: str,
        *,
        action_id: str,
        phase: str,
        freeze_identity: str,
        purpose=None,
        expected_state_version=None,
    ) -> dict:
        require_id(action_id, "action_id")
        if phase not in CALL_PHASES:
            raise ProtocolError(
                "invalid_call_phase",
                phase,
                f"call phase must be one of {tuple(CALL_PHASES)}",
            )
        if not freeze_identity:
            raise ProtocolError(
                "missing_freeze_identity",
                action_id,
                "an enforcing adapter attaches a freeze identity from the actual implementation/config; "
                "an adapter that only reports totals afterward is observational",
            )
        field = CALL_PHASES[phase]
        with self.store.lock(task_id):
            rec = self._require_managed(task_id)
            self._check_version(rec, expected_state_version)
            reservations = rec["usage"].setdefault("reservations", {})
            if action_id in reservations:
                return {
                    "idempotent": True,
                    "reservation": reservations[action_id],
                    "execution": self._summary(rec),
                }
            self._guard_new_work(rec, "work_dispatch", None)  # a live call is new work
            limit = rec["limits"].get(field)
            if limit is None:
                raise ProtocolError(
                    "no_call_allowance",
                    field,
                    f"no {field} allowance configured; cannot claim enforced calls",
                )
            if rec["usage"].get(field, 0) >= int(limit):
                raise ProtocolError(
                    "call_allowance_exhausted", field, f"{field} allowance exhausted"
                )
            if phase == PHASE_ACCEPTANCE:
                cur = rec.get("acceptance_freeze")
                if cur is None:
                    rec["acceptance_freeze"] = freeze_identity
                elif cur != freeze_identity:
                    # implementation changed inside the reserved acceptance batch
                    self._invalidate_acceptance(rec, freeze_identity)
                    rec = self._write(task_id, rec)
                    self._audit(
                        task_id,
                        "freeze_mismatch",
                        {"expected": cur, "got": freeze_identity},
                    )
                    raise ProtocolError(
                        "freeze_identity_mismatch",
                        field,
                        "implementation changed within the reserved acceptance batch; remaining "
                        "calls blocked and the acceptance attempt invalidated (no blended cycle)",
                    )
            rec["usage"][field] = rec["usage"].get(field, 0) + 1
            reservations[action_id] = {
                "action_id": action_id,
                "kind": "call",
                "call_phase": phase,
                "freeze_identity": freeze_identity,
                "purpose": purpose,
                "status": "reserved",
                "outcome": None,
                "response_ref": None,
                "reserved_at": utcnow(),
            }
            rec["last_action"] = {
                "id": action_id,
                "kind": "call",
                "outcome": "reserved",
                "evidence_ref": None,
            }
            rec = self._write(task_id, rec)
            self._audit(
                task_id, "reserve_call", {"action_id": action_id, "phase": phase}
            )
            return {
                "idempotent": False,
                "reservation": reservations[action_id],
                "execution": self._summary(rec),
            }

    def settle_call(
        self,
        task_id: str,
        *,
        action_id: str,
        freeze_identity: str,
        outcome: str,
        response_ref=None,
        expected_state_version=None,
    ) -> dict:
        require_id(action_id, "action_id")
        with self.store.lock(task_id):
            rec = self._require_managed(task_id)
            self._check_version(rec, expected_state_version)
            res = rec["usage"].get("reservations", {}).get(action_id)
            if res is None or res.get("kind") != "call":
                raise ProtocolError(
                    "unknown_reservation",
                    action_id,
                    "no call reservation with that action_id",
                )
            # bind the response to the reservation's freeze identity; a mismatch means the
            # implementation moved under the call -> invalidate, keep charged, never accept.
            if freeze_identity != res.get("freeze_identity"):
                if res.get("call_phase") == PHASE_ACCEPTANCE:
                    self._invalidate_acceptance(rec, freeze_identity)
                res["status"] = "invalidated"
                res["outcome"] = "freeze_mismatch"
                rec = self._write(task_id, rec)
                self._audit(task_id, "settle_call_mismatch", {"action_id": action_id})
                raise ProtocolError(
                    "freeze_identity_mismatch",
                    action_id,
                    "response freeze identity != reservation; acceptance invalidated",
                )
            res["status"] = "settled"
            res["outcome"] = outcome
            res["response_ref"] = response_ref
            res["settled_at"] = utcnow()
            rec["last_action"] = {
                "id": action_id,
                "kind": "call",
                "outcome": outcome,
                "evidence_ref": response_ref,
            }
            rec = self._write(task_id, rec)
            self._audit(
                task_id, "settle_call", {"action_id": action_id, "outcome": outcome}
            )
            return {"reservation": res, "execution": self._summary(rec)}

    # ------------------------------------------------------------------ pause / limits / phase
    def pause(
        self,
        task_id: str,
        *,
        authorization_ref: str,
        reason=None,
        expected_state_version=None,
    ) -> dict:
        if not authorization_ref:
            raise ProtocolError(
                "missing_authorization",
                task_id,
                "pause carries the user instruction reference",
            )
        with self.store.lock(task_id):
            rec = self._require_managed(task_id)
            self._check_version(rec, expected_state_version)
            if rec["status"] == STATUS_COMPLETED:
                raise ProtocolError(
                    "execution_completed",
                    task_id,
                    "completed is terminal; nothing to pause",
                )
            open_res = [
                a
                for a, r in rec["usage"].get("reservations", {}).items()
                if r.get("status") == "reserved"
            ]
            rec["status"] = STATUS_DRAINING if open_res else STATUS_PAUSED
            rec["pause"] = {
                "at": utcnow(),
                "authorization_ref": authorization_ref,
                "reason": reason,
                "draining": bool(open_res),
                "in_flight": sorted(open_res),
            }
            rec = self._write(task_id, rec)
            self._audit(task_id, "pause", {"draining": bool(open_res)})
            return {"execution": self._summary(rec)}

    def unpause(
        self, task_id: str, *, authorization_ref: str, expected_state_version=None
    ) -> dict:
        if not authorization_ref:
            raise ProtocolError(
                "missing_authorization",
                task_id,
                "unpause carries the user instruction reference",
            )
        with self.store.lock(task_id):
            rec = self._require_managed(task_id)
            self._check_version(rec, expected_state_version)
            if rec["status"] not in (STATUS_PAUSED, STATUS_DRAINING):
                raise ProtocolError("not_paused", task_id, "execution is not paused")
            rec["status"] = STATUS_ACTIVE
            rec["pause"] = None
            rec = self._write(task_id, rec)
            self._audit(task_id, "unpause", {"authorization_ref": authorization_ref})
            return {"execution": self._summary(rec)}

    def change_limits(
        self,
        task_id: str,
        *,
        authorization_ref: str,
        changes: dict,
        expected_state_version=None,
    ) -> dict:
        if not authorization_ref:
            raise ProtocolError(
                "missing_authorization",
                task_id,
                "raising/extending a limit requires an authorization_ref",
            )
        if not isinstance(changes, dict) or not changes:
            raise ProtocolError(
                "invalid_change", task_id, "changes must be a non-empty object"
            )
        with self.store.lock(task_id):
            rec = self._require_managed(task_id)
            self._check_version(rec, expected_state_version)
            limits = rec["limits"]
            applied = {}
            for k, v in changes.items():
                if k not in self.LOWER_ONLY and k not in self.TASK_SETTABLE:
                    raise ProtocolError("unknown_limit", k, f"unknown limit field {k}")
                old = limits.get(k)
                limits[k] = v if (k == "expires_at" or v is None) else int(v)
                applied[k] = {"old": old, "new": limits[k]}
            rec.setdefault("limit_changes", []).append(
                {
                    "at": utcnow(),
                    "authorization_ref": authorization_ref,
                    "changes": applied,
                }
            )
            # an authorized raise can lift an exhausted state; PAST USAGE IS UNCHANGED.
            if rec["status"] == STATUS_EXHAUSTED:
                wd = limits.get("work_dispatches")
                if wd is None or rec["usage"].get("work_dispatches", 0) < int(wd):
                    rec["status"] = STATUS_ACTIVE
            rec["last_action"] = {
                "id": "change_limits",
                "kind": "limit_change",
                "outcome": "applied",
                "evidence_ref": authorization_ref,
            }
            rec = self._write(task_id, rec)
            self._audit(
                task_id,
                "change_limits",
                {"authorization_ref": authorization_ref, "fields": sorted(applied)},
            )
            return {"execution": self._summary(rec), "applied": applied}

    def set_phase(
        self, task_id: str, *, phase: str, expected_state_version=None
    ) -> dict:
        if phase not in PHASES:
            raise ProtocolError(
                "invalid_phase", phase, f"phase must be one of {PHASES}"
            )
        with self.store.lock(task_id):
            rec = self._require_managed(task_id)
            self._check_version(rec, expected_state_version)
            if rec["status"] == STATUS_COMPLETED:
                raise ProtocolError(
                    "execution_completed",
                    task_id,
                    "completed is terminal; cannot change phase",
                )
            rec["phase"] = phase
            rec = self._write(task_id, rec)
            self._audit(task_id, "set_phase", {"phase": phase})
            return {"execution": self._summary(rec)}
