"""Tests for src/consult adapters using fake agy / fake claude."""
import importlib.util, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSULT = ROOT / "src" / "consult"
FIX = Path(__file__).resolve().parent / "fixtures" / "consult"
AGY_ASK = CONSULT / "agy_ask.py"
CLAUDE_ASK = CONSULT / "claude_ask.sh"


def throwaway_home():
    """A HOME with a .gemini/config/c.json; tests never touch the real ~/.gemini."""
    h = tempfile.mkdtemp()
    os.makedirs(os.path.join(h, ".gemini", "config"))
    open(os.path.join(h, ".gemini", "config", "c.json"), "w").write("{}")
    return h


def run_agy(prompt="review", mode="normal", timeout=20, extra_env=None, extra_args=(), home=None, pre=None):
    d = tempfile.mkdtemp(); home = home or throwaway_home()
    env = dict(os.environ, AGY_BIN=str(FIX / "fake_agy"), FAKE_AGY_MODE=mode, HOME=home,
               AGY_ASK_BASE=os.path.join(tempfile.mkdtemp(), "base"))
    env.update(extra_env or {})
    out = os.path.join(d, "ans.md")
    for suffix, text in (pre or {}).items():
        open(out + suffix, "w").write(text)
    p = subprocess.run([sys.executable, str(AGY_ASK), "--out", out, "--cwd", d, "--timeout", str(timeout), *extra_args],
                       input=prompt, text=True, capture_output=True, env=env)
    rc = json.load(open(out + ".receipt.json")) if os.path.exists(out + ".receipt.json") else None
    ans = open(out).read() if os.path.exists(out) else None
    part = open(out + ".partial").read() if os.path.exists(out + ".partial") else None
    return p, rc, ans, part, out, home


def load_agy_module():
    spec = importlib.util.spec_from_file_location("agy_ask_mod", str(AGY_ASK))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


OPTS = [{"optionId": "allow", "kind": "allow_once"}, {"optionId": "rej", "kind": "reject_once"}]


class AgyDecide(unittest.TestCase):
    def d(self, kind, title):
        return load_agy_module().decide({"kind": kind, "title": title}, OPTS)

    def test_grant_only_read_search_without_deny_word(self):
        self.assertEqual(self.d("read", "Read review_me.py"), ("allow", True))
        self.assertEqual(self.d("search", "Grep for foo"), ("allow", True))
        self.assertEqual(self.d("read", "Review the preview of global research"), ("allow", True))  # no substring matching

    def test_everything_else_rejected(self):
        for kind in ("other", "", "execute", "edit", "delete", "move", "fetch", "think", "unknown"):
            self.assertEqual(self.d(kind, "Read review_me.py"), ("rej", False), kind)
        self.assertEqual(self.d("other", "Modify review_me.py"), ("rej", False))
        self.assertEqual(self.d("other", "apply_patch review_me.py"), ("rej", False))
        self.assertEqual(self.d("", "send_message: please review"), ("rej", False))

    def test_deny_words_win_over_kind(self):
        self.assertEqual(self.d("read", "Write file x"), ("rej", False))
        self.assertEqual(self.d("search", "search and replace foo"), ("rej", False))
        self.assertEqual(self.d("read", "save_file x"), ("rej", False))

    def test_no_allow_once_never_grants(self):
        got = load_agy_module().decide({"kind": "read", "title": "Read x"}, [{"optionId": "aa", "kind": "allow_always"}])
        self.assertEqual(got, (None, False))


class AgyAsk(unittest.TestCase):
    def test_answered_chunks_and_permissions(self):
        p, rc, ans, part, out, _ = run_agy()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(ans, "AB|W=rej|R=allow|LATE")
        self.assertEqual(rc["outcome"], "ANSWERED"); self.assertEqual(rc["stop_reason"], "end_turn")
        self.assertEqual([x["kind"] for x in rc["permissions_rejected"]], ["edit"])
        self.assertEqual([x["kind"] for x in rc["permissions_granted"]], ["read"])
        self.assertEqual(rc["files_read"], ["/work/review_me.py"])
        self.assertNotIn("/work/edit_target.py", rc["files_read"])  # rejected edit is not a read
        self.assertFalse(rc["no_successful_read"]); self.assertIsNone(part)

    def test_receipt_schema(self):
        _, rc, _, _, _, _ = run_agy()
        for k in ("adapter host agy_version model_reported session_id started_at ended_at duration_s outcome reason "
                  "answer_chars stop_reason tool_calls files_read no_successful_read permissions_rejected "
                  "permissions_granted host_config_unchanged gemini_dir kept").split():
            self.assertIn(k, rc)
        self.assertEqual((rc["adapter"], rc["host"], rc["model_reported"], rc["session_id"]), ("agy_ask", "antigravity", "fake-model", "S1"))
        self.assertFalse(rc["kept"]); self.assertFalse(os.path.exists(rc["gemini_dir"]))
        self.assertTrue(rc["host_config_unchanged"])

    def test_keep_preserves_dir(self):
        _, rc, _, _, _, _ = run_agy(extra_args=("--keep",))
        self.assertTrue(rc["kept"]); self.assertTrue(os.path.isdir(rc["gemini_dir"]))

    def test_timeout_exit2(self):
        p, rc, ans, part, _, _ = run_agy(mode="hang", timeout=2)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(rc["outcome"], "CANNOT-EVALUATE"); self.assertIsNone(ans)

    def test_empty_exit3(self):
        p, rc, ans, _, _, _ = run_agy(mode="empty")
        self.assertEqual(p.returncode, 3)
        self.assertEqual(rc["outcome"], "CANNOT-EVALUATE"); self.assertEqual(rc["answer_chars"], 0); self.assertIsNone(ans)

    def test_unsafe_base_refused_case_insensitively_and_leaves_nothing(self):
        home = throwaway_home()
        for sub in (".gemini", ".GEMINI"):
            p, rc, ans, _, _, _ = run_agy(home=home, extra_env={"AGY_ASK_BASE": os.path.join(home, sub, "x")})
            self.assertEqual(p.returncode, 6, sub)
            self.assertEqual(rc["outcome"], "CANNOT-EVALUATE")
            self.assertIsNone(ans)
        self.assertEqual(sorted(os.listdir(os.path.join(home, ".gemini"))), ["config"])  # nothing created

    @unittest.skipUnless(sys.platform == "darwin", "fixed base is /private/tmp on macOS")
    def test_default_base_ignores_tmpdir(self):
        _, rc, _, _, _, _ = run_agy(extra_args=("--keep",), extra_env={"AGY_ASK_BASE": "", "TMPDIR": "/nonexistent-tmp"})
        try:
            self.assertTrue(rc["gemini_dir"].startswith("/private/tmp/agy_ask-"), rc["gemini_dir"])
        finally:
            import shutil; shutil.rmtree(rc["gemini_dir"], ignore_errors=True)

    def test_uses_gemini_dir_flag_and_relative_appdata(self):
        log = tempfile.mktemp()
        run_agy(extra_env={"FAKE_AGY_LOG": log})
        argv = json.load(open(log))
        self.assertEqual(argv[0], "acp")
        self.assertTrue(os.path.isabs(argv[argv.index("--gemini-dir") + 1]))
        self.assertFalse(os.path.isabs(argv[argv.index("--app-data-dir") + 1]))

    def test_kind_other_modify_and_patch_rejected(self):
        p, rc, ans, _, _, _ = run_agy(mode="kind_other")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(ans, "O|A=rej|B=rej")
        self.assertEqual(rc["permissions_granted"], [])
        self.assertEqual(len(rc["permissions_rejected"]), 2)
        self.assertEqual(rc["files_read"], []); self.assertTrue(rc["no_successful_read"])

    def test_kind_read_titled_write_rejected(self):
        _, rc, ans, _, _, _ = run_agy(mode="read_write_title")
        self.assertEqual(ans, "RW|A=rej"); self.assertEqual(rc["permissions_granted"], [])

    def test_stop_reason_not_end_turn_is_cannot_evaluate(self):
        for mode in ("max_tokens", "refusal"):
            p, rc, ans, part, _, _ = run_agy(mode=mode)
            self.assertEqual(p.returncode, 5, mode)
            self.assertEqual(rc["outcome"], "CANNOT-EVALUATE"); self.assertEqual(rc["stop_reason"], mode)
            self.assertIsNone(ans); self.assertEqual(part, "some text")

    def test_two_turn_chunks_do_not_bleed(self):
        p, rc, ans, _, _, _ = run_agy(mode="twoturn", extra_env={"AGY_ASK_TEST_SECOND_PROMPT": "again"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(ans, "T1-aT1-bT1-late")
        self.assertEqual(rc["test_second_turn_text"], "T2-aT2-bT2-late")

    def test_failed_read_not_counted(self):
        p, rc, ans, _, _, _ = run_agy(mode="failed_read")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(rc["files_read"], []); self.assertTrue(rc["no_successful_read"])
        self.assertEqual(rc["outcome"], "ANSWERED")  # outcome unchanged; the flag tells callers it is not a review

    def test_config_change_on_answered_run_is_exit4(self):
        home = throwaway_home(); touch = os.path.join(home, ".gemini", "config", "c.json")
        p, rc, ans, _, _, _ = run_agy(home=home, extra_env={"FAKE_AGY_TOUCH": touch})
        self.assertEqual(p.returncode, 4)
        self.assertEqual(rc["outcome"], "ANSWERED"); self.assertFalse(rc["host_config_unchanged"])
        self.assertIsNotNone(ans)

    def test_config_change_on_timeout_keeps_code2(self):
        home = throwaway_home(); touch = os.path.join(home, ".gemini", "config", "c.json")
        p, rc, ans, part, _, _ = run_agy(mode="partial_hang", timeout=2, home=home, extra_env={"FAKE_AGY_TOUCH": touch})
        self.assertEqual(p.returncode, 2)
        self.assertEqual(rc["outcome"], "CANNOT-EVALUATE"); self.assertFalse(rc["host_config_unchanged"])
        self.assertEqual(rc["host_config_changed_files"], [touch])
        self.assertIsNone(ans); self.assertEqual(part, "PARTIAL")

    def test_stale_outputs_removed(self):
        p, rc, ans, part, _, _ = run_agy(mode="empty", pre={"": "OLD", ".partial": "OLDP", ".receipt.json": '{"outcome":"ANSWERED"}'})
        self.assertEqual(p.returncode, 3)
        self.assertIsNone(ans); self.assertIsNone(part); self.assertEqual(rc["outcome"], "CANNOT-EVALUATE")

    def test_arg_error_writes_receipt(self):
        d = tempfile.mkdtemp(); out = os.path.join(d, "a.md")
        p = subprocess.run([sys.executable, str(AGY_ASK), "--out", out, "--bogus"], input="x", text=True, capture_output=True,
                           env=dict(os.environ, HOME=throwaway_home()))
        self.assertEqual(p.returncode, 5)
        self.assertEqual(json.load(open(out + ".receipt.json"))["outcome"], "CANNOT-EVALUATE")


class ClaudeAsk(unittest.TestCase):
    def run_claude(self, args=(), pre=None, **env):
        d = tempfile.mkdtemp(); out = os.path.join(d, "ans.md"); log = os.path.join(d, "argv.json")
        for suffix, text in (pre or {}).items():
            open(out + suffix, "w").write(text)
        e = dict(os.environ, CLAUDE_BIN=str(FIX / "fake_claude"), HOME=tempfile.mkdtemp(), FAKE_CLAUDE_LOG=log); e.update(env)
        p = subprocess.run(["bash", str(CLAUDE_ASK), "--out", out, "--cwd", d, "--timeout", "20", *args],
                           input="review", text=True, capture_output=True, env=e)
        rc = json.load(open(out + ".receipt.json")) if os.path.exists(out + ".receipt.json") else None
        ans = open(out).read() if os.path.exists(out) else None
        part = open(out + ".partial").read() if os.path.exists(out + ".partial") else None
        argv = json.load(open(log)) if os.path.exists(log) else None
        return p, rc, ans, part, argv

    def test_clean_init_and_isolation_flags(self):
        p, rc, ans, part, argv = self.run_claude()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(ans, "bug on line 3")
        self.assertEqual(rc["mcp_servers_at_init"], [])
        self.assertEqual(sorted(rc["tools_at_init"]), ["Glob", "Grep", "Read"])
        self.assertEqual((rc["host"], rc["session_id"], rc["outcome"]), ("claude", "fake-sid", "ANSWERED"))
        self.assertEqual(rc["files_read"], ["/work/review_me.py"]); self.assertFalse(rc["no_successful_read"])
        self.assertEqual(rc["effort_requested"], "high"); self.assertEqual(rc["messaging_socket_path"], "/tmp/cc-socks/1.sock")
        self.assertEqual(rc["plugins_at_init"][0]["path"], "builtin")
        for k in ("hook_events_seen", "host_config_unchanged", "permissions_rejected", "tool_calls"):
            self.assertIn(k, rc)
        for flag in ("--safe-mode", "--restricted", "--strict-mcp-config", "--include-hook-events", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--mcp-config") + 1], '{"mcpServers":{}}')
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[argv.index("--effort") + 1], "high"); self.assertEqual(argv[argv.index("--model") + 1], "opus")
        for bad in ("--bare", "--dangerously-skip-permissions"):
            self.assertNotIn(bad, argv)

    def test_explicit_model_effort_passed(self):
        _, rc, _, _, argv = self.run_claude(args=("--model", "sonnet", "--effort", "low"))
        self.assertEqual(argv[argv.index("--effort") + 1], "low"); self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(rc["effort_requested"], "low")

    def test_mcp_leak_flagged_exit4(self):
        p, rc, ans, _, _ = self.run_claude(FAKE_CLAUDE_MCP='[{"name":"mycelium-coord","status":"connected"}]')
        self.assertEqual(p.returncode, 4)
        self.assertIn("mcp_servers not empty", rc["reason"]); self.assertEqual(ans, "bug on line 3")

    def test_extra_tools_hook_event_nonbuiltin_plugin_exit4(self):
        for mode, needle in (("extra-tools", "tools not exactly"), ("hook-event", "hook events"), ("nonbuiltin-plugin", "non-builtin plugin")):
            p, rc, ans, _, _ = self.run_claude(FAKE_CLAUDE_MODE=mode)
            self.assertEqual(p.returncode, 4, mode)
            self.assertIn(needle, rc["reason"]); self.assertEqual(rc["outcome"], "ANSWERED"); self.assertEqual(ans, "bug on line 3")

    def test_empty_exit3(self):
        p, rc, ans, _, _ = self.run_claude(FAKE_CLAUDE_MODE="empty")
        self.assertEqual(p.returncode, 3); self.assertEqual(rc["outcome"], "CANNOT-EVALUATE"); self.assertIsNone(ans)

    def test_crash_after_text_is_not_answered(self):
        p, rc, ans, part, _ = self.run_claude(FAKE_CLAUDE_MODE="crash-after-text")
        self.assertEqual(p.returncode, 5)
        self.assertEqual(rc["outcome"], "CANNOT-EVALUATE"); self.assertIsNone(ans)
        self.assertEqual(part, "Let me read the file.")

    def test_error_result_is_not_answered(self):
        p, rc, ans, _, _ = self.run_claude(FAKE_CLAUDE_MODE="error-result")
        self.assertEqual(p.returncode, 5); self.assertIsNone(ans); self.assertEqual(rc["outcome"], "CANNOT-EVALUATE")

    def test_timeout_exit2(self):
        d = tempfile.mkdtemp(); out = os.path.join(d, "a.md")
        p = subprocess.run(["bash", str(CLAUDE_ASK), "--out", out, "--cwd", d, "--timeout", "2"], input="x", text=True,
                           capture_output=True, env=dict(os.environ, CLAUDE_BIN=str(FIX / "fake_claude"), HOME=tempfile.mkdtemp(), FAKE_CLAUDE_MODE="timeout"))
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.load(open(out + ".receipt.json"))["outcome"], "CANNOT-EVALUATE"); self.assertFalse(os.path.exists(out))

    def test_stale_outputs_removed(self):
        p, rc, ans, part, _ = self.run_claude(FAKE_CLAUDE_MODE="empty", pre={"": "OLD", ".partial": "OLDP", ".receipt.json": '{"outcome":"ANSWERED"}'})
        self.assertIsNone(ans); self.assertIsNone(part); self.assertEqual(rc["outcome"], "CANNOT-EVALUATE")

    def test_unsafe_cwd_exit6_writes_receipt(self):
        out = os.path.join(tempfile.mkdtemp(), "a.md"); home = tempfile.mkdtemp()
        p = subprocess.run(["bash", str(CLAUDE_ASK), "--out", out, "--cwd", home], input="x", text=True, capture_output=True,
                           env=dict(os.environ, HOME=home, CLAUDE_BIN=str(FIX / "fake_claude")))
        self.assertEqual(p.returncode, 6)
        self.assertEqual(json.load(open(out + ".receipt.json"))["outcome"], "CANNOT-EVALUATE")

    def test_unknown_arg_exit5_writes_receipt(self):
        out = os.path.join(tempfile.mkdtemp(), "a.md")
        p = subprocess.run(["bash", str(CLAUDE_ASK), "--out", out, "--bogus"], input="x", text=True, capture_output=True,
                           env=dict(os.environ, CLAUDE_BIN=str(FIX / "fake_claude")))
        self.assertEqual(p.returncode, 5)
        self.assertEqual(json.load(open(out + ".receipt.json"))["outcome"], "CANNOT-EVALUATE")


    def test_unknown_arg_before_out_still_cleans_and_writes_receipt(self):
        out = os.path.join(tempfile.mkdtemp(), "a.md")
        for stale in (out, out + ".partial"):
            open(stale, "w").write("STALE")
        p = subprocess.run(["bash", str(CLAUDE_ASK), "--bogus", "--out", out], input="x", text=True, capture_output=True,
                           env=dict(os.environ, CLAUDE_BIN=str(FIX / "fake_claude")))
        self.assertEqual(p.returncode, 5)
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.exists(out + ".partial"))
        self.assertEqual(json.load(open(out + ".receipt.json"))["outcome"], "CANNOT-EVALUATE")


class AgyArgParsing(unittest.TestCase):
    def test_abbreviated_out_is_rejected_not_silently_ignored(self):
        out = os.path.join(tempfile.mkdtemp(), "a.md")
        p = subprocess.run([sys.executable, str(AGY_ASK), "--ou", out], input="x", text=True, capture_output=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(os.path.exists(out))

if __name__ == "__main__":
    unittest.main()
