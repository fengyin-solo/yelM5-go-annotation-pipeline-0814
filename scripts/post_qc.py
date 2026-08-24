#!/usr/bin/env python3
"""后置质检：对一期根目录下所有记录做交付前的最终硬校验。

在整条流水线跑完（轨迹已上传、verify_result 已回填、finalize 已完成）之后执行。
只读，不改任何产物；逐条输出 ✅/❌，最后给出汇总，不合格时退出码非 0。

校验项（对应甲方抽检红线）：
  0. preflight   新批次必须有 20/20 红绿、目标断言和回退实跑证据
  1. privacy     目标测试只存在私有 evaluator，env 和初始 Bug 基线不得包含
  2. build       env 与 _gold 都能 `go build ./...`（项目能编译）
  3. scope       bugfix 的 gold 修复至少改 4 个功能文件且增删总行数至少 20 行
  4. red         埋错基线（.base_snapshot）跑验收/复现命令必须红（bug 真实可复现）
  5. green       _gold 跑同样命令必须绿（修复后通过）
  6. files       交付文件齐全（轨迹 jsonl、BUG_REPRO、collection.json）
  7. fields      collection.json 必填字段齐全（bugfix: verify_cmds/verify_result；
                 diagnosis: verify_cmds/gold_root_cause/verify_result）
  8. evidence    verify_result 结构正确、URL 可访问、session_id 匹配
  9. trajectory_guard  正式轨迹未接触测试/外部路径；bugfix 绿灯后全量回归通过
 10. diagnosis   diagnosis 题 env 与埋错基线零差异（全程零代码改动）
 11. coverage    verify_cmds 为单包、单测试、-count=1，且红灯失败测试真实存在
 12. difficulty  运行时机制、跨层触发、题面症状覆盖和逐文件回退证据齐全
 13. domain      项目名称与交付字段未命中禁止项目/功能点；仍须人工语义审查
 14. repository  orphan 分支拓扑、G1/G2/R1 文件树、快照与远程分支安全

用法:
  post_qc.py --root <root> [--date YYYY-MM-DD] [--project <name>__<record>] [--go-version 1.22]

verify_cmds 必须来自 collection.json，并与红灯证据轨迹中实际执行的唯一 Bash 命令、
最终回复里的【命令】逐字一致；bugfix 题的绿灯轨迹同样必须逐字一致。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trajectory_guard import inject_evaluator, is_test_artifact, private_test_issues, source_manifest  # noqa: E402


_REMOTE_CACHE: dict[str, tuple[int, str, str]] = {}
_REMOTE_CACHE_LOCK = threading.Lock()
_REMOTE_KEY_LOCKS: dict[str, threading.Lock] = {}


def _remote_heads(repo: Path) -> tuple[int, str, str]:
    key = str(repo.resolve())
    with _REMOTE_CACHE_LOCK:
        key_lock = _REMOTE_KEY_LOCKS.setdefault(key, threading.Lock())
    with key_lock:
        with _REMOTE_CACHE_LOCK:
            cached = _REMOTE_CACHE.get(key)
        if cached is None:
            result = _git(repo, "ls-remote", "--heads", "origin", check=False)
            cached = (result.returncode, result.stdout, result.stderr)
            with _REMOTE_CACHE_LOCK:
                _REMOTE_CACHE[key] = cached
    return cached


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


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result


def _git_tree(repo: Path, revision: str, tests: bool) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in _git(repo, "ls-tree", "-r", revision).stdout.splitlines():
        meta, path = line.split("\t", 1)
        if is_test_artifact(path) == tests:
            result[path] = meta.split()[2]
    return result


def repository_delivery_ok(proj: Path, coll: dict, task_type: str) -> tuple[bool, str]:
    meta_path = proj / "_evidence" / "repository_delivery.json"
    manifest_path = proj / "_delivery" / "g1_snapshot.json"
    if not meta_path.is_file() or not manifest_path.is_file():
        return False, "缺 repository_delivery.json 或 g1_snapshot.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"交付元数据无效: {exc}"

    repo_url = coll.get("repo_url") or ""
    match = re.match(r"https?://[^/]+/[^/]+/([^/]+)/tree/([^/?#]+)$", repo_url)
    if not match:
        return False, "repo_url 必须是 green 分支地址"
    repo_name, branch = match.groups()
    record = proj.name.rsplit("__", 1)[-1]
    green, red = f"bug{record}_green", f"bug{record}_red"
    if branch != green or meta.get("green_branch") != green or meta.get("red_branch") != red:
        return False, f"分支命名不符合 {green}/{red}"
    repo = proj.parents[1] / "_repos" / repo_name
    if not (repo / ".git").is_dir():
        return False, f"找不到本地 staging repo: {repo}"

    issues: list[str] = []
    remote_rc, remote_stdout, _remote_stderr = _remote_heads(repo)
    if remote_rc != 0:
        issues.append("无法读取远程分支")
        remote_refs = {}
    else:
        remote_refs = {
            line.split()[1].split("refs/heads/", 1)[1]: line.split()[0]
            for line in remote_stdout.splitlines() if "refs/heads/" in line
        }
        remote_branches = sorted(remote_refs)
        illegal = [name for name in remote_branches if not re.fullmatch(r"bug\d{3}_(?:green|red)", name)]
        if illegal:
            issues.append("远程存在非 orphan 交付命名分支: " + ", ".join(illegal))

    if _git(repo, "rev-parse", "--verify", green, check=False).returncode != 0:
        return False, f"缺少 green 分支 {green}"
    green_count = int(_git(repo, "rev-list", "--count", green).stdout.strip())
    g2 = _git(repo, "rev-parse", green).stdout.strip()
    if remote_refs.get(green) != g2:
        issues.append("repo_url 指向的远程 green 未同步到本地验收提交")
    if task_type == "diagnosis":
        g1 = g2
        if green_count != 1:
            issues.append("diagnosis green 必须只有 G1 单提交")
        if _git(repo, "rev-parse", "--verify", red, check=False).returncode == 0:
            issues.append("diagnosis 不应创建 R1")
    else:
        if green_count != 2:
            issues.append("bugfix green 必须恰好是 G1 -> G2 两个提交")
        g1 = _git(repo, "rev-parse", f"{green}^").stdout.strip() if green_count >= 2 else ""
        if _git(repo, "rev-parse", "--verify", red, check=False).returncode != 0:
            issues.append(f"缺少 red 分支 {red}")
        else:
            r1 = _git(repo, "rev-parse", red).stdout.strip()
            if remote_refs.get(red) != r1:
                issues.append("远程 red 未同步到本地 R1")
            if _git(repo, "rev-list", "--count", red).stdout.strip() != "1":
                issues.append("R1 必须为 orphan 单提交")
            if _git(repo, "merge-base", green, red, check=False).returncode == 0:
                issues.append("green/red 分支存在共同祖先")
            if g1 and _git_tree(repo, g1, False) != _git_tree(repo, r1, False):
                issues.append("R1 与 G1 的非测试文件不一致")
            if _git_tree(repo, g2, True) != _git_tree(repo, r1, True):
                issues.append("G2 与 R1 的验收测试不一致")
            if not _git_tree(repo, g2, True):
                issues.append("G2/R1 没有验收测试")
            if meta.get("g2_commit") != g2 or meta.get("r1_commit") != r1:
                issues.append("G2/R1 commit 与交付元数据不一致")

    if g1:
        if _git_tree(repo, g1, True):
            issues.append("G1 包含测试文件或测试夹具")
        if meta.get("g1_commit") != g1 or manifest.get("commit") != g1:
            issues.append("G1 commit 与交付元数据不一致")
        archive = subprocess.run(["git", "archive", "--format=tar", g1], cwd=repo, capture_output=True)
        if archive.returncode != 0:
            issues.append("无法导出 G1 进行快照复核")
        else:
            with tempfile.TemporaryDirectory(prefix="go-annotation-g1-qc-") as tmp:
                with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tf:
                    members = tf.getmembers()
                    unsafe = [
                        member.name for member in members
                        if member.issym() or member.islnk() or Path(member.name).is_absolute() or ".." in Path(member.name).parts
                    ]
                    if unsafe:
                        issues.append("G1 包含不安全链接/归档路径: " + ", ".join(unsafe[:8]))
                    else:
                        tf.extractall(tmp)
                if not unsafe and source_manifest(Path(tmp)) != manifest.get("files"):
                    issues.append("G1 实际文件树与 g1_snapshot.json 不一致")

    if meta.get("repo_url") != repo_url:
        issues.append("collection.repo_url 与交付元数据不一致")
    if task_type == "bugfix":
        if meta.get("state") != "finalized" or meta.get("session_id") != (coll.get("session_id") or ""):
            issues.append("G2/R1 交付状态未绑定本条正式轨迹 session")
        guard_path = proj / "_evidence" / "trajectory_guard.json"
        if guard_path.exists() and meta.get("finalized_at"):
            try:
                completed = datetime.fromisoformat(json.loads(guard_path.read_text(encoding="utf-8"))["completed_at"])
                finalized = datetime.fromisoformat(meta["finalized_at"])
                if finalized < completed:
                    issues.append("G2/R1 在正式轨迹完成前已创建")
            except Exception as exc:
                issues.append(f"无法核对轨迹/finalize 时序: {exc}")
    return not issues, "；".join(issues) or "orphan 拓扑、G1/G2/R1 文件树与快照均通过"


def check_record(proj: Path, go_ver: str, args) -> list[tuple[str, bool, str]]:
    coll = read_collection(proj)
    schema = int(coll.get("pipeline_schema") or 0)
    task_type = (coll.get("task_type") or "").strip() or "bugfix"
    verify_cmds = coll.get("verify_cmds") or ""
    env = go_env(go_ver)
    results: list[tuple[str, bool, str]] = []

    status = json.loads((proj / "status.json").read_text(encoding="utf-8")) if (proj / "status.json").exists() else {}
    pipeline = status.get("pipeline") if isinstance(status.get("pipeline"), dict) else {}
    if pipeline:
        preflight_path = proj / "_evidence" / "preflight.json"
        preflight_ok = False
        preflight_msg = "缺 _evidence/preflight.json"
        try:
            preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
            pre_checks = preflight.get("checks") if isinstance(preflight.get("checks"), dict) else {}
            red_cal = pre_checks.get("red_calibration") or {}
            green_cal = pre_checks.get("green_calibration") or {}
            ablation = pre_checks.get("repair_ablation") or {}
            preflight_ok = (
                preflight.get("result") == "passed"
                and red_cal.get("passed") is True and red_cal.get("completed", 0) >= 20
                and green_cal.get("passed") is True and green_cal.get("completed", 0) >= 20
                and (pre_checks.get("target_assertion_reached") or {}).get("passed") is True
                and (task_type != "bugfix" or ablation.get("passed") is True)
            )
            preflight_msg = "20/20 红绿、目标断言和回退证据通过" if preflight_ok else "批次预检证据不完整"
        except Exception as exc:
            preflight_msg = f"批次预检证据无效: {exc}"
        results.append(("preflight", preflight_ok, preflight_msg))

    env_dir = proj / "env"
    gold_dir = proj.parents[1] / "_gold" / proj.name
    if not gold_dir.exists():
        results.append(("build", False, "找不到 _gold 目录"))
        return results

    base = proj / ".base_snapshot"
    evaluator = proj / "evaluator"
    privacy_issues = private_test_issues(env_dir, evaluator, verify_cmds)
    if base.exists():
        privacy_issues.extend(private_test_issues(base, evaluator, verify_cmds))
    results.append(("privacy", not privacy_issues, "；".join(privacy_issues) or "目标测试只存在私有 evaluator"))

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
    with tempfile.TemporaryDirectory(prefix="go-annotation-post-qc-") as tmp:
        tmp_root = Path(tmp)
        red_dir = tmp_root / "red"
        green_dir = tmp_root / "green"
        shutil.copytree(buggy, red_dir)
        if evaluator.is_dir():
            inject_evaluator(evaluator, red_dir)
        if red_cmd:
            red_rc, red_out = run(red_cmd, red_dir, env)
            red_ok = red_rc != 0
            results.append(("red", red_ok, f"exit={red_rc}" + ("" if red_ok else "（基线竟然绿了）")))
        else:
            red_rc, red_out = 0, ""
            results.append(("red", False, "缺少定向复现命令"))

        if task_type == "bugfix":
            if verify_cmds:
                shutil.copytree(gold_dir, green_dir)
                if evaluator.is_dir():
                    inject_evaluator(evaluator, green_dir)
                green_rc, _ = run(verify_cmds, green_dir, env)
                green_ok = green_rc == 0
                results.append(("green", green_ok, f"exit={green_rc}"))
            else:
                results.append(("green", False, "缺 verify_cmds"))
        else:
            results.append(("green", True, "n/a（diagnosis 仅要求红灯证据）"))

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
    guard_path = proj / "_evidence" / "trajectory_guard.json"
    acceptance_path = proj / "_evidence" / "trajectory_acceptance.json"
    regression_path = proj / "_evidence" / "green_regression.json"
    guard_ok = False
    guard_msg = "缺 trajectory_guard.json"
    if guard_path.exists():
        try:
            guard = json.loads(guard_path.read_text(encoding="utf-8"))
            classification = guard.get("classification", "clean")
            guard_ok = (
                guard.get("result") == "passed"
                and classification == "clean"
                and guard.get("tests_visible") is False
                and (not sid or guard.get("session_id") == sid)
            )
            if classification == "suspect":
                review_path = proj / "_evidence" / "trajectory_review.json"
                if review_path.exists():
                    review = json.loads(review_path.read_text(encoding="utf-8"))
                    guard_ok = (
                        review.get("decision") == "approved"
                        and review.get("session_id") == guard.get("session_id")
                        and len((review.get("reason") or "").strip()) >= 20
                        and guard.get("tests_visible") is False
                        and (not sid or guard.get("session_id") == sid)
                    )
            guard_msg = "ok" if guard_ok else "轨迹守卫状态或 session_id 无效"
        except Exception as exc:
            guard_msg = f"轨迹守卫 JSON 无效: {exc}"
    if task_type == "bugfix":
        regression_ok = False
        if regression_path.exists():
            try:
                regression_ok = json.loads(regression_path.read_text(encoding="utf-8")).get("result") == "passed"
            except Exception:
                regression_ok = False
        guard_ok = guard_ok and regression_ok
        if not regression_ok:
            guard_msg += "；缺少或未通过绿灯后全量回归"
    if schema >= 2 or acceptance_path.exists():
        acceptance_ok = False
        try:
            acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
            checks = acceptance.get("checks") if isinstance(acceptance.get("checks"), dict) else {}
            required = {"trajectory_analysis", "regression", "task_semantics"}
            if task_type == "bugfix":
                required.add("private_verify")
            elif pipeline:
                required.add("diagnosis_root_cause")
            acceptance_ok = (
                acceptance.get("result") == "passed"
                and acceptance.get("session_id") == sid
                and required.issubset(checks)
                and all(checks[name].get("passed") is True for name in required)
            )
        except Exception as exc:
            guard_msg += f"；自动验收证据无效: {exc}"
        guard_ok = guard_ok and acceptance_ok
        if not acceptance_ok:
            guard_msg += "；缺少或未通过自动轨迹验收"
    results.append(("trajectory_guard", guard_ok, guard_msg))

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
        validate_delivery_field_wording,
        validate_success_criteria,
        validate_verify_cmds,
    )
    require_race = str(coll.get("bug_category") or "").strip() == CONCURRENCY_CATEGORY
    verify_issues = validate_verify_cmds(verify_cmds, require_race=require_race)
    verify_issues.extend(validate_concurrency_metadata(coll))
    verify_issues.extend(validate_success_criteria(coll))
    verify_issues.extend(validate_delivery_field_wording(coll))
    prompt_file = proj / "prompt.txt"
    if prompt_file.exists():
        verify_issues.extend(validate_delivery_field_wording({
            "user_query": prompt_file.read_text(encoding="utf-8").strip(),
        }))
    ft = fail_tests(red_out)
    known = project_tests(env_dir) | project_tests(gold_dir) | project_tests(evaluator)
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
    contract_path = proj / "contract_coverage.json"
    contract_checked = schema >= 2 or contract_path.exists()
    if contract_checked:
        from contract_coverage import validate_manifest
        contract_ok, contract_issues = validate_manifest(proj)
        if not contract_ok:
            coverage_errors.extend(contract_issues)
    coverage_message = (
        "命令形态、失败测试与 evaluator 契约覆盖已校验"
        if contract_checked else "命令形态与失败测试已校验（旧 schema 兼容，无契约清单）"
    )
    results.append(("coverage", not coverage_errors, "；".join(coverage_errors) or coverage_message))

    # 10. 难度审查：机器校验证据完整性，机制真实性仍由 reviewer_notes 的人工审查负责
    from difficulty_review import validate_review
    difficulty_ok, difficulty_issues = validate_review(proj, task_type)
    results.append((
        "difficulty",
        difficulty_ok,
        "；".join(difficulty_issues) if difficulty_issues else "难度审查证据齐全",
    ))

    # 11. 禁止项目/功能点：机器拦截明确关键词，语义等价项仍须人工审查。
    from domain_guard import validate_collection_domains, validate_project_domain
    domain_issues = validate_collection_domains(coll)
    if prompt_file.exists():
        domain_issues.extend(validate_collection_domains({
            "user_query": prompt_file.read_text(encoding="utf-8").strip(),
        }))
    domain_issues.extend(validate_project_domain(env_dir, proj.name.rsplit("__", 1)[0]))
    results.append((
        "domain",
        not domain_issues,
        "；".join(domain_issues) if domain_issues else "关键词门禁通过；仍须人工确认项目与功能点语义不在禁区",
    ))

    repository_ok, repository_msg = repository_delivery_ok(proj, coll, task_type)
    results.append(("repository", repository_ok, repository_msg))

    return results


def main():
    ap = argparse.ArgumentParser(description="后置质检")
    ap.add_argument("--root", default=".")
    ap.add_argument("--date")
    ap.add_argument("--project")
    ap.add_argument("--go-version", help="强制 Go 主版本（如 1.22），缺省从 go.mod 读取")
    ap.add_argument("--verify-cmds", help=argparse.SUPPRESS)
    ap.add_argument("--workers", type=int, default=3, help="本地记录并发数（默认 3）")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    projects = find_projects(root, args.date, args.project)
    if not projects:
        print("（没有找到任何记录）")
        sys.exit(1)

    print(f"后置质检：共 {len(projects)} 条记录\n")
    def inspect(proj: Path):
        go_ver = args.go_version or detect_go_version(proj / "env") or detect_go_version(proj)
        coll = read_collection(proj)
        task_type = (coll.get("task_type") or "bugfix").strip()
        return proj, task_type, check_record(proj, go_ver, args)

    workers = max(1, min(args.workers, len(projects)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="post-qc") as pool:
        inspected = list(pool.map(inspect, projects))

    all_ok = True
    for proj, task_type, results in inspected:
        print(f"== {proj.name}  ({task_type}) ==")
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
