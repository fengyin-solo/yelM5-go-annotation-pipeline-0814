import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_prompt_duplicates.py"


class PromptDuplicateTest(unittest.TestCase):
    def test_duplicates_warn_by_default_and_fail_in_strict_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repeated = "设备断线重连后状态没有恢复而且后续读取一直返回旧数据"
            for index in (1, 2):
                record = root / "2026-08-24" / f"demo__{index:03d}"
                record.mkdir(parents=True)
                (record / "collection.json").write_text(json.dumps({
                    "user_query": repeated + f"，请帮我修好第{index}个入口。",
                    "success_criteria": repeated + f"，入口{index}恢复正常。",
                    "verify_cmds": f"go test ./internal/device -run '^TestReconnect{index}$' -count=1",
                }, ensure_ascii=False), encoding="utf-8")

            normal = subprocess.run(
                [sys.executable, str(SCRIPT), "--root", str(root)],
                capture_output=True, text=True,
            )
            self.assertEqual(0, normal.returncode, normal.stdout + normal.stderr)
            self.assertIn("相似度提示", normal.stdout)

            strict = subprocess.run(
                [sys.executable, str(SCRIPT), "--root", str(root), "--strict-duplicates"],
                capture_output=True, text=True,
            )
            self.assertEqual(1, strict.returncode, strict.stdout + strict.stderr)
            self.assertIn("[硬红]", strict.stdout)


if __name__ == "__main__":
    unittest.main()
