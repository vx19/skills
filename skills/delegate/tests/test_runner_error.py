"""Runner stderr reasons, and Pi's config-dir write probe.

Fixtures are hand-written. They use the placeholder home `/srv/u`, not a
real log or a real home directory.
"""
import errno
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from delegate import core
from delegate.providers.base import Job
from delegate.providers.pi import Pi

# Pi died in under a second; the useful line does not start with `error:`.
PI_LOG = (
    "Warning: Invalid settings file /srv/u/.pi/agent/settings.json: "
    "EPERM: operation not permitted, mkdir '/srv/u/.pi/agent/settings.json.lock'\n"
    "Credential store read failed for example-provider: EPERM: operation not permitted, "
    "mkdir '/srv/u/.pi/agent/auth.json.lock'\n"
)
CREDENTIAL_LINE = (
    "Credential store read failed for example-provider: EPERM: operation not permitted, "
    "mkdir '/srv/u/.pi/agent/auth.json.lock'"
)

# Grok tracing. `error=` in the message must not be read as an `error:` reason.
GROK_LOG = (
    "2026-01-01T00:00:00.000000Z  WARN failed to remove managed config file "
    "file=/srv/u/.grok/managed_config.toml error=Operation not permitted (os error 1)\n"
    "2026-01-01T00:00:01.000000Z  WARN hook loading from settings file: "
    "failed to parse hook file ...\n"
)


class RunnerErrorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def log(self, text):
        path = self.root / "runner.log"
        path.write_text(text, encoding="utf-8")
        return path

    def test_pi_log_keeps_the_credential_line(self):
        self.assertEqual(core._runner_error(self.log(PI_LOG)), CREDENTIAL_LINE)

    def test_grok_warn_lines_are_not_a_reason(self):
        self.assertIsNone(core._runner_error(self.log(GROK_LOG)))

    def test_stdin_progress_line_is_not_a_reason(self):
        self.assertIsNone(core._runner_error(
            self.log("Reading additional input from stdin...\n")))

    def test_error_line_beats_later_noise(self):
        self.assertEqual(core._runner_error(self.log("error: boom\n" + GROK_LOG)), "boom")

    def test_error_line_beats_a_later_real_line(self):
        text = "error: boom\nCredential store read failed for example-provider\n"
        self.assertEqual(core._runner_error(self.log(text)), "boom")

    def test_fallback_is_the_last_qualifying_line(self):
        text = "first real failure\nWarning: ignore me\nsecond real failure\n"
        self.assertEqual(core._runner_error(self.log(text)), "second real failure")

    def test_fallback_line_is_truncated_to_300(self):
        self.assertEqual(core._runner_error(self.log("y" * 400 + "\n")), "y" * 300)

    def test_error_payload_is_truncated_to_300(self):
        self.assertEqual(core._runner_error(self.log("error: " + "z" * 400 + "\n")), "z" * 300)


class PiPreflightTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "base").write_text("base\n")
        self.git("add", ".")
        self.git("-c", "user.name=test", "-c", "user.email=test@test", "commit", "-qm", "initial")
        bindir = self.root / "bin"
        bindir.mkdir()
        pi = bindir / "pi"
        pi.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        pi.chmod(0o755)
        self.agent = self.root / "agent"
        self.env = {
            "PI_CODING_AGENT_DIR": str(self.agent),
            "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
        }

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def job(self, **extra):
        return Job("pi", self.repo, "task", "run", self.root / "run", extra=extra)

    def preflight(self, job):
        err = io.StringIO()
        with patch.dict(os.environ, self.env), redirect_stderr(err):
            core.preflight(job, Pi())
        return err.getvalue()

    def lock_agent(self):
        """A directory the owner cannot create children in. Pi locks with mkdir."""
        self.agent.mkdir()
        self.agent.chmod(0o500)
        self.addCleanup(self.agent.chmod, 0o700)

    def test_unwritable_agent_dir_is_a_preflight_error(self):
        self.lock_agent()
        message = (
            f"pi cannot write its config dir {self.agent} (Permission denied); "
            "the calling sandbox must allow writes there"
        )
        with self.assertRaises(core.Preflight) as caught:
            self.preflight(self.job())
        self.assertEqual(str(caught.exception), message)

    def test_missing_agent_dir_is_not_an_error(self):
        self.assertFalse(self.agent.exists())
        self.assertEqual(self.preflight(self.job()), "")

    def test_writable_agent_dir_is_left_empty(self):
        self.agent.mkdir()
        self.assertEqual(self.preflight(self.job()), "")
        self.assertEqual(list(self.agent.iterdir()), [])

    def test_unset_env_probes_home_pi_agent(self):
        home = self.root / "home"
        agent = home / ".pi" / "agent"
        agent.mkdir(parents=True)
        agent.chmod(0o500)
        self.addCleanup(agent.chmod, 0o700)
        message = (
            f"pi cannot write its config dir {agent} (Permission denied); "
            "the calling sandbox must allow writes there"
        )
        err = io.StringIO()
        with patch.dict(os.environ, self.env):
            os.environ.pop("PI_CODING_AGENT_DIR", None)
            with patch("delegate.providers.pi.Path.home", return_value=home), redirect_stderr(err):
                with self.assertRaises(core.Preflight) as caught:
                    core.preflight(self.job(), Pi())
        self.assertEqual(str(caught.exception), message)
        self.assertEqual(err.getvalue(), "")

    def _deny_agent_mkdir(self):
        """mkdir of a probe under the agent dir raises EPERM, as a host sandbox does."""
        return patch(
            "os.mkdir",
            side_effect=PermissionError(
                errno.EPERM, "Operation not permitted", str(self.agent)),
        )

    def test_eperm_mkdir_on_agent_dir_is_a_preflight_error(self):
        self.agent.mkdir()
        message = (
            f"pi cannot write its config dir {self.agent} (Operation not permitted); "
            "the calling sandbox must allow writes there"
        )
        with self._deny_agent_mkdir():
            with self.assertRaises(core.Preflight) as caught:
                self.preflight(self.job())
        self.assertEqual(str(caught.exception), message)


if __name__ == "__main__":
    unittest.main()
