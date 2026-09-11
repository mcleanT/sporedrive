"""Owned local jobs (efficiency v2, criterion 2): a small run/join path for work launched through
the host-authorized shell.

* ``run`` is CLI-only. The command is started by the owned CLI so its processes inherit the host
  shell's permissions; nothing here exposes arbitrary command execution over MCP.
* Every job has an immutable request record (identity, argv, cwd, deadline, stop policy, and the
  managed execution reservation it runs under) and a mutable status record. Output is retained
  complete on disk (``stdout.log`` / ``stderr.log`` / ``supervisor.log``) under the coordination
  state root and referenced, never inlined, so full evidence stays reachable through
  :meth:`JobManager.output` with offset/limit.
* A managed job reuses ONE open work reservation: the read-only execution gate is checked when the
  request is accepted and AGAIN by the supervisor immediately before the actual process launch. The
  reservation is never re-aimed: a job id that already exists is replayed (same content) or refused
  (different content) — never launched twice.
* ``join`` waits INSIDE the tool for at most :data:`JOIN_MAX_S` seconds, stops early at the task
  deadline or a paused/terminal execution, and returns deterministic ``changed`` / ``timed_out`` /
  ``stop_waiting`` metadata with bounded tails. It is a filesystem poll, not a host wake-up.
* The wrapper terminates only processes it started itself, and only under the job's declared
  deadline / cancellation / on-stop policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .execution import ExecutionManager
from .model import ProtocolError, utcnow
from .store import CoordStore, StoreError, require_id, valid_id
from .views import encoded_size, BATCH_BUDGET_BYTES, execution_view, fit_batch, read_bounded, stops_wait

JOB_SCHEMA_VERSION = "1.0.0"
JOIN_MAX_S = 50.0
JOIN_POLL_S = 0.25
DEFAULT_DEADLINE_S = 600.0
MAX_DEADLINE_S = 6 * 3600.0
GRACE_S = 5.0
DEFAULT_TAIL_BYTES = 512
ON_STOP_POLICIES = ("keep", "cancel")

STATUS_ACCEPTED = "accepted"      # request persisted; supervisor being spawned
STATUS_LAUNCHED = "launched"      # supervisor process spawned; command not yet started
STATUS_RUNNING = "running"
STATUS_EXITED = "exited"
STATUS_TIMED_OUT = "timed_out"
STATUS_CANCELLED = "cancelled"
STATUS_STOPPED = "stopped"        # killed by on_stop=cancel when the execution stopped
STATUS_REFUSED = "refused"        # execution gate refused; nothing was launched
STATUS_UNKNOWN = "unknown"        # supervisor failed to record an outcome
TERMINAL = frozenset({STATUS_EXITED, STATUS_TIMED_OUT, STATUS_CANCELLED, STATUS_STOPPED,
                      STATUS_REFUSED, STATUS_UNKNOWN})

_STATUS_FIELDS = ("job_id", "status", "effective_status", "state_version", "exit_code", "signal", "cleanup",
                  "reason", "started_at", "ended_at", "elapsed_s", "deadline_s", "task_id",
                  "action_id", "label", "supervisor_alive", "pid", "recorded_pid_alive")
_LIST_FIELDS = ("job_id", "status", "effective_status", "exit_code", "started_at", "ended_at",
                "task_id", "action_id", "label", "command")


class JobError(ProtocolError):
    """Codes: invalid_job, job_identity_conflict, job_not_found, invalid_deadline,
    invalid_on_stop, invalid_cwd, invalid_argv, unmanaged_action, job_not_cancellable."""


def _req(cond: bool, code: str, message: str) -> None:
    if not cond:
        raise JobError(code, "", message)


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _proc_start(pid):
    """The kernel's start time of ``pid`` as ``ps`` prints it: ownership evidence recorded at launch
    and re-read before any recovery signal, so a recycled pid is never mistaken for the owned child.
    None when unavailable (then recovery refuses to signal)."""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True,
                           text=True, timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    s = r.stdout.strip()
    return s or None


def _group_alive(pgid) -> bool:
    try:
        os.killpg(int(pgid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_group_empty(pgid: int, timeout_s: float, *, proc=None) -> bool:
    """Wait (bounded) until nothing is left in the group; the leader is reaped first when this
    process owns it, so a zombie never keeps the group looking alive."""
    end = _now_s() + max(0.0, float(timeout_s))
    while True:
        if proc is not None:
            proc.poll()
        if (proc is None or proc.returncode is not None) and not _group_alive(pgid):
            return True
        if _now_s() >= end:
            return False
        time.sleep(0.05)


def _terminate_group(pgid: int, *, proc=None, grace_s: float = GRACE_S) -> dict:
    """Bounded, escalating termination of ONE process group this wrapper created: SIGTERM, wait up
    to ``grace_s`` for the leader to exit AND the whole group to empty, else SIGKILL the group and
    wait again. Reports what actually happened: ``group_empty`` False means owned work may still be
    alive and no caller may claim otherwise."""
    cleanup = {"pgid": int(pgid), "signals": [], "escalated": False, "group_empty": False}
    for name in ("SIGTERM", "SIGKILL"):
        cleanup["escalated"] = name == "SIGKILL"
        try:
            os.killpg(int(pgid), getattr(signal, name))
            cleanup["signals"].append(name)
        except ProcessLookupError:
            pass
        except PermissionError:
            cleanup["error"] = "permission_denied"
            break
        if _wait_group_empty(int(pgid), grace_s, proc=proc):
            cleanup["group_empty"] = True
            break
    return cleanup


def _sha256(path: Path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _size(path: Path):
    try:
        return path.stat().st_size
    except OSError:
        return None


def _now_s() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


class JobManager:
    """Owned job records under ``<coord-root>/jobs/<job_id>/``."""

    def __init__(self, store: CoordStore | None = None):
        self.store = store or CoordStore()
        self.em = ExecutionManager(self.store)

    # ------------------------------------------------------------------ paths
    def _rel(self, job_id: str, name: str = "") -> str:
        base = f"jobs/{require_id(job_id, 'job_id')}"
        return f"{base}/{name}" if name else base

    def _dir(self, job_id: str) -> Path:
        return self.store.path(self._rel(job_id))

    def _lock_name(self, job_id: str) -> str:
        return "job." + hashlib.sha256(job_id.encode()).hexdigest()[:24]

    def _request_hash(self, req: dict) -> str:
        keys = ("job_id", "argv", "cwd", "deadline_s", "on_stop", "task_id", "action_id",
                "dispatch_identity", "label")
        canon = json.dumps({k: req.get(k) for k in keys}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ read
    def read_request(self, job_id: str):
        return self.store.read(self._rel(job_id, "request.json"))

    def read_status(self, job_id: str):
        return self.store.read(self._rel(job_id, "status.json"))

    def _require(self, job_id: str) -> tuple[dict, dict]:
        req = self.read_request(job_id)
        if req is None:
            raise JobError("job_not_found", job_id, f"no job {job_id!r}")
        st = self.read_status(job_id) or {"job_id": job_id, "status": STATUS_UNKNOWN,
                                          "state_version": 0, "reason": "status_record_missing"}
        return req, st

    def _effective(self, req: dict, st: dict) -> dict:
        """Derived view: a non-terminal record whose supervisor is gone is reported as ``unknown``
        (reason ``supervisor_gone``) WITHOUT rewriting the stored record — reads stay read-only and
        the retained attempt stays visible."""
        out = dict(st)
        sup = st.get("supervisor_pid")
        alive = _pid_alive(sup) if sup else False
        out["supervisor_alive"] = alive
        status = st.get("status")
        if status in TERMINAL:
            out["effective_status"] = status
        elif not alive:
            out["effective_status"] = STATUS_UNKNOWN
            out["reason"] = out.get("reason") or "supervisor_gone"
            if st.get("pid"):
                out["recorded_pid_alive"] = _pid_alive(st.get("pid"))
        else:
            out["effective_status"] = status
        out["terminal"] = out["effective_status"] in TERMINAL
        out["task_id"] = req.get("task_id")
        out["action_id"] = req.get("action_id")
        out["label"] = req.get("label")
        out["deadline_s"] = req.get("deadline_s")
        return out

    def _outputs(self, job_id: str, *, tail_bytes: int = 0) -> dict:
        d = self._dir(job_id)
        out = {}
        for stream in ("stdout", "stderr"):
            p = d / f"{stream}.log"
            entry = {"path": str(p), "bytes": _size(p)}
            if tail_bytes > 0 and p.is_file():
                b = read_bounded(p, limit=tail_bytes, tail=True)
                entry.update({"tail": b["text"], "tail_truncated": b["truncated"],
                              "tail_offset": b["offset"]})
            out[stream] = entry
        out["supervisor_log"] = str(d / "supervisor.log")
        out["evidence_dir"] = str(d)
        return out

    def status(self, job_id: str, *, tail_bytes: int = 0, budget: int = BATCH_BUDGET_BYTES) -> dict:
        req, st = self._require(job_id)
        eff = self._effective(req, st)
        out = {k: eff.get(k) for k in _STATUS_FIELDS}
        out["terminal"] = eff["terminal"]
        out["command"] = req.get("argv", [None])[0]
        out["outputs"] = self._outputs(job_id, tail_bytes=tail_bytes)
        for k in ("stdout_sha256", "stderr_sha256"):
            if st.get(k):
                out[k] = st[k]
        return _shrink(out, budget)

    def list(self, *, task_id=None, status=None, limit: int = 20, cursor=None,
             budget: int = BATCH_BUDGET_BYTES) -> dict:
        """Metadata-only listing, newest first, bounded by count AND a combined byte budget.
        ``truncated`` is True when either bound cut the list; ``next_cursor`` continues below the
        last returned id. ``omitted`` counts records dropped by the byte budget from the fetched
        window, or None when the count limit stopped the scan (the remainder was not read)."""
        limit = max(1, min(int(limit), 200))
        rows, count_limited = [], False
        for d in sorted((p for p in self.store.path("jobs").glob("*") if p.is_dir()), reverse=True):
            job_id = d.name
            if not valid_id(job_id) or (cursor is not None and job_id >= str(cursor)):
                continue
            try:
                req, st = self._require(job_id)
            except (JobError, StoreError) as e:
                row = {"job_id": job_id, "status": STATUS_UNKNOWN, "effective_status": STATUS_UNKNOWN,
                       "error": getattr(e, "code", "read_error")}
            else:
                if task_id is not None and req.get("task_id") != task_id:
                    continue
                eff = self._effective(req, st)
                if status is not None and eff["effective_status"] != status:
                    continue
                row = {k: eff.get(k) for k in _LIST_FIELDS}
                row["command"] = req.get("argv", [None])[0]
            if len(rows) >= limit:
                count_limited = True
                break
            rows.append(row)
        out = fit_batch(rows, budget=budget, cursor_key="job_id", envelope={}, items_key="jobs")
        if count_limited:
            out["truncated"] = True
            if not out["omitted"]:
                out["omitted"] = None  # the remainder was not read
            if out["next_cursor"] is None and out["jobs"]:
                out["next_cursor"] = out["jobs"][-1].get("job_id")
        while encoded_size(out) > budget and len(out["jobs"]) > 1:  # the real envelope, on the wire
            out["jobs"].pop()
            out.update({"returned": len(out["jobs"]), "omitted": (out["omitted"] or 0) + 1,
                        "truncated": True, "next_cursor": out["jobs"][-1].get("job_id")})
        return out

    def output(self, job_id: str, *, stream: str = "stdout", offset: int = 0,
               limit: int = BATCH_BUDGET_BYTES, tail: bool = False,
               budget: int = BATCH_BUDGET_BYTES) -> dict:
        """Bounded selected retrieval of retained output; the full file stays on disk. ``limit``
        caps the raw bytes read (<= 64 KiB); ``budget`` (default 4 KB; 0 = only the raw limit
        applies, for an explicit larger evidence read) caps the serialized RESPONSE, so what
        reaches the caller is bounded, not merely what was read. Pages are cut on character
        boundaries and concatenate losslessly."""
        self._require(job_id)
        _req(stream in ("stdout", "stderr", "supervisor"), "invalid_job",
             "stream must be stdout | stderr | supervisor")
        p = self._dir(job_id) / f"{stream}.log"
        return read_bounded(p, offset=offset, limit=limit, tail=tail, budget=budget or None,
                            envelope={"job_id": job_id, "stream": stream})

    # ------------------------------------------------------------------ run (CLI-only)
    def run(self, job_id: str, argv: list, *, cwd=None, deadline_s: float = DEFAULT_DEADLINE_S,
            task_id=None, action_id=None, dispatch_identity=None, on_stop: str = "keep",
            label=None, join_s: float = 0.0, tail_bytes: int = DEFAULT_TAIL_BYTES) -> dict:
        """Accept + launch one job (idempotent by job_id + content). Returns a compact receipt; with
        ``join_s`` > 0 it also joins inside the call (bounded by :data:`JOIN_MAX_S`)."""
        require_id(job_id, "job_id")
        _req(isinstance(argv, (list, tuple)) and len(argv) > 0 and all(isinstance(a, str) for a in argv),
             "invalid_argv", "argv must be a non-empty list of strings")
        cwd = os.path.abspath(str(cwd or os.getcwd()))
        _req(os.path.isdir(cwd), "invalid_cwd", f"cwd {cwd!r} is not a directory")
        try:
            deadline_s = float(deadline_s)
        except (TypeError, ValueError):
            raise JobError("invalid_deadline", "", "deadline_s must be a number") from None
        _req(0 < deadline_s <= MAX_DEADLINE_S, "invalid_deadline",
             f"deadline_s must be in (0, {MAX_DEADLINE_S:.0f}]")
        _req(on_stop in ON_STOP_POLICIES, "invalid_on_stop", f"on_stop must be one of {ON_STOP_POLICIES}")
        _req((task_id is None) == (action_id is None), "unmanaged_action",
             "task_id and action_id go together (managed job) or are both absent (unmanaged)")
        if task_id is not None:
            require_id(task_id, "task_id")
            require_id(action_id, "action_id")
        req = {
            "schema_version": JOB_SCHEMA_VERSION, "job_id": job_id, "argv": list(argv), "cwd": cwd,
            "deadline_s": deadline_s, "on_stop": on_stop, "task_id": task_id, "action_id": action_id,
            "dispatch_identity": dispatch_identity, "label": label,
        }
        req["request_hash"] = self._request_hash(req)
        with self.store.lock(self._lock_name(job_id)):
            existing = self.read_request(job_id)
            if existing is not None:
                if existing.get("request_hash") != req["request_hash"]:
                    raise JobError("job_identity_conflict", job_id,
                                   f"job {job_id!r} already exists with different argv/cwd/deadline/"
                                   f"reservation; a job id is never re-aimed")
                receipt = self.status(job_id, tail_bytes=tail_bytes)
                receipt["replayed"] = True
                receipt["launched"] = False
            else:
                req["requested_at"] = utcnow()
                req["requested_by_pid"] = os.getpid()
                gate = self._gate(req)
                req["gate_at_request"] = gate
                self.store.write(self._rel(job_id, "request.json"), req)
                try:
                    os.chmod(self.store.path(self._rel(job_id, "request.json")), 0o444)
                except OSError:
                    pass
                if not gate.get("ok"):
                    self._write_status(job_id, {
                        "job_id": job_id, "status": STATUS_REFUSED, "state_version": 1,
                        "reason": "execution_gate:" + str(gate.get("reason")),
                        "gate": gate, "ended_at": utcnow(), "exit_code": None, "signal": None,
                    })
                    receipt = self.status(job_id, tail_bytes=0)
                    receipt["replayed"] = False
                    receipt["launched"] = False
                    return receipt
                self._write_status(job_id, {"job_id": job_id, "status": STATUS_ACCEPTED,
                                            "state_version": 1, "exit_code": None, "signal": None})
                sup_pid = self._spawn_supervisor(job_id)
                self._write_status(job_id, {"job_id": job_id, "status": STATUS_LAUNCHED,
                                            "state_version": 2, "supervisor_pid": sup_pid,
                                            "launched_at": utcnow(), "exit_code": None,
                                            "signal": None})
                receipt = self.status(job_id, tail_bytes=0)
                receipt["replayed"] = False
                receipt["launched"] = True
        if join_s and float(join_s) > 0:
            joined = self.join(job_id, timeout_s=join_s, tail_bytes=tail_bytes)
            joined["replayed"] = receipt["replayed"]
            joined["launched"] = receipt["launched"]
            return joined
        return receipt

    def _gate(self, req: dict) -> dict:
        if req.get("task_id") is None:
            return {"ok": True, "reason": "unmanaged", "checked_at": utcnow()}
        res = self.em.dispatch_check(req["task_id"], req["action_id"], req.get("dispatch_identity"),
                                     expected_purpose="work_dispatch")
        res = dict(res)
        res["checked_at"] = utcnow()
        return res

    def _write_status(self, job_id: str, rec: dict) -> dict:
        rec = dict(rec)
        rec["schema_version"] = JOB_SCHEMA_VERSION
        rec["updated_at"] = utcnow()
        self.store.write(self._rel(job_id, "status.json"), rec)
        return rec

    def _spawn_supervisor(self, job_id: str) -> int:
        d = self._dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        pkg_parent = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["MYCELIUM_COORD_DIR"] = str(self.store.root)
        log = open(d / "supervisor.log", "ab")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "mycelium_coord.jobs", "--supervise", job_id],
                cwd=str(self.store.root), env=env, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
            )
        finally:
            log.close()
        return proc.pid

    # ------------------------------------------------------------------ join (bounded, in-tool)
    def join(self, job_id: str, *, timeout_s: float = JOIN_MAX_S, after_version=None,
             tail_bytes: int = DEFAULT_TAIL_BYTES, budget: int = BATCH_BUDGET_BYTES) -> dict:
        """Wait inside the tool (≤ :data:`JOIN_MAX_S` s) until the job is terminal, its status
        record changes past ``after_version``, the managed execution stops (paused/closed/expired/
        completed → ``stop_waiting``), the task deadline passes, or the timeout elapses. Never a
        bare "still running": ``changed`` is False only with ``timed_out`` or ``stop_waiting``."""
        req, _ = self._require(job_id)
        timeout_s = max(0.0, min(float(timeout_s), JOIN_MAX_S))
        deadline = time.monotonic() + timeout_s
        task_id = req.get("task_id")
        exec_deadline_reason = None
        if task_id is not None:
            ex = self.em.status(task_id)
            expires = (ex or {}).get("expires_at")
            if expires:
                try:
                    from .execution import _parse_ts
                    remaining = (_parse_ts(expires) - datetime.now(timezone.utc)).total_seconds()
                    if remaining < timeout_s:
                        deadline = time.monotonic() + max(0.0, remaining)
                        exec_deadline_reason = "task_expires_at"
                except Exception:
                    pass
        while True:
            req, st = self._require(job_id)
            eff = self._effective(req, st)
            ex_view = execution_view(self.em.status(task_id)) if task_id is not None else None
            stop = stops_wait(ex_view)
            changed = after_version is None or int(st.get("state_version", 0)) > int(after_version)
            if eff["terminal"] or (changed and after_version is not None) or stop \
                    or time.monotonic() >= deadline:
                timed_out = (not eff["terminal"]) and (not stop) and not (
                    changed and after_version is not None)
                if timed_out and exec_deadline_reason and after_version is None:
                    pass
                out = {k: eff.get(k) for k in _STATUS_FIELDS}
                out.update({
                    "terminal": eff["terminal"],
                    "changed": bool(eff["terminal"] or (changed and after_version is not None)),
                    "timed_out": bool(timed_out),
                    "stop_waiting": bool(stop),
                    "stop_reason": (("task_expired" if ex_view and ex_view.get("expired") else
                                     "task_" + str(ex_view.get("status"))) if stop else None),
                    "waited_s": round(timeout_s - max(0.0, deadline - time.monotonic()), 3),
                    "join_max_s": JOIN_MAX_S,
                    "execution": ex_view,
                    "command": req.get("argv", [None])[0],
                    "outputs": self._outputs(job_id, tail_bytes=tail_bytes if eff["terminal"] else 0),
                })
                if timed_out and exec_deadline_reason:
                    out["timed_out_by"] = exec_deadline_reason
                for k in ("stdout_sha256", "stderr_sha256"):
                    if st.get(k):
                        out[k] = st[k]
                return _shrink(out, budget)
            time.sleep(min(JOIN_POLL_S, max(0.0, deadline - time.monotonic())))

    # ------------------------------------------------------------------ cancel (CLI-only)
    def _owned_group(self, st: dict) -> dict:
        """Current ownership evidence for the recorded child of a job whose supervisor is gone: the
        pid must be alive, still lead the process group this wrapper created for it, and carry the
        same start time recorded at launch. Bare recorded numbers are never enough on their own."""
        pid, pgid, recorded = st.get("pid"), st.get("pgid"), st.get("pid_start")
        ev = {"pid": pid, "pgid": pgid, "recorded_start": recorded, "current_start": None,
              "alive": False, "owned": False, "why": None}
        if not pid or not pgid:
            ev["why"] = "no_process_recorded"
            return ev
        if not _pid_alive(pid):
            ev["why"] = "process_absent"
            return ev
        ev["alive"] = True
        try:
            if os.getpgid(int(pid)) != int(pgid):
                ev["why"] = "pgid_mismatch"
                return ev
        except OSError:
            ev["alive"], ev["why"] = False, "process_absent"
            return ev
        if not recorded:
            ev["why"] = "no_start_time_recorded"
            return ev
        ev["current_start"] = _proc_start(pid)
        if ev["current_start"] != recorded:
            ev["why"] = "start_time_mismatch"
            return ev
        ev["owned"] = True
        return ev

    def _finalize(self, job_id: str, st: dict, update: dict) -> dict:
        """Record a terminal outcome for a job whose supervisor can no longer do so (under the job
        lock; a record that became terminal meanwhile is never overwritten)."""
        with self.store.lock(self._lock_name(job_id)):
            cur = self.read_status(job_id) or dict(st)
            if cur.get("status") in TERMINAL:
                return cur
            cur.update(update)
            cur["ended_at"] = cur.get("ended_at") or utcnow()
            cur["state_version"] = int(cur.get("state_version", 0)) + 1
            self._write_status(job_id, cur)
            return cur

    def cancel(self, job_id: str, *, reason: str = "cancel_requested") -> dict:
        """Ask the owning supervisor to terminate ITS child (``requested`` = recorded for the
        supervisor to act on; join to see the outcome). When the supervisor is gone, recover with
        identity-checked, bounded cleanup of the process group this wrapper created and report the
        real outcome: ``cancelled`` is True only once that group is confirmed empty."""
        req, st = self._require(job_id)
        eff = self._effective(req, st)
        stored = st.get("status")
        if stored in TERMINAL:
            return {"job_id": job_id, "cancelled": False, "status": stored, "reason": "already_terminal"}
        self.store.write(self._rel(job_id, "cancel.json"), {"requested_at": utcnow(), "reason": reason,
                                                            "by_pid": os.getpid()})
        if eff["supervisor_alive"]:
            return {"job_id": job_id, "cancelled": False, "requested": True,
                    "status": eff["effective_status"], "reason": "cancel_requested_via_supervisor"}
        if not st.get("supervisor_pid"):
            return {"job_id": job_id, "cancelled": False, "requested": True,
                    "status": eff["effective_status"],
                    "reason": "cancel_recorded_before_supervisor_start"}
        ev = self._owned_group(st)
        out = {"job_id": job_id, "ownership": ev}
        if ev["owned"]:
            cleanup = _terminate_group(int(st["pgid"]))
            if cleanup["group_empty"]:
                rec = self._finalize(job_id, st, {"status": STATUS_CANCELLED,
                                                  "reason": "cancelled_after_supervisor_gone",
                                                  "cleanup": cleanup})
                out.update({"cancelled": rec.get("status") == STATUS_CANCELLED, "status": rec.get("status"),
                            "reason": rec.get("reason"), "cleanup": cleanup})
            else:
                out.update({"cancelled": False, "status": STATUS_UNKNOWN,
                            "reason": "owned_group_not_confirmed_dead", "cleanup": cleanup})
            return out
        if ev["why"] in ("process_absent", "pgid_mismatch", "start_time_mismatch", "no_process_recorded"):
            # the owned process is gone (a mismatch means the pid now belongs to something else)
            rec = self._finalize(job_id, st, {"status": STATUS_UNKNOWN,
                                              "reason": f"supervisor_gone_child_absent:{ev['why']}"})
            out.update({"cancelled": False, "status": rec.get("status"), "reason": rec.get("reason")})
            return out
        out.update({"cancelled": False, "status": STATUS_UNKNOWN,
                    "reason": f"ownership_unverifiable:{ev['why']}"})
        return out


def _shrink(record: dict, budget: int) -> dict:
    """Fit one record into ``budget`` bytes by trimming output tails first; flag truthfully."""
    from .views import encoded_size, shrink_tails
    if encoded_size(record) <= budget:
        record["truncated"] = False
        return record
    return shrink_tails(record, budget)


# ---------------------------------------------------------------------------- supervisor process
def _supervise(job_id: str) -> int:
    """Runs in the detached supervisor process: gate recheck → launch → enforce deadline / cancel /
    on-stop → record the outcome. Every failure path records a status; nothing is left implicit."""
    store = CoordStore()
    jm = JobManager(store)
    req = jm.read_request(job_id)
    if req is None:
        return 2

    def bump(update: dict):
        with store.lock(jm._lock_name(job_id)):
            st = jm.read_status(job_id) or {"job_id": job_id, "state_version": 0}
            st.update(update)
            st["state_version"] = int(st.get("state_version", 0)) + 1
            st.setdefault("supervisor_pid", os.getpid())
            jm._write_status(job_id, st)

    try:
        gate = jm._gate(req)
        if not gate.get("ok"):
            bump({"status": STATUS_REFUSED, "reason": "execution_gate_at_launch:" + str(gate.get("reason")),
                  "gate_at_launch": gate, "ended_at": utcnow()})
            return 3
        if store.exists(jm._rel(job_id, "cancel.json")):
            bump({"status": STATUS_CANCELLED, "reason": "cancelled_before_launch",
                  "gate_at_launch": gate, "ended_at": utcnow()})
            return 0
        d = jm._dir(job_id)
        env = dict(os.environ)
        env["MYCELIUM_JOB_ID"] = job_id
        env.pop("MYCELIUM_COORD_DIR", None) if env.get("MYCELIUM_COORD_DIR") == "" else None
        out_f = open(d / "stdout.log", "ab")
        err_f = open(d / "stderr.log", "ab")
        for f in (d / "stdout.log", d / "stderr.log", d / "supervisor.log"):
            try:
                os.chmod(f, 0o600)
            except OSError:
                pass
        started = _now_s()
        try:
            proc = subprocess.Popen(req["argv"], cwd=req["cwd"], env=env, stdin=subprocess.DEVNULL,
                                    stdout=out_f, stderr=err_f, start_new_session=True)
        except (OSError, ValueError) as e:
            bump({"status": STATUS_EXITED, "exit_code": 127, "reason": f"launch_failed: {e}",
                  "started_at": _iso(started), "ended_at": utcnow(), "elapsed_s": 0.0,
                  "gate_at_launch": gate})
            return 0
        finally:
            out_f.close()
            err_f.close()
        bump({"status": STATUS_RUNNING, "pid": proc.pid, "pgid": proc.pid, "pid_start": _proc_start(proc.pid),
              "started_at": _iso(started), "gate_at_launch": gate, "reason": None})
        deadline = started + float(req["deadline_s"])
        outcome, reason, cleanup = None, None, None
        last_exec_check = 0.0
        while True:
            try:
                proc.wait(timeout=0.5)
                outcome = STATUS_EXITED
                break
            except subprocess.TimeoutExpired:
                pass
            now = _now_s()
            if now >= deadline:
                outcome, reason = STATUS_TIMED_OUT, f"deadline_{req['deadline_s']}s_exceeded"
                cleanup = _terminate_group(proc.pid, proc=proc)
                break
            if store.exists(jm._rel(job_id, "cancel.json")):
                outcome, reason = STATUS_CANCELLED, "cancel_requested"
                cleanup = _terminate_group(proc.pid, proc=proc)
                break
            if req.get("on_stop") == "cancel" and req.get("task_id") and now - last_exec_check >= 2.0:
                last_exec_check = now
                ex_view = execution_view(jm.em.status(req["task_id"]))
                if stops_wait(ex_view):
                    outcome = STATUS_STOPPED
                    reason = "task_expired" if ex_view.get("expired") else "task_" + str(ex_view.get("status"))
                    cleanup = _terminate_group(proc.pid, proc=proc)
                    break
        ended = _now_s()
        rc = proc.returncode
        exit_code = rc if (rc is not None and rc >= 0) else None
        sig = -rc if (rc is not None and rc < 0) else None
        if cleanup is None:  # exited on its own: still report whether it left anything in its group
            cleanup = {"pgid": proc.pid, "signals": [], "escalated": False,
                       "group_empty": not _group_alive(proc.pid)}
        bump({"status": outcome, "exit_code": exit_code, "signal": sig, "reason": reason, "cleanup": cleanup,
              "ended_at": _iso(ended), "elapsed_s": round(ended - started, 3),
              "stdout_bytes": _size(d / "stdout.log"), "stderr_bytes": _size(d / "stderr.log"),
              "stdout_sha256": _sha256(d / "stdout.log"), "stderr_sha256": _sha256(d / "stderr.log")})
        return 0
    except Exception as e:  # pragma: no cover - defensive: an unknown attempt is still recorded
        try:
            bump({"status": STATUS_UNKNOWN, "reason": f"supervisor_error: {type(e).__name__}: {e}",
                  "ended_at": utcnow()})
        except Exception:
            pass
        raise


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m mycelium_coord.jobs")
    ap.add_argument("--supervise", metavar="JOB_ID", required=True)
    args = ap.parse_args(argv)
    return _supervise(args.supervise)


if __name__ == "__main__":
    raise SystemExit(main())
