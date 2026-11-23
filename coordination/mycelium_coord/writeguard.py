#!/usr/bin/env python3
"""Cross-session stale-write guard (spec: docs/triangle/WRITE_GUARD_SPEC.md).

Stdlib only; runnable standalone; no package imports. Python 3.9+.

    python3 writeguard.py hook --host claude|codex|antigravity --event pre|post   (payload on stdin)
    python3 writeguard.py status [--path P]
    python3 writeguard.py selftest

Refuses STALE whole-file overwrites (optimistic concurrency): agy write_file and Codex
apply_patch Add File / Move to over an existing path that this session has not read or
written since the file last changed. Shell overwrites are warned about, never denied.
Fail-open-and-logged: any internal error emits the host's ALLOW output.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import time

HASH_LIMIT = 16 * 1024 * 1024
SOFT_DEADLINE_S = 2.0
PRUNE_AGE_S = 14 * 86400
PRUNE_MAX_DIRS = 50
PRUNE_ONE_IN = 200
PENDING_TTL_S = 86400  # an unconsumed PRE snapshot older than this is abandoned (command denied or never finished)
PENDING_MAX = 64  # per session
HOSTS = ("claude", "codex", "antigravity")

READ_CMDS = {"cat", "head", "tail", "sed", "nl", "wc", "less", "bat"}
# Programs that finish at once. A shell overwrite is recorded at POST only when every program in the command
# is in this set; with anything slower (make, pytest, sleep, ...) the file may change between the redirect and POST.
FAST_PROGS = READ_CMDS | {"echo", "printf", "true", "false", ":"}
SEPARATORS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"}

_T0 = time.monotonic()


# --------------------------------------------------------------------------- config


def state_dir() -> str:
    d = os.environ.get("MYCELIUM_WRITE_GUARD_DIR")
    if d:
        return d
    return os.path.join(
        os.path.expanduser("~"), ".local", "state", "mycelium", "write-guard"
    )


def mode() -> str:
    m = (os.environ.get("MYCELIUM_WRITE_GUARD") or "enforce").strip().lower()
    return m if m in ("enforce", "warn", "off") else "enforce"


def log_error(msg: str) -> None:
    try:
        d = state_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "errors.log"), "a") as f:
            f.write(
                "%s %s\n"
                % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg.replace("\n", " | "))
            )
    except Exception:
        pass


def log_event(kind: str, **fields) -> None:
    """Append one JSON line to <dir>/events.log (denials, warnings, uncovered shell writes)."""
    try:
        d = state_dir()
        os.makedirs(d, exist_ok=True)
        rec = {"at": time.time(), "kind": kind}
        rec.update(fields)
        with open(os.path.join(d, "events.log"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- paths + signatures


def resolve(path: str, cwd: str | None) -> str:
    """realpath; for a not-yet-existing path, realpath(parent) + basename. Relative -> against cwd."""
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    path = os.path.normpath(path)
    if os.path.lexists(path):
        return os.path.realpath(path)
    parent, base = os.path.split(path)
    return os.path.join(os.path.realpath(parent), base)


def signature(path: str):
    """(size, mtime_ns, sha256-or-None) or None when the path is not a regular file."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    import stat as _stat

    if not _stat.S_ISREG(st.st_mode):
        return None
    sha = None
    if st.st_size <= HASH_LIMIT:
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                while True:
                    if time.monotonic() - _T0 > SOFT_DEADLINE_S:
                        h = None
                        break
                    b = f.read(1 << 20)
                    if not b:
                        break
                    h.update(b)
        except OSError:
            h = None  # stat worked but the file is unreadable: compare by size + mtime_ns
        sha = h.hexdigest() if h is not None else None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha}


def same_sig(a, b) -> bool:
    if a is None or b is None:
        return False
    if a.get("sha256") and b.get("sha256"):
        return a["size"] == b["size"] and a["sha256"] == b["sha256"]
    return a["size"] == b["size"] and a["mtime_ns"] == b["mtime_ns"]


def pkey(real: str) -> str:
    return hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()


# --------------------------------------------------------------------------- stores


def atomic_write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), time.monotonic_ns())
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)[:128] or "_"


def session_file(host: str, sid: str, real: str) -> str:
    return os.path.join(
        state_dir(), "sessions", host, safe_name(sid), pkey(real) + ".json"
    )


def ledger_file(real: str) -> str:
    return os.path.join(state_dir(), "last_writer", pkey(real) + ".json")


def read_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def record(
    host: str, sid: str, real: str, event: str, tool: str, write: bool, sig=None
) -> None:
    """Record a read/write. Never raises: a store failure is logged (best effort) and ignored.
    `sig` is the signature of the content the session actually saw (PRE snapshot); default: now."""
    try:
        sig = sig or signature(real)
        if sig is None:
            return
        now = time.time()
        atomic_write_json(
            session_file(host, sid, real),
            {"path": real, "sig": sig, "event": event, "tool": tool, "at": now},
        )
        if write:
            atomic_write_json(
                ledger_file(real),
                {"path": real, "host": host, "session_id": sid, "at": now},
            )
    except Exception as e:  # noqa: BLE001
        log_error("record %s: %r" % (real, e))


def store_writable(host: str, sid: str) -> bool:
    """Probe that the session's record dir accepts an atomic write (then remove the probe)."""
    probe = os.path.join(
        os.path.dirname(session_file(host, sid, "probe")),
        ".probe.%d.%d.json" % (os.getpid(), time.monotonic_ns()),
    )
    try:
        atomic_write_json(probe, {"probe": 1})
    except OSError as e:
        log_error("write guard store not writable: %r" % (e,))
        return False
    try:
        os.unlink(probe)
    except OSError:
        pass
    return True


def call_key(host: str, sid: str, tool: str, payload: dict, what) -> str:
    """Key that pairs a PRE with its POST: the host's call id when the payload carries one (Codex and Claude
    `tool_use_id`, agy `toolCall.id`; the fixtures show the same id in PRE and POST), otherwise a hash of
    (host, session, tool, command-or-paths)."""
    cid = payload.get("tool_use_id") or (payload.get("toolCall") or {}).get("id")
    if cid:
        return "id-" + hashlib.sha256(str(cid).encode("utf-8", "replace")).hexdigest()[:40]
    blob = json.dumps([host, sid, tool, what], sort_keys=True, default=str)
    return "cmd-" + hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:40]


def _is_fast(info) -> bool:
    """A segment is fast when its program is in FAST_PROGS and it cannot run until stopped
    (`tail -f/-F/--follow` follows a file forever: review repair-verify NX2)."""
    if info == "cd" or not info:
        return False
    prog, args = info[0], info[1] if len(info) > 1 else []
    if prog not in FAST_PROGS:
        return False
    if prog == "tail":
        for a in args:
            t = a[0] if isinstance(a, (tuple, list)) else str(a)
            if t.startswith("--f") or t.startswith("--retry"):
                return False
            if t.startswith("-") and not t.startswith("--") and ("f" in t[1:] or "F" in t[1:]):
                return False
    return True


def pending_dir(host: str, sid: str) -> str:
    return os.path.join(state_dir(), "pending", host, safe_name(sid))


def _expire_pending(d: str) -> None:
    """Drop abandoned snapshots and keep the directory below PENDING_MAX (room for one more)."""
    now = time.time()
    entries = []
    for n in os.listdir(d):
        fp = os.path.join(d, n)
        try:
            mt = os.stat(fp).st_mtime
        except OSError:
            continue
        if now - mt > PENDING_TTL_S:
            try:
                os.unlink(fp)
            except OSError:
                pass
        else:
            entries.append((mt, fp))
    entries.sort()
    for _mt, fp in entries[: max(0, len(entries) - (PENDING_MAX - 1))]:
        try:
            os.unlink(fp)
        except OSError:
            pass


def take_snapshot(host: str, sid: str, key: str, reals) -> None:
    """PRE: remember the signature of each candidate read path. Never raises."""
    try:
        sigs = {}
        for real in reals:
            sg = signature(real)
            if sg is not None:
                sigs[real] = sg
        if not sigs:
            return
        d = pending_dir(host, sid)
        os.makedirs(d, exist_ok=True)
        _expire_pending(d)
        atomic_write_json(os.path.join(d, key + ".json"), {"at": time.time(), "sigs": sigs})
    except Exception as e:  # noqa: BLE001
        log_error("snapshot: %r" % (e,))


def consume_snapshot(host: str, sid: str, key: str):
    """POST: the PRE snapshot ({real: sig}) for this call, removed; None when there is none."""
    fp = os.path.join(pending_dir(host, sid), key + ".json")
    rec = read_json(fp)
    if rec is None:
        return None
    try:
        os.unlink(fp)
    except OSError:
        pass
    sigs = rec.get("sigs") if isinstance(rec, dict) else None
    return sigs if isinstance(sigs, dict) else None


def record_seen(host: str, sid: str, real: str, tool: str, snap: dict) -> None:
    """Record a read only when the file still has the signature it had at PRE (content the session saw)."""
    s0 = snap.get(real)
    if s0 is not None and same_sig(s0, signature(real)):
        record(host, sid, real, "post", tool, write=False, sig=s0)


def patch_succeeded(payload: dict) -> bool:
    """Codex apply_patch POST: success only as in the captured fixture ('Exit code: 0' ... 'Success')."""
    resp = payload.get("tool_response")
    return (
        isinstance(resp, str)
        and re.match(r"\s*Exit code: 0\b", resp) is not None
        and "Success" in resp
    )


def forget(host: str, sid: str, real: str) -> None:
    try:
        os.unlink(session_file(host, sid, real))
    except Exception:  # noqa: BLE001
        pass


def prune() -> None:
    root = os.path.join(state_dir(), "sessions")
    cutoff = time.time() - PRUNE_AGE_S
    removed = 0
    try:
        hosts = os.listdir(root)
    except OSError:
        return
    import shutil

    for h in hosts:
        hd = os.path.join(root, h)
        try:
            sids = os.listdir(hd)
        except OSError:
            continue
        for s in sids:
            if removed >= PRUNE_MAX_DIRS:
                return
            sd = os.path.join(hd, s)
            try:
                newest = max(
                    [os.stat(sd).st_mtime]
                    + [os.stat(os.path.join(sd, n)).st_mtime for n in os.listdir(sd)]
                )
            except OSError:
                continue
            if newest < cutoff:
                shutil.rmtree(sd, ignore_errors=True)
                removed += 1
    # Abandoned PRE snapshots (command denied, interrupted, or POST with error) of any session:
    # expire them by PENDING_TTL_S and drop the emptied session dir (review repair-verify NX1).
    proot = os.path.join(state_dir(), "pending")
    try:
        phosts = os.listdir(proot)
    except OSError:
        return
    for h in phosts:
        hd = os.path.join(proot, h)
        try:
            sids = os.listdir(hd)
        except OSError:
            continue
        for s in sids:
            if removed >= PRUNE_MAX_DIRS:
                return
            sd = os.path.join(hd, s)
            try:
                _expire_pending(sd)
                if not os.listdir(sd):
                    os.rmdir(sd)
                    removed += 1
            except OSError:
                continue


# --------------------------------------------------------------------------- payload parsing


def get_session(host: str, payload: dict) -> str:
    if host == "antigravity":
        return str(
            payload.get("conversationId") or payload.get("conversation_id") or "unknown"
        )
    return str(payload.get("session_id") or "unknown")


PATCH_RE = re.compile(r"^\*\*\* (Add File|Update File|Delete File): (.+?)\s*$")
MOVE_RE = re.compile(r"^\*\*\* Move to: (.+?)\s*$")


def parse_patch(text: str):
    """Return list of (op, path, move_to_or_None) for an apply_patch body."""
    ops = []
    for line in (text or "").splitlines():
        m = PATCH_RE.match(line)
        if m:
            ops.append([m.group(1), m.group(2), None])
            continue
        m = MOVE_RE.match(line)
        if m and ops:
            ops[-1][2] = m.group(1)
    return [tuple(o) for o in ops]


def shell_command(payload: dict) -> str | None:
    ti = payload.get("tool_input")
    if isinstance(ti, dict):
        c = ti.get("command") or ti.get("cmd")
        if isinstance(c, list):
            c = " ".join(str(x) for x in c)
        if isinstance(c, str):
            return c
    return None


PUNCT = "();<>|&"
REDIRECTS = {">", ">>", "<", "2>", ">&", "<<", "&>"}
_OPSET = SEPARATORS | REDIRECTS | {"{", "}"}
# Fixed operator list; a punctuation run is split by longest match against it. A run that cannot be
# split into a plausible operator sequence becomes UNKNOWN_OP, which shell_effects treats as
# "record nothing for the whole command".
OPERATORS = (
    ";;&", "&>>", "<<<", "<<-", ";;", ";&", "&&", "||", "|&", ">>", ">|", ">&", "<&", "&>", "<>", "<<",
    "<", ">", ";", "&", "|", "(", ")",
)  # fmt: skip
_REDIR_OPS = {">>", ">|", ">&", "<&", "&>", "&>>", "<>", "<<", "<<<", "<<-", "<", ">"}
UNKNOWN_OP = "\x01UNKNOWN_OP"


def split_punct(run: str):
    """Split a punctuation run into known operators (longest match). A run that does not split, or
    that is not plausible shell (a redirection directly followed by another operator other than
    '(' , e.g. '><' or '>;'), returns [UNKNOWN_OP]."""
    pieces, i = [], 0
    while i < len(run):
        for op in OPERATORS:
            if run.startswith(op, i):
                pieces.append(op)
                i += len(op)
                break
        else:
            return [UNKNOWN_OP]
    for a, b in zip(pieces, pieces[1:]):
        if a in _REDIR_OPS and b != "(":
            return [UNKNOWN_OP]
    return pieces


def tokens(cmd: str):
    """Quote-aware shell tokenizer. Unquoted newlines become ';'. Operators are runs of ();<>|&.
    A quoted word equal to an operator is prefixed with NUL so it is never read as one."""
    out, buf = [], []
    inword = quoted = False
    punct = []
    n = len(cmd)
    i = 0

    def flush_word():
        nonlocal inword, quoted
        if inword:
            w = "".join(buf)
            if quoted and w in _OPSET:
                w = "\x00" + w
            out.append(w)
        buf.clear()
        inword = quoted = False

    def flush_punct():
        if punct:
            out.extend(split_punct("".join(punct)))
            punct.clear()

    while i < n:
        c = cmd[i]
        if punct and c not in PUNCT:
            flush_punct()
        if c == "\\":
            if i + 1 < n and cmd[i + 1] == "\n":
                i += 2
                continue
            if i + 1 < n:
                buf.append(cmd[i + 1])
                inword = quoted = True  # an escaped character is literal, like a quoted one
            i += 2
            continue
        if c == "'":
            j = cmd.find("'", i + 1)
            j = n if j < 0 else j
            buf.append(cmd[i + 1 : j])
            inword = quoted = True
            i = j + 1
            continue
        if c == '"':
            i += 1
            while i < n and cmd[i] != '"':
                if cmd[i] == "\\" and i + 1 < n and cmd[i + 1] in '"\\$`\n':
                    if cmd[i + 1] != "\n":
                        buf.append(cmd[i + 1])
                    i += 2
                    continue
                buf.append(cmd[i])
                i += 1
            inword = quoted = True
            i += 1
            continue
        if c in " \t\r":
            flush_word()
        elif c == "\n":
            flush_word()
            flush_punct()
            out.append(";")
        elif c in PUNCT:
            flush_word()
            punct.append(c)
        elif c == "#" and not inword:
            while i < n and cmd[i] != "\n":
                i += 1
            continue
        else:
            buf.append(c)
            inword = True
        i += 1
    flush_word()
    flush_punct()
    return out


def segments(toks):
    seg, out = [], []
    for t in toks:
        if t in SEPARATORS:
            if seg:
                out.append(seg)
            seg = []
        else:
            seg.append(t)
    if seg:
        out.append(seg)
    return out


def _literal_cd_target(args):
    """Return the literal directory of a `cd`, or None when it cannot be known statically."""
    args = [a for a in args if not (a.startswith("-") and a != "-")]
    if not args:
        return os.path.expanduser("~")
    t = args[0]
    if t == "-" or any(ch in t for ch in "$`*?[{\x00"):
        return None
    if t.startswith("~") and t != "~" and not t.startswith("~/"):
        return None
    return os.path.expanduser(t)


def _seg_reads(seg, cur):
    """RESOLVED paths read by ONE segment (cat/head/tail/sed/nl/wc/less/bat), resolved against cur."""
    prog = os.path.basename(seg[0])
    if prog not in READ_CMDS:
        return []
    # drop redirection operators and their targets
    clean, skip = [], False
    for a in seg[1:]:
        if skip:
            skip = False
            continue
        if a in (">", ">>", "<", "2>", ">&", "<<"):
            skip = True
            continue
        if prog == "sed" and a in ("-e", "-f", "--expression", "--file"):
            skip = True
            continue
        if a.startswith("-"):
            continue
        clean.append(a)
    args = clean
    if prog == "sed":
        has_script_flag = any(
            a in ("-e", "-f", "--expression", "--file") for a in seg[1:]
        )
        if not has_script_flag and args:
            args = args[1:]
    return [resolve(a, cur) for a in args if a != "-"]


def shell_reads(cmd: str, cwd: str | None = None):
    """Best-effort RESOLVED paths read by cat/head/tail/sed/nl/wc/less/bat. Tracks the effective
    cwd across a literal `cd DIR`; after a non-literal cd (or pushd/popd) stops (records nothing more)."""
    found = []
    cur = cwd or os.getcwd()
    for seg in segments(tokens(cmd)):
        prog = os.path.basename(seg[0])
        if prog in ("pushd", "popd"):
            return found
        if prog == "cd":
            tgt = _literal_cd_target(seg[1:])
            if tgt is None:
                return found
            cur = resolve(tgt, cur)
            continue
        found.extend(_seg_reads(seg, cur))
    return found


def _seg_overwrites(seg):
    """Overwrite targets (kind, raw path) of ONE command segment."""
    out = []
    for i, t in enumerate(seg):
        if t == ">" and i + 1 < len(seg):
            if i > 0 and seg[i - 1] in ("2", "&"):
                continue
            tgt = seg[i + 1]
            if tgt in SEPARATORS or tgt.startswith("&") or tgt == "/dev/null":
                continue
            out.append(("redirect", tgt))
    prog = os.path.basename(seg[0])
    rest = seg[1:]
    if prog == "tee":
        if any(
            a in ("-a", "--append")
            or (a.startswith("-") and not a.startswith("--") and "a" in a)
            for a in rest
        ):
            return out
        out.extend(
            ("tee", a) for a in rest if not a.startswith("-") and a != "/dev/null"
        )
    elif prog in ("cp", "mv"):
        cleaned = []
        skip = False
        for a in rest:
            if skip:
                skip = False
                continue
            if a in (">", ">>", "2>", "<"):
                skip = True
                continue
            if a.startswith("-"):
                continue
            cleaned.append(a)
        if len(cleaned) >= 2:
            out.append((prog, cleaned[-1]))
    return out


def shell_overwrites(cmd: str):
    """Best-effort overwrite targets (loose; used only for the never-denying pre-event warning):
    `>` (not `>>`), `tee` without -a, cp/mv destination. Returns list of (kind, raw path)."""
    out = []
    for seg in segments(tokens(cmd)):
        out.extend(_seg_overwrites(seg))
    return out


# --------------------------------------------------------------------------- trusted command shapes
# Round 4: shell_effects is an ALLOWLIST validator. A command is trusted only if it matches the
# grammar below in full; anything else returns None = "record nothing". A missed record only causes
# a later "re-read first" deny (safe); a wrong record would grant false freshness (the bug).
# See WRITE_GUARD_SPEC.md "Round 4: trusted command shapes".

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PROG_RE = re.compile(r"^[A-Za-z0-9_./+-]+$")
_CD_DIR_RE = re.compile(r"^[A-Za-z0-9_./+-]+$")
ALLOWED_SEPARATORS = {";", "&&", "||", "|"}  # plus the newline separator
OVERWRITE_REDIRECTS = {">", ">|", "&>"}  # and fd-prefixed 2>
APPEND_REDIRECTS = {">>", "&>>"}  # and fd-prefixed 2>>
INPUT_REDIRECTS = {"<"}
DUP_REDIRECTS = {">&"}  # and fd-prefixed 2>&, only to a digit or '-'
ALLOWED_REDIRECTS = OVERWRITE_REDIRECTS | APPEND_REDIRECTS | INPUT_REDIRECTS | DUP_REDIRECTS
RESERVED_WORDS = {
    "if", "then", "elif", "else", "fi", "do", "done", "while", "until", "for", "in", "case",
    "esac", "select", "time", "!", "coproc", "function", "{", "}", "[[", "]]",
}  # fmt: skip
# Builtins that change shell state, control flow or what a later command means. A segment starting
# with one of these makes the whole command untrusted (exit/return stop later segments from running).
GATED_BUILTINS = {
    "builtin", "command", "eval", "source", ".", "exec", "pushd", "popd", "alias", "trap", "set",
    "shopt", "setopt", "unsetopt", "exit", "return", "logout", "break", "continue", "export",
    "unset", "readonly", "declare", "typeset", "local", "let", "read", "wait", "shift", "getopts",
    "umask", "ulimit", "hash", "enable", "bind", "fg", "bg", "disown", "jobs", "kill", "times",
    "type", "unalias", "emulate", "autoload", "noglob", "nocorrect", "zmodload", "zstyle",
    "compdef", "limit", "unlimit", "rehash", "dirs", "chdir", "fc", "history", "integer",
    "float", "functions", "unfunction", "whence", "where", "which", "print", "vared", "sched",
}  # fmt: skip
# an assignment prefix to one of these changes what a later program is or how it parses
_DANGEROUS_ASSIGN = {
    "PATH", "IFS", "SHELL", "ENV", "BASH_ENV", "CDPATH", "HOME", "PWD", "OLDPWD", "POSIXLY_CORRECT",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "GLOBIGNORE",
    "SHELLOPTS", "BASHOPTS", "PS4", "PROMPT_COMMAND", "FPATH", "ZDOTDIR", "NULLCMD", "READNULLCMD",
}  # fmt: skip
# per read program: (short flags without a value, short flags that take a value). Anything else,
# any --long option, or an option after the first file makes that segment record no read.
_READ_OPTS = {
    "cat": ("AbeEnstTuv", ""),
    "head": ("qvz0123456789", "nc"),
    "tail": ("qvz0123456789", "nc"),
    "wc": ("lwcmL", ""),
    "nl": ("p", "bdfhilnsvw"),
    "sed": ("nEr", ""),
    "less": ("", ""),
    "bat": ("", ""),
}
_SED_SCRIPT_RE = re.compile(r"^(\$|[0-9]+)?(,(\$|[0-9]+))?[pdq=]?$")
_OP_CHARS = "();<>|&"
_SPECIAL_CHARS = "*?[]{}~!"


def _pregate_ok(cmd: str) -> bool:
    """Raw-text gate before tokenizing: no backslash anywhere, no control characters, no $ or backtick
    outside single quotes, no unterminated quote."""
    if "\\" in cmd:
        return False
    for ch in cmd:
        if ord(ch) < 32 and ch not in "\t\n":
            return False
    q = None
    for c in cmd:
        if q == "'":
            if c == "'":
                q = None
        elif q == '"':
            if c == '"':
                q = None
            elif c in "$`":
                return False
        elif c in "'\"":
            q = c
        elif c in "$`":
            return False
    return q is None


def _lex(cmd: str):
    """Tokens: ('w', text, start, end, quoted, special) | ('op', text, start, end) | ('nl',).
    Valid only after _pregate_ok (quotes are balanced, no escapes, no expansions)."""
    toks, i, n = [], 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c in " \t":
            i += 1
        elif c == "\n":
            toks.append(("nl",))
            i += 1
        elif c in _OP_CHARS:
            j = i
            while j < n and cmd[j] in _OP_CHARS:
                j += 1
            pos = i
            for p in split_punct(cmd[i:j]):
                toks.append(("op", p, pos, pos + len(p)))
                pos += len(p)
            i = j
        else:
            buf, quoted, special, start = [], False, False, i
            while i < n and cmd[i] not in " \t\n" and cmd[i] not in _OP_CHARS:
                ch = cmd[i]
                if ch in "'\"":
                    j = cmd.find(ch, i + 1)
                    if j < 0:
                        return None
                    buf.append(cmd[i + 1 : j])
                    quoted = True
                    i = j + 1
                    continue
                if i == start and ch == "#":
                    return None  # unquoted word-initial # starts a comment: not modelled
                if ch in _SPECIAL_CHARS or (i == start and ch == "="):
                    special = True  # unquoted glob/brace/tilde/comment/zsh-equals: not a literal word
                buf.append(ch)
                i += 1
            toks.append(("w", "".join(buf), start, i, quoted, special))
    return toks


def _parse_segments(toks):
    """Group tokens into command segments, or None for any shape outside the grammar.
    Segment = dict(words=[(text, quoted, special)], redirs=[(kind, target_word)], sep=str|None)."""
    segs = []
    cur = {"words": [], "redirs": []}
    pending = None  # a redirect waiting for its target word: (kind)
    i, n = 0, len(toks)

    def nonempty():
        return bool(cur["words"] or cur["redirs"])

    while i < n:
        t = toks[i]
        if t[0] == "w":
            _k, text, start, end, quoted, special = t
            nxt = toks[i + 1] if i + 1 < n else None
            if pending is not None:
                kind = pending
                pending = None
                if kind == "dup":
                    if quoted or not (text.isdigit() or text == "-"):
                        return None
                else:
                    if special or not text or "\x00" in text:
                        return None
                cur["redirs"].append((kind, text))
            elif (
                text.isdigit()
                and not quoted
                and nxt is not None
                and nxt[0] == "op"
                and nxt[2] == end
                and nxt[1][0] in "<>"
            ):
                # fd-prefixed redirect: only 2> 2>> 2>& are modelled
                op = nxt[1]
                if text != "2" or op not in (">", ">>", ">&"):
                    return None
                pending = {">": "over", ">>": "append", ">&": "dup"}[op]
                i += 2
                continue
            else:
                cur["words"].append((text, quoted, special))
        elif t[0] == "nl":
            if pending is not None:
                return None
            if nonempty():
                cur["sep"] = "nl"
                segs.append(cur)
                cur = {"words": [], "redirs": []}
        else:
            op = t[1]
            if pending is not None:
                return None
            if op in ALLOWED_REDIRECTS:
                if op in OVERWRITE_REDIRECTS:
                    pending = "over"
                elif op in APPEND_REDIRECTS:
                    pending = "append"
                elif op in INPUT_REDIRECTS:
                    pending = "in"
                else:
                    pending = "dup"
            elif op in ALLOWED_SEPARATORS:
                if not nonempty():
                    return None
                cur["sep"] = op
                segs.append(cur)
                cur = {"words": [], "redirs": []}
                if op in ("&&", "||", "|"):
                    while i + 1 < n and toks[i + 1][0] == "nl":  # line continuation after the operator
                        i += 1
            else:
                return None  # any other operator, including UNKNOWN
        i += 1
    if pending is not None:
        return None
    if nonempty():
        cur["sep"] = None
        segs.append(cur)
    elif segs and segs[-1]["sep"] in ("&&", "||", "|"):
        return None  # dangling operator
    return segs or None


def _classify_segment(seg):
    """Return (prog, args) after dropping assignment prefixes, or None when the segment is not a
    trusted simple command. args are (text, quoted, special) tuples."""
    words = list(seg["words"])
    while words and _ASSIGN_RE.match(words[0][0]):
        text, quoted, special = words[0]
        name = text.split("=", 1)[0]
        if quoted or special or name in _DANGEROUS_ASSIGN or "'" in text or '"' in text:
            return None
        words.pop(0)
    if not words:
        return None
    prog, quoted, special = words[0]
    if (
        special
        or not _PROG_RE.match(prog)
        or prog in RESERVED_WORDS
        or prog in GATED_BUILTINS
        or prog.startswith("\x00")
    ):
        return None
    return prog, words[1:]


def _literal_cd(seg):
    """The resolved-later literal directory of a segment that is exactly `cd <literal dir>`, else None."""
    if seg["redirs"] or len(seg["words"]) != 2 or seg["words"][0][0] != "cd":
        return None
    text, _q, special = seg["words"][1]
    if special or not _CD_DIR_RE.match(text) or text.startswith("-"):
        return None
    if ".." in text.split("/"):
        return None
    return text


def _read_files(prog, args):
    """Literal file words read by a READ_CMDS program, or [] when the options/script are not a shape
    this module models."""
    noval, withval = _READ_OPTS[prog]
    files, script_seen = [], prog != "sed"
    i = 0
    while i < len(args):
        text, _q, special = args[i]
        i += 1
        if text == "-":
            continue
        if text.startswith("-"):
            if files or text.startswith("--") or (prog == "sed" and script_seen):
                return []
            cluster = text[1:]
            for idx, ch in enumerate(cluster):
                if ch in noval:
                    continue
                if ch in withval:
                    if idx + 1 == len(cluster):
                        if i >= len(args):
                            return []
                        i += 1  # the separate value
                    break
                return []
            continue
        if not script_seen:
            script_seen = True
            if special or not _SED_SCRIPT_RE.match(text):
                return []
            continue
        files.append((text, special))
    return [
        t
        for t, special in files
        if not special and t and not t.startswith("~") and ".." not in t.split("/")
    ]


def shell_effects(cmd: str, cwd: str | None = None):
    """Trusted (reads, overwrites) of a shell command as RESOLVED paths, or None = "record nothing".

    Positive definition: the command must match the Round-4 grammar in full (see the section
    "Round 4: trusted command shapes" in WRITE_GUARD_SPEC.md). Summary: no $ ` or backslash outside the
    allowed forms; only ; && || | newline and the redirects > >| &> >> &>> < >& (and 2> 2>> 2>&) as
    operators; every segment a simple command with a literal program word that is not a reserved
    word or state-changing builtin; `cd` only as a leading `cd <dir> && ...` chain. Because post
    payloads carry no exit status, only what runs unconditionally is recorded: the segments up to
    and including the first one followed by && or ||. Reads come only from READ_CMDS segments with
    no file redirect; overwrite targets only from a single overwrite redirect."""
    if not isinstance(cmd, str) or not cmd.strip() or not _pregate_ok(cmd):
        return None
    toks = _lex(cmd)
    if toks is None:
        return None
    segs = _parse_segments(toks)
    if segs is None:
        return None
    infos = []
    for seg in segs:
        if _literal_cd(seg) is not None:
            infos.append(("cd", seg))
            continue
        c = _classify_segment(seg)
        if c is None or c[0] == "cd":
            return None  # includes any cd that is not a literal one
        infos.append((c, seg))
    cur = cwd or os.getcwd()
    k = 0
    while k < len(infos) and infos[k][0] == "cd" and infos[k][1]["sep"] == "&&":
        cur = resolve(_literal_cd(infos[k][1]), cur)
        k += 1
    reads, overwrites = [], []
    fast = all(_is_fast(i[0]) for i in infos[k:])  # see FAST_PROGS: overwrites need an all-fast command
    created = set()  # every redirect target anywhere in the command: it may be created by this command
    for info, seg in infos[k:]:
        if info == "cd":
            return None  # a cd that is not part of the leading && chain
        for kd, tg in seg["redirs"]:
            if kd != "dup" and tg != "/dev/null":
                created.add(resolve(tg, cur))
    for info, seg in infos[k:]:
        prog, args = info
        file_redirs = [(kd, tg) for kd, tg in seg["redirs"] if kd != "dup" and tg != "/dev/null"]
        if len(file_redirs) == 1 and file_redirs[0][0] == "over":
            if fast:
                overwrites.append(resolve(file_redirs[0][1], cur))
        elif not file_redirs and prog in READ_CMDS and prog in _READ_OPTS:
            reads.extend(resolve(f, cur) for f in _read_files(prog, args))
        if seg["sep"] in ("&&", "||"):
            break  # later segments may not have run; post payloads carry no exit status
    # a read of a path that a redirect in the same command may have created proves nothing was read
    return [r for r in reads if r not in created], overwrites


# --------------------------------------------------------------------------- tool classification


def classify(host: str, payload: dict):
    """Return dict: {'kind': 'read'|'write'|'overwrite'|'patch'|'shell'|None, 'paths': [...], 'tool': name, ...}."""
    cwd = payload.get("cwd")
    if host == "claude":
        tool = payload.get("tool_name") or ""
        ti = payload.get("tool_input") or {}
        if tool == "Read":
            fp = ti.get("file_path")
            return {"kind": "read", "tool": tool, "paths": [fp] if fp else []}
        if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            fp = ti.get("file_path") or ti.get("notebook_path")
            return {"kind": "write", "tool": tool, "paths": [fp] if fp else []}
        if tool == "Bash":
            return {"kind": "shell", "tool": tool, "command": shell_command(payload)}
        return {"kind": None, "tool": tool, "paths": []}
    if host == "codex":
        tool = payload.get("tool_name") or ""
        if tool == "apply_patch":
            cmd = shell_command(payload) or ""
            return {"kind": "patch", "tool": tool, "ops": parse_patch(cmd)}
        if tool in ("Bash", "shell", "local_shell", "exec_command", "container.exec"):
            return {"kind": "shell", "tool": tool, "command": shell_command(payload)}
        return {"kind": None, "tool": tool, "paths": []}
    if host == "antigravity":
        tc = payload.get("toolCall") or {}
        tool = tc.get("name") or ""
        a = tc.get("args") or {}
        p = a.get("Path") or a.get("TargetFile") or a.get("AbsolutePath")
        if tool == "read_file":
            return {"kind": "read", "tool": tool, "paths": [p] if p else []}
        if tool == "write_file":
            return {"kind": "overwrite", "tool": tool, "paths": [p] if p else []}
        if tool in ("edit_file", "replace_file_content", "multi_replace_file_content"):
            return {"kind": "write", "tool": tool, "paths": [p] if p else []}
        if tool == "run_command":
            return {"kind": "shell", "tool": tool, "command": a.get("CommandLine")}
        return {"kind": None, "tool": tool, "paths": []}
    return {"kind": None, "tool": "", "paths": []}


# --------------------------------------------------------------------------- output formats


def allow_out(host: str, event: str, context: str | None = None):
    if host == "antigravity":
        return {"decision": "allow"} if event == "pre" else {}
    if context:
        name = "PreToolUse" if event == "pre" else "PostToolUse"
        return {
            "hookSpecificOutput": {"hookEventName": name, "additionalContext": context}
        }
    return None  # empty stdout == allow


def deny_out(host: str, reason: str):
    if host == "antigravity":
        return {"decision": "deny", "reason": reason}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def hhmm_et(ts: float) -> str:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.fromtimestamp(ts, ZoneInfo("America/New_York")).strftime(
            "%H:%M ET"
        )
    except Exception:
        return time.strftime("%H:%M UTC", time.gmtime(ts))


def deny_reason(host: str, real: str, sid: str, paths_note: str | None = None) -> str:
    led = read_json(ledger_file(real))
    who = ""
    if led and not (led.get("host") == host and led.get("session_id") == sid):
        who = "; last written by %s session %s at %s" % (
            led.get("host", "?"),
            str(led.get("session_id", "?"))[:8],
            hhmm_et(float(led.get("at", 0))),
        )
    msg = (
        "WRITE GUARD: %s has changed since this session last read or wrote it "
        "(or this session never read it)%s. Read the current file again and make your change "
        "on top of it — do not overwrite another session's work." % (real, who)
    )
    if host == "codex":
        msg += (
            " Prefer an Update File hunk, which applies on top of the current content."
            " To unlock: run a standalone read such as `cat %s` (on its own, not after && or ||),"
            " then retry." % shlex.quote(real)
        )
    elif host == "antigravity":
        msg += " To unlock: call read_file on %s, then retry." % real
    else:
        msg += " To unlock: read the file with the Read tool, then retry."
    return msg


# --------------------------------------------------------------------------- hook core


def is_stale(host: str, sid: str, real: str) -> bool:
    """True when the target exists and the session's recorded signature does not match."""
    cur = signature(real)
    if cur is None:
        return False  # new file (or non-regular): nothing to overwrite
    rec = read_json(session_file(host, sid, real))
    if not rec:
        return True
    return not same_sig(cur, rec.get("sig"))


def handle(host: str, event: str, payload: dict, md: str):
    """Return (output_obj_or_None, side effects performed). Raises on internal error."""
    sid = get_session(host, payload)
    cwd = payload.get("cwd")
    c = classify(host, payload)
    kind = c["kind"]
    tool = c.get("tool", "")

    if event == "pre":
        # --- whole-file overwrite checks
        targets = []
        if kind == "overwrite":
            targets = [resolve(p, cwd) for p in c["paths"]]
        elif kind == "patch":
            for op, path, move in c["ops"]:
                if op == "Add File":
                    targets.append(resolve(path, cwd))
                if move:
                    targets.append(resolve(move, cwd))
        if targets:
            denied = [t for t in dict.fromkeys(targets) if is_stale(host, sid, t)]
            if denied and not store_writable(host, sid):
                # fail open: with no working record store a missing record proves nothing
                return allow_out(
                    host, event, "write guard inactive: state dir not writable"
                ), True
            if denied:
                if len(denied) == 1:
                    reason = deny_reason(host, denied[0], sid)
                else:
                    reason = (
                        deny_reason(host, denied[0], sid)
                        + " Paths denied: "
                        + ", ".join(denied)
                    )
                log_event(
                    "deny" if md == "enforce" else "would_deny",
                    host=host,
                    session=sid,
                    tool=tool,
                    paths=denied,
                )
                if md == "enforce":
                    return deny_out(host, reason), True
                return allow_out(
                    host, event, "WRITE GUARD (warn mode): " + reason
                ), True
        # --- PRE snapshot of what a read is about to show (Round 5): POST credits a read only if unchanged
        try:
            if kind == "shell" and c.get("command"):
                eff = shell_effects(c["command"], cwd)
                reals = [r for r in (eff[0] if eff else []) if os.path.isfile(r)]
                if reals:
                    take_snapshot(
                        host, sid, call_key(host, sid, tool, payload, c["command"]), reals
                    )
            elif kind == "read" and c["paths"]:
                reals = [resolve(p, cwd) for p in c["paths"]]
                take_snapshot(
                    host, sid, call_key(host, sid, tool, payload, sorted(reals)), reals
                )
        except Exception as e:  # noqa: BLE001
            log_error("pre snapshot: %r" % (e,))
        # --- shell overwrite: warn only, never deny
        if kind == "shell" and c.get("command"):
            stale = []
            for k, p in shell_overwrites(c["command"]):
                real = resolve(p, cwd)
                if os.path.isfile(real) and is_stale(host, sid, real):
                    stale.append(real)
            if stale:
                log_event(
                    "uncovered_shell_overwrite",
                    host=host,
                    session=sid,
                    tool=tool,
                    paths=stale,
                    command=c["command"][:300],
                )
                msg = (
                    "WRITE GUARD warning (not enforced for shell commands): %s changed since this session "
                    "last read or wrote it; this command overwrites it. Re-read it first if another "
                    "session may have edited it." % ", ".join(stale)
                )
                if host == "antigravity":
                    return allow_out(host, event), True
                return allow_out(host, event, msg), True
        return allow_out(host, event), False

    # --- post: record
    if host == "antigravity" and payload.get("error"):
        return allow_out(host, event), False
    if kind == "read":
        reals = [resolve(p, cwd) for p in c["paths"]]
        snap = consume_snapshot(
            host, sid, call_key(host, sid, tool, payload, sorted(reals))
        )
        for real in reals:
            if snap is None:  # no PRE hook ran for this call: keep the plain post-time record
                record(host, sid, real, "post", tool, write=False)
            else:
                record_seen(host, sid, real, tool, snap)
    elif kind in ("write", "overwrite"):
        for p in c["paths"]:
            record(host, sid, resolve(p, cwd), "post", tool, write=True)
    elif kind == "patch":
        if not patch_succeeded(payload):
            return allow_out(host, event), False  # a failed or unrecognised patch proves nothing
        for op, path, move in c["ops"]:
            if op == "Delete File":
                forget(host, sid, resolve(path, cwd))
            elif op == "Update File" and move:
                forget(host, sid, resolve(path, cwd))
                record(host, sid, resolve(move, cwd), "post", tool, write=True)
            else:
                record(host, sid, resolve(path, cwd), "post", tool, write=True)
    elif kind == "shell" and c.get("command"):
        cmd = c["command"]
        # Gated: shell_effects returns None (record nothing) whenever the command is not simple
        # enough to parse safely. A missed record only costs a later "re-read first" deny.
        eff = shell_effects(cmd, cwd)
        snap = consume_snapshot(host, sid, call_key(host, sid, tool, payload, cmd))
        if eff is not None:
            reads, overwrites = eff
            for real in reads:
                # a read counts only if a PRE snapshot exists and the file is unchanged since: a command
                # that keeps running after its read (`cat f && make`) must not credit later edits
                if snap is not None and os.path.isfile(real):
                    record_seen(host, sid, real, tool, snap)
            for real in overwrites:
                if os.path.isfile(real) and os.access(real, os.W_OK):
                    record(host, sid, real, "post", tool, write=True)
    return allow_out(host, event), True


def emit(obj) -> None:
    if obj is not None:
        sys.stdout.write(json.dumps(obj))
        sys.stdout.write("\n")
    sys.stdout.flush()


def cmd_hook(argv) -> int:
    host = event = None
    i = 0
    while i < len(argv):
        if argv[i] == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
        elif argv[i] == "--event" and i + 1 < len(argv):
            event = argv[i + 1]
            i += 2
        else:
            i += 1
    if host not in HOSTS or event not in ("pre", "post"):
        sys.stderr.write(
            "usage: writeguard.py hook --host claude|codex|antigravity --event pre|post\n"
        )
        # unknown host: nothing sensible to emit; do not block
        return 0
    # Everything below is fail-open: on any exception emit the host's ALLOW output.
    try:
        md = mode()
        if md == "off":
            emit(allow_out(host, event))
            return 0
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        out, _ = handle(host, event, payload, md)
        try:
            import random

            if random.randrange(PRUNE_ONE_IN) == 0:
                prune()
        except Exception as e:  # pragma: no cover
            log_error("prune: %r" % (e,))
        emit(out)
    except BaseException as e:  # noqa: BLE001 - fail-open by design
        if isinstance(e, SystemExit) and e.code in (0, None):
            raise
        log_error("hook %s/%s: %r" % (host, event, e))
        emit(allow_out(host, event))
    return 0


# --------------------------------------------------------------------------- status / selftest


def cmd_status(argv) -> int:
    path = None
    if "--path" in argv:
        path = argv[argv.index("--path") + 1]
    root = state_dir()
    out = {"dir": root, "mode": mode(), "sessions": []}
    if path:
        real = resolve(path, os.getcwd())
        out["path"] = real
        out["current_sig"] = signature(real)
        out["last_writer"] = read_json(ledger_file(real))
        key = pkey(real) + ".json"
    else:
        key = None
    sroot = os.path.join(root, "sessions")
    for h in sorted(os.listdir(sroot)) if os.path.isdir(sroot) else []:
        for s in sorted(os.listdir(os.path.join(sroot, h))):
            sd = os.path.join(sroot, h, s)
            if key:
                rec = read_json(os.path.join(sd, key))
                if rec:
                    rec["host"], rec["session_id"] = h, s
                    rec["fresh"] = same_sig(out["current_sig"], rec.get("sig"))
                    out["sessions"].append(rec)
            else:
                out["sessions"].append(
                    {
                        "host": h,
                        "session_id": s,
                        "records": len(
                            [n for n in os.listdir(sd) if n.endswith(".json")]
                        ),
                    }
                )
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_selftest(argv) -> int:
    import subprocess
    import tempfile

    failures = []
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, MYCELIUM_WRITE_GUARD_DIR=os.path.join(d, "state"))
        env.pop("MYCELIUM_WRITE_GUARD", None)
        target = os.path.join(d, "f.txt")
        with open(target, "w") as f:
            f.write("one\n")

        def run(host, event, payload):
            p = subprocess.run(
                [
                    sys.executable,
                    os.path.abspath(__file__),
                    "hook",
                    "--host",
                    host,
                    "--event",
                    event,
                ],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=env,
            )
            return p.stdout.strip()

        def agy(ev, name, path):
            return {
                "conversationId": "cv1",
                "cwd": d,
                "hookEventName": ev,
                "toolCall": {"name": name, "args": {"Path": path}},
            }

        o = run("antigravity", "pre", agy("PreToolUse", "write_file", target))
        if '"deny"' not in o:
            failures.append("unread overwrite not denied: %s" % o)
        run("antigravity", "post", agy("PostToolUse", "read_file", target))
        o = run("antigravity", "pre", agy("PreToolUse", "write_file", target))
        if '"allow"' not in o:
            failures.append("read-then-overwrite not allowed: %s" % o)
        with open(target, "a") as f:
            f.write("two\n")
        o = run("antigravity", "pre", agy("PreToolUse", "write_file", target))
        if '"deny"' not in o:
            failures.append("changed file not denied: %s" % o)
        o = run(
            "antigravity",
            "pre",
            agy("PreToolUse", "write_file", os.path.join(d, "new.txt")),
        )
        if '"allow"' not in o:
            failures.append("new file not allowed: %s" % o)
        patch = "*** Begin Patch\n*** Add File: f.txt\n+x\n*** End Patch"
        cp = {
            "session_id": "s1",
            "cwd": d,
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
        }
        o = run("codex", "pre", dict(cp, hook_event_name="PreToolUse"))
        if "permissionDecision" not in o or '"deny"' not in o:
            failures.append("codex Add File over unread not denied: %s" % o)
    if failures:
        print("selftest FAILED")
        for f in failures:
            print(" -", f)
        return 1
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write(__doc__ or "")
        return 2
    sub, rest = argv[0], argv[1:]
    if sub == "hook":
        return cmd_hook(rest)
    if sub == "status":
        return cmd_status(rest)
    if sub == "selftest":
        return cmd_selftest(rest)
    sys.stderr.write("unknown subcommand %r\n" % sub)
    return 2


if __name__ == "__main__":
    sys.exit(main())
