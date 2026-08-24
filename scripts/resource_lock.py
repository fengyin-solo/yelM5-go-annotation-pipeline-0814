#!/usr/bin/env python3
"""Small cross-process file locks for shared pipeline resources."""
from __future__ import annotations

import fcntl
import hashlib
import time
from contextlib import contextmanager
from pathlib import Path


def lock_name(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{digest}.lock"


@contextmanager
def resource_lock(path: Path, *, timeout: int | None = 0, label: str = "共享资源"):
    """Hold an exclusive flock; timeout=0/None waits indefinitely."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    deadline = None if timeout in (None, 0) else time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"等待{label}锁超时（{timeout}s）: {path}")
                time.sleep(0.1)
        yield
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

