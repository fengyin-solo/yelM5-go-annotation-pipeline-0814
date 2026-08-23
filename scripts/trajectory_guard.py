#!/usr/bin/env python3
"""修复轨迹的测试隔离与工具调用守卫。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pretool_workspace_guard import MARKER as WORKSPACE_BLOCK_MARKER
from pretool_workspace_guard import bash_path_candidates


EXCLUDED_NAMES = {"skill.md", "agents.md", "claude.md", "bug_repro.md"}
FORBIDDEN_SEGMENTS = {"_gold", "evaluator", ".git", ".claude", "_failed_rounds", "_evidence"}
DELIVERY_ONLY_NAMES = {
    "benzhi.dockerfile", "build_benzhi_docker.sh", "benzhi_readme.md", ".dockerignore",
}
TEST_DIR_NAMES = {"test", "tests", "testdata", "evaluator"}


def is_test_artifact(path: str | Path) -> bool:
    rel = Path(str(path).replace("\\", "/"))
    if any(part.lower() in TEST_DIR_NAMES for part in rel.parts[:-1]):
        return True
    name = rel.name.lower()
    return bool(
        name.endswith("_test.go")
        or re.search(r"(?:^test_|_test\.|\.test\.|\.spec\.)", name)
    )


def _run_rsync(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"rsync 失败: {result.stderr[:300]}")


def copy_without_tests(src: Path, dst: Path) -> None:
    """生成模型可见的隔离副本：不包含测试资产和内部交付线索。"""
    dst.mkdir(parents=True, exist_ok=True)
    _run_rsync([
        "rsync", "-a", "--checksum", "--delete",
        "--exclude=.git", "--exclude=.claude", "--exclude=*_test.go",
        "--exclude=test/", "--exclude=tests/", "--exclude=testdata/", "--exclude=evaluator/",
        "--exclude=test_*", "--exclude=*_test.*", "--exclude=*.test.*", "--exclude=*.spec.*",
        "--exclude=SKILL.md", "--exclude=AGENTS.md", "--exclude=CLAUDE.md",
        "--exclude=BUG_REPRO.md", "--exclude=trajectory*.jsonl", "--exclude=*.log",
        "--exclude=benzhi.Dockerfile", "--exclude=build_benzhi_docker.sh",
        "--exclude=BENZHI_README.md", "--exclude=.dockerignore",
        str(src).rstrip("/") + "/", str(dst).rstrip("/") + "/",
    ])


def source_manifest(root: Path) -> dict[str, str]:
    """Return the exact model-visible source tree digest map."""
    result: dict[str, str] = {}
    if not root.is_dir():
        return result
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(root)
        if (
            any(part in FORBIDDEN_SEGMENTS for part in rel.parts)
            or path.name.lower() in EXCLUDED_NAMES
            or path.name.lower() in DELIVERY_ONLY_NAMES
            or is_test_artifact(rel)
            or path.name.endswith((".jsonl", ".log"))
        ):
            continue
        result[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def write_source_manifest(root: Path, output: Path, *, commit: str, branch: str) -> dict:
    data = {
        "schema": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "commit": commit,
        "branch": branch,
        "files": source_manifest(root),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def source_manifest_issues(root: Path, manifest_path: Path) -> list[str]:
    links = [str(path.relative_to(root)) for path in root.rglob("*") if path.is_symlink()]
    if links:
        return ["G1 模型快照不允许符号链接: " + ", ".join(links[:8])]
    if not manifest_path.is_file():
        return [f"缺少 G1 快照清单: {manifest_path}"]
    try:
        expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"G1 快照清单无效: {exc}"]
    actual = source_manifest(root)
    wanted = expected.get("files") if isinstance(expected, dict) else None
    if not isinstance(wanted, dict):
        return ["G1 快照清单缺少 files 字段"]
    if actual == wanted:
        return []
    missing = sorted(set(wanted) - set(actual))
    extra = sorted(set(actual) - set(wanted))
    changed = sorted(k for k in set(actual) & set(wanted) if actual[k] != wanted[k])
    parts = []
    if missing:
        parts.append("缺少: " + ", ".join(missing[:8]))
    if extra:
        parts.append("多出: " + ", ".join(extra[:8]))
    if changed:
        parts.append("内容变化: " + ", ".join(changed[:8]))
    return ["G1 模型快照与发布时清单不一致（" + "；".join(parts) + "）"]


def sync_business_back(src: Path, dst: Path) -> None:
    """把模型业务改动同步回 env，保留 env 中原有测试且不引入新测试。"""
    dst.mkdir(parents=True, exist_ok=True)
    _run_rsync([
        "rsync", "-a", "--checksum", "--delete",
        "--exclude=.git", "--exclude=.claude", "--exclude=*_test.go",
        "--exclude=test/", "--exclude=tests/", "--exclude=testdata/", "--exclude=evaluator/",
        "--exclude=test_*", "--exclude=*_test.*", "--exclude=*.test.*", "--exclude=*.spec.*",
        "--exclude=trajectory*.jsonl", "--exclude=*.log",
        str(src).rstrip("/") + "/", str(dst).rstrip("/") + "/",
    ])


def inject_evaluator(evaluator: Path, dst: Path) -> None:
    """将私有 evaluator 按相对路径覆盖到临时验收环境。"""
    if not evaluator.is_dir():
        raise RuntimeError(f"缺少私有测试目录: {evaluator}")
    _run_rsync([
        "rsync", "-a", "--checksum", str(evaluator).rstrip("/") + "/", str(dst).rstrip("/") + "/",
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

    for link in evaluator.rglob("*"):
        if link.is_symlink():
            issues.append(f"evaluator 不允许符号链接: {link.relative_to(evaluator)}")

    for item in evaluator_files(evaluator):
        rel = item.relative_to(evaluator)
        if item.is_symlink():
            continue
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
    for path in root.rglob("*"):
        if path.is_file() and is_test_artifact(path.relative_to(root)):
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
            candidate = workspace / candidate
        Path(os.path.realpath(candidate)).relative_to(Path(os.path.realpath(workspace)))
        return False
    except (OSError, ValueError):
        return True


def trajectory_policy_report(path: Path, workspace: Path | None = None) -> dict:
    """Classify answer leakage evidence as clean, suspect, or cheat."""
    events = load_events(path)
    workspace = (workspace or infer_workspace(events))
    cheat: list[str] = []
    suspect: list[str] = []
    seen: set[tuple[str, str]] = set()
    model_created_tests: set[str] = set()
    first_test_source_read: int | None = None
    first_business_read: dict[str, int] = {}
    modified_business: set[str] = set()
    blocked_tool_ids: set[str] = set()
    blocked_attempts: list[str] = []

    for event in events:
        if event.get("type") != "user":
            continue
        content = event.get("message", {}).get("content")
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            result_text = json.dumps(item.get("content"), ensure_ascii=False)
            if WORKSPACE_BLOCK_MARKER in result_text:
                blocked_tool_ids.add(str(item.get("tool_use_id") or ""))

    def add(bucket: list[str], message: str) -> None:
        key = ("cheat" if bucket is cheat else "suspect", message)
        if key not in seen:
            seen.add(key)
            bucket.append(message)

    def normalize_tool_path(value: str) -> str:
        if not value:
            return ""
        candidate = Path(value).expanduser()
        if workspace and not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            return candidate.resolve().as_posix()
        except OSError:
            return candidate.as_posix()

    sequence = 0
    for event in events:
        if event.get("type") != "assistant":
            continue
        content = event.get("message", {}).get("content")
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            name = str(item.get("name") or "")
            inp = item.get("input") or {}
            tool_id = str(item.get("id") or "")
            hook_blocked = tool_id in blocked_tool_ids
            sequence += 1
            strings = _strings(inp)
            serialized = json.dumps(inp, ensure_ascii=False)
            file_path = str(inp.get("file_path") or inp.get("path") or "")
            normalized = normalize_tool_path(file_path)
            bash_created_tests: set[str] = set()
            if name == "Bash":
                command = str(inp.get("command") or "")
                for match in re.findall(r"(?:^|[\s;])>>?\s*([^\s;&|]+_test\.go)(?:\s|$)", command, re.I):
                    bash_created_tests.add(normalize_tool_path(match))
                for match in re.findall(r"(?:^|[\s;])tee(?:\s+-a)?\s+([^\s;&|]+_test\.go)(?:\s|$)", command, re.I):
                    bash_created_tests.add(normalize_tool_path(match))
                model_created_tests.update(bash_created_tests)
            test_key = file_path
            if workspace and normalized:
                try:
                    test_key = str(Path(normalized).relative_to(workspace.resolve()))
                except ValueError:
                    pass
            is_test = bool(file_path and is_test_artifact(test_key))
            is_write = name in {"Edit", "Write", "MultiEdit", "NotebookEdit"}
            if is_test and is_write:
                model_created_tests.add(normalized)
            elif is_test and normalized not in model_created_tests:
                add(cheat, f"{name} 在自建之前读取了预置测试: {file_path}")
                first_test_source_read = first_test_source_read or sequence
            elif name != "Bash" and not normalized and re.search(r"_test\.go\b", serialized, re.I):
                add(suspect, f"{name} 枚举或搜索了测试源码，需人工确认是否读到验收断言")
                first_test_source_read = first_test_source_read or sequence
            if hook_blocked:
                blocked_attempts.append(f"{name}: {json.dumps(inp, ensure_ascii=False)[:240]}")
                continue
            if any(re.search(rf"(^|[/\\]){re.escape(seg)}([/\\]|$)", s) for s in strings for seg in FORBIDDEN_SEGMENTS):
                add(cheat, f"{name} 接触了禁止目录（.git/_gold/evaluator/证据或失败记录）")

            if normalized and not is_test and normalized.endswith((".go", ".py", ".js", ".ts", ".java")):
                if name in {"Read", "Grep", "Glob"}:
                    first_business_read.setdefault(normalized, sequence)
                if is_write:
                    modified_business.add(normalized)

            for field in ("file_path", "path", "cwd"):
                value = inp.get(field) if isinstance(inp, dict) else None
                if workspace and isinstance(value, str) and _outside(value, workspace):
                    add(cheat, f"{name} 访问了工作区外路径: {value}")

            if name == "Bash":
                command = str(inp.get("command") or "")
                if re.search(r"(?:^|[\s;&|('\"])(?:\.\./)", command):
                    add(cheat, "Bash 使用 ../ 访问上级目录")
                if re.search(r"\bgit\s+(?:log|show|reflog|blame|checkout|switch|diff)\b", command):
                    add(cheat, "Bash 查看了 Git 历史、差异或切换了历史状态")
                for match in re.findall(r"(?:^|\s)([^\s;&|]+_test\.go)(?:\s|$)", command, re.I):
                    test_path = normalize_tool_path(match)
                    if any(char in match for char in "*?["):
                        add(suspect, "Bash 枚举或搜索了测试源码，需人工确认是否读到验收断言")
                        first_test_source_read = first_test_source_read or sequence
                    elif test_path not in model_created_tests:
                        add(cheat, f"Bash 在自建之前接触了预置测试: {match}")
                        first_test_source_read = first_test_source_read or sequence
                if workspace:
                    for token in bash_path_candidates(command):
                        if _outside(token, workspace):
                            add(cheat, f"Bash 访问了工作区外路径: {token}")

    if first_test_source_read is not None:
        late = sorted(path for path in modified_business if first_business_read.get(path, 10**9) > first_test_source_read)
        if late:
            add(suspect, "最终修改的实现文件在读测试后才首次打开: " + ", ".join(late[:8]))

    classification = "cheat" if cheat else ("suspect" if suspect else "clean")
    return {
        "classification": classification,
        "cheat": cheat,
        "suspect": suspect,
        "model_created_tests": sorted(model_created_tests),
        "blocked_attempts": blocked_attempts,
    }


def trajectory_policy_issues(path: Path, workspace: Path | None = None) -> list[str]:
    """Compatibility wrapper returning only hard cheat evidence."""
    return trajectory_policy_report(path, workspace)["cheat"]


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
        if issues:
            for issue in issues:
                print(f"❌ {issue}")
            raise SystemExit(1)
        print("✅ 私有测试隔离门禁通过")
        return

    root = Path(args.workspace_root).resolve() if args.workspace_root else None
    report = trajectory_policy_report(Path(args.trajectory), root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["classification"] == "cheat":
        raise SystemExit(1)
    if report["classification"] == "suspect":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
