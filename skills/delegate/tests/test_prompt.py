import sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from delegate.prompt import (
    assemble, read_task, read_rules, BOILERPLATE, READ_ONLY_BOILERPLATE,
    COMPUTER_USE_CLAUSE, PromptError,
)

# The write-early clause is only on a run that may edit. Read-only and
# computer-use prompts keep their own text.
_EARLY_WRITE = (
    "Write work to disk early and in small pieces; prefer several small edits "
    "over one large single-pass write."
)


class TestPrompt(unittest.TestCase):
    def test_order_rules_task_boilerplate(self):
        out = assemble("TASKBODY", ["RULE1", "RULE2"])
        i_r1, i_r2 = out.index("RULE1"), out.index("RULE2")
        i_task, i_bp = out.index("TASKBODY"), out.index(BOILERPLATE)
        self.assertLess(i_r1, i_r2)
        self.assertLess(i_r2, i_task)
        self.assertLess(i_task, i_bp)

    def test_inline_task(self):
        self.assertEqual(read_task("hello", task_is_file=False), "hello")

    def test_empty_task_raises(self):
        with self.assertRaises(PromptError):
            read_task("   ", task_is_file=False)

    def test_task_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.md"; p.write_text("FROMFILE")
            self.assertEqual(read_task(str(p), task_is_file=True), "FROMFILE")

    def test_missing_rules_file_raises(self):
        with self.assertRaises(PromptError):
            read_rules(["/no/such/rules.md"])

    def test_boilerplate_asks_for_early_small_writes(self):
        self.assertIn(_EARLY_WRITE, BOILERPLATE)
        self.assertNotIn("write work to disk early", READ_ONLY_BOILERPLATE)
        self.assertNotIn("write work to disk early", COMPUTER_USE_CLAUSE)
        self.assertIn(_EARLY_WRITE, assemble("TASK", []))
        self.assertNotIn("write work to disk early", assemble("TASK", [], read_only=True))
        self.assertNotIn(
            "write work to disk early",
            assemble("TASK", [], computer_use=True).replace(BOILERPLATE, ""),
        )


if __name__ == "__main__":
    unittest.main()
