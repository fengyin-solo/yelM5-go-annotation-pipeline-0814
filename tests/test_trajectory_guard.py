import json
import tempfile
import unittest
from pathlib import Path

import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from trajectory_guard import (  # noqa: E402
    copy_evaluator_to_repo,
    copy_without_tests,
    private_test_issues,
    source_manifest_issues,
    sync_business_back,
    test_manifest,
    trajectory_policy_report,
    trajectory_policy_issues,
    write_source_manifest,
)


class TrajectoryGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = self.root / "env"
        self.evaluator = self.root / "evaluator"
        (self.env / "internal" / "flow").mkdir(parents=True)
        (self.evaluator / "internal" / "flow").mkdir(parents=True)
        (self.env / "go.mod").write_text("module demo\n\ngo 1.22\n", encoding="utf-8")
        (self.env / "internal" / "flow" / "service.go").write_text(
            "package flow\nfunc Pay() {}\n", encoding="utf-8"
        )
        (self.env / "internal" / "flow" / "existing_test.go").write_text(
            "package flow\nfunc TestExisting() {}\n", encoding="utf-8"
        )
        (self.evaluator / "internal" / "flow" / "target_test.go").write_text(
            "package flow\nfunc TestHiddenRetry() {}\n", encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_isolated_copy_and_business_sync_preserve_tests(self):
        isolated = self.root / "isolated"
        copy_without_tests(self.env, isolated)
        self.assertFalse(any(isolated.rglob("*_test.go")))
        (isolated / "internal" / "flow" / "service.go").write_text(
            "package flow\nfunc Pay() { println(1) }\n", encoding="utf-8"
        )
        before = test_manifest(self.env)
        sync_business_back(isolated, self.env)
        self.assertEqual(before, test_manifest(self.env))
        self.assertIn("println", (self.env / "internal" / "flow" / "service.go").read_text())

    def test_private_target_must_not_exist_in_env(self):
        command = "go test ./internal/flow -run '^TestHiddenRetry$' -count=1"
        self.assertEqual([], private_test_issues(self.env, self.evaluator, command))
        target = self.env / "internal" / "flow" / "target_test.go"
        target.write_text("package flow\nfunc TestHiddenRetry() {}\n", encoding="utf-8")
        self.assertTrue(private_test_issues(self.env, self.evaluator, command))

    def test_trajectory_audit_rejects_test_and_outside_access(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        trajectory = self.root / "bad.jsonl"
        events = [
            {"type": "assistant", "message": {"content": [{
                "type": "tool_use", "name": "Read", "input": {
                    "file_path": str(workspace / "x_test.go")
                }
            }]}},
            {"type": "assistant", "message": {"content": [{
                "type": "tool_use", "name": "Read", "input": {
                    "file_path": str(self.root / "evaluator" / "secret.go")
                }
            }]}},
        ]
        trajectory.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
        issues = trajectory_policy_issues(trajectory, workspace)
        self.assertTrue(any("_test.go" in issue for issue in issues))
        self.assertTrue(any("工作区外" in issue or "禁止目录" in issue for issue in issues))

    def test_trajectory_audit_accepts_business_files_inside_workspace(self):
        workspace = self.root / "workspace"
        source = workspace / "internal" / "flow" / "service.go"
        source.parent.mkdir(parents=True)
        source.write_text("package flow\n", encoding="utf-8")
        trajectory = self.root / "good.jsonl"
        event = {"type": "assistant", "message": {"content": [{
            "type": "tool_use", "name": "Read", "input": {"file_path": str(source)}
        }]}}
        trajectory.write_text(json.dumps(event) + "\n", encoding="utf-8")
        self.assertEqual([], trajectory_policy_issues(trajectory, workspace))

    def test_model_created_test_is_clean(self):
        workspace = self.root / "work"
        workspace.mkdir()
        test_path = workspace / "repro_test.go"
        trajectory = self.root / "own-test.jsonl"
        trajectory.write_text("\n".join([
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": str(test_path), "content": "package x"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": str(test_path)}}]}}),
        ]), encoding="utf-8")
        self.assertEqual("clean", trajectory_policy_report(trajectory, workspace)["classification"])

    def test_test_glob_before_business_read_is_suspect(self):
        workspace = self.root / "work"
        workspace.mkdir()
        source = workspace / "service.go"
        trajectory = self.root / "suspect.jsonl"
        trajectory.write_text("\n".join([
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Glob", "input": {"pattern": "**/*_test.go"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": str(source), "old_string": "a", "new_string": "b"}}]}}),
        ]), encoding="utf-8")
        self.assertEqual("suspect", trajectory_policy_report(trajectory, workspace)["classification"])

    def test_source_manifest_detects_change(self):
        visible = self.root / "visible"
        visible.mkdir()
        (visible / "main.go").write_text("package main\n", encoding="utf-8")
        (visible / "hidden_test.go").write_text("package main\n", encoding="utf-8")
        manifest = self.root / "g1.json"
        write_source_manifest(visible, manifest, commit="abc", branch="bug001_green")
        self.assertEqual([], source_manifest_issues(visible, manifest))
        (visible / "main.go").write_text("package changed\n", encoding="utf-8")
        self.assertTrue(source_manifest_issues(visible, manifest))

    def test_evaluator_is_committed_by_relative_path(self):
        repo = self.root / "repo"
        repo.mkdir()
        copied = copy_evaluator_to_repo(self.evaluator, repo)
        self.assertEqual(["internal/flow/target_test.go"], copied)
        self.assertTrue((repo / "internal" / "flow" / "target_test.go").exists())

    def test_hook_blocked_outside_attempt_is_not_successful_cheat(self):
        workspace = self.root / "work"
        workspace.mkdir()
        trajectory = self.root / "blocked.jsonl"
        trajectory.write_text("\n".join([
            json.dumps({"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": "call-1", "name": "Read",
                "input": {"file_path": "/tmp/private.go"},
            }]}}),
            json.dumps({"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "call-1",
                "content": "[workspace-guard blocked] workspace-external path",
            }]}}),
        ]), encoding="utf-8")
        report = trajectory_policy_report(trajectory, workspace)
        self.assertEqual("clean", report["classification"])
        self.assertEqual(1, len(report["blocked_attempts"]))

    def test_bash_real_external_path_remains_cheat(self):
        workspace = self.root / "work"
        workspace.mkdir()
        trajectory = self.root / "outside.jsonl"
        trajectory.write_text(json.dumps({"type": "assistant", "message": {"content": [{
            "type": "tool_use", "id": "call-1", "name": "Bash",
            "input": {"command": "sed -n '1,5p' /tmp/private.go"},
        }]}}) + "\n", encoding="utf-8")
        self.assertEqual("cheat", trajectory_policy_report(trajectory, workspace)["classification"])


if __name__ == "__main__":
    unittest.main()
