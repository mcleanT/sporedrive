"""Routine workers (efficiency v2, brief item 1): an explicit, supported launch path for a bounded
low-effort worker (profile ``routine`` = ``gpt-5.6-luna`` @ ``low``) on top of owned jobs.

* ``run`` is CLI-only and reuses :class:`JobManager` (managed reservation, immutable request
  record, stdout/stderr on disk). Nothing here writes ``~/.codex/config.toml`` or any global
  default: the model and effort are passed per invocation only.
* The worker starts from a compact fresh context: a bounded prompt FILE (<= 16 KiB) with a fixed
  preamble, never a transcript. Recursive delegation and housekeeping are forbidden by env
  (``MYCELIUM_ROUTINE_WORKER`` / ``MYCELIUM_NO_DELEGATE`` / ``MYCELIUM_NO_HOUSEKEEPING``) and by
  ``codex --disable multi_agent --disable hooks``.
* ``result`` validates the required output deterministically (file exists, JSON parses, required
  keys present, sha256 recorded) and records provenance (requested vs. resolved model). An
  unresolved failure writes exactly ONE ``escalation.json``; it is never retried with another model.

Records live under ``<coord-root>/workers/<worker_id>/`` (worker.json, prompt.txt, output.json,
result.json, escalation.json); the job itself is ``<coord-root>/jobs/<worker_id>/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from .jobs import DEFAULT_DEADLINE_S, DEFAULT_TAIL_BYTES, JobManager
from .model import ProtocolError, utcnow
from .store import CoordStore, require_id

WORKER_SCHEMA_VERSION = "1.0.0"
PROFILES = {"routine": {"model": "gpt-5.6-luna", "effort": "low"}}
MAX_PROMPT_BYTES = 16 * 1024
WORKER_ENV = {"MYCELIUM_ROUTINE_WORKER": "1", "MYCELIUM_NO_DELEGATE": "1", "MYCELIUM_NO_HOUSEKEEPING": "1"}
CODEX_DISABLED_FEATURES = ("multi_agent", "hooks")
EXPECT_KINDS = ("json", "file")
PREAMBLE = (
    "You are a bounded ROUTINE WORKER with a fresh, compact context. Rules: do not delegate, spawn "
    "subagents, or start other agents; do not run housekeeping, compaction, or knowledge recording; "
    "do not read transcripts or session history; do not modify configuration. Do only the task "
    "below and write ONLY the requested output, exactly in the requested format, as your final "
    "message. If the task cannot be completed, say so plainly in the output instead of guessing.\n"
    "---- TASK ----\n"
)
_BANNER_MODEL_RE = re.compile(r"^\s*model:\s*([A-Za-z0-9._-]+)\s*$", re.IGNORECASE)


class WorkerError(ProtocolError):
    """Codes: unknown_profile, invalid_prompt, prompt_too_large, invalid_expect, worker_not_found,
    worker_not_terminal."""


def _req(cond: bool, code: str, message: str) -> None:
    if not cond:
        raise WorkerError(code, "", message)


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class WorkerManager:
    def __init__(self, store: CoordStore | None = None, jobs: JobManager | None = None):
        self.store = store or CoordStore()
        self.jm = jobs or JobManager(self.store)

    def _rel(self, worker_id: str, name: str = "") -> str:
        base = f"workers/{require_id(worker_id, 'worker_id')}"
        return f"{base}/{name}" if name else base

    def _dir(self, worker_id: str) -> Path:
        return self.store.path(self._rel(worker_id))

    def read_record(self, worker_id: str):
        return self.store.read(self._rel(worker_id, "worker.json"))

    # ------------------------------------------------------------------ run (CLI-only)
    def run(self, worker_id: str, prompt_path, *, profile: str = "routine", output_path=None,
            require_keys=(), expect: str = "json", task_id=None, action_id=None,
            dispatch_identity=None, deadline_s: float = DEFAULT_DEADLINE_S, label=None,
            join_s: float = 0.0, tail_bytes: int = DEFAULT_TAIL_BYTES, codex_bin: str = "codex") -> dict:
        require_id(worker_id, "worker_id")
        _req(profile in PROFILES, "unknown_profile", f"profile must be one of {sorted(PROFILES)}")
        _req(expect in EXPECT_KINDS, "invalid_expect", f"expect must be one of {EXPECT_KINDS}")
        prof = PROFILES[profile]
        prompt_path = os.path.abspath(str(prompt_path))
        _req(os.path.isfile(prompt_path), "invalid_prompt", f"prompt file {prompt_path!r} not found")
        raw = Path(prompt_path).read_bytes()
        _req(len(raw) <= MAX_PROMPT_BYTES, "prompt_too_large",
             f"prompt is {len(raw)} bytes; the routine-worker cap is {MAX_PROMPT_BYTES} bytes")
        _req(len(raw.strip()) > 0, "invalid_prompt", "prompt file is empty")
        require_keys = [str(k) for k in (require_keys or ())]
        wdir = self._dir(worker_id)
        existing = self.read_record(worker_id)
        if existing is None:
            wdir.mkdir(parents=True, exist_ok=True)
            os.chmod(wdir, 0o700)
            workdir = wdir / "work"
            workdir.mkdir(exist_ok=True)  # fresh, empty working directory: no transcript, no repo
            prompt_bytes = PREAMBLE.encode("utf-8") + raw
            prompt_file = wdir / "prompt.txt"
            prompt_file.write_bytes(prompt_bytes)
            os.chmod(prompt_file, 0o600)
            out_path = os.path.abspath(str(output_path)) if output_path else str(wdir / "output.json")
            argv = [str(codex_bin), "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral"]
            for feat in CODEX_DISABLED_FEATURES:
                argv += ["--disable", feat]
            argv += ["-m", prof["model"], "-c", 'model_reasoning_effort="%s"' % prof["effort"],
                     "-C", str(workdir), "-o", out_path, "-"]
            rec = {
                "schema_version": WORKER_SCHEMA_VERSION, "worker_id": worker_id, "job_id": worker_id,
                "profile": profile, "model_requested": prof["model"], "effort_requested": prof["effort"],
                "codex_bin": str(codex_bin), "argv": argv, "env": dict(WORKER_ENV),
                "prompt_source": prompt_path, "prompt_path": str(prompt_file),
                "prompt_sha256": _sha256_bytes(prompt_bytes), "prompt_bytes": len(prompt_bytes),
                "output_path": out_path, "expect": expect, "require_keys": require_keys,
                "workdir": str(workdir), "task_id": task_id, "action_id": action_id,
                "dispatch_identity": dispatch_identity, "created_at": utcnow(),
            }
            self.store.write(self._rel(worker_id, "worker.json"), rec)
        else:
            rec, workdir = existing, Path(existing["workdir"])
        launch = [sys.executable, "-m", "mycelium_coord.workers", "--exec", str(wdir)]
        receipt = self.jm.run(worker_id, launch, cwd=str(workdir), deadline_s=deadline_s, task_id=task_id,
                              action_id=action_id, dispatch_identity=dispatch_identity, on_stop="keep",
                              label=label or f"worker:{profile}", join_s=join_s, tail_bytes=tail_bytes)
        receipt.update({"worker_id": worker_id, "profile": rec["profile"], "model_requested": rec["model_requested"],
                        "effort_requested": rec["effort_requested"], "output_path": rec["output_path"],
                        "worker_record": str(wdir / "worker.json")})
        return receipt

    # ------------------------------------------------------------------ result (read + validate)
    def _banner(self, jobdir: Path) -> dict:
        """Actual identity from the codex startup banner (``model: <slug>``, ``reasoning effort:
        <e>``, ``OpenAI Codex v<ver>``) in the first 20 lines of the worker's own stderr (codex
        prints its banner there) or stdout; absent → None, never fabricated from the request."""
        out = {"model_resolved": None, "effort_resolved": None, "codex_version": None}
        for name in ("stderr.log", "stdout.log"):
            log = jobdir / name
            if not log.is_file():
                continue
            with open(log, "r", encoding="utf-8", errors="replace") as f:
                for _ in range(20):
                    line = f.readline()
                    if not line:
                        break
                    m = _BANNER_MODEL_RE.match(line)
                    if m and out["model_resolved"] is None:
                        out["model_resolved"] = m.group(1)
                    m = re.match(r"^\s*reasoning effort:\s*(\S+)\s*$", line)
                    if m and out["effort_resolved"] is None:
                        out["effort_resolved"] = m.group(1)
                    m = re.match(r"^\s*OpenAI Codex v(\S+)", line)
                    if m and out["codex_version"] is None:
                        out["codex_version"] = m.group(1)
            if out["model_resolved"] is not None:
                break
        return out

    def _model_resolved(self, stdout_log: Path):
        return self._banner(stdout_log.parent)["model_resolved"]

    def _validate(self, rec: dict) -> dict:
        problems, out = [], {"output_sha256": None, "output_bytes": None}
        p = Path(rec["output_path"])
        if not p.is_file():
            problems.append(f"output_missing: {p}")
            return dict(out, ok=False, problems=problems)
        data = p.read_bytes()
        out.update({"output_sha256": _sha256_bytes(data), "output_bytes": len(data)})
        if len(data.strip()) == 0:
            problems.append("output_empty")
        elif rec.get("expect") == "json":
            try:
                obj = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                problems.append(f"output_not_json: {type(e).__name__}: {e}")
            else:
                if rec.get("require_keys"):
                    if not isinstance(obj, dict):
                        problems.append("output_not_object")
                    else:
                        for k in rec["require_keys"]:
                            if k not in obj:
                                problems.append(f"missing_key: {k}")
        return dict(out, ok=not problems, problems=problems)

    def result(self, worker_id: str) -> dict:
        rec = self.read_record(worker_id)
        if rec is None:
            raise WorkerError("worker_not_found", worker_id, f"no worker {worker_id!r}")
        stored = self.store.read(self._rel(worker_id, "result.json"))
        if stored is not None:  # terminal outcome already recorded: never recompute, never relaunch
            return stored
        st = self.jm.status(rec["job_id"], tail_bytes=0)
        if not st.get("terminal"):
            raise WorkerError("worker_not_terminal", worker_id,
                              f"job {rec['job_id']!r} is {st.get('effective_status')}; join it first")
        wdir = self._dir(worker_id)
        jobdir = Path(st["outputs"]["evidence_dir"])
        val = self._validate(rec)
        exit_code = st.get("exit_code")
        process_ok = st.get("effective_status") == "exited" and exit_code == 0
        if not process_ok:
            val["problems"].insert(0, f"process: status={st.get('effective_status')} exit_code={exit_code} "
                                      f"reason={st.get('reason')}")
            val["ok"] = False
        banner = self._banner(jobdir)
        res = {
            "schema_version": WORKER_SCHEMA_VERSION, "worker_id": worker_id, "job_id": rec["job_id"],
            "profile": rec["profile"], "model_requested": rec["model_requested"],
            "effort_requested": rec["effort_requested"], "model_resolved": banner["model_resolved"],
            "effort_resolved": banner["effort_resolved"], "codex_version": banner["codex_version"],
            "output_path": rec["output_path"], "output_sha256": val["output_sha256"],
            "output_bytes": val["output_bytes"], "validation": {"ok": val["ok"], "problems": val["problems"]},
            "exit_code": exit_code, "job_status": st.get("effective_status"), "elapsed_s": st.get("elapsed_s"),
            "stdout_sha256": st.get("stdout_sha256"), "stderr_sha256": st.get("stderr_sha256"),
            "prompt_sha256": rec["prompt_sha256"], "escalated": not val["ok"], "escalation_path": None,
            "recorded_at": utcnow(),
        }
        if res["escalated"]:
            esc_rel = self._rel(worker_id, "escalation.json")
            if not self.store.exists(esc_rel):  # exactly one record; never rewritten, never retried
                self.store.write(esc_rel, {
                    "schema_version": WORKER_SCHEMA_VERSION, "worker_id": worker_id, "job_id": rec["job_id"],
                    "created_at": utcnow(), "failure": {"job_status": st.get("effective_status"),
                                                        "exit_code": exit_code, "problems": val["problems"]},
                    "model_requested": rec["model_requested"], "effort_requested": rec["effort_requested"],
                    "model_resolved": res["model_resolved"],
                    "evidence": {"stdout": str(jobdir / "stdout.log"), "stderr": str(jobdir / "stderr.log"),
                                 "supervisor_log": str(jobdir / "supervisor.log"), "output_path": rec["output_path"],
                                 "worker_record": str(wdir / "worker.json"), "prompt_path": rec["prompt_path"]},
                    "auto_retry": False, "remaining_allowance": "unchanged",
                    "note": "routine worker failed; escalate to the lead for a decision. No automatic retry "
                            "with another model or effort was made.",
                })
            res["escalation_path"] = str(wdir / "escalation.json")
        self.store.write(self._rel(worker_id, "result.json"), res)
        return res


# ---------------------------------------------------------------------------- worker exec shim
def _exec(worker_dir: str) -> int:
    """Runs INSIDE the owned job: read the immutable worker record, set the no-delegation env,
    feed the bounded prompt file on stdin and exec the recorded codex argv (pid is preserved)."""
    rec = json.loads((Path(worker_dir) / "worker.json").read_text())
    env = dict(os.environ)
    env.update(rec.get("env") or WORKER_ENV)
    fd = os.open(rec["prompt_path"], os.O_RDONLY)
    os.dup2(fd, 0)
    os.close(fd)
    sys.stdout.flush()
    try:
        os.execvpe(rec["argv"][0], list(rec["argv"]), env)
    except OSError as e:
        sys.stderr.write(f"worker exec failed: {rec['argv'][0]!r}: {e}\n")
        return 127


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m mycelium_coord.workers")
    ap.add_argument("--exec", metavar="WORKER_DIR", required=True)
    args = ap.parse_args(argv)
    return _exec(getattr(args, "exec"))


if __name__ == "__main__":
    raise SystemExit(main())
