from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("safe_repo_scan.py")
sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("safe_repo_scan", SCRIPT)
assert SPEC and SPEC.loader
SCANNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCANNER)


class SafeRepoScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args], cwd=self.repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        )

    def run_scan(self, *args: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return result, json.loads(result.stdout)

    def assert_not_leaked(self, result: subprocess.CompletedProcess[str], secret: str) -> None:
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)

    def test_worktree_and_binary_findings_never_emit_values(self) -> None:
        token = "ghp_" + "A" * 28
        (self.repo / "plain.txt").write_text(f"token={token}\n", encoding="utf-8")
        (self.repo / "binary.dat").write_bytes(b"\0prefix " + token.encode() + b"\n")

        result, report = self.run_scan()

        self.assertEqual(result.returncode, 1)
        self.assert_not_leaked(result, token)
        worktree = [item for item in report["findings"] if item["scope"] == "worktree"]
        self.assertEqual({item["content_kind"] for item in worktree}, {"text", "binary"})
        self.assertIn("github-token", {item["category"] for item in worktree})

    def test_reachable_old_blob_is_scanned_without_emitting_value(self) -> None:
        token = "AKIA" + "B" * 16
        tracked = self.repo / "config.txt"
        tracked.write_text(token + "\n", encoding="utf-8")
        self.git("add", "config.txt")
        self.git("commit", "-qm", "old value")
        tracked.write_text("clean\n", encoding="utf-8")
        self.git("commit", "-qam", "remove value")

        result, report = self.run_scan()

        self.assertEqual(result.returncode, 1)
        self.assert_not_leaked(result, token)
        history = [item for item in report["findings"] if item["scope"] == "history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["category"], "aws-access-key")
        self.assertRegex(history[0]["blob_id"], r"^[0-9a-f]{40,64}$")
        self.assertNotIn("reachable_blobs", report)

    def test_secret_shaped_filename_is_redacted(self) -> None:
        filename_token = "ghp_" + "C" * 28
        content_token = "xoxb-" + "1" * 24
        (self.repo / filename_token).write_text(content_token + "\n", encoding="utf-8")

        result, report = self.run_scan()

        self.assertEqual(result.returncode, 1)
        self.assert_not_leaked(result, filename_token)
        self.assert_not_leaked(result, content_token)
        finding = next(item for item in report["findings"] if item["scope"] == "worktree")
        self.assertTrue(finding["path_redacted"])
        self.assertNotIn("path", finding)

    def test_empty_secret_filename_is_found_in_worktree_and_history(self) -> None:
        filename_token = "github_pat_" + "G" * 24
        unsafe = self.repo / filename_token
        unsafe.touch()

        worktree_result, worktree_report = self.run_scan()

        self.assertEqual(worktree_result.returncode, 1)
        self.assert_not_leaked(worktree_result, filename_token)
        worktree_names = [
            item
            for item in worktree_report["findings"]
            if item["scope"] == "worktree" and item["category"] == "filename-secret"
        ]
        self.assertEqual(len(worktree_names), 1)
        self.assertTrue(worktree_names[0]["path_redacted"])
        self.assertNotIn("path", worktree_names[0])

        self.git("add", filename_token)
        self.git("commit", "-qm", "add empty file")
        self.git("mv", filename_token, "clean-name.txt")
        self.git("commit", "-qm", "rename empty file")
        history_result, history_report = self.run_scan()

        self.assertEqual(history_result.returncode, 1)
        self.assert_not_leaked(history_result, filename_token)
        history_names = [
            item
            for item in history_report["findings"]
            if item["scope"] == "history" and item["category"] == "filename-secret"
        ]
        self.assertEqual(len(history_names), 1)
        self.assertTrue(history_names[0]["path_redacted"])
        self.assertNotIn("path", history_names[0])
        self.assertRegex(history_names[0]["blob_id"], r"^[0-9a-f]{40,64}$")

    def test_diff_safety_reports_added_line_without_printing_it(self) -> None:
        path = self.repo / "app.cfg"
        path.write_text("safe=true\n", encoding="utf-8")
        self.git("add", "app.cfg")
        self.git("commit", "-qm", "base")
        token = "sk-" + "D" * 30
        path.write_text(f"safe=true\nauth = Bearer {token}\n", encoding="utf-8")

        result, report = self.run_scan("--diff-safety")

        self.assertEqual(result.returncode, 1)
        self.assert_not_leaked(result, token)
        self.assertEqual(report["mode"], "diff-safety")
        self.assertTrue(report["summary"]["suspected_secrets"])
        self.assertEqual({item["line"] for item in report["findings"]}, {2})
        self.assertEqual({item["change_kind"] for item in report["findings"]}, {"added"})
        self.assertFalse(report["head"]["detached"])
        self.assertIn("branch", report["head"])

    def test_diff_inventory_redacts_unsafe_untracked_filename(self) -> None:
        filename_token = "ghp_" + "H" * 28
        (self.repo / filename_token).touch()

        result, report = self.run_scan("--diff-safety")

        self.assertEqual(result.returncode, 1)
        self.assert_not_leaked(result, filename_token)
        self.assertEqual(
            report["changes"],
            [{"path_redacted": True, "scope": "diff-untracked", "state": "untracked"}],
        )
        filename_findings = [
            item for item in report["findings"] if item["category"] == "filename-secret"
        ]
        self.assertEqual(len(filename_findings), 1)
        self.assertTrue(filename_findings[0]["path_redacted"])
        self.assertNotIn("path", filename_findings[0])

    def test_diff_inventory_covers_each_change_scope(self) -> None:
        tracked = self.repo / "tracked.txt"
        tracked.write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "base")
        tracked.write_text("changed\n", encoding="utf-8")
        (self.repo / "staged.txt").write_text("staged\n", encoding="utf-8")
        self.git("add", "staged.txt")
        (self.repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")

        result, report = self.run_scan("--diff-safety")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            {
                (item["scope"], item["state"], item["path"])
                for item in report["changes"]
            },
            {
                ("diff-staged", "added", "staged.txt"),
                ("diff-unstaged", "modified", "tracked.txt"),
                ("diff-untracked", "untracked", "untracked.txt"),
            },
        )

    def test_diff_inventory_reports_detached_head_by_commit(self) -> None:
        (self.repo / "base.txt").write_text("clean\n", encoding="utf-8")
        self.git("add", "base.txt")
        self.git("commit", "-qm", "base")
        self.git("checkout", "--detach", "-q", "HEAD")

        result, report = self.run_scan("--diff-safety")

        self.assertEqual(result.returncode, 0)
        self.assertTrue(report["head"]["detached"])
        self.assertRegex(report["head"]["head_commit"], r"^[0-9a-f]{40,64}$")

    def test_diff_safety_reports_removed_line_without_printing_it(self) -> None:
        token = "glpat-" + "E" * 24
        path = self.repo / "app.cfg"
        path.write_text(f"first=true\nsecret={token}\nlast=true\n", encoding="utf-8")
        self.git("add", "app.cfg")
        self.git("commit", "-qm", "base with secret")
        path.write_text("first=true\nlast=true\n", encoding="utf-8")

        result, report = self.run_scan("--diff-safety")

        self.assertEqual(result.returncode, 1)
        self.assert_not_leaked(result, token)
        removed = [item for item in report["findings"] if item["change_kind"] == "removed"]
        self.assertTrue(removed)
        self.assertEqual({item["line"] for item in removed}, {2})
        self.assertIn("gitlab-token", {item["category"] for item in removed})
        self.assertEqual(report["summary"]["removed_lines_checked"], 1)

    def test_diff_safety_scans_lines_that_start_like_diff_headers(self) -> None:
        removed = "--glpat-" + "E" * 24
        added = "++AKIA" + "B" * 16
        path = self.repo / "app.cfg"
        path.write_text(f"keep\n{removed}\n", encoding="utf-8")
        self.git("add", "app.cfg")
        self.git("commit", "-qm", "base")
        path.write_text(f"keep\n{added}\n", encoding="utf-8")

        result, report = self.run_scan("--diff-safety")

        self.assertEqual(result.returncode, 1)
        self.assert_not_leaked(result, removed)
        self.assert_not_leaked(result, added)
        self.assertEqual(
            {(item["change_kind"], item["category"], item["line"]) for item in report["findings"]},
            {("removed", "gitlab-token", 2), ("added", "aws-access-key", 2)},
        )

    def test_nested_git_directory_is_not_scanned(self) -> None:
        token = "xoxb-" + "2" * 24
        nested_git = self.repo / "vendor" / ".git"
        nested_git.mkdir(parents=True)
        (nested_git / "config").write_text(token + "\n", encoding="utf-8")

        result, report = self.run_scan()

        self.assertEqual(result.returncode, 0)
        self.assert_not_leaked(result, token)
        self.assertFalse(report["summary"]["suspected_secrets"])

    def test_untracked_symlink_target_is_not_followed(self) -> None:
        token = "ghp_" + "F" * 28
        outside = self.repo.parent / "outside-target.txt"
        outside.write_text(token + "\n", encoding="utf-8")
        try:
            (self.repo / "linked.txt").symlink_to(outside)

            result, report = self.run_scan("--diff-safety")

            self.assertEqual(result.returncode, 0)
            self.assert_not_leaked(result, token)
            self.assertFalse(report["summary"]["suspected_secrets"])
        finally:
            outside.unlink(missing_ok=True)

    def test_size_thresholds_match_github_defaults(self) -> None:
        self.assertIsNone(SCANNER.classify_blob_size(50 * 1024 * 1024 - 1))
        self.assertIsNone(SCANNER.classify_blob_size(50 * 1024 * 1024))
        self.assertEqual(SCANNER.classify_blob_size(50 * 1024 * 1024 + 1), "warning")
        self.assertEqual(SCANNER.classify_blob_size(100 * 1024 * 1024), "warning")
        self.assertEqual(SCANNER.classify_blob_size(100 * 1024 * 1024 + 1), "block")

    def test_non_repository_error_is_json_and_does_not_echo_git_stderr(self) -> None:
        marker = "SHOULD_NOT_APPEAR"
        with tempfile.TemporaryDirectory(prefix=marker) as directory:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), directory],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(marker, result.stdout)
        self.assertNotIn(marker, result.stderr)

    def test_clean_repository_returns_zero(self) -> None:
        (self.repo / "clean.txt").write_text("clean\n", encoding="utf-8")
        self.git("add", "clean.txt")
        self.git("commit", "-qm", "base")

        result, report = self.run_scan()

        self.assertEqual(result.returncode, 0)
        self.assertTrue(report["summary"]["complete"])
        self.assertFalse(report["summary"]["suspected_secrets"])

    def test_incomplete_beats_findings_in_both_modes(self) -> None:
        token = "ghp_" + "J" * 28
        (self.repo / "token.txt").write_text(token + "\n", encoding="utf-8")
        (self.repo / ".git" / "refs" / "heads" / "loop").write_text(
            "ref: refs/heads/loop\n", encoding="ascii")

        for args in ((), ("--diff-safety",)):
            with self.subTest(args=args):
                head = "ref: refs/heads/loop\n" if args else "f" * 40 + "\n"
                (self.repo / ".git" / "HEAD").write_text(head, encoding="ascii")
                result, report = self.run_scan(*args)

                self.assertEqual(result.returncode, 2)
                self.assertFalse(report["summary"]["complete"])
                self.assertTrue(report["summary"]["suspected_secrets"])
                self.assert_not_leaked(result, token)


if __name__ == "__main__":
    unittest.main()
