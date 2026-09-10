#!/usr/bin/env python3
"""TOML-aware, OWNED-KEY config surgery for r1 native activation (seq36 repair).

Repairs the two defects reproduced in the naive stage-codex-activation.sh helper:
  (a) whole-file `cp` rollback clobbered an unrelated intervening config edit;
  (b) a legal commented table header ("[marketplaces.mycelium] # local registry") was
      missed, so apply appended an INVALID DUPLICATE table and still exited 0.

What this owns (and ONLY this — everything else is copied verbatim, wfctl's ownership model):
  [marketplaces.mycelium]        source, source_type
  [plugins."mycelium@mycelium"]  enabled

Design (tomlkit / tomli_w are NOT available here; tomllib is the VALIDATOR):
  * A small string/comment-aware scanner finds the owned tables' body line ranges. It
    recognizes a header that carries a trailing comment ("[t] # c"), skips any "[" that
    appears inside a comment or a (multiline) string, and never confuses "[[array]]" with a
    plain table. Fix for defect (b): the header IS found, so apply edits in place.
  * apply / rollback edit ONLY owned key lines (replace value / add within the section /
    delete a line or a section we created). Comments, formatting, key order, and every
    unrelated table are preserved byte-for-byte. Fix for defect (a): rollback rewrites only
    owned keys, so unrelated intervening edits survive.
  * Every candidate result is VALIDATED with tomllib BEFORE commit: it must parse (this alone
    rejects the duplicate-table corruption), and reading the owned keys back must equal the
    intended values. Any failure -> nothing is written, non-zero exit (fail closed).
  * Transactional + atomic, mirroring wfctl: snapshot the pre-image, stage to a temp file in
    the same directory, fsync, os.replace. Rollback is OWNED-KEY + CONFLICT-AWARE: if any
    owned key currently differs from the value apply wrote (someone changed it since), it
    REFUSES and writes nothing unless --force; it never restores non-owned bytes.

Exit codes: 0 ok / no-op | 1 conflict or validation failure (nothing written) | 2 usage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover - fail closed on old interpreters
    tomllib = None


class DriftError(Exception):
    """The config changed between the read that produced the new text and the atomic commit."""


# OWNED key slots (the ONLY slots a receipt may ever reference — tamper defense for rollback).
OWNED_SLOTS = frozenset(
    {
        "marketplaces.mycelium::source",
        "marketplaces.mycelium::source_type",
        "plugins.mycelium@mycelium::enabled",
    }
)

# Owned tables -> owned keys. Values are TOML value *tokens* (already quoted/typed).
OWNED_TABLES = ("marketplaces.mycelium", 'plugins."mycelium@mycelium"')
OWNED_TUPLES = {
    ("marketplaces", "mycelium"): ("marketplaces.mycelium", "[marketplaces.mycelium]"),
    ("plugins", "mycelium@mycelium"): (
        'plugins."mycelium@mycelium"',
        '[plugins."mycelium@mycelium"]',
    ),
}


def desired(cand: str) -> dict[tuple, dict[str, str]]:
    """Owned key -> intended TOML value token, keyed by (table tuple)."""
    return {
        ("marketplaces", "mycelium"): {"source": f'"{cand}"', "source_type": '"local"'},
        ("plugins", "mycelium@mycelium"): {"enabled": "true"},
    }


# --------------------------------------------------------------------------- #
# String/comment-aware header scanning
# --------------------------------------------------------------------------- #
def _advance_ml(line: str, in_ml: str | None) -> str | None:
    """Return the multiline-string state at end of `line` given the state at its start."""
    i, n = 0, len(line)
    while i < n:
        if in_ml is not None:
            j = line.find(in_ml, i)
            if j == -1:
                return in_ml
            i = j + 3
            in_ml = None
            continue
        c = line[i]
        if c == "#":
            return None
        if c in "\"'":
            if line[i : i + 3] == c * 3:
                close = line.find(c * 3, i + 3)
                if close == -1:
                    return c * 3
                i = close + 3
                continue
            if c == '"':
                i += 1
                while i < n:
                    if line[i] == "\\":
                        i += 2
                        continue
                    if line[i] == '"':
                        i += 1
                        break
                    i += 1
                continue
            j = line.find("'", i + 1)
            i = (j + 1) if j != -1 else n
            continue
        i += 1
    return in_ml


def _split_keypath(content: str) -> list[str] | None:
    """Split a header's dotted key path, respecting quotes. Returns None if malformed."""
    parts: list[str] = []
    cur: list[str] = []
    i, n = 0, len(content)
    while i < n:
        c = content[i]
        if c in "\"'":
            q = c
            i += 1
            s: list[str] = []
            if q == '"':
                while i < n:
                    if content[i] == "\\":
                        nxt = content[i + 1] if i + 1 < n else ""
                        s.append(
                            {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt)
                        )
                        i += 2
                        continue
                    if content[i] == '"':
                        i += 1
                        break
                    s.append(content[i])
                    i += 1
            else:
                while i < n:
                    if content[i] == "'":
                        i += 1
                        break
                    s.append(content[i])
                    i += 1
            cur.append("".join(s))
        elif c == ".":
            parts.append("".join(cur))
            cur = []
            i += 1
        elif c.isspace():
            i += 1
        else:
            cur.append(c)
            i += 1
    parts.append("".join(cur))
    return parts if all(p != "" for p in parts) else None


def _parse_header(probe: str):
    """probe is left-stripped and starts with '['. Return ('header'|'arrayheader', tuple) or None."""
    if probe.startswith("[["):
        closeb, kind, start = "]]", "arrayheader", 2
    else:
        closeb, kind, start = "]", "header", 1
    i, n = start, len(probe)
    buf: list[str] = []
    closed = False
    while i < n:
        c = probe[i]
        if c in "\"'":
            q = c
            buf.append(c)
            i += 1
            if q == '"':
                while i < n:
                    if probe[i] == "\\":
                        buf.append(probe[i : i + 2])
                        i += 2
                        continue
                    buf.append(probe[i])
                    if probe[i] == '"':
                        i += 1
                        break
                    i += 1
            else:
                while i < n:
                    buf.append(probe[i])
                    if probe[i] == "'":
                        i += 1
                        break
                    i += 1
            continue
        if probe[i : i + len(closeb)] == closeb:
            i += len(closeb)
            closed = True
            break
        buf.append(c)
        i += 1
    if not closed:
        return None
    rest = probe[i:].strip()
    if rest and not rest.startswith("#"):
        return None  # junk after the closing bracket -> not a valid header line
    parts = _split_keypath("".join(buf).strip())
    if not parts:
        return None
    return (kind, tuple(parts))


def line_roles(text: str):
    """Return (lines, roles). roles[i] = ('header'|'arrayheader', keytuple) | ('other', None)."""
    lines = text.splitlines(keepends=True)
    roles = []
    in_ml: str | None = None
    for raw in lines:
        if in_ml is None:
            probe = raw.lstrip().rstrip("\r\n")
            if probe.startswith("["):
                hdr = _parse_header(probe)
                if hdr is not None:
                    roles.append(hdr)
                    continue  # header lines do not open a multiline string
        in_ml = _advance_ml(raw, in_ml)
        roles.append(("other", None))
    return lines, roles


def section_body_range(lines, roles, table_tuple):
    """Return (header_idx, body_start, body_end) for a plain table, or None if absent.

    Raises ValueError if the plain table appears more than once (invalid TOML we refuse to edit).
    """
    hits = [i for i, r in enumerate(roles) if r == ("header", table_tuple)]
    if not hits:
        return None
    if len(hits) > 1:
        raise ValueError(
            f"duplicate table {table_tuple!r} at lines {[h + 1 for h in hits]}"
        )
    h = hits[0]
    body_start = h + 1
    body_end = len(lines)
    for j in range(h + 1, len(lines)):
        k = roles[j][0]
        if k in ("header", "arrayheader"):
            body_end = j
            break
    return (h, body_start, body_end)


# --------------------------------------------------------------------------- #
# Key line editing within a known body range
# --------------------------------------------------------------------------- #
_KEY_RE = re.compile(
    r"^(\s*)([A-Za-z0-9_-]+|\"[^\"]*\"|'[^']*')(\s*=\s*)(.*?)(\r?\n?)$"
)


def _unquote_key(tok: str) -> str:
    if len(tok) >= 2 and tok[0] in "\"'" and tok[-1] == tok[0]:
        return tok[1:-1]
    return tok


def _split_value_comment(rhs: str):
    """Split 'value  # comment' into (value_str, comment_str) using string-aware scan."""
    i, n = 0, len(rhs)
    in_ml = None  # reuse single-line logic; no multiline values in owned keys
    while i < n:
        c = rhs[i]
        if c == "#":
            return rhs[:i].rstrip(), rhs[i:]
        if c in "\"'":
            if c == '"':
                i += 1
                while i < n:
                    if rhs[i] == "\\":
                        i += 2
                        continue
                    if rhs[i] == '"':
                        i += 1
                        break
                    i += 1
                continue
            j = rhs.find("'", i + 1)
            i = (j + 1) if j != -1 else n
            continue
        i += 1
    return rhs.rstrip(), ""


def find_key_line(lines, body_start, body_end, key):
    for j in range(body_start, body_end):
        m = _KEY_RE.match(lines[j])
        if m and _unquote_key(m.group(2)) == key:
            return j, m
    return None, None


# --------------------------------------------------------------------------- #
# tomllib validation helpers
# --------------------------------------------------------------------------- #
def _require_tomllib():
    if tomllib is None:
        sys.stderr.write(
            "FAIL: tomllib unavailable (need Python 3.11+); refusing to edit config blind\n"
        )
        sys.exit(1)


def _parse(text: str):
    return tomllib.loads(text)


def _get_owned(parsed, table_tuple, key):
    node = parsed
    for seg in table_tuple:
        if not isinstance(node, dict) or seg not in node:
            return (False, None)
        node = node[seg]
    if not isinstance(node, dict) or key not in node:
        return (False, None)
    return (True, node[key])


def _token_to_py(tok: str):
    """Interpret an owned value token for semantic comparison against tomllib output."""
    t = tok.strip()
    if t == "true":
        return True
    if t == "false":
        return False
    if len(t) >= 2 and t[0] in "\"'" and t[-1] == t[0]:
        return t[1:-1]
    return t


# --------------------------------------------------------------------------- #
# Compute an edited text applying a set of (table -> {key: token}) operations
# --------------------------------------------------------------------------- #
def _detect_indent(lines, body_start, body_end):
    for j in range(body_start, body_end):
        m = _KEY_RE.match(lines[j])
        if m:
            return m.group(1)
    return ""


def render_header(table_tuple) -> str:
    return OWNED_TUPLES[table_tuple][1]


def apply_ops(text: str, ops: dict[tuple, dict[str, str]]):
    """Return (new_text, changes, created_tables). ops maps table tuple -> {key: token|None}.

    token None means DELETE the key. A table absent from `text` but present in ops (with at
    least one non-None token) is CREATED. Only owned keys are touched.
    """
    lines, roles = line_roles(text)
    changes = []
    created = []
    # Work table by table so ranges stay valid; re-scan after any structural change.
    for table_tuple, kv in ops.items():
        lines, roles = line_roles("".join(lines))
        rng = section_body_range(lines, roles, table_tuple)
        if rng is None:
            adds = {k: v for k, v in kv.items() if v is not None}
            if not adds:
                continue  # nothing to create / delete
            block = ["\n", render_header(table_tuple) + "\n"]
            for k, v in adds.items():
                block.append(f"{k} = {v}\n")
                changes.append((table_tuple, k, "<section absent>", v))
            if lines and not lines[-1].endswith("\n"):
                lines[-1] = lines[-1] + "\n"
            lines = lines + block
            created.append(table_tuple)
            continue
        h, bstart, bend = rng
        indent = _detect_indent(lines, bstart, bend)
        for key, tok in kv.items():
            j, m = find_key_line(lines, bstart, bend, key)
            if tok is None:  # delete
                if j is not None:
                    old_val, _ = _split_value_comment(m.group(4))
                    changes.append((table_tuple, key, old_val, "<deleted>"))
                    del lines[j]
                    bend -= 1
                continue
            if j is not None:
                old_val, comment = _split_value_comment(m.group(4))
                if old_val.strip() == tok.strip():
                    continue  # already at target -> no-op
                newline = m.group(5) or "\n"
                suffix = f"  {comment}" if comment else ""
                lines[j] = f"{m.group(1)}{m.group(2)}{m.group(3)}{tok}{suffix}{newline}"
                changes.append((table_tuple, key, old_val, tok))
            else:  # add within existing section, at end of body
                lines.insert(bend, f"{indent}{key} = {tok}\n")
                changes.append((table_tuple, key, "<absent>", tok))
                bend += 1
    return "".join(lines), changes, created


# --------------------------------------------------------------------------- #
# Atomic write (wfctl pattern: unique staging in same dir -> fsync -> os.replace)
# --------------------------------------------------------------------------- #
def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def atomic_write(path: str, text: str, expected_pre_sha: str | None = None) -> None:
    """Atomically replace `path` with `text`, safely and drift-guarded.

    * Symlink-safe (defect 4): a UNIQUE staging file is created with tempfile.mkstemp (O_EXCL,
      never follows a pre-existing symlink), so a stale/hostile `.<name>.r1act-staging` symlink
      can neither be written through nor become the committed file. A symlinked config is followed
      to its real target so the target is replaced and the link itself is preserved.
    * Drift guard (defect 3): when expected_pre_sha is given, the target is re-read immediately
      before the commit; if it changed since the caller read it (a concurrent/intervening edit),
      DriftError is raised and NOTHING is written — the intervening edit is preserved.
    """
    real = os.path.realpath(
        path
    )  # follow a symlinked config to its real file; preserve the link
    d = os.path.dirname(real) or "."
    try:
        mode = os.stat(real).st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    fd, staging = tempfile.mkstemp(
        dir=d, prefix="." + os.path.basename(real) + ".", suffix=".r1act-staging"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(staging, mode)
        # Test-only fault injection: simulate a concurrent writer landing between render and commit.
        inject = os.environ.get("R1ACT_TEST_INJECT_BEFORE_COMMIT")
        if inject:
            with open(real, "a") as g:
                g.write(inject)
        if expected_pre_sha is not None:
            try:
                cur = open(real).read()
            except FileNotFoundError:
                cur = None
            if cur is None or sha(cur) != expected_pre_sha:
                raise DriftError(
                    f"{path} changed since it was read; refusing to overwrite the intervening edit"
                )
        os.replace(staging, real)
    except BaseException:
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise


def write_receipt_exclusive(path: str, rec: dict) -> None:
    """Write a receipt with EXCLUSIVE creation (defects 1 & 4): the parent must exist/creatable,
    the final path must NOT already exist (no clobber, no write-through-symlink), and the bytes are
    staged uniquely then atomically renamed into place."""
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    if os.path.lexists(path):
        raise FileExistsError(f"refusing to overwrite an existing receipt path: {path}")
    fd, tmp = tempfile.mkstemp(
        dir=parent, prefix="." + os.path.basename(path) + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(rec, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def validate_result(new_text: str, ops: dict[tuple, dict[str, str]]) -> None:
    """Parse the result and assert every owned op took effect. Fail-closed on any problem."""
    try:
        parsed = _parse(new_text)  # rejects duplicate tables, malformed output
    except Exception as e:
        sys.stderr.write(
            f"FAIL: result does not parse as valid TOML (refusing to write): {e}\n"
        )
        sys.exit(1)
    for table_tuple, kv in ops.items():
        for key, tok in kv.items():
            present, val = _get_owned(parsed, table_tuple, key)
            if tok is None:
                if present:
                    sys.stderr.write(
                        f"FAIL: post-condition: {table_tuple}.{key} should be deleted\n"
                    )
                    sys.exit(1)
            else:
                if not present or val != _token_to_py(tok):
                    sys.stderr.write(
                        f"FAIL: post-condition: {table_tuple}.{key} = {val!r} != intended {tok!r}\n"
                    )
                    sys.exit(1)


def cmd_dryrun(cfg: str, cand: str) -> int:
    _require_tomllib()
    text = open(cfg).read()
    try:
        _parse(text)
    except Exception as e:
        sys.stderr.write(
            f"FAIL: current config is not valid TOML; refusing to touch it: {e}\n"
        )
        return 1
    _new, changes, _created = apply_ops(text, desired(cand))
    if not changes:
        print("no changes needed (config already points at the candidate + enabled)")
        return 0
    print("targeted changes (DRY-RUN, nothing written):")
    for tt, k, old, new in changes:
        print(f"  [{OWNED_TUPLES[tt][0]}] {k}: {old} -> {new}")
    return 0


def cmd_apply(cfg: str, cand: str, receipt: str) -> int:
    _require_tomllib()
    # Preflight the receipt path BEFORE any mutation (defect 1): it must be creatable and must NOT
    # already exist (a directory, file, or symlink) — otherwise persisting the record could raise
    # AFTER the config was changed, leaving no rollback receipt.
    if os.path.isdir(receipt):
        sys.stderr.write(f"FAIL: --receipt is an existing directory: {receipt}\n")
        return 1
    if os.path.lexists(receipt):
        sys.stderr.write(
            f"FAIL: --receipt already exists (refusing to overwrite): {receipt}\n"
        )
        return 1
    parent = os.path.dirname(os.path.abspath(receipt)) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as e:
        sys.stderr.write(f"FAIL: cannot create receipt directory {parent}: {e}\n")
        return 1
    if not os.access(parent, os.W_OK):
        sys.stderr.write(f"FAIL: receipt directory not writable: {parent}\n")
        return 1

    text = open(cfg).read()
    pre_sha = sha(text)
    try:
        parsed = _parse(text)
    except Exception as e:
        sys.stderr.write(
            f"FAIL: current config is not valid TOML; refusing to touch it: {e}\n"
        )
        return 1
    ops = desired(cand)
    # Pre-image of owned keys (drives conflict-aware rollback later).
    pre = {}
    for tt, kv in ops.items():
        for key in kv:
            present, val = _get_owned(parsed, tt, key)
            pre[f"{'.'.join(tt)}::{key}"] = {"present": present, "value": val}
    new_text, changes, created = apply_ops(text, ops)
    if not changes:
        print("no changes needed (config already points at the candidate + enabled)")
        return 0
    validate_result(new_text, ops)
    # Post-image of owned keys (what we wrote) — the rollback conflict baseline.
    post_parsed = _parse(new_text)
    post = {}
    for tt, kv in ops.items():
        for key in kv:
            present, val = _get_owned(post_parsed, tt, key)
            post[f"{'.'.join(tt)}::{key}"] = {"present": present, "value": val}
    rec = {
        "schema": "r1-activation-receipt/1",
        "written_at": datetime.now(timezone.utc).isoformat(),
        "config_path": os.path.realpath(cfg),
        "candidate": cand,
        "pre_image_sha256": pre_sha,
        "post_image_sha256": sha(new_text),
        "created_tables": [OWNED_TUPLES[t][0] for t in created],
        "owned_pre": pre,  # values BEFORE apply -> what rollback restores to
        "owned_post": post,  # values AFTER apply -> the rollback conflict baseline
    }
    # Durably persist the receipt BEFORE mutating the config (defect 1). Then commit the config
    # with a drift guard (defect 3): if the config changed since we read it, abort and remove the
    # now-stale receipt so the config is never mutated without a matching, live receipt.
    try:
        write_receipt_exclusive(receipt, rec)
    except (FileExistsError, OSError) as e:
        sys.stderr.write(f"FAIL: could not persist receipt (nothing applied): {e}\n")
        return 1
    try:
        atomic_write(cfg, new_text, expected_pre_sha=pre_sha)
    except DriftError as e:
        try:
            os.unlink(receipt)
        except OSError:
            pass
        sys.stderr.write(f"FAIL: {e} (receipt removed; nothing applied)\n")
        return 1
    except OSError as e:
        try:
            os.unlink(receipt)
        except OSError:
            pass
        sys.stderr.write(
            f"FAIL: config commit failed (receipt removed; nothing applied): {e}\n"
        )
        return 1
    print("targeted changes:")
    for tt, k, old, new in changes:
        print(f"  [{OWNED_TUPLES[tt][0]}] {k}: {old} -> {new}")
    print(f"APPLIED (atomic). receipt: {receipt}")
    print(f"rollback: toml_owned_edit.py rollback {cfg} --receipt {receipt}")
    return 0


def cmd_rollback(cfg: str, receipt: str, force: bool) -> int:
    _require_tomllib()
    if not os.path.isfile(receipt):
        sys.stderr.write(f"FAIL: receipt is not a file: {receipt}\n")
        return 1
    try:
        rec = json.load(open(receipt))
    except (json.JSONDecodeError, OSError) as e:
        sys.stderr.write(f"FAIL: receipt unreadable/malformed: {e}\n")
        return 1
    # Validate the receipt identity/scope BEFORE touching any config (defect 2): a receipt for
    # config A must never mutate config B just because owned values happen to match.
    if rec.get("schema") != "r1-activation-receipt/1":
        sys.stderr.write(f"FAIL: unrecognized receipt schema: {rec.get('schema')!r}\n")
        return 1
    slots = list(rec.get("owned_pre", {})) + list(rec.get("owned_post", {}))
    if not slots:
        sys.stderr.write("FAIL: receipt records no owned keys; nothing to roll back\n")
        return 1
    stray = [s for s in slots if s not in OWNED_SLOTS]
    if stray:
        sys.stderr.write(
            f"FAIL: receipt references non-owned key(s) {stray}; refusing\n"
        )
        return 1
    rec_path = rec.get("config_path")
    if not rec_path or os.path.realpath(cfg) != os.path.realpath(rec_path):
        sys.stderr.write(
            f"FAIL: receipt is for {rec_path!r}, not {os.path.realpath(cfg)!r}; "
            "refusing wrong-config rollback\n"
        )
        return 1
    text = open(cfg).read()
    pre_sha = sha(text)
    try:
        parsed = _parse(text)
    except Exception as e:
        sys.stderr.write(
            f"FAIL: current config is not valid TOML; refusing to touch it: {e}\n"
        )
        return 1
    # Conflict check: every owned key must still equal what apply WROTE (owned_post).
    conflicts = []
    for slot, postrec in rec.get("owned_post", {}).items():
        tt = tuple(slot.split("::")[0].split("."))
        # reconstruct the (table tuple) properly for quoted plugin key
        table_name, key = slot.split("::")
        tt = _name_to_tuple(table_name)
        present, val = _get_owned(parsed, tt, key)
        want_present, want_val = postrec["present"], postrec["value"]
        if present != want_present or (present and val != want_val):
            conflicts.append((slot, {"present": present, "value": val}, postrec))
    if conflicts and not force:
        for slot, cur, want in conflicts:
            sys.stderr.write(
                f"CONFLICT {slot}: now {cur} but apply wrote {want} "
                "(changed since apply — refusing to clobber)\n"
            )
        sys.stderr.write(
            f"rollback refused: {len(conflicts)} owned-key conflict(s); nothing changed "
            "(re-run with --force to override; unrelated edits are always preserved)\n"
        )
        return 1
    # Build rollback ops from the pre-image: restore prior value, or delete if it was absent.
    ops: dict[tuple, dict[str, str]] = {}
    for slot, prerec in rec.get("owned_pre", {}).items():
        table_name, key = slot.split("::")
        tt = _name_to_tuple(table_name)
        ops.setdefault(tt, {})
        if prerec["present"]:
            ops[tt][key] = _py_to_token(prerec["value"])
        else:
            ops[tt][key] = None  # delete key we added
    new_text, changes, _created = apply_ops(text, ops)
    # If we created a table at apply time and it now holds no keys, drop the empty header too.
    new_text = _drop_created_empty_tables(new_text, rec.get("created_tables", []))
    # Validate: result parses, and every restored key matches the pre-image.
    try:
        post_parsed = _parse(new_text)
    except Exception as e:
        sys.stderr.write(
            f"FAIL: rollback result does not parse (refusing to write): {e}\n"
        )
        return 1
    for slot, prerec in rec.get("owned_pre", {}).items():
        table_name, key = slot.split("::")
        tt = _name_to_tuple(table_name)
        present, val = _get_owned(post_parsed, tt, key)
        if prerec["present"]:
            if not present or val != prerec["value"]:
                sys.stderr.write(
                    f"FAIL: rollback post-condition {slot}: {val!r} != pre {prerec['value']!r}\n"
                )
                return 1
        elif present:
            sys.stderr.write(
                f"FAIL: rollback post-condition {slot}: key should be absent again\n"
            )
            return 1
    try:
        atomic_write(cfg, new_text, expected_pre_sha=pre_sha)
    except DriftError as e:
        sys.stderr.write(f"FAIL: {e} (nothing rolled back)\n")
        return 1
    print("rollback applied (owned keys only; unrelated edits preserved):")
    for tt, k, old, new in changes:
        print(f"  [{OWNED_TUPLES[tt][0]}] {k}: {old} -> {new}")
    print(f"ROLLED BACK (atomic) from receipt: {receipt}")
    return 0


def _name_to_tuple(table_name: str) -> tuple:
    if table_name == "marketplaces.mycelium":
        return ("marketplaces", "mycelium")
    if table_name == "plugins.mycelium@mycelium":
        return ("plugins", "mycelium@mycelium")
    # generic fallback: split respecting the single quoted plugin case
    return tuple(table_name.split("."))


def _py_to_token(val) -> str:
    if val is True:
        return "true"
    if val is False:
        return "false"
    if isinstance(val, str):
        return f'"{val}"'
    return str(val)


def _drop_created_empty_tables(text: str, created_names: list[str]) -> str:
    if not created_names:
        return text
    lines, roles = line_roles(text)
    drop_ranges = []
    for name in created_names:
        tt = _name_to_tuple(name)
        rng = section_body_range(lines, roles, tt)
        if rng is None:
            continue
        h, bstart, bend = rng
        # only drop if the body has no key=value lines at all (we own the whole created table)
        has_key = any(_KEY_RE.match(lines[j]) for j in range(bstart, bend))
        if not has_key:
            start = h
            # also swallow a single blank separator line we inserted right before the header
            if start > 0 and lines[start - 1].strip() == "":
                start -= 1
            drop_ranges.append((start, bend))
    if not drop_ranges:
        return text
    keep = []
    drop_set = set()
    for a, b in drop_ranges:
        drop_set.update(range(a, b))
    for i, ln in enumerate(lines):
        if i not in drop_set:
            keep.append(ln)
    return "".join(keep)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="TOML-aware owned-key config surgery (r1 seq36)."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("dryrun")
    p.add_argument("config")
    p.add_argument("candidate")
    p = sub.add_parser("apply")
    p.add_argument("config")
    p.add_argument("candidate")
    p.add_argument("--receipt", required=True)
    p = sub.add_parser("rollback")
    p.add_argument("config")
    p.add_argument("--receipt", required=True)
    p.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "dryrun":
        return cmd_dryrun(args.config, args.candidate)
    if args.cmd == "apply":
        return cmd_apply(args.config, args.candidate, args.receipt)
    if args.cmd == "rollback":
        return cmd_rollback(args.config, args.receipt, args.force)
    return 2


if __name__ == "__main__":
    sys.exit(main())
