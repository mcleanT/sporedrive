"""FastMCP server for mycelium_coord (stdio) — the native-plugin MCP interface both Mycelium hosts
declare. It wraps the ONE shared Coordinator, so Claude and Codex get identical coordination
semantics. Every tool has a title + truthful annotations and follows the ToolError passthrough
pattern; sync core calls run in a thread.

Run: python3 -m mycelium_coord.mcp_server   (or via the bundled launcher coordination/bin/mycelium-coord-mcp)
"""

from __future__ import annotations

import asyncio
import os
from functools import lru_cache

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import schedule
from .coord import Coordinator
from .execution import ExecutionManager
from .jobs import DEFAULT_TAIL_BYTES, JOIN_MAX_S, JobManager
from .model import ProtocolError
from .store import CoordStore, StoreError

_READ = ToolAnnotations(readOnlyHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
_WRITE_NI = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False
)

mcp = FastMCP(
    "mycelium-coord",
    instructions=(
        "Provider-neutral Mycelium session coordination. Attach a supervisor/executor/observer to an "
        "authorized task, send addressed messages, read a bounded inbox after a cursor, acknowledge, "
        "publish/read versioned checkpoints, wait (bounded) for new events, and resume after "
        "compaction. Attaching a supervisor grants no repository-write authority; message states are "
        "distinct (persisted/delivered/acknowledged/completion_claimed/completed) and completion "
        "requires verified artifact evidence. Refusals are ToolErrors with a code prefix."
    ),
)


@lru_cache(maxsize=1)
def _co() -> Coordinator:
    return Coordinator(CoordStore(os.environ.get("MYCELIUM_COORD_DIR") or None))


@lru_cache(maxsize=1)
def _em() -> ExecutionManager:
    return ExecutionManager(CoordStore(os.environ.get("MYCELIUM_COORD_DIR") or None))


@lru_cache(maxsize=1)
def _jm() -> JobManager:
    return JobManager(CoordStore(os.environ.get("MYCELIUM_COORD_DIR") or None))


def _fail(name: str, e: Exception) -> ToolError:
    if isinstance(e, (ProtocolError, StoreError)):
        return ToolError(f"{e.code}: {e.message}")
    return ToolError(f"{name} failed: {e}")


def _wrap(name, fn):
    try:
        return fn()
    except (ProtocolError, StoreError) as e:
        raise _fail(name, e) from e


@mcp.tool(title="Coord Create Task", annotations=_WRITE)
async def coord_create_task(
    task_id: str,
    project: str,
    worktree_realpath: str,
    authorization_ref: str | None = None,
    revision: int = 0,
    title: str = "",
) -> dict:
    """Create-or-return an authorized coordination task. Idempotent; never resets an existing task.

    Args:
        task_id: Stable task id ([A-Za-z0-9._:-], <=120).
        project: Project name.
        worktree_realpath: The executor's canonical worktree realpath.
        authorization_ref: Owner authorization reference (recorded, never a grant).
        revision: Initial task revision.
        title: Human title.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_create_task",
            lambda: _co().create_task(
                task_id,
                project=project,
                worktree_realpath=worktree_realpath,
                authorization_ref=authorization_ref,
                revision=revision,
                title=title,
            ),
        )
    )


@mcp.tool(title="Coord Attach", annotations=_WRITE_NI)
async def coord_attach(
    task_id: str,
    participant_id: str,
    role: str,
    worktree_realpath: str,
    host: dict | None = None,
    allow_transition: bool = False,
) -> dict:
    """Explicitly attach a participant (supervisor/executor/observer) by canonical identity.

    A controller whose cwd is outside the executor repo is first-class (pass its own
    worktree_realpath). Attaching a supervisor grants no repository-write or lifecycle-owner
    authority. Re-attaching with a different identity/role is rejected unless allow_transition.

    Args:
        task_id: Task to attach to (must exist).
        participant_id: Stable participant id.
        role: supervisor | executor | observer.
        worktree_realpath: Participant's canonical worktree (executor must match the task's).
        host: Native identity object: {host: claude|codex|..., session: <id>, native_id: <id>}.
        allow_transition: Permit a controlled rebind to a new identity/role.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_attach",
            lambda: _co().attach(
                task_id,
                participant_id,
                role=role,
                worktree_realpath=worktree_realpath,
                host=host or {},
                allow_transition=allow_transition,
            ),
        )
    )


@mcp.tool(title="Coord Send", annotations=_WRITE)
async def coord_send(
    task_id: str,
    message_id: str,
    sender: str,
    recipient: str,
    kind: str,
    task_revision: int,
    text: str | None = None,
    artifact_ref: str | None = None,
    artifacts: list | None = None,
    reply_to: str | None = None,
    correlation_id: str | None = None,
    host: dict | None = None,
    execution_action_id: str | None = None,
    actionable: bool | None = None,
) -> dict:
    """Persist an addressed message. Idempotent by (message_id, content); a same-id different-content
    send is rejected. Exactly one of text / artifact_ref. A completion_receipt requires reply_to and
    a non-empty artifacts list; completion is only VERIFIED when an artifact ({file, sha256|contains})
    checks out.

    Args:
        task_id: Task id.
        message_id: Idempotency key ([A-Za-z0-9._:-], <=120).
        sender: Attached sender participant id.
        recipient: A participant id, a role, or 'all' (this task only).
        kind: task|amendment|review_finding|progress|blocker|question|checkpoint_request|checkpoint_ready|completion_receipt|acknowledgment.
        task_revision: Task revision this message pertains to.
        text: Bounded inline body (<=8000 chars) — or use artifact_ref.
        artifact_ref: Immutable artifact reference instead of inline text.
        artifacts: Completion evidence list; entries {file, sha256} / {file, contains} verify.
        reply_to: message_id this replies to / completes / acknowledges.
        correlation_id: Correlation id (e.g. a bridge request id).
        host: Sender native identity.
        execution_action_id: On a MANAGED task, the reservation funding an actionable work request
            (review R1). Required when the message is actionable on a managed task; ignored otherwise.
        actionable: Explicit managed disposition (review R1). True marks a work dispatch (requires a
            compatible reservation via execution_action_id); False marks a status/notice that never
            wakes a completed/closed executor. Default (None) derives it from kind — task-like kinds
            are actionable. A trusted-agent disposition, not a free-text parse.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_send",
            lambda: _co().send(
                message_id=message_id,
                task_id=task_id,
                sender=sender,
                recipient=recipient,
                kind=kind,
                task_revision=task_revision,
                text=text,
                artifact_ref=artifact_ref,
                artifacts=artifacts,
                reply_to=reply_to,
                correlation_id=correlation_id,
                host=host or {},
                execution_action_id=execution_action_id,
                actionable=actionable,
            ),
        )
    )


@mcp.tool(title="Coord Inbox", annotations=_READ)
async def coord_inbox(
    task_id: str,
    participant_id: str,
    after_seq: int = 0,
    limit: int = 10,
    kinds: list | None = None,
    compact: bool = True,
) -> dict:
    """Bounded read of messages addressed to this participant with seq > after_seq. Does NOT consume
    and does NOT move the stored cursor. Compact summaries are the default. Use coord_read_message
    for a selected full body before acting; summaries are not complete briefs or acceptance evidence.

    Args:
        task_id: Task id.
        participant_id: The reading participant.
        after_seq: Return messages with seq greater than this.
        limit: Max messages (default 10; compact cap 20).
        kinds: Optional kind filter.
        compact: False explicitly returns full message bodies (cap 200).
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_inbox",
            lambda: _co().inbox(
                task_id, participant_id, after_seq=after_seq, limit=limit, kinds=kinds,
                compact=compact,
            ),
        )
    )


@mcp.tool(title="Coord Read Message", annotations=_READ)
async def coord_read_message(task_id: str, participant_id: str, message_id: str) -> dict:
    """Read ONE complete addressed message selected from a compact inbox/resume. No acknowledgment.
    content_hash hashes the message envelope, not a referenced file. Verify file hashes separately.
    """
    return await asyncio.to_thread(lambda: _wrap(
        "coord_read_message", lambda: _co().read_message(task_id, participant_id, message_id)))


@mcp.tool(title="Coord Wait", annotations=_READ)
async def coord_wait(
    task_id: str,
    participant_id: str,
    after_seq: int = 0,
    timeout_s: float = 30.0,
    kinds: list | None = None,
    limit: int = 10,
    compact: bool = True,
) -> dict:
    """Bounded wait for new addressed messages after a cursor. Truthful: a finite-timeout poll, NOT a
    host wake-up. The wait runs locally without model polling inside it. Returns for a message,
    pause/closure/expiry/exhaustion, or timeout. Stop reissuing when stop_waiting=True. An unchanged
    timeout is not progress and does not require a new status read or narrated model turn. Choose one
    finite wait within the host's call limit and the task deadline; do not add clock/sleep loops.

    Args:
        task_id: Task id.
        participant_id: The waiting participant.
        after_seq: Wait for messages with seq greater than this.
        timeout_s: Finite timeout seconds (max 600).
        kinds: Optional kind filter.
        limit: Max messages returned (default 10).
        compact: Summaries by default; fetch one full message with coord_read_message.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_wait",
            lambda: _co().wait(
                task_id,
                participant_id,
                after_seq=after_seq,
                timeout_s=timeout_s,
                kinds=kinds,
                limit=limit,
                compact=compact,
            ),
        )
    )


@mcp.tool(title="Coord Ack", annotations=_WRITE)
async def coord_ack(
    task_id: str, participant_id: str, message_id: str, note: str = ""
) -> dict:
    """Acknowledge RECEIPT of a specific message (not a claim its action completed). Only an
    addressed recipient may ack. Idempotent.

    Args:
        task_id: Task id.
        participant_id: The acknowledging (addressed) participant.
        message_id: Message being acknowledged.
        note: Optional note.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_ack",
            lambda: _co().ack(task_id, participant_id, message_id, note=note),
        )
    )


@mcp.tool(title="Coord Message State", annotations=_READ)
async def coord_message_state(
    task_id: str, participant_id: str, message_id: str
) -> dict:
    """Return the highest distinct state of a message for this participant: persisted | delivered |
    acknowledged | completion_claimed | completed.

    Args:
        task_id: Task id.
        participant_id: Perspective participant.
        message_id: Message id.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_message_state",
            lambda: {"state": _co().message_state(task_id, participant_id, message_id)},
        )
    )


@mcp.tool(title="Coord Set Cursor", annotations=_WRITE)
async def coord_set_cursor(task_id: str, participant_id: str, after_seq: int) -> dict:
    """Advance a participant's bounded NOTIFICATION cursor (monotonic). Separate from acknowledgment,
    so a missed reminder never loses a message.

    Args:
        task_id: Task id.
        participant_id: Participant.
        after_seq: New cursor (max with existing).
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_set_cursor",
            lambda: _co().set_cursor(task_id, participant_id, after_seq),
        )
    )


@mcp.tool(title="Coord Checkpoint Publish", annotations=_WRITE)
async def coord_checkpoint_publish(
    task_id: str,
    revision: int,
    participant_id: str,
    checkpoint: dict,
    authorization_ref: str | None = None,
) -> dict:
    """Publish an immutable versioned checkpoint. Same revision + same content is idempotent; a
    different content at the same revision is rejected.

    Args:
        task_id: Task id.
        revision: Checkpoint revision.
        participant_id: Publishing participant.
        checkpoint: Checkpoint object.
        authorization_ref: Authorization reference (recorded, never a grant).
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_checkpoint_publish",
            lambda: _co().publish_checkpoint(
                task_id,
                revision=revision,
                participant_id=participant_id,
                checkpoint=checkpoint,
                authorization_ref=authorization_ref,
            ),
        )
    )


@mcp.tool(title="Coord Checkpoint Read", annotations=_READ)
async def coord_checkpoint_read(
    task_id: str, revision: int | None = None
) -> dict | None:
    """Read a checkpoint (latest if revision omitted).

    Args:
        task_id: Task id.
        revision: Specific revision, or null for latest.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_checkpoint_read",
            lambda: _co().read_checkpoint(task_id, revision=revision),
        )
    )


@mcp.tool(title="Coord Resume", annotations=_READ)
async def coord_resume(task_id: str, participant_id: str, limit: int = 10,
                       compact: bool = True, after_checkpoint_revision: int | None = None) -> dict:
    """ONE batched state read: execution/stop status, checkpoint identity, participant and pending
    message summaries. Prefer this to separate status/checkpoint/inbox reads. Compact by default;
    use coord_read_message or coord_checkpoint_read for selected full evidence. Current execution
    state overrides stale checkpoint instructions. Never resume work from a summary alone.

    Args:
        task_id: Task id.
        participant_id: Resuming participant.
        limit: Max pending messages returned.
        compact: False opts into full checkpoint and message bodies.
        after_checkpoint_revision: Previously seen revision; omit unchanged checkpoint content.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_resume", lambda: _co().resume(task_id, participant_id, limit=limit,
                compact=compact, after_checkpoint_revision=after_checkpoint_revision)
        )
    )


@mcp.tool(title="Coord Participants", annotations=_READ)
async def coord_participants(task_id: str) -> list:
    """List attached participants for a task.

    Args:
        task_id: Task id.
    """
    return await asyncio.to_thread(
        lambda: _wrap("coord_participants", lambda: _co().list_participants(task_id))
    )


@mcp.tool(title="Coord List Tasks", annotations=_READ)
async def coord_list_tasks(
    project: str | None = None, limit: int = 200, cursor: str | None = None
) -> dict:
    """Bounded task discovery: known tasks (optionally scoped to one project) as lightweight
    summaries. The only way to find a task whose id you do not already hold; it never joins or
    notifies. Pick a task_id and attach separately to join.

    Args:
        project: Restrict to this project when set.
        limit: Max tasks per page (clamped to a finite server maximum).
        cursor: next_cursor from a prior page; omit for the first page.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_list_tasks",
            lambda: _co().list_tasks(project, limit=limit, cursor=cursor),
        )
    )


@mcp.tool(title="Coord Find Recipients", annotations=_READ)
async def coord_find_recipients(
    task_id: str,
    role: str | None = None,
    host_kind: str | None = None,
    exclude: str | None = None,
    attached_only: bool = True,
    limit: int = 200,
    cursor: str | None = None,
) -> dict:
    """Bounded recipient scoping within a KNOWN task: addressable participants filtered by role /
    host kind, excluding one id (typically self), attached-only by default. Reports who exists so a
    caller can address a concrete recipient; it does not broadcast.

    Args:
        task_id: Task id.
        role: Keep only this role (supervisor/executor/observer) when set.
        host_kind: Keep only this host kind (claude/codex) when set.
        exclude: Drop this participant id (e.g. the caller itself).
        attached_only: Drop detached participants (default true).
        limit: Max recipients per page (clamped to a finite server maximum).
        cursor: next_cursor from a prior page; omit for the first page.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_find_recipients",
            lambda: _co().find_recipients(
                task_id,
                role=role,
                host_kind=host_kind,
                exclude=exclude,
                attached_only=attached_only,
                limit=limit,
                cursor=cursor,
            ),
        )
    )


@mcp.tool(title="Coord List Sessions", annotations=_READ)
async def coord_list_sessions(
    host_kind: str, live_only: bool = False, limit: int = 200, cursor: str | None = None
) -> dict:
    """Bounded session discovery for one host kind: native-session selection records and the
    task/participant each is bound to. Each carries `live` (still attached on exactly that session);
    stale post-rebind records are dropped under live_only.

    Args:
        host_kind: Host kind to enumerate (claude/codex).
        live_only: Drop stale (non-live) selection records.
        limit: Max sessions per page (clamped to a finite server maximum).
        cursor: next_cursor from a prior page; omit for the first page.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_list_sessions",
            lambda: _co().list_sessions(
                host_kind, live_only=live_only, limit=limit, cursor=cursor
            ),
        )
    )


@mcp.tool(title="Coord Detach", annotations=_WRITE)
async def coord_detach(task_id: str, participant_id: str) -> dict:
    """Detach a participant WITHOUT terminating the peer. Leaves messages/acks intact.

    Args:
        task_id: Task id.
        participant_id: Participant to detach.
    """
    return await asyncio.to_thread(
        lambda: _wrap("coord_detach", lambda: _co().detach(task_id, participant_id))
    )


@mcp.tool(title="Coord Notify Via Bridge", annotations=_WRITE)
async def coord_notify_via_bridge(
    task_id: str,
    message_id: str,
    binding_id: str,
    controller_id: str,
    expected_revision: int,
    accept_timeout_s: float = 12.0,
    execution_action_id: str | None = None,
) -> dict:
    """Deliver a short cmux notification for a coordination message through the EXISTING bridge,
    correlating message_id with the bridge request_id. Only a bridge accepted/completed outcome is
    recorded as delivered; a busy refusal or uncertain outcome stays pending (never a fabricated
    delivery). Returns bridge_unavailable if the bridge is not present on this host.

    Args:
        task_id: Task id.
        message_id: Coordination message to notify about.
        binding_id: An existing bridge writer binding for the executor session.
        controller_id: Stable controller id.
        expected_revision: The bridge binding revision last observed.
        accept_timeout_s: Seconds to wait for bridge transcript correlation (max 60).
        execution_action_id: When set, gate this managed dispatch on the bound execution: recheck
            status/expiry and that the reserved action is still open immediately before sending;
            a paused/expired/closed execution or stale reservation is refused without dispatching.
    """
    from .bridge_link import notify_via_bridge

    return await asyncio.to_thread(
        lambda: _wrap(
            "coord_notify_via_bridge",
            lambda: notify_via_bridge(
                _co(),
                task_id=task_id,
                message_id=message_id,
                binding_id=binding_id,
                controller_id=controller_id,
                expected_revision=expected_revision,
                accept_timeout_s=accept_timeout_s,
                execution_action_id=execution_action_id,
            ),
        )
    )


# ---- execution record (SporeDrive stopping/permission layer; identical core to the CLI) ----
@mcp.tool(title="Execution Open", annotations=_WRITE)
async def execution_open(
    task_id: str,
    execution_id: str,
    scope_ref: str,
    authorization_ref: str,
    acceptance_manifest: dict | None = None,
    limits_overrides: dict | None = None,
    expires_at: str | None = None,
    phase: str = "implementation",
    previous_execution_id: str | None = None,
    attended: bool = True,
    automation_ref: str | None = None,
) -> dict:
    """Open (or idempotently return) the ONE managed execution record for a task. Limits come from the
    maintained policy config; an override may only LOWER a supervisor-abuse limit (raising needs
    execution_change_limits). The acceptance_manifest freezes ALL required criteria for auto-closure.

    Args:
        task_id: Task id ([A-Za-z0-9._:-], <=120).
        execution_id: Stable identity for this authorized work; a same-work continuation reuses it.
        scope_ref: Reference to the immutable accepted criteria/exclusions.
        authorization_ref: The user instruction reference (recorded, never a grant).
        acceptance_manifest: {criterion_id: {description, evidence_requirements, kind}} frozen set.
        limits_overrides: May lower work/review dispatches; may set task-specific call allowances/expiry.
        expires_at: UTC ISO8601 expiry for unattended work, else null.
        phase: implementation|technical_debug|acceptance|execution|closure.
        previous_execution_id: Prior execution this one succeeds, if any.
        attended: False marks unattended/automation-driven; then a finite expires_at AND automation_ref
            are required so the run fails closed and its automation can be paused on shutdown (R5).
        automation_ref: The automation to pause on expiry/completion for an unattended execution.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_open",
            lambda: _em().open_execution(
                task_id,
                execution_id=execution_id,
                scope_ref=scope_ref,
                authorization_ref=authorization_ref,
                acceptance_manifest=acceptance_manifest,
                limits_overrides=limits_overrides,
                expires_at=expires_at,
                phase=phase,
                previous_execution_id=previous_execution_id,
                attended=attended,
                automation_ref=automation_ref,
            ),
        )
    )


@mcp.tool(title="Execution Read", annotations=_READ)
async def execution_read(task_id: str) -> dict | None:
    """Read the full execution record (or null if none). Always allowed, even when paused/closed.

    Args:
        task_id: Task id.
    """
    return await asyncio.to_thread(
        lambda: _wrap("execution_read", lambda: _em().read_execution(task_id))
    )


@mcp.tool(title="Execution Status", annotations=_READ)
async def execution_status(task_id: str) -> dict | None:
    """Compact execution status: status/phase, usage vs limits, open reservations, coverage, blockers.
    Always allowed.

    Args:
        task_id: Task id.
    """
    return await asyncio.to_thread(
        lambda: _wrap("execution_status", lambda: _em().status(task_id))
    )


@mcp.tool(title="Execution Reserve", annotations=_WRITE)
async def execution_reserve(
    task_id: str,
    action_id: str,
    kind: str,
    purpose: str | None = None,
    criterion_ref: str | None = None,
    repair_blocker_id: str | None = None,
    dispatch_binding: str | None = None,
    expected_state_version: int | None = None,
) -> dict:
    """Atomically reserve a work-producing action against the shared work-dispatch allowance BEFORE
    dispatch. Idempotent by action_id (no double charge on retry). A review_launch consumes both the
    review and work allowances. A closed/completed execution refuses new work except a bounded repair
    linked to an open, evidenced blocker.

    Args:
        task_id: Task id.
        action_id: Stable id for this action (idempotency + reconciliation key).
        kind: work_dispatch | review_launch | repair.
        purpose: Short human purpose.
        criterion_ref: Acceptance criterion this action serves, if any.
        repair_blocker_id: For kind=repair against a closed execution: the open blocker it repairs.
        dispatch_binding: Optionally pre-bind this reservation to one concrete dispatch identity so it
            can fund exactly that dispatch (review R1); else the first managed dispatch claims it.
        expected_state_version: Optimistic-concurrency guard; reject if the record moved.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_reserve",
            lambda: _em().reserve(
                task_id,
                action_id=action_id,
                kind=kind,
                purpose=purpose,
                criterion_ref=criterion_ref,
                repair_blocker_id=repair_blocker_id,
                dispatch_binding=dispatch_binding,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Settle", annotations=_WRITE)
async def execution_settle(
    task_id: str,
    action_id: str,
    outcome: str,
    evidence_ref: str | None = None,
    criterion_ref: str | None = None,
    expected_state_version: int | None = None,
) -> dict:
    """Reconcile a reserved action. NEVER refunds the charge: an uncertain outcome stays charged until
    a later settle updates it. Allowed while paused/exhausted (a bounded closure op).

    Args:
        task_id: Task id.
        action_id: The reserved action id.
        outcome: success | failure | unknown | ... (free text; unknown stays charged/pending).
        evidence_ref: Reference to the outcome evidence.
        criterion_ref: Acceptance criterion this settles against, if any.
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_settle",
            lambda: _em().settle(
                task_id,
                action_id=action_id,
                outcome=outcome,
                evidence_ref=evidence_ref,
                criterion_ref=criterion_ref,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Record Evidence", annotations=_WRITE)
async def execution_record_evidence(
    task_id: str,
    criterion_id: str,
    evidence_ref: str,
    attestation: str | None = None,
    evidence_sha256: str | None = None,
    accepted_by: str | None = None,
    expected_state_version: int | None = None,
) -> dict:
    """Record accepted evidence for a FROZEN acceptance criterion. Missing/unknown evidence is never a
    pass. On each settlement coverage is recomputed; once every criterion is accepted the record
    auto-closes in the same write. A bounded closure op (allowed while paused).

    Args:
        task_id: Task id.
        criterion_id: A criterion id from the frozen acceptance manifest.
        evidence_ref: Reference to the decisive artifact/attestation (required).
        attestation: Executor/reviewer attestation text (not a machine-proof claim).
        evidence_sha256: Optional content hash of the evidence artifact.
        accepted_by: Participant id attesting acceptance.
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_record_evidence",
            lambda: _em().record_evidence(
                task_id,
                criterion_id=criterion_id,
                evidence_ref=evidence_ref,
                attestation=attestation,
                evidence_sha256=evidence_sha256,
                accepted_by=accepted_by,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Open Blocker", annotations=_WRITE)
async def execution_open_blocker(
    task_id: str,
    blocker_id: str,
    criterion_id: str,
    evidence_ref: str,
    description: str | None = None,
    expected_state_version: int | None = None,
) -> dict:
    """Open an EVIDENCED blocker against an existing criterion. It invalidates ONLY that criterion; it
    cannot add scope, reset counters or mint allowance. Against a closed execution it reopens just
    enough for a bounded repair. Idempotent by blocker_id.

    Args:
        task_id: Task id.
        blocker_id: Stable blocker id.
        criterion_id: The affected acceptance criterion.
        evidence_ref: Reproducer/result/contract substantiating the blocker (required).
        description: Short description.
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_open_blocker",
            lambda: _em().open_blocker(
                task_id,
                blocker_id=blocker_id,
                criterion_id=criterion_id,
                evidence_ref=evidence_ref,
                description=description,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Resolve Blocker", annotations=_WRITE)
async def execution_resolve_blocker(
    task_id: str,
    blocker_id: str,
    resolution_ref: str | None = None,
    expected_state_version: int | None = None,
) -> dict:
    """Mark a blocker resolved (its criterion is re-accepted separately via record_evidence).

    Args:
        task_id: Task id.
        blocker_id: The blocker to resolve.
        resolution_ref: Reference to the fix/repair evidence.
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_resolve_blocker",
            lambda: _em().resolve_blocker(
                task_id,
                blocker_id=blocker_id,
                resolution_ref=resolution_ref,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Backlog", annotations=_WRITE_NI)
async def execution_backlog(
    task_id: str,
    item: str,
    source: str | None = None,
    expected_state_version: int | None = None,
) -> dict:
    """Record an optional/late request as NON-ACTIONABLE backlog. Recording is not executing: backlog
    text can never generate a work reservation or wake an idle executor.

    Args:
        task_id: Task id.
        item: The backlog item text (bounded).
        source: Who raised it.
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_backlog",
            lambda: _em().add_backlog(
                task_id,
                item=item,
                source=source,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Reserve Call", annotations=_WRITE)
async def execution_reserve_call(
    task_id: str,
    action_id: str,
    phase: str,
    freeze_identity: str,
    purpose: str | None = None,
    expected_state_version: int | None = None,
) -> dict:
    """Reserve an adapter provider call against its task-specific allowance, tagged with a freeze
    identity the ADAPTER computes from the actual implementation/config. In the acceptance phase a
    changed freeze identity mid-batch blocks remaining calls and invalidates the attempt (no blended
    passing cycle). Idempotent by action_id.

    Args:
        task_id: Task id.
        action_id: Stable call id.
        phase: technical_debug (charges technical_calls) | acceptance (charges acceptance_calls).
        freeze_identity: Identity of the frozen implementation/config issuing the call (required).
        purpose: Short purpose.
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_reserve_call",
            lambda: _em().reserve_call(
                task_id,
                action_id=action_id,
                phase=phase,
                freeze_identity=freeze_identity,
                purpose=purpose,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Settle Call", annotations=_WRITE)
async def execution_settle_call(
    task_id: str,
    action_id: str,
    freeze_identity: str,
    outcome: str,
    response_ref: str | None = None,
    expected_state_version: int | None = None,
) -> dict:
    """Bind a provider response to its reservation. A freeze-identity mismatch invalidates the
    acceptance attempt and keeps the call charged. Unknown outcomes remain charged.

    Args:
        task_id: Task id.
        action_id: The reserved call id.
        freeze_identity: Identity of the implementation that produced the response.
        outcome: success | failure | unknown | ...
        response_ref: Reference to the retained response body.
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_settle_call",
            lambda: _em().settle_call(
                task_id,
                action_id=action_id,
                freeze_identity=freeze_identity,
                outcome=outcome,
                response_ref=response_ref,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Pause", annotations=_WRITE)
async def execution_pause(
    task_id: str,
    authorization_ref: str,
    reason: str | None = None,
    expected_state_version: int | None = None,
) -> dict:
    """User-authorized pause. Blocks new work immediately; sets draining if identified owned work is
    in flight, else paused. Reconciliation/read/record remain possible.

    Args:
        task_id: Task id.
        authorization_ref: The user instruction reference (required).
        reason: Short reason.
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_pause",
            lambda: _em().pause(
                task_id,
                authorization_ref=authorization_ref,
                reason=reason,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Unpause", annotations=_WRITE)
async def execution_unpause(
    task_id: str, authorization_ref: str, expected_state_version: int | None = None
) -> dict:
    """User-authorized unpause back to active.

    Args:
        task_id: Task id.
        authorization_ref: The user instruction reference (required).
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_unpause",
            lambda: _em().unpause(
                task_id,
                authorization_ref=authorization_ref,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Change Limits", annotations=_WRITE_NI)
async def execution_change_limits(
    task_id: str,
    authorization_ref: str,
    changes: dict,
    expected_state_version: int | None = None,
) -> dict:
    """The ONLY path that may raise/extend a limit. Append-only and authorization-linked; PAST USAGE
    is unchanged. An authorized raise can lift an exhausted state.

    Args:
        task_id: Task id.
        authorization_ref: The user instruction reference authorizing the change (required).
        changes: {limit_field: new_value} (work_dispatches, review_launches, review_deadline_seconds,
            technical_calls, acceptance_calls, expires_at).
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_change_limits",
            lambda: _em().change_limits(
                task_id,
                authorization_ref=authorization_ref,
                changes=changes,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Set Phase", annotations=_WRITE)
async def execution_set_phase(
    task_id: str, phase: str, expected_state_version: int | None = None
) -> dict:
    """Advance the integration-readiness phase (implementation -> technical_debug -> acceptance ->
    execution -> closure). Refused once completed (terminal).

    Args:
        task_id: Task id.
        phase: implementation|technical_debug|acceptance|execution|closure.
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_set_phase",
            lambda: _em().set_phase(
                task_id, phase=phase, expected_state_version=expected_state_version
            ),
        )
    )


@mcp.tool(title="Execution Claim Dispatch", annotations=_WRITE)
async def execution_claim_dispatch(
    task_id: str,
    action_id: str,
    dispatch_identity: str,
    expected_state_version: int | None = None,
) -> dict:
    """Bind an OPEN work reservation to one concrete dispatch identity and report whether the dispatch
    may proceed (review R1). Idempotent for the same identity (safe reconciliation of an uncertain
    send); refuses a second, different dispatch that tries to spend the same reservation, and a
    paused/expired/completed execution. Returns {ok, reason, ...} rather than raising for a refusal.

    Args:
        task_id: Task id.
        action_id: The open reservation to claim.
        dispatch_identity: The concrete dispatch this reservation funds (e.g. a message id).
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_claim_dispatch",
            lambda: _em().claim_dispatch(
                task_id,
                action_id=action_id,
                dispatch_identity=dispatch_identity,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Claim Review", annotations=_WRITE)
async def execution_claim_review(
    task_id: str,
    execution_id: str,
    action_id: str,
    launch_identity: str,
    caller_deadline_seconds: float | None = None,
    expected_state_version: int | None = None,
) -> dict:
    """Claim an existing OPEN review-launch reservation for one owned reviewer process (review R3):
    verify the execution identity, that the reservation is a review launch, open and dispatchable
    (fresh pause/expiry recheck here), bind a stable launch identity (idempotent same / refused
    different), and return the effective hard deadline (the policy value, which the caller may only
    SHORTEN). Returns {ok, reason, effective_deadline_seconds, ...} rather than raising for a refusal.

    Args:
        task_id: Task id.
        execution_id: The execution the reservation must belong to.
        action_id: The review-launch reservation to claim.
        launch_identity: A stable identity for this owned launch (one reservation, one launch).
        caller_deadline_seconds: Optional caller deadline; may only shorten the policy deadline.
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_claim_review",
            lambda: _em().claim_review(
                task_id,
                execution_id=execution_id,
                action_id=action_id,
                launch_identity=launch_identity,
                caller_deadline_seconds=caller_deadline_seconds,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Settle Review", annotations=_WRITE)
async def execution_settle_review(
    task_id: str,
    action_id: str,
    outcome: str,
    launch_identity: str | None = None,
    response_ref: str | None = None,
    expected_state_version: int | None = None,
) -> dict:
    """Reconcile a claimed review launch (complete/timeout/uncertain). Never refunds, never relaunches
    (review R3): local termination does not prove the remote inference stopped.

    Args:
        task_id: Task id.
        action_id: The review-launch reservation.
        outcome: complete | timeout | uncertain (free text recorded).
        launch_identity: The claimed launch identity, to guard a stray settle.
        response_ref: Reference to the reviewer output artifact, if any.
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_settle_review",
            lambda: _em().settle_review(
                task_id,
                action_id=action_id,
                outcome=outcome,
                launch_identity=launch_identity,
                response_ref=response_ref,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Record Completion", annotations=_WRITE)
async def execution_record_completion(
    task_id: str,
    completion_ref: str,
    accepted_by: str,
    attestation: str | None = None,
    expected_state_version: int | None = None,
) -> dict:
    """Record the explicit FINAL completion receipt: closure -> terminal completed (review R4).
    Requires coverage complete (closed) and an attributable receipt. Terminal thereafter: no mutation
    reactivates the execution; an unattended one records a durable pending shutdown intent.

    Args:
        task_id: Task id.
        completion_ref: Reference to the final completion receipt/artifact.
        accepted_by: The identity accepting final completion.
        attestation: Optional attestation text.
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_record_completion",
            lambda: _em().record_completion(
                task_id,
                completion_ref=completion_ref,
                accepted_by=accepted_by,
                attestation=attestation,
                expected_state_version=expected_state_version,
            ),
        )
    )


@mcp.tool(title="Execution Request Shutdown", annotations=_WRITE)
async def execution_request_shutdown(
    task_id: str,
    reason: str = "expiry",
    expected_state_version: int | None = None,
) -> dict:
    """Record a durable pending shutdown intent (review R5). Idempotent. A host adapter later pauses
    the named automation and reconciles.

    Args:
        task_id: Task id.
        reason: Why shutdown is intended (e.g. expiry / completion).
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_request_shutdown",
            lambda: _em().request_shutdown(
                task_id, reason=reason, expected_state_version=expected_state_version
            ),
        )
    )


@mcp.tool(title="Execution Note Expiry Shutdown", annotations=_WRITE)
async def execution_note_expiry_shutdown(task_id: str) -> dict:
    """Record a pending expiry shutdown intent IFF the execution is expired and none is recorded yet;
    a no-op otherwise (review R5). Safe to call from a host reconciliation sweep.

    Args:
        task_id: Task id.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_note_expiry_shutdown",
            lambda: _em().note_expiry_shutdown(task_id),
        )
    )


@mcp.tool(title="Execution Reconcile Shutdown", annotations=_WRITE)
async def execution_reconcile_shutdown(
    task_id: str,
    outcome: str = "paused",
    detail: dict | None = None,
    expected_state_version: int | None = None,
) -> dict:
    """Mark a pending shutdown reconciled — the host paused the named automation through the supported
    tool (review R5). Idempotent.

    Args:
        task_id: Task id.
        outcome: Outcome of the pause (e.g. paused / no_automation).
        detail: Optional structured detail.
        expected_state_version: Optimistic-concurrency guard.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "execution_reconcile_shutdown",
            lambda: _em().reconcile_shutdown(
                task_id,
                outcome=outcome,
                detail=detail,
                expected_state_version=expected_state_version,
            ),
        )
    )


# ---------------------------------------------------------------- efficiency v2: read-only routes
@mcp.tool(title="Sched Plan", annotations=_READ)
async def sched_plan(
    at: str | None = None,
    elapsed: str | None = None,
    tz: str | None = None,
    now: str | None = None,
    ambiguous: str = "reject",
    allow_past: bool = False,
) -> dict:
    """Deterministic scheduling plan: ONE intended instant from an absolute local wall time or an
    elapsed delay, in America/New_York by default (EST/EDT handled), emitting the intended UTC
    instant, the Eastern display and one-shot scheduler submission data. Pure and read-only: it never
    creates, updates or pauses an automation — use the host's official automation tool, then confirm
    the persisted next_run_at with sched_verify. A fixed UTC-05:00 clock is honored only when
    explicitly requested (tz="UTC-05:00"). Refuses nonexistent (spring-forward) and unresolved
    ambiguous (fall-back) local times with a distinct error code.

    Args:
        at: Local wall time 'YYYY-MM-DD HH:MM[:SS]' in tz, or an ISO instant with its own offset.
        elapsed: Delay from now such as 90m, 1h30m, 45s, 2d (give at OR elapsed).
        tz: America/New_York (default), UTC-05:00 (fixed), UTC, or an IANA zone.
        now: Reference instant (ISO/epoch) for a reproducible plan; default real now.
        ambiguous: reject (default) | earlier | later for a fall-back overlap.
        allow_past: Permit an instant before now (default refuses with past_instant).
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "sched_plan",
            lambda: schedule.plan(at=at, elapsed=elapsed, tz=tz, now=now, ambiguous=ambiguous,
                                  allow_past=allow_past),
        )
    )


@mcp.tool(title="Sched Verify", annotations=_READ)
async def sched_verify(
    intended_utc: str,
    persisted: dict | None = None,
    next_run_at: str | None = None,
    active: str | None = None,
    tolerance_s: float = schedule.DEFAULT_TOLERANCE_S,
) -> dict:
    """Read-only verification of a persisted schedule against the intended instant: pass the record
    the host automation tool returned (persisted) or the bare next_run_at/active values. Verdict is
    match | mismatch | cannot_evaluate — an absent or unparseable record is never a pass or a fail.
    Only match backs a success claim; requires_native_pause=True means an ACTIVE automation sits at
    the wrong instant and must be paused/deleted through the official tool. Both UTC and Eastern
    renderings are returned.

    Args:
        intended_utc: The planned instant (sched_plan.intended_utc).
        persisted: The persisted automation record (any shape carrying next_run_at and a status).
        next_run_at: Persisted next_run_at when not passing a record.
        active: Persisted active flag / status word when not passing a record.
        tolerance_s: Allowed difference in seconds (default 60).
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "sched_verify",
            lambda: schedule.verify(intended_utc=intended_utc, persisted=persisted,
                                    next_run_at=next_run_at, active=active,
                                    tolerance_s=tolerance_s),
        )
    )


@mcp.tool(title="Job Status", annotations=_READ)
async def job_status(job_id: str, tail_bytes: int = 0) -> dict:
    """Compact status of one owned local job (started by the owned CLI `job-run`; there is no MCP
    launch route). Field-selected, fits the 4 KB budget, references retained output by path;
    a job whose supervisor vanished reads as effective_status=unknown without rewriting the record.

    Args:
        job_id: Job id.
        tail_bytes: Inline tail of stdout/stderr to include (0 = references only).
    """
    return await asyncio.to_thread(
        lambda: _wrap("job_status", lambda: _jm().status(job_id, tail_bytes=tail_bytes))
    )


@mcp.tool(title="Job Join", annotations=_READ)
async def job_join(
    job_id: str,
    timeout_s: float = JOIN_MAX_S,
    after_version: int | None = None,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> dict:
    """Wait INSIDE the tool (at most 50 s) for the job to finish or its status to change past
    after_version, stopping early at the task deadline or a paused/closed/expired/completed
    execution (stop_waiting=True). Returns deterministic changed / timed_out / stop_waiting / status /
    exit metadata with bounded tails; the full output stays on disk (job_output). One join per
    permitted interval is the whole protocol: do not add host wait/sleep loops around it, and do not
    join at all for synchronous or batched reads.

    Args:
        job_id: Job id.
        timeout_s: In-tool wait, clamped to 0..50 seconds.
        after_version: Last seen state_version; a newer version returns changed=True immediately.
        tail_bytes: Inline tail of stdout/stderr once terminal.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "job_join",
            lambda: _jm().join(job_id, timeout_s=timeout_s, after_version=after_version,
                               tail_bytes=tail_bytes),
        )
    )


@mcp.tool(title="Job List", annotations=_READ)
async def job_list(
    task_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> dict:
    """Metadata-only listing of owned jobs, newest first, bounded by count AND by the combined 4 KB
    batch budget; truncated=True with next_cursor when either bound cut the list.

    Args:
        task_id: Only jobs under this managed task.
        status: Only jobs with this effective status.
        limit: Max rows (default 20).
        cursor: Continue below this job id (from next_cursor).
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "job_list",
            lambda: _jm().list(task_id=task_id, status=status, limit=limit, cursor=cursor),
        )
    )


@mcp.tool(title="Job Output", annotations=_READ)
async def job_output(
    job_id: str,
    stream: str = "stdout",
    offset: int = 0,
    limit: int = 4096,
    tail: bool = False,
) -> dict:
    """Bounded retrieval of a job's retained stdout / stderr / supervisor log by offset+limit
    (tail=True reads the last `limit` bytes). Carries bytes_total, next_offset and a truthful
    truncated flag so the complete evidence remains reachable in further bounded reads.

    Args:
        job_id: Job id.
        stream: stdout | stderr | supervisor.
        offset: Byte offset to start from.
        limit: Max bytes to return (<= 65536).
        tail: Read the last `limit` bytes instead.
    """
    return await asyncio.to_thread(
        lambda: _wrap(
            "job_output",
            lambda: _jm().output(job_id, stream=stream, offset=offset, limit=limit, tail=tail),
        )
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
