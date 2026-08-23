import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from contract_coverage import extract_contracts, initialize_manifest, validate_manifest  # noqa: E402


class ContractCoverageTest(unittest.TestCase):
    def test_every_assertion_requires_exact_four_way_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            evaluator = project / "evaluator"
            evaluator.mkdir()
            (evaluator / "target_test.go").write_text(
                'package x\nimport "testing"\nfunc f(check *testing.T) { check.Fatalf("pre-cancel must make zero calls: %d", 1) }\n',
                encoding="utf-8",
            )
            prompt = "When pre-cancelled, make zero calls and return cancellation."
            (project / "prompt.txt").write_text(prompt, encoding="utf-8")
            (project / "collection.json").write_text(json.dumps({
                "success_criteria": "pre-cancel makes zero calls",
            }), encoding="utf-8")
            (project / "difficulty_review.json").write_text(json.dumps({
                "reviewer_notes": "pre-cancel zero-call behavior is covered",
            }), encoding="utf-8")
            self.assertEqual(1, len(extract_contracts(evaluator)))
            manifest = initialize_manifest(project)
            data = json.loads(manifest.read_text(encoding="utf-8"))
            row = data["contracts"][0]
            row.update({
                "prompt_trigger_fragment": "pre-cancelled",
                "prompt_expected_fragment": "zero calls",
                "success_criteria_fragment": "zero calls",
                "difficulty_evidence_fragment": "zero-call behavior",
            })
            manifest.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual((True, []), validate_manifest(project))
            row["prompt_expected_fragment"] = "not present"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            ok, issues = validate_manifest(project)
            self.assertFalse(ok)
            self.assertTrue(any("prompt_expected_fragment" in issue for issue in issues))

    def test_new_evaluator_assertion_invalidates_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            evaluator = project / "evaluator"
            evaluator.mkdir()
            target = evaluator / "target_test.go"
            target.write_text('package x\nimport "testing"\nfunc f(t *testing.T) { t.Fatal("first contract") }\n', encoding="utf-8")
            (project / "prompt.txt").write_text("first trigger and first expected", encoding="utf-8")
            (project / "collection.json").write_text('{"success_criteria":"first expected"}', encoding="utf-8")
            (project / "difficulty_review.json").write_text('{"reviewer_notes":"first expected"}', encoding="utf-8")
            initialize_manifest(project)
            target.write_text(target.read_text() + 'func g(t *testing.T) { t.Error("second contract") }\n', encoding="utf-8")
            self.assertTrue(any("unmapped evaluator assertions" in issue for issue in validate_manifest(project)[1]))

    def test_dynamic_failure_message_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            evaluator = project / "evaluator"
            evaluator.mkdir()
            (evaluator / "target_test.go").write_text(
                'package x\nimport "testing"\nfunc f(t *testing.T, err error) { t.Fatal(err) }\n', encoding="utf-8"
            )
            for name, value in (
                ("collection.json", {}), ("difficulty_review.json", {}),
            ):
                (project / name).write_text(json.dumps(value), encoding="utf-8")
            (project / "prompt.txt").write_text("some prompt", encoding="utf-8")
            initialize_manifest(project)
            self.assertTrue(any("literal contract message" in issue for issue in validate_manifest(project)[1]))


if __name__ == "__main__":
    unittest.main()
