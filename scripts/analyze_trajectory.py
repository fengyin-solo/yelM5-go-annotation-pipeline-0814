#!/usr/bin/env python3
"""Claude Code 轨迹（session jsonl）质检分析脚本。

用法:
  python3 analyze_trajectory.py <session.jsonl> [--task-type bugfix|diagnosis]
      [--go-version <收集表 go_version 原文> | --collection <项目 collection.json>]

输出质检所需的客观信息：
  1. 事件统计与截断检查（完整性）
  2. 真实用户文本输入条数（过滤 /command 等本地噪音，判断是否单轮）
  3. 工具调用序列（过程审查：是否先读码定位、是否改完验证）
  4. Edit/Write 改动文件清单（指令遵循：diagnosis 应零改动；是否动了测试文件）
  5. 测试命令输出摘录（结果核验线索，仍需质检人独立复跑）
  6. 最终回复全文（对照实际行为是否一致）

追加自动核对（发现硬问题以非 0 退出）：
  - go_version：轨迹内实际 go 版本 vs 收集表 go_version 字段声明版本
  - 声称命令：最终回复里声称执行的 go 命令，是否真的在 Bash 工具调用里出现过
  - 任务语义：bugfix 应有业务改动；diagnosis 应零代码变更
  - 隔离守卫：不得接触测试、Git 历史、私有答案或工作区外路径
  - 定位过程：轨迹里是否有读码动作（Read/Grep/Glob 或 Bash cat/sed/grep 等）
  - 反复改撤：只在「同一位置被多次 Edit」时提示，同一文件多处同类修改不算
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trajectory_guard import trajectory_policy_issues  # noqa: E402


def _declared_go_major_minor(declared: str | None) -> str | None:
    if not declared:
        return None
    m = re.search(r"golang:\s*([0-9]+\.[0-9]+)", declared)
    if m:
        return m.group(1)
    m = re.search(r"go\.mod:\s*go\s+([0-9]+\.[0-9]+)", declared)
    if m:
        return m.group(1)
    return None


def _claimed_go_verbs(final_text: str) -> set[str]:
    """从最终回复里抽出被声称执行过的 go 子命令（build/vet/test/run 等）。"""
    verbs = set()
    for m in re.finditer(r"\bgo\s+(build|vet|test|run|mod|fmt|list)\b", final_text):
        verbs.add(m.group(1))
    return verbs


def main():
    ap = argparse.ArgumentParser(description="Claude Code 轨迹质检分析")
    ap.add_argument("path", help="session jsonl 文件路径")
    ap.add_argument("--task-type", choices=["bugfix", "diagnosis"], help="任务类型")
    ap.add_argument("--go-version", help="收集表 go_version 字段原文，用于与轨迹实际版本比对")
    ap.add_argument("--collection", help="项目 collection.json 路径，自动读 task_type / go_version")
    ap.add_argument("--workspace-root", help="修复轨迹的隔离工作区；缺省从原始轨迹 cwd 推断")
    args = ap.parse_args()

    declared = args.go_version
    task_type = args.task_type
    if args.collection:
        try:
            cj = json.loads(Path(args.collection).read_text(encoding="utf-8"))
            declared = declared or cj.get("go_version") or None
            task_type = task_type or cj.get("task_type") or None
        except Exception as e:
            print(f"[警告] 读 collection.json 失败: {e}")

    events = []
    with open(args.path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"[警告] 第 {lineno} 行 JSON 解析失败（可能截断）")

    print(f"总事件数: {len(events)}")
    if events:
        print(f"首尾事件类型: {events[0].get('type')} -> {events[-1].get('type')}")

    user_texts, noise, slash_inputs = [], [], []
    tools, edits, test_results, final_texts = [], [], [], []
    bash_commands = []
    read_actions = []
    go_versions = set()
    # tool_use_id -> 该命令文本，用于把 tool_result 关联回命令
    tool_cmd_by_id = {}

    def _scan_text(s: str):
        # 兼容 "go1.25.1"（go version 输出）与 "go/1.25.1"（panic 栈路径）两种形态
        for m in re.finditer(r"go[/]?1\.([0-9]+)(?:\.([0-9]+))?", s):
            go_versions.add(f"1.{m.group(1)}" + (f".{m.group(2)}" if m.group(2) else ""))

    for e in events:
        etype = e.get("type")
        msg = e.get("message", {})
        content = msg.get("content")
        _scan_text(json.dumps(content, ensure_ascii=False) if content else "")
        if etype == "user":
            items = content if isinstance(content, list) else [{"type": "text", "text": content}]
            for it in items:
                if not isinstance(it, dict):
                    continue
                if it.get("type") == "text":
                    t = it.get("text") or ""
                    if re.match(r"^/[A-Za-z]", t.strip()):
                        slash_inputs.append(t.strip()[:80])
                    elif t.startswith("<local-command") or t.startswith("<command-"):
                        noise.append(t.split("\n")[0][:80])
                    elif t.strip():
                        user_texts.append(t)
                elif it.get("type") == "tool_result":
                    tid = it.get("tool_use_id")
                    txt = str(it.get("content"))
                    _scan_text(txt)
                    if tid in tool_cmd_by_id and "go test" in tool_cmd_by_id[tid]:
                        test_results.append((tool_cmd_by_id[tid], txt[:600]))
        elif etype == "assistant":
            for it in content if isinstance(content, list) else []:
                if not isinstance(it, dict):
                    continue
                if it.get("type") == "tool_use":
                    name = it.get("name")
                    inp = it.get("input", {})
                    tools.append((name, inp))
                    if name == "Bash":
                        cmd = str(inp.get("command") or "")
                        bash_commands.append(cmd)
                        tool_cmd_by_id[it.get("id")] = cmd
                        if re.search(r"\b(cat|sed|grep|head|tail|less|awk)\b", cmd):
                            read_actions.append(("Bash读码", cmd[:80]))
                    if name in ("Read", "Grep", "Glob"):
                        read_actions.append((name, (inp.get("file_path") or inp.get("pattern") or inp.get("command") or "")[:80]))
                    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                        edits.append((name, inp.get("file_path", "?"), inp.get("old_string") or inp.get("content") or ""))
                elif it.get("type") == "text" and (it.get("text") or "").strip():
                    final_texts.append(it["text"])

    print(f"\n== 真实用户输入: {len(user_texts)} 条（{'✅ 单轮' if len(user_texts) == 1 else '❌ 非单轮，需人工复核'}）")
    for t in user_texts:
        print("  USER:", t[:150].replace("\n", " ⏎ "))
    if slash_inputs:
        print(f"  ❌ 发现斜杠命令输入（禁止，如 /model /status）: {slash_inputs}")
    if noise:
        print(f"  本地命令噪音 {len(noise)} 条（需在质检备注说明）:")
        for n in noise:
            print("   -", n)

    print(f"\n== 工具调用序列（{len(tools)} 次）")
    for name, inp in tools:
        brief = inp.get("file_path") or inp.get("command") or json.dumps(inp, ensure_ascii=False)
        print(f"  - {name}: {str(brief)[:110]}")

    print(f"\n== 文件改动（{len(edits)} 次）")
    test_file_touched = False
    for name, fp, _old in edits:
        is_test = "_test.go" in fp
        test_file_touched = test_file_touched or is_test
        print(f"  {'❌ 动了测试文件!' if is_test else '✅'} {name}: {fp}")
    if not edits:
        print("  （无任何文件改动——diagnosis 应为零改动，bugfix 应有改动）")

    print(f"\n== 测试/验证命令输出（{len(test_results)} 段，仅供线索，质检人须独立复跑）")
    for i, (cmd, out) in enumerate(test_results, 1):
        print(f"  --- 第{i}段: {cmd[:80]} ---")
        print("  " + out.replace("\n", "\n  ")[:400])

    print("\n== 最终回复（最后一条 assistant 文本）")
    print(final_texts[-1][:2000] if final_texts else "  ❌ 无最终回复，轨迹可能截断")

    # ---------------- 追加自动核对 ----------------
    hard_issues = []
    warns = []

    print("\n== 自动核对 ==")

    if go_versions:
        print(f"  轨迹内检测到的 Go 版本: {sorted(go_versions)}")
        actual_mm = sorted({m.group(1) for v in go_versions for m in [re.match(r"(\d+\.\d+)", v)] if m})
        if declared:
            decl_mm = _declared_go_major_minor(declared)
            if decl_mm and actual_mm and decl_mm not in actual_mm:
                msg = (f"❌ go_version 与轨迹实际版本不一致：字段声明 {decl_mm}，轨迹实际 {actual_mm}；字段原文: {declared}")
                print(f"  {msg}")
                hard_issues.append(msg)
            elif decl_mm:
                print(f"  ✅ go_version 一致（声明 {decl_mm}，轨迹 {actual_mm}）")
        else:
            print("  ℹ️ 未提供 go_version 字段，跳过版本一致性核对（可加 --go-version 或 --collection）")
    else:
        print("  ℹ️ 轨迹内未发现 go1.X.Y 版本字符串，跳过 go_version 核对")

    if final_texts:
        claimed = _claimed_go_verbs(final_texts[-1])
        executed = set()
        for c in bash_commands:
            executed.update(re.findall(r"\bgo\s+(build|vet|test|run|mod|fmt|list)\b", c))
        for v in sorted(claimed - executed):
            msg = f"⚠️ 最终回复提到 go {v}，但轨迹 Bash 里没有对应执行（甲方可能判『声称命令未执行』，请确认是建议还是漏跑）"
            print(f"  {msg}")
            warns.append(msg)
        if claimed and claimed <= executed:
            print(f"  ✅ 最终回复声称的 go 命令都能在 Bash 调用中找到（{sorted(claimed)}）")
    else:
        warns.append("⚠️ 无最终回复，无法核对声称命令")

    if task_type == "bugfix":
        if edits:
            print("  ✅ bugfix 存在业务改动；绿灯由轨迹通过后的私有 evaluator 独立验收")
        else:
            msg = "❌ bugfix 没有文件改动"
            print(f"  {msg}")
            hard_issues.append(msg)
    elif task_type == "diagnosis":
        if edits:
            msg = f"❌ diagnosis 动了代码（{len(edits)} 次改动）——指令遵循失败"
            print(f"  {msg}")
            hard_issues.append(msg)
        else:
            print("  ✅ diagnosis 零代码变更")
    else:
        print("  ℹ️ 未指定 --task-type，跳过 bugfix/diagnosis 语义核对")

    # 定位过程检查
    if read_actions:
        kinds = []
        for a in read_actions[:8]:
            kinds.append(a[0])
        print(f"  ✅ 有读码/定位动作（{len(read_actions)} 次）: {', '.join(kinds)}")
    else:
        msg = "❌ 未发现任何读码/定位动作（无 Read/Grep/Glob，也无 Bash cat/sed/grep/head/tail）"
        print(f"  {msg}")
        hard_issues.append(msg)

    # 正式轨迹不得看到目标红绿测试；复现与验收由私有 evaluator 独立完成。
    workspace = Path(args.workspace_root).resolve() if args.workspace_root else None
    policy_issues = trajectory_policy_issues(Path(args.path), workspace)
    for issue in policy_issues:
        msg = f"❌ 轨迹守卫: {issue}"
        print(f"  {msg}")
        hard_issues.append(msg)
    if not policy_issues:
        print("  ✅ 轨迹未接触测试、历史、私有答案或工作区外路径")

    # 反复改撤：只在「同一位置被多次 Edit」时提示；同一文件多处同类修改不算
    from collections import Counter
    loc_counter = Counter()
    for name, fp, old in edits:
        if name == "Edit" and old:
            loc_counter[(fp, re.sub(r"\s+", "", old))] += 1
    revert_suspects = [(k, v) for k, v in loc_counter.items() if v > 1]
    for (fp, _old), v in revert_suspects:
        msg = f"⚠️ 同一位置被反复 Edit {v} 次（疑似反复改撤）: {fp}"
        print(f"  {msg}")
        warns.append(msg)
    if not revert_suspects:
        file_counts = Counter(fp for _, fp, _ in edits)
        multi = {fp: n for fp, n in file_counts.items() if n >= 3}
        if multi:
            print(f"  ℹ️ 同一文件多次 Edit 但均在「不同位置」（属同类型多处修复，非反复改撤）: {multi}")

    if test_file_touched:
        hard_issues.append("❌ 正式轨迹动了 _test.go，必须作废重跑")

    print()
    if hard_issues:
        print(f"❌ 发现 {len(hard_issues)} 个硬问题（请处理后再提交）:")
        for h in hard_issues:
            print("   -", h)
        sys.exit(1)
    if warns:
        print(f"⚠️ 发现 {len(warns)} 个需人工复核的提示:")
        for w in warns:
            print("   -", w)
    else:
        print("✅ 自动核对通过")


if __name__ == "__main__":
    main()
