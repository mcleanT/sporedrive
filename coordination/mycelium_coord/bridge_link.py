"""Bridge linkage: deliver a coordination message as a short cmux notification through the EXISTING
codex-claude bridge, correlating the coordination message with the bridge request so the two are one
history (D4 / contract §5).

Adapter safety (adapter review):
* the recipient is resolved to ONE addressed Claude participant (role/'all' that fans out is refused
  for this single-recipient transport);
* BEFORE any send, the supplied bridge binding is loaded and its native identity (worktree, Claude
  session, declared controller) is compared to that recipient — a valid binding to a DIFFERENT
  session/worktree is refused, not delivered;
* the bridge request identity is namespaced by task AND message (per-task ids would otherwise collide
  across tasks on one binding);
* only a bridge accepted/completed outcome is recorded as delivered; a busy refusal or uncertain
  outcome stays pending (never a fabricated delivery); a missing bridge returns bridge_unavailable.
The bridge's own lease/identity checks are preserved — this never re-implements delivery.
"""

from __future__ import annotations

import hashlib
import os
import sys

from .execution import (
    ExecutionManager,
    ACTIONABLE_KINDS,
    STATUS_COMPLETED,
    STATUS_CLOSED,
)
from .model import ProtocolError, utcnow
from .store import ID_RE

BRIDGE_ACCEPTED = ("accepted", "completed")
BRIDGE_UNCERTAIN = (
    "uncertain",
    "uncertain_foreign",
    "staged_unverified",
    "submitted_unconfirmed",
    "delivering",
    "lease_lost",
)
_SINGLE_LINE_MAX = 160


def _ensure_bridge_path() -> None:
    for cand in (
        os.environ.get("MYCELIUM_BRIDGE_PATH"),
        os.path.join(os.path.dirname(__file__), "..", "..", "bridge"),
    ):
        if cand and os.path.isdir(cand) and os.path.abspath(cand) not in sys.path:
            sys.path.insert(0, os.path.abspath(cand))


def _import_bridge():
    """Import the bridge core, best-effort. Returns (Bridge, BridgeError) or (None, None)."""
    try:
        from cmux_bridge.core import Bridge, BridgeError  # type: ignore

        return Bridge, BridgeError
    except Exception:
        _ensure_bridge_path()
        try:
            from cmux_bridge.core import Bridge, BridgeError  # type: ignore

            return Bridge, BridgeError
        except Exception:
            return None, None


def _read_binding(binding_id: str):
    """Read the bridge binding record from the bridge's own state store (no cmux contact). Returns
    the dict or None; returns None (not raise) if the bridge state package is unavailable."""
    try:
        from cmux_bridge.state import StateStore  # type: ignore
    except Exception:
        _ensure_bridge_path()
        try:
            from cmux_bridge.state import StateStore  # type: ignore
        except Exception:
            return None
    try:
        return StateStore().read(f"bindings/{binding_id}.json")
    except Exception:
        return None


def _same_path(a, b) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.realpath(str(a)) == os.path.realpath(str(b))
    except Exception:
        return str(a) == str(b)


def _norm_session(s: str) -> str:
    s = str(s or "").strip().lower()
    return s[len("claude-") :] if s.startswith("claude-") else s


def _session_match(binding_session, host: dict) -> bool:
    """EXACT match of the full native session id (after documented host-prefix normalization).
    An abbreviated/prefix id or the PID (native_id) is NOT authority to route to a live pane
    (adapter identity follow-up)."""
    b = _norm_session(binding_session)
    s = _norm_session(host.get("session"))
    return bool(b and s and b == s)


def _resolve_recipient(co, task_id: str, msg: dict) -> dict:
    """Resolve the message's addressing to exactly ONE attached, non-detached Claude participant.
    A role/'all' that fans out to more than one is refused for this single-recipient transport."""
    addressed = [
        p
        for p in co.list_participants(task_id)
        if not p.get("detached") and co._addressed_to(msg, p)
    ]
    claude = [p for p in addressed if (p.get("host") or {}).get("host") == "claude"]
    if len(claude) == 1:
        return claude[0]
    if not claude:
        raise ProtocolError(
            "no_addressed_recipient",
            msg["message_id"],
            "no attached Claude participant is addressed by this message",
        )
    raise ProtocolError(
        "ambiguous_recipient",
        msg["message_id"],
        "message addresses multiple recipients; bridge transport needs one specific "
        "participant_id",
    )


def _verify_binding_identity(binding: dict, recip: dict, controller_id: str) -> None:
    """Fail BEFORE any send if the bridge binding does not match the addressed recipient."""
    rhost = recip.get("host") or {}
    if rhost.get("host") != "claude":
        raise ProtocolError(
            "recipient_not_claude",
            recip.get("participant_id", ""),
            "cmux/bridge transport delivers to a Claude executor only",
        )
    if (binding.get("role") or "writer") != "writer":
        raise ProtocolError(
            "binding_not_writer",
            binding.get("binding_id", ""),
            "notification requires a writer binding",
        )
    if not _same_path(binding.get("worktree_realpath"), recip.get("worktree_realpath")):
        raise ProtocolError(
            "binding_identity_mismatch",
            binding.get("binding_id", ""),
            "binding worktree does not match the addressed recipient's worktree",
        )
    if not _session_match(binding.get("claude_session_id"), rhost):
        raise ProtocolError(
            "binding_identity_mismatch",
            binding.get("binding_id", ""),
            "binding Claude session does not match the addressed recipient",
        )
    for key in ("surface_uuid", "workspace_uuid"):
        want = rhost.get(key)
        if want and binding.get(key) and str(want) != str(binding.get(key)):
            raise ProtocolError(
                "binding_identity_mismatch",
                binding.get("binding_id", ""),
                f"binding {key} does not match the addressed recipient",
            )
    if (
        controller_id
        and binding.get("controller_id")
        and controller_id != binding.get("controller_id")
    ):
        raise ProtocolError(
            "controller_mismatch",
            binding.get("binding_id", ""),
            "declared controller is not the binding's controller/writer",
        )


def _bridge_request_id(task_id: str, message_id: str) -> str:
    """Bridge request_id (1..80 [A-Za-z0-9._:-]) namespaced by task AND message, so the same core
    message id under two tasks does not collide in bridge request history. Deterministic (stable on
    retry)."""
    # length-prefix makes the split point unambiguous even though task/message may both contain
    # '.' — plain "{task}.{message}" is NOT injective ("a.b"+"c" == "a"+"b.c").
    combined = f"{len(task_id)}-{task_id}.{message_id}"
    if len(combined) <= 80 and ID_RE.fullmatch(combined):
        return combined
    return (
        "coordmsg-"
        + hashlib.sha256(f"{task_id}\x00{message_id}".encode()).hexdigest()[:16]
    )


def _notification_line(task_id: str, msg: dict) -> str:
    line = (
        f"Coord mail {msg['message_id']} [{msg.get('kind')}] task={task_id} "
        f"rev={msg.get('task_revision')} to={msg.get('recipient')} — read: "
        f"mycelium-coord inbox {task_id} <you>"
    )
    return line if len(line) <= _SINGLE_LINE_MAX else line[:_SINGLE_LINE_MAX]


def notify_via_bridge(
    co,
    *,
    task_id: str,
    message_id: str,
    binding_id: str,
    controller_id: str,
    expected_revision: int,
    accept_timeout_s: float = 12.0,
    execution_action_id: str | None = None,
) -> dict:
    msg = co.get_message(task_id, message_id)
    if not msg:
        raise ProtocolError(
            "unknown_message",
            message_id,
            "cannot notify for a message that does not exist",
        )
    req_id = _bridge_request_id(task_id, message_id)
    corr_rel = f"tasks/{task_id}/correlations/{message_id}.json"

    def _record(outcome: dict, recipient_id: str = "") -> None:
        rec = {
            "task_id": task_id,
            "message_id": message_id,
            "bridge_request_id": req_id,
            "readable_ids": {"task": task_id, "message": message_id},
            "binding_id": binding_id,
            "controller_id": controller_id,
            "recipient": recipient_id,
            "at": utcnow(),
            **outcome,
        }
        co.store.write(corr_rel, rec)
        co.store.append_receipt(
            f"tasks/{task_id}/audit.jsonl",
            "bridge_notify",
            {
                "message_id": message_id,
                "bridge_request_id": req_id,
                "outcome": outcome.get("state"),
            },
            lock_name=task_id,
        )

    Bridge, BridgeError = _import_bridge()
    if Bridge is None:
        out = {
            "delivered": False,
            "state": "bridge_unavailable",
            "detail": "cmux_bridge not importable on this host",
        }
        _record(out)
        return {"message_id": message_id, "bridge_request_id": req_id, **out}

    # Resolve the addressed recipient and verify the binding BEFORE any send (adapter review 1).
    recip = _resolve_recipient(co, task_id, msg)
    binding = _read_binding(binding_id)
    if binding is None:
        raise ProtocolError(
            "unknown_binding",
            binding_id,
            "bridge binding not found; bind the executor session first",
        )
    _verify_binding_identity(binding, recip, controller_id)

    # Execution gate (PLAN section 3, review R1). A task is MANAGED once an execution record exists;
    # managed-ness is durable, not a per-call field, so a work dispatch cannot slip past the bound by
    # omitting metadata. A MANAGED work dispatch (an actionable kind, or one that names a reservation)
    # requires a valid reservation BOUND to THIS message: the claim fixes the binding (one reservation
    # funds exactly one concrete dispatch, never a second message id or a review) and a FRESH recheck
    # immediately before the cmux mutation catches a pause/expiry that landed after staging. An
    # unmanaged task and a managed NON-actionable notice (ack/status/closure) pass unchanged — message
    # delivery state stays separate from permission to execute.
    em = ExecutionManager(co.store)
    managed = em.is_managed(task_id)
    # The message carries its AUTHORITATIVE disposition, persisted by coord.send at publication
    # (review R1): whether it dispatches work (actionable) and the reservation it named. Fall back to
    # the intrinsic kind for a legacy message that predates the field. The persisted action id is
    # authoritative over any caller-passed hint, so notify uses the SAME binding coord.send claimed.
    msg_actionable = msg.get("actionable")
    if msg_actionable is None:
        msg_actionable = msg.get("kind") in ACTIONABLE_KINDS
    action_id = msg.get("execution_action_id") or execution_action_id
    managed_dispatch = managed and (msg_actionable or action_id is not None)

    # No-wake (review R1/R4): a NON-actionable notice on a COMPLETED (terminal) or CLOSED (coverage
    # complete) managed execution must NOT press the bridge and wake an idle executor. It stays in the
    # durable inbox/backlog for the executor to read on its own. Safe read/ack/evidence/stop are
    # unaffected; the notice keeps its explicit actionable=false metadata.
    if managed and not managed_dispatch:
        _rec = em.read_execution(task_id)
        _status = (_rec or {}).get("status")
        if _status in (STATUS_COMPLETED, STATUS_CLOSED):
            out = {
                "delivered": False,
                "state": "inbox_only_non_actionable",
                "actionable": False,
                "execution_status": _status,
            }
            _record(out, recip.get("participant_id", ""))
            return {"message_id": message_id, "bridge_request_id": req_id, **out}

    def _gate_refuse(reason: str, gate: dict | None = None) -> dict:
        out = {
            "delivered": False,
            "state": "refused_by_execution_gate",
            "reason": reason,
            "execution_action_id": action_id,
            "execution_status": (gate or {}).get("status"),
        }
        if gate and gate.get("bound_to") is not None:
            out["bound_to"] = gate.get("bound_to")
        _record(out, recip.get("participant_id", ""))
        return {"message_id": message_id, "bridge_request_id": req_id, **out}

    if managed_dispatch:
        if action_id is None:
            # a managed work dispatch with no reservation must fail closed (never deliver by omission)
            return _gate_refuse("managed_dispatch_requires_reservation")
        # claim the reservation for THIS dispatch identity (the message id), under the per-task lock;
        # the lock is released before any transport wait. coord.send already bound it at publication,
        # so this is normally the idempotent same-identity reconciliation (no re-charge).
        claim = em.claim_dispatch(
            task_id, action_id=action_id, dispatch_identity=message_id
        )
        if not claim.get("ok"):
            return _gate_refuse(claim.get("reason"), claim)

    text = _notification_line(task_id, msg)

    # The AUTHORITATIVE post-staging gate: the bridge invokes this immediately before its real Enter
    # keystroke, i.e. AFTER its own staging/waits, so a pause/expiry that lands DURING submit's staging
    # still withholds the actual cmux mutation (review R1 amendment). It re-reads durable state and
    # raises a BridgeError to withhold the Enter (nothing is sent; staged text is left for reconcile).
    pre_enter_gate = None
    if managed_dispatch:
        # a cheap early-out before staging even begins (a pause already landed before this notify)
        early = em.dispatch_check(task_id, action_id, dispatch_identity=message_id)
        if not early.get("ok"):
            return _gate_refuse(early.get("reason"), early)

        def pre_enter_gate():
            g = em.dispatch_check(task_id, action_id, dispatch_identity=message_id)
            if not g.get("ok"):
                raise BridgeError(
                    "refused_by_execution_gate",
                    f"execution gate refused immediately before Enter: {g.get('reason')}",
                    gate_reason=g.get("reason"),
                    execution_status=g.get("status"),
                    bound_to=g.get("bound_to"),
                )

    try:
        res = Bridge().submit(
            binding_id,
            req_id,
            text,
            int(expected_revision),
            "task",
            float(accept_timeout_s),
            pre_enter_gate=pre_enter_gate,
        )
    except BridgeError as e:
        if getattr(e, "code", "") == "refused_by_execution_gate":
            # the post-staging gate withheld the Enter — this is a permission refusal, not a transport
            # failure, so record it as such (nothing was mutated).
            detail = getattr(e, "detail", {}) or {}
            return _gate_refuse(
                detail.get("gate_reason") or "execution_gate_refused",
                {
                    "status": detail.get("execution_status"),
                    "bound_to": detail.get("bound_to"),
                },
            )
        out = {
            "delivered": False,
            "state": "pending",
            "refused": e.code,
            "message": e.message,
            "detail": getattr(e, "detail", {}),
        }
        _record(out, recip.get("participant_id", ""))
        return {"message_id": message_id, "bridge_request_id": req_id, **out}
    except Exception as e:
        out = {
            "delivered": False,
            "state": "uncertain",
            "error": f"{type(e).__name__}: {e}",
        }
        _record(out, recip.get("participant_id", ""))
        return {"message_id": message_id, "bridge_request_id": req_id, **out}

    status = (res or {}).get("status")
    summary = {
        k: res.get(k)
        for k in (
            "status",
            "request_id",
            "text_sha256",
            "delivered_text_sha256",
            "send_result",
        )
        if k in (res or {})
    }
    if status in BRIDGE_ACCEPTED:
        co.mark_delivered(
            task_id,
            message_id,
            via="cmux-bridge",
            recipient=recip.get("participant_id"),
            detail={
                "bridge_request_id": req_id,
                "status": status,
                "recipient": recip.get("participant_id"),
                "delivered_text_sha256": res.get("delivered_text_sha256"),
            },
        )
        out = {"delivered": True, "state": "delivered", "bridge_result": summary}
    elif status in BRIDGE_UNCERTAIN:
        out = {"delivered": False, "state": "uncertain", "bridge_result": summary}
    else:
        out = {"delivered": False, "state": "unknown", "bridge_result": summary}
    _record(out, recip.get("participant_id", ""))
    return {"message_id": message_id, "bridge_request_id": req_id, **out}
