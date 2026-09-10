"""execution.py — one persistent, versioned, opt-in execution record per managed task.

This is SporeDrive's stopping/permission layer (PLAN sections 2-4). It is deliberately built ON TOP
of the existing coordination primitives rather than beside them:

* storage, atomic temp+rename writes, corruption-vs-absence distinction and the re-entrant per-task
  advisory ``flock`` all come from :class:`~mycelium_coord.store.CoordStore`;
* rejections are :class:`~mycelium_coord.model.ProtocolError` so both hosts see one error contract;
* message *delivery* state (persisted/delivered/acknowledged/completed) stays entirely in
  ``coord.py``. This module is about *permission to execute a task*, which is a distinct axis: a
  delivered progress message never becomes authority to spend a new work dispatch.

Design invariants (measurement-integrity + Fable F1-F6, hardened for the primary review R1-R5):

* Limits live in a maintained, versioned policy config (``execution_policy.json``), never in a
  model-composed brief. An open override may only *lower* the supervisor-abuse limits
  (work/review dispatches); *raising* an initial limit, extending it later, enabling it after open,
  or unpausing all require the user-authorization-linked ``change_limits``/``unpause`` path.
* Every work-producing action reserves the shared work-dispatch allowance atomically under the
  per-task lock with a stable ``action_id`` before dispatch; a replay of that id is idempotent and
  never double-charges; an uncertain action stays charged until reconciled (no blind refund/retry).
  A reservation is BOUND to one concrete dispatch identity (R1): the first managed dispatch that
  claims it fixes that binding, so an idempotent same-request replay reconciles without a new charge
  while a *different* request (or a review) can never be funded by that same reservation.
* The frozen acceptance manifest enumerates ALL required deliverables/checks/reviews. On each
  evidence settlement coverage is recomputed; once every required criterion is accepted the record
  enters CLOSURE automatically in the SAME conditional write. Closure is NOT terminal completion
  (R4): a closed execution accepts ONLY a bounded repair linked to an open, evidenced blocker on an
  existing criterion; terminal ``completed`` is reached solely by an explicit final completion
  receipt, after which no mutation may reactivate the execution (late concerns become non-actionable
  backlog, never an executor wake). Missing/unknown evidence is never a pass; external/semantic
  evidence requires an attributable attestation and a local artifact reference is validated by
  identity where one is supplied (R4).
* Reading, recording evidence, acknowledging, reporting completion and safe stopping remain possible
  after pause/exhaustion/closure — these are bounded closure operations, not new analysis.
* Adapter calls (technical-debug / frozen-acceptance) reserve against their own task-specific
  allowance with a freeze identity the ADAPTER computes from the actual implementation/config. An
  implementation change mid-batch (a new freeze identity) blocks remaining dispatch and invalidates
  the current acceptance attempt so old/new responses cannot be blended into a passing cycle.
* Unattended execution MUST carry a finite, valid UTC expiry and a named automation reference; an
  invalid expiry fails closed (treated as expired), and expiry/completion record a durable pending
  shutdown intent so the associated automation can be paused through the supported tool even across a
  crash between the terminal state and the scheduler pause (R5).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
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
STATUS_CLOSED = (
    "closed"  # coverage complete; ONLY a bounded evidenced repair is allowed
)
STATUS_COMPLETED = (
    "completed"  # final completion receipt written; TERMINAL, no reactivation
)
STATUSES = (
    STATUS_ACTIVE,
    STATUS_WAITING,
    STATUS_DRAINING,
    STATUS_PAUSED,
    STATUS_EXHAUSTED,
    STATUS_CLOSED,
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

# a work DISPATCH may be funded ONLY by a work-dispatch reservation (generic work or a bounded
# repair). A reviewer launch (kind='review_launch', claimed via claim_review) and an enforced adapter
# call (kind='call', via reserve_call/settle_call) are DISTINCT purposes: they occupy the
# reservations map but must never fund a work dispatch merely by naming their action_id (review R1
# reservation-purpose interchange). expected_purpose maps a dispatch boundary to its allowed kinds.
DISPATCH_KINDS = frozenset({"work_dispatch", "repair"})
_PURPOSE_KINDS = {
    "work_dispatch": DISPATCH_KINDS,
    "review_launch": frozenset({"review_launch"}),
}

# coordination message kinds that DISPATCH executable work. On a MANAGED task (an execution record
# exists) a send/notify of one of these requires a valid reservation; every other kind (ack, status,
# checkpoint, review finding, completion receipt, backlog) is a non-actionable record and passes.
# Classification is by the message's intrinsic kind, so a paused/closed task can never become
# "unmanaged" merely by omitting an optional field (review R1).
ACTIONABLE_KINDS = frozenset(
    {
        "task",
        "work",
        "work_dispatch",
        "dispatch",
        "repair",
        "review_launch",
        "review_request",
    }
)

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


def _parse_ts(value) -> datetime:
    """Parse an ISO-8601 timestamp, defaulting a naive value to UTC. Raises ValueError/TypeError on
    anything unparseable — the single place expiry strings are interpreted."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _sha256_file(path: Path) -> str | None:
    """Content digest of a local artifact, or None if it is not a readable regular file. Used to
    validate a supplied local-evidence reference by identity (review R4); never used to *interpret*
    the artifact."""
    try:
        if not path.is_file():
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


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

    def is_managed(self, task_id: str) -> bool:
        """A task is MANAGED once an execution record exists for it. Managed-ness is a durable fact,
        never a per-call field — so a work dispatch cannot slip past the bound by omitting metadata
        (review R1)."""
        try:
            return self.store.read(self._exec_rel(task_id)) is not None
        except Exception:
            # a corrupt record is still "managed"; fail closed rather than treat it as legacy
            return True

    def status(self, task_id: str):
        rec = self.read_execution(task_id)
        return None if rec is None else self._summary(rec)

    # ------------------------------------------------------------------ dispatchability (read-only)
    def _dispatchable(
        self,
        rec: dict,
        action_id: str,
        dispatch_identity=None,
        expected_purpose: str = "work_dispatch",
    ) -> dict:
        """Pure predicate shared by the read-only :meth:`dispatch_check` and the writing
        :meth:`claim_dispatch`: is ``action_id`` an OPEN reservation of the expected PURPOSE on a
        currently dispatchable execution, and (if a dispatch identity is supplied) is the reservation
        bound to THIS dispatch? Terminal ``completed`` and paused/expired/exhausted refuse. Purpose
        is enforced here: a ``call``/``review_launch`` reservation can NEVER fund a work dispatch
        (review R1). ``closed`` is repair-only for dispatch too: a generic work reservation made
        before closure is refused, and only a ``repair`` linked to a still-open evidenced blocker may
        dispatch (review R4) — the closure restriction is NOT relied on at reserve time alone."""
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
        if status == STATUS_COMPLETED:
            return {"ok": False, "reason": "execution_completed", "status": status}
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
        allowed_kinds = _PURPOSE_KINDS.get(expected_purpose, DISPATCH_KINDS)
        if res.get("kind") not in allowed_kinds:
            # a call/review reservation cannot fund a work dispatch merely by naming its action id
            return {
                "ok": False,
                "reason": "reservation_purpose_mismatch",
                "status": status,
                "reservation_kind": res.get("kind"),
                "expected_purpose": expected_purpose,
            }
        if status == STATUS_CLOSED or rec.get("phase") == PHASE_CLOSURE:
            # closure applies to the DISPATCH/CLAIM predicate, not only reserve-time (review R4): a
            # generic work_dispatch reserved before coverage completed must not still fire; only a
            # repair linked to a still-open, evidenced blocker may dispatch while closed.
            if (
                res.get("kind") != "repair"
                or not res.get("repair_blocker_id")
                or self._find_open_blocker(rec, res.get("repair_blocker_id")) is None
            ):
                return {
                    "ok": False,
                    "reason": "execution_closed",
                    "status": status,
                }
        bound = res.get("dispatch_binding")
        if (
            dispatch_identity is not None
            and bound is not None
            and bound != dispatch_identity
        ):
            # one reservation can fund exactly ONE concrete dispatch (review R1 reservation_reuse)
            return {
                "ok": False,
                "reason": "dispatch_binding_mismatch",
                "status": status,
                "bound_to": bound,
            }
        return {
            "ok": True,
            "reason": "dispatchable",
            "status": status,
            "state_version": rec.get("state_version"),
            "dispatch_binding": bound,
        }

    def dispatch_check(
        self,
        task_id: str,
        action_id: str,
        dispatch_identity=None,
        expected_purpose: str = "work_dispatch",
    ) -> dict:
        """Read-only pre-dispatch gate. A managed transport calls this as a FRESH read of durable
        state immediately before the actual cmux mutation, so a request queued before a newer
        pause/expiry cannot still fire and a reservation bound to a different dispatch cannot be
        re-aimed. Deliberately NOT a lock held across the transport (a flock is never held across a
        network wait, store.py); a pause landing in the microseconds after this read but before the
        send is the documented residual window, not an atomicity guarantee this layer can make across
        cmux."""
        rec = self.read_execution(task_id)
        if rec is None:
            return {"ok": False, "reason": "execution_not_found"}
        return self._dispatchable(rec, action_id, dispatch_identity, expected_purpose)

    def claim_dispatch(
        self,
        task_id: str,
        *,
        action_id: str,
        dispatch_identity: str,
        expected_purpose: str = "work_dispatch",
        expected_state_version=None,
    ) -> dict:
        """Atomically bind an OPEN work reservation to one concrete dispatch identity, under the
        per-task lock, and return whether the dispatch may proceed (review R1). The lock is held only
        for this fast durable check+bind — NEVER across the subsequent transport/cmux wait. Idempotent
        for the same dispatch identity (safe reconciliation of an uncertain send); refuses a second,
        different dispatch that tries to spend the same reservation, and refuses a paused/expired/
        completed execution. Returns a dict (never raises for a gate refusal) so a transport can
        record a structured 'refused_by_execution_gate' outcome."""
        require_id(action_id, "action_id")
        with self.store.lock(task_id):
            rec = self.store.read(self._exec_rel(task_id))
            if rec is None:
                return {"ok": False, "reason": "execution_not_found"}
            try:
                self._check_version(rec, expected_state_version)
            except ProtocolError as e:
                return {"ok": False, "reason": e.code, "status": rec.get("status")}
            verdict = self._dispatchable(
                rec, action_id, dispatch_identity, expected_purpose
            )
            if not verdict.get("ok"):
                return verdict
            res = rec["usage"]["reservations"][action_id]
            if res.get("dispatch_binding") is None:
                res["dispatch_binding"] = dispatch_identity
                res["dispatched_at"] = utcnow()
                rec = self._write(task_id, rec)
                self._audit(
                    task_id,
                    "claim_dispatch",
                    {"action_id": action_id, "dispatch_identity": dispatch_identity},
                )
                verdict = dict(verdict)
                verdict.update(
                    {
                        "claimed": True,
                        "dispatch_binding": dispatch_identity,
                        "state_version": rec.get("state_version"),
                    }
                )
                return verdict
            # already bound to THIS identity: idempotent reconciliation, no re-charge, no re-write
            verdict = dict(verdict)
            verdict.update(
                {
                    "claimed": False,
                    "idempotent": True,
                    "dispatch_binding": res.get("dispatch_binding"),
                }
            )
            return verdict

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
            dt = _parse_ts(exp)
        except (ValueError, TypeError):
            # FAIL CLOSED (review R5): an unparseable expiry means we cannot prove the task is still
            # inside its window, so new work is refused (reads/reconciliation remain allowed).
            return True
        return datetime.now(timezone.utc) > dt

    @staticmethod
    def _validate_limits(limits: dict) -> None:
        """Reject a limits dict that could silently defeat the bound (review R5). Runs at open and at
        every change: expiry must be a valid UTC-aware timestamp (or null); the abuse/adapter
        counters must be nonnegative integers (or null); the review deadline must be positive and
        finite (or null)."""
        exp = limits.get("expires_at")
        if exp is not None:
            try:
                _parse_ts(exp)
            except (ValueError, TypeError):
                raise ProtocolError(
                    "invalid_expiry",
                    str(exp),
                    "expires_at must be a valid ISO-8601 UTC timestamp or null",
                )
        for f in (
            "work_dispatches",
            "review_launches",
            "technical_calls",
            "acceptance_calls",
        ):
            v = limits.get(f)
            if v is not None and (
                isinstance(v, bool) or not isinstance(v, int) or v < 0
            ):
                raise ProtocolError(
                    "invalid_limit", f, f"{f} must be a nonnegative integer or null"
                )
        rd = limits.get("review_deadline_seconds")
        if rd is not None and (
            isinstance(rd, bool)
            or not isinstance(rd, (int, float))
            or rd != rd  # NaN
            or rd in (float("inf"), float("-inf"))
            or rd <= 0
        ):
            raise ProtocolError(
                "invalid_review_deadline",
                str(rd),
                "review_deadline_seconds must be a positive finite number or null",
            )

    def _summary(self, rec: dict) -> dict:
        limits = rec.get("limits", {})
        usage = rec.get("usage", {})
        manifest = rec.get("acceptance_manifest", {})
        total = len(manifest)
        accepted = sum(1 for c in manifest.values() if c.get("accepted"))
        reservations = usage.get("reservations", {})
        shutdown = rec.get("shutdown")
        return {
            "execution_id": rec["execution_id"],
            "task_id": rec["task_id"],
            "status": rec["status"],
            "phase": rec["phase"],
            "state_version": rec["state_version"],
            "policy_version": rec["policy_version"],
            "attended": rec.get("attended", True),
            "automation_ref": rec.get("automation_ref"),
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
            "closure": rec.get("closure"),
            "shutdown": shutdown,
            "shutdown_pending": bool(shutdown and shutdown.get("status") == "pending"),
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
        attended: bool = True,
        automation_ref=None,
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
            # validate BEFORE persisting: an invalid expiry/limit must never enter durable state (R5)
            self._validate_limits(limits)
            if not attended:
                # an unattended/automation-driven execution must fail closed on time and name what to
                # pause; a null expiry there would run unbounded (review R5, PLAN section 3).
                if not limits.get("expires_at"):
                    raise ProtocolError(
                        "unattended_requires_expiry",
                        task_id,
                        "an unattended execution requires a finite expires_at",
                    )
                if not automation_ref:
                    raise ProtocolError(
                        "unattended_requires_automation_ref",
                        task_id,
                        "an unattended execution must name the automation to pause on shutdown",
                    )
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
                "attended": bool(attended),
                "automation_ref": automation_ref,
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
                "completion": None,
                "shutdown": None,
                "opened_at": utcnow(),
            }
            rec = self._write(task_id, rec)  # state_version 0 -> 1
            self._audit(
                task_id,
                "open_execution",
                {
                    "execution_id": execution_id,
                    "policy_version": pv,
                    "attended": bool(attended),
                },
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
        if status == STATUS_COMPLETED:
            # terminal: no work of any kind reactivates a completed execution (review R4). A late
            # concern is a non-actionable backlog entry or a NEW user-authorized execution.
            raise ProtocolError(
                "execution_completed",
                rec["task_id"],
                "completed is terminal; a new concern uses a related new execution, not this one",
            )
        if status == STATUS_CLOSED or rec["phase"] == PHASE_CLOSURE:
            # closed (coverage complete, not yet finally completed): the ONLY new work permitted is a
            # bounded repair linked to an open, evidenced blocker on an existing criterion.
            if kind != "repair" or not repair_blocker_id:
                raise ProtocolError(
                    "execution_closed",
                    rec["task_id"],
                    "closed execution refuses new work except a bounded evidenced repair",
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
        dispatch_binding=None,
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
                # a pre-bound dispatch identity fixes which concrete dispatch this reservation funds;
                # None means the first managed dispatch claims it (review R1).
                "dispatch_binding": dispatch_binding,
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

    # ------------------------------------------------------------------ managed reviewer launch
    def claim_review(
        self,
        task_id: str,
        *,
        execution_id: str,
        action_id: str,
        launch_identity: str,
        caller_deadline_seconds=None,
        expected_state_version=None,
    ) -> dict:
        """Claim an existing OPEN review-launch reservation for one owned reviewer process (review
        R3). Verifies the execution identity matches, the reservation is a review launch, open and
        dispatchable; recheck of pause/expiry happens HERE, immediately before the caller starts the
        owned group. Binds a stable ``launch_identity`` (idempotent for the same identity, refused for
        a different one so one reservation cannot fund two launches) and returns the effective hard
        deadline: the maintained-policy ``review_deadline_seconds``, which the caller may only SHORTEN
        (never lengthen). Returns a dict (never raises for a gate refusal) so the shell guard can fail
        the launch before codex starts."""
        require_id(action_id, "action_id")
        if not launch_identity:
            return {"ok": False, "reason": "missing_launch_identity"}
        with self.store.lock(task_id):
            rec = self.store.read(self._exec_rel(task_id))
            if rec is None:
                return {"ok": False, "reason": "execution_not_found"}
            if rec.get("execution_id") != execution_id:
                return {
                    "ok": False,
                    "reason": "execution_identity_mismatch",
                    "expected": rec.get("execution_id"),
                }
            try:
                self._check_version(rec, expected_state_version)
            except ProtocolError as e:
                return {"ok": False, "reason": e.code}
            res = rec.get("usage", {}).get("reservations", {}).get(action_id)
            if res is None:
                return {"ok": False, "reason": "unknown_reservation"}
            if res.get("kind") != "review_launch":
                return {
                    "ok": False,
                    "reason": "not_a_review_reservation",
                    "reservation_kind": res.get("kind"),
                }
            # freshness recheck immediately before the owned group starts
            if self._expired(rec):
                return {
                    "ok": False,
                    "reason": "execution_expired",
                    "status": rec.get("status"),
                }
            if rec.get("status") in (STATUS_PAUSED, STATUS_DRAINING):
                return {
                    "ok": False,
                    "reason": "execution_paused",
                    "status": rec.get("status"),
                }
            if rec.get("status") == STATUS_COMPLETED:
                return {
                    "ok": False,
                    "reason": "execution_completed",
                    "status": rec.get("status"),
                }
            if rec.get("status") == STATUS_CLOSED or rec.get("phase") == PHASE_CLOSURE:
                # closure is repair-only; a reviewer launch (not a bounded repair) cannot start while
                # the execution is closed, regardless of when the reservation was made (review R4).
                return {
                    "ok": False,
                    "reason": "execution_closed",
                    "status": rec.get("status"),
                }
            bound = res.get("launch_identity")
            if bound is not None and bound != launch_identity:
                return {
                    "ok": False,
                    "reason": "review_already_launched",
                    "bound_to": bound,
                }
            if res.get("status") != "reserved":
                return {
                    "ok": False,
                    "reason": "reservation_not_open",
                    "reservation_status": res.get("status"),
                }
            policy_deadline = rec.get("limits", {}).get("review_deadline_seconds")
            effective = self._effective_review_deadline(
                policy_deadline, caller_deadline_seconds
            )
            if effective is None:
                return {
                    "ok": False,
                    "reason": "no_review_deadline",
                    "detail": "no finite review deadline configured and none supplied",
                }
            newly = False
            if bound is None:
                res["launch_identity"] = launch_identity
                res["launched_at"] = utcnow()
                res["effective_deadline_seconds"] = effective
                rec = self._write(task_id, rec)
                self._audit(
                    task_id,
                    "claim_review",
                    {
                        "action_id": action_id,
                        "launch_identity": launch_identity,
                        "effective_deadline_seconds": effective,
                    },
                )
                newly = True
            return {
                "ok": True,
                "reason": "review_claimed",
                "effective_deadline_seconds": res.get(
                    "effective_deadline_seconds", effective
                ),
                "claimed": newly,
                "status": rec.get("status"),
                "state_version": rec.get("state_version"),
            }

    @staticmethod
    def _effective_review_deadline(policy_deadline, caller_deadline):
        """The hard deadline for an owned reviewer: the policy value, which the caller may only
        shorten. Both may be present; a None policy falls back to the caller (both None -> None)."""
        vals = [v for v in (policy_deadline, caller_deadline) if v is not None]
        try:
            vals = [float(v) for v in vals if float(v) > 0]
        except (TypeError, ValueError):
            return None
        return min(vals) if vals else None

    def settle_review(
        self,
        task_id: str,
        *,
        action_id: str,
        outcome: str,
        launch_identity=None,
        response_ref=None,
        expected_state_version=None,
    ) -> dict:
        """Reconcile a claimed review launch (complete / timeout / uncertain). NEVER refunds and never
        relaunches (review R3): local termination does not prove the remote inference stopped, so an
        uncertain outcome stays charged like any other uncertain work."""
        require_id(action_id, "action_id")
        with self.store.lock(task_id):
            rec = self._require_managed(task_id)
            self._check_version(rec, expected_state_version)
            res = rec.get("usage", {}).get("reservations", {}).get(action_id)
            if res is None or res.get("kind") != "review_launch":
                raise ProtocolError(
                    "unknown_reservation",
                    action_id,
                    "no review reservation with that action_id",
                )
            if (
                launch_identity is not None
                and res.get("launch_identity") is not None
                and res.get("launch_identity") != launch_identity
            ):
                raise ProtocolError(
                    "review_launch_mismatch",
                    action_id,
                    "settle launch identity does not match the claimed review launch",
                )
            res["status"] = "settled"
            res["outcome"] = outcome
            res["response_ref"] = response_ref
            res["settled_at"] = utcnow()
            rec["last_action"] = {
                "id": action_id,
                "kind": "review_launch",
                "outcome": outcome,
                "evidence_ref": response_ref,
            }
            rec = self._write(task_id, rec)
            self._audit(
                task_id, "settle_review", {"action_id": action_id, "outcome": outcome}
            )
            return {"reservation": res, "execution": self._summary(rec)}

    # ------------------------------------------------------------------ acceptance evidence / close
    def _validate_evidence(
        self, evidence_ref, attestation, accepted_by, evidence_sha256
    ):
        """Enforce attributable evidence (review R4). A supplied local artifact is validated by
        identity (it must exist; if a digest is supplied it must match; the computed digest is
        returned to record). Evidence that is NOT a resolvable local artifact is external/semantic and
        requires an attributable attestation plus an accepted_by identity — we never attempt to prove
        arbitrary text, only to require that a person/adapter attests to it. Returns the digest to
        store (or None)."""
        try:
            candidate = Path(evidence_ref)
        except (TypeError, ValueError):
            candidate = None
        computed = _sha256_file(candidate) if candidate is not None else None
        if evidence_sha256:
            if computed is None:
                raise ProtocolError(
                    "evidence_artifact_missing",
                    str(evidence_ref),
                    "a digest was supplied but the local artifact does not resolve to a readable file",
                )
            if computed != evidence_sha256:
                raise ProtocolError(
                    "evidence_hash_mismatch",
                    str(evidence_ref),
                    "supplied evidence_sha256 does not match the artifact on disk",
                )
            return computed
        if computed is not None:
            return computed  # validated local artifact identity; attestation optional
        # external / semantic evidence: require an attributable attestation
        if not attestation or not accepted_by:
            raise ProtocolError(
                "unattributable_evidence",
                str(evidence_ref),
                "external/semantic evidence requires an attestation and accepted_by; a bare "
                "unverifiable reference is never a pass",
            )
        return None

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
            if rec["status"] == STATUS_COMPLETED:
                raise ProtocolError(
                    "execution_completed",
                    task_id,
                    "completed is terminal; evidence for a new concern uses a new execution",
                )
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
            computed_sha = self._validate_evidence(
                evidence_ref, attestation, accepted_by, evidence_sha256
            )
            crit["accepted"] = True
            crit["evidence_ref"] = evidence_ref
            crit["evidence_sha256"] = computed_sha or evidence_sha256
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
                "evidence_sha256": crit["evidence_sha256"],
                "attestation": attestation,
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
        """Enter CLOSURE (coverage complete) — NOT terminal completion (review R4). Returns True when
        the record is in closure."""
        if rec["status"] == STATUS_COMPLETED:
            return True  # already terminal
        manifest = rec.get("acceptance_manifest", {})
        if (
            not manifest
        ):  # nothing frozen to satisfy: never auto-close on an empty manifest
            return False
        if all(c.get("accepted") for c in manifest.values()):
            rec["phase"] = PHASE_CLOSURE
            rec["status"] = STATUS_CLOSED
            rec["closure"] = {
                "closed_at": utcnow(),
                "reason": "coverage_complete",
                "completed": False,
            }
            return True
        if rec["status"] == STATUS_CLOSED:
            # coverage dropped below complete (a criterion was invalidated) but the execution is still
            # in closure for the bounded repair; keep it closed, do not silently reactivate.
            return True
        return False

    def record_completion(
        self,
        task_id: str,
        *,
        completion_ref: str,
        accepted_by: str,
        attestation=None,
        expected_state_version=None,
    ) -> dict:
        """Record the explicit FINAL completion receipt: closure -> terminal ``completed`` (review
        R4). Requires coverage complete (status closed) and an attributable receipt. Terminal: after
        this, no mutation reactivates the execution. If the execution is unattended (or names an
        automation), a durable pending shutdown intent is recorded so the host can pause the
        automation even across a crash (review R5)."""
        require_id(task_id, "task_id")
        if not completion_ref or not accepted_by:
            raise ProtocolError(
                "unattributable_completion",
                task_id,
                "a final completion receipt requires completion_ref and accepted_by",
            )
        with self.store.lock(task_id):
            rec = self._require_managed(task_id)
            self._check_version(rec, expected_state_version)
            if rec["status"] == STATUS_COMPLETED:
                return {"idempotent": True, "execution": self._summary(rec)}
            manifest = rec.get("acceptance_manifest", {})
            covered = bool(manifest) and all(
                c.get("accepted") for c in manifest.values()
            )
            if rec["status"] != STATUS_CLOSED or not covered:
                raise ProtocolError(
                    "not_in_closure",
                    task_id,
                    "final completion requires coverage complete (closure); it is not the same "
                    "transition as coverage->closure",
                )
            rec["status"] = STATUS_COMPLETED
            rec["phase"] = PHASE_CLOSURE
            rec["closure"] = {
                **(rec.get("closure") or {}),
                "completed": True,
                "completion_ref": completion_ref,
                "accepted_by": accepted_by,
                "attestation": attestation,
                "completed_at": utcnow(),
            }
            rec["completion"] = {
                "completion_ref": completion_ref,
                "accepted_by": accepted_by,
                "attestation": attestation,
                "at": utcnow(),
            }
            # terminal completion pauses any associated automation through the supported tool
            self._record_shutdown_intent(rec, reason="completion")
            rec["last_action"] = {
                "id": "record_completion",
                "kind": "completion",
                "outcome": "completed",
                "evidence_ref": completion_ref,
            }
            rec = self._write(task_id, rec)
            self._audit(
                task_id, "record_completion", {"completion_ref": completion_ref}
            )
            return {"idempotent": False, "execution": self._summary(rec)}

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
            if rec["status"] == STATUS_COMPLETED:
                # terminal: a post-completion concern does NOT reopen the execution (review R4). It is
                # recorded as non-actionable backlog and the caller is told to use a new execution.
                raise ProtocolError(
                    "execution_completed",
                    task_id,
                    "completed is terminal; record a late concern via backlog / a new execution, "
                    "it cannot reopen this execution",
                )
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
            # opening a blocker during CLOSURE keeps the execution CLOSED (repair-only). It does NOT
            # flip to a fully-active state that would fund GENERIC work (review R4 completed_reopened):
            # generic work stays refused; only a repair linked to this blocker may reserve.
            if rec["status"] == STATUS_CLOSED or rec["phase"] == PHASE_CLOSURE:
                rec["status"] = STATUS_CLOSED
                rec["phase"] = PHASE_ACCEPTANCE
                rec["closure"] = {
                    **(rec.get("closure") or {}),
                    "reopened_for_repair": True,
                    "reopened_at": utcnow(),
                    "completed": False,
                }
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
            rec = self._require(
                task_id
            )  # a late optional request is recorded, not executed — allowed even when completed
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

    # ------------------------------------------------------------------ shutdown reconciliation (R5)
    def _record_shutdown_intent(self, rec: dict, *, reason: str) -> None:
        """Record a durable pending shutdown intent on the record (idempotent). This is the seam that
        survives a crash between the terminal/expired state and the actual scheduler pause: the intent
        persists until a host adapter reconciles it by pausing the named automation."""
        cur = rec.get("shutdown")
        if cur and cur.get("status") == "reconciled":
            return
        if cur and cur.get("status") == "pending":
            return
        rec["shutdown"] = {
            "status": "pending",
            "reason": reason,
            "automation_ref": rec.get("automation_ref"),
            "requested_at": utcnow(),
        }

    def request_shutdown(
        self, task_id: str, *, reason: str = "expiry", expected_state_version=None
    ) -> dict:
        """Explicitly record a pending shutdown intent (e.g. on detected expiry). Idempotent. Records
        the intent for ANY execution; the host adapter only pauses automation when one is named."""
        with self.store.lock(task_id):
            rec = self._require_managed(task_id)
            self._check_version(rec, expected_state_version)
            self._record_shutdown_intent(rec, reason=reason)
            rec = self._write(task_id, rec)
            self._audit(task_id, "request_shutdown", {"reason": reason})
            return {"execution": self._summary(rec), "shutdown": rec.get("shutdown")}

    def note_expiry_shutdown(self, task_id: str) -> dict:
        """If the execution is expired and has no reconciled/pending shutdown yet, record a pending
        expiry shutdown intent. A no-op (no write) when not expired or already recorded — safe to call
        from a host reconciliation sweep."""
        with self.store.lock(task_id):
            rec = self.store.read(self._exec_rel(task_id))
            if rec is None:
                return {"noted": False, "reason": "execution_not_found"}
            if not self._expired(rec):
                return {"noted": False, "reason": "not_expired"}
            cur = rec.get("shutdown")
            if cur and cur.get("status") in ("pending", "reconciled"):
                return {"noted": False, "reason": "already_recorded", "shutdown": cur}
            self._record_shutdown_intent(rec, reason="expiry")
            rec = self._write(task_id, rec)
            self._audit(task_id, "request_shutdown", {"reason": "expiry"})
            return {"noted": True, "shutdown": rec.get("shutdown")}

    def reconcile_shutdown(
        self,
        task_id: str,
        *,
        outcome: str = "paused",
        detail=None,
        expected_state_version=None,
    ) -> dict:
        """Mark the pending shutdown reconciled — the host actually paused the named automation
        through the supported tool. Idempotent; a no pending shutdown is an error the host can log."""
        with self.store.lock(task_id):
            rec = self._require_managed(task_id)
            self._check_version(rec, expected_state_version)
            cur = rec.get("shutdown")
            if not cur:
                raise ProtocolError(
                    "no_pending_shutdown", task_id, "no shutdown intent to reconcile"
                )
            if cur.get("status") == "reconciled":
                return {"idempotent": True, "execution": self._summary(rec)}
            cur["status"] = "reconciled"
            cur["outcome"] = outcome
            cur["detail"] = detail
            cur["reconciled_at"] = utcnow()
            rec = self._write(task_id, rec)
            self._audit(task_id, "reconcile_shutdown", {"outcome": outcome})
            return {"idempotent": False, "execution": self._summary(rec)}

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
            limits = dict(rec["limits"])  # apply onto a copy, validate, then commit
            applied = {}
            for k, v in changes.items():
                if k not in self.LOWER_ONLY and k not in self.TASK_SETTABLE:
                    raise ProtocolError("unknown_limit", k, f"unknown limit field {k}")
                old = limits.get(k)
                limits[k] = v if (k == "expires_at" or v is None) else int(v)
                applied[k] = {"old": old, "new": limits[k]}
            # validate the RESULTING limits before committing (review R5): an authorized change can
            # never install an invalid expiry / negative counter / non-positive review deadline.
            self._validate_limits(limits)
            if not rec.get("attended", True) and not limits.get("expires_at"):
                raise ProtocolError(
                    "unattended_requires_expiry",
                    task_id,
                    "an unattended execution cannot drop to a null expiry",
                )
            rec["limits"] = limits
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


# ---------------------------------------------------------------------- host shutdown adapter (R5)
def reconcile_pending_shutdown(em: ExecutionManager, task_id: str, pause_fn) -> dict:
    """Host-facing adapter: pause the automation named by a task's PENDING shutdown intent through the
    supported automation tool, then mark it reconciled. This is the crash-safe seam (review R5): the
    pending intent is durable, so a host that crashed between recording a terminal/expired state and
    pausing the scheduler simply re-runs this on restart and completes the pause exactly once.

    ``pause_fn(automation_ref) -> Any`` is the caller-supplied binding to the supported automation
    tool (e.g. the scheduler's pause). This adapter adds NO daemon and writes NO scheduler config; it
    only reconciles intent to action. Returns a small dict describing what happened.
    """
    rec = em.read_execution(task_id)
    if rec is None:
        return {"acted": False, "reason": "execution_not_found"}
    sd = rec.get("shutdown")
    if not sd or sd.get("status") != "pending":
        return {"acted": False, "reason": "no_pending_shutdown"}
    automation_ref = sd.get("automation_ref")
    if not automation_ref:
        # nothing to pause (attended task); reconcile the intent so the sweep does not loop on it
        em.reconcile_shutdown(task_id, outcome="no_automation")
        return {"acted": False, "reason": "no_automation_ref", "reconciled": True}
    try:
        result = pause_fn(automation_ref)
    except (
        Exception
    ) as e:  # the pause failed; leave the intent PENDING for the next sweep
        return {
            "acted": False,
            "reason": "pause_failed",
            "error": f"{type(e).__name__}: {e}",
        }
    em.reconcile_shutdown(
        task_id, outcome="paused", detail={"automation_ref": automation_ref}
    )
    return {"acted": True, "automation_ref": automation_ref, "pause_result": result}
