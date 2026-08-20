#!/usr/bin/env python3
"""后置质检：对一期根目录下所有记录做交付前的最终硬校验。

在整条流水线跑完（轨迹已上传、verify_result 已回填、push-fix 已完成）之后执行。
只读，不改任何产物；逐条输出 ✅/❌，最后给出汇总，不合格时退出码非 0。

校验项（对应甲方抽检红线）：
  1. build       env 与 _gold 都能 `go build ./...`（项目能编译）
  2. scope       bugfix 的 gold 修复至少改 4 个功能文件且增删总行数至少 20 行
  3. red         埋错基线（.base_snapshot）跑验收/复现命令必须红（bug 真实可复现）
  4. green       _gold 跑同样命令必须绿（修复后通过）
  5. files       交付文件齐全（轨迹 jsonl、BUG_REPRO、collection.json）
  6. fields      collection.json 必填字段齐全（bugfix: verify_cmds/verify_result；
                 diagnosis: verify_cmds/gold_root_cause/verify_result）
  7. evidence    verify_result 结构正确、URL 可访问、session_id 匹配
  8. diagnosis   diagnosis 题 env 与埋错基线零差异（全程零代码改动）
  9. coverage    verify_cmds 为单包、单测试、-count=1，且红灯失败测试真实存在
 10. difficulty  运行时机制、跨层触发、题面症状覆盖和逐文件回退证据齐全

用法:
  post_qc.py --root <root> [--date YYYY-MM-DD] [--project <name>__<record>] [--go-version 1.22]

verify_cmds 必须来自 collection.json，并与红灯证据轨迹中实际执行的唯一 Bash 命令、
最终回复里的【命令】逐字一致；bugfix 题的绿灯轨迹同样必须逐字一致。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def read_collection(proj: Path) -> dict:
    p = proj / "collection.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def find_projects(root: Path, date: str | None, project: str | None) -> list[Path]:
    date_dirs = [root / date] if date else sorted(p for p in root.glob("*/") if p.is_dir() and not p.name.startswith("_"))
    out = []
    for d in date_dirs:
        if not d.is_dir():
            continue
        for child in sorted(d.iterdir()):
            if not child.is_dir():
                continue
            if project and child.name.lower() != project.replace("/", "__").lower():
                continue
            if (child / "collection.json").exists():
                out.append(child)
    return out


def detect_go_version(proj: Path) -> str:
    for mod in (proj / "go.mod", proj / "backend" / "go.mod"):
        if mod.exists():
            m = re.search(r"^go\s+([0-9]+\.[0-9]+)", mod.read_text(encoding="utf-8", errors="ignore"), re.M)
            if m:
                return m.group(1)
    return ""


def go_env(go_version: str) -> dict:
    env = os.environ.copy()
    if go_version:
        if go_version == "local":
            env["GOTOOLCHAIN"] = "local"
        else:
            parts = go_version.split(".")
            env["GOTOOLCHAIN"] = f"go{go_version if len(parts) >= 3 else go_version + '.0'}"
    return env


def run(cmd: str, cwd: Path, env: dict, timeout: int = 900) -> tuple[int, str]:
    r = subprocess.run(["bash", "-c", cmd], cwd=str(cwd), env=env,
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + "\n" + r.stderr)


def evidence_command_for(proj: Path, phase: str) -> str:
    """从证据轨迹最终回复中提取【命令】，用于与 collection.verify_cmds 逐字比对。"""
    ev = proj / "_evidence" / f"verify_{phase}.jsonl"
    if ev.exists():
        text = ""
        for line in ev.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") == "assistant":
                c = e.get("message", {}).get("content") or []
                for it in c if isinstance(c, list) else []:
                    if isinstance(it, dict) and it.get("type") == "text":
                        text += (it.get("text") or "") + "\n"
        m = re.search(r"(?:^|\n)【命令】([^\n]*)", text)
        if m:
            return m.group(1)
    return ""


def evidence_executed_commands_for(proj: Path, phase: str) -> list[str]:
    """提取交付的原始证据轨迹中的实际 Bash 调用，保留原字符串。"""
    ev = proj / "_evidence" / f"verify_{phase}.jsonl"
    commands: list[str] = []
    if not ev.exists():
        return commands
    for line in ev.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("type") != "assistant":
            continue
        content = event.get("message", {}).get("content") or []
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            if str(item.get("name") or "").lower() not in {"bash", "shell"}:
                continue
            command = (item.get("input") or {}).get("command")
            if isinstance(command, str):
                commands.append(command)
    return commands


def fail_tests(output: str) -> set[str]:
    tests = set()
    for line in output.splitlines():
        m = re.match(r"\s*--- FAIL:\s+(\S+)", line)
        if m:
            tests.add(m.group(1).split("/", 1)[0])
    return tests


def project_tests(proj: Path) -> set[str]:
    """grep 项目源码里的 func Test* 名称，用于核对红灯失败测试是否真实存在于项目。"""
    names = set()
    for p in proj.rglob("*_test.go"):
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        names.update(re.findall(r"^func\s+(Test\w+)\s*\(", txt, re.M))
    return names


def functional_diff_scope(buggy: Path, gold: Path) -> tuple[int, int]:
    """返回 gold 修复涉及的功能文件数和增删总行数。"""
    r = subprocess.run(
        ["git", "diff", "--no-index", "--numstat", str(buggy), str(gold)],
        capture_output=True, text=True,
    )
    files = 0
    lines = 0
    for row in r.stdout.splitlines():
        parts = row.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        normalized = path.lower().replace("\\", "/")
        name = normalized.rsplit("/", 1)[-1]
        if (
            name.endswith("_test.go")
            or name.startswith("readme")
            or name.endswith((".md", ".rst", ".txt"))
            or "benzhi" in name
            or name == "build_benzhi_docker.sh"
        ):
            continue
        if not added.isdigit() or not deleted.isdigit():
            continue
        files += 1
        lines += int(added) + int(deleted)
    return files, lines


def verify_result_ok(proj: Path, coll: dict, task_type: str) -> tuple[bool, str]:
    obj = coll.get("verify_result")
    if not obj:
        return False, "verify_result 为空"
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except Exception:
            return False, "verify_result 不是合法 JSON"
    pre = obj.get("pre_fix") or {}
    post = obj.get("post_fix")
    if task_type == "bugfix":
        if not post:
            return False, "bugfix 缺 post_fix"
    if not pre.get("session_id") or pre.get("result") != "red":
        return False, "pre_fix 结构/result 异常"
    if task_type == "bugfix":
        if not post.get("session_id") or post.get("result") != "green":
            return False, "post_fix 结构/result 异常"
    # URL 可访问性
    urls = [pre.get("trajectory_url", "")]
    if post:
        urls.append(post.get("trajectory_url", ""))
    for u in urls:
        if not u:
            return False, "verify_result 缺 trajectory_url"
        parsed = urlparse(u)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False, "trajectory_url 非法"
    return True, "ok"


def check_record(proj: Path, go_ver: str, args) -> list[tuple[str, bool, str]]:
    coll = read_collection(proj)
    task_type = (coll.get("task_type") or "").strip() or "bugfix"
    verify_cmds = coll.get("verify_cmds") or ""
    env = go_env(go_ver)
    results: list[tuple[str, bool, str]] = []

    env_dir = proj / "env"
    gold_dir = proj.parents[1] / "_gold" / proj.name
    if not gold_dir.exists():
        results.append(("build", False, "找不到 _gold 目录"))
        return results

    base = proj / ".base_snapshot"

    # 1. build
    build_fail = []
    for label, d in (("env", env_dir), ("gold", gold_dir)):
        if not (d / "go.mod").exists() and not (d / "backend" / "go.mod").exists():
            build_fail.append(f"{label} 无 go.mod")
            continue
        rc, out = run("go build ./...", d, env)
        if rc != 0:
            build_fail.append(f"{label} build 失败: {out[-200:]}")
    results.append(("build", not build_fail, "; ".join(build_fail) or "ok"))

    # 2. bugfix gold 修复规模
    if task_type == "bugfix":
        if not base.exists():
            results.append(("scope", False, "缺 .base_snapshot，无法核对 gold 修复规模"))
        else:
            changed_files, changed_lines = functional_diff_scope(base, gold_dir)
            scope_ok = changed_files >= 4 and changed_lines >= 20
            results.append(("scope", scope_ok, f"候选功能代码 {changed_files} 个文件，增删 {changed_lines} 行（要求 ≥4 文件且 ≥20 行；仍须人工排除注释/格式化/无效改动）"))

    # 3/4. red / green
    buggy = base if base.exists() else env_dir
    red_evidence_cmd = evidence_command_for(proj, "red")
    green_evidence_cmd = evidence_command_for(proj, "green") if task_type == "bugfix" else ""
    red_executed_cmds = evidence_executed_commands_for(proj, "red")
    green_executed_cmds = evidence_executed_commands_for(proj, "green") if task_type == "bugfix" else []
    red_cmd = verify_cmds
    if red_cmd:
        red_rc, red_out = run(red_cmd, buggy, env)
        red_ok = red_rc != 0
        results.append(("red", red_ok, f"exit={red_rc}" + ("" if red_ok else "（基线竟然绿了）")))
    else:
        red_rc, red_out = 0, ""
        results.append(("red", False, "缺少定向复现命令"))

    if task_type == "bugfix":
        if verify_cmds:
            green_rc, _ = run(verify_cmds, gold_dir, env)
            green_ok = green_rc == 0
            results.append(("green", green_ok, f"exit={green_rc}"))
        else:
            results.append(("green", False, "缺 verify_cmds"))
    else:
        rc, _ = run("go test ./...", gold_dir, env)
        results.append(("green", rc == 0, f"go test exit={rc}"))

    # 5. files
    sid = (coll.get("session_id") or "").strip()
    missing = []
    for f, ok in ((f"{sid}.jsonl", (proj / f"{sid}.jsonl").exists() if sid else False),
                  ("BUG_REPRO.md", (proj / "BUG_REPRO.md").exists()),
                  ("collection.json", (proj / "collection.json").exists())):
        if not ok:
            missing.append(f)
    results.append(("files", not missing, "; ".join(f"缺 {m}" for m in missing) or "ok"))

    # 6. fields
    miss = []
    if not sid:
        miss.append("session_id")
    if not coll.get("repo_url"):
        miss.append("repo_url")
    if not coll.get("trajectory"):
        miss.append("trajectory")
    harness = (coll.get("harness") or "").strip()
    if not harness:
        miss.append("harness")
    elif not re.search(r"\bv?\d+(?:\.\d+)+\b", harness):
        miss.append("harness(缺工具版本号)")
    if not verify_cmds:
        miss.append("verify_cmds")
    if task_type == "diagnosis":
        if not coll.get("gold_root_cause"):
            miss.append("gold_root_cause")
    results.append(("fields", not miss, "; ".join(f"缺 {m}" for m in miss) or "ok"))

    # 7. evidence
    ok, msg = verify_result_ok(proj, coll, task_type)
    results.append(("evidence", ok, msg))

    # 8. diagnosis 零改动
    if task_type == "diagnosis":
        if base.exists():
            r = subprocess.run(["diff", "-rq", str(base), str(env_dir)], capture_output=True, text=True)
            zero = r.returncode == 0
            results.append(("diagnosis", zero, "env==base_snapshot" if zero else "env 有改动"))
        else:
            results.append(("diagnosis", False, "缺 .base_snapshot"))

    # 9. coverage：命令形态合规，且红灯失败测试能在项目测试中找到
    from verify_cmds import (
        CONCURRENCY_CATEGORY,
        validate_concurrency_metadata,
        validate_success_criteria,
        validate_verify_cmds,
    )
    require_race = str(coll.get("bug_category") or "").strip() == CONCURRENCY_CATEGORY
    verify_issues = validate_verify_cmds(verify_cmds, require_race=require_race)
    verify_issues.extend(validate_concurrency_metadata(coll))
    verify_issues.extend(validate_success_criteria(coll))
    ft = fail_tests(red_out)
    known = project_tests(env_dir) | project_tests(gold_dir)
    unknown = sorted(t for t in ft if t not in known)
    if not ft:
        relation_error = "红灯无 FAIL 测试（可能 build 失败或命令跑空）"
    else:
        relation_error = f"不在项目里的失败测试: {unknown}" if unknown else ""
    command_errors = []
    if not red_evidence_cmd:
        command_errors.append("红灯轨迹最终回复缺【命令】")
    elif red_evidence_cmd != verify_cmds:
        command_errors.append(f"verify_cmds 与红灯轨迹【命令】不一致: collection={verify_cmds!r}; red={red_evidence_cmd!r}")
    if red_executed_cmds != [verify_cmds]:
        command_errors.append(f"verify_cmds 与红灯轨迹实际执行命令不一致: collection={verify_cmds!r}; red={red_executed_cmds!r}")
    if task_type == "bugfix":
        if not green_evidence_cmd:
            command_errors.append("绿灯轨迹最终回复缺【命令】")
        elif green_evidence_cmd != verify_cmds:
            command_errors.append(f"verify_cmds 与绿灯轨迹【命令】不一致: collection={verify_cmds!r}; green={green_evidence_cmd!r}")
        if green_executed_cmds != [verify_cmds]:
            command_errors.append(f"verify_cmds 与绿灯轨迹实际执行命令不一致: collection={verify_cmds!r}; green={green_executed_cmds!r}")
    coverage_errors = verify_issues + command_errors + ([relation_error] if relation_error else [])
    results.append(("coverage", not coverage_errors, "；".join(coverage_errors) or "命令形态与失败测试已校验；仍须人工逐项核对 user_query 覆盖"))

    # 10. 难度审查：机器校验证据完整性，机制真实性仍由 reviewer_notes 的人工审查负责
    from difficulty_review import validate_review
    difficulty_ok, difficulty_issues = validate_review(proj, task_type)
    results.append((
        "difficulty",
        difficulty_ok,
        "；".join(difficulty_issues) if difficulty_issues else "难度审查证据齐全",
    ))

    return results


def main():
    ap = argparse.ArgumentParser(description="后置质检")
    ap.add_argument("--root", default=".")
    ap.add_argument("--date")
    ap.add_argument("--project")
    ap.add_argument("--go-version", help="强制 Go 主版本（如 1.22），缺省从 go.mod 读取")
    ap.add_argument("--verify-cmds", help=argparse.SUPPRESS)
    args = ap.parse_args()

    root = Path(args.root).resolve()
    projects = find_projects(root, args.date, args.project)
    if not projects:
        print("（没有找到任何记录）")
        sys.exit(1)

    print(f"后置质检：共 {len(projects)} 条记录\n")
    all_ok = True
    for proj in projects:
        go_ver = args.go_version or detect_go_version(proj / "env") or detect_go_version(proj)
        coll = read_collection(proj)
        task_type = (coll.get("task_type") or "bugfix").strip()
        print(f"== {proj.name}  ({task_type}) ==")
        results = check_record(proj, go_ver, args)
        for key, ok, msg in results:
            print(f"   [{'✅' if ok else '❌'}] {key:10s} {msg}")
            if not ok:
                all_ok = False
        print()

    print("=" * 44)
    if all_ok:
        print("✅ 全部记录后置质检通过。")
        sys.exit(0)
    print("❌ 存在不合格记录，请逐项修复后重跑。")
    sys.exit(1)


if __name__ == "__main__":
    main()
