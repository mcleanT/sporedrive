"""Model routing policy for Codex-driven Claude Code sessions (owner rule, 2026-09-20).

The owner's rule: **Fable is planning-only.** Implementation runs on an explicitly chosen Anthropic
implementation model — Opus by default, Sonnet or Haiku when the operator picks one — and never on
a model silently inherited from `~/.claude/settings.json` (whose default is Fable) or on a session
that is, or was, the owner's Fable planning session.

This module is the single source of truth for that policy. It is imported by the bridge
(`core.bind` / `core.submit` gate writer bindings by purpose and model) and mirrored VERBATIM
into the installed Codex skill script `src/codex/skills/cmux-driver/scripts/executor_session.py`
(which cannot import this package once installed under ~/.codex). The block between the
`# --- model policy: begin ---` / `# --- model policy: end ---` markers must stay byte-identical in
both files; `bridge/tests/test_model_routing.py::test_launcher_mirrors_policy_module` enforces it.

Three independent kinds of model evidence exist for a running session, and the policy treats them
differently because they answer different questions:

* the **footer** (`Model: Opus 5 | … Ctx Used:`) is the model the session will use for its NEXT
  turn — the live, resolved value; it is the gate;
* the process **argv** (`claude … --model opus …`) is what was REQUESTED at launch; a missing
  `--model` means the session inherited the settings default; a Fable argv means the session was
  started as a planning session even if `/model` switched it later;
* the **transcript** (`message.model` on assistant entries) is what actually answered; a Fable
  entry means Fable context was built in this session.

Fable in ANY of the three disqualifies a session for implementation: the footer says it is on
Fable now; argv/transcript say it is a planning session being reused. Neither the pane's window
nor its focus state is evidence of anything here (UI placement is not a security boundary).
"""

from __future__ import annotations

import re

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
