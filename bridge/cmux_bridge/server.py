"""FastMCP server for the codex-claude-bridge (stdio).

Wraps: cmux_bridge.core.Bridge. Seven bounded tools; no generic RPC, no shell, no pane deletion,
no process cleanup. Every tool has a title and annotations; every body follows the ToolError
passthrough pattern; sync core calls run in a thread.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .core import CONTRACT, BridgeError
from .cmuxcli import CmuxError

# Truthful annotations (review item 11). `bind` takes a lease and can supersede an older binding,
# so it is neither read-only nor idempotent. `submit`/`compact` are keyed by request_id, so a
# repeat call replays the persisted receipt instead of acting twice. `wait` sends nothing but does
# persist bridge bookkeeping (per-request milestones), so it is not marked read-only.
_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_BIND = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
_BOOKKEEPING = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True
)
_SUBMIT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True
)

mcp = FastMCP(
    "codex-claude-bridge",
    instructions=f"{CONTRACT}. Observe/submit/wait/compact one Claude Code session in cmux. Bind by UUIDs, one writer per worktree, submit reconciles instead of resending. Refusals are ToolErrors with a code prefix.",
)


@lru_cache(maxsize=1)
def _bridge():
    from .core import Bridge

    return Bridge()


def _fail(name: str, e: Exception) -> ToolError:
    if isinstance(e, BridgeError):
        return ToolError(
            f"{e.code}: {e.message}" + (f" | {e.detail}" if e.detail else "")
        )
    if isinstance(e, CmuxError):
        return ToolError(f"transport_{e.kind}: {e.message}")
    return ToolError(f"{name} failed: {e}")


@mcp.tool(title="Bridge Discover", annotations=_READ_ONLY)
async def bridge_discover(
    workspace_uuid: str | None = None, max_targets: int = 20
) -> dict:
    """Diagnose cmux socket access and list Claude Code sessions visible in cmux.

    Args:
        workspace_uuid: Optional workspace UUID to filter targets.
        max_targets: Maximum targets to return (default 20, max 50).
    """
    try:
        return await asyncio.to_thread(
            _bridge().discover, workspace_uuid, min(int(max_targets), 50)
        )
    except ToolError:
        raise
    except Exception as e:
        raise _fail("bridge_discover", e) from e


@mcp.tool(title="Bridge Bind", annotations=_BIND)
async def bridge_bind(
    workspace_uuid: str,
    surface_uuid: str,
    claude_session_id: str,
    claude_pid: int,
    worktree_realpath: str,
    controller_id: str,
    role: str = "writer",
    lease_ttl_s: int = 1800,
) -> dict:
    """Bind one Claude Code session by full identity and take a controller lease.

    Args:
        workspace_uuid: cmux workspace UUID (refs like workspace:N are rejected).
        surface_uuid: cmux surface UUID hosting the Claude session.
        claude_session_id: Claude session id (with or without the claude- prefix).
        claude_pid: PID of the Claude Code process.
        worktree_realpath: Absolute path of the worktree the session runs in.
        controller_id: Stable id of this controller task.
        role: writer (may submit/compact; one per surface+worktree; REQUIRES an agent.hook event
            for this session/pid with a matching cwd) or monitor (observe/wait only; allowed with
            partial identity evidence).
        lease_ttl_s: Lease lifetime in seconds (60..86400, default 1800).
    """
    try:
        return await asyncio.to_thread(
            _bridge().bind,
            workspace_uuid,
            surface_uuid,
            claude_session_id,
            int(claude_pid),
            worktree_realpath,
            controller_id,
            role,
            int(lease_ttl_s),
        )
    except ToolError:
        raise
    except Exception as e:
        raise _fail("bridge_bind", e) from e


@mcp.tool(title="Bridge Observe", annotations=_READ_ONLY)
async def bridge_observe(
    binding_id: str,
    lines: int = 30,
    max_events: int = 200,
    after_seq: int | None = None,
) -> dict:
    """Raw observations of the bound session. Mutates no bridge state; the cursor is caller-owned.

    Returns the screen classification, ctx_used_pct, background_agents (int, or null when the
    footer does not show them), event counts with the ack's gap/boot flags, a transcript summary,
    and next_after_seq to pass back on the next call.

    Args:
        binding_id: Binding returned by bridge_bind.
        lines: Screen rows to read (default 30, max 200).
        max_events: Events to fetch after the cursor (default 200, max 400).
        after_seq: Caller-owned event cursor; defaults to the binding's bind cursor. Observing
            never consumes evidence a later bridge_wait needs.
    """
    try:
        return await asyncio.to_thread(
            _bridge().observe,
            binding_id,
            min(int(lines), 200),
            min(int(max_events), 400),
            None if after_seq is None else int(after_seq),
        )
    except ToolError:
        raise
    except Exception as e:
        raise _fail("bridge_observe", e) from e


@mcp.tool(title="Bridge Submit", annotations=_SUBMIT)
async def bridge_submit(
    binding_id: str,
    request_id: str,
    text: str,
    expected_revision: int,
    kind: str = "task",
    accept_timeout_s: float = 12.0,
) -> dict:
    """Stage literal text on the bound Claude prompt and press Enter at most once.

    This is NOT exactly-once and does not claim to be. What it establishes: Enter is pressed only
    on an editor whose staged text is exactly this request's *delivered* line. A single-line payload
    is delivered inline; a multi-line or long payload is written to an immutable (0444), bridge-
    owned task file and the delivered line is a short reference to it — the bridge never pastes raw
    multi-line text (a paste marker's line count is not payload identity, and a raw bracketed-paste
    auto-submits on this Claude build). Acceptance requires an exact transcript correlation (a user
    message for this session whose sha256 equals the delivered line's, at/after the request start).
    Everything else is retained as `uncertain` or `uncertain_foreign` and is never resolved by
    resending; only a simulated pre-send fault is a resend basis. A repeat call with the same
    request_id replays the persisted receipt (terminal) or reconciles (non-terminal). Deliveries on
    one binding are serialised: a concurrent call observes the in-flight op instead of re-sending.

    Refuses a stale revision on a NEW request, a busy/unknown/non-Claude surface, a superseded or
    released binding, and a monitor binding. The footer "← N agent" hint is reported raw and never
    gates a write.

    Args:
        binding_id: Writer binding from bridge_bind.
        request_id: Idempotency key, 1-80 chars of [A-Za-z0-9._:-].
        text: Literal prompt text (multi-line allowed; max 16000 chars; no control chars).
        expected_revision: The binding revision the caller last observed (new requests only).
        kind: task (default) or reply.
        accept_timeout_s: Seconds to wait for transcript correlation (max 60).
    """
    try:
        return await asyncio.to_thread(
            _bridge().submit,
            binding_id,
            request_id,
            text,
            int(expected_revision),
            kind,
            float(accept_timeout_s),
        )
    except ToolError:
        raise
    except Exception as e:
        raise _fail("bridge_submit", e) from e


@mcp.tool(title="Bridge Wait", annotations=_BOOKKEEPING)
async def bridge_wait(
    binding_id: str,
    until: str,
    timeout_s: float = 120.0,
    request_id: str | None = None,
    evidence: dict | None = None,
    after_seq: int | None = None,
) -> dict:
    """Wait (bounded) for accepted | turn_complete | idle | task_complete | compaction_complete.

    Sends nothing. Replays from the request's own cursor when request_id is given, so completion
    that happened before this call — including a Stop already consumed by an earlier observe —
    stays discoverable. Per-request milestones are persisted, which is the only state it writes.

    Args:
        binding_id: Binding from bridge_bind.
        until: accepted | turn_complete | idle | task_complete | compaction_complete.
            accepted and task_complete require request_id.
        timeout_s: Finite timeout in seconds (max 600; non-finite values fall back to the default).
        request_id: Scope the wait to one request and persist its milestones.
        evidence: task_complete only, and required: {"file": path, "contains": str} or
            {"file": path, "sha256": hex}. Existence is not evidence. task_complete additionally
            requires the request to be accepted, its turn complete, the file at least as new as
            acceptance, an idle prompt, AND a CURRENT controller drained_attestation inside
            evidence["drained_attestation"]: {"drained": true, "attested_by": <controller_id>,
            "evidence": <text>, "at": <iso ts at/after this request's acceptance>}. Attester,
            evidence and a fresh "at" are all required — a bare {"drained": true} or a stale one
            (timestamped before acceptance) does not satisfy it. Without a qualifying attestation,
            task_complete is simply NOT satisfied (job state stays honestly unknown; this call
            keeps waiting or times out) — it is never reported "satisfied" on a job_state of
            "unknown". For a raw acceptance/turn/artifact observation that does not claim
            completion, wait until="turn_complete" or "idle" instead.
        after_seq: Caller-owned event cursor; defaults to the request cursor, else the bind cursor.
    """
    try:
        return await asyncio.to_thread(
            _bridge().wait,
            binding_id,
            until,
            float(timeout_s),
            request_id,
            evidence,
            None if after_seq is None else int(after_seq),
        )
    except ToolError:
        raise
    except Exception as e:
        raise _fail("bridge_wait", e) from e


@mcp.tool(title="Bridge Compact", annotations=_SUBMIT)
async def bridge_compact(
    binding_id: str,
    request_id: str,
    expected_revision: int,
    checkpoint: dict,
    ctx_used_pct: float | None = None,
    reason: str | None = None,
    drained_attestation: dict | None = None,
    force: bool = False,
    timeout_s: float = 120.0,
) -> dict:
    """Apply the 30%-used/before-40%-used policy: compact at a safe boundary or defer with a reason.

    Idempotent by request_id: a repeat call replays a completed receipt or re-checks the transcript,
    and never resends /compact. Completion evidence is a transcript compact_boundary after THIS
    request's submit; SessionStart events are corroboration only, and an older one never counts.

    Requires BOTH a fresh drained-checkpoint artifact AND an explicit controller drained
    attestation before it will submit /compact: the bridge cannot natively verify that owned jobs
    are drained (the footer "← N agent" hint is not job tracking), so job state is controller-
    attested, not measured. Without the attestation it returns outcome=needs_drained_attestation.

    Args:
        binding_id: Writer binding from bridge_bind.
        request_id: Idempotency key for this compaction, 1-80 chars of [A-Za-z0-9._:-].
        expected_revision: The binding revision the caller last observed (new requests only).
        checkpoint: Required drained-checkpoint artifact: {"file": path, "contains": str} or
            {"file": path, "sha256": hex}. Its mtime must be at least as new as the last accepted
            request on this binding. force does not bypass it.
        ctx_used_pct: Controller-observed context used percentage (overrides the screen meter).
        reason: Required when the session is busy and compaction must be deferred.
        drained_attestation: {"drained": true, "attested_by": <controller_id>, "evidence": <text>}
            — the controller's explicit claim that owned jobs are drained. Recorded as attested,
            never as native job tracking. Required (or force=true) to actually submit /compact.
        force: Compact even when not due, and without an attestation (tests or explicit owner
            instruction only). Never bypasses the checkpoint artifact.
        timeout_s: Seconds to wait for completion evidence (max 600).
    """
    try:
        return await asyncio.to_thread(
            _bridge().compact,
            binding_id,
            request_id,
            int(expected_revision),
            checkpoint,
            ctx_used_pct,
            reason,
            drained_attestation,
            bool(force),
            float(timeout_s),
        )
    except ToolError:
        raise
    except Exception as e:
        raise _fail("bridge_compact", e) from e


@mcp.tool(title="Bridge Release", annotations=_BOOKKEEPING)
async def bridge_release(binding_id: str) -> dict:
    """Release the controller lease. Never closes the surface or the Claude process.

    Args:
        binding_id: Binding from bridge_bind.
    """
    try:
        return await asyncio.to_thread(_bridge().release, binding_id)
    except ToolError:
        raise
    except Exception as e:
        raise _fail("bridge_release", e) from e


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
