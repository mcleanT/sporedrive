"""Codex desktop idle-wake linkage: deliver an ADDRESSED coordination message to an idle Codex
desktop session by waking its owning thread through the app's OWN already-running IPC router, so the
peer can act on it through a concrete SporeDrive entry point (CLI ``wake-peer``, MCP
``coord_wake_peer``). This is the product integration around the guarded seam in ``codex_ipc_wake`` —
NOT a second server, daemon or dispatcher, and NOT a live wake by default.

It mirrors ``bridge_link.notify_via_bridge`` for the desktop-IPC route:

  * the message is resolved to exactly ONE addressed, attached CODEX participant (a role/'all' that
    fans out is refused for this single-recipient route);
  * the SAME managed-execution gate applies — a non-actionable notice on a completed/closed task does
    NOT wake, and a managed work dispatch requires a reservation claim plus a FRESH pre-send dispatch
    check, so a pause/expiry/closure that landed before the send withholds the wake;
  * the short opaque ``sd:`` handle is the correlation carried into the woken turn; the ORIGINAL task
    settings (task, revision, recipient, native session/thread) are captured from the addressed
    participant, never guessed from focus/title;
  * existing-owner and app-build guards run READ-ONLY before any send — owner-discovery must find a
    live owning client and the installed build must match the pinned hash;
  * by default the seam is NOT armed, so the entry point PREPARES the wake (frame + guards + gates)
    and returns ``prepared_not_armed`` with wake status still ``not_established``. Arming is a
    reserved, human-authorized canary; a known active/root thread refuses an armed wake unless the
    ready/arm/yield handshake explicitly authorizes it. Even armed, an absent/uncertain response is
    retained under the SAME request id as unknown — never upgraded to a delivered wake, never resent.

Every attempt (a refusal, an unavailable route, or a prepared-but-unarmed plan) is recorded to a
durable wake correlation/receipt, so a queued unknown-send identity is preserved for reconciliation.
"""

from __future__ import annotations

from .bridge_link import _notification_line
from .codex_ipc_wake import (
    CodexIpcWakeAdapter,
    WAKE_SCHEMA_LIMITATION,
    WakeError,
    build_turn_start,
)
from .execution import (
    ACTIONABLE_KINDS,
    STATUS_CLOSED,
    STATUS_COMPLETED,
    ExecutionManager,
)
from .model import ProtocolError, utcnow

# The supervisor's own live root thread (contract additional evidence). An ARMED wake to it is
# refused unless the caller passes an explicit ready/arm/yield handshake authorization — "no
# active-root wake" is encoded here, not left to convention.
PROTECTED_CONVERSATIONS = frozenset({"01a07d32-9171-7960-b4fb-32be3018bed9"})


def _resolve_codex_recipient(co, task_id: str, msg: dict) -> dict:
    """Resolve the message's addressing to exactly ONE attached, non-detached CODEX participant."""
    addressed = [
        p
        for p in co.list_participants(task_id)
        if not p.get("detached") and co._addressed_to(msg, p)
    ]
    codex = [p for p in addressed if (p.get("host") or {}).get("host") == "codex"]
    if len(codex) == 1:
        return codex[0]
    if not codex:
        raise ProtocolError(
            "no_addressed_recipient",
            msg["message_id"],
            "no attached Codex participant is addressed by this message",
        )
    raise ProtocolError(
        "ambiguous_recipient",
        msg["message_id"],
        "message addresses multiple recipients; wake needs one specific participant_id",
    )


def _conversation_for(recip: dict, conversation_id: str | None) -> tuple[str, str]:
    """Pick the IPC conversation/thread id to wake, and report where it came from. An explicit
    caller value wins; otherwise the addressed participant's native session id is used. The
    coordination-session -> IPC-thread identity is a host-specific mapping this layer does not
    invent — an unresolved id is reported, and read-only owner-discovery simply fails to find an
    owner rather than a wake being sent to a guessed thread."""
    if conversation_id:
        return conversation_id, "caller"
    host = recip.get("host") or {}
    sess = host.get("session")
    if sess:
        return str(sess), "recipient_native_session"
    return "", "unresolved"


def wake_peer(
    co,
    *,
    task_id: str,
    message_id: str,
    controller_id: str = "",
    conversation_id: str | None = None,
    execution_action_id: str | None = None,
    armed: bool = False,
    dry_run: bool = True,
    handshake_authorized: bool = False,
    adapter: CodexIpcWakeAdapter | None = None,
    timeout_s: float = 12.0,
) -> dict:
    """Prepare (and, only when explicitly armed + authorized, fire) an addressed idle-wake of the
    Codex desktop session that owns the addressed participant's thread. Returns a structured wake
    receipt; the wake route stays ``not_established`` until a live start is validated in a reserved
    canary. Never raises for a gate/route refusal — it records and returns a structured outcome so a
    transport can reconcile; it raises only for a malformed request (unknown message/recipient)."""
    msg = co.get_message(task_id, message_id)
    if not msg:
        raise ProtocolError(
            "unknown_message",
            message_id,
            "cannot wake for a message that does not exist",
        )
    recip = _resolve_codex_recipient(co, task_id, msg)
    participant_id = recip.get("participant_id", "")
    conv, conv_source = _conversation_for(recip, conversation_id)

    def _base(**extra) -> dict:
        return {
            "task_id": task_id,
            "message_id": message_id,
            "recipient": participant_id,
            "conversation_id": conv,
            "conversation_id_source": conv_source,
            "route": "desktop_ipc",
            "wake_status": "not_established",
            "schema_limitation": WAKE_SCHEMA_LIMITATION,
            "at": utcnow(),
            **extra,
        }

    def _record(receipt: dict) -> dict:
        # retained even for a refusal / unavailable / prepared-not-armed plan, so a queued unknown
        # send identity is never lost.
        co.store.write(f"tasks/{task_id}/wake/{message_id}.json", receipt)
        co.store.append_receipt(
            f"tasks/{task_id}/audit.jsonl",
            "codex_wake",
            {
                "message_id": message_id,
                "recipient": participant_id,
                "outcome": receipt.get("state"),
                "wake_status": receipt.get("wake_status"),
            },
            lock_name=task_id,
        )
        return receipt

    # ---- managed-execution gate (identical policy to notify_via_bridge) ---------------------------
    em = ExecutionManager(co.store)
    managed = em.is_managed(task_id)
    msg_actionable = msg.get("actionable")
    if msg_actionable is None:
        msg_actionable = msg.get("kind") in ACTIONABLE_KINDS
    # the message carries its authoritative action id (persisted by coord.send); fall back to the
    # caller hint for a legacy message that predates the field, exactly like notify_via_bridge.
    action_id = msg.get("execution_action_id") or execution_action_id
    managed_dispatch = managed and (msg_actionable or action_id is not None)

    if managed and not managed_dispatch:
        status = (em.read_execution(task_id) or {}).get("status")
        if status in (STATUS_COMPLETED, STATUS_CLOSED):
            # a non-actionable notice on a terminal/closed execution stays in the durable inbox; it
            # must not wake an idle peer.
            return _record(
                _base(
                    state="inbox_only_non_actionable",
                    prepared=False,
                    actionable=False,
                    execution_status=status,
                )
            )

    if managed_dispatch:
        if action_id is None:
            return _record(
                _base(
                    state="refused_by_execution_gate",
                    prepared=False,
                    reason="managed_dispatch_requires_reservation",
                )
            )
        claim = em.claim_dispatch(
            task_id, action_id=action_id, dispatch_identity=message_id
        )
        if not claim.get("ok"):
            return _record(
                _base(
                    state="refused_by_execution_gate",
                    prepared=False,
                    reason=claim.get("reason"),
                    execution_status=claim.get("status"),
                )
            )
        gate = em.dispatch_check(task_id, action_id, dispatch_identity=message_id)
        if not gate.get("ok"):
            return _record(
                _base(
                    state="refused_by_execution_gate",
                    prepared=False,
                    reason=gate.get("reason"),
                    execution_status=gate.get("status"),
                )
            )

    # ---- correlation: mint (idempotently) the short handle for this exact addressing --------------
    handle = None
    try:
        hrec = co.mint_handle(
            task_id,
            message_id,
            participant_id,
            int(msg.get("task_revision", 0)),
            controller_id=controller_id,
        )
        handle = hrec.get("handle")
    except Exception:
        handle = None
    line = _notification_line(task_id, msg, handle)

    if not conv:
        return _record(
            _base(
                state="wake_unavailable",
                prepared=False,
                reason="unresolved_conversation_id",
                handle=handle,
            )
        )

    # ---- read-only guards: existing owner + pinned app build -------------------------------------
    adapter = adapter or CodexIpcWakeAdapter()
    probe = adapter.capability_probe(conv, timeout_s=timeout_s)
    build = probe.get("app_build") or adapter.verify_app_build()
    if not build.get("verified"):
        return _record(
            _base(
                state="wake_unavailable",
                prepared=False,
                reason="app_build_unverified",
                handle=handle,
                app_build=build,
            )
        )
    if probe.get("status") != "owner_discovered":
        return _record(
            _base(
                state="wake_unavailable",
                prepared=False,
                reason=probe.get("status") or "owner_not_discovered",
                handle=handle,
                probe={k: probe.get(k) for k in ("status", "error", "elapsed_seconds")},
            )
        )
    owner_client_id = probe.get("owner_client_id")

    # ---- build the wake frame (correlation-carrying start turn) -----------------------------------
    client_user_message_id = f"sporedrive-wake-{task_id}.{message_id}"
    turn_start = build_turn_start(line, client_user_message_id=client_user_message_id)
    frame = adapter.build_start_turn_frame(conv, turn_start, owner_client_id)

    if armed and conv in PROTECTED_CONVERSATIONS and not handshake_authorized:
        # "no active-root wake": an armed wake to the known live root refuses without explicit
        # handshake authorization. The prepared frame is still recorded for the reserved canary.
        return _record(
            _base(
                state="refused_active_root_wake",
                prepared=True,
                reason="armed_wake_to_protected_conversation_requires_handshake",
                handle=handle,
                owner_client_id=owner_client_id,
                request_id=frame["requestId"],
                notification_line=line,
            )
        )

    # ---- the seam. Default (armed=False) PREPARES; it never fires ---------------------------------
    try:
        result = adapter.start_turn(
            conv, turn_start, owner_client_id, armed=armed, dry_run=dry_run
        )
    except WakeError as e:
        if e.code == "wake_not_armed":
            # the expected default: guards + gates passed, frame built, seam left un-armed.
            return _record(
                _base(
                    state="prepared_not_armed",
                    prepared=True,
                    handle=handle,
                    owner_client_id=owner_client_id,
                    request_id=frame["requestId"],
                    notification_line=line,
                    arm_procedure=(
                        "reserved canary only: ready/arm/yield handshake with the supervisor, then "
                        "call with armed=True, dry_run=False (and handshake_authorized=True for a "
                        "protected/root thread); count the resumed Codex turn in the acceptance "
                        "allowance"
                    ),
                )
            )
        return _record(
            _base(
                state="wake_unavailable",
                prepared=True,
                reason=e.code,
                handle=handle,
                detail=e.to_dict(),
            )
        )

    # armed + fired. Offline this branch is unreachable (the socket path is guarded); live it retains
    # an uncertain outcome as unknown under the same request id, never a confirmed delivery.
    if result.get("dry_run"):
        return _record(
            _base(
                state="prepared_armed_dry_run",
                prepared=True,
                handle=handle,
                owner_client_id=owner_client_id,
                request_id=frame["requestId"],
                notification_line=line,
            )
        )
    confirmed = (
        result.get("response") is not None and result.get("status") != "uncertain"
    )
    return _record(
        _base(
            state="sent_confirmed" if confirmed else "sent_uncertain",
            prepared=True,
            sent=True,
            handle=handle,
            owner_client_id=owner_client_id,
            request_id=result.get("request_id") or frame["requestId"],
            notification_line=line,
            # a confirmed live start is the ONLY thing that would establish the route; anything short
            # of it keeps wake_status not_established.
            wake_status="established" if confirmed else "not_established",
            response=result.get("response"),
        )
    )
