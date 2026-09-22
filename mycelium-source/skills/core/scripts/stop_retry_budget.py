#!/usr/bin/env python3
"""Bound Stop continuations independently of host recursion flags.

Exhaustion ends automatic prompting, not the lifecycle transaction. Its original
evidence stays pending for explicit repair; only an accepted Stop clears budget.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time

MAX_PROMPTS = 2


def retry_budget(state: Path, session: str, action: str, reason: str = "") -> bool:
    for p in (state, *state.parents):
        if p.is_symlink() or not p.is_dir():
            raise ValueError("unsafe retry-state directory")
    key = hashlib.sha256(session.encode()).hexdigest()[:32]
    receipt = state / f"stop-retry-{key}.json"
    lock = state / f"stop-retry-{key}.lock"
    if action == "clear" and not receipt.exists() and not receipt.is_symlink():
        return True
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(lock, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("unsafe retry lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = None
        mode = 0o600
        if receipt.exists() or receipt.is_symlink():
            info = receipt.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise ValueError("unsafe retry receipt")
            mode = stat.S_IMODE(info.st_mode)
            previous = json.loads(receipt.read_text())
            if (not isinstance(previous, dict)
                    or previous.get("session_id") != session
                    or type(previous.get("prompts")) is not int
                    or not 0 <= previous["prompts"] <= MAX_PROMPTS):
                raise ValueError("invalid retry receipt; preserve for repair")
        if action == "clear":
            receipt.unlink(missing_ok=True)
            return True
        if action != "claim":
            raise ValueError("unknown retry action")
        count = previous["prompts"] if previous else 0
        allowed = count < MAX_PROMPTS
        data = {
            "schema_version": 1, "session_id": session,
            "prompts": count + 1 if allowed else count,
            "status": "pending" if allowed else "deferred",
            "last_reason": reason[:16000], "updated_at": int(time.time()),
        }
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=".stop-retry-", dir=state)
        try:
            with os.fdopen(tmp_fd, "w") as stream:
                os.fchmod(stream.fileno(), mode)
                json.dump(data, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, receipt)
        finally:
            Path(tmp_name).unlink(missing_ok=True)
        return allowed
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        allowed = retry_budget(Path(sys.argv[1]), sys.argv[2] or "legacy",
                               sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "")
    except (OSError, ValueError, IndexError) as exc:
        # A broken budget must not buy unlimited model turns. Do not erase the
        # malformed receipt or pretend that lifecycle finalization succeeded.
        print(f"Mycelium Stop prompting deferred: {exc}", file=sys.stderr)
        allowed = False
    raise SystemExit(0 if allowed else 1)
