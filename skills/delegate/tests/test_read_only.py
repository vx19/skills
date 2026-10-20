"""--read-only, --web, --schema and answer capture (ticket 09, phase 1).

Posture checks are offline dry-runs. Lifecycle checks use real processes, fake
runners and disposable git repos, like test_core.
"""
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delegate import answer, cli, core, jobs  # noqa: E402
from delegate.prompt import BOILERPLATE, READ_ONLY_BOILERPLATE  # noqa: E402
from delegate.providers.base import Job  # noqa: E402
from delegate.providers.cursor import Cursor  # noqa: E402
from delegate.providers.grok import Grok  # noqa: E402

USAGE_EXIT = 64


class _State(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
                                      "GROK_AGENT": "",
                                      "DELEGATE_GROK_PROFILE_APPLY": "yes"})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def dry(self, provider, *flags):
        code, out, err = self.run_cli(["run", "--provider", provider, "--cwd", ".",
                                       "--dry-run", *flags, "look at it"])
        self.assertEqual(code, 0, err)
        return json.loads(out)["providerExtra"]["argv"], err


class PostureTests(_State):
    def test_grok_read_only_uses_kernel_profile_and_drops_web(self):
        argv, _ = self.dry("grok", "--read-only")
        self.assertIn("read-only", argv)
        self.assertNotIn("workspace", argv)
        self.assertIn("--disable-web-search", argv)
        argv, _ = self.dry("grok", "--read-only", "--web")
        self.assertNotIn("--disable-web-search", argv)

    def test_dry_run_envelope_echoes_read_only(self):
        code, out, err = self.run_cli(["run", "--provider", "grok", "--cwd", ".",
                                       "--dry-run", "--read-only", "x"])
        self.assertTrue(json.loads(out)["readOnly"], err)

    def test_grok_default_posture_unchanged(self):
        argv, _ = self.dry("grok")
        profile = argv[argv.index("--sandbox") + 1]
        self.assertNotIn(profile, ("read-only", "off"))
        self.assertNotIn("--disable-web-search", argv)

    def test_claude_read_only_is_plan_mode_without_edit_tools(self):
        argv, _ = self.dry("claude", "--read-only")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        denied = argv[argv.index("--disallowed-tools") + 1].split(",")
        for tool in ("Edit", "Write", "NotebookEdit", "WebSearch", "WebFetch"):
            self.assertIn(tool, denied)
        argv, _ = self.dry("claude", "--read-only", "--web")
        self.assertNotIn("WebSearch", argv[argv.index("--disallowed-tools") + 1])

    def test_codex_read_only_sandbox_replaces_approve_for_me(self):
        argv, err = self.dry("codex", "--read-only")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn('approval_policy="never"', argv)
        self.assertNotIn("--approve-for-me", argv)
        self.assertIn("cannot switch web access for codex", err)

    def test_codex_always_captures_last_message(self):
        argv, _ = self.dry("codex")
        self.assertTrue(argv[argv.index("--output-last-message") + 1].endswith("answer.txt"))

    def test_cursor_read_only_is_plan_mode(self):
        argv, _ = self.dry("cursor", "--read-only")
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")

    def test_read_only_prompt_drops_implement_directly(self):
        argv, _ = self.dry("claude", "--read-only")
        self.assertIn(READ_ONLY_BOILERPLATE, argv[-1])
        self.assertNotIn(BOILERPLATE, argv[-1])

    def test_web_without_read_only_warns(self):
        _, err = self.dry("grok", "--web")
        self.assertIn("--web only matters with --read-only", err)

    def test_schema_rides_in_the_prompt(self):
        schema = self.root / "s.json"
        schema.write_text(json.dumps({"type": "object", "required": ["verdict"]}))
        argv, _ = self.dry("grok", "--schema", str(schema))
        self.assertIn('"verdict"', argv[-1])


class RefusalTests(_State):
    def refused(self, *argv):
        code, out, err = self.run_cli(["run", "--cwd", ".", "--dry-run", *argv, "x"])
        self.assertEqual(code, USAGE_EXIT, err)
        return err

    def test_providers_without_a_no_write_mode_are_refused(self):
        self.assertIn("no mode that stops writes",
                      self.refused("--provider", "pi", "--read-only"))

    def test_contradictions_are_refused(self):
        self.refused("--provider", "grok", "--read-only", "--no-sandbox")
        self.refused("--provider", "grok", "--read-only", "--report", str(self.root / "r.md"))

    def test_schema_must_be_a_json_object(self):
        bad = self.root / "bad.json"
        bad.write_text("[1]")
        self.refused("--provider", "grok", "--schema", str(bad))
        self.refused("--provider", "grok", "--schema", str(self.root / "missing.json"))


class Scripted(Grok):
    """Grok's parser over a scripted python runner."""
    runner = sys.executable

    def __init__(self, script):
        self.script = script

    def trust(self, cwd, job=None):
        return None

    def argv(self, job):
        return ["-c", self.script]


def emit(*lines, then=""):
    body = "".join(f"print({json.dumps(json.dumps(line))});" for line in lines)
    return body + then


class LifecycleTests(_State):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True,
                                        capture_output=True)
        git("init", "-q")
        (self.repo / "base").write_text("base\n")
        git("add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self.job = Job("grok", self.repo, "task", "run", jobs.run_dir_for("run"))

    def test_answer_is_text_after_the_last_tool_call(self):
        env = core.run(self.job, Scripted(emit(
            {"type": "text", "data": "Let me look."},
            {"type": "tool_call", "toolName": "read_file"},
            {"type": "tool_call_update", "status": "completed"},
            {"type": "text", "data": "Verdict: "},
            {"type": "thought", "data": "hidden"},
            {"type": "text", "data": "pass."},
            {"type": "end", "stopReason": "end_turn", "num_turns": 2},
        )), quiet=True)
        self.assertEqual(env.exit_code, 0, env.error)
        self.assertEqual(Path(env.answer_path).read_text(), "Verdict: pass.")

    def test_no_text_means_no_answer_path(self):
        env = core.run(self.job, Scripted("pass"), quiet=True)
        self.assertIsNone(env.answer_path)

    def test_read_only_write_fails_closed(self):
        self.job.read_only = True
        env = core.run(self.job, Scripted(
            "from pathlib import Path; Path('leak').write_text('x')"), quiet=True)
        self.assertEqual(env.exit_code, 2)
        self.assertIn("work tree changed while the read-only run was active", env.error)
        self.assertIn("another process wrote it", env.error)
        self.assertTrue(env.read_only)

    def test_read_only_clean_run_succeeds(self):
        self.job.read_only = True
        env = core.run(self.job, Scripted(emit({"type": "text", "data": "ok"})), quiet=True)
        self.assertEqual((env.exit_code, env.empty_diff, env.read_only), (0, True, True))

    def test_schema_mismatch_is_a_worker_failure(self):
        self.job.schema = {"type": "object", "required": ["verdict"]}
        env = core.run(self.job, Scripted(emit({"type": "text", "data": '{"other": 1}'})),
                       quiet=True)
        self.assertEqual(env.exit_code, 1)
        self.assertIn("verdict", env.error)
        self.assertIsNotNone(env.answer_path)  # the bad answer stays inspectable

    def test_schema_match_saves_the_bare_object(self):
        self.job.schema = {"type": "object", "required": ["verdict"]}
        env = core.run(self.job, Scripted(emit(
            {"type": "text", "data": 'Done.\n```json\n{"verdict": "pass"}\n```'})), quiet=True)
        self.assertEqual(env.exit_code, 0, env.error)
        self.assertTrue(env.answer_path.endswith("answer.json"))
        self.assertEqual(json.loads(Path(env.answer_path).read_text()), {"verdict": "pass"})
        # the raw final message stays next to it
        self.assertIn("Done.", (Path(env.answer_path).parent / "answer.txt").read_text())

    def test_the_launch_is_recorded_with_the_prompt_elided(self):
        self.job.prompt = "SECRET-LOOKING TASK BODY"
        prov = Scripted("pass")
        prov.argv = lambda job: ["-c", "pass", "--single", job.prompt]
        env = core.run(self.job, prov, quiet=True)
        run_dir = Path(env.log_path).parent
        command = json.loads((run_dir / "command.json").read_text())
        self.assertNotIn("SECRET-LOOKING TASK BODY", json.dumps(command))
        self.assertIn("<prompt: 24 chars, prompt.txt>", command["argv"])
        self.assertEqual((run_dir / "prompt.txt").read_text(), "SECRET-LOOKING TASK BODY")


class ParserTests(unittest.TestCase):
    def test_grok_text_chunks_carry_data(self):
        ev = Grok().parse(json.dumps({"type": "text", "data": "I'll"}))
        self.assertEqual((ev.kind, ev.text), ("text", "I'll"))

    def test_cursor_result_line_is_the_answer(self):
        ev = Cursor().parse(json.dumps({"type": "result", "subtype": "success",
                                        "result": "done"}))
        self.assertEqual((ev.kind, ev.text), ("end", "done"))
        self.assertIsNone(Cursor().parse(json.dumps({"type": "assistant"})))


class AnswerCheckTests(unittest.TestCase):
    schema = {"required": ["a", "b"]}

    def reason(self, text):
        return answer.check(text, self.schema)[1]

    def test_reasons(self):
        self.assertEqual(answer.check('{"a":1,"b":2}', self.schema), ({"a": 1, "b": 2}, None))
        self.assertIn("missing required key(s): b", self.reason('{"a":1}'))
        self.assertIn("holds no JSON object", self.reason("prose"))
        self.assertIn("not a JSON object", self.reason("[1]"))
        self.assertIn("no final answer", self.reason(None))

    def test_narration_around_the_object_is_tolerated(self):
        fenced = 'Checked.\n```json\n{"a": 1, "b": 2}\n```\nThat is all.'
        self.assertEqual(answer.check(fenced, self.schema)[0], {"a": 1, "b": 2})
        inline = 'My verdict: {"a": 1, "b": {"c": 2}} -- done'
        self.assertEqual(answer.check(inline, self.schema)[0], {"a": 1, "b": {"c": 2}})


if __name__ == "__main__":
    unittest.main()
