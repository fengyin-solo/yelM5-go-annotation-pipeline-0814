#!/usr/bin/env python3
"""生成并上传红(pre_fix)/绿(post_fix)证据轨迹，并回填 verify_result。

执行顺序（红线）——红灯是门禁、绿灯是验收，两阶段分开跑：
1. 埋错 + 发布之后、第 7 步跑修复轨迹**之前**：`generate --phase red`
   红灯在埋错基线上确认 bug 真实可复现；不通过 → 重新埋错，不得进入跑轨迹。
2. 修复轨迹 + 第 8 步质检通过**之后**：`generate --phase green`（仅 bugfix）
   绿灯在测试模型修复后的 env/ 上确认修复成果有效，并上传两条、回填 verify_result。
   diagnosis 在红灯阶段即完成上传与回填（仅 pre_fix）。
   缺省 --phase auto：没跑过红灯就跑红灯，跑过就进入绿灯阶段。

task_type 行为：
- bugfix：红灯（门禁）+ 绿灯（验收）两条（pre_fix + post_fix）。
- diagnosis：只生成红灯一条（仅 pre_fix，不出现 post_fix）。

模型（红线）：红/绿都调用 Claude Code CLI（目标模型），只验证、不改代码；不再生成 gold 修复轨迹。

轨迹文件（红线）：交付的 verify_red.jsonl / verify_green.jsonl 都是 Claude Code 自己落盘的
原始 session 文件（~/.claude/projects/<目录>/<session_id>.jsonl），不用 stdout 捕获拼装的
stream-json；stream 捕获另存为 *.stream.jsonl 仅供排查。

验收标准（统一，硬门禁）：
- 红灯（pre_fix）：测试模型在埋错基线上运行验证命令，结论必须含「BUG 存在」，
  且 env 与基线零差异（没动代码）。不满足 → 需要回滚重新埋错（红）。
- 绿灯（post_fix，仅 bugfix）：测试模型在**修复轨迹改好的 env/**（即测试模型自己的修复成果）
  上运行 verify_cmds，结论必须含「已修复」，且验证环境与 env/ 零差异（验证时没动代码）。
  不满足 → 说明该修复轨迹实际无效，回滚重跑第 7 步修复轨迹并重新质检。
  注意：绿灯证明的是测试模型的修复成果，不是 _gold；因此必须在修复轨迹 + 质检之后执行。

串行：red / green / 修复轨迹共用同一把全局锁（~/.codex/go-annotation-pipeline/test_model.lock），
因为测试模型限流，全流程必须串行。

回填：上传后把下面 JSON 写入 collection.json 的 verify_result 字段：
  bugfix    -> {"pre_fix": {...red...}, "post_fix": {...green...}}
  diagnosis -> {"pre_fix": {...red...}}

用法:
  run_evidence_trajectories.py generate --root . --project <name>__<record> \
      [--date YYYY-MM-DD] [--claude claude] [--verify-cmds <red复现命令>] \
      [--timeout 1800] [--cookie <sid>] [--skip-upload] [--lock-timeout 0]
  run_evidence_trajectories.py validate --root . --project <name>__<record> [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from workspace import find_project          # noqa: E402
from upload_trajectory import upload_file, get_cookie  # noqa: E402
from serial_lock import test_model_lock     # noqa: E402
from run_trajectory import find_native_transcript, archive_files  # noqa: E402
from trajectory_guard import inject_evaluator, private_test_issues  # noqa: E402

RED_RESULT = "red"
GREEN_RESULT = "green"


def _load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _default_claude() -> str:
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    cfg = Path.home() / ".codex" / "go-annotation-pipeline" / "config.json"
    if cfg.exists():
        try:
            return _load_json(cfg).get("claude_bin") or "claude"
        except Exception:
            pass
    return "claude"


def _declared_go(collection: dict, env: Path) -> str:
    """从 collection.go_version 或 go.mod 取 go 主版本（如 1.22）。"""
    gv = collection.get("go_version") or ""
    m = re.search(r"go\.mod:\s*go\s+([0-9]+\.[0-9]+)", gv)
    if m:
        return m.group(1)
    for mod in [env / "go.mod", env / "backend" / "go.mod"]:
        if mod.exists():
            txt = mod.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"^go\s+([0-9]+\.[0-9]+)", txt, re.M)
            if m:
                return m.group(1)
    return ""


def _go_env(go_version: str) -> dict:
    env = os.environ.copy()
    if go_version:
        env["GOTOOLCHAIN"] = f"go{go_version}.0"
    return env


def _extract_session_from_text(text: str) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "result":
            return (e.get("session_id") or "").strip()
        # Claude Code 原始 session 文件：每行带 sessionId 字段（无 result 事件）
        sid = (e.get("sessionId") or "").strip() if isinstance(e.get("sessionId"), str) else ""
        if sid:
            return sid
    return ""


def _extract_session_from_file(path: Path) -> str:
    if not path.exists():
        return ""
    return _extract_session_from_text(path.read_text(encoding="utf-8", errors="ignore"))


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _make_verify_prompt(verify_cmds: str, mode: str) -> str:
    if mode == "red":
        return f"""这是一个 Go 项目的复现确认任务。你现在只做【验证】，严禁修改任何代码、严禁尝试修复、严禁创建/删除/重命名文件。

请只执行下面这一条命令（等它完整跑完，不要提前中断、不要重复运行）。Bash 工具的 command 字段必须逐字符等于这一行，禁止添加 echo、2>&1、tee、pwd、ls、cd 包裹或任何其它字符：
{verify_cmds}

运行结束后，基于退出码和输出如实判断问题是否确实存在，并摘出最能说明问题的关键报错。

最后只输出以下格式，不要写修复建议：
【结论】BUG 存在 / 不存在
【命令】{verify_cmds}
【执行结果】...
【关键失败信息】
- ...
【是否改动代码】否"""
    return f"""这是一个 Go 项目的目标 Bug 修复确认任务。你现在只做【验证】，严禁修改任何代码、严禁创建/删除/重命名文件。

请只执行下面这一条命令（只运行一次，不要重复运行、不要用 -v 重跑、不要抓取额外日志）。Bash 工具的 command 字段必须逐字符等于这一行，禁止添加 echo、2>&1、tee、pwd、ls、cd 包裹或任何其它字符：
{verify_cmds}

运行结束后，基于退出码和目标测试的 PASS/FAIL 直接给结论。

最后只输出以下格式，不要写其它内容：
【结论】已修复 / 未修复
【命令】{verify_cmds}
【执行结果】目标测试通过 / 失败
【是否改动代码】否"""


def _run_claude_verify(env_dir: Path, prompt: str, out: Path, claude_bin: str, go_env: dict, timeout: int) -> int:
    stdin_data = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
    }, ensure_ascii=False) + "\n"
    cmd = [claude_bin, "-p", "--input-format", "stream-json", "--replay-user-messages",
           "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose",
           "--disallowedTools", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
           "Bash(git clone *)", "Bash(curl *)", "Bash(wget *)"]
    with open(out, "w", encoding="utf-8") as fo, open(str(out) + ".log", "w", encoding="utf-8") as fe:
        proc = subprocess.run(cmd, cwd=str(env_dir), stdout=fo, stderr=fe,
                              input=stdin_data, text=True, timeout=timeout, env=go_env)
    return proc.returncode


def _diff_clean(a: Path, b: Path) -> list[str]:
    r = _run(["diff", "-rq", str(a), str(b)])
    if r.returncode == 0:
        return []
    return [ln for ln in (r.stdout + "\n" + r.stderr).splitlines() if ln.strip()]


def _final_text(path: Path) -> str:
    if not path.exists():
        return ""
    texts = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "assistant":
            content = e.get("message", {}).get("content") or []
            for it in content if isinstance(content, list) else []:
                if isinstance(it, dict) and it.get("type") == "text" and (it.get("text") or "").strip():
                    texts.append(it["text"])
        if e.get("type") == "item.completed":
            it = e.get("item", {})
            if it.get("type") == "agent_message" and (it.get("text") or "").strip():
                texts.append(it["text"])
    return texts[-1] if texts else ""


def _executed_bash_commands(path: Path) -> list[str]:
    """提取原始轨迹中实际发出的 Bash 命令，保留原字符串，不做规范化。"""
    commands: list[str] = []
    if not path.exists():
        return commands
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
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


def _exact_command_issue(path: Path, expected: str) -> str:
    actual = _executed_bash_commands(path)
    if actual == [expected]:
        return ""
    return f"实际执行命令与 verify_cmds 不完全一致: expected={expected!r}; actual={actual!r}"


def _reported_command_issue(text: str, expected: str) -> str:
    """最终回复的【命令】也必须逐字符等于 verify_cmds。"""
    reported = re.findall(r"(?:^|\n)【命令】([^\n]*)", text)
    if reported == [expected]:
        return ""
    return f"最终回复【命令】与 verify_cmds 不完全一致: expected={expected!r}; reported={reported!r}"


def _prepare_env(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    r = _run(["rsync", "-a", "--delete", str(src).rstrip("/") + "/", str(dst).rstrip("/") + "/"])
    if r.returncode != 0:
        raise RuntimeError(f"rsync 失败: {r.stderr[:300]}")


def _run_full_regression(env_dir: Path, go_env: dict) -> tuple[int, str]:
    module_dir = env_dir
    if not (module_dir / "go.mod").exists():
        candidates = sorted(env_dir.glob("*/go.mod"))
        if len(candidates) == 1:
            module_dir = candidates[0].parent
    result = _run(["go", "test", "./..."], cwd=str(module_dir), env=go_env)
    return result.returncode, (result.stdout + result.stderr)


def _pass_red(text: str, changed: list[str]) -> bool:
    return ("BUG 存在" in text) and ("BUG 不存在" not in text) and (not changed)


def _pass_green(text: str, changed: list[str]) -> bool:
    return ("已修复" in text) and ("未修复" not in text) and (not changed)


def _best_url(res: dict) -> str:
    return (res.get("directUrl") or res.get("url") or "").strip()


def _check_url_accessible(url: str) -> str:
    """返回 HTTP 状态码字符串；网络失败返回空串。"""
    r = _run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-L", "--max-time", "30", url])
    return r.stdout.strip()


def _session_id_from_url(url: str) -> str:
    r = _run(["curl", "-sS", "-L", "--max-time", "60", url])
    return _extract_session_from_text(r.stdout or "")


def _validate_verify_result(obj, ev: Path, task_type: str = "bugfix") -> list[str]:
    """校验 verify_result JSON：结构、result 值、URL 可访问、session_id 匹配。

    bugfix 要求 pre_fix + post_fix；diagnosis 只要求 pre_fix（post_fix 不得出现）。
    """
    issues: list[str] = []
    if not isinstance(obj, dict):
        return ["verify_result 不是 JSON 对象"]
    pairs = [("pre_fix", RED_RESULT, "verify_red.jsonl")]
    if task_type != "diagnosis":
        pairs.append(("post_fix", GREEN_RESULT, "verify_green.jsonl"))
    for key, expected_result, local_name in pairs:
        if key not in obj:
            issues.append(f"缺少字段 {key}")
            continue
        item = obj[key]
        if not isinstance(item, dict):
            issues.append(f"{key} 不是对象")
            continue
        for field in ("trajectory_url", "session_id", "result"):
            if field not in item:
                issues.append(f"{key}.{field} 缺失")
        if item.get("result") != expected_result:
            issues.append(f"{key}.result 应为 {expected_result!r}，实际 {item.get('result')!r}")
        url = item.get("trajectory_url") or ""
        sid = (item.get("session_id") or "").strip()
        if url:
            if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
                issues.append(f"{key}.trajectory_url 不是合法 http(s) 地址: {url!r}")
            else:
                code = _check_url_accessible(url)
                if not code.startswith("2"):
                    issues.append(f"{key}.trajectory_url 不可访问（HTTP {code or '网络错误'}）: {url}")
        if sid:
            local = ev / local_name
            if local.exists():
                local_sid = _extract_session_from_file(local)
                if local_sid and local_sid != sid:
                    issues.append(f"{key}.session_id 与本地轨迹不一致: {sid!r} != {local_sid!r}")
            elif url:
                url_sid = _session_id_from_url(url)
                if url_sid and url_sid != sid:
                    issues.append(f"{key}.session_id 与 URL 轨迹不一致: {sid!r} != {url_sid!r}")
    if task_type == "diagnosis" and "post_fix" in obj:
        issues.append("diagnosis 只允许 pre_fix，不应出现 post_fix")
    return issues


def _build_verify_result(red_sid: str, green_sid: str, red_url: str, green_url: str, task_type: str = "bugfix") -> dict:
    obj = {
        "pre_fix": {"trajectory_url": red_url, "session_id": red_sid, "result": RED_RESULT},
    }
    if task_type != "diagnosis":
        obj["post_fix"] = {"trajectory_url": green_url, "session_id": green_sid, "result": GREEN_RESULT}
    return obj


def _snapshot_baseline(env: Path, snap: Path) -> None:
    """从未跑轨迹的 env/ 生成埋错基线快照（与 run_trajectory.py 的排除规则一致）。"""
    snap.mkdir(parents=True, exist_ok=True)
    r = _run(["rsync", "-a", "--delete",
              "--exclude=.git", "--exclude=trajectory*.jsonl", "--exclude=*.log",
              str(env).rstrip("/") + "/", str(snap).rstrip("/") + "/"])
    if r.returncode != 0:
        raise RuntimeError(f"rsync 快照失败: {r.stderr[:300]}")


def _run_verify_mode(mode: str, src: Path, env_dir: Path, out: Path, verify_cmds: str,
                     claude_bin: str, go_env: dict, timeout: int, proj_name: str,
                     evaluator: Path) -> str:
    """跑一种颜色的证据轨迹（最多重试 3 次）。通过返回 session_id，不通过返回空串。"""
    prompt = _make_verify_prompt(verify_cmds, mode)
    for attempt in range(1, 4):
        _prepare_env(src, env_dir)
        inject_evaluator(evaluator, env_dir)
        expected = env_dir.parent / f".{mode}_expected"
        _prepare_env(env_dir, expected)
        print(f"--- {mode}/验证 第 {attempt}/3 次（目标模型）: {proj_name} ---")
        rc = _run_claude_verify(env_dir, prompt, out, claude_bin, go_env, timeout)
        sid = _extract_session_from_file(out)
        text = _final_text(out)
        changed = _diff_clean(env_dir, expected)
        command_issue = _exact_command_issue(out, verify_cmds)
        reported_issue = _reported_command_issue(text, verify_cmds)
        ok = (not command_issue and not reported_issue and
              ((mode == "red" and _pass_red(text, changed)) or
               (mode == "green" and _pass_green(text, changed))))
        if rc != 0:
            print(f"    ❌ claude 退出码={rc}")
        if not sid:
            print("    ❌ 未提取到 session_id")
        if ok:
            # 交付文件 = Claude Code 原始 session 轨迹；stream 捕获另存为 *.stream.jsonl 备查。
            native = find_native_transcript(sid)
            if native is None:
                print(f"    ❌ 未找到 Claude Code 原始轨迹文件（session {sid}），无法交付原始轨迹。")
                print("       请确认 ~/.claude/projects/（或 CLAUDE_CONFIG_DIR/projects/）下存在该 session 的 jsonl。")
                sys.exit(2)
            stream_keep = out.with_name(out.stem + ".stream.jsonl")
            out.rename(stream_keep)
            shutil.copy2(native, out)
            native_issue = _exact_command_issue(out, verify_cmds)
            if native_issue:
                print(f"    ❌ {mode} 验收失败：原始 session {native_issue}")
                continue
            print(f"    ✅ {mode} 验收通过（结论正确、环境零改动）；已保存原始轨迹: {out.name}")
            shutil.rmtree(expected, ignore_errors=True)
            return sid
        if command_issue:
            print(f"    ❌ {mode} 验收失败：{command_issue}")
        elif reported_issue:
            print(f"    ❌ {mode} 验收失败：{reported_issue}")
        elif mode == "red":
            print(f"    ❌ 红灯验收失败：结论={'未识别' if ('BUG 存在' not in text and 'BUG 不存在' not in text) else ('BUG 不存在' if 'BUG 不存在' in text else 'BUG 存在')}，环境改动={len(changed)} 文件")
        else:
            print(f"    ❌ 绿灯验收失败：结论={'未识别' if ('已修复' not in text and '未修复' not in text) else ('未修复' if '未修复' in text else '已修复')}，环境改动={len(changed)} 文件")
        if changed:
            print("       环境改动示例: " + "; ".join(changed[:5]))
        shutil.rmtree(expected, ignore_errors=True)
    return ""


def _upload_evidence(path: Path, key: str, sid: str, cookie: str) -> str:
    try:
        res = upload_file(path, f"{key}_{sid}.jsonl", cookie)
        url = _best_url(res)
        print(f"✅ {key}: {url}")
        return url
    except Exception as e:
        print(f"❌ {key} 上传失败: {e}")
        sys.exit(1)


def _finalize_verify_result(obj: dict, ev: Path, coll: dict, coll_path: Path, root: Path, task_type: str) -> None:
    issues = _validate_verify_result(obj, ev, task_type)
    if issues:
        print("❌ verify_result 校验失败：")
        for i in issues:
            print("   -", i)
        sys.exit(1)
    # 回填 collection.json 的 verify_result（紧凑 JSON 字符串），并重建 xlsx。
    compact = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if coll_path.exists():
        coll["verify_result"] = compact
        coll_path.write_text(json.dumps(coll, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        print(f"⚠️  缺少 {coll_path}，仅写出 evidence 文件，未回填 collection.json。")
    (ev / "verify_result.json").write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✅ verify_result 已回填: {compact}")
    collection_table = Path(__file__).with_name("collection_table.py")
    if collection_table.exists() and coll_path.exists():
        subprocess.run([sys.executable, str(collection_table), "sync", "--root", str(root)])
    print(f"✅ verify_result.json 已写入: {ev / 'verify_result.json'}")


def cmd_generate(args):
    root = Path(args.root).resolve()
    proj = find_project(root, args.project, args.date)
    if not proj:
        print(f"❌ 未找到项目: {args.project}")
        sys.exit(1)
    coll_path = proj / "collection.json"
    coll = _load_json(coll_path) if coll_path.exists() else {}
    task_type = (coll.get("task_type") or "").strip() or "bugfix"
    if task_type not in ("bugfix", "diagnosis"):
        task_type = "bugfix"
    coll_verify_cmds = coll.get("verify_cmds") or ""
    override_verify_cmds = args.verify_cmds or ""
    if coll_verify_cmds and override_verify_cmds and coll_verify_cmds != override_verify_cmds:
        print("❌ --verify-cmds 与 collection.json 的 verify_cmds 不一致；正式证据轨迹必须使用收集表里的原始字符串。")
        print(f"   collection.json: {coll_verify_cmds!r}")
        print(f"   --verify-cmds:     {override_verify_cmds!r}")
        sys.exit(1)
    verify_cmds = override_verify_cmds or coll_verify_cmds
    from verify_cmds import CONCURRENCY_CATEGORY, validate_concurrency_metadata, validate_verify_cmds
    require_race = str(coll.get("bug_category") or "").strip() == CONCURRENCY_CATEGORY
    verify_issues = validate_verify_cmds(verify_cmds, require_race=require_race)
    verify_issues.extend(validate_concurrency_metadata(coll))
    if verify_issues:
        print("❌ verify_cmds 硬门禁：" + "；".join(verify_issues))
        sys.exit(1)

    evaluator = proj / "evaluator"
    private_issues = private_test_issues(proj / "env", evaluator, verify_cmds)
    if private_issues:
        print("❌ 私有测试门禁：")
        for issue in private_issues:
            print("   -", issue)
        sys.exit(1)

    ev = proj / "_evidence"
    ev.mkdir(parents=True, exist_ok=True)
    base_snap = proj / ".base_snapshot"
    red_out = ev / "verify_red.jsonl"
    green_out = ev / "verify_green.jsonl"
    red_state = ev / "red_result.json"

    # 阶段选择：红灯是跑修复轨迹前的门禁；绿灯在修复轨迹 + 质检之后验证测试模型的修复成果。
    phase = getattr(args, "phase", "auto") or "auto"
    if task_type == "diagnosis":
        if phase == "green":
            print("❌ diagnosis 只有红灯（pre_fix），没有绿灯阶段。")
            sys.exit(1)
        phase = "red"
    elif phase == "auto":
        phase = "green" if red_state.exists() else "red"

    go_env = _go_env(_declared_go(coll, base_snap if base_snap.exists() else proj / "env"))
    claude_bin = args.claude or _default_claude()
    timeout = args.timeout
    lock_timeout = args.lock_timeout

    if phase == "red":
        # 红灯门禁：在第 7 步跑修复轨迹之前执行，此时 env/ 就是埋错基线。
        env_dir = proj / "env"
        if not env_dir.exists():
            print(f"❌ 缺少 env 目录: {env_dir}")
            sys.exit(1)
        # 防呆：红灯已通过且 env 与基线有差异 → 疑似已跑过修复轨迹，红灯阶段不能再跑。
        if red_state.exists() and base_snap.exists() and _diff_clean(env_dir, base_snap):
            print("❌ 红灯已通过，且 env 与基线快照有差异——疑似已跑过修复轨迹。红灯阶段只能在跑轨迹之前执行。")
            print("   如确认是重新埋错后重跑红灯：先删除 _evidence/red_result.json，并确认 env/ 是新的埋错基线，再重跑本命令。")
            sys.exit(1)
        # 重跑红灯（如重新埋错后）：把上一轮红灯产物归档到 _failed_rounds/，不污染本轮。
        prev = [p for p in (red_out, red_out.with_name("verify_red.stream.jsonl"),
                            Path(str(red_out) + ".log"), red_state) if p.exists()]
        if prev:
            dest = archive_files(proj, prev, "red-retry")
            print(f"🗂  已把上一轮红灯产物归档到: {dest}")
        # 红灯前置于跑轨迹，env 即基线：快照按当前 env 重建，避免重新埋错后用到旧快照。
        print("📸 按当前 env/ 重建埋错基线快照 .base_snapshot（红灯应在跑修复轨迹之前执行，此时 env 即基线）")
        _snapshot_baseline(env_dir, base_snap)
        with test_model_lock(timeout=lock_timeout):
            red_sid = _run_verify_mode("red", base_snap, ev / "red_env", red_out,
                                       verify_cmds, claude_bin, go_env, timeout, proj.name,
                                       evaluator)
        if not red_sid:
            print("❌ 红灯不达标：bug 在基线上未按预期复现，或测试模型动了代码。")
            print("   请回滚 env 重新埋错（红）后重跑本命令；红灯通过之前不要进入第 7 步跑修复轨迹。")
            sys.exit(1)
        red_url = ""
        if not args.skip_upload:
            cookie = get_cookie(args)
            if not cookie:
                print("❌ 缺少 COS cookie（可用 --cookie 或配置），或加 --skip-upload 仅本地生成。")
                sys.exit(1)
            red_url = _upload_evidence(red_out, "verify_red", red_sid, cookie)
        red_state.write_text(json.dumps({"session_id": red_sid, "trajectory_url": red_url},
                                        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if task_type == "diagnosis":
            obj = _build_verify_result(red_sid, "", red_url, "", task_type)
            if args.skip_upload:
                (ev / "verify_result.json").write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print("已按 --skip-upload 跳过上传；verify_result.json 已生成（trajectory_url 为空，仅本地预览，未回填 collection.json）。")
                return
            _finalize_verify_result(obj, ev, coll, coll_path, root, task_type)
            return
        print("✅ 红灯门禁通过。请继续第 7 步跑修复轨迹；轨迹质检通过后，再跑本命令（--phase green 或缺省 auto）生成绿灯并回填 verify_result。")
        return

    # phase == green（仅 bugfix）：验证修复轨迹改好的 env/（测试模型自己的修复成果），不是 _gold。
    if not red_state.exists():
        print(f"❌ 缺少红灯结果 {red_state}：请先在跑修复轨迹之前执行红灯阶段（本命令 --phase red）。")
        sys.exit(1)
    red_info = _load_json(red_state)
    red_sid = (red_info.get("session_id") or "").strip()
    red_url = (red_info.get("trajectory_url") or "").strip()
    if not red_sid:
        print(f"❌ {red_state} 缺少 session_id，请重跑红灯阶段。")
        sys.exit(1)
    if not base_snap.exists():
        print(f"❌ 缺少 .base_snapshot: {base_snap}（红灯阶段或跑修复轨迹时会生成）")
        sys.exit(1)
    fixed_env = proj / "env"
    if not fixed_env.exists():
        print(f"❌ 缺少修复后的 env 目录: {fixed_env}")
        sys.exit(1)
    if not _diff_clean(fixed_env, base_snap):
        print("❌ env 与埋错基线零差异：没有测试模型的修复改动。")
        print("   绿灯验证的是测试模型的修复成果，请先跑第 7 步修复轨迹并通过质检，再生成绿灯。")
        sys.exit(1)

    # 重跑绿灯：把上一轮绿灯产物归档到 _failed_rounds/，不污染本轮。
    prev_green = [p for p in (green_out, green_out.with_name("verify_green.stream.jsonl"),
                              Path(str(green_out) + ".log"), ev / "green_regression.json") if p.exists()]
    if prev_green:
        dest = archive_files(proj, prev_green, "green-retry")
        print(f"🗂  已把上一轮绿灯产物归档到: {dest}")

    with test_model_lock(timeout=lock_timeout):
        green_sid = _run_verify_mode("green", fixed_env, ev / "green_env", green_out,
                                     verify_cmds, claude_bin, go_env, timeout, proj.name,
                                     evaluator)
    if not green_sid:
        print("❌ 绿灯不达标：测试模型修复后的 env 未让验收命令全绿，或验证时动了代码。")
        print("   这说明该修复轨迹实际无效（质检结论可能有误或测试 flaky），请回滚重跑第 7 步修复轨迹并重新质检，再重跑本命令。")
        sys.exit(1)

    regression_rc, regression_output = _run_full_regression(ev / "green_env", go_env)
    regression_state = ev / "green_regression.json"
    regression_state.write_text(json.dumps({
        "result": "passed" if regression_rc == 0 else "failed",
        "command": "go test ./...",
        "exit_code": regression_rc,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if regression_rc != 0:
        print("❌ 私有绿灯通过，但全量回归 go test ./... 失败：")
        print(regression_output[-1000:])
        sys.exit(1)
    print("✅ 私有绿灯后全量回归通过: go test ./...")

    if args.skip_upload:
        obj = _build_verify_result(red_sid, green_sid, red_url, "", task_type)
        (ev / "verify_result.json").write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("已按 --skip-upload 跳过上传；verify_result.json 已生成（部分 trajectory_url 为空，仅本地预览，未回填 collection.json）。")
        return

    cookie = get_cookie(args)
    if not cookie:
        print("❌ 缺少 COS cookie，跳过上传（可用 --cookie 或配置）。")
        sys.exit(1)
    if not red_url:
        # 红灯阶段曾 --skip-upload：此时补传红灯并更新状态文件。
        red_url = _upload_evidence(red_out, "verify_red", red_sid, cookie)
        red_state.write_text(json.dumps({"session_id": red_sid, "trajectory_url": red_url},
                                        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    green_url = _upload_evidence(green_out, "verify_green", green_sid, cookie)
    obj = _build_verify_result(red_sid, green_sid, red_url, green_url, task_type)
    _finalize_verify_result(obj, ev, coll, coll_path, root, task_type)


def cmd_validate(args):
    root = Path(args.root).resolve()
    proj = find_project(root, args.project, args.date)
    if not proj:
        print(f"❌ 未找到项目: {args.project}")
        sys.exit(1)
    ev = proj / "_evidence"
    obj = None
    coll_path = proj / "collection.json"
    task_type = "bugfix"
    if coll_path.exists():
        data = _load_json(coll_path)
        task_type = (data.get("task_type") or "").strip() or "bugfix"
        raw = data.get("verify_result") or ""
        if isinstance(raw, dict):
            obj = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                print(f"⚠️  collection.json 的 verify_result 不是合法 JSON，忽略。")
    if obj is None and (ev / "verify_result.json").exists():
        obj = _load_json(ev / "verify_result.json")

    if obj is None:
        print("❌ 未找到 verify_result（collection.json 或 _evidence/verify_result.json）。")
        sys.exit(1)

    issues = _validate_verify_result(obj, ev, task_type)
    if issues:
        print("❌ verify_result 校验未通过：")
        for i in issues:
            print("   -", i)
        sys.exit(1)
    print(f"✅ verify_result 校验通过（task_type={task_type}）：JSON 结构、result 值、URL 可访问、session_id 匹配均正常。")


def main():
    ap = argparse.ArgumentParser(description="生成并上传红/绿两条证据轨迹，回填 verify_result")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("generate", help="按 task_type 分阶段生成红/绿证据轨迹（红=跑轨迹前门禁，绿=质检后验收测试模型修复）、上传并回填 verify_result")
    c.add_argument("--root", default=".")
    c.add_argument("--project", required=True)
    c.add_argument("--date", help="记录日期，如 2026-08-16")
    c.add_argument("--phase", choices=["auto", "red", "green"], default="auto",
                   help="red=红灯门禁（跑修复轨迹前）；green=绿灯验收（质检通过后，仅 bugfix）；auto=按已有红灯结果自动判断")
    c.add_argument("--claude", help="Claude Code CLI 路径（目标模型）")
    c.add_argument("--verify-cmds", help="临时覆盖红灯复现命令；正式交付必须与 collection.json 的 verify_cmds 逐字一致")
    c.add_argument("--timeout", type=int, default=1800)
    c.add_argument("--cookie", help="COS 上传 cookie")
    c.add_argument("--skip-upload", action="store_true")
    c.add_argument("--lock-timeout", type=int, default=0, help="全局串行锁等待秒数；0 表示一直等")
    c.set_defaults(func=cmd_generate)

    c = sub.add_parser("validate", help="校验已有 verify_result（结构/URL 可访问/session_id 匹配）")
    c.add_argument("--root", default=".")
    c.add_argument("--project", required=True)
    c.add_argument("--date")
    c.set_defaults(func=cmd_validate)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
