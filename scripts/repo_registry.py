#!/usr/bin/env python3
"""全局已用仓库/项目注册表（去重的唯一事实源）。

新口径（2026-08-15）：
- 选题来源统一为“自己 0-1 生成的项目”，不再去 GitHub 找题。
- 去重身份按优先级：优先用 GitHub 仓库地址，其次用本地项目绝对路径。
- 同一个 repo（GitHub 地址或本地路径任一命中即视为同一个 repo）最多出 30 条记录，
  每条记录必须是一个不同 bug；同一个 bug 只能出一个 task_type（bugfix / diagnosis 二选一）。

用法:
  repo_registry.py check <repo|url|local-path> [--source auto|github|local] \
      [--github-url <url>] [--local-path <path>]
  repo_registry.py register <repo|url|local-path> [--source auto|github|local] \
      [--github-url <url>] [--local-path <path>] [--project <id>] \
      [--date YYYY-MM-DD] [--note <text>] [--force]
  repo_registry.py list
  repo_registry.py sync [--root <period_root>]
"""
import argparse
import json
import os
import re
import sys
from datetime import date as _date
from pathlib import Path

from resource_lock import resource_lock

SKILL_DIR = Path(__file__).resolve().parent.parent
LOCAL_DIR = Path.home() / ".codex" / "go-annotation-pipeline"
DEFAULT_REGISTRY = LOCAL_DIR / "used-repositories.json"
DEFAULT_MD = LOCAL_DIR / "used-repositories.md"
# 旧版存放位置（技能目录内）：升级/分享时会被覆盖或带给别人，已弃用；首次运行自动迁移。
LEGACY_REGISTRY = SKILL_DIR / "references" / "used-repositories.json"
LEGACY_MD = SKILL_DIR / "references" / "used-repositories.md"
MAX_RECORDS_PER_REPO = 30


def registry_path() -> Path:
    return Path(os.environ.get("GO_ANNOTATION_REGISTRY", str(DEFAULT_REGISTRY)))


def md_path() -> Path:
    p = registry_path()
    return p.parent / (p.stem + ".md")


def _migrate_legacy() -> None:
    """把旧版技能目录内的注册表迁移到 ~/.codex/go-annotation-pipeline/（保留记录，不丢数据）。

    - 旧记录合并进新注册表：新表没有的条目直接搬入；两边都有的取 uses 较大值。
    - 迁移后旧文件重命名为 *.migrated 备份，避免重复迁移，也不删除任何数据。
    - 设置了 GO_ANNOTATION_REGISTRY 环境变量时不做迁移。
    """
    if os.environ.get("GO_ANNOTATION_REGISTRY"):
        return
    if not LEGACY_REGISTRY.exists():
        return
    try:
        legacy = json.loads(LEGACY_REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        legacy = {}
    legacy_repos = legacy.get("repositories", []) or []

    if legacy_repos:
        if DEFAULT_REGISTRY.exists():
            data = json.loads(DEFAULT_REGISTRY.read_text(encoding="utf-8"))
        else:
            data = {"version": 2, "repositories": []}
        existing = {(r.get("owner_repo"), r.get("source", "github")): r for r in data.get("repositories", [])}
        merged = 0
        for r in legacy_repos:
            key = (r.get("owner_repo"), r.get("source", "github"))
            if key in existing:
                existing[key]["uses"] = max(existing[key].get("uses", 0), r.get("uses", 0))
            else:
                data.setdefault("repositories", []).append(r)
                merged += 1
        DEFAULT_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_REGISTRY.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"📦 已把旧注册表（技能目录内）的 {len(legacy_repos)} 条记录迁移到 {DEFAULT_REGISTRY}（新增 {merged} 条，重复条目取较大 uses）", file=sys.stderr)

    for old in (LEGACY_REGISTRY, LEGACY_MD):
        if old.exists():
            old.rename(old.with_name(old.name + ".migrated"))


def normalize_github(repo: str) -> str:
    """把任意 GitHub/Git 常见写法规范成 `<owner>/<repo>` 全小写。"""
    s = (repo or "").strip()
    if not s:
        return ""
    s = re.sub(r"\.git$", "", s.rstrip("/"))
    s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", s)
    s = re.sub(r"^[^@/]+@", "", s)
    s = s.split("://")[-1]
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


def canonical_local(path: str) -> str:
    """本地项目去重身份：绝对路径。"""
    s = (path or "").strip()
    if not s:
        return ""
    return str(Path(s).expanduser().resolve())


def identify(repo: str, source: str, github_url: str | None, local_path: str | None) -> tuple[str, str, str]:
    """返回 (source, key, display)。

    优先级：显式 github_url > 显式 local_path > source 提示 > repo 自身格式。
    """
    repo = (repo or "").strip()
    github_url = (github_url or "").strip()
    local_path = (local_path or "").strip()

    if github_url:
        key = normalize_github(github_url)
        if key:
            return "github", key, github_url
    if local_path:
        key = canonical_local(local_path)
        if key:
            return "local", key, key

    # 能识别成 GitHub 地址就用 GitHub；否则按本地路径。
    if source == "auto" or not source:
        if repo.startswith("/") or repo.startswith(".") or Path(repo).exists():
            key = canonical_local(repo)
            return "local", key, key
        gh = normalize_github(repo)
        if "/" in gh or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", repo):
            return "github", gh, repo
        key = canonical_local(repo)
        return "local", key, key

    if source == "github":
        key = normalize_github(repo)
        return "github", key, repo
    key = canonical_local(repo)
    return "local", key, key


def load(path: Path | None = None) -> dict:
    if path is None:
        _migrate_legacy()
    p = path or registry_path()
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"version": 2, "repositories": []}


def save(data: dict, path: Path | None = None) -> Path:
    p = path or registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    return p


def find(data: dict, key: str, source: str) -> dict | None:
    for r in data.get("repositories", []):
        if r.get("owner_repo") == key and (r.get("source", "github") == source):
            return r
    return None


def render_md(data: dict) -> str:
    lines = [
        "# 已用仓库/项目清单",
        "",
        "选题前必须读取本清单，并结合当前收集表复核。",
        "- 去重身份优先用 GitHub 仓库地址，其次用本地项目绝对路径。",
        f"- 同一个 repo（GitHub 地址或本地路径任一命中）最多出 {MAX_RECORDS_PER_REPO} 条记录，每条一个不同 bug。",
        "- 同一个 bug 只能出 bugfix / diagnosis 二选一，不得同时出两条。",
        "",
        "| 来源 | 身份 | GitHub 地址 | 本地路径 | 使用日期 | 项目 | 已用条数 | 备注 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in data.get("repositories", []):
        source = r.get("source", "github")
        owner_repo = r.get("owner_repo", "")
        github_url = r.get("github_url", "")
        local_path = r.get("local_path", "")
        if github_url:
            github_url = f"[{github_url}]({github_url})"
        uses = r.get("uses", "")
        lines.append(
            f"| {source} | `{owner_repo}` | {github_url} | `{local_path}` | "
            f"{r.get('used_at','')} | {r.get('project','')} | {uses}/{MAX_RECORDS_PER_REPO} | {r.get('note','')} |"
        )
    lines.append("")
    lines.append("> 本文件由 `scripts/repo_registry.py sync` 自动生成，请勿手改；改 `used-repositories.json`。")
    lines.append("")
    return "\n".join(lines)


def mirror_to_root(data: dict, root: str | None):
    if not root:
        return None
    shared = Path(root) / "_shared"
    shared.mkdir(parents=True, exist_ok=True)
    dst = shared / "used-repositories.json"
    dst.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (shared / "used-repositories.md").write_text(render_md(data), encoding="utf-8")
    return dst


def cmd_check(args):
    data = load()
    source, key, disp = identify(args.repo, args.source, args.github_url, args.local_path)
    r = find(data, key, source)
    uses = r.get("uses", 0) if r else 0
    if r and uses >= MAX_RECORDS_PER_REPO:
        print(f"❌ 已用满  {disp}  ->  {key}  ({uses}/{MAX_RECORDS_PER_REPO} 条)")
        sys.exit(1)
    print(f"✅ 可用    {disp}  ->  {key}  (已用 {uses}/{MAX_RECORDS_PER_REPO} 条)")
    sys.exit(0)


def _cmd_register_unlocked(args):
    data = load()
    source, key, disp = identify(args.repo, args.source, args.github_url, args.local_path)
    r = find(data, key, source)
    uses = r.get("uses", 0) if r else 0
    if r and args.project:
        registered_projects = set(r.get("projects") or [])
        if r.get("project"):
            registered_projects.add(r["project"])
        if args.project in registered_projects:
            print(f"✅ 已登记 {key} / {args.project}，幂等跳过（仍为 {uses}/{MAX_RECORDS_PER_REPO} 条）")
            return
    if uses >= MAX_RECORDS_PER_REPO:
        print(f"❌ 拒绝登记：{key} 已用满 {MAX_RECORDS_PER_REPO} 条，请新建独立 0-1 项目和 GitHub 仓库。")
        sys.exit(1)

    github_url = normalize_github(args.github_url or (args.repo if source == "github" else ""))
    if github_url and source == "github" and not github_url.startswith("https://"):
        github_url = f"https://github.com/{github_url}"

    local_path = canonical_local(args.local_path) if source == "local" else ""

    if r:
        r["uses"] = uses + 1
        r["used_at"] = args.date or _date.today().isoformat()
        if args.project:
            r["project"] = args.project
            projects = set(r.get("projects") or [])
            projects.add(args.project)
            r["projects"] = sorted(projects)
        if args.note:
            r["note"] = args.note
        if github_url:
            r["github_url"] = github_url
        if local_path:
            r["local_path"] = local_path
        entry = r
    else:
        entry = {
            "owner_repo": key,
            "source": source,
            "uses": 1,
            "github_url": github_url,
            "local_path": local_path,
            "used_at": args.date or _date.today().isoformat(),
            "project": args.project or "",
            "projects": [args.project] if args.project else [],
            "note": args.note or "",
        }
        data.setdefault("repositories", []).append(entry)
    data["repositories"].sort(key=lambda r: (r.get("used_at", ""), r.get("owner_repo", "")))
    save(data)
    md_path().write_text(render_md(data), encoding="utf-8")
    print(f"✅ 已登记 {key}（第 {entry.get('uses')}/{MAX_RECORDS_PER_REPO} 条）")
    if github_url:
        print(f"   GitHub: {github_url}")


def cmd_register(args):
    lock = registry_path().with_name(registry_path().name + ".lock")
    with resource_lock(lock, label="全局仓库注册表"):
        _cmd_register_unlocked(args)


def cmd_list(args):
    data = load()
    print(f"已用仓库/项目 {len(data.get('repositories', []))} 个：")
    for r in data.get("repositories", []):
        source = r.get("source", "github")
        uses = f"  {r.get('uses','')}/{MAX_RECORDS_PER_REPO}条"
        print(f"  - [{source}] {r.get('owner_repo')}  {r.get('used_at','')}{uses}  {r.get('note','')}")
        if r.get("github_url"):
            print(f"      github: {r.get('github_url')}")
        if r.get("local_path"):
            print(f"      local:  {r.get('local_path')}")


def _cmd_sync_unlocked(args):
    data = load()
    md = md_path()
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(render_md(data), encoding="utf-8")
    print(f"✅ 已重写 {md}")
    dst = mirror_to_root(data, args.root)
    if dst:
        print(f"✅ 已镜像到 {dst}")


def cmd_sync(args):
    lock = registry_path().with_name(registry_path().name + ".lock")
    with resource_lock(lock, label="全局仓库注册表"):
        _cmd_sync_unlocked(args)


def main():
    p = argparse.ArgumentParser(description="全局已用仓库/项目注册表")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="检查仓库/本地项目是否可用")
    c.add_argument("repo")
    c.add_argument("--source", choices=["auto", "github", "local"], default="auto")
    c.add_argument("--github-url")
    c.add_argument("--local-path")
    c.set_defaults(func=cmd_check)

    c = sub.add_parser("register", help="登记已用仓库/本地项目")
    c.add_argument("repo")
    c.add_argument("--source", choices=["auto", "github", "local"], default="auto")
    c.add_argument("--github-url")
    c.add_argument("--local-path")
    c.add_argument("--project")
    c.add_argument("--date")
    c.add_argument("--note")
    c.add_argument("--force", action="store_true", help="保留兼容；不能突破每仓 30 条硬上限")
    c.set_defaults(func=cmd_register)

    c = sub.add_parser("list")
    c.set_defaults(func=cmd_list)

    c = sub.add_parser("sync", help="重写 md 并镜像到本期 _shared/")
    c.add_argument("--root", help="本期根目录（默认当前目录）")
    c.set_defaults(func=cmd_sync)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
