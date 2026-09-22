#!/usr/bin/env python3
"""Launch or verify a Claude Code EXECUTOR session in cmux with an explicit, policy-checked model.

Owner rule (2026-09-20): Fable is planning-only. Implementation runs on Opus by default — or on
Sonnet/Haiku when the operator names one — and never on a model silently inherited from
`~/.claude/settings.json` (whose default is Fable), never in the owner's focused planning session,
and never through the bare `clauded` alias (`claude --dangerously-skip-permissions`, no `--model`).

    executor_session.py launch --cwd <worktree> [--model opus|sonnet|haiku|<id>] [--purpose implementation|planning]
                               [--effort low|medium|high|xhigh|max]
                               [--name <title>] [--window <id|ref>] [--focus true|false] [--session-id <uuid>]
                               [--subagent-model sonnet] [--permission-mode <mode>] [--wait-s 45]
                               [--receipt <path>] [--dry-run]
    executor_session.py verify --surface <uuid> --pid <pid> [--session-id <uuid>] [--cwd <worktree>]
                               [--purpose implementation|planning] [--expected-model <m>]
    executor_session.py effort --surface <uuid> --pid <pid> --session-id <uuid> --cwd <dir> --to low|medium|high
                               --request-id <id> --authority policy|owner [--owner-ref <ref>] [--pin|--release-pin]
                               --reason <why> --checkpoint <id> --attest-drained [--expected-model <m>]
    executor_session.py policy [--purpose P] [--model M]

`launch` creates a DEDICATED workspace (identity by UUIDs, never by window or focus) whose initial
command is the absolute `claude` binary with `--model <resolved>` and a pre-generated `--session-id`,
so the executor's exact native session identity is known BEFORE it starts. An Opus implementation
launch that names no `--effort` is launched at `--effort medium` (Opus 5.5's own default, and it
thinks more per level than Opus 5); an explicit `--effort` always wins, and a non-Opus or planner
launch that names none passes no `--effort` at all. The receipt records the effort that was
REQUESTED on the command line — the launcher does not claim to have verified the running session's
effort. It exports
`CLAUDE_CODE_SUBAGENT_MODEL=<implementation model>` into that workspace so the executor's own
workers/subagents cannot inherit Fable either; then it waits (bounded) for the Claude prompt and
footer and reports the REQUESTED model next to the RESOLVED footer model. A trust dialog is
reported as `modal` and never auto-confirmed. A launch receipt (JSON) is written for the bridge /
coordination brief. Nothing is typed into the session.

`verify` is the reuse gate for an ALREADY RUNNING session: footer model (live), process argv
(requested at launch), transcript tail (what answered) — the same policy the bridge's
`bridge_bind(purpose=…)` applies. Exit 0 = ok to use for that purpose; 3 = refused.

`effort` applies ONE supervisor-named session-only effort level to a running executor via the native
`/effort` slider committed with `s` (never Enter, never `/effort <level>`, never settings files), and
keeps a per-session receipt chain beside the launch receipts (see the effort section below).
Effort exit codes: 0 applied/applied_ui/no_op | 3 refused | 5 unknown | 6 runtime_mismatch.

Exit codes: 0 ok | 1 cmux/transport failure | 2 usage or preflight | 3 model policy refusal |
4 launched but NOT verified (identity known; model/prompt not yet confirmed — verify before use).

Stdlib only; runs on the system python3 (3.9+). The policy block below is a verbatim mirror of
`bridge/cmux_bridge/model_policy.py` (test-enforced); edit it there first.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path

# --- model policy: begin ---
PURPOSES = ("implementation", "planning")
DEFAULT_PURPOSE = "implementation"
IMPLEMENTATION_FAMILIES = ("opus", "sonnet", "haiku")
PLANNING_ONLY_FAMILIES = ("fable",)
KNOWN_FAMILIES = PLANNING_ONLY_FAMILIES + IMPLEMENTATION_FAMILIES
DEFAULT_IMPLEMENTATION_MODEL = "opus"

# A family token bounded by non-letters, so 'claude-opus-5', 'opus', 'Opus 5', 'claude-fable-5-1[1m]'
# and 'Fable 5.1' all resolve, while unrelated words never do.
_FAMILY_RE = re.compile(r"(?<![A-Za-z])(fable|opus|sonnet|haiku)(?![A-Za-z])", re.IGNORECASE)
# The Claude Code status footer: '  Model: Opus 5 | Context: … Ctx Used: 11.6% | …'
FOOTER_MODEL_RE = re.compile(r"Model:\s*([^|]+?)\s*\|")
# `claude … --model opus …` or `--model=opus` on a process command line.
ARGV_MODEL_RE = re.compile(r"(?:^|\s)--model(?:=|\s+)(\S+)")


class ModelPolicyError(ValueError):
    """A refused model/purpose combination. `code` is stable and machine-readable."""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.detail}


def model_family(name) -> "str | None":
    """Family ('fable' | 'opus' | 'sonnet' | 'haiku') of a model spelled as a CLI alias, a model
    id, or the footer display name. None when the spelling names no known family."""
    if not name:
        return None
    m = _FAMILY_RE.search(str(name))
    return m.group(1).lower() if m else None


def footer_model(lines) -> "str | None":
    """The display name after 'Model:' on the LAST footer row of a screen read, or None."""
    for line in reversed(list(lines or [])):
        m = FOOTER_MODEL_RE.search(line)
        if m:
            return m.group(1).strip()
    return None


def argv_model(command_line) -> "str | None":
    """The value of `--model` on a process command line, or None when absent (inherited)."""
    if not command_line:
        return None
    m = ARGV_MODEL_RE.search(str(command_line))
    return m.group(1) if m else None


def transcript_model(entries) -> "str | None":
    """`message.model` of the LAST assistant entry in a transcript tail, or None."""
    for e in reversed(list(entries or [])):
        if not isinstance(e, dict) or e.get("type") != "assistant":
            continue
        msg = e.get("message")
        if isinstance(msg, dict) and msg.get("model"):
            return str(msg["model"])
    return None


def resolve_launch_model(purpose, requested) -> dict:
    """Decide the explicit `--model` for a NEW session, or raise ModelPolicyError.

    implementation: default 'opus'; any Opus/Sonnet/Haiku spelling is accepted as given; Fable and
    unknown spellings are refused (fail closed — nothing is ever launched on an inherited default).
    planning: the model must be named explicitly (no silent default); any known family is allowed.
    """
    if purpose not in PURPOSES:
        raise ModelPolicyError(
            "bad_purpose", f"purpose must be one of {list(PURPOSES)}", purpose=purpose
        )
    if purpose == "implementation":
        model = requested or DEFAULT_IMPLEMENTATION_MODEL
        fam = model_family(model)
        if fam in PLANNING_ONLY_FAMILIES:
            raise ModelPolicyError(
                "model_policy",
                f"{model!r} is planning-only; implementation runs on Opus (default), Sonnet or Haiku",
                purpose=purpose,
                requested=model,
                family=fam,
                allowed=list(IMPLEMENTATION_FAMILIES),
            )
        if fam not in IMPLEMENTATION_FAMILIES:
            raise ModelPolicyError(
                "model_unknown",
                f"{model!r} names no supported implementation model family",
                purpose=purpose,
                requested=model,
                allowed=list(IMPLEMENTATION_FAMILIES),
            )
        return {
            "purpose": purpose,
            "model": model,
            "family": fam,
            "defaulted": requested is None or requested == "",
        }
    if not requested:
        raise ModelPolicyError(
            "model_required",
            "a planning session names its model explicitly; there is no silent default",
            purpose=purpose,
        )
    fam = model_family(requested)
    if fam not in KNOWN_FAMILIES:
        raise ModelPolicyError(
            "model_unknown",
            f"{requested!r} names no known model family",
            purpose=purpose,
            requested=requested,
            allowed=list(KNOWN_FAMILIES),
        )
    return {"purpose": purpose, "model": requested, "family": fam, "defaulted": False}


def check_session_model(
    purpose,
    footer=None,
    argv=None,
    transcript=None,
    expected=None,
) -> dict:
    """Verdict on REUSING an existing session for `purpose`, from up to three evidence sources
    (display names / ids / aliases; None = unavailable). Never raises; returns
    {"ok": bool, "code": str|None, "family": str|None, "reason": str, "evidence": {...}}.

    The footer is the gate (it is the live model). A Fable footer is `model_policy`; a Fable argv or
    transcript under a non-Fable footer is `planning_session` (the session was started or ran as
    the planning session and is being reused). An unreadable footer is `model_unverified`, never a
    pass. `expected` (what the caller asked for) must match the footer family: `model_mismatch`.
    For purpose=planning the footer must still be readable, and any known family passes.
    """
    ev = {
        "footer": footer,
        "footer_family": model_family(footer),
        "argv": argv,
        "argv_family": model_family(argv),
        "transcript": transcript,
        "transcript_family": model_family(transcript),
        "expected": expected,
        "expected_family": model_family(expected) if expected else None,
    }

    def verdict(ok, code, reason):
        return {
            "ok": ok,
            "code": code,
            "family": ev["footer_family"],
            "purpose": purpose,
            "reason": reason,
            "evidence": ev,
        }

    if purpose not in PURPOSES:
        return verdict(False, "bad_purpose", f"purpose must be one of {list(PURPOSES)}")
    if ev["footer_family"] is None:
        return verdict(
            False,
            "model_unverified",
            "the session's model could not be read from its footer; nothing is assumed",
        )
    if expected and ev["expected_family"] != ev["footer_family"]:
        return verdict(
            False,
            "model_mismatch",
            f"footer model {footer!r} is not the requested {expected!r}",
        )
    if purpose == "planning":
        return verdict(True, None, f"planning session on {footer!r}")
    if ev["footer_family"] in PLANNING_ONLY_FAMILIES:
        return verdict(
            False,
            "model_policy",
            f"the session is on {footer!r}, which is planning-only; implementation needs Opus, Sonnet or Haiku",
        )
    if ev["footer_family"] not in IMPLEMENTATION_FAMILIES:
        return verdict(False, "model_policy", f"{footer!r} is not an implementation model")
    for src in ("argv", "transcript"):
        if ev[f"{src}_family"] in PLANNING_ONLY_FAMILIES:
            return verdict(
                False,
                "planning_session",
                f"the session's {src} shows {ev[src]!r}: it was started or ran as a Fable planning session and is not reused for implementation",
            )
    return verdict(True, None, f"implementation session on {footer!r}")


# --- model policy: end ---

EXIT_OK, EXIT_CMUX, EXIT_USAGE, EXIT_POLICY, EXIT_UNVERIFIED = 0, 1, 2, 3, 4
DEFAULT_CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"
DEFAULT_SUBAGENT_MODEL = "sonnet"

# --- launch effort (launcher-only; the bridge's purpose/model gate does not cover effort) ---
# `claude --effort <level>` on the installed CLI. Opus 5.5 defaults to medium and thinks more per
# level than Opus 5, so an Opus IMPLEMENTATION launch that names no effort is launched at medium
# rather than inheriting whatever the session default happens to be. An explicit --effort always
# wins. Other families (and the Fable planner) keep their own defaults: nothing is passed unless
# the operator asked for it.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_OPUS_IMPLEMENTATION_EFFORT = "medium"


def resolve_launch_effort(purpose, family, requested):
    """Decide the `--effort` for a NEW session, or raise ModelPolicyError on an unsupported value.

    Returns {"effort": <level|None>, "defaulted": <bool>, "source": "requested"|"opus_default"|"unset"}.
    `effort is None` means: pass no --effort and leave the session's own default alone.
    """
    if requested is not None and str(requested) != "":
        eff = str(requested).strip().lower()
        if eff not in EFFORTS:
            raise ModelPolicyError(
                "bad_effort",
                "effort must be one of %s" % list(EFFORTS),
                effort=requested,
            )
        return {"effort": eff, "defaulted": False, "source": "requested"}
    if purpose == "implementation" and family == "opus":
        return {
            "effort": DEFAULT_OPUS_IMPLEMENTATION_EFFORT,
            "defaulted": True,
            "source": "opus_default",
        }
    return {"effort": None, "defaulted": True, "source": "unset"}

UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
WORKSPACE_REF_RE = re.compile(r"workspace:(\d+)")
# Claude's input line is "❯" + NBSP at column 0; a dialog cursor is " ❯ option" (leading plain
# space) and must NOT count as a prompt. Same shape as the bridge's PROMPT_RE.
PROMPT_RE = re.compile("^❯(?:\u00a0(.*))?$")
MODAL_HINTS = (
    "Do you want to proceed",
    "Is this a project you created or one you trust",
    "Yes, I trust this folder",
    "Do you trust the files in this folder",
    "Yes, and don't ask again",
    "Enter to confirm",
    "esc to cancel",
    "Esc to cancel",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _emit(obj: dict, as_json: bool = True) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True))
    else:
        for k, v in obj.items():
            print(f"{k}: {v}")


def _fail(code: int, kind: str, message: str, **detail) -> int:
    _emit({"ok": False, "error": kind, "message": message, **detail})
    return code


def transcript_path(session_id: str, cwd: str) -> Path:
    root = Path(os.environ.get("CMUX_BRIDGE_CLAUDE_PROJECTS") or (Path.home() / ".claude/projects"))
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
    return root / slug / f"{str(session_id).removeprefix('claude-')}.jsonl"


def read_transcript_tail(p: Path, tail_bytes: int = 2_000_000) -> list:
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()
            raw = f.read()
    except OSError:
        return []
    out = []
    for line in raw.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def classify(lines: list) -> dict:
    """Minimal screen classification: modal / prompt / footer-only / not_claude. The launcher never
    types, so this only decides whether the executor is READY (prompt + footer) or needs a human
    (modal) or more time. It mirrors the bridge's classify_screen conservatively."""
    tail = [l for l in lines[-40:]]
    model = footer_model(tail)
    prompt = any(PROMPT_RE.match(l) for l in tail)
    region = [l for l in tail if l.strip()][-12:]
    # A dialog replaces the input box, so its hints sit in the last rows; the folder-trust dialog
    # shows its own " ❯ No, exit" cursor (default = exit!), which is not an input prompt.
    if not prompt and any(h in l for l in region for h in MODAL_HINTS):
        return {"state": "modal", "model": model}
    if model and prompt:
        return {"state": "prompt", "model": model}
    if model:
        return {"state": "footer_only", "model": model}
    return {"state": "not_claude", "model": None}


class Cmux:
    """Thin argv runner around the installed cmux CLI (no shell). The socket password, if any, comes
    from CMUX_SOCKET_PASSWORD or the owner-only file named by CMUX_BRIDGE_PASSWORD_FILE — never argv."""

    def __init__(self, cli: str, timeout_s: float = 15.0):
        self.cli = cli
        self.timeout_s = timeout_s
        self.calls: list = []

    def _env(self) -> dict:
        env = dict(os.environ)
        env["CMUX_QUIET"] = "1"
        pw_file = env.get("CMUX_BRIDGE_PASSWORD_FILE")
        if pw_file and not env.get("CMUX_SOCKET_PASSWORD"):
            try:
                env["CMUX_SOCKET_PASSWORD"] = Path(pw_file).read_text().strip()
            except OSError:
                pass
        return env

    def run(self, *args: str, timeout: "float | None" = None) -> subprocess.CompletedProcess:
        argv = [self.cli, *args]
        t0 = time.monotonic()
        try:
            r = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout or self.timeout_s, env=self._env()
            )
        except subprocess.TimeoutExpired:
            r = subprocess.CompletedProcess(argv, 124, "", "timeout")
        except OSError as e:
            r = subprocess.CompletedProcess(argv, 127, "", f"cli_missing: {e}")
        self.calls.append({"args": args, "rc": r.returncode, "s": round(time.monotonic() - t0, 3)})
        return r

    def ok(self, *args: str, timeout: "float | None" = None) -> str:
        r = self.run(*args, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"cmux {' '.join(args[:2])}: rc={r.returncode} {(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout

    def json(self, *args: str, timeout: "float | None" = None):
        out = self.ok(*args, timeout=timeout)
        try:
            return json.loads(out)
        except ValueError:
            raise RuntimeError(f"cmux {' '.join(args[:2])}: non-JSON output {out.strip()[:200]!r}")


def resolve_claude_bin(explicit: "str | None") -> "str | None":
    # The path is used as found (not realpath'd): the versioned target's basename is a version
    # number, and `ps` must keep showing a `claude` argv[0] for pid_for_session / verify.
    for cand in (explicit, os.environ.get("CLAUDE_BIN"), shutil.which("claude"), str(Path.home() / ".local/bin/claude")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def workspace_uuid_for_ref(cm: Cmux, ref: str) -> "str | None":
    tree = cm.json("tree", "--all", "--json")
    for w in tree.get("windows", []) or []:
        for ws in w.get("workspaces", []) or []:
            if ws.get("ref") == ref and ws.get("id"):
                return str(ws["id"]).upper()
    return None


def surfaces_of(cm: Cmux, workspace_uuid: str) -> list:
    d = cm.json("list-pane-surfaces", "--workspace", workspace_uuid, "--id-format", "both", "--json")
    return [
        {"uuid": str(s.get("id")).upper(), "ref": s.get("ref"), "type": s.get("type"), "title": s.get("title")}
        for s in (d.get("surfaces") or [])
        if s.get("id")
    ]


def read_screen(cm: Cmux, surface_uuid: str, lines: int = 20) -> list:
    r = cm.run("read-screen", "--surface", surface_uuid, "--lines", str(lines))
    if r.returncode != 0:
        return []
    return r.stdout.splitlines()


def pid_for_session(session_id: str) -> "int | None":
    """The claude process whose own argv carries `--session-id <our uuid>`: an exact identity."""
    try:
        out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    needle = f"--session-id {session_id}"
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or needle not in line:
            continue
        pid, first = parts[0], parts[1]
        # argv[0] must be the claude binary: this excludes the launcher's own `python3 … --session-id`
        if "claude" not in os.path.basename(first):
            continue
        try:
            return int(pid)
        except ValueError:
            continue
    return None


def proc_argv(pid: int) -> "str | None":
    try:
        out = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    return out.strip() or None


def proc_cwd(pid: int) -> "str | None":
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(int(pid)), "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    for l in out.splitlines():
        if l.startswith("n/"):
            return l[1:]
    return None


def proc_cmux_surface(pid: int) -> "str | None":
    """CMUX_SURFACE_ID from the process's own environment (cmux sets it for the terminal it runs
    in): the native surface↔process binding. None when unreadable."""
    try:
        out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    m = re.search(r"(?:^|\s)CMUX_SURFACE_ID=([0-9A-Fa-f-]{36})(?:\s|$)", out)
    return m.group(1).upper() if m else None


def effort_identity(pid: int, session_id: str, surface: str, cwd: str, lines, purpose, expected) -> tuple:
    """(ok, code, detail): the surface we would type into must be THE terminal of the process
    whose argv carries --session-id <session_id> and whose cwd is `cwd`. Unknown = refused."""
    argv = proc_argv(pid)
    env_surface = proc_cmux_surface(pid)
    pcwd = proc_cwd(pid)
    detail = {"pid": pid, "argv_present": argv is not None, "process_surface": env_surface, "process_cwd": pcwd}
    if not argv or f"--session-id {session_id}" not in argv or "claude" not in os.path.basename(argv.split()[0]):
        return False, "foreign_session", detail
    if env_surface is None:
        return False, "surface_unbound", detail
    if env_surface != surface.upper():
        return False, "surface_mismatch", detail
    if pcwd is None or os.path.realpath(pcwd) != os.path.realpath(cwd):
        return False, "cwd_mismatch", detail
    v = check_session_model(purpose, footer=footer_model(lines), argv=argv_model(argv), expected=expected)
    detail.update({"footer_model": footer_model(lines), "verdict": v["reason"]})
    return v["ok"], v["code"], detail


def default_receipt_dir() -> Path:
    root = Path(os.environ.get("CMUX_BRIDGE_STATE_DIR") or (Path.home() / ".local/state/codex-claude-bridge"))
    return root / "launches"


# ------------------------------------------------------------------------------------------ launch


def cmd_launch(a: argparse.Namespace) -> int:
    try:
        pol = resolve_launch_model(a.purpose, a.model)
        sub = resolve_launch_model("implementation", a.subagent_model)
        eff = resolve_launch_effort(pol["purpose"], pol["family"], a.effort)
    except ModelPolicyError as e:
        return _fail(EXIT_POLICY, e.code, e.message, **e.detail, launched=False)
    cwd = os.path.realpath(a.cwd)
    if not os.path.isdir(cwd):
        return _fail(EXIT_USAGE, "bad_cwd", f"--cwd is not a directory: {a.cwd}", launched=False)
    session_id = (a.session_id or str(_uuid.uuid4())).lower()
    if not UUID_RE.match(session_id):
        return _fail(EXIT_USAGE, "bad_session_id", "--session-id must be a UUID", launched=False)
    tp = transcript_path(session_id, cwd)
    if tp.exists():
        return _fail(
            EXIT_USAGE,
            "session_exists",
            "a transcript for that session id already exists in this worktree; never start a second process on an existing conversation",
            transcript=str(tp),
            launched=False,
        )
    claude_bin = resolve_claude_bin(a.claude_bin)
    if not claude_bin:
        return _fail(EXIT_USAGE, "claude_missing", "claude binary not found (pass --claude-bin or set CLAUDE_BIN)", launched=False)
    if a.focus not in ("true", "false"):
        return _fail(EXIT_USAGE, "bad_focus", "--focus must be true or false", launched=False)

    argv = [claude_bin]
    if a.permission_mode:
        argv += ["--permission-mode", a.permission_mode]
    else:
        argv.append("--dangerously-skip-permissions")  # what the owner's `clauded` alias does
    argv += ["--model", pol["model"], "--session-id", session_id]
    if eff["effort"]:
        argv += ["--effort", eff["effort"]]
    command = shlex.join(argv)
    name = a.name or f"executor · {os.path.basename(cwd)} · {pol['model']}"
    env_pairs = [
        f"CLAUDE_CODE_SUBAGENT_MODEL={sub['model']}",
        f"SPOREDRIVE_SESSION_PURPOSE={pol['purpose']}",
        f"SPOREDRIVE_REQUESTED_MODEL={pol['model']}",
    ]
    ws_args = ["new-workspace", "--name", name, "--cwd", cwd, "--command", command, "--focus", a.focus, "--json"]
    if a.window:
        ws_args += ["--window", a.window]
    for kv in env_pairs:
        ws_args += ["--env", kv]

    receipt = {
        "kind": "executor_launch",
        "at": _now(),
        "requested": {
            "purpose": pol["purpose"],
            "model": pol["model"],
            "family": pol["family"],
            "model_defaulted": pol["defaulted"],
            "effort": eff["effort"],
            "effort_defaulted": eff["defaulted"],
            "effort_source": eff["source"],
            "subagent_model": sub["model"],
            "permission_mode": a.permission_mode or "bypass (--dangerously-skip-permissions)",
        },
        "launch": {
            "claude_bin": claude_bin,
            "command": command,
            "cwd": cwd,
            "name": name,
            "window": a.window,
            "focus": a.focus,
            "workspace_env": env_pairs,
        },
        "identity": {
            "session_id": session_id,
            "transcript_path": str(tp),
            "workspace_uuid": None,
            "surface_uuid": None,
            "pid": None,
        },
        "resolved": {"state": None, "footer_model": None, "footer_family": None, "model_verified": False},
        "launched": False,
        "ok": False,
    }
    if a.dry_run:
        receipt["dry_run"] = True
        receipt["cmux_args"] = ws_args
        receipt["ok"] = True
        _emit(receipt)
        return EXIT_OK

    cm = Cmux(a.cmux)
    try:
        ping = cm.run("ping", timeout=10)
        if ping.returncode != 0:
            return _fail(EXIT_CMUX, "transport", f"cmux ping failed: {(ping.stderr or ping.stdout).strip()[:300]}", launched=False)
        raw = cm.ok(*ws_args)
    except RuntimeError as e:
        return _fail(EXIT_CMUX, "transport", str(e), launched=False)
    receipt["launched"] = True
    receipt["cmux_raw"] = raw.strip()[:400]
    ref = None
    try:
        j = json.loads(raw)
        ref = j.get("workspace_ref") or j.get("ref")
        if j.get("workspace_id") or j.get("id"):
            receipt["identity"]["workspace_uuid"] = str(j.get("workspace_id") or j.get("id")).upper()
    except ValueError:
        m = WORKSPACE_REF_RE.search(raw)
        ref = m.group(0) if m else None
    receipt["identity"]["workspace_ref"] = ref
    try:
        if not receipt["identity"]["workspace_uuid"] and ref:
            receipt["identity"]["workspace_uuid"] = workspace_uuid_for_ref(cm, ref)
        ws = receipt["identity"]["workspace_uuid"]
        if not ws:
            raise RuntimeError(f"could not resolve the new workspace's UUID from {raw.strip()[:120]!r}")
        surfs = surfaces_of(cm, ws)
        terms = [s for s in surfs if s.get("type") == "terminal"]
        if len(terms) != 1:
            raise RuntimeError(f"expected exactly one terminal surface in the new workspace, found {len(terms)}")
        receipt["identity"]["surface_uuid"] = terms[0]["uuid"]
        receipt["identity"]["surface_ref"] = terms[0]["ref"]
        try:
            receipt["launch"]["workspace_env_applied"] = cm.ok("workspace", "env", ws, "--mask").strip()[:400]
        except RuntimeError as e:
            receipt["launch"]["workspace_env_applied"] = f"unverified: {e}"
    except RuntimeError as e:
        receipt["error"] = str(e)
        _write_receipt(a, receipt)
        _emit(receipt)
        return EXIT_UNVERIFIED

    # bounded wait for the executor's prompt + footer; never type into it
    deadline = time.monotonic() + max(5.0, float(a.wait_s))
    state, model, last = "unknown", None, []
    while time.monotonic() < deadline:
        last = read_screen(cm, receipt["identity"]["surface_uuid"], 20)
        c = classify(last)
        state, model = c["state"], c["model"]
        if state in ("prompt", "modal"):
            break
        time.sleep(1.5)
    receipt["resolved"]["state"] = state
    receipt["resolved"]["footer_model"] = model
    receipt["resolved"]["footer_family"] = model_family(model)
    receipt["resolved"]["screen_tail"] = [l[:160] for l in last[-6:]]
    pid = pid_for_session(session_id)
    receipt["identity"]["pid"] = pid
    receipt["identity"]["pid_argv"] = proc_argv(pid) if pid else None
    receipt["identity"]["pid_argv_model"] = argv_model(receipt["identity"]["pid_argv"])
    verified = bool(model) and model_family(model) == pol["family"] and state == "prompt"
    receipt["resolved"]["model_verified"] = verified
    if model and model_family(model) != pol["family"]:
        receipt["resolved"]["note"] = f"footer shows {model!r} but {pol['model']!r} was requested: do NOT use this session; report it"
    elif state == "modal":
        receipt["resolved"]["note"] = "a dialog (folder trust?) is showing; a human confirms it, then re-run `verify`"
    elif state != "prompt":
        receipt["resolved"]["note"] = "no Claude prompt+footer yet; re-run `verify` before binding"
    receipt["ok"] = verified
    _write_receipt(a, receipt)
    _emit(receipt)
    return EXIT_OK if verified else EXIT_UNVERIFIED


def _write_receipt(a: argparse.Namespace, receipt: dict) -> None:
    path = Path(a.receipt) if a.receipt else default_receipt_dir() / f"{receipt['identity']['session_id']}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        receipt["receipt_path"] = str(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        receipt["receipt_path"] = None
        receipt["receipt_error"] = str(e)


# ------------------------------------------------------------------------------------------ verify


def cmd_verify(a: argparse.Namespace) -> int:
    if a.purpose not in PURPOSES:
        return _fail(EXIT_USAGE, "bad_purpose", f"--purpose must be one of {list(PURPOSES)}")
    if not UUID_RE.match(a.surface or ""):
        return _fail(EXIT_USAGE, "bad_surface", "--surface must be a surface UUID (refs like surface:N are not identities)")
    cm = Cmux(a.cmux)
    lines = read_screen(cm, a.surface.upper(), 20)
    c = classify(lines)
    argv = proc_argv(a.pid)
    cwd = a.cwd or proc_cwd(a.pid)
    sid = a.session_id or (re.search(r"--session-id\s+(\S+)", argv or "") or [None, None])[1]
    tr = None
    tp = None
    if sid and cwd:
        tp = transcript_path(sid, cwd)
        tr = transcript_model(read_transcript_tail(tp))
    v = check_session_model(a.purpose, footer=c["model"], argv=argv_model(argv), transcript=tr, expected=a.expected_model)
    out = {
        "kind": "executor_verify",
        "at": _now(),
        "ok": v["ok"],
        "code": v["code"],
        "reason": v["reason"],
        "purpose": a.purpose,
        "family": v["family"],
        "screen_state": c["state"],
        "identity": {"surface_uuid": a.surface.upper(), "pid": a.pid, "session_id": sid, "cwd": cwd, "transcript_path": str(tp) if tp else None},
        "evidence": {**v["evidence"], "argv_raw": argv, "argv_present": argv is not None, "screen_tail": [l[:160] for l in lines[-4:]]},
    }
    _emit(out)
    if v["ok"]:
        return EXIT_OK
    return EXIT_UNVERIFIED if v["code"] == "model_unverified" else EXIT_POLICY


def cmd_policy(a: argparse.Namespace) -> int:
    try:
        r = resolve_launch_model(a.purpose, a.model)
    except ModelPolicyError as e:
        _emit({"ok": False, **e.to_dict()})
        return EXIT_POLICY
    _emit({"ok": True, **r})
    return EXIT_OK


# ------------------------------------------------------------------------------------------ effort
# Session-only effort switch for an ALREADY RUNNING executor (owner-approved adaptive effort,
# 2026-09-22). The supervisor decides the level at a checkpoint; this applies and verifies it.
# Native path observed on v2.1.280: bare `/effort` + Enter opens a slider
# ("low medium high xhigh max ultracode", marker ▲, hint "… s for this session only …"); ←/→ move
# one level; `s` commits for THIS SESSION ONLY. Enter on the slider and `/effort <level>` save a
# persistent default and are never used. UI success is `applied_ui`; only the first assistant
# transcript record after the commit whose `effort` AND `perTurnEffort` equal the target makes it
# `applied`. The helper never spends a model call and never resends an unknown request.
POLICY_EFFORTS = ("low", "medium", "high")
EFFORT_MIN_VERSION = (2, 1, 257)
FOOTER_EFFORT_RE = re.compile(r"Thinking:\s*([A-Za-z]+)")
FOOTER_VERSION_RE = re.compile(r"\bv(\d+)\.(\d+)\.(\d+)\b")
SLIDER_LABELS = ("low", "medium", "high", "xhigh", "max", "ultracode")
SLIDER_HINT = "s for this session only"
EXIT_REFUSED, EXIT_UNKNOWN, EXIT_MISMATCH = 3, 5, 6
SETTINGS_FILES = ("~/.claude/settings.json", "~/.claude/settings.local.json")


def footer_effort(lines) -> "str | None":
    for line in reversed(list(lines or [])):
        m = FOOTER_EFFORT_RE.search(line)
        if m:
            return m.group(1).lower()
    return None


def footer_version(lines) -> "tuple | None":
    for line in reversed(list(lines or [])):
        if "Model:" in line:
            m = FOOTER_VERSION_RE.search(line)
            if m:
                return tuple(int(x) for x in m.groups())
    return None


def prompt_text(lines) -> "str | None":
    """Text on Claude's input line ('' = empty), or None when no input line is visible."""
    for line in reversed(list(lines or [])):
        m = PROMPT_RE.match(line)
        if m:
            return (m.group(1) or "").strip()
    return None


def slider_marker(lines) -> "dict | None":
    """{'at': label under the ▲ marker, 'hint_ok': bool} or None when no slider is showing."""
    label_i = next((i for i, l in enumerate(lines) if all(f" {w} " in f" {l} " for w in SLIDER_LABELS[:5])), None)
    if label_i is None:
        return None
    marker_line = next((lines[i] for i in range(label_i - 1, -1, -1) if "▲" in lines[i]), None)
    hint_ok = any(SLIDER_HINT in l for l in lines)
    if marker_line is None:
        return {"at": None, "hint_ok": hint_ok}
    col = marker_line.index("▲")
    label_line = lines[label_i]
    best, dist = None, None
    for w in SLIDER_LABELS:
        m = re.search(rf"(?<![A-Za-z]){w}(?![A-Za-z])", label_line)
        if not m:
            continue
        d = abs((m.start() + m.end() - 1) / 2 - col)
        if dist is None or d < dist:
            best, dist = w, d
    return {"at": best, "hint_ok": hint_ok}


def settings_hashes() -> dict:
    import hashlib

    out = {}
    for f in SETTINGS_FILES:
        p = Path(os.path.expanduser(f))
        try:
            out[f] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            out[f] = None
    return out


def first_runtime_effort(transcript: Path, session_id: str, after_iso: str) -> "dict | None":
    """The FIRST assistant record of `session_id` timestamped after `after_iso`, or None."""
    after = datetime.fromisoformat(after_iso.replace("Z", "+00:00"))
    for e in read_transcript_tail(transcript):
        if e.get("type") != "assistant" or str(e.get("sessionId") or e.get("session_id") or session_id) != session_id:
            continue
        try:
            ts = datetime.fromisoformat(str(e.get("timestamp")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts > after:
            return {"timestamp": e.get("timestamp"), "effort": e.get("effort"), "perTurnEffort": e.get("perTurnEffort"), "uuid": e.get("uuid")}
    return None


class CmuxKeys:
    """The three UI primitives the effort switch needs, over the cmux CLI (tests inject a fake)."""

    def __init__(self, cm: Cmux, surface: str):
        self.cm, self.surface = cm, surface

    def screen(self) -> list:
        return read_screen(self.cm, self.surface, 30)

    def text(self, s: str) -> None:
        self.cm.ok("send", "--surface", self.surface, s)

    def key(self, k: str) -> None:
        self.cm.ok("send-key", "--surface", self.surface, k)


def effort_dir() -> Path:
    return default_receipt_dir() / "effort"


def _chain_path(session_id: str) -> Path:
    return effort_dir() / f"{session_id}.jsonl"


class ChainDamaged(Exception):
    """The receipt chain exists but cannot be read completely: never treated as empty history."""


def _read_chain(session_id: str) -> list:
    p = _chain_path(session_id)
    if not p.exists():
        return []
    try:
        raw = p.read_text()
    except OSError as e:
        raise ChainDamaged(f"unreadable: {e}")
    out = []
    for i, l in enumerate(raw.splitlines(), 1):
        if not l.strip():
            continue
        try:
            out.append(json.loads(l))
        except ValueError:
            raise ChainDamaged(f"malformed line {i}")
    if raw and not raw.endswith("\n"):
        raise ChainDamaged("torn final line")
    return out


def _append_chain(rec: dict) -> None:
    p = _chain_path(rec["session_id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def pin_state(chain: list) -> "dict | None":
    """The retained owner pin (the last owner record that pinned or released), or None."""
    pin = None
    for r in chain:
        if r.get("authority") == "owner" and r.get("pin_action") == "pin" and r.get("outcome") in ("applied", "applied_ui", "no_op"):
            pin = {"level": r["requested"], "owner_ref": r.get("owner_ref"), "request_id": r["request_id"]}
        elif r.get("authority") == "owner" and r.get("pin_action") == "release" and r.get("outcome") in ("applied", "applied_ui", "no_op"):
            pin = None
    return pin


class SurfaceLock:
    """One O_EXCL lock per surface; a lock whose holder pid is dead is stale and replaced."""

    def __init__(self, surface: str):
        self.path = effort_dir() / f"{surface}.lock"
        self.held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The pid is written to a private temp file first and then hard-linked into place, so a
        # visible lock always carries its complete holder pid. An empty/unparsable lock is NOT
        # proof its holder died: it is treated as held and left for a human.
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{_uuid.uuid4().hex[:8]}")
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, str(os.getpid()).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            return self._acquire_linked(tmp)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _acquire_linked(self, tmp: Path) -> bool:
        for _ in range(2):
            try:
                os.link(tmp, self.path)
                self.held = True
                return True
            except FileExistsError:
                try:
                    holder = int(self.path.read_text().strip())
                except (OSError, ValueError):
                    return False
                alive = False
                if holder:
                    try:
                        os.kill(holder, 0)
                        alive = True
                    except ProcessLookupError:
                        alive = False
                    except PermissionError:
                        alive = True
                if alive:
                    return False
                try:
                    self.path.unlink()
                except OSError:
                    return False
        return False

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False


def _request_hash(req: dict) -> str:
    import hashlib

    keys = ("session_id", "surface_uuid", "requested", "authority", "owner_ref", "pin_action", "reason", "checkpoint")
    return hashlib.sha256(json.dumps({k: req.get(k) for k in keys}, sort_keys=True).encode()).hexdigest()


def _reconcile(req: dict, prior: dict, keys, transcript: "Path | None") -> dict:
    """Read-only follow-up of an earlier request: never types anything."""
    rec = {**prior, "at": _now(), "phase": "reconcile", "reconciles": prior.get("at"), "keys_sent": []}
    lines = keys.screen()
    rec["observed_ui"] = footer_effort(lines)
    if prior.get("outcome") in ("applied_ui",) and transcript and prior.get("committed_at"):
        rt = first_runtime_effort(transcript, req["session_id"], prior["committed_at"])
        rec["observed_runtime"] = rt
        if rt is None:
            rec["outcome"] = "applied_ui"
            rec["note"] = "runtime pending: no assistant record after the commit yet"
        elif rt.get("effort") == req["requested"] and rt.get("perTurnEffort") == req["requested"]:
            rec["outcome"] = "applied"
        else:
            rec["outcome"] = "runtime_mismatch"
    elif prior.get("outcome") in ("unknown", "pending"):
        rec["outcome"] = "unknown"
        rec["note"] = f"{prior.get('outcome')} outcome retained; observed state recorded, nothing resent"
    return rec


def apply_effort(req: dict, keys, identity_check, transcript: "Path | None", pause: float = 0.8) -> dict:
    """Apply one supervisor-named session-only effort decision. Returns the receipt record
    (also appended to the per-session chain). `identity_check(lines)` -> (ok, code, detail)."""
    req["request_hash"] = _request_hash(req)
    base = {
        "kind": "executor_effort",
        "at": _now(),
        "request_id": req["request_id"],
        "session_id": req["session_id"],
        "surface_uuid": req["surface_uuid"],
        "requested": req["requested"],
        "authority": req["authority"],
        "owner_ref": req.get("owner_ref"),
        "pin_action": req.get("pin_action"),
        "reason": req.get("reason"),
        "checkpoint": req.get("checkpoint"),
        "attested_drained": bool(req.get("attested_drained")),
        "request_hash": req["request_hash"],
        "prior": None,
        "observed_ui": None,
        "observed_runtime": None,
        "keys_sent": [],
    }

    def done(outcome, code=None, **kw):
        rec = {**base, "outcome": outcome, "code": code, **kw}
        _append_chain(rec)
        return rec

    lock = SurfaceLock(req["surface_uuid"])
    if not lock.acquire():
        # not persisted: every chain write happens under this surface's lock
        return {**base, "outcome": "refused", "code": "busy_lock", "note": "another effort transition holds this surface (or its lock is incomplete)"}
    try:
        return _apply_locked(req, base, done, keys, identity_check, transcript, pause)
    finally:
        lock.release()


def _apply_locked(req, base, done, keys, identity_check, transcript, pause) -> dict:
    sid = req["session_id"]
    try:
        chain = _read_chain(sid)
    except ChainDamaged as e:
        return {**base, "outcome": "refused", "code": "history_damaged", "note": f"receipt chain {e}; not reset or repaired"}
    if req["requested"] not in POLICY_EFFORTS:
        return done("refused", "unsupported_level", note=f"only {list(POLICY_EFFORTS)} are switched; xhigh/max/ultracode are out of policy")
    if req["authority"] not in ("policy", "owner"):
        return done("refused", "bad_authority")
    if req["authority"] == "owner" and not req.get("owner_ref"):
        return done("refused", "owner_ref_required", note="an owner pin/release needs the owner's actual authorization reference")
    if req.get("pin_action") and req["authority"] != "owner":
        return done("refused", "pin_requires_owner")
    if not req.get("attested_drained"):
        return done("refused", "not_drained", note="the supervisor must attest a drained, authorized checkpoint")
    try:
        same = [r for r in chain if r.get("request_id") == req["request_id"]]
        if same:
            if same[0].get("request_hash") != req["request_hash"]:
                return done("refused", "request_conflict", note="same request_id, different content")
            last = same[-1]
            if last.get("outcome") in ("applied_ui", "unknown", "pending"):
                ok, code, detail = identity_check(keys.screen())
                if not ok:
                    return done("refused", code or "identity", identity=detail, note="identity not re-established; no reconcile claim")
                rec = {**_reconcile(req, last, keys, transcript), "identity": detail}
                _append_chain(rec)
                return rec
            return {**last, "replayed": True}

        lines = keys.screen()
        ok, code, detail = identity_check(lines)
        base["identity"] = detail
        if not ok:
            return done("refused", code or "identity")
        c = classify(lines)
        pt = prompt_text(lines)
        if c["state"] != "prompt" or pt is None:
            return done("refused", "not_at_prompt", screen_state=c["state"])
        if pt:
            return done("refused", "prompt_not_empty", note="drafted input is never touched")
        ver = footer_version(lines)
        base["claude_version"] = ".".join(map(str, ver)) if ver else None
        if not ver or ver < EFFORT_MIN_VERSION:
            return done("refused", "unsupported_version")
        cur = footer_effort(lines)
        base["prior"] = cur
        pin = pin_state(chain)
        base["pin_before"] = pin
        if cur is None:
            return done("refused", "effort_unreadable")
        if req["authority"] == "policy" and pin:
            return done("refused", "owner_pin", note=f"owner pinned {pin['level']} ({pin['owner_ref']}); only an owner release changes it")
        if req["authority"] == "policy" and cur not in POLICY_EFFORTS:
            return done("refused", "out_of_policy_current", note=f"current {cur!r} was not set by policy; treated as owner state")
        if cur == req["requested"]:
            return done("no_op", observed_ui=cur, pin_after=pin_state(chain + [{**base, "outcome": "no_op"}]))
        if cur not in SLIDER_LABELS:
            return done("refused", "effort_unreadable")

        hashes_before = settings_hashes()
        base["settings_before"] = hashes_before

        def recover(outcome, code, note=None, **kw):
            keys.key("escape")
            base["keys_sent"].append("escape")
            time.sleep(pause)
            after = keys.screen()
            if prompt_text(after) != "":
                left = "input line not empty after Esc; left for a human, never cleared with Enter"
                return done("unknown", code, note="; ".join(x for x in (note, left) if x), screen_tail=[l[:160] for l in after[-6:]], **kw)
            return done(outcome, code, note=note, **kw)

        # Durable intent BEFORE the first native side effect: an interruption from here on leaves
        # a `pending` record that a replay reconciles read-only and never resends.
        _append_chain({**base, "outcome": "pending", "code": None, "at": _now()})
        keys.text("/effort")
        base["keys_sent"].append("text:/effort")
        time.sleep(pause)
        keys.key("enter")
        base["keys_sent"].append("enter")
        time.sleep(pause)
        s = slider_marker(keys.screen())
        if s is None:
            return recover("refused", "slider_absent")
        if not s["hint_ok"]:
            return recover("refused", "session_only_hint_missing")
        if s["at"] != cur:
            return recover("refused", "state_mismatch", note=f"marker at {s['at']!r}, footer {cur!r}")
        steps = SLIDER_LABELS.index(req["requested"]) - SLIDER_LABELS.index(cur)
        k = "right" if steps > 0 else "left"
        for _ in range(abs(steps)):
            keys.key(k)
            base["keys_sent"].append(k)
            time.sleep(pause / 2)
        time.sleep(pause / 2)
        s2 = slider_marker(keys.screen())
        if not s2 or s2["at"] != req["requested"]:
            return recover("refused", "state_mismatch", note=f"marker at {s2 and s2['at']!r} after arrows, wanted {req['requested']!r}")
        keys.text("s")
        base["keys_sent"].append("text:s")
        committed_at = _now()
        base["committed_at"] = committed_at
        time.sleep(pause * 1.5)
        after = keys.screen()
        ui = footer_effort(after)
        base["observed_ui"] = ui
        hashes_after = settings_hashes()
        base["settings_after"] = hashes_after
        base["settings_unchanged"] = hashes_after == hashes_before
        if slider_marker(after) is not None or prompt_text(after) != "":
            return done("unknown", "post_commit_screen", note="slider or input text still showing after `s`; not resent", screen_tail=[l[:160] for l in after[-6:]])
        if ui != req["requested"]:
            return done("unknown", "footer_unchanged", note="footer did not show the target after `s`; not resent")
        if not base["settings_unchanged"]:
            return done("applied_ui", "settings_changed", note="saved-settings hash changed during the switch; reported, not repaired")
        return done("applied_ui", None, note="runtime pending: re-run the same request after the next reply to confirm")
    except RuntimeError as e:
        return done("unknown", "transport", note=str(e)[:300])


def cmd_effort(a: argparse.Namespace) -> int:
    if not UUID_RE.match(a.surface or "") or not UUID_RE.match(a.session_id or ""):
        return _fail(EXIT_USAGE, "bad_identity", "--surface and --session-id must be UUIDs")
    if a.release_pin and a.pin:
        return _fail(EXIT_USAGE, "bad_pin", "--pin and --release-pin are exclusive")
    req = {
        "request_id": a.request_id,
        "session_id": a.session_id,
        "surface_uuid": a.surface.upper(),
        "requested": a.to,
        "authority": a.authority,
        "owner_ref": a.owner_ref,
        "pin_action": "pin" if a.pin else ("release" if a.release_pin else None),
        "reason": a.reason,
        "checkpoint": a.checkpoint,
        "attested_drained": a.attest_drained,
    }
    keys = CmuxKeys(Cmux(a.cmux), req["surface_uuid"])

    def identity_check(lines):
        return effort_identity(a.pid, a.session_id, req["surface_uuid"], a.cwd, lines, a.purpose, a.expected_model)

    tp = transcript_path(a.session_id, a.cwd)
    rec = apply_effort(req, keys, identity_check, tp, pause=a.pause_s)
    _emit(rec)
    return {
        "applied": EXIT_OK,
        "applied_ui": EXIT_OK,
        "no_op": EXIT_OK,
        "refused": EXIT_REFUSED,
        "unknown": EXIT_UNKNOWN,
        "runtime_mismatch": EXIT_MISMATCH,
    }.get(rec["outcome"], EXIT_UNKNOWN)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="executor_session.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    L = sub.add_parser("launch", help="launch a dedicated executor workspace with an explicit model")
    L.add_argument("--cwd", required=True, help="authorized worktree the executor runs in")
    L.add_argument("--purpose", default=DEFAULT_PURPOSE, choices=PURPOSES)
    L.add_argument("--model", default=None, help="opus (default for implementation) | sonnet | haiku | a claude-* id; Fable is refused for implementation")
    L.add_argument("--effort", default=None, choices=EFFORTS, help="claude --effort level (low|medium|high|xhigh|max). Default: medium for an Opus implementation launch; unset (session default) otherwise. low = mechanical work, high = hard work or demonstrated failures; xhigh/max only on request or measured benefit")
    L.add_argument("--subagent-model", default=DEFAULT_SUBAGENT_MODEL, help="CLAUDE_CODE_SUBAGENT_MODEL exported into the workspace so the executor's workers cannot inherit Fable (default sonnet)")
    L.add_argument("--name", default=None, help="workspace title (display only; identity is by UUID)")
    L.add_argument("--window", default=None, help="target window id/ref (optional; default: the caller's window). Placement is not a security boundary")
    L.add_argument("--focus", default="false", help="true|false (default false: never steal the owner's focus)")
    L.add_argument("--session-id", default=None, help="UUID to run the session under (default: generated)")
    L.add_argument("--permission-mode", default=None, help="claude --permission-mode value instead of --dangerously-skip-permissions")
    L.add_argument("--claude-bin", default=None)
    L.add_argument("--cmux", default=os.environ.get("CMUX_BRIDGE_CLI", DEFAULT_CMUX))
    L.add_argument("--wait-s", default=45, type=float, help="bounded wait for the prompt+footer")
    L.add_argument("--receipt", default=None, help="launch receipt path (default: $CMUX_BRIDGE_STATE_DIR/launches/<session>.json)")
    L.add_argument("--dry-run", action="store_true", help="resolve policy, identity and the exact cmux argv; launch nothing")
    L.set_defaults(func=cmd_launch)

    V = sub.add_parser("verify", help="verify an existing session's model/purpose before reusing it")
    V.add_argument("--surface", required=True)
    V.add_argument("--pid", required=True, type=int)
    V.add_argument("--session-id", default=None)
    V.add_argument("--cwd", default=None)
    V.add_argument("--purpose", default=DEFAULT_PURPOSE, choices=PURPOSES)
    V.add_argument("--expected-model", default=None)
    V.add_argument("--cmux", default=os.environ.get("CMUX_BRIDGE_CLI", DEFAULT_CMUX))
    V.set_defaults(func=cmd_verify)

    E = sub.add_parser("effort", help="apply + verify a supervisor-named SESSION-ONLY effort level on a running executor")
    E.add_argument("--surface", required=True)
    E.add_argument("--pid", required=True, type=int)
    E.add_argument("--session-id", required=True)
    E.add_argument("--cwd", required=True, help="the session's cwd (locates its transcript for runtime confirmation)")
    E.add_argument("--to", required=True, help="low|medium|high (xhigh/max/ultracode are refused)")
    E.add_argument("--request-id", required=True, help="supervisor-chosen stable id; a replay never resends")
    E.add_argument("--authority", required=True, choices=("policy", "owner"))
    E.add_argument("--owner-ref", default=None, help="the owner's actual authorization reference (required for --authority owner)")
    E.add_argument("--pin", action="store_true", help="owner pin: policy switches are refused until an owner release")
    E.add_argument("--release-pin", action="store_true")
    E.add_argument("--reason", required=True)
    E.add_argument("--checkpoint", required=True, help="checkpoint/phase id the decision belongs to")
    E.add_argument("--attest-drained", action="store_true", help="supervisor attests the session is drained at an authorized checkpoint (recorded, not measured)")
    E.add_argument("--purpose", default=DEFAULT_PURPOSE, choices=PURPOSES)
    E.add_argument("--expected-model", default=None)
    E.add_argument("--pause-s", default=0.8, type=float)
    E.add_argument("--cmux", default=os.environ.get("CMUX_BRIDGE_CLI", DEFAULT_CMUX))
    E.set_defaults(func=cmd_effort)

    P = sub.add_parser("policy", help="resolve what --model a launch would use (no cmux)")
    P.add_argument("--purpose", default=DEFAULT_PURPOSE)
    P.add_argument("--model", default=None)
    P.set_defaults(func=cmd_policy)
    return p


def main(argv: "list | None" = None) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    if not getattr(a, "func", None):
        p.print_help()
        return EXIT_USAGE
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
