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
from delegate.lib.paths import git_head
from delegate.lib.snapshot import SnapshotError
from tests.helpers import git, init_repo  # noqa: E402

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
        self.repo = init_repo(self.root / "repo")
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
        return git(self.repo, *args)

    def test_an_emptied_worker_group_is_never_signalled_again(self):
        # Once the group is empty and its leader reaped, the id can name another
        # process's group, so the cancel/timeout kill and the final one must not
        # both signal it.
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                start_new_session=True)
        group = core._WorkerGroup(proc)
        group.kill()
        self.assertIsNotNone(proc.returncode)
        with patch("delegate.core.os.killpg") as killpg:
            group.kill()
        killpg.assert_not_called()

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

    def test_a_binary_change_is_an_applicable_patch(self):
        (self.repo / "blob.bin").write_bytes(bytes(range(256)))
        self.git("add", "blob.bin")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "blob")
        env = core.run(self.job, Fake(
            "from pathlib import Path; Path('blob.bin').write_bytes(bytes(range(255, -1, -1)))"))
        self.git("checkout", "--", "blob.bin")
        check = subprocess.run(["git", "-C", str(self.repo), "apply", "--check", env.diff_path],
                               capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_the_stat_counts_a_removed_line_that_starts_with_dashes(self):
        (self.repo / "q.sql").write_text("-- old comment\nselect 1;\n")
        self.git("add", "q.sql")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "sql")
        env = core.run(self.job, Fake(
            "from pathlib import Path; Path('q.sql').write_text('select 1;\\n')"))
        self.assertEqual(env.diff_stat, "1 file(s) changed, +0/-1")

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
        missing = Fake("pass")
        missing.runner = str(self.root / "no-such-runner")
        env = core.run(self.job, missing)
        self.assertEqual(env.exit_code, 2)
        self.assertIn("run failed", env.error)
        self.assertIn("no-such-runner", env.error)

    def test_diff_failure_is_not_success(self):
        with patch("delegate.core.diff_from_tree", side_effect=SnapshotError("broken")):
            env = core.run(self.job, Fake("pass"))
        self.assertEqual(env.exit_code, 2)
        self.assertIn("post-run diff failed", env.error)

    def test_a_late_cancel_or_timeout_leaves_a_stub_report(self):
        report = self.root / "report.md"
        self.job.report = report
        self.job.run_dir.mkdir(parents=True)
        (self.job.run_dir / "cancel").write_text("")
        env = core.run(self.job, Fake("pass"), quiet=True)
        self.assertEqual((env.stop_reason, env.report_exists), ("cancelled", True))
        self.assertIn("Auto-generated stub", report.read_text())
        report.unlink()
        timed = Job("fake", self.repo, "task", "run2", jobs.run_dir_for("run2"), report=report)
        env = core.run(timed, Fake("import time; time.sleep(30)"), idle_timeout=.3, quiet=True)
        self.assertEqual((env.stop_reason, env.report_exists), ("timeout", True))
        self.assertIn("— timeout", report.read_text())

    def test_a_report_the_worker_wrote_is_never_replaced(self):
        report = self.root / "report.md"
        report.write_text("mine")
        self.job.report = report
        env = core.run(self.job, Fake("import time; time.sleep(30)"), idle_timeout=.3, quiet=True)
        self.assertEqual(env.stop_reason, "timeout")
        self.assertEqual(report.read_text(), "mine")

    def test_a_concurrent_run_in_the_same_tree_fails_a_read_only_run(self):
        import threading
        # The read-only run waits for the other run's write, so the order is fixed.
        reviewer = Job("fake", self.repo, "task", "ro", jobs.run_dir_for("ro"), read_only=True)
        writer = Job("fake", self.repo, "task", "rw", jobs.run_dir_for("rw"))
        out = {}
        ready = self.root / "ready"  # outside the tree: not part of any delta
        wait_for_b = ("import pathlib, time\n"
                      f"pathlib.Path({str(ready)!r}).write_text('')\n"
                      "while not pathlib.Path('b').exists(): time.sleep(.02)")
        t = threading.Thread(target=lambda: out.setdefault(
            "ro", core.run(reviewer, Fake(wait_for_b), quiet=True)))
        t.start()
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        out["rw"] = core.run(writer, Fake("open('b', 'w').write('b')"), quiet=True)
        t.join(10)
        self.assertEqual((out["rw"].exit_code, out["ro"].exit_code), (0, 2))
        self.assertIn("b", Path(out["ro"].diff_path).read_text())

    def test_timeout_kills_descendant_that_ignores_term(self):
        script = ("import subprocess, sys, time; "
                  "subprocess.Popen([sys.executable, '-c', "
                  "\"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                  "from pathlib import Path; Path('ready').write_text('yes'); time.sleep(999)\"]); "
                  "time.sleep(999)")
        started = time.monotonic()
        env = core.run(self.job, Fake(script), idle_timeout=.4)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((env.stop_reason, env.to_json()["errorKind"]), ("timeout", "timeout"))
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

    def test_a_stdin_runner_reads_the_prompt_on_stdin(self):
        got = self.root / "got"  # outside the tree: not part of the delta
        provider = Fake(f"import pathlib, sys; pathlib.Path({str(got)!r}).write_text(sys.stdin.read())")
        provider.prompt_via = "stdin"
        self.job.prompt = "a prompt\n" * 20_000  # beyond one argv string
        env = core.run(self.job, provider)
        self.assertEqual(env.exit_code, 0, env.error)
        self.assertEqual(got.read_text(), self.job.prompt)

    def test_a_non_utf8_byte_on_stdout_does_not_end_the_run(self):
        env = core.run(self.job, Fake(
            "import sys; sys.stdout.buffer.write(b'caf\\xe9\\n'); sys.stdout.flush(); print('done')"))
        self.assertEqual((env.stop_reason, env.exit_code), ("end_turn", 0), env.error)
        self.assertIn("done", Path(env.stream_path).read_text())

    def test_ignored_only_write_is_empty_delta(self):
        (self.repo / ".gitignore").write_text("ignored\n")
        self.git("add", ".gitignore")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "ignore")
        env = core.run(self.job, Fake("from pathlib import Path; Path('ignored').write_text('data')"))
        self.assertEqual((env.exit_code, env.empty_diff), (0, True))

    def test_scope_is_relative_to_a_subdirectory_cwd(self):
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "keep").write_text("k\n")
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "pkg")
        self.job.cwd, self.job.scope = self.repo / "pkg", ["src/"]
        env = core.run(self.job, Fake(
            "from pathlib import Path; Path('src').mkdir(); Path('src/a.py').write_text('x'); "
            "Path('../root.txt').write_text('y')"))
        self.assertEqual(env.scope_violations, ["root.txt"])

    def test_the_requested_report_is_not_a_scope_violation(self):
        self.job.scope, self.job.report = ["src/"], self.repo / ".reports" / "r.md"
        env = core.run(self.job, Fake(
            "from pathlib import Path; Path('.reports').mkdir(); "
            "Path('.reports/r.md').write_text('done'); Path('stray').write_text('x')"))
        self.assertTrue(env.report_exists)
        self.assertEqual(env.scope_violations, ["stray"])

    def add_submodule(self):
        lib = self.root / "lib"
        lib.mkdir()
        sub = ["git", "-C", str(lib), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*sub, "init", "-q"], check=True)
        (lib / "f").write_text("lib\n")
        subprocess.run([*sub, "add", "."], check=True)
        subprocess.run([*sub, "commit", "-qm", "lib"], check=True)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "sub")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "sub")
        (self.repo / "sub" / "pre").write_text("dirt before the run\n")

    def test_a_write_inside_a_submodule_is_in_the_delta(self):
        self.add_submodule()
        self.job.scope = ["src/"]
        env = core.run(self.job, Fake("from pathlib import Path; Path('sub/f').write_text('worker')"))
        self.assertFalse(env.empty_diff)
        diff = Path(env.diff_path).read_text()
        self.assertIn("+worker", diff)
        self.assertNotIn("dirt before the run", diff)
        self.assertEqual(env.scope_violations, ["sub/f"])

    def test_a_read_only_write_inside_a_submodule_fails(self):
        self.add_submodule()
        self.job.read_only = True
        env = core.run(self.job, Fake("from pathlib import Path; Path('sub/new').write_text('x')"))
        self.assertEqual(env.exit_code, 2, env.error)
        self.assertIn("read-only", env.error)

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
                  "for _ in range(20):\n"
                  "    print('tick', flush=True)\n"
                  "    time.sleep(0.05)\n")
        with patch.dict(os.environ, {"DELEGATE_IDLE_TIMEOUT": "0.5",
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
                  "    time.sleep(0.05)\n")
        started = time.monotonic()
        with patch.dict(os.environ, {"DELEGATE_IDLE_TIMEOUT": "5",
                                     "DELEGATE_HARD_CAP": "0.5"}):
            env = core.run(self.job, Fake(script))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(env.stop_reason, "timeout")
        self.assertEqual(env.exit_code, 1)
        self.assertTrue(env.error.startswith("hard cap "), env.error)

    def test_stderr_growth_counts_as_output(self):
        script = ("import sys, time\n"
                  "for _ in range(20):\n"
                  "    print('err', file=sys.stderr, flush=True)\n"
                  "    time.sleep(0.05)\n")
        with patch.dict(os.environ, {"DELEGATE_IDLE_TIMEOUT": "0.5",
                                     "DELEGATE_HARD_CAP": "10"}):
            env = core.run(self.job, Fake(script))
        self.assertEqual((env.stop_reason, env.exit_code), ("end_turn", 0), env.error)
        self.assertIsNone(env.error)

    def test_max_idle_records_the_longest_silence(self):
        script = ("import sys, time\n"
                  "print('a', flush=True)\n"
                  "time.sleep(1.1)\n"
                  "print('b', flush=True)\n")
        with patch.dict(os.environ, {"DELEGATE_IDLE_TIMEOUT": "5",
                                     "DELEGATE_HARD_CAP": "10"}):
            env = core.run(self.job, Fake(script))
        self.assertEqual((env.stop_reason, env.exit_code), ("end_turn", 0), env.error)
        self.assertGreaterEqual(env.provider_extra["maxIdleSeconds"], 1)
        progress = json.loads((self.job.run_dir / "progress.json").read_text())
        self.assertGreaterEqual(progress["maxIdleSeconds"], 1)

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
