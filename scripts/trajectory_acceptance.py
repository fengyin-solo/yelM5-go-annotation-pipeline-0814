#!/usr/bin/env python3
"""Run independent post-trajectory acceptance checks concurrently."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from trajectory_guard import inject_evaluator


def _run(command: str | list[str], cwd: Path, env: dict[str, str], timeout: int) -> dict:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        shell=isinstance(command, str),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return {"passed": result.returncode == 0, "exit_code": result.returncode, "output": output[-12000:]}


def _copy_workspace(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=False)


def _private_check(workspace: Path, evaluator: Path, verify_cmds: str, env: dict[str, str], timeout: int) -> dict:
    if not verify_cmds.strip():
        return {"passed": False, "exit_code": None, "output": "verify_cmds is empty"}
    with tempfile.TemporaryDirectory(prefix="trajectory-private-") as tmp:
        target = Path(tmp) / "workspace"
        _copy_workspace(workspace, target)
        inject_evaluator(evaluator, target)
        return _run(verify_cmds, target, env, timeout)


def _regression_check(workspace: Path, module_path: str | None, env: dict[str, str], timeout: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="trajectory-regression-") as tmp:
        target = Path(tmp) / "workspace"
        _copy_workspace(workspace, target)
        cwd = target / module_path if module_path else target
        return _run(["go", "test", "./..."], cwd, env, timeout)


def _analysis_check(transcript: Path, workspace: Path, collection: Path, task_type: str, env: dict[str, str], timeout: int) -> dict:
    script = Path(__file__).with_name("analyze_trajectory.py")
    command = [
        sys.executable, str(script), str(transcript), "--task-type", task_type,
        "--workspace-root", str(workspace), "--collection", str(collection),
    ]
    return _run(command, workspace, env, timeout)


def _semantic_check(workspace: Path, snapshot: Path, task_type: str) -> dict:
    from run_trajectory import validate_task
    issues = validate_task(workspace, snapshot, task_type)
    return {"passed": not issues, "exit_code": 0 if not issues else 1, "output": "; ".join(issues)}


def run_acceptance(
    *, project: Path, workspace: Path, snapshot: Path, transcript: Path,
    session_id: str, task_type: str, verify_cmds: str, evaluator: Path,
    module_path: str | None, env: dict[str, str], timeout: int = 900,
) -> dict:
    """Run independent checks without mutating the model workspace or project env."""
    jobs = {
        "trajectory_analysis": lambda: _analysis_check(
            transcript, workspace, project / "collection.json", task_type, env, timeout
        ),
        "regression": lambda: _regression_check(workspace, module_path, env, timeout),
        "task_semantics": lambda: _semantic_check(workspace, snapshot, task_type),
    }
    if task_type == "bugfix":
        jobs["private_verify"] = lambda: _private_check(workspace, evaluator, verify_cmds, env, timeout)

    checks: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="trajectory-accept") as pool:
        futures = {pool.submit(func): name for name, func in jobs.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                checks[name] = future.result()
            except Exception as exc:
                checks[name] = {"passed": False, "exit_code": None, "output": f"{type(exc).__name__}: {exc}"}

    ordered = {name: checks[name] for name in jobs}
    passed = all(item.get("passed") is True for item in ordered.values())
    payload = {
        "schema": 1,
        "session_id": session_id,
        "task_type": task_type,
        "result": "passed" if passed else "failed",
        "checks": ordered,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    evidence = project / "_evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    target = evidence / "trajectory_acceptance.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload

