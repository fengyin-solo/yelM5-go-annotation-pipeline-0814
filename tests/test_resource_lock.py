import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _hold_resource(path: str, hold_seconds: float, queue) -> None:
    from resource_lock import resource_lock

    with resource_lock(Path(path), timeout=3, label="test"):
        queue.put(time.monotonic())
        time.sleep(hold_seconds)


class ResourceLockTest(unittest.TestCase):
    def test_same_resource_serializes_but_different_resources_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = multiprocessing.get_context("spawn")
            queue = ctx.Queue()
            same = str(Path(tmp) / "same.lock")
            first = ctx.Process(target=_hold_resource, args=(same, 0.35, queue))
            second = ctx.Process(target=_hold_resource, args=(same, 0.0, queue))
            first.start()
            first_entered = queue.get(timeout=3)
            second.start()
            second_entered = queue.get(timeout=3)
            first.join(timeout=3)
            second.join(timeout=3)
            self.assertGreater(second_entered - first_entered, 0.25)

            queue = ctx.Queue()
            started = time.monotonic()
            processes = [
                ctx.Process(target=_hold_resource, args=(str(Path(tmp) / f"{index}.lock"), 0.2, queue))
                for index in range(2)
            ]
            for process in processes:
                process.start()
            entered = [queue.get(timeout=3) for _ in processes]
            for process in processes:
                process.join(timeout=3)
                self.assertEqual(0, process.exitcode)
            self.assertLess(max(entered) - started, 0.3)

    def test_concurrent_registry_updates_do_not_lose_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "used-repositories.json"
            env = os.environ.copy()
            env["GO_ANNOTATION_REGISTRY"] = str(registry)
            base = [
                sys.executable, str(SCRIPTS / "repo_registry.py"), "register",
                "https://github.com/example/demo", "--source", "github",
                "--github-url", "https://github.com/example/demo",
            ]
            processes = [
                subprocess.Popen(base + ["--project", f"demo__00{index}"], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for index in (1, 2)
            ]
            outputs = [process.communicate(timeout=5) for process in processes]
            self.assertEqual([0, 0], [process.returncode for process in processes], outputs)
            data = json.loads(registry.read_text(encoding="utf-8"))
            entry = data["repositories"][0]
            self.assertEqual(2, entry["uses"])
            self.assertEqual(["demo__001", "demo__002"], entry["projects"])


if __name__ == "__main__":
    unittest.main()
