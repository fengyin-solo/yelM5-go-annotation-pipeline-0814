import multiprocessing
import inspect
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _hold_slot(lock_dir: str, slots: int, hold_seconds: float, queue) -> None:
    import serial_lock

    serial_lock.LOCK_DIR = Path(lock_dir)
    serial_lock.LOCK_FILE = serial_lock.LOCK_DIR / "test_model.lock"
    serial_lock.SLOT_DIR = serial_lock.LOCK_DIR / "model-slots"
    with serial_lock.test_model_lock(timeout=3, slots=slots):
        queue.put(("entered", time.monotonic()))
        time.sleep(hold_seconds)


class SerialLockTest(unittest.TestCase):
    def test_accepts_eight_and_rejects_more_than_eight_slots(self):
        import serial_lock

        self.assertEqual(8, inspect.signature(serial_lock.test_model_lock).parameters["slots"].default)
        with serial_lock.test_model_lock(slots=8):
            pass
        with self.assertRaisesRegex(ValueError, "只能是 1-8"):
            with serial_lock.test_model_lock(slots=9):
                pass

    def test_two_slots_admit_two_processes_and_delay_the_third(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = multiprocessing.get_context("spawn")
            queue = ctx.Queue()
            processes = [ctx.Process(target=_hold_slot, args=(tmp, 2, 0.4, queue)) for _ in range(3)]
            started = time.monotonic()
            for process in processes:
                process.start()
            entered = sorted(queue.get(timeout=3)[1] for _ in processes)
            for process in processes:
                process.join(timeout=3)
                self.assertEqual(0, process.exitcode)
            self.assertLess(entered[1] - started, 0.35)
            self.assertGreater(entered[2] - entered[0], 0.3)

    def test_serial_mode_excludes_two_slot_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = multiprocessing.get_context("spawn")
            queue = ctx.Queue()
            serial = ctx.Process(target=_hold_slot, args=(tmp, 1, 0.4, queue))
            parallel = ctx.Process(target=_hold_slot, args=(tmp, 2, 0.0, queue))
            serial.start()
            first = queue.get(timeout=3)[1]
            parallel.start()
            second = queue.get(timeout=3)[1]
            serial.join(timeout=3)
            parallel.join(timeout=3)
            self.assertGreater(second - first, 0.3)
            self.assertEqual(0, serial.exitcode)
            self.assertEqual(0, parallel.exitcode)


if __name__ == "__main__":
    unittest.main()
