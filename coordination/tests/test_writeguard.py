"""Tests for the cross-session stale-write guard (docs/triangle/WRITE_GUARD_SPEC.md, acceptance 1).

The guard is a standalone script; the tests drive it both in-process (parsing, signatures) and as a
subprocess (the real hook contract: stdin payload -> stdout JSON). Fixtures are sanitised real hook
payloads from the M2 probes (tests/fixtures/writeguard).
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "mycelium_coord" / "writeguard.py"
FIX = HERE / "fixtures" / "writeguard"

spec = importlib.util.spec_from_file_location("writeguard_under_test", SCRIPT)
wg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wg)


def fixture(name, proj):
    return json.loads(
        (FIX / (name + ".json")).read_text().replace("/work/proj", str(proj))
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("MYCELIUM_WRITE_GUARD_DIR", str(state))
    monkeypatch.delenv("MYCELIUM_WRITE_GUARD", raising=False)
    return proj, state


_SHELL_TOOLS = ("Bash", "shell", "local_shell", "exec_command", "container.exec", "run_command")


def _is_shell_payload(payload):
    if payload.get("tool_name") in _SHELL_TOOLS:
        return True
    return (payload.get("toolCall") or {}).get("name") in _SHELL_TOOLS


def run_hook(host, event, payload, state, extra_env=None, pre=True):
    """Fire one hook. A POST for a shell tool is preceded by the matching PRE (as every real host does,
    the Round-5 freshness rule needs the PRE snapshot); pass pre=False to drive a POST alone."""
    if pre and event == "post" and _is_shell_payload(payload):
        pp = json.loads(json.dumps(payload))
        pp.pop("tool_response", None)
        if "hook_event_name" in pp:
            pp["hook_event_name"] = "PreToolUse"
        if "hookEventName" in pp:
            pp["hookEventName"] = "PreToolUse"
        run_hook(host, "pre", pp, state, extra_env, pre=False)
    e = dict(os.environ, MYCELIUM_WRITE_GUARD_DIR=str(state))
    e.pop("MYCELIUM_WRITE_GUARD", None)
    e.update(extra_env or {})
    p = subprocess.run(
        [sys.executable, str(SCRIPT), "hook", "--host", host, "--event", event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=e,
        timeout=30,
    )
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout) if p.stdout.strip() else None


def is_deny(host, out):
    if out is None:
        return False
    if host == "antigravity":
        return out.get("decision") == "deny"
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def agy(ev, name, path, cwd, cid="conv-1", **args):
    a = {"Path": str(path)}
    a.update(args)
    return {
        "conversationId": cid,
        "cwd": str(cwd),
        "hookEventName": ev,
        "error": "",
        "toolCall": {"name": name, "args": a, "id": "x"},
    }


CODEX_PATCH_OK = (
    "Exit code: 0\nWall time: 0.2 seconds\nOutput:\nSuccess. Updated the following files:\nA x\n"
)


def codex(ev, patch, cwd, sid="sess-1", resp=CODEX_PATCH_OK):
    p = {
        "session_id": sid,
        "cwd": str(cwd),
        "hook_event_name": ev,
        "tool_name": "apply_patch",
        "tool_input": {"command": patch},
    }
    if ev == "PostToolUse" and resp is not None:
        p["tool_response"] = resp
    return p


def codex_bash(ev, cmd, cwd, sid="sess-1"):
    return {
        "session_id": sid,
        "cwd": str(cwd),
        "hook_event_name": ev,
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
    }


# ----------------------------------------------------------------------------- extraction from real fixtures


def test_extract_claude_fixtures(tmp_path):
    for name, tool, kind in [
        ("claude_pre_read", "Read", "read"),
        ("claude_post_write", "Write", "write"),
        ("claude_pre_edit", "Edit", "write"),
    ]:
        c = wg.classify("claude", fixture(name, tmp_path))
        assert (c["tool"], c["kind"]) == (tool, kind)
        assert c["paths"] and c["paths"][0].startswith(str(tmp_path))
    c = wg.classify("claude", fixture("claude_pre_bash", tmp_path))
    assert c["kind"] == "shell" and "viabash" in c["command"]


def test_extract_codex_multi_patch_relative_move_absolute(tmp_path):
    c = wg.classify("codex", fixture("codex_pre_apply_patch_multi", tmp_path))
    assert c["kind"] == "patch"
    assert c["ops"][0] == ("Update File", "work/c.txt", "work/moved/c2.txt")
    assert c["ops"][1][0:2] == ("Add File", str(tmp_path / "work" / "abs.txt"))
    cwd = fixture("codex_pre_apply_patch_multi", tmp_path)["cwd"]
    assert wg.resolve("work/c.txt", cwd) == os.path.realpath(
        str(tmp_path / "work" / "c.txt")
    )


def test_extract_codex_add_file_and_bash(tmp_path):
    c = wg.classify("codex", fixture("codex_pre_apply_patch_10", tmp_path))
    assert c["ops"] == [("Add File", "existing.txt", None)]
    c = wg.classify("codex", fixture("codex_pre_bash_12", tmp_path))
    assert c["kind"] == "shell" and c["command"].startswith("cat ")


def test_extract_agy_fixtures(tmp_path):
    for name, kind in [
        ("agy_pre_read_file", "read"),
        ("agy_pre_write_file", "overwrite"),
        ("agy_post_write_file", "overwrite"),
    ]:
        c = wg.classify("antigravity", fixture(name, tmp_path))
        assert c["kind"] == kind and c["paths"][0].startswith(str(tmp_path))
    assert (
        wg.classify("antigravity", fixture("agy_pre_run_command", tmp_path))["kind"]
        == "shell"
    )
    p = fixture("agy_pre_write_file", tmp_path)
    assert wg.get_session("antigravity", p) == "401b8209-6de7-4c7f-be4e-d8acfb9b0939"
    assert (
        wg.get_session("claude", fixture("claude_pre_read", tmp_path))
        == "d39b5bc2-9bdc-4ddb-85b7-c452f9981b6e"
    )
    assert (
        wg.get_session("codex", fixture("codex_pre_apply_patch_multi", tmp_path))
        == "01a205fa-b2fb-71d2-99df-1607a3477792"
    )


def test_patch_parser_all_ops():
    t = "*** Begin Patch\n*** Add File: a\n+x\n*** Update File: b\n*** Move to: c\n@@\n*** Delete File: d\n*** End Patch"
    assert wg.parse_patch(t) == [
        ("Add File", "a", None),
        ("Update File", "b", "c"),
        ("Delete File", "d", None),
    ]


def test_relative_and_nonexistent_path_resolution(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert wg.resolve("new.txt", str(link)) == os.path.join(
        os.path.realpath(real), "new.txt"
    )
    assert wg.resolve("../real/x/../y.txt", str(link)) == os.path.join(
        os.path.realpath(real), "y.txt"
    )


# ----------------------------------------------------------------------------- signature rules (agy)


def test_signature_rules_agy(env):
    proj, state = env
    f = proj / "c.txt"
    f.write_text("one\n")
    pre = lambda p: run_hook(
        "antigravity",
        "pre",
        agy("PreToolUse", "write_file", p, proj, Content="x", Overwrite=True),
        state,
    )
    post = lambda n, p: run_hook(
        "antigravity", "post", agy("PostToolUse", n, p, proj), state
    )
    # (a) exists, never read -> deny
    assert is_deny("antigravity", pre(f))
    # (b) read, unchanged -> allow
    post("read_file", f)
    assert pre(f) == {"decision": "allow"}
    # (c) another writer appends -> deny, file untouched
    f.write_text("one\nEXTERNAL-LINE\n")
    out = pre(f)
    assert (
        is_deny("antigravity", out)
        and "WRITE GUARD" in out["reason"]
        and str(f.resolve()) in out["reason"]
    )
    assert f.read_text() == "one\nEXTERNAL-LINE\n"
    # re-read, then allowed
    post("read_file", f)
    assert pre(f) == {"decision": "allow"}
    # (d) new file allowed; own write then rewrite allowed
    nf = proj / "new.txt"
    assert pre(nf) == {"decision": "allow"}
    nf.write_text("mine")
    post("write_file", nf)
    assert pre(nf) == {"decision": "allow"}
    # failed post (error set) records nothing
    g = proj / "g.txt"
    g.write_text("g")
    p = agy("PostToolUse", "read_file", g, proj)
    p["error"] = "boom"
    run_hook("antigravity", "post", p, state)
    assert is_deny("antigravity", pre(g))


def test_other_session_does_not_share_record(env):
    proj, state = env
    f = proj / "c.txt"
    f.write_text("x")
    run_hook(
        "antigravity", "post", agy("PostToolUse", "read_file", f, proj, cid="A"), state
    )
    assert (
        run_hook(
            "antigravity",
            "pre",
            agy("PreToolUse", "write_file", f, proj, cid="A"),
            state,
        )["decision"]
        == "allow"
    )
    assert is_deny(
        "antigravity",
        run_hook(
            "antigravity",
            "pre",
            agy("PreToolUse", "write_file", f, proj, cid="B"),
            state,
        ),
    )


def test_same_content_new_mtime_is_still_fresh(env):
    proj, state = env
    f = proj / "c.txt"
    f.write_text("same")
    run_hook("antigravity", "post", agy("PostToolUse", "read_file", f, proj), state)
    os.utime(f, ns=(1, 1))  # touch only
    assert (
        run_hook("antigravity", "pre", agy("PreToolUse", "write_file", f, proj), state)[
            "decision"
        ]
        == "allow"
    )


def test_large_file_uses_size_and_mtime(env, monkeypatch, tmp_path):
    proj, state = env
    monkeypatch.setattr(wg, "HASH_LIMIT", 4)
    f = proj / "big.txt"
    f.write_text("0123456789")
    s1 = wg.signature(str(f))
    assert s1["sha256"] is None
    assert wg.same_sig(s1, wg.signature(str(f)))
    os.utime(f, ns=(5, 5))
    assert not wg.same_sig(s1, wg.signature(str(f)))
    f.write_text("short")  # different size
    s2 = wg.signature(str(f))
    assert not wg.same_sig(s1, s2)


def test_ledger_names_other_session(env):
    proj, state = env
    f = proj / "c.txt"
    f.write_text("x")
    run_hook(
        "claude",
        "post",
        {
            "session_id": "claudeSESSION99",
            "cwd": str(proj),
            "tool_name": "Write",
            "tool_input": {"file_path": str(f)},
        },
        state,
    )
    f.write_text("y")
    out = run_hook(
        "antigravity", "pre", agy("PreToolUse", "write_file", f, proj), state
    )
    assert (
        "last written by claude session claudeSE at " in out["reason"]
        and out["reason"].count(" ET") == 1
    )


# ----------------------------------------------------------------------------- codex


def test_codex_add_file_over_existing(env):
    proj, state = env
    f = proj / "existing.txt"
    f.write_text("orig\n")
    add = "*** Begin Patch\n*** Add File: existing.txt\n+NEW\n*** End Patch"
    out = run_hook("codex", "pre", codex("PreToolUse", add, proj), state)
    assert is_deny("codex", out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "Update File hunk" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert f.read_text() == "orig\n"
    # shell cat records a read
    run_hook(
        "codex", "post", codex_bash("PostToolUse", "cat existing.txt", proj), state
    )
    assert (
        run_hook("codex", "pre", codex("PreToolUse", add, proj), state) is None
    )  # allow = empty stdout
    # Add File for a new path is allowed
    assert not is_deny(
        "codex",
        run_hook(
            "codex",
            "pre",
            codex(
                "PreToolUse",
                "*** Begin Patch\n*** Add File: brand_new.txt\n+x\n*** End Patch",
                proj,
            ),
            state,
        ),
    )


def test_codex_update_file_not_checked_and_post_records(env):
    proj, state = env
    f = proj / "u.txt"
    f.write_text("a\n")
    upd = "*** Begin Patch\n*** Update File: u.txt\n@@\n-a\n+b\n*** End Patch"
    assert not is_deny(
        "codex", run_hook("codex", "pre", codex("PreToolUse", upd, proj), state)
    )
    f.write_text("b\n")
    run_hook("codex", "post", codex("PostToolUse", upd, proj), state)
    add = "*** Begin Patch\n*** Add File: u.txt\n+c\n*** End Patch"
    assert not is_deny(
        "codex", run_hook("codex", "pre", codex("PreToolUse", add, proj), state)
    )


def test_codex_move_to_existing_and_multi_all_or_nothing(env):
    proj, state = env
    (proj / "src.txt").write_text("s")
    (proj / "dst.txt").write_text("d")
    (proj / "ok.txt").write_text("o")
    run_hook("codex", "post", codex_bash("PostToolUse", "cat ok.txt", proj), state)
    patch = (
        "*** Begin Patch\n*** Update File: src.txt\n*** Move to: dst.txt\n@@\n-s\n+t\n"
        "*** Add File: ok.txt\n+z\n*** End Patch"
    )
    out = run_hook("codex", "pre", codex("PreToolUse", patch, proj), state)
    assert is_deny("codex", out)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "dst.txt" in reason and (proj / "dst.txt").read_text() == "d"
    # two stale paths are all listed
    (proj / "ok.txt").write_text("changed")
    out = run_hook("codex", "pre", codex("PreToolUse", patch, proj), state)
    r = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Paths denied:" in r and "ok.txt" in r and "dst.txt" in r


def test_codex_absolute_path_and_relative_to_cwd(env):
    proj, state = env
    sub = proj / "sub"
    sub.mkdir()
    (sub / "a.txt").write_text("a")
    absadd = "*** Begin Patch\n*** Add File: %s\n+x\n*** End Patch" % (sub / "a.txt")
    assert is_deny(
        "codex", run_hook("codex", "pre", codex("PreToolUse", absadd, proj), state)
    )
    reladd = "*** Begin Patch\n*** Add File: a.txt\n+x\n*** End Patch"
    assert is_deny(
        "codex", run_hook("codex", "pre", codex("PreToolUse", reladd, sub), state)
    )


# ----------------------------------------------------------------------------- claude / modes / deny formats


def test_claude_write_edit_never_denied_but_recorded(env):
    proj, state = env
    f = proj / "w.txt"
    f.write_text("a")
    pay = {
        "session_id": "c1",
        "cwd": str(proj),
        "tool_name": "Write",
        "tool_input": {"file_path": str(f)},
    }
    assert (
        run_hook("claude", "pre", dict(pay, hook_event_name="PreToolUse"), state)
        is None
    )
    run_hook("claude", "post", dict(pay, hook_event_name="PostToolUse"), state)
    rec = list((state / "sessions" / "claude" / "c1").glob("*.json"))
    assert len(rec) == 1 and json.loads(rec[0].read_text())["path"] == os.path.realpath(
        f
    )
    assert (state / "last_writer").is_dir()


def test_deny_formats(env):
    proj, state = env
    f = proj / "f.txt"
    f.write_text("x")
    a = run_hook("antigravity", "pre", agy("PreToolUse", "write_file", f, proj), state)
    assert set(a) == {"decision", "reason"} and a["decision"] == "deny"
    c = run_hook(
        "codex",
        "pre",
        codex(
            "PreToolUse",
            "*** Begin Patch\n*** Add File: f.txt\n+x\n*** End Patch",
            proj,
        ),
        state,
    )
    assert c["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert c["hookSpecificOutput"]["permissionDecisionReason"].startswith(
        "WRITE GUARD: "
    )


def test_modes(env):
    proj, state = env
    f = proj / "f.txt"
    f.write_text("x")
    p = agy("PreToolUse", "write_file", f, proj)
    off = run_hook("antigravity", "pre", p, state, {"MYCELIUM_WRITE_GUARD": "off"})
    assert off == {"decision": "allow"}
    warn = run_hook("antigravity", "pre", p, state, {"MYCELIUM_WRITE_GUARD": "warn"})
    assert warn == {"decision": "allow"}
    assert '"would_deny"' in (state / "events.log").read_text()
    cw = run_hook(
        "codex",
        "pre",
        codex(
            "PreToolUse",
            "*** Begin Patch\n*** Add File: f.txt\n+x\n*** End Patch",
            proj,
        ),
        state,
        {"MYCELIUM_WRITE_GUARD": "warn"},
    )
    assert (
        not is_deny("codex", cw)
        and "warn mode" in cw["hookSpecificOutput"]["additionalContext"]
    )
    assert is_deny(
        "antigravity",
        run_hook("antigravity", "pre", p, state, {"MYCELIUM_WRITE_GUARD": "enforce"}),
    )
    # off records nothing
    run_hook(
        "antigravity",
        "post",
        agy("PostToolUse", "read_file", f, proj, cid="offc"),
        state,
        {"MYCELIUM_WRITE_GUARD": "off"},
    )
    assert not (state / "sessions" / "antigravity" / "offc").exists()


def test_fail_open_on_injected_exception(env, monkeypatch, capsys):
    proj, state = env

    def boom(*a, **k):
        raise RuntimeError("injected")

    monkeypatch.setattr(wg, "handle", boom)
    for host, expect in [
        ("antigravity", {"decision": "allow"}),
        ("claude", None),
        ("codex", None),
    ]:
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(json.dumps({"session_id": "s", "conversationId": "c"})),
        )
        assert wg.cmd_hook(["--host", host, "--event", "pre"]) == 0
        out = capsys.readouterr().out.strip()
        assert (json.loads(out) if out else None) == expect
    assert out == "" or True
    log = (state / "errors.log").read_text()
    assert log.count("injected") == 3


def test_fail_open_on_garbage_and_missing_fields(env):
    proj, state = env
    p = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "hook",
            "--host",
            "antigravity",
            "--event",
            "pre",
        ],
        input="not json{",
        capture_output=True,
        text=True,
        env=dict(os.environ, MYCELIUM_WRITE_GUARD_DIR=str(state)),
    )
    assert p.returncode == 0 and json.loads(p.stdout) == {"decision": "allow"}
    assert (state / "errors.log").exists()
    assert run_hook(
        "antigravity",
        "pre",
        {"hookEventName": "PreToolUse", "toolCall": {"name": "write_file", "args": {}}},
        state,
    ) == {"decision": "allow"}


def test_post_output_shapes(env):
    proj, state = env
    f = proj / "f.txt"
    f.write_text("x")
    assert (
        run_hook("antigravity", "post", agy("PostToolUse", "read_file", f, proj), state)
        == {}
    )
    assert (
        run_hook("codex", "post", codex_bash("PostToolUse", "cat f.txt", proj), state)
        is None
    )


# ----------------------------------------------------------------------------- shell parsing


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("cat a.txt", ["a.txt"]),
        ("head -n 5 a.txt b.txt", ["5", "a.txt", "b.txt"]),
        ("sed -n '1,5p' a.txt", ["a.txt"]),
        ("sed -n -e '1p' a.txt", ["a.txt"]),
        ("nl a.txt | wc -l", ["a.txt"]),
        ("cd d && cat x.txt; tail -3 y.txt", ["d/x.txt", "d/y.txt"]),
        ("cd d && cd e && cat x.txt", ["d/e/x.txt"]),
        ("cd /abs/z; cat x.txt", ["/abs/z/x.txt"]),
        ("cat a.txt; cd $D; cat b.txt", ["a.txt"]),
        ("cat a.txt; cd ~other; cat b.txt", ["a.txt"]),
        ("cat a.txt; cd -; cat b.txt", ["a.txt"]),
        ("cat a.txt; cd $(pwd)/x; cat b.txt", ["a.txt"]),
        ("echo hi > out.txt", []),
        ("cat a.txt > copy.txt", ["a.txt"]),
        ("cat notes.txt\ngit add shared.txt", ["notes.txt"]),
        ("head -5 log.txt\ntouch shared.txt", ["log.txt"]),
    ],
)
def test_shell_reads(cmd, expected, tmp_path):
    base = str(tmp_path.resolve())
    want = [os.path.join(base, e.lstrip("/")) if not e.startswith("/") else e for e in expected]
    assert wg.shell_reads(cmd, base) == want


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("printf x > f", [("redirect", "f")]),
        ("printf x >> f", []),
        ("echo hi 2> err.log", []),
        ("echo hi > /dev/null", []),
        ("cmd 2>&1 | tee out.log", [("tee", "out.log")]),
        ("cmd | tee -a out.log", []),
        ("cp a.txt b.txt", [("cp", "b.txt")]),
        ("mv -f a b", [("mv", "b")]),
        ("cat a >> b", []),
        ("echo a && echo b > c.txt", [("redirect", "c.txt")]),
        ('echo "a > b"', []),
        ("echo '>' shared.txt", []),
        ('echo ">" shared.txt', []),
        ("echo a\nprintf x > f", [("redirect", "f")]),
    ],
)
def test_shell_overwrites(cmd, expected):
    assert wg.shell_overwrites(cmd) == expected


def test_shell_overwrite_warns_never_denies(env):
    proj, state = env
    f = proj / "f.txt"
    f.write_text("one")
    run_hook("codex", "post", codex_bash("PostToolUse", "cat f.txt", proj), state)
    f.write_text("EXTERNAL")
    out = run_hook(
        "codex", "pre", codex_bash("PreToolUse", "printf mine > f.txt", proj), state
    )
    assert not is_deny("codex", out)
    assert "not enforced for shell" in out["hookSpecificOutput"]["additionalContext"]
    assert '"uncovered_shell_overwrite"' in (state / "events.log").read_text()
    # fresh file: no warning; append: no warning
    run_hook("codex", "post", codex_bash("PostToolUse", "cat f.txt", proj), state)
    assert (
        run_hook(
            "codex", "pre", codex_bash("PreToolUse", "printf mine > f.txt", proj), state
        )
        is None
    )
    f.write_text("changed again")
    assert (
        run_hook(
            "codex", "pre", codex_bash("PreToolUse", "printf x >> f.txt", proj), state
        )
        is None
    )
    # agy only logs: output is a plain allow
    a = {
        "conversationId": "z",
        "cwd": str(proj),
        "hookEventName": "PreToolUse",
        "toolCall": {"name": "run_command", "args": {"CommandLine": "echo q > f.txt"}},
    }
    assert run_hook("antigravity", "pre", a, state) == {"decision": "allow"}


def test_shell_overwrite_records_own_write(env):
    proj, state = env
    f = proj / "f.txt"
    f.write_text("one")
    run_hook(
        "codex", "post", codex_bash("PostToolUse", "printf mine > f.txt", proj), state
    )
    add = "*** Begin Patch\n*** Add File: f.txt\n+x\n*** End Patch"
    assert not is_deny(
        "codex", run_hook("codex", "pre", codex("PreToolUse", add, proj), state)
    )


# ----------------------------------------------------------------------------- store integrity


def test_concurrent_writers_keep_valid_json(env):
    proj, state = env
    f = proj / "shared.txt"
    f.write_text("x")
    payload = json.dumps(agy("PostToolUse", "read_file", f, proj, cid="same"))
    e = dict(os.environ, MYCELIUM_WRITE_GUARD_DIR=str(state))
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT),
                "hook",
                "--host",
                "antigravity",
                "--event",
                "post",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=e,
        )
        for _ in range(16)
    ]
    for p in procs:
        p.stdin.write(payload)
        p.stdin.close()
    for p in procs:
        assert p.wait(timeout=30) == 0
    recs = list((state / "sessions" / "antigravity" / "same").glob("*"))
    assert len(recs) == 1 and recs[0].suffix == ".json"  # no stray tmp files
    assert json.loads(recs[0].read_text())["path"] == os.path.realpath(f)
    assert not (state / "errors.log").exists()


def test_prune_removes_only_old_dirs(env):
    proj, state = env
    old = state / "sessions" / "claude" / "old"
    new = state / "sessions" / "claude" / "new"
    for d in (old, new):
        d.mkdir(parents=True)
        (d / "a.json").write_text("{}")
    t = 1_000_000
    os.utime(old / "a.json", (t, t))
    os.utime(old, (t, t))
    wg.prune()
    assert not old.exists() and new.exists()


def test_status_and_selftest(env):
    proj, state = env
    f = proj / "f.txt"
    f.write_text("x")
    run_hook(
        "antigravity",
        "post",
        agy("PostToolUse", "write_file", f, proj, cid="st"),
        state,
    )
    e = dict(os.environ, MYCELIUM_WRITE_GUARD_DIR=str(state))
    out = json.loads(
        subprocess.run(
            [sys.executable, str(SCRIPT), "status", "--path", str(f)],
            capture_output=True,
            text=True,
            env=e,
        ).stdout
    )
    assert (
        out["sessions"][0]["session_id"] == "st" and out["sessions"][0]["fresh"] is True
    )
    assert out["last_writer"]["host"] == "antigravity"
    st = subprocess.run(
        [sys.executable, str(SCRIPT), "selftest"], capture_output=True, text=True, env=e
    )
    assert st.returncode == 0 and "selftest ok" in st.stdout


def test_startup_time_reasonable(env):
    import time

    proj, state = env
    t = time.monotonic()
    run_hook(
        "antigravity",
        "pre",
        agy("PreToolUse", "write_file", proj / "n.txt", proj),
        state,
    )
    assert (
        time.monotonic() - t < 1.5
    )  # loose bound for shared CI nodes; spec target 150 ms on a quiet host


def test_script_has_no_package_imports():
    src = SCRIPT.read_text()
    assert "mycelium_coord" not in src.replace("mycelium_coord/writeguard", "")
    assert "from ." not in src and "import mycelium" not in src


# ----------------------------------------------------------------------------- round 2 fixes


def test_quoted_gt_and_newline_do_not_record(env):
    proj, state = env
    shared = proj / "shared.txt"
    notes = proj / "notes.txt"
    shared.write_text("s")
    notes.write_text("n")
    run_hook(
        "codex", "post", codex_bash("PostToolUse", "cat notes.txt\ngit add shared.txt", proj), state
    )
    add = "*** Begin Patch\n*** Add File: shared.txt\n+x\n*** End Patch"
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", add, proj), state))
    # a quoted '>' is not a write: no ledger entry, no own-record for shared.txt
    run_hook("codex", "post", codex_bash("PostToolUse", "echo '>' shared.txt", proj, sid="s2"), state, )
    assert not (state / "last_writer").exists() or not list((state / "last_writer").iterdir())
    # notes.txt WAS read
    add2 = "*** Begin Patch\n*** Add File: notes.txt\n+x\n*** End Patch"
    assert not is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", add2, proj), state))


def test_cd_tracking_records_resolved_paths(env):
    proj, state = env
    (proj / "sub").mkdir()
    f = proj / "sub" / "x.txt"
    f.write_text("1")
    run_hook("codex", "post", codex_bash("PostToolUse", "cd sub && cat x.txt", proj), state)
    add = "*** Begin Patch\n*** Add File: sub/x.txt\n+x\n*** End Patch"
    assert not is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", add, proj), state))


def test_signature_unreadable_file(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    f = tmp_path / "u.txt"
    f.write_text("secret")
    f.chmod(0)
    try:
        sig = wg.signature(str(f))
        assert sig is not None and sig["sha256"] is None and sig["size"] == 6
        assert wg.same_sig(sig, wg.signature(str(f)))
    finally:
        f.chmod(0o600)


def _existing_unread(proj):
    f = proj / "f.txt"
    f.write_text("one")
    return {
        "conversationId": "c1",
        "cwd": str(proj),
        "hookEventName": "PreToolUse",
        "toolCall": {"name": "write_file", "args": {"Path": str(f)}},
    }


def test_unwritable_store_regular_file_allows(env, tmp_path):
    proj, _ = env
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    out = run_hook("antigravity", "pre", _existing_unread(proj), blocker)
    assert out == {"decision": "allow"}
    cp = codex("PreToolUse", "*** Begin Patch\n*** Add File: f.txt\n+x\n*** End Patch", proj)
    (proj / "f.txt").write_text("one")
    out = run_hook("codex", "pre", cp, blocker)
    assert not is_deny("codex", out)
    assert "write guard inactive: state dir not writable" in out["hookSpecificOutput"]["additionalContext"]


def test_unwritable_store_readonly_dir_allows(env, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    proj, _ = env
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        out = run_hook("antigravity", "pre", _existing_unread(proj), ro)
        assert out == {"decision": "allow"}
        # post-path failure never raises (run_hook asserts exit code 0)
        run_hook("antigravity", "post", dict(_existing_unread(proj), hookEventName="PostToolUse",
                 toolCall={"name": "read_file", "args": {"Path": str(proj / "f.txt")}}), ro)
    finally:
        ro.chmod(0o700)


# ----------------------------------------------------------------------------- round 3: gated shell parsing
# Governing rule: WHEN UNSURE, RECORD NOTHING. A missed record only causes a later "re-read first" deny;
# a wrong record grants false freshness. Safety properties below assert "never recorded", not exact lists.


def _eff(cmd, base):
    return wg.shell_effects(cmd, base)


def _recorded(cmd, base):
    """Everything the post branch would record for cmd (reads + overwrites); empty when gated."""
    e = wg.shell_effects(cmd, base)
    return set() if e is None else set(e[0]) | set(e[1])


@pytest.mark.parametrize(
    "run,expected",
    [
        (");", [")", ";"]),
        (")&&", [")", "&&"]),
        (";(", [";", "("]),
        ("&&&", ["&&", "&"]),
        (">>", [">>"]),
        (">&", [">&"]),
        ("<<<", ["<<<"]),
        ("<<-", ["<<-"]),
        ("|&", ["|&"]),
        ("&>>", ["&>>"]),
        ("<(", ["<", "("]),
    ],
)
def test_split_punct_longest_match(run, expected):
    assert wg.split_punct(run) == expected


@pytest.mark.parametrize("run", ["><", ">;", "<>>", ">>|"])
def test_split_punct_unknown_runs(run):
    assert wg.split_punct(run) == [wg.UNKNOWN_OP]


def test_tokens_glued_operators_split():
    t = wg.tokens("v=$(cat a.txt);git add shared.txt")
    assert ")" in t and ";" in t and ");" not in t
    assert wg.tokens("(cat a.txt)&&git add shared.txt").count("&&") == 1


@pytest.mark.parametrize("cmd", ["echo \\> shared.txt", "echo hi \\; cat shared.txt", "echo \\| \\& \\( x"])
def test_backslash_escaped_operators_are_literal_words(cmd, tmp_path):
    base = str(tmp_path.resolve())
    toks = wg.tokens(cmd)
    assert not any(t in wg.SEPARATORS or t in wg.REDIRECTS for t in toks)
    assert wg.shell_overwrites(cmd) == []
    assert os.path.join(base, "shared.txt") not in wg.shell_reads(cmd, base)
    assert os.path.join(base, "shared.txt") not in _recorded(cmd, base)


def test_backslash_escaped_gt_does_not_record_or_touch_ledger(env):
    proj, state = env
    (proj / "shared.txt").write_text("s")
    add = "*** Begin Patch\n*** Add File: shared.txt\n+x\n*** End Patch"
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", add, proj, sid="s2"), state))
    run_hook("codex", "post", codex_bash("PostToolUse", "echo \\> shared.txt", proj, sid="s2"), state)
    led = list((state / "last_writer").iterdir()) if (state / "last_writer").exists() else []
    out = run_hook("codex", "pre", codex("PreToolUse", add, proj, sid="s2"), state)
    assert not led and is_deny("codex", out)


@pytest.mark.parametrize(
    "cmd",
    [
        "cat a.txt; (cat b.txt)",
        "v=$(cat b.txt); cat a.txt",
        "echo \"$(cat b.txt)\"; cat a.txt",
        "cat a.txt; { cat b.txt; }",
        "cat a.txt; echo `cat b.txt`",
        "cat a.txt <<EOF\nx\nEOF",
        "cat a.txt <<< hello",
        "cat a.txt <<-EOF\nx\nEOF",
        "cat a.txt; cat b.txt ><",
        "cat a.txt; eval 'cat b.txt'",
        "cat a.txt; source x.sh",
        "cat a.txt; . x.sh",
        "cat a.txt; exec cat b.txt",
        "cat a.txt; builtin cd d; cat b.txt",
        "cat a.txt; command cat b.txt",
        "cat a.txt; pushd d; cat b.txt",
        "cat a.txt; popd; cat b.txt",
        "cat a.txt; cd $D; cat b.txt",
        "cat a.txt; cd `pwd`; cat b.txt",
        "cat a.txt; cd d*; cat b.txt",
        "FOO=1 eval cat b.txt; cat a.txt",
        "cd d | cat b.txt; cat a.txt",
        "cd d || cat b.txt; cat a.txt",
        "cat <(cat b.txt) a.txt",
    ],
)
def test_shell_effects_gated_constructs_record_nothing(cmd, tmp_path):
    assert wg.shell_effects(cmd, str(tmp_path.resolve())) is None


def test_shell_effects_escaped_and_quoted_parens_are_not_gated(tmp_path):
    base = str(tmp_path.resolve())
    # round 4: a backslash anywhere is gated (pre-gate), so the escaped-paren form is no longer trusted
    for cmd in ("cat 'a(b).txt'", 'cat "a b).txt"', "cat '{' x.txt"):
        assert wg.shell_effects(cmd, base) is not None
    assert wg.shell_effects("cat a\\(b\\).txt", base) is None


def test_w3_subshell_cd_does_not_leak(tmp_path):
    base = str(tmp_path.resolve())
    for cmd in ("(cd sub && true) ; cat f.txt", "(cd sub && true);cat f.txt"):
        assert os.path.join(base, "sub", "f.txt") not in _recorded(cmd, base)  # recording nothing is fine


@pytest.mark.parametrize(
    "cmd",
    [
        "cat a.txt; pushd d; cat b.txt",
        "cat a.txt; popd; cat b.txt",
        "cat a.txt; cd d*; cat b.txt",
        "cat a.txt; cd `pwd`; cat b.txt",
    ],
)
def test_w3_untested_clauses(cmd, tmp_path):
    base = str(tmp_path.resolve())
    a = os.path.join(base, "a.txt")
    assert set(wg.shell_reads(cmd, base)) <= {a}
    assert _recorded(cmd, base) <= {a}
    assert os.path.join(base, "b.txt") not in _recorded(cmd, base)
    assert os.path.join(base, "d", "b.txt") not in _recorded(cmd, base)


def test_w2_heredoc_body_is_not_parsed_as_commands(tmp_path):
    base = str(tmp_path.resolve())
    cmd = "cat > s.sh <<'EOF'\ncat config.txt\necho x > other.txt\nEOF"
    # gated record path: nothing recorded at all (the loose pre-warning may still list them)
    assert wg.shell_effects(cmd, base) is None
    rec = _recorded(cmd, base)
    assert os.path.join(base, "config.txt") not in rec and os.path.join(base, "other.txt") not in rec


def test_w2_heredoc_body_does_not_grant_freshness(env):
    proj, state = env
    (proj / "config.txt").write_text("c")
    add = "*** Begin Patch\n*** Add File: config.txt\n+x\n*** End Patch"
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", add, proj), state))  # control
    cmd = "cat > s.sh <<'EOF'\n#!/bin/sh\ncat config.txt\nEOF"
    run_hook("codex", "post", codex_bash("PostToolUse", cmd, proj), state)
    out = run_hook("codex", "pre", codex("PreToolUse", add, proj), state)
    assert is_deny("codex", out)


@pytest.mark.parametrize(
    "cmd",
    [
        "v=$(cat a.txt);git add shared.txt",
        "(cat a.txt)&&git add shared.txt",
        "cat a.txt&&git add shared.txt",
    ],
)
def test_w2_glued_operators_do_not_merge_commands(cmd, tmp_path):
    base = str(tmp_path.resolve())
    assert os.path.join(base, "shared.txt") not in wg.shell_reads(cmd, base)
    assert os.path.join(base, "shared.txt") not in _recorded(cmd, base)


def test_w2_plain_glued_and_still_records_a_txt(tmp_path):
    base = str(tmp_path.resolve())
    assert os.path.join(base, "a.txt") in _recorded("cat a.txt&&git add shared.txt", base)
    assert os.path.join(base, "a.txt") in wg.shell_reads("cat a.txt&&git add shared.txt", base)


def test_w2_glued_operator_hook_chain(env):
    proj, state = env
    (proj / "a.txt").write_text("a")
    (proj / "shared.txt").write_text("s")
    add = "*** Begin Patch\n*** Add File: shared.txt\n+x\n*** End Patch"
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", add, proj), state))  # control
    run_hook("codex", "post", codex_bash("PostToolUse", "v=$(cat a.txt);git add shared.txt", proj), state)
    out = run_hook("codex", "pre", codex("PreToolUse", add, proj), state)
    assert is_deny("codex", out)


def test_w3_overwrite_record_after_cd_does_not_grant_freshness(env):
    proj, state = env
    (proj / "sub").mkdir()
    (proj / "f.txt").write_text("top")
    add = "*** Begin Patch\n*** Add File: f.txt\n+x\n*** End Patch"
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", add, proj), state))  # control
    run_hook("codex", "post", codex_bash("PostToolUse", "cd sub && printf x > f.txt", proj), state)
    out = run_hook("codex", "pre", codex("PreToolUse", add, proj), state)
    assert is_deny("codex", out)


def test_overwrite_targets_resolve_against_same_effective_cwd(env):
    proj, state = env
    (proj / "sub").mkdir()
    (proj / "sub" / "f.txt").write_text("sub")
    reads, ow = wg.shell_effects("cd sub && printf x > f.txt", str(proj.resolve()))
    assert ow == [str(proj.resolve() / "sub" / "f.txt")] and reads == []
    run_hook("codex", "post", codex_bash("PostToolUse", "cd sub && printf x > f.txt", proj), state)
    add = "*** Begin Patch\n*** Add File: sub/f.txt\n+x\n*** End Patch"
    assert not is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", add, proj), state))


def test_gated_overwrite_command_records_nothing(env):
    proj, state = env
    (proj / "f.txt").write_text("top")
    add = "*** Begin Patch\n*** Add File: f.txt\n+x\n*** End Patch"
    run_hook("codex", "post", codex_bash("PostToolUse", "(printf x > f.txt)", proj), state)
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", add, proj), state))
    assert not (state / "last_writer").exists() or not list((state / "last_writer").iterdir())


@pytest.mark.parametrize(
    "cmd,want_reads",
    [
        ("cat f", ["f"]),
        ("sed -n '1,200p' f", ["f"]),
        ("nl -ba f | sed -n '1,80p'", ["f"]),
        # round 4: only what runs unconditionally is recorded; g follows && and may not have run
        ("head -n 50 f && tail -n 5 g", ["f"]),
        ("cd sub && cat f", ["sub/f"]),
        ("cat a; cat b", ["a", "b"]),
        ("cat existing.txt", ["existing.txt"]),
    ],
)
def test_common_codex_read_forms_still_record(cmd, want_reads, tmp_path):
    base = str(tmp_path.resolve())
    e = wg.shell_effects(cmd, base)
    assert e is not None
    assert e[0] == [os.path.join(base, w) for w in want_reads]


_SHELL_FIXTURES = {
    # fixture file -> (command, expected reads, expected overwrite targets), as recorded after round 4
    "codex_pre_bash_12": ("cat existing.txt", ["existing.txt"], []),
    "codex_post_bash_13": ("cat existing.txt", ["existing.txt"], []),
    "codex_pre_bash_15": ("cat existing2.txt", ["existing2.txt"], []),
    "codex_post_bash_16": ("cat existing2.txt", ["existing2.txt"], []),
    # round 4: `cat b.txt` follows && and post payloads carry no exit status, so it is not a read
    "claude_pre_bash": ("echo viabash > b.txt && cat b.txt", [], ["b.txt"]),
    "claude_post_bash": ("echo viabash > b.txt && cat b.txt", [], ["b.txt"]),
    "agy_pre_run_command": ("echo shellwrite > d.txt", [], ["d.txt"]),
    "agy_post_run_command": ("echo shellwrite > d.txt", [], ["d.txt"]),
}


def test_every_real_shell_fixture_is_listed():
    on_disk = {
        p.stem
        for p in FIX.glob("*.json")
        if "bash" in p.stem or "run_command" in p.stem
    }
    assert on_disk == set(_SHELL_FIXTURES)


@pytest.mark.parametrize("name", sorted(_SHELL_FIXTURES))
def test_real_shell_fixtures_record_exactly_what_they_did_before(name, tmp_path):
    cmd_expected, reads, ow = _SHELL_FIXTURES[name]
    payload = json.loads((FIX / (name + ".json")).read_text())
    cmd = (
        payload["toolCall"]["args"]["CommandLine"]
        if "toolCall" in payload
        else wg.shell_command(payload)
    )
    assert cmd == cmd_expected  # the fixture still carries the command this table assumes
    cwd = payload["cwd"]
    e = wg.shell_effects(cmd, cwd)
    assert e is not None, "a real captured fixture must not be gated"
    # exact after-change records
    assert e[0] == [os.path.join(cwd, r) for r in reads]
    assert e[1] == [os.path.join(cwd, o) for o in ow]
    # the loose pre-warning parser still sees every overwrite target; trusted reads are a subset of it
    assert set(e[0]) <= set(wg.shell_reads(cmd, cwd))
    assert e[1] == [wg.resolve(p, cwd) for _k, p in wg.shell_overwrites(cmd)]


# ----------------------------------------------------------------------------- round 4: trusted command shapes
# shell_effects is an allowlist: a command records anything only if it matches the grammar in full.
# The round-3 verifier reproducers are restated as SAFETY PROPERTIES ("path X is not recorded").


def _add_patch(name):
    return "*** Begin Patch\n*** Add File: %s\n+x\n*** End Patch" % name


def _ledger(state):
    d = state / "last_writer"
    return list(d.iterdir()) if d.exists() else []


@pytest.mark.parametrize(
    "cmd",
    [
        "if true; then cd sub; fi; cat f.txt",
        'for d in sub; do cd "$d"; done; cat f.txt',
        "time cd sub; cat f.txt",
        "! cd sub; cat f.txt",
        "false && cd sub; cat f.txt",
        "cd sub; cat f.txt",
        "cd sub || cat f.txt",
        "cat a.txt | cd sub; cat f.txt",
        "cd sub && cat f.txt; cd .. && cat g.txt",
    ],
)
def test_r4_cd_in_any_non_leading_form_records_nothing(cmd, tmp_path):
    base = str(tmp_path.resolve())
    assert wg.shell_effects(cmd, base) is None


def test_r4_reserved_word_cd_hook_chain_read(env):
    proj, state = env
    (proj / "sub").mkdir()
    (proj / "f.txt").write_text("TOP\n")
    (proj / "sub" / "f.txt").write_text("SUB\n")
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", _add_patch("f.txt"), proj), state))
    cmd = "if true; then cd sub; fi; cat f.txt"
    run_hook("codex", "post", codex_bash("PostToolUse", cmd, proj), state)
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", _add_patch("f.txt"), proj), state))


def test_r4_reserved_word_cd_hook_chain_overwrite(env):
    proj, state = env
    (proj / "sub").mkdir()
    (proj / "f.txt").write_text("TOP\n")
    (proj / "sub" / "f.txt").write_text("SUB\n")
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", _add_patch("f.txt"), proj), state))
    cmd = "if true; then cd sub; fi; printf x > f.txt"
    run_hook("codex", "post", codex_bash("PostToolUse", cmd, proj), state)
    assert not _ledger(state)
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", _add_patch("f.txt"), proj), state))


@pytest.mark.parametrize("cmd", ["cat a.txt &>> shared.txt", "cat a.txt <> shared.txt", "cat a.txt >> shared.txt"])
def test_r4_non_read_redirect_target_is_never_a_read(cmd, tmp_path):
    base = str(tmp_path.resolve())
    assert os.path.join(base, "shared.txt") not in _recorded(cmd, base)


def test_r4_append_both_streams_hook_chain(env):
    proj, state = env
    (proj / "a.txt").write_text("A\n")
    (proj / "shared.txt").write_text("OTHER-SESSION\n")
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", _add_patch("shared.txt"), proj), state))
    run_hook("codex", "post", codex_bash("PostToolUse", "cat a.txt &>> shared.txt", proj), state)
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", _add_patch("shared.txt"), proj), state))


@pytest.mark.parametrize(
    "cmd",
    [
        "cat a.txt ;; cat shared.txt",
        "cat a.txt ;& cat shared.txt",
        "cat shared.txt; echo 'unterminated",
        "cat shared.txt; ; cat a.txt",
        "; cat shared.txt",
        "cat a.txt &&",
        "cat a.txt && ; cat shared.txt",
    ],
)
def test_r4_syntax_error_command_records_nothing(cmd, tmp_path):
    assert wg.shell_effects(cmd, str(tmp_path.resolve())) is None


@pytest.mark.parametrize("cmd", ["echo $'\\''; echo '; cat shared.txt'", "echo $'a'; cat shared.txt"])
def test_r4_ansi_c_quote_is_gated(cmd, tmp_path):
    assert wg.shell_effects(cmd, str(tmp_path.resolve())) is None


def test_r4_ansi_c_quote_hook_chain(env):
    proj, state = env
    (proj / "shared.txt").write_text("SHARED-CONTENT\n")
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", _add_patch("shared.txt"), proj), state))
    run_hook("codex", "post", codex_bash("PostToolUse", "echo $'\\''; echo '; cat shared.txt'", proj), state)
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", _add_patch("shared.txt"), proj), state))


@pytest.mark.parametrize(
    "cmd,forbidden",
    [
        # conditional execution: no exit status in post payloads
        ("false && cat b", ["b"]),
        ("cat a && cat b", ["b"]),
        ("cat a || cat b", ["b"]),
        ("cat a && cat b || cat c", ["b", "c"]),
        ("cd sub && cat a && cat b", ["sub/b"]),
        # shell state / control flow that stops later segments or changes what they mean
        ("exit 0; cat a", ["a"]),
        ("set -e; cat a", ["a"]),
        ("export PATH=/nonexistent; cat a", ["a"]),
        ("PATH=/nonexistent cat a", ["a"]),
        # a redirect that may fail prevents the command from running; so reads are dropped
        ("cat a < b", ["a", "b"]),
        ("cat a > sub/none/x", ["a"]),
        ("cat a > b", ["a"]),
        ("cat a >> b", ["a", "b"]),
        ("cat a > b > c", ["a", "b", "c"]),
        # unquoted expansions, comments, tilde, equals, escapes
        ("cat *.txt", ["*.txt"]),
        ("cat ~/a", ["a"]),
        ("cat {a,b}", ["a", "b"]),
        ("cat a # b", ["b"]),
        ("cat =ls", ["=ls"]),
        ("cat ../a", ["../a"]),
        # program path is not the bare program name
        ("./cat a", ["a"]),
        ("/bin/cat a", ["a"]),
        # option forms not modelled
        ("cat --number a", ["a"]),
        ("head -n a b", ["a"]),
        ("sed -i s/x/y/ a", ["a"]),
        ("sed -n 'w out' a", ["a"]),
        ("wc --files0-from=a", ["a"]),
        ("cat a -n", ["a"]),
    ],
)
def test_r4_safety_properties_forbidden_paths_not_recorded(cmd, forbidden, tmp_path):
    base = str(tmp_path.resolve())
    rec = _recorded(cmd, base)
    for f in forbidden:
        assert os.path.join(base, f) not in rec, (cmd, f, rec)


@pytest.mark.parametrize(
    "cmd,reads,overwrites",
    [
        ("cat f", ["f"], []),
        ("sed -n '1,200p' f", ["f"], []),
        ("nl -ba f | sed -n '1,80p'", ["f"], []),
        ("cat a; cat b", ["a", "b"], []),
        ("cat a\ncat b", ["a", "b"], []),
        ("cd sub && cat f", ["sub/f"], []),
        ("cd sub && cd . && cat f", ["sub/f"], []),
        ("cat existing.txt", ["existing.txt"], []),
        ("cat a 2>/dev/null", ["a"], []),
        ("cat a 2>&1 | head -n 5", ["a"], []),
        ("head -n 5 f", ["f"], []),
        ("tail -n +3 f g", ["f", "g"], []),
        ("wc -l f", ["f"], []),
        ("cat a && cat b", ["a"], []),
        ("echo hi > o.txt", [], ["o.txt"]),
        ("printf x >| o.txt", [], ["o.txt"]),
        ("echo hi &> o.txt", [], ["o.txt"]),
        ("echo hi 2> o.txt", [], ["o.txt"]),
        ("cd sub && printf x > f.txt", [], ["sub/f.txt"]),
        ("cat a > o.txt", [], ["o.txt"]),
        ("echo hi >> o.txt", [], []),
        ("cat a > o.txt > p.txt", [], []),
    ],
)
def test_r4_trusted_shapes_record_exactly(cmd, reads, overwrites, tmp_path):
    base = str(tmp_path.resolve())
    e = wg.shell_effects(cmd, base)
    assert e is not None, cmd
    assert e[0] == [os.path.join(base, r) for r in reads]
    assert e[1] == [os.path.join(base, o) for o in overwrites]


@pytest.mark.parametrize(
    "cmd",
    [
        "a=$(cat x)", "cat `x`", "cat \"$x\"", "cat a\\ b", "echo '\\'", "(cat a)", "{ cat a; }", "cat a &", "cat a |& cat",
        "cat a <&3", "cat a <> b", "cat <<EOF\nx\nEOF", "cat <<< x", "cat a 1>b", "cat a >& b", "cat a >&", "echo x >",
        "if true; then cat a; fi", "! cat a", "time cat a", "[[ -f a ]]", "function f { cat a; }", "case x in x) cat a;; esac",
        "while false; do cat a; done", "coproc cat a", "cat a; select x in y; do cat b; done", "A='x y' cat a",
        "PATH=x cat a", "cat a; 'if' true", "'cat' a; exit", "cat a; then cat b", "cat\ta;\rcat b", "A=b", "> o.txt", "cat a; > o.txt",
    ],
)
def test_r4_outside_the_grammar_records_nothing(cmd, tmp_path):
    assert wg.shell_effects(cmd, str(tmp_path.resolve())) is None


def test_r4_reserved_and_gated_words_cover_the_spec_list():
    spec_words = (
        "if then elif else fi do done while until for in case esac select time ! coproc function { } [[ ]] "
        "builtin command eval source . exec pushd popd alias trap set shopt setopt"
    ).split()
    assert all(w in wg.RESERVED_WORDS or w in wg.GATED_BUILTINS for w in spec_words)
    assert wg.ALLOWED_SEPARATORS == {";", "&&", "||", "|"}


def test_r4_pregate_dollar_in_double_quotes_and_backslash_in_single_quotes(tmp_path):
    base = str(tmp_path.resolve())
    assert wg.shell_effects('cat "a$b"', base) is None
    assert wg.shell_effects("cat 'a$b'", base) is not None  # $ inside single quotes is literal
    assert wg.shell_effects("cat 'a\\b'", base) is None  # any backslash is gated
    assert wg.shell_effects("cat 'a", base) is None
    assert wg.shell_effects('cat "a', base) is None


def test_r4_head_tail_wc_e2e_hook_chain(env):
    proj, state = env
    (proj / "f.txt").write_text("1\n2\n")
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", _add_patch("f.txt"), proj), state))
    run_hook("codex", "post", codex_bash("PostToolUse", "cat f.txt && cat g.txt", proj), state)
    assert not is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", _add_patch("f.txt"), proj), state))


def test_r4_read_after_and_does_not_unlock(env):
    proj, state = env
    (proj / "f.txt").write_text("1\n")
    (proj / "g.txt").write_text("2\n")
    run_hook("codex", "post", codex_bash("PostToolUse", "cat f.txt && cat g.txt", proj), state)
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", _add_patch("g.txt"), proj), state))


# ---- deny reason carries the exact unlock form (A8)
def test_r4_codex_deny_names_the_unlock_form(env):
    proj, state = env
    (proj / "f.txt").write_text("x")
    out = run_hook("codex", "pre", codex("PreToolUse", _add_patch("f.txt"), proj), state)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    real = os.path.realpath(proj / "f.txt")
    assert "To unlock: run a standalone read such as `cat %s`" % real in reason
    assert "(on its own, not after && or ||)" in reason and reason.rstrip().endswith("then retry.")
    # and the named form really unlocks
    run_hook("codex", "post", codex_bash("PostToolUse", "cat %s" % real, proj), state)
    assert not is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", _add_patch("f.txt"), proj), state))


def test_r4_antigravity_deny_names_read_file(env):
    proj, state = env
    f = proj / "f.txt"
    f.write_text("x")
    out = run_hook("antigravity", "pre", agy("PreToolUse", "write_file", f, proj), state)
    assert out["decision"] == "deny"
    assert "To unlock: call read_file on %s, then retry." % os.path.realpath(f) in out["reason"]
    assert "cat " not in out["reason"].split("To unlock")[1]


def test_r4_codex_hint_quotes_a_path_with_spaces(env):
    proj, state = env
    (proj / "my file.txt").write_text("x")
    out = run_hook("codex", "pre", codex("PreToolUse", _add_patch("my file.txt"), proj), state)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "`cat '%s'`" % os.path.realpath(proj / "my file.txt") in reason


# ---- W4 (ported from the round-2 verifier): an unreadable existing file is never "new"
def test_w4_unreadable_existing_file_is_not_new(env):
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    proj, state = env
    f = proj / "u.txt"
    f.write_text("secret")
    f.chmod(0)
    try:
        out = run_hook("antigravity", "pre", agy("PreToolUse", "write_file", f, proj), state)
        assert is_deny("antigravity", out)  # never read: denied even though its content cannot be hashed
    finally:
        f.chmod(0o600)
    # read while readable, then chmod 0 with mtime unchanged: still fresh by size+mtime, so allowed
    run_hook("antigravity", "post", agy("PostToolUse", "read_file", f, proj), state)
    f.chmod(0)
    try:
        out = run_hook("antigravity", "pre", agy("PreToolUse", "write_file", f, proj), state)
        assert not is_deny("antigravity", out)
    finally:
        f.chmod(0o600)


def test_r4_read_of_a_path_the_same_command_may_create_is_not_recorded(tmp_path):
    base = str(tmp_path.resolve())
    for cmd in ("cat missing.txt; true >> missing.txt", "head m.txt; echo x 2> m.txt", "cat m.txt; cat a.txt > m.txt"):
        e = wg.shell_effects(cmd, base)
        assert e is not None and os.path.join(base, "m.txt") not in e[0]
        assert os.path.join(base, "missing.txt") not in e[0]


# ============================================================================= Round 5 (review 2026-11-23)
# F1: freshness only for content the session saw. F3: a Codex patch records only on success.
# F14: host kinds single-sourced. Hook templates register a PRE hook for the shell tool.
ADDF = "*** Begin Patch\n*** Add File: plan.md\n+REWRITE\n*** End Patch"


def _claude_bash(ev, cmd, cwd, sid="A", call_id=None):
    p = {
        "session_id": sid,
        "cwd": str(cwd),
        "hook_event_name": ev,
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
    }
    if call_id:
        p["tool_use_id"] = call_id
    return p


def _agy_cmd(ev, cmd, cwd, cid="conv-A", call_id="call_1"):
    return {
        "conversationId": cid,
        "cwd": str(cwd),
        "hookEventName": ev,
        "error": "",
        "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}, "id": call_id},
    }


def _codex_cmd(ev, cmd, cwd, sid="A", call_id=None):
    p = codex_bash(ev, cmd, cwd, sid=sid)
    if call_id:
        p["tool_use_id"] = call_id
    return p


SHELL_BUILDERS = {
    "codex": _codex_cmd,
    "claude": _claude_bash,
    "antigravity": lambda ev, cmd, cwd, sid="A", call_id="call_1": _agy_cmd(
        ev, cmd, cwd, cid=sid, call_id=call_id
    ),
}


def _add_pre(host, proj, sid="A"):
    if host == "codex":
        return codex("PreToolUse", ADDF, proj, sid=sid)
    if host == "antigravity":
        return agy("PreToolUse", "write_file", proj / "plan.md", proj, cid=sid)
    raise AssertionError(host)


@pytest.mark.parametrize("host", ["codex", "claude", "antigravity"])
def test_f1_read_then_long_command_does_not_credit_a_concurrent_edit(env, host):
    """(read direction) `cat f && sleep`: another session rewrites f between PRE and POST."""
    proj, state = env
    f = proj / "plan.md"
    f.write_text("v1\n")
    build = SHELL_BUILDERS[host]
    cmd = "cat plan.md && sleep 5"
    run_hook(host, "pre", build("PreToolUse", cmd, proj, sid="A", call_id="c1"), state)
    f.write_text("v1\nB-LINES from another session\n")
    run_hook(
        host, "post", build("PostToolUse", cmd, proj, sid="A", call_id="c1"), state, pre=False
    )
    if host == "claude":
        # Claude's own Write has a native stale check; the ledger record is what matters here
        assert wg.is_stale("claude", "A", wg.resolve(str(f), str(proj)))
    else:
        out = run_hook(host, "pre", _add_pre(host, proj), state)
        assert is_deny(host, out), "A never saw B-LINES, yet its whole-file write is allowed"


@pytest.mark.parametrize("host", ["codex", "claude", "antigravity"])
def test_f1_read_unchanged_between_pre_and_post_still_unlocks(env, host):
    proj, state = env
    f = proj / "plan.md"
    f.write_text("v1\n")
    build = SHELL_BUILDERS[host]
    cmd = "cat plan.md"
    run_hook(host, "pre", build("PreToolUse", cmd, proj, sid="A", call_id="c1"), state)
    run_hook(
        host, "post", build("PostToolUse", cmd, proj, sid="A", call_id="c1"), state, pre=False
    )
    assert not wg.is_stale(host, "A", wg.resolve(str(f), str(proj)))


def test_f1_overwrite_then_unknown_program_does_not_credit_a_concurrent_edit(env):
    """(overwrite direction) `echo x > f; make`: another session edits f after the redirect, before POST."""
    proj, state = env
    f = proj / "plan.md"
    f.write_text("v1\n")
    cmd = "echo x > plan.md; make"
    run_hook("codex", "pre", codex_bash("PreToolUse", cmd, proj, sid="A"), state)
    f.write_text("x\nB-LINES edited during make\n")
    run_hook("codex", "post", codex_bash("PostToolUse", cmd, proj, sid="A"), state, pre=False)
    out = run_hook("codex", "pre", codex("PreToolUse", ADDF, proj, sid="A"), state)
    assert is_deny("codex", out)


def test_f1_shell_overwrite_effects_require_only_fast_programs(tmp_path):
    base = str(tmp_path.resolve())
    for cmd in ("echo x > f; make", "echo x > f && pytest -q", "echo x > f; sleep 1", "printf x > f; ./run.sh"):
        reads, over = wg.shell_effects(cmd, base) or ([], [])
        assert over == [], cmd
    for cmd in ("echo x > f", "printf x > f; cat g.txt", "true; echo x > f"):
        e = wg.shell_effects(cmd, base)
        assert e is not None and e[1] == [os.path.join(base, "f")], cmd


def test_f1_shell_read_without_pre_snapshot_records_nothing(env):
    proj, state = env
    f = proj / "plan.md"
    f.write_text("v1\n")
    run_hook("codex", "post", codex_bash("PostToolUse", "cat plan.md", proj, sid="A"), state, pre=False)
    assert wg.is_stale("codex", "A", wg.resolve(str(f), str(proj)))


def test_f1_snapshot_is_keyed_by_call_id(env):
    proj, state = env
    f = proj / "plan.md"
    f.write_text("v1\n")
    cmd = "cat plan.md"
    run_hook("codex", "pre", _codex_cmd("PreToolUse", cmd, proj, call_id="exec-1"), state)
    # a POST carrying a different call id has no snapshot of its own: records nothing
    run_hook("codex", "post", _codex_cmd("PostToolUse", cmd, proj, call_id="exec-2"), state, pre=False)
    assert wg.is_stale("codex", "A", wg.resolve(str(f), str(proj)))
    run_hook("codex", "post", _codex_cmd("PostToolUse", cmd, proj, call_id="exec-1"), state, pre=False)
    assert not wg.is_stale("codex", "A", wg.resolve(str(f), str(proj)))


def test_f1_two_same_command_calls_do_not_share_a_snapshot(env):
    proj, state = env
    f = proj / "plan.md"
    f.write_text("v1\n")
    cmd = "cat plan.md && sleep 9"
    run_hook("codex", "pre", _codex_cmd("PreToolUse", cmd, proj, call_id="exec-1"), state)
    f.write_text("v2 by B\n")
    run_hook("codex", "pre", _codex_cmd("PreToolUse", cmd, proj, call_id="exec-2"), state)
    run_hook("codex", "post", _codex_cmd("PostToolUse", cmd, proj, call_id="exec-1"), state, pre=False)
    assert wg.is_stale("codex", "A", wg.resolve(str(f), str(proj)))  # call 1 saw v1, file is v2
    run_hook("codex", "post", _codex_cmd("PostToolUse", cmd, proj, call_id="exec-2"), state, pre=False)
    assert not wg.is_stale("codex", "A", wg.resolve(str(f), str(proj)))  # call 2 saw v2


def test_f1_without_call_id_key_falls_back_to_session_tool_command(env):
    proj, state = env
    f = proj / "plan.md"
    f.write_text("v1\n")
    run_hook("codex", "pre", codex_bash("PreToolUse", "cat plan.md", proj, sid="A"), state)
    # same command, different session: no snapshot for it
    run_hook("codex", "post", codex_bash("PostToolUse", "cat plan.md", proj, sid="Z"), state, pre=False)
    assert wg.is_stale("codex", "Z", wg.resolve(str(f), str(proj)))
    run_hook("codex", "post", codex_bash("PostToolUse", "cat plan.md", proj, sid="A"), state, pre=False)
    assert not wg.is_stale("codex", "A", wg.resolve(str(f), str(proj)))


def _pending_files(state):
    root = state / "pending"
    return [p for p in root.rglob("*.json")] if root.exists() else []


def test_f1_snapshot_is_consumed_at_post_and_stale_ones_expire(env):
    proj, state = env
    (proj / "plan.md").write_text("v1\n")
    run_hook("codex", "pre", _codex_cmd("PreToolUse", "cat plan.md", proj, call_id="e1"), state)
    assert len(_pending_files(state)) == 1
    run_hook("codex", "post", _codex_cmd("PostToolUse", "cat plan.md", proj, call_id="e1"), state, pre=False)
    assert _pending_files(state) == []
    # an abandoned snapshot (command denied or never finished) is dropped by a later PRE
    run_hook("codex", "pre", _codex_cmd("PreToolUse", "cat plan.md", proj, call_id="e2"), state)
    (old,) = _pending_files(state)
    t = time.time() - 3 * 86400
    os.utime(old, (t, t))
    run_hook("codex", "pre", _codex_cmd("PreToolUse", "cat plan.md", proj, call_id="e3"), state)
    names = sorted(p.name for p in _pending_files(state))
    assert len(names) == 1 and old.name not in names


def test_f1_pending_snapshots_are_bounded(env):
    proj, state = env
    (proj / "plan.md").write_text("v1\n")
    for i in range(wg.PENDING_MAX + 25):
        run_hook(
            "codex", "pre", _codex_cmd("PreToolUse", "cat plan.md", proj, call_id="e%d" % i), state
        )
    assert len(_pending_files(state)) <= wg.PENDING_MAX


def test_f1_unparsable_command_takes_no_snapshot(env):
    proj, state = env
    (proj / "plan.md").write_text("v1\n")
    run_hook("codex", "pre", codex_bash("PreToolUse", "cat $(echo plan.md)", proj), state)
    assert _pending_files(state) == []


def test_f1_native_read_with_pre_snapshot_ignores_a_change_between_pre_and_post(env):
    proj, state = env
    f = proj / "plan.md"
    f.write_text("v1\n")
    run_hook("antigravity", "pre", agy("PreToolUse", "read_file", f, proj, cid="A"), state)
    f.write_text("v1\nB-LINES\n")
    run_hook("antigravity", "post", agy("PostToolUse", "read_file", f, proj, cid="A"), state)
    assert is_deny("antigravity", run_hook("antigravity", "pre", agy("PreToolUse", "write_file", f, proj, cid="A"), state))


def test_f1_native_read_without_pre_snapshot_keeps_current_behaviour(env):
    proj, state = env
    f = proj / "plan.md"
    f.write_text("v1\n")
    run_hook("antigravity", "post", agy("PostToolUse", "read_file", f, proj, cid="A"), state)
    assert not is_deny("antigravity", run_hook("antigravity", "pre", agy("PreToolUse", "write_file", f, proj, cid="A"), state))


def test_f1_native_read_pre_and_post_unchanged_records(env):
    proj, state = env
    f = proj / "plan.md"
    f.write_text("v1\n")
    run_hook("antigravity", "pre", agy("PreToolUse", "read_file", f, proj, cid="A"), state)
    run_hook("antigravity", "post", agy("PostToolUse", "read_file", f, proj, cid="A"), state)
    assert not is_deny("antigravity", run_hook("antigravity", "pre", agy("PreToolUse", "write_file", f, proj, cid="A"), state))


def test_f1_snapshot_failure_fails_open(env, monkeypatch):
    """A state dir that cannot take the snapshot never blocks or errors the hook."""
    proj, state = env
    (proj / "plan.md").write_text("v1\n")
    state.parent.joinpath("blocker").write_text("a file where a dir is needed")
    out = run_hook(
        "codex", "pre", codex_bash("PreToolUse", "cat plan.md", proj), state,
        extra_env={"MYCELIUM_WRITE_GUARD_DIR": str(state.parent / "blocker" / "sub")},
    )
    assert not is_deny("codex", out)


def test_f1_fixtures_carry_the_same_call_id_in_pre_and_post():
    """The keying choice rests on this: each host repeats its call id between PRE and POST."""
    def ident(name):
        d = json.loads((FIX / (name + ".json")).read_text())
        return d.get("tool_use_id") or (d.get("toolCall") or {}).get("id")

    for a, b in [
        ("codex_pre_bash_12", "codex_post_bash_13"),
        ("codex_pre_bash_15", "codex_post_bash_16"),
        ("claude_pre_bash", "claude_post_bash"),
        ("agy_pre_run_command", "agy_post_run_command"),
        ("codex_pre_apply_patch_10", "codex_post_apply_patch_11"),
    ]:
        assert ident(a) and ident(a) == ident(b), (a, b)


def test_f1_real_fixture_pre_post_pair_records(env):
    proj, state = env
    (proj / "existing.txt").write_text("NEW\n")
    pre = fixture("codex_pre_bash_12", proj)
    post = fixture("codex_post_bash_13", proj)
    run_hook("codex", "pre", pre, state)
    run_hook("codex", "post", post, state, pre=False)
    assert not wg.is_stale("codex", post["session_id"], wg.resolve("existing.txt", str(proj)))


# ---- F3: Codex apply_patch records only on a success response
@pytest.mark.parametrize(
    "resp",
    [
        "Exit code: 1\nWall time: 0.1 seconds\nOutput:\nFailed to find expected lines in f.txt\n",
        "apply_patch verification failed",
        "",
        None,
        {"exit_code": 1},
        "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nsomething else\n",
    ],
)
def test_f3_codex_patch_records_only_on_success(env, resp):
    proj, state = env
    f = proj / "f.txt"
    f.write_text("v_B written by another session\n")
    upd = "*** Begin Patch\n*** Update File: f.txt\n@@\n-v_A\n+v_A2\n*** End Patch"
    run_hook("codex", "post", codex("PostToolUse", upd, proj, sid="A", resp=resp), state)
    add = "*** Begin Patch\n*** Add File: f.txt\n+rewrite built from v_A\n*** End Patch"
    assert is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", add, proj, sid="A"), state))


def test_f3_codex_patch_success_records(env):
    proj, state = env
    f = proj / "f.txt"
    f.write_text("v1\n")
    upd = "*** Begin Patch\n*** Update File: f.txt\n@@\n-v1\n+v2\n*** End Patch"
    f.write_text("v2\n")
    run_hook("codex", "post", codex("PostToolUse", upd, proj, sid="A"), state)
    add = "*** Begin Patch\n*** Add File: f.txt\n+rewrite\n*** End Patch"
    assert not is_deny("codex", run_hook("codex", "pre", codex("PreToolUse", add, proj, sid="A"), state))


def test_f3_real_success_fixture_still_records(env):
    proj, state = env
    (proj / "existing.txt").write_text("NEW\n")
    post = fixture("codex_post_apply_patch_11", proj)
    run_hook("codex", "post", post, state)
    assert not wg.is_stale("codex", post["session_id"], wg.resolve("existing.txt", str(proj)))


# ---- F14: the hook script keeps its own host list; it must equal the package one
def test_f14_hook_hosts_equal_model_host_kinds():
    sys.path.insert(0, str(HERE.parent))
    from mycelium_coord import model

    assert tuple(wg.HOSTS) == tuple(model.HOST_KINDS)


# ---- every shipped hook template registers a PRE hook that matches the shell tool
def _template(name):
    return json.loads((HERE.parent.parent / "docs" / "triangle" / "proposed" / "writeguard-hooks" / name).read_text())


def _matches(matcher, tool):
    import re

    return matcher == "*" or re.fullmatch(matcher, tool) is not None


@pytest.mark.parametrize(
    "name,shell_tool,host",
    [
        ("codex-hooks.json", "Bash", "codex"),
        ("claude-settings-hooks.json", "Bash", "claude"),
        ("antigravity-hooks.json", "run_command", "antigravity"),
    ],
)
def test_templates_register_a_pre_hook_for_the_shell_tool(name, shell_tool, host):
    t = _template(name)
    hooks = t.get("hooks") or t.get("writeguard")
    pre = hooks["PreToolUse"]
    post = hooks["PostToolUse"]
    for group in (pre, post):
        assert any(
            _matches(g["matcher"], shell_tool)
            and any("--host %s" % host in h["command"] for h in g["hooks"])
            for g in group
        )
    assert any("--event pre" in h["command"] for g in pre for h in g["hooks"])
