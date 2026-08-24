#!/usr/bin/env python3
"""Create and validate the evaluator-to-prompt contract coverage manifest."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from pathlib import Path


ASSERT_RE = re.compile(
    r"\b([A-Za-z_]\w*)\.(Fatalf?|Errorf?)\s*\(\s*(`[^`]*`|\"(?:\\.|[^\"\\])*\")",
    re.MULTILINE,
)
ASSERT_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\.(Fatalf?|Errorf?)\s*\(")


def _decode_go_string(value: str) -> str:
    if value.startswith("`"):
        return value[1:-1]
    try:
        return ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return value.strip('"')


def extract_contracts(evaluator: Path) -> list[dict]:
    contracts: list[dict] = []
    for path in sorted(evaluator.rglob("*.go")) if evaluator.is_dir() else []:
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(evaluator).as_posix()
        testing_receivers = set(re.findall(r"\b([A-Za-z_]\w*)\s+\*testing\.(?:T|B)\b", text))
        for match in ASSERT_RE.finditer(text):
            if match.group(1) not in testing_receivers:
                continue
            message = _decode_go_string(match.group(3)).strip()
            if not message:
                continue
            line = text.count("\n", 0, match.start()) + 1
            digest = hashlib.sha256(f"{rel}:{line}:{match.group(2)}:{message}".encode()).hexdigest()[:16]
            contracts.append({
                "id": digest,
                "file": rel,
                "line": line,
                "assertion": match.group(2),
                "message": message,
            })
    return contracts


def unsupported_assertions(evaluator: Path) -> list[str]:
    """Return direct testing assertions whose first argument is not a literal string."""
    issues: list[str] = []
    for path in sorted(evaluator.rglob("*.go")) if evaluator.is_dir() else []:
        text = path.read_text(encoding="utf-8", errors="replace")
        receivers = set(re.findall(r"\b([A-Za-z_]\w*)\s+\*testing\.(?:T|B)\b", text))
        literal_starts = {match.start() for match in ASSERT_RE.finditer(text) if match.group(1) in receivers}
        for match in ASSERT_CALL_RE.finditer(text):
            if match.group(1) not in receivers or match.start() in literal_starts:
                continue
            line = text.count("\n", 0, match.start()) + 1
            issues.append(f"{path.relative_to(evaluator).as_posix()}:{line} {match.group(2)} needs a literal contract message")
    return issues


def _load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} top level must be an object")
    return data


def _success_text(collection: dict) -> str:
    value = collection.get("success_criteria")
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def initialize_manifest(project: Path, force: bool = False) -> Path:
    target = project / "contract_coverage.json"
    if target.exists() and not force:
        raise FileExistsError(f"refusing to overwrite {target}; pass --force")
    rows = []
    for contract in extract_contracts(project / "evaluator"):
        rows.append({
            **contract,
            "prompt_trigger_fragment": "",
            "prompt_expected_fragment": "",
            "success_criteria_fragment": "",
            "difficulty_evidence_fragment": "",
        })
    payload = {"version": 2, "contracts": rows, "test_cases": [{
        "kind": "target",
        "evaluator_trigger_fragment": "",
        "prompt_trigger_fragment": "",
        "prompt_expected_fragment": "",
        "success_criteria_fragment": "",
    }]}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def validate_manifest(project: Path) -> tuple[bool, list[str]]:
    issues: list[str] = []
    manifest_path = project / "contract_coverage.json"
    try:
        manifest = _load_json(manifest_path)
        collection = _load_json(project / "collection.json")
        difficulty = _load_json(project / "difficulty_review.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, [str(exc)]
    version = manifest.get("version")
    if version not in (1, 2):
        issues.append("contract_coverage.version must be 1 or 2")
    if version == 1:
        try:
            status = _load_json(project / "status.json")
        except (OSError, ValueError, json.JSONDecodeError):
            status = {}
        finalized = ((status.get("pipeline") or {}).get("stages") or {}).get("finalized", {}).get("result") == "passed"
        if status.get("state") != "done" and not finalized:
            issues.append("active records require contract_coverage.version 2; version 1 is legacy-only")
    issues.extend(unsupported_assertions(project / "evaluator"))
    rows = manifest.get("contracts")
    if not isinstance(rows, list):
        return False, issues + ["contract_coverage.contracts must be an array"]

    actual = extract_contracts(project / "evaluator")
    if not actual:
        issues.append("evaluator must contain at least one direct testing assertion with a literal message")
    expected_by_id = {item["id"]: item for item in actual}
    row_by_id = {row.get("id"): row for row in rows if isinstance(row, dict) and row.get("id")}
    missing = sorted(set(expected_by_id) - set(row_by_id))
    stale = sorted(set(row_by_id) - set(expected_by_id))
    if missing:
        issues.append("unmapped evaluator assertions: " + ", ".join(missing))
    if stale:
        issues.append("stale evaluator assertion mappings: " + ", ".join(stale))
    if len(row_by_id) != len(rows):
        issues.append("contract ids must be present and unique")

    prompt_path = project / "prompt.txt"
    prompt = prompt_path.read_text(encoding="utf-8").strip() if prompt_path.exists() else str(collection.get("user_query") or "")
    success = _success_text(collection)
    difficulty_text = json.dumps(difficulty, ensure_ascii=False)
    for contract_id, expected in expected_by_id.items():
        row = row_by_id.get(contract_id)
        if not row:
            continue
        for key in ("file", "line", "assertion", "message"):
            if row.get(key) != expected.get(key):
                issues.append(f"{contract_id}.{key} no longer matches evaluator")
        checks = (
            ("prompt_trigger_fragment", prompt),
            ("prompt_expected_fragment", prompt),
            ("success_criteria_fragment", success),
            ("difficulty_evidence_fragment", difficulty_text),
        )
        for field, haystack in checks:
            fragment = str(row.get(field) or "").strip()
            if len(fragment) < 4 or fragment not in haystack:
                issues.append(f"{contract_id}.{field} must be an exact fragment of its source")
    if version == 2:
        cases = manifest.get("test_cases")
        if not isinstance(cases, list) or not cases:
            issues.append("contract_coverage.test_cases must contain at least one exact input/behavior mapping")
        else:
            evaluator_text = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in sorted((project / "evaluator").rglob("*.go"))
            )
            assertion_messages = {item["message"] for item in actual}
            valid_kinds = {"target", "preservation"}
            for index, case in enumerate(cases):
                prefix = f"test_cases[{index}]"
                if not isinstance(case, dict):
                    issues.append(f"{prefix} must be an object")
                    continue
                if case.get("kind") not in valid_kinds:
                    issues.append(f"{prefix}.kind must be target or preservation")
                checks = (
                    ("evaluator_trigger_fragment", evaluator_text),
                    ("prompt_trigger_fragment", prompt),
                    ("prompt_expected_fragment", prompt),
                    ("success_criteria_fragment", success),
                )
                for field, haystack in checks:
                    fragment = str(case.get(field) or "").strip()
                    if len(fragment) < 4 or fragment not in haystack:
                        issues.append(f"{prefix}.{field} must be an exact fragment of its source")
                evaluator_fragment = str(case.get("evaluator_trigger_fragment") or "").strip()
                if evaluator_fragment in assertion_messages:
                    issues.append(f"{prefix}.evaluator_trigger_fragment must point to setup/input, not an assertion message")
                boundary_field = re.search(
                    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?::=|:|=)\s*(?:\"\"|nil)",
                    evaluator_fragment,
                )
                prompt_fragment = str(case.get("prompt_trigger_fragment") or "")
                if boundary_field and boundary_field.group(1).lower() not in prompt_fragment.lower():
                    issues.append(
                        f"{prefix}.prompt_trigger_fragment must name boundary field {boundary_field.group(1)!r}"
                    )
            if isinstance(cases, list) and not any(
                isinstance(case, dict) and case.get("kind") == "target" for case in cases
            ):
                issues.append("contract_coverage.test_cases must include at least one target case")
            preservation_required = bool(re.search(
                r"(?:照常|仍(?:能|可|应)|继续(?:支持|可用)|不能影响|不应影响|保持兼容|原有.{0,8}(?:行为|功能))",
                prompt,
            ))
            if preservation_required and not any(
                isinstance(case, dict) and case.get("kind") == "preservation" for case in cases
            ):
                issues.append(
                    "prompt contains an existing-behavior requirement; test_cases must include a preservation case"
                )
    return not issues, issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluator contract coverage gate")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--project", required=True)
    init.add_argument("--force", action="store_true")
    check = sub.add_parser("check")
    check.add_argument("--project", required=True)
    args = parser.parse_args()
    project = Path(args.project).resolve()
    if args.command == "init":
        try:
            target = initialize_manifest(project, args.force)
        except FileExistsError as exc:
            print(exc)
            return 1
        print(f"created {target}; fill every contract and test_cases exact fragment before formal trajectory")
        return 0
    ok, issues = validate_manifest(project)
    if ok:
        print("contract coverage passed")
        return 0
    print("contract coverage failed:")
    for issue in issues:
        print(f"  - {issue}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
