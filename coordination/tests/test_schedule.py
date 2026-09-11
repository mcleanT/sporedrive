"""Efficiency v2, criterion 1 — deterministic Eastern scheduling helper + read-only verifier.

Run: python3 -m pytest coordination/tests/test_schedule.py -q
Covers: winter (EST) and summer (EDT) wall times, the spring-forward gap and the fall-back overlap,
elapsed delays, the explicitly requested fixed UTC-05:00 clock, scheduler submission data,
verification against correct / mismatched / absent / paused persisted records (three verdicts),
epoch-millisecond next_run_at as the Codex app stores it, a read-only SQLite automation lookup, and
bounded error output through the actual CLI.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import schedule as s  # noqa: E402
from mycelium_coord.schedule import ScheduleError  # noqa: E402

NOW = "2026-01-01T12:00:00Z"


def _err(**kw):
    with pytest.raises(ScheduleError) as ei:
        s.plan(**kw)
    return ei.value.code


# ---------------------------------------------------------------- planning: absolute wall times
def test_winter_wall_time_is_est_utc_minus_5():
    p = s.plan(at="2026-01-15 09:00", now=NOW)
    assert p["intended_utc"] == "2026-01-15T14:00:00Z"
    assert p["eastern"]["abbreviation"] == "EST" and p["eastern"]["utc_offset"] == "UTC-05:00"
    assert p["eastern"]["dst"] is False
    assert p["display"] == "2026-01-15 09:00:00 EST (UTC-05:00)"
    assert p["timezone"] == {"requested": "America/New_York", "fixed_offset": False,
                             "default_is_eastern": True}


def test_summer_wall_time_is_edt_utc_minus_4():
    p = s.plan(at="2026-07-15 09:00", now=NOW)
    assert p["intended_utc"] == "2026-07-15T13:00:00Z"
    assert p["eastern"]["abbreviation"] == "EDT" and p["eastern"]["utc_offset"] == "UTC-04:00"
    assert p["eastern"]["dst"] is True


def test_the_audited_case_20_49_eastern_is_not_20_49_utc():
    """The observed defect: 20:49 ET was persisted as 20:49 UTC. The plan says 00:49Z next day."""
    p = s.plan(at="2026-09-10 20:49", now="2026-09-10T20:00:00Z")
    assert p["intended_utc"] == "2026-09-11T00:49:00Z"
    assert p["scheduler"]["utc"] == {"date": "2026-09-11", "time": "00:49:00",
                                     "iso": "2026-09-11T00:49:00Z"}
    assert p["scheduler"]["cron_utc"] == "49 0 11 9 *"
    assert p["scheduler"]["kind"] == "one_shot"
    assert "DST" in p["scheduler"]["note"] or "re-plan" in p["scheduler"]["note"]


def test_spring_forward_gap_is_rejected_with_nearest_valid_times():
    with pytest.raises(ScheduleError) as ei:
        s.plan(at="2026-03-08 02:30", now=NOW)
    assert ei.value.code == "nonexistent_local_time"
    assert "01:30-05:00" in ei.value.message and "03:30-04:00" in ei.value.message


def test_fall_back_overlap_is_rejected_unless_resolved():
    assert _err(at="2026-11-01 01:30", now=NOW) == "ambiguous_local_time"
    earlier = s.plan(at="2026-11-01 01:30", now=NOW, ambiguous="earlier")
    later = s.plan(at="2026-11-01 01:30", now=NOW, ambiguous="later")
    assert earlier["intended_utc"] == "2026-11-01T05:30:00Z"
    assert earlier["eastern"]["abbreviation"] == "EDT"
    assert later["intended_utc"] == "2026-11-01T06:30:00Z"
    assert later["eastern"]["abbreviation"] == "EST"
    assert _err(at="2026-11-01 01:30", now=NOW, ambiguous="whatever") == "invalid_ambiguity_policy"


def test_non_overlap_time_is_unaffected_by_ambiguity_policy():
    a = s.plan(at="2026-11-01 03:00", now=NOW)
    b = s.plan(at="2026-11-01 03:00", now=NOW, ambiguous="later")
    assert a["intended_utc"] == b["intended_utc"] == "2026-11-01T08:00:00Z"


def test_explicit_offset_instant_is_honored_verbatim():
    p = s.plan(at="2026-09-11T16:49:00-04:00", now=NOW)
    assert p["intended_utc"] == "2026-09-11T20:49:00Z"
    assert p["source"]["interpreted_as"] == "absolute_instant_with_offset"


def test_fixed_utc_minus_5_is_separate_and_labelled():
    p = s.plan(at="2026-07-15 09:00", tz="UTC-05:00", now=NOW)
    assert p["intended_utc"] == "2026-07-15T14:00:00Z"  # NOT the 13:00Z an Eastern plan gives
    assert p["timezone"]["fixed_offset"] is True and p["timezone"]["requested"] == "UTC-05:00"
    assert p["requested_zone"]["fixed_offset"] is True
    assert p["eastern"]["display"] == "2026-07-15 10:00:00 EDT (UTC-04:00)"
    assert p["display"].startswith("2026-07-15 09:00:00 UTC-05:00 (UTC-05:00) = 2026-07-15 10:00:00 EDT")
    assert "not Eastern local time" in p["scheduler"]["note"]


def test_default_tz_is_eastern_without_being_asked():
    assert s.plan(at="2026-07-15 09:00", now=NOW)["intended_utc"] == \
        s.plan(at="2026-07-15 09:00", tz="eastern", now=NOW)["intended_utc"] == \
        s.plan(at="2026-07-15 09:00", tz="America/New_York", now=NOW)["intended_utc"]


# ---------------------------------------------------------------- planning: elapsed delays
@pytest.mark.parametrize("delay,secs", [("90m", 5400), ("1h30m", 5400), ("45s", 45), ("2d", 172800),
                                        ("1.5h", 5400), ("30", 30), (600, 600)])
def test_elapsed_delays(delay, secs):
    p = s.plan(elapsed=delay, now=NOW)
    assert p["delay_s"] == secs and p["mode"] == "elapsed"
    assert p["intended_epoch"] == s.parse_iso(NOW).timestamp() + secs


def test_elapsed_across_dst_transition_is_true_elapsed_not_wall_clock():
    p = s.plan(elapsed="3h", now="2026-03-08T05:00:00Z")  # 00:00 EST -> 04:00 EDT wall clock
    assert p["intended_utc"] == "2026-03-08T08:00:00Z"
    assert p["eastern"]["display"] == "2026-03-08 04:00:00 EDT (UTC-04:00)"


@pytest.mark.parametrize("bad", ["90x", "", "-5m", "h", "1h -30m"])
def test_invalid_elapsed_rejected(bad):
    assert _err(elapsed=bad, now=NOW) == "invalid_elapsed"


def test_target_exclusivity_and_past_refusal():
    assert _err(now=NOW) == "missing_target"
    assert _err(at="2026-02-01 09:00", elapsed="5m", now=NOW) == "conflicting_target"
    assert _err(at="2025-12-31 09:00", now=NOW) == "past_instant"
    assert s.plan(at="2025-12-31 09:00", now=NOW, allow_past=True)["delay_s"] < 0
    assert _err(at="2026-02-01 09:00", tz="Mars/Olympus", now=NOW) == "invalid_timezone"
    assert _err(at="2026-13-01 09:00", now=NOW) == "invalid_time"
    assert _err(at="2026-02-01 09:00", now="2026-02-01 08:00") == "invalid_time"  # naive now


def test_plan_is_pure_and_reproducible():
    assert s.plan(at="2026-02-01 09:00", now=NOW) == s.plan(at="2026-02-01 09:00", now=NOW)


# ---------------------------------------------------------------- verification
INTENDED = "2026-09-11T00:49:00Z"


def test_verify_match_within_tolerance_and_active():
    v = s.verify(intended_utc=INTENDED, next_run_at="2026-09-11T00:49:30Z", active=True)
    assert v["verdict"] == "match" and v["ok"] is True and v["requires_native_pause"] is False
    assert v["difference_s"] == 30.0
    assert v["intended_eastern"] == "2026-09-10 20:49:00 EDT (UTC-04:00)"
    assert v["persisted_eastern"] == "2026-09-10 20:49:30 EDT (UTC-04:00)"


def test_verify_mismatch_on_active_record_requires_native_pause():
    persisted = {"id": "x", "name": "n", "status": "ACTIVE", "next_run_at": "2026-09-11T20:49:12Z"}
    v = s.verify(intended_utc=INTENDED, persisted=persisted)
    assert v["verdict"] == "mismatch" and v["ok"] is False
    assert v["requires_native_pause"] is True and v["difference_s"] == 72012.0
    assert v["persisted_utc"] == "2026-09-11T20:49:12Z"


def test_verify_mismatch_on_inactive_record_does_not_require_pause():
    v = s.verify(intended_utc=INTENDED, next_run_at="2026-09-11T00:49:00Z", active=False)
    assert v["verdict"] == "mismatch" and v["reason"] == "automation_inactive"
    assert v["requires_native_pause"] is False


def test_verify_absent_or_unparseable_is_cannot_evaluate_never_pass_or_fail():
    for persisted in ({}, None, {"status": "ACTIVE"}, {"next_run_at": None, "status": "PAUSED"}):
        v = s.verify(intended_utc=INTENDED, persisted=persisted)
        assert v["verdict"] == "cannot_evaluate" and v["ok"] is False
        assert v["reason"] == "no_persisted_next_run_at"
    v = s.verify(intended_utc=INTENDED, next_run_at="garbage", active=True)
    assert v["verdict"] == "cannot_evaluate" and v["reason"].startswith("unparseable_next_run_at")
    v = s.verify(intended_utc=INTENDED, next_run_at=INTENDED)  # active flag unknown
    assert v["verdict"] == "cannot_evaluate" and v["reason"] == "active_flag_unknown"
    assert v["time_matches"] is True


def test_verify_accepts_epoch_milliseconds_and_status_words():
    ms = int(s.parse_iso(INTENDED).timestamp() * 1000) + 12_000
    v = s.verify(intended_utc=INTENDED, persisted={"next_run_at": ms, "status": "active"})
    assert v["verdict"] == "match" and v["difference_s"] == 12.0
    v = s.verify(intended_utc=INTENDED, persisted={"next_run_at": ms, "status": "PAUSED"})
    assert v["verdict"] == "mismatch" and v["reason"] == "automation_inactive"
    v = s.verify(intended_utc=INTENDED, persisted={"next_run_at": ms, "status": "ACTIVE"},
                 tolerance_s=5)
    assert v["verdict"] == "mismatch" and v["requires_native_pause"] is True


def test_read_automation_record_is_read_only_and_reports_absence(tmp_path):
    db = tmp_path / "codex-dev.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE automations (id TEXT PRIMARY KEY, name TEXT, status TEXT, rrule TEXT,"
                " prompt TEXT, next_run_at INTEGER, last_run_at INTEGER, updated_at INTEGER)")
    ms = int(s.parse_iso(INTENDED).timestamp() * 1000) + 7_000  # epoch ms, as the app stores it
    con.execute("INSERT INTO automations VALUES ('auto-1','Nightly audit','ACTIVE',"
                "'FREQ=DAILY;BYHOUR=0;BYMINUTE=49;COUNT=1','secret prompt',?,NULL,1)", (ms,))
    con.commit()
    con.close()
    before = db.read_bytes()
    r = s.read_automation_record(name="Nightly audit", db_path=str(db))
    assert r["found"] and r["matches"] == 1 and r["record"]["id"] == "auto-1"
    assert "prompt" not in r["record"] and r["record"]["status"] == "active"
    assert s.read_automation_record(automation_id="auto-1", db_path=str(db))["found"]
    v = s.verify(intended_utc=INTENDED, persisted=r["record"])
    assert v["verdict"] == "match" and v["difference_s"] == 7.0
    missing = s.read_automation_record(name="nope", db_path=str(db))
    assert missing == {"db": str(db), "found": False, "record": None, "matches": 0, "error": None}
    absent = s.read_automation_record(name="x", db_path=str(tmp_path / "none.db"))
    assert absent["error"] == "automation_store_not_found" and not absent["found"]
    assert db.read_bytes() == before
    with pytest.raises(ScheduleError) as ei:
        s.read_automation_record(db_path=str(db))
    assert ei.value.code == "missing_target"


# ---------------------------------------------------------------- the actual CLI
def _cli(*args, root):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-m", "mycelium_coord", "--root", str(root), *args],
                          env=env, text=True, capture_output=True)


def test_cli_sched_plan_and_verify_round_trip(tmp_path):
    r = _cli("sched-plan", "--at", "2026-09-10 20:49", "--now", "2026-09-10T20:00:00Z", root=tmp_path)
    assert r.returncode == 0, r.stderr
    plan = json.loads(r.stdout)
    assert plan["intended_utc"] == "2026-09-11T00:49:00Z"
    rec = tmp_path / "persisted.json"
    rec.write_text(json.dumps({"next_run_at": "2026-09-11T00:49:05Z", "status": "ACTIVE"}))
    r = _cli("sched-verify", "--intended-utc", plan["intended_utc"], "--persisted-json", str(rec),
             root=tmp_path)
    assert r.returncode == 0 and json.loads(r.stdout)["verdict"] == "match"
    r = _cli("sched-verify", "--intended-utc", plan["intended_utc"], "--next-run-at",
             "2026-09-11T20:49:12Z", "--active", "true", root=tmp_path)
    assert r.returncode == 3
    assert json.loads(r.stdout)["verdict"] == "mismatch"
    r = _cli("sched-verify", "--intended-utc", plan["intended_utc"], root=tmp_path)
    assert r.returncode == 3 and json.loads(r.stdout)["verdict"] == "cannot_evaluate"


def test_cli_errors_are_bounded_json_with_distinct_codes(tmp_path):
    r = _cli("sched-plan", "--at", "2026-03-08 02:30", "--now", NOW, root=tmp_path)
    assert r.returncode == 3
    body = json.loads(r.stdout)
    assert body["error"] == "nonexistent_local_time"
    assert len(r.stdout.encode()) < 1024 and r.stderr == ""
    r = _cli("sched-plan", "--at", "2026-11-01 01:30", "--now", NOW, root=tmp_path)
    assert json.loads(r.stdout)["error"] == "ambiguous_local_time" and len(r.stdout.encode()) < 1024
