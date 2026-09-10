#!/usr/bin/env python3
"""seq35: verify the actual INSTALLED Mycelium PLUGIN matches the pinned candidate — distinct from
the cmux-driver postimage check.

PRIMARY signal is the Codex plugin REGISTRY (per note codex-native-installed-registry-locator-r1):
    codex plugin list --marketplace mycelium --json
which exposes the registered installed version, enabled state, and source. A matching unused cache
directory alone is NOT native installation, so the registry is the gate; the cache-dir closure is
corroborating evidence only, and is required to agree ONLY once the registry says the candidate is
installed.

Gates (each a distinct, truthful reason; FAIL on old/missing/mismatched/disabled):
  1. an installed `mycelium@mycelium` entry exists (installed == true);
  2. it is enabled;
  3. its version == the pinned candidate's codex version (EXPORT_MANIFEST.manifest_versions.codex);
  4. its source path == the pinned candidate directory;
  5. corroboration: the installed version's cache dir exists and its coordination/bin/* files are
     byte-identical to the candidate's EXPORT_MANIFEST closure (a registry claim of the candidate
     version backed by absent/lookalike bytes FAILS).

Input: registry JSON from --registry-json FILE ('-' = stdin). Pinned identity from --candidate DIR.
Exit: 0 all gates pass; 1 any gate fails; 2 usage/unreadable inputs. Prints a JSON verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

PLUGIN_ID = "mycelium@mycelium"


def _load_pinned(cand: str) -> dict:
    man = os.path.join(cand, "EXPORT_MANIFEST.json")
    m = json.load(open(man))
    mv = m.get("manifest_versions") or {}
    ver = mv.get("codex")
    if not ver:
        raise ValueError("EXPORT_MANIFEST.manifest_versions.codex missing")
    clo = m.get("closure")
    if not isinstance(clo, dict) or not clo:
        raise ValueError(
            "EXPORT_MANIFEST.closure missing/empty (cannot verify installed bytes)"
        )
    for rel, meta in clo.items():
        if not isinstance(meta, dict) or not meta.get("sha256"):
            raise ValueError(f"EXPORT_MANIFEST.closure entry missing sha256: {rel}")
    return {
        "version": ver,
        "path": os.path.realpath(cand),
        "closure": clo,
    }


def _find_installed(registry: dict, plugin_id: str) -> dict | None:
    for e in registry.get("installed", []) or []:
        if e.get("pluginId") == plugin_id:
            return e
    return None


def _entry_source_path(entry: dict) -> str | None:
    src = entry.get("source") or {}
    p = src.get("path")
    if p:
        return os.path.realpath(p)
    ms = entry.get("marketplaceSource") or {}
    return os.path.realpath(ms["source"]) if ms.get("source") else None


def _sha_file(p: str) -> str | None:
    try:
        return hashlib.sha256(open(p, "rb").read()).hexdigest()
    except OSError:
        return None


def _registered_cache_dir(cache_root: str, entry: dict, version: str) -> str | None:
    """The EXACT cache dir for the REGISTERED marketplace/plugin/version — never a wildcard that
    could match another namespace (review case 3). Codex layout: <cache_root>/<marketplace>/<plugin
    short name>/<version>. Both segments come from the registry entry itself."""
    mkt = entry.get("marketplaceName")
    pid = entry.get("pluginId") or ""
    short = pid.split("@", 1)[0]
    if not mkt or not short:
        return None
    return os.path.join(cache_root, mkt, short, version)


def _verify_full_closure(cache_dir: str, closure: dict):
    """Verify the COMPLETE recorded closure against the cache dir (review case 2): every recorded
    file must be present, byte-identical, and carry the recorded exec bit. Returns lists of
    (missing, hash-mismatched, exec-bit-wrong) and the count checked."""
    missing: list[str] = []
    mism: list[str] = []
    execbad: list[str] = []
    checked = 0
    for rel, meta in closure.items():
        p = os.path.join(cache_dir, rel)
        if not os.path.isfile(p):
            missing.append(rel)
            continue
        checked += 1
        want = meta.get("sha256") if isinstance(meta, dict) else None
        if want is None or _sha_file(p) != want:
            mism.append(rel)
            continue
        if isinstance(meta, dict) and "exec" in meta:
            if bool(meta["exec"]) != os.access(p, os.X_OK):
                execbad.append(rel)
    return missing, mism, execbad, checked


def _default_cache_root() -> str:
    ch = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(ch, "plugins", "cache")


def check(
    cand: str, registry: dict, cache_root: str | None, plugin_id: str = PLUGIN_ID
) -> dict:
    reasons: list[str] = []
    pinned = _load_pinned(cand)
    entry = _find_installed(registry, plugin_id)

    observed = None
    if entry is None or not entry.get("installed", False):
        reasons.append(
            f"plugin NOT installed (registry has no installed {plugin_id} entry)"
        )
    else:
        observed = {
            "version": entry.get("version"),
            "enabled": entry.get("enabled"),
            "source_path": _entry_source_path(entry),
            "marketplace": entry.get("marketplaceName"),
        }
        if not entry.get("enabled", False):
            reasons.append(
                f"installed plugin is NOT enabled (enabled={entry.get('enabled')!r})"
            )
        if entry.get("version") != pinned["version"]:
            reasons.append(
                f"version mismatch (old/mismatched plugin): installed {entry.get('version')!r} "
                f"!= pinned {pinned['version']!r}"
            )
        if observed["source_path"] != pinned["path"]:
            reasons.append(
                f"source path mismatch: installed {observed['source_path']!r} "
                f"!= pinned candidate {pinned['path']!r}"
            )

    # Cache-closure verification — MANDATORY once the registry identity matches (review case 1: a
    # registry claim alone, with no backing bytes, must NOT pass). Bound to the EXACT registered
    # namespace (case 3) and checking the COMPLETE recorded closure incl. exec bits (case 2).
    registry_matches = entry is not None and not reasons
    corroboration = None
    if registry_matches:
        cr = cache_root or _default_cache_root()
        cache_dir = _registered_cache_dir(cr, entry, pinned["version"])
        if not cache_dir:
            reasons.append(
                "cannot derive the registered cache dir (registry entry lacks marketplaceName/"
                "pluginId); refusing to accept an unverifiable install"
            )
        elif not os.path.isdir(cache_dir):
            reasons.append(
                f"exact registered cache dir is ABSENT: {cache_dir} "
                "(registry claim needs backing bytes; a cache in another namespace does not count)"
            )
        else:
            missing, mism, execbad, checked = _verify_full_closure(
                cache_dir, pinned["closure"]
            )
            corroboration = {
                "cache_dir": cache_dir,
                "closure_total": len(pinned["closure"]),
                "checked": checked,
                "missing": len(missing),
                "hash_mismatch": len(mism),
                "exec_bit_wrong": len(execbad),
            }
            if missing or mism or execbad:
                sample = (missing + mism + execbad)[:5]
                reasons.append(
                    f"installed cache {cache_dir} does not match the recorded plugin closure "
                    f"({len(missing)} missing, {len(mism)} hash-mismatch, {len(execbad)} exec-bit "
                    f"of {len(pinned['closure'])} recorded) — e.g. {sample}"
                )

    return {
        "plugin_id": plugin_id,
        "pinned": {"version": pinned["version"], "path": pinned["path"]},
        "observed": observed,
        "corroboration": corroboration,
        "pass": not reasons,
        "reasons": reasons,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Verify the installed Mycelium plugin vs the pinned candidate (seq35)."
    )
    ap.add_argument(
        "--candidate",
        required=True,
        help="pinned candidate dir (has EXPORT_MANIFEST.json)",
    )
    ap.add_argument(
        "--registry-json",
        required=True,
        help="codex plugin list --json output; '-' for stdin",
    )
    ap.add_argument(
        "--cache-root",
        default=None,
        help="Codex plugin cache root for byte corroboration",
    )
    ap.add_argument(
        "--plugin-id",
        default=PLUGIN_ID,
        help=f"registry pluginId to match (default: {PLUGIN_ID})",
    )
    args = ap.parse_args(argv)
    try:
        raw = (
            sys.stdin.read()
            if args.registry_json == "-"
            else open(args.registry_json).read()
        )
        registry = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"FAIL: registry JSON unreadable/malformed: {e}\n")
        return 2
    try:
        verdict = check(args.candidate, registry, args.cache_root, args.plugin_id)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        sys.stderr.write(f"FAIL: cannot load pinned candidate identity: {e}\n")
        return 2
    print(json.dumps(verdict, indent=2))
    if verdict["pass"]:
        print(
            "PLUGIN OK: installed plugin matches the pinned candidate (registry + cache).",
            file=sys.stderr,
        )
        return 0
    for r in verdict["reasons"]:
        print(f"FAIL: {r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
