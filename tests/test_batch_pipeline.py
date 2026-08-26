import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import batch_pipeline  # noqa: E402
import batch_preflight  # noqa: E402
from batch_state import input_fingerprint, load_json, update_status  # noqa: E402
from pretool_workspace_guard import inspect_hook  # noqa: E402


class BatchPipelineTest(unittest.TestCase):
    def make_project(self, root: Path) -> Path:
        project = root / "2026-08-24" / "demo-service__001"
        for path in (project / "env", project / "evaluator", root / "_gold" / project.name):
            path.mkdir(parents=True, exist_ok=True)
        (project / "status.json").write_text(json.dumps({"name": project.name, "repo": "demo-service"}))
        (project / "prompt.txt").write_text("当前项目就可以了，帮我修好这个问题。", encoding="utf-8")
        (project / "project_summary.txt").write_text(
            "基于 Go 实现的设备状态服务，提供设备状态写入与查询 API。\n", encoding="utf-8"
        )
        (project / "collection.json").write_text(json.dumps({
            "bug_id": "demo-001", "session_id": "00000000-0000-4000-8000-000000000001",
            "task_type": "bugfix", "bug_category": "error异常错误",
            "user_query": "当前项目就可以了，帮我修好这个问题。",
            "verify_cmds": "go test ./internal/demo -run '^TestDemo$' -count=1",
        }, ensure_ascii=False), encoding="utf-8")
        for base in (project / "env", project / "evaluator", root / "_gold" / project.name):
            (base / "x.go").write_text("package demo\n", encoding="utf-8")
        return project

    def test_fingerprint_ignores_mutable_delivery_fields_but_tracks_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.make_project(root)
            first = input_fingerprint(project, root)
            data = load_json(project / "collection.json")
            data.update({"session_id": "sid", "trajectory": "https://example.test/x", "repo_url": "https://example.test/r"})
            (project / "collection.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(first, input_fingerprint(project, root))
            (project / "prompt.txt").write_text("题面被改了", encoding="utf-8")
            self.assertNotEqual(first, input_fingerprint(project, root))

    def test_fingerprint_can_use_immutable_bug_snapshot_after_model_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.make_project(root)
            snapshot = project / ".base_snapshot"
            shutil.copytree(project / "env", snapshot)
            before = input_fingerprint(project, root)
            (project / "env" / "main.go").write_text("package demo\nfunc Fixed() {}\n", encoding="utf-8")
            self.assertNotEqual(before, input_fingerprint(project, root))
            self.assertEqual(before, input_fingerprint(project, root, env_source=snapshot))

    def test_status_updates_are_additive_and_keep_attempt_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self.make_project(Path(tmp))
            update_status(project, stage="preflight_passed", fingerprint="abc")
            update_status(project, stage="main_running", result="failed",
                          attempt={"stage": "main_running", "attempt": 1, "result": "failed"})
            pipeline = load_json(project / "status.json")["pipeline"]
            self.assertEqual("abc", pipeline["input_fingerprint"])
            self.assertEqual(1, len(pipeline["attempt_history"]))
            self.assertEqual("passed", pipeline["stages"]["preflight_passed"]["result"])

    def test_reconcile_restores_final_repo_url_after_interrupted_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self.make_project(Path(tmp))
            (project / "_delivery").mkdir()
            (project / "_delivery" / "g1_snapshot.json").write_text("{}\n", encoding="utf-8")
            evidence = project / "_evidence"
            evidence.mkdir()
            repo_url = "https://github.com/example/demo/tree/bug001_red"
            (evidence / "repository_delivery.json").write_text(json.dumps({
                "state": "finalized", "repo_url": repo_url,
            }), encoding="utf-8")

            batch_pipeline.reconcile(project)

            self.assertEqual(repo_url, load_json(project / "collection.json")["repo_url"])
            self.assertEqual("passed", load_json(project / "status.json")["pipeline"]["stages"]["finalized"]["result"])

    def test_resume_rejects_changed_preflight_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.make_project(root)
            update_status(project, stage="preflight_passed", fingerprint=input_fingerprint(project, root))
            (project / "evaluator" / "x.go").write_text("package demo\n// changed\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "preflight inputs changed"):
                batch_pipeline.require_unchanged_inputs(project, root)

    def test_diagnosis_hook_blocks_writes_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            edit = {"tool_name": "Edit", "tool_input": {"file_path": str(workspace / "x.go")}, "cwd": str(workspace)}
            bash = {"tool_name": "Bash", "tool_input": {"command": "touch x.go"}, "cwd": str(workspace)}
            self.assertTrue(inspect_hook(edit, workspace, read_only=True))
            self.assertTrue(inspect_hook(bash, workspace, read_only=True))
            read = {"tool_name": "Read", "tool_input": {"file_path": str(workspace / "x.go")}, "cwd": str(workspace)}
            self.assertEqual([], inspect_hook(read, workspace, read_only=True))

    def test_finish_syncs_collection_and_registry_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = [self.make_project(root)]
            calls = []

            def fake_run(cmd, **_kwargs):
                calls.append(cmd)
                if "platform_submit.py" in " ".join(cmd):
                    result_path = Path(cmd[cmd.index("--result") + 1])
                    result_path.parent.mkdir(parents=True, exist_ok=True)
                    result_path.write_text(json.dumps({
                        "submitted": 1,
                        "skipped": 0,
                        "records": [{
                            "bug_id": "demo-001",
                            "session_id": "00000000-0000-4000-8000-000000000001",
                            "state": "submitted",
                            "submission_id": "submission-1",
                        }],
                    }), encoding="utf-8")
                return ""

            output = io.StringIO()
            with patch.object(batch_pipeline, "upload_one"), patch.object(
                batch_pipeline, "run_checked", side_effect=fake_run
            ), patch.object(batch_pipeline, "mark"), redirect_stdout(output):
                args = type("Args", (), {
                    "upload_workers": 3, "timeout": 1, "workers": 3,
                    "date": None, "projects": None,
                })()
                batch_pipeline.finish(projects, root, args)
            sync_calls = [cmd for cmd in calls if "collection_table.py" in " ".join(cmd) and "sync" in cmd]
            registry_sync = [cmd for cmd in calls if "repo_registry.py" in " ".join(cmd) and "sync" in cmd]
            self.assertEqual(1, len(sync_calls))
            self.assertEqual(1, len(registry_sync))
            platform_calls = [cmd for cmd in calls if "platform_submit.py" in " ".join(cmd)]
            self.assertEqual(1, len(platform_calls))
            qc_index = next(i for i, cmd in enumerate(calls) if "post_qc.py" in " ".join(cmd))
            platform_index = next(i for i, cmd in enumerate(calls) if "platform_submit.py" in " ".join(cmd))
            registry_index = next(i for i, cmd in enumerate(calls) if "repo_registry.py" in " ".join(cmd))
            self.assertLess(qc_index, platform_index)
            self.assertLess(platform_index, registry_index)
            self.assertEqual(
                "平台上传摘要：\n上传成功：1 条\n跳过：0 条\n提交 ID：\n- demo-001：submission-1",
                output.getvalue().strip(),
            )

    def test_completed_platform_stage_is_summarized_as_skipped_without_resubmission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.make_project(root)
            update_status(project, stage="platform_submitted")
            ledger = root / "_shared" / "platform-submissions.json"
            ledger.parent.mkdir(parents=True)
            key = batch_pipeline.identity_key((
                "demo-001", "00000000-0000-4000-8000-000000000001",
            ))
            ledger.write_text(json.dumps({"submissions": {key: {
                "state": "submitted", "submission_id": "submission-1", "status": "pending",
            }}}), encoding="utf-8")
            args = type("Args", (), {"timeout": 1})()
            with patch.object(batch_pipeline, "run_checked") as run:
                report = batch_pipeline.submit_platform([project], root, args)
            run.assert_not_called()
            self.assertEqual(0, report["submitted"])
            self.assertEqual(1, report["skipped"])
            self.assertEqual("submission-1", report["records"][0]["submission_id"])

    def test_model_phases_run_two_records_at_a_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = [root / f"demo__{index:03d}" for index in range(1, 5)]
            active = 0
            peak = 0
            lock = threading.Lock()

            def fake_phases(project, phase_root, args):
                nonlocal active, peak
                self.assertEqual(root, phase_root)
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.05)
                with lock:
                    active -= 1

            args = type("Args", (), {"model_workers": 2})()
            with patch.object(batch_pipeline, "model_phases", side_effect=fake_phases):
                batch_pipeline.run_model_phases(projects, root, args)
            self.assertEqual(2, peak)

    def test_model_phases_keep_each_record_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "demo__001"
            calls = []
            args = type("Args", (), {})()
            with patch.object(batch_pipeline, "red", side_effect=lambda *unused: calls.append("red")), patch.object(
                batch_pipeline, "main_trajectory", side_effect=lambda *unused: calls.append("main")
            ), patch.object(batch_pipeline, "green", side_effect=lambda *unused: calls.append("green")):
                batch_pipeline.model_phases(project, root, args)
            self.assertEqual(["red", "main", "green"], calls)

    def test_record_pipelines_overlap_without_reordering_each_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = [root / "demo__001", root / "demo__002"]
            events = []
            event_lock = threading.Lock()
            publish_barrier = threading.Barrier(2)

            def note(project, stage):
                with event_lock:
                    events.append((project.name, stage))

            def fake_publish(project, *_unused):
                note(project, "publish")
                publish_barrier.wait(timeout=1)

            def fake_model(project, *_unused):
                note(project, "model")
                time.sleep(0.12 if project.name.endswith("001") else 0.01)

            def fake_finalize(project, *_unused):
                note(project, "finalize")

            args = type("Args", (), {"workers": 2, "model_workers": 2})()
            with patch.object(batch_pipeline, "reconcile"), patch.object(
                batch_pipeline, "publish", side_effect=fake_publish
            ), patch.object(batch_pipeline, "model_phases", side_effect=fake_model), patch.object(
                batch_pipeline, "finalize", side_effect=fake_finalize
            ):
                batch_pipeline.run_record_pipelines(projects, root, args)

            for project in projects:
                stages = [stage for name, stage in events if name == project.name]
                self.assertEqual(["publish", "model", "finalize"], stages)
            self.assertLess(events.index(("demo__002", "finalize")), events.index(("demo__001", "finalize")))

    def test_default_model_workers_allow_eight_record_pipelines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = [root / f"demo__{index:03d}" for index in range(1, 9)]
            active = 0
            peak = 0
            active_lock = threading.Lock()
            barrier = threading.Barrier(8)

            def fake_record_pipeline(*_unused):
                nonlocal active, peak
                with active_lock:
                    active += 1
                    peak = max(peak, active)
                barrier.wait(timeout=2)
                with active_lock:
                    active -= 1

            args = type("Args", (), {"workers": 3, "model_workers": 8})()
            with patch.object(batch_pipeline, "record_pipeline", side_effect=fake_record_pipeline):
                batch_pipeline.run_record_pipelines(projects, root, args)
            self.assertEqual(8, peak)

    @unittest.skipUnless(shutil.which("go"), "Go toolchain is required")
    def test_isolated_evaluator_compile_rejects_original_test_helper_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "env"
            evaluator = root / "evaluator"
            source.mkdir()
            evaluator.mkdir()
            (source / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
            (source / "demo.go").write_text("package demo\n", encoding="utf-8")
            (source / "original_test.go").write_text(
                'package demo\nimport "testing"\nfunc setupOriginal(t *testing.T) {}\n', encoding="utf-8"
            )
            (evaluator / "target_test.go").write_text(
                'package demo\nimport "testing"\nfunc TestTarget(t *testing.T) { setupOriginal(t) }\n', encoding="utf-8"
            )
            result = batch_preflight._isolated_evaluator_compile(source, evaluator, os.environ.copy(), 120)
            self.assertFalse(result["passed"])
            self.assertIn("undefined: setupOriginal", result["output_tail"])

    def test_diagnosis_acceptance_precheck_rejects_unrecognizable_gold_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self.make_project(Path(tmp))
            data = load_json(project / "collection.json")
            data.update({
                "task_type": "diagnosis",
                "gold_root_cause": "文件: x.go 符号: UpdateThing 机制: 自定义业务描述，没有可识别的双重机制。",
            })
            (project / "collection.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            (project / "difficulty_review.json").write_text(json.dumps({
                "core_defect_review": {"root_cause_locations": [{"file": "x.go"}]}
            }), encoding="utf-8")
            result = batch_preflight._diagnosis_acceptance_precheck(project)
            self.assertFalse(result["passed"])
            self.assertIn("required_mechanisms", result["output"])

    def test_same_private_failure_stops_after_second_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.make_project(root)
            fingerprint = input_fingerprint(project, root)
            update_status(project, stage="preflight_passed", fingerprint=fingerprint)
            args = type("Args", (), {
                "main_attempts": 3, "rerun_reason": None, "model_timeout": 1, "model_workers": 2,
            })()
            error = RuntimeError(
                "trajectory acceptance failed\n"
                "- private_verify: --- FAIL: TestBoundary (0.00s)\n"
                "target_test.go:42: status=201"
            )
            with patch.object(batch_pipeline, "run_checked", side_effect=error) as run:
                with self.assertRaisesRegex(RuntimeError, "重复确定性失败已熔断"):
                    batch_pipeline.main_trajectory(project, root, args)
            self.assertEqual(2, run.call_count)
            attempts = load_json(project / "_evidence" / "attempt_history.json")["attempts"]
            failures = [item for item in attempts if item.get("result") == "failed"]
            self.assertEqual("private_verify:TestBoundary", failures[-1]["failure_signature"])

    @unittest.skipUnless(shutil.which("go"), "Go toolchain is required")
    def test_real_calibration_reaches_contract_and_ablation_rered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "2026-08-24" / "demo__001"
            buggy = project / "env"
            gold = root / "_gold" / project.name
            evaluator = project / "evaluator"
            for path in (buggy, gold, evaluator):
                path.mkdir(parents=True)
                (path / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
            for index in range(1, 5):
                (buggy / f"part{index}.go").write_text(
                    f"package demo\nfunc Part{index}() int {{ return 0 }}\n", encoding="utf-8"
                )
                (gold / f"part{index}.go").write_text(
                    f"package demo\nfunc Part{index}() int {{ return 1 }}\n", encoding="utf-8"
                )
            (evaluator / "target_test.go").write_text(
                'package demo\nimport "testing"\nfunc TestTotal(t *testing.T) {'
                ' if Part1()+Part2()+Part3()+Part4()!=4 { t.Fatalf("total mismatch: got partial result") } }\n',
                encoding="utf-8",
            )
            rows = [{"file": f"part{index}.go"} for index in range(1, 5)]
            (project / "difficulty_review.json").write_text(
                json.dumps({"repair_ablation_checks": rows}), encoding="utf-8"
            )
            (project / "contract_coverage.json").write_text(json.dumps({
                "contracts": [{"message": "total mismatch: got partial result"}]
            }), encoding="utf-8")
            verify = "go test . -run '^TestTotal$' -count=1"
            env = os.environ.copy()
            red = batch_preflight._run_calibration(buggy, evaluator, verify, "red", env, 2, 120)
            green = batch_preflight._run_calibration(gold, evaluator, verify, "green", env, 2, 120)
            self.assertTrue(red["passed"])
            self.assertTrue(green["passed"])
            self.assertTrue(batch_preflight._assertion_reached(red, project)["passed"])
            ablation = batch_preflight._run_ablation(project, gold, evaluator, verify, env, 120)
            self.assertTrue(ablation["passed"], ablation)


if __name__ == "__main__":
    unittest.main()
