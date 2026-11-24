"""Mode 2 "link": two named live sessions pinned to one shared task (design TWO-MODES-DESIGN.md section 3).

A link record lives at tasks/<task_id>/links/<link_id>.json and is written only under the task lock, each
change with an audit receipt. A link DESIGNATES the recipient of a send or wake; it does not make the board
private and it grants no authority. The task is shared as a whole (no per-member filter).

Identity pin: the PAIR (host kind, host.session, host.native_id) copied from the participant's stored attach
record, never from the caller. A link is stale when a member is detached or a DIFFERENT native session holds the
participant (any change of that triple; None never equals a changed value). A new send capability on the SAME
session (a headless Codex wake re-attach, a restarted session re-attaching) changes none of the pin and is not
stale. A stale link refuses `link_stale`; it never retargets. The pin is a claimed label copied from the attach
record, not an authenticated identity (member `session_source` is "attach_record", `session_verified` is always
false). Design v1.4; review fixes F1, F12, F14-F17.
"""

from __future__ import annotations

from . import model
from .model import ProtocolError, utcnow
from .store import StoreError, require_id

LINK_SCHEMA = "session-link/1"


def _root(task_id: str) -> str:
    return f"tasks/{task_id}"


def _rel(task_id: str, link_id: str) -> str:
    return f"{_root(task_id)}/links/{link_id}.json"


def pin_of(participant: dict) -> dict:
    h = (participant or {}).get("host") or {}
    return {
        "host": h.get("host"),
        "session": h.get("session"),
        "native_id": h.get("native_id"),
    }


def wake_route_for(task: dict, participant: dict) -> str:
    """What `wake` would do for this participant, recorded truthfully (wake.py: Claude cmux bridge, headless
    Codex `exec resume`, other Codex and Antigravity poll, observers never woken)."""
    from .wake import WAKE_HEADLESS_MARKER

    host = participant.get("host") or {}
    kind = host.get("host")
    if participant.get("role") == "observer":
        return "none"
    if kind == "claude":
        # the cmux bridge matches a delivery by host.session; a Claude member with no session can never be
        # delivered to, so recording "claude-bridge" would be untrue (review F17)
        return "claude-bridge" if host.get("session") else "none"
    if kind == "codex":
        if (
            model.FEATURE_CODEX_EXEC_WAKE in (task.get("protocol_features") or [])
            and host.get(WAKE_HEADLESS_MARKER[0]) == WAKE_HEADLESS_MARKER[1]
        ):
            return "codex-exec-resume"
        return "poll"
    if kind == "antigravity":
        return "poll"
    return "none"


class Links:
    def __init__(self, co):
        self.co = co
        self.store = co.store

    # ------------------------------------------------------------------ helpers
    def _task(self, task_id: str) -> dict:
        require_id(task_id, "task_id")
        task = self.co._require_task(task_id)
        if model.FEATURE_SESSION_LINKS not in (task.get("protocol_features") or []):
            raise ProtocolError(
                "link_feature_required",
                task_id,
                f"the task did not opt in to protocol_features [{model.FEATURE_SESSION_LINKS!r}, "
                f"{model.FEATURE_SEND_CAPABILITY!r}] at create-task",
            )
        return task

    def _read(self, task_id: str, link_id: str) -> dict:
        require_id(link_id, "link_id")
        rec = self.store.read(_rel(task_id, link_id))
        if not rec:
            raise ProtocolError(
                "unknown_link", link_id, f"no link {link_id} on task {task_id}"
            )
        return rec

    def _write(
        self, task_id: str, link: dict, event: str, extra: dict | None = None
    ) -> None:
        self.store.write(_rel(task_id, link["link_id"]), link)
        self.store.append_receipt(
            f"{_root(task_id)}/audit.jsonl",
            event,
            {"link_id": link["link_id"], "state": link["state"], **(extra or {})},
            lock_name=task_id,
        )

    def _stale_reason(self, task_id: str, link: dict) -> str | None:
        """None when every member is still attached on its pinned native session."""
        for m in link["members"]:
            p = self.store.read(
                f"{_root(task_id)}/participants/{m['participant_id']}.json"
            )
            if not p or p.get("detached"):
                return f"member_detached:{m['participant_id']}"
            if pin_of(p) != {
                "host": m["host"],
                "session": m["session"],
                "native_id": m["native_id"],
            }:
                return f"member_session_changed:{m['participant_id']}"
            if m["host"] == "antigravity" and p.get("role") != "observer":
                # an Antigravity member is an observer for the whole life of the link, not only at propose
                return f"member_role_changed:{m['participant_id']}"
        return None

    def _refresh(self, task_id: str, link: dict) -> dict:
        """Persist staleness (sticky) for a proposed/active link. Caller holds the task lock."""
        if link["state"] in ("proposed", "active"):
            why = self._stale_reason(task_id, link)
            if why:
                link["state"] = "stale"
                link["stale_reason"] = why
                link["stale_at"] = utcnow()
                self._write(task_id, link, "link_stale", {"reason": why})
        return link

    def _all(self, task_id: str, errors: list | None = None) -> list[dict]:
        """Every link record. Dot-names (a stray `.tmp-*.json` left by an interrupted write) are never links
        (review F1a). With `errors` given, an unreadable file is collected and skipped instead of raising."""
        out = []
        for path in self.store.listdir(f"{_root(task_id)}/links", "[!.]*.json"):
            try:
                rec = self.store.read(f"{_root(task_id)}/links/{path.name}")
            except StoreError as e:
                if errors is None:
                    raise
                errors.append((path.name, e.code))
                continue
            if rec:
                out.append(rec)
        return out

    def prepare_change(self, task_id: str, participant_id: str, new_record: dict) -> None:
        """Called by attach/detach under the task lock BEFORE the participant record is written (Codex verify of
        review F1): persist staleness for every proposed/active link of this participant that the new record would
        make stale. STRICT for readable links: a failed stale write raises, so the attach/detach is refused with
        the participant record untouched, and a link can never revive through a same-session re-attach after a
        stale write that did not land. Unreadable link files are skipped here (they cannot be used for a send or
        wake either). A same-session re-attach makes no link stale and writes nothing. If the participant write
        then fails, the link stays stale (the safe direction; re-propose)."""
        task = self.co.get_task(task_id)
        if not task or model.FEATURE_SESSION_LINKS not in (
            task.get("protocol_features") or []
        ):
            return
        for link in self._all(task_id, errors=[]):
            if link.get("state") not in ("proposed", "active"):
                continue
            m = next(
                (x for x in link["members"] if x["participant_id"] == participant_id), None
            )
            if m is None:
                continue
            if not new_record or new_record.get("detached"):
                why = f"member_detached:{participant_id}"
            elif pin_of(new_record) != {
                "host": m["host"],
                "session": m["session"],
                "native_id": m["native_id"],
            }:
                why = f"member_session_changed:{participant_id}"
            elif m["host"] == "antigravity" and new_record.get("role") != "observer":
                why = f"member_role_changed:{participant_id}"
            else:
                continue
            link["state"] = "stale"
            link["stale_reason"] = why
            link["stale_at"] = utcnow()
            self._write(task_id, link, "link_stale", {"reason": why})

    def participant_changed(self, task_id: str, participant_id: str) -> None:
        """Called by attach/detach under the task lock: mark this participant's links stale when its pin
        no longer holds. A same-session re-attach changes nothing.

        BEST-EFFORT (review F1c): attach/detach already wrote the participant, so a failure here must not leave
        the audit receipt, the selection binding and the capability revocation undone. Each failure (an
        unreadable link file, a failed write) is recorded as a `link_stale_update_failed` audit entry and the
        scan continues. "Stale is sticky" still holds: `_require_live`/`get` recompute staleness from the live
        participant record on every check and persist it, so a link whose update failed here is refused and
        retried on its next use. The strict pre-write step `prepare_change` closes the window where a stale
        write that never landed could be undone by a same-session re-attach."""
        task = self.co.get_task(task_id)
        if not task or model.FEATURE_SESSION_LINKS not in (
            task.get("protocol_features") or []
        ):
            return
        failures: list[dict] = []
        scan_errors: list = []
        links = self._all(task_id, errors=scan_errors)
        for name, code in scan_errors:
            failures.append({"file": name, "error": code})
        for link in links:
            if any(m["participant_id"] == participant_id for m in link["members"]):
                try:
                    self._refresh(task_id, link)
                except (StoreError, OSError) as e:
                    failures.append(
                        {"link_id": link.get("link_id"), "error": getattr(e, "code", type(e).__name__)}
                    )
        if failures:
            try:
                self.store.append_receipt(
                    f"{_root(task_id)}/audit.jsonl",
                    "link_stale_update_failed",
                    {"participant_id": participant_id, "failures": failures},
                    lock_name=task_id,
                )
            except (StoreError, OSError):
                pass

    @staticmethod
    def _member(p: dict, now: str | None) -> dict:
        pin = pin_of(p)
        return {
            "participant_id": p["participant_id"],
            "role": p.get("role"),
            "host": pin["host"],
            "session": pin["session"],
            "native_id": pin["native_id"],
            "native_session_id": pin["session"] or pin["native_id"],
            # review F14: the pin is a LABEL copied from the participant's attach record (containment, not
            # authentication; sendcap.py). Nothing here verifies the session is real, so the honest field names
            # its source instead of claiming verification.
            "session_source": "attach_record",
            "session_verified": False,
            "wake_route": None,
            "confirmed_at": now,
        }

    def _view(self, task_id: str, link: dict) -> dict:
        """Read-only effective view: a stale member shows as stale without writing."""
        if link["state"] in ("proposed", "active"):
            why = self._stale_reason(task_id, link)
            if why:
                return {**link, "state": "stale", "stale_reason": why}
        return link

    # ------------------------------------------------------------------ lifecycle
    def propose(self, task_id: str, link_id: str, from_pid: str, to_pid: str) -> dict:
        model.require_authority(
            "link-propose"
        )  # advisory processes may confirm and end, not propose
        require_id(link_id, "link_id")
        require_id(from_pid, "from_pid")
        require_id(to_pid, "to_pid")
        for pid in (from_pid, to_pid):
            if pid in model.ROLES or pid == model.RECIPIENT_ALL:
                # a participant named like a role or the all-recipient could never be addressed exactly
                raise ProtocolError(
                    "link_member_id_reserved",
                    pid,
                    f"{pid!r} is a role or the all-recipient value and cannot be a link member id",
                )
        with self.co._lock(task_id):
            task = self._task(task_id)
            if from_pid == to_pid:
                raise ProtocolError(
                    "link_invalid_members",
                    link_id,
                    "a link needs two different participants",
                )
            pa = self.co._participant(task_id, from_pid)
            pb = self.co._participant(task_id, to_pid)
            pa_pin, pb_pin = pin_of(pa), pin_of(pb)
            if pa_pin == pb_pin and pa_pin["host"] and (pa_pin["session"] or pa_pin["native_id"]):
                # two participants on one native session would make the "two live sessions" link one session
                # linked to itself (review F16)
                raise ProtocolError(
                    "link_invalid_members",
                    link_id,
                    "both members are pinned to the same native session; a link needs two different sessions",
                )
            self.co._require_cap(task_id, from_pid, "link_propose", task=task)
            for p in (pa, pb):
                if (p.get("host") or {}).get("host") == "antigravity" and p.get(
                    "role"
                ) != "observer":
                    raise ProtocolError(
                        "link_antigravity_observer_only",
                        p["participant_id"],
                        "an Antigravity link member must be attached with role observer",
                    )
            prior = self.store.read(_rel(task_id, link_id))
            if prior:
                if [m["participant_id"] for m in prior["members"]] == [
                    from_pid,
                    to_pid,
                ] and prior["state"] == "proposed":
                    return {**self._view(task_id, prior), "idempotent": True}
                raise ProtocolError(
                    "link_exists", link_id, f"link {link_id} already exists"
                )
            now = utcnow()
            link = {
                "schema": LINK_SCHEMA,
                "link_id": link_id,
                "task_id": task_id,
                "state": "proposed",
                "created_by": from_pid,
                "created_at": now,
                "members": [
                    self._member(pa, now),
                    self._member(pb, None),
                ],
                "ended_at": None,
                "end_reason": None,
            }
            self._write(task_id, link, "link_propose", {"from": from_pid, "to": to_pid})
            return link

    def confirm(self, task_id: str, link_id: str, participant_id: str) -> dict:
        require_id(participant_id, "participant_id")
        with self.co._lock(task_id):
            task = self._task(task_id)
            link = self._read(task_id, link_id)
            partner = link["members"][1]
            if participant_id != partner["participant_id"]:
                raise ProtocolError(
                    "link_confirm_not_partner",
                    participant_id,
                    "only the proposed partner may confirm",
                )
            self.co._participant(task_id, participant_id)
            self.co._require_advisory_observer(task_id, participant_id, "link_confirm")
            self.co._require_cap(task_id, participant_id, "link_confirm", task=task)
            if link["state"] == "stale":
                raise ProtocolError(
                    "link_stale", link_id, link.get("stale_reason") or "stale"
                )
            if link["state"] != "proposed":
                raise ProtocolError(
                    "link_not_proposed",
                    link_id,
                    f"link is {link['state']}, not proposed",
                )
            link = self._refresh(task_id, link)
            if link["state"] == "stale":
                raise ProtocolError(
                    "link_stale", link_id, link.get("stale_reason") or "stale"
                )
            now = utcnow()
            for m in link["members"]:
                p = self.store.read(
                    f"{_root(task_id)}/participants/{m['participant_id']}.json"
                )
                m["wake_route"] = wake_route_for(task, p)
            partner["confirmed_at"] = now
            link["state"] = "active"
            link["confirmed_at"] = now
            self._write(task_id, link, "link_confirm", {"by": participant_id})
            return link

    def end(
        self, task_id: str, link_id: str, participant_id: str, reason: str | None = None
    ) -> dict:
        require_id(participant_id, "participant_id")
        with self.co._lock(task_id):
            task = self._task(task_id)
            link = self._read(task_id, link_id)
            if participant_id not in [m["participant_id"] for m in link["members"]]:
                raise ProtocolError(
                    "link_not_member", participant_id, "only a link member may end it"
                )
            self.co._participant(task_id, participant_id)
            self.co._require_advisory_observer(task_id, participant_id, "link_end")
            self.co._require_cap(task_id, participant_id, "link_end", task=task)
            if link["state"] == "ended":
                return {**link, "idempotent": True}
            link["state"] = "ended"
            link["ended_at"] = utcnow()
            link["end_reason"] = (reason or "ended")[:500]
            link["ended_by"] = participant_id
            self._write(
                task_id,
                link,
                "link_end",
                {"by": participant_id, "reason": link["end_reason"]},
            )
            return link

    def get(self, task_id: str, link_id: str) -> dict:
        self._task(task_id)
        return self._view(task_id, self._read(task_id, link_id))

    # ------------------------------------------------------------------ the send / wake gate
    def _active(self, task_id: str, link_id: str, sender: str) -> tuple[dict, dict]:
        task = self._task(task_id)
        link = self._read(task_id, link_id)
        ids = [m["participant_id"] for m in link["members"]]
        if sender not in ids:
            raise ProtocolError(
                "link_sender_not_member",
                sender,
                f"{sender} is not a member of link {link_id}",
            )
        return task, link

    def _require_live(self, task_id: str, link: dict) -> None:
        link = self._refresh(task_id, link)
        if link["state"] == "stale":
            raise ProtocolError(
                "link_stale",
                link["link_id"],
                f"link is stale ({link.get('stale_reason')}); never retargeted",
            )
        if link["state"] != "active":
            raise ProtocolError(
                "link_not_active",
                link["link_id"],
                f"link is {link['state']}, not active",
            )

    @staticmethod
    def _partner(link: dict, sender: str) -> dict:
        return next(m for m in link["members"] if m["participant_id"] != sender)

    def check_send(
        self,
        task_id: str,
        link_id: str,
        sender: str,
        recipient: str,
        kind: str | None = None,
        actionable: bool = False,
    ) -> dict:
        """Caller holds the task lock (send). The recipient must be exactly the partner participant. An Antigravity
        member sends only non-actionable advisory kinds (the same gate as an advisory process)."""
        _task, link = self._active(task_id, link_id, sender)
        partner = self._partner(link, sender)
        if (
            recipient != partner["participant_id"]
            or recipient in model.ROLES
            or recipient == model.RECIPIENT_ALL
        ):
            raise ProtocolError(
                "link_requires_exact_recipient",
                recipient,
                f"a send inside link {link_id} must name the partner participant {partner['participant_id']!r}; "
                "a role, 'all' or any other participant is refused",
            )
        self._require_live(task_id, link)
        me = next(m for m in link["members"] if m["participant_id"] == sender)
        if me["host"] == "antigravity" and (
            kind not in model.ADVISORY_ALLOWED_SEND_KINDS or actionable
        ):
            raise ProtocolError(
                "link_antigravity_kind_refused",
                sender,
                "an Antigravity link member may send only "
                + ", ".join(model.ADVISORY_ALLOWED_SEND_KINDS),
            )
        return link

    def check_wake(
        self, task_id: str, link_id: str, sender: str, recipient: str
    ) -> dict:
        with self.co._lock(task_id):
            _task, link = self._active(task_id, link_id, sender)
            partner = self._partner(link, sender)
            if recipient != partner["participant_id"]:
                raise ProtocolError(
                    "link_requires_exact_recipient",
                    recipient,
                    f"a wake inside link {link_id} must target the partner participant {partner['participant_id']!r}",
                )
            self._require_live(task_id, link)
            return link

    def recheck_live(self, task_id: str, link_id: str) -> dict:
        """Full link-state re-check for a launch (review F15). Caller holds the task lock. The link must still
        be active, not ended, and BOTH members' participant records must still match the pins it recorded
        (a member detached or rebound makes it stale and refuses). Raises ProtocolError; never retargets."""
        link = self._read(task_id, link_id)
        self._require_live(task_id, link)
        return self._read(task_id, link_id)

    @staticmethod
    def member_pin(link: dict, participant_id: str) -> dict | None:
        """The (host, session, native_id) pin recorded in the link for one member, or None."""
        for m in link.get("members") or []:
            if m.get("participant_id") == participant_id:
                return {
                    "host": m.get("host"),
                    "session": m.get("session"),
                    "native_id": m.get("native_id"),
                }
        return None
