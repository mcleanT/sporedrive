#!/usr/bin/env python3
"""Durable, guarded ADDITIVE enable/disable toggle for `[plugins."<id>"].enabled`.

Built for the additive personal-marketplace activation method: `mycelium@personal` is
installed in a separate cache namespace alongside the old `mycelium@mycelium`, which is
DISABLED (not removed) via this same tool. Reuses the SAME guarded patterns as
scripts/toml_owned_edit.py, adapted to a single, arbitrary, already-existing owned key:

  * string/comment-aware line-role scanning to find an EXISTING table's body range
    (never confused by a commented header, a multiline string, or an [[array]] header) —
    ported from toml_owned_edit.py's line_roles/_parse_header/_advance_ml/_split_keypath.
  * the config file's ORIGINAL permission mode is preserved across the atomic replace:
    st_mode is captured BEFORE mutation and os.chmod() is applied AFTER os.replace().
    THIS IS THE KEY REQUIREMENT — a prior scratch script lost 0600->0644 by writing
    through a default-mode temp file and never restoring the original mode.
  * symlink-safe: a symlinked config is followed (os.path.realpath) to its real target
    for staging + replace; the symlink itself is left untouched.
  * atomic write: a UNIQUELY created temp file (tempfile.mkstemp, O_EXCL) in the same
    directory as the real target, fsync'd, then os.replace() into place.
  * a receipt (target path, owned key, pre-value, post-value, pre-sha256) is written
    BEFORE mutation, with EXCLUSIVE creation, so a crash between receipt and commit
    never leaves the config mutated without a live receipt to undo it.
  * a DRIFT GUARD immediately before commit re-reads the owned key's LIVE value and
    refuses (exit 1, writes nothing) if it no longer matches what this operation read
    at the start — distinct from, and narrower than, toml_owned_edit's whole-file sha
    drift guard: only the owned key's value is required to be stable.
  * rollback is OWNED-KEY + CONFLICT-AWARE: it restores only the single key it owns and
    refuses (unless --force) if that key changed since apply — never a blind whole-file
    restore over an unrelated, later edit.
  * the result is (a) parsed with tomllib and (b) diffed against the pre-image with the
    owned key normalized out of both parsed dicts, so ANY collateral change elsewhere in
    the file fails the write closed.

Scope, deliberately narrower than toml_owned_edit.py: this tool NEVER creates a table.
It only edits the `enabled` key of an EXISTING `[plugins."<id>"]` table (expected to
already exist post-install, since `codex plugin add`/install creates it). If the table
is absent, apply/rollback error clearly and change nothing.

Exit codes: 0 ok/no-op | 1 conflict, drift, or validation failure (nothing written) | 2 usage.
"""

from __future__ import annotations

import argparse
import copy
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

SCHEMA = "additive-plugin-toggle-receipt/1"
OWNED_KEY = "enabled"


class DriftError(Exception):
    """The owned key's live value no longer matches what this operation expects to change."""


# --------------------------------------------------------------------------- #
# String/comment-aware header scanning (ported from toml_owned_edit.py; table-agnostic
# already in that file — reused here verbatim so this script is self-contained).
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


_KEY_RE = re.compile(
    r"^(\s*)([A-Za-z0-9_-]+|\"[^\"]*\"|'[^']*')(\s*=\s*)(.*?)(\r?\n?)$"
)


def _unquote_key(tok: str) -> str:
    if len(tok) >= 2 and tok[0] in "\"'" and tok[-1] == tok[0]:
        return tok[1:-1]
    return tok


def _split_value_comment(rhs: str):
    """Split 'value  # comment' into (value_str, comment_str) using a string-aware scan."""
    i, n = 0, len(rhs)
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


def _detect_indent(lines, body_start, body_end):
    for j in range(body_start, body_end):
        m = _KEY_RE.match(lines[j])
        if m:
            return m.group(1)
    return ""


_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _quote_seg(seg: str) -> str:
    if _BARE_KEY_RE.match(seg):
        return seg
    return '"' + seg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_header(table_tuple) -> str:
    return "[" + ".".join(_quote_seg(s) for s in table_tuple) + "]"


# --------------------------------------------------------------------------- #
# tomllib helpers
# --------------------------------------------------------------------------- #
def _require_tomllib():
    if tomllib is None:
        sys.stderr.write(
            "FAIL: tomllib unavailable (need Python 3.11+); refusing to edit config blind\n"
        )
        sys.exit(1)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _get_key(parsed, table_tuple, key):
    node = parsed
    for seg in table_tuple:
        if not isinstance(node, dict) or seg not in node:
            return (False, None)
        node = node[seg]
    if not isinstance(node, dict) or key not in node:
        return (False, None)
    return (True, node[key])


def _bool_token(v: bool) -> str:
    return "true" if v else "false"


def _normalize_for_diff(parsed, table_tuple, key):
    """Deep-copy `parsed` and drop the owned key so the rest can be compared for equality."""
    d = copy.deepcopy(parsed)
    node = d
    for seg in table_tuple:
        if not isinstance(node, dict) or seg not in node:
            return d
        node = node[seg]
    if isinstance(node, dict):
        node.pop(key, None)
    return d


def validate_only_owned_changed(
    old_text: str,
    new_text: str,
    table_tuple,
    key: str,
    expected_present: bool,
    expected_value,
) -> None:
    """Parse the result, assert the owned key matches what was intended, and assert that
    NOTHING ELSE in the parsed structure differs from the pre-image (owned key normalized
    out of both sides). Raises ValueError on any failure; callers must write nothing."""
    try:
        new_parsed = tomllib.loads(new_text)
    except Exception as e:
        raise ValueError(
            f"result does not parse as valid TOML (refusing to write): {e}"
        )
    present, val = _get_key(new_parsed, table_tuple, key)
    if present != expected_present or (expected_present and val != expected_value):
        raise ValueError(
            f"post-condition failed: {'.'.join(table_tuple)}.{key} "
            f"present={present} val={val!r} != expected present={expected_present} "
            f"val={expected_value!r}"
        )
    old_parsed = tomllib.loads(old_text)  # already validated readable at read time
    old_norm = _normalize_for_diff(old_parsed, table_tuple, key)
    new_norm = _normalize_for_diff(new_parsed, table_tuple, key)
    if old_norm != new_norm:
        raise ValueError(
            "collateral change detected: something beyond the owned key changed "
            "(refusing to write)"
        )


def set_key_in_text(text: str, table_tuple, key: str, token: str | None):
    """Return (new_text, old_value_str_or_None, changed_bool).

    token=None means DELETE the key line (no-op if already absent). Otherwise the key is
    replaced in place if present, else appended within the existing body. The TABLE must
    already exist -> raises LookupError if absent (this tool never creates a table).
    """
    lines, roles = line_roles(text)
    rng = section_body_range(lines, roles, table_tuple)
    if rng is None:
        raise LookupError(f"table {render_header(table_tuple)} not found")
    h, bstart, bend = rng
    j, m = find_key_line(lines, bstart, bend, key)
    if token is None:
        if j is None:
            return text, None, False
        old_val, _ = _split_value_comment(m.group(4))
        del lines[j]
        return "".join(lines), old_val, True
    if j is not None:
        old_val, comment = _split_value_comment(m.group(4))
        if old_val.strip() == token.strip():
            return text, old_val, False
        newline = m.group(5) or "\n"
        suffix = f"  {comment}" if comment else ""
        lines[j] = f"{m.group(1)}{m.group(2)}{m.group(3)}{token}{suffix}{newline}"
        return "".join(lines), old_val, True
    indent = _detect_indent(lines, bstart, bend)
    lines.insert(bend, f"{indent}{key} = {token}\n")
    return "".join(lines), None, True


# --------------------------------------------------------------------------- #
# Atomic write — mode-preserving (THE key requirement), symlink-safe
# --------------------------------------------------------------------------- #
def atomic_write(path: str, text: str) -> None:
    """Atomically replace `path` with `text`.

    * Symlink-safe: a symlinked config is followed (os.path.realpath) to its real target;
      the target is replaced and the symlink itself is preserved untouched.
    * Staging is a UNIQUELY created temp file (tempfile.mkstemp, O_EXCL) in the same
      directory as the real target, so a stale/hostile fixed-name staging path can never
      be written through or become the committed file.
    * MODE PRESERVATION (the key requirement): the real target's permission mode is
      captured with os.stat() BEFORE any mutation, and re-applied with os.chmod() AFTER
      os.replace() lands the staged content — so the file's original mode (e.g. 0600)
      survives the atomic replace regardless of the staging file's own creation mode.
    """
    real = os.path.realpath(path)
    d = os.path.dirname(real) or "."
    try:
        st_mode = os.stat(real).st_mode & 0o777
    except FileNotFoundError:
        st_mode = None
    fd, staging = tempfile.mkstemp(
        dir=d, prefix="." + os.path.basename(real) + ".", suffix=".apt-staging"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(staging, real)
        if st_mode is not None:
            os.chmod(real, st_mode)  # PRESERVE original mode across the atomic replace
    except BaseException:
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise


def write_receipt_exclusive(path: str, rec: dict) -> None:
    """Write a receipt with EXCLUSIVE creation: the final path must NOT already exist (no
    clobber, no write-through-symlink); bytes are staged uniquely then atomically renamed."""
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


def _current_owned(cfg: str, table_tuple, key: str):
    """Re-read the live config and return (present, value) for the owned key. Raises on any
    read/parse failure — callers treat that as an unconditional drift/abort signal."""
    text = open(cfg).read()
    parsed = tomllib.loads(text)
    return _get_key(parsed, table_tuple, key)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_apply(cfg: str, plugin_id: str, enabled: bool, receipt: str) -> int:
    _require_tomllib()
    # Preflight the receipt path BEFORE any mutation: creatable, and must NOT already exist.
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

    table_tuple = ("plugins", plugin_id)
    key = OWNED_KEY
    token = _bool_token(enabled)

    try:
        text = open(cfg).read()
    except OSError as e:
        sys.stderr.write(f"FAIL: cannot read config {cfg}: {e}\n")
        return 1
    pre_sha = sha(text)
    try:
        parsed = tomllib.loads(text)
    except Exception as e:
        sys.stderr.write(
            f"FAIL: current config is not valid TOML; refusing to touch it: {e}\n"
        )
        return 1
    present, pre_val = _get_key(parsed, table_tuple, key)

    try:
        new_text, old_val, changed = set_key_in_text(text, table_tuple, key, token)
    except LookupError as e:
        sys.stderr.write(
            f"FAIL: {e} (table must already exist; this tool never creates it)\n"
        )
        return 1

    if not changed:
        print(f"no changes needed ({render_header(table_tuple)}.{key} already {token})")
        return 0

    try:
        validate_only_owned_changed(text, new_text, table_tuple, key, True, enabled)
    except ValueError as e:
        sys.stderr.write(f"FAIL: {e}\n")
        return 1

    rec = {
        "schema": SCHEMA,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "config_path": os.path.realpath(cfg),
        "plugin_id": plugin_id,
        "owned_key": f"plugins.{plugin_id}::{key}",
        "pre_present": present,
        "pre_value": pre_val if present else None,
        "post_value": enabled,
        "pre_sha256": pre_sha,
    }
    # Durably persist the receipt BEFORE mutating the config, so a crash between the two never
    # mutates config without a matching, live receipt.
    try:
        write_receipt_exclusive(receipt, rec)
    except (FileExistsError, OSError) as e:
        sys.stderr.write(f"FAIL: could not persist receipt (nothing applied): {e}\n")
        return 1

    # Test-only fault injection: simulate a concurrent writer flipping the owned key between
    # the initial read and the drift-guard re-check below.
    _inject = os.environ.get("APT_TEST_INJECT_TOGGLE_BEFORE_COMMIT")
    if _inject in ("true", "false"):
        _cur = open(cfg).read()
        _mut, _, _ = set_key_in_text(
            _cur, table_tuple, key, _bool_token(_inject == "true")
        )
        atomic_write(cfg, _mut)

    # DRIFT GUARD: immediately before commit, re-read the LIVE owned key and confirm it still
    # equals what this apply read at the start. Refuse (nothing written) if it changed.
    try:
        cur_present, cur_val = _current_owned(cfg, table_tuple, key)
    except Exception as e:
        try:
            os.unlink(receipt)
        except OSError:
            pass
        sys.stderr.write(
            f"FAIL: config unreadable/unparsable immediately before commit: {e} "
            "(receipt removed; nothing applied)\n"
        )
        return 1
    if cur_present != present or (present and cur_val != pre_val):
        try:
            os.unlink(receipt)
        except OSError:
            pass
        sys.stderr.write(
            f"FAIL: plugins.{plugin_id}::{key} changed since it was read "
            f"(now present={cur_present} value={cur_val!r}); refusing to clobber "
            "(receipt removed; nothing applied)\n"
        )
        return 1

    try:
        atomic_write(cfg, new_text)
    except OSError as e:
        try:
            os.unlink(receipt)
        except OSError:
            pass
        sys.stderr.write(
            f"FAIL: config commit failed (receipt removed; nothing applied): {e}\n"
        )
        return 1

    print(f"[{plugin_id}] {key}: {old_val!r} -> {token}")
    print(f"APPLIED (atomic, mode-preserved). receipt: {receipt}")
    print(f"rollback: additive_plugin_toggle.py rollback {cfg} --receipt {receipt}")
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
    if rec.get("schema") != SCHEMA:
        sys.stderr.write(f"FAIL: unrecognized receipt schema: {rec.get('schema')!r}\n")
        return 1
    plugin_id = rec.get("plugin_id")
    if not plugin_id:
        sys.stderr.write("FAIL: receipt missing plugin_id\n")
        return 1
    rec_path = rec.get("config_path")
    if not rec_path or os.path.realpath(cfg) != os.path.realpath(rec_path):
        sys.stderr.write(
            f"FAIL: receipt is for {rec_path!r}, not {os.path.realpath(cfg)!r}; "
            "refusing wrong-config rollback\n"
        )
        return 1

    table_tuple = ("plugins", plugin_id)
    key = OWNED_KEY

    try:
        text = open(cfg).read()
    except OSError as e:
        sys.stderr.write(f"FAIL: cannot read config {cfg}: {e}\n")
        return 1
    try:
        parsed = tomllib.loads(text)
    except Exception as e:
        sys.stderr.write(
            f"FAIL: current config is not valid TOML; refusing to touch it: {e}\n"
        )
        return 1

    present, val = _get_key(parsed, table_tuple, key)
    post_present, post_value = True, rec.get("post_value")
    # CONFLICT CHECK: the owned key must still equal what apply WROTE, else someone changed it
    # since apply — refuse (never a blind whole-file restore) unless --force.
    if present != post_present or val != post_value:
        if not force:
            sys.stderr.write(
                f"CONFLICT: plugins.{plugin_id}::{key} is now present={present} value={val!r}, "
                f"but apply wrote {post_value!r} (changed since apply — refusing to clobber; "
                "use --force to override)\n"
            )
            return 1

    pre_present = rec.get("pre_present", True)
    pre_value = rec.get("pre_value")
    token = None if not pre_present else _bool_token(bool(pre_value))

    try:
        new_text, old_val, changed = set_key_in_text(text, table_tuple, key, token)
    except LookupError as e:
        sys.stderr.write(f"FAIL: {e}\n")
        return 1

    if not changed:
        label = "absent" if token is None else token
        print(f"no changes needed (plugins.{plugin_id}::{key} already {label})")
        return 0

    try:
        validate_only_owned_changed(
            text,
            new_text,
            table_tuple,
            key,
            pre_present,
            bool(pre_value) if pre_present else None,
        )
    except ValueError as e:
        sys.stderr.write(f"FAIL: {e}\n")
        return 1

    # DRIFT GUARD: immediately before commit, re-read the LIVE owned key and confirm it still
    # matches what the conflict check above just observed (guards a race right up to commit).
    try:
        cur_present, cur_val = _current_owned(cfg, table_tuple, key)
    except Exception as e:
        sys.stderr.write(
            f"FAIL: config unreadable/unparsable immediately before commit: {e} "
            "(nothing rolled back)\n"
        )
        return 1
    if cur_present != present or (present and cur_val != val):
        sys.stderr.write(
            f"FAIL: plugins.{plugin_id}::{key} changed immediately before commit "
            "(nothing rolled back)\n"
        )
        return 1

    try:
        atomic_write(cfg, new_text)
    except OSError as e:
        sys.stderr.write(f"FAIL: rollback commit failed: {e}\n")
        return 1

    label = "<deleted>" if token is None else token
    print(f"[{plugin_id}] {key}: {old_val!r} -> {label}")
    print(f"ROLLED BACK (atomic, mode-preserved) from receipt: {receipt}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='Additive guarded [plugins."<id>"].enabled toggle (apply/rollback).'
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("apply")
    p.add_argument("config")
    p.add_argument("--plugin-id", required=True)
    p.add_argument("--enabled", required=True, choices=["true", "false"])
    p.add_argument("--receipt", required=True)
    p = sub.add_parser("rollback")
    p.add_argument("config")
    p.add_argument("--receipt", required=True)
    p.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "apply":
        return cmd_apply(
            args.config, args.plugin_id, args.enabled == "true", args.receipt
        )
    if args.cmd == "rollback":
        return cmd_rollback(args.config, args.receipt, args.force)
    return 2


if __name__ == "__main__":
    sys.exit(main())
