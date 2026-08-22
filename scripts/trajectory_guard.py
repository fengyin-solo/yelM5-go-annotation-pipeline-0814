#!/usr/bin/env python3
"""修复轨迹的测试隔离与工具调用守卫。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path


EXCLUDED_NAMES = {"skill.md", "agents.md", "claude.md", "bug_repro.md"}
FORBIDDEN_SEGMENTS = {"_gold", "evaluator", ".git", ".claude", "_failed_rounds", "_evidence"}


def _run_rsync(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"rsync 失败: {result.stderr[:300]}")


def copy_without_tests(src: Path, dst: Path) -> None:
    """生成模型可见的隔离副本：不包含任何 Go 测试和内部交付线索。"""
    dst.mkdir(parents=True, exist_ok=True)
    _run_rsync([
        "rsync", "-a", "--delete",
        "--exclude=.git", "--exclude=.claude", "--exclude=*_test.go",
        "--exclude=SKILL.md", "--exclude=AGENTS.md", "--exclude=CLAUDE.md",
        "--exclude=BUG_REPRO.md", "--exclude=trajectory*.jsonl", "--exclude=*.log",
        str(src).rstrip("/") + "/", str(dst).rstrip("/") + "/",
    ])


def sync_business_back(src: Path, dst: Path) -> None:
    """把模型业务改动同步回 env，保留 env 中原有测试且不引入新测试。"""
    dst.mkdir(parents=True, exist_ok=True)
    _run_rsync([
        "rsync", "-a", "--delete",
        "--exclude=.git", "--exclude=.claude", "--exclude=*_test.go",
        "--exclude=trajectory*.jsonl", "--exclude=*.log",
        str(src).rstrip("/") + "/", str(dst).rstrip("/") + "/",
    ])


def inject_evaluator(evaluator: Path, dst: Path) -> None:
    """将私有 evaluator 按相对路径覆盖到临时验收环境。"""
    if not evaluator.is_dir():
        raise RuntimeError(f"缺少私有测试目录: {evaluator}")
    _run_rsync([
        "rsync", "-a", str(evaluator).rstrip("/") + "/", str(dst).rstrip("/") + "/",
    ])


def evaluator_test_files(evaluator: Path) -> list[Path]:
    return sorted(p for p in evaluator.rglob("*_test.go") if p.is_file()) if evaluator.is_dir() else []


def evaluator_files(evaluator: Path) -> list[Path]:
    if not evaluator.is_dir():
        return []
    return sorted(
        p for p in evaluator.rglob("*")
        if p.is_file() and p.name != ".DS_Store" and p.suffix not in {".jsonl", ".log"}
        and ".git" not in p.parts
    )


def verify_test_name(verify_cmds: str) -> str:
    match = re.search(r"(?:^|\s)-run(?:=|\s+)(?:['\"])?\^?(Test[A-Za-z0-9_]+)", verify_cmds or "")
    return match.group(1) if match else ""


def private_test_issues(env: Path, evaluator: Path, verify_cmds: str = "") -> list[str]:
    """跑修复轨迹前的私有测试门禁。"""
    issues: list[str] = []
    env = env.resolve()
    evaluator = evaluator.resolve()
    if not env.is_dir():
        return [f"env 不存在: {env}"]
    tests = evaluator_test_files(evaluator)
    if not tests:
        issues.append(f"evaluator 中没有私有 *_test.go: {evaluator}")
        return issues

    for item in evaluator_files(evaluator):
        rel = item.relative_to(evaluator)
        if not (item.name.endswith("_test.go") or "testdata" in rel.parts):
            issues.append(f"evaluator 只允许 *_test.go 和 testdata 夹具: {rel}")

    for test in evaluator_files(evaluator):
        rel = test.relative_to(evaluator)
        if (env / rel).exists():
            issues.append(f"私有 evaluator 文件仍存在于 env: {rel}")

    target = verify_test_name(verify_cmds)
    if target:
        pattern = re.compile(rf"\bfunc\s+{re.escape(target)}\s*\(")
        if not any(pattern.search(p.read_text(encoding="utf-8", errors="ignore")) for p in tests):
            issues.append(f"evaluator 中未找到 verify_cmds 的目标测试: {target}")
        for test in env.rglob("*_test.go"):
            if pattern.search(test.read_text(encoding="utf-8", errors="ignore")):
                issues.append(f"目标红绿测试仍存在于 env: {test.relative_to(env)}")
    return issues


def test_manifest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.is_dir():
        return result
    for path in root.rglob("*_test.go"):
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def load_events(path: Path) -> list[dict]:
    events: list[dict] = []
    if not path.exists():
        return events
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def infer_workspace(events: list[dict]) -> Path | None:
    for event in events:
        cwd = event.get("cwd")
        if isinstance(cwd, str) and cwd.startswith("/"):
            return Path(cwd).resolve()
    return None


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_strings(item))
        return out
    return []


def _outside(path_text: str, workspace: Path) -> bool:
    try:
        candidate = Path(path_text).expanduser()
        if str(candidate) == "/dev/null":
            return False
        if not candidate.is_absolute():
            return False
        candidate.resolve().relative_to(workspace.resolve())
        return False
    except (OSError, ValueError):
        return True


def trajectory_policy_issues(path: Path, workspace: Path | None = None) -> list[str]:
    """检查原始轨迹是否越界或接触测试；返回可直接判失败的问题。"""
    events = load_events(path)
    workspace = (workspace or infer_workspace(events))
    issues: list[str] = []
    seen: set[str] = set()

    def add(message: str) -> None:
        if message not in seen:
            seen.add(message)
            issues.append(message)

    for event in events:
        if event.get("type") != "assistant":
            continue
        content = event.get("message", {}).get("content")
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            name = str(item.get("name") or "")
            inp = item.get("input") or {}
            strings = _strings(inp)
            serialized = json.dumps(inp, ensure_ascii=False)
            if re.search(r"_test\.go\b", serialized, re.I):
                add(f"{name} 接触了 _test.go")
            if any(re.search(rf"(^|[/\\]){re.escape(seg)}([/\\]|$)", s) for s in strings for seg in FORBIDDEN_SEGMENTS):
                add(f"{name} 接触了禁止目录（.git/_gold/evaluator/证据或失败记录）")
            if name in {"Edit", "Write", "MultiEdit", "NotebookEdit"} and "test" in serialized.lower():
                if re.search(r"(?:^|[/\\])[^/\\]*test[^/\\]*\.(?:go|py|js|ts|java)\b", serialized, re.I):
                    add(f"{name} 创建或修改了测试文件")

            for field in ("file_path", "path", "cwd"):
                value = inp.get(field) if isinstance(inp, dict) else None
                if workspace and isinstance(value, str) and _outside(value, workspace):
                    add(f"{name} 访问了工作区外路径: {value}")

            if name == "Bash":
                command = str(inp.get("command") or "")
                if re.search(r"(?:^|[\s;&|('\"])(?:\.\./)", command):
                    add("Bash 使用 ../ 访问上级目录")
                if re.search(r"\bgit\s+(?:log|show|reflog|blame|checkout)\b", command):
                    add("Bash 查看了 Git 历史或切换了历史状态")
                if re.search(r"_test\.go\b", command, re.I):
                    add("Bash 接触了 _test.go")
                if workspace:
                    try:
                        tokens = shlex.split(command)
                    except ValueError:
                        tokens = command.split()
                    for token in tokens:
                        token = token.strip("(){}[];,|&<>")
                        if token.startswith("/") and _outside(token, workspace):
                            add(f"Bash 访问了工作区外路径: {token}")
    return issues


def copy_evaluator_to_repo(evaluator: Path, repo: Path) -> list[str]:
    """轨迹验收后将私有测试复制到 Repo，返回相对路径。"""
    tests = evaluator_test_files(evaluator)
    if not tests:
        raise RuntimeError(f"evaluator 中没有可提交的 *_test.go: {evaluator}")
    copied: list[str] = []
    for source in evaluator_files(evaluator):
        rel = source.relative_to(evaluator)
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(rel))
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description="修复轨迹的私有测试与越界访问守卫")
    sub = parser.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("preflight", help="确认目标测试只存在 evaluator")
    preflight.add_argument("--env", required=True)
    preflight.add_argument("--evaluator", required=True)
    preflight.add_argument("--verify-cmds", default="")
    audit = sub.add_parser("audit", help="检查轨迹是否越界或接触测试")
    audit.add_argument("--trajectory", required=True)
    audit.add_argument("--workspace-root")
    args = parser.parse_args()

    if args.command == "preflight":
        issues = private_test_issues(Path(args.env), Path(args.evaluator), args.verify_cmds)
    else:
        root = Path(args.workspace_root).resolve() if args.workspace_root else None
        issues = trajectory_policy_issues(Path(args.trajectory), root)
    if issues:
        for issue in issues:
            print(f"❌ {issue}")
        raise SystemExit(1)
    print("✅ 轨迹守卫检查通过")


if __name__ == "__main__":
    main()
