#!/usr/bin/env python3
"""Shared validation for the one-line BENZHI project-type summary."""
from __future__ import annotations

from pathlib import Path


PROJECT_TYPE_MARKERS = (
    "CLI", "命令行", "服务", "API", "工具", "系统", "应用", "库", "平台",
    "代理", "守护进程", "模拟器", "引擎", "处理器", "网关", "调度器",
)


def validate_project_summary(summary: str) -> list[str]:
    value = str(summary or "").strip()
    issues: list[str] = []
    if not value:
        return ["项目简介为空"]
    if "\n" in value or "\r" in value:
        issues.append("项目简介必须只有一行")
    if "Go" not in value:
        issues.append("项目简介必须明确包含 Go")
    if not any(marker in value for marker in PROJECT_TYPE_MARKERS):
        issues.append("项目简介必须包含明确项目类型，如 CLI、命令行工具、服务、API、系统或库")
    if len(value) < 15 or len(value) > 180:
        issues.append("项目简介长度应为 15-180 个字符")
    return issues


def read_project_summary(project: Path) -> str:
    path = project / "project_summary.txt"
    if not path.is_file():
        raise RuntimeError(f"缺少项目类型简介: {path}")
    summary = path.read_text(encoding="utf-8").strip()
    issues = validate_project_summary(summary)
    if issues:
        raise RuntimeError("项目类型简介不合格：" + "；".join(issues))
    return summary
