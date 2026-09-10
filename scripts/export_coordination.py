#!/usr/bin/env python3
"""Reproducible export of the shared coordination subsystem into an owned native Mycelium
candidate (MYCELIUM-INTEGRATION r1, accepted D1).

The candidate is the COMPLETE native Mycelium plugin (built from the preserved SOURCE candidate,
default ~/tools/mycelium-lifecycle-wfi, working tree = 12862e5 + its uncommitted lifecycle fixes)
with the coordination subsystem OVERLAID and its registrations MERGED into the existing manifests /
hooks — NOT a renamed coordination-only overlay, and not a second plugin beside Mycelium. The single
canonical source of the coordination subsystem (and the bridge it links to) is this repository
(`codex-claude-workflow/`).

What the export does:
  * copies the full source working tree (native Mycelium identity + existing skills/hooks + lifecycle
    fixes) into a STAGING dir, minus VCS/junk;
  * overlays the canonical `coordination/` subsystem and the canonical `bridge/` package (so
    `coord_notify_via_bridge` can import `cmux_bridge` from the installed candidate);
  * ships `skills/coordinate/SKILL.md`;
  * MERGES the coordination MCP into both plugin manifests (adding `mcpServers: ./.mcp.json` and a
    VALID distinct build version) and adds a conventional plugin-root `.mcp.json`;
  * MERGES a SessionStart coordination attach hook into the existing `hooks/hooks.json` (resolving
    the plugin root from PLUGIN_ROOT on Codex OR CLAUDE_PLUGIN_ROOT on Claude), without dropping the
    existing SessionStart/PostToolUse/Stop registrations;
  * writes an EXPORT_MANIFEST.json that cross-verifies every retained file against the SOURCE and
    every coordination/bridge file against the CANONICAL repo (not merely self-generated hashes).

Safety (finding 4): staging is validated BEFORE the owned target is touched; the target is refused if
it aliases/overlaps the source, the canonical repo, or a protected frozen/loaded path, and a
non-empty target is only replaced when it carries this exporter's ownership marker (or --force). It
performs NO network, NO git writes, and never touches the frozen WFI runtimes or the source.

Usage:
  export_coordination.py [--source DIR] [--target DIR] [--build-id ID] [--force] [--verify-only]
                         [--keep-stage]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]  # codex-claude-workflow
CANONICAL = REPO / "coordination"  # single canonical coordination source
CANONICAL_BRIDGE = REPO / "bridge"  # single canonical bridge source
DEFAULT_SOURCE = Path.home() / "tools" / "mycelium-lifecycle-wfi"
DEFAULT_TARGET = Path.home() / "tools" / "mycelium-integration-wfi"

MCP_SERVER_NAME = "mycelium-coord"
CLAUDE_MCP_FILE = ".mcp.claude.json"  # Claude-host MCP config (CLAUDE_PLUGIN_ROOT form)
OWN_MARKER = ".mycelium-integration-candidate"  # proves this exporter owns the target
BUILD_ID = "coord.integration.r1"  # semver build identifier appended
_SKIP_NAMES = {"__pycache__", ".pytest_cache", ".ruff_cache", ".DS_Store", ".git"}

# Manifest/registration files we intentionally REWRITE (everything else copied from source must stay
# byte-identical to the source, and is verified so).
_MODIFIED_FROM_SOURCE = {
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "hooks/hooks.json",
}
# Files the exporter GENERATES fresh (no source or canonical origin): legitimately present.
_GENERATED = {
    ".mcp.json",
    CLAUDE_MCP_FILE,
    "README-COORDINATION.md",
    "EXPORT_MANIFEST.json",
    OWN_MARKER,
}
# Critical retained hook bytes the review calls out explicitly (verified == source, must exist).
_CRITICAL_RETAINED = (
    "hooks/mycelium-codex-dispatch.sh",
    "skills/core/hooks/mycelium-hook-lib.sh",
    "skills/core/hooks/mycelium-stop-check.sh",
)

# Protected destinations that must never be overwritten or aliased by the target.
_PROTECTED = [
    Path.home() / ".claude" / "mycelium-runtime",
    Path.home() / ".claude" / "mycelium-runtime-wfi",
    Path.home() / ".claude" / "mycelium-release-wfi",
    Path.home() / "tools" / "mycelium-main",
    Path.home() / ".mycelium",
]

# Semver 2.0.0 (with optional prerelease/build). Practical, not exhaustive.
_SEMVER = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


# ------------------------------------------------------------------ small helpers
def _git(cwd: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _git_identity(cwd: Path) -> dict:
    return {
        "path": str(cwd),
        "commit": _git(cwd, "rev-parse", "HEAD") or None,
        "dirty": bool(_git(cwd, "status", "--porcelain")),
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_exec(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IXUSR)


def _rel_files(root: Path) -> list[str]:
    out = []
    for dp, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_NAMES]
        for name in names:
            if name in _SKIP_NAMES or name.endswith(".pyc"):
                continue
            out.append(str((Path(dp) / name).relative_to(root)))
    return sorted(out)


def _copy_tree(src: Path, dst: Path) -> None:
    for dp, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in _SKIP_NAMES]
        rel = Path(dp).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            if name in _SKIP_NAMES or name.endswith(".pyc"):
                continue
            shutil.copy2(Path(dp) / name, dst / rel / name)


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def _bump_build(version: str, build_id: str) -> str:
    """Append a build identifier as VALID semver: extend existing +build with a dot, else add +build.
    Never produces two '+' delimiters (the review's defect)."""
    base, sep, build = version.partition("+")
    if sep:
        return f"{base}+{build}.{build_id}"
    return f"{base}+{build_id}"


# ------------------------------------------------------------------ path guards (finding 4)
def _resolve(p: Path) -> Path:
    return Path(os.path.realpath(str(p)))


def _is_within(inner: Path, outer: Path) -> bool:
    try:
        inner.relative_to(outer)
        return True
    except ValueError:
        return False


def _guard_target(source: Path, target: Path, force: bool) -> None:
    tr, sr, cr = _resolve(target), _resolve(source), _resolve(REPO)
    forbidden = [sr, cr, *[_resolve(p) for p in _PROTECTED if p.exists()]]
    for f in forbidden:
        if tr == f or _is_within(tr, f) or _is_within(f, tr):
            sys.exit(
                f"refusing target {target}: it aliases/overlaps a protected path ({f})"
            )
    if target.exists() and any(c for c in target.iterdir() if c.name != ".git"):
        if not (target / OWN_MARKER).exists() and not force:
            sys.exit(
                f"refusing to replace non-empty target {target} without ownership marker "
                f"{OWN_MARKER}; pass --force only if you are sure it is the candidate"
            )


# ------------------------------------------------------------------ manifests / mcp / hooks
def build_manifests(source: Path, build_id: str) -> tuple[dict, dict]:
    """Preserve the source Mycelium identity (name, author, skills, interface) and MERGE the
    coordination MCP + a valid distinct build version. Both hosts also discover the conventional
    ./.mcp.json (plugin-json-spec: conventional discovery is SUPPLEMENTED, not replaced)."""
    claude = _load_json(source / ".claude-plugin" / "plugin.json")
    codex = _load_json(source / ".codex-plugin" / "plugin.json")
    coord_desc = (
        str(claude.get("description", "")).rstrip(".")
        + ". Adds shared provider-neutral Codex<->Claude session coordination."
    )
    for m in (claude, codex):
        m["version"] = _bump_build(str(m.get("version", "0.0.0")), build_id)
        m["description"] = coord_desc
        m.setdefault("skills", "./skills/")
    # HOST-SPECIFIC MCP config: the two hosts resolve a plugin's server command differently, proven
    # natively (checks/native-plugin-proof-r1). Claude's plugin loader needs a ${CLAUDE_PLUGIN_ROOT}
    # command and rejects the bare-relative+cwd Codex form (server registers but never connects);
    # Codex uses the relative command + cwd '.'. Each host reads only its own manifest, so each points
    # at the config shape it can actually launch.
    claude["mcpServers"] = f"./{CLAUDE_MCP_FILE}"
    codex["mcpServers"] = "./.mcp.json"
    return claude, codex


def build_mcp_json() -> dict:
    """CODEX-host plugin-root .mcp.json. Mirrors the verified local example
    (openai-bundled/computer-use/.mcp.json): a RELATIVE command with cwd '.', NO PLUGIN_ROOT template
    in the MCP command field (that template is only proven for hook command fields). The launcher
    self-locates and probes for a fastmcp-capable interpreter. Referenced by .codex-plugin/plugin.json.
    NOTE: this form registers but does NOT connect on Claude — Claude uses build_mcp_json_claude()."""
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "command": "./coordination/bin/mycelium-coord-mcp",
                "args": [],
                "cwd": ".",
                "env_vars": [
                    "MYCELIUM_COORD_DIR",
                    "MYCELIUM_COORD_PYTHON",
                    "MYCELIUM_BRIDGE_PATH",
                ],
            }
        }
    }


def build_mcp_json_claude() -> dict:
    """CLAUDE-host plugin MCP config. Claude's plugin loader resolves the server command through the
    CLAUDE_PLUGIN_ROOT it exports; the bare-relative+cwd Codex form registers the server but fails to
    connect (proven natively, checks/native-plugin-proof-r1). No env dict is hardcoded: the launcher
    probes for a fastmcp-capable interpreter and MYCELIUM_COORD_DIR is inherited from the session env
    (defaulting to the owner-only store when unset). Referenced by .claude-plugin/plugin.json."""
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "command": "${CLAUDE_PLUGIN_ROOT}/coordination/bin/mycelium-coord-mcp",
                "args": [],
            }
        }
    }


def _coord_hook_entry() -> dict:
    """SessionStart coordination attach hook that fires on BOTH hosts: Codex exports PLUGIN_ROOT,
    Claude exports CLAUDE_PLUGIN_ROOT. The attach script itself detects the host and stays silent
    when this native session owns no selection."""
    cmd = (
        'ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"; '
        'if [ -n "$ROOT" ] && [ -x "$ROOT/coordination/hooks/mycelium-coord-attach.sh" ]; then '
        '"$ROOT/coordination/hooks/mycelium-coord-attach.sh"; fi'
    )
    return {"type": "command", "command": cmd}


def merge_hooks(source: Path) -> dict:
    """Preserve the source hooks.json (SessionStart health, PostToolUse, Stop) and ADD the
    coordination attach hook to SessionStart without duplicates."""
    hooks = _load_json(source / "hooks" / "hooks.json")
    hooks.setdefault("hooks", {})
    ss = hooks["hooks"].setdefault("SessionStart", [])
    entry = _coord_hook_entry()
    already = any(
        "mycelium-coord-attach.sh" in h.get("command", "")
        for grp in ss
        for h in grp.get("hooks", [])
    )
    if not already:
        ss.append({"matcher": "startup|resume|clear|compact", "hooks": [entry]})
    return hooks


# ------------------------------------------------------------------ closure + cross-origin verify
def closure(root: Path) -> dict:
    files = {}
    for rel in _rel_files(root):
        if rel == "EXPORT_MANIFEST.json":
            continue
        p = root / rel
        files[rel] = {"sha256": _sha256(p), "exec": _is_exec(p)}
    return dict(sorted(files.items()))


def _cross_origin(stage: Path, source: Path) -> tuple[dict, dict, list[str]]:
    """Verify retained files == SOURCE and coordination/bridge/skill == CANONICAL. Returns
    (retained_map, canonical_map, problems)."""
    problems = []
    retained, canonical = {}, {}
    canon_prefixes = ("coordination/", "bridge/", "skills/coordinate/")
    for rel in _rel_files(stage):
        if rel in _GENERATED:
            continue
        sp = stage / rel
        digest = _sha256(sp)
        if any(rel.startswith(pref) for pref in canon_prefixes):
            # map candidate coordination/bridge/skill file to its canonical origin
            if rel.startswith("coordination/"):
                origin = CANONICAL / rel[len("coordination/") :]
            elif rel.startswith("bridge/"):
                origin = CANONICAL_BRIDGE / rel[len("bridge/") :]
            else:  # skills/coordinate/SKILL.md <- canonical coordination/skill/SKILL.md
                origin = CANONICAL / "skill" / rel[len("skills/coordinate/") :]
            canonical[rel] = digest
            if not origin.is_file():
                problems.append(
                    f"coordination/bridge file has no canonical origin: {rel}"
                )
            elif _sha256(origin) != digest:
                problems.append(f"coordination/bridge file != canonical: {rel}")
        elif rel in _MODIFIED_FROM_SOURCE:
            continue  # intentionally rewritten
        else:
            origin = source / rel
            if origin.is_file():
                retained[rel] = digest
                if _sha256(origin) != digest:
                    problems.append(f"retained file != source: {rel}")
            # a candidate file with no source/canonical origin is an unexpected artifact
            elif not any(rel.startswith(pref) for pref in canon_prefixes):
                problems.append(f"candidate file has no source origin: {rel}")
    for rel in _CRITICAL_RETAINED:
        if not (stage / rel).is_file():
            problems.append(f"critical retained file missing: {rel}")
    return retained, canonical, problems


def _validate_manifests(stage: Path) -> list[str]:
    problems = []
    for name in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        try:
            m = _load_json(stage / name)
        except Exception as e:
            problems.append(f"{name}: invalid JSON ({e})")
            continue
        for key in ("name", "version", "description"):
            if not m.get(key):
                problems.append(f"{name}: missing '{key}'")
        v = str(m.get("version", ""))
        if not _SEMVER.match(v):
            problems.append(f"{name}: invalid semver '{v}'")
        skills = m.get("skills")
        if skills and not (stage / str(skills).lstrip("./")).exists():
            problems.append(f"{name}: skills path '{skills}' does not exist")
        mref = m.get("mcpServers")
        mrel = mref[2:] if isinstance(mref, str) and mref.startswith("./") else mref
        if mref and not (stage / str(mrel)).is_file():
            problems.append(f"{name}: mcpServers -> {mref} missing")
    for name in (".mcp.json", CLAUDE_MCP_FILE, "hooks/hooks.json"):
        try:
            _load_json(stage / name)
        except Exception as e:
            problems.append(f"{name}: invalid JSON ({e})")
    return problems


def _validate_bridge_import(stage: Path) -> list[str]:
    """Prove cmux_bridge is importable from the candidate's overlaid bridge, distinct from any
    transport permission (finding 3)."""
    code = (
        "import sys; sys.path.insert(0, %r); import cmux_bridge.core as c; "
        "print('ok', bool(c.Bridge))" % str(stage / "bridge")
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if r.returncode != 0 or "ok" not in r.stdout:
        return [f"bridge import failed: {r.stderr.strip()[:200]}"]
    return []


# ------------------------------------------------------------------ build + swap
def _stage(source: Path, target: Path, build_id: str) -> tuple[Path, dict]:
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".stage.export.", dir=str(parent)))

    _copy_tree(source, stage)  # 1. full source (Mycelium identity)
    _copy_tree(CANONICAL, stage / "coordination")  # 2. coordination overlay
    _copy_tree(CANONICAL_BRIDGE, stage / "bridge")  # 3. bridge overlay (import path)
    (stage / "skills" / "coordinate").mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        CANONICAL / "skill" / "SKILL.md", stage / "skills" / "coordinate" / "SKILL.md"
    )  # 4. shared skill

    claude_m, codex_m = build_manifests(source, build_id)  # 5. merged manifests
    _write_json(stage / ".claude-plugin" / "plugin.json", claude_m)
    _write_json(stage / ".codex-plugin" / "plugin.json", codex_m)
    _write_json(stage / ".mcp.json", build_mcp_json())  # 6a. Codex MCP (relative+cwd)
    _write_json(
        stage / CLAUDE_MCP_FILE, build_mcp_json_claude()
    )  # 6b. Claude MCP (CLAUDE_PLUGIN_ROOT)
    _write_json(stage / "hooks" / "hooks.json", merge_hooks(source))  # 7. merged hooks

    _write_json(
        stage / OWN_MARKER,
        {
            "candidate": "mycelium (coordination-integrated)",
            "purpose": f"MYCELIUM-INTEGRATION native coordination candidate (build {build_id})",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "canonical_source": _git_identity(REPO),
            "source_candidate": _git_identity(source),
        },
    )
    (stage / "README-COORDINATION.md").write_text(_readme())
    return stage, {"claude": claude_m, "codex": codex_m}


def _swap(stage: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in target.iterdir():
        if child.name == ".git":
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    for child in stage.iterdir():
        shutil.move(str(child), str(target / child.name))
    shutil.rmtree(stage, ignore_errors=True)


def export(
    source: Path, target: Path, force: bool, keep_stage: bool, build_id: str
) -> dict:
    for req in (
        CANONICAL,
        CANONICAL_BRIDGE,
        source,
        source / ".claude-plugin" / "plugin.json",
        source / "hooks" / "hooks.json",
    ):
        if not req.exists():
            sys.exit(f"required input missing: {req}")
    _guard_target(source, target, force)

    stage, manifests = _stage(source, target, build_id)

    problems = _validate_manifests(stage)
    retained, canonical, xo = _cross_origin(stage, source)
    problems += xo
    problems += _validate_bridge_import(stage)
    if problems:
        if not keep_stage:
            shutil.rmtree(stage, ignore_errors=True)
        sys.exit(
            "staging validation FAILED (target untouched):\n  " + "\n  ".join(problems)
        )

    manifest = {
        "candidate": "mycelium (coordination-integrated)",
        "purpose": f"MYCELIUM-INTEGRATION native coordination candidate (build {build_id})",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "canonical_source": _git_identity(REPO),
        "source_candidate": _git_identity(source),
        "mcp_server": MCP_SERVER_NAME,
        "manifest_versions": {
            "claude": manifests["claude"]["version"],
            "codex": manifests["codex"]["version"],
        },
        "retained_from_source": retained,
        "coordination_from_canonical": canonical,
        "closure": closure(stage),
    }
    _write_json(stage / "EXPORT_MANIFEST.json", manifest)
    _swap(stage, target)
    return manifest


# ------------------------------------------------------------------ verify (standalone)
def verify(target: Path, source: Path) -> tuple[bool, list[str]]:
    mpath = target / "EXPORT_MANIFEST.json"
    if not mpath.is_file():
        return False, [f"no EXPORT_MANIFEST.json in {target}"]
    rec = _load_json(mpath)
    problems = []
    # 1. closure self-consistency
    recorded = rec.get("closure", {})
    current = closure(target)
    for rel, meta in recorded.items():
        if rel not in current:
            problems.append(f"missing: {rel}")
        elif current[rel]["sha256"] != meta["sha256"]:
            problems.append(f"hash mismatch: {rel}")
        elif meta.get("exec") and not current[rel]["exec"]:
            problems.append(f"lost +x: {rel}")
    for rel in current:
        if rel not in recorded and rel != OWN_MARKER:
            problems.append(f"unexpected: {rel}")
    # 2. cross-origin against source + canonical (not merely self-generated hashes)
    for rel, digest in rec.get("retained_from_source", {}).items():
        o = source / rel
        if not o.is_file() or _sha256(o) != digest:
            problems.append(f"retained != source: {rel}")
    for rel, digest in rec.get("coordination_from_canonical", {}).items():
        if rel.startswith("coordination/"):
            o = CANONICAL / rel[len("coordination/") :]
        elif rel.startswith("bridge/"):
            o = CANONICAL_BRIDGE / rel[len("bridge/") :]
        else:
            o = CANONICAL / "skill" / rel[len("skills/coordinate/") :]
        if not o.is_file() or _sha256(o) != digest:
            problems.append(f"coordination/bridge != canonical: {rel}")
    problems += _validate_manifests(target)
    return (not problems), problems


def _readme() -> str:
    """Deployment-neutral generated README.

    Records NO build-cycle label or source path inline: the exact source
    revision, canonical coordination revision, build identifier, and full
    per-file closure for THIS build live in EXPORT_MANIFEST.json and are the
    single source of truth. Keeping the README free of a hardcoded revision /
    source / transport-state claim means it never drifts from the actual build
    metadata across successive builds.
    """
    return (
        "# Mycelium — coordination-integrated candidate\n\n"
        "This is the COMPLETE native Mycelium plugin (built from the preserved SOURCE candidate)\n"
        "with the shared Codex<->Claude coordination subsystem overlaid and its registrations merged\n"
        "— one plugin, not a separate coordination plugin. The exact source revision, canonical\n"
        "coordination revision, build identifier, manifest versions, and full per-file closure for\n"
        "THIS build are recorded in `EXPORT_MANIFEST.json` (`source_candidate`, `canonical_source`,\n"
        "`purpose`, `manifest_versions`, `closure`) and are the single source of truth. Regenerate or\n"
        "verify with `codex-claude-workflow/scripts/export_coordination.py --verify-only` (pass the\n"
        "`--source` recorded in EXPORT_MANIFEST.source_candidate and `--target` set to this dir).\n\n"
        "## Coordination additions\n"
        "- `coordination/` — canonical subsystem (MCP server, CLI, bridge link, tests).\n"
        "- `bridge/` — canonical `cmux_bridge` package so `coord_notify_via_bridge` imports it when\n"
        "  installed (the import path is distinct from any cmux transport permission).\n"
        "- `.mcp.json` / `.mcp.claude.json` — host-specific MCP config for server `mycelium-coord`\n"
        "  (Codex: relative command + cwd '.'; Claude: a ${CLAUDE_PLUGIN_ROOT} command).\n"
        "- `hooks/hooks.json` — existing Mycelium hooks PRESERVED + a SessionStart coordination attach\n"
        "  hook resolving PLUGIN_ROOT (Codex) or CLAUDE_PLUGIN_ROOT (Claude).\n"
        "- `skills/coordinate/SKILL.md` — the shared coordination skill, beside existing skills.\n"
        "- `EXPORT_MANIFEST.json` — source + canonical identities, retained==source and\n"
        "  coordination==canonical cross-origin maps, and a full per-file closure.\n\n"
        "## Native proof (authorized, isolated)\n"
        "Load into REAL Claude Code and Codex CLI runs with SEPARATE disposable config/task dirs,\n"
        "preserving installed-build identity + account/model policy: prove MCP discovery/call on both\n"
        "hosts, one addressed exchange, and selected-task restoration via the supported resume path.\n"
        "Production loading into still-active sessions uses a supported non-disruptive activation that\n"
        "preserves the exact plugin bytes and paths already in use by live sessions.\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Export the coordination subsystem into the native Mycelium candidate"
    )
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    ap.add_argument(
        "--force",
        action="store_true",
        help="replace a non-empty target lacking the ownership marker",
    )
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument(
        "--keep-stage",
        action="store_true",
        help="keep the staging dir on validation failure",
    )
    ap.add_argument(
        "--build-id",
        default=BUILD_ID,
        help="semver build identifier appended to the plugin version (default: %(default)s)",
    )
    args = ap.parse_args()
    if not re.fullmatch(r"[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*", args.build_id):
        sys.exit(
            f"invalid --build-id {args.build_id!r}: expected dot-separated "
            "semver build identifiers ([0-9A-Za-z-])"
        )

    if args.verify_only:
        ok, problems = verify(args.target, args.source)
        print(json.dumps({"verified": ok, "problems": problems}, indent=2))
        return 0 if ok else 1

    manifest = export(
        args.source, args.target, args.force, args.keep_stage, args.build_id
    )
    ok, problems = verify(args.target, args.source)
    print(
        json.dumps(
            {
                "staged": str(args.target),
                "files": len(manifest["closure"]),
                "retained_from_source": len(manifest["retained_from_source"]),
                "coordination_from_canonical": len(
                    manifest["coordination_from_canonical"]
                ),
                "manifest_versions": manifest["manifest_versions"],
                "canonical_source": manifest["canonical_source"],
                "source_candidate": manifest["source_candidate"],
                "self_verified": ok,
                "problems": problems,
            },
            indent=2,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
