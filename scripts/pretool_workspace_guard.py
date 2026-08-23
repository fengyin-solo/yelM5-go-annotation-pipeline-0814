#!/usr/bin/env python3
"""Claude PreToolUse hook that blocks workspace-external and private paths."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path


MARKER = "[workspace-guard blocked]"
FORBIDDEN_PARTS = {".git", "_gold", "evaluator", "_evidence", "_failed_rounds", ".base_snapshot"}
PATH_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep"}


def canonical(path: Path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def path_issue(value: str, workspace: Path, cwd: Path) -> str | None:
    value = value.strip().strip("'\"")
    if not value or value == "/dev/null" or value.startswith(("http://", "https://")):
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    resolved = canonical(candidate)
    if any(part in FORBIDDEN_PARTS for part in resolved.parts):
        return f"forbidden private/history path: {value}"
    try:
        resolved.relative_to(canonical(workspace))
    except ValueError:
        return f"workspace-external path: {value}"
    return None


def _command_prefix(command: str) -> str:
    """Discard heredoc bodies so Go comments and division are never parsed as shell paths."""
    lines = command.splitlines()
    kept: list[str] = []
    terminator: str | None = None
    for line in lines:
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        kept.append(line)
        match = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        if match:
            terminator = match.group(1)
    return "\n".join(kept)


def bash_path_candidates(command: str) -> list[str]:
    prefix = _command_prefix(command)
    try:
        lexer = shlex.shlex(prefix, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = prefix.split()
    candidates: list[str] = []
    for token in tokens:
        if token.startswith("file://"):
            candidates.append(token[7:])
        elif token.startswith(("/", "~/", "../", "./")):
            candidates.append(token)
        elif token == ".." or "/../" in f"/{token}/":
            candidates.append(token)
        elif any(f"/{part}/" in f"/{token}/" for part in FORBIDDEN_PARTS):
            candidates.append(token)
        else:
            # Catch interpreter snippets such as python -c "open('/tmp/x')" and X=/tmp/x.
            for match in re.finditer(r"(?:^|[=(,:])(/[A-Za-z][^\s'\"),;]*)", token):
                candidates.append(match.group(1))
    # shlex removes quotes, so also inspect the raw shell prefix for embedded paths.
    for match in re.finditer(r"(?:^|[=(,:\"'])(/[A-Za-z][^\s'\"),;]*)", prefix):
        candidates.append(match.group(1))
    return candidates


def inspect_hook(payload: dict, workspace: Path) -> list[str]:
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    cwd = canonical(Path(str(payload.get("cwd") or workspace)))
    issues: list[str] = []
    values: list[str] = []
    if tool == "Bash":
        command = str(tool_input.get("command") or "")
        values.extend(bash_path_candidates(command))
        if re.search(r"\$(?:\{)?(?:HOME|TMPDIR|OLDPWD)(?:\})?", command):
            issues.append("external-directory environment variable is forbidden")
        if re.search(r"\bgit\s+(?:log|show|reflog|blame|checkout|switch|diff)\b", command):
            issues.append("Git history/diff access is forbidden")
        if re.search(r"(?:^|[/:])(?:\.git|_gold|evaluator|_evidence|_failed_rounds)(?:/|$)", command):
            issues.append("forbidden private/history path in Bash command")
    elif tool in PATH_TOOLS:
        for key in ("file_path", "path", "cwd", "pattern"):
            if isinstance(tool_input.get(key), str):
                values.append(tool_input[key])
        if tool in {"Glob", "Grep"} and isinstance(tool_input.get("path"), str):
            values.append(tool_input["path"])
    for value in values:
        issue = path_issue(value, workspace, cwd)
        if issue and issue not in issues:
            issues.append(issue)
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"{MARKER} invalid hook input: {exc}", file=sys.stderr)
        return 2
    issues = inspect_hook(payload, Path(args.workspace))
    if not issues:
        return 0
    print(f"{MARKER} " + "; ".join(issues), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
