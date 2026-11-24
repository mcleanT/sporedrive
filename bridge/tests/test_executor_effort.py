"""Session-only effort switch in the cmux-driver launcher (`executor_session.py effort`).

SIMULATED (kind: simulated): a local model of the native v2.1.280 `/effort` UI observed on a live
canary (slider row, ▲ marker, "s for this session only" hint, `Thinking:` footer) and a synthetic
transcript. No cmux, no Claude, no model calls. The live path is recorded in the run-4 receipt.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAUNCHER = REPO / "src" / "codex" / "skills" / "cmux-driver" / "scripts" / "executor_session.py"
SID = "0a4b7360-ff4b-42ef-bbfe-d9cde350e49a"
SURF = "FFD17380-9845-45E1-9A6A-C296D471053B"
LABELS = ["low", "medium", "high", "xhigh", "max", "ultracode"]


@pytest.fixture()
def es(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("executor_session_effort_test", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("CMUX_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    mod._settings = {"~/.claude/settings.json": "h1"}
    monkeypatch.setattr(mod, "settings_hashes", lambda: dict(mod._settings))
    return mod


class FakeUI:
    """Minimal model of the native effort UI. Flags inject the failure shapes."""

    def __init__(self, level="medium", draft="", slider=True, hint=True, marker_offset=0,
                 arrow_ignored=False, commit_ignored=False, esc_leaves_text=False, on_commit=None):
        self.level, self.draft = level, draft
        self.slider_ok, self.hint, self.marker_offset = slider, hint, marker_offset
        self.arrow_ignored, self.commit_ignored, self.esc_leaves_text = arrow_ignored, commit_ignored, esc_leaves_text
        self.on_commit = on_commit
        self.open = False
        self.pos = None
        self.sent = []

    def screen(self):
        if self.open:
            label = " " * 33 + "low     medium     high     xhigh      max       ultracode"
            starts = [label.index(w) for w in ("low", "medium", "high", "xhigh", " max ", "ultracode")]
            starts[4] += 1
            i = min(max(self.pos + self.marker_offset, 0), 5)
            center = starts[i] + (len(LABELS[i]) - 1) // 2
            marker = "─" * center + "▲" + "─" * 10
            hint = "   ←/→ to adjust · Enter to confirm · " + ("s for this session only · " if self.hint else "") + "Esc to cancel"
            return ["", "   Effort", marker, label, hint]
        return [
            "",
            "❯ " + self.draft,
            "  Model: Opus 5.5 | Context:  Ctx Used: 0.0% | v2.1.280 | Session: 8s",
            f"  Thinking: {self.level} | Mem: 31.4G/64.0G",
        ]

    def text(self, s):
        self.sent.append(("text", s))
        if self.open and s == "s":
            if not self.commit_ignored:
                self.level = LABELS[self.pos]
                if self.on_commit:
                    self.on_commit()
            self.open = False
            self.draft = ""
        elif not self.open:
            self.draft += s

    def key(self, k):
        self.sent.append(("key", k))
        if k == "enter" and not self.open and self.draft == "/effort":
            if self.slider_ok:
                self.open, self.pos, self.draft = True, LABELS.index(self.level), ""
        elif k in ("left", "right") and self.open and not self.arrow_ignored:
            self.pos = max(0, min(5, self.pos + (1 if k == "right" else -1)))
        elif k == "escape":
            self.open = False
            if not self.esc_leaves_text:
                self.draft = ""


def ok_identity(lines):
    return True, None, {"pid": 1}


def req(**kw):
    r = {"request_id": "R1", "session_id": SID, "surface_uuid": SURF, "requested": "high", "authority": "policy",
         "owner_ref": None, "pin_action": None, "reason": "missed dependency", "checkpoint": "C1", "attested_drained": True}
    r.update(kw)
    return r


def transcript(tmp_path, entries):
    p = tmp_path / "t.jsonl"
    p.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return p


def run(es, ui, tp=None, **kw):
    return es.apply_effort(req(**kw), ui, ok_identity, tp, pause=0)


def test_successful_transition_then_runtime_confirmation(es, tmp_path):
    ui = FakeUI("medium")
    r = run(es, ui)
    assert r["outcome"] == "applied_ui" and r["prior"] == "medium" and r["observed_ui"] == "high"
    assert r["keys_sent"] == ["text:/effort", "enter", "right", "text:s"]
    assert r["settings_unchanged"] is True
    # an assistant record BEFORE the commit never confirms; the first one after it does
    tp = transcript(tmp_path, [
        {"type": "assistant", "timestamp": "2000-01-01T00:00:00Z", "effort": "medium", "perTurnEffort": "medium"},
        {"type": "assistant", "timestamp": "2999-01-01T00:00:00Z", "effort": "high", "perTurnEffort": "high"},
    ])
    n = len(ui.sent)
    r2 = run(es, ui, tp)
    assert r2["outcome"] == "applied" and r2["phase"] == "reconcile" and r2["keys_sent"] == []
    assert len(ui.sent) == n


def test_runtime_mismatch_is_distinct(es, tmp_path):
    ui = FakeUI("medium")
    run(es, ui)
    tp = transcript(tmp_path, [{"type": "assistant", "timestamp": "2999-01-01T00:00:00Z", "effort": "high", "perTurnEffort": "medium"}])
    assert run(es, ui, tp)["outcome"] == "runtime_mismatch"


def test_downward_transition_uses_left(es):
    ui = FakeUI("high")
    r = run(es, ui, requested="medium")
    assert r["outcome"] == "applied_ui" and "left" in r["keys_sent"] and ui.level == "medium"


def test_owner_pin_blocks_policy_and_release_needs_owner_ref(es):
    ui = FakeUI("medium")
    assert run(es, ui, request_id="O1", authority="owner", pin_action="pin")["code"] == "owner_ref_required"
    r = run(es, ui, request_id="O2", authority="owner", owner_ref="owner:msg-1", pin_action="pin", requested="medium")
    assert r["outcome"] == "no_op"
    n = len(ui.sent)
    r = run(es, ui, request_id="P1")
    assert (r["outcome"], r["code"]) == ("refused", "owner_pin") and len(ui.sent) == n
    # a policy no-op later does not erase the pin
    assert run(es, ui, request_id="P2", requested="medium")["code"] == "owner_pin"
    assert run(es, ui, request_id="P3", pin_action="release")["code"] == "pin_requires_owner"
    run(es, ui, request_id="O3", authority="owner", owner_ref="owner:msg-2", pin_action="release", requested="medium")
    assert run(es, ui, request_id="P4")["outcome"] == "applied_ui"


@pytest.mark.parametrize("level", ["xhigh", "max", "ultracode", "bogus"])
def test_unsupported_level_refused_without_keys(es, level):
    ui = FakeUI("medium")
    r = run(es, ui, requested=level)
    assert (r["outcome"], r["code"]) == ("refused", "unsupported_level") and ui.sent == []


def test_out_of_policy_current_is_owner_state(es):
    ui = FakeUI("xhigh")
    r = run(es, ui)
    assert r["code"] == "out_of_policy_current" and ui.sent == []


def test_busy_draft_foreign_and_undrained_send_nothing(es):
    ui = FakeUI("medium", draft="half-typed")
    assert run(es, ui)["code"] == "prompt_not_empty" and ui.sent == []
    ui = FakeUI("medium")
    r = es.apply_effort(req(request_id="F1"), ui, lambda l: (False, "foreign_session", {}), None, pause=0)
    assert r["code"] == "foreign_session" and ui.sent == []
    assert run(es, ui, request_id="D1", attested_drained=False)["code"] == "not_drained" and ui.sent == []
    ui.open, ui.pos = True, 1  # a slider/dialog already showing = not at an empty prompt
    assert run(es, ui, request_id="B1")["code"] == "not_at_prompt"


def test_surface_lock_serializes(es):
    lock = es.SurfaceLock(SURF)
    assert lock.acquire()
    ui = FakeUI("medium")
    try:
        assert run(es, ui)["code"] == "busy_lock" and ui.sent == []
    finally:
        lock.release()
    # a lock left by a dead pid is stale and replaced
    lock.path.write_text("999999")
    assert run(es, ui, request_id="R2")["outcome"] == "applied_ui"


def test_noop_and_identical_replay_send_nothing(es):
    ui = FakeUI("high")
    assert run(es, ui)["outcome"] == "no_op" and ui.sent == []
    ui = FakeUI("medium")
    run(es, ui, request_id="R9", requested="low")
    ui.sent.clear()
    r = run(es, ui, request_id="R9", requested="low")
    assert r["phase"] == "reconcile" and ui.sent == []  # applied_ui replays as a read-only reconcile
    run(es, ui, request_id="R10", requested="max")
    r = run(es, ui, request_id="R10", requested="max")
    assert r["code"] == "unsupported_level" and ui.sent == []  # validation refusals repeat, never type
    assert run(es, ui, request_id="R9", requested="high")["code"] == "request_conflict"


def test_unknown_outcome_reconciles_read_only(es):
    ui = FakeUI("medium", commit_ignored=True)
    r = run(es, ui)
    assert (r["outcome"], r["code"]) == ("unknown", "footer_unchanged")
    ui.sent.clear()
    r2 = run(es, ui)
    assert r2["outcome"] == "unknown" and r2["phase"] == "reconcile" and ui.sent == []


def test_settings_hash_change_is_reported_not_repaired(es):
    def mutate():
        es._settings["~/.claude/settings.json"] = "h2"

    ui = FakeUI("medium", on_commit=mutate)
    r = run(es, ui)
    assert r["code"] == "settings_changed" and r["settings_unchanged"] is False
    assert es._settings["~/.claude/settings.json"] == "h2"


def test_slider_absent_and_hint_missing_escape(es):
    ui = FakeUI("medium", slider=False)
    r = run(es, ui)
    # `/effort` stays typed when no slider opened: Esc clears it here; a leftover would be unknown
    assert r["code"] == "slider_absent" and ("key", "escape") in ui.sent
    ui = FakeUI("medium", hint=False)
    r = run(es, ui, request_id="R2")
    assert (r["outcome"], r["code"]) == ("refused", "session_only_hint_missing")
    assert ("text", "s") not in ui.sent and ("key", "right") not in ui.sent


def test_marker_mismatch_before_and_after_arrows(es):
    ui = FakeUI("medium", marker_offset=1)
    r = run(es, ui)
    assert r["code"] == "state_mismatch" and ("key", "right") not in ui.sent and ("text", "s") not in ui.sent
    ui = FakeUI("medium", arrow_ignored=True)
    r = run(es, ui, request_id="R2")
    assert r["code"] == "state_mismatch" and ("text", "s") not in ui.sent and ui.level == "medium"


def test_leftover_prompt_text_after_escape_is_unknown(es):
    ui = FakeUI("medium", slider=False, esc_leaves_text=True)
    r = run(es, ui)
    assert (r["outcome"], r["code"]) == ("unknown", "slider_absent")
    assert ui.sent.count(("key", "enter")) == 1  # never cleared with a second Enter


def test_parser_exposes_effort_without_settings_or_persistent_paths(es):
    src = LAUNCHER.read_text()
    assert '"/effort"' in src and 'keys.text("s")' in src
    assert "/effort high" not in src.split("def apply_effort")[1].split("def cmd_effort")[0]
    assert "write_text" not in src.split("def settings_hashes")[1].split("def first_runtime_effort")[0]
    a = es.build_parser().parse_args(["effort", "--surface", SURF, "--pid", "1", "--session-id", SID, "--cwd", "/x",
                                      "--to", "high", "--request-id", "R", "--authority", "policy", "--reason", "r",
                                      "--checkpoint", "c", "--attest-drained"])
    assert a.func is es.cmd_effort


# ---- review R4-REVIEW-1 regressions -------------------------------------------------------------

SID_B = "11111111-2222-4333-8444-555555555555"
SURF_B = "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"


def _two_sessions(es, monkeypatch, tmp_path, ui):
    """Two live Opus sessions: pid 111 = (SID, SURF, cwd A), pid 222 = (SID_B, SURF_B, cwd B)."""
    cwd_a, cwd_b = tmp_path / "a", tmp_path / "b"
    cwd_a.mkdir(), cwd_b.mkdir()
    procs = {
        111: (f"/x/claude --model opus --session-id {SID} --effort medium", SURF, str(cwd_a)),
        222: (f"/x/claude --model opus --session-id {SID_B} --effort medium", SURF_B, str(cwd_b)),
    }
    monkeypatch.setattr(es, "proc_argv", lambda pid: procs.get(pid, (None,))[0])
    monkeypatch.setattr(es, "proc_cmux_surface", lambda pid: procs[pid][1] if pid in procs else None)
    monkeypatch.setattr(es, "proc_cwd", lambda pid: procs[pid][2] if pid in procs else None)
    monkeypatch.setattr(es, "CmuxKeys", lambda cm, surface: ui)
    return cwd_a, cwd_b


def _cli(es, pid, sid, surface, cwd, rid="C1", to="high"):
    return es.main(["effort", "--surface", surface, "--pid", str(pid), "--session-id", sid, "--cwd", str(cwd),
                    "--to", to, "--request-id", rid, "--authority", "policy", "--reason", "r", "--checkpoint", "c",
                    "--attest-drained", "--expected-model", "opus", "--pause-s", "0"])


def test_cmd_effort_refuses_surface_not_bound_to_the_pid(es, monkeypatch, tmp_path, capsys):
    ui = FakeUI("medium")
    cwd_a, cwd_b = _two_sessions(es, monkeypatch, tmp_path, ui)
    # pid/session of A, surface of B (both Opus, both at an empty prompt): refused, no keys
    assert _cli(es, 111, SID, SURF_B, cwd_a, rid="M1") == 3
    assert json.loads(capsys.readouterr().out)["code"] == "surface_mismatch" and ui.sent == []
    assert _cli(es, 111, SID, SURF, cwd_b, rid="M2") == 3
    assert json.loads(capsys.readouterr().out)["code"] == "cwd_mismatch" and ui.sent == []
    assert _cli(es, 222, SID, SURF_B, cwd_b, rid="M3") == 3
    assert json.loads(capsys.readouterr().out)["code"] == "foreign_session" and ui.sent == []
    monkeypatch.setattr(es, "proc_cmux_surface", lambda pid: None)
    assert _cli(es, 111, SID, SURF, cwd_a, rid="M4") == 3
    assert json.loads(capsys.readouterr().out)["code"] == "surface_unbound" and ui.sent == []


def test_cmd_effort_bound_identity_applies(es, monkeypatch, tmp_path, capsys):
    ui = FakeUI("medium")
    cwd_a, _ = _two_sessions(es, monkeypatch, tmp_path, ui)
    assert _cli(es, 111, SID, SURF, cwd_a) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["outcome"] == "applied_ui" and out["identity"]["process_surface"] == SURF


class Killed(BaseException):
    pass


def test_interrupted_send_leaves_pending_and_replay_never_resends(es):
    ui = FakeUI("medium")
    orig = ui.text

    def dies_on_commit(s):
        orig(s)
        if s == "s":
            raise Killed()

    ui.text = dies_on_commit
    with pytest.raises(Killed):
        run(es, ui)
    chain = es._read_chain(SID)
    assert [r["outcome"] for r in chain] == ["pending"]
    ui.text = orig
    ui.sent.clear()
    r = run(es, ui)
    assert r["outcome"] == "unknown" and r["phase"] == "reconcile" and ui.sent == []
    assert r["observed_ui"] == "high"  # observed, recorded; still not claimed as applied


def test_torn_or_malformed_chain_fails_closed_and_keeps_the_pin(es):
    ui = FakeUI("medium")
    run(es, ui, request_id="O1", authority="owner", owner_ref="owner:msg-1", pin_action="pin", requested="medium")
    p = es._chain_path(SID)
    good = p.read_text()
    p.write_text(good + '{"request_id": "torn')  # interrupted append
    r = run(es, ui, request_id="P1")
    assert (r["outcome"], r["code"]) == ("refused", "history_damaged") and ui.sent == []
    assert p.read_text().endswith('"torn')  # not repaired
    p.write_text("not json\n" + good)
    assert run(es, ui, request_id="P2")["code"] == "history_damaged" and ui.sent == []


def test_incomplete_lock_is_not_stale(es):
    lock = es.SurfaceLock(SURF)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text("")  # create happened, pid not written yet
    ui = FakeUI("medium")
    r = run(es, ui)
    assert r["code"] == "busy_lock" and ui.sent == [] and lock.path.exists()


def test_marker_mismatch_with_leftover_prompt_is_one_unknown_receipt(es):
    ui = FakeUI("medium", marker_offset=1, esc_leaves_text=True)
    orig_key = ui.key

    def key(k):
        orig_key(k)
        if k == "escape":
            ui.draft = "/effort"  # Esc closed the slider but left the command text behind

    ui.key = key
    r = run(es, ui)
    assert (r["outcome"], r["code"]) == ("unknown", "state_mismatch")
    assert "marker at" in r["note"] and "never cleared with Enter" in r["note"]
    assert [x["outcome"] for x in es._read_chain(SID)] == ["pending", "unknown"]
