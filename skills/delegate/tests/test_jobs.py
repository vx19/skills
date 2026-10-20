import json, os, sys, time, unittest, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class TestJobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_STATE_HOME"] = self.tmp
        # import after env set so state_root resolves under tmp
        import importlib
        from delegate.lib import paths as _paths
        importlib.reload(_paths)
        from delegate import jobs as _jobs
        importlib.reload(_jobs)
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
        self.assertIn("r2", self.jobs.scope_overlap(["packages/core"]))
        self.assertEqual(self.jobs.scope_overlap(["totally/other"]), [])

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

    def test_live_wrapper_writes_nothing(self):
        rec = self._rec("live", pid=os.getpid())
        fd = self.jobs.hold_live_lock("live")
        try:
            self.assertIsNone(self.jobs.settle_dead(rec))
        finally:
            os.close(fd)
        self.assertFalse((self.jobs.run_dir_for("live") / "result.json").exists())

    def test_live_worker_group_writes_nothing(self):
        rec = self._rec("worker-live", worker_pid=os.getpid())
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
