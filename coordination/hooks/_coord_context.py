#!/usr/bin/env python3
"""Render a coordination `resume` result into a SessionStart hookSpecificOutput.additionalContext
payload (model-visible). Reads the resume JSON on stdin; task/participant from argv[1]/argv[2].
Emits nothing (exit 0) when there is no usable state, so the hook stays silent on errors."""
from __future__ import annotations

import json
import sys


def main() -> int:
    task = sys.argv[1] if len(sys.argv) > 1 else ""
    who = sys.argv[2] if len(sys.argv) > 2 else ""
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
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
