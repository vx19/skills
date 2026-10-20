"""Replay synthetic recorded runs and pin today's envelope literals.

Each fixture goes through the real `bin/delegate run`; the fake runner, scratch
repo, HOME and state root all live under a throwaway temp directory. These
assertions are intentionally literal: later units change the behaviour and must
update them deliberately.
"""

from __future__ import annotations

import json
import sys
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

    def test_grok_success(self) -> None:
        env = self.envelope("grok-success")
        self.assertEqual(env["provider"], "grok")
        self.assertEqual(env["stopReason"], "end_turn")
        self.assertEqual(env["exitCode"], 0)
        self.assertIsNone(env["error"])

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