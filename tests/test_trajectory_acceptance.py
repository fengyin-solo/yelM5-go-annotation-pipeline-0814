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


if __name__ == "__main__":
    unittest.main()
