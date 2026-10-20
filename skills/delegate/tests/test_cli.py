"""CLI surface tests for the new unified `delegate` command.

Driven in-process via `delegate.cli.main([...])`, capturing the stdout envelope.
Everything here is offline: `run --dry-run` skips preflight and never launches a
runner, so we can assert each provider's launch *posture* (the argv the provider
would exec) without any runner on PATH. Usage errors, `--` handling, and doctor's
exit contract are checked too.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delegate import cli  # noqa: E402

USAGE_EXIT = 64

# provider -> (expected argv[0] runner, [substrings that MUST appear in argv])
POSTURE = {
    "grok":   ("grok",        ["--permission-mode", "auto", "--no-plan", "--single"]),
    "claude": ("claude",      ["-p", "--permission-mode", "auto", "--add-dir"]),
    "codex":  ("codex",       ["exec", "--approve-for-me", "--json"]),
    "cursor": ("cursor-agent", ["--print", "--force", "--trust"]),
    "pi":     ("pi",          ["-p", "--no-session", "--mode", "json"]),
}


def _contains_flag_value(argv, flag, value):
    """True if `flag` is immediately followed by `value` somewhere in argv."""
    for i, tok in enumerate(argv):
        if tok == flag and i + 1 < len(argv) and argv[i + 1] == value:
            return True
    return False


class CliTestBase(unittest.TestCase):
    def setUp(self) -> None:
        # Isolate all job/state writes into a throwaway dir.
        self._tmp = tempfile.TemporaryDirectory()
        self._old_xdg = os.environ.get("XDG_STATE_HOME")
        self._old_grok_agent = os.environ.get("GROK_AGENT")
        os.environ["XDG_STATE_HOME"] = self._tmp.name
        os.environ.pop("GROK_AGENT", None)

    def tearDown(self) -> None:
        if self._old_xdg is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self._old_xdg
        if self._old_grok_agent is None:
            os.environ.pop("GROK_AGENT", None)
        else:
            os.environ["GROK_AGENT"] = self._old_grok_agent
        self._tmp.cleanup()

    def run_cli(self, argv):
        """Return (exit_code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def dry_run(self, provider, extra=None, prompt="do the thing"):
        argv = ["run", "--provider", provider, "--cwd", ".", "--dry-run"]
        argv += extra or []
        if prompt is not None:
            argv.append(prompt)
        code, out, err = self.run_cli(argv)
        self.assertEqual(code, 0, f"{provider} dry-run exit={code} err={err}")
        env = json.loads(out.strip())
        return env


class TestDryRunPosture(CliTestBase):
    def test_envelope_is_a_dry_run(self) -> None:
        env = self.dry_run("grok")
        self.assertEqual(env["schemaVersion"], 3)
        self.assertTrue(env["dryRun"])
        self.assertEqual(env["provider"], "grok")
        self.assertEqual(env["stopReason"], "end_turn")
        self.assertIn("argv", env["providerExtra"])

    def test_all_providers_posture(self) -> None:
        for provider, (runner, must_have) in POSTURE.items():
            with self.subTest(provider=provider):
                extra = ["--provider-arg", "provider=openai"] if provider == "pi" else None
                env = self.dry_run(provider, extra=extra)
                argv = env["providerExtra"]["argv"]
                self.assertEqual(argv[0], runner,
                                 f"{provider}: argv[0]={argv[0]!r} != {runner!r}")
                for tok in must_have:
                    self.assertIn(tok, argv, f"{provider}: missing {tok!r} in {argv}")

    def test_grok_permission_mode_pairs_auto(self) -> None:
        argv = self.dry_run("grok")["providerExtra"]["argv"]
        self.assertTrue(_contains_flag_value(argv, "--permission-mode", "auto"))

    def test_claude_add_dir_and_permission_mode(self) -> None:
        argv = self.dry_run("claude")["providerExtra"]["argv"]
        self.assertTrue(_contains_flag_value(argv, "--permission-mode", "auto"))
        self.assertIn("--add-dir", argv)

    def test_codex_exec_and_approval(self) -> None:
        argv = self.dry_run("codex")["providerExtra"]["argv"]
        self.assertEqual(argv[1], "exec")
        # --approve-for-me auto-approves in codex's workspace-write sandbox and
        # is mutually exclusive with --sandbox, so --sandbox must NOT appear.
        self.assertIn("--approve-for-me", argv)
        self.assertNotIn("--sandbox", argv)

    def test_pi_mode_json(self) -> None:
        argv = self.dry_run("pi", extra=["--provider-arg", "provider=openai"])["providerExtra"]["argv"]
        self.assertTrue(_contains_flag_value(argv, "--mode", "json"))

    def test_pi_schema_is_accepted(self) -> None:
        schema = Path(self._tmp.name) / "s.json"
        schema.write_text("{}", encoding="utf-8")
        argv = self.dry_run("pi", extra=["--schema", str(schema)])["providerExtra"]["argv"]
        self.assertIn("Answer format", argv[argv.index("-p") + 1])

    def test_model_flows_into_envelope_and_argv(self) -> None:
        env = self.dry_run("grok", extra=["--model", "grok-4"])
        self.assertEqual(env["model"], "grok-4")
        self.assertTrue(_contains_flag_value(env["providerExtra"]["argv"], "--model", "grok-4"))


class TestPromptSurvival(CliTestBase):
    def test_double_dash_separates_dashy_prompt(self) -> None:
        # Everything after `--` is the positional prompt, even if it looks like a flag.
        code, out, err = self.run_cli(
            ["run", "--provider", "grok", "--cwd", ".", "--dry-run", "--", "--not-a-flag hello"]
        )
        self.assertEqual(code, 0, err)
        argv = json.loads(out.strip())["providerExtra"]["argv"]
        # grok passes the assembled prompt as the final --single argument.
        self.assertIn("--not-a-flag hello", argv[-1])

    def test_multiword_prompt_survives(self) -> None:
        argv = self.dry_run("claude", prompt="fix the bug now")["providerExtra"]["argv"]
        self.assertIn("fix the bug now", argv[-1])


class TestUsageErrors(CliTestBase):
    def test_no_task_exits_64(self) -> None:
        code, out, err = self.run_cli(["run", "--provider", "grok", "--cwd", ".", "--dry-run"])
        self.assertEqual(code, USAGE_EXIT)
        self.assertEqual(out.strip(), "")  # no envelope on a usage error

    def test_missing_task_file_exits_64(self) -> None:
        missing = os.path.join(self._tmp.name, "nope.txt")
        code, out, err = self.run_cli(
            ["run", "--provider", "grok", "--cwd", ".", "--dry-run", "--task", missing]
        )
        self.assertEqual(code, USAGE_EXIT)
        self.assertIn("not found", err.lower())

    def test_both_task_and_prompt_exits_64(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("a task")
            taskfile = f.name
        try:
            code, out, err = self.run_cli(
                ["run", "--provider", "grok", "--cwd", ".", "--dry-run",
                 "--task", taskfile, "also inline"]
            )
            self.assertEqual(code, USAGE_EXIT)
        finally:
            os.unlink(taskfile)

    def test_task_file_is_used(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("TASK-FROM-FILE-MARKER")
            taskfile = f.name
        try:
            code, out, err = self.run_cli(
                ["run", "--provider", "grok", "--cwd", ".", "--dry-run", "--task", taskfile]
            )
            self.assertEqual(code, 0, err)
            argv = json.loads(out.strip())["providerExtra"]["argv"]
            self.assertIn("TASK-FROM-FILE-MARKER", argv[-1])
        finally:
            os.unlink(taskfile)


class TestContracts(CliTestBase):
    def test_report_is_in_prompt(self):
        report = str(Path(self._tmp.name) / "report.md")
        env = self.dry_run("grok", extra=["--report", report])
        self.assertIn(report, env["providerExtra"]["argv"][-1])

    def test_unsupported_limits_warn(self):
        code, out, err = self.run_cli(["run", "--provider", "cursor", "--dry-run",
                                      "--effort", "high", "--max-turns", "2", "task"])
        self.assertEqual(code, 0)
        self.assertIn("--effort is not supported", err)
        self.assertIn("--max-turns is not supported", err)

    def test_wait_honors_timeout_over_old_cap(self):
        from unittest.mock import patch
        from argparse import Namespace
        # A result arriving at 600 seconds must still be read with timeout=700.
        with patch("delegate.cli.time.time", side_effect=[0, 600]), \
             patch("delegate.cli.jobsmod.read_result", return_value={"exitCode": 0}), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_wait(Namespace(job="x", timeout=700)), 0)


class TestDoctor(CliTestBase):
    """Doctor is diagnostic: it must speak the runtime's rules."""

    def doctor(self, argv, *, which=None, **env_over):
        env = {"HOME": self._tmp.name, **env_over}
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch("shutil.which", return_value=which):
            # Drop what the host may have leaked into this process, then apply
            # the test's own values.
            for leaked in ("XAI_API_KEY", "GROK_API_KEY"):
                os.environ.pop(leaked, None)
            os.environ.update(env)
            return self.run_cli(argv)

    def test_runner_absent_is_only_a_warn(self):
        code, out, err = self.doctor(["doctor", "--provider", "grok"])
        self.assertEqual(code, 0, f"out={out} err={err}")
        self.assertIn("warn grok", out)

    def test_unreadable_grok_credentials_fail_doctor(self):
        auth = Path(self._tmp.name) / ".grok" / "auth.json"
        auth.parent.mkdir()
        auth.write_text("{}\n", encoding="utf-8")
        auth.chmod(0)
        try:
            code, out, err = self.doctor(
                ["doctor", "--provider", "grok"], which="/usr/bin/grok")
            self.assertEqual(code, 2, f"out={out} err={err}")
            self.assertIn(
                f"FAIL grok  : grok cannot read its credentials {auth} "
                "(Permission denied); the calling sandbox must allow reading it",
                out,
            )
        finally:
            auth.chmod(0o644)

    def test_missing_grok_credentials_are_readable(self):
        code, out, err = self.doctor(
            ["doctor", "--provider", "grok"], which="/usr/bin/grok")
        self.assertEqual(code, 0, f"out={out} err={err}")
        self.assertIn("ok   grok  : credentials readable", out)
        self.assertIn("ok   grok  : sandbox", out)


class TestLiveStatusAndWait(CliTestBase):
    def _job(self, run_id, *, pid, status="running", background=False, started_at=None):
        from delegate.jobs import JobRecord
        rec = JobRecord(
            run_id=run_id, provider="grok", cwd="/work", status=status,
            pid=pid, background=background,
            started_at=time.time() if started_at is None else started_at)
        rec.save()
        return rec

    def _result(self, rec, payload):
        path = rec.path().parent / "result.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_wait_help_mentions_exit_3(self):
        text = cli.build_parser().format_help()
        self.assertIn("exit 3", text)
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(buf):
                cli.main(["wait", "-h"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("exit 3", buf.getvalue())

    def test_wait_live_job_exits_3_and_says_still_running(self):
        rec = self._job("live1", pid=os.getpid(), started_at=time.time() - 10)
        progress = {
            "lastKind": "thought",
            "lastEventAt": time.time(),
            "lastToolAt": None,
            "lastWriteAt": None,
            "counts": {"tool_call": 0, "write": 0, "failed_tool": 0},
            "usage": {"requests": 0},
        }
        (rec.path().parent / "progress.json").write_text(json.dumps(progress), encoding="utf-8")
        code, out, err = self.run_cli(["wait", "live1", "--timeout", "0"])
        self.assertEqual(code, 3)
        self.assertEqual(code, cli.WAIT_PENDING_EXIT)
        self.assertEqual(out, "")
        self.assertIn("delegate: still running: ", err)
        self.assertIn("last tool never", err)

    def test_wait_finished_job_prints_envelope_and_exit_code(self):
        rec = self._job("done1", pid=os.getpid(), status="done")
        payload = {"exitCode": 7, "stopReason": "error", "providerExtra": {}}
        self._result(rec, payload)
        code, out, err = self.run_cli(["wait", "done1", "--timeout", "30"])
        self.assertEqual(code, 7, err)
        self.assertEqual(json.loads(out), payload)

    def test_wait_unknown_stays_exit_1(self):
        code, out, err = self.run_cli(["wait", "missing", "--timeout", "0"])
        self.assertEqual(code, 1)
        self.assertIn("no such job", err)
        self.assertNotIn("still running", err)

    def test_wait_dead_foreground_returns_synthetic_envelope_at_once(self):
        rec = self._job("gone1", pid=999999)
        started = time.time()
        code, out, err = self.run_cli(["wait", "gone1", "--timeout", "30"])
        self.assertLess(time.time() - started, 2, "dead foreground wait did not settle")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["stopReason"], "error")
        self.assertEqual(payload["error"], "wrapper died before writing a result")
        self.assertIsNone(payload["diffPath"])
        self.assertTrue((rec.path().parent / "result.json").exists())

    def test_result_settles_a_dead_record(self):
        rec = self._job("gone2", pid=999999)
        code, out, err = self.run_cli(["result", "gone2"])
        self.assertEqual(code, 1, err)
        payload = json.loads(out)
        self.assertEqual(payload["error"], "wrapper died before writing a result")
        self.assertTrue((rec.path().parent / "result.json").exists())

    def test_status_dead_record_reads_as_error_not_stalled(self):
        self._job("gone3", pid=999999)
        code, out, err = self.run_cli(["status", "gone3"])
        self.assertEqual(code, 0, err)
        self.assertIn("error", out)
        self.assertNotIn("stalled", out)

    def test_status_live_uses_progress_line_in_list_and_detail(self):
        rec = self._job("live2", pid=os.getpid(), started_at=time.time() - 90)
        progress = {
            "lastKind": "thought",
            "lastEventAt": time.time(),
            "lastToolAt": time.time() - 30,
            "lastWriteAt": None,
            "counts": {"tool_call": 9, "write": 0, "failed_tool": 0},
            "usage": {"output": 36100, "reasoning": 36000, "requests": 1},
        }
        (rec.path().parent / "progress.json").write_text(json.dumps(progress), encoding="utf-8")
        code, out, err = self.run_cli(["status", "live2"])
        self.assertEqual(code, 0, err)
        self.assertIn("live2", out)
        self.assertIn("tools 9 (writes 0, failed 0)", out)
        self.assertIn("tokens out 36.1k (reasoning 36.0k)", out)
        self.assertIn("last write never", out)
        code, listed, err = self.run_cli(["status"])
        self.assertEqual(code, 0, err)
        self.assertIn("tokens out 36.1k (reasoning 36.0k)", listed)

    def test_status_without_progress_uses_empty_progress(self):
        rec = self._job("old1", pid=os.getpid())
        hb = rec.path().parent / "heartbeat"
        hb.write_text("")
        os.utime(hb, None)
        code, out, err = self.run_cli(["status", "old1"])
        self.assertEqual(code, 0, err)
        self.assertIn("last event never", out)
        self.assertIn("last tool never", out)
        self.assertRegex(out, r"heartbeat \d+s ago")
        self.assertNotIn("running (heartbeat", out)

    def test_status_finished_job_prints_usage(self):
        rec = self._job("done2", pid=os.getpid(), status="done")
        usage = {"input": 14, "output": 3, "reasoning": 3, "cache_read": 0, "requests": 1}
        self._result(rec, {
            "exitCode": 0, "stopReason": "end_turn",
            "providerExtra": {"usage": usage, "argv": ["grok"]},
        })
        code, out, err = self.run_cli(["status", "done2"])
        self.assertEqual(code, 0, err)
        self.assertIn("usage:", out)
        self.assertIn('"requests": 1', out)
        code, listed, err = self.run_cli(["status"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("usage:", listed)
        self.assertIn("end_turn", listed)


class TestLogs(CliTestBase):
    def _stream(self, run_id, provider, lines):
        from delegate.jobs import JobRecord
        rec = JobRecord(run_id=run_id, provider=provider, cwd="/work")
        rec.save()
        path = rec.path().parent / "stream.jsonl"
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        return lines

    def test_pi_compact_versus_raw_tail(self):
        lines = self._stream("pi-logs", "pi", [
            json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "thinking_delta", "delta": "a"}}),
            json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "thinking_delta", "delta": "b"}}),
            json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "text_delta", "delta": "hello"}}),
            json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "toolcall_end",
                "toolCall": {"name": "write", "arguments": {"file_path": "out.txt"}},
            }}),
            json.dumps({"type": "tool_execution_end", "toolName": "bash", "isError": True}),
            json.dumps({"type": "tool_execution_end", "toolName": "write", "isError": False}),
            json.dumps({"type": "message_end", "message": {
                "role": "assistant",
                "usage": {"input": 1, "output": 1, "reasoning": 0, "cacheRead": 0,
                          "cost": {"total": 0.25}},
            }}),
            json.dumps({"type": "turn_end"}),
            json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "text_delta", "delta": "done"}}),
            json.dumps({"type": "agent_end", "willRetry": False}),
        ])
        expected = [
            "  … thinking (2 events)",
            "hello",
            "  → write out.txt",
            "  ✗ bash failed",
            "done",
        ]
        code, out, err = self.run_cli(["logs", "pi-logs"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), expected)
        code, raw, err = self.run_cli(["logs", "pi-logs", "--raw"])
        self.assertEqual(code, 0, err)
        self.assertEqual(raw.splitlines(), lines)
        code, tail, err = self.run_cli(["logs", "pi-logs", "--tail", "1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(tail.splitlines(), ["done"])
        code, raw_tail, err = self.run_cli(["logs", "pi-logs", "--raw", "--tail", "1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(raw_tail.splitlines(), [lines[-1]])
        self.assertNotEqual(tail, raw_tail)

    def test_grok_job_uses_the_grok_parser(self):
        self._stream("grok-logs", "grok", [
            json.dumps({"type": "thought", "data": "a"}),
            json.dumps({"type": "thought", "data": "b"}),
            json.dumps({"type": "tool_call", "name": "read_file", "input": {"path": "a.py"}}),
            json.dumps({"type": "tool_call_update", "status": "failed", "name": "read_file"}),
            json.dumps({"type": "text", "data": "hi"}),
        ])
        code, out, err = self.run_cli(["logs", "grok-logs", "--tail", "10"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), [
            "  … thinking (2 events)",
            "  → read_file a.py",
            "  ✗ read_file failed",
            "hi",
        ])

    def test_consecutive_text_deltas_join_into_lines(self):
        self._stream("grok-words", "grok", [
            json.dumps({"type": "text", "data": "Hel"}),
            "not json",
            json.dumps({"type": "text", "data": "lo "}),
            json.dumps({"type": "text", "data": "wor"}),
            json.dumps({"type": "text", "data": "ld\nNext"}),
            json.dumps({"type": "thought", "data": "pause"}),
            json.dumps({"type": "text", "data": "after "}),
            json.dumps({"type": "text", "data": "tool"}),
            json.dumps({"type": "tool_call", "name": "read_file",
                        "input": {"path": "a.py"}}),
        ])
        code, out, err = self.run_cli(["logs", "grok-words"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), [
            "Hello world",
            "Next",
            "  … thinking (1 events)",
            "after tool",
            "  → read_file a.py",
        ])

    def test_unknown_provider_stays_raw(self):
        lines = self._stream("unknown-logs", "unknown", [
            json.dumps({"type": "message_update", "assistantMessageEvent": {
                "type": "text_delta", "delta": "hello"}}),
            "plain line",
        ])
        code, out, err = self.run_cli(["logs", "unknown-logs"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), lines)
        from delegate.jobs import run_dir_for
        orphan = run_dir_for("no-job")
        orphan.mkdir(parents=True)
        (orphan / "stream.jsonl").write_text("only raw\n", encoding="utf-8")
        code, out, err = self.run_cli(["logs", "no-job"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "only raw\n")
        code, out, err = self.run_cli(["logs", "missing-job"])
        self.assertEqual(code, 1)
        self.assertIn("no stream log", err)


if __name__ == "__main__":
    unittest.main()
