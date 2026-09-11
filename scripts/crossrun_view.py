#!/usr/bin/env python3
"""Small cross-run results view for WFI-FINISH (rec 67).

Scans checks/*/summary.json and checks/*/results.tsv and emits ONE comparable table per run KIND
(live acceptance / bridge pilot / paste-probe / smoke). It deliberately does NOT pool pass rates
across kinds — they measure different things — and it preserves missing/absent values as "—" rather
than coercing them to 0 (an absent total must never read as a real denominator). Read-only.

Usage: python3 scripts/crossrun_view.py [--repo <root>] [--out <md path>]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

# `live-full40` is listed BEFORE `live` so the alternation matches the full-suite run dirs
# (`live-full40-<ts>`) as their own kind rather than leaving them unmatched (and silently dropped).
KIND_RE = re.compile(r"^(live-full40|live|bridge|paste-probe)-\d{8}T\d{6}Z$")


def short(v, n=8):
    return (str(v)[:n] + "…") if v else "—"


def load_json(p: Path):
    """Return the parsed summary as a dict. Malformed JSON, or valid-but-non-object JSON (e.g. a
    bare `[]`), is preserved as a one-key error dict so the run becomes an error row and the scan
    continues over the other runs instead of raising AttributeError on `.get`."""
    try:
        obj = json.loads(p.read_text())
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}
    if not isinstance(obj, dict):
        return {"_error": f"non-object summary (JSON {type(obj).__name__})"}
    return obj


def _iso_normalise(value) -> str:
    """'Z' -> '+00:00'; pad/trim a fractional second to 6 digits (python < 3.11 fromisoformat)."""
    import re

    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return re.sub(r"\.(\d+)(?=[+-]\d{2}:?\d{2}$|$)", lambda m: "." + (m.group(1) + "000000")[:6], text)


def wall_clock_s(started, finished):
    """Derived wall-clock seconds from ISO start/finish. Returns None (unknown — DISTINCT from 0.0)
    for every ill-defined interval: missing endpoint, unparseable timestamp, mixed naive/aware
    endpoints (the subtraction itself must be inside the guard — it raises TypeError), or a reversed
    interval (finish before start) which is invalid, not a negative duration. None never crashes a
    row; the run still lists, with dur_s shown as '—'."""
    from datetime import datetime

    if not started or not finished:
        return None
    try:
        # python < 3.11 rejects a trailing 'Z' and any fraction that is not 3 or 6 digits;
        # normalise so an unknown duration is only ever an absent/unparseable timestamp, not
        # an interpreter difference.
        a = datetime.fromisoformat(_iso_normalise(started))
        b = datetime.fromisoformat(_iso_normalise(finished))
        secs = (
            b - a
        ).total_seconds()  # inside the guard: mixed naive/aware raises here
    except Exception:
        return None
    if secs < 0:  # reversed timestamps -> invalid interval, not a negative wall-clock
        return None
    return round(secs, 1)


def scan(repo: Path):
    runs = {"live-full40": [], "live": [], "bridge": [], "paste-probe": [], "smoke": []}
    checks = repo / "checks"
    for d in sorted(p for p in checks.iterdir() if p.is_dir()):
        m = KIND_RE.match(d.name)
        # The live-acceptance runner writes summary.json either at the run-dir top level (when --out
        # is the run dir) or under an acceptance/ subdir (when --out is <run-dir>/acceptance). Accept
        # both so every preserved run is comparable regardless of which layout its launcher used.
        sj = d / "summary.json"
        if not sj.exists() and (d / "acceptance" / "summary.json").exists():
            sj = d / "acceptance" / "summary.json"
        rt = d / "results.tsv"
        if m and sj.exists():
            s = load_json(sj)
            runs[m.group(1)].append(
                {
                    "dir": f"checks/{d.name}",
                    "err": s.get("_error"),
                    "contract": s.get("contract"),
                    "head": s.get("head"),
                    "model": s.get("model"),
                    "access": s.get("cmux_access_mode"),
                    "passed": s.get("passed"),
                    "total": s.get("total"),
                    # summary.json records `failed` as a list of case names; show the COUNT here
                    # (the full case lists live in each run's cases.json — not lost, just not inlined)
                    "failed": (
                        len(f) if isinstance((f := s.get("failed")), list) else f
                    ),
                    "at": s.get("started_at") or s.get("at"),
                    # derived wall-clock; None when start/finish absent or invalid (distinct from 0)
                    "dur_s": wall_clock_s(s.get("started_at"), s.get("finished_at")),
                }
            )
        elif rt.exists():
            rows = [r for r in rt.read_text().splitlines() if r.strip()]
            body = rows[1:] if rows else []
            cols = [r.split("\t") for r in body]
            ran = sum(1 for c in cols if len(c) > 2 and c[2] == "ran")
            exit0 = sum(1 for c in cols if len(c) > 1 and c[1] == "0")
            verdicts = {}
            for c in cols:
                if len(c) > 3:
                    verdicts[c[3]] = verdicts.get(c[3], 0) + 1
            runs["smoke"].append(
                {
                    "dir": f"checks/{d.name}",
                    "cases": len(cols),
                    "ran": ran,
                    "exit0": exit0,
                    "verdicts": ", ".join(
                        f"{k}:{v}" for k, v in sorted(verdicts.items())
                    )
                    or "—",
                }
            )
    return runs


def render(repo: Path, runs: dict) -> str:
    try:
        head = (
            subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            or "—"
        )
    except Exception:
        head = "—"
    L = [
        f"# Cross-run results view (generated {'@ source ' + head})",
        "",
        "Generated by `scripts/crossrun_view.py` from `checks/*/summary.json` and `checks/*/results.tsv`.",
        "One table per run **kind**; pass rates are **not pooled across kinds** (they measure different",
        "things). Missing values (including an unknown `dur_s`, derived from `started_at`/`finished_at`)",
        "are shown as `—`, never coerced to 0. A malformed or non-object summary (e.g. a bare `[]`)",
        "becomes an error row (its `note`) and does not stop the other runs. `head` is each run's own",
        "recorded source revision (may differ from the current repo HEAD).",
        "",
    ]

    def cell(v):
        return "—" if v is None else str(v)

    for kind, title in [
        ("live-full40", "Full acceptance runs (complete 40-case suite)"),
        ("live", "Live acceptance runs (earlier partial suites)"),
        ("bridge", "Bridge pilot runs"),
        ("paste-probe", "Paste-probe runs"),
    ]:
        L += [
            f"## {title}",
            "",
            "| Run dir | UTC | contract | head | model | access | passed | total | failed | dur_s | note |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in runs[kind]:
            note = r["err"] or (
                "no `total` recorded"
                if r.get("passed") is not None and r.get("total") is None
                else ""
            )
            dur = "—" if r.get("dur_s") is None else f"{r['dur_s']}"
            L.append(
                f"| `{r['dir']}` | {cell(r.get('at'))} | {cell(r.get('contract'))} | {short(r.get('head'))} | "
                f"{cell(r.get('model'))} | {cell(r.get('access'))} | {cell(r.get('passed'))} | {cell(r.get('total'))} | "
                f"{cell(r.get('failed'))} | {dur} | {note} |"
            )
        if not runs[kind]:
            L.append("| _(none)_ | | | | | | | | | | |")
        L.append("")

    L += [
        "## Smoke runs (`results.tsv`)",
        "",
        "| Run dir | cases | ran | exit0 | semantic verdicts |",
        "|---|---|---|---|---|",
    ]
    for r in runs["smoke"]:
        L.append(
            f"| `{r['dir']}` | {r['cases']} | {r['ran']} | {r['exit0']} | {r['verdicts']} |"
        )
    if not runs["smoke"]:
        L.append("| _(none)_ | | | | |")
    L += [
        "",
        "_Note: `unjudged`/absent semantic verdicts are preserved, not scored. A missing `total`",
        "on a live/bridge row means that run recorded no denominator — do not infer a pass rate._",
        "",
    ]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    repo = Path(a.repo).resolve()
    out = render(repo, scan(repo))
    if a.out:
        Path(a.out).write_text(out)
        print(f"wrote {a.out}")
    else:
        print(out)


if __name__ == "__main__":
    main()
