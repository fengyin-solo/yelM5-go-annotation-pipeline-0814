#!/usr/bin/env python3
"""创建并校验每条记录的私有难度审查单 difficulty_review.json。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


RUNTIME_MECHANISMS = {
    "concurrency_sync",
    "channel_lifecycle",
    "context_lifecycle",
    "error_retry",
    "resource_lifecycle",
    "transaction_lifecycle",
    "typed_nil_dispatch",
    "panic_recovery",
    "shared_state_pollution",
    "state_machine_idempotency",
}


def _text(value) -> str:
    return str(value or "").strip()


def _load_json(path: Path) -> tuple[dict, list[str]]:
    if not path.exists():
        return {}, [f"缺 {path.name}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{path.name} 不是合法 JSON: {exc}"]
    if not isinstance(data, dict):
        return {}, [f"{path.name} 顶层必须是对象"]
    return data, []


def read_user_query(project: Path) -> str:
    collection = project / "collection.json"
    if collection.exists():
        try:
            data = json.loads(collection.read_text(encoding="utf-8"))
            query = _text(data.get("user_query"))
            if query:
                return query
        except (OSError, json.JSONDecodeError):
            pass
    prompt = project / "prompt.txt"
    return prompt.read_text(encoding="utf-8").strip() if prompt.exists() else ""


def infer_task_type(project: Path, review: dict) -> str:
    collection = project / "collection.json"
    if collection.exists():
        try:
            value = _text(json.loads(collection.read_text(encoding="utf-8")).get("task_type"))
            if value:
                return value
        except (OSError, json.JSONDecodeError):
            pass
    return _text(review.get("task_type")) or "bugfix"


def validate_review(project: Path, task_type: str | None = None) -> tuple[bool, list[str]]:
    review, issues = _load_json(project / "difficulty_review.json")
    if issues:
        return False, issues

    if review.get("version") != 1:
        issues.append("version 必须为 1")
    pattern_id = _text(review.get("pattern_id"))
    if pattern_id not in {f"P{i}" for i in range(1, 13)}:
        issues.append("pattern_id 必须是 P1-P12")

    primary = _text(review.get("primary_runtime_mechanism"))
    coupled = review.get("coupled_runtime_mechanisms")
    if primary not in RUNTIME_MECHANISMS:
        issues.append("primary_runtime_mechanism 不在允许列表")
    if not isinstance(coupled, list):
        issues.append("coupled_runtime_mechanisms 必须是数组")
        coupled = []
    invalid = sorted({_text(x) for x in coupled} - RUNTIME_MECHANISMS)
    if invalid:
        issues.append("未知耦合机制: " + ", ".join(invalid))
    if primary and primary in {_text(x) for x in coupled}:
        issues.append("主机制与耦合机制必须不同")

    trigger = review.get("trigger_sequence")
    if not isinstance(trigger, list) or len([x for x in trigger if _text(x)]) < 2:
        issues.append("trigger_sequence 至少包含 2 个有顺序的触发步骤")
    layers = review.get("affected_layers")
    unique_layers = {_text(x) for x in layers} if isinstance(layers, list) else set()
    unique_layers.discard("")
    if len(unique_layers) < 2:
        issues.append("affected_layers 至少包含 2 个不同模块/包")

    if review.get("simple_core_excluded") is not True:
        issues.append("simple_core_excluded 必须为 true")
    if review.get("single_local_fix_sufficient") is not False:
        issues.append("single_local_fix_sufficient 必须为 false")
    core = review.get("core_defect_review")
    if not isinstance(core, dict):
        issues.append("core_defect_review 必须是对象，明确排除局部数据变换")
        core = {}
    for key, label in (
        ("runtime_mechanism_failure", "运行时机制失效"),
        ("order_or_lifecycle_required", "调用顺序或生命周期依赖"),
        ("cross_layer_state_required", "跨层状态传导"),
        ("not_local_data_transform", "非局部数据变换"),
    ):
        if core.get(key) is not True:
            issues.append(f"core_defect_review.{key} 必须为 true（{label}）")
    if len(_text(core.get("failure_chain"))) < 20:
        issues.append("core_defect_review.failure_chain 必须描述不可拆的运行时故障链")
    if len(_text(core.get("local_fix_rejection"))) < 20:
        issues.append("core_defect_review.local_fix_rejection 必须说明为何不是单点局部修复")
    try:
        minimum_files = int(core.get("minimum_function_files", 0))
    except (TypeError, ValueError):
        minimum_files = 0
    # File count is useful evidence, but it is not a reliable proxy for difficulty.
    # Keep a small cross-file minimum and let the runtime chain carry the decision.
    required_files = 2
    if minimum_files < required_files:
        issues.append(f"core_defect_review.minimum_function_files 至少为 {required_files}")
    locations = core.get("root_cause_locations")
    if not isinstance(locations, list) or len(locations) < required_files:
        issues.append(f"core_defect_review.root_cause_locations 至少列出 {required_files} 个根因链功能文件")
        locations = []
    location_paths = set()
    gold = project.parents[1] / "_gold" / project.name
    for index, item in enumerate(locations, 1):
        if not isinstance(item, dict):
            issues.append(f"core_defect_review.root_cause_locations[{index}] 必须是对象")
            continue
        rel = _text(item.get("file"))
        rel_path = Path(rel)
        if not rel.endswith(".go") or rel.endswith("_test.go") or rel_path.is_absolute() or ".." in rel_path.parts:
            issues.append(f"core_defect_review.root_cause_locations[{index}].file 必须是相对路径功能 Go 文件")
        elif not any((root / rel).exists() for root in (project / ".base_snapshot", project / "env", gold)):
            issues.append(f"core_defect_review.root_cause_locations[{index}].file 不存在: {rel}")
        if rel in location_paths:
            issues.append(f"core_defect_review.root_cause_locations 文件重复: {rel}")
        location_paths.add(rel)
        if len(_text(item.get("runtime_responsibility"))) < 8:
            issues.append(f"core_defect_review.root_cause_locations[{index}] 缺具体运行时职责")
        if len(_text(item.get("failure_contribution"))) < 8:
            issues.append(f"core_defect_review.root_cause_locations[{index}] 缺故障链贡献")
    if review.get("manual_reviewed") is not True or not _text(review.get("reviewer_notes")):
        issues.append("必须完成人工审查并填写 reviewer_notes")

    query = read_user_query(project)
    if not query:
        issues.append("找不到 user_query（collection.json/prompt.txt）")
    query_evidence = review.get("query_evidence")
    if not isinstance(query_evidence, dict):
        issues.append("query_evidence 必须是对象")
        query_evidence = {}
    for key in ("trigger_fragment", "expected_fragment"):
        fragment = _text(query_evidence.get(key))
        if len(fragment) < 4 or fragment not in query:
            issues.append(f"query_evidence.{key} 必须是 user_query 中至少 4 字的原文片段")

    coverage = review.get("symptom_coverage")
    if not isinstance(coverage, list) or len(coverage) < 1:
        issues.append("symptom_coverage 至少填写 1 个主要用户可见症状及对应断言")
        coverage = []
    seen_fragments = set()
    for index, item in enumerate(coverage, 1):
        if not isinstance(item, dict):
            issues.append(f"symptom_coverage[{index}] 必须是对象")
            continue
        fragment = _text(item.get("query_fragment"))
        assertion = _text(item.get("test_assertion"))
        if len(fragment) < 4 or fragment not in query:
            issues.append(f"symptom_coverage[{index}].query_fragment 必须来自 user_query 原文")
        if not assertion:
            issues.append(f"symptom_coverage[{index}] 缺 test_assertion")
        if fragment:
            seen_fragments.add(fragment)

    actual_task_type = task_type or infer_task_type(project, review)
    if _text(review.get("task_type")) != actual_task_type:
        issues.append("difficulty_review.task_type 与 collection.json/命令指定类型不一致")
    if actual_task_type == "bugfix":
        repairs = review.get("repair_ablation_checks")
        if not isinstance(repairs, list):
            issues.append("bugfix 的 repair_ablation_checks 必须是数组")
            repairs = []
        # Ablation is optional evidence for unusually distributed fixes. It is
        # no longer a universal four-file gate.
        paths = set()
        for index, item in enumerate(repairs, 1):
            if not isinstance(item, dict):
                issues.append(f"repair_ablation_checks[{index}] 必须是对象")
                continue
            rel = _text(item.get("file"))
            rel_path = Path(rel)
            if not rel.endswith(".go") or rel.endswith("_test.go") or rel_path.is_absolute() or ".." in rel_path.parts:
                issues.append(f"repair_ablation_checks[{index}].file 必须是相对路径功能 Go 文件")
            else:
                gold = project.parents[1] / "_gold" / project.name
                exists = any((root / rel).exists() for root in (project / ".base_snapshot", project / "env", gold))
                if not exists:
                    issues.append(f"repair_ablation_checks[{index}].file 在 buggy/gold 项目中不存在: {rel}")
            if rel in paths:
                issues.append(f"repair_ablation_checks 文件重复: {rel}")
            paths.add(rel)
            if not _text(item.get("responsibility")):
                issues.append(f"repair_ablation_checks[{index}] 缺 responsibility")
            if _text(item.get("result")) != "red":
                issues.append(f"repair_ablation_checks[{index}].result 必须为 red")
            if not _text(item.get("observed_failure")):
                issues.append(f"repair_ablation_checks[{index}] 缺 observed_failure")

    return not issues, issues


def cmd_init(args) -> int:
    project = Path(args.project).resolve()
    path = project / "difficulty_review.json"
    if path.exists() and not args.force:
        print(f"拒绝覆盖已有文件: {path}（需要时加 --force）")
        return 1
    data = {
        "version": 1,
        "task_type": args.task_type,
        "pattern_id": args.pattern_id,
        "primary_runtime_mechanism": "",
        "coupled_runtime_mechanisms": [],
        "trigger_sequence": ["", ""],
        "affected_layers": ["", ""],
        "simple_core_excluded": False,
        "single_local_fix_sufficient": True,
        "core_defect_review": {
            "runtime_mechanism_failure": False,
            "order_or_lifecycle_required": False,
            "cross_layer_state_required": False,
            "not_local_data_transform": False,
            "failure_chain": "",
            "local_fix_rejection": "",
            "minimum_function_files": 0,
            "root_cause_locations": [],
        },
        "query_evidence": {"trigger_fragment": "", "expected_fragment": ""},
        "symptom_coverage": [{"query_fragment": "", "test_assertion": ""}],
        "repair_ablation_checks": [],
        "manual_reviewed": False,
        "reviewer_notes": "",
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已创建: {path}")
    print("填写后运行 difficulty_review.py check --project <记录目录>")
    return 0


def cmd_check(args) -> int:
    project = Path(args.project).resolve()
    ok, issues = validate_review(project, args.task_type)
    if ok:
        print("难度审查通过：运行时机制、跨层触发和题面覆盖证据齐全；可选回退证据有效。")
        return 0
    print("难度审查不通过：")
    for issue in issues:
        print(f"  - {issue}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="难度审查单管理")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="创建 difficulty_review.json 模板")
    init.add_argument("--project", required=True)
    init.add_argument("--pattern-id", required=True)
    init.add_argument("--task-type", choices=("bugfix", "diagnosis"), default="bugfix")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)
    check = sub.add_parser("check", help="校验 difficulty_review.json")
    check.add_argument("--project", required=True)
    check.add_argument("--task-type", choices=("bugfix", "diagnosis"))
    check.set_defaults(func=cmd_check)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
