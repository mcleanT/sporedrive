"""Send capability (triangle proposal step 3, "send binding").

The process that attaches a participant keeps a random capability IN MEMORY. The store keeps only its
sha256 and the identity of the holding process (pid, process start time, machine). On a task created
with protocol feature ``send-capability`` - and in every advisory process, on every task - an
identity-bearing write (send, ack, set_cursor, checkpoint publish, detach, wake, bridge notify) must come
from a process that holds the participant's CURRENT capability. Re-attaching a participant whose holder
process is still alive is refused; once the holder has exited, a same-identity re-attach mints a new
capability (a restarted session, or a woken headless Codex turn, which is a new process).

What this is and is not:
- It binds a participant to the attaching PROCESS. MCP tools never return the capability, so a model
  cannot copy it into another process, and an MCP-only client cannot act as a participant that another
  live process attached. The CLI is a new process per call, so it keeps capabilities in a 0600 file
  named by ``--cap-file`` / ``MYCELIUM_COORD_CAP_FILE``.
- It is containment, not authentication: a process with a shell can still edit the store files.
- Reads (inbox, read_message, resume, wait, participants) are not gated.
- A process running older code ignores the feature until it is restarted with this code.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import platform
import secrets
import subprocess
import tempfile
import threading

from .model import utcnow
from .store import StoreError

# (store_root, task_id, participant_id) -> capability. Module level, so every Coordinator in this
# process (MCP server, wake, bridge, CLI) sees the same capabilities.
_CAPS: dict[tuple[str, str, str], str] = {}
_GUARD = threading.Lock()
_SELF: dict | None = None

ALIVE, DEAD, UNKNOWN = "alive", "dead", "unknown"
CAP_FILE_ENV = "MYCELIUM_COORD_CAP_FILE"


def store_id(store) -> str:
    """Canonical store identity: the resolved real path, so relative, absolute and symlinked
    spellings of one store share capabilities (review 6)."""
    return os.path.realpath(os.path.abspath(str(getattr(store, "root", ""))))


def _key(store, task_id: str, participant_id: str) -> tuple[str, str, str]:
    return (store_id(store), task_id, participant_id)


def digest(cap: str) -> str:
    return hashlib.sha256(cap.encode()).hexdigest()


def _start_time(pid: int) -> str | None:
    """Process start time as printed by ps (seconds resolution), or None when it cannot be read."""
    try:
        r = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except Exception:  # noqa: BLE001 - no ps, timeout: the caller treats None as unknown
        return None
    out = " ".join((r.stdout or "").split())
    return out or None


def self_holder() -> dict:
    """This process's holder identity (cached per pid, so a fork recomputes it)."""
    global _SELF
    pid = os.getpid()
    if _SELF is None or _SELF.get("pid") != pid:
        _SELF = {"pid": pid, "start": _start_time(pid), "node": platform.node()}
    return dict(_SELF)


def holder_state(holder) -> str:
    """alive | dead | unknown for a stored holder. A record without a holder (attached by older code)
    has no live holder: dead."""
    if not isinstance(holder, dict) or not isinstance(holder.get("pid"), int):
        return DEAD
    if holder.get("node") != platform.node():
        return UNKNOWN
    pid = holder["pid"]
    if pid == os.getpid():
        # this pid: the same process only if the start time matches too (a reused pid is not the
        # holder, review 5)
        mine = self_holder().get("start")
        if not mine or not holder.get("start"):
            return UNKNOWN
        return ALIVE if mine == holder["start"] else DEAD
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return DEAD
    except PermissionError:
        pass  # exists, owned by another user
    except OSError:
        return UNKNOWN
    start = _start_time(pid)
    if start is None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return DEAD  # exited between the two checks
        except OSError:
            pass
        return UNKNOWN
    if not holder.get("start"):
        return UNKNOWN
    return (
        ALIVE if start == holder["start"] else DEAD
    )  # a different start time = a reused pid


def mint(store, task_id: str, participant_id: str) -> dict:
    """Create and keep a new capability for this process; return the record the store keeps."""
    cap = secrets.token_urlsafe(32)
    with _GUARD:
        _CAPS[_key(store, task_id, participant_id)] = cap
    return {"sha256": digest(cap), "holder": self_holder(), "minted_at": utcnow()}


def held(store, task_id: str, participant_id: str) -> str | None:
    with _GUARD:
        return _CAPS.get(_key(store, task_id, participant_id))


def holds_current(store, task_id: str, participant_id: str, record) -> bool:
    cap = held(store, task_id, participant_id)
    want = ((record or {}).get("send_cap") or {}).get("sha256")
    return bool(
        cap and isinstance(want, str) and hmac.compare_digest(digest(cap), want)
    )


def forget(
    store=None, task_id: str | None = None, participant_id: str | None = None
) -> None:
    """Drop capabilities held by this process (all of them with no arguments). Tests use this to act
    as a second process."""
    with _GUARD:
        if store is None:
            _CAPS.clear()
            return
        _CAPS.pop(_key(store, task_id or "", participant_id or ""), None)


def public(record: dict) -> dict:
    """A participant record as shown to a caller: the capability hash is never rendered."""
    if not isinstance(record, dict) or "send_cap" not in record:
        return record
    sc = record.get("send_cap") or {}
    out = dict(record)
    out["send_cap"] = {
        "holder_pid": (sc.get("holder") or {}).get("pid"),
        "minted_at": sc.get("minted_at"),
    }
    return out


# ---------------------------------------------------------------- CLI capability file
# Format: {"version": 2, "caps": [{"store": <real path>, "task": .., "participant": .., "cap": ..}]}.
# Owner-only (0600, owned by this user) or it is refused; every read-modify-replace holds an flock on
# "<file>.lock" and fsyncs before the atomic replace (review 8).
def _check_secure(path: str) -> None:
    st = os.stat(path)
    if st.st_uid != os.getuid() or (st.st_mode & 0o077):
        raise StoreError(
            "cap_file_insecure",
            path,
            "the capability file must be owned by you and readable by you only (chmod 600)",
        )


def _read_entries(path: str) -> list:
    try:
        _check_secure(path)
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except ValueError:
        return []
    caps = data.get("caps") if isinstance(data, dict) else None
    return [e for e in caps if isinstance(e, dict)] if isinstance(caps, list) else []


def load_file(path: str, store) -> int:
    """Adopt every capability in ``path`` that belongs to this store. Returns how many were adopted.
    A missing file adopts nothing; an insecure file is refused (StoreError cap_file_insecure)."""
    root = store_id(store)
    n = 0
    for e in _read_entries(path):
        if e.get("store") == root and isinstance(e.get("cap"), str) and e["cap"]:
            with _GUARD:
                _CAPS[(root, str(e.get("task")), str(e.get("participant")))] = e["cap"]
            n += 1
    return n


def _rewrite(path: str, mutate) -> None:
    path = os.path.abspath(path)
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    lock_fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        entries = mutate(_read_entries(path))
        fd, tmp = tempfile.mkstemp(prefix=".cap-", dir=d)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"version": 2, "caps": entries}, f, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _same(e: dict, store, task_id: str, participant_id: str) -> bool:
    return (e.get("store"), e.get("task"), e.get("participant")) == _key(store, task_id, participant_id)


def save_to_file(path: str, store, task_id: str, participant_id: str) -> bool:
    """Write this process's capability for one participant into ``path``. Returns False when this
    process holds none."""
    cap = held(store, task_id, participant_id)
    if not cap:
        return False
    root, t, p = _key(store, task_id, participant_id)
    _rewrite(path, lambda es: [e for e in es if not _same(e, store, task_id, participant_id)]
             + [{"store": root, "task": t, "participant": p, "cap": cap}])
    return True


def remove_from_file(path: str, store, task_id: str, participant_id: str) -> None:
    """Drop one participant's entry (detach)."""
    if os.path.exists(path):
        _rewrite(path, lambda es: [e for e in es if not _same(e, store, task_id, participant_id)])
