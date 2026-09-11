"""Shared CLI for the coordination protocol: ``mycelium-coord <op> ...`` — the exact same operations
both native hosts consume (and the launcher both plugin manifests point at). Every op prints one JSON
object to stdout; protocol/store errors print ``{"error": <code>, ...}`` and exit non-zero. Stateless
per invocation: state lives entirely in the CoordStore.
"""

from __future__ import annotations

import argparse
import json
import sys

from .coord import Coordinator
from .execution import ExecutionManager
from . import schedule
from .jobs import DEFAULT_DEADLINE_S, DEFAULT_TAIL_BYTES, JOIN_MAX_S, ON_STOP_POLICIES, JobManager
from .model import ProtocolError
from .store import CoordStore, StoreError
from .views import BATCH_BUDGET_BYTES


def _emit(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


def _kv(pairs: list[str] | None) -> dict:
    """Parse repeated --host k=v pairs into a dict."""
    out: dict = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"bad key=value: {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def _parse_artifacts(vals):
    """Parse repeated --artifact values. A value starting with { or [ is JSON (structured
    evidence like {"file":..., "sha256":...}); anything else is a bare reference string."""
    if not vals:
        return None
    out = []
    for v in vals:
        vs = v.strip()
        out.append(json.loads(vs) if vs[:1] in ("{", "[") else v)
    return out


def _load_json_arg(value: str | None):
    if value is None:
        return None
    if value == "-":
        return json.load(sys.stdin)
    return json.loads(value)


def _load_json_source(value: str | None):
    """'-' = stdin, a JSON literal, or a file path holding JSON (a persisted automation record)."""
    if value is None:
        return None
    if value == "-":
        return json.load(sys.stdin)
    vs = value.strip()
    if vs[:1] in ("{", "["):
        return json.loads(vs)
    with open(value) as f:
        return json.load(f)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="mycelium-coord", description=__doc__)
    ap.add_argument(
        "--root",
        default=None,
        help="coordination state root (default MYCELIUM_COORD_DIR or ~/.local/state/mycelium/coordination)",
    )
    sub = ap.add_subparsers(dest="op", required=True)

    p = sub.add_parser("create-task")
    p.add_argument("task_id")
    p.add_argument("--project", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--revision", type=int, default=0)
    p.add_argument("--authorization", default=None)

    p = sub.add_parser("task")
    p.add_argument("task_id")

    p = sub.add_parser("list-tasks")
    p.add_argument("--project", default=None)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--cursor", default=None)

    p = sub.add_parser("find-recipients")
    p.add_argument("task_id")
    p.add_argument("--role", default=None)
    p.add_argument("--host", default=None, dest="host_kind")
    p.add_argument("--exclude", default=None)
    p.add_argument("--include-detached", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--cursor", default=None)

    p = sub.add_parser("list-sessions")
    p.add_argument("host_kind")
    p.add_argument("--live-only", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--cursor", default=None)

    p = sub.add_parser("attach")
    p.add_argument("task_id")
    p.add_argument("participant_id")
    p.add_argument(
        "--role", required=True, choices=["supervisor", "executor", "observer"]
    )
    p.add_argument("--worktree", required=True)
    p.add_argument(
        "--host",
        action="append",
        default=[],
        help="native identity k=v (host=, session=, native_id=)",
    )
    p.add_argument("--allow-transition", action="store_true")

    p = sub.add_parser("detach")
    p.add_argument("task_id")
    p.add_argument("participant_id")
    p = sub.add_parser("participants")
    p.add_argument("task_id")

    p = sub.add_parser("send")
    p.add_argument("task_id")
    p.add_argument("--id", required=True, dest="message_id")
    p.add_argument("--from", required=True, dest="sender")
    p.add_argument("--to", required=True, dest="recipient")
    p.add_argument("--kind", required=True)
    p.add_argument("--revision", type=int, required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--artifact-ref")
    p.add_argument("--artifact", action="append", default=None, dest="artifacts")
    p.add_argument("--reply-to", default=None)
    p.add_argument("--correlation-id", default=None)
    p.add_argument("--host", action="append", default=[])
    p.add_argument(
        "--execution-action",
        default=None,
        dest="execution_action_id",
        help="reservation funding this work request on a managed task (review R1)",
    )
    p.add_argument(
        "--actionable",
        default=None,
        action=argparse.BooleanOptionalAction,
        help="explicit managed disposition (review R1): --actionable marks this message as a work "
        "dispatch and REQUIRES a compatible reservation (--execution-action); --no-actionable marks "
        "a non-actionable status/notice that never wakes a completed/closed executor. Default: "
        "derived from --kind (task-like kinds are actionable). A trusted-agent disposition, never a "
        "free-text parse.",
    )

    p = sub.add_parser("inbox")
    p.add_argument("task_id")
    p.add_argument("participant_id")
    p.add_argument("--after", type=int, default=0)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--full", action="store_true", help="include full message bodies")
    p.add_argument("--kind", action="append", default=None, dest="kinds")

    p = sub.add_parser("wait")
    p.add_argument("task_id")
    p.add_argument("participant_id")
    p.add_argument("--after", type=int, default=0)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--kind", action="append", default=None, dest="kinds")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--full", action="store_true", help="include full message bodies")

    p = sub.add_parser("read-message", help="read one full addressed message, without acknowledging")
    p.add_argument("task_id")
    p.add_argument("participant_id")
    p.add_argument("message_id")

    p = sub.add_parser("ack")
    p.add_argument("task_id")
    p.add_argument("participant_id")
    p.add_argument("message_id")
    p.add_argument("--note", default="")

    p = sub.add_parser("state")
    p.add_argument("task_id")
    p.add_argument("participant_id")
    p.add_argument("message_id")

    p = sub.add_parser("set-cursor")
    p.add_argument("task_id")
    p.add_argument("participant_id")
    p.add_argument("after", type=int)

    p = sub.add_parser("checkpoint-publish")
    p.add_argument("task_id")
    p.add_argument("--revision", type=int, required=True)
    p.add_argument("--by", required=True, dest="participant_id")
    p.add_argument("--checkpoint", required=True, help="JSON object or - for stdin")
    p.add_argument("--authorization", default=None)

    p = sub.add_parser("checkpoint-read")
    p.add_argument("task_id")
    p.add_argument("--revision", type=int, default=None)

    p = sub.add_parser("resume")
    p.add_argument("task_id")
    p.add_argument("participant_id")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--full", action="store_true", help="include full checkpoint and message bodies")
    p.add_argument("--after-checkpoint", type=int, default=None)

    p = sub.add_parser("select-session")
    p.add_argument("task_id")
    p.add_argument("participant_id")
    p.add_argument("--host", required=True, dest="host_kind")
    p.add_argument("--session", required=True, dest="session_id")
    p.add_argument("--worktree", default=None)

    p = sub.add_parser("resolve-session")
    p.add_argument("host_kind")
    p.add_argument("session_id")

    p = sub.add_parser("verify-session")
    p.add_argument("task_id")
    p.add_argument("participant_id")
    p.add_argument("host_kind")
    p.add_argument("session_id")

    p = sub.add_parser("notify-via-bridge")
    p.add_argument("task_id")
    p.add_argument("message_id")
    p.add_argument("--binding-id", required=True)
    p.add_argument("--controller-id", required=True)
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--accept-timeout", type=float, default=12.0)
    p.add_argument("--execution-action", default=None, dest="execution_action_id")

    # ---- execution record (SporeDrive stopping/permission layer; same core as MCP) ----
    p = sub.add_parser("exec-open")
    p.add_argument("task_id")
    p.add_argument("--execution-id", required=True, dest="execution_id")
    p.add_argument("--scope", required=True, dest="scope_ref")
    p.add_argument("--authorization", required=True, dest="authorization_ref")
    p.add_argument("--manifest", default=None, help="JSON {cid:{...}} or - for stdin")
    p.add_argument(
        "--limits", default=None, help="JSON limits overrides or - for stdin"
    )
    p.add_argument("--expires-at", default=None, dest="expires_at")
    p.add_argument("--phase", default="implementation")
    p.add_argument("--prev", default=None, dest="previous_execution_id")
    p.add_argument(
        "--unattended",
        action="store_true",
        help="unattended/automation-driven: requires a finite --expires-at and --automation-ref (R5)",
    )
    p.add_argument(
        "--automation-ref",
        default=None,
        dest="automation_ref",
        help="automation to pause on shutdown for an unattended execution (R5)",
    )

    p = sub.add_parser("exec-read")
    p.add_argument("task_id")
    p = sub.add_parser("exec-status")
    p.add_argument("task_id")

    p = sub.add_parser("exec-reserve")
    p.add_argument("task_id")
    p.add_argument("--action-id", required=True, dest="action_id")
    p.add_argument(
        "--kind", required=True, choices=["work_dispatch", "review_launch", "repair"]
    )
    p.add_argument("--purpose", default=None)
    p.add_argument("--criterion", default=None, dest="criterion_ref")
    p.add_argument("--repair-blocker", default=None, dest="repair_blocker_id")
    p.add_argument(
        "--dispatch-binding",
        default=None,
        dest="dispatch_binding",
        help="pre-bind this reservation to one concrete dispatch identity (review R1)",
    )
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-settle")
    p.add_argument("task_id")
    p.add_argument("--action-id", required=True, dest="action_id")
    p.add_argument("--outcome", required=True)
    p.add_argument("--evidence", default=None, dest="evidence_ref")
    p.add_argument("--criterion", default=None, dest="criterion_ref")
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-record-evidence")
    p.add_argument("task_id")
    p.add_argument("--criterion", required=True, dest="criterion_id")
    p.add_argument("--evidence", required=True, dest="evidence_ref")
    p.add_argument("--attestation", default=None)
    p.add_argument("--sha256", default=None, dest="evidence_sha256")
    p.add_argument("--by", default=None, dest="accepted_by")
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-open-blocker")
    p.add_argument("task_id")
    p.add_argument("--blocker-id", required=True, dest="blocker_id")
    p.add_argument("--criterion", required=True, dest="criterion_id")
    p.add_argument("--evidence", required=True, dest="evidence_ref")
    p.add_argument("--description", default=None)
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-resolve-blocker")
    p.add_argument("task_id")
    p.add_argument("--blocker-id", required=True, dest="blocker_id")
    p.add_argument("--resolution", default=None, dest="resolution_ref")
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-backlog")
    p.add_argument("task_id")
    p.add_argument("--item", required=True)
    p.add_argument("--source", default=None)
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-reserve-call")
    p.add_argument("task_id")
    p.add_argument("--action-id", required=True, dest="action_id")
    p.add_argument("--phase", required=True, choices=["technical_debug", "acceptance"])
    p.add_argument("--freeze-identity", required=True, dest="freeze_identity")
    p.add_argument("--purpose", default=None)
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-settle-call")
    p.add_argument("task_id")
    p.add_argument("--action-id", required=True, dest="action_id")
    p.add_argument("--freeze-identity", required=True, dest="freeze_identity")
    p.add_argument("--outcome", required=True)
    p.add_argument("--response", default=None, dest="response_ref")
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-pause")
    p.add_argument("task_id")
    p.add_argument("--authorization", required=True, dest="authorization_ref")
    p.add_argument("--reason", default=None)
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-unpause")
    p.add_argument("task_id")
    p.add_argument("--authorization", required=True, dest="authorization_ref")
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-change-limits")
    p.add_argument("task_id")
    p.add_argument("--authorization", required=True, dest="authorization_ref")
    p.add_argument(
        "--changes", required=True, help="JSON object of limit changes or - for stdin"
    )
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-set-phase")
    p.add_argument("task_id")
    p.add_argument(
        "--phase",
        required=True,
        choices=[
            "implementation",
            "technical_debug",
            "acceptance",
            "execution",
            "closure",
        ],
    )
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    # --- review R1/R3/R4/R5 managed-launch and lifecycle surfaces ---
    p = sub.add_parser("exec-claim-dispatch")
    p.add_argument("task_id")
    p.add_argument("--action-id", required=True, dest="action_id")
    p.add_argument("--dispatch-identity", required=True, dest="dispatch_identity")
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-claim-review")
    p.add_argument("task_id")
    p.add_argument("--execution-id", required=True, dest="execution_id")
    p.add_argument("--action-id", required=True, dest="action_id")
    p.add_argument("--launch-identity", required=True, dest="launch_identity")
    p.add_argument(
        "--deadline",
        type=float,
        default=None,
        dest="caller_deadline_seconds",
        help="caller deadline in seconds; may only SHORTEN the policy deadline",
    )
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-settle-review")
    p.add_argument("task_id")
    p.add_argument("--action-id", required=True, dest="action_id")
    p.add_argument("--outcome", required=True)
    p.add_argument("--launch-identity", default=None, dest="launch_identity")
    p.add_argument("--response", default=None, dest="response_ref")
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-record-completion")
    p.add_argument("task_id")
    p.add_argument("--completion-ref", required=True, dest="completion_ref")
    p.add_argument("--by", required=True, dest="accepted_by")
    p.add_argument("--attestation", default=None)
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-request-shutdown")
    p.add_argument("task_id")
    p.add_argument("--reason", default="expiry")
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    p = sub.add_parser("exec-note-expiry-shutdown")
    p.add_argument("task_id")

    p = sub.add_parser("exec-reconcile-shutdown")
    p.add_argument("task_id")
    p.add_argument("--outcome", default="paused")
    p.add_argument("--detail", default=None)
    p.add_argument(
        "--expected-version", type=int, default=None, dest="expected_state_version"
    )

    # ------------------------------------------------------------ efficiency v2: scheduling + owned jobs
    p = sub.add_parser("sched-plan", help="plan one intended instant (America/New_York by default); never submits")
    p.add_argument("--at", default=None, help="local wall time 'YYYY-MM-DD HH:MM[:SS]' in --tz, or an ISO instant with its own offset")
    p.add_argument("--in", dest="elapsed", default=None, help="elapsed delay from now, e.g. 90m, 1h30m, 45s, 2d")
    p.add_argument("--tz", default=None, help="America/New_York (default) | UTC-05:00 (fixed, only when explicitly wanted) | UTC | IANA zone")
    p.add_argument("--now", default=None, help="reference instant (ISO/epoch) for a reproducible plan; default real now")
    p.add_argument("--ambiguous", default="reject", choices=list(schedule.AMBIGUOUS_POLICIES), help="fall-back overlap policy")
    p.add_argument("--allow-past", action="store_true", dest="allow_past")

    p = sub.add_parser("sched-verify", help="read-only: compare a persisted next_run_at/active flag with the intended instant")
    p.add_argument("--intended-utc", required=True, dest="intended_utc")
    p.add_argument("--persisted-json", default=None, dest="persisted_json", help="file path, '-' (stdin) or JSON literal of the persisted automation record")
    p.add_argument("--next-run-at", default=None, dest="next_run_at")
    p.add_argument("--active", default=None, help="true/false or a status word (active/paused)")
    p.add_argument("--tolerance", type=float, default=schedule.DEFAULT_TOLERANCE_S, help="seconds")

    p = sub.add_parser("job-run", help="start an owned local job by CLI (never over MCP): -- <command> [args]")
    p.add_argument("job_id")
    p.add_argument("--cwd", default=None)
    p.add_argument("--deadline", type=float, default=DEFAULT_DEADLINE_S, dest="deadline_s", help="wall-clock seconds before the wrapper terminates its own child")
    p.add_argument("--task", default=None, dest="task_id", help="managed task id (with --action-id)")
    p.add_argument("--action-id", default=None, dest="action_id", help="open work reservation the job runs under")
    p.add_argument("--dispatch-identity", default=None, dest="dispatch_identity", help="the dispatch the reservation is bound to")
    p.add_argument("--on-stop", default="keep", choices=list(ON_STOP_POLICIES), dest="on_stop", help="what the wrapper does to its child when the execution stops")
    p.add_argument("--label", default=None)
    p.add_argument("--join", type=float, default=0.0, dest="join_s", help=f"also join inside this call for up to N s (max {JOIN_MAX_S:.0f})")
    p.add_argument("--tail", type=int, default=DEFAULT_TAIL_BYTES, dest="tail_bytes")
    p.add_argument("argv", nargs="*", default=[], help="everything after -- is the command and its arguments")

    p = sub.add_parser("job-join", help=f"wait inside the tool (<= {JOIN_MAX_S:.0f}s) for change/terminal/stop; deterministic metadata")
    p.add_argument("job_id")
    p.add_argument("--timeout", type=float, default=JOIN_MAX_S, dest="timeout_s")
    p.add_argument("--after-version", type=int, default=None, dest="after_version")
    p.add_argument("--tail", type=int, default=DEFAULT_TAIL_BYTES, dest="tail_bytes")

    p = sub.add_parser("job-status", help="compact status of one owned job")
    p.add_argument("job_id")
    p.add_argument("--tail", type=int, default=0, dest="tail_bytes")

    p = sub.add_parser("job-list", help="metadata-only listing within the combined byte budget")
    p.add_argument("--task", default=None, dest="task_id")
    p.add_argument("--status", default=None)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--cursor", default=None)

    p = sub.add_parser("job-output", help="bounded retrieval of retained stdout/stderr/supervisor output")
    p.add_argument("job_id")
    p.add_argument("--stream", default="stdout", choices=["stdout", "stderr", "supervisor"])
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=4096)
    p.add_argument("--tail", action="store_true", help="read the last --limit bytes")
    p.add_argument("--budget", type=int, default=BATCH_BUDGET_BYTES, help=f"max bytes of this whole response as emitted (default {BATCH_BUDGET_BYTES}; 0 = only --limit applies, an explicit larger evidence read)")

    p = sub.add_parser("job-cancel", help="terminate this wrapper's own child for one job")
    p.add_argument("job_id")
    p.add_argument("--reason", default="cancel_requested")

    return ap


def _split_command(argv: list[str] | None):
    """For ``job-run``, everything after the first ``--`` is the command verbatim (argparse must
    never reinterpret the job's own flags). Returns (argv_for_argparse, command_or_None)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "job-run" in argv and "--" in argv and argv.index("job-run") < argv.index("--"):
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, None


def run(argv: list[str] | None = None) -> int:
    argv, command = _split_command(argv)
    args = build_parser().parse_args(argv)
    if command is not None:
        args.argv = command
    store = CoordStore(args.root)
    co = Coordinator(store)
    em = ExecutionManager(store)
    jm = JobManager(store)
    op = args.op
    try:
        if op == "create-task":
            _emit(
                co.create_task(
                    args.task_id,
                    project=args.project,
                    worktree_realpath=args.worktree,
                    title=args.title,
                    revision=args.revision,
                    authorization_ref=args.authorization,
                )
            )
        elif op == "task":
            _emit(co.get_task(args.task_id))
        elif op == "list-tasks":
            _emit(co.list_tasks(args.project, limit=args.limit, cursor=args.cursor))
        elif op == "find-recipients":
            _emit(
                co.find_recipients(
                    args.task_id,
                    role=args.role,
                    host_kind=args.host_kind,
                    exclude=args.exclude,
                    attached_only=not args.include_detached,
                    limit=args.limit,
                    cursor=args.cursor,
                )
            )
        elif op == "list-sessions":
            _emit(
                co.list_sessions(
                    args.host_kind,
                    live_only=args.live_only,
                    limit=args.limit,
                    cursor=args.cursor,
                )
            )
        elif op == "attach":
            _emit(
                co.attach(
                    args.task_id,
                    args.participant_id,
                    role=args.role,
                    worktree_realpath=args.worktree,
                    host=_kv(args.host),
                    allow_transition=args.allow_transition,
                )
            )
        elif op == "detach":
            _emit(co.detach(args.task_id, args.participant_id))
        elif op == "participants":
            _emit(co.list_participants(args.task_id))
        elif op == "send":
            _emit(
                co.send(
                    message_id=args.message_id,
                    task_id=args.task_id,
                    sender=args.sender,
                    recipient=args.recipient,
                    kind=args.kind,
                    task_revision=args.revision,
                    text=args.text,
                    artifact_ref=args.artifact_ref,
                    artifacts=_parse_artifacts(args.artifacts),
                    reply_to=args.reply_to,
                    correlation_id=args.correlation_id,
                    host=_kv(args.host),
                    execution_action_id=args.execution_action_id,
                    actionable=args.actionable,
                )
            )
        elif op == "inbox":
            _emit(
                co.inbox(
                    args.task_id,
                    args.participant_id,
                    after_seq=args.after,
                    limit=args.limit,
                    kinds=args.kinds,
                    compact=not args.full,
                )
            )
        elif op == "wait":
            _emit(
                co.wait(
                    args.task_id,
                    args.participant_id,
                    after_seq=args.after,
                    timeout_s=args.timeout,
                    kinds=args.kinds,
                    limit=args.limit,
                    compact=not args.full,
                )
            )
        elif op == "read-message":
            _emit(co.read_message(args.task_id, args.participant_id, args.message_id))
        elif op == "ack":
            _emit(
                co.ack(
                    args.task_id, args.participant_id, args.message_id, note=args.note
                )
            )
        elif op == "state":
            _emit(
                {
                    "state": co.message_state(
                        args.task_id, args.participant_id, args.message_id
                    )
                }
            )
        elif op == "set-cursor":
            _emit(co.set_cursor(args.task_id, args.participant_id, args.after))
        elif op == "checkpoint-publish":
            _emit(
                co.publish_checkpoint(
                    args.task_id,
                    revision=args.revision,
                    participant_id=args.participant_id,
                    checkpoint=_load_json_arg(args.checkpoint),
                    authorization_ref=args.authorization,
                )
            )
        elif op == "checkpoint-read":
            _emit(co.read_checkpoint(args.task_id, revision=args.revision))
        elif op == "resume":
            _emit(co.resume(args.task_id, args.participant_id, limit=args.limit,
                            compact=not args.full, after_checkpoint_revision=args.after_checkpoint))
        elif op == "select-session":
            _emit(
                co.select_session(
                    args.task_id,
                    args.participant_id,
                    host_kind=args.host_kind,
                    session_id=args.session_id,
                    worktree=args.worktree,
                )
            )
        elif op == "resolve-session":
            _emit(co.resolve_session(args.host_kind, args.session_id) or {})
        elif op == "verify-session":
            ok = co.verify_participant_session(
                args.task_id, args.participant_id, args.host_kind, args.session_id
            )
            _emit(
                {
                    "ok": bool(ok),
                    "task": args.task_id,
                    "participant": args.participant_id,
                }
            )
            return 0 if ok else 3
        elif op == "notify-via-bridge":
            from .bridge_link import notify_via_bridge

            _emit(
                notify_via_bridge(
                    co,
                    task_id=args.task_id,
                    message_id=args.message_id,
                    binding_id=args.binding_id,
                    controller_id=args.controller_id,
                    expected_revision=args.expected_revision,
                    accept_timeout_s=args.accept_timeout,
                    execution_action_id=args.execution_action_id,
                )
            )
        elif op == "exec-open":
            _emit(
                em.open_execution(
                    args.task_id,
                    execution_id=args.execution_id,
                    scope_ref=args.scope_ref,
                    authorization_ref=args.authorization_ref,
                    acceptance_manifest=_load_json_arg(args.manifest),
                    limits_overrides=_load_json_arg(args.limits),
                    expires_at=args.expires_at,
                    phase=args.phase,
                    previous_execution_id=args.previous_execution_id,
                    attended=not args.unattended,
                    automation_ref=args.automation_ref,
                )
            )
        elif op == "exec-read":
            _emit(em.read_execution(args.task_id))
        elif op == "exec-status":
            _emit(em.status(args.task_id))
        elif op == "exec-reserve":
            _emit(
                em.reserve(
                    args.task_id,
                    action_id=args.action_id,
                    kind=args.kind,
                    purpose=args.purpose,
                    criterion_ref=args.criterion_ref,
                    repair_blocker_id=args.repair_blocker_id,
                    dispatch_binding=args.dispatch_binding,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-settle":
            _emit(
                em.settle(
                    args.task_id,
                    action_id=args.action_id,
                    outcome=args.outcome,
                    evidence_ref=args.evidence_ref,
                    criterion_ref=args.criterion_ref,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-record-evidence":
            _emit(
                em.record_evidence(
                    args.task_id,
                    criterion_id=args.criterion_id,
                    evidence_ref=args.evidence_ref,
                    attestation=args.attestation,
                    evidence_sha256=args.evidence_sha256,
                    accepted_by=args.accepted_by,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-open-blocker":
            _emit(
                em.open_blocker(
                    args.task_id,
                    blocker_id=args.blocker_id,
                    criterion_id=args.criterion_id,
                    evidence_ref=args.evidence_ref,
                    description=args.description,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-resolve-blocker":
            _emit(
                em.resolve_blocker(
                    args.task_id,
                    blocker_id=args.blocker_id,
                    resolution_ref=args.resolution_ref,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-backlog":
            _emit(
                em.add_backlog(
                    args.task_id,
                    item=args.item,
                    source=args.source,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-reserve-call":
            _emit(
                em.reserve_call(
                    args.task_id,
                    action_id=args.action_id,
                    phase=args.phase,
                    freeze_identity=args.freeze_identity,
                    purpose=args.purpose,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-settle-call":
            _emit(
                em.settle_call(
                    args.task_id,
                    action_id=args.action_id,
                    freeze_identity=args.freeze_identity,
                    outcome=args.outcome,
                    response_ref=args.response_ref,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-pause":
            _emit(
                em.pause(
                    args.task_id,
                    authorization_ref=args.authorization_ref,
                    reason=args.reason,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-unpause":
            _emit(
                em.unpause(
                    args.task_id,
                    authorization_ref=args.authorization_ref,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-change-limits":
            _emit(
                em.change_limits(
                    args.task_id,
                    authorization_ref=args.authorization_ref,
                    changes=_load_json_arg(args.changes),
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-set-phase":
            _emit(
                em.set_phase(
                    args.task_id,
                    phase=args.phase,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-claim-dispatch":
            res = em.claim_dispatch(
                args.task_id,
                action_id=args.action_id,
                dispatch_identity=args.dispatch_identity,
                expected_state_version=args.expected_state_version,
            )
            _emit(res)
            return 0 if res.get("ok") else 3
        elif op == "exec-claim-review":
            res = em.claim_review(
                args.task_id,
                execution_id=args.execution_id,
                action_id=args.action_id,
                launch_identity=args.launch_identity,
                caller_deadline_seconds=args.caller_deadline_seconds,
                expected_state_version=args.expected_state_version,
            )
            _emit(res)
            return 0 if res.get("ok") else 3
        elif op == "exec-settle-review":
            _emit(
                em.settle_review(
                    args.task_id,
                    action_id=args.action_id,
                    outcome=args.outcome,
                    launch_identity=args.launch_identity,
                    response_ref=args.response_ref,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-record-completion":
            _emit(
                em.record_completion(
                    args.task_id,
                    completion_ref=args.completion_ref,
                    accepted_by=args.accepted_by,
                    attestation=args.attestation,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-request-shutdown":
            _emit(
                em.request_shutdown(
                    args.task_id,
                    reason=args.reason,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "exec-note-expiry-shutdown":
            _emit(em.note_expiry_shutdown(args.task_id))
        elif op == "exec-reconcile-shutdown":
            _emit(
                em.reconcile_shutdown(
                    args.task_id,
                    outcome=args.outcome,
                    detail=args.detail,
                    expected_state_version=args.expected_state_version,
                )
            )
        elif op == "sched-plan":
            _emit(
                schedule.plan(
                    at=args.at,
                    elapsed=args.elapsed,
                    tz=args.tz,
                    now=args.now,
                    ambiguous=args.ambiguous,
                    allow_past=args.allow_past,
                )
            )
        elif op == "sched-verify":
            res = schedule.verify(
                intended_utc=args.intended_utc,
                persisted=_load_json_source(args.persisted_json),
                next_run_at=args.next_run_at,
                active=args.active,
                tolerance_s=args.tolerance,
            )
            _emit(res)
            return 0 if res.get("ok") else 3
        elif op == "job-run":
            res = jm.run(
                args.job_id,
                list(args.argv),
                cwd=args.cwd,
                deadline_s=args.deadline_s,
                task_id=args.task_id,
                action_id=args.action_id,
                dispatch_identity=args.dispatch_identity,
                on_stop=args.on_stop,
                label=args.label,
                join_s=args.join_s,
                tail_bytes=args.tail_bytes,
            )
            _emit(res)
            return 0 if res.get("effective_status") != "refused" else 3
        elif op == "job-join":
            _emit(
                jm.join(
                    args.job_id,
                    timeout_s=args.timeout_s,
                    after_version=args.after_version,
                    tail_bytes=args.tail_bytes,
                )
            )
        elif op == "job-status":
            _emit(jm.status(args.job_id, tail_bytes=args.tail_bytes))
        elif op == "job-list":
            _emit(jm.list(task_id=args.task_id, status=args.status, limit=args.limit, cursor=args.cursor))
        elif op == "job-output":
            _emit(
                jm.output(
                    args.job_id,
                    stream=args.stream,
                    offset=args.offset,
                    limit=args.limit,
                    tail=args.tail,
                    budget=args.budget,
                )
            )
        elif op == "job-cancel":
            _emit(jm.cancel(args.job_id, reason=args.reason))
        else:  # pragma: no cover
            _emit({"error": "unknown_op", "op": op})
            return 2
        return 0
    except (ProtocolError, StoreError) as e:
        _emit({"error": e.code, "message": e.message, "path": getattr(e, "path", "")})
        return 3


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
