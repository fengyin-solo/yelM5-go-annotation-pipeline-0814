import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import github_project  # noqa: E402
from github_project import clear_worktree  # noqa: E402
from post_qc import repository_delivery_ok  # noqa: E402
from trajectory_guard import write_source_manifest  # noqa: E402


class RepositoryTopologyTest(unittest.TestCase):
    def run_git(self, repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        if check and result.returncode != 0:
            self.fail(result.stderr or result.stdout)
        return result

    def test_orphan_g1_g2_r1_passes_repository_qc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "_repos" / "demo"
            project = root / "2026-08-23" / "demo__001"
            remote = root / "remote.git"
            repo.mkdir(parents=True)
            project.mkdir(parents=True)
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            self.run_git(repo, "init")
            self.run_git(repo, "config", "user.name", "QC Test")
            self.run_git(repo, "config", "user.email", "qc@example.com")
            self.run_git(repo, "remote", "add", "origin", str(remote))

            self.run_git(repo, "checkout", "--orphan", "bug001_green")
            (repo / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n", encoding="utf-8")
            (repo / "service.go").write_text("package demo\n\nfunc Value() int { return 1 }\n", encoding="utf-8")
            summary = "基于 Go 实现的设备状态服务，提供设备状态写入与查询 API。"
            (project / "project_summary.txt").write_text(summary + "\n", encoding="utf-8")
            (repo / "BENZHI_README.md").write_text(summary + "\n\n# demo\n", encoding="utf-8")
            self.run_git(repo, "add", ".")
            self.run_git(repo, "commit", "-m", "G1")
            g1 = self.run_git(repo, "rev-parse", "HEAD").stdout.strip()
            manifest = project / "_delivery" / "g1_snapshot.json"
            write_source_manifest(repo, manifest, commit=g1, branch="bug001_green")

            (repo / "service.go").write_text(
                "package demo\n\nfunc Value() int {\n\tvalue := 1\n\tvalue++\n\treturn value\n}\n",
                encoding="utf-8",
            )
            test_text = "package demo\n\nimport \"testing\"\n\nfunc TestValue(t *testing.T) {}\n"
            (repo / "service_test.go").write_text(test_text, encoding="utf-8")
            self.run_git(repo, "add", ".")
            self.run_git(repo, "commit", "-m", "G2")
            g2 = self.run_git(repo, "rev-parse", "HEAD").stdout.strip()

            self.run_git(repo, "checkout", "--orphan", "bug001_red")
            clear_worktree(repo)
            self.run_git(repo, "checkout", g1, "--", ".")
            (repo / "service_test.go").write_text(test_text, encoding="utf-8")
            self.run_git(repo, "add", ".")
            self.run_git(repo, "commit", "-m", "R1")
            r1 = self.run_git(repo, "rev-parse", "HEAD").stdout.strip()
            self.run_git(repo, "push", "origin", "bug001_green", "bug001_red")

            evidence = project / "_evidence"
            evidence.mkdir()
            completed = datetime.now(timezone.utc) - timedelta(seconds=1)
            (evidence / "trajectory_guard.json").write_text(json.dumps({
                "session_id": "sid",
                "completed_at": completed.isoformat(),
            }), encoding="utf-8")
            (evidence / "repository_delivery.json").write_text(json.dumps({
                "state": "finalized",
                "repo_url": "https://github.com/example/demo/tree/bug001_green",
                "green_branch": "bug001_green",
                "red_branch": "bug001_red",
                "g1_commit": g1,
                "g2_commit": g2,
                "r1_commit": r1,
                "session_id": "sid",
                "finalized_at": datetime.now(timezone.utc).isoformat(),
            }), encoding="utf-8")
            collection = {
                "repo_url": "https://github.com/example/demo/tree/bug001_green",
                "session_id": "sid",
            }

            ok, message = repository_delivery_ok(project, collection, "bugfix")
            self.assertTrue(ok, message)
            self.assertNotEqual(0, self.run_git(repo, "merge-base", "bug001_green", "bug001_red", check=False).returncode)

            self.run_git(repo, "checkout", "bug001_green")
            (repo / "service.go").write_text(
                "package demo\n\nfunc Value() int { return 2 }\n", encoding="utf-8"
            )
            self.run_git(repo, "add", "service.go")
            self.run_git(repo, "commit", "--amend", "-m", "G2 too small")
            small_g2 = self.run_git(repo, "rev-parse", "HEAD").stdout.strip()
            self.run_git(repo, "push", "--force", "origin", "bug001_green")
            metadata = json.loads((evidence / "repository_delivery.json").read_text(encoding="utf-8"))
            metadata["g2_commit"] = small_g2
            (evidence / "repository_delivery.json").write_text(json.dumps(metadata), encoding="utf-8")
            ok, message = repository_delivery_ok(project, collection, "bugfix")
            self.assertFalse(ok)
            self.assertIn("模型最终补丁只有 1 个功能 Go 文件、2 行增删", message)

    def test_diagnosis_single_red_passes_repository_qc_without_bug_repro(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "_repos" / "demo"
            project = root / "2026-08-23" / "demo__001"
            evaluator = project / "evaluator"
            remote = root / "remote.git"
            repo.mkdir(parents=True)
            evaluator.mkdir(parents=True)
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            self.run_git(repo, "init")
            self.run_git(repo, "config", "user.name", "QC Test")
            self.run_git(repo, "config", "user.email", "qc@example.com")
            self.run_git(repo, "remote", "add", "origin", str(remote))

            summary = "基于 Go 实现的设备状态诊断 CLI 工具，定位设备状态异常与事件链路。"
            (project / "project_summary.txt").write_text(summary + "\n", encoding="utf-8")
            self.run_git(repo, "checkout", "--orphan", "bug001_red")
            (repo / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n", encoding="utf-8")
            (repo / "service.go").write_text("package demo\n\nfunc Value() int { return 1 }\n", encoding="utf-8")
            (repo / "BENZHI_README.md").write_text(summary + "\n\n# demo\n", encoding="utf-8")
            test_text = "package demo\n\nimport \"testing\"\n\nfunc TestValue(t *testing.T) {}\n"
            (repo / "target_test.go").write_text(test_text, encoding="utf-8")
            (evaluator / "target_test.go").write_text(test_text, encoding="utf-8")
            self.run_git(repo, "add", ".")
            self.run_git(repo, "commit", "-m", "G1")
            g1 = self.run_git(repo, "rev-parse", "HEAD").stdout.strip()
            manifest = project / "_delivery" / "g1_snapshot.json"
            write_source_manifest(repo, manifest, commit=g1, branch="bug001_red")
            self.run_git(repo, "push", "origin", "bug001_red")

            evidence = project / "_evidence"
            evidence.mkdir()
            completed = datetime.now(timezone.utc) - timedelta(seconds=1)
            (evidence / "trajectory_guard.json").write_text(json.dumps({
                "session_id": "sid", "completed_at": completed.isoformat(),
            }), encoding="utf-8")
            (evidence / "repository_delivery.json").write_text(json.dumps({
                "state": "finalized",
                "task_type": "diagnosis",
                "repo_url": "https://github.com/example/demo/tree/bug001_red",
                "green_branch": "",
                "red_branch": "bug001_red",
                "g1_commit": g1,
                "r1_commit": g1,
                "session_id": "sid",
                "test_files": ["target_test.go"],
                "finalized_at": datetime.now(timezone.utc).isoformat(),
            }), encoding="utf-8")
            collection = {
                "repo_url": "https://github.com/example/demo/tree/bug001_red",
                "session_id": "sid",
            }

            ok, message = repository_delivery_ok(project, collection, "diagnosis")
            self.assertTrue(ok, message)
            self.assertNotEqual(0, self.run_git(repo, "rev-parse", "--verify", "bug001_green", check=False).returncode)
            tree = self.run_git(repo, "ls-tree", "-r", "--name-only", g1).stdout.splitlines()
            self.assertFalse(any(Path(name).name.lower() == "bug_repro.md" for name in tree))
            self.assertEqual(summary, self.run_git(repo, "show", f"{g1}:BENZHI_README.md").stdout.splitlines()[0])

            self.run_git(repo, "rm", "target_test.go")
            self.run_git(repo, "commit", "--amend", "-m", "red without tests")
            no_test_sha = self.run_git(repo, "rev-parse", "HEAD").stdout.strip()
            self.run_git(repo, "push", "--force", "origin", "bug001_red")
            write_source_manifest(repo, manifest, commit=no_test_sha, branch="bug001_red")
            metadata = json.loads((evidence / "repository_delivery.json").read_text(encoding="utf-8"))
            metadata["g1_commit"] = no_test_sha
            metadata["r1_commit"] = no_test_sha
            (evidence / "repository_delivery.json").write_text(json.dumps(metadata), encoding="utf-8")
            ok, message = repository_delivery_ok(project, collection, "diagnosis")
            self.assertFalse(ok)
            self.assertIn("diagnosis red 没有验收测试", message)

    def test_diagnosis_finalize_adds_tests_then_first_pushes_single_red_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "_repos" / "demo"
            project = root / "2026-08-23" / "demo__010"
            env = project / "env"
            evaluator = project / "evaluator"
            remote = root / "remote.git"
            env.mkdir(parents=True)
            evaluator.mkdir()
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            self.run_git(repo, "init")
            self.run_git(repo, "config", "user.name", "QC Test")
            self.run_git(repo, "config", "user.email", "qc@example.com")
            self.run_git(repo, "remote", "add", "origin", str(remote))
            (env / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n", encoding="utf-8")
            (env / "service.go").write_text("package demo\n\nfunc Value() int { return 1 }\n", encoding="utf-8")
            (evaluator / "target_test.go").write_text(
                "package demo\n\nimport \"testing\"\n\nfunc TestValue(t *testing.T) {}\n", encoding="utf-8"
            )
            (project / "project_summary.txt").write_text(
                "基于 Go 实现的设备诊断 CLI，定位设备状态异常。\n", encoding="utf-8"
            )
            (project / "collection.json").write_text(json.dumps({
                "bug_id": "demo-010", "task_type": "diagnosis",
                "verify_cmds": "go test ./internal/demo -run '^TestValue$' -count=1",
            }), encoding="utf-8")
            context = {
                "github": {"username": "local", "token": "unused"},
                "gitAuthor": {"name": "QC Test", "email": "qc@example.com"},
            }
            args = SimpleNamespace(
                root=str(root), repo_name="demo", project="demo__010", date="2026-08-23",
                bug_id="demo-010", module_path=None,
            )
            with patch.object(github_project, "load_context", return_value=context):
                github_project.cmd_publish(args)

            self.assertEqual("1", self.run_git(repo, "rev-list", "--count", "bug010_red").stdout.strip())
            self.assertNotEqual(0, self.run_git(repo, "rev-parse", "--verify", "bug010_green", check=False).returncode)
            prepared_files = self.run_git(repo, "ls-tree", "-r", "--name-only", "bug010_red").stdout.splitlines()
            self.assertFalse(any(github_project.is_test_artifact(name) for name in prepared_files))
            self.assertEqual("", self.run_git(repo, "ls-remote", "--heads", "origin", "bug010_red").stdout.strip())
            metadata = json.loads((project / "_evidence" / "repository_delivery.json").read_text(encoding="utf-8"))
            self.assertEqual("g1_prepared", metadata["state"])
            self.assertEqual("", metadata["repo_url"])
            self.assertEqual("", metadata["green_branch"])
            self.assertEqual("bug010_red", metadata["red_branch"])

            collection = json.loads((project / "collection.json").read_text(encoding="utf-8"))
            collection["session_id"] = "sid"
            (project / "collection.json").write_text(json.dumps(collection), encoding="utf-8")
            (project / "sid.jsonl").write_text("{}\n", encoding="utf-8")
            evidence = project / "_evidence"
            completed = datetime.now(timezone.utc) - timedelta(seconds=1)
            (evidence / "trajectory_guard.json").write_text(json.dumps({
                "result": "passed", "classification": "clean", "tests_visible": False,
                "session_id": "sid", "completed_at": completed.isoformat(),
            }), encoding="utf-8")
            (evidence / "trajectory_acceptance.json").write_text(json.dumps({
                "result": "passed", "session_id": "sid", "checks": {
                    name: {"passed": True} for name in (
                        "trajectory_analysis", "regression", "task_semantics", "diagnosis_root_cause"
                    )
                },
            }), encoding="utf-8")
            with patch.object(github_project, "load_context", return_value=context), patch.object(
                github_project, "_write_delivery_metadata", side_effect=RuntimeError("simulated interruption")
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    github_project.cmd_finalize(args)
            self.assertTrue(self.run_git(repo, "ls-remote", "--heads", "origin", "bug010_red").stdout.strip())
            with patch.object(github_project, "load_context", return_value=context):
                github_project.cmd_finalize(args)

            self.assertEqual("1", self.run_git(repo, "rev-list", "--count", "bug010_red").stdout.strip())
            final_files = self.run_git(repo, "ls-tree", "-r", "--name-only", "bug010_red").stdout.splitlines()
            self.assertIn("target_test.go", final_files)
            self.assertTrue(self.run_git(repo, "ls-remote", "--heads", "origin", "bug010_red").stdout.strip())
            metadata = json.loads((evidence / "repository_delivery.json").read_text(encoding="utf-8"))
            self.assertEqual("finalized", metadata["state"])
            self.assertTrue(metadata["repo_url"].endswith("/tree/bug010_red"), metadata["repo_url"])
            self.assertEqual(["target_test.go"], metadata["test_files"])

    def test_publish_and_finalize_commands_use_orphan_topology(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "_repos" / "demo"
            project = root / "2026-08-23" / "demo__001"
            env = project / "env"
            evaluator = project / "evaluator"
            remote = root / "remote.git"
            env.mkdir(parents=True)
            evaluator.mkdir()
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            self.run_git(repo, "init")
            self.run_git(repo, "config", "user.name", "QC Test")
            self.run_git(repo, "config", "user.email", "qc@example.com")
            self.run_git(repo, "remote", "add", "origin", str(remote))
            (env / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n", encoding="utf-8")
            (env / "service.go").write_text("package demo\n\nfunc Value() int { return 1 }\n", encoding="utf-8")
            (env / "legacy_test.go").write_text("package demo\n", encoding="utf-8")
            (env / "tests").mkdir()
            (env / "tests" / "fixture.json").write_text("{}\n", encoding="utf-8")
            test_text = "package demo\n\nimport \"testing\"\n\nfunc TestValue(t *testing.T) {}\n"
            (evaluator / "target_test.go").write_text(test_text, encoding="utf-8")
            summary = "基于 Go 实现的设备状态服务，提供设备状态写入与查询 API。"
            (project / "project_summary.txt").write_text(summary + "\n", encoding="utf-8")
            collection = {
                "bug_id": "bug-demo-001",
                "task_type": "bugfix",
                "verify_cmds": "go test . -run '^TestValue$' -count=1",
                "session_id": "sid",
            }
            (project / "collection.json").write_text(json.dumps(collection), encoding="utf-8")
            context = {
                "github": {"username": "local", "token": "unused"},
                "gitAuthor": {"name": "QC Test", "email": "qc@example.com"},
            }
            args = SimpleNamespace(
                root=str(root), repo_name="demo", project="demo__001", date="2026-08-23",
                bug_id="bug-demo-001", module_path=None,
            )
            with patch.object(github_project, "load_context", return_value=context):
                github_project.cmd_publish(args)

            g1 = self.run_git(repo, "rev-parse", "bug001_green").stdout.strip()
            g1_files = self.run_git(repo, "ls-tree", "-r", "--name-only", g1).stdout.splitlines()
            self.assertFalse(any("test" in Path(name).name.lower() or "tests" in Path(name).parts for name in g1_files))
            self.assertNotIn("BUG_REPRO.md", g1_files)
            readme = self.run_git(repo, "show", f"{g1}:BENZHI_README.md").stdout
            self.assertEqual(summary, readme.splitlines()[0])
            self.assertTrue((project / "_delivery" / "g1_snapshot.json").exists())

            (env / "service.go").write_text("package demo\n\nfunc Value() int { return 2 }\n", encoding="utf-8")
            evidence = project / "_evidence"
            evidence.mkdir(exist_ok=True)
            (evidence / "trajectory_guard.json").write_text(json.dumps({
                "session_id": "sid", "result": "passed", "classification": "clean",
                "tests_visible": False, "completed_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            }), encoding="utf-8")
            (evidence / "verify_green.jsonl").write_text("{}\n", encoding="utf-8")
            (evidence / "verify_result.json").write_text("{}\n", encoding="utf-8")
            (evidence / "green_regression.json").write_text('{"result":"passed"}\n', encoding="utf-8")
            (evidence / "trajectory_acceptance.json").write_text(json.dumps({
                "result": "passed", "session_id": "sid", "checks": {
                    name: {"passed": True} for name in (
                        "trajectory_analysis", "regression", "task_semantics", "private_verify"
                    )
                },
            }), encoding="utf-8")
            (project / "sid.jsonl").write_text("{}\n", encoding="utf-8")
            with patch.object(github_project, "load_context", return_value=context):
                with self.assertRaisesRegex(RuntimeError, "模型最终补丁只有 1 个功能 Go 文件、2 行增删"):
                    github_project.cmd_finalize(args)

            (env / "service.go").write_text(
                "package demo\n\nfunc Value() int {\n\tvalue := 1\n\tvalue++\n\treturn value\n}\n",
                encoding="utf-8",
            )
            with patch.object(github_project, "load_context", return_value=context):
                github_project.cmd_finalize(args)

            self.assertEqual("2", self.run_git(repo, "rev-list", "--count", "bug001_green").stdout.strip())
            self.assertEqual("1", self.run_git(repo, "rev-list", "--count", "bug001_red").stdout.strip())
            self.assertNotEqual(0, self.run_git(repo, "merge-base", "bug001_green", "bug001_red", check=False).returncode)


if __name__ == "__main__":
    unittest.main()
