"""V1/V2-class snapshot tests: private index, dirty tree, new files."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delegate.lib.policy import violations  # noqa: E402
from delegate.lib import snapshot as snap  # noqa: E402
from delegate.lib.snapshot import (  # noqa: E402
    SnapshotError,
    diff_from_tree,
    diff_paths,
    snapshot_tree,
)


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    e = os.environ.copy()
    if env:
        e.update(env)
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env=e,
    )
    return proc.stdout


def _init_repo(root: Path) -> Path:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return root


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        base = Path(self._td.name)
        # Private index must live *outside* the work tree or `git add -A` snapshots it.
        repo = base / "repo"
        repo.mkdir()
        self.repo = _init_repo(repo)
        self.idx_dir = base / "idx"
        self.idx_dir.mkdir()
        self.head = _git(self.repo, "rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_new_file_appears_without_touching_user_index(self) -> None:
        (self.repo / "new.py").write_text("print(1)\n", encoding="utf-8")
        before = _git(self.repo, "diff", "--cached", "--name-only")
        diff = diff_from_tree(self.repo, self.head, self.idx_dir / "priv-index")
        after = _git(self.repo, "diff", "--cached", "--name-only")
        self.assertIn("new.py", diff)
        self.assertIn("new.py", diff_paths(diff))
        self.assertEqual(before, after)
        # real index still empty
        self.assertEqual(after.strip(), "")

    def test_dirty_tracked_file_visible(self) -> None:
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        diff = diff_from_tree(self.repo, self.head, self.idx_dir / "dirty")
        self.assertIn("tracked.txt", diff)
        self.assertIn("-base", diff)
        self.assertIn("+dirty", diff)

    def test_user_staged_set_unchanged(self) -> None:
        (self.repo / "staged.txt").write_text("staged\n", encoding="utf-8")
        _git(self.repo, "add", "staged.txt")
        (self.repo / "other.txt").write_text("other\n", encoding="utf-8")
        staged_before = _git(self.repo, "diff", "--cached", "--name-only")
        diff = diff_from_tree(self.repo, self.head, self.idx_dir / "idx2")
        staged_after = _git(self.repo, "diff", "--cached", "--name-only")
        self.assertEqual(staged_before, staged_after)
        self.assertIn("staged.txt", staged_after)
        self.assertIn("other.txt", diff)
        self.assertIn("staged.txt", diff)

    def test_clean_tree_empty_diff(self) -> None:
        diff = diff_from_tree(self.repo, self.head, self.idx_dir / "idx3")
        self.assertEqual(diff.strip(), "")
        self.assertEqual(diff_paths(diff), [])

    def test_dirty_noop_empty_worker_delta(self) -> None:
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        staged_before = _git(self.repo, "diff", "--cached", "--name-only")
        tree0 = snapshot_tree(self.repo, self.idx_dir / "index0")
        self.assertTrue(tree0)
        delta = diff_from_tree(self.repo, tree0, self.idx_dir / "index1")
        staged_after = _git(self.repo, "diff", "--cached", "--name-only")
        self.assertEqual(delta.strip(), "")
        self.assertEqual(diff_paths(delta), [])
        self.assertEqual(staged_before, staged_after)

    def test_dirty_plus_new_file_only_in_worker_delta(self) -> None:
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        staged_before = _git(self.repo, "diff", "--cached", "--name-only")
        tree0 = snapshot_tree(self.repo, self.idx_dir / "index0")
        (self.repo / "new.py").write_text("print(1)\n", encoding="utf-8")
        delta = diff_from_tree(self.repo, tree0, self.idx_dir / "index1")
        staged_after = _git(self.repo, "diff", "--cached", "--name-only")
        self.assertIn("new.py", delta)
        self.assertNotIn("tracked.txt", delta)
        self.assertEqual(diff_paths(delta), ["new.py"])
        self.assertEqual(staged_before, staged_after)
        self.assertEqual(staged_after.strip(), "")

    def test_real_git_spaced_and_cquoted_names_in_worker_delta(self) -> None:
        """Paths come from live git diff, not a hand-written header."""
        tree0 = snapshot_tree(self.repo, self.idx_dir / "index0")
        (self.repo / "weird file.txt").write_text("space\n", encoding="utf-8")
        (self.repo / "tab\tname.txt").write_text("tab\n", encoding="utf-8")
        (self.repo / "café.txt").write_text("cafe\n", encoding="utf-8")
        delta = diff_from_tree(self.repo, tree0, self.idx_dir / "index1")
        paths = diff_paths(delta)
        self.assertIn("weird file.txt", paths)
        self.assertIn("tab\tname.txt", paths)
        self.assertIn("café.txt", paths)
        # deny-path would miss these if the parser returned []
        self.assertEqual(
            violations(paths, [], ["weird file.txt"]),
            ["weird file.txt"],
        )

    def test_allow_deny_ignore_preexisting_dirty(self) -> None:
        (self.repo / "other.txt").write_text("pre\n", encoding="utf-8")
        tree0 = snapshot_tree(self.repo, self.idx_dir / "index0")
        src = self.repo / "src"
        src.mkdir()
        (src / "a.py").write_text("x\n", encoding="utf-8")
        delta = diff_from_tree(self.repo, tree0, self.idx_dir / "index1")
        paths = diff_paths(delta)
        self.assertEqual(paths, ["src/a.py"])
        self.assertEqual(violations(paths, ["src/"], []), [])
        self.assertEqual(violations(paths, ["src/"], ["secrets/"]), [])
        self.assertEqual(violations(paths, [], ["src/"]), ["src/a.py"])

    def test_latin1_content_roundtrips_through_the_diff(self) -> None:
        tree0 = snapshot_tree(self.repo, self.idx_dir / "index0")
        raw = b"caf\xe9 and \xff\xfe\n"
        (self.repo / "latin1.txt").write_bytes(raw)
        diff = diff_from_tree(self.repo, tree0, self.idx_dir / "index1")
        self.assertIn("latin1.txt", diff_paths(diff))
        patch = self.idx_dir / "diff.patch"
        patch.write_bytes(diff.encode("utf-8", errors="surrogateescape"))
        self.assertIn(raw, patch.read_bytes())

    def test_add_failure_raises(self) -> None:
        index = self.idx_dir / "index-fail"
        lock = Path(str(index) + ".lock")
        lock.write_text("", encoding="utf-8")
        with self.assertRaises(SnapshotError):
            snapshot_tree(self.repo, index)

    def test_unreadable_path_is_excluded_and_named(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake(cwd, env, *args, check=False):
            calls.append(args)
            if args[:2] == ("add", "-A") and not any(
                str(a).startswith(":(exclude)") for a in args
            ):
                return subprocess.CompletedProcess(
                    args, 128, "",
                    "fatal: unable to stat 'secret.env': Operation not permitted\n",
                )
            return subprocess.CompletedProcess(args, 0, "", "")

        err = io.StringIO()
        with patch.object(snap, "_git", side_effect=fake), redirect_stderr(err):
            snap._add_all(self.repo, {})
        retry = [c for c in calls if ":(exclude)secret.env" in c]
        self.assertEqual(retry, [("add", "-A", "--", ".", ":(exclude)secret.env")])
        self.assertIn(("read-tree", "HEAD"), calls)
        self.assertIn("secret.env", err.getvalue())

    def test_same_unreadable_path_twice_still_fails(self) -> None:
        def fake(cwd, env, *args, check=False):
            if args[:2] == ("add", "-A"):
                return subprocess.CompletedProcess(
                    args, 128, "",
                    "fatal: unable to stat 'secret.env': Permission denied\n",
                )
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(snap, "_git", side_effect=fake):
            with self.assertRaises(SnapshotError) as caught:
                snap._add_all(self.repo, {})
        self.assertIn("Permission denied", str(caught.exception))

    def test_too_many_unreadable_paths_still_fail(self) -> None:
        blob = "".join(
            f"fatal: unable to stat 'p{i}': Operation not permitted\n"
            for i in range(65)
        )

        def fake(cwd, env, *args, check=False):
            return subprocess.CompletedProcess(args, 128, "", blob)

        with patch.object(snap, "_git", side_effect=fake):
            with self.assertRaises(SnapshotError):
                snap._add_all(self.repo, {})


if __name__ == "__main__":
    unittest.main()
