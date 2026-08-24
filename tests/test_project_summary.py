import tempfile
import unittest
from pathlib import Path
import subprocess

import sys


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from github_project import ensure_delivery_files  # noqa: E402
from project_summary import validate_project_summary  # noqa: E402


class ProjectSummaryTest(unittest.TestCase):
    def test_workspace_generation_writes_summary_and_drops_bug_repro(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "batch"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n", encoding="utf-8")
            (source / "BUG_REPRO.md").write_text("legacy\n", encoding="utf-8")
            summary = "基于 Go 实现的设备状态 API 服务，提供状态写入与查询能力。"
            result = subprocess.run([
                sys.executable, str(SCRIPTS / "workspace.py"), "new-project",
                "--root", str(root), "--source", "local", "--repo", "device-state-service",
                "--local-path", str(source), "--count", "2", "--date", "2026-08-24",
                "--project-summary", summary,
            ], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            for record in ("001", "002"):
                project = root / "2026-08-24" / f"device-state-service__{record}"
                self.assertEqual(summary, (project / "project_summary.txt").read_text(encoding="utf-8").strip())
                self.assertFalse((project / "env" / "BUG_REPRO.md").exists())

    def test_summary_requires_go_and_project_type(self):
        valid = "基于 Go 实现的停车场管理 CLI 项目，一款命令行工具，完成车位录入与车辆进出登记。"
        self.assertEqual([], validate_project_summary(valid))
        self.assertTrue(validate_project_summary("停车场管理项目，完成车辆进出登记。"))
        self.assertTrue(validate_project_summary("基于 Go 实现的停车场项目，完成车辆进出登记。"))

    def test_delivery_readme_starts_with_summary_and_removes_bug_repro(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n", encoding="utf-8")
            (repo / "BUG_REPRO.md").write_text("legacy\n", encoding="utf-8")
            summary = "基于 Go 实现的设备状态 API 服务，提供状态写入与查询能力。"
            ensure_delivery_files(repo, "demo", summary)
            self.assertEqual(summary, (repo / "BENZHI_README.md").read_text(encoding="utf-8").splitlines()[0])
            self.assertFalse((repo / "BUG_REPRO.md").exists())


if __name__ == "__main__":
    unittest.main()
