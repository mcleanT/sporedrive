#!/usr/bin/env python3
"""Regressions for the r2 provenance-scan performance + attribution fixes.

Covers, invoking session_file_changes.py as a subprocess exactly as the hooks
do (real CLI wiring, real files, real git):

  * bounded repeated work  -- a repeated scan of an unchanged tree content-hashes
    ZERO files (the seq80 latency fix: no more full re-hash per Bash call);
  * legacy-cache upgrade    -- a pre-r2 CACHE entry that lacks ctime_ns/ino must
    NOT bless a stale content hash; a same-size, mtime-restored rewrite is still
    detected (seq87), forcing a one-time re-hash;
  * advance-then-observe    -- once an explicit writer folds its path into the
    baseline, a later observer scan does not re-detect/re-attribute it (seq82);
  * concurrent advances     -- N concurrent advance-baseline calls to distinct
    files never lose a baseline entry (seq88: atomic replace alone does not
    serialize read-modify-write; the flock does);
  * mixed observer + writer -- a read-only observer scan racing an explicit
    writer advance on the SAME path leaves the WRITER as the ledger's last
    writer, never the observer (seq90: ledger append is inside the locked
    baseline transaction, and the lock is fail-closed);
  * cache lifetime          -- an accepted Stop resets the shared ownership
    baseline but retains the private fingerprint cache, so the next SessionStart
    rebuilds a fresh baseline WITHOUT re-hashing an unchanged tree (seq93);
  * old-reader compatibility -- the SHARED baseline carries only the legacy stat
    keys (mode,size,mtime_ns,content) so a preserved old runtime that compares
    stat dicts literally does not false-positive; the new cheap-stat fields
    (ctime_ns,ino) live only in the private cache (seq95).

Run: python3 -m pytest test_provenance_scan_r2.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parent / "session_file_changes.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path: Path, untracked: int = 0) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    for i in range(untracked):
        (repo / f"data_{i}.bin").write_text(f"pre-existing {i}\n")
    return repo


def _state_dir(repo: Path) -> Path:
    """Create the Mycelium state dir the way the real hook does -- gitignored, so
    its own state (baseline, cache, ledger) never enters the worktree scan."""
    state = repo / ".mycelium"
    state.mkdir(exist_ok=True)
    (state / ".gitignore").write_text("*\n!.gitignore\n")
    return state


def _state_baseline(repo: Path) -> Path:
    return _state_dir(repo) / "prov.json"


def _run(repo: Path, *args: str, hash_counter: Path | None = None) -> str:
    env = dict(os.environ)
    if hash_counter is not None:
        env["MYCELIUM_HASH_COUNTER_FILE"] = str(hash_counter)
    result = subprocess.run(
        [sys.executable, str(HELPER), *args],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _hash_count(counter: Path) -> int:
    if not counter.exists():
        return 0
    return sum(1 for line in counter.read_text().splitlines() if line.strip())


def test_repeated_scan_unchanged_tree_does_no_rehash(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, untracked=25)
    baseline = _state_baseline(repo)
    cache = _state_dir(repo) / "cache.json"
    counter = tmp_path / "hashes.log"

    # First scan establishes the baseline + cache (must hash every changed/
    # untracked file at least once) and attributes nothing.
    out = _run(
        repo,
        "scan",
        "--repo-root",
        str(repo),
        "--baseline",
        str(baseline),
        "--cache",
        str(cache),
        hash_counter=counter,
    )
    assert out.strip() == ""
    assert _hash_count(counter) >= 25
    assert baseline.exists()
    assert cache.exists()

    # A repeated scan over the byte-identical tree must re-hash NOTHING.
    counter.unlink()
    out = _run(
        repo,
        "scan",
        "--repo-root",
        str(repo),
        "--baseline",
        str(baseline),
        "--cache",
        str(cache),
        hash_counter=counter,
    )
    assert out.strip() == ""
    assert _hash_count(counter) == 0, (
        "unchanged tree was re-hashed (latency regression)"
    )


def test_scan_detects_new_and_modified(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, untracked=5)
    baseline = _state_baseline(repo)
    cache = _state_dir(repo) / "cache.json"
    _run(
        repo,
        "scan",
        "--repo-root",
        str(repo),
        "--baseline",
        str(baseline),
        "--cache",
        str(cache),
    )

    (repo / "new_work.py").write_text("print('hi')\n")
    (repo / "data_0.bin").write_text("mutated content is longer than before\n")
    out = _run(
        repo,
        "scan",
        "--repo-root",
        str(repo),
        "--baseline",
        str(baseline),
        "--cache",
        str(cache),
    )
    delta = set(out.split())
    assert "new_work.py" in delta
    assert "data_0.bin" in delta


def test_legacy_cache_missing_ctime_forces_rehash(tmp_path: Path) -> None:
    """seq87: a pre-r2 CACHE entry (no ctime_ns/ino) must not bless a stale hash.
    A same-size, mtime-restored rewrite is still detected."""
    repo = _make_repo(tmp_path)
    baseline = _state_baseline(repo)
    cache = _state_dir(repo) / "cache.json"
    target = repo / "changed.py"
    target.write_text("AAAA\n")  # 5 bytes
    stat0 = target.stat()

    # Establish baseline + cache, then downgrade the CACHE to emulate a pre-r2
    # schema by stripping the new cheap-stat fields from every entry.
    _run(
        repo,
        "scan",
        "--repo-root",
        str(repo),
        "--baseline",
        str(baseline),
        "--cache",
        str(cache),
    )
    data = json.loads(cache.read_text())
    for entry in data["cheap"].values():
        entry.pop("ctime_ns", None)
        entry.pop("ino", None)
    cache.write_text(json.dumps(data))

    # Same-length different bytes, mtime restored (ctime necessarily advances).
    target.write_text("BBBB\n")  # also 5 bytes
    os.utime(target, ns=(stat0.st_atime_ns, stat0.st_mtime_ns))

    out = _run(
        repo,
        "scan",
        "--repo-root",
        str(repo),
        "--baseline",
        str(baseline),
        "--cache",
        str(cache),
    )
    assert "changed.py" in out.split(), (
        "legacy cache blessed a stale content hash (seq87 regression)"
    )
    # The refreshed cache must now carry the complete identity.
    refreshed = json.loads(cache.read_text())["cheap"]["changed.py"]
    assert "ctime_ns" in refreshed and "ino" in refreshed


def test_advance_baseline_prevents_observer_reattribution(tmp_path: Path) -> None:
    """seq82: after an explicit writer folds its path into the baseline, an
    observer scan does not re-detect it."""
    repo = _make_repo(tmp_path, untracked=5)
    baseline = _state_baseline(repo)
    cache = _state_dir(repo) / "cache.json"
    # Observer establishes the baseline BEFORE the writer's file exists.
    _run(
        repo,
        "scan",
        "--repo-root",
        str(repo),
        "--baseline",
        str(baseline),
        "--cache",
        str(cache),
    )

    # Writer creates the file and advances the baseline (the Edit/Write path).
    (repo / "foreign.py").write_text("written by B\n")
    _run(
        repo,
        "advance-baseline",
        "--repo-root",
        str(repo),
        "--baseline",
        str(baseline),
        "--cache",
        str(cache),
        "--path",
        "foreign.py",
    )

    # Observer scan must NOT surface foreign.py (it is already in the baseline).
    out = _run(
        repo,
        "scan",
        "--repo-root",
        str(repo),
        "--baseline",
        str(baseline),
        "--cache",
        str(cache),
    )
    assert "foreign.py" not in out.split(), (
        "observer re-attributed an explicit writer's file (seq82 regression)"
    )


def test_concurrent_advances_do_not_lose_updates(tmp_path: Path) -> None:
    """seq88: N concurrent advance-baseline calls to distinct files must all
    land; atomic replace alone does not serialize read-modify-write."""
    repo = _make_repo(tmp_path, untracked=300)
    baseline = _state_baseline(repo)
    cache = _state_dir(repo) / "cache.json"
    _run(
        repo,
        "scan",
        "--repo-root",
        str(repo),
        "--baseline",
        str(baseline),
        "--cache",
        str(cache),
    )

    n = 8
    procs = []
    for i in range(n):
        name = f"writer_{i}.py"
        (repo / name).write_text(f"concurrent {i}\n")
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(HELPER),
                    "advance-baseline",
                    "--repo-root",
                    str(repo),
                    "--baseline",
                    str(baseline),
                    "--cache",
                    str(cache),
                    "--path",
                    name,
                ],
                cwd=str(repo),
            )
        )
    for p in procs:
        assert p.wait() == 0

    files = json.loads(baseline.read_text())["files"]
    missing = [f"writer_{i}.py" for i in range(n) if f"writer_{i}.py" not in files]
    assert not missing, (
        f"lost concurrent baseline updates (seq88 regression): {missing}"
    )


def test_mixed_concurrent_observer_and_writer_writer_owns(tmp_path: Path) -> None:
    """seq90: a read-only observer scan and an explicit writer advance racing on
    the SAME path must leave the WRITER as the ledger's LAST writer for that
    path -- never the observer. The ledger append now lives inside the same
    locked baseline transaction, so the two are totally ordered and
    last-writer-wins reflects the true (writer) owner in every interleaving."""
    for trial in range(6):
        base = tmp_path / f"trial{trial}"
        base.mkdir()
        repo = _make_repo(base, untracked=40)
        baseline = _state_baseline(repo)
        cache = _state_dir(repo) / "cache.json"
        ledger = _state_dir(repo) / "ledger.tmp"
        # Baseline established BEFORE the shared path exists, so an observer scan
        # would otherwise see it as a new change and could claim it.
        _run(
            repo,
            "scan",
            "--repo-root",
            str(repo),
            "--baseline",
            str(baseline),
            "--cache",
            str(cache),
        )
        name = "shared.py"
        (repo / name).write_text("written by the explicit writer\n")
        observer = subprocess.Popen(
            [
                sys.executable,
                str(HELPER),
                "scan",
                "--repo-root",
                str(repo),
                "--baseline",
                str(baseline),
                "--cache",
                str(cache),
                "--ledger",
                str(ledger),
                "--session-id",
                "observer",
            ],
            cwd=str(repo),
        )
        writer = subprocess.Popen(
            [
                sys.executable,
                str(HELPER),
                "advance-baseline",
                "--repo-root",
                str(repo),
                "--baseline",
                str(baseline),
                "--cache",
                str(cache),
                "--ledger",
                str(ledger),
                "--session-id",
                "writer",
                "--path",
                name,
            ],
            cwd=str(repo),
        )
        assert observer.wait() == 0
        assert writer.wait() == 0
        rows = (
            [
                line
                for line in ledger.read_text().splitlines()
                if line.endswith(f" {name}")
            ]
            if ledger.exists()
            else []
        )
        assert rows, f"trial {trial}: no ledger row for {name}"
        assert rows[-1].split()[0] == "writer", (
            f"trial {trial}: a read-only observer became the last writer of an "
            f"explicit writer's file (seq90 regression); ledger rows={rows}"
        )


def test_fingerprint_cache_survives_stop_no_rehash_on_next_start(
    tmp_path: Path,
) -> None:
    """seq93: an accepted Stop resets the shared (ownership) baseline but RETAINS
    the private fingerprint cache, so the next SessionStart rebuilds a fresh
    baseline WITHOUT re-hashing an unchanged tree."""
    repo = _make_repo(tmp_path, untracked=40)
    baseline = _state_baseline(repo)
    cache = _state_dir(repo) / "fingerprint-cache.json"
    counter = tmp_path / "hashes.log"

    # SessionStart A establishes baseline + cache (hashes every untracked file).
    _run(
        repo,
        "snapshot",
        "--repo-root",
        str(repo),
        "--output",
        str(baseline),
        "--prior",
        str(cache),
        "--cache",
        str(cache),
        hash_counter=counter,
    )
    assert _hash_count(counter) >= 40
    assert baseline.exists() and cache.exists()

    # Accepted Stop: reset the shared baseline (ownership), KEEP the cache.
    baseline.unlink()
    assert cache.exists()

    # SessionStart B rebuilds a FRESH baseline reusing the retained cache.
    counter.unlink()
    _run(
        repo,
        "snapshot",
        "--repo-root",
        str(repo),
        "--output",
        str(baseline),
        "--prior",
        str(cache),
        "--cache",
        str(cache),
        hash_counter=counter,
    )
    assert baseline.exists()
    assert _hash_count(counter) == 0, (
        "next SessionStart re-hashed an unchanged tree after Stop "
        "(seq93 cache-lifetime regression)"
    )


def test_shared_baseline_stays_legacy_for_old_readers(tmp_path: Path) -> None:
    """seq95: the SHARED baseline must carry ONLY the legacy stat keys
    (mode,size,mtime_ns,content). A preserved old runtime compares stat dicts
    literally, so the new cheap-stat fields (ctime_ns,ino) would make it report
    every unchanged file changed. Those fields live only in the private cache."""
    repo = _make_repo(tmp_path, untracked=3)
    baseline = _state_baseline(repo)
    cache = _state_dir(repo) / "cache.json"
    _run(
        repo,
        "scan",
        "--repo-root",
        str(repo),
        "--baseline",
        str(baseline),
        "--cache",
        str(cache),
    )

    baseline_files = json.loads(baseline.read_text())["files"]
    assert baseline_files, "baseline recorded no files"
    for entry in baseline_files.values():
        stat = entry["stat"]
        assert set(stat.keys()) == {"mode", "size", "mtime_ns", "content"}, (
            f"shared baseline leaked cheap-stat fields to old readers: "
            f"{sorted(stat.keys())}"
        )

    cache_files = json.loads(cache.read_text())["cheap"]
    assert cache_files, "cache recorded no files"
    for stat in cache_files.values():
        assert "ctime_ns" in stat and "ino" in stat, (
            "private cache is missing the cheap-stat fields it exists to hold"
        )

    # Reproduce an old reader's literal stat-dict comparison against an unchanged
    # file: its own fresh 4-field stat must equal the stored legacy stat (no
    # false 'changed'). content is identical because the file is unchanged.
    for rel, entry in baseline_files.items():
        st = (repo / rel).lstat()
        old_reader_stat = {
            "mode": st.st_mode,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "content": entry["stat"]["content"],
        }
        assert old_reader_stat == entry["stat"], (
            f"an old-reader stat comparison would false-positive on {rel}"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
