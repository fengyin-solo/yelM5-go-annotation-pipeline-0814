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

    def test_diagnosis_accepts_state_corruption_described_as_data_being_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "collection.json").write_text(json.dumps({
                "gold_root_cause": "文件: internal/model/topic.go、internal/store/topic.go、internal/service/topic.go 符号: Topic.Validate、UpdateTopic、Service.UpdateTopic 机制: 存储在返回冲突前先覆盖原对象，服务又吞掉冲突错误，失败更新变成可见成功和同名状态。"
            }), encoding="utf-8")
            (project / "difficulty_review.json").write_text(json.dumps({
                "core_defect_review": {"root_cause_locations": [
                    {"file": "internal/model/topic.go"},
                    {"file": "internal/store/topic.go"},
                    {"file": "internal/service/topic.go"},
                ]}
            }), encoding="utf-8")
            answer = (
                "internal/model/topic.go 的 Validate 接受该名称；"
                "internal/store/topic.go 的 UpdateTopic 在冲突时先写入，把原主题改坏成同名项；"
                "internal/service/topic.go 的 UpdateTopic 又丢掉错误，所以对外返回成功。"
            )
            transcript = root / "sid.jsonl"
            transcript.write_text(json.dumps({
                "type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            result = trajectory_acceptance._diagnosis_root_cause_check(project, transcript)
            self.assertTrue(result["passed"], result["output"])

    def test_diagnosis_recognizes_filter_enumeration_and_aggregation_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "collection.json").write_text(json.dumps({
                "gold_root_cause": "文件: internal/model/event.go、internal/store/event.go、internal/service/stats.go 符号: EventFilter.Match、ListEvents、Overview 机制: 状态筛选拿 topic_id 比较，存储列表跳过 delivered，概览又漏记 pending。"
            }), encoding="utf-8")
            (project / "difficulty_review.json").write_text(json.dumps({
                "core_defect_review": {"root_cause_locations": [
                    {"file": "internal/model/event.go"},
                    {"file": "internal/store/event.go"},
                    {"file": "internal/service/stats.go"},
                ]}
            }), encoding="utf-8")
            answer = (
                "internal/model/event.go 的 Match 用 TopicID 做状态匹配；"
                "internal/store/event.go 的 ListEvents 过滤掉 delivered；"
                "internal/service/stats.go 的 Overview 基于这份列表累计，所以概览总数和分类计数一起减少。"
            )
            transcript = root / "sid.jsonl"
            transcript.write_text(json.dumps({
                "type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            result = trajectory_acceptance._diagnosis_root_cause_check(project, transcript)
            self.assertTrue(result["passed"], result["output"])


if __name__ == "__main__":
    unittest.main()
