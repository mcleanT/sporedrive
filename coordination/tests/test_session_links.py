#!/usr/bin/env python3
"""Mode 2 "link" (docs/triangle/TWO-MODES-DESIGN.md sections 3-5): two named live sessions pinned to one task.
In-process, one process holds every capability it attached; ``sendcap.forget()`` makes this process look like a
second process that never attached. Nothing here launches a model CLI. Run on Betty only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import Coordinator, ProtocolError, model, sendcap, wake  # noqa: E402
from mycelium_coord.store import CoordStore  # noqa: E402

ADV = "MYCELIUM_COORD_ADVISORY"
PKG_ROOT = str(Path(__file__).resolve().parents[1])
THREAD = "01a2090c-f749-72e0-a856-3dcad3d24574"
LINK_FEATS = ["triangle-kinds", "send-capability", "session-links"]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv(ADV, raising=False)
    monkeypatch.delenv(sendcap.CAP_FILE_ENV, raising=False)
    sendcap.forget()
    yield
    sendcap.forget()


def _co(tmp_path, features=LINK_FEATS, name="store"):
    co = Coordinator(CoordStore(str(tmp_path / name)))
    co.create_task(
        "t",
        project="p",
        worktree_realpath="/wt",
        revision=1,
        protocol_features=list(features),
    )
    return co


def _attach(
    co,
    pid,
    role="supervisor",
    host="claude",
    session=None,
    native_id=None,
    extra=None,
    **kw,
):
    h = {"host": host}
    if session is not False:
        h["session"] = session or f"{pid}-sess"
    if native_id:
        h["native_id"] = native_id
    h.update(extra or {})
    return co.attach(
        "t",
        pid,
        role=role,
        worktree_realpath="/wt" if role == "executor" else "/out",
        host=h,
        **kw,
    )


def _pair(co):
    _attach(co, "lead", "supervisor", "claude")
    _attach(co, "ex", "executor", "codex")


def _active(co, link_id="L1"):
    _pair(co)
    co.links.propose("t", link_id, "lead", "ex")
    return co.links.confirm("t", link_id, "ex")


def _send(co, mid, sender, to, kind="progress", link_id=None):
    return co.send(
        task_id="t",
        message_id=mid,
        sender=sender,
        recipient=to,
        kind=kind,
        task_revision=1,
        text="x",
        link_id=link_id,
    )


def _code(fn):
    with pytest.raises(ProtocolError) as e:
        fn()
    return e.value.code


def _cli(store, *args, cap_file=None):
    env = {**os.environ, "PYTHONPATH": PKG_ROOT}
    env.pop(ADV, None)
    env.pop(sendcap.CAP_FILE_ENV, None)
    pre = ["--root", str(store)] + (["--cap-file", str(cap_file)] if cap_file else [])
    return subprocess.run(
        [sys.executable, "-m", "mycelium_coord", *pre, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


# ------------------------------------------------------------------ feature gate
def test_session_links_is_a_known_feature_and_requires_send_capability(tmp_path):
    assert "session-links" in model.PROTOCOL_FEATURES
    co = Coordinator(CoordStore(str(tmp_path / "s")))
    assert (
        _code(
            lambda: co.create_task(
                "t",
                project="p",
                worktree_realpath="/wt",
                protocol_features=["session-links"],
            )
        )
        == "session_links_requires_send_capability"
    )
    assert (
        _code(
            lambda: co.create_task(
                "t",
                project="p",
                worktree_realpath="/wt",
                protocol_features=["triangle-kinds", "session-links"],
            )
        )
        == "session_links_requires_send_capability"
    )
    assert co.get_task("t") is None  # nothing was created
    t = co.create_task(
        "t",
        project="p",
        worktree_realpath="/wt",
        protocol_features=["send-capability", "session-links"],
    )
    assert "session-links" in t["protocol_features"]


def test_link_ops_refused_on_a_task_without_the_feature(tmp_path):
    co = _co(tmp_path, features=["triangle-kinds", "send-capability"])
    _pair(co)
    assert (
        _code(lambda: co.links.propose("t", "L1", "lead", "ex"))
        == "link_feature_required"
    )
    assert (
        _code(lambda: _send(co, "m1", "lead", "ex", link_id="L1"))
        == "link_feature_required"
    )
    assert not (co.store.root / "tasks" / "t" / "links").exists()


# ------------------------------------------------------------------ propose / confirm / end
def test_propose_confirm_end_records_pins_from_the_attach_records(tmp_path):
    co = _co(tmp_path)
    _pair(co)
    link = co.links.propose("t", "L1", "lead", "ex")
    assert (
        link["state"] == "proposed"
        and link["created_by"] == "lead"
        and link["schema"] == "session-link/1"
    )
    a, b = link["members"]
    assert (a["participant_id"], a["role"], a["host"], a["session"]) == (
        "lead",
        "supervisor",
        "claude",
        "lead-sess",
    )
    assert (b["participant_id"], b["role"], b["host"], b["session"]) == (
        "ex",
        "executor",
        "codex",
        "ex-sess",
    )
    assert a["confirmed_at"] and b["confirmed_at"] is None and a["wake_route"] is None
    assert a["session_verified"] is True
    assert (co.store.root / "tasks" / "t" / "links" / "L1.json").is_file()

    # only the proposed partner may confirm
    assert (
        _code(lambda: co.links.confirm("t", "L1", "lead")) == "link_confirm_not_partner"
    )
    act = co.links.confirm("t", "L1", "ex")
    assert act["state"] == "active" and act["members"][1]["confirmed_at"]
    assert [m["wake_route"] for m in act["members"]] == ["claude-bridge", "poll"]
    assert _code(lambda: co.links.confirm("t", "L1", "ex")) == "link_not_proposed"

    # either member may end it; ending twice is idempotent; an outsider may not
    _attach(co, "third", "supervisor", "claude")
    assert _code(lambda: co.links.end("t", "L1", "third", "x")) == "link_not_member"
    ended = co.links.end("t", "L1", "lead", "done")
    assert (
        ended["state"] == "ended"
        and ended["end_reason"] == "done"
        and ended["ended_at"]
    )
    assert co.links.end("t", "L1", "ex")["idempotent"] is True
    assert co.links.get("t", "L1")["state"] == "ended"
    assert (
        _code(lambda: _send(co, "m1", "lead", "ex", link_id="L1")) == "link_not_active"
    )
    kinds = [
        json.loads(x)["kind"]
        for x in (co.store.root / "tasks" / "t" / "audit.jsonl")
        .read_text()
        .splitlines()
    ]
    assert {"link_propose", "link_confirm", "link_end"} <= set(kinds)


def test_propose_validations(tmp_path):
    co = _co(tmp_path)
    _pair(co)
    assert (
        _code(lambda: co.links.propose("t", "L1", "lead", "lead"))
        == "link_invalid_members"
    )
    assert (
        _code(lambda: co.links.propose("t", "L1", "lead", "nobody"))
        == "unknown_participant"
    )
    co.links.propose("t", "L1", "lead", "ex")
    assert (
        co.links.propose("t", "L1", "lead", "ex")["idempotent"] is True
    )  # same proposal replays
    assert _code(lambda: co.links.propose("t", "L1", "ex", "lead")) == "link_exists"
    assert _code(lambda: co.links.get("t", "nope")) == "unknown_link"


def test_confirm_without_capability_is_refused(tmp_path):
    co = _co(tmp_path)
    _pair(co)
    co.links.propose("t", "L1", "lead", "ex")
    sendcap.forget()  # this process now looks like one that never attached `ex`
    assert (
        _code(lambda: co.links.confirm("t", "L1", "ex")) == "send_capability_required"
    )
    assert co.links.get("t", "L1")["state"] == "proposed"
    assert (
        _code(lambda: co.links.propose("t", "L2", "lead", "ex"))
        == "send_capability_required"
    )
    assert _code(lambda: co.links.end("t", "L1", "lead")) == "send_capability_required"


def test_advisory_process_may_confirm_and_end_but_not_propose(tmp_path, monkeypatch):
    pytest.importorskip("fastmcp")
    from mycelium_coord import mcp_server  # import BEFORE the advisory env is set: an import under it hides tools

    co = _co(tmp_path)
    _attach(co, "lead", "supervisor", "claude")
    _attach(co, "ag", "observer", "antigravity", native_id="agy-1")
    co.links.propose("t", "L1", "lead", "ag")
    monkeypatch.setenv(ADV, "1")
    assert (
        _code(lambda: co.links.propose("t", "L2", "lead", "ag"))
        == "advisory_process_refused"
    )
    act = co.links.confirm("t", "L1", "ag")
    assert act["state"] == "active"
    assert co.links.end("t", "L1", "ag", "bye")["state"] == "ended"
    # the MCP surface: confirm / end / get are listed for an advisory process, propose is not
    allowed = mcp_server.ADVISORY_ALLOWED_TOOLS
    assert {"coord_link_confirm", "coord_link_end", "coord_link_get"} <= allowed
    assert "coord_link_propose" not in allowed and "coord_wake" not in allowed


def test_antigravity_member_must_be_observer_and_is_unverified(tmp_path):
    co = _co(tmp_path)
    _attach(co, "lead", "supervisor", "claude")
    _attach(co, "agx", "executor", "antigravity", session="agx-sess")
    assert (
        _code(lambda: co.links.propose("t", "L1", "lead", "agx"))
        == "link_antigravity_observer_only"
    )
    _attach(co, "ag", "observer", "antigravity", native_id="agy-1", session=False)
    link = co.links.propose("t", "L2", "lead", "ag")
    m = link["members"][1]
    assert (
        m["session_verified"] is False
        and m["session"] is None
        and m["native_id"] == "agy-1"
    )
    act = co.links.confirm("t", "L2", "ag")
    assert act["members"][1]["wake_route"] == "none"  # an observer is never woken
    # sends limited to the advisory kinds; to the partner only
    assert (
        _send(co, "m1", "ag", "lead", kind="question", link_id="L2")["message"][
            "recipient"
        ]
        == "lead"
    )
    assert (
        _code(lambda: _send(co, "m2", "ag", "lead", kind="task", link_id="L2"))
        == "link_antigravity_kind_refused"
    )


def test_wake_route_is_recorded_truthfully_for_a_headless_codex_member(tmp_path):
    co = _co(tmp_path, features=LINK_FEATS + ["codex-exec-wake"])
    _attach(co, "lead", "supervisor", "claude")
    _attach(co, "ex", "executor", "codex", session=THREAD, extra={"wake": "headless"})
    co.links.propose("t", "L1", "lead", "ex")
    act = co.links.confirm("t", "L1", "ex")
    assert [m["wake_route"] for m in act["members"]] == [
        "claude-bridge",
        "codex-exec-resume",
    ]


# ------------------------------------------------------------------ send inside a link
def test_send_in_link_goes_to_exactly_the_partner(tmp_path):
    co = _co(tmp_path)
    _active(co)
    _attach(co, "third", "supervisor", "claude")
    ok = _send(co, "m1", "lead", "ex", link_id="L1")
    assert ok["message"]["recipient"] == "ex"
    assert (
        _send(co, "m1b", "ex", "lead", link_id="L1")["message"]["recipient"] == "lead"
    )  # either direction
    for to in ("all", "executor", "supervisor", "observer", "third", "lead"):
        assert (
            _code(lambda: _send(co, "mx-" + to, "lead", to, link_id="L1"))
            == "link_requires_exact_recipient"
        ), to
    assert (
        _code(lambda: _send(co, "m9", "third", "ex", link_id="L1"))
        == "link_sender_not_member"
    )
    # nothing was persisted by the refusals
    assert sorted(
        p.name for p in (co.store.root / "tasks" / "t" / "ids").iterdir()
    ) == ["m1.json", "m1b.json"]
    # a send WITHOUT link_id behaves as before, even to a role
    assert _send(co, "free", "lead", "all")["message"]["recipient"] == "all"


# ------------------------------------------------------------------ staleness
def test_stale_on_detach_never_retargets(tmp_path):
    co = _co(tmp_path)
    _active(co)
    co.detach("t", "ex")
    assert co.links.get("t", "L1")["state"] == "stale"
    assert (
        co.store.read("tasks/t/links/L1.json")["state"] == "stale"
    )  # persisted (sticky) by the detach itself
    assert _code(lambda: _send(co, "m1", "lead", "ex", link_id="L1")) == "link_stale"
    # re-attaching the same session later does not revive it
    _attach(co, "ex", "executor", "codex")
    assert co.links.get("t", "L1")["state"] == "stale"
    assert _code(lambda: _send(co, "m2", "lead", "ex", link_id="L1")) == "link_stale"
    msgs = co.store.root / "tasks" / "t" / "messages"
    assert not msgs.exists() or not any(p.name.endswith(("-m1.json", "-m2.json")) for p in msgs.iterdir())


def test_stale_when_a_different_native_session_takes_the_participant(tmp_path):
    co = _co(tmp_path)
    _active(co)
    _attach(
        co, "ex", "executor", "codex", session="another-session", allow_transition=True
    )
    view = co.links.get("t", "L1")
    assert (
        view["state"] == "stale" and "member_session_changed:ex" in view["stale_reason"]
    )
    assert _code(lambda: _send(co, "m1", "lead", "ex", link_id="L1")) == "link_stale"
    # the stale link cannot be confirmed either
    co2 = _co(tmp_path, name="s2")
    _pair(co2)
    co2.links.propose("t", "L1", "lead", "ex")
    _attach(
        co2, "ex", "executor", "codex", session="another-session", allow_transition=True
    )
    assert _code(lambda: co2.links.confirm("t", "L1", "ex")) == "link_stale"


def test_stale_when_the_host_kind_changes_on_the_same_session(tmp_path):
    co = _co(tmp_path)
    _active(co)
    _attach(co, "ex", "executor", "claude", session="ex-sess", allow_transition=True)
    assert co.links.get("t", "L1")["state"] == "stale"


def test_null_never_equals_a_changed_session(tmp_path):
    co = _co(tmp_path)
    _attach(co, "lead", "supervisor", "claude")
    _attach(
        co, "ag", "observer", "antigravity", native_id="agy-1", session=False
    )  # pinned (session None, agy-1)
    co.links.propose("t", "L1", "lead", "ag")
    co.links.confirm("t", "L1", "ag")
    assert co.links.get("t", "L1")["state"] == "active"
    # the same native id now arrives with a real session: a changed pin, not a null match
    _attach(
        co,
        "ag",
        "observer",
        "antigravity",
        native_id="agy-1",
        session="S9",
        allow_transition=True,
    )
    assert co.links.get("t", "L1")["state"] == "stale"
    assert _code(lambda: _send(co, "m1", "lead", "ag", link_id="L1")) == "link_stale"
    # and the reverse direction: a pinned session that goes null is also a change
    co.links.propose("t", "L2", "lead", "ag")
    co.links.confirm("t", "L2", "ag")
    _attach(
        co,
        "ag",
        "observer",
        "antigravity",
        native_id="agy-1",
        session=False,
        allow_transition=True,
    )
    assert co.links.get("t", "L2")["state"] == "stale"


def test_same_session_reattach_with_a_new_capability_is_not_stale(tmp_path):
    co = _co(tmp_path)
    _active(co)
    old = co.store.read("tasks/t/participants/ex.json")["send_cap"]
    sendcap.forget()  # a new process (e.g. a headless Codex wake) attaches the SAME session again
    _attach(co, "ex", "executor", "codex", allow_transition=True)
    sendcap.forget()
    new = co.store.read("tasks/t/participants/ex.json")["send_cap"]
    assert new != old  # a new capability was minted
    assert co.links.get("t", "L1")["state"] == "active"
    _attach(
        co, "lead", "supervisor", "claude", allow_transition=True
    )  # the other side too (this process holds it)
    assert co.links.get("t", "L1")["state"] == "active"
    assert _send(co, "m1", "lead", "ex", link_id="L1")["message"]["recipient"] == "ex"


# ------------------------------------------------------------------ wake inside a link
def test_wake_in_link_checks_the_pin_then_runs_the_existing_route(tmp_path):
    co = _co(tmp_path)
    _active(co)
    _attach(co, "third", "supervisor", "claude")
    m = _send(co, "w1", "lead", "ex", kind="task", link_id="L1")["message"]
    rec = wake.wake_recipient(co, "t", "w1", sender="lead", link_id="L1")
    # the link check passed; the unchanged route then ran (Codex without codex-exec-wake: a recorded refusal)
    assert rec["recipient"] == "ex" and rec["outcome"] != "accepted" and "route" in rec
    # a message addressed to someone other than the partner is refused inside the link
    _send(co, "w2", "lead", "third", kind="task")
    assert (
        _code(lambda: wake.wake_recipient(co, "t", "w2", sender="lead", link_id="L1"))
        == "link_requires_exact_recipient"
    )
    # a role / all recipient too
    _send(co, "w3", "lead", "executor", kind="task")
    assert (
        _code(lambda: wake.wake_recipient(co, "t", "w3", sender="lead", link_id="L1"))
        == "link_requires_exact_recipient"
    )
    # a stale link refuses before any route
    _send(co, "w4", "lead", "ex", kind="task", link_id="L1")
    _attach(co, "ex", "executor", "codex", session="elsewhere", allow_transition=True)
    assert (
        _code(lambda: wake.wake_recipient(co, "t", "w4", sender="lead", link_id="L1"))
        == "link_stale"
    )
    assert m["recipient"] == "ex"


# ------------------------------------------------------------------ wake: the pin travels with the delivery
T2 = "01a2090c-f749-72e0-a856-3dcad3d24575"
CODEX_FEATS = LINK_FEATS + ["codex-exec-wake"]


def _dead_holder(co, pid):
    import platform

    rel = f"tasks/t/participants/{pid}.json"
    p = co.store.read(rel)
    p["send_cap"]["holder"] = {"pid": 2**22 + 12345, "node": platform.node(), "start": "x"}
    co.store.write(rel, p)
    sendcap.forget(co.store, "t", pid)


def _codex_link(tmp_path):
    co = _co(tmp_path, features=CODEX_FEATS)
    _attach(co, "lead", "supervisor", "claude")
    _attach(co, "ex", "executor", "codex", session=THREAD, extra={"wake": "headless"})
    co.links.propose("t", "L1", "lead", "ex")
    co.links.confirm("t", "L1", "ex")
    _dead_holder(co, "ex")
    _send(co, "w1", "lead", "ex", kind="task", link_id="L1")
    return co


def _rebind_ex(co):
    _attach(co, "ex", "executor", "codex", session=T2, extra={"wake": "headless"}, allow_transition=True)
    _dead_holder(co, "ex")


class _Proc:
    pid = 999999
    returncode = 0

    def communicate(self, *a, **k):
        return b"", b""


@pytest.mark.parametrize("where", ["after_link_check", "after_recipient_read"])
def test_codex_link_wake_never_launches_against_a_rebound_newcomer(tmp_path, monkeypatch, where):
    from mycelium_coord import links as links_mod

    co = _codex_link(tmp_path)
    launched = []
    monkeypatch.setattr(wake.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd) or _Proc())
    if where == "after_link_check":
        orig = links_mod.Links.check_wake

        def racing(self, *a, **k):
            out = orig(self, *a, **k)
            _rebind_ex(co)  # lands after the link lock is released, before the route reads the recipient
            return out

        monkeypatch.setattr(links_mod.Links, "check_wake", racing)
        monkeypatch.setattr(wake, "_queue_state", lambda th: ("clear", {}))
    else:
        def racing_queue(th):
            _rebind_ex(co)  # lands after the recipient was read, before the locked launch
            return "clear", {}

        monkeypatch.setattr(wake, "_queue_state", racing_queue)
    rec = wake.wake_recipient(co, "t", "w1", sender="lead", link_id="L1")
    assert launched == [], "codex exec resume was launched after the partner was rebound"
    assert rec["outcome"].startswith("refused") and "link_stale" in rec["outcome"], rec
    assert co.links.get("t", "L1")["state"] == "stale"


def test_claude_route_passes_the_link_pin_only_for_a_link_wake(tmp_path, monkeypatch):
    from mycelium_coord import bridge_link

    co = _co(tmp_path)
    _attach(co, "lead", "supervisor", "claude")
    _attach(co, "cl", "executor", "claude", session="cl-s")
    co.links.propose("t", "L2", "lead", "cl")
    co.links.confirm("t", "L2", "cl")
    seen = []

    def fake_notify(c, **kw):
        seen.append(kw)
        return {"message_id": kw["message_id"], "delivered": True, "state": "delivered"}

    monkeypatch.setattr(bridge_link, "notify_via_bridge", fake_notify)
    kw = dict(binding_id="b", controller_id="lead", expected_revision=1)
    _send(co, "w9", "lead", "cl", kind="task", link_id="L2")
    wake.wake_recipient(co, "t", "w9", sender="lead", link_id="L2", **kw)
    assert seen[-1]["link_pin"] == {"host": "claude", "session": "cl-s", "native_id": None}
    _send(co, "w10", "lead", "cl", kind="task")
    wake.wake_recipient(co, "t", "w10", sender="lead", **kw)
    assert "link_pin" not in seen[-1]


def test_claude_bridge_refuses_a_rebound_recipient_for_a_link_wake(tmp_path, monkeypatch):
    from mycelium_coord import bridge_link

    co = _co(tmp_path)
    _attach(co, "lead", "supervisor", "claude")
    _attach(co, "ex", "executor", "claude", session="s-old")
    old_pin = {"host": "claude", "session": "s-old", "native_id": None}
    _send(co, "w1", "lead", "ex", kind="task")
    _attach(co, "ex", "executor", "claude", session="s-new", allow_transition=True)
    monkeypatch.setattr(bridge_link, "_import_bridge", lambda: (object(), Exception))
    assert (
        _code(
            lambda: bridge_link.notify_via_bridge(
                co, task_id="t", message_id="w1", binding_id="b", controller_id="lead",
                expected_revision=1, link_pin=old_pin,
            )
        )
        == "link_stale"
    )


# ------------------------------------------------------------------ legacy tasks unchanged
def test_legacy_task_is_unchanged_by_the_link_code(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "utcnow", lambda: "2026-01-01T00:00:00Z")
    legacy = _co(
        tmp_path, features=["triangle-kinds", "send-capability"], name="legacy"
    )
    linked = _co(tmp_path, features=LINK_FEATS, name="linked")
    for co in (legacy, linked):
        _pair(co)
        _send(co, "m1", "lead", "ex", kind="task")
        _send(co, "m2", "ex", "lead", kind="progress")
        _send(co, "m3", "lead", "all")
        co.detach("t", "lead")
        _attach(co, "lead", "supervisor", "claude")
    for name in ("00000001-m1.json", "00000002-m2.json", "00000003-m3.json"):
        a = (legacy.store.root / "tasks" / "t" / "messages" / name).read_bytes()
        b = (linked.store.root / "tasks" / "t" / "messages" / name).read_bytes()
        assert a == b, (
            name
        )  # non-link operations are byte-identical with or without the feature
    assert not (legacy.store.root / "tasks" / "t" / "links").exists()
    assert (
        "protocol_features" in legacy.get_task("t")
        and "session-links" not in legacy.get_task("t")["protocol_features"]
    )
    p = legacy.store.read("tasks/t/participants/ex.json")
    assert not any("link" in k for k in p)
    audit = (legacy.store.root / "tasks" / "t" / "audit.jsonl").read_text()
    assert "link_" not in audit
    # a task created with no features at all has no links directory either
    plain = Coordinator(CoordStore(str(tmp_path / "plain")))
    plain.create_task("t", project="p", worktree_realpath="/wt")
    assert "protocol_features" not in plain.get_task("t")


# ------------------------------------------------------------------ CLI
def test_cli_link_round_trip_with_cap_files(tmp_path):
    co = _co(tmp_path)
    store = co.store.root
    cl, ce = tmp_path / "caps" / "lead.json", tmp_path / "caps" / "ex.json"
    for pid, role, host, wt, capf in (
        ("lead", "supervisor", "claude", "/out", cl),
        ("ex", "executor", "codex", "/wt", ce),
    ):
        r = _cli(
            store,
            "attach",
            "t",
            pid,
            "--role",
            role,
            "--worktree",
            wt,
            "--host",
            f"host={host}",
            "--host",
            f"session={pid}-sess",
            cap_file=capf,
        )
        assert r.returncode == 0, r.stderr + r.stdout
    r = _cli(
        store,
        "link",
        "propose",
        "t",
        "--id",
        "L1",
        "--from",
        "lead",
        "--to",
        "ex",
        cap_file=cl,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    assert json.loads(r.stdout)["state"] == "proposed"
    r = _cli(
        store, "link", "confirm", "t", "--id", "L1", "--as", "ex"
    )  # no capability file: refused
    assert r.returncode != 0 and "send_capability_required" in r.stdout + r.stderr
    r = _cli(store, "link", "confirm", "t", "--id", "L1", "--as", "ex", cap_file=ce)
    assert r.returncode == 0, r.stderr + r.stdout
    assert json.loads(r.stdout)["state"] == "active"
    base = [
        "send",
        "t",
        "--revision",
        "1",
        "--kind",
        "progress",
        "--text",
        "hi",
        "--from",
        "lead",
        "--link",
        "L1",
    ]
    ok = _cli(store, *base, "--id", "m1", "--to", "ex", cap_file=cl)
    assert ok.returncode == 0, ok.stderr + ok.stdout
    bad = _cli(store, *base, "--id", "m2", "--to", "all", cap_file=cl)
    assert (
        bad.returncode != 0
        and "link_requires_exact_recipient" in bad.stdout + bad.stderr
    )
    show = _cli(store, "link", "show", "t", "--id", "L1")
    assert show.returncode == 0 and json.loads(show.stdout)["state"] == "active"
    end = _cli(
        store,
        "link",
        "end",
        "t",
        "--id",
        "L1",
        "--as",
        "lead",
        "--reason",
        "done",
        cap_file=cl,
    )
    assert end.returncode == 0 and json.loads(end.stdout)["state"] == "ended"


# ================================================================== minor-defect repair round (two-modes-minor)
def _ag_link(co):
    _attach(co, "lead", "supervisor", "claude")
    _attach(co, "ag", "observer", "antigravity", native_id="agy-1", session=False)
    co.links.propose("t", "L1", "lead", "ag")
    return co.links.confirm("t", "L1", "ag")


def test_antigravity_member_rebound_to_another_role_makes_the_link_stale(tmp_path):
    co = _co(tmp_path)
    _ag_link(co)
    assert _send(co, "m0", "ag", "lead", kind="question", link_id="L1")["idempotent"] is False
    # same native id, same (null) session: the pin triple is unchanged, only the role moves
    _attach(co, "ag", "supervisor", "antigravity", native_id="agy-1", session=False, allow_transition=True)
    assert co.links.get("t", "L1")["state"] == "stale"
    assert co.links.get("t", "L1")["stale_reason"] == "member_role_changed:ag"
    assert _code(lambda: _send(co, "m1", "ag", "lead", kind="question", link_id="L1")) == "link_stale"
    assert _code(lambda: _send(co, "m2", "lead", "ag", kind="question", link_id="L1")) == "link_stale"


def test_stale_reason_alone_catches_a_role_change_without_the_attach_refresh(tmp_path):
    co = _co(tmp_path)
    _ag_link(co)
    rel = "tasks/t/participants/ag.json"
    p = co.store.read(rel)
    p["role"] = "supervisor"
    co.store.write(rel, p)  # a write path that did not call participant_changed
    assert co.links._stale_reason("t", co.store.read("tasks/t/links/L1.json")) == "member_role_changed:ag"
    assert _code(lambda: _send(co, "m1", "lead", "ag", kind="question", link_id="L1")) == "link_stale"


def test_antigravity_member_cannot_send_an_actionable_message_in_a_link(tmp_path):
    co = _co(tmp_path)
    _ag_link(co)
    with pytest.raises(ProtocolError) as e:
        co.send(task_id="t", message_id="a1", sender="ag", recipient="lead", kind="question", task_revision=1,
                text="x", actionable=True, link_id="L1")
    assert e.value.code == "link_antigravity_kind_refused"
    assert _send(co, "a2", "ag", "lead", kind="question", link_id="L1")["idempotent"] is False


def test_identical_send_replay_returns_the_stored_result_before_the_link_gate(tmp_path):
    co = _co(tmp_path)
    _active(co)
    first = _send(co, "m1", "lead", "ex", link_id="L1")
    assert first["idempotent"] is False
    _attach(co, "ex", "executor", "codex", session="elsewhere", allow_transition=True)
    assert co.links.get("t", "L1")["state"] == "stale"
    again = _send(co, "m1", "lead", "ex", link_id="L1")
    assert again["idempotent"] is True and again["seq"] == first["seq"]
    assert _code(lambda: _send(co, "m2", "lead", "ex", link_id="L1")) == "link_stale"
    # the same id with different content is still a conflict, not a replay
    assert _code(lambda: co.send(task_id="t", message_id="m1", sender="lead", recipient="ex", kind="progress",
                                 task_revision=1, text="different", link_id="L1")) == "idempotency_conflict"


def test_wake_replay_returns_the_prior_receipt_before_the_link_gate(tmp_path):
    co = _co(tmp_path)
    _active(co)
    _send(co, "w1", "lead", "ex", kind="task", link_id="L1")
    _send(co, "w2", "lead", "ex", kind="task", link_id="L1")
    prior = {"task_id": "t", "message_id": "w1", "request_id": "r", "route": "codex", "recipient": "ex",
             "outcome": wake.ACCEPTED, "at": "2026-01-01T00:00:00Z"}
    co.store.write(wake._receipt_rel("t", "w1"), prior)
    _attach(co, "ex", "executor", "codex", session="elsewhere", allow_transition=True)
    assert co.links.get("t", "L1")["state"] == "stale"
    rep = wake.wake_recipient(co, "t", "w1", sender="lead", link_id="L1")
    assert rep["idempotent_replay"] is True and rep["outcome"] == wake.ACCEPTED
    assert _code(lambda: wake.wake_recipient(co, "t", "w2", sender="lead", link_id="L1")) == "link_stale"


@pytest.mark.parametrize("bad", ["supervisor", "executor", "observer", "all"])
def test_propose_refuses_role_named_members(tmp_path, bad):
    co = _co(tmp_path)
    _attach(co, "lead", "supervisor", "claude")
    _attach(co, bad, "executor", "codex")
    for a, b in (("lead", bad), (bad, "lead")):
        assert _code(lambda: co.links.propose("t", "L1", a, b)) == "link_member_id_reserved"
    assert not (co.store.root / "tasks" / "t" / "links").exists() or not list(
        (co.store.root / "tasks" / "t" / "links").iterdir())
