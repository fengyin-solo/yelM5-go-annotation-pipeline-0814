import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import trajectory_acceptance  # noqa: E402


class TrajectoryAcceptanceTest(unittest.TestCase):
    def test_private_failure_fails_whole_acceptance_and_persists_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, workspace, snapshot, evaluator = [root / x for x in ("project", "workspace", "snapshot", "evaluator")]
            for path in (project, workspace, snapshot, evaluator):
                path.mkdir()
            (project / "collection.json").write_text("{}", encoding="utf-8")
            transcript = root / "sid.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            passing = {"passed": True, "exit_code": 0, "output": "ok"}
            failing = {"passed": False, "exit_code": 1, "output": "zero calls: got 1"}
            with patch.object(trajectory_acceptance, "_analysis_check", return_value=passing), \
                 patch.object(trajectory_acceptance, "_regression_check", return_value=passing), \
                 patch.object(trajectory_acceptance, "_semantic_check", return_value=passing), \
                 patch.object(trajectory_acceptance, "_private_check", return_value=failing):
                result = trajectory_acceptance.run_acceptance(
                    project=project, workspace=workspace, snapshot=snapshot,
                    transcript=transcript, session_id="sid", task_type="bugfix",
                    verify_cmds="go test .", evaluator=evaluator, module_path=None,
                    env={}, timeout=1,
                )
            self.assertEqual("failed", result["result"])
            saved = json.loads((project / "_evidence" / "trajectory_acceptance.json").read_text())
            self.assertIn("zero calls", saved["checks"]["private_verify"]["output"])

    def test_diagnosis_requires_root_cause_locations_symbols_and_mechanism(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "collection.json").write_text(json.dumps({
                "gold_root_cause": "文件: internal/a.go, internal/b.go, internal/c.go。符号: StartWorker StopWorker SaveState。机制: context 取消未传递，重试 goroutine 继续写入状态。"
            }), encoding="utf-8")
            (project / "difficulty_review.json").write_text(json.dumps({
                "core_defect_review": {"root_cause_locations": [
                    {"file": "internal/a.go"}, {"file": "internal/b.go"}, {"file": "internal/c.go"}
                ]}
            }), encoding="utf-8")
            transcript = root / "sid.jsonl"
            answer = "internal/a.go 的 StartWorker 和 internal/b.go 的 StopWorker 没有传递 context 取消，internal/c.go 的 SaveState 因此被重试 goroutine 继续写入状态。"
            transcript.write_text(json.dumps({
                "type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            result = trajectory_acceptance._diagnosis_root_cause_check(project, transcript)
            self.assertTrue(result["passed"], result["output"])
            transcript.write_text(json.dumps({
                "type": "assistant", "message": {"content": [{"type": "text", "text": "应该是取消有问题。"}]}
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            self.assertFalse(trajectory_acceptance._diagnosis_root_cause_check(project, transcript)["passed"])


if __name__ == "__main__":
    unittest.main()
