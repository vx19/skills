from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("publish_gate.py")
IDENTITY = "Example User <user@example.com>"


class PublishGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "candidate"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Example User")
        self.git("config", "user.email", "user@example.com")
        self.allow = self.root / "allow.txt"
        self.allow.write_text("Example User\nuser@example.com\n", encoding="utf-8")
        self.commit("clean\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", *args], cwd=self.repo, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True)

    def commit(self, content: str) -> None:
        (self.repo / "content.txt").write_text(content, encoding="utf-8")
        self.git("add", "content.txt")
        self.git("commit", "-qm", "snapshot")

    def run_gate(self, *args: str, allow: bool = True) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        command = [sys.executable, str(SCRIPT), str(self.repo), "--identity", IDENTITY]
        if allow:
            command.extend(["--allow-file", str(self.allow)])
        result = subprocess.run([*command, *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, check=False)
        report = json.loads(result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertNotIn("READY", result.stdout)
        self.assertNotIn(IDENTITY, result.stdout)
        self.assertNotIn("user@example.com", result.stdout)
        self.assert_not_leaked(result, "Example User")
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["mode"], "publish-gate")
        self.assertEqual(result.stdout.strip(), json.dumps(report, sort_keys=True, separators=(",", ":")))
        return result, report

    def check_named(self, report: dict[str, object], name: str) -> dict[str, object]:
        return next(entry for entry in report["checks"] if entry["name"] == name)

    def assert_not_leaked(self, result: subprocess.CompletedProcess[str], value: str) -> None:
        self.assertNotIn(value, result.stdout)
        self.assertNotIn(value, result.stderr)

    def test_clean_one_commit_matching_identity(self) -> None:
        result, report = self.run_gate()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["gate"], "PATTERN_CLEAN")
        self.assertTrue(all(entry["status"] == "pass" for entry in report["checks"]))
        self.assertEqual(len(report["checks"]), 5)
        self.assertIn("contextual review is still required", " ".join(report["limitations"]))

    def test_second_commit_blocks_unless_limit_increased(self) -> None:
        self.commit("second\n")
        result, report = self.run_gate()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["gate"], "BLOCKED")
        self.assertEqual(self.check_named(report, "commit-count")["categories"], {"commit-count": 1})
        result, report = self.run_gate("--max-commits", "2")
        self.assertEqual(result.returncode, 0)

    def test_tag_blocks_without_echoing_ref(self) -> None:
        marker = "ghp_" + "T" * 28
        self.git("tag", marker)
        result, report = self.run_gate()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.check_named(report, "refs")["categories"], {"non-branch-ref": 1})
        self.assert_not_leaked(result, marker)

    def test_author_mismatch_reports_only_commit_and_role(self) -> None:
        email = "other@example.com"
        self.git("commit", "--amend", "--no-edit", "--author", f"Example User <{email}>")
        commit_id = self.git("rev-parse", "HEAD").stdout.decode().strip()
        result, report = self.run_gate()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["gate"], "BLOCKED")
        identities = self.check_named(report, "identities")
        self.assertEqual(identities["finding_count"], 1)
        self.assertEqual(identities["findings"], [{"category": "identity-mismatch",
                                                 "commit_id": commit_id, "role": "author"}])
        self.assert_not_leaked(result, email)

    def test_secret_blocks_and_never_leaks(self) -> None:
        token = "ghp_" + "A" * 28
        (self.repo / "value.txt").write_text(token + "\n", encoding="utf-8")
        result, report = self.run_gate()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["gate"], "BLOCKED")
        secrets = self.check_named(report, "safe_repo_scan")
        self.assertEqual(secrets["status"], "fail")
        self.assertEqual(secrets["categories"], {"github-token": 1})
        self.assert_not_leaked(result, token)

    def test_pii_only_requires_review(self) -> None:
        email = "contact@example.com"
        (self.repo / "contact.txt").write_text(email + "\n", encoding="utf-8")
        result, report = self.run_gate()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["gate"], "REVIEW")
        self.assertEqual(self.check_named(report, "pii_scan")["categories"], {"email": 1})
        self.assert_not_leaked(result, email)

    def test_missing_identity_is_argparse_error(self) -> None:
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.repo)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--identity", result.stderr)

    def test_identity_permission_does_not_suppress_pii_review(self) -> None:
        result, report = self.run_gate(allow=False)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["gate"], "REVIEW")
        self.assertEqual(self.check_named(report, "identities")["status"], "pass")
        self.assertEqual(self.check_named(report, "pii_scan")["categories"],
                         {"author-field": 2, "email": 2})

    def test_committer_mismatch_and_multiple_allowed_identities(self) -> None:
        email = "other@example.com"
        other = f"Example User <{email}>"
        self.git("config", "user.email", email)
        self.git("commit", "--amend", "--no-edit")
        result, report = self.run_gate()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.check_named(report, "identities")["findings"][0]["role"], "committer")
        self.assert_not_leaked(result, email)
        self.allow.write_text("Example User\nuser@example.com\n" + email + "\n", encoding="utf-8")
        result, report = self.run_gate("--identity", other)
        self.assertEqual(result.returncode, 0)
        self.assert_not_leaked(result, email)

    def test_all_reachable_identities_are_checked(self) -> None:
        email = "other@example.com"
        self.git("commit", "--amend", "--no-edit", "--author", f"Example User <{email}>")
        old_id = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.commit("second\n")
        result, report = self.run_gate("--max-commits", "2")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.check_named(report, "identities")["findings"][0]["commit_id"], old_id)
        self.assert_not_leaked(result, email)

    def test_notes_and_stash_refs_block_but_branches_pass(self) -> None:
        self.git("branch", "public-handle")
        result, report = self.run_gate()
        self.assertEqual(result.returncode, 0)
        for ref in ("refs/notes/review", "refs/stash"):
            with self.subTest(ref=ref):
                self.git("update-ref", ref, "HEAD")
                result, report = self.run_gate()
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.check_named(report, "refs")["status"], "fail")
                self.git("update-ref", "-d", ref)

    def test_deny_policy_overrides_allow_policy(self) -> None:
        deny = self.root / "deny.txt"
        deny.write_text("user@example.com\n", encoding="utf-8")
        result, report = self.run_gate("--deny-file", str(deny))

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.check_named(report, "pii_scan")["categories"], {"denied-identity": 2})

    def test_missing_policy_is_incomplete_and_blocks(self) -> None:
        result, report = self.run_gate("--deny-file", str(self.root / "missing.txt"))

        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["gate"], "BLOCKED")
        self.assertEqual(self.check_named(report, "pii_scan")["status"], "incomplete")

    def test_non_repository_blocks_without_echoing_target(self) -> None:
        marker = "ghp_" + "B" * 28
        self.repo = self.root / marker
        self.repo.mkdir()
        result, report = self.run_gate()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["gate"], "BLOCKED")
        self.assertTrue(any(entry["status"] == "incomplete" for entry in report["checks"]))
        self.assert_not_leaked(result, marker)

    def test_value_overlap_requires_review_and_csv_columns_are_forwarded(self) -> None:
        value = "38172.6381"
        public = "47183.7462"
        source = self.root / "source.csv"
        source.write_text(f"private,public\n{value},{public}\n", encoding="utf-8")
        self.commit(value + "\n")
        result, report = self.run_gate("--max-commits", "2", "--source", str(source),
                                       "--csv-column", "private")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["gate"], "REVIEW")
        self.assertEqual(self.check_named(report, "value_overlap_scan")["categories"], {"value-overlap": 1})
        self.assert_not_leaked(result, value)
        self.assert_not_leaked(result, public)
        result, report = self.run_gate("--max-commits", "2", "--source", str(source),
                                       "--csv-column", "public")
        self.assertEqual(result.returncode, 0)
        self.assert_not_leaked(result, value)

    def test_repeated_sources_and_columns_are_forwarded(self) -> None:
        first, second = "38172.6381", "47183.7462"
        sources = [self.root / "first.csv", self.root / "second.csv"]
        sources[0].write_text(f"first,second\n{first},\n", encoding="utf-8")
        sources[1].write_text(f"first,second\n,{second}\n", encoding="utf-8")
        self.commit(first + "\n" + second + "\n")
        result, report = self.run_gate("--max-commits", "2", "--source", str(sources[0]),
                                       "--source", str(sources[1]), "--csv-column", "first",
                                       "--csv-column", "second")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.check_named(report, "value_overlap_scan")["finding_count"], 1)
        self.assert_not_leaked(result, first)
        self.assert_not_leaked(result, second)

    def test_unreadable_source_blocks_without_echoing_path(self) -> None:
        marker = "ghp_" + "C" * 28
        result, report = self.run_gate("--source", str(self.root / marker))

        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.check_named(report, "value_overlap_scan")["status"], "incomplete")
        self.assert_not_leaked(result, marker)

    def test_invalid_arguments_do_not_echo_values(self) -> None:
        marker = "ghp_" + "D" * 28
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.repo), "--identity", IDENTITY,
                                 "--max-commits", marker], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assert_not_leaked(result, marker)
        self.assert_not_leaked(result, IDENTITY)

    def test_help_recommends_noreply_identity(self) -> None:
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("@users.noreply.github.com", result.stdout)

    def test_symlink_loops_block_without_echoing_paths(self) -> None:
        marker = "ghp_" + "E" * 28
        loop = self.root / marker
        loop.symlink_to(loop.name)
        repo = self.repo
        for option in ("target", "--allow-file", "--source"):
            with self.subTest(option=option):
                if option == "target":
                    self.repo = loop
                    result, report = self.run_gate()
                    self.repo = repo
                else:
                    result, report = self.run_gate(option, str(loop))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(report["gate"], "BLOCKED")
                self.assert_not_leaked(result, marker)


if __name__ == "__main__":
    unittest.main()
