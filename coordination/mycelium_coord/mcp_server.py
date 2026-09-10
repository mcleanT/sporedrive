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

from .coord import Coordinator
from .model import ProtocolError
from .store import CoordStore, StoreError

_READ = ToolAnnotations(readOnlyHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
_WRITE_NI = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)

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
async def coord_create_task(task_id: str, project: str, worktree_realpath: str,
                            authorization_ref: str | None = None, revision: int = 0,
                            title: str = "") -> dict:
    """Create-or-return an authorized coordination task. Idempotent; never resets an existing task.

    Args:
        task_id: Stable task id ([A-Za-z0-9._:-], <=120).
        project: Project name.
        worktree_realpath: The executor's canonical worktree realpath.
        authorization_ref: Owner authorization reference (recorded, never a grant).
        revision: Initial task revision.
        title: Human title.
    """
    return await asyncio.to_thread(lambda: _wrap("coord_create_task", lambda: _co().create_task(
        task_id, project=project, worktree_realpath=worktree_realpath,
        authorization_ref=authorization_ref, revision=revision, title=title)))


@mcp.tool(title="Coord Attach", annotations=_WRITE_NI)
async def coord_attach(task_id: str, participant_id: str, role: str, worktree_realpath: str,
                       host: dict | None = None, allow_transition: bool = False) -> dict:
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
    return await asyncio.to_thread(lambda: _wrap("coord_attach", lambda: _co().attach(
        task_id, participant_id, role=role, worktree_realpath=worktree_realpath,
        host=host or {}, allow_transition=allow_transition)))


@mcp.tool(title="Coord Send", annotations=_WRITE)
async def coord_send(task_id: str, message_id: str, sender: str, recipient: str, kind: str,
                     task_revision: int, text: str | None = None, artifact_ref: str | None = None,
                     artifacts: list | None = None, reply_to: str | None = None,
                     correlation_id: str | None = None, host: dict | None = None) -> dict:
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
    """
    return await asyncio.to_thread(lambda: _wrap("coord_send", lambda: _co().send(
        message_id=message_id, task_id=task_id, sender=sender, recipient=recipient, kind=kind,
        task_revision=task_revision, text=text, artifact_ref=artifact_ref, artifacts=artifacts,
        reply_to=reply_to, correlation_id=correlation_id, host=host or {})))


@mcp.tool(title="Coord Inbox", annotations=_READ)
async def coord_inbox(task_id: str, participant_id: str, after_seq: int = 0,
                      limit: int = 100, kinds: list | None = None) -> dict:
    """Bounded read of messages addressed to this participant with seq > after_seq. Does NOT consume
    and does NOT move the stored cursor. Returns messages + next_after_seq.

    Args:
        task_id: Task id.
        participant_id: The reading participant.
        after_seq: Return messages with seq greater than this.
        limit: Max messages (default 100).
        kinds: Optional kind filter.
    """
    return await asyncio.to_thread(lambda: _wrap("coord_inbox", lambda: _co().inbox(
        task_id, participant_id, after_seq=after_seq, limit=limit, kinds=kinds)))


@mcp.tool(title="Coord Wait", annotations=_READ)
async def coord_wait(task_id: str, participant_id: str, after_seq: int = 0,
                     timeout_s: float = 30.0, kinds: list | None = None) -> dict:
    """Bounded wait for new addressed messages after a cursor. Truthful: a finite-timeout poll, NOT a
    host wake-up. Returns as soon as any qualifying message exists or timed_out=True at the deadline.

    Args:
        task_id: Task id.
        participant_id: The waiting participant.
        after_seq: Wait for messages with seq greater than this.
        timeout_s: Finite timeout seconds (max 600).
        kinds: Optional kind filter.
    """
    return await asyncio.to_thread(lambda: _wrap("coord_wait", lambda: _co().wait(
        task_id, participant_id, after_seq=after_seq, timeout_s=timeout_s, kinds=kinds)))


@mcp.tool(title="Coord Ack", annotations=_WRITE)
async def coord_ack(task_id: str, participant_id: str, message_id: str, note: str = "") -> dict:
    """Acknowledge RECEIPT of a specific message (not a claim its action completed). Only an
    addressed recipient may ack. Idempotent.

    Args:
        task_id: Task id.
        participant_id: The acknowledging (addressed) participant.
        message_id: Message being acknowledged.
        note: Optional note.
    """
    return await asyncio.to_thread(lambda: _wrap("coord_ack", lambda: _co().ack(
        task_id, participant_id, message_id, note=note)))


@mcp.tool(title="Coord Message State", annotations=_READ)
async def coord_message_state(task_id: str, participant_id: str, message_id: str) -> dict:
    """Return the highest distinct state of a message for this participant: persisted | delivered |
    acknowledged | completion_claimed | completed.

    Args:
        task_id: Task id.
        participant_id: Perspective participant.
        message_id: Message id.
    """
    return await asyncio.to_thread(lambda: _wrap(
        "coord_message_state", lambda: {"state": _co().message_state(task_id, participant_id, message_id)}))


@mcp.tool(title="Coord Set Cursor", annotations=_WRITE)
async def coord_set_cursor(task_id: str, participant_id: str, after_seq: int) -> dict:
    """Advance a participant's bounded NOTIFICATION cursor (monotonic). Separate from acknowledgment,
    so a missed reminder never loses a message.

    Args:
        task_id: Task id.
        participant_id: Participant.
        after_seq: New cursor (max with existing).
    """
    return await asyncio.to_thread(lambda: _wrap("coord_set_cursor", lambda: _co().set_cursor(
        task_id, participant_id, after_seq)))


@mcp.tool(title="Coord Checkpoint Publish", annotations=_WRITE)
async def coord_checkpoint_publish(task_id: str, revision: int, participant_id: str,
                                   checkpoint: dict, authorization_ref: str | None = None) -> dict:
    """Publish an immutable versioned checkpoint. Same revision + same content is idempotent; a
    different content at the same revision is rejected.

    Args:
        task_id: Task id.
        revision: Checkpoint revision.
        participant_id: Publishing participant.
        checkpoint: Checkpoint object.
        authorization_ref: Authorization reference (recorded, never a grant).
    """
    return await asyncio.to_thread(lambda: _wrap("coord_checkpoint_publish", lambda: _co().publish_checkpoint(
        task_id, revision=revision, participant_id=participant_id, checkpoint=checkpoint,
        authorization_ref=authorization_ref)))


@mcp.tool(title="Coord Checkpoint Read", annotations=_READ)
async def coord_checkpoint_read(task_id: str, revision: int | None = None) -> dict | None:
    """Read a checkpoint (latest if revision omitted).

    Args:
        task_id: Task id.
        revision: Specific revision, or null for latest.
    """
    return await asyncio.to_thread(lambda: _wrap("coord_checkpoint_read", lambda: _co().read_checkpoint(
        task_id, revision=revision)))


@mcp.tool(title="Coord Resume", annotations=_READ)
async def coord_resume(task_id: str, participant_id: str, limit: int = 50) -> dict:
    """Reattach after compaction/restart: current checkpoint + bounded UNACKNOWLEDGED messages +
    revision/authorization. Does not re-inject transcripts.

    Args:
        task_id: Task id.
        participant_id: Resuming participant.
        limit: Max pending messages returned.
    """
    return await asyncio.to_thread(lambda: _wrap("coord_resume", lambda: _co().resume(
        task_id, participant_id, limit=limit)))


@mcp.tool(title="Coord Participants", annotations=_READ)
async def coord_participants(task_id: str) -> list:
    """List attached participants for a task.

    Args:
        task_id: Task id.
    """
    return await asyncio.to_thread(lambda: _wrap("coord_participants", lambda: _co().list_participants(task_id)))


@mcp.tool(title="Coord List Tasks", annotations=_READ)
async def coord_list_tasks(project: str | None = None, limit: int = 200,
                           cursor: str | None = None) -> dict:
    """Bounded task discovery: known tasks (optionally scoped to one project) as lightweight
    summaries. The only way to find a task whose id you do not already hold; it never joins or
    notifies. Pick a task_id and attach separately to join.

    Args:
        project: Restrict to this project when set.
        limit: Max tasks per page (clamped to a finite server maximum).
        cursor: next_cursor from a prior page; omit for the first page.
    """
    return await asyncio.to_thread(lambda: _wrap("coord_list_tasks", lambda: _co().list_tasks(
        project, limit=limit, cursor=cursor)))


@mcp.tool(title="Coord Find Recipients", annotations=_READ)
async def coord_find_recipients(task_id: str, role: str | None = None, host_kind: str | None = None,
                                exclude: str | None = None, attached_only: bool = True,
                                limit: int = 200, cursor: str | None = None) -> dict:
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
    return await asyncio.to_thread(lambda: _wrap("coord_find_recipients", lambda: _co().find_recipients(
        task_id, role=role, host_kind=host_kind, exclude=exclude,
        attached_only=attached_only, limit=limit, cursor=cursor)))


@mcp.tool(title="Coord List Sessions", annotations=_READ)
async def coord_list_sessions(host_kind: str, live_only: bool = False, limit: int = 200,
                              cursor: str | None = None) -> dict:
    """Bounded session discovery for one host kind: native-session selection records and the
    task/participant each is bound to. Each carries `live` (still attached on exactly that session);
    stale post-rebind records are dropped under live_only.

    Args:
        host_kind: Host kind to enumerate (claude/codex).
        live_only: Drop stale (non-live) selection records.
        limit: Max sessions per page (clamped to a finite server maximum).
        cursor: next_cursor from a prior page; omit for the first page.
    """
    return await asyncio.to_thread(lambda: _wrap("coord_list_sessions", lambda: _co().list_sessions(
        host_kind, live_only=live_only, limit=limit, cursor=cursor)))


@mcp.tool(title="Coord Detach", annotations=_WRITE)
async def coord_detach(task_id: str, participant_id: str) -> dict:
    """Detach a participant WITHOUT terminating the peer. Leaves messages/acks intact.

    Args:
        task_id: Task id.
        participant_id: Participant to detach.
    """
    return await asyncio.to_thread(lambda: _wrap("coord_detach", lambda: _co().detach(task_id, participant_id)))


@mcp.tool(title="Coord Notify Via Bridge", annotations=_WRITE)
async def coord_notify_via_bridge(task_id: str, message_id: str, binding_id: str, controller_id: str,
                                  expected_revision: int, accept_timeout_s: float = 12.0) -> dict:
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
    """
    from .bridge_link import notify_via_bridge
    return await asyncio.to_thread(lambda: _wrap("coord_notify_via_bridge", lambda: notify_via_bridge(
        _co(), task_id=task_id, message_id=message_id, binding_id=binding_id,
        controller_id=controller_id, expected_revision=expected_revision, accept_timeout_s=accept_timeout_s)))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
