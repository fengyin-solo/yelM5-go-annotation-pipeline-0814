import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import post_qc  # noqa: E402
from verify_cmds import validate_success_criteria  # noqa: E402


class PostQCTest(unittest.TestCase):
    def test_patch_go_version_is_preserved_for_toolchain(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "go.mod").write_text("module example\n\ngo 1.26.3\n", encoding="utf-8")
            version = post_qc.detect_go_version(project)
            self.assertEqual("1.26.3", version)
            self.assertEqual("go1.26.3", post_qc.go_env(version)["GOTOOLCHAIN"])

    def test_diagnosis_accepts_explicit_unchanged_wording(self):
        base = {
            "task_type": "diagnosis",
            "user_query": "主题更名后列表出现同名项",
        }
        for criteria in (
            "主题更名后列表出现同名项；工作区保持不变。",
            "主题更名后列表出现同名项；项目内容不被修改。",
            "主题更名后列表出现同名项；整个工作区不发生变更。",
        ):
            self.assertEqual([], validate_success_criteria({**base, "success_criteria": criteria}))


if __name__ == "__main__":
    unittest.main()
