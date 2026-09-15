"""Efficiency v2, brief item 1 — routine workers: explicit gpt-5.6-luna@low routing on top of owned
jobs, compact fresh prompt context, no-delegation env, deterministic output validation with
provenance, and exactly one escalation record on failure (never an automatic retry).

Run: python3 -m pytest coordination/tests/test_workers.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord.execution import ExecutionManager  # noqa: E402
from mycelium_coord.jobs import JobManager  # noqa: E402
from mycelium_coord.store import CoordStore  # noqa: E402
from mycelium_coord.workers import PREAMBLE, WorkerError, WorkerManager  # noqa: E402

AUTH = "/auth/user-instruction.md"
SCOPE = "/scope/frozen.md"

FAKE_CODEX = """#!/bin/sh
# fake codex: record argv / stdin / env, print a banner, write the -o file, exit with FAKE_RC
d="{rec}"
printf '%s\\n' "$@" > "$d/argv.txt"
cat > "$d/stdin.txt"
env | grep '^MYCELIUM_' | sort > "$d/env.txt"
echo "OpenAI Codex v0.154.0"
echo "model: gpt-5.6-luna"
echo "--------"
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
[ -n "$out" ] && printf '%s' '{body}' > "$out"
exit {rc}
"""


def _managed(root, task="t1", identity="brief-1"):
    em = ExecutionManager(CoordStore(root))
    em.open_execution(task, execution_id=f"exec-{task}", scope_ref=SCOPE, authorization_ref=AUTH)
    em.reserve(task, action_id="impl-1", kind="work_dispatch")
    assert em.claim_dispatch(task, action_id="impl-1", dispatch_identity=identity)["ok"]
    return dict(task_id=task, action_id="impl-1", dispatch_identity=identity)


def _fake(tmp_path, body='{"answer": 42, "ok": true}', rc=0):
    rec = tmp_path / "fake"
    rec.mkdir(exist_ok=True)
    script = tmp_path / "codex"
    script.write_text(FAKE_CODEX.format(rec=rec, body=body, rc=rc))
    script.chmod(0o755)
    return script, rec


def _prompt(tmp_path, text="Return {\"answer\": 42, \"ok\": true} as JSON."):
    p = tmp_path / "prompt.md"
    p.write_text(text)
    return p


def _run(tmp_path, body='{"answer": 42, "ok": true}', rc=0, require_keys=("answer",), wid="w1"):
    root = tmp_path / "state"
    codex, rec = _fake(tmp_path, body=body, rc=rc)
    store = CoordStore(root)
    wm = WorkerManager(store)
    receipt = wm.run(wid, _prompt(tmp_path), profile="routine", require_keys=require_keys, join_s=30,
                     deadline_s=60, codex_bin=str(codex), **_managed(root))
    assert receipt["terminal"], receipt
    return wm, rec, receipt


def test_success_routes_luna_low_fresh_context_and_records_provenance(tmp_path):
    wm, rec, receipt = _run(tmp_path)
    assert receipt["launched"] is True and receipt["exit_code"] == 0
    assert receipt["model_requested"] == "gpt-5.6-luna" and receipt["effort_requested"] == "low"
    argv = (rec / "argv.txt").read_text().splitlines()
    assert argv[:3] == ["exec", "--sandbox", "read-only"]
    assert argv[argv.index("-m") + 1] == "gpt-5.6-luna"
    assert argv[argv.index("-c") + 1] in ('model_reasoning_effort="low"', "model_reasoning_effort=low")
    assert argv.count("--disable") == 2 and "multi_agent" in argv and "hooks" in argv
    assert argv[-1] == "-" and "--ignore-user-config" not in argv
    stdin = (rec / "stdin.txt").read_text()
    assert stdin.startswith(PREAMBLE) and stdin.endswith('Return {"answer": 42, "ok": true} as JSON.')
    env = (rec / "env.txt").read_text()
    for k in ("MYCELIUM_ROUTINE_WORKER=1", "MYCELIUM_NO_DELEGATE=1", "MYCELIUM_NO_HOUSEKEEPING=1"):
        assert k in env
    res = wm.result("w1")
    assert res["model_resolved"] == "gpt-5.6-luna" and res["validation"] == {"ok": True, "problems": []}
    assert res["escalated"] is False and res["escalation_path"] is None and res["exit_code"] == 0
    out = Path(res["output_path"]).read_bytes()
    assert res["output_sha256"] == hashlib.sha256(out).hexdigest() and res["output_bytes"] == len(out)
    assert json.loads(out)["answer"] == 42
    w = json.loads((wm._dir("w1") / "worker.json").read_text())
    assert w["prompt_sha256"] == hashlib.sha256(Path(w["prompt_path"]).read_bytes()).hexdigest()
    assert w["argv"][argv.index("-C") + 2] == w["workdir"] and not any(Path(w["workdir"]).iterdir())
    assert not (wm._dir("w1") / "escalation.json").exists()
    assert JobManager(wm.store).read_request("w1")["task_id"] == "t1"


@pytest.mark.parametrize("body,rc,expect_problem", [
    ('{"answer": 42}', 1, "process:"),
    ('{"other": 1}', 0, "missing_key: answer"),
    ('not json', 0, "output_not_json"),
])
def test_failure_escalates_exactly_once_and_never_relaunches(tmp_path, body, rc, expect_problem):
    wm, rec, receipt = _run(tmp_path, body=body, rc=rc)
    res = wm.result("w1")
    assert res["escalated"] is True and res["validation"]["ok"] is False
    assert any(p.startswith(expect_problem) for p in res["validation"]["problems"]), res
    esc = Path(res["escalation_path"])
    assert esc.is_file()
    first = esc.read_bytes()
    mtime = esc.stat().st_mtime_ns
    e = json.loads(first)
    assert e["remaining_allowance"] == "unchanged" and e["auto_retry"] is False
    assert e["model_requested"] == "gpt-5.6-luna" and Path(e["evidence"]["stdout"]).is_file()
    jobs_before = sorted(p.name for p in wm.store.path("jobs").glob("*"))
    argv_mtime = (rec / "argv.txt").stat().st_mtime_ns
    again = wm.result("w1")
    assert again == res
    assert esc.read_bytes() == first and esc.stat().st_mtime_ns == mtime
    assert sorted(p.name for p in wm.store.path("jobs").glob("*")) == jobs_before == ["w1"]
    assert (rec / "argv.txt").stat().st_mtime_ns == argv_mtime  # the fake codex was not invoked again


def test_prompt_over_16kib_refused(tmp_path):
    root = tmp_path / "state"
    codex, _ = _fake(tmp_path)
    wm = WorkerManager(CoordStore(root))
    big = _prompt(tmp_path, "x" * (16 * 1024 + 1))
    with pytest.raises(WorkerError) as ei:
        wm.run("w2", big, codex_bin=str(codex), **_managed(root))
    assert ei.value.code == "prompt_too_large"
    assert not wm.store.path("workers/w2").exists() and not wm.store.path("jobs/w2").exists()


def test_unknown_profile_refused(tmp_path):
    root = tmp_path / "state"
    codex, _ = _fake(tmp_path)
    wm = WorkerManager(CoordStore(root))
    with pytest.raises(WorkerError) as ei:
        wm.run("w3", _prompt(tmp_path), profile="astra", codex_bin=str(codex), **_managed(root))
    assert ei.value.code == "unknown_profile"
    assert not wm.store.path("jobs/w3").exists()


def test_result_before_terminal_and_unknown_worker(tmp_path):
    wm = WorkerManager(CoordStore(tmp_path / "state"))
    with pytest.raises(WorkerError) as ei:
        wm.result("nope")
    assert ei.value.code == "worker_not_found"


# ---------------------------------------------------------------- R2: the id binds the whole request
def test_r2_changed_payload_under_existing_id_is_refused_and_identical_replay_reuses(tmp_path):
    wm, rec, receipt = _run(tmp_path, wid="same")
    root = tmp_path / "state"
    codex = tmp_path / "codex"
    managed = dict(task_id="t1", action_id="impl-1", dispatch_identity="brief-1")
    before = wm.read_record("same")
    # identical replay: same job, same record, no relaunch
    again = wm.run("same", _prompt(tmp_path), profile="routine", require_keys=("answer",), join_s=5,
                   deadline_s=60, codex_bin=str(codex), **managed)
    assert again["replayed"] is True and again["request_fingerprint"] == before["request_fingerprint"]
    assert wm.read_record("same") == before
    # changed prompt
    other = tmp_path / "prompt-b.md"
    other.write_text("Different prompt B.")
    with pytest.raises(WorkerError) as ei:
        wm.run("same", other, profile="routine", require_keys=("answer",), codex_bin=str(codex), **managed)
    assert ei.value.code == "worker_identity_conflict"
    # changed validation contract / output target with the original prompt
    with pytest.raises(WorkerError) as ei:
        wm.run("same", _prompt(tmp_path), profile="routine", require_keys=("different",),
               codex_bin=str(codex), **managed)
    assert ei.value.code == "worker_identity_conflict"
    with pytest.raises(WorkerError) as ei:
        wm.run("same", _prompt(tmp_path), profile="routine", require_keys=("answer",),
               output_path=str(tmp_path / "elsewhere.json"), codex_bin=str(codex), **managed)
    assert ei.value.code == "worker_identity_conflict"
    assert wm.read_record("same") == before  # stored request untouched by refused calls
    assert len(list((root / "jobs").iterdir())) == 1  # no second job


def test_r2_preexisting_output_is_never_accepted(tmp_path):
    root = tmp_path / "state"
    codex, _ = _fake(tmp_path)
    pre = tmp_path / "pre.json"
    pre.write_text('{"answer": 42}')
    wm = WorkerManager(CoordStore(root))
    with pytest.raises(WorkerError) as ei:
        wm.run("w9", _prompt(tmp_path), output_path=str(pre), codex_bin=str(codex), **_managed(root))
    assert ei.value.code == "output_preexists"
    assert wm.read_record("w9") is None


def test_r2_concurrent_first_calls_bind_one_record(tmp_path):
    import threading
    root = tmp_path / "state"
    codex, _ = _fake(tmp_path)
    managed = _managed(root)
    wm = WorkerManager(CoordStore(root))
    prompt = _prompt(tmp_path)  # written once; the threads only read it
    results, errors = [], []

    def go(i):
        try:
            results.append(wm.run("wc", prompt, require_keys=("answer",), join_s=0,
                                  deadline_s=60, codex_bin=str(codex), **managed))
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    fps = {r["request_fingerprint"] for r in results}
    assert len(fps) == 1
    assert sum(1 for r in results if r.get("launched")) == 1
    assert sum(1 for r in results if r.get("replayed")) == 3
