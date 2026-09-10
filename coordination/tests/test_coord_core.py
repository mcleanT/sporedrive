#!/usr/bin/env python3
"""Focused component regression for the shared coordination core (mycelium_coord).

Covers the happy path AND the four core-review findings (MYCELIUM-INTEGRATION r1):
  F1 completion/ack eligibility, F2 crash-safe idempotency + hash identity,
  F3 attach identity stability / detached, F4 managed-root ancestor symlink escape.

Run: python3 -m pytest coordination/tests/test_coord_core.py   (or execute directly)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mycelium_coord import Coordinator, ProtocolError, StoreError  # noqa: E402
from mycelium_coord.coord import Coordinator as C  # noqa: E402
from mycelium_coord.store import CoordStore, valid_id  # noqa: E402


def _co(root):
    return Coordinator(CoordStore(root))


HOST_C = {"host": "codex", "session": "codex-1", "native_id": "task-01a07d32"}
HOST_CL = {"host": "claude", "session": "claude-ea7b12c1", "native_id": "pid-62036"}
HOST_O = {"host": "claude", "session": "obs-1", "native_id": "pid-9"}


def _seed(co, wt="/wt/projA"):
    co.create_task("t1", project="projA", worktree_realpath=wt, revision=1)
    co.attach("t1", "c", role="supervisor", worktree_realpath="/outside/ctrl", host=HOST_C)
    co.attach("t1", "cl", role="executor", worktree_realpath=wt, host=HOST_CL)
    co.attach("t1", "other", role="observer", worktree_realpath="/outside/obs", host=HOST_O)


def test_happy_path_states_are_distinct():
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)
        r = co.send(message_id="m1", task_id="t1", sender="c", recipient="cl",
                    kind="task", task_revision=1, text="do the thing", host=HOST_C)
        assert r["seq"] == 1 and not r["idempotent"]
        # persisted (not yet delivered/acked)
        assert co.message_state("t1", "cl", "m1") == "persisted"
        # delivered: past cl's notification cursor
        inbox = co.inbox("t1", "cl")
        assert [m["message_id"] for m in inbox["messages"]] == ["m1"]
        co.set_cursor("t1", "cl", inbox["next_after_seq"])
        assert co.message_state("t1", "cl", "m1") == "delivered"
        # acknowledged
        co.ack("t1", "cl", "m1")
        assert co.message_state("t1", "cl", "m1") == "acknowledged"
        # completed only via a proper completion_receipt from the addressed executor WITH
        # verifiable artifact evidence that checks out on disk
        ev = Path(t) / "EVIDENCE.md"
        ev.write_text("fix applied; 11 tests pass")
        co.send(message_id="done1", task_id="t1", sender="cl", recipient="c",
                kind="completion_receipt", task_revision=1, text="fixed", reply_to="m1",
                artifacts=[{"file": str(ev), "contains": "11 tests pass"}], host=HOST_CL)
        assert co.message_state("t1", "cl", "m1") == "completed"


def test_F1_completion_from_unaddressed_actor_is_not_completion():
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)
        co.send(message_id="m1", task_id="t1", sender="c", recipient="cl",
                kind="task", task_revision=1, text="do it", host=HOST_C)
        # observer 'other' tries to fabricate completion (wrong revision AND unaddressed AND no arts):
        # build_message now rejects a completion with no artifacts outright.
        try:
            co.send(message_id="fake", task_id="t1", sender="other", recipient="c",
                    kind="completion_receipt", task_revision=999, text="done",
                    reply_to="m1", host=HOST_O)
            assert False, "completion with no artifacts must be rejected"
        except ProtocolError as e:
            assert e.code == "invalid_completion"
        # even WITH artifacts, an unaddressed observer's completion does not grant completed
        co.send(message_id="fake2", task_id="t1", sender="other", recipient="c",
                kind="completion_receipt", task_revision=1, text="done",
                reply_to="m1", artifacts=["/tmp/x"], host=HOST_O)
        assert co.message_state("t1", "cl", "m1") == "persisted"
        # and a wrong-revision completion from the RIGHT actor also does not complete it
        co.send(message_id="wrongrev", task_id="t1", sender="cl", recipient="c",
                kind="completion_receipt", task_revision=999, text="done",
                reply_to="m1", artifacts=["/tmp/y"], host=HOST_CL)
        assert co.message_state("t1", "cl", "m1") == "persisted"


def test_F1_ack_must_be_from_addressed_recipient():
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)
        co.send(message_id="m1", task_id="t1", sender="c", recipient="cl",
                kind="task", task_revision=1, text="do it", host=HOST_C)
        try:
            co.ack("t1", "other", "m1")
            assert False, "unaddressed observer must not ack"
        except ProtocolError as e:
            assert e.code == "ack_not_addressed"
        co.ack("t1", "cl", "m1")  # the addressed executor can


def test_F2_interrupted_send_no_duplicate_index_fail_after_body():
    # Original finding-2 scenario: body durable, then the ids-index write fails. Retry must be
    # idempotent (repair the index) and leave exactly one copy at the same seq.
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)
        real_write = co.store.write
        state = {"boom": True}

        def flaky_write(rel, obj):
            if state["boom"] and "/ids/" in rel:
                state["boom"] = False
                raise OSError("crash after body durable, before index")
            return real_write(rel, obj)

        co.store.write = flaky_write
        try:
            co.send(message_id="m1", task_id="t1", sender="c", recipient="cl",
                    kind="task", task_revision=1, text="do it", host=HOST_C)
            assert False, "the injected fault should surface"
        except OSError:
            pass
        co.store.write = real_write
        r = co.send(message_id="m1", task_id="t1", sender="c", recipient="cl",
                    kind="task", task_revision=1, text="do it", host=HOST_C)
        assert r["idempotent"] is True and r["seq"] == 1
        got = [m["message_id"] for m in co.inbox("t1", "cl")["messages"]]
        assert got == ["m1"], f"duplicate leaked: {got}"


def test_F2_interrupted_send_no_duplicate_body_fail():
    # Body write fails -> no seq consumed -> retry republishes fresh, still exactly one copy.
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)
        real_write = co.store.write
        state = {"boom": True}

        def flaky_write(rel, obj):
            if state["boom"] and "/messages/" in rel:
                state["boom"] = False
                raise OSError("crash on body")
            return real_write(rel, obj)

        co.store.write = flaky_write
        try:
            co.send(message_id="m1", task_id="t1", sender="c", recipient="cl",
                    kind="task", task_revision=1, text="do it", host=HOST_C)
            assert False
        except OSError:
            pass
        co.store.write = real_write
        r = co.send(message_id="m1", task_id="t1", sender="c", recipient="cl",
                    kind="task", task_revision=1, text="do it", host=HOST_C)
        assert r["idempotent"] is False and r["seq"] == 1
        got = [m["message_id"] for m in co.inbox("t1", "cl")["messages"]]
        assert got == ["m1"], f"duplicate leaked: {got}"


def test_F2_same_id_different_sender_is_conflict():
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)
        co.send(message_id="m1", task_id="t1", sender="c", recipient="cl",
                kind="task", task_revision=1, text="do it", host=HOST_C)
        # same id + text but different SENDER -> content_hash differs -> conflict (not silent reuse)
        try:
            co.send(message_id="m1", task_id="t1", sender="cl", recipient="c",
                    kind="task", task_revision=1, text="do it", host=HOST_CL)
            assert False, "same id different sender must conflict"
        except ProtocolError as e:
            assert e.code == "idempotency_conflict"


def test_F3_attach_identity_stability_and_detach():
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)
        # a second attach that changes role/host/worktree is a conflict without allow_transition
        try:
            co.attach("t1", "cl", role="supervisor", worktree_realpath="/outside/x", host=HOST_C)
            assert False, "silent identity replacement must be rejected"
        except ProtocolError as e:
            assert e.code == "participant_identity_conflict"
        # executor worktree must match the task worktree
        try:
            co.attach("t1", "cl2", role="executor", worktree_realpath="/wrong/wt", host=HOST_CL)
            assert False, "executor worktree mismatch must be rejected"
        except ProtocolError as e:
            assert e.code == "executor_worktree_mismatch"
        # detached participant cannot act
        co.detach("t1", "cl")
        try:
            co.send(message_id="m2", task_id="t1", sender="cl", recipient="c",
                    kind="progress", task_revision=1, text="x", host=HOST_CL)
            assert False, "detached participant must not send"
        except ProtocolError as e:
            assert e.code == "participant_detached"


def test_F3_valid_id_edge_cases():
    assert valid_id("m1") and valid_id("a.b-c:d_e")
    assert not valid_id(None) and not valid_id(".") and not valid_id("..")
    assert not valid_id("a\n") and not valid_id("a/b") and not valid_id("")
    assert not valid_id("x" * 121)


def test_F4_ancestor_symlink_is_refused():
    with tempfile.TemporaryDirectory() as t:
        outside = Path(t) / "outside"
        outside.mkdir()
        linked = Path(t) / "linked-parent"
        os.symlink(outside, linked)
        try:
            CoordStore(linked / "coordination")
            assert False, "a symlinked managed-root ancestor must be refused"
        except StoreError as e:
            assert e.code == "unsafe_state_path"
        # and nothing was created under the real target
        assert not (outside / "coordination").exists()


def test_F4_normal_temp_path_still_works():
    # the default temp/home firmlink (/var -> /private/var) must NOT be broken by F4
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        co.create_task("t9", project="p", worktree_realpath="/wt", revision=0)
        assert co.get_task("t9")["task_id"] == "t9"


def test_wait_and_resume_bounded():
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)
        # wait times out truthfully when nothing addressed arrives
        res = co.wait("t1", "cl", timeout_s=0.2)
        assert res["timed_out"] is True and res["messages"] == []
        co.send(message_id="m1", task_id="t1", sender="c", recipient="cl",
                kind="task", task_revision=1, text="hi", host=HOST_C)
        res = co.wait("t1", "cl", timeout_s=2.0)
        assert res["timed_out"] is False and res["messages"][0]["message_id"] == "m1"
        # resume returns checkpoint + pending unacked, not a transcript
        co.publish_checkpoint("t1", revision=1, participant_id="cl",
                              checkpoint={"next": "fix"}, authorization_ref="brief-r1")
        rs = co.resume("t1", "cl")
        assert rs["checkpoint"]["revision"] == 1
        assert rs["pending_count"] == 1 and rs["pending_unacked"][0]["message_id"] == "m1"


def test_checkpoint_immutable_revision():
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)
        co.publish_checkpoint("t1", revision=2, participant_id="cl", checkpoint={"a": 1})
        # same content -> idempotent
        assert co.publish_checkpoint("t1", revision=2, participant_id="cl", checkpoint={"a": 1})["idempotent"]
        # different content at same revision -> conflict
        try:
            co.publish_checkpoint("t1", revision=2, participant_id="cl", checkpoint={"a": 2})
            assert False
        except ProtocolError as e:
            assert e.code == "checkpoint_conflict"


def test_B_completion_claimed_until_evidence_verifies():
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)
        co.send(message_id="m1", task_id="t1", sender="c", recipient="cl",
                kind="task", task_revision=1, text="do it", host=HOST_C)
        # addressed executor claims completion but the artifact is a bare, unverified reference
        co.send(message_id="claim", task_id="t1", sender="cl", recipient="c",
                kind="completion_receipt", task_revision=1, text="done",
                reply_to="m1", artifacts=["checks/x/EVIDENCE.md"], host=HOST_CL)
        assert co.message_state("t1", "cl", "m1") == "completion_claimed"
        # a structured evidence entry that does NOT verify (wrong sha) is still only a claim
        bad = Path(t) / "bad.md"; bad.write_text("nope")
        import hashlib
        wrong = hashlib.sha256(b"different").hexdigest()
        co.send(message_id="claim2", task_id="t1", sender="cl", recipient="c",
                kind="completion_receipt", task_revision=1, text="done",
                reply_to="m1", artifacts=[{"file": str(bad), "sha256": wrong}], host=HOST_CL)
        assert co.message_state("t1", "cl", "m1") == "completion_claimed"
        # now a verifying entry flips it to completed
        good = Path(t) / "good.md"; good.write_text("all green")
        gh = hashlib.sha256(good.read_bytes()).hexdigest()
        co.send(message_id="claim3", task_id="t1", sender="cl", recipient="c",
                kind="completion_receipt", task_revision=1, text="done",
                reply_to="m1", artifacts=[{"file": str(good), "sha256": gh}], host=HOST_CL)
        assert co.message_state("t1", "cl", "m1") == "completed"


def test_A_repaired_message_not_hidden_behind_consumed_cursor():
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)
        real_write = co.store.write
        state = {"boom": True}

        def flaky_write(rel, obj):
            if state["boom"] and "/messages/" in rel and "-first.json" in rel:
                state["boom"] = False
                raise OSError("crash on first body")
            return real_write(rel, obj)

        # 1. 'first' fails on body write (consumes no durable seq)
        co.store.write = flaky_write
        try:
            co.send(message_id="first", task_id="t1", sender="c", recipient="cl",
                    kind="task", task_revision=1, text="first", host=HOST_C)
            assert False
        except OSError:
            pass
        co.store.write = real_write
        # 2. 'second' publishes successfully -> it takes seq 1 (first consumed nothing)
        r2 = co.send(message_id="second", task_id="t1", sender="c", recipient="cl",
                     kind="task", task_revision=1, text="second", host=HOST_C)
        assert r2["seq"] == 1
        # 3. recipient consumes 'second' and saves the normal cursor
        box = co.inbox("t1", "cl")
        assert [m["message_id"] for m in box["messages"]] == ["second"]
        co.set_cursor("t1", "cl", box["next_after_seq"])
        # 4. retry 'first' -> republishes at a FRESH seq ahead of the cursor (seq 2)
        r1 = co.send(message_id="first", task_id="t1", sender="c", recipient="cl",
                     kind="task", task_revision=1, text="first", host=HOST_C)
        assert r1["seq"] == 2 and not r1["idempotent"]
        # 5. inbox after the saved cursor STILL delivers the repaired 'first' (not hidden)
        after = co.inbox("t1", "cl", after_seq=box["next_after_seq"])
        assert [m["message_id"] for m in after["messages"]] == ["first"], \
            f"repaired message hidden behind cursor: {after}"


def test_checkpoint_latest_pointer_recovers_after_interrupted_write():
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)
        real_write = co.store.write
        state = {"boom": True}

        def flaky(rel, obj):
            if state["boom"] and rel.endswith("checkpoints/latest.json"):
                state["boom"] = False
                raise OSError("crash writing latest pointer")
            return real_write(rel, obj)

        co.store.write = flaky
        try:
            co.publish_checkpoint("t1", revision=2, participant_id="cl", checkpoint={"a": 1})
            assert False, "the injected latest-pointer fault should surface"
        except OSError:
            pass
        co.store.write = real_write
        # the body is durable but latest.json never got written; read must still recover it,
        # and the idempotent retry must repair the pointer
        assert co.read_checkpoint("t1") is not None, "durable checkpoint unreachable after crash"
        r = co.publish_checkpoint("t1", revision=2, participant_id="cl", checkpoint={"a": 1})
        assert r["idempotent"] is True
        ck = co.read_checkpoint("t1")
        assert ck is not None and ck["revision"] == 2, f"latest pointer not repaired: {ck}"


def test_session_selection_is_per_native_session():
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        # two Claude sessions in the SAME worktree, each its own executor participant + session id
        co.create_task("t1", project="p", worktree_realpath="/wt", revision=1)
        co.attach("t1", "cla", role="executor", worktree_realpath="/wt",
                  host={"host": "claude", "session": "sid-aaaa", "native_id": "pid-a"})
        co.attach("t1", "clb", role="executor", worktree_realpath="/wt",
                  host={"host": "claude", "session": "sid-bbbb", "native_id": "pid-b"})
        assert co.resolve_session("claude", "sid-aaaa")["participant"] == "cla"
        assert co.resolve_session("claude", "sid-bbbb")["participant"] == "clb"
        # an unrelated session with no selection resolves to nothing (no cross-contamination)
        assert co.resolve_session("claude", "sid-unrelated") is None
        # a detached participant's session no longer resolves
        co.detach("t1", "cla")
        assert co.resolve_session("claude", "sid-aaaa") is None


def test_T1_controlled_rebind_refuses_stale_session_selection():
    # session-transition review 1: after a controlled rebind the OLD native session must not keep
    # resolving to the participant, and select-session must not bind a foreign native session.
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        co.create_task("t1", project="p", worktree_realpath="/wt", revision=1)
        old = {"host": "claude", "session": "sid-old", "native_id": "pid-1"}
        new = {"host": "claude", "session": "sid-new", "native_id": "pid-2"}
        co.attach("t1", "cl", role="executor", worktree_realpath="/wt", host=old)
        assert co.resolve_session("claude", "sid-old")["participant"] == "cl"
        # controlled identity transition to a new native session
        co.attach("t1", "cl", role="executor", worktree_realpath="/wt", host=new,
                  allow_transition=True)
        # the participant is now on sid-new; the stale sid-old selection must NOT resolve
        assert co.resolve_session("claude", "sid-old") is None
        assert co.resolve_session("claude", "sid-new")["participant"] == "cl"
        # select-session cannot bind a DIFFERENT native session without a controlled transition
        try:
            co.select_session("t1", "cl", host_kind="claude", session_id="sid-foreign")
            assert False, "expected session_identity_mismatch"
        except ProtocolError as e:
            assert e.code == "session_identity_mismatch"
        # re-affirming the CURRENT session is fine — intended same-session compaction still works
        co.select_session("t1", "cl", host_kind="claude", session_id="sid-new")
        assert co.resolve_session("claude", "sid-new")["participant"] == "cl"


def test_T2_verify_participant_session_blocks_env_impersonation():
    # session-transition review 2: the hook env fallback must validate this session's native
    # identity against the participant; an unrelated/missing session cannot impersonate it.
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)  # cl is claude session "claude-ea7b12c1"
        assert co.verify_participant_session("t1", "cl", "claude", "claude-ea7b12c1") is True
        # an unrelated inherited-env session cannot impersonate cl
        assert co.verify_participant_session("t1", "cl", "claude", "unrelated-session") is False
        # a missing native session id is refused (no identity => no impersonation)
        assert co.verify_participant_session("t1", "cl", "claude", "") is False
        # wrong host kind is refused
        assert co.verify_participant_session("t1", "cl", "codex", "claude-ea7b12c1") is False
        # an unknown participant is refused
        assert co.verify_participant_session("t1", "nope", "claude", "claude-ea7b12c1") is False
        # a detached participant cannot be resumed via env
        co.detach("t1", "cl")
        assert co.verify_participant_session("t1", "cl", "claude", "claude-ea7b12c1") is False


def test_T3_delivery_is_tracked_per_recipient():
    # session-transition review 3: delivery to one recipient of a broadcast must not leak to the
    # other recipients; the required direct Codex/Claude pair keeps its recipient default.
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)  # c=codex supervisor, cl=claude executor, other=claude observer
        co.send(message_id="m1", task_id="t1", sender="c", recipient="all",
                kind="progress", task_revision=1, text="status for all", host=HOST_C)
        # deliver to cl ONLY (e.g. via the bridge to the one Claude executor)
        co.mark_delivered("t1", "m1", via="cmux-bridge", recipient="cl")
        assert co.message_state("t1", "cl", "m1") == "delivered"
        # the OTHER addressed recipient (observer) never received it
        assert co.message_state("t1", "other", "m1") == "persisted"
        assert co.is_delivered("t1", "m1", "other") is False
        assert co.is_delivered("t1", "m1", "cl") is True
        # a broadcast delivery with NO concrete recipient is refused (no implicit partial delivery)
        co.send(message_id="m2", task_id="t1", sender="c", recipient="all",
                kind="progress", task_revision=1, text="second", host=HOST_C)
        try:
            co.mark_delivered("t1", "m2", via="x")
            assert False, "expected delivery_recipient_required"
        except ProtocolError as e:
            assert e.code == "delivery_recipient_required"
        # delivery to a NON-addressed participant (the sender) is refused
        try:
            co.mark_delivered("t1", "m1", via="x", recipient="c")
            assert False, "expected delivery_not_addressed"
        except ProtocolError as e:
            assert e.code == "delivery_not_addressed"
        # a directly-addressed message keeps its recipient default (the required Codex/Claude pair)
        co.send(message_id="m3", task_id="t1", sender="c", recipient="cl",
                kind="task", task_revision=1, text="direct", host=HOST_C)
        co.mark_delivered("t1", "m3", via="cmux-bridge")  # no explicit recipient -> defaults to cl
        assert co.message_state("t1", "cl", "m3") == "delivered"


def test_DISC1_list_tasks_project_scope_and_bounded():
    # bounded task discovery: find tasks you do not already hold the id for, scoped by project,
    # capped by limit with a truthful truncated flag. Discovery never joins (attach stays separate).
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        co.create_task("ta", project="Science", worktree_realpath="/wt/a", title="Alpha", revision=1)
        co.create_task("tb", project="Other", worktree_realpath="/wt/b", title="Beta")
        allt = co.list_tasks()
        assert allt["returned"] == 2 and allt["truncated"] is False
        assert {x["task_id"] for x in allt["tasks"]} == {"ta", "tb"}
        sci = co.list_tasks("Science")
        assert [x["task_id"] for x in sci["tasks"]] == ["ta"] and sci["project"] == "Science"
        # summaries are lightweight metadata, never message/participant bodies
        assert set(sci["tasks"][0]) == {"task_id", "project", "title", "revision",
                                        "worktree_realpath", "created_at"}
        one = co.list_tasks(limit=1)
        assert one["returned"] == 1 and one["truncated"] is True
        # an unknown project scopes to nothing (not an error, not a firehose)
        assert co.list_tasks("nope")["tasks"] == []


def test_DISC2_find_recipients_scoping():
    # bounded recipient scoping within a KNOWN task: who a sender may address, filtered by role /
    # host, excluding self, attached-only by default. It only reports; it never broadcasts.
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        _seed(co)  # c=codex supervisor, cl=claude executor, other=claude observer
        assert [r["participant_id"] for r in co.find_recipients("t1", role="executor")["recipients"]] == ["cl"]
        claude = {r["participant_id"] for r in co.find_recipients("t1", host_kind="claude")["recipients"]}
        assert claude == {"cl", "other"}
        # exclude self (the classic "everyone but me")
        not_c = {r["participant_id"] for r in co.find_recipients("t1", exclude="c")["recipients"]}
        assert not_c == {"cl", "other"}
        # attached_only (default) hides a detached participant; include-detached surfaces it
        co.detach("t1", "other")
        assert {r["participant_id"] for r in co.find_recipients("t1")["recipients"]} == {"c", "cl"}
        incl = {r["participant_id"] for r in co.find_recipients("t1", attached_only=False)["recipients"]}
        assert incl == {"c", "cl", "other"}


def test_DISC3_list_sessions_liveness_after_rebind():
    # bounded session discovery per host: which native sessions map to which task/participant, with
    # live computed by the SAME authority as resolve_session; a stale post-rebind record is not live.
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        co.create_task("t1", project="p", worktree_realpath="/wt", revision=1)
        co.attach("t1", "exe", role="executor", worktree_realpath="/wt",
                  host={"host": "claude", "session": "CL-1", "native_id": "pid-1"})
        co.attach("t1", "obs", role="observer", worktree_realpath="/wt",
                  host={"host": "claude", "session": "CL-2", "native_id": "pid-2"})
        co.select_session("t1", "exe", host_kind="claude", session_id="CL-1")
        co.select_session("t1", "obs", host_kind="claude", session_id="CL-2")
        live = {(s["session_id"], s["participant"]): s["live"] for s in co.list_sessions("claude")["sessions"]}
        assert live == {("CL-1", "exe"): True, ("CL-2", "obs"): True}
        # controlled rebind of exe to a new native session leaves CL-1 behind as stale
        co.attach("t1", "exe", role="executor", worktree_realpath="/wt",
                  host={"host": "claude", "session": "CL-9", "native_id": "pid-9"}, allow_transition=True)
        after = {(s["session_id"], s["participant"]): s["live"] for s in co.list_sessions("claude")["sessions"]}
        assert after[("CL-1", "exe")] is False and after[("CL-9", "exe")] is True
        # live_only drops the stale record
        ids = {s["session_id"] for s in co.list_sessions("claude", live_only=True)["sessions"]}
        assert ids == {"CL-2", "CL-9"}
        # a host with no sessions is empty, not an error
        assert co.list_sessions("codex")["sessions"] == []


def test_DISC4_discovery_pagination_traverses_all_without_dupes():
    # seq19: a fixed page size must not strand later matches. Page size 1 over 3 records must reach
    # every match exactly once via next_cursor, honoring project + stale-selection filters, and the
    # server must clamp limit to a finite maximum (no unbounded response).
    from mycelium_coord.coord import _MAX_PAGE
    with tempfile.TemporaryDirectory() as t:
        co = _co(Path(t) / "coordination")
        for i in range(3):
            co.create_task(f"task-{i}", project="Science", worktree_realpath=f"/wt/{i}",
                           title=f"Task {i}", revision=1)
        co.create_task("other", project="Other", worktree_realpath="/wt/x")  # filtered out by project

        def traverse(call):
            seen, cursor, pages = [], None, 0
            while True:
                r = call(cursor)
                ids = r.get("tasks") or r.get("recipients") or r.get("sessions")
                seen.extend(x.get("task_id") or x.get("participant_id") or x.get("session_id") for x in ids)
                pages += 1
                cursor = r["next_cursor"]
                if cursor is None:
                    assert r["truncated"] is False
                    return seen
                assert r["truncated"] is True
                assert pages <= 10, "pagination did not terminate"

        # tasks: exactly the 3 Science tasks, in created order, no dupes, no cross-project bleed
        assert traverse(lambda c: co.list_tasks("Science", limit=1, cursor=c)) == ["task-0", "task-1", "task-2"]
        assert len(traverse(lambda c: co.list_tasks("Science", limit=1, cursor=c))) == 3  # repeatable

        # recipients: page size 1 over the 3 participants reaches each exactly once
        _seed_r = co  # reuse this store: attach 3 participants to task-0
        co.attach("task-0", "c", role="supervisor", worktree_realpath="/o", host=HOST_C)
        co.attach("task-0", "cl", role="executor", worktree_realpath="/wt/0", host=HOST_CL)
        co.attach("task-0", "obs", role="observer", worktree_realpath="/o", host=HOST_O)
        recs = traverse(lambda c: co.find_recipients("task-0", limit=1, cursor=c))
        assert sorted(recs) == ["c", "cl", "obs"] and len(recs) == len(set(recs))

        # sessions: page size 1 over 2 live selections, stale record excluded by live_only
        co.select_session("task-0", "cl", host_kind="claude", session_id="claude-ea7b12c1")
        co.select_session("task-0", "obs", host_kind="claude", session_id="obs-1")
        co.attach("task-0", "cl", role="executor", worktree_realpath="/wt/0",
                  host={"host": "claude", "session": "claude-new", "native_id": "pid-n"}, allow_transition=True)
        sess = traverse(lambda c: co.list_sessions("claude", live_only=True, limit=1, cursor=c))
        assert sorted(sess) == ["claude-new", "obs-1"] and "claude-ea7b12c1" not in sess

        # finite server maximum: an absurd limit is clamped, never unbounded
        assert _MAX_PAGE <= 1000
        assert co.list_tasks(limit=10_000_000)["returned"] <= _MAX_PAGE


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\ncoordination core: {len(fns)} focused tests PASS")
