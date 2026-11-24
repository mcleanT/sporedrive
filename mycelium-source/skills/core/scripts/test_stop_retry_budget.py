"""Stop must never spend unbounded model turns on unresolved bookkeeping."""
import json
import os
from pathlib import Path
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from stop_retry_budget import retry_budget

PLUGIN = Path(__file__).resolve().parents[3]
HOOK = PLUGIN / "skills/core/hooks/mycelium-stop-check.sh"


def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    living = tmp_path / ".living"
    living.mkdir()
    for name in ("learnings.md", "decisions.md", "conventions.md"):
        p = living / name
        p.write_text("# Existing knowledge\n")
        os.utime(p, (time.time() - 7200,) * 2)
    state = tmp_path / ".mycelium"
    state.mkdir()
    (state / "mycelium-reminded.tmp").write_text(str(int(time.time()) - 3600))
    return state


def stop(root, sid="review", active=False):
    result = subprocess.run(
        ["bash", str(HOOK)], cwd=root,
        input=json.dumps({"session_id": sid, "stop_hook_active": active}),
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else {}


@pytest.mark.parametrize("active", [False, True])
def test_unresolved_stop_is_bounded_even_without_host_recursion_flag(tmp_path, active):
    state = repo(tmp_path)
    original = (state / "mycelium-reminded.tmp").read_bytes()
    results = [stop(tmp_path, active=active) for _ in range(5)]
    assert [x.get("decision") for x in results] == ["block", "block", None, None, None]
    assert (state / "mycelium-reminded.tmp").read_bytes() == original
    receipts = list(state.glob("stop-retry-*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"] == "deferred"
    assert receipt["prompts"] == 2
    assert receipt["last_reason"].startswith("STOP BLOCKED")
    assert not (state / "last-session.md").exists()


def test_concurrent_stops_share_one_budget(tmp_path):
    repo(tmp_path)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: stop(tmp_path), range(5)))
    assert sum(x.get("decision") == "block" for x in results) == 2


def test_repair_clears_budget_without_discarding_pending_work(tmp_path):
    state = repo(tmp_path)
    for _ in range(3):
        stop(tmp_path)
    (tmp_path / ".living/learnings.md").write_text("# Repaired\n")
    assert stop(tmp_path).get("decision") != "block"
    assert not list(state.glob("stop-retry-*.json"))
    assert not (state / "mycelium-reminded.tmp").exists()


def test_new_session_has_independent_budget(tmp_path):
    repo(tmp_path)
    for _ in range(3):
        stop(tmp_path, "first")
    assert stop(tmp_path, "second").get("decision") == "block"


def test_corrupt_budget_does_not_restart_model_loop(tmp_path):
    state = repo(tmp_path)
    stop(tmp_path)
    receipt = next(state.glob("stop-retry-*.json"))
    receipt.write_text("{broken")
    assert stop(tmp_path).get("decision") != "block"
    assert receipt.read_text() == "{broken"


def test_changing_reason_does_not_renew_budget(tmp_path):
    assert retry_budget(tmp_path, "review", "claim", "two files changed")
    assert retry_budget(tmp_path, "review", "claim", "nine files changed")
    assert not retry_budget(tmp_path, "review", "claim", "registry failed")


def test_no_work_cleanup_is_byte_stable(tmp_path):
    assert retry_budget(tmp_path, "review", "clear")
    assert not list(tmp_path.iterdir())


def test_symlink_receipt_is_not_read_or_overwritten(tmp_path):
    assert retry_budget(tmp_path, "review", "claim", "pending")
    receipt = next(tmp_path.glob("stop-retry-*.json"))
    target = tmp_path / "untouched.txt"
    target.write_text("private original")
    receipt.unlink()
    receipt.symlink_to(target)
    with pytest.raises(ValueError, match="unsafe retry receipt"):
        retry_budget(tmp_path, "review", "claim", "pending")
    assert target.read_text() == "private original"
    assert receipt.is_symlink()


def test_receipt_permissions_are_preserved(tmp_path):
    assert retry_budget(tmp_path, "review", "claim", "pending")
    receipt = next(tmp_path.glob("stop-retry-*.json"))
    receipt.chmod(0o600)
    assert retry_budget(tmp_path, "review", "claim", "pending")
    assert receipt.stat().st_mode & 0o777 == 0o600
