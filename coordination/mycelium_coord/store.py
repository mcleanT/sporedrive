"""Owner-only, cross-process coordination store for Mycelium session communication.

Root: ``$MYCELIUM_COORD_DIR`` or ``~/.local/state/mycelium/coordination``, mode 0700; every JSON
record is written atomically (temp + rename) mode 0600; every immutable artifact is 0444. Nothing
here is ever a repository file and it is never inside a scientific worktree (MYCELIUM-INTEGRATION r1
decision D2).

This deliberately mirrors ``cmux_bridge.state.StateStore`` rather than importing it, so the package
installs and runs on either native host independently of whether the full bridge is present:

* symlink / containment **preflight** before any mkdir/chmod/write — a linked or wrong-kind managed
  path refuses with ``StoreError('unsafe_state_path')`` and mutates nothing outside the root;
* cross-process serialisation via advisory ``flock(LOCK_EX)`` on ``locks/<name>.lock``, re-entrant
  per thread, never held across a wait/sleep;
* corrupt or uncertain records are preserved as explicit errors, never silently coerced to a value.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MANAGED_SUBDIRS = ("tasks", "locks", "sessions", "jobs")

# Filesystem-safe id charset shared with the bridge's request_id (so a message_id can BE a bridge
# request_id, D4). 1..120 chars. Rejects path traversal / separators outright.
ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,120}")

# macOS firmlinks: these ancestor symlinks are legitimate platform aliases (/var -> /private/var)
# and must not trip the managed-root ancestry check, or the default temp/home paths would break.
_ALLOWED_ANCESTOR_SYMLINKS = {"/var", "/tmp", "/etc"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class StoreError(RuntimeError):
    def __init__(self, code: str, path: str = "", message: str = ""):
        super().__init__(f"{code}: {message or path}")
        self.code = code
        self.path = str(path)
        self.message = message or str(path)


def valid_id(value) -> bool:
    """Central id validation for message/task/participant/lock ids. Rejects non-strings (so
    ``str(None)`` no longer passes), the bare ``.``/``..`` path segments, and any trailing
    newline (``fullmatch`` anchors both ends without ``$``'s pre-newline match)."""
    if not isinstance(value, str) or value in (".", ".."):
        return False
    return bool(ID_RE.fullmatch(value))


def require_id(value: str, what: str) -> str:
    if not valid_id(value):
        raise StoreError("invalid_id", str(value), f"{what} must match {ID_RE.pattern}")
    return str(value)


class CoordStore:
    def __init__(self, root: str | os.PathLike | None = None):
        root = (
            root
            or os.environ.get("MYCELIUM_COORD_DIR")
            or (Path.home() / ".local/state/mycelium/coordination")
        )
        self.root = Path(root)
        self._local = threading.local()
        self._preflight()
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        for sub in MANAGED_SUBDIRS:
            d = self.root / sub
            d.mkdir(exist_ok=True)
            os.chmod(d, 0o700)

    # ------------------------------------------------------------------ safety
    def _preflight(self) -> None:
        self._preflight_ancestry()
        for p in (self.root, *(self.root / s for s in MANAGED_SUBDIRS)):
            if p.is_symlink():
                raise StoreError(
                    "unsafe_state_path",
                    str(p),
                    f"{p} is a symlink; managed state paths must be real directories",
                )
            if p.exists() and not p.is_dir():
                raise StoreError(
                    "unsafe_state_path", str(p), f"{p} exists and is not a directory"
                )

    def _preflight_ancestry(self) -> None:
        """Refuse a symlinked ANCESTOR of the managed root before any mkdir/chmod/write, so a
        linked parent cannot silently relocate the whole store outside the named path. Known
        macOS firmlinks (/var, /tmp, /etc) are legitimate aliases and are allowed, preserving
        the default temp/home locations."""
        base = Path(os.path.abspath(str(self.root)))
        for anc in base.parents:
            if anc.is_symlink() and str(anc) not in _ALLOWED_ANCESTOR_SYMLINKS:
                raise StoreError(
                    "unsafe_state_path", str(anc),
                    f"managed-root ancestor {anc} is a symlink",
                )

    def path(self, rel: str) -> Path:
        """Containment + symlink check for one managed path, without following links."""
        rel = str(rel)
        if os.path.isabs(rel) or ".." in Path(rel).parts:
            raise StoreError(
                "unsafe_state_path", rel, "relative paths only, no parent traversal"
            )
        base = Path(os.path.normpath(str(self.root)))
        p = Path(os.path.normpath(str(base / rel)))
        if p != base and base not in p.parents:
            raise StoreError("unsafe_state_path", rel, "path escapes the state root")
        cur = base
        for part in p.relative_to(base).parts:
            cur = cur / part
            if cur.is_symlink():
                raise StoreError("unsafe_state_path", str(cur), f"{cur} is a symlink")
        return p

    # ------------------------------------------------------------------ lock
    @contextmanager
    def lock(self, name: str = "state", timeout_s: float = 5.0):
        """Cross-process exclusive lock on ``locks/<name>.lock``. Re-entrant per thread."""
        require_id(name, "lock name")
        held = getattr(self._local, "held", None)
        if held is None:
            held = self._local.held = {}
        if held.get(name):
            held[name] += 1
            try:
                yield
            finally:
                held[name] -= 1
            return
        p = self.path(f"locks/{name}.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise StoreError(
                            "lock_timeout",
                            str(p),
                            f"could not acquire {name} lock in {timeout_s}s",
                        )
                    time.sleep(0.01)
            held[name] = 1
            try:
                yield
            finally:
                held[name] = 0
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ------------------------------------------------------------------ io
    def read(self, rel: str):
        """Return parsed JSON, or None if absent. A corrupt file raises StoreError('corrupt_record')
        rather than silently reading as None (absence and corruption are distinct outcomes)."""
        p = self.path(rel)
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text())
        except ValueError as e:
            raise StoreError("corrupt_record", rel, f"unparseable JSON: {e}")

    def exists(self, rel: str) -> bool:
        return self.path(rel).is_file()

    def write(self, rel: str, obj) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(p.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        return p

    def write_artifact(self, rel: str, data: str) -> Path:
        """Write immutable (0444) owner-private text (e.g. a bounded message body captured as an
        artifact). A given relative path is written once; callers must not reuse it with new bytes."""
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(p.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.chmod(tmp, 0o444)
        os.replace(tmp, p)
        os.chmod(p, 0o444)
        return p

    def delete(self, rel: str) -> bool:
        p = self.path(rel)
        if p.is_file():
            p.unlink()
            return True
        return False

    def listdir(self, rel: str, pattern: str = "*.json") -> list[Path]:
        p = self.path(rel)
        return sorted(x for x in p.glob(pattern) if x.is_file()) if p.is_dir() else []

    def next_seq(self, counter_rel: str, lock_name: str) -> int:
        """Allocate a monotonic per-task sequence under a lock. Distinct from message ids: the seq
        is the delivery cursor space, the id is idempotency identity."""
        with self.lock(lock_name):
            cur = self.read(counter_rel) or {"seq": 0}
            nxt = int(cur.get("seq", 0)) + 1
            self.write(counter_rel, {"seq": nxt})
            return nxt

    def append_receipt(self, rel: str, kind: str, record: dict, lock_name: str = "receipts") -> Path:
        """O_APPEND a jsonl audit record under a lock, through the containment/symlink check."""
        rec = {"at": utcnow(), "kind": kind, **record}
        with self.lock(lock_name):
            p = self.path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(p.parent, 0o700)
            fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode())
            finally:
                os.close(fd)
            os.chmod(p, 0o600)
        return p
