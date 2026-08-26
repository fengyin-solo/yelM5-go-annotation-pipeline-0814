import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import platform_submit  # noqa: E402


class FakeClient:
    submitted = []
    login_count = 0

    def __init__(self, base_url, username, password):
        self.base_url = base_url
        self.username = username
        self.password = password

    def login(self):
        type(self).login_count += 1

    def submit(self, data):
        type(self).submitted.append((data["bug_id"], data["session_id"]))
        return {"id": f"submission-{len(type(self).submitted)}", "status": "pending"}


class UncertainClient(FakeClient):
    def submit(self, data):
        raise platform_submit.PlatformSubmitError("connection closed after send", uncertain=True)


class PlatformSubmitTest(unittest.TestCase):
    def setUp(self):
        FakeClient.submitted = []
        FakeClient.login_count = 0
        UncertainClient.submitted = []
        UncertainClient.login_count = 0

    def row(self, number: int, *, valid=True):
        session_id = f"00000000-0000-4000-8000-{number:012d}"
        return {
            "sample_id": str(number),
            "session id": session_id,
            "bug_id": f"batch-{number:03d}",
            "task_type": "diagnosis",
            "bug_category": "context相关问题",
            "repo_url": f"https://github.com/example/repo/tree/bug{number:03d}_red",
            "go_version": "golang:1.24; go 1.24; GOTOOLCHAIN=auto",
            "repro_determinism": "deterministic",
            "user_query": "当前项目的请求取消后仍会处理后续任务，先别改代码，帮我查清楚。",
            "trajectory": f"https://cos.example/{number}.jsonl",
            "verify_cmds": "go test ./internal/demo -run '^TestCancellation$' -count=1",
            "gold_root_cause": "文件: worker.go 符号: Run 机制: context 取消信号未传到队列消费循环",
            "success_criteria": "请求取消后的后续任务在问题存在时稳定继续执行，需定位取消信号与消费循环的传递链。",
            "verify_result": json.dumps({
                "pre_fix": {"trajectory_url": f"https://cos.example/red-{number}", "session_id": session_id, "result": "red"}
            }),
            "harness": "Claude Code CLI v2.1.233",
            "generator_model": "model_hub/glm-52-coding",
        } | ({} if valid else {"trajectory": ""})

    def workbook(self, path: Path, rows: list[dict]):
        workbook = Workbook()
        sheet = workbook.active
        headers = list(rows[0])
        sheet.append(headers)
        for row in rows:
            sheet.append([row.get(header, "") for header in headers])
        workbook.save(path)

    def args(self, root: Path, workbook: Path, **overrides):
        values = {
            "xlsx": str(workbook),
            "record": None,
            "username": "user",
            "password": "password",
            "base_url": "https://platform.example",
            "ledger": str(root / "platform-submissions.json"),
            "result": str(root / "platform-submit-result.json"),
            "dry_run": False,
            "retry_uncertain": False,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_prevalidates_every_row_before_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "collection.xlsx"
            self.workbook(workbook, [self.row(1), self.row(2, valid=False)])
            with patch.object(platform_submit, "PlatformClient") as client:
                with self.assertRaisesRegex(platform_submit.PlatformSubmitError, "workbook validation failed"):
                    platform_submit.submit_selected(self.args(root, workbook))
            client.assert_not_called()

    def test_filters_to_requested_records_and_skips_them_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "collection.xlsx"
            rows = [self.row(1), self.row(2)]
            self.workbook(workbook, rows)
            selected = [[rows[1]["bug_id"], rows[1]["session id"]]]
            args = self.args(root, workbook, record=selected)
            with patch.object(platform_submit, "PlatformClient", FakeClient):
                first = platform_submit.submit_selected(args)
                second = platform_submit.submit_selected(args)
            self.assertEqual([(rows[1]["bug_id"], rows[1]["session id"])], FakeClient.submitted)
            self.assertEqual(1, first["submitted"])
            self.assertEqual(1, second["skipped"])
            ledger = json.loads(Path(args.ledger).read_text(encoding="utf-8"))
            entry = next(iter(ledger["submissions"].values()))
            self.assertEqual("submitted", entry["state"])
            self.assertEqual("submission-1", entry["submission_id"])

    def test_completed_run_ends_with_fixed_chinese_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "collection.xlsx"
            self.workbook(workbook, [self.row(1)])
            output = io.StringIO()
            with patch.object(platform_submit, "PlatformClient", FakeClient), redirect_stdout(output):
                platform_submit.submit_selected(self.args(root, workbook))
            expected = (
                "平台上传摘要：\n"
                "上传成功：1 条\n"
                "跳过：0 条\n"
                "提交 ID：\n"
                "- batch-001：submission-1"
            )
            self.assertTrue(output.getvalue().rstrip().endswith(expected))

    def test_uncertain_request_is_not_retried_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "collection.xlsx"
            self.workbook(workbook, [self.row(1)])
            args = self.args(root, workbook)
            with patch.object(platform_submit, "PlatformClient", UncertainClient):
                with self.assertRaisesRegex(platform_submit.PlatformSubmitError, "incomplete"):
                    platform_submit.submit_selected(args)
            ledger = json.loads(Path(args.ledger).read_text(encoding="utf-8"))
            self.assertEqual("uncertain", next(iter(ledger["submissions"].values()))["state"])
            with patch.object(platform_submit, "PlatformClient") as client:
                with self.assertRaisesRegex(platform_submit.PlatformSubmitError, "ambiguous prior state"):
                    platform_submit.submit_selected(args)
            client.assert_not_called()

    def test_payload_change_after_submission_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = root / "collection.xlsx"
            row = self.row(1)
            self.workbook(workbook, [row])
            args = self.args(root, workbook)
            with patch.object(platform_submit, "PlatformClient", FakeClient):
                platform_submit.submit_selected(args)
            row["success_criteria"] += "已修改"
            self.workbook(workbook, [row])
            with self.assertRaisesRegex(platform_submit.PlatformSubmitError, "already-submitted record changed"):
                platform_submit.submit_selected(args)


if __name__ == "__main__":
    unittest.main()
