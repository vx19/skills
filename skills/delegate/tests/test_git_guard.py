"""A worker must not get the wrapper's own git to run its code, or change what
git does without the run saying so."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delegate import core, jobs
from delegate.providers.base import Job, Provider
from tests.helpers import git, init_repo  # noqa: E402


class Fake(Provider):
    name = "fake"
    runner = sys.executable
    parses_stream = False

    def __init__(self, script):
        self.script = script

    def argv(self, job):
        return ["-c", self.script]


class GitGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = init_repo(self.root / "repo", {"base.dat": "base\n",
                                                   ".gitattributes": "*.dat filter=evil\n"})
        self.marker = self.root / "marker"
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.job = Job("fake", self.repo, "task", "run", jobs.run_dir_for("run"))

    def git(self, *args):
        return git(self.repo, *args)

    def plant(self, key, command):
        """A worker script line that sets one repo config key to run `command`."""
        return ("import subprocess; subprocess.run(['git', 'config', "
                f"{key!r}, {command!r}], check=True)\n")

    def worker(self, *lines):
        return Fake("".join(lines) + "open('new.dat', 'w').write('changed\\n')\n")

    def assertFlagged(self, env, *names):
        self.assertEqual((env.stop_reason, env.exit_code), ("error", 2), env.error)
        self.assertIn("worker changed git metadata", env.error)
        for name in names:
            self.assertIn(name, env.error)

    def test_a_planted_fsmonitor_never_runs_in_the_wrapper(self):
        touch = f"touch {self.marker}; false"
        env = core.run(self.job, self.worker(self.plant("core.fsmonitor", touch)))
        self.assertFalse(self.marker.exists(), "wrapper git ran worker-planted code")
        self.assertFlagged(env, ".git/config")

    def test_a_planted_clean_filter_never_runs_in_the_wrapper(self):
        touch = f"touch {self.marker}; cat"
        env = core.run(self.job, self.worker(
            self.plant("filter.evil.clean", touch),
            self.plant("filter.evil.required", "true")))
        self.assertFalse(self.marker.exists(), "wrapper git ran a worker-planted filter")
        self.assertFlagged(env, ".git/config")
        self.assertIn("new.dat", Path(env.diff_path).read_text())

    def test_a_planted_process_filter_never_runs_in_the_wrapper(self):
        env = core.run(self.job, self.worker(
            self.plant("filter.evil.process", f"touch {self.marker}")))
        self.assertFalse(self.marker.exists())
        self.assertFlagged(env, ".git/config")

    def test_a_planted_textconv_never_runs_in_the_wrapper(self):
        (self.repo / ".gitattributes").write_text("*.dat diff=evil\n")
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "attrs")
        env = core.run(self.job, self.worker(
            self.plant("diff.evil.textconv", f"touch {self.marker}; cat")))
        self.assertFalse(self.marker.exists())
        self.assertFlagged(env, ".git/config")

    def test_a_planted_hook_is_reported(self):
        env = core.run(self.job, Fake(
            "open('.git/hooks/pre-commit', 'w').write('#!/bin/sh\\n')"))
        self.assertFlagged(env, ".git/hooks/pre-commit")

    def test_edited_attributes_are_reported(self):
        env = core.run(self.job, Fake(
            "open('.git/info/exclude', 'a').write('secret/\\n')"))
        self.assertFlagged(env, ".git/info/exclude")

    def test_a_new_tag_is_reported(self):
        env = core.run(self.job, Fake(
            "import subprocess; subprocess.run(['git', 'tag', 'sneaky'], check=True)"))
        self.assertFlagged(env, "refs/tags/sneaky")

    def test_a_read_only_run_is_flagged_too(self):
        self.job.read_only = True
        env = core.run(self.job, Fake(
            "open('.git/hooks/post-checkout', 'w').write('#!/bin/sh\\n')"))
        self.assertFlagged(env, ".git/hooks/post-checkout")

    def test_an_ordinary_run_is_not_flagged(self):
        env = core.run(self.job, Fake("open('new.dat', 'w').write('x\\n')"))
        self.assertEqual((env.stop_reason, env.exit_code), ("end_turn", 0), env.error)

    def test_a_filtered_repo_still_snapshots_the_raw_file(self):
        self.git("config", "filter.evil.clean", "tr a-z A-Z")
        self.git("config", "filter.evil.required", "true")
        env = core.run(self.job, Fake("open('new.dat', 'w').write('hello\\n')"))
        self.assertEqual((env.stop_reason, env.exit_code), ("end_turn", 0), env.error)
        self.assertIn("+hello", Path(env.diff_path).read_text())


if __name__ == "__main__":
    unittest.main()
