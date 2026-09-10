"""Deterministic fixture tests for codex_ask.sh's adjacent JSON result receipt.

Exercises the wrapper's receipt classification against a fake `codex` CLI stub
(tests/fixtures/fake_codex/codex, kind: simulated — the real codex/ChatGPT-subscription CLI is
never invoked here) so the pass/fail-shaped scenarios named in the workflow-audit brief are
covered offline and repeatably: success, a nonfatal models-cache warning next to a real answer,
auth failure, empty output, partial output, a timeout (rc=124), a signal death (rc>128), and a
served-model identity that doesn't match what was requested.

The one real, non-simulated check against the actual `codex` CLI (a tiny trivial smoke question,
not a substantive review) is separate — see the run report, not this file, since it depends on
live quota/auth and is recorded as a reported outcome either way.

Run from the repo root:  python3 -m pytest tests/test_codex_ask_receipt.py -q
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "src" / "claude" / "tools" / "codex_ask.sh"
FAKE_CODEX_DIR = Path(__file__).resolve().parent / "fixtures" / "fake_codex"
FAKE_CODEX_BIN = FAKE_CODEX_DIR / "codex"


@pytest.fixture(scope="module", autouse=True)
def _fake_codex_executable():
    mode = FAKE_CODEX_BIN.stat().st_mode
    FAKE_CODEX_BIN.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    yield


def run_wrapper(
    tmp_path: Path,
    scenario: str,
    *,
    question: str = "trivial fixture question",
    model: str | None = None,
):
    out_file = tmp_path / "out.txt"
    env = dict(os.environ)
    env["PATH"] = f"{FAKE_CODEX_DIR}:{env.get('PATH', '')}"
    env["FAKE_CODEX_SCENARIO"] = scenario
    env["CODEX_ASK_OUTDIR"] = str(tmp_path)
    args = ["bash", str(WRAPPER), "-o", str(out_file)]
    if model:
        args += ["-m", model]
    args.append(question)
    proc = subprocess.run(
        args, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30
    )
    receipt_file = out_file.with_name(out_file.name + ".receipt.json")
    receipt = json.loads(receipt_file.read_text()) if receipt_file.exists() else None
    return proc, out_file, receipt


def test_success_is_complete_ok(tmp_path):
    proc, out_file, receipt = run_wrapper(tmp_path, "success")
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(out_file)  # stdout contract: only the output path
    assert out_file.exists() and out_file.stat().st_size > 0
    assert receipt is not None
    assert receipt["exit_code"] == 0
    assert receipt["parse_status"] == "ok"
    assert receipt["error_class"] is None
    assert receipt["complete"] is True
    assert receipt["warnings"] == []
    assert receipt["model_requested"] == "gpt-5.6-sol"
    assert receipt["artifact_path"] == str(out_file)
    assert isinstance(receipt["duration_s"], int)
    assert receipt["duration_s"] >= 0
    # Completeness now comes solely from the -o/--output-last-message artifact.
    assert receipt["final_artifact"] is not None


def test_models_cache_warning_with_valid_answer_is_not_a_failure(tmp_path):
    """A nonfatal models-cache warning next to a real final answer must still report complete."""
    proc, out_file, receipt = run_wrapper(tmp_path, "warning_then_answer")
    assert proc.returncode == 0
    assert receipt["parse_status"] == "ok"
    assert receipt["error_class"] is None
    assert receipt["complete"] is True
    assert receipt["warnings"] == ["models_cache_nonfatal"]


def test_auth_failure_does_not_look_complete(tmp_path):
    proc, out_file, receipt = run_wrapper(tmp_path, "auth_failure")
    assert proc.returncode == 1
    assert receipt["exit_code"] == 1
    assert receipt["parse_status"] == "auth_failure"
    assert receipt["error_class"] == "auth_failure"
    assert receipt["complete"] is False


def test_empty_output_does_not_look_complete(tmp_path):
    proc, out_file, receipt = run_wrapper(tmp_path, "empty_output")
    assert proc.returncode == 0  # the CLI itself "succeeded" with nothing to show
    assert receipt["parse_status"] == "empty_output"
    assert receipt["error_class"] == "empty_output"
    assert receipt["complete"] is False


def test_partial_output_does_not_look_complete(tmp_path):
    """A run that wrote SOME real final-message content to the -o artifact but still exited
    nonzero is 'partial_output' — distinct from a bare nonzero exit with no artifact at all."""
    proc, out_file, receipt = run_wrapper(tmp_path, "partial_output")
    assert proc.returncode != 0
    assert receipt["parse_status"] == "partial_output"
    assert receipt["error_class"] == "partial_output"
    assert receipt["complete"] is False


def test_timeout_does_not_look_complete(tmp_path):
    proc, out_file, receipt = run_wrapper(tmp_path, "timeout")
    assert proc.returncode == 124
    assert receipt["exit_code"] == 124
    assert receipt["parse_status"] == "timeout"
    assert receipt["error_class"] == "timeout"
    assert receipt["complete"] is False


def test_signal_death_does_not_look_complete(tmp_path):
    proc, out_file, receipt = run_wrapper(tmp_path, "killed")
    assert proc.returncode == 137
    assert receipt["parse_status"] == "timeout"
    assert receipt["error_class"] == "process_terminated"
    assert receipt["complete"] is False


def test_final_prose_containing_example_json_model_is_not_a_mismatch(tmp_path):
    """A real, complete final answer that merely quotes example JSON containing a DIFFERENT
    model slug (as sample/test data) must not be read as response metadata: model_served stays
    null on this CLI route, and no mismatch is ever manufactured from prose (round-2 case 3)."""
    proc, out_file, receipt = run_wrapper(
        tmp_path, "final_prose_json_model", model="gpt-5.6-sol"
    )
    assert proc.returncode == 0
    assert receipt["model_requested"] == "gpt-5.6-sol"
    assert receipt["model_served"] is None
    assert receipt["parse_status"] == "ok"
    assert receipt["error_class"] is None
    assert receipt["complete"] is True


def test_raw_output_and_cli_contract_are_unchanged(tmp_path):
    """The receipt is additive: exit code, stdout (just the path) and the raw output file
    content must be byte-identical to the pre-receipt contract."""
    proc, out_file, receipt = run_wrapper(tmp_path, "success")
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(out_file)
    raw = out_file.read_text()
    assert "codex response: reviewed the requested files" in raw
    # the receipt lives adjacent, never inside, the raw output file
    assert not raw.strip().startswith("{")


def test_banner_only_without_answer_does_not_look_complete(tmp_path):
    """A startup banner (model/session-id lines included) with NO answer body after it must
    never report complete:true, no matter how many bytes the banner itself occupies."""
    proc, out_file, receipt = run_wrapper(tmp_path, "banner_only")
    assert proc.returncode == 0
    assert (
        out_file.stat().st_size > 80
    )  # the banner alone clears the old byte-length floor
    assert receipt["complete"] is False
    assert receipt["parse_status"] != "ok"
    assert receipt["error_class"] is not None
    # The banner is legitimate startup configuration evidence for "resolved"; it must NOT be
    # promoted to "served" absent real response metadata.
    assert receipt["model_resolved"] == "gpt-5.6-sol"
    assert receipt["model_served"] is None


def test_warning_only_without_answer_does_not_look_complete(tmp_path):
    """A long nonfatal models-cache warning with no answer body must never report complete:true
    merely because the warning text clears a byte-length floor."""
    proc, out_file, receipt = run_wrapper(tmp_path, "warning_only")
    assert proc.returncode == 0
    assert receipt["complete"] is False
    assert receipt["parse_status"] != "ok"
    assert receipt["error_class"] is not None
    assert receipt["model_served"] is None


def test_valid_answer_mentioning_auth_phrase_is_not_auth_failure(tmp_path):
    """A real, complete final answer (rc=0) that merely discusses the literal string
    'not logged in' as prose must classify as complete, not auth_failure. Only diagnostic
    evidence from a trusted status channel (here: nonzero exit) may drive that classification."""
    proc, out_file, receipt = run_wrapper(tmp_path, "auth_prose_in_answer")
    assert proc.returncode == 0
    assert receipt["parse_status"] == "ok"
    assert receipt["error_class"] is None
    assert receipt["complete"] is True


def test_short_valid_final_answer_is_complete(tmp_path):
    """A genuinely short but complete, properly terminated final answer (well under the
    partial-output byte floor) must not fail merely for being short."""
    proc, out_file, receipt = run_wrapper(tmp_path, "short_valid_answer")
    assert proc.returncode == 0
    assert out_file.stat().st_size < 80  # shorter than the legacy partial-output floor
    assert receipt["parse_status"] == "ok"
    assert receipt["error_class"] is None
    assert receipt["complete"] is True


def test_warning_before_banner_without_answer_does_not_look_complete(tmp_path):
    """A nonfatal models-cache warning printed BEFORE the ordinary startup banner, with no final
    answer, must never report complete:true — ordering relative to the banner cannot matter once
    completeness depends solely on the -o artifact, not on parsing raw-log text position."""
    proc, out_file, receipt = run_wrapper(tmp_path, "banner_prefixed_by_warning")
    assert proc.returncode == 0
    assert receipt["complete"] is False
    assert receipt["parse_status"] == "empty_output"
    assert receipt["error_class"] == "empty_output"
    assert receipt["final_artifact"] is None


def test_banner_plus_prompt_echo_and_tool_log_without_answer_does_not_look_complete(
    tmp_path,
):
    """A normal banner plus an echoed user-prompt turn plus a tool-execution log line, but no
    final answer, must never report complete:true regardless of how much raw log text (prompt
    echo, tool output) surrounds the missing answer."""
    proc, out_file, receipt = run_wrapper(tmp_path, "prompt_and_tool_log_no_final")
    assert proc.returncode == 0
    assert (
        out_file.stat().st_size > 200
    )  # plenty of raw log text, but still no real answer
    assert receipt["complete"] is False
    assert receipt["parse_status"] == "empty_output"
    assert receipt["error_class"] == "empty_output"
    assert receipt["final_artifact"] is None


def test_short_complete_token_is_complete(tmp_path):
    """A single-word but genuinely complete final answer ('READY') written to the real -o
    artifact must report complete:true — no punctuation or length heuristic is used at all."""
    proc, out_file, receipt = run_wrapper(tmp_path, "short_complete_token")
    assert proc.returncode == 0
    assert receipt["parse_status"] == "ok"
    assert receipt["error_class"] is None
    assert receipt["complete"] is True
    assert receipt["final_artifact"] is not None


def test_dry_run_writes_no_receipt(tmp_path):
    out_file = tmp_path / "dryrun_out.txt"
    env = dict(os.environ)
    env["PATH"] = f"{FAKE_CODEX_DIR}:{env.get('PATH', '')}"
    env["FAKE_CODEX_SCENARIO"] = "success"
    env["CODEX_ASK_OUTDIR"] = str(tmp_path)
    proc = subprocess.run(
        ["bash", str(WRAPPER), "-n", "-o", str(out_file), "trivial fixture question"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert not out_file.exists()
    assert not out_file.with_name(out_file.name + ".receipt.json").exists()


def test_stale_derived_artifact_never_trusted_when_output_dir_readonly(tmp_path):
    """Regression for the wrapper-stale-artifact review case
    (work/probe_wrapper_stale_artifact.py). A read-only output directory holds a writable OUT_FILE
    and a stale predictable-path '<out>.last-message' left by a prior run. The old scheme derived
    the artifact path from OUT_FILE and `rm -f`'d it before the call; that unlink fails silently in
    a non-writable directory, leaving the stale file to falsely mark a later banner-only run
    complete:true off a prior answer. With a UNIQUE mktemp'd artifact (created fresh in the writable
    OUTDIR fallback), the stale predictable-path file is never consulted or removed, and a
    banner-only run classifies complete:false."""
    outdir = tmp_path / "outdir"  # writable OUTDIR: prompt file + fresh-artifact fallback live here
    outdir.mkdir()
    ro = tmp_path / "readonly"  # read-only output directory (dir writes denied; existing files ok)
    ro.mkdir()
    out_file = ro / "review.txt"
    out_file.write_text("prior raw log")
    stale = ro / "review.txt.last-message"
    stale.write_text("OLD ANSWER FROM A PRIOR RUN")
    # A pre-existing writable receipt so classification is observable even though the dir denies
    # creating new entries (truncate-write of an existing writable file needs no dir-write perm).
    receipt_file = ro / "review.txt.receipt.json"
    receipt_file.write_text("{}")
    out_file.chmod(0o600)
    stale.chmod(0o600)
    receipt_file.chmod(0o600)
    ro.chmod(0o500)
    try:
        env = dict(os.environ)
        env["PATH"] = f"{FAKE_CODEX_DIR}:{env.get('PATH', '')}"
        env["FAKE_CODEX_SCENARIO"] = "banner_only"  # rc=0, no artifact written (startup only)
        env["CODEX_ASK_OUTDIR"] = str(outdir)
        proc = subprocess.run(
            ["bash", str(WRAPPER), "-o", str(out_file), "fixture only"],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30,
        )
        receipt = json.loads(receipt_file.read_text())
    finally:
        ro.chmod(0o700)
    assert proc.returncode == 0
    # Security property: the run is NOT reported complete off the stale prior-run artifact.
    assert receipt["complete"] is False
    assert receipt["parse_status"] != "ok"
    assert receipt["error_class"] is not None
    # The stale predictable-path artifact is preserved untouched — never consulted, never removed.
    assert stale.read_text() == "OLD ANSWER FROM A PRIOR RUN"


def test_deadline_times_out_via_owned_launcher(tmp_path):
    """codex_ask.sh -t routes through the owned-process launcher (codex_launch.py), which owns the
    codex process group and enforces a hard deadline. A slow run is terminated and reported
    timed_out (rc=124, complete:false) even with partial raw output; -x/-a are recorded."""
    out_file = tmp_path / "out.txt"
    env = dict(os.environ)
    env["PATH"] = f"{FAKE_CODEX_DIR}:{env.get('PATH', '')}"
    env["FAKE_CODEX_SCENARIO"] = "slow"
    env["CODEX_ASK_OUTDIR"] = str(tmp_path)
    args = [
        "bash", str(WRAPPER), "-o", str(out_file),
        "-t", "1", "-x", "exec-XYZ", "-a", "act-1", "slow fixture question",
    ]
    proc = subprocess.run(args, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30)
    receipt_file = out_file.with_name(out_file.name + ".receipt.json")
    receipt = json.loads(receipt_file.read_text())
    assert proc.returncode == 124
    assert receipt["exit_code"] == 124
    assert receipt["parse_status"] == "timeout"
    assert receipt["error_class"] == "timeout"
    assert receipt["complete"] is False
    assert receipt["execution_ref"] == "exec-XYZ"
    assert receipt["action_ref"] == "act-1"
    raw = out_file.read_text()
    assert "starting a long review" in raw          # partial raw output preserved
    assert "deadline of" in raw                       # launcher's explicit deadline marker
    assert proc.stdout.strip() == str(out_file)       # stdout contract: only the output path


def test_execution_and_action_refs_default_null_and_schema_bumped(tmp_path):
    """Without -x/-a the receipt records nulls, and the additive fields bump the schema minor."""
    proc, out_file, receipt = run_wrapper(tmp_path, "success")
    assert receipt["execution_ref"] is None
    assert receipt["action_ref"] is None
    assert receipt["schema_version"] == "1.1.0"


def test_deadline_path_success_is_still_complete(tmp_path):
    """A fast run UNDER the deadline still classifies exactly like the inline path (rc 0, complete)."""
    out_file = tmp_path / "out.txt"
    env = dict(os.environ)
    env["PATH"] = f"{FAKE_CODEX_DIR}:{env.get('PATH', '')}"
    env["FAKE_CODEX_SCENARIO"] = "success"
    env["CODEX_ASK_OUTDIR"] = str(tmp_path)
    args = ["bash", str(WRAPPER), "-o", str(out_file), "-t", "20", "fast fixture question"]
    proc = subprocess.run(args, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30)
    receipt = json.loads((out_file.with_name(out_file.name + ".receipt.json")).read_text())
    assert proc.returncode == 0
    assert receipt["parse_status"] == "ok"
    assert receipt["complete"] is True
    assert receipt["final_artifact"] is not None
