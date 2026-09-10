"""Coordinator: the shared operations both native hosts consume (attach, send, inbox, ack,
checkpoint, wait, resume, detach). All storage goes through CoordStore; all serialisation through a
per-task advisory lock. This is the single implementation — the CLI, the MCP server and the bridge
link all call it, so Claude and Codex share identical semantics.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time

from . import model
from .model import (
    ProtocolError,
    RECIPIENT_ALL,
    ROLES,
    STATE_ACKNOWLEDGED,
    STATE_COMPLETED,
    STATE_COMPLETION_CLAIMED,
    STATE_DELIVERED,
    STATE_PERSISTED,
    utcnow,
)
from .store import CoordStore, StoreError, require_id, valid_id


def _task_root(task_id: str) -> str:
    return f"tasks/{task_id}"


def _same_path(a, b) -> bool:
    """Compare two filesystem paths by realpath so macOS /var vs /private/var (and other
    firmlink aliases) are treated as equal. None never equals a real path."""
    if not a or not b:
        return False
    try:
        return os.path.realpath(str(a)) == os.path.realpath(str(b))
    except Exception:
        return str(a) == str(b)


def _norm_sid(s) -> str:
    s = str(s or "").strip().lower()
    return s[len("claude-") :] if s.startswith("claude-") else s


def _sid_match(stored, given) -> bool:
    # EXACT after normalization; an abbreviation must not resolve a native session
    # (adapter identity follow-up).
    a, b = _norm_sid(stored), _norm_sid(given)
    return bool(a and b and a == b)


def _session_seg(session_id: str) -> str:
    from .store import valid_id as _vi

    return (
        session_id
        if _vi(session_id)
        else "h." + hashlib.sha256(str(session_id).encode()).hexdigest()[:16]
    )


_MAX_PAGE = (
    500  # finite server-side ceiling: no single discovery call returns more than this
)


def _enc_cursor(prim: str, uid: str) -> str:
    """Opaque, transport-safe continuation token: base64url of ["primary","uid"]. Opaque so callers
    treat it as a token (not a parseable position), and free of control/shell/JSON-hostile bytes so
    it survives argv, JSON and the MCP boundary intact."""
    return base64.urlsafe_b64encode(json.dumps([prim, uid]).encode()).decode()


def _dec_cursor(cursor):
    try:
        prim, uid = json.loads(base64.urlsafe_b64decode(str(cursor).encode()).decode())
        return (str(prim), str(uid))
    except Exception:
        return None  # a malformed/foreign token starts from the beginning rather than erroring


def _page(rows, key, cursor, limit):
    """Deterministic cursor pagination for the bounded discovery reads. ``key(row)`` returns a
    ``(primary, uid)`` tuple whose ``uid`` is UNIQUE, so every row has one distinct ordered position;
    ``cursor`` is the opaque token of the last row a caller already saw and only rows strictly after
    it are returned. This guarantees every match stays reachable by following ``next_cursor``,
    whatever the page size — boundedness never becomes unreachability. ``limit`` is clamped to
    ``[1, _MAX_PAGE]`` so a call always makes progress and never exceeds the server budget. Returns
    ``(page_rows, next_cursor_or_None, more)``."""
    keyed = sorted(((key(r), r) for r in rows), key=lambda kr: kr[0])
    ck = _dec_cursor(cursor) if cursor else None
    if ck is not None:
        keyed = [kr for kr in keyed if kr[0] > ck]
    cap = min(max(1, int(limit)), _MAX_PAGE)
    page = keyed[:cap]
    more = len(keyed) > cap
    nxt = None
    if more and page:
        lp, lu = page[-1][0]
        nxt = _enc_cursor(lp, lu)
    return [r for _k, r in page], nxt, more


class Coordinator:
    def __init__(self, store: CoordStore | None = None):
        self.store = store or CoordStore()

    # ---- lock name: the task_id itself (validated, <=120), one coarse lock per task ----
    def _lock(self, task_id: str):
        return self.store.lock(require_id(task_id, "task_id"))

    # ------------------------------------------------------------------ native session selection
    def _write_selection(
        self,
        host_kind: str,
        session_id: str,
        task_id: str,
        participant_id: str,
        worktree,
    ) -> dict:
        require_id(host_kind, "host")
        rel = f"sessions/{host_kind}/{_session_seg(session_id)}.json"
        rec = {
            "host": host_kind,
            "session_id": session_id,
            "task_id": task_id,
            "participant_id": participant_id,
            "worktree_realpath": worktree,
            "selected_at": utcnow(),
        }
        self.store.write(rel, rec)
        return rec

    def select_session(
        self,
        task_id: str,
        participant_id: str,
        *,
        host_kind: str,
        session_id: str,
        worktree: str | None = None,
    ) -> dict:
        """Bind a task/participant selection to a specific NATIVE session id, in managed
        outside-repo state. Startup/compaction resolve by the same key, so two sessions in one
        worktree never resolve to each other (native-routing review 1). The participant must be
        attached with a matching host kind."""
        with self._lock(task_id):
            p = self._participant(task_id, participant_id)
            if (p.get("host") or {}).get("host") != host_kind:
                raise ProtocolError(
                    "session_host_mismatch",
                    participant_id,
                    "selection host kind does not match the participant's host",
                )
            # select-session may only (re)affirm the native session the participant is CURRENTLY
            # attached as. Binding a DIFFERENT session is an identity change and must go through a
            # controlled transition (attach allow_transition), never this selector — otherwise a
            # foreign/stale session could be silently bound (session-transition review 1). Initial
            # binding (no current session) is still allowed; same-session compaction is unaffected.
            cur = (p.get("host") or {}).get("session")
            if cur and not _sid_match(cur, session_id):
                raise ProtocolError(
                    "session_identity_mismatch",
                    participant_id,
                    "select-session cannot bind a native session other than the "
                    "participant's current attached session; use a controlled "
                    "transition (attach allow_transition) to rebind",
                )
            return self._write_selection(
                host_kind,
                session_id,
                task_id,
                participant_id,
                worktree or p.get("worktree_realpath"),
            )

    def resolve_session(self, host_kind: str, session_id: str) -> dict | None:
        """Resolve a native (host, session_id) to its selected {task, participant}, or None.
        Silent (None) if the participant is detached or its host kind no longer matches."""
        from .store import valid_id as _vi

        if not _vi(host_kind) or not session_id:
            return None
        rec = self.store.read(f"sessions/{host_kind}/{_session_seg(session_id)}.json")
        if not rec:
            for pth in self.store.listdir(f"sessions/{host_kind}"):
                r = self.store.read(f"sessions/{host_kind}/{pth.name}")
                if r and _sid_match(r.get("session_id"), session_id):
                    rec = r
                    break
        if not rec:
            return None
        p = self.store.read(
            f"{_task_root(rec['task_id'])}/participants/{rec['participant_id']}.json"
        )
        if not p or p.get("detached") or (p.get("host") or {}).get("host") != host_kind:
            return None
        # Valid only while the participant is STILL on this native session. A controlled rebind
        # (attach allow_transition) moves it to a new session and leaves the old selection record
        # behind; that stale record must not resolve (session-transition review 1). The
        # participant's CURRENT host.session is the authority.
        if not _sid_match((p.get("host") or {}).get("session"), rec.get("session_id")):
            return None
        return {
            "task": rec["task_id"],
            "participant": rec["participant_id"],
            "host": host_kind,
            "session_id": rec.get("session_id"),
            "worktree_realpath": rec.get("worktree_realpath"),
        }

    def verify_participant_session(
        self, task_id: str, participant_id: str, host_kind: str, session_id: str
    ) -> bool:
        """True iff the participant is attached, not detached, and its recorded native identity
        (host kind + session) EXACTLY matches this input. The SessionStart hook's env fallback uses
        this so an unrelated or inherited-env session cannot silently impersonate a participant
        (session-transition review 2). A missing session_id or unattached participant is False."""
        if not valid_id(host_kind) or not session_id:
            return False
        p = self.store.read(f"{_task_root(task_id)}/participants/{participant_id}.json")
        if not p or p.get("detached"):
            return False
        host = p.get("host") or {}
        if host.get("host") != host_kind:
            return False
        return _sid_match(host.get("session"), session_id)

    # ------------------------------------------------------------------ tasks
    def create_task(
        self,
        task_id: str,
        *,
        project: str,
        worktree_realpath: str,
        authorization_ref=None,
        revision: int = 0,
        title: str = "",
    ) -> dict:
        """Create-or-return a task. Idempotent: a second call returns the existing task unchanged
        (it never resets revision or authorization). authorization_ref is recorded, never a grant."""
        require_id(task_id, "task_id")
        rel = f"{_task_root(task_id)}/task.json"
        with self._lock(task_id):
            existing = self.store.read(rel)
            if existing:
                return existing
            task = {
                "schema_version": model.SCHEMA_VERSION,
                "task_id": task_id,
                "title": title,
                "project": project,
                "worktree_realpath": worktree_realpath,
                "authorization_ref": authorization_ref,
                "revision": int(revision),
                "created_at": utcnow(),
            }
            self.store.write(rel, task)
            self.store.append_receipt(
                f"{_task_root(task_id)}/audit.jsonl",
                "task_created",
                {"task_id": task_id},
                lock_name=task_id,
            )
            return task

    def get_task(self, task_id: str) -> dict | None:
        return self.store.read(f"{_task_root(task_id)}/task.json")

    # ------------------------------------------------------------------ discovery (bounded)
    def list_tasks(
        self, project: str | None = None, *, limit: int = 200, cursor: str | None = None
    ) -> dict:
        """Bounded task DISCOVERY: enumerate known tasks, optionally scoped to one project, as
        lightweight summaries (never message/participant bodies). This is the only way to find a
        task you do not already hold the id for; it never joins, never notifies, and never crosses
        into another store. Joining stays a SEPARATE explicit step: pick a task_id from here and
        attach to it. Ordered by created_at (task_id tiebreak); page with `cursor`/`limit` so every
        task remains reachable within a finite per-call budget (`next_cursor` continues, None ends)."""
        out = []
        for path in self.store.listdir("tasks", "*/task.json"):
            rec = self.store.read(f"tasks/{path.parent.name}/task.json")
            if not rec:
                continue
            if project is not None and rec.get("project") != project:
                continue
            out.append(
                {
                    "task_id": rec.get("task_id"),
                    "project": rec.get("project"),
                    "title": rec.get("title", ""),
                    "revision": rec.get("revision"),
                    "worktree_realpath": rec.get("worktree_realpath"),
                    "created_at": rec.get("created_at"),
                }
            )
        page, nxt, more = _page(
            out,
            lambda r: (r.get("created_at") or "", r.get("task_id") or ""),
            cursor,
            limit,
        )
        return {
            "tasks": page,
            "returned": len(page),
            "truncated": more,
            "next_cursor": nxt,
            "project": project,
        }

    def find_recipients(
        self,
        task_id: str,
        *,
        role: str | None = None,
        host_kind: str | None = None,
        exclude: str | None = None,
        attached_only: bool = True,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict:
        """Bounded RECIPIENT scoping within a KNOWN task: the addressable participants a sender may
        target, filtered by role / host kind, excluding one id (typically self), attached-only by
        default. It does NOT broadcast — it only reports who exists so a caller can address a
        concrete recipient; any `all`/role fan-out stays the caller's own explicit choice on send.
        Ordered by attached_at (participant_id tiebreak); page with `cursor`/`limit` (`next_cursor`
        continues) so a large roster stays fully reachable within a finite per-call budget."""
        recips = []
        for rec in self.list_participants(task_id):
            if attached_only and rec.get("detached"):
                continue
            if role is not None and rec.get("role") != role:
                continue
            if (
                host_kind is not None
                and (rec.get("host") or {}).get("host") != host_kind
            ):
                continue
            if exclude is not None and rec.get("participant_id") == exclude:
                continue
            recips.append(
                {
                    "participant_id": rec.get("participant_id"),
                    "role": rec.get("role"),
                    "host": rec.get("host") or {},
                    "detached": bool(rec.get("detached")),
                    "worktree_realpath": rec.get("worktree_realpath"),
                    "attached_at": rec.get("attached_at"),
                }
            )
        page, nxt, more = _page(
            recips,
            lambda r: (r.get("attached_at") or "", r.get("participant_id") or ""),
            cursor,
            limit,
        )
        return {
            "task": task_id,
            "recipients": page,
            "returned": len(page),
            "truncated": more,
            "next_cursor": nxt,
        }

    def list_sessions(
        self,
        host_kind: str,
        *,
        live_only: bool = False,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict:
        """Bounded SESSION discovery for one host kind: the native-session selection records under
        this host and the task/participant each is bound to. Each entry carries `live` — TRUE only
        while the bound participant is still attached on exactly that native session (the same
        authority resolve_session uses); a stale post-rebind record reports live=False and is dropped
        under live_only. Ordered by selected_at (session_id tiebreak); page with `cursor`/`limit`
        (`next_cursor` continues) so every session stays reachable within a finite per-call budget."""
        if not valid_id(host_kind):
            return {
                "host": host_kind,
                "sessions": [],
                "returned": 0,
                "truncated": False,
                "next_cursor": None,
            }
        out = []
        for pth in self.store.listdir(f"sessions/{host_kind}"):
            rec = self.store.read(f"sessions/{host_kind}/{pth.name}")
            if not rec:
                continue
            live = self.resolve_session(host_kind, rec.get("session_id")) is not None
            if live_only and not live:
                continue
            out.append(
                {
                    "host": host_kind,
                    "session_id": rec.get("session_id"),
                    "task": rec.get("task_id"),
                    "participant": rec.get("participant_id"),
                    "worktree_realpath": rec.get("worktree_realpath"),
                    "selected_at": rec.get("selected_at"),
                    "live": live,
                }
            )
        page, nxt, more = _page(
            out,
            lambda r: (r.get("selected_at") or "", r.get("session_id") or ""),
            cursor,
            limit,
        )
        return {
            "host": host_kind,
            "sessions": page,
            "returned": len(page),
            "truncated": more,
            "next_cursor": nxt,
        }

    def _require_task(self, task_id: str) -> dict:
        t = self.get_task(task_id)
        if not t:
            raise ProtocolError(
                "unknown_task",
                task_id,
                f"no such task {task_id}; attach requires create_task first",
            )
        return t

    # ------------------------------------------------------------------ participants
    def attach(
        self,
        task_id: str,
        participant_id: str,
        *,
        role: str,
        worktree_realpath: str,
        host: dict | None = None,
        allow_transition: bool = False,
    ) -> dict:
        """Explicitly attach a participant. Idempotent per participant_id. Attaching a SUPERVISOR
        grants no repository-write or lifecycle-owner authority (D3/contract §6). A controller whose
        cwd is outside the executor repo is first-class: worktree_realpath is a field, never inferred."""
        require_id(task_id, "task_id")
        require_id(participant_id, "participant_id")
        with self._lock(task_id):
            self._require_task(task_id)
            rel = f"{_task_root(task_id)}/participants/{participant_id}.json"
            p = {
                "schema_version": model.SCHEMA_VERSION,
                "participant_id": participant_id,
                "task_id": task_id,
                "role": role,
                "worktree_realpath": worktree_realpath,
                "host": host or {},
                "attached_at": utcnow(),
                "detached": False,
            }
            model.validate_participant(p)
            task = self._require_task(task_id)
            # An executor works IN the task's canonical worktree; a supervisor/observer may sit
            # outside it (D3). Bind the executor identity to the task worktree (finding 3).
            if role == "executor" and not _same_path(
                worktree_realpath, task.get("worktree_realpath")
            ):
                raise ProtocolError(
                    "executor_worktree_mismatch",
                    participant_id,
                    "executor worktree_realpath must equal the task's canonical worktree_realpath",
                )
            prev = self.store.read(rel)
            if prev and not prev.get("detached"):
                # Idempotent re-attach requires the SAME identity; a changed role/host/worktree is
                # a conflict unless an explicit controlled transition is requested (finding 3).
                same = (
                    prev.get("role") == role
                    and _same_path(prev.get("worktree_realpath"), worktree_realpath)
                    and (prev.get("host") or {}).get("host") == (host or {}).get("host")
                    and (prev.get("host") or {}).get("session")
                    == (host or {}).get("session")
                    and (prev.get("host") or {}).get("native_id")
                    == (host or {}).get("native_id")
                )
                if not same and not allow_transition:
                    raise ProtocolError(
                        "participant_identity_conflict",
                        participant_id,
                        "participant already attached with a different identity/role; pass "
                        "allow_transition for a controlled rebind",
                    )
                p["attached_at"] = prev.get("attached_at", p["attached_at"])
                if not same:
                    p["transitioned_from"] = {
                        "role": prev.get("role"),
                        "host": prev.get("host"),
                        "worktree_realpath": prev.get("worktree_realpath"),
                        "at": utcnow(),
                    }
            self.store.write(rel, p)
            # Bind the selection to this native session so startup/compaction resolve to the
            # right participant even with two sessions in one worktree (native-routing review 1).
            sid = (host or {}).get("session")
            hk = (host or {}).get("host")
            if sid and hk:
                try:
                    self._write_selection(
                        hk, sid, task_id, participant_id, worktree_realpath
                    )
                except (ProtocolError, StoreError):
                    pass
            self.store.append_receipt(
                f"{_task_root(task_id)}/audit.jsonl",
                "attach",
                {"participant_id": participant_id, "role": role},
                lock_name=task_id,
            )
            return p

    def detach(self, task_id: str, participant_id: str) -> dict:
        """Detach WITHOUT terminating the peer. Records detach; leaves messages/acks intact."""
        with self._lock(task_id):
            rel = f"{_task_root(task_id)}/participants/{participant_id}.json"
            p = self.store.read(rel)
            if not p:
                raise ProtocolError(
                    "unknown_participant", participant_id, "not attached"
                )
            p["detached"] = True
            p["detached_at"] = utcnow()
            self.store.write(rel, p)
            self.store.append_receipt(
                f"{_task_root(task_id)}/audit.jsonl",
                "detach",
                {"participant_id": participant_id},
                lock_name=task_id,
            )
            return p

    def list_participants(self, task_id: str) -> list[dict]:
        out = []
        for path in self.store.listdir(f"{_task_root(task_id)}/participants"):
            rec = self.store.read(f"{_task_root(task_id)}/participants/{path.name}")
            if rec:
                out.append(rec)
        return sorted(out, key=lambda r: r.get("attached_at", ""))

    def _participant(self, task_id: str, participant_id: str) -> dict:
        p = self.store.read(f"{_task_root(task_id)}/participants/{participant_id}.json")
        if not p:
            raise ProtocolError(
                "unknown_participant",
                participant_id,
                f"{participant_id} is not attached to {task_id}",
            )
        # A detached participant must not keep acting merely because its JSON persists
        # (core review finding 3).
        if p.get("detached"):
            raise ProtocolError(
                "participant_detached",
                participant_id,
                f"{participant_id} is detached; re-attach to act",
            )
        return p

    # ------------------------------------------------------------------ messages
    def send(
        self,
        *,
        message_id: str,
        task_id: str,
        sender: str,
        recipient: str,
        kind: str,
        task_revision,
        text: str | None = None,
        artifact_ref: str | None = None,
        artifacts: list | None = None,
        reply_to: str | None = None,
        correlation_id: str | None = None,
        host: dict | None = None,
        execution_action_id: str | None = None,
        actionable: bool | None = None,
    ) -> dict:
        """Persist an addressed message. Idempotent by (message_id, content): a repeat with the SAME
        content returns the stored message and does NOT re-append; a repeat with the SAME id but
        DIFFERENT content is rejected (idempotency_conflict). Reading never consumes; redelivery is
        allowed and consumers deduplicate by message_id.

        Execution gate (review R1): on a MANAGED task (an execution record exists) a NEW message whose
        kind DISPATCHES work must carry ``execution_action_id`` naming an open, dispatchable reservation
        bound to (or claimable by) this message. This is fail-closed by the message's intrinsic kind,
        so a paused/closed managed task cannot get a work request PERSISTED merely by omitting the
        field. Non-actionable records (ack/status/checkpoint/review-finding/completion/backlog) and
        unmanaged tasks are unaffected; an idempotent replay of an already-persisted message reconciles
        without re-gating."""
        require_id(task_id, "task_id")
        from .execution import ACTIONABLE_KINDS, ExecutionManager

        # Resolve the message's explicit actionable DISPOSITION (review R1): default by intrinsic kind
        # (ACTIONABLE_KINDS dispatch work), overridable by a trusted caller, and forced True whenever
        # the caller names a reservation. A non-actionable message (progress/review_finding/amendment/
        # status) is a durable notice, never an implicit unmanaged work route. It is a disposition the
        # trusted agent supplies, not a free-text parse. The disposition is persisted on the message
        # (and, only when it names a reservation, folded into the idempotency hash) so a native
        # inbox-reader or the bridge can honour it without re-deriving it.
        default_actionable = kind in ACTIONABLE_KINDS
        effective_actionable = (
            default_actionable or bool(actionable) or (execution_action_id is not None)
        )
        msg = model.build_message(
            message_id=message_id,
            task_id=task_id,
            sender=sender,
            recipient=recipient,
            kind=kind,
            task_revision=task_revision,
            host=host,
            reply_to=reply_to,
            correlation_id=correlation_id,
            text=text,
            artifact_ref=artifact_ref,
            artifacts=artifacts,
            execution_action_id=execution_action_id,
            actionable=effective_actionable,
        )
        em = ExecutionManager(self.store)
        idx_rel = f"{_task_root(task_id)}/ids/{message_id}.json"
        with self._lock(task_id):
            self._require_task(task_id)
            self._participant(task_id, sender)  # sender must be attached
            # Idempotency keyed by the DURABLE body, not a pre-written index. The message file is
            # the single authoritative publication (atomic temp+rename); the index is a rebuildable
            # accelerator. A crash that loses the index is recovered by locating the durable body by
            # id; a crash that loses the BODY consumes NO seq, so a repaired retry re-publishes at a
            # FRESH seq ahead of any consumed cursor and can never hide behind it (cursor-recovery).
            existing = self._find_message_record(task_id, message_id)
            if existing is not None:
                rec, rel = existing
                if rec.get("content_hash") == msg["content_hash"]:
                    self.store.write(
                        idx_rel,
                        {
                            "seq": rec["seq"],
                            "content_hash": rec["content_hash"],
                            "rel": rel,
                        },
                    )
                    return {"idempotent": True, "message": rec, "seq": rec["seq"]}
                raise ProtocolError(
                    "idempotency_conflict",
                    message_id,
                    "message_id reused with different content; a payload change requires a new id",
                )
            # managed work-request gate (review R1): a NEW actionable message on a managed task CLAIMS
            # and BINDS its reservation to THIS message id, atomically, under the already-held per-task
            # lock (re-entrant) — NOT a deferred read-only check. Native Claude reads the inbox
            # directly, so notify_via_bridge is not a universal dispatch point; binding at publication
            # is what makes one reservation fund exactly one message even when notify is never called.
            # An idempotent replay of an already-persisted message returned above, so this never
            # double-claims. A managed NON-actionable notice persists freely (no reservation).
            if em.is_managed(task_id):
                if effective_actionable:
                    if execution_action_id is None:
                        raise ProtocolError(
                            "managed_send_requires_reservation",
                            message_id,
                            "a work request on a managed task must name an execution_action_id "
                            "reservation; a paused/closed task is not made unmanaged by omitting it",
                        )
                    claim = em.claim_dispatch(
                        task_id,
                        action_id=execution_action_id,
                        dispatch_identity=message_id,
                    )
                    if not claim.get("ok"):
                        raise ProtocolError(
                            "refused_by_execution_gate",
                            message_id,
                            f"work request refused: {claim.get('reason')}",
                        )
            seq = self._max_seq(task_id) + 1
            msg["seq"] = seq
            msg_rel = f"{_task_root(task_id)}/messages/{seq:08d}-{message_id}.json"
            # authoritative publication FIRST (atomic rename), then the derived accelerator index.
            self.store.write(msg_rel, msg)
            self.store.write(
                idx_rel,
                {"seq": seq, "content_hash": msg["content_hash"], "rel": msg_rel},
            )
            self.store.append_receipt(
                f"{_task_root(task_id)}/audit.jsonl",
                "send",
                {
                    "message_id": message_id,
                    "seq": seq,
                    "kind": kind,
                    "sender": sender,
                    "recipient": recipient,
                },
                lock_name=task_id,
            )
            return {"idempotent": False, "message": msg, "seq": seq}

    def _all_messages(self, task_id: str) -> list[dict]:
        out = []
        for path in self.store.listdir(f"{_task_root(task_id)}/messages"):
            rec = self.store.read(f"{_task_root(task_id)}/messages/{path.name}")
            if rec:
                out.append(rec)
        return sorted(out, key=lambda r: r.get("seq", 0))

    def get_message(self, task_id: str, message_id: str) -> dict | None:
        found = self._find_message_record(task_id, message_id)
        return found[0] if found else None

    def read_message(self, task_id: str, participant_id: str, message_id: str) -> dict:
        """Fetch one complete addressed message after inspecting a compact view; never acknowledge."""
        p = self._participant(task_id, participant_id)
        require_id(message_id, "message_id")
        message = self.get_message(task_id, message_id)
        if not message or not self._addressed_to(message, p):
            raise ProtocolError("message_not_addressed", message_id,
                                "no message addressed to this participant")
        return message

    def _max_seq(self, task_id: str) -> int:
        """Highest DURABLE message seq (derived from atomic body filenames). This is the seq
        authority: a seq is "taken" only once its body is durably renamed into place."""
        mx = 0
        for p in self.store.listdir(f"{_task_root(task_id)}/messages"):
            try:
                mx = max(mx, int(p.name.split("-", 1)[0]))
            except (ValueError, IndexError):
                continue
        return mx

    def _find_message_record(self, task_id: str, message_id: str):
        """Locate a message's DURABLE record + rel by id, resilient to a lost index. Returns
        (record, rel) or None. The index is a fast path; the authoritative fallback scans durable
        bodies (filenames embed the id) and verifies the message_id field, since a glob can
        over-match a longer id that ends with this one."""
        idx = self.store.read(f"{_task_root(task_id)}/ids/{message_id}.json")
        if idx and idx.get("rel") and self.store.exists(idx["rel"]):
            rec = self.store.read(idx["rel"])
            if rec and rec.get("message_id") == message_id:
                return rec, idx["rel"]
        for p in self.store.listdir(
            f"{_task_root(task_id)}/messages", f"*-{message_id}.json"
        ):
            rel = f"{_task_root(task_id)}/messages/{p.name}"
            rec = self.store.read(rel)
            if rec and rec.get("message_id") == message_id:
                return rec, rel
        return None

    def _addressed_to(self, msg: dict, p: dict) -> bool:
        r = msg.get("recipient")
        if msg.get("sender") == p["participant_id"]:
            return False  # never deliver your own send back to you
        return r == p["participant_id"] or r == p.get("role") or r == RECIPIENT_ALL

    def inbox(
        self,
        task_id: str,
        participant_id: str,
        *,
        after_seq: int = 0,
        limit: int = 100,
        kinds: list[str] | None = None,
        compact: bool = False,
    ) -> dict:
        """Bounded read of messages addressed to this participant with seq > after_seq. Does NOT
        consume and does NOT move the stored cursor (reading is side-effect free). Returns messages
        and next_after_seq to pass back."""
        p = self._participant(task_id, participant_id)
        msgs = [
            m
            for m in self._all_messages(task_id)
            if m.get("seq", 0) > int(after_seq) and self._addressed_to(m, p)
        ]
        if kinds:
            kset = set(kinds)
            msgs = [m for m in msgs if m.get("kind") in kset]
        limit = min(max(0, int(limit)), 20 if compact else 200)
        truncated = len(msgs) > limit
        page = msgs[: max(0, int(limit))]
        next_after = page[-1]["seq"] if page else int(after_seq)
        if compact:
            from .views import message_view

            page = [message_view(m) for m in page]
        return {
            "messages": page,
            "next_after_seq": next_after,
            "truncated": truncated,
            "returned": len(page),
        }

    # ------------------------------------------------------------------ cursor (notification only)
    def get_cursor(self, task_id: str, participant_id: str) -> int:
        rec = self.store.read(f"{_task_root(task_id)}/cursors/{participant_id}.json")
        return int(rec.get("after_seq", 0)) if rec else 0

    def set_cursor(self, task_id: str, participant_id: str, after_seq: int) -> dict:
        """Advance the bounded NOTIFICATION cursor. This is deliberately SEPARATE from acknowledgment
        so a reminder failure never loses a message (contract §Delivery). It is monotonic."""
        with self._lock(task_id):
            rel = f"{_task_root(task_id)}/cursors/{participant_id}.json"
            cur = self.store.read(rel) or {"after_seq": 0}
            new = max(int(cur.get("after_seq", 0)), int(after_seq))
            rec = {
                "participant_id": participant_id,
                "after_seq": new,
                "updated_at": utcnow(),
            }
            self.store.write(rel, rec)
            return rec

    def mark_delivered(
        self,
        task_id: str,
        message_id: str,
        *,
        via: str,
        recipient: str | None = None,
        detail: dict | None = None,
    ) -> dict:
        """Record that a message was EXPOSED to ONE addressed recipient by an out-of-band route
        (e.g. a cmux/bridge notification). This is delivery, NOT acknowledgment and NOT completion —
        it is only recorded when the route actually succeeded. Delivery is tracked PER concrete
        recipient, never globally, so 'delivered' for one recipient of a broadcast/role message
        never leaks to its other recipients (session-transition review 3). The concrete recipient is
        `recipient`, else `detail['recipient']`, else — only when the message addresses a single
        concrete participant — the message's own recipient; a broadcast/role message with no
        concrete recipient is refused (this transport cannot represent partial delivery)."""
        with self._lock(task_id):
            msg = self.get_message(task_id, message_id)
            if not msg:
                raise ProtocolError("unknown_message", message_id, "no such message")
            who = recipient or (detail or {}).get("recipient")
            if not who:
                r = msg.get("recipient")
                if r and r != RECIPIENT_ALL and r not in ROLES and valid_id(r):
                    who = r
                else:
                    raise ProtocolError(
                        "delivery_recipient_required",
                        message_id,
                        "a broadcast/role message needs the concrete recipient that "
                        "was reached; pass recipient=",
                    )
            rp = self.store.read(f"{_task_root(task_id)}/participants/{who}.json")
            if not rp or not self._addressed_to(msg, rp):
                raise ProtocolError(
                    "delivery_not_addressed",
                    message_id,
                    f"{who} is not an addressed, attached recipient of {message_id}",
                )
            rel = f"{_task_root(task_id)}/deliveries/{message_id}.json"
            rec = self.store.read(rel) or {"message_id": message_id, "deliveries": []}
            rec["deliveries"].append(
                {"via": via, "at": utcnow(), "recipient": who, "detail": detail or {}}
            )
            self.store.write(rel, rec)
            return rec

    def is_delivered(
        self, task_id: str, message_id: str, participant_id: str | None = None
    ) -> bool:
        """Whether a message was delivered. With participant_id, TRUE only if THAT recipient was
        reached (per-recipient); without one, TRUE if delivered to anyone."""
        rec = self.store.read(f"{_task_root(task_id)}/deliveries/{message_id}.json")
        if not rec:
            return False
        ds = rec.get("deliveries") or []
        if participant_id is None:
            return bool(ds)
        return any(str(d.get("recipient")) == str(participant_id) for d in ds)

    # ------------------------------------------------------------------ acknowledgments
    def ack(
        self, task_id: str, participant_id: str, message_id: str, *, note: str = ""
    ) -> dict:
        """Explicitly acknowledge that a specific message was RECEIVED. This is not a claim that its
        requested action completed (contract §Intended outcome). Idempotent per (participant, id)."""
        with self._lock(task_id):
            p = self._participant(task_id, participant_id)
            msg = self.get_message(task_id, message_id)
            if not msg:
                raise ProtocolError(
                    "unknown_message",
                    message_id,
                    "cannot ack a message that does not exist",
                )
            # Only an ADDRESSED recipient may acknowledge; an unaddressed observer cannot
            # (core review finding 1).
            if not self._addressed_to(msg, p):
                raise ProtocolError(
                    "ack_not_addressed",
                    message_id,
                    f"{participant_id} was not an addressed recipient of {message_id}",
                )
            rel = f"{_task_root(task_id)}/acks/{participant_id}/{message_id}.json"
            rec = self.store.read(rel) or {
                "participant_id": participant_id,
                "message_id": message_id,
                "acked_at": utcnow(),
                "note": note,
            }
            self.store.write(rel, rec)
            self.store.append_receipt(
                f"{_task_root(task_id)}/audit.jsonl",
                "ack",
                {"participant_id": participant_id, "message_id": message_id},
                lock_name=task_id,
            )
            return rec

    def is_acked(self, task_id: str, participant_id: str, message_id: str) -> bool:
        return self.store.exists(
            f"{_task_root(task_id)}/acks/{participant_id}/{message_id}.json"
        )

    @staticmethod
    def _verify_artifact(entry) -> bool:
        """Verify one completion artifact. Structured evidence only: {"file","sha256"} or
        {"file","contains"} that actually checks against the file on disk. A bare path string is
        an UNVERIFIED reference and existence alone is NOT evidence (mirrors the bridge's
        task_complete evidence contract)."""
        if not isinstance(entry, dict) or not entry.get("file"):
            return False
        try:
            from pathlib import Path as _P

            p = _P(str(entry["file"]))
            if not p.is_file():
                return False
            sha = str(entry.get("sha256") or "").strip().lower()
            if sha:
                return hashlib.sha256(p.read_bytes()).hexdigest() == sha
            if entry.get("contains") is not None:
                needle = str(entry["contains"])
                # an empty/whitespace-only predicate is trivially satisfied by any file -> NOT
                # evidence; reject it so the state stays completion_claimed (adapter review 3).
                if not needle.strip():
                    return False
                return needle in p.read_text(errors="replace")
        except OSError:
            return False
        return False

    def message_state(self, task_id: str, participant_id: str, message_id: str) -> str:
        """Highest reached of the four DISTINCT states for this message w.r.t. this participant.
        Never collapses states; 'completed' requires a completion_receipt reply referencing it."""
        msg = self.get_message(task_id, message_id)
        if not msg:
            raise ProtocolError("unknown_message", message_id, "no such message")
        # A completion_receipt only advances state if it BINDS to this request (reply_to), matches
        # its task_revision, carries artifacts, AND comes from a participant the request was addressed
        # to. Even then a non-empty artifacts list is only a CLAIM: `completed` additionally requires
        # at least one artifact to be VERIFIABLE evidence that actually verifies (cursor-recovery
        # review B). An unverified claim is the explicit intermediate state `completion_claimed`.
        claimed = False
        for m in self._all_messages(task_id):
            if m.get("kind") != "completion_receipt" or m.get("reply_to") != message_id:
                continue
            if m.get("task_revision") != msg.get("task_revision"):
                continue
            arts = m.get("artifacts") or []
            if not arts:
                continue
            completer = self.store.read(
                f"{_task_root(task_id)}/participants/{m.get('sender')}.json"
            )
            if not (completer and self._addressed_to(msg, completer)):
                continue
            claimed = True
            if any(self._verify_artifact(a) for a in arts):
                return STATE_COMPLETED
        if claimed:
            return STATE_COMPLETION_CLAIMED
        if self.is_acked(task_id, participant_id, message_id):
            return STATE_ACKNOWLEDGED
        # exposed/delivered = past this recipient's cursor OR bridge-notified (contract).
        if msg.get("seq", 0) <= self.get_cursor(
            task_id, participant_id
        ) or self.is_delivered(task_id, message_id, participant_id):
            return STATE_DELIVERED
        return STATE_PERSISTED

    # ------------------------------------------------------------------ checkpoints
    def publish_checkpoint(
        self,
        task_id: str,
        *,
        revision: int,
        participant_id: str,
        checkpoint: dict,
        authorization_ref=None,
    ) -> dict:
        """Publish a VERSIONED checkpoint. A given revision is immutable: re-publishing the same
        revision with identical content is idempotent; with different content it is rejected
        (checkpoint_conflict). authorization_ref is carried, never a grant."""
        require_id(task_id, "task_id")
        import hashlib
        import json as _json

        body_hash = hashlib.sha256(
            _json.dumps(checkpoint, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        rel = f"{_task_root(task_id)}/checkpoints/{int(revision)}.json"
        with self._lock(task_id):
            self._require_task(task_id)
            self._participant(task_id, participant_id)
            prev = self.store.read(rel)
            if prev:
                if prev.get("content_hash") == body_hash:
                    # Repair the DERIVED latest pointer even on an idempotent retry: if the
                    # original publish crashed after the body but before latest.json, the body is
                    # durable yet unreachable via latest. The bodies are the authority; latest is
                    # rederived from them (checkpoint-recovery review).
                    self._repair_checkpoint_latest(task_id)
                    return {"idempotent": True, "checkpoint": prev}
                raise ProtocolError(
                    "checkpoint_conflict",
                    str(revision),
                    "a published checkpoint revision is immutable",
                )
            rec = {
                "schema_version": model.SCHEMA_VERSION,
                "task_id": task_id,
                "revision": int(revision),
                "published_by": participant_id,
                "authorization_ref": authorization_ref,
                "content_hash": body_hash,
                "published_at": utcnow(),
                "checkpoint": checkpoint,
            }
            self.store.write(
                rel, rec
            )  # authoritative publication of the checkpoint body
            self._repair_checkpoint_latest(task_id)  # derive latest from durable bodies
            self.store.append_receipt(
                f"{_task_root(task_id)}/audit.jsonl",
                "checkpoint",
                {"revision": int(revision), "by": participant_id},
                lock_name=task_id,
            )
            return {"idempotent": False, "checkpoint": rec}

    def _max_checkpoint_revision(self, task_id: str):
        """Highest DURABLE checkpoint revision, from the body filenames (the authority)."""
        mx = None
        for p in self.store.listdir(f"{_task_root(task_id)}/checkpoints"):
            if p.name == "latest.json":
                continue
            try:
                r = int(p.name[:-5])  # strip .json
            except ValueError:
                continue
            mx = r if mx is None else max(mx, r)
        return mx

    def _repair_checkpoint_latest(self, task_id: str) -> None:
        """Rederive the latest pointer from the durable checkpoint bodies (called under the task
        lock on every publish, so a crashed pointer write is always recovered on retry)."""
        mx = self._max_checkpoint_revision(task_id)
        if mx is None:
            return
        body = self.store.read(f"{_task_root(task_id)}/checkpoints/{mx}.json") or {}
        self.store.write(
            f"{_task_root(task_id)}/checkpoints/latest.json",
            {"revision": mx, "content_hash": body.get("content_hash")},
        )

    def read_checkpoint(self, task_id: str, revision: int | None = None) -> dict | None:
        if revision is None:
            latest = self.store.read(f"{_task_root(task_id)}/checkpoints/latest.json")
            if latest:
                revision = int(latest["revision"])
            else:
                # derived-pointer lost: fall back to the durable bodies (the authority)
                revision = self._max_checkpoint_revision(task_id)
                if revision is None:
                    return None
        return self.store.read(
            f"{_task_root(task_id)}/checkpoints/{int(revision)}.json"
        )

    # ------------------------------------------------------------------ wait (bounded, no wake)
    def wait(
        self,
        task_id: str,
        participant_id: str,
        *,
        after_seq: int = 0,
        timeout_s: float = 30.0,
        kinds: list[str] | None = None,
        poll_s: float = 0.5,
        limit: int = 10,
        compact: bool = False,
    ) -> dict:
        """Bounded wait for NEW addressed messages after a cursor. Truthful: a filesystem poll with a
        finite timeout, NOT a host wake-up. Returns as soon as any qualifying message exists or the
        timeout elapses (timed_out True, empty list). Never blocks unbounded."""
        from .execution import ExecutionManager
        from .views import execution_view, stops_wait

        # A bad participant must not turn into a successful terminal-state observation.
        self._participant(task_id, participant_id)
        manager = ExecutionManager(self.store)
        timeout_s = max(0.0, min(float(timeout_s), 600.0))
        poll_s = max(0.05, min(float(poll_s), 5.0))
        deadline = time.monotonic() + timeout_s
        while True:
            status = execution_view(manager.status(task_id))
            if stops_wait(status):
                return {"messages": [], "next_after_seq": int(after_seq), "returned": 0,
                        "truncated": False, "timed_out": False, "stop_waiting": True,
                        "reason": "task_expired" if status.get("expired") else "task_" + status["status"],
                        "execution": status}
            res = self.inbox(task_id, participant_id, after_seq=after_seq, kinds=kinds,
                             limit=limit, compact=compact)
            if res["messages"]:
                res["timed_out"] = False
                res["execution"] = status
                res["stop_waiting"] = False
                return res
            if time.monotonic() >= deadline:
                return {
                    "messages": [],
                    "next_after_seq": int(after_seq),
                    "truncated": False,
                    "returned": 0,
                    "timed_out": True,
                    "unchanged": True,
                    "stop_waiting": False,
                    "execution": status,
                }
            time.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))

    # ------------------------------------------------------------------ resume after compaction
    def resume(self, task_id: str, participant_id: str, *, limit: int = 50,
               compact: bool = False, after_checkpoint_revision: int | None = None) -> dict:
        """Reattach context after compaction/restart: current checkpoint + bounded UNACKNOWLEDGED
        messages addressed to this participant + the current revision/authorization. Large transcripts
        are NOT re-injected — only pending items and the checkpoint."""
        task = self._require_task(task_id)
        p = self._participant(task_id, participant_id)
        from .execution import ExecutionManager
        from .views import checkpoint_view, execution_view, message_view, stops_wait

        status = execution_view(ExecutionManager(self.store).status(task_id))
        limit = min(max(0, int(limit)), 20 if compact else 200)
        pending = [
            m
            for m in self._all_messages(task_id)
            if self._addressed_to(m, p)
            and not self.is_acked(task_id, participant_id, m["message_id"])
        ]
        count = len(pending)
        pending = pending[:limit]
        checkpoint = self.read_checkpoint(task_id)
        if compact:
            pending = [message_view(m) for m in pending]
            checkpoint = checkpoint_view(checkpoint, after_revision=after_checkpoint_revision,
                                         stopped=stops_wait(status))
            task = {k: task.get(k) for k in ("task_id", "revision", "authorization_ref")}
            p = {k: p.get(k) for k in ("participant_id", "role", "host", "state")}
        return {
            "task": task,
            "participant": p,
            "checkpoint": checkpoint,
            "pending_unacked": pending,
            "pending_count": count,
            "pending_truncated": count > len(pending),
            "cursor_after_seq": self.get_cursor(task_id, participant_id),
            "execution": status,
            "stop_waiting": stops_wait(status),
        }
