"""SKILL.md is what an orchestrator acts on; keep its tables true to the CLI."""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from delegate import cli, envelope  # noqa: E402

SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")


def _table_after(heading: str) -> list[str]:
    """The first column of the first table under `heading`."""
    body = SKILL.split(heading, 1)[1]
    rows = []
    for line in body.splitlines():
        if line.startswith("|"):
            rows.append(line.split("|")[1].strip())
        elif rows:
            break
    return rows[2:]  # drop the header and its rule


class SkillDocTests(unittest.TestCase):
    def test_every_run_flag_is_in_the_flag_table(self):
        run = cli.build_parser()._subparsers._group_actions[0].choices["run"]
        flags = {o for a in run._actions for o in a.option_strings
                 if o.startswith("--") and o != "--help"}
        table = " ".join(_table_after("`run` flags"))
        missing = sorted(f for f in flags if not re.search(re.escape(f) + r"\b", table))
        self.assertEqual(missing, [])

    def test_the_exit_code_table_lists_every_code_the_cli_returns(self):
        codes = {int(c.strip("`")) for c in _table_after("## Exit codes")}
        self.assertEqual(codes, {0, 1, 2, cli.WAIT_PENDING_EXIT, cli.USAGE_EXIT, 130})

    def test_the_documented_stop_reasons_are_the_ones_the_envelope_uses(self):
        line = next(l for l in SKILL.splitlines() if l.startswith("- `stopReason` — one of"))
        documented = set(re.findall(r"`([a-z_]+)`", line.split("one of", 1)[1]))
        used = {envelope.STOP_END, envelope.STOP_CANCELLED, envelope.STOP_TIMEOUT,
                envelope.STOP_ERROR}
        self.assertEqual(documented, used)


if __name__ == "__main__":
    unittest.main()
