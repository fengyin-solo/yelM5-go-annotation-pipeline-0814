import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import post_qc  # noqa: E402
from verify_cmds import validate_success_criteria  # noqa: E402


class PostQCTest(unittest.TestCase):
    def test_read_collection_returns_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "collection.json").write_text('{"task_type":"bugfix"}', encoding="utf-8")
            self.assertEqual({"task_type": "bugfix"}, post_qc.read_collection(project))

    def test_runtime_evidence_requires_matching_input_fingerprint(self):
        checks = {
            name: {"passed": True}
            for name in ("env_build", "gold_build", "gold_regression", "red_calibration", "green_calibration")
        }
        preflight = {"result": "passed", "fingerprint": "input-v1", "checks": checks}
        self.assertEqual(
            (True, "preflight 与 Docker 的输入绑定运行证据通过（未重复执行）"),
            post_qc.runtime_evidence_status(
                preflight, {"result": "passed", "fingerprint": "input-v1"}, "input-v1"
            ),
        )
        ok, message = post_qc.runtime_evidence_status(
            preflight, {"result": "passed", "fingerprint": "input-v2"}, "input-v1"
        )
        self.assertFalse(ok)
        self.assertIn("指纹不一致", message)
        ok, message = post_qc.runtime_evidence_status(
            preflight, {"result": "passed", "fingerprint": "input-v1"}, "input-v2"
        )
        self.assertFalse(ok)
        self.assertIn("当前输入", message)

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
