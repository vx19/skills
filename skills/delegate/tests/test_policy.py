from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from delegate.lib.policy import path_matches, violations  # noqa: E402


class PolicyTests(unittest.TestCase):
    def test_deny(self) -> None:
        self.assertEqual(
            violations(["src/a.py", "secrets/k"], [], ["secrets/"]),
            ["secrets/k"],
        )

    def test_allow_miss(self) -> None:
        self.assertEqual(
            violations(["src/a.py", "README.md"], ["src/"], []),
            ["README.md"],
        )

    def test_allow_and_deny(self) -> None:
        # deny wins even inside allow
        self.assertEqual(
            violations(["src/a.py", "src/secret.py"], ["src/"], ["src/secret.py"]),
            ["src/secret.py"],
        )

    def test_glob(self) -> None:
        self.assertTrue(path_matches("tests/test_x.py", "tests/*.py"))
        self.assertTrue(path_matches("src/pkg/mod.py", "src"))
        self.assertFalse(path_matches("other/x.py", "src/"))


if __name__ == "__main__":
    unittest.main()
