"""Shared counting and minimums for bugfix functional Go changes."""

from __future__ import annotations

import subprocess
from pathlib import Path

MIN_FUNCTIONAL_CHANGED_FILES = 1
MIN_FUNCTIONAL_CHANGED_LINES = 5


def meets_minimum_functional_change(changed_files: int, changed_lines: int) -> bool:
    return (
        changed_files >= MIN_FUNCTIONAL_CHANGED_FILES
        and changed_lines >= MIN_FUNCTIONAL_CHANGED_LINES
    )


def functional_go_diff_from_numstat(output: str) -> tuple[int, int]:
    """Count non-test Go files and added+deleted lines from git numstat output."""
    files = 0
    lines = 0
    for row in output.splitlines():
        parts = row.split("\t")
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        path = parts[-1].lower().replace("\\", "/")
        name = path.rsplit("/", 1)[-1]
        if not name.endswith(".go") or name.endswith("_test.go"):
            continue
        files += 1
        lines += int(parts[0]) + int(parts[1])
    return files, lines


def functional_go_diff_dirs(before: Path, after: Path) -> tuple[int, int]:
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--no-index", "--numstat",
         str(before), str(after)],
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"无法统计功能 Go 代码改动: {result.stderr.strip()}")
    return functional_go_diff_from_numstat(result.stdout)


def functional_go_diff_revisions(repo: Path, before: str, after: str) -> tuple[int, int]:
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--numstat", before, after, "--", "."],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"无法统计提交间功能 Go 代码改动: {result.stderr.strip()}")
    return functional_go_diff_from_numstat(result.stdout)
