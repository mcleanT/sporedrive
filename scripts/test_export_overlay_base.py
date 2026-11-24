#!/usr/bin/env python3
"""The canonical core-overlay is refused on any Mycelium other than the release it was written for.

The overlay replaces whole core hook files. Written for Mycelium 0.6.0, applied to 0.9.x it would ship
0.6.0-era hooks that lack the helpers 0.9.x's other hooks call, and roll back 0.9.x Stop/handoff/lock
fixes. The default export applies the overlay, so the refusal has to live in export itself.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_coordination as ec  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _fake_source(tmp_path: Path, version: str) -> Path:
    src = tmp_path / ("src-" + version.replace("+", "_"))
    (src / ".claude-plugin").mkdir(parents=True)
    (src / ".codex-plugin").mkdir(parents=True)
    (src / "hooks").mkdir(parents=True)
    for d in (".claude-plugin", ".codex-plugin"):
        (src / d / "plugin.json").write_text(
            json.dumps({"name": "mycelium", "version": version})
        )
    (src / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {}}))
    return src


@pytest.mark.parametrize(
    "version", ["0.9.7", "0.9.5", "0.9.5+coord.20261116.r1", "1.0.0", ""]
)
def test_canonical_overlay_refused_on_other_releases(tmp_path, version):
    why = ec.overlay_base_problem(_fake_source(tmp_path, version), ec.CANONICAL_OVERLAY)
    assert why and "--no-overlay" in why


@pytest.mark.parametrize(
    "version", ["0.6.0", "0.6.0+coord.request-reduction.20260915.r2"]
)
def test_canonical_overlay_allowed_on_its_base(tmp_path, version):
    assert (
        ec.overlay_base_problem(_fake_source(tmp_path, version), ec.CANONICAL_OVERLAY)
        is None
    )


def test_bundled_source_still_takes_the_overlay():
    # the bundled mycelium-source is the 0.6.0 base the stop-budget export test relies on
    assert (
        ec.overlay_base_problem(ROOT / "mycelium-source", ec.CANONICAL_OVERLAY) is None
    )


def test_no_overlay_and_custom_overlay_are_not_checked(tmp_path):
    src = _fake_source(tmp_path, "0.9.7")
    assert ec.overlay_base_problem(src, None) is None
    custom = tmp_path / "my-overlay"
    custom.mkdir()
    assert ec.overlay_base_problem(src, custom) is None


def test_export_refuses_before_touching_the_target(tmp_path):
    src = _fake_source(tmp_path, "0.9.7")
    target = tmp_path / "out" / "mycelium"
    with pytest.raises(SystemExit) as e:
        ec.export(
            src,
            target,
            force=False,
            keep_stage=False,
            build_id="t",
            overlay=ec.CANONICAL_OVERLAY,
        )
    assert "overlay refused (target untouched)" in str(e.value.code)
    assert not target.exists() and not (tmp_path / "out").exists()
