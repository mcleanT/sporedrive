#!/usr/bin/env python3
"""codex_launch.py — an owned-process launcher for codex_ask.sh's optional review deadline.

Why this exists (PLAN Section 3, "Owned Codex review processes"): a hard elapsed deadline for an
owned read-only reviewer must not be a shell `timeout` wrapper that leaves a detached child running.
The supervisor logic belongs in a small launcher, not embedded in shell. This launcher:

* runs the child in its OWN session/process group (start_new_session) so a deadline can terminate
  exactly that owned group and nothing else;
* on deadline, requests graceful termination (SIGTERM to the group), waits a bounded grace, then
  forces termination (SIGKILL to the group) only if still alive;
* preserves the raw merged stdout+stderr in the output file exactly as the inline path would, and
  appends a single explicit deadline marker line so the raw log tells the truth;
* returns exit code 124 on deadline — the SAME convention the GNU `timeout` command uses and the one
  codex_ask.sh's receipt writer already classifies as parse_status=timeout / complete=false — so the
  completeness contract is unchanged whether or not a partial final-message artifact exists;
* NEVER relaunches. Local process termination does not prove the remote inference stopped; that
  uncertainty is the caller's to hold, not something to paper over with a retry.

This is POSIX-only (macOS/Linux — the workflow's hosts); it does not claim an OS sandbox and does not
change codex's own arguments. Invoke as:

    cat prompt | codex_launch.py --deadline 900 --out OUTFILE [--grace 5] -- codex exec ... -

stdin is inherited by the child (so the piped prompt reaches codex unchanged); the child's stdout and
stderr are merged into OUTFILE. The launcher's own exit status is the child's, or 124 on deadline.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

EXIT_TIMEOUT = 124  # GNU `timeout` convention; codex_ask.sh receipt maps this to timeout/incomplete
EXIT_USAGE = 64
EXIT_NOEXEC = 126


def _parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="codex_launch.py", add_help=True, description=__doc__
    )
    ap.add_argument(
        "--deadline",
        type=float,
        required=True,
        help="hard elapsed deadline in seconds (>0); on expiry the owned group is terminated",
    )
    ap.add_argument(
        "--out", required=True, help="output file for merged child stdout+stderr"
    )
    ap.add_argument(
        "--grace",
        type=float,
        default=5.0,
        help="seconds to wait after SIGTERM before SIGKILL (default 5)",
    )
    ap.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="-- followed by the command to run (e.g. -- codex exec ... -)",
    )
    args = ap.parse_args(argv)
    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        ap.error("no command given after --")
    if args.deadline <= 0:
        ap.error("--deadline must be > 0")
    args.cmd = cmd
    return args


def _terminate_group(pgid: int, grace: float, out) -> None:
    """Graceful then bounded-forced termination of exactly the owned process group."""
    for sig, label in ((signal.SIGTERM, "SIGTERM"), (signal.SIGKILL, "SIGKILL")):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return  # group already gone
        except OSError as e:  # pragma: no cover - defensive
            out.write(f"\n[codex_launch] killpg {label} failed: {e}\n".encode())
            out.flush()
            return
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + max(0.0, grace)
            while time.monotonic() < deadline:
                try:
                    os.killpg(pgid, 0)  # liveness probe
                except OSError:
                    # ESRCH (group gone) or EPERM (all members are zombies / unsignalable) both mean
                    # there is nothing live left to escalate to SIGKILL — stop waiting.
                    return
                time.sleep(0.05)


def main(argv=None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        out = open(args.out, "wb")
    except OSError as e:
        sys.stderr.write(f"[codex_launch] cannot open output {args.out}: {e}\n")
        return EXIT_NOEXEC
    try:
        try:
            proc = subprocess.Popen(
                args.cmd,
                stdin=sys.stdin,  # inherit the piped prompt unchanged
                stdout=out,
                stderr=subprocess.STDOUT,  # merge into the same raw log the inline path produces
                start_new_session=True,  # own session/process group for scoped termination
            )
        except FileNotFoundError:
            out.write(f"[codex_launch] command not found: {args.cmd[0]}\n".encode())
            out.flush()
            return EXIT_NOEXEC
        except OSError as e:  # pragma: no cover - defensive
            out.write(f"[codex_launch] failed to launch {args.cmd[0]}: {e}\n".encode())
            out.flush()
            return EXIT_NOEXEC

        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid

        try:
            rc = proc.wait(timeout=args.deadline)
            return int(rc)
        except subprocess.TimeoutExpired:
            out.write(
                f"\n[codex_launch] deadline of {args.deadline:g}s exceeded; terminating owned "
                f"process group {pgid} (SIGTERM, then SIGKILL after {args.grace:g}s grace). "
                f"NOTE: local termination does not prove the remote inference stopped; this run is "
                f"reported timed_out/incomplete and is NOT retried automatically.\n".encode()
            )
            out.flush()
            _terminate_group(pgid, args.grace, out)
            try:
                proc.wait(timeout=max(2.0, args.grace))
            except subprocess.TimeoutExpired:  # pragma: no cover - group refused to die
                pass
            return EXIT_TIMEOUT
        except (
            KeyboardInterrupt
        ):  # pragma: no cover - propagate an interrupt as a scoped kill
            _terminate_group(pgid, args.grace, out)
            return 130
    finally:
        try:
            out.flush()
            out.close()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
