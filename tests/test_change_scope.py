import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from change_scope import meets_minimum_functional_change  # noqa: E402
from batch_preflight import _functional_diff  # noqa: E402
from post_qc import functional_diff_scope  # noqa: E402


class ChangeScopeTest(unittest.TestCase):
    def test_four_changed_lines_are_rejected(self):
        self.assertFalse(meets_minimum_functional_change(1, 4))

    def test_five_changed_lines_are_accepted(self):
        self.assertTrue(meets_minimum_functional_change(1, 5))

    def test_changed_lines_still_require_a_functional_file(self):
        self.assertFalse(meets_minimum_functional_change(0, 5))

    def test_preflight_and_post_qc_count_five_added_go_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            buggy = root / "buggy"
            gold = root / "gold"
            buggy.mkdir()
            gold.mkdir()
            (buggy / "main.go").write_text("package demo\n", encoding="utf-8")
            (gold / "main.go").write_text(
                "package demo\n\nvar a = 1\nvar b = 2\nvar c = 3\nvar d = 4\n",
                encoding="utf-8",
            )
            self.assertEqual((1, 5), _functional_diff(buggy, gold))
            self.assertEqual((1, 5), functional_diff_scope(buggy, gold))

    def test_non_go_changes_do_not_satisfy_functional_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            buggy = root / "buggy"
            gold = root / "gold"
            buggy.mkdir()
            gold.mkdir()
            (buggy / "config.yaml").write_text("enabled: false\n", encoding="utf-8")
            (gold / "config.yaml").write_text(
                "enabled: true\na: 1\nb: 2\nc: 3\nd: 4\ne: 5\n",
                encoding="utf-8",
            )
            self.assertEqual((0, 0), _functional_diff(buggy, gold))
            self.assertEqual((0, 0), functional_diff_scope(buggy, gold))


if __name__ == "__main__":
    unittest.main()
