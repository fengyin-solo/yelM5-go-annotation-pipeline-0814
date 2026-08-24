import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from user_query_rules import user_query_go_version_issues  # noqa: E402


class UserQueryRulesTest(unittest.TestCase):
    def test_rejects_go_version_environment_variants(self):
        samples = [
            "Go 1.23 项目可以直接改，帮我修好。",
            "项目的 Go版本为1.23.0，先别改。",
            "这是 Go v1.24 的工程。",
            "1.25 版 Go 项目请直接修。",
            "Go 工具链是 1.23，帮我看看。",
            "Go版本已经装好，当前问题帮我修一下。",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertTrue(user_query_go_version_issues(sample))

    def test_allows_current_project_wording_and_go_mechanisms(self):
        samples = [
            "当前项目就可以了，帮我把重试修好。",
            "当前项目可以直接改，帮我把重试修好。",
            "当前项目先别改，帮我查清取消链为什么断了。",
            "这个 Go 项目的 goroutine 取消后还在继续写状态。",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual([], user_query_go_version_issues(sample))


if __name__ == "__main__":
    unittest.main()
