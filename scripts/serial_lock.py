#!/usr/bin/env python3
"""测试模型调用全局并发槽位。

red / green / 修复轨迹都调用同一个限流测试模型，默认全局最多并发 2 路。
槽位锁放在 ~/.codex/go-annotation-pipeline/model-slots/，跨批次、跨项目生效。
同时对旧 test_model.lock 持有共享锁，使新版进程会等待仍在运行的旧版排他锁。

用法（其它脚本 import）:
    from serial_lock import test_model_lock
    with test_model_lock(timeout=0):
        ...
"""
from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_DIR = Path.home() / ".codex" / "go-annotation-pipeline"
LOCK_FILE = LOCK_DIR / "test_model.lock"
SLOT_DIR = LOCK_DIR / "model-slots"


@contextmanager
def test_model_lock(timeout: int | None = 0, slots: int = 2):
    """获取一个测试模型全局并发槽位。

    timeout=0 或 None 表示一直等待；>0 表示最多等待该秒数。所有调用方
    必须使用相同的 slots；批处理默认传 2。slots=1 可恢复串行模式。
    """
    if slots not in (1, 2):
        raise ValueError("测试模型并发槽位数只能是 1 或 2")
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    SLOT_DIR.mkdir(parents=True, exist_ok=True)
    legacy_fh = open(LOCK_FILE, "a+")
    slot_fhs = [open(SLOT_DIR / f"slot-{index}.lock", "a+") for index in range(slots)]
    deadline = None if (timeout in (None, 0)) else time.monotonic() + timeout
    acquired = None
    try:
        while True:
            legacy_mode = fcntl.LOCK_EX if slots == 1 else fcntl.LOCK_SH
            try:
                fcntl.flock(legacy_fh.fileno(), legacy_mode | fcntl.LOCK_NB)
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"等待测试模型兼容锁超时（{timeout}s，slots={slots}），"
                        "可能有旧版或串行模式轨迹正在运行"
                    )
                time.sleep(1)
                continue
            for index, fh in enumerate(slot_fhs):
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = index
                    break
                except BlockingIOError:
                    continue
            if acquired is not None:
                break
            fcntl.flock(legacy_fh.fileno(), fcntl.LOCK_UN)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"等待测试模型并发槽位超时（{timeout}s，slots={slots}），"
                    "可能有其它 red/green/修复轨迹正在运行"
                )
            time.sleep(1)
        print(f"🔒 已获取测试模型并发槽位 {acquired + 1}/{slots}（red / green / 修复 共用）", flush=True)
        yield
    finally:
        if acquired is not None:
            fcntl.flock(slot_fhs[acquired].fileno(), fcntl.LOCK_UN)
        for fh in slot_fhs:
            fh.close()
        fcntl.flock(legacy_fh.fileno(), fcntl.LOCK_UN)
        legacy_fh.close()
        if acquired is not None:
            print(f"🔓 已释放测试模型并发槽位 {acquired + 1}/{slots}", flush=True)
