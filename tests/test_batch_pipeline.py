import json
import os
import shutil
import sys
import tempfile
import unittest
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
        (project / "collection.json").write_text(json.dumps({
            "bug_id": "demo-001", "task_type": "bugfix", "bug_category": "error异常错误",
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
            with patch.object(batch_pipeline, "upload_one"), patch.object(
                batch_pipeline, "run_checked", side_effect=lambda cmd, **kwargs: calls.append(cmd) or ""
            ), patch.object(batch_pipeline, "mark"):
                args = type("Args", (), {
                    "upload_workers": 3, "timeout": 1, "workers": 3,
                    "date": None, "projects": None,
                })()
                batch_pipeline.finish(projects, root, args)
            sync_calls = [cmd for cmd in calls if "collection_table.py" in " ".join(cmd) and "sync" in cmd]
            registry_sync = [cmd for cmd in calls if "repo_registry.py" in " ".join(cmd) and "sync" in cmd]
            self.assertEqual(1, len(sync_calls))
            self.assertEqual(1, len(registry_sync))

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
