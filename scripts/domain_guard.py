#!/usr/bin/env python3
"""禁止项目类型与功能点的最低关键词门禁。

语义审查仍由人工完成；本脚本负责尽早拦截明确命中，不能用于证明候选一定合格。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("特别禁止/查账账务", (
        r"查账", r"对账", r"账务", r"账面", r"财务(?:报表|核算|系统)?", r"会计",
        r"结账", r"记账", r"账本", r"总账", r"明细账", r"往来账",
        r"应收(?:账款|款|管理|系统|明细)", r"应付(?:账款|款|管理|系统|明细)",
        r"支付.{0,6}核对", r"accounting", r"bookkeep", r"ledger", r"reconciliation",
        r"financial[-_ ]?(?:report|closing|accounting)",
    )),
    ("特别禁止/订单", (
        r"订单", r"购物车", r"外卖", r"点餐", r"采购单", r"预订订单",
        r"e-?commerce.{0,12}orders?", r"orders?[-_ ]?(?:service|system|management|processor|api)",
        r"shopping[-_ ]?cart", r"purchase[-_ ]?order",
    )),
    ("游戏/图形", (
        r"贪吃蛇", r"打砖块", r"俄罗斯方块", r"坦克大战", r"粒子模拟", r"物理模拟",
        r"星系模拟", r"烟花", r"落沙", r"布料模拟", r"塔防", r"2d\s*解谜", r"潜行游戏",
        r"平台跳跃", r"喂食小动物", r"记忆翻牌", r"连连看", r"五子棋", r"棋类",
        r"2048", r"扫雷", r"打地鼠", r"snake[-_ ]?game", r"tetris", r"tower[-_ ]?defen[cs]e",
        r"brick[-_ ]?breaker", r"minesweeper", r"gomoku", r"memory[-_ ]?(?:match|cards)",
        r"particle[-_ ]?simulation", r"physics[-_ ]?simulation", r"platformer[-_ ]?game",
    )),
    ("平台/业务系统", (
        r"rbac", r"权限(?:管理|后台|系统)", r"仓库(?:库存|管理|系统|出入库)", r"仓储", r"库存",
        r"(?:商品|货物|物料|库存|仓储|仓库|库位).{0,6}(?:入库|出库|调拨)",
        r"(?:入库|出库|调拨).{0,6}(?:商品|货物|物料|库存|仓储|仓库|库位)",
        r"资产调拨", r"投票", r"问卷", r"考勤", r"\boa\b",
        r"图书借阅", r"博客", r"\bcms\b", r"医院挂号", r"问诊", r"\bcrm\b",
        r"im\s*私信", r"即时通讯", r"拍卖", r"停车场", r"工单", r"客服系统",
        r"商品.{0,8}excel", r"excel.{0,8}商品", r"积分商城", r"预约", r"预订系统",
        r"warehouse", r"inventory", r"auction", r"parking[-_ ]?(?:lot|system)",
        r"survey[-_ ]?(?:service|system|platform)", r"attendance[-_ ]?(?:service|system)",
        r"library[-_ ]?(?:lending|management)", r"blog[-_ ]?(?:cms|platform|service)",
        r"hospital[-_ ]?(?:registration|appointment)", r"clinic[-_ ]?(?:booking|appointment)",
        r"private[-_ ]?messaging", r"direct[-_ ]?messaging", r"helpdesk", r"ticketing[-_ ]?system",
        r"points[-_ ]?(?:mall|store)", r"booking[-_ ]?(?:service|system|platform)",
        r"reservation[-_ ]?(?:service|system|platform)",
    )),
    ("本地/桌面工具", (
        r"命令行(?:\s*cli|工具|应用|程序)", r"cli[-_ ]?(?:tool|app|utility|manager)", r"代码片段", r"批量重命名", r"截图标注",
        r"文件管理", r"文件同步", r"书签(?:管理|工具)?", r"密码管理", r"snippet[-_ ]?manager",
        r"code[-_ ]?snippet[-_ ]?(?:manager|tool)", r"batch[-_ ]?renam", r"screenshot[-_ ]?annotat",
        r"file[-_ ]?(?:manager|sync)", r"bookmark[-_ ]?manager", r"password[-_ ]?manager",
    )),
    ("数据可视化/前端页面", (
        r"报表统计", r"统计报表", r"streamlit", r"csv\s*看板", r"数据看板", r"可视化看板",
        r"纯前端", r"前端页面", r"健康健身", r"菜谱", r"天气(?:应用|页面|工具|网站|查询|预报)", r"番茄钟",
        r"习惯打卡", r"音乐播放器", r"旅行日记", r"观影记录", r"dashboard",
        r"fitness[-_ ]?(?:app|tracker)", r"recipe[-_ ]?(?:app|manager)", r"weather[-_ ]?(?:app|dashboard)",
        r"pomodoro", r"habit[-_ ]?tracker", r"music[-_ ]?player", r"travel[-_ ]?diary",
        r"movie[-_ ]?(?:log|tracker|watchlist)",
    )),
)

DEFAULT_FIELDS = ("bug_id", "user_query", "gold_root_cause", "success_criteria")


def validate_forbidden_domain(text: str) -> list[dict[str, str]]:
    value = str(text or "").strip()
    if not value:
        return []
    issues: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for category, patterns in RULES:
        for pattern in patterns:
            match = re.search(pattern, value, flags=re.IGNORECASE)
            if not match:
                continue
            key = (category, match.group(0).lower())
            if key in seen:
                continue
            seen.add(key)
            issues.append({"category": category, "match": match.group(0)})
    return issues


def validate_collection_domains(data: dict, fields: tuple[str, ...] = DEFAULT_FIELDS) -> list[str]:
    errors: list[str] = []
    for field in fields:
        value = str(data.get(field) or "").strip()
        for issue in validate_forbidden_domain(value):
            errors.append(
                f"{field} 命中禁止类型 {issue['category']}：{issue['match']!r}；"
                "必须更换项目或功能点，禁止只改写措辞"
            )
    return errors


def project_domain_texts(project: Path, repo_name: str = "") -> list[tuple[str, str]]:
    texts = [("repo_name", repo_name), ("project_dir", project.name)]
    for name in ("README.md", "README", "readme.md"):
        path = project / name
        if path.is_file():
            texts.append((str(path), path.read_text(encoding="utf-8", errors="ignore")))
            break
    return [(label, text) for label, text in texts if str(text).strip()]


def validate_project_domain(project: Path, repo_name: str = "") -> list[str]:
    errors: list[str] = []
    for label, text in project_domain_texts(project, repo_name):
        for issue in validate_forbidden_domain(text):
            errors.append(f"{label} 命中 {issue['category']}：{issue['match']!r}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="禁止项目类型与功能点检查")
    parser.add_argument("--text", action="append", default=[], help="候选项目描述或功能点，可重复")
    parser.add_argument("--file", action="append", default=[], help="待检查文本文件，可重复")
    parser.add_argument("--project", help="候选项目目录；检查目录名与根 README")
    parser.add_argument("--repo-name", default="", help="候选 GitHub 仓库名")
    args = parser.parse_args()

    sources: list[tuple[str, str]] = [("text", text) for text in args.text]
    for raw in args.file:
        path = Path(raw)
        sources.append((str(path), path.read_text(encoding="utf-8", errors="ignore")))
    if args.project:
        sources.extend(project_domain_texts(Path(args.project), args.repo_name))
    elif args.repo_name:
        sources.append(("repo_name", args.repo_name))
    if not sources:
        parser.error("至少提供 --text、--file、--project 或 --repo-name")

    failures = []
    for label, value in sources:
        for issue in validate_forbidden_domain(value):
            failures.append({"source": label, **issue})
    if failures:
        print(json.dumps({"ok": False, "issues": failures}, ensure_ascii=False, indent=2))
        print("候选命中禁止类型：必须更换项目或功能点，不能只删除关键词。", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "note": "关键词门禁通过，仍须人工完成语义审查"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
