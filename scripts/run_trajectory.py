#!/usr/bin/env python3
"""跑 Claude Code 轨迹 + 失败回滚重试。

目的：调用 Claude Code 生产轨迹时，若一次没有「成功结束处理」（进程非 0 退出、
被中断、超时、无 result 事件、result subtype 非 success、JSONL 截断），
自动把环境代码回滚到 base 干净态，再重新跑，直到成功或达到最大重试次数。

回滚方式（二选一，自动判断）:
  1. env 是 git 仓库（step 2 重建的单提交 base）→ `git reset --hard HEAD` + `git clean -fd`
  2. 指定 --snapshot <dir>（rsync 快照）→ `rsync -a --delete snapshot/ env/`

用法:
  run_trajectory.py snapshot --env <env_dir> --snapshot <dir>
  run_trajectory.py run --env <env_dir> --prompt <prompt.txt> --output <trajectory.jsonl> \
      [--snapshot <dir>] [--max-attempts 3] [--timeout 1800] [--claude claude]
  run_trajectory.py check --json <trajectory.jsonl>
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from serial_lock import test_model_lock  # noqa: E402
from contract_coverage import validate_manifest  # noqa: E402
from trajectory_acceptance import run_acceptance  # noqa: E402
from trajectory_guard import (  # noqa: E402
    copy_without_tests,
    private_test_issues,
    sync_business_back,
    test_manifest,
    source_manifest_issues,
    trajectory_policy_report,
)


def _default_claude() -> str:
    """claude 可执行文件：CLAUDE_BIN 环境变量 > configure.py 写入的 config > 默认 claude。"""
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    cfg_path = Path.home() / ".codex" / "go-annotation-pipeline" / "config.json"
    if cfg_path.exists():
        try:
            import json as _json
            cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
            if cfg.get("claude_bin"):
                return cfg["claude_bin"]
        except Exception:
            pass
    return "claude"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def detect_go_version(mod_path: Path | None) -> str | None:
    """从 go.mod 读取 `go X.Y` 主版本号；无 go.mod 返回 None。"""
    if not mod_path or not mod_path.exists():
        return None
    txt = mod_path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"^go\s+([0-9]+\.[0-9]+)", txt, re.M)
    return m.group(1) if m else None


def find_go_mod(env: Path, module_path: str | None) -> Path | None:
    """定位 go.mod：优先 --module-path，其次 env 根目录，再次一层子目录（如 backend/）。"""
    if module_path:
        cand = env / module_path / "go.mod"
        return cand if cand.exists() else None
    root = env / "go.mod"
    if root.exists():
        return root
    cands = sorted(env.glob("*/go.mod"))
    return cands[0] if cands else None


def host_go_major_minor(env: Path) -> str | None:
    """读本机 go 主版本（如 1.25）。"""
    r = run(["go", "version"], cwd=str(env))
    if r.returncode != 0:
        return None
    m = re.search(r"go(\d+)\.(\d+)(?:\.(\d+))?", r.stdout)
    return f"{m.group(1)}.{m.group(2)}" if m else None


def pin_go_env(env: Path, declared: str | None) -> dict:
    """返回传给 claude 子进程的环境，尽量把工具链钉到 go.mod 声明版本。

    - 本机 go 主版本 == 声明版本：直接用当前环境；
    - 不一致：尝试 GOTOOLCHAIN=go<声明>.0 自动切换（Go 1.21+ 支持，需要时自动下载）；
    - 切换失败：抛错，拒绝产出「声明 1.22 实际 1.25」的不一致轨迹。
    """
    if not declared:
        return os.environ.copy()
    host = host_go_major_minor(env)
    if host is None or host == declared:
        return os.environ.copy()
    toolchain = f"go{declared}.0"
    test_env = os.environ.copy()
    test_env["GOTOOLCHAIN"] = toolchain
    r = run(["go", "version"], cwd=str(env), env=test_env)
    if r.returncode == 0 and f"go{declared}" in r.stdout:
        print(f"   🔧 本机 go {host} != go.mod 声明 {declared}，已用 GOTOOLCHAIN={toolchain} 钉住")
        return test_env
    raise RuntimeError(
        f"本机 Go 是 {host}，与 go.mod 声明的 {declared} 不一致，且无法自动切换到 {toolchain}。"
        f"请安装 Go {declared}（或临时用 --no-pin-go 跳过，但会产出不一致轨迹）。"
    )


def _file_digest(d: Path) -> dict[str, str] | None:
    import hashlib
    if not d.exists():
        return None
    out = {}
    for fp in d.rglob("*"):
        if fp.is_file() and ".git" not in fp.parts:
            rel = fp.relative_to(d)
            if rel.suffix in (".jsonl", ".log"):
                continue
            try:
                out[str(rel)] = hashlib.sha256(fp.read_bytes()).hexdigest()
            except OSError:
                continue
    return out


def env_changed(env: Path, snapshot: Path | None) -> list[str] | None:
    """对比 env 与 base 快照，返回有变化的文件相对路径；无法判断返回 None。"""
    if snapshot and snapshot.exists():
        a, b = _file_digest(env), _file_digest(snapshot)
        if a is None or b is None:
            return None
        return sorted(rel for rel in set(a) | set(b) if a.get(rel) != b.get(rel))
    if (env / ".git").exists():
        r = run(["git", "-C", str(env), "status", "--porcelain"])
        if r.returncode == 0:
            return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    return None


def validate_task(env: Path, snapshot: Path | None, task_type: str) -> list[str]:
    """正式修复轨迹只校验改动语义，绝不在此阶段执行 verify_cmds。"""
    issues = []
    if task_type == "bugfix":
        changed = env_changed(env, snapshot)
        if not changed:
            issues.append("bugfix 没有业务文件改动")
    elif task_type == "diagnosis":
        changed = env_changed(env, snapshot)
        if changed:
            issues.append(f"diagnosis 动了代码（{len(changed)} 个文件变化）: {', '.join(changed[:5])}")
    return issues


def rollback(env: Path, snapshot: Path | None) -> str:
    """把 env 回滚到 base 干净态，返回采用的回滚方式。"""
    env.mkdir(parents=True, exist_ok=True)
    if snapshot and snapshot.exists():
        r = run(["rsync", "-a", "--delete", str(snapshot).rstrip("/") + "/", str(env).rstrip("/") + "/"])
        if r.returncode != 0:
            raise RuntimeError(f"rsync 回滚失败: {r.stderr[:300]}")
        return "rsync snapshot"
    if (env / ".git").exists():
        r1 = run(["git", "-C", str(env), "reset", "--hard", "HEAD"])
        r2 = run(["git", "-C", str(env), "clean", "-fd"])
        if r1.returncode != 0 or r2.returncode != 0:
            raise RuntimeError(f"git 回滚失败: {r1.stderr[:200]} {r2.stderr[:200]}")
        return "git reset --hard + clean -fd"
    raise RuntimeError("env 不是 git 仓库，也没有 --snapshot 快照，无法回滚")


def cmd_snapshot(args):
    env = Path(args.env)
    if not env.exists():
        print(f"❌ env 目录不存在: {env}")
        sys.exit(1)
    snap = Path(args.snapshot)
    snap.mkdir(parents=True, exist_ok=True)
    r = run(["rsync", "-a", "--delete",
             "--exclude=.git", "--exclude=trajectory*.jsonl", "--exclude=*.log",
             str(env).rstrip("/") + "/", str(snap).rstrip("/") + "/"])
    if r.returncode != 0:
        print(f"❌ 快照失败: {r.stderr[:300]}")
        sys.exit(1)
    print(f"✅ 已生成 base 快照: {snap}")


def check_success(path: Path, expected_user_text: str | None = None) -> tuple[bool, list[str]]:
    """判断一次 claude stream-json 是否「成功结束处理」。

    expected_user_text 传入时，额外校验轨迹内回放的唯一一条用户输入与 prompt 原文
    （strip 后）完全一致，防止轨迹与题面张冠李戴。
    """
    if not path.exists() or path.stat().st_size == 0:
        return False, ["轨迹文件不存在或为空"]
    text = path.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return False, ["轨迹文件为空"]
    reasons = []
    truncated = False
    try:
        json.loads(lines[-1])
    except json.JSONDecodeError:
        truncated = True
        reasons.append("末尾 JSON 不完整（进程被中断/超时）")
    events = []
    for ln in lines:
        try:
            events.append(json.loads(ln))
        except json.JSONDecodeError:
            truncated = True
    result = None
    user_texts: list[str] = []
    slash_inputs = []
    final_texts = []
    for e in events:
        if e.get("type") == "result":
            result = e
        elif e.get("type") == "user":
            content = e.get("message", {}).get("content")
            items = content if isinstance(content, list) else [{"type": "text", "text": content}]
            for it in items:
                if isinstance(it, dict) and it.get("type") == "text":
                    t = (it.get("text") or "").strip()
                    if re.match(r"^/[A-Za-z]", t):
                        slash_inputs.append(t[:80])
                        continue
                    if t and not t.startswith("<local-command") and not t.startswith("<command-"):
                        user_texts.append(t)
        elif e.get("type") == "assistant":
            content = e.get("message", {}).get("content")
            for it in content if isinstance(content, list) else []:
                if isinstance(it, dict) and it.get("type") == "text" and (it.get("text") or "").strip():
                    final_texts.append(it["text"])
    if result is None:
        reasons.append("无 result 事件（未正常结束）")
    elif result.get("subtype") != "success" or result.get("is_error", False):
        reasons.append(f"result subtype={result.get('subtype')} is_error={result.get('is_error')}")
    if truncated:
        reasons.append("JSONL 截断")
    if slash_inputs:
        reasons.append("出现斜杠命令输入（禁止，如 /model /status）: " + " | ".join(slash_inputs))
    if len(user_texts) != 1:
        reasons.append(f"真实用户输入条数异常: {len(user_texts)}（应恰好 1 条，即回放的 prompt）")
    elif expected_user_text is not None and user_texts[0] != (expected_user_text or "").strip():
        reasons.append("轨迹内回放的用户输入与 prompt.txt 原文不一致（疑似轨迹与题面不匹配）")
    if not final_texts:
        reasons.append("无最终 assistant 回复文本")
    ok = (not reasons)
    return ok, reasons


def extract_session_id(path: Path) -> str:
    """从轨迹 jsonl 的 result 事件里取 session_id，用于文件命名。"""
    if not path.exists():
        return ""
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "result":
            return (e.get("session_id") or "").strip()
    return ""


def find_native_transcript(session_id: str) -> Path | None:
    """定位 Claude Code 自己落盘的原始 session 轨迹文件。

    Claude Code 每次会话都把原始轨迹写到
    `~/.claude/projects/<按 cwd 转换的目录>/<session_id>.jsonl`（CLAUDE_CONFIG_DIR 可覆盖根目录）。
    交付的轨迹必须用这个原始文件；捕获 stdout 拼装的 stream-json 只用于运行时校验。
    """
    if not session_id:
        return None
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    cands = sorted((base / "projects").glob(f"*/{session_id}.jsonl"))
    return cands[0] if cands else None


def archive_files(project_dir: Path, files: list[Path], reason: str) -> Path | None:
    """把指定文件移入 <project>/_failed_rounds/<时间戳>-<reason>/，保持相对路径。"""
    files = [p for p in files if p.exists()]
    if not files:
        return None
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = project_dir / "_failed_rounds" / f"{ts}-{reason}"
    for p in files:
        try:
            rel = p.relative_to(project_dir)
        except ValueError:
            rel = Path(p.name)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        p.rename(target)
    return dest


def archive_previous_round(project_dir: Path, reason: str = "rerun") -> Path | None:
    """重跑修复轨迹前，把上一轮产物归档到 _failed_rounds/，避免污染下一轮。

    归档：上一轮主轨迹（<uuid>.jsonl / <uuid>.stream.jsonl）、trajectory.* 临时/失败/日志文件、
    绿灯产物（verify_green*，上一轮修复对应的绿灯已随轨迹一起作废）。
    保留：红灯证据（verify_red*、red_result.json，基线未变仍然有效）、基线快照、
    prompt.txt、collection.json、BUG_REPRO.md。
    """
    if not project_dir.exists():
        return None
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(\.stream)?\.jsonl$")
    candidates: list[Path] = []
    for p in project_dir.iterdir():
        if p.is_file() and (uuid_re.match(p.name) or p.name.startswith("trajectory.")):
            candidates.append(p)
    ev = project_dir / "_evidence"
    if ev.exists():
        for p in ev.iterdir():
            if p.is_file() and (p.name.startswith("verify_green") or p.name in {
                "trajectory_guard.json", "trajectory_acceptance.json", "green_regression.json",
            }):
                candidates.append(p)
        green_env = ev / "green_env"
        if green_env.exists():
            shutil.rmtree(green_env)  # 工作目录直接清理，不归档
    return archive_files(project_dir, candidates, reason)


def cmd_check(args):
    ok, reasons = check_success(Path(args.json))
    if ok:
        print("✅ 轨迹成功结束处理")
    else:
        print("❌ 轨迹未成功结束处理：")
        for r in reasons:
            print("   -", r)
        sys.exit(1)


def skill_leak_issues(env: Path, prompt_text: str) -> list[str]:
    """检查 env 和 prompt 是否暴露本技能/答案线索。

    测试模型只能看到题目和普通项目源码，不能看到 SKILL/AGENTS/CLAUDE/BUG_REPRO
    等技能文件，也不能从 prompt 里读到 repo_url 等交付字段名。
    """
    issues: list[str] = []
    env = env.resolve()
    if env.exists():
        for p in env.rglob("*"):
            try:
                rel = p.relative_to(env)
            except ValueError:
                continue
            lowered = str(rel).lower()
            if p.is_dir() and p.name == ".claude":
                issues.append(f"禁止目录: {rel}")
            if p.is_file() and p.name.lower() in {"skill.md", "agents.md", "claude.md", "bug_repro.md"}:
                issues.append(f"禁止文件: {rel}")
            if "go-annotation-pipeline" in lowered or "最新-go-annotation-pipeline" in lowered:
                issues.append(f"路径包含技能名: {rel}")

    low = (prompt_text or "").lower()
    for marker in [
        "go-annotation-pipeline", "最新-go-annotation-pipeline", "skill.md",
        "agents.md", "claude.md", "bug_repro.md", "repo_url",
    ]:
        if marker in low:
            issues.append(f"prompt 中包含技能/答案标记: {marker}")
    return issues


def cmd_run(args):
    env = Path(args.env).resolve()
    prompt = Path(args.prompt).resolve()
    out = Path(args.output).resolve()
    if not prompt.exists():
        print(f"❌ 题面不存在: {prompt}")
        sys.exit(1)
    prompt_text = prompt.read_text(encoding="utf-8").strip()
    if not prompt_text:
        print(f"❌ 题面为空: {prompt}")
        sys.exit(1)
    issues = skill_leak_issues(env, prompt_text)
    collection_path = out.parent / "collection.json"
    collection = {}
    if collection_path.exists():
        try:
            collection = json.loads(collection_path.read_text(encoding="utf-8"))
        except Exception as exc:
            issues.append(f"collection.json 无法解析: {exc}")
    verify_cmds = (getattr(args, "verify_cmds", "") or collection.get("verify_cmds") or "").strip()
    evaluator = Path(getattr(args, "evaluator", "") or out.parent / "evaluator").resolve()
    issues.extend(private_test_issues(env, evaluator, verify_cmds))
    contract_ok, contract_issues = validate_manifest(out.parent)
    if not contract_ok:
        issues.extend(f"验收契约覆盖: {issue}" for issue in contract_issues)
    if issues:
        print("❌ 检测到技能/答案泄露风险，拒绝跑轨迹：")
        for i in issues:
            print("  -", i)
        sys.exit(1)

    # 重跑时先把上一轮的轨迹/绿灯产物归档到 _failed_rounds/，不污染本轮；
    # 红灯证据与基线快照仍然有效，保留在原位。
    archived = archive_previous_round(out.parent, "rerun")
    if archived:
        print(f"🗂  已把上一轮轨迹/绿灯产物归档到: {archived}（红灯证据与基线快照保留）")

    # 红灯门禁：正式跑轨迹前必须有已通过的红灯证据（run_evidence_trajectories.py generate --phase red）。
    red_state = out.parent / "_evidence" / "red_result.json"
    if not red_state.exists():
        print("❌ 红灯门禁未通过：缺少 _evidence/red_result.json。")
        print("   请先运行 run_evidence_trajectories.py generate --phase red，让测试模型实测确认 bug 可复现，再跑修复轨迹。")
        sys.exit(1)

    # Go 版本钉死：轨迹必须跑在 go.mod 声明的版本上，避免「声明 1.22 实际 1.25」。
    go_mod = find_go_mod(env, getattr(args, "module_path", None))
    declared = args.go_version or detect_go_version(go_mod)
    if getattr(args, "no_pin_go", False):
        go_env = os.environ.copy()
        if declared and go_mod:
            print(f"   ⚠️ 已按 --no-pin-go 跳过 Go 版本钉死（go.mod 声明 {declared}）")
    else:
        try:
            go_env = pin_go_env(env, declared)
        except RuntimeError as e:
            print(f"❌ {e}")
            sys.exit(2)
    if go_mod and declared:
        print(f"   🎯 go.mod={go_mod.relative_to(env)} 声明 Go {declared}，轨迹将使用该工具链")

    snapshot = Path(args.snapshot) if args.snapshot else None
    # env 无 .git 且未显式给 snapshot 时，用 .base_snapshot 做失败回滚基线。
    # 快照已存在（红灯门禁阶段或上次运行创建）则必须复用、绝不覆盖：
    # 重跑时 env 里是上一轮模型的改动，覆盖会把污染代码拍成"基线"、真基线永久丢失。
    if snapshot is None and not (env / ".git").exists():
        snapshot = env.parent / ".base_snapshot"
        if snapshot.exists() and any(snapshot.iterdir()):
            print(f"📸 复用已有 base 快照: {snapshot}（不覆盖）")
        else:
            snapshot.mkdir(parents=True, exist_ok=True)
            r = run(["rsync", "-a", "--delete",
                     "--exclude=.git", "--exclude=trajectory*.jsonl", "--exclude=*.log",
                     str(env).rstrip("/") + "/", str(snapshot).rstrip("/") + "/"])
            if r.returncode != 0:
                print(f"❌ 自动创建 base 快照失败: {r.stderr[:300]}")
                sys.exit(2)
            print(f"📸 已自动创建 base 快照: {snapshot}")
    if snapshot is None:
        print("❌ 正式轨迹必须使用无 .git 的 env 和独立快照，不允许直接在 Git 工作树中运行")
        sys.exit(2)
    snapshot = snapshot.resolve()
    snapshot_issues = private_test_issues(snapshot, evaluator, verify_cmds)
    if snapshot_issues:
        print("❌ 基线快照仍含私有目标测试，拒绝跑轨迹：")
        for issue in snapshot_issues:
            print("   -", issue)
        sys.exit(1)
    g1_manifest = Path(args.g1_manifest) if args.g1_manifest else out.parent / "_delivery" / "g1_snapshot.json"
    manifest_issues = source_manifest_issues(snapshot, g1_manifest)
    if manifest_issues:
        print("❌ 基线快照不是已发布 G1 的模型可见文件树，拒绝跑轨迹：")
        for issue in manifest_issues:
            print("   -", issue)
        sys.exit(1)

    # 原始 env 先回到基线；正式轨迹在系统临时目录的无测试副本中运行。
    try:
        rollback(env, snapshot)
    except RuntimeError as exc:
        print(f"❌ env 回滚失败: {exc}")
        sys.exit(2)
    baseline_tests = test_manifest(env)
    task_type = (getattr(args, "task_type", None) or collection.get("task_type") or "").strip()
    if task_type not in {"bugfix", "diagnosis"}:
        print("❌ 正式轨迹必须在 collection.json 或 --task-type 中明确 bugfix/diagnosis")
        sys.exit(1)
    lock_timeout = getattr(args, "lock_timeout", 0)
    with tempfile.TemporaryDirectory(prefix="go-annotation-trajectory-") as temp_dir:
      temp_root = Path(temp_dir)
      work_env = temp_root / "workspace"
      work_snapshot = temp_root / "base"
      copy_without_tests(snapshot, work_snapshot)
      copy_without_tests(snapshot, work_env)
      print(f"🔒 修复轨迹将在无任何 *_test.go 的隔离副本中运行: {work_env}")

      hook_script = Path(__file__).with_name("pretool_workspace_guard.py").resolve()
      settings = temp_root / "claude-settings.json"
      hook_command = " ".join(shlex.quote(value) for value in (
          sys.executable, str(hook_script), "--workspace", str(work_env),
      ))
      settings.write_text(json.dumps({
          "hooks": {"PreToolUse": [{
              "matcher": "Bash|Read|Edit|Write|MultiEdit|NotebookEdit|Glob|Grep",
              "hooks": [{"type": "command", "command": hook_command}],
          }]},
      }, ensure_ascii=False, indent=2), encoding="utf-8")

      # 测试模型限流：red / green / 修复轨迹必须全局串行；整个重试循环持锁。
      with test_model_lock(timeout=lock_timeout):
       for attempt in range(1, args.max_attempts + 1):
        try:
            method = rollback(work_env, work_snapshot)
        except RuntimeError as e:
            print(f"❌ 第 {attempt} 次回滚失败: {e}")
            sys.exit(2)
        print(f"--- 第 {attempt}/{args.max_attempts} 次（回滚方式: {method}） ---")

        attempt_out = out if attempt == args.max_attempts else out.with_suffix(out.suffix + f".try{attempt}")
        log = out.with_suffix(".log")
        # 用 stream-json 输入 + --replay-user-messages 把 prompt 作为唯一 user 消息
        # 回放到轨迹里，保证轨迹文件里能看到题面原文（而不是只藏在 -p 参数里）。
        stdin_data = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": prompt_text}]},
        }, ensure_ascii=False) + "\n"

        cmd = [args.claude, "-p", "--input-format", "stream-json", "--replay-user-messages"]
        if not args.no_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if not args.quiet:
            cmd.append("--verbose")
        cmd += ["--output-format", "stream-json"]
        cmd += ["--settings", str(settings)]
        # 防作弊：默认禁用会让测试模型拿到上游修复 commit 的工具/命令
        if args.disallowed_tools:
            cmd += ["--disallowedTools"] + list(args.disallowed_tools)
        print(f"   $ {' '.join(cmd[:3])} ... > {attempt_out.name}")

        with open(attempt_out, "w", encoding="utf-8") as fo, open(log, "w", encoding="utf-8") as fe:
            try:
                proc = subprocess.run(cmd, cwd=str(work_env), stdout=fo, stderr=fe,
                                      input=stdin_data, text=True,
                                      timeout=args.timeout or None, env=go_env)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                rc = -9
        ok, reasons = check_success(attempt_out, expected_user_text=prompt_text)
        if rc != 0:
            print(f"   ❌ 进程退出码 {rc}，重试")
            ok = False
            reasons = [f"Claude 进程退出码 {rc}", *reasons]
        if ok and task_type:
            task_issues = validate_task(work_env, work_snapshot, task_type)
            if task_issues:
                print("   ❌ 任务结果校验失败: " + "; ".join(task_issues))
                ok = False
                reasons = task_issues
        sid = extract_session_id(attempt_out) if ok else ""
        if ok and not sid:
            ok = False
            reasons = ["stream 输出中未提取到 session_id"]
        if ok:
            print(f"   ✅ 成功结束处理")
            # 交付文件 = Claude Code 自己落盘的原始 session 轨迹；
            # stdout 捕获的 stream-json 只用于上面的成功校验，另存为 *.stream.jsonl 备查。
            native = find_native_transcript(sid)
            if native is None:
                print(f"❌ 未找到 Claude Code 原始轨迹文件（session {sid}）。")
                print("   请确认 ~/.claude/projects/（或 CLAUDE_CONFIG_DIR/projects/）下存在该 session 的 jsonl；")
                print("   交付必须用原始轨迹文件，不能用 stdout 捕获拼装的文件。")
                sys.exit(2)
            policy_report = trajectory_policy_report(native, work_env)
            if policy_report["classification"] == "cheat":
                print("   ❌ 轨迹守卫命中作弊证据: " + "; ".join(policy_report["cheat"]))
                ok = False
                reasons = policy_report["cheat"]
                sid = ""
            if not ok:
                if attempt_out != out:
                    keep = out.with_suffix(out.suffix + f".fail{attempt}")
                    shutil.copy2(attempt_out, keep)
                continue

            acceptance = run_acceptance(
                project=out.parent,
                workspace=work_env,
                snapshot=work_snapshot,
                transcript=native,
                session_id=sid,
                task_type=task_type,
                verify_cmds=verify_cmds,
                evaluator=evaluator,
                module_path=(
                    str(go_mod.parent.relative_to(env))
                    if go_mod and go_mod.parent != env else None
                ),
                env=go_env,
                timeout=min(args.timeout or 900, 900),
            )
            if acceptance["result"] != "passed":
                print("   ❌ 轨迹后独立验收失败，停止自动重试：")
                for check_name, check in acceptance["checks"].items():
                    if not check.get("passed"):
                        print(f"      - {check_name}: {check.get('output', '')[-1200:]}")
                print("   题面或修复未覆盖已知契约，必须先修正再发起新的正式轨迹。")
                sys.exit(1)

            sync_business_back(work_env, env)
            if test_manifest(env) != baseline_tests:
                rollback(env, snapshot)
                print("   ❌ 同步业务改动后测试文件与基线不一致，已回滚")
                sys.exit(2)

            final = out.parent / f"{sid}.jsonl"
            shutil.copy2(native, final)
            stream_keep = out.parent / f"{sid}.stream.jsonl"
            if attempt_out.resolve() != stream_keep.resolve():
                attempt_out.rename(stream_keep)
            if out.exists() and out.resolve() not in (final.resolve(), stream_keep.resolve()):
                out.unlink()
            print(f"✅ 轨迹已保存（Claude Code 原始 session 文件）: {final}")
            print(f"✅ 轨迹守卫分类: {policy_report['classification']}")
            evidence_dir = out.parent / "_evidence"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            (evidence_dir / "trajectory_guard.json").write_text(
                json.dumps({
                    "session_id": sid,
                    "result": "passed" if policy_report["classification"] == "clean" else "manual_review_required",
                    "classification": policy_report["classification"],
                    "cheat_evidence": policy_report["cheat"],
                    "suspect_evidence": policy_report["suspect"],
                    "model_created_tests": policy_report["model_created_tests"],
                    "blocked_attempts": policy_report.get("blocked_attempts", []),
                    "tests_visible": False,
                    "outside_workspace_access": False,
                    "g1_manifest": str(g1_manifest),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            collection["session_id"] = sid
            collection["pipeline_schema"] = max(int(collection.get("pipeline_schema") or 0), 2)
            collection_tmp = collection_path.with_suffix(".json.tmp")
            collection_tmp.write_text(json.dumps(collection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            collection_tmp.replace(collection_path)
            print(f"✅ collection.json 已自动绑定 session_id: {sid}")
            print(f"   （stream 校验副本: {stream_keep.name}，来源: {native}）")
            return
        print(f"   ❌ 未成功: {'; '.join(reasons) if reasons else '未知'}")
        # 保存失败现场便于排查，然后继续回滚重试
        if attempt_out != out:
            keep = out.with_suffix(out.suffix + f".fail{attempt}")
            keep.write_text(attempt_out.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"   （失败轨迹留档: {keep.name}）")

    print(f"❌ 已重试 {args.max_attempts} 次仍未成功结束，请人工检查环境与 prompt。")
    sys.exit(1)


def main():
    p = argparse.ArgumentParser(description="跑 Claude Code 轨迹 + 失败回滚重试")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("snapshot")
    c.add_argument("--env", required=True)
    c.add_argument("--snapshot", required=True)
    c.set_defaults(func=cmd_snapshot)

    c = sub.add_parser("run")
    c.add_argument("--env", required=True)
    c.add_argument("--prompt", required=True)
    c.add_argument("--output", required=True)
    c.add_argument("--snapshot", help="rsync 快照目录；缺省时用 env 的 git 回滚")
    c.add_argument("--max-attempts", type=int, default=3)
    c.add_argument("--timeout", type=int, default=1800, help="单次超时秒数，0 表示不超时")
    c.add_argument("--claude", default=_default_claude())
    c.add_argument("--no-skip-permissions", action="store_true")
    c.add_argument("--quiet", action="store_true")
    c.add_argument("--disallowed-tools", nargs="*",
                   default=["WebFetch", "WebSearch", "Bash(git clone *)", "Bash(curl *)", "Bash(wget *)"],
                   help="跑轨迹时禁用的工具/命令（默认禁 WebFetch/WebSearch/git clone/curl/wget，防抓上游）")
    c.add_argument("--go-version", help="强制指定 Go 主版本（如 1.22）；缺省从 go.mod 自动读取")
    c.add_argument("--module-path", help="Go module 相对 env 的子目录（如 backend），缺省自动探测")
    c.add_argument("--no-pin-go", action="store_true", help="跳过 Go 版本钉死（不推荐）")
    c.add_argument("--task-type", choices=["bugfix", "diagnosis"], help="任务类型；提供后跑完会做任务结果校验")
    c.add_argument("--verify-cmds", help="仅用于识别私有目标测试；修复轨迹阶段绝不执行该命令")
    c.add_argument("--evaluator", help="私有测试目录；缺省为 <project>/evaluator")
    c.add_argument("--g1-manifest", help="G1 模型快照清单；缺省为 <project>/_delivery/g1_snapshot.json")
    c.add_argument("--lock-timeout", type=int, default=0, help="全局串行锁等待秒数；0 表示一直等")
    c.set_defaults(func=cmd_run)

    c = sub.add_parser("check")
    c.add_argument("--json", required=True)
    c.set_defaults(func=cmd_check)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
