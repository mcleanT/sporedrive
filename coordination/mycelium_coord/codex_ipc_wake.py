"""Codex (ChatGPT desktop) idle-wake adapter — SEAM ONLY, not a dispatcher or daemon.

Criterion D (contract §4). The desktop app exposes an ALREADY-RUNNING owner-only IPC router at
``~/.codex/ipc/ipc.sock`` (0600, uid-owned). A bounded, zero-model-turn, READ-ONLY probe can connect,
``initialize`` and run ``thread-owner-discovery`` to learn which live client owns a conversation — the
supervisor validated exactly this (CODEX-IPC-PROBE.json). This module wraps that probe and BUILDS the
``thread-follower-start-turn`` / ``thread-follower-steer-turn`` request frames, but it deliberately
does NOT fire them:

  * it starts no daemon, no second app-server and no listener (that would fabricate reachability);
  * it never mutates private app state and never resumes a live conversation in a second process;
  * the start/steer SEAM refuses to send unless it is (a) explicitly ARMED by a caller that has done
    the ready/arm/yield handshake AND (b) the installed app build matches the exact validated hash —
    otherwise it raises rather than sending a version-sensitive frame into an unknown protocol.

So the wake status stays ``not_established`` until a live start/steer is validated inside a reserved
acceptance turn. The wire contract is read from THIS installation's app.asar and is experimental and
version-sensitive; every protocol version and the app-build hash are pinned and re-checked.

Wire contract (read read-only from the pinned app.asar, 2026-09-10):
  * the router routes a request by its TOP-LEVEL ``targetClientId`` (``findClientForRequest`` reads
    ``t.targetClientId``), so the frame carries ``targetClientId`` beside ``method``/``params`` — NOT
    inside ``params``;
  * the idle-wake method is ``thread-follower-start-turn`` (v2), params ``{conversationId, turnStart}``
    where ``turnStart = {request, context}`` and the app checks ``turnStart.context.responseItems``;
  * the running-turn steer is ``thread-follower-steer-turn`` (v1), params
    ``{conversationId, input, clientUserMessageId, ...}`` — the content field is ``input`` (a list of
    turn-input items), NOT a ``steer`` object;
  * a turn-input item is ``{"type": "text", "text": <str>, "text_elements": []}``.

The one part NOT fully validated is the inner ``turnStart.request`` schema (the app-server turn
request carries many more fields — turnTrigger, cwd, approvalPolicy, permissions, model, effort,
serviceTier, …). ``WAKE_SCHEMA_LIMITATION`` records that; it is the concrete reason a live start MUST
be a reserved, human-authorized canary rather than something this module fires on its own.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import struct
import time
import uuid
from pathlib import Path

# --- pinned wire contract (validated read-only on 2026-09-10; CODEX-IPC-PROBE.json) ---------------
DEFAULT_SOCKET_PATH = str(Path.home() / ".codex/ipc/ipc.sock")
DEFAULT_APP_ASAR_PATH = "/Applications/ChatGPT.app/Contents/Resources/app.asar"
# The EXACT app.asar this protocol was read from. A different build may change the frames, so the
# start/steer seam refuses to fire against any other hash rather than guess (strict app-build guard).
EXPECTED_APP_ASAR_SHA256 = (
    "077cc65356aeae34c5d8b4de0b4cc383f6fb137ed1d69a9b3dfe69ffafa058ab"
)
# Method names + versions read from the installed build's IPC version map
# ("thread-owner-discovery":1, "thread-follower-start-turn":2, "thread-follower-steer-turn":1).
METHOD_OWNER_DISCOVERY = "thread-owner-discovery"
METHOD_START_TURN = "thread-follower-start-turn"
METHOD_STEER_TURN = "thread-follower-steer-turn"
OWNER_DISCOVERY_VERSION = 1
FOLLOWER_START_TURN_VERSION = 2
STEER_TURN_VERSION = 1
MAX_FRAME_BYTES = 2 * 1024 * 1024

# The residual host limitation: the inner app-server turn request nested inside turnStart is a larger,
# version-sensitive schema than the read-only probe could confirm end to end. This is surfaced in
# every wake report and is the reason a live start/steer stays a reserved, guarded canary.
WAKE_SCHEMA_LIMITATION = (
    "turnStart.request is the app-server turn request (turnTrigger, cwd, approvalPolicy, permissions, "
    "model, effort, serviceTier, …); only the fields verified read-only from the installed build are "
    "set. The full nested schema is version-sensitive and unvalidated end to end, so a live "
    "start/steer must be a reserved human-authorized canary — the seam refuses to fire otherwise."
)


class WakeError(RuntimeError):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, **self.detail}


def encode_frame(obj: dict) -> bytes:
    """LE32 length-prefixed compact JSON — the exact framing the desktop IPC router uses."""
    body = json.dumps(obj, separators=(",", ":")).encode()
    return struct.pack("<I", len(body)) + body


def decode_frame(reader) -> dict:
    """Read one LE32-framed JSON object from a callable reader(n)->bytes (bounded)."""

    def take(n: int) -> bytes:
        out = b""
        while len(out) < n:
            part = reader(n - len(out))
            if not part:
                raise EOFError("connection closed")
            out += part
        return out

    n = struct.unpack("<I", take(4))[0]
    if n > MAX_FRAME_BYTES:
        raise ValueError("bounded frame size exceeded")
    return json.loads(take(n))


def text_input_items(text: str) -> list[dict]:
    """The verified turn-input item shape read from the installed build's ``startTurn`` call:
    a single user text item. Both a start turn's ``turnStart.request.input`` and a steer's ``input``
    are lists of these items."""
    return [{"type": "text", "text": text, "text_elements": []}]


def build_turn_start(text: str, *, client_user_message_id: str) -> dict:
    """Derived ``thread-follower-start-turn`` ``turnStart`` wrapper: ``{request, context}``. Only the
    fields verified read-only from the installed build are set (the input items and a client message
    id); ``context.responseItems`` is present because the app reads it. The inner ``request`` remains
    the version-sensitive surface named in ``WAKE_SCHEMA_LIMITATION`` — which is exactly why the seam
    will not fire this frame outside a reserved canary."""
    return {
        "request": {
            "clientUserMessageId": client_user_message_id,
            "input": text_input_items(text),
        },
        "context": {"responseItems": []},
    }


def _sha256_file(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def assert_owner_only_socket(path: str) -> None:
    """The socket and its parent must be owned by us, not a symlink, and not group/other-writable."""
    p = Path(path)
    for q in (p.parent, p):
        try:
            st = q.lstat()
        except OSError as e:
            raise WakeError("ipc_socket_absent", f"{q}: {e}")
        if st.st_uid != os.getuid() or stat.S_ISLNK(st.st_mode) or (st.st_mode & 0o022):
            raise WakeError("ipc_ownership_failed", f"{q} failed ownership/mode check")
    if not stat.S_ISSOCK(p.lstat().st_mode):
        raise WakeError("ipc_socket_absent", f"{p} is not a socket")


class CodexIpcWakeAdapter:
    def __init__(
        self,
        *,
        socket_path: str = DEFAULT_SOCKET_PATH,
        app_asar_path: str = DEFAULT_APP_ASAR_PATH,
        expected_app_asar_sha256: str = EXPECTED_APP_ASAR_SHA256,
        client_type: str = "sporedrive-wake-adapter",
    ):
        self.socket_path = socket_path
        self.app_asar_path = app_asar_path
        self.expected_app_asar_sha256 = expected_app_asar_sha256
        self.client_type = client_type

    # ------------------------------------------------------------------ build guard
    def verify_app_build(self) -> dict:
        """Compute the installed app.asar hash and compare it to the pinned one. A mismatch means the
        validated protocol may not apply — start/steer must refuse. Read-only; a report, never a gate
        that opens anything."""
        actual = _sha256_file(self.app_asar_path)
        return {
            "app_asar_path": self.app_asar_path,
            "expected_sha256": self.expected_app_asar_sha256,
            "actual_sha256": actual,
            "verified": bool(actual) and actual == self.expected_app_asar_sha256,
        }

    # ------------------------------------------------------------------ read-only probe
    def capability_probe(
        self, conversation_id: str, *, host_id: str = "local", timeout_s: float = 12.0
    ) -> dict:
        """Bounded, zero-model-turn, READ-ONLY probe: verify build, check socket ownership, connect,
        initialize, and run thread-owner-discovery. Sends NO start/steer and mutates nothing. Returns
        a structured capability report; a failure is reported, never raised past the bounded window."""
        build = self.verify_app_build()
        result: dict = {
            "socket": self.socket_path,
            "conversation_id": conversation_id,
            "read_only": True,
            "model_turns": 0,
            "app_build": build,
            "protocol_versions": {
                METHOD_OWNER_DISCOVERY: OWNER_DISCOVERY_VERSION,
                METHOD_START_TURN: FOLLOWER_START_TURN_VERSION,
                METHOD_STEER_TURN: STEER_TURN_VERSION,
            },
            "status": "unknown",
        }
        started = time.monotonic()
        deadline = started + max(1.0, float(timeout_s))
        s = None
        try:
            assert_owner_only_socket(self.socket_path)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect(self.socket_path)

            def _reader(n: int) -> bytes:
                s.settimeout(max(0.1, deadline - time.monotonic()))
                return s.recv(n)

            def request(method, params, client_id="", version=0):
                rid = "sporedrive-wake-" + uuid.uuid4().hex
                s.sendall(
                    encode_frame(
                        {
                            "type": "request",
                            "requestId": rid,
                            "sourceClientId": client_id,
                            "version": version,
                            "method": method,
                            "params": params,
                            "timeoutMs": 5000,
                        }
                    )
                )
                while time.monotonic() < deadline:
                    row = decode_frame(_reader)
                    if row.get("type") == "response" and row.get("requestId") == rid:
                        return row
                    # a router client-discovery ping: answer canHandle=False (we own nothing)
                    if row.get("type") == "client-discovery-request":
                        s.sendall(
                            encode_frame(
                                {
                                    "type": "client-discovery-response",
                                    "requestId": row.get("requestId"),
                                    "response": {"canHandle": False},
                                }
                            )
                        )
                raise TimeoutError("bounded probe deadline")

            init = request("initialize", {"clientType": self.client_type})
            result["initialize_result"] = init.get("resultType")
            if init.get("resultType") != "success":
                result["status"] = "initialize_refused"
                return result
            cid = init["result"]["clientId"]
            result["client_id"] = cid
            owner = request(
                METHOD_OWNER_DISCOVERY,
                {"hostId": host_id, "conversationId": conversation_id},
                cid,
                OWNER_DISCOVERY_VERSION,
            )
            result["owner"] = {
                k: owner.get(k)
                for k in (
                    "resultType",
                    "method",
                    "handledByClientId",
                    "result",
                    "error",
                )
            }
            ok = owner.get("resultType") == "success"
            result["owner_client_id"] = owner.get("handledByClientId") if ok else None
            result["supports_untrusted_app_input"] = bool(
                (owner.get("result") or {}).get("supportsUntrustedAppInput")
            )
            result["status"] = "owner_discovered" if ok else "owner_not_discovered"
        except Exception as e:
            result["status"] = "probe_failed"
            result["error"] = f"{type(e).__name__}: {e}"
        finally:
            if s is not None:
                s.close()
            result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return result

    # ------------------------------------------------------------------ start/steer SEAM (guarded)
    def build_start_turn_frame(
        self, conversation_id: str, turn_start: dict, owner_client_id: str
    ) -> dict:
        """Construct (do NOT send) the thread-follower-start-turn v2 request. ``targetClientId`` is a
        TOP-LEVEL frame field (the router's ``findClientForRequest`` reads ``t.targetClientId``), so it
        routes to the discovered owner. Kept separate from any socket so it is fully unit-testable
        offline."""
        return {
            "type": "request",
            "requestId": "sporedrive-wake-" + uuid.uuid4().hex,
            "sourceClientId": self.client_type,
            "targetClientId": owner_client_id,
            "version": FOLLOWER_START_TURN_VERSION,
            "method": METHOD_START_TURN,
            "params": {
                "conversationId": conversation_id,
                "turnStart": turn_start,
            },
            "timeoutMs": 5000,
        }

    def build_steer_turn_frame(
        self,
        conversation_id: str,
        steer_input: list,
        owner_client_id: str,
        *,
        client_user_message_id: str | None = None,
    ) -> dict:
        """Construct (do NOT send) the thread-follower-steer-turn v1 request, routed to the discovered
        owner. The content field is ``input`` (a list of turn-input items), matching the installed
        build's ``steerTurn(conversationId, input, …)`` dispatch."""
        return {
            "type": "request",
            "requestId": "sporedrive-wake-" + uuid.uuid4().hex,
            "sourceClientId": self.client_type,
            "targetClientId": owner_client_id,
            "version": STEER_TURN_VERSION,
            "method": METHOD_STEER_TURN,
            "params": {
                "conversationId": conversation_id,
                "input": steer_input,
                "clientUserMessageId": client_user_message_id or uuid.uuid4().hex,
            },
            "timeoutMs": 5000,
        }

    def start_turn(
        self,
        conversation_id: str,
        turn_start: dict,
        owner_client_id: str,
        *,
        armed: bool = False,
        dry_run: bool = True,
    ) -> dict:
        """The wake SEAM. It NEVER sends unless the caller both ARMS it (the ready/arm/yield handshake
        of a reserved acceptance turn) AND the installed app build matches the pinned hash. Offline and
        by default it refuses/dry-runs, so importing or exercising this module can never wake anyone."""
        return self._seam(
            self.build_start_turn_frame(conversation_id, turn_start, owner_client_id),
            armed=armed,
            dry_run=dry_run,
        )

    def steer_turn(
        self,
        conversation_id: str,
        steer_input: list,
        owner_client_id: str,
        *,
        armed: bool = False,
        dry_run: bool = True,
    ) -> dict:
        return self._seam(
            self.build_steer_turn_frame(conversation_id, steer_input, owner_client_id),
            armed=armed,
            dry_run=dry_run,
        )

    def _seam(self, frame: dict, *, armed: bool, dry_run: bool) -> dict:
        build = self.verify_app_build()
        if not build["verified"]:
            raise WakeError(
                "app_build_unverified",
                "installed app.asar does not match the validated protocol hash; refusing to send",
                app_build=build,
            )
        if not armed:
            # the default, offline path: construct but never send. Live start/steer is a reserved
            # acceptance turn set up by an explicit ready/arm/yield handshake.
            raise WakeError(
                "wake_not_armed",
                "start/steer is a guarded seam; arm it only inside a reserved acceptance turn",
                method=frame.get("method"),
            )
        if dry_run:
            return {"sent": False, "dry_run": True, "frame": frame}
        # ARMED + build-verified + not dry-run: this is the ONLY branch that touches the socket, and
        # it runs only inside a reserved live acceptance turn — never offline, never by default. An
        # absent/uncertain response is retained as sent-but-unconfirmed under the SAME request id; it
        # is NEVER upgraded to a confirmed delivery and NEVER auto-resent.
        assert_owner_only_socket(self.socket_path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(3.0)
            s.connect(self.socket_path)
            s.sendall(encode_frame(frame))
            deadline = time.monotonic() + 12.0

            def _reader(n: int) -> bytes:
                s.settimeout(max(0.1, deadline - time.monotonic()))
                return s.recv(n)

            while time.monotonic() < deadline:
                row = decode_frame(_reader)
                if (
                    row.get("type") == "response"
                    and row.get("requestId") == frame["requestId"]
                ):
                    return {
                        "sent": True,
                        "dry_run": False,
                        "request_id": frame["requestId"],
                        "response": row,
                    }
            return {
                "sent": True,
                "dry_run": False,
                "request_id": frame["requestId"],
                "response": None,
                "status": "uncertain",
            }
        finally:
            s.close()

    def wake_status(self) -> dict:
        """Wake is NOT established until a live start/steer is validated in a reserved acceptance turn.
        The read-only owner-discovery route existing does not by itself establish a delivered wake."""
        return {
            "route": "desktop_ipc",
            "status": "not_established",
            "socket": self.socket_path,
            "app_server_control_socket": "absent",
            "evidence": (
                "owner-only ipc.sock owner-discovery read-only ok; live start/steer pending "
                "a reserved acceptance turn (guarded seam, not fired)"
            ),
            "schema_limitation": WAKE_SCHEMA_LIMITATION,
            "app_build": self.verify_app_build(),
        }
