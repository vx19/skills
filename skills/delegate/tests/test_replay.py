"""Replay synthetic recorded runs and pin today's envelope literals.

Each fixture goes through the real `bin/delegate run`; the fake runner, scratch
repo, HOME and state root all live under a throwaway temp directory. These
assertions are intentionally literal: later units change the behaviour and must
update them deliberately.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TESTS_DIR))

import replay as replaymod  # noqa: E402

FIXTURES = TESTS_DIR / "fixtures" / "replay"


class ReplayPinTests(unittest.TestCase):
    def envelope(self, name: str) -> dict:
        return replaymod.replay(FIXTURES / name)

    def assert_exit0_error(self, name: str, runner: str, message: str) -> dict:
        env = self.envelope(name)
        self.assertEqual((env["stopReason"], env["exitCode"]), ("error", 1))
        self.assertTrue(
            env["error"].startswith(f"{runner} exited 0 but reported an error: "),
            env["error"])
        self.assertIn(message, env["error"])
        self.assertIsNone(env.get("answerPath"))
        return env

    def test_claude_429_with_exit_0_is_an_error(self) -> None:
        self.assert_exit0_error("claude-429-exit0", "claude", "session limit")

    def test_claude_error_subtype_without_is_error_is_an_error(self) -> None:
        self.assert_exit0_error("claude-max-turns", "claude", "error_max_turns")

    def test_grok_error_event_with_exit_0_is_an_error(self) -> None:
        self.assert_exit0_error("grok-error", "grok", "model overloaded")

    def test_pi_error_message_with_exit_0_is_an_error(self) -> None:
        self.assert_exit0_error("pi-error", "pi", "429 rate limit exceeded")

    def test_cursor_is_error_with_exit_0_is_an_error(self) -> None:
        self.assert_exit0_error("cursor-is-error", "cursor-agent", "Usage limit reached")

    def test_a_retried_error_followed_by_progress_still_succeeds(self) -> None:
        env = self.envelope("codex-reconnect-then-success")
        self.assertEqual((env["stopReason"], env["exitCode"]), ("end_turn", 0))
        self.assertIsNone(env["error"])

    def test_a_recovered_retry_does_not_mask_the_real_failure(self) -> None:
        env = self.envelope("codex-reconnect-then-exit1")
        self.assertEqual((env["stopReason"], env["exitCode"]), ("error", 1))
        self.assertEqual(env["error"], "codex exited 1: quota exceeded for this account")

    @unittest.skipIf(sys.platform == "win32", "POSIX modes")
    def test_a_first_run_creates_a_private_state_dir(self) -> None:
        artifacts: dict = {}
        replaymod.replay(FIXTURES / "grok-success", artifacts=artifacts)
        self.assertEqual(oct(artifacts["stateRootMode"]), oct(0o700))

    def test_codex_usage_limit(self) -> None:
        env = self.envelope("codex-usage-limit")
        self.assertEqual(env["provider"], "codex")
        self.assertEqual(env["stopReason"], "error")
        self.assertEqual(env["exitCode"], 1)
        self.assertTrue(
            env["error"].startswith("codex exited 1: You've hit your usage limit"))
        self.assertEqual(env["turns"], 1)

    def test_codex_usage_limit_progress_has_tool_calls_and_a_write(self) -> None:
        artifacts: dict[str, str] = {}
        replaymod.replay(FIXTURES / "codex-usage-limit", artifacts=artifacts)
        progress = json.loads(artifacts["progress.json"])
        self.assertGreater(progress["counts"]["tool_call"], 0)
        self.assertGreater(progress["counts"]["write"], 0)

    def test_codex_model_refused(self) -> None:
        env = self.envelope("codex-model-refused")
        self.assertEqual(env["provider"], "codex")
        self.assertEqual(env["stopReason"], "error")
        self.assertEqual(env["exitCode"], 1)
        self.assertEqual(
            env["error"],
            "codex exited 1: The 'example-model' model is not supported "
            "when using Codex with a ChatGPT account.")
        self.assertEqual(env["turns"], 1)

    def test_claude_429(self) -> None:
        env = self.envelope("claude-429")
        self.assertEqual(env["provider"], "claude")
        self.assertEqual(env["stopReason"], "error")
        self.assertEqual(env["exitCode"], 1)
        self.assertIn("session limit", env["error"])
        self.assertIn("HTTP 429", env["error"])

    def success(self, name: str | Path) -> tuple[dict, str | None]:
        artifacts: dict = {}
        env = replaymod.replay(FIXTURES / name, artifacts=artifacts)
        self.assertEqual((env["stopReason"], env["exitCode"], env["error"]),
                         ("end_turn", 0, None), name)
        return env, artifacts.get("answer")

    def test_grok_success(self) -> None:
        env, answer = self.success("grok-success")
        self.assertEqual((env["turns"], env["costUsd"], answer), (35, 0.01, "done"))
        self.assertEqual(env["providerExtra"]["usage"],
                         {"input": 10, "output": 5, "reasoning": 0, "cache_read": 0,
                          "requests": 1})

    def test_claude_success(self) -> None:
        env, answer = self.success("claude-success")
        self.assertEqual((env["turns"], env["costUsd"], answer), (2, 0.02, "Done."))

    def test_codex_success_reads_the_answer_codex_wrote(self) -> None:
        env, answer = self.success("codex-success")
        self.assertEqual((env["turns"], answer), (1, "All done.\n"))
        self.assertEqual(env["providerExtra"]["usage"]["cache_read"], 2)

    def test_cursor_success(self) -> None:
        env, answer = self.success("cursor-success")
        self.assertEqual((env["turns"], env["costUsd"], answer), (None, None, "done"))

    def test_pi_success(self) -> None:
        env, answer = self.success("pi-success")
        self.assertEqual((env["turns"], env["costUsd"], answer), (1, 0.003, "Finished."))

    def test_a_truncated_last_line_is_ignored(self) -> None:
        env, answer = self.success("grok-truncated")
        self.assertEqual(answer, "partial answer")

    def test_non_utf8_bytes_on_stdout_do_not_end_the_run(self) -> None:
        # Built here: a tracked fixture stays UTF-8 text (see the next test).
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            (fixture / "meta.json").write_text('{"provider": "grok", "exit": 0}')
            (fixture / "runner.log").write_text("")
            (fixture / "stream.jsonl").write_bytes(
                b'{"type":"text","data":"caf\xe9 "}\n{"type":"text","data":"done"}\n'
                b'{"type":"end","stopReason":"end_turn","num_turns":1}\n')
            env, answer = self.success(fixture)
        self.assertEqual(answer, "caf\ufffd done")

    def test_every_fixture_file_is_utf8_text(self) -> None:
        # The public mirror publishes text only; one stray byte stops its export.
        for path in sorted(p for p in FIXTURES.rglob("*") if p.is_file()):
            with self.subTest(path=path.relative_to(FIXTURES)):
                path.read_bytes().decode("utf-8")

    def test_an_api_key_in_the_environment_reaches_no_run_file(self) -> None:
        secret = "sk-replay-secret-0123456789"
        for name in ("grok-success", "codex-success", "claude-429"):
            with self.subTest(name=name):
                artifacts: dict = {}
                env = replaymod.replay(FIXTURES / name, artifacts=artifacts, env_extra={
                    "XAI_API_KEY": secret, "OPENAI_API_KEY": secret,
                    "ANTHROPIC_API_KEY": secret})
                self.assertNotIn(secret, json.dumps(env))
                self.assertIn("command.json", artifacts["files"])
                for file, text in artifacts["files"].items():
                    self.assertNotIn(secret, text, file)

    def test_claude_reads_every_block_of_a_message(self) -> None:
        env = self.envelope("claude-parallel-tools")
        self.assertEqual((env["stopReason"], env["exitCode"]), ("end_turn", 0))
        self.assertEqual(env["failedToolCalls"], 1)

    def test_error_kind_comes_from_structured_fields_only(self) -> None:
        # Error prose is not matched ("429 rate limit" stays "other"), except
        # codex's usage-limit message, its only signal for that failure.
        expected = {
            "claude-429": "usage_limit",          # api_error_status 429
            "claude-429-exit0": "usage_limit",
            "claude-max-turns": "max_turns",      # subtype error_max_turns
            "codex-model-refused": "other",       # inner status 400
            "codex-usage-limit": "usage_limit",   # codex's turn.failed message
            "codex-reconnect-then-exit1": "other",
            "cursor-auth": "other",
            "cursor-is-error": "other",
            "grok-error": "other",
            "pi-error": "other",
            "grok-success": None,
            "claude-parallel-tools": None,
            "codex-reconnect-then-success": None,
            "claude-success": None,
            "codex-success": None,
            "cursor-success": None,
            "pi-success": None,
            "grok-truncated": None,
        }
        self.assertEqual(sorted(expected), sorted(m.parent.name for m in FIXTURES.glob("*/meta.json")))
        got = {name: self.envelope(name)["errorKind"] for name in expected}
        self.assertEqual(got, expected)

    def test_a_rejected_usage_window_gives_its_reset_time(self) -> None:
        self.assertEqual(self.envelope("claude-429")["resetsAt"], "2026-01-01T00:00:00Z")
        self.assertIsNone(self.envelope("codex-usage-limit")["resetsAt"])

    def test_cursor_auth(self) -> None:
        env = self.envelope("cursor-auth")
        self.assertEqual(env["provider"], "cursor")
        self.assertEqual(env["stopReason"], "error")
        self.assertEqual(env["exitCode"], 1)
        self.assertEqual(
            env["error"],
            "cursor-agent exited 1: Authentication required. Please run "
            "'agent login' first, or set CURSOR_API_KEY environment variable.")


if __name__ == "__main__":
    unittest.main()