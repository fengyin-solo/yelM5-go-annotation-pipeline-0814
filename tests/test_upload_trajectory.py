import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import upload_trajectory  # noqa: E402


class UploadTrajectoryTest(unittest.TestCase):
    def test_expired_cookie_refreshes_and_retries_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trajectory.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            success = {"url": "https://cos.example/trajectory.jsonl"}
            with patch.object(
                upload_trajectory,
                "_upload_file_once",
                side_effect=[upload_trajectory.CookieExpiredError("expired"), success],
            ) as upload_once, patch.object(
                upload_trajectory, "refresh_cookie", return_value="fresh-sid"
            ):
                result = upload_trajectory.upload_file(path, path.name, "stale-sid")
            self.assertEqual(success, result)
            self.assertEqual("stale-sid", upload_once.call_args_list[0].args[2])
            self.assertEqual("fresh-sid", upload_once.call_args_list[1].args[2])


if __name__ == "__main__":
    unittest.main()
