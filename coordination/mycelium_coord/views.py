"""Small, explicitly incomplete transport views. Stored messages/evidence remain unchanged.

Efficiency v2 adds the owned-output helpers: field selection before serialization, a combined
byte budget for a batch of records (:data:`BATCH_BUDGET_BYTES`, normally 4 KB) with a truthful
``truncated`` flag and a resume cursor, tail trimming for a single over-budget record, and bounded
offset/limit retrieval of a retained file so full evidence stays reachable. These cap only the
outputs this package produces — never a host-native shell or file tool's output.
"""
from __future__ import annotations

import json
from pathlib import Path

BATCH_BUDGET_BYTES = 4096
MAX_READ_BYTES = 65536


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


# ---------------------------------------------------------------------------- owned-output budget
def encoded_size(obj) -> int:
    """Bytes the object occupies once serialized the way the CLI/MCP emit it."""
    return len(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))


def select_fields(record: dict, fields) -> dict:
    """Field selection BEFORE serialization; absent keys are omitted, not invented."""
    return {k: record[k] for k in fields if k in record}


def fit_batch(items: list, *, budget: int = BATCH_BUDGET_BYTES, cursor_key=None,
              envelope_bytes: int = 256) -> dict:
    """Keep the leading records whose combined serialized size fits ``budget`` (less an allowance
    for the enclosing envelope). Anything dropped is counted in ``omitted`` and flagged with
    ``truncated``; ``next_cursor`` names the last kept record's ``cursor_key`` so a caller can
    continue exactly where the budget stopped. A single record too large for the budget is
    replaced by a preview stub rather than silently emitted over budget."""
    budget = max(64, int(budget))
    room = budget - envelope_bytes
    kept, size, stubbed = [], 2, False
    for it in items:
        s = encoded_size(it) + 1
        if size + s > room:
            if not kept:
                stub = {"preview": preview(it, max(32, room - 80)), "_truncated_record": True}
                if cursor_key and isinstance(it, dict) and cursor_key in it:
                    stub[cursor_key] = it[cursor_key]
                kept.append(stub)
                stubbed = True
            break
        kept.append(it)
        size += s
    omitted = len(items) - len(kept)
    next_cursor = None
    if omitted > 0 and cursor_key and kept and isinstance(kept[-1], dict):
        next_cursor = kept[-1].get(cursor_key)
    return {"items": kept, "returned": len(kept), "omitted": omitted,
            "truncated": omitted > 0 or stubbed, "next_cursor": next_cursor,
            "budget_bytes": budget}


def shrink_tails(record: dict, budget: int = BATCH_BUDGET_BYTES, keys=("tail", "text")) -> dict:
    """Fit ONE record into ``budget`` bytes by trimming inline output tails (keeping their END)
    before anything else; references (paths, offsets, hashes) are never removed. The result says
    ``truncated`` whenever any inline text was cut, and ``over_budget`` if it still does not fit."""
    rec = json.loads(json.dumps(record, default=str))
    targets = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in list(o.items()):
                if k in keys and isinstance(v, str):
                    targets.append((o, k))
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(rec)
    trimmed = set()
    while encoded_size(rec) > budget and any(o[k] for o, k in targets):
        o, k = max(targets, key=lambda t: len(t[0][t[1]].encode("utf-8")))
        raw = o[k].encode("utf-8")
        keep = len(raw) // 2
        o[k] = raw[-keep:].decode("utf-8", errors="ignore") if keep else ""
        o[k + "_truncated"] = True
        trimmed.add(k)
    rec["truncated"] = bool(trimmed) or encoded_size(rec) > budget
    if trimmed:
        rec["truncation"] = {"trimmed_fields": sorted(trimmed), "budget_bytes": int(budget),
                             "note": "full text remains on disk at the referenced paths"}
    if encoded_size(rec) > budget:
        rec["over_budget"] = True
    return rec


def read_bounded(path, *, offset: int = 0, limit: int = BATCH_BUDGET_BYTES, tail: bool = False) -> dict:
    """Bounded slice of a retained file. ``tail=True`` reads the LAST ``limit`` bytes. The result
    always carries the total size, the offset actually read, ``next_offset`` (None at EOF) and a
    ``truncated`` flag that is True whenever the slice is not the whole file."""
    p = Path(path)
    if not p.is_file():
        return {"path": str(p), "exists": False, "bytes_total": None, "offset": 0, "returned": 0,
                "next_offset": None, "truncated": False, "text": None}
    total = p.stat().st_size
    limit = max(0, min(int(limit), MAX_READ_BYTES))
    if tail:
        offset = max(0, total - limit)
    offset = max(0, min(int(offset), total))
    with open(p, "rb") as f:
        f.seek(offset)
        raw = f.read(limit)
    end = offset + len(raw)
    return {"path": str(p), "exists": True, "bytes_total": total, "offset": offset,
            "returned": len(raw), "next_offset": end if end < total else None,
            "truncated": not (offset == 0 and end == total),
            "text": raw.decode("utf-8", errors="replace")}
