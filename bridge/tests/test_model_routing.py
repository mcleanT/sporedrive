"""Model routing (owner rule 2026-09-20): Fable is planning-only; implementation is explicit Opus by
default, Sonnet/Haiku on request, and never a silently inherited Fable or a reused Fable planning
session. SIMULATED (kind: simulated): FakeCLI screens/transcripts, a fake `cmux` executable for the
launcher, monkeypatched `ps` readers. No real cmux socket, no real ~/.claude, no model call.

Covers acceptance (1) of MODEL-ROUTING-R1:
  * the shared policy (`cmux_bridge.model_policy`) and its verbatim mirror in the installed skill
    script `src/codex/skills/cmux-driver/scripts/executor_session.py`;
  * bridge bind: an implementation WRITER binding cannot take a Fable session — by live footer
    (`model_policy`), by launch argv or transcript history (`planning_session`), or by an unreadable
    footer (`model_unverified`); default Opus and explicit Sonnet/Haiku bind and record their family;
    purpose=planning may bind Fable; a monitor records the verdict without enforcing it;
  * bridge submit: every submit re-reads the footer — a `/model` switch to Fable is refused before
    anything is typed (`model_policy`), any other switch is `model_changed` (re-bind);
  * discover reports argv/transcript model evidence and `fable_evidence` per target;
  * the launcher: default `--model opus`, explicit alternatives, Fable refused before any cmux call,
    pre-generated `--session-id`, `CLAUDE_CODE_SUBAGENT_MODEL` exported for the executor's workers,
    and the launched session is verified by its footer (requested vs resolved);
  * launch EFFORT (owner rule 2026-09-22): an Opus implementation launch that names none is
    launched at `--effort medium`, an explicit level always wins and is forwarded to the CLI and
    recorded in the receipt as REQUESTED (never as a verified runtime setting), a non-Opus or
    planner launch that names none passes no `--effort`, and an unsupported level is refused
    before any transport.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cmux_bridge import core
from cmux_bridge.core import Bridge, BridgeError, classify_screen
from cmux_bridge.model_policy import (
    ModelPolicyError,
    argv_model,
    check_session_model,
    footer_model,
    model_family,
    resolve_launch_model,
    transcript_model,
)
from cmux_bridge.state import StateStore

from tests.fakecli import FakeCLI, FakeClock, claude_screen, footer

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_launcher_module():
    """Import the installed-skill launcher script (hyphenated dir, not a package) for its
    launcher-only helpers. The model policy itself is imported from `cmux_bridge.model_policy`
    above and is test-enforced to be a verbatim mirror."""
    import importlib.util

    path = REPO_ROOT / "src" / "codex" / "skills" / "cmux-driver" / "scripts" / "executor_session.py"
    spec = importlib.util.spec_from_file_location("executor_session_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_launcher_mod = _load_launcher_module()
resolve_launch_effort = _launcher_mod.resolve_launch_effort
# the script mirrors the policy rather than importing it, so its ModelPolicyError is a distinct class
LauncherModelPolicyError = _launcher_mod.ModelPolicyError

REPO = Path(__file__).resolve().parents[2]
POLICY_MODULE = REPO / "bridge" / "cmux_bridge" / "model_policy.py"
LAUNCHER = REPO / "src" / "codex" / "skills" / "cmux-driver" / "scripts" / "executor_session.py"
FABLE = "Fable 5.1"
OPUS_ARGV = "claude --dangerously-skip-permissions --model opus --session-id 00000000-0000-4000-8000-000000000000"
FABLE_ARGV = "claude --dangerously-skip-permissions --model claude-fable-5-1[1m] --session-id 00000000-0000-4000-8000-000000000001"
BARE_ARGV = "claude --dangerously-skip-permissions"  # the owner's `clauded` alias: inherits settings


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CMUX_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CMUX_BRIDGE_CLAUDE_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.delenv("CMUX_BRIDGE_PASSWORD_FILE", raising=False)
    wt = tmp_path / "wt"
    wt.mkdir()
    cwd = os.path.realpath(str(wt))
    clk = FakeClock()
    cli = FakeCLI(clock=clk, cwd=cwd, transcript_root=tmp_path / "projects")
    store = StateStore(tmp_path / "state")
    bridge = Bridge(cli=cli, store=store, clock=clk, sleep=clk.sleep)
    holder = {"proc_start": "Tue Sep  8 11:47:38 2026", "argv": {}}
    monkeypatch.setattr(core.Bridge, "_proc_start", staticmethod(lambda pid: holder["proc_start"]))
    # per-pid argv: tests register what `ps -o command=` would print for a surface's pid
    monkeypatch.setattr(
        core.Bridge, "_proc_argv", staticmethod(lambda pid: holder["argv"].get(int(pid), OPUS_ARGV))
    )
    return SimpleNamespace(cli=cli, clk=clk, store=store, bridge=bridge, cwd=cwd, tmp_path=tmp_path, holder=holder)


def add_surface(env, model="Opus 5", pid=None, argv=None):
    pid = pid if pid is not None else (os.getpid() + len(env.cli.surfaces) + 1)
    u = env.cli.add_claude_surface(model=model, pid=pid)
    # a distinct pid per surface must still look alive: the fake pids are never real processes
    env.holder.setdefault("alive", set()).add(pid)
    if argv is not None:
        env.holder["argv"][pid] = argv
    return u


@pytest.fixture
def alive(env, monkeypatch):
    """Make the fake per-surface pids look alive to os.kill-based checks."""
    real_alive = core.Bridge._pid_alive

    def fake_alive(pid):
        if int(pid) in env.holder.get("alive", set()):
            return True
        return real_alive(pid)

    monkeypatch.setattr(core.Bridge, "_pid_alive", staticmethod(fake_alive))
    return env


def bind(env, surface, role="writer", **kw):
    s = env.cli.surfaces[surface]
    return env.bridge.bind(env.cli.workspace, s["uuid"], s["session_id"], s["pid"], env.cwd, "ctl-A", role, **kw)


def write_assistant(env, surface, model):
    env.cli._append_transcript(
        surface,
        {
            "type": "assistant",
            "message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": "ok"}]},
            "timestamp": env.cli._now_iso(),
            "sessionId": env.cli.surfaces[surface]["session_id"].removeprefix("claude-"),
        },
    )


# --------------------------------------------------------------------------- policy (pure)


@pytest.mark.parametrize(
    "spelling,family",
    [
        ("opus", "opus"),
        ("claude-opus-5", "opus"),
        ("Opus 5", "opus"),
        ("sonnet", "sonnet"),
        ("claude-sonnet-5", "sonnet"),
        ("Sonnet 5", "sonnet"),
        ("haiku", "haiku"),
        ("claude-haiku-4-5-20251001", "haiku"),
        ("Haiku 4.5", "haiku"),
        ("fable", "fable"),
        ("claude-fable-5-1", "fable"),
        ("claude-fable-5-1[1m]", "fable"),
        ("Fable 5.1", "fable"),
        ("Fable 5.1 (1M context)", "fable"),
        ("gpt-5.6-sol", None),
        ("opusx", None),
        ("", None),
        (None, None),
    ],
)
def test_model_family_recognises_alias_id_and_footer_spellings(spelling, family):
    assert model_family(spelling) == family


def test_footer_argv_transcript_readers():
    assert footer_model(claude_screen(model="Opus 5")) == "Opus 5"
    assert footer_model(claude_screen(model=FABLE)) == FABLE
    assert footer_model(["(base) user@host % "]) is None
    assert argv_model(OPUS_ARGV) == "opus"
    assert argv_model(FABLE_ARGV) == "claude-fable-5-1[1m]"
    assert argv_model("claude --model=sonnet") == "sonnet"
    assert argv_model(BARE_ARGV) is None  # inherited: nothing explicit was requested
    entries = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"model": "claude-fable-5-1[1m]"}},
        {"type": "assistant", "message": {"model": "claude-opus-5"}},
    ]
    assert transcript_model(entries) == "claude-opus-5"
    assert transcript_model(entries[:2]) == "claude-fable-5-1[1m]"
    assert transcript_model([]) is None


def test_launch_policy_defaults_to_opus_and_refuses_fable_for_implementation():
    r = resolve_launch_model("implementation", None)
    assert (r["model"], r["family"], r["defaulted"]) == ("opus", "opus", True)
    assert resolve_launch_model("implementation", "sonnet")["family"] == "sonnet"
    assert resolve_launch_model("implementation", "claude-haiku-4-5-20251001")["family"] == "haiku"
    assert resolve_launch_model("implementation", "claude-opus-5")["model"] == "claude-opus-5"
    for fable in ("fable", "claude-fable-5-1", "claude-fable-5-1[1m]", "Fable 5.1"):
        with pytest.raises(ModelPolicyError) as ei:
            resolve_launch_model("implementation", fable)
        assert ei.value.code == "model_policy"
    with pytest.raises(ModelPolicyError) as ei:
        resolve_launch_model("implementation", "gpt-5.6-sol")
    assert ei.value.code == "model_unknown"
    with pytest.raises(ModelPolicyError) as ei:
        resolve_launch_model("nonsense", "opus")
    assert ei.value.code == "bad_purpose"


def test_launch_policy_planning_needs_an_explicit_model_and_allows_fable():
    with pytest.raises(ModelPolicyError) as ei:
        resolve_launch_model("planning", None)
    assert ei.value.code == "model_required"  # no silent default for planning either
    assert resolve_launch_model("planning", "fable")["family"] == "fable"
    assert resolve_launch_model("planning", "opus")["family"] == "opus"


@pytest.mark.parametrize(
    "purpose,footer_,argv,transcript,expected,ok,code",
    [
        ("implementation", "Opus 5", "opus", "claude-opus-5", None, True, None),
        ("implementation", "Sonnet 5", "sonnet", None, None, True, None),
        ("implementation", "Haiku 4.5", None, None, None, True, None),
        ("implementation", "Opus 5", None, None, None, True, None),  # bare launch, but live on Opus
        ("implementation", FABLE, "claude-fable-5-1[1m]", None, None, False, "model_policy"),
        ("implementation", FABLE, None, None, None, False, "model_policy"),  # inherited Fable default
        ("implementation", "Opus 5", "claude-fable-5-1[1m]", None, None, False, "planning_session"),
        ("implementation", "Opus 5", "opus", "claude-fable-5-1[1m]", None, False, "planning_session"),
        ("implementation", None, "opus", None, None, False, "model_unverified"),
        ("implementation", "???", "opus", None, None, False, "model_unverified"),
        ("implementation", "Sonnet 5", "sonnet", None, "opus", False, "model_mismatch"),
        ("implementation", "Sonnet 5", "sonnet", None, "claude-sonnet-5", True, None),
        ("planning", FABLE, "fable", "claude-fable-5-1[1m]", None, True, None),
        ("planning", "Opus 5", None, None, None, True, None),
        ("planning", None, None, None, None, False, "model_unverified"),
        ("planning", FABLE, None, None, "opus", False, "model_mismatch"),
        ("bogus", "Opus 5", None, None, None, False, "bad_purpose"),
    ],
)
def test_check_session_model_table(purpose, footer_, argv, transcript, expected, ok, code):
    v = check_session_model(purpose, footer=footer_, argv=argv, transcript=transcript, expected=expected)
    assert (v["ok"], v["code"]) == (ok, code), v["reason"]
    assert v["evidence"]["footer"] == footer_


def _policy_block(path: Path) -> str:
    text = path.read_text()
    m = re.search(r"# --- model policy: begin ---\n(.*?)# --- model policy: end ---", text, re.S)
    assert m, f"policy markers missing in {path}"
    return m.group(1)


def test_launcher_mirrors_policy_module():
    """The installed skill script cannot import the bridge package, so it carries a verbatim copy of
    the policy; this keeps the two from drifting apart."""
    assert _policy_block(LAUNCHER) == _policy_block(POLICY_MODULE)


# --------------------------------------------------------------------------- bridge: bind


def test_classify_screen_reports_the_footer_model():
    assert classify_screen(claude_screen(model="Opus 5"))["model"] == "Opus 5"
    assert classify_screen(claude_screen(model=FABLE))["model"] == FABLE
    assert classify_screen(claude_screen(running=True, model="Sonnet 5"))["model"] == "Sonnet 5"


def test_writer_bind_refuses_a_fable_session_for_implementation_by_default(alive):
    env = alive
    fable = add_surface(env, model=FABLE, argv=FABLE_ARGV)
    with pytest.raises(BridgeError) as ei:
        bind(env, fable)  # purpose defaults to implementation
    assert ei.value.code == "model_policy"
    assert ei.value.detail["model"]["footer"] == FABLE
    assert "planning-only" in ei.value.message
    # nothing was leased or persisted for the refused session
    assert not list((env.tmp_path / "state").rglob("bindings/*.json"))
    assert not list((env.tmp_path / "state").rglob("leases/*.json"))


def test_writer_bind_refuses_the_focused_fable_planning_session_even_when_switched_to_opus(alive):
    """The owner's planning session: launched on Fable (argv) and answered on Fable (transcript),
    then `/model opus` typed into it. Its footer now says Opus, but it is still the planning session
    and is not reused for implementation. Focus/window play no part in the verdict."""
    env = alive
    planning = add_surface(env, model="Opus 5", argv=FABLE_ARGV)
    write_assistant(env, planning, "claude-fable-5-1[1m]")
    with pytest.raises(BridgeError) as ei:
        bind(env, planning)
    assert ei.value.code == "planning_session"
    assert ei.value.detail["model"]["argv"] == "claude-fable-5-1[1m]"
    assert ei.value.detail["model"]["transcript"] == "claude-fable-5-1[1m]"


def test_writer_bind_refuses_when_only_the_transcript_shows_fable(alive):
    env = alive
    s = add_surface(env, model="Opus 5", argv=BARE_ARGV)
    write_assistant(env, s, "claude-fable-5-1[1m]")
    with pytest.raises(BridgeError) as ei:
        bind(env, s)
    assert ei.value.code == "planning_session"


def test_writer_bind_refuses_an_unreadable_model_rather_than_assuming(alive):
    env = alive
    s = add_surface(env, model="Opus 5")
    rows = claude_screen(model="Opus 5")
    rows[rows.index(footer(17.0, "Opus 5"))] = footer(17.0, "???")
    env.cli.set_screen(s, rows)
    with pytest.raises(BridgeError) as ei:
        bind(env, s)
    assert ei.value.code == "model_unverified"


@pytest.mark.parametrize(
    "display,argv,family",
    [("Opus 5", OPUS_ARGV, "opus"), ("Sonnet 5", "claude --model sonnet", "sonnet"), ("Haiku 4.5", "claude --model=claude-haiku-4-5-20251001", "haiku")],
)
def test_writer_bind_admits_and_records_opus_sonnet_haiku(alive, display, argv, family):
    env = alive
    s = add_surface(env, model=display, argv=argv)
    b = bind(env, s)
    assert b["purpose"] == "implementation"
    assert b["model"]["family"] == family
    assert b["model"]["footer"] == display
    assert b["model"]["argv_present"] is True
    assert b["model"]["verified"] is True and b["model"]["code"] is None
    # the bind receipt carries the verdict too
    receipts = [json.loads(l) for l in (env.tmp_path / "state" / "receipts.jsonl").read_text().splitlines()] if (env.tmp_path / "state" / "receipts.jsonl").is_file() else []
    if receipts:
        last = receipts[-1]
        assert last.get("model_family", family) == family


def test_writer_bind_default_opus_session_from_the_launcher_shape(alive):
    """A session the launcher started: explicit --model opus in argv, Opus footer, Opus transcript."""
    env = alive
    s = add_surface(env, model="Opus 5", argv=OPUS_ARGV)
    write_assistant(env, s, "claude-opus-5")
    b = bind(env, s, expected_model="opus")
    assert b["model"]["transcript"] == "claude-opus-5" and b["model"]["expected"] == "opus"


def test_expected_model_must_match_the_live_footer(alive):
    env = alive
    s = add_surface(env, model="Sonnet 5", argv="claude --model sonnet")
    with pytest.raises(BridgeError) as ei:
        bind(env, s, expected_model="opus")
    assert ei.value.code == "model_mismatch"
    assert bind(env, s, expected_model="claude-sonnet-5")["model"]["family"] == "sonnet"


def test_planning_purpose_may_bind_fable_and_records_it(alive):
    env = alive
    fable = add_surface(env, model=FABLE, argv=FABLE_ARGV)
    b = bind(env, fable, purpose="planning")
    assert b["purpose"] == "planning" and b["model"]["family"] == "fable" and b["model"]["verified"] is True


def test_monitor_records_the_verdict_without_enforcing_it(alive):
    env = alive
    fable = add_surface(env, model=FABLE, argv=FABLE_ARGV)
    b = bind(env, fable, role="monitor")
    assert b["role"] == "monitor" and b["model"]["verified"] is False and b["model"]["code"] == "model_policy"


def test_bad_purpose_is_a_bad_request(alive):
    env = alive
    s = add_surface(env)
    with pytest.raises(BridgeError) as ei:
        bind(env, s, purpose="whatever")
    assert ei.value.code == "bad_request"


# --------------------------------------------------------------------------- bridge: submit


def test_submit_refuses_after_a_model_switch_to_fable_before_typing_anything(alive):
    env = alive
    s = add_surface(env, model="Opus 5", argv=OPUS_ARGV)
    b = bind(env, s)
    env.cli.set_model(s, FABLE)  # `/model fable` typed into the session after the bind
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], "r-1", "implement the thing", 1)
    assert ei.value.code == "model_policy"
    assert env.cli.send_count == 0 and env.cli.enter_count == 0
    # no request record was reserved either: the refusal happened before the reservation
    assert not list((env.tmp_path / "state").rglob("requests/*/*.json"))


def test_submit_refuses_any_other_model_change_until_rebound(alive):
    env = alive
    s = add_surface(env, model="Opus 5", argv=OPUS_ARGV)
    b = bind(env, s)
    env.cli.set_model(s, "Sonnet 5")
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], "r-1", "implement the thing", 1)
    assert ei.value.code == "model_changed"
    assert env.cli.send_count == 0
    # back on the bound model: the same request now goes through and is accepted exactly
    env.cli.set_model(s, "Opus 5")
    out = env.bridge.submit(b["binding_id"], "r-1", "implement the thing", 1)
    assert out["status"] == "accepted" and env.cli.enter_count == 1
    # re-binding verifies the new model and admits Sonnet for implementation
    env.cli.set_model(s, "Sonnet 5")
    b2 = bind(env, s)
    assert b2["model"]["family"] == "sonnet"


def test_submit_on_a_planning_binding_still_refuses_a_model_change(alive):
    env = alive
    fable = add_surface(env, model=FABLE, argv=FABLE_ARGV)
    b = bind(env, fable, purpose="planning")
    env.cli.set_model(fable, "Opus 5")
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], "r-1", "plan the thing", 1)
    assert ei.value.code == "model_changed"


def test_legacy_binding_without_model_record_is_held_to_the_live_footer(alive):
    """A binding persisted by an older bridge (no purpose/model keys) is treated as implementation
    on the live footer alone: Opus passes, Fable is refused."""
    env = alive
    s = add_surface(env, model="Opus 5", argv=OPUS_ARGV)
    b = bind(env, s)
    rel = f"bindings/{b['binding_id']}.json"
    rec = env.store.read(rel)
    rec.pop("purpose")
    rec.pop("model")
    env.store.write(rel, rec)
    assert env.bridge.submit(b["binding_id"], "r-1", "ok on opus", 1)["status"] == "accepted"
    env.cli.set_model(s, FABLE)
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], "r-2", "not on fable", 2)
    assert ei.value.code == "model_policy"


# --------------------------------------------------------------------------- bridge: discover


def test_discover_reports_model_evidence_per_target(alive):
    env = alive
    opus = add_surface(env, model="Opus 5", argv=OPUS_ARGV)
    fable = add_surface(env, model=FABLE, argv=FABLE_ARGV)
    write_assistant(env, fable, "claude-fable-5-1[1m]")
    d = env.bridge.discover()
    by_surface = {t["surface_uuid"]: t for t in d["targets"]}
    o, f = by_surface[opus], by_surface[fable]
    assert o["model"]["argv_family"] == "opus" and o["model"]["fable_evidence"] is False
    assert f["model"]["argv_family"] == "fable" and f["model"]["transcript_family"] == "fable"
    assert f["model"]["fable_evidence"] is True


def test_server_bind_tool_exposes_purpose_and_expected_model(env, monkeypatch):
    pytest.importorskip("fastmcp")
    import asyncio

    from fastmcp import Client
    from cmux_bridge import server

    monkeypatch.setattr(server, "_bridge", lambda: env.bridge)

    async def go():
        async with Client(server.mcp) as c:
            return await c.list_tools()

    tools = {t.name: t for t in asyncio.run(go())}
    props = tools["bridge_bind"].inputSchema["properties"]
    assert props["purpose"]["default"] == "implementation"
    assert "expected_model" in props
    assert "planning-only" in (tools["bridge_bind"].description or "")


# --------------------------------------------------------------------------- launcher (skill script)

FAKE_CMUX = r'''#!/usr/bin/env python3
"""Fake cmux for the launcher tests: answers only the commands the launcher sends, from WORLD."""
import json, os, sys
world = json.load(open(os.environ["FAKE_CMUX_WORLD"]))
log = open(os.environ["FAKE_CMUX_LOG"], "a")
args = sys.argv[1:]
log.write(json.dumps(args) + "\n"); log.flush()
cmd = args[0] if args else ""
if cmd == "ping":
    print("pong"); sys.exit(0)
if cmd == "new-workspace":
    world["created"] = args
    json.dump(world, open(os.environ["FAKE_CMUX_WORLD"], "w"))
    print("OK workspace:9"); sys.exit(0)
if cmd == "tree":
    print(json.dumps({"windows": [{"ref": "window:1", "workspaces": [{"ref": "workspace:9", "id": world["ws"]}]}]})); sys.exit(0)
if cmd == "list-pane-surfaces":
    print(json.dumps({"surfaces": [{"id": world["surface"], "ref": "surface:12", "type": "terminal", "title": "claude"}]})); sys.exit(0)
if cmd == "workspace" and args[1:2] == ["env"]:
    print("CLAUDE_CODE_SUBAGENT_MODEL=***"); sys.exit(0)
if cmd == "read-screen":
    print("\n".join(world["screen"])); sys.exit(0)
print("Error: not_found: unsupported in fake", file=sys.stderr); sys.exit(1)
'''


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    fake = tmp_path / "cmux"
    fake.write_text(FAKE_CMUX)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    world = tmp_path / "world.json"
    log = tmp_path / "cmux.log"
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    wt = tmp_path / "wt"
    wt.mkdir()
    ws, surface = "11111111-2222-4333-8444-555555555555", "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"
    env = {
        **os.environ,
        "FAKE_CMUX_WORLD": str(world),
        "FAKE_CMUX_LOG": str(log),
        "CMUX_BRIDGE_STATE_DIR": str(tmp_path / "state"),
        "CMUX_BRIDGE_CLAUDE_PROJECTS": str(tmp_path / "projects"),
    }

    def set_world(model="Opus 5", screen=None):
        world.write_text(json.dumps({"ws": ws, "surface": surface, "screen": screen or claude_screen(model=model)}))

    def run(*argv, timeout=60):
        return subprocess.run(
            [sys.executable, str(LAUNCHER), *argv], capture_output=True, text=True, timeout=timeout, env=env
        )

    def calls():
        return [json.loads(l) for l in log.read_text().splitlines()] if log.is_file() else []

    set_world()
    return SimpleNamespace(run=run, calls=calls, set_world=set_world, cmux=str(fake), claude=str(claude), wt=str(wt), ws=ws, surface=surface, tmp=tmp_path)


def _launch(L, *extra):
    return L.run("launch", "--cwd", L.wt, "--cmux", L.cmux, "--claude-bin", L.claude, "--wait-s", "5", *extra)


def test_launch_effort_defaults_to_medium_only_for_opus_implementation():
    """Opus implementation launches name their effort; everything else keeps its own default."""
    assert resolve_launch_effort("implementation", "opus", None) == {
        "effort": "medium",
        "defaulted": True,
        "source": "opus_default",
    }
    for fam in ("sonnet", "haiku"):
        assert resolve_launch_effort("implementation", fam, None)["effort"] is None
    assert resolve_launch_effort("planning", "fable", None)["effort"] is None


def test_launch_effort_explicit_choice_wins_for_every_family():
    for purpose, fam in (("implementation", "opus"), ("implementation", "sonnet"), ("planning", "fable")):
        for level in ("low", "medium", "high", "xhigh", "max"):
            got = resolve_launch_effort(purpose, fam, level)
            assert got == {"effort": level, "defaulted": False, "source": "requested"}
    # spelling is normalized, an empty value is "not requested"
    assert resolve_launch_effort("implementation", "opus", " HIGH ")["effort"] == "high"
    assert resolve_launch_effort("implementation", "sonnet", "")["effort"] is None


def test_launch_effort_rejects_an_unsupported_level():
    with pytest.raises(LauncherModelPolicyError) as ei:
        resolve_launch_effort("implementation", "opus", "turbo")
    assert ei.value.code == "bad_effort"


def test_launcher_forwards_an_explicit_effort_and_records_it_in_the_receipt(launcher):
    r = _launch(launcher, "--effort", "high")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["requested"]["effort"] == "high" and out["requested"]["effort_defaulted"] is False
    assert out["requested"]["effort_source"] == "requested"
    argv = [c for c in launcher.calls() if c[0] == "new-workspace"][0]
    cmd = argv[argv.index("--command") + 1]
    assert cmd.endswith("--effort high")
    # the receipt on disk records the REQUESTED effort; nothing claims the running session verified it
    rp = Path(out["receipt_path"])
    assert json.loads(rp.read_text())["requested"]["effort"] == "high"
    assert "effort" not in out["resolved"]


def test_launcher_omits_effort_for_a_non_opus_launch_that_names_none(launcher):
    launcher.set_world(model="Sonnet 5")
    r = _launch(launcher, "--model", "sonnet")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["requested"]["effort"] is None and out["requested"]["effort_source"] == "unset"
    argv = [c for c in launcher.calls() if c[0] == "new-workspace"][0]
    assert "--effort" not in argv[argv.index("--command") + 1]


def test_launcher_rejects_an_unsupported_effort_before_touching_cmux(launcher):
    r = _launch(launcher, "--effort", "turbo")
    assert r.returncode == 2, r.stdout + r.stderr  # argparse choices: refused before any transport
    assert "--effort" in r.stderr and launcher.calls() == []


def test_launcher_refuses_fable_for_implementation_before_touching_cmux(launcher):
    r = _launch(launcher, "--model", "fable")
    assert r.returncode == 3, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["error"] == "model_policy" and out["launched"] is False
    assert launcher.calls() == []  # not even a ping


def test_launcher_refuses_a_fable_subagent_model_too(launcher):
    r = _launch(launcher, "--subagent-model", "claude-fable-5-1")
    assert r.returncode == 3
    assert json.loads(r.stdout)["error"] == "model_policy"
    assert launcher.calls() == []


def test_launcher_launches_explicit_opus_by_default_and_verifies_the_footer(launcher):
    r = _launch(launcher)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] is True and out["requested"]["model"] == "opus" and out["requested"]["model_defaulted"] is True
    assert out["resolved"]["footer_model"] == "Opus 5" and out["resolved"]["model_verified"] is True
    sid = out["identity"]["session_id"]
    assert re.fullmatch(r"[0-9a-f-]{36}", sid)
    assert out["identity"]["workspace_uuid"] == launcher.ws and out["identity"]["surface_uuid"] == launcher.surface
    created = [c for c in launcher.calls() if c[0] == "new-workspace"]
    assert len(created) == 1
    argv = created[0]
    cmd = argv[argv.index("--command") + 1]
    assert cmd == (
        f"{launcher.claude} --dangerously-skip-permissions --model opus --session-id {sid} --effort medium"
    )
    assert "clauded" not in cmd and "--window" not in argv  # no alias, no window placement by default
    assert argv[argv.index("--focus") + 1] == "false"
    envs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert "CLAUDE_CODE_SUBAGENT_MODEL=sonnet" in envs and "SPOREDRIVE_SESSION_PURPOSE=implementation" in envs
    # the launch receipt is on disk, under the bridge state dir
    rp = Path(out["receipt_path"])
    assert rp.is_file() and json.loads(rp.read_text())["identity"]["session_id"] == sid
    # nothing was typed into the new surface
    assert not any(c[0] in ("send", "send-key", "set-buffer", "paste-buffer") for c in launcher.calls())


@pytest.mark.parametrize("model,display,family", [("sonnet", "Sonnet 5", "sonnet"), ("claude-haiku-4-5-20251001", "Haiku 4.5", "haiku")])
def test_launcher_routes_explicit_alternatives(launcher, model, display, family):
    launcher.set_world(model=display)
    r = _launch(launcher, "--model", model, "--name", "exec-alt", "--focus", "true", "--window", "window:1")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["requested"]["family"] == family and out["resolved"]["footer_family"] == family and out["ok"] is True
    argv = [c for c in launcher.calls() if c[0] == "new-workspace"][0]
    assert f"--model {model} " in argv[argv.index("--command") + 1]
    assert argv[argv.index("--window") + 1] == "window:1" and argv[argv.index("--focus") + 1] == "true"


def test_launcher_reports_a_session_that_resolved_to_the_wrong_model_as_unverified(launcher):
    """Requested Opus, but the footer says Fable (e.g. a wrapper ignored --model): exit 4, ok=false,
    identity still reported so the operator can inspect — never silently usable."""
    launcher.set_world(model=FABLE)
    r = _launch(launcher)
    assert r.returncode == 4
    out = json.loads(r.stdout)
    assert out["ok"] is False and out["resolved"]["footer_model"] == FABLE and out["resolved"]["model_verified"] is False
    assert "do NOT use" in out["resolved"]["note"]


def test_launcher_reports_a_trust_dialog_as_modal_and_never_confirms_it(launcher):
    from tests.fakecli import modal_screen

    launcher.set_world(screen=modal_screen())
    r = _launch(launcher)
    assert r.returncode == 4
    out = json.loads(r.stdout)
    assert out["resolved"]["state"] == "modal" and out["ok"] is False
    assert not any(c[0] in ("send", "send-key") for c in launcher.calls())


def test_launcher_never_starts_a_second_process_on_an_existing_session(launcher):
    sid = "12345678-1234-4123-8123-123456789abc"
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(launcher.wt))
    tp = launcher.tmp / "projects" / slug / f"{sid}.jsonl"
    tp.parent.mkdir(parents=True)
    tp.write_text("{}\n")
    r = _launch(launcher, "--session-id", sid)
    assert r.returncode == 2 and json.loads(r.stdout)["error"] == "session_exists"
    assert launcher.calls() == []


def test_launcher_dry_run_launches_nothing(launcher):
    r = _launch(launcher, "--dry-run")
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["dry_run"] is True and out["launched"] is False and "--model opus" in out["launch"]["command"]
    assert launcher.calls() == []


def test_launcher_verify_applies_the_same_policy_to_an_existing_session(launcher, monkeypatch):
    # footer Fable -> refused (exit 3); footer Opus -> ok (exit 0); unreadable -> exit 4
    launcher.set_world(model=FABLE)
    r = launcher.run("verify", "--surface", launcher.surface, "--pid", str(os.getpid()), "--cmux", launcher.cmux, "--cwd", launcher.wt)
    assert r.returncode == 3, r.stdout + r.stderr
    assert json.loads(r.stdout)["code"] == "model_policy"
    launcher.set_world(model="Opus 5")
    r = launcher.run("verify", "--surface", launcher.surface, "--pid", str(os.getpid()), "--cmux", launcher.cmux, "--cwd", launcher.wt)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] is True and out["family"] == "opus" and out["evidence"]["argv_present"] is True
    launcher.set_world(screen=["(base) user@host % "])
    r = launcher.run("verify", "--surface", launcher.surface, "--pid", str(os.getpid()), "--cmux", launcher.cmux, "--cwd", launcher.wt)
    assert r.returncode == 4 and json.loads(r.stdout)["code"] == "model_unverified"
    # refs are not identities
    r = launcher.run("verify", "--surface", "surface:12", "--pid", "1", "--cmux", launcher.cmux)
    assert r.returncode == 2


def test_launcher_policy_subcommand_matches_the_module():
    r = subprocess.run([sys.executable, str(LAUNCHER), "policy"], capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)["model"] == "opus"
    r = subprocess.run([sys.executable, str(LAUNCHER), "policy", "--model", "claude-fable-5-1[1m]"], capture_output=True, text=True)
    assert r.returncode == 3 and json.loads(r.stdout)["code"] == "model_policy"


# Rows captured verbatim from the live canary launch on 2026-09-21T04:29Z (folder-trust dialog):
# its own " ❯ No, exit" cursor (default = EXIT) is not an input prompt, and the launcher must report
# `modal` — not `not_claude` — so the operator confirms the dialog deliberately (Down, Enter).
REAL_TRUST_DIALOG = [
    " Quick safety check: Is this a project you created or one you trust? (Like your own code, a",
    " well-known open source project, or work from your team). If not, take a moment to review what's",
    " in this folder first.",
    "",
    " Claude Code'll be able to read, edit, and execute files here.",
    "",
    " Security guide",
    "",
    " ❯ No, exit",
    "   Yes, I trust this folder",
    "",
    " Enter to confirm · Esc to cancel",
]


def test_launcher_classifies_the_real_trust_dialog_as_modal(launcher):
    launcher.set_world(screen=REAL_TRUST_DIALOG)
    r = _launch(launcher)
    assert r.returncode == 4
    out = json.loads(r.stdout)
    assert out["resolved"]["state"] == "modal" and out["ok"] is False
    assert "dialog" in out["resolved"]["note"]
    assert not any(c[0] in ("send", "send-key") for c in launcher.calls())


def test_launcher_receipt_file_names_itself(launcher):
    r = _launch(launcher, "--receipt", str(launcher.tmp / "r" / "launch.json"))
    assert r.returncode == 0
    out = json.loads(r.stdout)
    on_disk = json.loads(Path(out["receipt_path"]).read_text())
    assert on_disk["receipt_path"] == out["receipt_path"] == str(launcher.tmp / "r" / "launch.json")
