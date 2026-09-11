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
    """Bytes the object occupies on the wire as the owned CLI emits it (``indent=2``, sorted keys,
    ASCII-escaped, one trailing newline). That is the largest JSON spelling in use here — a compact
    UTF-8 form, as an MCP host may send, is never bigger — so what fits this measure fits every
    emitter."""
    return len(json.dumps(obj, indent=2, sort_keys=True, default=str).encode("utf-8")) + 1


def select_fields(record: dict, fields) -> dict:
    """Field selection BEFORE serialization; absent keys are omitted, not invented."""
    return {k: record[k] for k in fields if k in record}


def _utf8_safe_end(raw: bytes) -> int:
    """Largest k such that ``raw[:k]`` does not end inside a multi-byte UTF-8 sequence."""
    n = len(raw)
    i = n - 1
    while i >= 0 and i >= n - 4 and (raw[i] & 0xC0) == 0x80:
        i -= 1
    if i < 0 or i < n - 4:
        return n  # not UTF-8 at this end; nothing safe to do
    b = raw[i]
    need = (1 if b < 0x80 else 2 if (b >> 5) == 0b110 else 3 if (b >> 4) == 0b1110
            else 4 if (b >> 3) == 0b11110 else 1)
    return i if i + need > n else n


def _utf8_safe_start(raw: bytes) -> int:
    """Smallest j (at most 3) such that ``raw[j:]`` does not start inside a multi-byte sequence."""
    j = 0
    while j < len(raw) and j < 3 and (raw[j] & 0xC0) == 0x80:
        j += 1
    return j


def fit_batch(items: list, *, budget: int = BATCH_BUDGET_BYTES, cursor_key=None,
              envelope: dict | None = None, items_key: str = "items") -> dict:
    """Keep the leading records for which the WHOLE serialized response — the records plus the
    envelope they travel in — fits ``budget`` on the wire. Anything dropped is counted in ``omitted``
    and flagged with ``truncated``; ``next_cursor`` names the last kept record's ``cursor_key`` so a
    caller can continue exactly where the budget stopped. A single record too large for the budget
    is replaced by a preview stub rather than silently emitted over budget."""
    budget = max(64, int(budget))
    total = len(items)

    def build(kept, stubbed):
        omitted = total - len(kept)
        next_cursor = None
        if omitted > 0 and cursor_key and kept and isinstance(kept[-1], dict):
            next_cursor = kept[-1].get(cursor_key)
        out = dict(envelope or {})
        out.update({items_key: kept, "returned": len(kept), "omitted": omitted,
                    "truncated": omitted > 0 or stubbed, "next_cursor": next_cursor,
                    "budget_bytes": budget})
        return out

    kept = []
    for it in items:
        kept.append(it)
        if encoded_size(build(kept, False)) > budget:
            kept.pop()
            break
    stubbed = False
    if not kept and items:
        it, width = items[0], max(32, budget // 2)
        while True:
            stub = {"preview": preview(it, width), "_truncated_record": True}
            if cursor_key and isinstance(it, dict) and cursor_key in it:
                stub[cursor_key] = it[cursor_key]
            if encoded_size(build([stub], True)) <= budget or width <= 32:
                break
            width //= 2
        kept, stubbed = [stub], True
    return build(kept, stubbed)


_REFERENCE_KEYS = frozenset({"path", "job_id", "stream", "evidence_dir", "supervisor_log", "next_cursor",
                             "cursor", "task_id", "action_id", "status", "effective_status", "error",
                             "command", "preview"})


def _is_reference(key: str) -> bool:
    return key in _REFERENCE_KEYS or key.endswith(("_sha256", "_path", "_id", "_at", "_truncated"))


def shrink_tails(record: dict, budget: int = BATCH_BUDGET_BYTES, keys=("tail", "text")) -> dict:
    """Fit ONE record into ``budget`` bytes on the wire. Inline output tails are trimmed first
    (keeping their END); if the record still does not fit, other long text fields are trimmed
    (keeping their HEAD) — never references, ids, hashes, statuses or timestamps. Every trimmed
    field is marked ``<key>_truncated`` and listed under ``truncation``; ``over_budget`` is set only
    if the record still does not fit once nothing trimmable remains."""
    rec = json.loads(json.dumps(record, default=str))
    tails, others = [], []

    def walk(o):
        if isinstance(o, dict):
            for k, v in list(o.items()):
                if isinstance(v, str):
                    if k in keys:
                        tails.append((o, k))
                    elif not _is_reference(k):
                        others.append((o, k))
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(rec)
    trimmed = set()
    for targets, keep_end in ((tails, True), (others, False)):
        while encoded_size(rec) > budget and any(o[k] for o, k in targets):
            o, k = max(targets, key=lambda t: len(t[0][t[1]].encode("utf-8")))
            raw = o[k].encode("utf-8")
            keep = len(raw) // 2
            if keep_end:
                chunk = raw[-keep:] if keep else b""
                chunk = chunk[_utf8_safe_start(chunk):]
            else:
                chunk = raw[:keep]
                chunk = chunk[:_utf8_safe_end(chunk)]
            o[k] = chunk.decode("utf-8", errors="replace")
            o[k + "_truncated"] = True
            trimmed.add(k)
            # the marker metadata travels with the record, so it is measured with it
            rec["truncated"] = True
            rec["truncation"] = {"trimmed_fields": sorted(trimmed), "budget_bytes": int(budget),
                                 "note": "full text remains on disk at the referenced paths"}
    if not trimmed:
        rec["truncated"] = encoded_size(rec) > budget
    if encoded_size(rec) > budget:
        rec["over_budget"] = True
    return rec


def read_bounded(path, *, offset: int = 0, limit: int = BATCH_BUDGET_BYTES, tail: bool = False,
                 budget=None, envelope: dict | None = None) -> dict:
    """Bounded slice of a retained file, cut on UTF-8 character boundaries so consecutive pages
    concatenate losslessly. ``tail=True`` reads the LAST ``limit`` bytes. The result always carries
    the total size, the offset actually read, ``next_offset`` (None at EOF), a ``truncated`` flag
    that is True whenever the slice is not the whole file, and ``lossy`` — True only when the bytes
    at the requested boundaries were not valid UTF-8, i.e. ``text`` is not byte-exact. With
    ``budget`` the slice is shrunk until the WHOLE serialized response (with ``envelope`` fields)
    fits that many bytes on the wire; the full file stays on disk for further pages."""
    p = Path(path)
    base = dict(envelope or {})
    if not p.is_file():
        out = dict(base)
        out.update({"path": str(p), "exists": False, "bytes_total": None, "offset": 0, "returned": 0,
                    "next_offset": None, "truncated": False, "lossy": False, "text": None})
        return out
    total = p.stat().st_size
    limit = max(0, min(int(limit), MAX_READ_BYTES))
    if tail:
        offset = max(0, total - limit)
    offset = max(0, min(int(offset), total))
    with open(p, "rb") as f:
        f.seek(offset)
        raw = f.read(limit)
    budget = max(512, int(budget)) if budget else None

    def trim(chunk: bytes, start: int):
        if tail:
            j = _utf8_safe_start(chunk)
            return chunk[j:], start + j
        if start + len(chunk) < total:
            k = _utf8_safe_end(chunk)
            if k > 0:
                chunk = chunk[:k]
        return chunk, start

    def build(chunk: bytes, start: int) -> dict:
        end = start + len(chunk)
        text = chunk.decode("utf-8", errors="replace")
        out = dict(base)
        out.update({"path": str(p), "exists": True, "bytes_total": total, "offset": start,
                    "returned": len(chunk), "next_offset": end if end < total else None,
                    "truncated": not (start == 0 and end == total),
                    "lossy": text.encode("utf-8") != chunk, "text": text})
        if budget:
            out["budget_bytes"] = budget
        return out

    raw, offset = trim(raw, offset)
    end = offset + len(raw)
    out = build(raw, offset)
    while budget and encoded_size(out) > budget and raw:
        keep = max(0, min(len(raw) - 1, int(len(raw) * (budget / encoded_size(out)) * 0.9)))
        if tail:
            raw, offset = trim(raw[len(raw) - keep:], end - keep)
        else:
            raw, offset = trim(raw[:keep], offset)
        out = build(raw, offset)
    return out
