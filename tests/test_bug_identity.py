import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from bug_identity import bug_id_for_project  # noqa: E402


class BugIdentityTest(unittest.TestCase):
    def test_project_directory_name_and_record_form_bug_id(self):
        self.assertEqual(
            "16-exam-system【10】-001",
            bug_id_for_project("16-exam-system【10】__001"),
        )
        self.assertEqual(
            "16-exam-system【10】-002",
            bug_id_for_project("16-exam-system【10】__002"),
        )

    def test_collection_new_uses_canonical_bug_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "2026-08-26" / "16-exam-system【10】__001"
            project.mkdir(parents=True)
            (project / "status.json").write_text("{}\n", encoding="utf-8")
            result = subprocess.run([
                sys.executable, str(SCRIPTS / "collection_table.py"), "new",
                "--root", str(root), "--date", "2026-08-26",
                "--project", project.name,
            ], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr or result.stdout)
            data = json.loads((project / "collection.json").read_text(encoding="utf-8"))
            self.assertEqual("16-exam-system【10】-001", data["bug_id"])

    def test_workspace_preserves_directory_marker_and_initializes_bug_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "batch"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "go.mod").write_text("module example.com/exam\n\ngo 1.22\n", encoding="utf-8")
            result = subprocess.run([
                sys.executable, str(SCRIPTS / "workspace.py"), "new-project",
                "--root", str(root), "--source", "local",
                "--repo", "16-exam-system【10】", "--local-path", str(source),
                "--record", "001", "--date", "2026-08-26",
                "--project-summary", "基于 Go 实现的考试管理 API 服务，提供试卷录入与成绩查询。",
            ], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr or result.stdout)
            project = root / "2026-08-26" / "16-exam-system【10】__001"
            status = json.loads((project / "status.json").read_text(encoding="utf-8"))
            self.assertEqual("16-exam-system【10】-001", status["bug_id"])
