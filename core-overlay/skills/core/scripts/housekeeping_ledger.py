#!/usr/bin/env python3
"""Durable, atomic housekeeping attempt accounting (request-reduction v1, item 3).

The core health hook used to dispatch the knowledge audit / transfer workers from a last-SUCCESS
timestamp alone, on every SessionStart (startup, resume, clear, compact). A worker that was
dispatched but never finished left the timestamp stale, so every later hook run dispatched
another worker: four workers, 57 responses, zero completions.

This helper is the single decision point the hook calls instead. Per housekeeping KIND it keeps a
small JSON ledger with one attempt record at a time:

    state       in_flight | completed | failed | exhausted
    attempt_id  stable id of the current/last attempt
    attempts    consecutive failed/timed-out attempts since the last completion
    last_success_at   preserved on every transition (never lost by a failure)
    failures    bounded list of {attempt_id, at, reason} evidence

`decide` answers dispatch/skip deterministically and, when it answers `dispatch`, atomically
reserves the attempt so a concurrent or later hook run (resume/compact) deduplicates instead of
dispatching again. An in-flight attempt older than its TTL is recorded as a timed-out FAILURE
(attempts += 1), never silently reset. After `max_attempts` failures the kind is `exhausted` and
stays skipped until `cooldown` elapses, and the hook is told to notify exactly once.

`complete` / `fail` record the worker's real outcome. Success timestamps supplied by the hook
(the legacy .last-audit / .last-run files) are honored as evidence of success too, so an older
worker that only touches those files still counts.

Writes are atomic (temp file + os.replace) under an O_EXCL lock with a bounded wait; the helper
never blocks unbounded and never deletes a ledger. It uses no model and performs no dispatch
itself — it only accounts. Python 3.9+ (hooks run under the host interpreter)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

STATES = ("in_flight", "completed", "failed", "exhausted")
MAX_FAILURES_KEPT = 10


def _now() -> int:
    return int(time.time())


def _iso(ts):
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))


class LedgerLock:
    def __init__(self, path: str, wait_s: float = 2.0):
        self.path = path + ".lock"
        self.wait_s = wait_s
        self.fd = None

    def __enter__(self):
        end = time.monotonic() + self.wait_s
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                # A dead holder cannot release; reclaim only when its pid is provably gone.
                try:
                    with open(self.path) as fh:
                        pid = int((fh.read() or "0").strip() or 0)
                except Exception:
                    pid = 0
                if pid and not _pid_alive(pid):
                    try:
                        os.unlink(self.path)
                        continue
                    except FileNotFoundError:
                        continue
                if time.monotonic() >= end:
                    raise TimeoutError("ledger lock busy: %s" % self.path)
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load(path: str) -> dict:
    try:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("ledger is not an object")
        data.setdefault("kinds", {})
        return data
    except FileNotFoundError:
        return {"format_version": 1, "kinds": {}}
    except Exception as e:  # corrupt ledger: preserve it, start a fresh one beside it
        try:
            os.replace(path, path + ".corrupt-%d" % _now())
        except Exception:
            pass
        return {"format_version": 1, "kinds": {}, "recovered_from_corrupt": str(e)[:200]}


def save(path: str, data: dict) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp-%d-%s" % (path, os.getpid(), uuid.uuid4().hex[:8])
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _record_failure(rec: dict, attempt_id: str, reason: str, now: int) -> None:
    rec["attempts"] = int(rec.get("attempts") or 0) + 1
    rec["state"] = "failed"
    rec["last_failure_at"] = now
    rec["last_failure_reason"] = reason[:300]
    failures = list(rec.get("failures") or [])
    failures.append({"attempt_id": attempt_id, "at": now, "reason": reason[:300]})
    rec["failures"] = failures[-MAX_FAILURES_KEPT:]


def _settle_inflight(rec: dict, now: int, inflight_ttl_s: int) -> None:
    """An in-flight attempt past its TTL is a timed-out failure, recorded once."""
    if rec.get("state") == "in_flight":
        started = int(rec.get("started_at") or 0)
        if now - started > inflight_ttl_s:
            _record_failure(rec, rec.get("attempt_id") or "?",
                            "timed out: no completion within %ds" % inflight_ttl_s, now)


def decide(data: dict, kind: str, *, now: int, external_success_ts: int, interval_s: int,
           max_attempts: int, cooldown_s: int, inflight_ttl_s: int, dispatcher: str) -> dict:
    rec = data["kinds"].setdefault(kind, {"state": None, "attempts": 0, "failures": []})
    # Preserve the best-known success time from either the ledger or the legacy marker.
    last_success = max(int(rec.get("last_success_at") or 0), int(external_success_ts or 0))
    if last_success:
        rec["last_success_at"] = last_success
    _settle_inflight(rec, now, inflight_ttl_s)
    state = rec.get("state")
    attempts = int(rec.get("attempts") or 0)

    if state == "in_flight":
        return {"action": "skip", "reason": "in_flight", "attempt_id": rec.get("attempt_id"),
                "notify": False}
    fresh = last_success and (now - last_success) < interval_s
    if fresh:
        return {"action": "skip", "reason": "fresh", "age_s": now - last_success, "notify": False}
    if state == "failed" and attempts >= max_attempts:
        rec["state"] = "exhausted"
        rec["exhausted_at"] = now
        state = "exhausted"
    if state == "exhausted":
        since = now - int(rec.get("exhausted_at") or rec.get("last_failure_at") or now)
        if since < cooldown_s:
            notify = not rec.get("exhausted_notified_at")
            if notify:
                rec["exhausted_notified_at"] = now
            return {"action": "skip", "reason": "exhausted", "attempts": attempts,
                    "cooldown_remaining_s": cooldown_s - since, "notify": notify,
                    "last_failure_reason": rec.get("last_failure_reason")}
        # cooldown elapsed: allow one fresh cycle, keep failure evidence
        rec["attempts"] = 0
        rec.pop("exhausted_notified_at", None)
        attempts = 0
    if state == "failed":
        since = now - int(rec.get("last_failure_at") or 0)
        if since < cooldown_s:
            return {"action": "skip", "reason": "cooldown", "attempts": attempts,
                    "cooldown_remaining_s": cooldown_s - since, "notify": False}
    attempt_id = "%s-%d-%s" % (kind, now, uuid.uuid4().hex[:6])
    rec.update({"state": "in_flight", "attempt_id": attempt_id, "started_at": now,
                "dispatcher": dispatcher[:120]})
    return {"action": "dispatch", "reason": "due", "attempt_id": attempt_id,
            "attempts_before": attempts, "notify": False}


def complete(data: dict, kind: str, attempt_id: str, *, now: int, success_ts: int) -> dict:
    rec = data["kinds"].setdefault(kind, {"state": None, "attempts": 0, "failures": []})
    if attempt_id and rec.get("attempt_id") and rec.get("attempt_id") != attempt_id:
        # Stale completion from an older attempt: keep evidence, still a real success.
        rec["stale_completion"] = {"attempt_id": attempt_id, "at": now}
    rec["state"] = "completed"
    rec["completed_at"] = now
    rec["last_success_at"] = max(int(rec.get("last_success_at") or 0), int(success_ts or now))
    rec["attempts"] = 0
    rec.pop("exhausted_at", None)
    rec.pop("exhausted_notified_at", None)
    return {"state": "completed", "attempt_id": rec.get("attempt_id"),
            "last_success_at": _iso(rec["last_success_at"])}


def fail(data: dict, kind: str, attempt_id: str, reason: str, *, now: int,
         max_attempts: int) -> dict:
    rec = data["kinds"].setdefault(kind, {"state": None, "attempts": 0, "failures": []})
    _record_failure(rec, attempt_id or rec.get("attempt_id") or "?", reason or "failed", now)
    if rec["attempts"] >= max_attempts:
        rec["state"] = "exhausted"
        rec["exhausted_at"] = now
    return {"state": rec["state"], "attempts": rec["attempts"]}


def status(data: dict, kind: str) -> dict:
    rec = dict(data["kinds"].get(kind) or {})
    for k in ("last_success_at", "started_at", "completed_at", "last_failure_at", "exhausted_at",
              "exhausted_notified_at"):
        if rec.get(k):
            rec[k + "_iso"] = _iso(rec[k])
    return {"kind": kind, "record": rec}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("op", choices=("decide", "complete", "fail", "status"))
    ap.add_argument("kind")
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--attempt-id", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--dispatcher", default="")
    ap.add_argument("--last-success-ts", type=int, default=0,
                    help="legacy success marker epoch seconds (0 = none)")
    ap.add_argument("--success-ts", type=int, default=0)
    ap.add_argument("--interval-hours", type=float, default=24.0)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--cooldown-minutes", type=float, default=360.0)
    ap.add_argument("--inflight-ttl-minutes", type=float, default=120.0)
    ap.add_argument("--now", type=int, default=0, help="override clock (tests only)")
    a = ap.parse_args(argv)
    now = a.now or _now()
    try:
        with LedgerLock(a.ledger):
            data = load(a.ledger)
            if a.op == "decide":
                out = decide(data, a.kind, now=now, external_success_ts=a.last_success_ts,
                             interval_s=int(a.interval_hours * 3600),
                             max_attempts=max(1, a.max_attempts),
                             cooldown_s=int(a.cooldown_minutes * 60),
                             inflight_ttl_s=int(a.inflight_ttl_minutes * 60),
                             dispatcher=a.dispatcher)
            elif a.op == "complete":
                out = complete(data, a.kind, a.attempt_id, now=now, success_ts=a.success_ts)
            elif a.op == "fail":
                out = fail(data, a.kind, a.attempt_id, a.reason, now=now,
                           max_attempts=max(1, a.max_attempts))
            else:
                out = status(data, a.kind)
            if a.op != "status":
                save(a.ledger, data)
    except TimeoutError as e:
        # Never dispatch on an unreadable ledger: a busy lock is a skip, reported truthfully.
        out = {"action": "skip", "reason": "ledger_busy", "error": str(e), "notify": False}
    out["kind"] = a.kind
    out["ledger"] = a.ledger
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
