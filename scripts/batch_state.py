#!/usr/bin/env python3
"""Shared, atomic batch state and immutable-input fingerprint helpers."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from resource_lock import lock_name, resource_lock


PIPELINE_SCHEMA = 2
STAGES = (
    "prepared", "preflight_passed", "g1_published", "red_passed",
    "main_running", "main_accepted", "green_passed", "finalized",
    "uploaded", "qc_passed", "platform_submitted", "done",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default=None):
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def update_status(project: Path, *, stage: str | None = None, result: str | None = None,
                  detail: str = "", fingerprint: str | None = None,
                  attempt: dict | None = None) -> dict:
    path = project / "status.json"
    status_lock = project.parents[1] / "_locks" / "status" / lock_name(str(project.resolve()))
    with resource_lock(status_lock, label=f"状态 {project.name}"):
        data = load_json(path)
        pipeline = data.setdefault("pipeline", {"schema": PIPELINE_SCHEMA, "stages": {}, "attempt_history": []})
        pipeline["schema"] = PIPELINE_SCHEMA
        if stage:
            if stage not in STAGES:
                raise ValueError(f"unknown pipeline stage: {stage}")
            item = pipeline.setdefault("stages", {}).setdefault(stage, {})
            item.update({"result": result or "passed", "updated_at": now()})
            if detail:
                item["detail"] = detail
            if result in (None, "passed"):
                pipeline["stage"] = stage
        if fingerprint is not None:
            pipeline["input_fingerprint"] = fingerprint
        if attempt:
            pipeline.setdefault("attempt_history", []).append({**attempt, "at": now()})
        data["updated_at"] = now()
        atomic_json(path, data)
        if attempt:
            atomic_json(project / "_evidence" / "attempt_history.json", {
                "schema": PIPELINE_SCHEMA,
                "attempts": pipeline["attempt_history"],
            })
    return data


def iter_projects(root: Path, date: str | None = None, names: set[str] | None = None) -> list[Path]:
    days = [root / date] if date else sorted(
        p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")
    )
    projects = []
    for day in days:
        if not day.exists():
            continue
        for project in sorted(day.iterdir()):
            if not project.is_dir() or not (project / "status.json").exists():
                continue
            if names and project.name not in names:
                continue
            projects.append(project)
    return projects


def _hash_tree(digest, root: Path, label: str) -> None:
    if not root.exists():
        digest.update(f"missing:{label}\n".encode())
        return
    for path in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts):
        rel = path.relative_to(root).as_posix()
        if rel.endswith((".jsonl", ".log")):
            continue
        digest.update(f"{label}/{rel}\0".encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())


def input_fingerprint(project: Path, root: Path, *, env_source: Path | None = None) -> str:
    """Hash only preparation inputs; later session/upload fields do not invalidate it."""
    digest = hashlib.sha256()
    for name in ("prompt.txt", "difficulty_review.json", "contract_coverage.json", "project_summary.txt"):
        path = project / name
        digest.update(f"{name}\0".encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.exists() else b"missing")
    collection = load_json(project / "collection.json")
    immutable_fields = {
        key: collection.get(key) for key in (
            "bug_id", "task_type", "bug_category", "go_version", "repro_determinism",
            "user_query", "verify_cmds", "gold_root_cause", "success_criteria",
        )
    }
    digest.update(json.dumps(immutable_fields, ensure_ascii=False, sort_keys=True).encode())
    _hash_tree(digest, env_source or project / "env", "env")
    _hash_tree(digest, project / "evaluator", "evaluator")
    _hash_tree(digest, root / "_gold" / project.name, "gold")
    return digest.hexdigest()
