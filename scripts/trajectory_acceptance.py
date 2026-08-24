#!/usr/bin/env python3
"""Run independent post-trajectory acceptance checks concurrently."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import re
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


def _final_assistant_text(transcript: Path) -> str:
    texts = []
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant":
            continue
        content = event.get("message", {}).get("content")
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
    return "\n".join(texts[-3:])


def _diagnosis_root_cause_check(project: Path, transcript: Path) -> dict:
    """Require the diagnosis conclusion to cover locations, symbols and mechanism facts."""
    collection = json.loads((project / "collection.json").read_text(encoding="utf-8"))
    review = json.loads((project / "difficulty_review.json").read_text(encoding="utf-8"))
    answer = _final_assistant_text(transcript)
    locations = [
        str(item.get("file") or "") for item in
        (review.get("core_defect_review", {}).get("root_cause_locations") or [])
        if isinstance(item, dict)
    ]
    location_hits = [path for path in locations if path and (path in answer or Path(path).name in answer)]
    gold = str(collection.get("gold_root_cause") or "")
    symbol_part = gold.split("符号:", 1)[-1].split("机制:", 1)[0] if "符号:" in gold else ""
    symbols = sorted(set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_.]{2,}\b", symbol_part)))
    symbol_leaves = sorted({token.rsplit(".", 1)[-1] for token in symbols})
    symbol_hits = [token for token in symbol_leaves if token in answer]
    mechanism_part = gold.split("机制:", 1)[-1] if "机制:" in gold else gold
    concepts = {
        "context": ("context", "上下文"), "cancel": ("cancel", "取消"),
        "goroutine": ("goroutine", "协程"), "retry": ("retry", "重试"),
        "channel": ("channel", "通道"), "waitgroup": ("waitgroup",),
        "mutex": ("mutex", "锁"), "race": ("race", "竞态", "并发"),
        "timeout": ("timeout", "超时"), "propagation": ("propagat", "传递", "传播"),
        "wake": ("wake", "唤醒"), "shutdown": ("shutdown", "关闭", "停止"),
        "write": ("write", "写入", "回写", "写回"),
        "state": ("state", "状态", "终态", "中间态", "原样保留", "改坏", "同名项"),
        "filter": ("filter", "筛选", "过滤", "匹配"),
        "enumeration": ("enumerat", "枚举", "列表", "跳过"),
        "aggregation": ("aggregat", "聚合", "概览", "统计", "计数", "累计"),
        "pollution": ("pollut", "污染", "串扰"), "lifecycle": ("lifecycle", "生命周期"),
        "defer": ("defer", "延迟释放"), "panic": ("panic", "崩溃"),
        "slice": ("slice", "切片"), "error": ("error", "错误链", "错误"),
        "nil": ("nil", "空指针"), "interface": ("interface", "接口值"),
        "map": ("map", "映射"), "cache": ("cache", "缓存"),
        "shared": ("shared", "共享"), "alias": ("alias", "别名", "底层数组"),
        "copy": ("copy", "复制", "拷贝"), "timer": ("timer", "定时器"),
        "atomic": ("atomic", "原子"), "idempotency": ("idempoten", "幂等"),
        "resource": ("resource", "资源", "释放"), "recovery": ("recover", "恢复路径", "恢复"),
    }
    gold_lower, answer_lower = mechanism_part.lower(), answer.lower()
    gold_concepts = {name for name, words in concepts.items() if any(word in gold_lower for word in words)}
    mechanism_hits = sorted(name for name in gold_concepts
                            if any(word in answer_lower for word in concepts[name]))
    required_locations = min(3, len(locations))
    required_mechanisms = min(3, len(gold_concepts))
    passed = (len(location_hits) >= required_locations and len(symbol_hits) >= min(2, len(symbol_leaves))
              and required_mechanisms >= 2 and len(mechanism_hits) >= required_mechanisms)
    detail = {
        "passed": passed, "exit_code": 0 if passed else 1,
        "output": json.dumps({
            "location_hits": location_hits, "required_locations": required_locations,
            "symbol_hits": symbol_hits, "mechanism_hits": mechanism_hits,
            "required_mechanisms": required_mechanisms,
        }, ensure_ascii=False),
    }
    return detail


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
    else:
        jobs["diagnosis_root_cause"] = lambda: _diagnosis_root_cause_check(project, transcript)

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
