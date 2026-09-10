"""Small, explicitly incomplete transport views. Stored messages/evidence remain unchanged."""
from __future__ import annotations

import json


def preview(value, limit=240):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    raw = text.encode("utf-8")
    return raw[:limit].decode("utf-8", errors="ignore") + ("…" if len(raw) > limit else "")


def message_view(message):
    keys = ("message_id", "seq", "kind", "sender", "recipient", "task_revision",
            "reply_to", "correlation_id", "content_hash", "actionable")
    return {**{k: message[k] for k in keys if k in message},
            "preview": preview(message.get("text") or message.get("artifact_ref") or ""),
            "summary_only": True}


def checkpoint_view(checkpoint, *, after_revision=None, stopped=False):
    if not checkpoint:
        return None
    result = {k: checkpoint.get(k) for k in ("revision", "content_hash", "authorization_ref")}
    result["summary_only"] = True
    result["unchanged"] = checkpoint.get("revision") == after_revision
    if not result["unchanged"] and not stopped:
        body = checkpoint.get("checkpoint") or {}
        next_action = body.get("next_action") or body.get("next")
        if next_action:
            result["checkpoint"] = {"next_action": preview(next_action)}
    return result


def execution_view(status):
    if status is None:
        return None
    return {k: status.get(k) for k in ("execution_id", "status", "phase", "state_version",
                                      "expired", "expires_at", "usage", "coverage")}


def stops_wait(status):
    return bool(status and (status.get("expired") or status.get("phase") == "closure"
                           or status.get("status") in
                           {"paused", "draining", "exhausted", "closed", "completed"}))
