import json, os, shutil, sys, time, unittest, tempfile
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestJobs(unittest.TestCase):
    def setUp(self):
        import importlib
        from delegate.lib import paths as _paths
        from delegate import jobs as _jobs

        def reload():
            importlib.reload(_paths)
            importlib.reload(_jobs)

        # cleanups run last-in first-out: remove tmp, restore env, then reload
        self.addCleanup(reload)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.environ["XDG_STATE_HOME"] = self.tmp
        # reload after env set so state_root resolves under tmp
        reload()
        self.jobs = _jobs

    def test_save_read_roundtrip(self):
        rec = self.jobs.JobRecord(run_id="r1", provider="grok", cwd="/tmp",
                                  scope=["src"], status="running", pid=os.getpid())
        rec.save()
        got = self.jobs.read_job("r1")
        self.assertIsNotNone(got)
        self.assertEqual(got.provider, "grok")
        self.assertEqual(got.scope, ["src"])

    def test_scope_overlap_detects_live(self):
        rec = self.jobs.JobRecord(run_id="r2", provider="grok", cwd="/tmp",
                                  scope=["packages/core"], status="running",
                                  pid=os.getpid())
        rec.save()
        self.assertIn("r2", self.jobs.scope_overlap(Path("/tmp"), ["packages/core"]))
        self.assertEqual(self.jobs.scope_overlap(Path("/tmp"), ["totally/other"]), [])

    def test_scope_overlap_sees_nesting_the_whole_tree_and_other_cwds(self):
        for run_id, cwd, scope in (("nested", "/r", ["src"]), ("whole", "/w", ["."]),
                                   ("unscoped", "/u", []), ("sub", "/s/pkg", ["src"])):
            self.jobs.JobRecord(run_id=run_id, provider="grok", cwd=cwd, scope=scope,
                                status="running", pid=os.getpid()).save()
        self.assertEqual(self.jobs.scope_overlap(Path("/r"), ["src/api"]), ["nested"])
        self.assertEqual(self.jobs.scope_overlap(Path("/w"), ["docs"]), ["whole"])
        self.assertEqual(self.jobs.scope_overlap(Path("/u"), ["lib"]), ["unscoped"])
        self.assertEqual(self.jobs.scope_overlap(Path("/s"), ["pkg/src/x.py"]), ["sub"])
        self.assertEqual(self.jobs.scope_overlap(Path("/elsewhere"), ["src"]), [])

    def test_dead_pid_no_result_not_live(self):
        # a pid that is (almost certainly) not alive
        rec = self.jobs.JobRecord(run_id="r3", provider="grok", cwd="/tmp",
                                  scope=["x"], status="running", pid=999999)
        rec.save()
        self.assertNotIn("r3", [j.run_id for j in self.jobs.live_jobs()])




class TestLivenessLock(unittest.TestCase):
    """The wrapper's flock — not its pid — answers whether a run is still alive."""

    @unittest.skipIf(os.name == "nt", "flock is POSIX-only")
    def test_lock_roundtrip_drives_the_liveness_answer(self) -> None:
        from unittest.mock import patch
        from delegate import jobs
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"XDG_STATE_HOME": d}):
            rec = jobs.JobRecord(run_id="r1", provider="grok", cwd="/tmp",
                                 status="running", pid=os.getpid())
            rec.save()
            self.assertIsNone(jobs.wrapper_alive("r1"), "no lock file to ask")
            self.assertTrue(jobs.is_wrapper_alive(rec), "pid fallback")
            fd = jobs.hold_live_lock("r1")
            self.assertTrue(jobs.wrapper_alive("r1"), "lock held")
            self.assertTrue(jobs.is_wrapper_alive(rec))
            with self.assertRaises(jobs.RunIdCollision):
                jobs.hold_live_lock("r1")  # a second wrapper for a live run
            os.close(fd)  # the kernel drops the lock with the fd
            self.assertFalse(jobs.wrapper_alive("r1"), "lock released")
            self.assertFalse(jobs.is_wrapper_alive(rec))


class TestProgressFormat(unittest.TestCase):
    """`progress.json` plus the one-line live status built from it."""

    def setUp(self):
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._env = patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        from delegate import jobs
        self.jobs = jobs
        self.now = 1_700_000_000.0

    def _rec(self, started_at):
        rec = self.jobs.JobRecord(
            run_id="r", provider="grok", cwd="/work", status="running",
            pid=os.getpid(), started_at=started_at)
        rec.save()
        return rec

    def _heartbeat(self, age):
        hb = self.jobs.run_dir_for("r") / "heartbeat"
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text("")
        stamp = self.now - age
        os.utime(hb, (stamp, stamp))

    def _write_progress(self, payload):
        path = self.jobs.run_dir_for("r") / "progress.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_read_progress_missing_or_corrupt_is_none(self):
        self._rec(self.now)
        self.assertIsNone(self.jobs.read_progress("r"))
        self.assertIsNone(self.jobs.read_progress("no-such-job"))
        path = self.jobs.run_dir_for("r") / "progress.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.jobs.read_progress("r"))
        path.write_text("[]", encoding="utf-8")
        self.assertIsNone(self.jobs.read_progress("r"))

    def test_format_progress_with_writes_and_tokens(self):
        rec = self._rec(self.now - (14 * 60 + 12))
        self._heartbeat(1)
        progress = {
            "lastKind": "thought",
            "lastEventAt": self.now - 2,
            "lastToolAt": self.now - (11 * 60),
            "lastWriteAt": self.now - (3 * 60 + 5),
            "counts": {"tool_call": 9, "write": 2, "failed_tool": 1},
            "usage": {"input": 10, "output": 36100, "reasoning": 36000,
                      "cache_read": 0, "requests": 1},
        }
        self._write_progress(progress)
        line = self.jobs.format_live_status(rec, self.jobs.read_progress("r"), self.now)
        self.assertEqual(
            line,
            "running 14m12s · last thought 2s ago · last tool 11m00s ago · "
            "last write 3m05s ago · tools 9 (writes 2, failed 1) · "
            "tokens out 36.1k (reasoning 36.0k) · heartbeat 1s ago",
        )

    def test_format_progress_with_no_tool_yet(self):
        rec = self._rec(self.now - (3600 + 2 * 60))
        self._heartbeat(8)
        progress = {
            "lastKind": "thought",
            "lastEventAt": self.now - 45,
            "lastToolAt": None,
            "lastWriteAt": None,
            "counts": {"thought": 1, "tool_call": 0, "write": 0, "failed_tool": 0},
            "usage": {"input": 0, "output": 0, "reasoning": 0, "requests": 0},
        }
        line = self.jobs.format_live_status(rec, progress, self.now)
        self.assertEqual(
            line,
            "running 1h02m · last thought 45s ago · last tool never · "
            "last write never · tools 0 (writes 0, failed 0) · heartbeat 8s ago",
        )
        self.assertNotIn("tokens", line)

    def test_format_without_progress_is_empty_progress(self):
        rec = self._rec(self.now - 45)
        self._heartbeat(5)
        line = (
            "running 45s · last event never · last tool never · "
            "last write never · tools 0 (writes 0, failed 0) · heartbeat 5s ago"
        )
        self.assertEqual(self.jobs.format_live_status(rec, None, self.now), line)
        self.assertEqual(
            self.jobs.format_live_status(rec, self.jobs.read_progress("r"), self.now),
            line,
        )

    def test_duration_and_token_units(self):
        rec = self._rec(self.now - 45)
        self._heartbeat(0)
        bare = {"lastKind": "text", "lastEventAt": self.now, "lastToolAt": None,
                "lastWriteAt": None, "counts": {}, "usage": {"requests": 0}}
        self.assertTrue(
            self.jobs.format_live_status(rec, bare, self.now).startswith("running 45s ·"))
        rec.started_at = self.now - (3 * 60 + 5)
        self.assertTrue(
            self.jobs.format_live_status(rec, bare, self.now).startswith("running 3m05s ·"))
        rec.started_at = self.now - (11 * 60)
        self.assertTrue(
            self.jobs.format_live_status(rec, bare, self.now).startswith("running 11m00s ·"))
        progress = dict(bare)
        progress["usage"] = {"output": 1_500_000, "reasoning": 850, "requests": 2}
        line = self.jobs.format_live_status(rec, progress, self.now)
        self.assertIn("tokens out 1.5M (reasoning 850)", line)

    def test_missing_kind_and_heartbeat_are_named(self):
        rec = self._rec(self.now - 45)
        progress = {
            "lastKind": None, "lastEventAt": self.now - 2,
            "lastToolAt": None, "lastWriteAt": None,
            "counts": {}, "usage": {"requests": 0},
        }
        line = self.jobs.format_live_status(rec, progress, self.now)
        self.assertIn("running 45s · last event 2s ago", line)
        self.assertIn("heartbeat never", line)

    def test_heartbeat_age_takes_the_same_clock(self):
        self._rec(self.now - 45)
        self._heartbeat(5)
        self.assertEqual(self.jobs.heartbeat_age("r", self.now), 5)
        self.assertIsNone(self.jobs.heartbeat_age("no-such", self.now))
        self.assertFalse(hasattr(self.jobs, "_heartbeat_age_at"))


class TestSettleDead(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._env = patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        from delegate import jobs
        self.jobs = jobs

    def _rec(self, run_id="dead", **kw):
        fields = {"pid": 999999, **kw}
        rec = self.jobs.JobRecord(
            run_id=run_id, provider="grok", cwd="/work", status="running",
            **fields)
        rec.save()
        return rec

    def test_dead_record_gets_one_envelope_then_stays_byte_identical(self):
        rec = self._rec()
        payload = self.jobs.settle_dead(rec)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["error"], "wrapper died before writing a result")
        self.assertEqual((payload["stopReason"], payload["exitCode"]), ("error", 1))
        self.assertEqual((payload["runId"], payload["provider"], payload["cwd"]),
                         ("dead", "grok", "/work"))
        self.assertIsNone(payload["diffPath"])
        result = self.jobs.run_dir_for("dead") / "result.json"
        before = result.read_bytes()
        self.assertIsNone(self.jobs.settle_dead(rec))
        self.assertEqual(result.read_bytes(), before)

    def test_a_recovered_retry_is_not_the_dead_run_reason(self):
        rec = self.jobs.JobRecord(run_id="retried", provider="codex", cwd="/work",
                                  status="running", pid=999999)
        rec.save()
        (self.jobs.run_dir_for("retried") / "stream.jsonl").write_text(
            '{"type":"error","message":"Reconnecting... 1/5"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n')
        payload = self.jobs.settle_dead(rec)
        self.assertEqual(payload["error"], "wrapper died before writing a result")

    def _orphaned_run(self, run_id):
        """A repo, its run baseline, and a dead wrapper and worker."""
        import subprocess
        from delegate.lib.snapshot import snapshot_tree
        from tests.helpers import init_repo
        repo = init_repo(Path(self.tmp.name) / f"repo-{run_id}", {"base.txt": "base\n"})
        dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                              capture_output=True, text=True, check=True)
        dead_pid = int(dead.stdout)
        rec = self.jobs.JobRecord(run_id=run_id, provider="grok", cwd=str(repo),
                                  status="running", pid=dead_pid, worker_pid=dead_pid,
                                  scope=["src/"])
        rec.save()
        snapshot_tree(repo, self.jobs.run_dir_for(run_id) / "index0")
        return rec, repo

    def test_a_dead_run_reports_what_the_worker_wrote(self):
        rec, repo = self._orphaned_run("orphan")
        (repo / "new.txt").write_text("written after the baseline\n")
        payload = self.jobs.settle_dead(rec)
        self.assertFalse(payload["emptyDiff"])
        self.assertIn("new.txt", Path(payload["diffPath"]).read_text())
        self.assertEqual(payload["scopeViolations"], ["new.txt"])

    def test_a_dead_run_does_not_flag_its_requested_report(self):
        rec, repo = self._orphaned_run("reporter")
        rec.report = str(repo / "REPORT.md")
        rec.save()
        (repo / "REPORT.md").write_text("report\n")
        (repo / "new.txt").write_text("stray\n")
        payload = self.jobs.settle_dead(self.jobs.read_job("reporter"))
        self.assertEqual(payload["scopeViolations"], ["new.txt"])

    def test_a_dead_run_that_wrote_nothing_has_an_empty_delta(self):
        rec, _ = self._orphaned_run("idle")
        payload = self.jobs.settle_dead(rec)
        self.assertTrue(payload["emptyDiff"])
        self.assertIsNone(payload["diffPath"])

    def _orphaned_worker(self):
        """A worker group whose launcher already exited, like a dead wrapper's
        worker: its own session, a background child, reparented to init."""
        import subprocess
        worker = ("import subprocess, sys, time\n"
                  "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                  "time.sleep(60)\n")
        launcher = ("import subprocess, sys\n"
                    f"p = subprocess.Popen([sys.executable, '-c', {worker!r}], start_new_session=True,\n"
                    "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                    "print(p.pid)\n")
        out = subprocess.run([sys.executable, "-c", launcher], capture_output=True,
                             text=True, check=True)
        pid = int(out.stdout)
        self.addCleanup(self._kill_group_quietly, pid)
        return pid

    @staticmethod
    def _kill_group_quietly(pgid):
        import signal
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass

    def test_cancel_stops_the_worker_of_a_dead_wrapper(self):
        rec, repo = self._orphaned_run("orphan-cancel")
        rec.worker_pid = self._orphaned_worker()
        rec.save()
        (repo / "late.txt").write_text("the orphan kept writing\n")
        ok, msg = self.jobs.cancel("orphan-cancel")
        self.assertTrue(ok, msg)
        self.assertTrue(self.jobs._worker_group_gone(rec), "worker group survived cancel")
        result = self.jobs.read_result("orphan-cancel")
        self.assertEqual(result["error"], "wrapper died before writing a result")
        self.assertIn("late.txt", Path(result["diffPath"]).read_text())

    def test_cancel_never_signals_a_process_that_reused_the_pid(self):
        rec, _ = self._orphaned_run("recycled")
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        rec.worker_pid = child.pid  # alive, but in the test's group: not the worker's
        rec.save()
        ok, msg = self.jobs.cancel("recycled")
        self.assertTrue(ok, msg)
        self.assertIsNone(child.poll(), "cancel signalled a process outside the worker group")

    def test_cancel_stops_a_group_whose_leader_already_exited(self):
        rec, _ = self._orphaned_run("leaderless-cancel")
        rec.worker_pid = self._leaderless_group()
        rec.save()
        ok, msg = self.jobs.cancel("leaderless-cancel")
        self.assertTrue(ok, msg)
        self.assertTrue(self.jobs._worker_group_gone(rec))

    @staticmethod
    def _zombie(new_group=False):
        """A child that has exited and that we have not reaped."""
        pid = os.fork()
        if pid == 0:
            if new_group:
                os.setsid()
            os._exit(0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # until it has exited, unreaped
            try:
                os.getpgid(pid)
            except ProcessLookupError:
                break
            if Path(f"/proc/{pid}/stat").is_file() and \
                    Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
            time.sleep(0.01)
        return pid

    def test_a_zombie_is_not_alive(self):
        pid = self._zombie()
        self.addCleanup(os.waitpid, pid, 0)
        self.assertFalse(self.jobs.pid_alive(pid))

    @unittest.skipUnless(Path("/proc/self/stat").is_file(), "needs /proc (Linux)")
    def test_a_group_of_only_zombies_is_gone(self):
        pid = self._zombie(new_group=True)
        self.addCleanup(os.waitpid, pid, 0)
        rec = self.jobs.JobRecord(run_id="zombie-group", provider="grok", cwd="/work",
                                  worker_pid=pid)
        self.assertTrue(self.jobs._worker_group_gone(rec))

    def test_a_group_whose_leader_exited_is_alive_while_a_member_runs(self):
        rec = self.jobs.JobRecord(run_id="leaderless", provider="grok", cwd="/work",
                                  worker_pid=self._leaderless_group())
        self.assertFalse(self.jobs._worker_group_gone(rec),
                         "the leader exited but its group still has a writer")

    def _leaderless_group(self):
        """A worker group whose leader exited, leaving a background member."""
        import subprocess
        worker = ("import subprocess, sys\n"
                  "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
                  "                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n")
        launcher = ("import subprocess, sys\n"
                    f"p = subprocess.Popen([sys.executable, '-c', {worker!r}], start_new_session=True,\n"
                    "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                    "p.wait()\n"
                    "print(p.pid)\n")
        out = subprocess.run([sys.executable, "-c", launcher], capture_output=True,
                             text=True, check=True)
        leader = int(out.stdout)
        self.addCleanup(self._kill_group_quietly, leader)
        return leader

    def test_live_wrapper_writes_nothing(self):
        rec = self._rec("live", pid=os.getpid())
        fd = self.jobs.hold_live_lock("live")
        try:
            self.assertIsNone(self.jobs.settle_dead(rec))
        finally:
            os.close(fd)
        self.assertFalse((self.jobs.run_dir_for("live") / "result.json").exists())

    def test_live_worker_group_writes_nothing(self):
        import subprocess
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                  start_new_session=True)
        self.addCleanup(worker.wait)
        self.addCleanup(worker.kill)
        rec = self._rec("worker-live", worker_pid=worker.pid)
        self.assertIsNone(self.jobs.settle_dead(rec))
        self.assertFalse((self.jobs.run_dir_for("worker-live") / "result.json").exists())


class TestPruneRuns(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._env = patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        from delegate import jobs
        self.jobs = jobs
        self.now = time.time()

    def _run(self, run_id, age_days, *, result=True):
        rec = self.jobs.JobRecord(run_id=run_id, provider="grok", cwd="/work")
        rec.save()
        d = self.jobs.run_dir_for(run_id)
        if result:
            (d / "result.json").write_text('{"exitCode": 0}', encoding="utf-8")
        stamp = self.now - age_days * 86400
        os.utime(d, (stamp, stamp))
        return d

    def test_removes_only_old_dirs_with_a_result(self):
        old = self._run("old", 31)
        recent = self._run("recent", 29)
        unfinished = self._run("unfinished", 31, result=False)
        removed = self.jobs.prune_runs(keep_days=30, now=self.now)
        self.assertEqual(removed, ["old"])
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unfinished.exists())

    def test_does_not_scan_twice_in_a_day(self):
        self.jobs.prune_runs(keep_days=30, now=self.now)
        late = self._run("late", 31)
        self.assertEqual(self.jobs.prune_runs(keep_days=30, now=self.now + 3600), [])
        self.assertTrue(late.exists())

    def test_off_unless_keep_days_is_set(self):
        old = self._run("old", 400)
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DELEGATE_KEEP_DAYS", None)
            self.assertEqual(self.jobs.prune_runs(now=self.now), [])
        self.assertTrue(old.exists())

    def test_keep_days_env_sets_the_window(self):
        old = self._run("old", 2)
        recent = self._run("recent", 1)
        from unittest.mock import patch
        with patch.dict(os.environ, {"DELEGATE_KEEP_DAYS": "1"}):
            removed = self.jobs.prune_runs(now=self.now)
        self.assertEqual(removed, ["old"])
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())


if __name__ == "__main__":
    unittest.main()
