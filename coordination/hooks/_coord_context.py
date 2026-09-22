#!/usr/bin/env python3
"""Render coordination state into a hookSpecificOutput.additionalContext payload (model-visible).

Two modes, selected by the COORD_HOOK_EVENT env var (the adapter sets it):

* SessionStart (default): read a `resume` result on stdin (task/participant from argv[1]/argv[2]) and
  render the current checkpoint + bounded pending messages, so a starting/resuming session reattaches
  without a transcript copy.
* PostToolUse: read a bounded `boundary-inbox` result from the COORD_BOUNDARY_JSON env var and render
  ONLY the new addressed mail (already count/byte capped upstream). This is the active-boundary
  exposure that lets an already-running peer see fresh mail mid-turn. Emits NOTHING when nothing is
  unread, so an active turn is never spammed.

Emits nothing (exit 0) whenever there is no usable state, so the hook stays silent on errors."""
from __future__ import annotations

import json
import os
import sys


def _emit(event: str, lines: list[str]) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event, "additionalContext": "\n".join(lines)}}))


def _render_active(task: str, who: str) -> int:
    raw = os.environ.get("COORD_BOUNDARY_JSON") or ""
    try:
        b = json.loads(raw)
    except Exception:
        return 0
    if not isinstance(b, dict):
        return 0
    unread = b.get("unread") or []
    if not unread:
        return 0  # silent: nothing new at this active boundary
    lines = [
        f"Mycelium coordination — new addressed mail for {who} on task {task} "
        f"({b.get('unread_count', len(unread))} bounded, after seq {b.get('after_seq')}):"
    ]
    for m in unread:
        lines.append(
            f"  - {m.get('message_id')} [{m.get('kind')}] from {m.get('sender')} "
            f"rev {m.get('task_revision')}: {str(m.get('text_preview') or '')[:200]}"
        )
    lines.append(
        "Read/ack via the mycelium-coord CLI or coord_* MCP tools. Coordination state only; it "
        "changes no repository ownership and runs no analysis hooks."
    )
    _emit("PostToolUse", lines)
    return 0


def main() -> int:
    task = sys.argv[1] if len(sys.argv) > 1 else ""
    who = sys.argv[2] if len(sys.argv) > 2 else ""
    event = os.environ.get("COORD_HOOK_EVENT", "SessionStart")
    if event == "PostToolUse":
        return _render_active(task, who)

    try:
        r = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(r, dict) or r.get("error"):
        return 0
    ck = r.get("checkpoint") or {}
    body = (ck.get("checkpoint") or {}) if isinstance(ck, dict) else {}
    pend = r.get("pending_unacked") or []
    lines = [f"Mycelium coordination: attached to task {task} as {who}."]
    if ck:
        lines.append(f"Current checkpoint rev {ck.get('revision')} (auth {ck.get('authorization_ref')}).")
        nxt = body.get("next_action") or body.get("next")
        if nxt:
            lines.append(f"Next action: {str(nxt)[:300]}")
        refs = body.get("knowledge_refs") or body.get("knowledge") or []
        if isinstance(refs, list) and refs:
            lines.append("Knowledge refs: " + ", ".join(str(x) for x in refs[:8]))
        # a bounded view of remaining checkpoint keys so a compacted session sees substance, not just
        # a revision number; the full record is resolvable via the read instruction below.
        shown = {"next_action", "next", "knowledge_refs", "knowledge"}
        extra = {k: v for k, v in body.items() if k not in shown}
        if extra:
            blob = json.dumps(extra, default=str)
            lines.append("Checkpoint (bounded): " + (blob if len(blob) <= 600 else blob[:600] + " …"))
        lines.append(f"Read full checkpoint: mycelium-coord checkpoint-read {task} --revision {ck.get('revision')}")
    lines.append(f"Pending unacknowledged messages: {r.get('pending_count', len(pend))}.")
    for m in pend[:10]:
        body = m.get("text") or m.get("artifact_ref") or ""
        lines.append(
            f"  - {m.get('message_id')} [{m.get('kind')}] from {m.get('sender')} "
            f"rev {m.get('task_revision')}: {str(body)[:120]}"
        )
    lines.append(
        "Read/ack via the mycelium-coord CLI or the coord_* MCP tools. This is coordination "
        "state only; it does not change repository ownership or run analysis hooks."
    )
    _emit("SessionStart", lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
