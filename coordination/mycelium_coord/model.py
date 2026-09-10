"""Provider-neutral coordination protocol: schema, kinds, roles, states, and validation.

One schema for both native hosts (no divergent Claude/Codex semantics). A message records a schema
version, message id, task id, sender/recipient participant ids, native host+session identity, kind,
task revision, creation time, correlation/reply-to id, content hash, and EITHER a bounded text body
OR an immutable artifact reference. A message can never create new owner authorization — authorization
references are carried on the task/checkpoint, and are inputs, not grants.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone

from .store import StoreError, valid_id

SCHEMA_VERSION = "1.0.0"

# Roles are protocol checks, not OS-permission checks (contract §3).
ROLES = ("supervisor", "executor", "observer")

# Message kinds (contract §Minimum protocol).
KINDS = (
    "task",
    "amendment",
    "review_finding",
    "progress",
    "blocker",
    "question",
    "checkpoint_request",
    "checkpoint_ready",
    "completion_receipt",
    "acknowledgment",
)

# Which message KINDS default to actionable (they DISPATCH executable work) on a managed task. This
# is the message-kind projection of execution.ACTIONABLE_KINDS (which also lists internal reservation
# kinds that are not valid message kinds); execution.py stays the authority, and model.py cannot
# import it without a cycle (execution imports ProtocolError from here). A caller may override per
# message via build_message(actionable=...); a non-actionable kind is a status/notice, not work.
_ACTIONABLE_MESSAGE_KINDS = frozenset({"task"})

# The four DISTINCT lifecycle states of a message w.r.t. a recipient. They never collapse into one
# another or into a falsy value (measurement-integrity): persisted != delivered != acknowledged !=
# completed. "completed" is a property of a completion_receipt referencing concrete artifacts, not a
# flag anyone may set on an ordinary message.
STATE_PERSISTED = "persisted"
STATE_DELIVERED = "delivered"  # exposed past a recipient cursor or bridge-notified
STATE_ACKNOWLEDGED = "acknowledged"  # an explicit ack record exists
STATE_COMPLETION_CLAIMED = (
    "completion_claimed"  # a bound completion claim exists but its
)
# artifact evidence is NOT yet verified (an unverified receipt is not completion).
STATE_COMPLETED = "completed"  # a bound completion whose artifact evidence VERIFIED
STATES = (
    STATE_PERSISTED,
    STATE_DELIVERED,
    STATE_ACKNOWLEDGED,
    STATE_COMPLETION_CLAIMED,
    STATE_COMPLETED,
)

RECIPIENT_ALL = (
    "all"  # broadcast within THIS task only (never cross-task / unsolicited)
)

MAX_TEXT_CHARS = (
    8000  # a longer body must be delivered as an immutable artifact_ref instead
)
MAX_ARTIFACT_REF = 4096


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def gen_id(prefix: str) -> str:
    """Convenience id (caller may instead supply a deterministic id for idempotency)."""
    return f"{prefix}-{secrets.token_hex(8)}"


def content_hash(
    *,
    kind: str,
    task_id: str,
    task_revision,
    sender: str,
    recipient: str,
    reply_to,
    correlation_id,
    text,
    artifact_ref,
    artifacts,
    execution_action_id=None,
    actionable=None,
) -> str:
    """Stable hash of the semantic payload. Idempotency key content: reusing a message_id with the
    SAME hash is a no-op; reusing it with a DIFFERENT hash is rejected. Excludes created_at/sender
    session nonces so an honest retry of the same message matches.

    Managed work-dispatch identity (execution_action_id + actionable) enters the hash ONLY for a
    message that actually names a reservation (execution_action_id is not None). This keeps every
    unmanaged/legacy message and every non-actionable managed notice hashing EXACTLY as before
    (review R1), while making a managed dispatch's action identity conflict-detectable: reusing a
    message id with a DIFFERENT action is an idempotency conflict, and an honest replay of the SAME
    dispatch matches (so notify/native reconciliation never re-claims)."""
    payload = {
        "kind": kind,
        "task_id": task_id,
        "task_revision": task_revision,
        "sender": sender,
        "recipient": recipient,
        "reply_to": reply_to or None,
        "correlation_id": correlation_id or None,
        "text": text if text is not None else None,
        "artifact_ref": artifact_ref or None,
        "artifacts": list(artifacts) if artifacts else None,
    }
    if execution_action_id is not None:
        payload["execution_action_id"] = execution_action_id
        payload["actionable"] = bool(actionable)
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class ProtocolError(StoreError):
    """A protocol-level rejection (invalid field, bad kind/role, idempotency conflict)."""


def _req(cond: bool, code: str, msg: str) -> None:
    if not cond:
        raise ProtocolError(code, "", msg)


def validate_participant(p: dict) -> dict:
    _req(isinstance(p, dict), "invalid_participant", "participant must be an object")
    _req(valid_id(p.get("participant_id", "")), "invalid_id", "participant_id")
    _req(p.get("role") in ROLES, "invalid_role", f"role must be one of {ROLES}")
    _req(valid_id(p.get("task_id", "")), "invalid_id", "task_id")
    host = p.get("host") or {}
    _req(isinstance(host, dict), "invalid_participant", "host must be an object")
    # A valid NATIVE identity is required: which host implementation, and a session/task id.
    _req(
        bool(host.get("host")),
        "invalid_participant",
        "host.host (native host kind) required",
    )
    _req(
        bool(host.get("session") or host.get("native_id")),
        "invalid_participant",
        "host.session/native_id (native session identity) required",
    )
    # worktree_realpath is REQUIRED and is the canonical identity, never inferred from cwd (D3).
    _req(
        bool(p.get("worktree_realpath")),
        "invalid_participant",
        "worktree_realpath required",
    )
    return p


def build_message(
    *,
    message_id: str,
    task_id: str,
    sender: str,
    recipient: str,
    kind: str,
    task_revision,
    host: dict | None = None,
    reply_to: str | None = None,
    correlation_id: str | None = None,
    text: str | None = None,
    artifact_ref: str | None = None,
    artifacts: list | None = None,
    created_at: str | None = None,
    execution_action_id: str | None = None,
    actionable: bool | None = None,
) -> dict:
    _req(valid_id(message_id), "invalid_id", "message_id")
    _req(valid_id(task_id), "invalid_id", "task_id")
    _req(valid_id(sender), "invalid_id", "sender participant_id")
    _req(
        recipient == RECIPIENT_ALL or recipient in ROLES or valid_id(recipient),
        "invalid_recipient",
        "recipient must be a participant_id, a role, or 'all'",
    )
    _req(kind in KINDS, "invalid_kind", f"kind must be one of {KINDS}")
    has_text = text is not None
    has_ref = bool(artifact_ref)
    _req(has_text ^ has_ref, "invalid_body", "exactly one of text / artifact_ref")
    if has_text:
        _req(
            len(text) <= MAX_TEXT_CHARS,
            "text_too_long",
            f"inline text >{MAX_TEXT_CHARS} chars; use an artifact_ref",
        )
    if has_ref:
        _req(
            len(artifact_ref) <= MAX_ARTIFACT_REF,
            "artifact_ref_too_long",
            "artifact_ref too long",
        )
    if reply_to is not None:
        _req(valid_id(reply_to), "invalid_id", "reply_to")
    # A completion_receipt must BIND to the request it completes and carry concrete artifacts —
    # an arbitrary kind string/text is not completion evidence (core review finding 1).
    if kind == "completion_receipt":
        _req(
            reply_to is not None,
            "invalid_completion",
            "completion_receipt requires reply_to (the request message_id it completes)",
        )
        _req(
            isinstance(artifacts, list) and len(artifacts) > 0,
            "invalid_completion",
            "completion_receipt requires a non-empty artifacts list (concrete evidence)",
        )
    if kind == "acknowledgment":
        _req(
            reply_to is not None,
            "invalid_ack",
            "acknowledgment message requires reply_to (the message acknowledged)",
        )
    if artifacts is not None:
        _req(
            isinstance(artifacts, list), "invalid_artifacts", "artifacts must be a list"
        )
    # A managed message's disposition is an explicit, first-class boolean the trusted agent supplies
    # (default by intrinsic kind); it is never inferred by parsing free text. actionable is stored on
    # EVERY message so a reader/notifier can respect it without re-deriving from kind, but it only
    # enters the idempotency hash when the message names a reservation (see content_hash).
    resolved_actionable = (
        bool(actionable)
        if actionable is not None
        else (kind in _ACTIONABLE_MESSAGE_KINDS)
    )
    h = content_hash(
        kind=kind,
        task_id=task_id,
        task_revision=task_revision,
        sender=sender,
        recipient=recipient,
        reply_to=reply_to,
        correlation_id=correlation_id,
        text=text,
        artifact_ref=artifact_ref,
        artifacts=artifacts,
        execution_action_id=execution_action_id,
        actionable=resolved_actionable,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "message_id": message_id,
        "task_id": task_id,
        "sender": sender,
        "recipient": recipient,
        "host": host or {},
        "kind": kind,
        "task_revision": task_revision,
        "created_at": created_at or utcnow(),
        "reply_to": reply_to,
        "correlation_id": correlation_id,
        "content_hash": h,
        "text": text,
        "artifact_ref": artifact_ref,
        "artifacts": list(artifacts) if artifacts else [],
        # managed execution disposition (review R1): the named reservation (or None) and whether this
        # message DISPATCHES work. A non-actionable notice (progress/review_finding/amendment/status)
        # carries actionable=False so a completed/closed executor is never woken by it (review R4).
        "execution_action_id": execution_action_id,
        "actionable": resolved_actionable,
    }
