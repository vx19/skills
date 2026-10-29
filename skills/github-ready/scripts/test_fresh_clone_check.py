import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("fresh_clone_check.py")


class FreshCloneCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo, self.watch = self.root / "repo", self.root / "watch"
        for directory in (self.repo, self.watch):
            directory.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Example User")
        self.git("config", "user.email", "user@example.com")
        (self.repo / "tracked").write_text("public snapshot\n")
        self.git("add", "tracked")
        self.git("commit", "-qm", "initial")

    def git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True)

    def run_check(self, *args: str, repo: Path | None = None,
                  env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], dict]:
        result = subprocess.run([sys.executable, str(SCRIPT), str(repo or self.repo), *args],
                                capture_output=True, text=True, env=env)
        report = json.loads(result.stdout)
        self.assertEqual(list(report), sorted(report))
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["mode"], "fresh-clone-check")
        if report["kept_path"]:
            self.addCleanup(shutil.rmtree, report["kept_path"], ignore_errors=True)
        return result, report

    def assert_not_leaked(self, result: subprocess.CompletedProcess[str], *values: str) -> None:
        for value in values:
            self.assertNotIn(value, result.stdout + result.stderr)

    def fixture(self, body: str) -> list[str]:
        tool = self.root / "fixture-command"
        tool.write_text("#!/bin/sh\n" + body + "\n")
        tool.chmod(0o755)
        return ["--path", str(self.root), "--run", "fixture-command"]

    def test_pass_kept_environment_and_clone(self) -> None:
        (self.repo / "tracked").write_text("uncommitted\n")
        (self.repo / "untracked").write_text("private source content\n")
        env = os.environ.copy()
        planted = "ghp_" + "A" * 28
        env.update({"PRIVATE_TEST_VALUE": planted, "PATH": "/unusable",
                    "GIT_DIR": str(self.watch), "GIT_CONFIG_COUNT": "invalid"})
        command = 'env > environment; git remote > remotes; printf public-output'
        result, report = self.run_check("--run", command, "--keep", "--profile", "test", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["profile"], "test")
        root = Path(report["kept_path"])
        clone = root / "clone"
        values = dict(line.split("=", 1) for line in (clone / "environment").read_text().splitlines())
        expected = {"HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
                    "GIT_CEILING_DIRECTORIES": str(root), "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null"}
        self.assertEqual({key: values[key] for key in expected}, expected)
        self.assertLessEqual(set(values), set(expected) | {"PWD", "SHLVL", "_"})
        self.assertEqual((clone / "remotes").read_text(), "")
        self.assertEqual((clone / "tracked").read_text(), "public snapshot\n")
        self.assertEqual(report["commit"], self.git("rev-parse", "HEAD").stdout.decode().strip())
        self.assertEqual(report["commands"], [{"index": 1, "command": command, "exit_code": 0}])
        self.assertEqual(set(report), {"schema_version", "mode", "profile", "commit",
                                       "commands", "watch", "kept_path", "status"})
        self.assertEqual(result.stderr, "")
        self.assert_not_leaked(result, planted, "private source content")
        self.assertFalse((clone / "untracked").exists())
        source_objects = {(path.stat().st_dev, path.stat().st_ino)
                          for path in (self.repo / ".git" / "objects").rglob("*") if path.is_file()}
        clone_objects = clone / ".git" / "objects"
        for path in clone_objects.rglob("*"):
            if path.is_file():
                self.assertNotIn((path.stat().st_dev, path.stat().st_ino), source_objects)
        self.assertFalse((clone_objects / "info" / "alternates").exists())
        self.assertEqual((self.repo / "tracked").read_text(), "uncommitted\n")

    def test_cleanup_and_stop_at_first_failure(self) -> None:
        location = self.root / "location"
        first = self.root / "bin-one"
        first.mkdir()
        for code in (0, 7):
            with self.subTest(code=code):
                args = self.fixture(f'[ "$PATH" = "/usr/bin:/bin:/usr/sbin:/sbin:{first}:{self.root}" ] || exit 9\n'
                                    f'printf %s "$PWD" > {shlex.quote(str(location))}; exit {code}')
                result, report = self.run_check("--path", str(first), *args, "--run", "true")
                self.assertEqual(result.returncode, 1 if code else 0)
                self.assertEqual(report["status"], "fail" if code else "pass")
                self.assertEqual([item["exit_code"] for item in report["commands"]], [7] if code else [0, 0])
                self.assertEqual([item["index"] for item in report["commands"]], [1] if code else [1, 2])
                self.assertEqual(report["profile"], "default")
                self.assertIsNone(report["kept_path"])
                self.assertFalse(Path(location.read_text()).parent.exists())

    def test_watch_changes_and_output_privacy(self) -> None:
        filename = "private-entry-" + "X" * 20
        planted = "sk-" + "B" * 28
        target = self.watch / filename
        for code in (0, 3):
            with self.subTest(code=code):
                target.write_text(planted)
                args = self.fixture(f'cat {shlex.quote(str(target))}; cat {shlex.quote(str(target))} >&2; '
                                    f'printf changed > {shlex.quote(str(target))}; exit {code}')
                result, report = self.run_check("--watch", str(self.watch), *args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(report["status"], "fail")
                self.assertEqual(report["commands"][0]["exit_code"], code)
                self.assertEqual(report["watch"], [{"index": 1, "entries_before": 1,
                                                   "entries_after": 1, "changed": True}])
                self.assert_not_leaked(result, filename, planted, str(self.watch))

    def test_watch_tree_records_symlinks_without_following(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private-target").write_text("original")
        nested = self.watch / "private-directory"
        nested.mkdir()
        (nested / "private-file").write_text("original")
        for name, target in (("private-link", outside), ("private-loop", self.watch),
                             ("private-dangling", self.root / "missing")):
            (self.watch / name).symlink_to(target, target_is_directory=True)
        other = self.root / "other-watch"
        other.mkdir()
        result, report = self.run_check("--watch", str(self.watch), "--watch", str(other),
                                        *self.fixture(f'printf changed > {shlex.quote(str(outside / "private-target"))}'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report["watch"], [
            {"index": 1, "entries_before": 5, "entries_after": 5, "changed": False},
            {"index": 2, "entries_before": 0, "entries_after": 0, "changed": False},
        ])
        self.assert_not_leaked(result, "private-directory", "private-file", "private-link",
                               "private-loop", "private-dangling", "private-target")

    def test_setup_errors_are_redacted_json(self) -> None:
        missing = self.root / ("private-path-" + "Z" * 20)
        (self.watch / "not-a-directory").write_text("original")
        link = self.root / "link"
        link.symlink_to(self.repo, target_is_directory=True)
        cases = [
            (("--run", "true"), self.watch),
            (("--run", "true"), missing),
            (("--watch", str(missing), "--run", "true"), None),
            (("--watch", str(self.watch / "not-a-directory"), "--run", "true"), None),
            (("--path", "relative-path", "--run", "true"), None),
            (("--path", str(missing), "--run", "true"), None),
            (("--path", str(self.repo / "tracked"), "--run", "true"), None),
            (("--unknown", "private-argument", "--run", "true"), None),
            ((), None),
            *((("--watch", str(path), "--run", "true"), None)
              for path in (self.repo, self.repo / ".git", link)),
        ]
        for args, repo in cases:
            with self.subTest(args=args):
                result, report = self.run_check(*args, repo=repo)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(report["status"], "error")
                self.assertEqual(report["commands"], [])
                self.assertEqual(result.stderr, "")
                self.assert_not_leaked(result, str(self.root), "private-argument")


if __name__ == "__main__":
    unittest.main()
