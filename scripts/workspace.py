#!/usr/bin/env python3
"""本期工作区与项目状态管理。

目录约定（本期根目录 = 调用时的 cwd，例如 .../bug-go项目标注/0813）:

  <root>/
    _shared/                  # 本期全局共享（已用仓库镜像、全局汇总填表）
    _gold/                    # 出题人私有答案区（gold 模型正确代码，不交付、不进 git/zip）
    _rejected/                # 已标记删除的项目统一移到这里
    YYYY-MM-DD/               # 按日期创建的文件夹
      <owner>__<repo>/        # github：一个仓库 = 一个项目文件夹
      <name>__<record>/       # local：本地项目第 N 条（record=001…030）
        status.json           # 项目状态卡（唯一状态事实源，不含答案线索）
        repo/                 # github: 下载源码(含 .git 供分析)
        env/                  # 埋好 bug 的业务代码（脚本由此生成无测试隔离副本）
        evaluator/            # 私有目标测试（修复轨迹不可见）
        prompt.txt            # 题面
        project_summary.txt   # 单行项目类型简介，发布后成为 BENZHI_README.md 第一行
        <session_id>.jsonl    # 轨迹
        collection.json       # 本项目 21 字段填表数据
        收集表_<project>.xlsx  # 本项目独立填表数据

用法:
  workspace.py init --root <dir> [--date YYYY-MM-DD]
  workspace.py new-project --root <dir> --source github --repo <owner/repo|url> \
      --project-summary '<包含 Go 与项目类型的一句话简介>' [--url <clone-url>] [--date YYYY-MM-DD]
  workspace.py new-project --root <dir> --source local --repo <name> --local-path <dir> \
      --project-summary '<包含 Go 与项目类型的一句话简介>' \
      [--record 001] [--count 30] [--date YYYY-MM-DD]
  workspace.py list --root <dir> [--date YYYY-MM-DD] [--state <state>]
  workspace.py set --root <dir> --project <name> --state <state> [--reason <text>] [--date YYYY-MM-DD]
  workspace.py reject --root <dir> --project <name> --reason <text> [--date YYYY-MM-DD]
  workspace.py purge --root <dir> --confirm
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date as _date
from datetime import datetime
from pathlib import Path

from bug_identity import bug_id_for_project
from project_summary import validate_project_summary
from resource_lock import lock_name, resource_lock

STATES = {"candidate", "selected", "done", "rejected"}
SOURCES = {"github", "local"}
LOCAL_MAX_RECORDS = 30


def _today():
    return _date.today().isoformat()


def date_dir(root: Path, date: str) -> Path:
    return root / (date or _today())


def normalize(repo: str) -> str:
    s = (repo or "").strip()
    s = re.sub(r"\.git$", "", s.rstrip("/"))
    s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", s)
    s = re.sub(r"^[^@/]+@", "", s)
    m = re.match(r"^[^:]+:([^:]+)/([^/]+)$", s)
    if m and not s.startswith("/"):
        return f"{m.group(1).lower()}/{m.group(2).lower()}"
    parts = [p for p in s.split("/") if p]
    if len(parts) >= 2:
        if "." in parts[0] or ":" in parts[0]:
            parts = parts[1:]
        if len(parts) >= 2:
            return f"{parts[0].lower()}/{parts[1].lower()}"
    return s.lower()


def slugify(name: str) -> str:
    """本地项目名规范化为文件夹安全名称，并保留 Unicode 目录标识。"""
    s = (name or "").strip().lower()
    s = re.sub(r"[^\w.【】()-]+", "-", s, flags=re.UNICODE)
    s = s.strip("._-")
    return s or "local-project"


def load_status(proj: Path) -> dict:
    f = proj / "status.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {}


def save_status(proj: Path, data: dict):
    path = proj / "status.json"
    status_lock = proj.parents[1] / "_locks" / "status" / lock_name(str(proj.resolve()))
    with resource_lock(status_lock, label=f"状态 {proj.name}"):
        current = {}
        if path.exists():
            current = json.loads(path.read_text(encoding="utf-8"))
        pipeline = current.get("pipeline")
        current.update({key: value for key, value in data.items() if key != "pipeline"})
        if pipeline is not None:
            current["pipeline"] = pipeline
        data = current
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


def find_project(root: Path, name: str, date: str) -> Path | None:
    if Path(name).exists():
        return Path(name)
    key = name.replace("/", "__").lower()
    d = date_dir(root, date)
    if not d.exists():
        return None
    for child in d.iterdir():
        if child.is_dir() and child.name.lower() == key:
            return child
    return None


def cmd_init(args):
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    d = date_dir(root, args.date)
    d.mkdir(parents=True, exist_ok=True)
    (root / "_rejected").mkdir(exist_ok=True)
    (root / "_gold").mkdir(exist_ok=True)
    shared = root / "_shared"
    shared.mkdir(exist_ok=True)
    readme = shared / "README.md"
    if not readme.exists():
        readme.write_text(
            "# 本期全局共享\n\n"
            "- `used-repositories.json` / `used-repositories.md`：全局已用仓库镜像，由 `repo_registry.py sync` 同步。\n"
            "- `收集表_汇总.xlsx`：全局汇总填表数据（一条一行），由 `collection_table.py sync` 生成。\n"
            "- `../_gold/`：出题人私有答案区（gold 模型正确代码），不交付、不进 git/zip。\n",
            encoding="utf-8",
        )
    print(f"✅ 已初始化本期根目录 {root}")
    print(f"   - 日期文件夹: {d}")
    print(f"   - 全局共享:   {shared}")
    print(f"   - 私有答案区: {root / '_gold'}")
    print(f"   - 回收区:     {root / '_rejected'}")


def rsync_copy(src: Path, dst: Path) -> str:
    """rsync 导出干净副本（排除 .git / 轨迹 / 日志 / 构建产物）。"""
    dst.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["rsync", "-a", "--delete",
         "--exclude=.git", "--exclude=trajectory*.jsonl", "--exclude=*.log",
         "--exclude=.claude", "--exclude=CLAUDE.md", "--exclude=AGENTS.md",
         "--exclude=SKILL.md", "--exclude=BUG_REPRO.md",
         "--exclude=_rejected", "--exclude=_shared", "--exclude=_gold",
         str(src).rstrip("/") + "/", str(dst).rstrip("/") + "/"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"rsync 失败: {r.stderr[:300]}")
    return "ok"


def cmd_new_project(args):
    root = Path(args.root)
    source = args.source
    d = date_dir(root, args.date)
    summary = str(args.project_summary or "").strip()
    summary_issues = validate_project_summary(summary)
    if summary_issues:
        print("❌ --project-summary 不合格：" + "；".join(summary_issues))
        sys.exit(1)

    if source == "github":
        key = normalize(args.repo)
        if not key or "/" not in key:
            print(f"❌ 仓库名无法识别: {args.repo}（github 来源需要 owner/repo 或完整 URL）")
            sys.exit(1)
        proj = d / key.replace("/", "__")
        if proj.exists() and (proj / "status.json").exists():
            st = load_status(proj)
            print(f"⚠️  项目已存在，跳过创建: {proj}（状态 {st.get('state','?')}）")
            sys.exit(1)
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "repo").mkdir(exist_ok=True)
        (proj / "project_summary.txt").write_text(summary + "\n", encoding="utf-8")
        data = {
            "name": key.replace("/", "__"),
            "source": "github",
            "repo": key,
            "clone_url": args.url or f"https://github.com/{key}",
            "state": "candidate",
            "bug_id": bug_id_for_project(root, proj.name),
            "base_commit": None,
            "fix_commits": [],
            "analysis": {},
            "reason": "",
            "created_at": _today(),
        }
        save_status(proj, data)
        print(f"✅ 已创建项目 {proj}")
        print(f"   source=github  repo={key}  state=candidate")

    elif source == "local":
        name = slugify(args.repo)
        if not args.local_path or not Path(args.local_path).is_dir():
            print(f"❌ source=local 需要 --local-path 指向存在的本地项目目录")
            sys.exit(1)
        if args.count is not None and not 1 <= args.count <= LOCAL_MAX_RECORDS:
            print(f"❌ 一个本地项目每次只能建 1~{LOCAL_MAX_RECORDS} 条记录；总数超过 {LOCAL_MAX_RECORDS} 时请拆成多个独立 0-1 项目和 GitHub 仓库。")
            sys.exit(1)
        src = Path(args.local_path)
        records = []
        if args.count and args.count > 1:
            records = [f"{i:03d}" for i in range(1, args.count + 1)]
        else:
            records = [(args.record or "001").strip() or "001"]

        invalid_records = [rec for rec in records if not re.fullmatch(r"\d{3}", rec) or not 1 <= int(rec) <= LOCAL_MAX_RECORDS]
        if invalid_records:
            print(f"❌ record 必须是 001~{LOCAL_MAX_RECORDS:03d}，收到: {', '.join(invalid_records)}")
            sys.exit(1)

        for rec in records:
            proj_name = f"{name}__{rec}"
            proj = d / proj_name
            if proj.exists() and (proj / "status.json").exists():
                st = load_status(proj)
                print(f"⚠️  项目已存在，跳过创建: {proj}（状态 {st.get('state','?')}）")
                continue
            proj.mkdir(parents=True, exist_ok=True)
            (proj / "env").mkdir(exist_ok=True)
            (proj / "evaluator").mkdir(exist_ok=True)
            (proj / "project_summary.txt").write_text(summary + "\n", encoding="utf-8")
            # env = 待埋错 workspace（模型工作目录）
            rsync_copy(src, proj / "env")
            # gold = 出题人私有答案区（干净基线 + 正确代码），项目目录外，不进交付
            gold = root / "_gold" / proj_name
            gold.mkdir(parents=True, exist_ok=True)
            rsync_copy(src, gold)
            data = {
                "name": proj_name,
                "source": "local",
                "repo": name,
                "record": rec,
                "state": "candidate",
                "bug_id": bug_id_for_project(root, proj_name),
                "base_commit": None,
                "fix_commits": [],
                "analysis": {},
                "reason": "",
                "created_at": _today(),
            }
            save_status(proj, data)
            print(f"✅ 已创建本地项目 {proj}")
            print(f"   source=local  repo={name}  record={rec}  state=candidate")
            print(f"   env/ = 待埋错 workspace；evaluator/ = 私有目标测试；gold 基线在 {gold}")

    else:
        print(f"❌ 未知 source: {source}")
        sys.exit(1)


def iter_projects(root: Path, date: str | None):
    roots = [date_dir(root, date)] if date else sorted(root.glob("*/"))
    seen = set()
    for d in roots:
        d = Path(d)
        if not d.is_dir() or d.name.startswith("_"):
            continue
        for child in sorted(d.iterdir()):
            if child.is_dir() and not child.name.startswith("_") and (child / "status.json").exists():
                if child.resolve() not in seen:
                    seen.add(child.resolve())
                    yield child


def cmd_list(args):
    root = Path(args.root)
    projects = list(iter_projects(root, args.date))
    if not projects:
        print("（无项目）")
        return
    print(f"{'状态':10} {'来源':8} {'日期':12} {'项目':40} bug_id")
    print("-" * 86)
    for p in projects:
        st = load_status(p)
        state = st.get("state", "?")
        if args.state and state != args.state:
            continue
        day = p.parent.name
        source = st.get("source", "github")
        print(f"{state:10} {source:8} {day:12} {p.name:40} {st.get('bug_id') or ''}")


def cmd_set(args):
    if args.state not in STATES:
        print(f"❌ 状态必须是 {sorted(STATES)}")
        sys.exit(1)
    root = Path(args.root)
    p = find_project(root, args.project, args.date)
    if not p:
        print(f"❌ 找不到项目: {args.project}")
        sys.exit(1)
    st = load_status(p)
    st["state"] = args.state
    if args.reason is not None:
        st["reason"] = args.reason
    save_status(p, st)
    print(f"✅ {p.name} -> {args.state}" + (f"（{args.reason}）" if args.reason else ""))


def cmd_reject(args):
    root = Path(args.root)
    p = find_project(root, args.project, args.date)
    if not p:
        print(f"❌ 找不到项目: {args.project}")
        sys.exit(1)
    st = load_status(p)
    st["state"] = "rejected"
    st["reason"] = args.reason
    save_status(p, st)
    (p / "REJECTED.md").write_text(
        f"# 已标记删除\n\n- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n- 原因：{args.reason}\n",
        encoding="utf-8",
    )
    rejected = root / "_rejected"
    rejected.mkdir(exist_ok=True)
    dest = rejected / p.name
    if dest.exists():
        import uuid
        dest = rejected / f"{p.name}__{uuid.uuid4().hex[:6]}"
    p.rename(dest)
    print(f"🗑️  已标记删除并移入回收区: {dest}")
    print(f"   原因: {args.reason}")
    print(f"   确认后可用 `workspace.py purge` 物理删除。")


def cmd_purge(args):
    root = Path(args.root)
    rejected = root / "_rejected"
    if not rejected.exists():
        print("（回收区为空）")
        return
    items = list(rejected.iterdir())
    if not items:
        print("（回收区为空）")
        return
    if not args.confirm:
        print(f"将物理删除 {len(items)} 个已标记项目，加 --confirm 执行：")
        for i in items:
            print("  -", i.name)
        sys.exit(1)
    for i in items:
        shutil.rmtree(i, ignore_errors=True)
        print("  已删除:", i.name)
    print(f"✅ 已清空回收区（{len(items)} 个）")


def main():
    p = argparse.ArgumentParser(description="本期工作区与项目状态管理")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("init", help="初始化本期根目录（_shared/_gold/_rejected/日期文件夹）")
    c.add_argument("--root", default=".")
    c.add_argument("--date", help="日期文件夹名，默认今天 YYYY-MM-DD")
    c.set_defaults(func=cmd_init)

    c = sub.add_parser("new-project", help="为仓库/本地项目创建项目文件夹")
    c.add_argument("--root", default=".")
    c.add_argument("--source", choices=sorted(SOURCES), default="local",
                   help="选题来源：local（默认，本地 0-1 自建项目）或 github（兼容旧流程，不使用）")
    c.add_argument("--repo", required=True, help="github: owner/repo 或 URL；local: 本地项目名")
    c.add_argument("--url", help="git 克隆地址（仅 github，默认 https://github.com/<owner>/<repo>）")
    c.add_argument("--local-path", help="本地项目源码目录（source=local 时必填）")
    c.add_argument("--project-summary", required=True,
                   help="单行项目类型简介，需包含 Go 和明确类型；将写入每条记录的 project_summary.txt")
    c.add_argument("--record", help="本地项目记录编号 001~030（单条，默认 001）")
    c.add_argument("--count", type=int, help="本地项目一次建几条（1~30；总数超过 30 时拆到多个仓库）")
    c.add_argument("--date")
    c.set_defaults(func=cmd_new_project)

    c = sub.add_parser("list")
    c.add_argument("--root", default=".")
    c.add_argument("--date")
    c.add_argument("--state", choices=sorted(STATES))
    c.set_defaults(func=cmd_list)

    c = sub.add_parser("set")
    c.add_argument("--root", default=".")
    c.add_argument("--project", required=True)
    c.add_argument("--state", required=True)
    c.add_argument("--reason")
    c.add_argument("--date")
    c.set_defaults(func=cmd_set)

    c = sub.add_parser("reject")
    c.add_argument("--root", default=".")
    c.add_argument("--project", required=True)
    c.add_argument("--reason", required=True)
    c.add_argument("--date")
    c.set_defaults(func=cmd_reject)

    c = sub.add_parser("purge")
    c.add_argument("--root", default=".")
    c.add_argument("--confirm", action="store_true")
    c.set_defaults(func=cmd_purge)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
