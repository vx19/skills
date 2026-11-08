"""--read-only, --web, --schema and answer capture (ticket 09, phase 1).

Posture checks are offline dry-runs. Lifecycle checks use real processes, fake
runners and disposable git repos, like test_core.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delegate import answer, core, jobs  # noqa: E402
from delegate.prompt import BOILERPLATE, READ_ONLY_BOILERPLATE  # noqa: E402
from delegate.providers.base import Job  # noqa: E402
from delegate.providers.cursor import Cursor  # noqa: E402
from delegate.providers.grok import Grok  # noqa: E402
from tests.helpers import init_repo, run_cli  # noqa: E402

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
        return run_cli(argv)

    def dry(self, provider, *flags, key="argv"):
        code, out, err = self.run_cli(["run", "--provider", provider, "--cwd", ".",
                                       "--dry-run", *flags, "look at it"])
        self.assertEqual(code, 0, err)
        return json.loads(out)["providerExtra"][key], err


class PostureTests(_State):
    def test_dry_run_envelope_echoes_read_only(self):
        code, out, err = self.run_cli(["run", "--provider", "grok", "--cwd", ".",
                                       "--dry-run", "--read-only", "x"])
        self.assertTrue(json.loads(out)["readOnly"], err)

    def test_envelope_names_how_read_only_is_enforced(self):
        for provider, expected in (("grok", "kernel"), ("codex", "kernel"),
                                   ("claude", "app"), ("cursor", "app")):
            code, out, err = self.run_cli(["run", "--provider", provider, "--cwd", ".",
                                           "--dry-run", "--read-only", "x"])
            self.assertEqual(json.loads(out)["readOnlyEnforcement"], expected, provider)
        code, out, err = self.run_cli(["run", "--provider", "grok", "--cwd", ".",
                                       "--dry-run", "x"])
        self.assertIsNone(json.loads(out)["readOnlyEnforcement"], err)

    def test_codex_read_only_cannot_switch_web(self):
        _, err = self.dry("codex", "--read-only")
        self.assertIn("cannot switch web access for codex", err)

    def test_codex_always_captures_last_message(self):
        argv, _ = self.dry("codex")
        self.assertTrue(argv[argv.index("--output-last-message") + 1].endswith("answer.txt"))

    def test_read_only_prompt_drops_implement_directly(self):
        prompt, _ = self.dry("claude", "--read-only", key="prompt")
        self.assertIn(READ_ONLY_BOILERPLATE, prompt)
        self.assertNotIn(BOILERPLATE, prompt)

    def test_web_without_read_only_warns(self):
        _, err = self.dry("grok", "--web")
        self.assertIn("--web only matters with --read-only", err)

    def test_schema_rides_in_the_prompt(self):
        schema = self.root / "s.json"
        schema.write_text(json.dumps({"type": "object", "required": ["verdict"]}))
        prompt, _ = self.dry("grok", "--schema", str(schema), key="prompt")
        self.assertIn('"verdict"', prompt)


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

    def argv(self, job):
        return ["-c", self.script]


def emit(*lines, then=""):
    body = "".join(f"print({json.dumps(json.dumps(line))});" for line in lines)
    return body + then


class LifecycleTests(_State):
    def setUp(self):
        super().setUp()
        self.repo = init_repo(self.root / "repo")
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
        self.assertEqual(env.to_json()["readOnlyEnforcement"], "kernel")

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

    def test_a_failed_answer_json_write_still_runs_every_check(self):
        import signal
        self.job.read_only = True
        self.job.schema = {"type": "object", "required": ["verdict"]}
        self.job.run_dir.mkdir(parents=True, exist_ok=True)
        (self.job.run_dir / "answer.json").mkdir()  # the write fails, as on a full disk
        before = signal.getsignal(signal.SIGTERM)
        env = core.run(self.job, Scripted(emit(
            {"type": "text", "data": '{"verdict": "pass"}'},
            then="from pathlib import Path; Path('leak').write_text('x')")), quiet=True)
        self.assertEqual(env.exit_code, 2)
        self.assertIn("work tree changed while the read-only run was active", env.error)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

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
        [ev] = Grok().parse(json.dumps({"type": "text", "data": "I'll"}))
        self.assertEqual((ev.kind, ev.text), ("text", "I'll"))

    def test_cursor_result_line_is_the_answer(self):
        [ev] = Cursor().parse(json.dumps({"type": "result", "subtype": "success",
                                        "result": "done"}))
        self.assertEqual((ev.kind, ev.text), ("end", "done"))
        self.assertEqual(Cursor().parse(json.dumps({"type": "assistant"})), [])


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

    def test_the_last_object_in_prose_with_other_braces_is_the_answer(self):
        two = 'Draft: {"a": 0} and final: {"a": 1, "b": 2}'
        self.assertEqual(answer.check(two, self.schema)[0], {"a": 1, "b": 2})
        braces = 'Set {x} first, then:\n{"a": 1, "b": 2}'
        self.assertEqual(answer.check(braces, self.schema)[0], {"a": 1, "b": 2})


if __name__ == "__main__":
    unittest.main()
