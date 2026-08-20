#!/usr/bin/env python3
"""verify_cmds 字段的硬门禁。"""
from __future__ import annotations

import re
import shlex


_VALUE_FLAGS = {
    "-benchtime", "-count", "-cpu", "-exec", "-list", "-parallel",
    "-run", "-timeout", "-vet",
}

CONCURRENCY_CATEGORY = "concurrency并发问题"
_DETERMINISM_MARKERS = re.compile(
    r"(?:channel|barrier|waitgroup|hook|latch|clock|deadline|timeout|"
    r"mutex|cond|atomic|semaphore|同步|信号|栅栏|测试钩子|时钟|超时|"
    r"锁|条件变量|原子|调度器|阻塞点|固定.{0,8}顺序|控制.{0,8}交错)",
    re.IGNORECASE,
)

_GENERIC_SUCCESS_CRITERIA_PHRASES = (
    "出问题的代码状态下定向命令稳定变红",
    "定位结论说清文件、符号和现象链路",
    "全程不改项目文件，只看公开现象和真实复现",
)

_NON_BUSINESS_PHRASES = (
    "项目代码文件工作区当前目录代码状态定向命令测试用例验证验收复现运行执行",
    "修复修完改完修改回退回归全量基线红灯绿灯变红全绿通过失败稳定二十遍",
    "公开行为公开现象真实复现定位结论文件符号现象链路原因根因机制保持原样零改动不改代码",
)

_NON_BUSINESS_WORDS = {
    "test", "tests", "code", "project", "file", "files", "workspace",
    "verify", "verification", "fix", "fixed", "bugfix", "diagnosis",
}


def _business_ngrams(text: str, size: int = 4) -> set[str]:
    """提取可用于跨字段核对的中文业务短语，排除纯流程术语。"""
    out: set[str] = set()
    for run in re.findall(r"[\u3400-\u9fff]+", str(text or "")):
        for index in range(0, len(run) - size + 1):
            gram = run[index:index + size]
            if not any(gram in phrase for phrase in _NON_BUSINESS_PHRASES):
                out.add(gram)
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", str(text or "")):
        lowered = word.lower()
        if lowered not in _NON_BUSINESS_WORDS:
            out.add(lowered)
    return out


def validate_success_criteria(collection: dict) -> list[str]:
    """校验 success_criteria 是否锚定本条 user_query 的业务场景。"""
    criteria = str(collection.get("success_criteria") or "").strip()
    query = str(collection.get("user_query") or "").strip()
    if not criteria:
        return ["success_criteria 为空"]

    errors: list[str] = []
    compact = re.sub(r"\s+", "", criteria)
    matched_generic = [phrase for phrase in _GENERIC_SUCCESS_CRITERIA_PHRASES if phrase in compact]
    if matched_generic:
        errors.append("success_criteria 命中空泛流程描述：" + "、".join(matched_generic))

    shared = _business_ngrams(query) & _business_ngrams(criteria)
    if not shared:
        errors.append("success_criteria 必须原样复用 user_query 中至少一个 4 字以上业务短语，并写明该场景的具体异常或后续影响")

    task_type = str(collection.get("task_type") or "").strip()
    if task_type == "diagnosis" and not re.search(r"(?:不改|未改|零改动|保持原样|没有改动|无修改)", criteria):
        errors.append("diagnosis 的 success_criteria 必须明确工作区或项目文件零改动")
    return errors


def validate_verify_cmds(command: str, require_race: bool = False) -> list[str]:
    command = command or ""
    if not command:
        return ["verify_cmds 为空"]
    if command != command.strip():
        return ["verify_cmds 不允许有首尾空白，证据轨迹会按原始字符逐一比对"]
    if "\n" in command or re.search(r"(?:;|[<>`]|\$\(|\|\||(?<!&)\|(?!&)|(?<!&)&(?!&))", command):
        return ["只允许一条定向 go test 命令，不得拼接其它命令"]

    parts = re.split(r"\s*&&\s*", command)
    if len(parts) == 2 and re.fullmatch(r"cd\s+\S+", parts[0]):
        test_command = parts[1]
    elif len(parts) == 1:
        test_command = parts[0]
    else:
        return ["只允许可选的 cd <目录> && 前缀，后面必须直接跟定向 go test"]

    try:
        tokens = shlex.split(test_command)
    except ValueError as exc:
        return [f"命令无法解析: {exc}"]
    if len(tokens) < 3 or tokens[:2] != ["go", "test"]:
        return ["verify_cmds 必须是 go test 定向测试命令"]

    errors: list[str] = []
    packages: list[str] = []
    run_pattern = ""
    count_value = ""
    has_race = False
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if token == "-args":
            errors.append("verify_cmds 不允许使用 -args 扩展执行范围")
            break
        if token.startswith("-"):
            flag, separator, value = token.partition("=")
            if flag == "-race":
                has_race = True
            if flag in ("-run", "-count"):
                if separator:
                    parsed_value = value
                elif index + 1 < len(tokens):
                    index += 1
                    parsed_value = tokens[index]
                else:
                    parsed_value = ""
                if flag == "-run":
                    run_pattern = parsed_value
                else:
                    count_value = parsed_value
            elif not separator and flag in _VALUE_FLAGS and index + 1 < len(tokens):
                index += 1
        else:
            packages.append(token)
        index += 1

    if len(packages) != 1:
        errors.append("必须明确且只写一个目标包")
    elif packages[0] in {".", "./...", "..."} or "..." in packages[0]:
        errors.append("不允许使用 go test ./...、通配包或当前目录这类宽泛目标")
    if count_value != "1":
        errors.append("必须显式使用 -count=1")
    if not re.fullmatch(r"\^Test[A-Za-z0-9_]+\$", run_pattern):
        errors.append("必须用 -run '^TestName$' 精确写出一个目标测试名称")
    if require_race and not has_race:
        errors.append("concurrency并发问题的 verify_cmds 必须显式包含 -race")
    return errors


def validate_concurrency_metadata(collection: dict) -> list[str]:
    """校验并发题是否记录了可复核的确定性复现策略。"""
    if str(collection.get("bug_category") or "").strip() != CONCURRENCY_CATEGORY:
        return []
    errors: list[str] = []
    determinism = str(collection.get("repro_determinism") or "").strip()
    if determinism not in {"deterministic", "flaky"}:
        errors.append("repro_determinism 只能填写 deterministic 或 flaky")
    if str(collection.get("task_type") or "").strip() == "bugfix" and determinism != "deterministic":
        errors.append("并发 bugfix 的 repro_determinism 必须为 deterministic")
    evidence_fields = ["success_criteria"]
    if str(collection.get("task_type") or "").strip() == "diagnosis":
        evidence_fields.append("gold_root_cause")
    for evidence_field in evidence_fields:
        evidence = str(collection.get(evidence_field) or "").strip()
        if len(evidence) < 20 or not _DETERMINISM_MARKERS.search(evidence):
            errors.append(f"并发题 {evidence_field} 必须说明确定性复现方案及稳定性验收事实")
    return errors
