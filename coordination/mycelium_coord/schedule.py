"""Deterministic Eastern-time scheduling helper + read-only verifier (efficiency v2, criterion 1).

Plans one intended instant from either an absolute local wall time or an elapsed delay, in
``America/New_York`` by default (EST in winter, EDT in summer), and emits the intended UTC instant,
the Eastern rendering and the scheduler submission data a UTC-only automation tool needs. A fixed
``UTC-05:00`` is honored only when explicitly requested and is reported as such — it is NOT Eastern
local time in summer. Invalid (spring-forward gap) and unresolved ambiguous (fall-back overlap) local
times are refused with a distinct error code rather than silently normalised.

The verifier compares a persisted ``next_run_at`` and active flag against the intended instant and
returns one of three verdicts — ``match`` / ``mismatch`` / ``cannot_evaluate`` — so an absent or
unparseable record is never reported as either success or failure. Nothing here submits, updates or
pauses an automation: create/update/pause go through the official host automation tool, and a
``mismatch`` on an active automation is reported as ``requires_native_pause``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import StoreError

DEFAULT_TZ = "America/New_York"
FIXED_UTC_MINUS_5 = "UTC-05:00"
_FIXED_ALIASES = {"utc-5", "utc-05", "utc-05:00", "utc−5", "utc−05:00", "fixed-utc-5",
                  "fixed-utc-05:00", "-05:00", "est-fixed"}
_ELAPSED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd])", re.IGNORECASE)
_UNIT_S = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
AMBIGUOUS_POLICIES = ("reject", "earlier", "later")
DEFAULT_TOLERANCE_S = 60.0


class ScheduleError(StoreError):
    """Raised for an invalid request. ``code`` is one of: invalid_time, nonexistent_local_time,
    ambiguous_local_time, invalid_elapsed, invalid_timezone, missing_target, conflicting_target,
    invalid_ambiguity_policy, past_instant."""


def _req(cond: bool, code: str, message: str) -> None:
    if not cond:
        raise ScheduleError(code, "", message)


# ---------------------------------------------------------------------------- parsing
EPOCH_MS_THRESHOLD = 1e11  # epoch seconds this large are in the year 5138; treat as milliseconds


def _from_epoch(value: float) -> datetime:
    """Epoch seconds, or epoch milliseconds (the Codex app stores next_run_at in ms)."""
    if abs(value) >= EPOCH_MS_THRESHOLD:
        value = value / 1000.0
    return datetime.fromtimestamp(value, tz=timezone.utc)


def parse_iso(value, *, what: str = "timestamp") -> datetime:
    """Parse an ISO-8601 timestamp (a trailing ``Z`` is accepted on Python 3.9) or an epoch number.
    A tz-naive string is refused: the caller must say which clock it meant."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        dt = _from_epoch(float(value))
    else:
        text = str(value).strip()
        _req(bool(text), "invalid_time", f"{what} is empty")
        if re.fullmatch(r"\d{9,}(?:\.\d+)?", text):
            dt = _from_epoch(float(text))
        else:
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            # python < 3.11 accepts only 3- or 6-digit fractions: pad/trim to 6
            text = re.sub(r"\.(\d+)(?=[+-]\d{2}:?\d{2}$|$)",
                          lambda m: "." + (m.group(1) + "000000")[:6], text)
            try:
                dt = datetime.fromisoformat(text.replace(" ", "T", 1) if "T" not in text else text)
            except ValueError as e:
                raise ScheduleError("invalid_time", "", f"{what} {value!r}: {e}") from None
    _req(dt.tzinfo is not None and dt.utcoffset() is not None, "invalid_time",
         f"{what} {value!r} has no UTC offset; supply one or use the local-time planner")
    return dt.astimezone(timezone.utc)


def parse_elapsed(value) -> float:
    """``90m``, ``2h``, ``1h30m``, ``45s``, ``1.5h``, ``2d`` or a bare number of seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        secs = float(value)
    else:
        text = str(value).strip().lower()
        _req(bool(text), "invalid_elapsed", "elapsed delay is empty")
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            secs = float(text)
        else:
            parts = _ELAPSED_RE.findall(text)
            _req(bool(parts) and "".join(n + u for n, u in parts) == re.sub(r"\s+", "", text),
                 "invalid_elapsed", f"elapsed delay {value!r} is not like 90m / 1h30m / 45s / 2d")
            secs = sum(float(n) * _UNIT_S[u] for n, u in parts)
    _req(secs >= 0, "invalid_elapsed", f"elapsed delay {value!r} is negative")
    return secs


def resolve_tz(name):
    """Return ``(tzinfo, canonical_label, fixed)``. ``None``/``'eastern'`` mean America/New_York;
    the fixed UTC-05:00 offset must be asked for explicitly."""
    if name is None or str(name).strip() == "" or str(name).strip().lower() in ("eastern", "et"):
        name = DEFAULT_TZ
    key = str(name).strip()
    if key.lower() in _FIXED_ALIASES:
        return timezone(timedelta(hours=-5), FIXED_UTC_MINUS_5), FIXED_UTC_MINUS_5, True
    if key.upper() in ("UTC", "Z"):
        return timezone.utc, "UTC", True
    try:
        return ZoneInfo(key), key, False
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ScheduleError("invalid_timezone", "", f"unknown timezone {name!r}: {e}") from None


def _abbrev(dt: datetime) -> str:
    return dt.tzname() or ""


def _offset_label(dt: datetime) -> str:
    off = dt.utcoffset() or timedelta(0)
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def render(dt_utc: datetime, tz=None) -> dict:
    """Render one UTC instant in a zone (default Eastern): ISO with offset, abbreviation, offset."""
    tzinfo, label, fixed = resolve_tz(tz)
    local = dt_utc.astimezone(tzinfo)
    return {
        "iso": local.isoformat(timespec="seconds"),
        "display": f"{local.strftime('%Y-%m-%d %H:%M:%S')} {_abbrev(local)} ({_offset_label(local)})",
        "tz": label,
        "abbreviation": _abbrev(local),
        "utc_offset": _offset_label(local),
        "dst": bool(local.dst()) if not fixed else False,
        "fixed_offset": fixed,
    }


def _resolve_local(naive: datetime, tzinfo, label: str, fixed: bool, ambiguous: str) -> datetime:
    """Attach a zone to a naive wall time, refusing gaps and unresolved overlaps."""
    _req(ambiguous in AMBIGUOUS_POLICIES, "invalid_ambiguity_policy",
         f"ambiguous policy must be one of {AMBIGUOUS_POLICIES}")
    if fixed:
        return naive.replace(tzinfo=tzinfo)
    first = naive.replace(tzinfo=tzinfo, fold=0)
    second = naive.replace(tzinfo=tzinfo, fold=1)
    # A wall time in a spring-forward gap does not round-trip through UTC.
    back = first.astimezone(timezone.utc).astimezone(tzinfo)
    if back.replace(tzinfo=None) != naive:
        earlier = (first.astimezone(timezone.utc) - timedelta(hours=1)).astimezone(tzinfo)
        raise ScheduleError(
            "nonexistent_local_time", "",
            f"{naive.isoformat(sep=' ')} does not exist in {label} (clocks skip forward); "
            f"nearest valid: {earlier.isoformat(timespec='minutes')} or "
            f"{back.isoformat(timespec='minutes')}",
        )
    if first.utcoffset() != second.utcoffset():
        if ambiguous == "reject":
            raise ScheduleError(
                "ambiguous_local_time", "",
                f"{naive.isoformat(sep=' ')} occurs twice in {label} "
                f"({_abbrev(first)} {_offset_label(first)} and {_abbrev(second)} "
                f"{_offset_label(second)}); pass ambiguous=earlier|later",
            )
        return first if ambiguous == "earlier" else second
    return first


def _parse_wall(text: str) -> datetime | None:
    """A tz-naive ``YYYY-MM-DD HH:MM[:SS]`` (or ``T``) wall time; None if the text carries an
    offset/``Z`` and should be parsed as an absolute instant instead."""
    t = str(text).strip()
    if re.search(r"(Z|z|[+-]\d{2}:?\d{2})$", t) and "T" in t:
        return None
    if re.search(r"(Z|z)$", t):
        return None
    if re.search(r"[+-]\d{2}:\d{2}$", t) and len(t) > 10:
        return None
    try:
        return datetime.fromisoformat(t.replace(" ", "T", 1))
    except ValueError as e:
        raise ScheduleError("invalid_time", "", f"local time {text!r}: {e}") from None


# ---------------------------------------------------------------------------- planning
def plan(*, at=None, elapsed=None, tz=None, now=None, ambiguous: str = "reject",
         allow_past: bool = False) -> dict:
    """Plan one intended instant. Exactly one of ``at`` (absolute local wall time, or an ISO instant
    with its own offset) or ``elapsed`` (delay from ``now``). ``tz`` selects the wall clock for a
    naive ``at`` and the display zone (default America/New_York; fixed UTC-05:00 only when asked).
    ``now`` (ISO/epoch) makes the plan reproducible in tests; it is recorded in the output."""
    _req(not (at is None and elapsed is None), "missing_target", "give at= or elapsed=")
    _req(at is None or elapsed is None, "conflicting_target", "give only one of at= / elapsed=")
    tzinfo, label, fixed = resolve_tz(tz)
    now_utc = parse_iso(now, what="now") if now is not None else datetime.now(timezone.utc)
    now_utc = now_utc.replace(microsecond=0)
    mode = "elapsed" if elapsed is not None else "absolute"
    if mode == "elapsed":
        secs = parse_elapsed(elapsed)
        intended = now_utc + timedelta(seconds=secs)
        source = {"elapsed": elapsed, "elapsed_s": secs}
    else:
        naive = _parse_wall(at)
        if naive is None:
            intended = parse_iso(at, what="at")
            source = {"at": str(at), "interpreted_as": "absolute_instant_with_offset"}
        else:
            local = _resolve_local(naive, tzinfo, label, fixed, ambiguous)
            intended = local.astimezone(timezone.utc)
            source = {"at": str(at), "interpreted_as": f"wall_time_in_{label}",
                      "ambiguity_policy": ambiguous}
    intended = intended.replace(microsecond=0)
    delta_s = (intended - now_utc).total_seconds()
    _req(allow_past or delta_s >= 0, "past_instant",
         f"intended instant {intended.isoformat()} is {abs(delta_s):.0f}s before now "
         f"{now_utc.isoformat()}")
    eastern = render(intended, DEFAULT_TZ)
    requested = render(intended, label)
    out = {
        "mode": mode,
        "source": source,
        "timezone": {"requested": label, "fixed_offset": fixed,
                     "default_is_eastern": label == DEFAULT_TZ},
        "now_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "intended_utc": intended.isoformat().replace("+00:00", "Z"),
        "intended_epoch": int(intended.timestamp()),
        "delay_s": delta_s,
        "eastern": eastern,
        "requested_zone": requested,
        "display": eastern["display"] if label == DEFAULT_TZ else
        f"{requested['display']} = {eastern['display']}",
        "scheduler": scheduler_submission(intended, eastern, fixed_label=label if fixed else None),
    }
    return out


def scheduler_submission(intended_utc: datetime, eastern: dict, *, fixed_label=None) -> dict:
    """What a UTC-only scheduler needs for a ONE-SHOT at the intended instant. The helper never
    submits; the caller passes these values to the official host automation tool and then verifies
    the persisted ``next_run_at`` with :func:`verify`."""
    u = intended_utc.astimezone(timezone.utc)
    note = ("one-shot at the intended UTC instant; a recurring Eastern wall-clock rule cannot be "
            "expressed as a single fixed-UTC recurrence across DST changes — re-plan each "
            "occurrence, or re-submit the UTC hour at each transition")
    if fixed_label:
        note = (f"planned on the fixed {fixed_label} clock as explicitly requested; this is not "
                f"Eastern local time when daylight saving is in effect. " + note)
    return {
        "kind": "one_shot",
        "utc": {"date": u.strftime("%Y-%m-%d"), "time": u.strftime("%H:%M:%S"),
                "iso": u.isoformat().replace("+00:00", "Z")},
        "cron_utc": f"{u.minute} {u.hour} {u.day} {u.month} *",
        "rrule_utc": f"DTSTART:{u.strftime('%Y%m%dT%H%M%SZ')}\nRRULE:FREQ=DAILY;COUNT=1",
        "eastern_display": eastern["display"],
        "note": note,
    }


# ---------------------------------------------------------------------------- verification
def _as_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "active", "enabled", "on", "scheduled", "running"):
        return True
    if text in ("false", "0", "no", "inactive", "paused", "disabled", "off", "deleted",
                "cancelled", "canceled", "stopped"):
        return False
    return None


def extract_persisted(record) -> dict:
    """Pull ``next_run_at`` and an active flag out of a persisted automation record of unknown
    shape (the host tool's returned object, or a row read from its store). Missing fields stay
    ``None`` — they are reported as unmeasured, never coerced."""
    rec = record if isinstance(record, dict) else {}
    for key in ("automation", "record", "result", "data", "item"):
        if isinstance(rec.get(key), dict) and "next_run_at" in rec[key]:
            rec = rec[key]
            break
    next_run = None
    for key in ("next_run_at", "nextRunAt", "next_run", "next_fire_at", "next_run_time"):
        if rec.get(key) not in (None, ""):
            next_run = rec.get(key)
            break
    active = None
    for key in ("active", "enabled", "is_active", "is_enabled"):
        if key in rec:
            active = _as_bool(rec.get(key))
            break
    if active is None and "status" in rec:
        active = _as_bool(rec.get("status"))
    if active is None and "paused" in rec:
        paused = _as_bool(rec.get("paused"))
        active = None if paused is None else (not paused)
    return {"next_run_at": next_run, "active": active,
            "name": rec.get("name") or rec.get("title") or rec.get("id"),
            "raw_keys": sorted(str(k) for k in rec.keys())[:40]}


def verify(*, intended_utc, persisted=None, next_run_at=None, active=None,
           tolerance_s: float = DEFAULT_TOLERANCE_S) -> dict:
    """Read-only comparison of a persisted schedule against the intended instant.

    Verdicts: ``match`` (|persisted - intended| <= tolerance and the automation is active),
    ``mismatch`` (persisted time differs beyond tolerance, or the record is inactive when a live
    schedule was expected), ``cannot_evaluate`` (no persisted next_run_at, unparseable value, or
    unknown active flag). Only ``match`` may back a success claim; ``requires_native_pause`` is
    set when an ACTIVE automation is persisted at the wrong instant.
    """
    intended = parse_iso(intended_utc, what="intended_utc")
    got = extract_persisted(persisted) if persisted is not None else {"next_run_at": None,
                                                                       "active": None, "name": None}
    if next_run_at is not None:
        got["next_run_at"] = next_run_at
    if active is not None:
        got["active"] = _as_bool(active)
    tol = max(0.0, float(tolerance_s))
    out = {
        "intended_utc": intended.isoformat().replace("+00:00", "Z"),
        "intended_eastern": render(intended)["display"],
        "persisted_next_run_at": got["next_run_at"],
        "persisted_active": got["active"],
        "automation": got.get("name"),
        "tolerance_s": tol,
        "verdict": "cannot_evaluate",
        "ok": False,
        "requires_native_pause": False,
        "reason": None,
    }
    if got["next_run_at"] in (None, ""):
        out["reason"] = "no_persisted_next_run_at"
        return out
    try:
        persisted_dt = parse_iso(got["next_run_at"], what="persisted next_run_at")
    except ScheduleError as e:
        out["reason"] = f"unparseable_next_run_at: {e.message}"
        return out
    diff = (persisted_dt - intended).total_seconds()
    out["persisted_utc"] = persisted_dt.isoformat().replace("+00:00", "Z")
    out["persisted_eastern"] = render(persisted_dt)["display"]
    out["difference_s"] = diff
    if got["active"] is None:
        out["reason"] = "active_flag_unknown"
        out["time_matches"] = abs(diff) <= tol
        return out
    if abs(diff) > tol:
        out["verdict"] = "mismatch"
        out["reason"] = "next_run_at_differs_from_intended"
        out["requires_native_pause"] = bool(got["active"])
        return out
    if not got["active"]:
        out["verdict"] = "mismatch"
        out["reason"] = "automation_inactive"
        return out
    out["verdict"] = "match"
    out["ok"] = True
    out["reason"] = "persisted_next_run_at_within_tolerance_and_active"
    return out


# ---------------------------------------------------------------------------- persisted record source
DEFAULT_AUTOMATION_DB = "~/.codex/sqlite/codex-dev.db"


def read_automation_record(*, name=None, automation_id=None, db_path=None) -> dict:
    """READ-ONLY lookup of one persisted automation row in the host app's SQLite store (opened with
    ``mode=ro``; nothing is ever written). Returns ``{"found": bool, "record": row|None, "db": path,
    "matches": n}`` — an absent row or an unreadable store is reported as such, never as a
    schedule verdict. ``next_run_at`` in that store is epoch milliseconds; a PAUSED row carries
    ``next_run_at = NULL`` by design."""
    import os
    import sqlite3
    _req(name is not None or automation_id is not None, "missing_target",
         "give name= or automation_id=")
    path = os.path.expanduser(db_path or DEFAULT_AUTOMATION_DB)
    out = {"db": path, "found": False, "record": None, "matches": 0, "error": None}
    if not os.path.isfile(path):
        out["error"] = "automation_store_not_found"
        return out
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        try:
            con.row_factory = sqlite3.Row
            key = str(automation_id if automation_id is not None else name)
            rows = con.execute("SELECT * FROM automations WHERE id = ? OR name = ?", (key, key)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        out["error"] = f"automation_store_unreadable: {e}"
        return out
    out["matches"] = len(rows)
    if not rows:
        return out
    if len(rows) > 1:
        rows = sorted(rows, key=lambda r: (r["updated_at"] if "updated_at" in r.keys() else 0) or 0,
                      reverse=True)
    row = {k: rows[0][k] for k in rows[0].keys() if k not in ("prompt",)}
    if isinstance(row.get("status"), str):
        row["status"] = row["status"].lower()
    out["found"] = True
    out["record"] = row
    return out
