#!/usr/bin/env python3
"""Codex desktop wake adapter — OFFLINE seam tests (criterion D, PLAN v1 section 4 + R2/R6).

The adapter must be fully exercisable without EVER waking anyone: the start/steer seam refuses unless
it is both explicitly ARMED and the installed app build matches the pinned hash, and even then a
dry-run builds the frame without touching the socket. These tests never open the real IPC socket; the
'verified build' cases use a controlled temp file, and socket paths point at absent files so any
accidental connect would fail loudly rather than pass silently.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import codex_ipc_wake as w  # noqa: E402


def _verified_adapter(socket_path="/nonexistent/ipc.sock"):
    """An adapter whose app-build verifies (temp file hashed to its own sha), so the seam's build
    guard passes and the ARM guard is what's under test. socket_path is absent on purpose."""
    d = tempfile.mkdtemp()
    asar = os.path.join(d, "app.asar")
    content = b"pinned-build-bytes"
    with open(asar, "wb") as fh:
        fh.write(content)
    sha = hashlib.sha256(content).hexdigest()
    return w.CodexIpcWakeAdapter(
        socket_path=socket_path, app_asar_path=asar, expected_app_asar_sha256=sha
    )


def test_frame_roundtrip_le32():
    obj = {"type": "request", "method": "x", "n": 7}
    buf = io.BytesIO(w.encode_frame(obj))
    assert w.decode_frame(buf.read) == obj


def test_text_input_item_shape_matches_installed_build():
    # the verified turn-input item shape read read-only from the installed build's startTurn call
    items = w.text_input_items("hello")
    assert items == [{"type": "text", "text": "hello", "text_elements": []}]
    ts = w.build_turn_start("hello", client_user_message_id="cid-1")
    assert ts["request"]["input"] == items
    assert ts["request"]["clientUserMessageId"] == "cid-1"
    # the app reads turnStart.context.responseItems, so it must be present
    assert ts["context"]["responseItems"] == []


def test_start_frame_targets_owner_at_top_level_not_in_params():
    a = w.CodexIpcWakeAdapter()
    ts = w.build_turn_start("wake up", client_user_message_id="cid-2")
    f1 = a.build_start_turn_frame("conv-1", ts, "owner-9")
    assert f1["method"] == "thread-follower-start-turn"
    assert f1["version"] == w.FOLLOWER_START_TURN_VERSION == 2
    # the router routes by the TOP-LEVEL targetClientId; params must NOT carry it
    assert f1["targetClientId"] == "owner-9"
    assert "targetClientId" not in f1["params"]
    assert f1["params"]["conversationId"] == "conv-1"
    assert f1["params"]["turnStart"] == ts


def test_steer_frame_uses_installed_method_name_and_input_field():
    a = w.CodexIpcWakeAdapter()
    items = w.text_input_items("steer text")
    f2 = a.build_steer_turn_frame(
        "conv-1", items, "owner-9", client_user_message_id="cid-3"
    )
    # the installed build's method is thread-follower-steer-turn (NOT "steer-turn"); content is `input`
    assert f2["method"] == "thread-follower-steer-turn"
    assert f2["version"] == w.STEER_TURN_VERSION == 1
    assert f2["targetClientId"] == "owner-9"
    assert "targetClientId" not in f2["params"]
    assert f2["params"]["input"] == items
    assert f2["params"]["clientUserMessageId"] == "cid-3"


def test_verify_app_build_true_and_false():
    a = _verified_adapter()
    assert a.verify_app_build()["verified"] is True
    bad = w.CodexIpcWakeAdapter(
        app_asar_path="/nonexistent/app.asar", expected_app_asar_sha256="deadbeef"
    )
    vb = bad.verify_app_build()
    assert vb["verified"] is False and vb["actual_sha256"] is None


def test_seam_refuses_when_build_unverified_even_if_armed():
    # strict app-build guard fires FIRST — a mismatched/absent build refuses even an armed live send.
    bad = w.CodexIpcWakeAdapter(
        socket_path="/nonexistent/ipc.sock",
        app_asar_path="/nonexistent/app.asar",
        expected_app_asar_sha256="deadbeef",
    )
    for call in (
        lambda: bad.start_turn(
            "c",
            w.build_turn_start("x", client_user_message_id="c"),
            "owner",
            armed=True,
            dry_run=False,
        ),
        lambda: bad.steer_turn(
            "c", w.text_input_items("x"), "owner", armed=True, dry_run=False
        ),
    ):
        try:
            call()
            assert False, "must refuse an unverified build"
        except w.WakeError as e:
            assert e.code == "app_build_unverified"


def test_seam_refuses_when_verified_but_not_armed():
    a = _verified_adapter()
    for call in (
        lambda: a.start_turn(
            "c", w.build_turn_start("x", client_user_message_id="c"), "owner"
        ),
        lambda: a.steer_turn(
            "c", w.text_input_items("x"), "owner"
        ),  # default armed=False
    ):
        try:
            call()
            assert False, "must refuse an un-armed seam"
        except w.WakeError as e:
            assert e.code == "wake_not_armed"


def test_seam_dry_run_builds_frame_without_touching_socket():
    a = (
        _verified_adapter()
    )  # socket path is absent; a real connect would raise, not return
    ts = w.build_turn_start("x", client_user_message_id="c")
    out = a.start_turn("c", ts, "owner", armed=True, dry_run=True)
    assert out == {"sent": False, "dry_run": True, "frame": out["frame"]}
    assert out["frame"]["method"] == "thread-follower-start-turn"
    out2 = a.steer_turn("c", w.text_input_items("x"), "owner", armed=True, dry_run=True)
    assert out2["sent"] is False and out2["dry_run"] is True
    assert out2["frame"]["method"] == "thread-follower-steer-turn"


def test_owner_only_socket_guard_absent_and_not_a_socket():
    try:
        w.assert_owner_only_socket("/nonexistent/dir/ipc.sock")
        assert False
    except w.WakeError as e:
        assert e.code == "ipc_socket_absent"
    d = tempfile.mkdtemp()
    reg = os.path.join(d, "not-a-socket")
    with open(reg, "wb") as fh:
        fh.write(b"")
    try:
        w.assert_owner_only_socket(reg)
        assert False
    except w.WakeError as e:
        assert e.code == "ipc_socket_absent"  # owned+safe-mode, but not a socket


def test_wake_status_is_not_established_and_carries_schema_limitation():
    a = _verified_adapter()
    st = a.wake_status()
    assert st["status"] == "not_established"
    assert st["route"] == "desktop_ipc"
    assert "app_build" in st and st["app_server_control_socket"] == "absent"
    # the honest host limitation must travel with the status report
    assert st["schema_limitation"] == w.WAKE_SCHEMA_LIMITATION
    assert "turnStart.request" in st["schema_limitation"]
