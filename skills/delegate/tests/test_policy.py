from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delegate.lib.policy import path_matches, violations  # noqa: E402


class PolicyTests(unittest.TestCase):
    def test_allow_miss(self) -> None:
        self.assertEqual(
            violations(["src/a.py", "README.md"], ["src/"]),
            ["README.md"],
        )

    def test_a_dot_prefixed_name_is_not_inside_the_scope(self) -> None:
        self.assertEqual(violations([".src/evil.py", "..src/x", "src/ok.py"], ["src"]),
                         [".src/evil.py", "..src/x"])
        self.assertFalse(path_matches(".env", "env"))
        self.assertTrue(path_matches("src/a.py", "./src/"))

    def test_dot_is_the_whole_tree(self) -> None:
        self.assertEqual(violations(["a", "src/b"], ["."]), [])
        self.assertEqual(violations(["a"], [""]), [])

    def test_glob(self) -> None:
        self.assertTrue(path_matches("tests/test_x.py", "tests/*.py"))
        self.assertTrue(path_matches("src/pkg/mod.py", "src"))
        self.assertFalse(path_matches("other/x.py", "src/"))


if __name__ == "__main__":
    unittest.main()
