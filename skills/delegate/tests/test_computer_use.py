"""--computer-use for codex.

Offline: dry-run argv, the refusal for providers without a desktop-control tool,
denial detection over a recorded codex stream, and the doctor's plugin check.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delegate import cli  # noqa: E402
from delegate.envelope import Envelope  # noqa: E402
from delegate.prompt import COMPUTER_USE_CLAUSE  # noqa: E402
from delegate.providers.base import Job  # noqa: E402
from delegate.providers.codex import Codex, computer_use_status, denied_apps  # noqa: E402

USAGE_EXIT = 64


class _Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def dry(self, provider, *flags):
        code, out, err = self.run_cli(["run", "--provider", provider, "--cwd", ".",
                                       "--dry-run", *flags, "read the screen"])
        self.assertEqual(code, 0, err)
        return json.loads(out)["providerExtra"]["argv"]


class FlagTests(_Cli):
    def test_codex_prompt_carries_the_clause(self):
        argv = self.dry("codex", "--computer-use", "--read-only")
        self.assertTrue(argv[-1].endswith(COMPUTER_USE_CLAUSE))
        self.assertIn("read-only", argv)

    def test_clause_absent_without_the_flag(self):
        self.assertNotIn(COMPUTER_USE_CLAUSE, self.dry("codex")[-1])

    def test_refused_for_providers_without_the_tool(self):
        for provider in ("grok", "claude", "cursor", "pi"):
            code, _, err = self.run_cli(["run", "--provider", provider, "--cwd", ".",
                                         "--dry-run", "--computer-use", "x"])
            self.assertEqual(code, USAGE_EXIT, provider)
            self.assertIn("no desktop-control tool", err)


def _tool_result(text):
    return json.dumps({"type": "item.completed", "item": {
        "type": "mcp_tool_call", "server": "cua_repl", "tool": "js",
        "result": {"content": [{"type": "text", "text": text}]}}})


def _message(text):
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                                                          "text": text}})


class DenialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def stream(self, *lines):
        path = self.dir / "stream.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_reads_tool_results_not_the_workers_quote(self):
        path = self.stream(
            _tool_result("Computer Use was not approved to use iPhone Mirroring\n## docs"),
            _message("It said “Computer Use was not approved to use iPhone Mirroring,”"),
            _tool_result("Computer Use was not approved to use iPhone Mirroring"),
            "not json")
        self.assertEqual(denied_apps(path), ["iPhone Mirroring"])

    def finish(self, path, *, computer_use=True):
        job = Job(provider="codex", cwd=self.dir, prompt="p", run_id="r",
                  run_dir=self.dir, computer_use=computer_use)
        env = Envelope(provider="codex", run_id="r", cwd=str(self.dir))
        env.stop_reason, env.exit_code, env.stream_path = "end_turn", 0, str(path)
        Codex().finish(job, env, 0)
        return env

    def test_denial_fails_the_run_with_the_fix(self):
        env = self.finish(self.stream(_tool_result(
            "Computer Use was not approved to use iPhone Mirroring")))
        self.assertEqual((env.stop_reason, env.exit_code), ("error", 1))
        self.assertIn("iPhone Mirroring", env.error)
        self.assertIn("Always allow", env.error)
        self.assertEqual(env.provider_extra["computerUseDenied"], ["iPhone Mirroring"])

    def test_clean_run_and_non_computer_use_runs_are_untouched(self):
        env = self.finish(self.stream(_tool_result("ok")))
        self.assertEqual((env.stop_reason, env.exit_code), ("end_turn", 0))
        self.assertEqual(env.provider_extra["computerUseDenied"], [])
        env = self.finish(self.stream(_tool_result(
            "Computer Use was not approved to use iPhone Mirroring")), computer_use=False)
        self.assertEqual((env.stop_reason, env.exit_code), ("end_turn", 0))
        self.assertNotIn("computerUseDenied", env.provider_extra)

    def test_a_denial_does_not_replace_an_existing_error(self):
        path = self.stream(_tool_result(
            "Computer Use was not approved to use iPhone Mirroring"))
        job = Job(provider="codex", cwd=self.dir, prompt="p", run_id="r",
                  run_dir=self.dir, computer_use=True)
        env = Envelope(provider="codex", run_id="r", cwd=str(self.dir))
        env.stop_reason, env.exit_code = "end_turn", 0
        env.stream_path, env.error = str(path), "codex exited 1: usage limit"
        Codex().finish(job, env, 1)
        self.assertEqual(env.error, "codex exited 1: usage limit")
        self.assertEqual(env.provider_extra["computerUseDenied"], ["iPhone Mirroring"])


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def plugin(self, command, enabled=True):
        d = self.home / "plugins/cache/openai-bundled/unified-computer-use/1.0"
        d.mkdir(parents=True)
        (d / ".mcp.json").write_text(json.dumps({"mcpServers": {"cua_repl": {
            "command": command, "enabled": enabled}}}), encoding="utf-8")

    def test_missing_plugin(self):
        ready, detail = computer_use_status(self.home)
        self.assertFalse(ready)
        self.assertIn("not found", detail)

    def test_ready_needs_an_executable_runtime(self):
        self.plugin(sys.executable)
        self.assertEqual(computer_use_status(self.home)[0], True)

    def test_missing_runtime(self):
        self.plugin(str(self.home / "gone"))
        self.assertIn("runtime missing", computer_use_status(self.home)[1])

    def test_disabled(self):
        self.plugin(sys.executable, enabled=False)
        self.assertIn("disabled", computer_use_status(self.home)[1])


if __name__ == "__main__":
    unittest.main()
