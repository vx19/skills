"""Each provider's launch argv, one table: delegate flags -> runner flags.

A dry run prints the argv without launching anything. Flags are checked as
exact runs of list items (`--sandbox`, `workspace`), never as substrings, so
`--trust` cannot match `--trusted` or a word inside the prompt.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.helpers import run_cli  # noqa: E402

KNOBS = ["--model", "M", "--effort", "high", "--max-turns", "9"]
CWD = "<cwd>"  # replaced by the test's --cwd

# (provider, delegate flags, runs that must appear, flags that must not)
CASES = [
    ("grok", [], [("--cwd", CWD), ("--permission-mode", "auto"), ("--no-plan",),
                  ("--no-subagents",), ("--output-format", "streaming-json"),
                  ("--sandbox", "workspace"), ("--trust",)],
     ["--disable-web-search"]),
    ("grok", ["--read-only"], [("--sandbox", "read-only"), ("--trust",),
                               ("--disable-web-search",)], []),
    ("grok", ["--read-only", "--web"], [("--sandbox", "read-only")],
     ["--disable-web-search"]),
    ("grok", ["--no-sandbox"], [("--sandbox", "off")], []),
    ("grok", ["--provider-arg", "sandbox=personal"], [("--sandbox", "personal")], []),
    ("grok", ["--provider-arg", "disallowed-tools=web_search,web_fetch",
              "--provider-arg", "deny=*__*", "--provider-arg", "no-memory=true"],
     [("--disallowed-tools", "web_search,web_fetch"), ("--deny", "*__*"),
      ("--no-memory",)], []),
    ("grok", KNOBS, [("--max-turns", "9"), ("--model", "M"),
                     ("--reasoning-effort", "high")], []),
    ("claude", [], [("-p",), ("--permission-mode", "auto"), ("--add-dir", CWD),
                    ("--output-format", "stream-json"), ("--verbose",),
                    ("--no-session-persistence",)],
     ["--disallowed-tools"]),
    ("claude", ["--read-only"],
     [("--permission-mode", "plan"),
      ("--disallowed-tools", "Edit,Write,MultiEdit,NotebookEdit,WebSearch,WebFetch")], []),
    ("claude", ["--read-only", "--web"],
     [("--disallowed-tools", "Edit,Write,MultiEdit,NotebookEdit")], []),
    ("claude", KNOBS, [("--max-turns", "9"), ("--model", "M"), ("--effort", "high")], []),
    # --approve-for-me and --sandbox are mutually exclusive in codex.
    ("codex", [], [("exec",), ("--cd", CWD), ("--skip-git-repo-check",), ("--json",),
                   ("--ephemeral",), ("--approve-for-me",)],
     ["--sandbox"]),
    ("codex", ["--read-only"], [("--sandbox", "read-only"),
                                ("-c", 'approval_policy="never"')],
     ["--approve-for-me"]),
    ("codex", ["--no-sandbox"], [("--dangerously-bypass-approvals-and-sandbox",)],
     ["--approve-for-me", "--sandbox"]),
    ("codex", KNOBS, [("-m", "M"), ("-c", 'model_reasoning_effort="high"')],
     ["--max-turns"]),
    ("cursor", [], [("--print",), ("--workspace", CWD), ("--trust",), ("--force",),
                    ("--output-format", "stream-json"), ("--sandbox", "enabled")],
     ["--mode"]),
    ("cursor", ["--read-only"], [("--sandbox", "enabled"), ("--mode", "plan")], []),
    ("cursor", ["--no-sandbox"], [("--sandbox", "disabled")], []),
    ("cursor", KNOBS, [("--model", "M")], ["--max-turns", "--effort"]),
    ("pi", [], [("-p",), ("--no-session",), ("--no-extensions",), ("--mode", "json")], []),
    ("pi", ["--provider-arg", "provider=openai"], [("--provider", "openai")], []),
    ("pi", KNOBS, [("--model", "M"), ("--thinking", "high")], ["--max-turns"]),
]

RUNNER = {"grok": "grok", "claude": "claude", "codex": "codex",
          "cursor": "cursor-agent", "pi": "pi"}


def _has_run(argv: list[str], run: tuple[str, ...]) -> bool:
    n = len(run)
    return any(tuple(argv[i:i + n]) == run for i in range(len(argv) - n + 1))


class ArgvTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = str(Path(tmp.name).resolve())
        # Assume grok's read-only profile applies; a sandboxed host would flip it.
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(tmp.name) / "state"),
                                      "DELEGATE_GROK_PROFILE_APPLY": "yes",
                                      "GROK_AGENT": ""})
        env.start()
        self.addCleanup(env.stop)

    def argv(self, provider, flags, task="do the thing"):
        code, out, err = run_cli(["run", "--provider", provider, "--cwd", self.cwd,
                                  "--dry-run", *flags, "--", task])
        self.assertEqual(code, 0, err)
        return json.loads(out)["providerExtra"]["argv"]

    def test_each_flag_set_maps_to_the_runner_flags(self):
        for provider, flags, present, absent in CASES:
            with self.subTest(provider=provider, flags=flags):
                argv = self.argv(provider, flags)
                self.assertEqual(argv[0], RUNNER[provider])
                for run in present:
                    run = tuple(self.cwd if t == CWD else t for t in run)
                    self.assertTrue(_has_run(argv, run), f"{run} not in {argv}")
                for flag in absent:
                    self.assertNotIn(flag, argv[:-1])

    def test_the_prompt_reaches_each_runner_its_own_way(self):
        # Through stdin or a file it is not capped by the OS, nor shown in
        # the process list; as an argument it follows `--`, so `-x` stays text.
        for provider in RUNNER:
            with self.subTest(provider=provider):
                argv = self.argv(provider, [], task="-x dashy")
                prompt = [a for a in argv if "-x dashy" in a]
                if provider == "grok":
                    self.assertEqual(prompt, [])
                    self.assertTrue(argv[argv.index("--prompt-file") + 1].endswith("prompt.txt"))
                elif provider == "codex":
                    self.assertEqual((prompt, argv[-1]), ([], "-"))
                elif provider == "claude":
                    self.assertEqual(prompt, [])
                else:
                    self.assertEqual(argv[-2], "--")
                    self.assertTrue(argv[-1].startswith("-x dashy"))


if __name__ == "__main__":
    unittest.main()
