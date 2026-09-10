"""Argument-array runner around the installed cmux CLI.

Every call is an argv list handed to subprocess (no shell). Errors are classified from the CLI's
own messages so callers can distinguish access denial, a missing or stale socket, an unknown
target, an unreadable surface, and a timeout. A `FaultInjector` lets tests simulate transport
faults; every simulated fault is labelled `simulated=True` in the call log so real-runtime evidence
is never confused with a simulated one.

The socket password, if any, is read from the file named by `CMUX_BRIDGE_PASSWORD_FILE` (owner-only)
and passed to the CLI through the `CMUX_SOCKET_PASSWORD` environment variable, never on argv.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DEFAULT_CLI = "/Applications/cmux.app/Contents/Resources/bin/cmux"

ERROR_KINDS = (
    "access_denied",
    "socket_missing",
    "connection_refused",
    "not_found",
    "internal_error",
    "timeout",
    "cli_missing",
    "other",
)


class CmuxError(RuntimeError):
    def __init__(self, kind: str, message: str, result: "CmuxResult | None" = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.result = result


class SimulatedFault(CmuxError):
    """Raised by a FaultInjector. Always labelled simulated in the call log.

    `phase` is set by `CmuxCLI.run`: "before" = the command was never executed (zero bytes sent);
    "after" = the command ran and only its reply was lost. Callers must treat the two differently.
    """

    phase: str = "unknown"


@dataclass
class CmuxResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    error_kind: str | None = None
    simulated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.error_kind is None


def classify(returncode: int, stdout: str, stderr: str) -> str | None:
    """Classify a *failed* CLI invocation. rc == 0 is never an error (review item 1).

    The real cmux CLI (0.64.17) exits 1 and prints `Error: <kind>: <message>` on stderr for every
    failure. A successful command's stdout is arbitrary terminal text that may legitimately quote
    "Access denied", "internal_error" or "not_found" — classifying that as a transport failure
    turns a normal observation into a fake outage, so rc == 0 always returns None.
    """
    if returncode == 0:
        return None
    for text in (stderr, stdout):
        low = (text or "").lower()
        if "access denied" in low or "broken pipe" in low:
            return "access_denied"
        if "socket not found" in low:
            return "socket_missing"
        if "connection refused" in low:
            return "connection_refused"
        if "not_found" in low or "not found" in low:
            return "not_found"
        if "internal_error" in low:
            return "internal_error"
    return "other"


@dataclass
class FaultInjector:
    """Simulated transport faults for tests.

    `before(args)` may raise SimulatedFault to model a lost request before delivery.
    `after(args, result)` may raise SimulatedFault to model a lost response after delivery
    (the command DID run). `rewrite(args, result)` may return a replacement result (e.g. a
    fabricated access-denied reply or a changed boot_id).
    """

    before: Callable[[list[str]], None] | None = None
    after: Callable[[list[str], CmuxResult], None] | None = None
    rewrite: Callable[[list[str], CmuxResult], CmuxResult | None] | None = None


@dataclass
class CmuxCLI:
    cli_path: str = field(default_factory=lambda: os.environ.get("CMUX_BRIDGE_CLI", DEFAULT_CLI))
    socket: str | None = field(default_factory=lambda: os.environ.get("CMUX_BRIDGE_SOCKET"))
    timeout_s: float = 15.0
    fault: FaultInjector | None = None
    log: list[dict] = field(default_factory=list)

    def _env(self) -> dict:
        env = dict(os.environ)
        env["CMUX_QUIET"] = "1"
        pw_file = env.get("CMUX_BRIDGE_PASSWORD_FILE")
        if pw_file:
            try:
                p = Path(pw_file)
                if p.is_file() and (p.stat().st_mode & 0o077) == 0:
                    env["CMUX_SOCKET_PASSWORD"] = p.read_text().strip()
            except OSError:
                pass
        return env

    def run(self, *args: str, timeout: float | None = None) -> CmuxResult:
        argv = [self.cli_path]
        if self.socket:
            argv += ["--socket", self.socket]
        argv += list(args)
        t0 = time.monotonic()
        entry = {"args": list(args), "simulated": False}
        if self.fault and self.fault.before:
            try:
                self.fault.before(list(args))
            except SimulatedFault as e:
                e.phase = "before"  # the command was never executed: zero bytes reached cmux
                entry.update({"simulated": True, "fault": "before", "kind": e.kind})
                self.log.append(entry)
                raise
        res = self._execute(argv, list(args), timeout or self.timeout_s, t0)
        if self.fault and self.fault.rewrite:
            alt = self.fault.rewrite(list(args), res)
            if alt is not None:
                alt.simulated = True
                res = alt
        entry.update({"rc": res.returncode, "error_kind": res.error_kind, "duration_s": round(res.duration_s, 3), "simulated": res.simulated})
        self.log.append(entry)
        if self.fault and self.fault.after:
            try:
                self.fault.after(list(args), res)
            except SimulatedFault as e:
                e.phase = "after"  # the command DID run; only its reply was lost
                entry.update({"simulated": True, "fault": "after", "kind": e.kind})
                raise
        return res

    def _execute(self, argv: list[str], args: list[str], timeout: float, t0: float) -> CmuxResult:
        """Run the real CLI. Subclasses (test fakes) override this and nothing else."""
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=self._env())
            res = CmuxResult(args, p.returncode, p.stdout, p.stderr, time.monotonic() - t0)
            res.error_kind = classify(p.returncode, p.stdout, p.stderr)
            return res
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return CmuxResult(args, -1, out, "TIMEOUT", time.monotonic() - t0, "timeout")
        except FileNotFoundError:
            return CmuxResult(args, -1, "", f"cli not found: {self.cli_path}", 0.0, "cli_missing")

    def run_ok(self, *args: str, timeout: float | None = None) -> str:
        res = self.run(*args, timeout=timeout)
        if not res.ok:
            raise CmuxError(res.error_kind or "other", (res.stderr or res.stdout).strip()[:400], res)
        return res.stdout
