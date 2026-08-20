#!/usr/bin/env python3
"""测试模型调用全局串行锁。

red / green / 修复轨迹都调用同一个限流测试模型，必须全局串行，避免并发触发限流。
锁文件固定放在 ~/.codex/go-annotation-pipeline/test_model.lock，跨批次、跨项目生效。

用法（其它脚本 import）:
    from serial_lock import test_model_lock
    with test_model_lock(timeout=0):
        ...
"""
from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_DIR = Path.home() / ".codex" / "go-annotation-pipeline"
LOCK_FILE = LOCK_DIR / "test_model.lock"


@contextmanager
def test_model_lock(timeout: int | None = 0):
    """获取测试模型全局排他锁。

    timeout=0 或 None 表示一直等待（串行流水线推荐）；>0 表示最多等待该秒数，
    超时抛 TimeoutError。red / green / 修复轨迹共用同一把锁。
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "a+")
    deadline = None if (timeout in (None, 0)) else time.monotonic() + timeout
    got = False
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"等待测试模型全局串行锁超时（{timeout}s），可能有其它 red/green/修复轨迹正在运行"
                    )
                time.sleep(3)
        print("🔒 已获取测试模型全局串行锁（red / green / 修复 共用）", flush=True)
        yield
    finally:
        if got:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
        print("🔓 已释放测试模型全局串行锁", flush=True)
