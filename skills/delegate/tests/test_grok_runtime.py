"""Grok runtime contract: folder trust, self-ended runs, sandbox posture.

Trust follows grok's own rule: a grant covers the subdirectories of the same
repository, not a nested checkout and not a parent folder. Offline: a fake
trust file and disposable repos.
"""
import errno
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

from delegate import cli, core, jobs  # noqa: E402
from delegate.providers import grok as grokmod  # noqa: E402
from delegate.providers.base import Job  # noqa: E402
from delegate.providers.grok import Grok  # noqa: E402


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,
                                    capture_output=True)
    run("init", "-q")
    (path / "f").write_text("f\n")
    run("add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return path.resolve()


class TrustTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.trust_file = self.root / "trusted_folders.toml"
        patcher = patch.object(grokmod, "_TRUST_FILE", self.trust_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = _repo(self.root / "projects" / "repo")
        self.sub = self.repo / "providers" / "tool"
        self.sub.mkdir(parents=True)

    def trust(self, *folders):
        self.trust_file.write_text("".join(
            f'[folders."{f}"]\ntrusted = true\ndecided_at = 1\n\n' for f in folders))

    def verdict(self, cwd):
        return Grok().trust(cwd)

    def test_exact_repo_root(self):
        self.trust(self.repo)
        self.assertEqual(self.verdict(self.repo), (True, ""))

    def test_subdirectory_inherits_repo_grant(self):
        self.trust(self.repo)
        self.assertEqual(self.verdict(self.sub), (True, ""))

    def test_nested_checkout_is_a_separate_workspace(self):
        nested = _repo(self.repo / "vendor" / "inner")
        self.trust(self.repo)
        ok, hint = self.verdict(nested)
        self.assertFalse(ok)
        self.assertIn(f"cd {nested} && grok", hint)

    def test_trusted_parent_folder_does_not_cover_a_repo(self):
        self.trust(self.root / "projects")
        ok, hint = self.verdict(self.sub)
        self.assertFalse(ok)
        self.assertIn(str(self.repo), hint)  # the hint names the repo root to trust

    def test_explicit_untrust_is_not_trusted(self):
        self.trust_file.write_text(f'[folders."{self.repo}"]\ntrusted = false\n')
        self.assertFalse(self.verdict(self.sub)[0])

    def test_missing_trust_file(self):
        self.assertFalse(self.verdict(self.repo)[0])

    def test_a_run_does_not_block_on_the_saved_grant(self):
        job = Job("grok", self.repo, "t", "r", self.repo)
        self.assertIsNone(Grok().trust(self.repo, job))


class Scripted(Grok):
    runner = sys.executable

    def __init__(self, script):
        self.script = script

    def trust(self, cwd, job=None):
        return None

    def argv(self, job):
        return ["-c", self.script]


class SelfEndedRunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.job = Job("grok", _repo(root / "repo"), "task", "run", jobs.run_dir_for("run"))

    def test_max_turns_is_an_error_with_the_reason_not_a_cancel(self):
        # grok 1.0.x: `--max-turns` exhaustion ends with stopReason "cancelled"
        # and says why only on stderr.
        script = ("import sys; print('{\"type\":\"end\",\"stopReason\":\"cancelled\","
                  "\"num_turns\":24}'); sys.stderr.write('\\x1b[33m WARN\\x1b[0m noise\\n"
                  "Error: max turns reached\\n'); sys.exit(1)")
        env = core.run(self.job, Scripted(script), quiet=True)
        self.assertEqual((env.stop_reason, env.exit_code), ("error", 1))
        self.assertIn("ended the run itself (cancelled): max turns reached", env.error)
        self.assertEqual(env.turns, 24)

    def test_nonzero_exit_carries_the_runner_error_line(self):
        script = "import sys; sys.stderr.write('Error: could not apply sandbox\\n'); sys.exit(1)"
        env = core.run(self.job, Scripted(script), quiet=True)
        self.assertEqual(env.error, f"{sys.executable} exited 1: could not apply sandbox")

    def test_nonzero_exit_carries_lowercase_grok_error_line(self):
        script = ("import sys; sys.stderr.write("
                  "\"error: could not apply the 'read-only' sandbox profile\\n\"); "
                  "sys.exit(1)")
        env = core.run(self.job, Scripted(script), quiet=True)
        self.assertEqual(
            env.error,
            f"{sys.executable} exited 1: could not apply the 'read-only' sandbox profile",
        )

    def test_operator_cancel_stays_a_cancel(self):
        self.job.run_dir.mkdir(parents=True, exist_ok=True)
        (self.job.run_dir / "cancel").touch()
        env = core.run(self.job, Scripted("pass"), quiet=True)
        self.assertEqual((env.stop_reason, env.error), ("cancelled", None))


class PostureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        # Assume the read-only profile can be applied. A sandboxed host would
        # otherwise flip this suite's argv.
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state),
                                      "DELEGATE_GROK_PROFILE_APPLY": "yes",
                                      "GROK_AGENT": ""})
        env.start()
        self.addCleanup(env.stop)

    def dry(self, *flags):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["run", "--provider", "grok", "--cwd", ".", "--dry-run",
                             *flags, "x"])
        self.assertEqual(code, 0, err.getvalue())
        return json.loads(out.getvalue())["providerExtra"]["argv"]

    def refused(self, *flags):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["run", "--provider", "grok", "--cwd", ".", "--dry-run",
                             *flags, "x"])
        self.assertEqual(code, 64, err.getvalue())
        return err.getvalue()

    def test_default_sandbox_is_workspace_and_trusts(self):
        argv = self.dry()
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace")
        self.assertIn("--trust", argv)

    def test_no_sandbox_is_explicit_off(self):
        argv = self.dry("--no-sandbox")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "off")

    def test_restrict_only_extras_reach_grok(self):
        argv = self.dry("--provider-arg", "disallowed-tools=web_search,web_fetch",
                        "--provider-arg", "deny=*__*", "--provider-arg", "no-memory=true")
        self.assertEqual(argv[argv.index("--disallowed-tools") + 1], "web_search,web_fetch")
        self.assertEqual(argv[argv.index("--deny") + 1], "*__*")
        self.assertIn("--no-memory", argv)

    def test_sandbox_extra_reaches_grok(self):
        argv = self.dry("--provider-arg", "sandbox=personal")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "personal")

    def test_read_only_is_the_read_only_profile(self):
        argv = self.dry("--read-only")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--trust", argv)
        self.assertIn("--disable-web-search", argv)

    def test_grok_agent_does_not_change_the_sandbox(self):
        with patch.dict(os.environ, {"GROK_AGENT": "1"}):
            argv = self.dry()
            self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace")
            self.assertIn("--trust", argv)

    def test_read_only_inherits_when_the_profile_cannot_apply(self):
        # Parent sandbox: grok
        # refuses --sandbox read-only. The run must still start.
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"GROK_AGENT": "1",
                                     "DELEGATE_GROK_PROFILE_APPLY": ""}):
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["run", "--provider", "grok", "--cwd", ".",
                                 "--dry-run", "--read-only", "x"])
        self.assertEqual(code, 0, err.getvalue())
        argv = json.loads(out.getvalue())["providerExtra"]["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "off")
        self.assertIn("--trust", argv)
        self.assertIn("--disable-web-search", argv)
        self.assertIn("post-run delta check", err.getvalue())

    def test_read_only_inherits_when_a_probe_says_refused(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"GROK_AGENT": "",
                                     "DELEGATE_GROK_PROFILE_APPLY": ""}), \
             patch.object(grokmod, "_profile_refused_cached", return_value=True):
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["run", "--provider", "grok", "--cwd", ".",
                                 "--dry-run", "--read-only", "x"])
        self.assertEqual(code, 0, err.getvalue())
        argv = json.loads(out.getvalue())["providerExtra"]["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "off")
        self.assertIn("--disable-web-search", argv)

    def test_sandbox_off_via_provider_arg_is_refused(self):
        err = self.refused("--provider-arg", "sandbox=off")
        self.assertIn("pass --no-sandbox", err)

    def test_sandbox_extra_contradicts_no_sandbox(self):
        err = self.refused("--no-sandbox", "--provider-arg", "sandbox=personal")
        self.assertIn("contradict", err)

    def test_sandbox_extra_contradicts_read_only(self):
        err = self.refused("--read-only", "--provider-arg", "sandbox=personal")
        self.assertIn("contradict", err)

    def test_unknown_provider_arg_is_refused_not_ignored(self):
        err = self.refused("--provider-arg", "foo=bar")
        self.assertIn("'foo' is not read by grok", err)

    def test_hosted_is_not_a_provider_arg(self):
        err = self.refused("--provider-arg", "hosted=true")
        self.assertIn("not read by grok", err)

    def test_dry_run_leaves_no_run_dir(self):
        self.dry()
        self.assertFalse(self.state.exists() and any(self.state.rglob("*")))


class CredentialPreflightTests(unittest.TestCase):
    """`~/.grok/auth.json` must be readable, unless an API key is already set."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.job = Job("grok", self.home, "t", "r", self.home)
        self.auth = self.home / ".grok" / "auth.json"

    def check(self, **extra):
        env = {"HOME": str(self.home), **extra}
        with patch.dict(os.environ, env, clear=False):
            for key in ("XAI_API_KEY", "GROK_API_KEY"):
                if key not in extra:
                    os.environ.pop(key, None)
            return Grok().preflight_check(self.job)

    def _lock_auth(self):
        self.auth.parent.mkdir()
        self.auth.write_text("{}\n", encoding="utf-8")
        self.auth.chmod(0)
        self.addCleanup(self.auth.chmod, 0o644)

    def test_unreadable_auth_file_names_the_path(self):
        self._lock_auth()
        self.assertEqual(
            self.check(),
            f"grok cannot read its credentials {self.auth} (Permission denied); "
            "the calling sandbox must allow reading it",
        )

    def test_api_key_skips_an_unreadable_auth_file(self):
        self._lock_auth()
        self.assertIsNone(self.check(XAI_API_KEY="test-key"))
        self.assertIsNone(self.check(GROK_API_KEY="test-key"))

    def test_missing_auth_file_is_not_an_error(self):
        self.assertFalse(self.auth.exists())
        self.assertIsNone(self.check())

    def test_readable_auth_file_is_not_an_error(self):
        self.auth.parent.mkdir()
        self.auth.write_text("{}\n", encoding="utf-8")
        self.assertIsNone(self.check())

    def _deny_auth(self):
        """stat and open of the auth path raise EPERM, as a host sandbox does."""
        real_stat = Path.stat
        real_open = io.open

        def is_auth(file) -> bool:
            try:
                return Path(file) == self.auth
            except TypeError:
                return False

        def guarded_stat(path, *, follow_symlinks=True):
            if is_auth(path):
                raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
            return real_stat(path, follow_symlinks=follow_symlinks)

        def guarded_open(file, *args, **kwargs):
            if is_auth(file):
                raise PermissionError(errno.EPERM, "Operation not permitted", str(file))
            return real_open(file, *args, **kwargs)

        return guarded_stat, guarded_open

    def test_eperm_stat_returns_the_message(self):
        guarded_stat, guarded_open = self._deny_auth()
        message = (
            f"grok cannot read its credentials {self.auth} (Operation not permitted); "
            "the calling sandbox must allow reading it"
        )
        with patch.object(Path, "stat", guarded_stat), patch("io.open", guarded_open):
            self.assertEqual(self.check(), message)

    def test_doctor_eperm_stat_fails_without_traceback(self):
        guarded_stat, guarded_open = self._deny_auth()
        message = (
            f"FAIL grok  : grok cannot read its credentials {self.auth} "
            "(Operation not permitted); the calling sandbox must allow reading it"
        )
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"HOME": str(self.home)}, clear=False):
            os.environ.pop("XAI_API_KEY", None)
            os.environ.pop("GROK_API_KEY", None)
            with patch.object(Path, "stat", guarded_stat), \
                 patch("io.open", guarded_open), \
                 patch("delegate.cli.shutil.which", return_value="/usr/bin/grok"), \
                 redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["doctor", "--provider", "grok"])
        text = out.getvalue()
        self.assertEqual(code, 2, text + err.getvalue())
        self.assertIn(message, text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
