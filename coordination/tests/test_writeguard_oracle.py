"""Real-shell differential oracle for writeguard.shell_effects (Round 4).

Why: four review rounds each found a shell form that slipped past a denylist. The guard is now an
allowlist (only trusted command shapes record anything), and THIS file is the closure mechanism: a
seeded generator produces thousands of small commands, runs each in a REAL shell inside a fresh temp
dir, and checks the two safety properties against what the shell actually did.

  read oracle       wrapper scripts for every READ_CMD sit first on PATH and log the realpath of every
                    non-option argument that is an existing file (resolved from the wrapper's own cwd)
                    before exec'ing the real binary. Every path shell_effects records as a read (and
                    that is a file after the run, as the post branch requires) must be in that log.
  overwrite oracle  every file is snapshotted before/after (pre-filled with unique markers). Every path
                    shell_effects records as an overwrite (and that is a file after the run) must have
                    been created or changed.

A missed record is fine (safe); a wrong record is a violation. Run under bash and, when present, under
`zsh -f` (Codex executes commands with zsh). A missing zsh is an explicit skip, never silent.
A coverage floor stops the gate from passing trivially by returning None for everything.

Summary line to grep: `WG_ORACLE shell=... violations=N coverage=F ...`.
"""

from __future__ import annotations

import importlib.util
import os
import random
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "mycelium_coord" / "writeguard.py"
spec = importlib.util.spec_from_file_location("writeguard_oracle_under_test", SCRIPT)
wg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wg)

SEED = int(os.environ.get("WG_ORACLE_SEED", "20261123"))
N = int(os.environ.get("WG_ORACLE_N", "1500"))
COVERAGE_FLOOR = 0.15
FILES = ["a.txt", "b.txt", "sub/a.txt", "sub/b.txt", "missing.txt"]
EXISTING = ["a.txt", "b.txt", "sub/a.txt", "sub/b.txt"]
DIRS = ["sub", "nosuch"]
READ_PROGS = ["cat", "head", "tail", "sed -n '1p'", "nl", "wc"]


# ------------------------------------------------------------------------------------ generator
def _quote(rng, w):
    return rng.choice(
        [w] * 12 + ["'%s'" % w, '"%s"' % w, "$'%s'" % w, w.replace(".", "\\.")]
    )


def _redirect(rng):
    t = _quote(rng, rng.choice(FILES))
    return rng.choice(
        [
            "> " + t, ">" + t, ">| " + t, "&> " + t, ">> " + t, "&>> " + t, "< " + t, "<> " + t,
            "2> /dev/null", "2>&1", ">&2", "2> " + t, "2>> " + t, "> /dev/null",
        ]
    )  # fmt: skip


def _segment(rng):
    k = rng.random()
    if k < 0.52:
        prog = rng.choice(READ_PROGS)
        if prog in ("head", "tail") and rng.random() < 0.4:
            prog += " -n 1"
        if prog == "nl" and rng.random() < 0.4:
            prog += " -ba"
        args = " ".join(
            _quote(rng, rng.choice(FILES)) for _ in range(rng.choice([0, 1, 1, 1, 2]))
        )
        s = "%s %s" % (prog, args)
    elif k < 0.60:
        s = rng.choice(
            [
                "echo hi",
                "printf x",
                "echo hi \\> a.txt",
                "echo '>' b.txt",
                "printf '%s' y",
            ]
        )
    elif k < 0.66:
        s = rng.choice(["true", "false"])
    elif k < 0.72:
        s = "cd " + rng.choice(DIRS)
    elif k < 0.76:
        s = rng.choice(
            ["exit 0", "exit 3", "set -e", "export X=1", "unset PATH", "export PATH=/nonexistent", "eval 'cat a.txt'", "source b.txt", ". b.txt"]
        )  # fmt: skip
    elif k < 0.79:
        s = "cat %s <<EOF\nbody\nEOF" % rng.choice(EXISTING)
    elif k < 0.81:
        s = "cat a.txt <<< hello"
    elif k < 0.83:
        s = "echo $(cat %s)" % rng.choice(EXISTING)
    elif k < 0.84:
        s = "echo `cat b.txt`"
    else:
        s = "%s %s" % (rng.choice(READ_PROGS), _quote(rng, rng.choice(EXISTING)))
    if rng.random() < 0.30:
        s += " " + _redirect(rng)
    return s


def gen_command(rng):
    nseg = rng.choice([1, 1, 2, 2, 3, 4])
    out = []
    if rng.random() < 0.20:
        out.append("cd %s && " % rng.choice(["sub", "sub", "nosuch"]))
    for i in range(nseg):
        out.append(_segment(rng))
        if i < nseg - 1:
            sep = rng.choice([";", "&&", "||", "|", "\n"])
            out.append(sep if sep == "\n" or rng.random() < 0.25 else " %s " % sep)
    cmd = "".join(out)
    w = rng.random()
    if w < 0.04:
        cmd = "if true; then %s; fi" % cmd
    elif w < 0.08:
        cmd = "for i in x; do %s; done" % cmd
    elif w < 0.10:
        cmd = "! " + cmd
    elif w < 0.12:
        cmd = "time " + cmd
    elif w < 0.15:
        cmd = "( %s )" % cmd
    elif w < 0.18:
        cmd = "{ %s; }" % cmd
    return cmd


# ------------------------------------------------------------------------------------ harness
def _make_wrappers(tmp: Path):
    wdir = tmp / "wrappers"
    wdir.mkdir()
    made = []
    for name in sorted(wg.READ_CMDS):
        real = shutil.which(name)
        if not real:
            continue
        body = (
            "#!/bin/sh\n"
            'LOG="$WG_ORACLE_LOG"\n'
            'prog=$(basename "$0")\n'
            "skip=0\n"
            'if [ "$prog" = sed ]; then\n'
            "  skip=1\n"
            '  for a in "$@"; do case "$a" in -e|-f|--expression*|--file*) skip=0;; esac; done\n'
            "fi\n"
            'for a in "$@"; do\n'
            '  case "$a" in -*) continue;; esac\n'
            '  if [ "$skip" = 1 ]; then skip=0; continue; fi\n'
            '  if [ -f "$a" ]; then readlink -f -- "$a" >> "$LOG"; fi\n'
            "done\n"
            'exec "%s" "$@"\n' % real
        )
        f = wdir / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
        made.append(name)
    return wdir, made


def _fill(root: Path, n: int):
    if root.exists():
        shutil.rmtree(root)
    (root / "sub").mkdir(parents=True)
    for f in EXISTING:
        (root / f).write_text("MARK-%s-%d\n" % (f, n))


def _snapshot(root: Path):
    return {
        str(p.resolve()): p.read_text(errors="replace")
        for p in root.rglob("*")
        if p.is_file()
    }


def run_oracle(shell_argv, n, effects_fn, tmp: Path):
    """Run n generated commands in a real shell; return a result dict. effects_fn(cmd, cwd) -> (reads,
    overwrites) | None, exactly the shell_effects contract."""
    rng = random.Random(SEED)
    wdir, wrapped = _make_wrappers(tmp)
    log = tmp / "reads.log"
    root = tmp / "work"
    env = dict(os.environ)
    env.pop("CDPATH", None)
    env["PATH"] = str(wdir) + os.pathsep + env.get("PATH", "")
    env["WG_ORACLE_LOG"] = str(log)
    env["LC_ALL"] = "C"
    env["HOME"] = str(tmp)
    res = dict(
        commands=0, with_record=0, read_checks=0, overwrite_checks=0, timeouts=0,
        violations=[], wrapped=wrapped, none_count=0, errors=[],
    )  # fmt: skip
    for i in range(n):
        cmd = gen_command(rng)
        _fill(root, i)
        base = str(root.resolve())
        try:
            eff = effects_fn(cmd, base)
        except Exception as e:  # shell_effects must never raise
            res["errors"].append((cmd, repr(e)))
            continue
        before = _snapshot(root)
        if log.exists():
            log.unlink()
        try:
            subprocess.run(
                shell_argv + [cmd], cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                capture_output=True, timeout=10,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            res["timeouts"] += 1
            continue
        res["commands"] += 1
        if eff is None:
            res["none_count"] += 1
            continue
        reads, overwrites = eff
        if reads or overwrites:
            res["with_record"] += 1
        logged = set(log.read_text().split("\n")) if log.exists() else set()
        after = _snapshot(root)
        for p in reads:
            if not os.path.isfile(p):
                continue  # the post branch only records existing files
            res["read_checks"] += 1
            if os.path.realpath(p) not in logged:
                res["violations"].append(("read", cmd, p))
        for p in overwrites:
            if not os.path.isfile(p):
                continue
            res["overwrite_checks"] += 1
            rp = os.path.realpath(p)
            if rp in before and before[rp] == after.get(rp):
                res["violations"].append(("overwrite", cmd, p))
    res["coverage"] = res["with_record"] / res["commands"] if res["commands"] else 0.0
    return res


def _summary(shell, res):
    line = (
        "WG_ORACLE shell=%s seed=%d commands=%d violations=%d coverage=%.3f none=%d "
        "read_checks=%d overwrite_checks=%d timeouts=%d wrapped=%s"
        % (
            shell, SEED, res["commands"], len(res["violations"]), res["coverage"], res["none_count"],
            res["read_checks"], res["overwrite_checks"], res["timeouts"], ",".join(res["wrapped"]),
        )
    )  # fmt: skip
    print(line)
    return line


def _shell_cases():
    return [
        pytest.param(["bash", "-c"], "bash", id="bash"),
        pytest.param(["zsh", "-f", "-c"], "zsh", id="zsh"),
    ]


@pytest.mark.parametrize("argv,name", _shell_cases())
def test_shell_effects_never_records_what_the_real_shell_did_not_do(
    argv, name, tmp_path
):
    if shutil.which(argv[0]) is None:
        pytest.skip(
            "SKIPPED: %s is not installed on this host; the %s leg of the oracle did not run"
            % (argv[0], name)
        )
    res = run_oracle(argv, N, wg.shell_effects, tmp_path)
    _summary(name, res)
    assert not res["errors"], res["errors"][:5]
    assert res["commands"] > 0.97 * N, "too many timeouts: the corpus did not run"
    assert {"cat", "head", "tail", "sed", "nl", "wc"} <= set(res["wrapped"])
    assert not res["violations"], res["violations"][:20]
    assert res["coverage"] > COVERAGE_FLOOR, (
        "gate is trivially None: coverage=%.3f" % res["coverage"]
    )
    assert res["read_checks"] > 50 and res["overwrite_checks"] > 20


def test_oracle_has_power_it_flags_the_loose_denylist_parser(tmp_path):
    """Meta-test: the same corpus run against the OLD loose reader must produce violations, otherwise
    the oracle proves nothing."""
    if shutil.which("bash") is None:
        pytest.skip("SKIPPED: bash missing")

    def loose(cmd, cwd):
        return wg.shell_reads(cmd, cwd), [
            wg.resolve(p, cwd) for _k, p in wg.shell_overwrites(cmd)
        ]

    res = run_oracle(["bash", "-c"], min(N, 600), loose, tmp_path)
    print("WG_ORACLE_META loose_parser_violations=%d" % len(res["violations"]))
    assert len(res["violations"]) > 0


def test_oracle_generator_is_deterministic():
    a = [gen_command(random.Random(SEED)) for _ in range(1)]
    r1, r2 = random.Random(SEED), random.Random(SEED)
    assert [gen_command(r1) for _ in range(50)] == [gen_command(r2) for _ in range(50)]
    assert a


# ------------------------------------------------------------------------------------ Round 5: hook-level oracle
def _hook_payload(event, sid, cmd, base, call_id):
    return {
        "session_id": sid,
        "cwd": base,
        "hook_event_name": event,
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "tool_use_id": call_id,
        **({"tool_response": "Exit code: 0\n"} if event == "PostToolUse" else {}),
    }


@pytest.mark.parametrize("argv,name", _shell_cases())
def test_hook_never_credits_content_changed_after_the_pre_hook(argv, name, tmp_path, monkeypatch):
    """Drives the real hook, PRE then a real shell then POST (review F1). Two sessions run the same
    command: S1 is POSTed straight away (control: some reads must be credited, or the oracle is vacuous);
    S2 is POSTed after every file was rewritten by 'another session' (no read may be credited)."""
    if shutil.which(argv[0]) is None:
        pytest.skip("SKIPPED: %s is not installed on this host; the %s leg did not run" % (argv[0], name))
    monkeypatch.setenv("MYCELIUM_WRITE_GUARD_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("MYCELIUM_WRITE_GUARD", raising=False)
    rng = random.Random(SEED + 5)
    wdir, _wrapped = _make_wrappers(tmp_path)
    root = tmp_path / "work"
    env = dict(os.environ)
    env.pop("CDPATH", None)
    env["PATH"] = str(wdir) + os.pathsep + env.get("PATH", "")
    env["WG_ORACLE_LOG"] = str(tmp_path / "reads.log")
    env["LC_ALL"] = "C"
    env["HOME"] = str(tmp_path)
    n = min(N, 400)
    credited_when_unchanged = credited_when_changed = checked = 0
    violations = []
    for i in range(n):
        cmd = gen_command(rng)
        _fill(root, i)
        base = str(root.resolve())
        eff = wg.shell_effects(cmd, base)
        reads = [r for r in (eff[0] if eff else []) if os.path.isfile(r)]
        for sid in ("S1", "S2"):
            wg.handle("codex", "pre", _hook_payload("PreToolUse", sid, cmd, base, "c-%d-%s" % (i, sid)), "enforce")
        try:
            subprocess.run(shell_argv_for(argv, cmd), cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=10)  # fmt: skip
        except subprocess.TimeoutExpired:
            continue
        wg.handle("codex", "post", _hook_payload("PostToolUse", "S1", cmd, base, "c-%d-S1" % i), "enforce")
        credited_when_unchanged += sum(1 for r in reads if not wg.is_stale("codex", "S1", r))
        for f in EXISTING:  # another session rewrites everything while the command was "still running"
            (root / f).write_text("OTHER-SESSION-%d\n" % i)
        wg.handle("codex", "post", _hook_payload("PostToolUse", "S2", cmd, base, "c-%d-S2" % i), "enforce")
        for r in reads:
            checked += 1
            if not wg.is_stale("codex", "S2", r):
                credited_when_changed += 1
                violations.append((cmd, r))
    print(
        "WG_ORACLE_HOOK shell=%s seed=%d commands=%d reads_checked=%d credited_unchanged=%d credited_changed=%d"
        % (name, SEED + 5, n, checked, credited_when_unchanged, credited_when_changed)
    )
    assert not violations, violations[:10]
    assert checked > 50 and credited_when_unchanged > 10, "hook oracle is vacuous"


def shell_argv_for(argv, cmd):
    return list(argv) + [cmd]
