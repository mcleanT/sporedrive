#!/usr/bin/env python3
"""wfctl v2 — snapshot / install / verify / rollback for the Codex→Claude workflow instruction set.

Maintained source lives in this repo under src/. Installed copies live under ~/.claude and
~/.codex. Every install first takes a snapshot of the installed targets, so any install can be
reverted with `wfctl.py rollback <label>`.

Ownership model (v2)
--------------------
wfctl OWNS exactly two things and restores only those:
  * the files/dirs in TARGETS
  * the settings *leaves* listed in src/claude/settings.fields.json
    (dotted leaf paths, e.g. "skillOverrides.offer-k-dense-web")
Everything else — SNAPSHOT_EXTRA hooks, INSPECTED_ONLY files, other settings keys,
~/.codex/config.toml fields — is EVIDENCE ONLY: hashes (and a scrubbed settings copy) are
recorded so drift is visible, but rollback never writes them back.

Safety properties (v2)
----------------------
  * install runs a preflight that mutates nothing; any failure exits non-zero with no snapshot.
  * install stages every change (".<name>.wfctl-staging", "settings.json.wfctl-staging"),
    hash-verifies the staged bytes, writes a transaction receipt, then commits each step with an
    atomic os.replace. A failure mid-commit triggers recovery from the verified pre-install
    snapshot and exits non-zero naming the receipt.
  * install/rollback refuse to clobber a target that diverged from every recorded wfctl
    postimage (--accept-drift / --force override; --force archives the diverged copy first).
  * snapshots are owner-only (0700 dirs / 0600 files), record copy_sha256 for every retained
    copy, and are re-hashed before any restore.
  * destination file modes are preserved across atomic replacement (settings.json included).

Usage:
  wfctl.py snapshot <label>                     take a snapshot of the installed targets
  wfctl.py install [--label L] [--accept-drift] snapshot, then copy src -> installed locations
  wfctl.py verify                               compare installed files/fields to src (exit 1 on drift)
  wfctl.py rollback <label> [--dry-run] [--force]  restore owned files + owned settings leaves
  wfctl.py list                                 list snapshots

Environment:
  WFCTL_HOME     root that stands in for ~ (default: Path.home())
  WFCTL_REPO     root of this repo        (default: the repo containing this script)
  WFCTL_FAIL_AT  fault injection for tests: raise RuntimeError immediately before the commit
                 rename of that step. Accepts a step id ("step-1" … "step-5", "settings") or a
                 1-based index ("3"). Step ids are printed by install before it commits.

Exit codes:
  0 ok | 1 drift (verify) or rollback conflict | 2 preflight failure | 3 install failed+recovered
  4 snapshot refused (token-like strings) | 5 snapshot/rollback integrity or privacy refusal
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat as statmod
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Declarative tables (paths are resolved against the configured HOME/REPO at
# call time, never at import time — see configure()).
# ---------------------------------------------------------------------------

# (kind, src relative to REPO, installed destination relative to HOME)
TARGETS = [
    ("file", "src/claude/CLAUDE.md", ".claude/CLAUDE.md"),
    (
        "file",
        "src/claude/skills/codex-review/SKILL.md",
        ".claude/skills/codex-review/SKILL.md",
    ),
    ("file", "src/claude/tools/codex_ask.sh", ".claude/tools/codex_ask.sh"),
    ("file", "src/claude/tools/codex_launch.py", ".claude/tools/codex_launch.py"),
    (
        "file",
        "src/claude/tools/sporedrive_review_guard.py",
        ".claude/tools/sporedrive_review_guard.py",
    ),
    (
        "dir",
        "src/claude/skills/prompt-optimizer",
        ".claude/skills/prompt-optimizer",
    ),
    (
        "dir",
        "src/claude/skills/run-pipeline-local",
        ".claude/skills/run-pipeline-local",
    ),
    ("file", "src/codex/AGENTS.md", ".codex/AGENTS.md"),
    ("dir", "src/codex/skills/cmux-driver", ".codex/skills/cmux-driver"),
]
# Fields merged into ~/.claude/settings.json. Its leaves are the owned settings leaves.
SETTINGS_FIELDS_SRC = "src/claude/settings.fields.json"
SETTINGS_REL = ".claude/settings.json"
CODEX_CONFIG_REL = ".codex/config.toml"

# Evidence only: snapshotted for context, never restored.
SNAPSHOT_EXTRA = [
    ".claude/skills/offer-k-dense-web/SKILL.md",
    ".claude/hooks/session-resume.sh",
    ".claude/hooks/session-save-reminder.sh",
    ".claude/hooks/cost-tracker.sh",
    ".claude/hooks/herdr-agent-state.sh",
]
# Hash-only records for files inspected by the audit but deliberately left unchanged.
# NOTE: .claude/skills/prompt-optimizer/SKILL.md moved OUT of this list (WFI-FINISH
# revision-2) — it and run-pipeline-local are now owned "dir" TARGETS above, installed
# from src/claude/skills/{prompt-optimizer,run-pipeline-local}/ via the transactional
# installer rather than left as a hash-only drift record.
INSPECTED_ONLY = [
    "Desktop/Projects/Science/.claude/hooks/pre-compact-save.sh",
    ".claude/plugins/marketplaces/hummer98-using-cmux/skills/using-cmux/SKILL.md",
    ".codex/hooks.json",
]
SETTINGS_SNAPSHOT_KEYS = [
    "model",
    "effortLevel",
    "modelSettings",
    "skillOverrides",
    "enabledPlugins",
    "extraKnownMarketplaces",
    "hooks",
    "workflowSizeGuideline",
    "ultracode",
    "enableWorkflows",
    "skillListingBudgetFraction",
    "permissions",
    "statusLine",
]
CODEX_CONFIG_KEYS = (
    "model",
    "model_reasoning_effort",
    "approvals_reviewer",
    "service_tier",
)

SECRET_KEY_RE = re.compile(r"key|token|secret|password|credential", re.I)
SECRET_VALUE_RE = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{16,}|xox[abp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{12,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY|Bearer [A-Za-z0-9._-]{20,}"
)
REDACTED = "<redacted>"
DIR_MODE = 0o700
FILE_MODE = 0o600
IGNORE = ("__pycache__", ".DS_Store")

EXIT_OK, EXIT_DRIFT, EXIT_PREFLIGHT, EXIT_INSTALL_FAILED = 0, 1, 2, 3
EXIT_SECRET, EXIT_INTEGRITY = 4, 5


class Config:
    def __init__(self, home: Path, repo: Path):
        self.home = home
        self.repo = repo
        self.snapshots = repo / "snapshots"
        self.installed = repo / "installed"
        self.receipts = repo / "receipts"
        self.history = self.installed / "history.jsonl"
        self.installed_manifest = self.installed / "manifest.json"
        self.settings_path = home / SETTINGS_REL
        self.codex_config = home / CODEX_CONFIG_REL
        self.settings_fields_src = repo / SETTINGS_FIELDS_SRC
        self.targets = [(k, s, home / d) for k, s, d in TARGETS]
        self.snapshot_extra = [home / p for p in SNAPSHOT_EXTRA]
        self.inspected_only = [home / p for p in INSPECTED_ONLY]
        self.default_home = "WFCTL_HOME" not in os.environ
        self.default_repo = "WFCTL_REPO" not in os.environ


_CFG: Config | None = None


def configure(home: str | Path | None = None, repo: str | Path | None = None) -> Config:
    """(Re)compute every path from WFCTL_HOME / WFCTL_REPO (or explicit args)."""
    global _CFG
    h = home if home is not None else os.environ.get("WFCTL_HOME") or Path.home()
    r = (
        repo
        if repo is not None
        else os.environ.get("WFCTL_REPO") or Path(__file__).resolve().parent.parent
    )
    _CFG = Config(Path(h).expanduser().resolve(), Path(r).expanduser().resolve())
    return _CFG


def cfg() -> Config:
    return _CFG if _CFG is not None else configure()


# ---------------------------------------------------------------------------
# hashing / small helpers
# ---------------------------------------------------------------------------


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_files(p: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for x in sorted(p.rglob("*")):
        if not x.is_file():
            continue
        rel = x.relative_to(p)
        if any(part in IGNORE for part in rel.parts):
            continue
        out[str(rel)] = sha256(x)
    return out


def path_hash(kind: str, p: Path) -> Any:
    """File -> hex digest, dir -> {relpath: digest}, missing -> None."""
    if kind == "dir":
        return dir_files(p) if p.is_dir() else None
    return sha256(p) if p.is_file() else None


def mode_of(p: Path) -> int | None:
    try:
        return statmod.S_IMODE(p.stat().st_mode)
    except OSError:
        return None


def now_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(cfg().repo), "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "(no commit yet)"


def scrub(o):
    if isinstance(o, dict):
        return {
            k: (REDACTED if SECRET_KEY_RE.search(k) else scrub(v)) for k, v in o.items()
        }
    if isinstance(o, list):
        return [scrub(x) for x in o]
    return o


def contains_redacted(o) -> bool:
    if isinstance(o, str):
        return o == REDACTED
    if isinstance(o, dict):
        return any(contains_redacted(k) or contains_redacted(v) for k, v in o.items())
    if isinstance(o, list):
        return any(contains_redacted(x) for x in o)
    return False


def rel_home(p: Path) -> str:
    try:
        return str(p.relative_to(cfg().home))
    except ValueError:
        return "abs" + str(p)


def secure_dir(p: Path) -> Path:
    """mkdir -p, then 0700 the created dir (and every level below snapshots/)."""
    p.mkdir(parents=True, exist_ok=True)
    root = cfg().snapshots
    chain = [p]
    for anc in p.parents:
        if anc == root or root not in anc.parents:
            break
        chain.append(anc)
    for d in chain:
        try:
            os.chmod(d, DIR_MODE)
        except OSError:
            pass
    return p


def harden(root: Path) -> None:
    """0700 every dir and 0600 every file under root (root included), explicitly."""
    if root.is_dir():
        os.chmod(root, DIR_MODE)
        for x in root.rglob("*"):
            os.chmod(x, DIR_MODE if x.is_dir() else FILE_MODE)
    elif root.exists():
        os.chmod(root, FILE_MODE)


def write_json(p: Path, obj, mode: int | None = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, sort_keys=False) + "\n")
    if mode is not None:
        os.chmod(p, mode)


# ---------------------------------------------------------------------------
# owned settings leaves
# ---------------------------------------------------------------------------


def leaf_paths(
    obj: dict, prefix: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], Any]]:
    out = []
    for k, v in obj.items():
        if isinstance(v, dict) and v:
            out.extend(leaf_paths(v, prefix + (k,)))
        else:
            out.append((prefix + (k,), v))
    return out


def wanted_leaves() -> list[tuple[tuple[str, ...], Any]]:
    return leaf_paths(json.loads(cfg().settings_fields_src.read_text()))


def dotted(segs) -> str:
    return ".".join(segs)


def get_leaf(doc: dict, segs) -> dict:
    cur: Any = doc
    for s in segs:
        if not isinstance(cur, dict) or s not in cur:
            return {"present": False}
        cur = cur[s]
    return {"present": True, "value": cur}


def set_leaf(doc: dict, segs, value) -> bool:
    """Set a leaf; returns True if a parent dict had to be created."""
    created = False
    cur = doc
    for s in segs[:-1]:
        if s not in cur or not isinstance(cur[s], dict):
            cur[s] = {}
            created = True
        cur = cur[s]
    cur[segs[-1]] = value
    return created


def del_leaf(doc: dict, segs, delete_parent: bool) -> None:
    chain = [doc]
    cur = doc
    for s in segs[:-1]:
        if not isinstance(cur, dict) or s not in cur or not isinstance(cur[s], dict):
            return
        cur = cur[s]
        chain.append(cur)
    if isinstance(cur, dict):
        cur.pop(segs[-1], None)
    if delete_parent and len(segs) > 1 and isinstance(cur, dict) and not cur:
        chain[-2].pop(segs[-2], None)


def load_settings() -> dict:
    return json.loads(cfg().settings_path.read_text())


def current_leaves() -> dict[str, dict]:
    doc = load_settings() if cfg().settings_path.is_file() else {}
    return {dotted(segs): get_leaf(doc, segs) for segs, _ in wanted_leaves()}


# ---------------------------------------------------------------------------
# postimage bookkeeping (what wfctl actually wrote)
# ---------------------------------------------------------------------------


def build_postimage(label: str) -> dict:
    c = cfg()
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "repo_head": git_head(),
        "targets": {
            str(dest): {"kind": kind, "hash": path_hash(kind, dest)}
            for kind, _, dest in c.targets
        },
        "leaves": current_leaves(),
    }


def recorded_postimages(label: str | None = None) -> list[dict]:
    c = cfg()
    out: list[dict] = []
    if label:
        p = c.snapshots / label / "postimage.json"
        if p.is_file():
            try:
                out.append(json.loads(p.read_text()))
            except Exception:
                pass
    if c.history.is_file():
        for line in c.history.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    if c.installed_manifest.is_file():
        try:
            m = json.loads(c.installed_manifest.read_text())
            if isinstance(m.get("postimage"), dict):
                out.append(m["postimage"])
        except Exception:
            pass
    return out


def postimage_hashes(posts: list[dict], dest: str) -> list[Any]:
    return [
        p["targets"][dest].get("hash")
        for p in posts
        if isinstance(p.get("targets"), dict) and dest in p["targets"]
    ]


def postimage_leaves(posts: list[dict], key: str) -> list[dict]:
    return [
        p["leaves"][key]
        for p in posts
        if isinstance(p.get("leaves"), dict) and key in p["leaves"]
    ]


def leaf_equal(a: dict, b: dict) -> bool:
    if not a.get("present") and not b.get("present"):
        return True
    return bool(a.get("present")) == bool(b.get("present")) and a.get("value") == b.get(
        "value"
    )


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def codex_config_fields() -> str:
    p = cfg().codex_config
    if not p.exists():
        return "# (config.toml absent)\n"
    out = []
    for line in p.read_text().splitlines():
        s = line.strip()
        if s.startswith("["):
            break  # only the top-level table is relevant; nothing below it is copied
        if any(
            s.startswith(k + " ") or s.startswith(k + "=") for k in CODEX_CONFIG_KEYS
        ):
            out.append(line)
    return "\n".join(out) + "\n"


def secret_scan(root: Path) -> list[str]:
    hits = []
    paths = [root] if root.is_file() else list(root.rglob("*"))
    for p in paths:
        if p.is_file():
            try:
                txt = p.read_text(errors="ignore")
            except Exception:
                continue
            if SECRET_VALUE_RE.search(txt):
                hits.append(str(p))
    return hits


def _copy_entry(path: Path, kind: str, dest_root: Path, owned: bool) -> dict:
    """Record + retain a copy of one snapshot entry."""
    rec: dict[str, Any] = {
        "path": str(path),
        "home_rel": rel_home(path),
        "kind": kind,
        "exists": path.exists(),
        "owned": owned,
    }
    if not rec["exists"]:
        rec["sha256"] = None
        return rec
    copy_to = dest_root / "files" / rel_home(path)
    copy_to.parent.mkdir(parents=True, exist_ok=True)
    if kind == "dir":
        shutil.copytree(path, copy_to, ignore=shutil.ignore_patterns(*IGNORE))
        rec["files"] = dir_files(path)
        rec["sha256"] = rec["files"]
        rec["copy_sha256"] = dir_files(copy_to)
        rec["file_modes"] = {k: mode_of(path / k) for k in rec["files"]}
    else:
        shutil.copy2(path, copy_to)
        rec["sha256"] = sha256(path)
        rec["copy_sha256"] = sha256(copy_to)
        rec["size"] = path.stat().st_size
    rec["mode"] = mode_of(path)
    rec["snapshot_copy"] = str(copy_to.relative_to(dest_root))
    return rec


def snapshot(label: str) -> Path:
    c = cfg()
    dest = c.snapshots / label
    if dest.exists():
        sys.exit(f"snapshot {label} already exists: {dest}")
    secure_dir(dest)
    manifest: dict[str, Any] = {
        "format_version": 2,
        "label": label,
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "repo_head": git_head(),
        "host": os.uname().nodename,
        "home": str(c.home),
        "owned": [],
        "evidence": [],
        "inspected_only": [],
    }
    for kind, _, path in c.targets:
        manifest["owned"].append(_copy_entry(path, kind, dest, owned=True))
    for path in c.snapshot_extra:
        manifest["evidence"].append(_copy_entry(path, "file", dest, owned=False))
    for path in c.inspected_only:
        rec = {
            "path": str(path),
            "home_rel": rel_home(path),
            "exists": path.exists(),
            "sha256": sha256(path) if path.is_file() else None,
        }
        manifest["inspected_only"].append(rec)

    owned_leaves: dict[str, dict] = {}
    if c.settings_path.is_file():
        settings = json.loads(c.settings_path.read_text())
        write_json(dest / "settings.full.scrubbed.json", scrub(settings))
        fields = {k: settings[k] for k in SETTINGS_SNAPSHOT_KEYS if k in settings}
        write_json(dest / "settings.fields.json", scrub(fields))
        manifest["settings_sha256"] = sha256(c.settings_path)
        manifest["settings_mode"] = mode_of(c.settings_path)
        for segs, _ in wanted_leaves():
            rec = get_leaf(settings, segs)
            rec["segments"] = list(segs)
            rec["parent_created"] = False
            owned_leaves[dotted(segs)] = rec
    else:
        for segs, _ in wanted_leaves():
            owned_leaves[dotted(segs)] = {
                "present": False,
                "segments": list(segs),
                "parent_created": False,
            }
    # owned leaves are stored UNSCRUBBED (preflight guarantees no secret-like key here)
    write_json(dest / "owned-leaves.json", owned_leaves)
    manifest["owned_leaves_file"] = "owned-leaves.json"

    (dest / "codex-config.fields.toml").write_text(codex_config_fields())
    if c.codex_config.exists():
        manifest["codex_config_sha256"] = sha256(c.codex_config)
    write_json(dest / "manifest.json", manifest)
    harden(dest)

    receipt = sanitized_receipt(manifest, owned_leaves)
    receipt_path = c.receipts / f"{label}.json"
    write_json(receipt_path, receipt)

    hits = secret_scan(dest) + secret_scan(receipt_path)
    if hits:
        shutil.rmtree(dest)
        receipt_path.unlink(missing_ok=True)
        print(
            "ABORTED: token-like strings found in snapshot/receipt; nothing kept: "
            + ", ".join(hits),
            file=sys.stderr,
        )
        sys.exit(EXIT_SECRET)
    print(f"snapshot written: {dest}")
    print(f"receipt written: {receipt_path}")
    return dest


def sanitized_receipt(manifest: dict, owned_leaves: dict) -> dict:
    """Hash-only receipt intended for version control: no file contents anywhere."""

    def strip(rec: dict) -> dict:
        return {
            k: rec.get(k)
            for k in ("home_rel", "kind", "exists", "sha256", "copy_sha256", "mode")
            if k in rec
        }

    return {
        "format_version": 2,
        "label": manifest["label"],
        "taken_at": manifest["taken_at"],
        "repo_head": manifest["repo_head"],
        "host": manifest["host"],
        "owned": [strip(r) for r in manifest["owned"]],
        "evidence": [strip(r) for r in manifest["evidence"]],
        "inspected_only": [
            {"home_rel": r["home_rel"], "exists": r["exists"], "sha256": r["sha256"]}
            for r in manifest["inspected_only"]
        ],
        "settings_sha256": manifest.get("settings_sha256"),
        "settings_mode": manifest.get("settings_mode"),
        "codex_config_sha256": manifest.get("codex_config_sha256"),
        # leaf values are non-secret by construction (preflight refuses secret-like keys)
        "owned_leaves": {
            k: {"present": v.get("present"), "value": v.get("value")}
            for k, v in owned_leaves.items()
        },
    }


def verify_snapshot_copies(snap: Path, entries: list[dict]) -> None:
    """Re-hash every retained copy; refuse on mismatch."""
    for rec in entries:
        if not rec.get("exists") or "snapshot_copy" not in rec:
            continue
        copy = snap / rec["snapshot_copy"]
        if not copy.exists():
            sys.exit(f"snapshot bytes altered: missing copy {copy}")
        expect = rec.get("copy_sha256", rec.get("sha256"))
        actual = dir_files(copy) if rec.get("kind") == "dir" else sha256(copy)
        if expect is not None and actual != expect:
            sys.exit(f"snapshot bytes altered: {copy}")


# ---------------------------------------------------------------------------
# atomic replacement helpers (mode-preserving)
# ---------------------------------------------------------------------------


def staging_for(dest: Path, settings_style: bool = False) -> Path:
    if settings_style:
        return dest.with_name(dest.name + ".wfctl-staging")
    return dest.with_name("." + dest.name + ".wfctl-staging")


def commit_file(staging: Path, dest: Path, mode: int | None) -> None:
    if mode is not None:
        os.chmod(staging, mode)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, dest)


def commit_dir(staging: Path, dest: Path, mode: int | None) -> None:
    old = dest.with_name("." + dest.name + ".wfctl-old")
    if old.exists():
        shutil.rmtree(old)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        os.replace(dest, old)
    if mode is not None:
        os.chmod(staging, mode)
    os.replace(staging, dest)
    if old.exists():
        shutil.rmtree(old)


def cleanup_staging(paths: list[Path]) -> None:
    for p in paths:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            try:
                p.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def preflight(label: str, accept_drift: bool) -> list[str]:
    c = cfg()
    errs: list[str] = []

    for kind, src_rel, _dest in c.targets:
        src = c.repo / src_rel
        if kind == "dir" and not src.is_dir():
            errs.append(f"missing source dir: {src}")
        elif kind == "file" and not src.is_file():
            errs.append(f"missing source file: {src}")
    if not c.settings_fields_src.is_file():
        errs.append(f"missing source file: {c.settings_fields_src}")

    for dest in [d for _, _, d in c.targets] + [c.settings_path]:
        anc = dest.parent
        while not anc.exists() and anc != anc.parent:
            anc = anc.parent
        if not anc.is_dir():
            errs.append(f"destination parent is not a directory: {anc}")
        elif not os.access(anc, os.W_OK | os.X_OK):
            errs.append(f"destination parent not writable: {anc}")

    settings: dict | None = None
    if not c.settings_path.is_file():
        errs.append(f"settings.json missing: {c.settings_path}")
    else:
        try:
            loaded = json.loads(c.settings_path.read_text())
        except Exception as e:
            errs.append(f"settings.json does not parse: {c.settings_path}: {e}")
        else:
            if not isinstance(loaded, dict):
                errs.append(f"settings.json is not a JSON object: {c.settings_path}")
            else:
                settings = loaded

    leaves: list[tuple[tuple[str, ...], Any]] = []
    if c.settings_fields_src.is_file():
        try:
            leaves = wanted_leaves()
        except Exception as e:
            errs.append(f"settings.fields.json does not parse: {e}")
    for segs, _ in leaves:
        for s in segs:
            if SECRET_KEY_RE.search(s):
                errs.append(
                    f"owned settings leaf key looks secret-like: {dotted(segs)}"
                )
        if settings is not None:
            cur: Any = settings
            for s in segs[:-1]:
                if not isinstance(cur, dict):
                    break
                if s not in cur:
                    break
                cur = cur[s]
                if not isinstance(cur, dict):
                    errs.append(
                        f"owned settings leaf parent is not a dict: {dotted(segs)} (at {s})"
                    )
                    break

    if (c.snapshots / label).exists():
        errs.append(f"snapshot label directory already exists: {c.snapshots / label}")

    seen_parents: set[Path] = set()
    for dest in [d for _, _, d in c.targets] + [c.settings_path]:
        parent = dest.parent
        if parent in seen_parents or not parent.is_dir():
            continue
        seen_parents.add(parent)
        for pat in ("*.wfctl-staging", "*.wfctl-tmp"):
            for stale in sorted(parent.glob(pat)):
                errs.append(f"stale staging artifact next to destination: {stale}")

    if not accept_drift:
        posts = recorded_postimages()
        for kind, _src_rel, dest in c.targets:
            recorded = postimage_hashes(posts, str(dest))
            if not recorded:
                continue  # never installed by wfctl -> not a conflict
            cur_hash = path_hash(kind, dest)
            if cur_hash is None:
                continue  # absent destination: nothing of the user's to clobber
            if not any(cur_hash == r for r in recorded):
                errs.append(
                    f"destination diverged from every recorded wfctl postimage: {dest} "
                    "(pass --accept-drift to install over it; the pre-install snapshot keeps your edit)"
                )
        if settings is not None:
            for segs, _ in leaves:
                key = dotted(segs)
                recorded_l = postimage_leaves(posts, key)
                if not recorded_l:
                    continue
                cur_leaf = get_leaf(settings, segs)
                if not any(leaf_equal(cur_leaf, r) for r in recorded_l):
                    errs.append(
                        f"settings leaf diverged from every recorded wfctl postimage: {key} "
                        "(pass --accept-drift)"
                    )
    return errs


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def build_steps() -> list[dict]:
    c = cfg()
    steps: list[dict] = []
    for i, (kind, src_rel, dest) in enumerate(c.targets, 1):
        src = c.repo / src_rel
        steps.append(
            {
                "id": f"step-{i}",
                "index": i,
                "kind": kind,
                "src": str(src),
                "src_rel": src_rel,
                "dest": str(dest),
                "staging": str(staging_for(dest)),
                "pre_hash": path_hash(kind, dest),
                "post_hash": path_hash(kind, src),
                "dest_mode": mode_of(dest),
                "src_mode": mode_of(src),
                "status": "planned",
            }
        )
    steps.append(
        {
            "id": "settings",
            "index": len(steps) + 1,
            "kind": "settings",
            "src": str(c.settings_fields_src),
            "src_rel": SETTINGS_FIELDS_SRC,
            "dest": str(c.settings_path),
            "staging": str(staging_for(c.settings_path, settings_style=True)),
            "pre_hash": path_hash("file", c.settings_path),
            "post_hash": None,
            "dest_mode": mode_of(c.settings_path),
            "src_mode": mode_of(c.settings_fields_src),
            "status": "planned",
        }
    )
    return steps


def merged_settings_doc() -> tuple[dict, dict, bool]:
    """Return (new document, changed leaves, parent_created)."""
    settings = load_settings()
    changed: dict[str, dict] = {}
    parent_created = False
    for segs, value in wanted_leaves():
        before = get_leaf(settings, segs)
        if before.get("present") and before.get("value") == value:
            continue
        created = set_leaf(settings, segs, value)
        parent_created = parent_created or created
        changed[dotted(segs)] = {
            "from": before.get("value") if before.get("present") else None,
            "from_present": bool(before.get("present")),
            "to": value,
        }
    return settings, changed, parent_created


def stage_all(steps: list[dict]) -> tuple[dict, bool]:
    changed: dict[str, dict] = {}
    parent_created = False
    for st in steps:
        staging = Path(st["staging"])
        if staging.exists():
            cleanup_staging([staging])
        if st["kind"] == "dir":
            shutil.copytree(
                Path(st["src"]), staging, ignore=shutil.ignore_patterns(*IGNORE)
            )
            if dir_files(staging) != st["post_hash"]:
                raise RuntimeError(f"staged bytes do not match source for {st['id']}")
        elif st["kind"] == "file":
            shutil.copy2(Path(st["src"]), staging)
            if sha256(staging) != st["post_hash"]:
                raise RuntimeError(f"staged bytes do not match source for {st['id']}")
        else:
            doc, changed, parent_created = merged_settings_doc()
            staging.write_text(json.dumps(doc, indent=2) + "\n")
            st["post_hash"] = sha256(staging)
            st["changed_leaves"] = changed
            st["parent_created"] = parent_created
        st["status"] = "staged"
    return changed, parent_created


def commit_step(st: dict) -> None:
    staging, dest = Path(st["staging"]), Path(st["dest"])
    mode = st["dest_mode"] if st["dest_mode"] is not None else st["src_mode"]
    if st["kind"] == "dir":
        commit_dir(staging, dest, mode)
    else:
        commit_file(staging, dest, mode)


def restore_step(st: dict, snap: Path, manifest: dict, owned_leaves: dict) -> bool:
    """Restore one committed step from the verified pre-install snapshot."""
    try:
        if st["kind"] == "settings":
            if not Path(st["dest"]).is_file():
                return False
            doc = json.loads(Path(st["dest"]).read_text())
            restore_owned_leaves(doc, owned_leaves)
            staging = staging_for(Path(st["dest"]), settings_style=True)
            staging.write_text(json.dumps(doc, indent=2) + "\n")
            commit_file(staging, Path(st["dest"]), st["dest_mode"] or FILE_MODE)
            return True
        rec = next(
            (r for r in manifest.get("owned", []) if r["path"] == st["dest"]), None
        )
        dest = Path(st["dest"])
        if rec is None or not rec.get("exists"):
            if dest.is_dir():
                shutil.rmtree(dest)
            elif dest.exists():
                dest.unlink()
            return True
        return restore_owned_entry(snap, rec, dest)
    except Exception:
        return False


def restore_owned_entry(snap: Path, rec: dict, dest: Path) -> bool:
    copy = snap / rec["snapshot_copy"]
    mode = mode_of(dest)
    if mode is None:
        mode = rec.get("mode")
    if rec.get("kind") == "dir":
        staging = staging_for(dest)
        cleanup_staging([staging])
        shutil.copytree(copy, staging)
        for rel, m in (rec.get("file_modes") or {}).items():
            f = staging / rel
            if m is not None and f.is_file():
                os.chmod(f, m)
        commit_dir(staging, dest, mode if mode is not None else mode_of(copy))
    else:
        staging = staging_for(dest)
        cleanup_staging([staging])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(copy, staging)
        commit_file(staging, dest, mode if mode is not None else mode_of(copy))
    return True


def restore_owned_leaves(doc: dict, owned_leaves: dict) -> list[str]:
    touched = []
    for key, rec in owned_leaves.items():
        segs = tuple(rec.get("segments") or key.split("."))
        if rec.get("present"):
            set_leaf(doc, segs, rec.get("value"))
        else:
            del_leaf(doc, segs, delete_parent=bool(rec.get("parent_created")))
        touched.append(key)
    return touched


def install(label: str | None, accept_drift: bool) -> int:
    c = cfg()
    label = label or f"pre-install-{now_label()}"
    errs = preflight(label, accept_drift)
    if errs:
        for e in errs:
            print(f"preflight: {e}", file=sys.stderr)
        print(
            f"preflight: {len(errs)} problem(s); nothing was changed", file=sys.stderr
        )
        return EXIT_PREFLIGHT

    snap = snapshot(label)
    manifest = json.loads((snap / "manifest.json").read_text())
    verify_snapshot_copies(snap, manifest.get("owned", []))
    owned_leaves = json.loads((snap / "owned-leaves.json").read_text())

    steps = build_steps()
    staging_paths = [Path(s["staging"]) for s in steps]
    try:
        changed, parent_created = stage_all(steps)
    except Exception as e:
        cleanup_staging(staging_paths)
        print(f"staging failed, nothing committed: {e}", file=sys.stderr)
        return EXIT_INSTALL_FAILED

    # parent_created is only knowable once the merge is computed
    for rec in owned_leaves.values():
        if not rec.get("present"):
            rec["parent_created"] = parent_created
    write_json(snap / "owned-leaves.json", owned_leaves, mode=FILE_MODE)

    c.installed.mkdir(parents=True, exist_ok=True)
    receipt_path = c.installed / f"transaction-{label}.json"
    receipt = {
        "label": label,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "repo_head": git_head(),
        "home": str(c.home),
        "pre_install_snapshot": str(snap),
        "accept_drift": accept_drift,
        "status": "staged",
        "steps": steps,
    }
    write_json(receipt_path, receipt)

    print("commit plan: " + ", ".join(f"{s['id']}({s['kind']})" for s in steps))
    fail_at = os.environ.get("WFCTL_FAIL_AT")
    committed: list[dict] = []
    try:
        for st in steps:
            if fail_at and fail_at in (st["id"], str(st["index"])):
                raise RuntimeError(
                    f"WFCTL_FAIL_AT={fail_at}: injected failure before commit of {st['id']}"
                )
            commit_step(st)
            st["status"] = "done"
            committed.append(st)
            write_json(receipt_path, receipt)
    except Exception as e:
        receipt["status"] = "failed"
        receipt["error"] = str(e)
        receipt["failed_at"] = datetime.now(timezone.utc).isoformat()
        for st in reversed(committed):
            st["recovered"] = restore_step(st, snap, manifest, owned_leaves)
            st["status"] = "rolled-back" if st["recovered"] else "recovery-failed"
        for st in steps:
            if st["status"] == "staged":
                st["status"] = "not-run"
        cleanup_staging(staging_paths)
        receipt["recovered"] = all(st.get("recovered", True) for st in steps)
        write_json(receipt_path, receipt)
        print(f"install FAILED: {e}", file=sys.stderr)
        print(f"recovery receipt: {receipt_path}", file=sys.stderr)
        return EXIT_INSTALL_FAILED

    post = build_postimage(label)
    write_json(snap / "postimage.json", post, mode=FILE_MODE)
    with open(c.history, "a") as fh:
        fh.write(json.dumps(post) + "\n")
    installed_manifest = {
        "installed_at": post["ts"],
        "repo_head": post["repo_head"],
        "pre_install_snapshot": str(snap),
        "entries": [
            {
                "kind": s["kind"],
                "src": s["src_rel"],
                "dest": s["dest"],
                "src_sha256": s["post_hash"],
                "dest_sha256": path_hash(
                    "dir" if s["kind"] == "dir" else "file", Path(s["dest"])
                ),
            }
            for s in steps
        ],
        "settings_fields_changed": changed,
        "postimage": post,
    }
    write_json(c.installed_manifest, installed_manifest)
    receipt["status"] = "committed"
    receipt["finished_at"] = post["ts"]
    write_json(receipt_path, receipt)
    print(
        f"installed {len(steps) - 1} targets; settings leaves changed: {changed or 'none'}"
    )
    print(f"pre-install snapshot: {snap}")
    print(f"transaction receipt: {receipt_path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def verify() -> int:
    c = cfg()
    drift = 0
    for kind, src_rel, dest in c.targets:
        src = c.repo / src_rel
        s, d = path_hash(kind, src), path_hash(kind, dest)
        ok = d is not None and s is not None and s == d
        print(f"{'OK   ' if ok else 'DRIFT'} {dest}  <-  {src_rel}")
        drift += 0 if ok else 1
    want = json.loads(c.settings_fields_src.read_text())
    settings = load_settings() if c.settings_path.is_file() else {}
    bad = [
        dotted(segs)
        for segs, v in wanted_leaves()
        if not leaf_equal(get_leaf(settings, segs), {"present": True, "value": v})
    ]
    print(f"{'OK   ' if not bad else 'DRIFT'} {c.settings_path} fields {want}")
    drift += 1 if bad else 0
    print("verify:", "clean" if drift == 0 else f"{drift} drifted target(s)")
    return EXIT_DRIFT if drift else EXIT_OK


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


def load_snapshot_plan(snap: Path) -> tuple[dict, list[dict], list[dict], dict]:
    """Return (manifest, owned entries, evidence entries, owned leaves)."""
    c = cfg()
    manifest = json.loads((snap / "manifest.json").read_text())
    if manifest.get("format_version", 1) >= 2:
        owned = manifest.get("owned", [])
        evidence = manifest.get("evidence", [])
        leaves_file = snap / manifest.get("owned_leaves_file", "owned-leaves.json")
        owned_leaves = (
            json.loads(leaves_file.read_text()) if leaves_file.is_file() else {}
        )
        return manifest, owned, evidence, owned_leaves

    # --- old format: entries[] + scrubbed settings.fields.json ---
    owned_dests = {str(d): k for k, _, d in c.targets}
    owned, evidence = [], []
    for rec in manifest.get("entries", []):
        rec = dict(rec)
        rec.setdefault("home_rel", rel_home(Path(rec["path"])))
        if rec["path"] in owned_dests:
            rec["owned"] = True
            rec.setdefault("kind", owned_dests[rec["path"]])
            # old manifests carry only the ORIGINAL hash; verify the copy against it
            if rec.get("kind") == "dir":
                rec["copy_sha256"] = rec.get("files")
                rec["sha256"] = rec.get("files")
            else:
                rec["copy_sha256"] = rec.get("sha256")
            owned.append(rec)
        else:
            rec["owned"] = False
            evidence.append(rec)
    owned_leaves: dict[str, dict] = {}
    fields_file = snap / "settings.fields.json"
    fields = json.loads(fields_file.read_text()) if fields_file.is_file() else {}
    for segs, _ in wanted_leaves():
        rec = get_leaf(fields, segs)
        rec["segments"] = list(segs)
        rec["parent_created"] = False
        owned_leaves[dotted(segs)] = rec
    return manifest, owned, evidence, owned_leaves


def rollback(label: str, dry: bool, force: bool) -> int:
    c = cfg()
    snap = c.snapshots / label
    if not (snap / "manifest.json").is_file():
        sys.exit(f"no such snapshot: {snap}")
    manifest, owned, evidence, owned_leaves = load_snapshot_plan(snap)

    # privacy: never write a scrubbed placeholder back into the user's config
    for key, rec in owned_leaves.items():
        if rec.get("present") and contains_redacted(rec.get("value")):
            print(
                f"REFUSING: snapshot leaf {key} holds the scrubbed placeholder {REDACTED!r}; "
                "the real value was never retained",
                file=sys.stderr,
            )
            return EXIT_INTEGRITY

    verify_snapshot_copies(snap, owned)

    posts = recorded_postimages(label)
    conflicts: list[dict] = []
    for rec in owned:
        dest = Path(rec["path"])
        kind = rec.get("kind", "file")
        recorded = postimage_hashes(posts, str(dest))
        cur = path_hash(kind, dest)
        if not recorded:
            conflicts.append(
                {
                    "target": str(dest),
                    "kind": kind,
                    "reason": "no wfctl postimage on record (unverifiable)",
                }
            )
        elif not any(cur == r for r in recorded):
            conflicts.append(
                {
                    "target": str(dest),
                    "kind": kind,
                    "reason": "installed copy diverged from every recorded wfctl postimage",
                }
            )
    live = load_settings() if c.settings_path.is_file() else {}
    for key, rec in owned_leaves.items():
        segs = tuple(rec.get("segments") or key.split("."))
        recorded_l = postimage_leaves(posts, key)
        cur_leaf = get_leaf(live, segs)
        if not recorded_l:
            conflicts.append(
                {
                    "target": f"settings:{key}",
                    "kind": "leaf",
                    "reason": "no wfctl postimage on record (unverifiable)",
                }
            )
        elif not any(leaf_equal(cur_leaf, r) for r in recorded_l):
            conflicts.append(
                {
                    "target": f"settings:{key}",
                    "kind": "leaf",
                    "reason": "leaf diverged from every recorded wfctl postimage",
                }
            )

    for rec in evidence:
        print(f"evidence-only (not restored)  {rec['path']}")

    if conflicts and not force:
        for cft in conflicts:
            print(f"CONFLICT {cft['target']}: {cft['reason']}", file=sys.stderr)
        print(
            f"rollback refused: {len(conflicts)} conflict(s); nothing changed "
            "(re-run with --force to archive the diverged copies and restore)",
            file=sys.stderr,
        )
        return EXIT_DRIFT

    restored: list[dict] = []
    archived: list[str] = []
    if conflicts and force and not dry:
        arch = secure_dir(snap / "conflicts" / now_label())
        for cft in conflicts:
            if cft["kind"] == "leaf":
                continue
            dest = Path(cft["target"])
            if not dest.exists():
                continue
            to = arch / "files" / rel_home(dest)
            to.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_dir():
                shutil.copytree(dest, to)
            else:
                shutil.copy2(dest, to)
            archived.append(str(to))
        leaf_conflicts = {
            cft["target"].split(":", 1)[1]: get_leaf(
                live,
                tuple(owned_leaves[cft["target"].split(":", 1)[1]].get("segments")),
            )
            for cft in conflicts
            if cft["kind"] == "leaf"
        }
        if leaf_conflicts:
            write_json(arch / "settings-leaves.json", leaf_conflicts)
            archived.append(str(arch / "settings-leaves.json"))
        harden(arch)
        print(f"archived diverged copies: {arch}")

    for rec in owned:
        dest = Path(rec["path"])
        if rec.get("exists"):
            print(f"restore  {dest}  <-  {snap / rec['snapshot_copy']}")
            if not dry:
                restore_owned_entry(snap, rec, dest)
            restored.append({"target": str(dest), "kind": rec.get("kind", "file")})
        else:
            if dest.exists():
                print(f"remove   {dest}  (absent in snapshot)")
                if not dry:
                    shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
                restored.append({"target": str(dest), "kind": "removed"})
            else:
                print(f"absent   {dest}  (unchanged)")

    leaf_actions = []
    if c.settings_path.is_file() and owned_leaves:
        doc = load_settings()  # fresh load: unrelated keys survive untouched
        for key, rec in owned_leaves.items():
            segs = tuple(rec.get("segments") or key.split("."))
            if rec.get("present"):
                print(f"settings {key}: restore snapshot value")
                leaf_actions.append(
                    {"leaf": key, "action": "set", "value": rec.get("value")}
                )
            else:
                print(f"settings {key}: delete (absent in snapshot)")
                leaf_actions.append(
                    {
                        "leaf": key,
                        "action": "delete",
                        "parent_created": bool(rec.get("parent_created")),
                    }
                )
        if not dry:
            restore_owned_leaves(doc, owned_leaves)
            if contains_redacted(doc):
                print(
                    f"REFUSING: refusing to write {REDACTED!r} into settings.json",
                    file=sys.stderr,
                )
                return EXIT_INTEGRITY
            staging = staging_for(c.settings_path, settings_style=True)
            staging.write_text(json.dumps(doc, indent=2) + "\n")
            commit_file(staging, c.settings_path, mode_of(c.settings_path) or FILE_MODE)

    if not dry:
        rec_path = c.receipts / f"rollback-{label}-{now_label()}.json"
        write_json(
            rec_path,
            {
                "label": label,
                "rolled_back_at": datetime.now(timezone.utc).isoformat(),
                "snapshot_format": manifest.get("format_version", 1),
                "repo_head": git_head(),
                "force": force,
                "restored": restored,
                "settings_leaves": leaf_actions,
                "skipped_evidence_only": [r["path"] for r in evidence],
                "conflicts": conflicts,
                "archived": archived,
            },
        )
        print(f"rollback receipt: {rec_path}")
    print("rollback", "(dry run)" if dry else "done", "->", label)
    return EXIT_OK


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def list_snapshots() -> int:
    c = cfg()
    for p in sorted(c.snapshots.glob("*/manifest.json")):
        m = json.loads(p.read_text())
        snap = p.parent
        has_post = (snap / "postimage.json").is_file()
        mode = mode_of(snap)
        print(
            f"{m.get('label', snap.name):40s} {m.get('taken_at', '?')}  "
            f"head={m.get('repo_head', '?')}  postimage={'yes' if has_post else 'no '}  "
            f"mode={oct(mode) if mode is not None else '?'}"
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="wfctl.py", add_help=True, description=__doc__
    )
    sub = parser.add_subparsers(dest="cmd")
    p_snap = sub.add_parser("snapshot")
    p_snap.add_argument("label", nargs="?")
    p_inst = sub.add_parser("install")
    p_inst.add_argument("--label")
    p_inst.add_argument("--accept-drift", action="store_true")
    sub.add_parser("verify")
    p_roll = sub.add_parser("rollback")
    p_roll.add_argument("label")
    p_roll.add_argument("--dry-run", action="store_true")
    p_roll.add_argument("--force", action="store_true")
    sub.add_parser("list")

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return EXIT_OK
    args = parser.parse_args(argv)
    configure()
    if args.cmd == "snapshot":
        snapshot(args.label or now_label())
    elif args.cmd == "install":
        return install(args.label, args.accept_drift)
    elif args.cmd == "verify":
        return verify()
    elif args.cmd == "rollback":
        return rollback(args.label, args.dry_run, args.force)
    elif args.cmd == "list":
        return list_snapshots()
    else:
        sys.exit(f"unknown command: {args.cmd}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
