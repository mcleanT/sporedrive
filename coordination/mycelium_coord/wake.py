"""Wake: make an idle recipient look at a coordination message, through ONE honest route per host kind.

A stored message does not reach an idle session until its next turn. wake_recipient() picks the route
from the RECIPIENT participant's stored host kind (there is NO caller-supplied target) and records
every outcome. Outcome vocabulary (shared by all routes):

    accepted | uncertain | unavailable | not_supported | refused:<reason>

Only ``accepted`` counts as delivery. An ``uncertain`` outcome stays pending and is NEVER retried
automatically (a retry is another billed model turn), so a stored accepted/uncertain receipt for
(task, message) is returned as-is.

Routes: claude -> thin adapter over bridge_link.notify_via_bridge; codex -> ``codex exec resume``
(opt-in per task, headless-owned threads only); antigravity and any unknown host -> not_supported
(next-turn inbox only). See docs/triangle/M6_WAKE_DESIGN.md.
"""

from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import time

from . import bridge_link, sendcap
from .store import StoreError
from .execution import (
    ExecutionManager,
    ACTIONABLE_KINDS,
    STATUS_CLOSED,
    STATUS_COMPLETED,
    STATUS_DRAINING,
    STATUS_PAUSED,
)
from .model import (
    HOST_KINDS,
    FEATURE_CODEX_EXEC_WAKE,
    RECIPIENT_ALL,
    ROLES,
    STATE_ACKNOWLEDGED,
    STATE_COMPLETED,
    STATE_COMPLETION_CLAIMED,
    ProtocolError,
    authority_op,
    utcnow,
)

ACCEPTED = "accepted"
UNCERTAIN = "uncertain"
UNAVAILABLE = "unavailable"
NOT_SUPPORTED = "not_supported"
REFUSED = "refused"  # always written as "refused:<reason>"

WAKEABLE_KINDS = ("task", "review_request", "consult_request")
DEFAULT_WAKE_CAP = 20
TASK_CAP_FIELD = "wake_budget"  # optional task.json integer overriding DEFAULT_WAKE_CAP
DEFAULT_TIMEOUT_S = 240.0
MAX_TIMEOUT_S = 900.0
WAKE_HEADLESS_MARKER = ("wake", "headless")

ENV_CODEX_BIN = "MYCELIUM_WAKE_CODEX_BIN"
ENV_CODEX_ARGS = (
    "MYCELIUM_WAKE_CODEX_ARGS"  # JSON list of extra args (isolation flags for probes)
)
ENV_QUEUE_DB = "MYCELIUM_WAKE_CODEX_QUEUE_DB"
DEFAULT_QUEUE_DB = "~/.codex/queue_1.sqlite"

# Positive rules (M7 repair): a Codex thread id is a UUID, nothing else; extra codex args are an ALLOWLIST.
_THREAD_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_ALLOWED_FLAG = "--ignore-user-config"
_ALLOWED_C_KEY_PREFIX = "mcp_servers."

WAKE_PROMPT = (
    "New coordination message {mid} on task {tid} for participant {pid}. Read it with coord_inbox "
    "(task {tid}, participant {pid}), acknowledge it with coord_ack (task {tid}, participant {pid}, "
    "message {mid}), then act within your role."
)
# On a send-capability task the woken turn is a new process: it must attach as the participant (same
# identity, taken from the stored record) before it can ack. The earlier holder has exited, so this
# same-identity attach mints a new capability for the woken process.
WAKE_ATTACH_PREFIX = (
    "This turn is a new process: first attach as {pid} with coord_attach using exactly task_id={tid}, "
    "participant_id={pid}, role={role}, worktree_realpath={wt}, host={host}. "
)


def wake_prompt(task_id: str, message_id: str, participant_id: str, attach: dict | None = None) -> str:
    """The fixed wake prompt. ``attach`` (the recipient's stored record) adds the re-attach step that
    a send-capability task needs."""
    body = WAKE_PROMPT.format(mid=message_id, tid=task_id, pid=participant_id)
    if not attach:
        return body
    host = {k: v for k, v in (attach.get("host") or {}).items() if isinstance(v, str)}
    return WAKE_ATTACH_PREFIX.format(
        tid=task_id,
        pid=participant_id,
        role=attach.get("role"),
        wt=attach.get("worktree_realpath"),
        host=json.dumps(host, sort_keys=True),
    ) + body


def validate_timeout(timeout_s) -> float:
    """None -> default; otherwise a finite real in (0, MAX_TIMEOUT_S]. Anything else raises
    ProtocolError('invalid_timeout') before any side effect."""
    if timeout_s is None:
        return DEFAULT_TIMEOUT_S
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise ProtocolError(
            "invalid_timeout", str(timeout_s), "timeout must be a number"
        )
    t = float(timeout_s)
    if not math.isfinite(t) or t <= 0 or t > MAX_TIMEOUT_S:
        raise ProtocolError(
            "invalid_timeout",
            str(timeout_s),
            f"timeout must be finite and in (0, {MAX_TIMEOUT_S:g}]",
        )
    return t


def _refused(reason: str) -> str:
    return f"{REFUSED}:{reason}"


def _request_id(task_id: str, message_id: str) -> str:
    return (
        "wake-" + hashlib.sha256(f"{task_id}\x00{message_id}".encode()).hexdigest()[:16]
    )


def _receipt_rel(task_id: str, message_id: str) -> str:
    return f"tasks/{task_id}/wakes/{message_id}.json"


def _count_billed(co, task_id: str) -> int:
    n = 0
    for p in co.store.listdir(f"tasks/{task_id}/wakes"):
        rec = co.store.read(f"tasks/{task_id}/wakes/{p.name}")
        if rec and rec.get("outcome") in (ACCEPTED, UNCERTAIN):
            n += 1
    return n


def _wake_cap(task: dict) -> int:
    v = task.get(TASK_CAP_FIELD)
    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
        return v
    return DEFAULT_WAKE_CAP


# --------------------------------------------------------------------------- execution gate
def _execution_gate(
    em: ExecutionManager, task_id: str, msg: dict, *, claim: bool
) -> str | None:
    """Return a refusal reason or None. Unmanaged tasks pass. A managed work dispatch needs its bound
    reservation (same checks bridge_link uses); a managed non-actionable notice only needs a live
    (not paused/draining/expired/completed/closed) execution."""
    if not em.is_managed(task_id):
        return None
    actionable = msg.get("actionable")
    if actionable is None:
        actionable = msg.get("kind") in ACTIONABLE_KINDS
    action_id = msg.get("execution_action_id")
    if actionable or action_id is not None:
        if action_id is None:
            return "managed_dispatch_requires_reservation"
        if claim:
            res = em.claim_dispatch(
                task_id, action_id=action_id, dispatch_identity=msg["message_id"]
            )
        else:
            res = em.dispatch_check(
                task_id, action_id, dispatch_identity=msg["message_id"]
            )
        if not res.get("ok"):
            return f"execution_gate:{res.get('reason')}"
        return None
    rec = em.read_execution(task_id) or {}
    if em._expired(rec):  # noqa: SLF001 - same predicate the dispatch gate uses
        return "execution_gate:execution_expired"
    if rec.get("status") in (
        STATUS_PAUSED,
        STATUS_DRAINING,
        STATUS_COMPLETED,
        STATUS_CLOSED,
    ):
        return f"execution_gate:execution_{rec.get('status')}"
    return None


# --------------------------------------------------------------------------- codex queue guard
def _queue_state(thread: str) -> tuple[str, dict]:
    """('clear'|'queued'|'unknown', detail). DB absent -> clear (an absent DEFAULT path is unknown when
    another queue_*.sqlite sits beside it; an absent env-override path stays clear). Present but unreadable or no table
    with a recognisable thread column -> unknown (fail closed)."""
    override = os.environ.get(ENV_QUEUE_DB)
    path = os.path.expanduser(override or DEFAULT_QUEUE_DB)
    if not os.path.exists(path):
        if not override:
            # The default file is gone but Codex left another queue database next to it (renamed by an
            # upgrade?): the guard cannot say what it did not look at.
            others = sorted(
                f
                for f in glob.glob(os.path.join(os.path.dirname(path), "queue_*.sqlite"))
                if not f.endswith(("-shm", "-wal")) and f != path
            )
            if others:
                return "unknown", {"db": "absent", "other_queue_dbs": len(others)}
        return "clear", {"db": "absent"}
    try:
        uri = "file:" + path.replace("?", "%3f").replace("#", "%23") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
    except Exception as e:  # noqa: BLE001
        return "unknown", {"error": f"{type(e).__name__}"}
    try:
        tables = [
            r[0]
            for r in con.execute("select name from sqlite_master where type='table'")
        ]
        # Positive rule: only the KNOWN layout counts (codex 0.171.0: table queued_items with a
        # thread_id column; drained items are deleted). Other tables that merely carry a thread
        # column (queued_thread_revisions keeps a permanent row per thread) are NOT queues.
        # Any other layout -> unknown -> refused (fail closed).
        cols = (
            [r[1] for r in con.execute('pragma table_info("queued_items")')]
            if "queued_items" in tables
            else []
        )
        if "thread_id" not in cols:
            return "unknown", {"tables": len(tables)}
        queued = con.execute(
            'select count(*) from "queued_items" where "thread_id" = ?', (thread,)
        ).fetchone()[0]
        return ("queued" if queued else "clear"), {"queued": queued}
    except Exception as e:  # noqa: BLE001
        return "unknown", {"error": f"{type(e).__name__}"}
    finally:
        con.close()


# --------------------------------------------------------------------------- routes
# A route returns (outcome, detail). It may raise ProtocolError (recorded as refused:<code>).
def _route_claude(ctx) -> tuple[str, dict]:
    kw = ctx["route_args"]
    missing = [
        k
        for k in ("binding_id", "controller_id", "expected_revision")
        if kw.get(k) is None
    ]
    if missing:
        return _refused("bridge_arguments_required"), {"missing": missing}
    args = dict(
        task_id=ctx["task_id"],
        message_id=ctx["message_id"],
        binding_id=kw["binding_id"],
        controller_id=kw["controller_id"],
        expected_revision=kw["expected_revision"],
        accept_timeout_s=kw.get("accept_timeout_s", 12.0),
        execution_action_id=kw.get("execution_action_id"),
    )
    # common managed pause/expiry/draining/closed gate, immediately before the route's mutation
    reason = _execution_gate(ctx["em"], ctx["task_id"], ctx["msg"], claim=False)
    if reason:
        return _refused(reason), {}
    res = bridge_link.notify_via_bridge(ctx["co"], **args)
    state = res.get("state")
    detail = {"bridge_state": state, "bridge_request_id": res.get("bridge_request_id")}
    if res.get("refused"):
        detail["bridge_refused"] = res.get("refused")
    if state == "delivered" and res.get("delivered"):
        return ACCEPTED, detail
    if state == "bridge_unavailable":
        return UNAVAILABLE, detail
    if state == "refused_by_execution_gate":
        return _refused("execution_gate:" + str(res.get("reason"))), detail
    if state == "inbox_only_non_actionable":
        return _refused("inbox_only_non_actionable"), detail
    if state == "pending" and res.get("refused"):
        return _refused("bridge_" + str(res.get("refused"))), detail
    return UNCERTAIN, detail


def _route_unsupported(ctx) -> tuple[str, dict]:
    return NOT_SUPPORTED, {"note": "next-turn inbox only for this host kind"}


def _extra_codex_args() -> list[str]:
    raw = os.environ.get(ENV_CODEX_ARGS)
    if not raw:
        return []
    try:
        arr = json.loads(raw)
    except ValueError as e:
        raise ProtocolError(
            "invalid_wake_codex_args", ENV_CODEX_ARGS, f"not JSON: {e}"
        ) from None
    if not isinstance(arr, list) or not all(isinstance(a, str) for a in arr):
        raise ProtocolError(
            "invalid_wake_codex_args", ENV_CODEX_ARGS, "must be a JSON list of strings"
        )
    i = 0
    while i < len(arr):
        a = arr[i]
        if a == _ALLOWED_FLAG:
            i += 1
            continue
        if (
            a == "-c"
            and i + 1 < len(arr)
            and "=" in arr[i + 1]
            and arr[i + 1].split("=", 1)[0].startswith(_ALLOWED_C_KEY_PREFIX)
        ):
            i += 2
            continue
        raise ProtocolError(
            "codex_args_not_allowed",
            a,
            "only --ignore-user-config and -c mcp_servers.<...>=<value> are allowed",
        )
    return arr


def _kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _route_codex(ctx) -> tuple[str, dict]:
    co, task, task_id, msg, recip = (
        ctx["co"],
        ctx["task"],
        ctx["task_id"],
        ctx["msg"],
        ctx["recip"],
    )
    if FEATURE_CODEX_EXEC_WAKE not in (task.get("protocol_features") or []):
        return _refused("codex_exec_wake_not_enabled"), {}
    host = recip.get("host") or {}
    if host.get(WAKE_HEADLESS_MARKER[0]) != WAKE_HEADLESS_MARKER[1]:
        return _refused("not_headless_owned"), {}
    thread = str(host.get("session") or "").strip()
    if not thread:
        return _refused("no_thread_id"), {}
    if not _THREAD_ID_RE.fullmatch(thread):
        return _refused("invalid_thread_id"), {}
    # One headless run per Codex thread at a time (review F2): a per-THREAD lock held from before the
    # queue check through the whole run (Popen to communicate), taken with zero wait BEFORE the task
    # lock. Busy -> a retryable, unbilled refusal (the claim record is replaced by it), never a StoreError.
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(
                co.store.lock("codex-thread-" + thread.lower(), timeout_s=0)
            )
        except StoreError as e:
            if getattr(e, "code", "") == "lock_timeout":
                return _refused("thread_wake_in_progress"), {"thread_lock": "busy"}
            raise
        return _codex_locked(ctx, thread)


def _codex_locked(ctx, thread: str) -> tuple[str, dict]:
    co, task_id, msg, recip = ctx["co"], ctx["task_id"], ctx["msg"], ctx["recip"]
    qs, qd = _queue_state(thread)
    ctx["queue"] = {"state": qs, "detail": qd}
    if qs == "queued":
        return _refused("thread_has_queued_items"), {"queue": {"state": qs, "detail": qd}}
    if qs == "unknown":
        return _refused("queue_unknown"), {"queue": {"state": qs, "detail": qd}}
    extra = _extra_codex_args()
    pid = recip["participant_id"]
    prompt = wake_prompt(
        task_id,
        msg["message_id"],
        pid,
        attach=recip if co._cap_enforced(ctx["task"]) else None,
    )
    cmd = [
        os.environ.get(ENV_CODEX_BIN) or "codex",
        "exec",
        "resume",
        "--json",
        *extra,
        thread,
        "-",
    ]
    timeout = validate_timeout(ctx["route_args"].get("timeout_s"))
    cwd = recip.get("worktree_realpath")
    cwd = cwd if cwd and os.path.isdir(cwd) else None

    # immediate pre-mutation re-read of pause/expiry (managed tasks)
    reason = _execution_gate(ctx["em"], task_id, msg, claim=False)
    if reason:
        return _refused(reason), {}

    seq = msg.get("seq", 0)
    cur_before = co.get_cursor(task_id, pid)
    timed_out = False
    exit_code = None
    detail: dict = {"argv_len": len(cmd), "queue": {"state": qs, "detail": qd}}

    # Pre-launch re-read of the recipient, HELD UNDER THE TASK LOCK together with the launch
    # (Codex verify of finding 4): attach/detach (the only participant writers, incl. transitions)
    # take the same lock, so the recipient cannot change between this check and Popen.
    # A task-lock timeout BEFORE launch is `unavailable` (retryable), never `uncertain`: nothing ran.
    proc = None
    try:
        with co._lock(task_id):
            fresh = co.store.read(f"tasks/{task_id}/participants/{pid}.json") or {}
            fh = fresh.get("host") or {}
            if not (
                not fresh.get("detached")
                and fh.get("host") == "codex"
                and str(fh.get("session") or "").strip() == thread
                and fh.get(WAKE_HEADLESS_MARKER[0]) == WAKE_HEADLESS_MARKER[1]
            ):
                return _refused("recipient_changed"), {}
            if co._cap_enforced(ctx["task"]):
                # Send capability, under the same lock as attach (review 1, 7): the sender must still
                # hold its capability, and the recipient's holder must have exited, or the woken
                # process could not attach and ack (a billed turn with no possible evidence).
                try:
                    co._require_cap(task_id, ctx["sender"], "wake", task=ctx["task"])
                except ProtocolError as e:
                    return _refused("sender_" + e.code), {}
                hs = sendcap.holder_state((fresh.get("send_cap") or {}).get("holder"))
                if hs != sendcap.DEAD:
                    return _refused("recipient_held"), {"holder": hs}
            t0 = time.monotonic()
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    start_new_session=True,
                )
            except OSError as e:
                return UNAVAILABLE, {"error": type(e).__name__}
    except StoreError as e:
        if proc is None and getattr(e, "code", "") == "lock_timeout":
            return UNAVAILABLE, {"error": "task_lock_timeout"}
        raise
    try:
        try:
            out, err = proc.communicate(prompt.encode(), timeout=timeout)
            exit_code = proc.returncode
            detail["stdout_bytes"], detail["stderr_bytes"] = (
                len(out or b""),
                len(err or b""),
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            exit_code = proc.returncode
        detail.update(
            exit=exit_code,
            timed_out=timed_out,
            duration_s=round(time.monotonic() - t0, 3),
        )

        state_after = co.message_state(task_id, pid, msg["message_id"])
        cur_after = co.get_cursor(task_id, pid)
    except BaseException:
        # any failure after launch: never leave the headless run alive; the caller records uncertain
        _kill_group(proc)
        raise
    detail["state_after"] = state_after
    seen = state_after in (
        STATE_ACKNOWLEDGED,
        STATE_COMPLETION_CLAIMED,
        STATE_COMPLETED,
    ) or (seq and cur_before < seq <= cur_after)
    # the store, not the exit code, decides; a timeout or nonzero exit with store evidence is still
    # evidence (the ack landed), but a clean exit without it is only uncertain.
    return (ACCEPTED if seen else UNCERTAIN), detail


_ROUTE_BY_HOST = {"claude": _route_claude, "codex": _route_codex}
# one route per model.HOST_KINDS entry; a host without a wake route is not_supported
ROUTES = {h: _ROUTE_BY_HOST.get(h, _route_unsupported) for h in HOST_KINDS}


# --------------------------------------------------------------------------- public entry
@authority_op("wake")
def wake_recipient(
    co,
    task_id: str,
    message_id: str,
    *,
    sender: str,
    binding_id: str | None = None,
    controller_id: str | None = None,
    expected_revision: int | None = None,
    accept_timeout_s: float = 12.0,
    execution_action_id: str | None = None,
    timeout_s: float | None = None,
) -> dict:
    """Wake the single recipient of ``message_id``. Returns the stored receipt (dict with ``outcome``).
    The Claude route needs the bridge arguments; they identify a bridge binding, not a target (the
    adapter verifies the binding against the recipient's stored identity)."""
    validate_timeout(
        timeout_s
    )  # finite (0, 900] or ProtocolError, before any side effect
    msg = co.get_message(task_id, message_id)
    if not msg:
        raise ProtocolError(
            "unknown_message",
            message_id,
            "cannot wake for a message that does not exist",
        )
    co._participant(
        task_id, sender
    )  # sender must be an attached, non-detached participant
    co._require_cap(task_id, sender, "wake")  # send capability (enforced tasks / advisory processes)
    rel = _receipt_rel(task_id, message_id)
    prior = co.store.read(rel)
    if prior and prior.get("outcome") in (ACCEPTED, UNCERTAIN):
        return {**prior, "idempotent_replay": True}

    task = co._require_task(task_id)
    rid = _request_id(task_id, message_id)
    recipient_id = str(msg.get("recipient") or "")
    recip = None
    route_name = None
    state_before = None

    def record(outcome: str, detail: dict | None = None) -> dict:
        rec = {
            "task_id": task_id,
            "message_id": message_id,
            "request_id": rid,
            "sender": sender,
            "recipient": recipient_id,
            "route": route_name,
            "outcome": outcome,
            "state_before": state_before,
            "at": utcnow(),
            **(detail or {}),
        }
        co.store.write(rel, rec)
        co.store.append_receipt(
            f"tasks/{task_id}/audit.jsonl",
            "wake",
            {
                k: rec.get(k)
                for k in (
                    "message_id",
                    "request_id",
                    "route",
                    "outcome",
                    "state_before",
                    "state_after",
                    "exit",
                    "timed_out",
                    "duration_s",
                )
            },
            lock_name=task_id,
        )
        return rec

    # ---- common gates (no side effect before all pass)
    if recipient_id == RECIPIENT_ALL or recipient_id in ROLES:
        return record(_refused("fan_out_recipient"))
    recip = co.store.read(f"tasks/{task_id}/participants/{recipient_id}.json")
    if not recip:
        return record(_refused("recipient_not_attached"))
    if recip.get("detached"):
        return record(_refused("recipient_detached"))
    host_kind = (recip.get("host") or {}).get("host")
    route_name = host_kind if host_kind in ROUTES else "unknown"
    if sender == recipient_id:
        return record(_refused("sender_is_recipient"))
    if recip.get("role") == "observer":
        return record(_refused("observer_not_wakeable"))
    if msg.get("kind") not in WAKEABLE_KINDS:
        return record(_refused("kind_not_wakeable"))
    state_before = co.message_state(task_id, recipient_id, message_id)
    if state_before in (STATE_ACKNOWLEDGED, STATE_COMPLETION_CLAIMED, STATE_COMPLETED):
        return record(_refused("already_acknowledged"))

    route = ROUTES.get(host_kind, _route_unsupported)
    if route is not _route_unsupported:
        em = ExecutionManager(co.store)
        if route is _route_codex:
            reason = _execution_gate(em, task_id, msg, claim=True)
            if reason:
                return record(_refused(reason))
        # cap check + in-flight claim under one lock, so two callers cannot both pass or both run
        with co.store.lock(f"wake-{task_id}"):
            again = co.store.read(rel)
            if again and again.get("outcome") in (ACCEPTED, UNCERTAIN):
                return {**again, "idempotent_replay": True}
            if _count_billed(co, task_id) >= _wake_cap(task):
                return record(_refused("wake_budget_exhausted"))
            # claim: a crash mid-run leaves this uncertain record, never a re-run
            co.store.write(
                rel,
                {
                    "task_id": task_id,
                    "message_id": message_id,
                    "request_id": rid,
                    "route": route_name,
                    "recipient": recipient_id,
                    "outcome": UNCERTAIN,
                    "in_flight": True,
                    "state_before": state_before,
                    "at": utcnow(),
                },
            )
        ctx = {
            "co": co,
            "em": em,
            "task": task,
            "task_id": task_id,
            "message_id": message_id,
            "msg": msg,
            "recip": recip,
            "sender": sender,
            "route_args": {
                "binding_id": binding_id,
                "controller_id": controller_id,
                "expected_revision": expected_revision,
                "accept_timeout_s": accept_timeout_s,
                "execution_action_id": execution_action_id,
                "timeout_s": timeout_s,
            },
        }
        try:
            outcome, detail = route(ctx)
        except ProtocolError as e:
            return record(_refused(e.code), {"message": e.message})
        except Exception as e:  # noqa: BLE001 - the wake may have fired: stay uncertain, never retry
            err = {"error": f"{type(e).__name__}: {e}"}
            if ctx.get("queue"):
                err["queue"] = ctx["queue"]
            return record(UNCERTAIN, err)
        return record(outcome, detail)

    outcome, detail = _route_unsupported(None)
    return record(outcome, detail)
