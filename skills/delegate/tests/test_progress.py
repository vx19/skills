"""Live progress.json and grok usage totals. Offline, synthetic lines only."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delegate import core, jobs
from delegate.providers.base import Event, Job
from delegate.providers.grok import Grok
from test_core import Fake


_ZERO_COUNTS = {"tool_call": 0, "write": 0, "failed_tool": 0}
_ZERO_USAGE = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "requests": 0}


def _lines():
    """One short grok-like stream: thought, two usage deltas, read, write, failure, text."""
    return [
        {"type": "thought", "data": "checking the fixture"},
        {"type": "usage", "usage": {"input_tokens": 10, "output_tokens": 2,
                                    "reasoning_tokens": 3, "cache_read_input_tokens": 1}},
        {"type": "tool_call", "toolName": "read_file", "kind": "read",
         "rawInput": {"target_file": "src/a.py"}},
        {"type": "usage", "usage": {"input_tokens": 4, "output_tokens": 1,
                                    "reasoning_tokens": 0, "cache_read_input_tokens": 2}},
        {"type": "tool_call", "toolName": "write", "kind": "edit",
         "rawInput": {"file_path": "src/b.py", "content": "x"}},
        {"type": "tool_call_update", "status": "failed", "name": "write"},
        {"type": "text", "data": "blocked"},
    ]


def _script(lines, *, exit_code=0, cancel_path=None, hang=False):
    payload = "".join(json.dumps(line) + "\n" for line in lines)
    parts = [
        "import sys, time",
        "from pathlib import Path",
        f"sys.stdout.write({payload!r})",
        "sys.stdout.flush()",
    ]
    if cancel_path is not None:
        parts.append(f"Path({cancel_path!r}).write_text('x')")
        parts.append("time.sleep(30)")
    elif hang:
        parts.append("time.sleep(30)")
    else:
        parts.append(f"sys.exit({int(exit_code)})")
    return "\n".join(parts)


class GrokLike(Fake):
    """Fake runner (python -c) that core parses with the real grok parser."""

    name = "grok"
    parses_stream = True

    def parse(self, line):
        return Grok().parse(line)


class AccumTests(unittest.TestCase):
    def test_two_usage_events_sum_and_count_requests(self):
        acc = core._Accum()
        acc.add(Event(kind="usage", tokens={"input": 10, "output": 2, "reasoning": 3, "cache_read": 1}))
        acc.add(Event(kind="usage", tokens={"input": 5, "output": 1, "reasoning": 0, "cache_read": 4, "nope": 9}))
        acc.add(Event(kind="usage"))
        self.assertEqual(acc.usage, {"input": 15, "output": 3, "reasoning": 3,
                                     "cache_read": 5, "requests": 3})
        self.assertIsNone(acc.turns)
        self.assertIsNone(acc.cost_usd)
        self.assertIsNone(acc.answer())

    def test_kind_counts_writes_and_timestamps(self):
        acc = core._Accum()
        before = time.time()
        acc.add(Event(kind="thought"))
        acc.add(Event(kind="text", text="hi"))
        acc.add(Event(kind="tool_call", name="read_file", path="src/a.py", write=False))
        acc.add(Event(kind="tool_call", name="write", path="src/b.py", write=True))
        acc.add(Event(kind="tool_result", name="write", ok=False))
        after = time.time()
        self.assertEqual(acc.counts, {"tool_call": 2, "write": 1, "failed_tool": 1})
        self.assertEqual(acc.failed_tool_calls, 1)
        self.assertEqual(acc.last_kind, "tool_result")
        self.assertFalse(hasattr(acc, "last_path"))
        self.assertGreaterEqual(acc.last_event_at, before)
        self.assertLessEqual(acc.last_event_at, after)
        self.assertEqual(acc.last_event_at, acc.last_tool_at)
        self.assertGreaterEqual(acc.last_tool_at, acc.last_write_at)
        # A thought is not tool activity and is not the answer.
        fresh = core._Accum()
        fresh.add(Event(kind="thought"))
        self.assertIsNone(fresh.last_tool_at)
        self.assertIsNone(fresh.last_write_at)
        self.assertIsNone(fresh.answer())


class ProgressRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True, capture_output=True)
        (self.repo / "base").write_text("base\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=test", "-c", "user.email=test@test",
                        "commit", "-qm", "initial"], check=True, capture_output=True)
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.job = Job("grok", self.repo, "task", "run", jobs.run_dir_for("run"))

    def progress(self):
        return json.loads((self.job.run_dir / "progress.json").read_text(encoding="utf-8"))

    def test_nonzero_exit_records_progress_and_usage_without_turns(self):
        env = core.run(self.job, GrokLike(_script(_lines(), exit_code=1)), quiet=True)
        self.assertEqual(env.stop_reason, "error")
        self.assertEqual(env.exit_code, 1)
        self.assertIsNone(env.turns)
        self.assertIsNone(env.cost_usd)
        usage = {"input": 14, "output": 3, "reasoning": 3, "cache_read": 3, "requests": 2}
        self.assertEqual(env.provider_extra["usage"], usage)
        progress = self.progress()
        self.assertNotIn("lines", progress)
        self.assertNotIn("lastPath", progress)
        self.assertEqual(progress["lastKind"], "text")
        self.assertEqual(progress["counts"], {"tool_call": 2, "write": 1, "failed_tool": 1})
        self.assertEqual(progress["usage"], usage)
        self.assertIsInstance(progress["updatedAt"], float)
        self.assertIsInstance(progress["lastEventAt"], float)
        self.assertIsInstance(progress["lastToolAt"], float)
        self.assertIsInstance(progress["lastWriteAt"], float)
        self.assertFalse((self.job.run_dir / "progress.json.tmp").exists())

    def test_cancel_and_timeout_keep_usage(self):
        cancel = str(self.job.run_dir / "cancel")
        env = core.run(self.job, GrokLike(_script(_lines(), cancel_path=cancel)),
                       quiet=True, idle_timeout=5)
        self.assertEqual((env.stop_reason, env.exit_code), ("cancelled", 1))
        self.assertEqual(env.provider_extra["usage"]["requests"], 2)
        self.assertEqual(env.provider_extra["usage"]["input"], 14)
        self.assertIsNone(env.turns)
        self.assertIsNone(env.cost_usd)
        self.assertEqual(self.progress()["lastKind"], "text")
        self.assertEqual(self.progress()["counts"]["write"], 1)

        self.job.run_id = "run-timeout"
        self.job.run_dir = jobs.run_dir_for("run-timeout")
        env = core.run(self.job, GrokLike(_script(_lines()[:2], hang=True)),
                       quiet=True, idle_timeout=0.6)
        self.assertEqual(env.stop_reason, "timeout")
        self.assertEqual(env.provider_extra["usage"],
                         {"input": 10, "output": 2, "reasoning": 3, "cache_read": 1, "requests": 1})
        self.assertEqual(self.progress()["lastKind"], "usage")
        self.assertIsNone(env.turns)
        self.assertIsNone(env.cost_usd)

    def test_end_event_still_owns_turns_and_cost(self):
        lines = [
            {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 1,
                                        "reasoning_tokens": 0, "cache_read_input_tokens": 0}},
            {"type": "result", "stopReason": "end_turn", "num_turns": 2, "total_cost_usd": 0.5},
        ]
        env = core.run(self.job, GrokLike(_script(lines, exit_code=0)), quiet=True)
        self.assertEqual(env.stop_reason, "end_turn")
        self.assertEqual(env.turns, 2)
        self.assertAlmostEqual(env.cost_usd, 0.5)
        self.assertEqual(env.provider_extra["usage"],
                         {"input": 3, "output": 1, "reasoning": 0, "cache_read": 0, "requests": 1})

    def test_unparsed_stream_writes_lines_only(self):
        env = core.run(self.job, Fake("import sys; sys.stdout.write('tick\\n')"), quiet=True)
        self.assertEqual(env.exit_code, 0)
        self.assertNotIn("usage", env.provider_extra)
        progress = self.progress()
        self.assertNotIn("lines", progress)
        self.assertNotIn("lastPath", progress)
        self.assertIsInstance(progress["updatedAt"], float)
        self.assertIsInstance(progress["lastEventAt"], float)
        self.assertIsNone(progress["lastKind"])
        self.assertIsNone(progress["lastToolAt"])
        self.assertIsNone(progress["lastWriteAt"])
        self.assertEqual(progress["counts"], _ZERO_COUNTS)
        self.assertEqual(progress["usage"], _ZERO_USAGE)

    def test_progress_write_oserror_is_swallowed(self):
        missing = Path(self.root) / "no-such-dir" / "nested"
        core._write_progress(missing, core._Accum())


if __name__ == "__main__":
    unittest.main()
