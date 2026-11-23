"""Triangle (Antigravity as a third, advisory host): advisory guard, host-aware session ids,
opt-in message kinds, opt-in reply budget, antigravity wait shape, and old-reader compatibility.
Run: python -m pytest coordination/tests/test_triangle_advisory.py -q"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import Coordinator, ProtocolError, waitpath  # noqa: E402
from mycelium_coord.coord import _norm_sid, _session_seg, _sid_match  # noqa: E402
from mycelium_coord.execution import ExecutionManager  # noqa: E402
from mycelium_coord.model import KINDS  # noqa: E402
from mycelium_coord.store import CoordStore  # noqa: E402

AUTH = "/owner/auth.md"
ADV = "MYCELIUM_COORD_ADVISORY"


def _co(tmp_path, **task_kw):
    co = Coordinator(CoordStore(str(tmp_path / "store")))
    co.create_task("t", project="p", worktree_realpath="/project", revision=1, **task_kw)
    for pid, role, host in (("sup", "supervisor", "codex"), ("ex", "executor", "claude"),
                            ("adv", "observer", "antigravity")):
        co.attach("t", pid, role=role, worktree_realpath="/project" if pid == "ex" else "/outside",
                  host={"host": host, "session": pid, "native_id": pid})
    return co


def _send(co, mid, sender, kind="progress", to="all", **kw):
    return co.send(task_id="t", message_id=mid, sender=sender, recipient=to, kind=kind,
                   task_revision=1, text="x", **kw)


# ---------------------------------------------------------------- 1. advisory guard
def _calls(tmp_path):
    em = ExecutionManager(CoordStore(str(tmp_path / "store")))
    return {
        "execution_open": lambda: em.open_execution("t", execution_id="e", scope_ref="/s", authorization_ref=AUTH),
        "execution_unpause": lambda: em.unpause("t", authorization_ref=AUTH),
        "execution_change_limits": lambda: em.change_limits("t", authorization_ref=AUTH, changes={}),
        "execution_owner_request": lambda: em.owner_request("t", request_id="r", authorization_ref=AUTH,
                                                            scope_ref="/s"),
    }


@pytest.mark.parametrize("val", ["1", "true", "YES"])
def test_advisory_refuses_authority_ops_in_core(tmp_path, monkeypatch, val):
    _co(tmp_path)
    monkeypatch.setenv(ADV, val)
    for name, call in _calls(tmp_path).items():
        with pytest.raises(ProtocolError) as e:
            call()
        assert e.value.code == "advisory_process_refused", name
    from mycelium_coord.bridge_link import notify_via_bridge
    with pytest.raises(ProtocolError) as e:
        notify_via_bridge(None, task_id="t", message_id="m", binding_id="b", controller_id="c",
                          expected_revision=1)
    assert e.value.code == "advisory_process_refused"


def test_without_env_authority_ops_are_not_refused(tmp_path, monkeypatch):
    _co(tmp_path)
    monkeypatch.delenv(ADV, raising=False)
    monkeypatch.setenv(ADV, "0")  # falsy values do not enable the guard
    for name, call in _calls(tmp_path).items():
        try:
            call()
        except ProtocolError as e:
            assert e.code != "advisory_process_refused", name


def test_advisory_allows_read_and_nonactionable_send(tmp_path, monkeypatch):
    co = _co(tmp_path, protocol_features=["triangle-kinds"])
    monkeypatch.setenv(ADV, "1")
    _send(co, "m1", "adv", kind="consult_request", to="sup")
    assert co.inbox("t", "sup")["messages"][0]["message_id"] == "m1"


def test_advisory_refused_on_cli_path(tmp_path):
    env = {**os.environ, ADV: "1", "PYTHONPATH": str(ROOT / "coordination")}
    root = str(tmp_path / "s")
    cmds = [["exec-open", "t", "--execution-id", "e", "--scope", "/s", "--authorization", AUTH],
            ["exec-unpause", "t", "--authorization", AUTH],
            ["exec-owner-request", "t", "--request-id", "r", "--scope", "/s", "--authorization", AUTH],
            ["exec-change-limits", "t", "--authorization", AUTH, "--changes", "{}"],
            ["notify-via-bridge", "t", "m", "--binding-id", "b", "--controller-id", "c",
             "--expected-revision", "1"],
            ["job-run", "j1", "--", "true"],
            ["job-cancel", "j1"]]
    for c in cmds:
        r = subprocess.run([sys.executable, "-m", "mycelium_coord", "--root", root] + c, env=env,
                           text=True, capture_output=True, timeout=60)
        assert r.returncode == 3, (c, r.stdout, r.stderr)
        assert json.loads(r.stdout)["error"] == "advisory_process_refused", c


def test_mcp_advisory_tool_list_and_refusal(tmp_path):
    pytest.importorskip("fastmcp")
    code = ("import asyncio,json\nfrom mycelium_coord import mcp_server as m\n"
            "tools=asyncio.run(m.mcp.list_tools())\n"
            "names=[getattr(t,'name',t) for t in tools]\n"
            "print(json.dumps({'names':sorted(names),'removed':m._hide_authority_tools()}))\n")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "coordination"), "MYCELIUM_COORD_DIR": str(tmp_path / "s")}
    env.pop(ADV, None)
    plain = json.loads(subprocess.run([sys.executable, "-c", code], env=env, text=True,
                                      capture_output=True, timeout=120, check=True).stdout)
    assert "execution_open" in plain["names"]
    env[ADV] = "1"
    adv = json.loads(subprocess.run([sys.executable, "-c", code], env=env, text=True,
                                    capture_output=True, timeout=120, check=True).stdout)
    for n in ("execution_open", "execution_unpause", "execution_change_limits",
              "execution_owner_request", "coord_notify_via_bridge", "coord_wake"):
        assert n not in adv["names"], n
    assert "coord_send" in adv["names"] and "execution_read" in adv["names"]


# ---------------------------------------------------------------- 2. host-aware session ids
def test_claude_prefix_strip_only_for_claude():
    assert _norm_sid("Claude-AB12", "claude") == "ab12"
    assert _sid_match("claude-ab12", "ab12", "claude")
    assert _norm_sid("claude-ab12", "codex") == "claude-ab12"
    assert not _sid_match("claude-ab12", "ab12", "antigravity")
    assert _sid_match("AG-1", "ag-1", "antigravity")  # still lowercased
    assert not _sid_match("a", "", "claude") and not _sid_match(None, "a", "codex")


def test_session_resolution_uses_host_kind(tmp_path):
    co = _co(tmp_path)
    co.attach("t", "cl2", role="observer", worktree_realpath="/o",
              host={"host": "claude", "session": "claude-abc", "native_id": "n"})
    co.select_session("t", "cl2", host_kind="claude", session_id="abc")  # prefix-equivalent: ok
    assert co.resolve_session("claude", "claude-abc")["participant"] == "cl2"
    assert co.verify_participant_session("t", "cl2", "claude", "ABC")
    co.attach("t", "ag2", role="observer", worktree_realpath="/o",
              host={"host": "antigravity", "session": "claude-xyz", "native_id": "n"})
    assert co.verify_participant_session("t", "ag2", "antigravity", "claude-xyz")
    assert not co.verify_participant_session("t", "ag2", "antigravity", "xyz")
    with pytest.raises(ProtocolError):
        co.select_session("t", "ag2", host_kind="antigravity", session_id="xyz")


def test_bridge_session_match_host_aware():
    from mycelium_coord.bridge_link import _norm_session, _session_match
    assert _session_match("claude-ea7b", {"host": "claude", "session": "EA7B"})
    assert not _session_match("claude-ea7b", {"host": "codex", "session": "ea7b"})
    assert _norm_session("claude-x", "codex") == "claude-x"


# ---------------------------------------------------------------- 3. kinds
def test_kinds_registered():
    for k in ("review_request", "consult_request", "report"):
        assert k in KINDS


def test_new_kinds_need_task_opt_in(tmp_path):
    co = _co(tmp_path)  # no protocol_features
    for k in ("consult_request", "report", "review_request"):
        with pytest.raises(ProtocolError) as e:
            _send(co, "m-" + k, "adv", kind=k, to="sup")
        assert e.value.code == "kind_requires_feature"
    assert "protocol_features" not in co.get_task("t")  # default task.json is the base shape


def test_new_kinds_allowed_with_opt_in_and_nonactionable(tmp_path):
    co = _co(tmp_path, protocol_features=["triangle-kinds"])
    assert co.get_task("t")["protocol_features"] == ["triangle-kinds"]
    r = _send(co, "m1", "adv", kind="consult_request", to="sup")["message"]
    assert r["actionable"] is False
    assert _send(co, "m2", "adv", kind="report", to="sup")["message"]["actionable"] is False
    with pytest.raises(ProtocolError) as e:
        _send(co, "m3", "adv", kind="consult_request", to="sup", actionable=True)
    assert e.value.code == "invalid_actionable"
    assert _send(co, "m4", "sup", kind="review_request", to="ex")["message"]["actionable"] is True


def test_invalid_protocol_features_and_budget_rejected(tmp_path):
    co = Coordinator(CoordStore(str(tmp_path / "s")))
    for kw in ({"protocol_features": ["nope"]}, {"message_budget": {"per_x": 1}},
               {"message_budget": {"per_sender": -1}}, {"message_budget": {}}):
        with pytest.raises(ProtocolError):
            co.create_task("t", project="p", worktree_realpath="/p", **kw)


# ---------------------------------------------------------------- 4. reply budget
def test_no_budget_is_unchanged(tmp_path):
    co = _co(tmp_path)
    for i in range(10):
        _send(co, f"m{i}", "adv", correlation_id="c1")


def test_per_correlation_budget_with_supervisor_exempt(tmp_path):
    co = _co(tmp_path, message_budget={"per_correlation": 2})
    _send(co, "m1", "adv", correlation_id="c1")
    _send(co, "m2", "ex", correlation_id="c1")
    with pytest.raises(ProtocolError) as e:
        _send(co, "m3", "adv", correlation_id="c1")
    assert e.value.code == "message_budget_exceeded"
    _send(co, "m4", "sup", correlation_id="c1")  # supervisor exempt from (a)
    _send(co, "m5", "adv", correlation_id="c2")  # other correlation unaffected
    _send(co, "m6", "adv")  # no correlation: not counted
    # idempotent replay of a persisted message is not re-budgeted
    assert _send(co, "m1", "adv", correlation_id="c1")["idempotent"] is True


def test_per_sender_budget_counts_nonactionable_and_not_exempt_for_supervisor(tmp_path):
    co = _co(tmp_path, message_budget={"per_sender": 2})
    _send(co, "m1", "sup")
    _send(co, "m2", "sup")
    with pytest.raises(ProtocolError) as e:
        _send(co, "m3", "sup")
    assert e.value.code == "message_budget_exceeded"
    _send(co, "a1", "adv")  # budget is per sender
    # R1: actionable messages count toward (and are refused by) per_sender too
    with pytest.raises(ProtocolError) as e:
        _send(co, "t1", "sup", kind="task", to="ex")
    assert e.value.code == "message_budget_exceeded"


def test_budget_is_persisted_on_task_and_returned_on_idempotent_create(tmp_path):
    co = _co(tmp_path, message_budget={"per_correlation": 3, "per_sender": 40})
    assert co.get_task("t")["message_budget"] == {"per_correlation": 3, "per_sender": 40}
    again = co.create_task("t", project="p", worktree_realpath="/project", message_budget={"per_sender": 1})
    assert again["message_budget"]["per_sender"] == 40  # never reset


def test_cli_create_task_flags(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "coordination")}
    r = subprocess.run([sys.executable, "-m", "mycelium_coord", "--root", str(tmp_path / "s"), "create-task", "t",
                        "--project", "p", "--worktree", "/w", "--feature", "triangle-kinds",
                        "--message-budget", '{"per_correlation":3,"per_sender":40}'],
                       env=env, text=True, capture_output=True, timeout=60)
    t = json.loads(r.stdout)
    assert t["protocol_features"] == ["triangle-kinds"] and t["message_budget"]["per_sender"] == 40


# ---------------------------------------------------------------- 5. antigravity wait shape
def test_antigravity_wait_shape():
    p = waitpath.preferred_pattern("mcp", host="antigravity")
    assert p["host"] == "antigravity" and p["route"] == "mcp"
    assert p["inner_s"] == 25.0 and p["plan"]["applied_s"] == 25.0
    assert p["shell_route"] == "unmeasured" and "UNMEASURED" in p["call"]["note"]
    assert "host_yield_s" not in p["call"]["args"]
    # cli request on antigravity falls back to the MCP shape
    assert waitpath.preferred_pattern("cli", host="antigravity")["route"] == "mcp"
    chk = waitpath.check_wait("cli", 50, 60, host="antigravity")
    assert chk["ok"] is False and chk["shell_route"] == "unmeasured"
    assert waitpath.check_wait("mcp", 20, host="antigravity")["ok"] is True
    with pytest.raises(ValueError):
        waitpath.preferred_pattern("mcp", host="bogus")


def test_other_hosts_unchanged():
    base = waitpath.preferred_pattern("cli")
    assert waitpath.preferred_pattern("cli", host="claude") == base
    assert waitpath.preferred_pattern("mcp", host="codex") == waitpath.preferred_pattern("mcp")


# ---------------------------------------------------------------- old-reader compatibility
BASE_PKG = Path(os.environ.get("MYCELIUM_BASE_PKG") or ROOT / "docs/triangle/evidence/base-91546b3/mycelium_coord")

OLD_READER = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import mycelium_coord
assert not hasattr(mycelium_coord.model, "TRIANGLE_KINDS"), "not the base package"
from mycelium_coord import Coordinator
from mycelium_coord.store import CoordStore
co = Coordinator(CoordStore(sys.argv[2]))
out = {}
out["task"] = co.get_task("t")
out["parts"] = [p["participant_id"] for p in co.list_participants("t")]
inbox = co.inbox("t", "sup", compact=True)
out["inbox"] = [m["kind"] for m in inbox["messages"]]
full = co.inbox("t", "ex")
out["inbox_full"] = [m["kind"] for m in full["messages"]]
for m in co._all_messages("t"):
    if m["recipient"] in ("sup", "ex", "adv"):
        assert co.read_message("t", m["recipient"], m["message_id"])["kind"] == m["kind"]
out["resume"] = sorted(co.resume("t", "sup", compact=True).keys())
out["resume_full"] = sorted(co.resume("t", "ex", compact=False).keys())
out["state"] = co.message_state("t", "sup", "m1")
out["old_send_ok"] = co.send(task_id="t", message_id="old1", sender="ex", recipient="sup",
                             kind="progress", task_revision=1, text="old server on a new task")["seq"]
print(json.dumps(out))
"""


def test_old_reader_reads_everything_a_new_server_writes(tmp_path):
    if not (BASE_PKG / "model.py").exists():
        pytest.skip("base-commit package not available (set MYCELIUM_BASE_PKG)")
    co = _co(tmp_path, protocol_features=["triangle-kinds"],
             message_budget={"per_correlation": 3, "per_sender": 40})
    _send(co, "m1", "adv", kind="consult_request", to="sup", correlation_id="c1")
    _send(co, "m2", "adv", kind="report", to="sup", correlation_id="c1")
    _send(co, "m3", "sup", kind="review_request", to="ex", correlation_id="c2")
    _send(co, "m4", "sup", kind="progress", to="ex")
    r = subprocess.run([sys.executable, "-I", "-c", OLD_READER, str(BASE_PKG.parent), str(tmp_path / "store")],
                       text=True, capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["inbox"][:2] == ["consult_request", "report"]
    assert "review_request" in out["inbox_full"]
    assert out["task"]["protocol_features"] == ["triangle-kinds"]  # extra task fields survive a read
    assert out["old_send_ok"] > 4
    # a base-version SENDER refuses the new kinds (why they are opt-in per task)
    s = subprocess.run([sys.executable, "-I", "-c",
                        "import sys\nsys.path.insert(0,sys.argv[1])\n"
                        "from mycelium_coord import Coordinator\nfrom mycelium_coord.store import CoordStore\n"
                        "co=Coordinator(CoordStore(sys.argv[2]))\n"
                        "co.send(task_id='t',message_id='z',sender='adv',recipient='sup',kind='report',task_revision=1,text='x')\n",
                        str(BASE_PKG.parent), str(tmp_path / "store")],
                       text=True, capture_output=True, timeout=120)
    assert s.returncode != 0 and "invalid_kind" in s.stderr



# ---------------------------------------------------------------- R1-R5 repairs
def test_per_sender_counts_actionable_progress(tmp_path):
    co = _co(tmp_path, message_budget={"per_sender": 2})
    _send(co, "a1", "adv", actionable=True)
    _send(co, "a2", "adv", actionable=True)
    with pytest.raises(ProtocolError) as e:
        _send(co, "a3", "adv", actionable=True)
    assert e.value.code == "message_budget_exceeded"


def test_per_correlation_exempts_ack_and_completion_receipt(tmp_path):
    co = _co(tmp_path, message_budget={"per_correlation": 1, "per_sender": 50})
    _send(co, "q1", "ex", kind="question", correlation_id="c1")
    with pytest.raises(ProtocolError):
        _send(co, "q2", "ex", kind="question", correlation_id="c1")
    _send(co, "k1", "ex", kind="acknowledgment", correlation_id="c1", reply_to="q1")
    _send(co, "k2", "ex", kind="completion_receipt", correlation_id="c1", reply_to="q1",
          artifacts=["/tmp/x"])
    _send(co, "k3", "ex", kind="acknowledgment", correlation_id="c1", reply_to="q1")  # exempt: never counted


def test_per_sender_still_counts_exempt_kinds(tmp_path):
    co = _co(tmp_path, message_budget={"per_correlation": 9, "per_sender": 2})
    _send(co, "q0", "sup", kind="question", to="ex")
    _send(co, "k1", "ex", kind="acknowledgment", correlation_id="c1", reply_to="q0")
    _send(co, "k2", "ex", kind="completion_receipt", correlation_id="c1", reply_to="q0",
          artifacts=["/tmp/x"])
    with pytest.raises(ProtocolError) as e:
        _send(co, "k3", "ex", kind="acknowledgment", correlation_id="c1", reply_to="q0")
    assert e.value.code == "message_budget_exceeded"


def _refused(fn):
    with pytest.raises(ProtocolError) as e:
        fn()
    assert e.value.code == "advisory_process_refused"


def test_advisory_refusals_attach_send_checkpoint(tmp_path, monkeypatch):
    co = _co(tmp_path, protocol_features=["triangle-kinds"])
    _send(co, "root", "sup", kind="question", to="adv")  # supervisor's message, sent before the advisory process
    monkeypatch.setenv(ADV, "1")
    wt = "/outside"
    _refused(lambda: co.attach("t", "x1", role="supervisor", worktree_realpath=wt))
    _refused(lambda: co.attach("t", "x2", role="executor", worktree_realpath=wt))
    _refused(lambda: co.attach("t", "adv", role="observer", worktree_realpath=wt, allow_transition=True,
                               host={"host": "antigravity", "session": "other", "native_id": "o"}))
    for i, kind in enumerate(("task", "amendment", "review_request", "checkpoint_request",
                              "checkpoint_ready", "completion_receipt", "blocker")):
        _refused(lambda k=kind, i=i: _send(co, f"d{i}", "adv", kind=k, to="ex", reply_to="root"))
    _refused(lambda: _send(co, "act", "adv", kind="progress", actionable=True))
    _refused(lambda: co.publish_checkpoint("t", revision=1, participant_id="adv", checkpoint={"a": 1}))


def test_advisory_allows_observer_attach_and_listed_sends_and_reads(tmp_path, monkeypatch):
    co = _co(tmp_path, protocol_features=["triangle-kinds"])
    _send(co, "root", "sup", kind="question", to="adv")  # supervisor's message, sent before the advisory process
    monkeypatch.setenv(ADV, "1")
    co.attach("t", "adv", role="observer", worktree_realpath="/outside",
              host={"host": "antigravity", "session": "adv", "native_id": "adv"})
    co.create_task("t2", project="p", worktree_realpath="/project", revision=1)
    for i, kind in enumerate(("question", "progress", "review_finding", "acknowledgment",
                              "consult_request", "report")):
        extra = {"reply_to": "root"} if kind == "acknowledgment" else {}
        _send(co, f"s{i}", "adv", kind=kind, to="sup", **extra)
    assert len(co.inbox("t", "sup")["messages"]) == 6
    co.read_checkpoint("t")
    co.list_tasks()
    em = ExecutionManager(CoordStore(str(tmp_path / "store")))
    em.read_execution("t")
    em.status("t")
    em.receipt("t")


def test_advisory_refuses_every_execution_mutator(tmp_path, monkeypatch):
    _co(tmp_path)
    monkeypatch.setenv(ADV, "1")
    em = ExecutionManager(CoordStore(str(tmp_path / "store")))
    names = ["open_execution", "reserve", "settle", "claim_review", "settle_review", "record_evidence",
             "record_completion", "open_blocker", "resolve_blocker", "reserve_call", "settle_call",
             "request_shutdown", "note_expiry_shutdown", "reconcile_shutdown", "pause", "unpause",
             "change_limits", "owner_request", "set_phase", "claim_dispatch", "add_backlog"]
    for n in names:
        fn = getattr(em, n)
        with pytest.raises(ProtocolError) as e:
            fn("t")  # the gate fires before argument handling
        assert e.value.code == "advisory_process_refused", n


def test_advisory_refuses_jobs_and_workers(tmp_path, monkeypatch):
    from mycelium_coord.jobs import JobManager
    from mycelium_coord.workers import WorkerManager
    monkeypatch.setenv(ADV, "1")
    store = CoordStore(str(tmp_path / "store"))
    for call in (lambda: JobManager(store).run("j", ["true"]), lambda: JobManager(store).cancel("j"),
                 lambda: WorkerManager(store).run("w", str(tmp_path / "p"))):
        try:
            call()
        except TypeError:
            pytest.fail("gate must fire before argument handling")
        except ProtocolError as e:
            assert e.code == "advisory_process_refused"
        else:
            pytest.fail("not refused")


def test_check_wait_antigravity_ignores_host_yield():
    r = waitpath.check_wait("mcp", 50, host_yield_s=60, host="antigravity")
    assert r["ok"] is False and r["problem"]
    assert r["host"] == "antigravity" and r["shell_route"] == "unmeasured" and r["note"]
    assert waitpath.check_wait("mcp", 25, host_yield_s=60, host="antigravity")["ok"] is True
    assert waitpath.check_wait("mcp", 40, host_yield_s=60)["ok"] is True  # other hosts unchanged


def test_mcp_advisory_allowlist_and_core_refusal_when_removal_disabled(tmp_path):
    pytest.importorskip("fastmcp")
    code = r"""
import asyncio, json
from mycelium_coord import mcp_server as m
names = sorted(getattr(t, "name", t) for t in asyncio.run(m.mcp.list_tools()))
extra = sorted(set(names) - m.ADVISORY_ALLOWED_TOOLS)
print(json.dumps({"names": names, "extra": extra}))
"""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "coordination"), "MYCELIUM_COORD_DIR": str(tmp_path / "s"),
           ADV: "1"}
    out = json.loads(subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True,
                                    timeout=120, check=True).stdout)
    assert out["extra"] == [], out["extra"]
    for gone in ("coord_checkpoint_publish", "execution_pause", "execution_reserve", "coord_notify_via_bridge", "coord_wake"):
        assert gone not in out["names"]
    assert "coord_send" in out["names"]


def test_mcp_refused_tool_listed_when_removal_disabled_still_refused(tmp_path):
    pytest.importorskip("fastmcp")
    # Import with the advisory env unset => no tool removal (removal disabled). Then flip the env on:
    # the tool stays listed, and the CORE gate must still refuse the call over the in-process client.
    code = r"""
import asyncio, json, os
import mycelium_coord.mcp_server as ms
from fastmcp import Client
os.environ["MYCELIUM_COORD_ADVISORY"] = "1"
async def go():
    async with Client(ms.mcp) as c:
        names = [t.name for t in await c.list_tools()]
        try:
            await c.call_tool("execution_pause", {"task_id": "t", "authorization_ref": "/a.md"})
            err = None
        except Exception as e:
            err = str(e)
        return {"listed": "execution_pause" in names, "error": err}
print(json.dumps(asyncio.run(go())))
"""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "coordination"), "MYCELIUM_COORD_DIR": str(tmp_path / "s")}
    env.pop(ADV, None)
    r = subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["listed"] is True, out
    assert out["error"] and "advisory_process_refused" in out["error"], out


def test_advisory_refuses_backlog_and_not_in_allowlist(tmp_path, monkeypatch):
    from mycelium_coord import mcp_server
    _co(tmp_path)
    monkeypatch.setenv(ADV, "1")
    em = ExecutionManager(CoordStore(str(tmp_path / "store")))
    with pytest.raises(ProtocolError) as e:
        em.add_backlog("t", item="x")
    assert e.value.code == "advisory_process_refused"
    assert "execution_backlog" not in mcp_server.ADVISORY_ALLOWED_TOOLS


def test_advisory_detach_refused_for_non_observer(tmp_path, monkeypatch):
    co = _co(tmp_path)
    monkeypatch.setenv(ADV, "1")
    for pid in ("ex", "sup"):
        _refused(lambda pid=pid: co.detach("t", pid))
    monkeypatch.delenv(ADV)
    assert not any(r.get("detached") for r in co.list_participants("t") if r["participant_id"] != "adv")


def test_advisory_detach_then_observer_reattach_of_executor_id_refused(tmp_path, monkeypatch):
    co = _co(tmp_path)
    monkeypatch.setenv(ADV, "1")
    # even with the executor record detached (by a non-advisory actor), observer re-attach is refused
    monkeypatch.delenv(ADV)
    co.detach("t", "ex")
    monkeypatch.setenv(ADV, "1")
    _refused(lambda: co.attach("t", "ex", role="observer", worktree_realpath="/outside",
                               host={"host": "antigravity", "session": "adv", "native_id": "adv"}))
    # attached (non-detached) executor id as observer: also refused
    _refused(lambda: co.attach("t", "sup", role="observer", worktree_realpath="/outside",
                               host={"host": "antigravity", "session": "adv", "native_id": "adv"}))
    # observer of a different host/session cannot take over the adv id either
    _refused(lambda: co.attach("t", "adv", role="observer", worktree_realpath="/outside",
                               host={"host": "antigravity", "session": "intruder", "native_id": "i"}))


def test_advisory_observer_can_detach_and_reattach_itself(tmp_path, monkeypatch):
    co = _co(tmp_path)
    monkeypatch.setenv(ADV, "1")
    co.detach("t", "adv")
    p = co.attach("t", "adv", role="observer", worktree_realpath="/outside",
                  host={"host": "antigravity", "session": "adv", "native_id": "adv"})
    assert p["role"] == "observer" and not p["detached"]


def test_check_wait_antigravity_caller_can_only_lower_yield():
    r = waitpath.check_wait("mcp", 25, host_yield_s=10, host="antigravity")
    assert r["ok"] is False and r["problem"]
    assert waitpath.check_wait("mcp", 5, host_yield_s=10, host="antigravity")["ok"] is True
    assert waitpath.check_wait("mcp", 25, host_yield_s=100, host="antigravity")["ok"] is True


# ---------------------------------------------------------------- round 3: N1 selection capture
def _audit_lines(tmp_path):
    return sorted(
        (str(p), len(p.read_text().splitlines()))
        for p in (tmp_path / "store").rglob("audit.jsonl")
    )


def _ex_host(session="ex"):
    return {"host": "claude", "session": session, "native_id": "x"}


def test_n1_same_task_capture_refused_with_no_side_effects(tmp_path, monkeypatch):
    co = _co(tmp_path)
    before, audit0 = co.resolve_session("claude", "ex"), _audit_lines(tmp_path)
    assert before and before["participant"] == "ex"
    monkeypatch.setenv(ADV, "1")
    with pytest.raises(ProtocolError) as e:
        co.attach("t", "adv2", role="observer", worktree_realpath="/outside", host=_ex_host())
    assert e.value.code == "advisory_process_refused"
    monkeypatch.delenv(ADV)
    after = co.resolve_session("claude", "ex")
    assert after and after["participant"] == "ex" and after["task"] == "t"
    assert not [p for p in co.list_participants("t") if p["participant_id"] == "adv2"]
    assert _audit_lines(tmp_path) == audit0  # no new attach audit line


def test_n1_cross_task_capture_refused(tmp_path, monkeypatch):
    co = _co(tmp_path)
    monkeypatch.setenv(ADV, "1")
    co.create_task("t9", project="p", worktree_realpath="/elsewhere", revision=1)  # still allowed
    with pytest.raises(ProtocolError) as e:
        co.attach("t9", "spy", role="observer", worktree_realpath="/outside", host=_ex_host())
    assert e.value.code == "advisory_process_refused"
    monkeypatch.delenv(ADV)
    after = co.resolve_session("claude", "ex")
    assert after and after["task"] == "t" and after["participant"] == "ex"
    assert not [p for p in co.list_participants("t9") if p["participant_id"] == "spy"]


def test_n1_repeat_capture_after_executor_reattach_refused(tmp_path, monkeypatch):
    co = _co(tmp_path)
    co.attach("t", "ex", role="executor", worktree_realpath="/project",
              host={"host": "claude", "session": "ex", "native_id": "ex"})
    monkeypatch.setenv(ADV, "1")
    for _ in range(2):
        with pytest.raises(ProtocolError) as e:
            co.attach("t", "adv2", role="observer", worktree_realpath="/outside", host=_ex_host())
        assert e.value.code == "advisory_process_refused"
    monkeypatch.delenv(ADV)
    after = co.resolve_session("claude", "ex")
    assert after and after["participant"] == "ex"


def test_n1_same_advisory_observer_may_move_to_second_task(tmp_path, monkeypatch):
    co = _co(tmp_path)
    monkeypatch.setenv(ADV, "1")
    co.create_task("t2", project="p", worktree_realpath="/elsewhere", revision=1)
    assert co.resolve_session("antigravity", "adv")["task"] == "t"
    p = co.attach("t2", "adv-b", role="observer", worktree_realpath="/outside",
                  host={"host": "antigravity", "session": "adv", "native_id": "adv"})
    assert p["role"] == "observer"
    moved = co.resolve_session("antigravity", "adv")
    assert moved and moved["task"] == "t2" and moved["participant"] == "adv-b"


def test_n1_attach_over_detached_participants_selection_allowed(tmp_path, monkeypatch):
    co = _co(tmp_path)
    co.detach("t", "ex")  # non-advisory process
    monkeypatch.setenv(ADV, "1")
    p = co.attach("t", "adv3", role="observer", worktree_realpath="/outside", host=_ex_host())
    assert p["participant_id"] == "adv3"
    sel = co.resolve_session("claude", "ex")
    assert sel and sel["participant"] == "adv3"


def test_n1_non_advisory_last_attach_wins_unchanged(tmp_path):
    co = _co(tmp_path)
    co.attach("t", "obs2", role="observer", worktree_realpath="/outside", host=_ex_host())
    sel = co.resolve_session("claude", "ex")
    assert sel and sel["participant"] == "obs2"


def test_a2_advisory_backlog_on_open_execution_leaves_state_version(tmp_path, monkeypatch):
    _co(tmp_path)
    em = ExecutionManager(CoordStore(str(tmp_path / "store")))
    em.open_execution("t", execution_id="e", scope_ref="/s", authorization_ref=AUTH)
    v0 = em.read_execution("t")["state_version"]
    monkeypatch.setenv(ADV, "1")
    with pytest.raises(ProtocolError) as e:
        em.add_backlog("t", item="x")
    assert e.value.code == "advisory_process_refused"
    monkeypatch.delenv(ADV)
    rec = em.read_execution("t")
    assert rec["state_version"] == v0 and not rec.get("backlog")


# ---------------------------------------------------------------- round 4: N1 closure (both writers, exact record, canonical host kinds)
def _attach_adv(co, pid, host_kind, session, task="t"):
    return co.attach(task, pid, role="observer", worktree_realpath="/outside",
                     host={"host": host_kind, "session": session, "native_id": "x"})


def _assert_ex_selection_intact(co, session="ex", host="claude"):
    after = co.resolve_session(host, session)
    assert after and after["participant"] == "ex" and after["task"] == "t"


@pytest.mark.parametrize("host_kind", ["Claude", "CLAUDE", "cLaUdE", "claude ", " claude", "Codex", "codex-x", "Antigravity"])
def test_n1_non_canonical_host_kind_is_refused_native(tmp_path, monkeypatch, host_kind):
    # NATIVE (no store emulation): a case variant or any non-canonical host kind never reaches a write
    co = _co(tmp_path)
    audit0 = _audit_lines(tmp_path)
    monkeypatch.setenv(ADV, "1")
    for pid in ("adv2", "adv3"):  # also repeated with a fresh id
        with pytest.raises(ProtocolError) as e:
            _attach_adv(co, pid, host_kind, "ex")
        assert e.value.code == "advisory_process_refused"
    monkeypatch.delenv(ADV)
    _assert_ex_selection_intact(co)
    assert not [p for p in co.list_participants("t") if p["participant_id"] in ("adv2", "adv3")]
    assert _audit_lines(tmp_path) == audit0


def test_n1_host_case_variant_changes_nothing_in_the_store_tree(tmp_path, monkeypatch):
    co = _co(tmp_path)
    sess_dir = tmp_path / "store" / "sessions"
    names0 = sorted(str(p.relative_to(sess_dir)) for p in sess_dir.rglob("*"))
    monkeypatch.setenv(ADV, "1")
    with pytest.raises(ProtocolError) as e:
        _attach_adv(co, "adv2", "Claude", "ex")
    assert e.value.code == "advisory_process_refused"
    monkeypatch.delenv(ADV)
    assert sorted(str(p.relative_to(sess_dir)) for p in sess_dir.rglob("*")) == names0
    _assert_ex_selection_intact(co)


def test_n1_hashed_segment_name_as_session_is_refused_native(tmp_path, monkeypatch):
    # The victim's native id is not a valid store id, so its selection file is sessions/claude/h.<sha16>.json;
    # that file name is itself a valid session id. Supplying it must not reach the victim's record.
    co = Coordinator(CoordStore(str(tmp_path / "store")))
    co.create_task("t", project="p", worktree_realpath="/project", revision=1)
    victim = "ex session/1"
    co.attach("t", "ex", role="executor", worktree_realpath="/project",
              host={"host": "claude", "session": victim, "native_id": "ex"})
    assert co.resolve_session("claude", victim)["participant"] == "ex"
    seg = _session_seg(victim)
    assert seg != victim and seg.startswith("h.")
    monkeypatch.setenv(ADV, "1")
    for pid in ("adv2", "adv3"):
        with pytest.raises(ProtocolError) as e:
            _attach_adv(co, pid, "claude", seg)
        assert e.value.code == "advisory_process_refused"
    monkeypatch.delenv(ADV)
    after = co.resolve_session("claude", victim)
    assert after and after["participant"] == "ex"
    assert not [p for p in co.list_participants("t") if p["participant_id"].startswith("adv")]


@pytest.mark.parametrize("given", ["EX", "Ex", "claude-ex", "CLAUDE-EX"])
def test_n1_fallback_scan_match_is_protected(tmp_path, monkeypatch, given):
    # the victim's record is stored under a different file name than the exact path the write would use
    co = Coordinator(CoordStore(str(tmp_path / "store")))
    co.create_task("t", project="p", worktree_realpath="/project", revision=1)
    co.attach("t", "ex", role="executor", worktree_realpath="/project",
              host={"host": "claude", "session": "Ex", "native_id": "ex"})
    monkeypatch.setenv(ADV, "1")
    with pytest.raises(ProtocolError) as e:
        _attach_adv(co, "adv2", "claude", given)
    assert e.value.code == "advisory_process_refused"
    monkeypatch.delenv(ADV)
    assert co.resolve_session("claude", "Ex")["participant"] == "ex"
    assert co.resolve_session("claude", given)["participant"] == "ex"


def test_n1_stale_selection_compares_with_the_stored_records_own_host_and_session(tmp_path, monkeypatch):
    # the holder re-attached on ANOTHER session, so the old selection record is stale and not protected;
    # (the decision uses the record's own host/session, never the caller's)
    co = _co(tmp_path)
    co.attach("t", "ex", role="executor", worktree_realpath="/project", allow_transition=True,
              host={"host": "claude", "session": "ex2", "native_id": "ex"})
    monkeypatch.setenv(ADV, "1")
    p = _attach_adv(co, "adv2", "claude", "ex")  # old record (session ex) is stale
    assert p["participant_id"] == "adv2"
    monkeypatch.delenv(ADV)
    assert co.resolve_session("claude", "ex2")["participant"] == "ex"
    assert co.resolve_session("claude", "ex")["participant"] == "adv2"


def test_n1_select_session_writer_is_guarded_in_advisory_process(tmp_path, monkeypatch):
    co = _co(tmp_path)
    _attach_adv(co, "adv2", "claude", "mine")  # non-advisory attach of a second claude observer
    audit0 = _audit_lines(tmp_path)
    monkeypatch.setenv(ADV, "1")
    with pytest.raises(ProtocolError) as e:
        co.select_session("t", "adv2", host_kind="claude", session_id="ex")
    assert e.value.code == "advisory_process_refused"
    with pytest.raises(ProtocolError) as e:
        co.select_session("t", "adv2", host_kind="Claude", session_id="mine")
    assert e.value.code == "advisory_process_refused"
    monkeypatch.delenv(ADV)
    _assert_ex_selection_intact(co)
    assert co.resolve_session("claude", "mine")["participant"] == "adv2"
    assert _audit_lines(tmp_path) == audit0


def test_n1_select_session_same_identity_reaffirm_allowed_in_advisory(tmp_path, monkeypatch):
    co = _co(tmp_path)
    monkeypatch.setenv(ADV, "1")
    rec = co.select_session("t", "adv", host_kind="antigravity", session_id="adv")
    assert rec["participant_id"] == "adv"


def test_n1_non_advisory_attach_last_writer_wins_unchanged(tmp_path):
    co = _co(tmp_path)
    _attach_adv(co, "adv2", "claude", "mine")
    co.attach("t", "adv2", role="observer", worktree_realpath="/outside", allow_transition=True,
              host={"host": "claude", "session": "ex", "native_id": "x"})
    sel = co.resolve_session("claude", "ex")
    assert sel and sel["participant"] == "adv2"  # non-advisory behaviour is unchanged: last writer wins
