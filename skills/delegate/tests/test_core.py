"""Offline lifecycle regressions using real processes and disposable git repos."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from delegate import core, jobs
from delegate.providers.base import Job, Provider
from delegate.providers.pi import Pi
from delegate.lib.paths import git_head
from delegate.lib.snapshot import SnapshotError

ROOT = Path(__file__).resolve().parents[1]


class Fake(Provider):
    name = "fake"
    runner = sys.executable
    parses_stream = False

    def __init__(self, script):
        self.script = script

    def argv(self, job):
        return ["-c", self.script]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "base").write_text("base\n")
        self.git("add", ".")
        self.git("-c", "user.name=test", "-c", "user.email=test@test", "commit", "-qm", "initial")
        self.state = self.root / "state"
        # Pi preflight probes $PI_CODING_AGENT_DIR. Point it at a missing dir so
        # this suite never mkdir's inside the operator's ~/.pi/agent.
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state),
                                         "OPENAI_API_KEY": "offline-test",
                                         "PI_CODING_AGENT_DIR": str(self.root / "no-pi-agent")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.job = Job("fake", self.repo, "task", "run", jobs.run_dir_for("run"))

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_noop_succeeds_streaming(self):
        env = core.run(self.job, Fake("pass"))
        self.assertEqual((env.exit_code, env.empty_diff), (0, True))

    def test_dirty_delta_scope_and_report(self):
        (self.repo / "dirty").write_text("pre-existing")
        self.job.scope = ["src/"]
        env = core.run(self.job, Fake("from pathlib import Path; Path('outside').write_text('worker')"))
        self.assertTrue(env.dirty_before)
        self.assertEqual(env.scope_violations, ["outside"])
        self.assertNotIn("dirty", Path(env.diff_path).read_text())
        self.assertIn("1 file(s)", env.diff_stat)

    def test_dot_scope_allows_whole_tree(self):
        self.job.scope = [".", "src/"]
        env = core.run(self.job, Fake("from pathlib import Path; Path('outside').write_text('worker')"))
        self.assertEqual(env.scope_violations, [])

    def test_head_move_empty_and_content_commits(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                before = git_head(self.repo)
                provider = Fake("import subprocess; from pathlib import Path; " +
                    ("" if empty else "Path('worker').write_text('changed'); ") +
                    "subprocess.run(['git','add','-A'],check=True); "
                    "subprocess.run(['git','-c','user.name=t','-c','user.email=t@t',"
                    "'commit','--allow-empty','-qm','worker'],check=True)")
                env = core.run(self.job, provider)
                self.assertEqual(env.exit_code, 2)
                self.assertIn("moved HEAD", env.error)
                self.assertNotEqual(git_head(self.repo), before)  # no history repair
                self.assertEqual(env.empty_diff, empty)

    def test_spawn_failure_is_an_error(self):
        with patch("delegate.core._spawn", side_effect=OSError("spawn failed")):
            env = core.run(self.job, Fake("pass"))
        self.assertEqual(env.exit_code, 2)
        self.assertIn("spawn failed", env.error)

    def test_diff_failure_is_not_success(self):
        with patch("delegate.core.diff_from_tree", side_effect=SnapshotError("broken")):
            env = core.run(self.job, Fake("pass"))
        self.assertEqual(env.exit_code, 2)
        self.assertIn("post-run diff failed", env.error)

    def test_timeout_kills_descendant_that_ignores_term(self):
        script = ("import subprocess, sys, time; "
                  "subprocess.Popen([sys.executable, '-c', "
                  "\"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                  "from pathlib import Path; Path('ready').write_text('yes'); time.sleep(999)\"]); "
                  "time.sleep(999)")
        started = time.monotonic()
        env = core.run(self.job, Fake(script), idle_timeout=.4)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(env.stop_reason, "timeout")
        self.assertIn("ready", Path(env.diff_path).read_text())

    def test_background_child_dies_before_the_delta_is_taken(self):
        """A leader can exit 0 and leave a detached writer behind (it redirects
        its own stdout, so the stream still reaches EOF and the run 'succeeds')."""
        child = ("import os, time\n"
                 "from pathlib import Path\n"
                 "Path('child.pid').write_text(str(os.getpid()))\n"
                 "time.sleep(30)\n")
        leader = ("import subprocess, sys, time\n"
                  "from pathlib import Path\n"
                  f"subprocess.Popen([sys.executable, '-c', {child!r}],\n"
                  "                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                  "deadline = time.monotonic() + 10\n"
                  "while time.monotonic() < deadline and not Path('child.pid').exists():\n"
                  "    time.sleep(0.02)\n")
        env = core.run(self.job, Fake(leader))
        self.assertEqual(env.exit_code, 0, env.error)
        pid = int((self.repo / "child.pid").read_text())
        self.assertFalse(jobs.pid_alive(pid), "stray worker child outlived the run")

    def test_undecodable_diff_is_written_as_exact_bytes(self):
        provider = Fake("open('latin1.txt','wb').write(bytes([0xff, 0xfe, 0x41]))")
        calls = []
        with patch.object(type(provider), "cleanup", lambda self: calls.append("cleanup")):
            env = core.run(self.job, provider)
        self.assertEqual(env.exit_code, 0, env.error)
        self.assertTrue(env.diff_path)
        self.assertIn(b"\xff\xfeA", Path(env.diff_path).read_bytes())
        self.assertEqual(calls, ["cleanup"])  # teardown was not bypassed

    def test_ignored_only_write_is_empty_delta(self):
        (self.repo / ".gitignore").write_text("ignored\n")
        self.git("add", ".gitignore")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "ignore")
        env = core.run(self.job, Fake("from pathlib import Path; Path('ignored').write_text('data')"))
        self.assertEqual((env.exit_code, env.empty_diff), (0, True))

    def test_head_move_takes_precedence_over_timeout(self):
        env = core.run(self.job, Fake(
            "import subprocess,time; subprocess.run(['git','-c','user.name=t',"
            "'-c','user.email=t@t','commit','--allow-empty','-qm','worker'],check=True); "
            "time.sleep(999)"), idle_timeout=.3)
        self.assertEqual(env.exit_code, 2)
        self.assertIn("moved HEAD", env.error)
        self.assertTrue(env.empty_diff)

    def test_idle_limit_lets_a_talking_runner_finish(self):
        """A run that keeps printing survives a silence limit it never exceeds."""
        script = ("import sys, time\n"
                  "for _ in range(10):\n"
                  "    print('tick', flush=True)\n"
                  "    time.sleep(0.2)\n")
        with patch.dict(os.environ, {"DELEGATE_IDLE_TIMEOUT": "1",
                                     "DELEGATE_HARD_CAP": "10"}):
            env = core.run(self.job, Fake(script))
        self.assertEqual((env.stop_reason, env.exit_code), ("end_turn", 0), env.error)
        self.assertIsNone(env.error)

    def test_idle_limit_ends_a_silent_runner(self):
        script = ("import sys, time\n"
                  "print('one', flush=True)\n"
                  "time.sleep(3)\n")
        started = time.monotonic()
        with patch.dict(os.environ, {"DELEGATE_IDLE_TIMEOUT": "1",
                                     "DELEGATE_HARD_CAP": "10"}):
            env = core.run(self.job, Fake(script))
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(env.stop_reason, "timeout")
        self.assertEqual(env.exit_code, 1)
        self.assertTrue(env.error.startswith("idle "), env.error)
        self.assertIn("no runner output since start", env.error)
        self.assertGreaterEqual(env.provider_extra["maxIdleSeconds"], 1)
        progress = json.loads((self.job.run_dir / "progress.json").read_text())
        self.assertGreaterEqual(progress["maxIdleSeconds"], 1)

    def test_hard_cap_ends_a_runner_that_never_goes_quiet(self):
        script = ("import sys, time\n"
                  "while True:\n"
                  "    print('tick', flush=True)\n"
                  "    time.sleep(0.2)\n")
        started = time.monotonic()
        with patch.dict(os.environ, {"DELEGATE_IDLE_TIMEOUT": "1",
                                     "DELEGATE_HARD_CAP": "2"}):
            env = core.run(self.job, Fake(script))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(env.stop_reason, "timeout")
        self.assertEqual(env.exit_code, 1)
        self.assertTrue(env.error.startswith("hard cap "), env.error)

    def test_stderr_growth_counts_as_output(self):
        script = ("import sys, time\n"
                  "for _ in range(10):\n"
                  "    print('err', file=sys.stderr, flush=True)\n"
                  "    time.sleep(0.2)\n")
        with patch.dict(os.environ, {"DELEGATE_IDLE_TIMEOUT": "1",
                                     "DELEGATE_HARD_CAP": "10"}):
            env = core.run(self.job, Fake(script))
        self.assertEqual((env.stop_reason, env.exit_code), ("end_turn", 0), env.error)
        self.assertIsNone(env.error)

    def test_max_idle_records_the_longest_silence(self):
        script = ("import sys, time\n"
                  "print('a', flush=True)\n"
                  "time.sleep(2)\n"
                  "print('b', flush=True)\n")
        with patch.dict(os.environ, {"DELEGATE_IDLE_TIMEOUT": "5",
                                     "DELEGATE_HARD_CAP": "10"}):
            env = core.run(self.job, Fake(script))
        self.assertEqual((env.stop_reason, env.exit_code), ("end_turn", 0), env.error)
        self.assertGreaterEqual(env.provider_extra["maxIdleSeconds"], 2)
        progress = json.loads((self.job.run_dir / "progress.json").read_text())
        self.assertGreaterEqual(progress["maxIdleSeconds"], 2)

    def test_cancel_cli_kills_worker(self):
        for provider, background, direct_signal in (("pi", False, False),
                                                    ("pi", False, True),
                                                    ("pi", True, False)):
            with self.subTest(provider=provider, background=background, direct_signal=direct_signal):
                bindir = self.root / provider
                if bindir.is_file():
                    bindir.unlink()
                bindir.mkdir(exist_ok=True)
                runner = bindir / provider
                runner.write_text("#!/usr/bin/env python3\nimport os\nimport time\nfrom pathlib import Path\n"
                                  "Path('hung.txt').write_text('running')\n"
                                  "Path('worker.pid').write_text(str(os.getpid()))\ntime.sleep(999)\n")
                runner.chmod(0o755)
                (self.repo / "hung.txt").unlink(missing_ok=True)
                (self.repo / "worker.pid").unlink(missing_ok=True)
                child_env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"]}
                argv = [sys.executable, "-m", "delegate.cli", "run", "--provider", provider,
                        "--cwd", str(self.repo), "--quiet", "task"]
                if background:
                    argv.insert(-1, "--background")
                proc = subprocess.Popen(argv, cwd=ROOT, env=child_env, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
                try:
                    handle = json.loads(proc.communicate(timeout=5)[0]) if background else None
                    deadline = time.monotonic() + 10
                    rec = None
                    while time.monotonic() < deadline:
                        live = [r for r in jobs.list_jobs()
                                if (r.run_id == handle["jobId"] if handle else r.pid == proc.pid)]
                        if live and (self.repo / "hung.txt").exists():
                            rec = live[0]
                            break
                        time.sleep(.05)
                    self.assertIsNotNone(rec)
                    if direct_signal:
                        os.kill(rec.pid, signal.SIGTERM)
                    else:
                        self.assertTrue(jobs.cancel(rec.run_id)[0])
                    if background:
                        deadline = time.monotonic() + 10
                        while jobs.read_result(rec.run_id) is None and time.monotonic() < deadline:
                            time.sleep(.05)
                        env = jobs.read_result(rec.run_id)
                        self.assertIsNotNone(env)
                    else:
                        stdout, stderr = proc.communicate(timeout=10)
                        env = json.loads(stdout)
                        self.assertEqual(proc.returncode, 1, stderr)
                    self.assertEqual(env["stopReason"], "cancelled")
                    self.assertIn("hung.txt", Path(env["diffPath"]).read_text())
                    worker_pid = int((self.repo / "worker.pid").read_text())
                    self.assertFalse(jobs.pid_alive(worker_pid))
                    self.assertEqual(jobs.read_result(rec.run_id)["stopReason"], "cancelled")
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
