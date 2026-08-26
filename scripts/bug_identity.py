#!/usr/bin/env python3
"""Canonical bug identity derived from a record directory name."""
import re


RECORD_SUFFIX = re.compile(r"^(?P<name>.+)__(?P<record>\d{3})$")


def bug_id_for_project(project_name: str) -> str:
    """Return '<directory-name>-<record>' while preserving the name verbatim."""
    name = str(project_name or "").strip()
    match = RECORD_SUFFIX.fullmatch(name)
    if match:
        return f"{match.group('name')}-{match.group('record')}"
    return f"{name}-001"
