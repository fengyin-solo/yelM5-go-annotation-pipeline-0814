#!/usr/bin/env python3
"""Shared wording rules for user_query and prompt.txt."""

import re


_GO_VERSION_PATTERNS = [
    re.compile(
        r"(?<![A-Za-z0-9_])go\s*(?:语言\s*)?"
        r"(?:(?:版本|工具链)\s*)?(?:(?:为|是|用的是|使用|采用)\s*)?"
        r"v?\d+(?:\.\d+){1,2}(?![\d.])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\d.])v?\d+(?:\.\d+){1,2}\s*"
        r"(?:版本的?|版)?\s*(?:的\s*)?go(?![A-Za-z0-9_])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9_])go\s*(?:语言\s*)?(?:的\s*)?(?:版本|工具链)",
        re.IGNORECASE,
    ),
]


def user_query_go_version_issues(text: str) -> list[str]:
    """Reject Go version/toolchain environment descriptions in user_query."""
    value = str(text or "").strip()
    if not value:
        return []
    matches: list[str] = []
    for pattern in _GO_VERSION_PATTERNS:
        for match in pattern.finditer(value):
            fragment = match.group(0).strip()
            if fragment and fragment not in matches:
                matches.append(fragment)
    return [
        f"user_query 不得写 Go 版本或工具链环境描述: {fragment!r}；请改为‘当前项目’的自然表达"
        for fragment in matches
    ]
