#!/usr/bin/env python3
"""Snapshot and report repository changes made during one Mycelium session.

The Stop hook cannot treat the current ``git status`` as a session delta: a
repository may already contain uncommitted or untracked work when Codex starts.
SessionStart therefore records the dirty-state metadata and HEAD revision, and
Stop compares the current state against that baseline.  Explicit activity paths
are included even when the file was subsequently removed (for example, a
create/delete ``apply_patch`` probe).

Performance (r2, provenance-latency fix): content-hashing every ``git status``
entry on every scan is O(worktree bytes) and dominates a large dirty tree (e.g.
tens of GB of untracked data), which made every PostToolUse Bash hook take
minutes. The fix keeps a rolling content fingerprint and reuses it for any path
whose cheap stat identity (size, mtime_ns, ctime_ns, ino, mode) is unchanged, so
an unchanged path is never re-hashed. ctime_ns is what preserves the same-size /
restored-mtime guarantee: a content rewrite always bumps the inode change time
even when size and mtime are restored, and ctime cannot be set backwards from
user space.

Two on-disk representations (r2, cross-reader + cache-lifetime fixes):

* the SHARED baseline (mycelium-provenance-baseline.json) is written in the
  LEGACY schema -- each entry's ``stat`` carries only (mode, size, mtime_ns,
  content). A preserved old-runtime reader (R1) compares stat dictionaries
  directly, so adding the new cheap-stat fields there made it falsely report
  every unchanged file changed; keeping the shared baseline legacy preserves
  old/new-reader coexistence. Delta semantics are unchanged: the comparison
  projects an entry to (status, previous_path, mode, size, mtime_ns, content),
  which never depended on the cheap-stat fields.
* the PRIVATE fingerprint cache (mycelium-fingerprint-cache.json) is R2-only and
  holds the full cheap stat (adds ctime_ns, ino) plus the content fingerprint
  per path. It drives the re-hash short-circuit and, unlike the shared baseline
  (reset at each accepted Stop to refresh session ownership), it is retained
  across an accepted Stop so the next SessionStart can rebuild a fresh baseline
  WITHOUT re-hashing an unchanged large tree.

The single-pass ``scan`` command computes the provenance delta AND rewrites both
the shared baseline and the private cache from one worktree read, and appends
the observing session's provenance rows to the shared ledger INSIDE the same
locked transaction so a read-only observer can never overtake a genuine explicit
writer for a path.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
# Private fingerprint-cache schema. Distinct file, R2-only, never read by a
# legacy runtime, so it may carry the extra cheap-stat fields freely.
CACHE_SCHEMA_VERSION = 2
LIVING_FILES = ("learnings.md", "decisions.md", "conventions.md")
# Per-topic directory layout: .living/decisions/*.md (and any sibling
# directories following the same convention) hold substantive reflection
# content just like the top-level *.md files above.
LIVING_DIRS = ("decisions",)

# Test-only instrumentation: when set to a path, every ACTUAL content hash
# appends the hashed path to that file. A regression asserts a repeated scan of
# an unchanged tree hashes ZERO files (the bounded-repeat guarantee). Read once
# so production incurs at most a single os.environ lookup at import.
_HASH_COUNTER_FILE = os.environ.get("MYCELIUM_HASH_COUNTER_FILE") or None


class BaselineLockError(RuntimeError):
    """Raised when the shared-baseline lock cannot be created or acquired.

    A baseline read-modify-write that cannot be serialized must FAIL CLOSED
    rather than run unlocked: an unlocked transaction can drop a genuine
    writer's baseline+ledger update and let a read-only observer reclaim the
    path (the mixed observer/writer defect). Callers catch this and skip the
    one call's attribution instead of corrupting shared state.
    """


@contextmanager
def _baseline_lock(baseline_path: Path):
    """Bounded cross-process exclusive lock serializing every read-modify-write
    of the shared provenance baseline, its private cache, AND its coupled ledger
    append as ONE transaction.

    Atomic os.replace makes each write atomic but does NOT serialize the
    read->modify->write transaction: two concurrent advance-baseline calls (or
    an advance racing a scan) each read the same baseline and the second write
    clobbers the first's addition. Worse, the ledger append and the baseline
    update must be one transaction: if a read-only observer's scan and an
    explicit writer's advance interleave with the ledger appended OUTSIDE the
    lock, the observer can become the ledger's last writer for a path the writer
    actually created. Both scan and advance therefore acquire this lock and,
    inside it, re-read the baseline, append to the ledger, and rewrite the
    baseline + cache.

    Fail-closed: if the lock cannot be created or acquired, raise
    BaselineLockError rather than proceed unlocked. Skipping one call's
    attribution is strictly safer than an unserialized transaction that can
    silently misattribute a genuine writer's file.
    """
    lock_path = baseline_path.parent / (baseline_path.name + ".lock")
    fd = None
    try:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise BaselineLockError(f"cannot create baseline lock: {exc}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        os.close(fd)
        raise BaselineLockError(f"cannot acquire baseline lock: {exc}") from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _append_ledger(
    ledger_path: Path | None, session_id: str | None, rel_paths: list[str]
) -> None:
    """Append "<session_id> <relative-path>" rows to the shared append-only
    provenance ledger, one per path, in caller order, INSIDE the baseline lock.

    Coupling the append to the locked baseline transaction is what makes
    last-writer-wins correct under concurrency: an explicit writer's advance and
    a read-only observer's scan are now totally ordered, so a genuine writer's
    row is never overtaken by an observer's for a path it created, while a
    genuinely-later Bash writer still reclaims a path by appending last. A
    symlinked or non-regular ledger is never followed -- doing so would turn a
    normal hook into an arbitrary out-of-tree writer. Best-effort; never raises.
    """
    if ledger_path is None or not session_id:
        return
    if not session_id.isascii() or not all(
        char.isalnum() or char in "._-" for char in session_id
    ):
        return
    rows = [path for path in rel_paths if path]
    if not rows:
        return
    if os.path.islink(ledger_path):
        return
    if os.path.exists(ledger_path) and not os.path.isfile(ledger_path):
        return
    try:
        with open(ledger_path, "a", encoding="utf-8") as handle:
            for path in rows:
                handle.write(f"{session_id} {path}\n")
    except OSError:
        return


def _run_git(repo_root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else b""


def _head(repo_root: Path) -> str | None:
    value = (
        _run_git(repo_root, "rev-parse", "--verify", "HEAD")
        .decode("ascii", errors="ignore")
        .strip()
    )
    return value or None


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _cheap_stat(repo_root: Path, relative_path: str) -> dict[str, Any] | None:
    """lstat-only identity used to decide whether a re-hash is needed.

    Cheap (no content read). ctime_ns and ino are the fields that make skipping
    the hash safe: a same-size rewrite that restores mtime still changes ctime,
    and an atomic os.replace changes the inode number -- both are caught here
    without hashing, and neither can be forged backwards from user space.
    """
    try:
        stat = (repo_root / relative_path).lstat()
    except OSError:
        return None
    return {
        "mode": stat.st_mode,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "ino": stat.st_ino,
    }


def _stat_unchanged(
    prior_stat: dict[str, Any] | None, cheap: dict[str, Any] | None
) -> bool:
    """True when a fresh cheap stat proves the prior fingerprint is still valid.

    Reuse of a prior content hash requires a COMPLETE cheap identity: size,
    mtime_ns, mode, ctime_ns AND ino must all be present and match. A legacy or
    otherwise incomplete fingerprint (a pre-r2 cache entry that predates
    ctime_ns/ino) is deliberately NOT trusted: size+mtime+mode alone cannot see
    a same-size rewrite that restored its mtime, and blessing such an entry
    would return a stale content hash. Forcing a re-hash there costs one cold
    migration scan; the refreshed entry then carries ctime_ns/ino and every
    later warm scan short-circuits safely.
    """
    if not prior_stat or not cheap:
        return False
    for key in ("size", "mtime_ns", "mode", "ctime_ns", "ino"):
        if key not in prior_stat:
            return False
        if prior_stat.get(key) != cheap.get(key):
            return False
    return True


def _stat_fingerprint(repo_root: Path, relative_path: str) -> dict[str, Any] | None:
    """Full internal fingerprint: cheap stat (incl. ctime_ns/ino) + content sha.

    This full form is kept in memory while a scan runs and persisted verbatim to
    the PRIVATE fingerprint cache; the shared baseline stores the legacy
    projection of it (see ``_legacy_stat``).
    """
    try:
        path = repo_root / relative_path
        stat = path.lstat()
    except OSError:
        return None
    return {
        "mode": stat.st_mode,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "ino": stat.st_ino,
        # Metadata alone is not a content identity: a same-size rewrite can
        # restore its original mtime (intentionally or through a coarse
        # filesystem) and would otherwise disappear from the session delta.
        "content": _content_fingerprint(path),
    }


def _legacy_stat(stat: Any) -> Any:
    """Project a full stat down to the LEGACY schema (mode, size, mtime_ns,
    content) written to the SHARED baseline. Dropping the cheap-stat-only fields
    (ctime_ns, ino) is what keeps a preserved old-runtime reader -- which
    compares stat dicts literally -- from falsely seeing every file as changed.
    """
    if not isinstance(stat, dict):
        return stat
    return {
        key: stat[key] for key in ("mode", "size", "mtime_ns", "content") if key in stat
    }


def _legacy_files(files: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Project a full-entry map to the legacy shared-baseline representation."""
    legacy: dict[str, dict[str, Any]] = {}
    for path, entry in files.items():
        legacy[path] = {
            "status": entry.get("status"),
            "previous_path": entry.get("previous_path"),
            "stat": _legacy_stat(entry.get("stat")),
        }
    return legacy


def _cache_map(files: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Extract the private cache map (path -> full stat, incl. ctime_ns/ino/
    content) from a full-entry map."""
    cache: dict[str, dict[str, Any]] = {}
    for path, entry in files.items():
        stat = entry.get("stat")
        if isinstance(stat, dict):
            cache[path] = stat
    return cache


def load_cache(cache_path: Path | None) -> dict[str, dict[str, Any]]:
    """Load the private fingerprint cache, returning its path->full-stat map.

    Returns ``{}`` (short-circuit disabled -> re-hash) for a missing, unreadable,
    wrong-schema, or non-cache file, so a cold or corrupt cache is always safe.
    """
    if cache_path is None:
        return {}
    try:
        value = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    if value.get("schema_version") != CACHE_SCHEMA_VERSION:
        return {}
    cheap = value.get("cheap")
    if not isinstance(cheap, dict):
        return {}
    return cheap


def write_cache(cache_path: Path | None, cache_map: dict[str, dict[str, Any]]) -> None:
    if cache_path is None:
        return
    _write_payload(
        cache_path,
        {"schema_version": CACHE_SCHEMA_VERSION, "cheap": cache_map},
    )


def _decode_path(raw: bytes) -> str:
    return os.fsdecode(raw)


def _decode_paths(payload: bytes) -> set[str]:
    return {_decode_path(raw) for raw in payload.split(b"\0") if raw}


def _content_key(content: Any) -> Any:
    """A comparable, order-stable projection of the content fingerprint dict."""
    if isinstance(content, dict):
        return tuple(sorted(content.items()))
    return content


def _entry_identity(entry: dict[str, Any] | None) -> tuple | None:
    """Content-change identity of a worktree entry.

    Deliberately EXCLUDES the cheap-stat-only fields (ctime_ns, ino): they exist
    solely to decide whether a re-hash is needed and must never themselves make
    an otherwise-identical path look changed. This projection reproduces the
    pre-r2 delta exactly -- (status, previous_path, mode, size, mtime_ns,
    content) -- and works identically on a full entry or its legacy projection,
    so a full current entry compares correctly against a legacy baseline entry.
    """
    if not entry:
        return None
    stat = entry.get("stat") or {}
    return (
        entry.get("status"),
        entry.get("previous_path"),
        stat.get("mode"),
        stat.get("size"),
        stat.get("mtime_ns"),
        _content_key(stat.get("content")),
    )


def _entries_differ(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    return _entry_identity(a) != _entry_identity(b)


def _head_reflog(repo_root: Path) -> list[tuple[str, str]] | None:
    exists = subprocess.run(
        ["git", "-C", str(repo_root), "reflog", "exists", "HEAD"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if exists.returncode != 0:
        return None
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "reflog",
            "show",
            "--format=%H%x00%gs",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    payload = result.stdout
    records: list[tuple[str, str]] = []
    for record in payload.splitlines():
        raw_oid, separator, raw_action = record.partition(b"\0")
        oid = raw_oid.decode("ascii", errors="ignore").strip()
        if separator and oid:
            records.append((oid, os.fsdecode(raw_action)))
    return records


def _session_reflog_change_paths(
    repo_root: Path,
    baseline_head: str,
    baseline_reflog_entries: int,
) -> set[str] | None:
    """Replay content-producing HEAD transitions after the snapshot.

    Reflog entries are newest-first. Comparing each commit-like transition to
    the HEAD immediately before it captures the actual session delta: an amend
    excludes unchanged historical paths, while a normal commit that restores
    baseline content remains visible. Checkout and rebase bookkeeping update
    the prior HEAD but do not themselves claim historical paths as session work;
    content-producing rebase actions do.
    ``None`` asks callers to use the legacy timestamp fallback when the reflog
    was pruned or cannot be aligned with the snapshot.
    """
    records = _head_reflog(repo_root)
    if records is None:
        return None
    if len(records) < baseline_reflog_entries:
        return None
    new_entry_count = len(records) - baseline_reflog_entries
    session_records = list(reversed(records[:new_entry_count]))
    previous_head = baseline_head
    changed: set[str] = set()
    content_actions = (
        "commit:",
        "commit (initial):",
        "commit (amend):",
        "commit (merge):",
        "merge ",
        "merge:",
        "cherry-pick:",
        "revert:",
        "rebase (pick):",
        "rebase (reword):",
        "rebase (edit):",
        "rebase (squash):",
        "rebase (fixup):",
        "rebase (continue):",
    )
    for current_head, action in session_records:
        if action.startswith(content_actions):
            changed.update(
                _decode_paths(
                    _run_git(
                        repo_root,
                        "diff",
                        "--name-only",
                        "-z",
                        previous_head,
                        current_head,
                    )
                )
            )
        previous_head = current_head
    return changed


def _parse_status_payload(
    repo_root: Path,
    payload: bytes,
    cheap_prior: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Turn ``git status --porcelain=v1 -z`` output into full keyed entries,
    reusing an unchanged path's prior content fingerprint (no re-hash) when its
    cheap stat identity still matches the PRIVATE cache. Entries carry the full
    stat (incl. ctime_ns/ino); the caller projects to legacy for the shared
    baseline and keeps the full form for the private cache."""
    cheap_prior = cheap_prior or {}
    fields = payload.split(b"\0")
    entries: dict[str, dict[str, Any]] = {}
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record or len(record) < 4:
            continue
        status = record[:2].decode("ascii", errors="replace")
        path = _decode_path(record[3:])
        previous_path = None
        if "R" in status or "C" in status:
            if index < len(fields) and fields[index]:
                previous_path = _decode_path(fields[index])
                index += 1
        prior_stat = cheap_prior.get(path)
        cheap = _cheap_stat(repo_root, path)
        if prior_stat and _stat_unchanged(prior_stat, cheap):
            stat_fp: dict[str, Any] | None = dict(prior_stat)
        else:
            stat_fp = _stat_fingerprint(repo_root, path)
        entries[path] = {
            "status": status,
            "previous_path": previous_path,
            "stat": stat_fp,
        }
    return entries


def read_worktree_state(
    repo_root: Path, cheap_prior: dict[str, dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    """Return porcelain status plus full file metadata, keyed by repository path.

    When ``cheap_prior`` (the private cache's path->full-stat map) is supplied,
    an unchanged path reuses its prior content fingerprint instead of re-hashing.
    """
    payload = _run_git(
        repo_root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    return _parse_status_payload(repo_root, payload, cheap_prior)


def _scoped_worktree_state(
    repo_root: Path,
    relative_paths: list[str],
    cheap_prior: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """``read_worktree_state`` restricted to specific paths (a cheap pathspec
    ``git status``). Only dirty/untracked paths among the request appear."""
    if not relative_paths:
        return {}
    payload = _run_git(
        repo_root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        *relative_paths,
    )
    return _parse_status_payload(repo_root, payload, cheap_prior)


def _content_fingerprint(path: Path) -> dict[str, Any] | None:
    """Return a content identity that does not depend on timestamp precision."""
    try:
        stat = path.lstat()
    except OSError:
        return None
    if path.is_symlink():
        try:
            target = os.readlink(path)
        except OSError:
            target = ""
        return {"kind": "symlink", "target": target}
    if not path.is_file():
        return {"kind": "other", "mode": stat.st_mode}
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    if _HASH_COUNTER_FILE:
        try:
            with open(_HASH_COUNTER_FILE, "a", encoding="utf-8") as counter:
                counter.write(f"{path}\n")
        except OSError:
            pass
    return {
        "kind": "file",
        "size": stat.st_size,
        "sha256": digest.hexdigest(),
    }


def read_living_state(repo_root: Path) -> dict[str, dict[str, Any]]:
    """Fingerprint lifecycle artifacts whose content satisfies Stop."""
    living_dir = repo_root / ".living"
    candidates = [living_dir / name for name in LIVING_FILES]
    # Substantive knowledge can also live under a per-topic directory layout
    # (.living/decisions/*.md) alongside — or instead of — the single
    # top-level decisions.md file. Both are reflection content and must
    # satisfy Stop identically; only the directory shape differs.
    for directory_name in LIVING_DIRS:
        directory = living_dir / directory_name
        if directory.is_dir() and not directory.is_symlink():
            candidates.extend(
                path for path in directory.rglob("*") if not path.is_dir()
            )
        elif directory.exists() or directory.is_symlink():
            candidates.append(directory)
    findings_dir = living_dir / "findings"
    if findings_dir.is_dir() and not findings_dir.is_symlink():
        candidates.extend(path for path in findings_dir.rglob("*") if not path.is_dir())
    elif findings_dir.exists() or findings_dir.is_symlink():
        candidates.append(findings_dir)

    state: dict[str, dict[str, Any]] = {}
    for path in candidates:
        fingerprint = _content_fingerprint(path)
        if fingerprint is not None:
            state[path.relative_to(repo_root).as_posix()] = fingerprint
    return state


def _snapshot_payload(
    repo_root: Path, cheap_prior: dict[str, dict[str, Any]] | None
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Read the worktree once and return (legacy snapshot, full-entry map).

    The snapshot is written to a baseline file that a legacy runtime may read,
    so its ``files`` are the legacy projection; the full-entry map is kept so
    the caller can also refresh the private fingerprint cache from the same read.
    """
    current = read_worktree_state(repo_root, cheap_prior=cheap_prior)
    reflog = _head_reflog(repo_root)
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "head": _head(repo_root),
        "head_reflog_entries": len(reflog) if reflog is not None else None,
        "files": _legacy_files(current),
        "living_files": read_living_state(repo_root),
    }
    return snapshot, current


def build_snapshot(
    repo_root: Path, cheap_prior: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    snapshot, _ = _snapshot_payload(repo_root, cheap_prior)
    return snapshot


def _write_payload(output: Path, payload: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, output)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def write_snapshot(
    repo_root: Path,
    output: Path,
    cheap_prior: dict[str, dict[str, Any]] | None = None,
    cache_path: Path | None = None,
) -> None:
    snapshot, current = _snapshot_payload(repo_root, cheap_prior)
    _write_payload(output, snapshot)
    if cache_path is not None:
        write_cache(cache_path, _cache_map(current))


def write_living_snapshot(repo_root: Path, output: Path) -> None:
    """Write the minimal baseline needed for lifecycle-content comparison."""
    _write_payload(
        output,
        {
            "schema_version": SCHEMA_VERSION,
            "files": {},
            "living_files": read_living_state(repo_root),
        },
    )


def load_snapshot(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != SCHEMA_VERSION:
        return None
    if not isinstance(value.get("files"), dict):
        return None
    return value


def living_changed(repo_root: Path, baseline: dict[str, Any] | None) -> bool | None:
    """Return lifecycle-content change state, or None for an old baseline."""
    if baseline is None or not isinstance(baseline.get("living_files"), dict):
        return None
    return read_living_state(repo_root) != baseline["living_files"]


def _normalize_repo_path(repo_root: Path, raw_path: str) -> str | None:
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    try:
        return (
            candidate.resolve(strict=False)
            .relative_to(repo_root.resolve(strict=False))
            .as_posix()
        )
    except (OSError, ValueError):
        return None


def _activity_paths(repo_root: Path, activity_file: Path | None) -> set[str]:
    if activity_file is None:
        return set()
    try:
        lines = activity_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    return {
        normalized
        for line in lines
        if (normalized := _normalize_repo_path(repo_root, line)) is not None
    }


def _committed_paths(
    repo_root: Path,
    baseline_head: str | None,
    baseline_reflog_entries: int | None,
    start_ts: int | None,
) -> set[str]:
    current_head = _head(repo_root)
    if current_head is None:
        return set()
    if baseline_head:
        is_descendant = _is_ancestor(repo_root, baseline_head, current_head)
        if (current_head == baseline_head or not is_descendant) and isinstance(
            baseline_reflog_entries, int
        ):
            reflog_paths = _session_reflog_change_paths(
                repo_root,
                baseline_head,
                baseline_reflog_entries,
            )
            if reflog_paths is not None:
                return reflog_paths

        if current_head == baseline_head:
            return set()

        log_args = ["log", f"{baseline_head}..{current_head}"]
        if start_ts is not None:
            # A HEAD transition may be only a checkout of an existing branch.
            # Restrict the range to commits created during this session so the
            # destination branch's older history is not reported as new work.
            log_args.append(f"--since=@{start_ts}")
        log_args.extend(("--name-only", "-z", "--pretty=format:"))
        committed = _decode_paths(_run_git(repo_root, *log_args))
        if not is_descendant:
            # Amend/rebase/branch-switch transitions rewrite ancestry. Commit
            # path lists can then contain unchanged historical files, while a
            # raw tree diff can contain an existing destination branch. Their
            # intersection retains only recent commits whose final tree content
            # actually differs from the session baseline.
            tree_delta = _decode_paths(
                _run_git(
                    repo_root,
                    "diff",
                    "--name-only",
                    "-z",
                    baseline_head,
                    current_head,
                )
            )
            committed.intersection_update(tree_delta)
        return committed
    elif start_ts is not None:
        payload = _run_git(
            repo_root,
            "log",
            f"--since=@{start_ts}",
            "--name-only",
            "-z",
            "--pretty=format:",
        )
    else:
        payload = _run_git(
            repo_root, "ls-tree", "-r", "--name-only", "-z", current_head
        )
    return _decode_paths(payload)


def collect_changes(
    repo_root: Path,
    baseline: dict[str, Any] | None,
    activity_file: Path | None = None,
    start_ts: int | None = None,
    excludes: set[str] | None = None,
    exclude_prefixes: tuple[str, ...] = (".mycelium/",),
    current: dict[str, dict[str, Any]] | None = None,
    cheap_prior: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Return a sorted, de-duplicated list of session-local paths.

    ``current`` may be supplied by a caller that has already read the worktree
    state (the single-pass ``scan`` path) to avoid a second read. Otherwise the
    worktree is read here, reusing ``cheap_prior`` (the private cache) for
    unchanged paths so a large clean tree is not re-hashed.
    """
    excludes = excludes or set()
    baseline_files = baseline.get("files", {}) if baseline else {}
    if current is None:
        current = read_worktree_state(repo_root, cheap_prior=cheap_prior)
    changed: set[str] = _activity_paths(repo_root, activity_file)

    if baseline is not None:
        for path in set(current) | set(baseline_files):
            if _entries_differ(current.get(path), baseline_files.get(path)):
                changed.add(path)
    elif start_ts is not None:
        threshold_ns = start_ts * 1_000_000_000
        for path, entry in current.items():
            stat = entry.get("stat")
            if isinstance(stat, dict) and int(stat.get("mtime_ns", 0)) > threshold_ns:
                changed.add(path)

    changed.update(
        _committed_paths(
            repo_root,
            baseline.get("head") if baseline else None,
            baseline.get("head_reflog_entries") if baseline else None,
            start_ts,
        )
    )
    return sorted(
        path
        for path in changed
        if path
        and path not in excludes
        and not any(path.startswith(prefix) for prefix in exclude_prefixes)
    )


def _write_baseline_and_cache(
    repo_root: Path,
    baseline_path: Path,
    cache_path: Path | None,
    current: dict[str, dict[str, Any]],
) -> None:
    """Persist one worktree read as BOTH the legacy shared baseline (R1-readable)
    and the private fingerprint cache (ctime_ns/ino + content)."""
    reflog = _head_reflog(repo_root)
    _write_payload(
        baseline_path,
        {
            "schema_version": SCHEMA_VERSION,
            "head": _head(repo_root),
            "head_reflog_entries": len(reflog) if reflog is not None else None,
            "files": _legacy_files(current),
            "living_files": read_living_state(repo_root),
        },
    )
    write_cache(cache_path, _cache_map(current))


def cmd_scan(
    repo_root: Path,
    baseline_path: Path,
    excludes: set[str],
    exclude_prefixes: tuple[str, ...],
    cache_path: Path | None = None,
    ledger_path: Path | None = None,
    session_id: str | None = None,
) -> list[str]:
    """Single-pass rolling provenance scan: compute the delta since the last
    baseline, rewrite the shared baseline + private cache from ONE worktree read,
    AND append this observing session's provenance rows -- all inside one locked
    transaction.

    Replaces the prior ``collect`` + ``snapshot`` pair (two full scans per tool
    call). With no baseline yet it only ESTABLISHES a starting point and
    attributes nothing, so a first observation never blames the whole
    pre-existing tree on the observing session. Fails closed (returns []) when
    the baseline lock cannot be acquired.
    """
    try:
        with _baseline_lock(baseline_path):
            cheap_prior = load_cache(cache_path)
            prior = load_snapshot(baseline_path)
            if prior is None:
                current = read_worktree_state(repo_root, cheap_prior=cheap_prior)
                _write_baseline_and_cache(repo_root, baseline_path, cache_path, current)
                return []
            current = read_worktree_state(repo_root, cheap_prior=cheap_prior)
            changed = collect_changes(
                repo_root,
                prior,
                activity_file=None,
                start_ts=None,
                excludes=excludes,
                exclude_prefixes=exclude_prefixes,
                current=current,
            )
            _write_baseline_and_cache(repo_root, baseline_path, cache_path, current)
            _append_ledger(ledger_path, session_id, changed)
            return changed
    except BaselineLockError:
        return []


def advance_baseline(
    repo_root: Path,
    baseline_path: Path,
    rel_paths: list[str],
    cache_path: Path | None = None,
    ledger_path: Path | None = None,
    session_id: str | None = None,
) -> bool:
    """Fold a set of explicitly-attributed writes (Edit/Write/apply_patch) into
    the shared baseline + private cache AND append their provenance rows, all
    inside ONE locked transaction.

    The ledger append happens even before a baseline exists, so a pre-baseline
    explicit write is still attributed; the baseline/cache fold is a no-op until
    the first scan establishes a baseline. Coupling the append to the baseline
    lock is what prevents a concurrent read-only observer scan from overtaking
    this genuine writer for the same path. Only the requested paths are touched
    (a cheap pathspec ``git status``); every other entry is left as-is. Fails
    closed (returns False) when the lock cannot be acquired.
    """
    unique = [path for path in dict.fromkeys(rel_paths) if path]
    if not unique:
        return False
    try:
        with _baseline_lock(baseline_path):
            _append_ledger(ledger_path, session_id, unique)
            baseline = load_snapshot(baseline_path)
            if baseline is None:
                return False
            cheap_prior = load_cache(cache_path)
            scoped = _scoped_worktree_state(repo_root, unique, cheap_prior=cheap_prior)
            files = baseline.setdefault("files", {})
            cache_map = dict(cheap_prior)
            for path in unique:
                if path in scoped:
                    entry = scoped[path]
                    files[path] = _legacy_files({path: entry})[path]
                    stat = entry.get("stat")
                    if isinstance(stat, dict):
                        cache_map[path] = stat
                    else:
                        cache_map.pop(path, None)
                else:
                    files.pop(path, None)
                    cache_map.pop(path, None)
            _write_payload(baseline_path, baseline)
            write_cache(cache_path, cache_map)
            return True
    except BaselineLockError:
        return False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--repo-root", type=Path, required=True)
    snapshot_parser.add_argument("--output", type=Path, required=True)
    # --prior reads the private fingerprint cache for the re-hash short-circuit
    # so building a fresh baseline over a large clean tree does not re-hash it.
    # --cache (re)writes that cache from the same read. Both ignored if absent.
    snapshot_parser.add_argument("--prior", type=Path)
    snapshot_parser.add_argument("--cache", type=Path)

    living_snapshot_parser = subparsers.add_parser("living-snapshot")
    living_snapshot_parser.add_argument("--repo-root", type=Path, required=True)
    living_snapshot_parser.add_argument("--output", type=Path, required=True)

    collect_parser = subparsers.add_parser("collect")
    collect_parser.add_argument("--repo-root", type=Path, required=True)
    collect_parser.add_argument("--baseline", type=Path)
    collect_parser.add_argument("--cache", type=Path)
    collect_parser.add_argument("--activity-file", type=Path)
    collect_parser.add_argument("--start-ts", type=int)
    collect_parser.add_argument("--exclude", action="append", default=[])
    collect_parser.add_argument("--exclude-prefix", action="append", default=[])

    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("--repo-root", type=Path, required=True)
    scan_parser.add_argument("--baseline", type=Path, required=True)
    scan_parser.add_argument("--cache", type=Path)
    scan_parser.add_argument("--ledger", type=Path)
    scan_parser.add_argument("--session-id")
    scan_parser.add_argument("--exclude", action="append", default=[])
    scan_parser.add_argument("--exclude-prefix", action="append", default=[])

    advance_parser = subparsers.add_parser("advance-baseline")
    advance_parser.add_argument("--repo-root", type=Path, required=True)
    advance_parser.add_argument("--baseline", type=Path, required=True)
    advance_parser.add_argument("--cache", type=Path)
    advance_parser.add_argument("--ledger", type=Path)
    advance_parser.add_argument("--session-id")
    advance_parser.add_argument("--path", action="append", default=[])

    living_parser = subparsers.add_parser("living-changed")
    living_parser.add_argument("--repo-root", type=Path, required=True)
    living_parser.add_argument("--baseline", type=Path, required=True)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    repo_root = args.repo_root.resolve()
    if args.command == "snapshot":
        cheap_prior = load_cache(getattr(args, "prior", None))
        write_snapshot(
            repo_root,
            args.output,
            cheap_prior=cheap_prior,
            cache_path=getattr(args, "cache", None),
        )
        return 0
    if args.command == "living-snapshot":
        write_living_snapshot(repo_root, args.output)
        return 0

    if args.command == "living-changed":
        changed = living_changed(repo_root, load_snapshot(args.baseline))
        if changed is None:
            return 2
        return 0 if changed else 1

    if args.command == "scan":
        changes = cmd_scan(
            repo_root,
            args.baseline,
            excludes=set(args.exclude),
            exclude_prefixes=(".mycelium/", *args.exclude_prefix),
            cache_path=getattr(args, "cache", None),
            ledger_path=getattr(args, "ledger", None),
            session_id=getattr(args, "session_id", None),
        )
        for path in changes:
            print(path)
        return 0

    if args.command == "advance-baseline":
        advance_baseline(
            repo_root,
            args.baseline,
            list(args.path),
            cache_path=getattr(args, "cache", None),
            ledger_path=getattr(args, "ledger", None),
            session_id=getattr(args, "session_id", None),
        )
        return 0

    baseline = load_snapshot(args.baseline)
    cheap_prior = load_cache(getattr(args, "cache", None))
    changes = collect_changes(
        repo_root,
        baseline,
        activity_file=args.activity_file,
        start_ts=args.start_ts,
        excludes=set(args.exclude),
        exclude_prefixes=(".mycelium/", *args.exclude_prefix),
        cheap_prior=cheap_prior,
    )
    for path in changes:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
