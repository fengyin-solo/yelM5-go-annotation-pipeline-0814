#!/usr/bin/env python3
"""Canonical bug identity derived from the batch root and record number."""
import re
from pathlib import Path


RECORD_SUFFIX = re.compile(r"^(?P<name>.+)__(?P<record>\d{3})$")


def bug_id_for_project(root: str | Path, project_name: str | Path) -> str:
    """Return '<batch-root-name>-<record>' while preserving root characters."""
    root_name = Path(root).resolve().name
    if not root_name:
        raise ValueError("batch root must have a directory name")

    name = Path(str(project_name or "").strip()).name
    match = RECORD_SUFFIX.fullmatch(name)
    record = match.group("record") if match else "001"
    return f"{root_name}-{record}"
