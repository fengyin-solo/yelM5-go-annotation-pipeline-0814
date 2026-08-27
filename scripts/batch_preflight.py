#!/usr/bin/env python3
"""One-time batch preflight with retained 20/20 red and green calibration."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from batch_state import atomic_json, input_fingerprint, iter_projects, load_json, now, update_status  # noqa: E402
from change_scope import (  # noqa: E402
    MIN_FUNCTIONAL_CHANGED_LINES,
    functional_go_diff_dirs,
    meets_minimum_functional_change,
)
from contract_coverage import validate_manifest  # noqa: E402
from difficulty_review import validate_review  # noqa: E402
from trajectory_guard import copy_without_tests, inject_evaluator, private_test_issues  # noqa: E402
from user_query_rules import user_query_go_version_issues  # noqa: E402
from project_summary import read_project_summary  # noqa: E402
from verify_cmds import CONCURRENCY_CATEGORY, validate_concurrency_metadata, validate_verify_cmds  # noqa: E402

PREFLIGHT_GATE_VERSION = 5


def run(command, cwd: Path, *, env=None, timeout=1800, shell=False) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, env=env, shell=shell, capture_output=True, text=True, timeout=timeout)


def find_module(root: Path) -> Path | None:
    if (root / "go.mod").exists():
        return root
    candidates = sorted(root.glob("*/go.mod"))
    return candidates[0].parent if len(candidates) == 1 else None


def go_environment(module: Path) -> dict[str, str]:
    text = (module / "go.mod").read_text(encoding="utf-8", errors="ignore")
    import re
    match = re.search(r"^go\s+([0-9]+\.[0-9]+)", text, re.MULTILINE)
    env = os.environ.copy()
    if match:
        host = run(["go", "version"], module).stdout
        if f"go{match.group(1)}" not in host:
            env["GOTOOLCHAIN"] = f"go{match.group(1)}.0"
    return env


def declared_version(project: Path) -> str | None:
    module = find_module(project / "env")
    if module is None:
        return None
    import re
    match = re.search(r"^go\s+([0-9]+\.[0-9]+)", (module / "go.mod").read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def toolchain_canary(root: Path, *, versions: set[str] | None = None,
                     require_lc_uuid: set[str] | None = None, force: bool = False) -> dict:
    target = root / "_shared" / "preflight" / "toolchain.json"
    go_version = subprocess.run(["go", "version"], capture_output=True, text=True).stdout.strip()
    if not versions:
        import re
        match = re.search(r"go([0-9]+\.[0-9]+)", go_version)
        versions = {match.group(1)} if match else set()
    require_lc_uuid = require_lc_uuid or set()
    identity = {"go_version": go_version, "declared_versions": sorted(versions),
                "require_lc_uuid": sorted(require_lc_uuid),
                "platform": platform.platform(), "machine": platform.machine()}
    if target.exists() and not force:
        cached = load_json(target)
        if cached.get("identity") == identity and cached.get("result") == "passed":
            return cached
    checks = {}
    for version in sorted(versions):
        version_checks = {}
        with tempfile.TemporaryDirectory(prefix=f"go-{version}-canary-") as tmp:
            d = Path(tmp)
            (d / "go.mod").write_text(f"module annotation.local/canary\n\ngo {version}\n", encoding="utf-8")
            (d / "canary.go").write_text("package canary\nfunc Value() int { return 1 }\n", encoding="utf-8")
            (d / "canary_test.go").write_text(
                'package canary\nimport "testing"\nfunc TestValue(t *testing.T) { if Value()!=1 { t.Fatal("bad") } }\n',
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["GOTOOLCHAIN"] = f"go{version}.0"
            selected = run(["go", "version"], d, env=env, timeout=300)
            version_checks["selected"] = {
                "passed": selected.returncode == 0 and f"go{version}" in selected.stdout,
                "exit_code": selected.returncode, "output_tail": (selected.stdout + selected.stderr)[-2000:],
            }
            race = run(["go", "test", "-race", "./..."], d, env=env, timeout=300)
            version_checks["race"] = {"passed": race.returncode == 0, "exit_code": race.returncode,
                                      "output_tail": (race.stdout + race.stderr)[-2000:]}
            binary = d / "canary.test"
            built = run(["go", "test", "-c", "-o", str(binary)], d, env=env, timeout=300)
            version_checks["test_binary"] = {"passed": built.returncode == 0, "exit_code": built.returncode,
                                             "output_tail": (built.stdout + built.stderr)[-2000:]}
            if platform.system() == "Darwin" and built.returncode == 0:
                uuid = run(["otool", "-l", str(binary)], d)
                present = "LC_UUID" in uuid.stdout
                required = version in require_lc_uuid
                version_checks["lc_uuid"] = {
                    "passed": present or not required, "available": present, "required": required,
                    "exit_code": 0 if present else 1,
                }
            else:
                version_checks["lc_uuid"] = {"passed": True, "skipped": "non-Darwin host"}
        checks[version] = version_checks
    passed = bool(checks) and all(
        item.get("passed") is True for version_checks in checks.values() for item in version_checks.values()
    )
    payload = {"schema": 1, "identity": identity, "result": "passed" if passed else "failed",
               "checks": checks, "completed_at": now()}
    atomic_json(target, payload)
    if not passed:
        failed = [f"{version}/{name}" for version, version_checks in checks.items()
                  for name, item in version_checks.items() if not item.get("passed")]
        raise RuntimeError("toolchain canary failed: " + ", ".join(failed))
    return payload


def lc_uuid_required(project: Path) -> bool:
    evaluator = project / "evaluator"
    return any("LC_UUID" in path.read_text(encoding="utf-8", errors="ignore")
               for path in evaluator.rglob("*") if path.is_file())


def _copy_source(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=False, ignore=shutil.ignore_patterns(".git", "*.jsonl", "*.log"))


def _run_calibration(source: Path, evaluator: Path, verify_cmd: str, expected: str,
                     env: dict[str, str], count: int, timeout: int) -> dict:
    runs = []
    with tempfile.TemporaryDirectory(prefix=f"calibration-{expected}-") as tmp:
        for index in range(1, count + 1):
            workspace = Path(tmp) / f"workspace-{index}"
            _copy_source(source, workspace)
            inject_evaluator(evaluator, workspace)
            module = find_module(workspace)
            if module is None:
                raise RuntimeError(f"cannot locate module in {source}")
            started = time.monotonic()
            result = run(verify_cmd, module, env=env, timeout=timeout, shell=True)
            observed = "green" if result.returncode == 0 else "red"
            runs.append({"run": index, "exit_code": result.returncode, "observed": observed,
                         "duration_seconds": round(time.monotonic() - started, 3),
                         "output_tail": (result.stdout + result.stderr)[-1200:]})
            if observed != expected:
                break
    return {"expected": expected, "required": count, "completed": len(runs),
            "passed": len(runs) == count and all(x["observed"] == expected for x in runs), "runs": runs}


def _isolated_evaluator_compile(source: Path, evaluator: Path, env: dict[str, str], timeout: int) -> dict:
    """Compile evaluator in the exact no-original-tests shape used by formal acceptance."""
    with tempfile.TemporaryDirectory(prefix="evaluator-self-contained-") as tmp:
        workspace = Path(tmp) / "workspace"
        copy_without_tests(source, workspace)
        inject_evaluator(evaluator, workspace)
        module = find_module(workspace)
        if module is None:
            return {"passed": False, "exit_code": None, "output_tail": "cannot locate isolated go.mod"}
        result = run(["go", "test", "./...", "-run", "^$", "-count=1"], module, env=env, timeout=timeout)
        output = (result.stdout + result.stderr)[-3000:]
        return {"passed": result.returncode == 0, "exit_code": result.returncode, "output_tail": output}


def _diagnosis_acceptance_precheck(project: Path) -> dict:
    """Ensure the configured gold diagnosis is recognizable by the formal hard gate."""
    from trajectory_acceptance import _diagnosis_root_cause_check

    collection = load_json(project / "collection.json")
    answer = str(collection.get("gold_root_cause") or "").strip()
    if not answer:
        return {"passed": False, "exit_code": 1, "output": "gold_root_cause is empty"}
    with tempfile.TemporaryDirectory(prefix="diagnosis-acceptance-precheck-") as tmp:
        transcript = Path(tmp) / "gold.jsonl"
        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": answer}]},
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        return _diagnosis_root_cause_check(project, transcript)


def _assertion_reached(calibration: dict, project: Path) -> dict:
    import re
    manifest = load_json(project / "contract_coverage.json")
    messages = [str(row.get("message") or "") for row in manifest.get("contracts", []) if isinstance(row, dict)]
    def reached(message: str) -> bool:
        fragments = [part.strip() for part in re.split(r"%[-+#0-9.*]*[A-Za-z%]", message) if len(part.strip()) >= 4]
        return bool(fragments) and any(
            all(fragment in run.get("output_tail", "") for fragment in fragments)
            for run in calibration.get("runs", [])
        )
    hits = sorted({message for message in messages if reached(message)})
    return {"passed": bool(hits), "contract_messages_reached": hits}


def _run_ablation(project: Path, gold: Path, evaluator: Path, verify_cmd: str,
                  env: dict[str, str], timeout: int) -> dict:
    review = load_json(project / "difficulty_review.json")
    manifest = load_json(project / "contract_coverage.json")
    messages = [str(row.get("message") or "") for row in manifest.get("contracts", []) if isinstance(row, dict)]
    import re
    def message_hits(output: str) -> list[str]:
        hits = []
        for message in messages:
            fragments = [part.strip() for part in re.split(r"%[-+#0-9.*]*[A-Za-z%]", message)
                         if len(part.strip()) >= 4]
            if fragments and all(fragment in output for fragment in fragments):
                hits.append(message)
        return hits
    rows = review.get("repair_ablation_checks") or []
    results = []
    with tempfile.TemporaryDirectory(prefix="calibration-ablation-") as tmp:
        workspace = Path(tmp) / "workspace"
        for row in rows:
            rel = Path(str(row.get("file") or "")) if isinstance(row, dict) else Path("")
            if not rel.as_posix() or rel.is_absolute() or ".." in rel.parts:
                results.append({"file": rel.as_posix(), "passed": False, "error": "invalid path"})
                continue
            if workspace.exists():
                shutil.rmtree(workspace)
            _copy_source(gold, workspace)
            buggy = project / "env" / rel
            target = workspace / rel
            if not buggy.exists() or not target.exists():
                results.append({"file": rel.as_posix(), "passed": False, "error": "file missing"})
                continue
            shutil.copy2(buggy, target)
            inject_evaluator(evaluator, workspace)
            module = find_module(workspace)
            result = run(verify_cmd, module, env=env, timeout=timeout, shell=True)
            output = (result.stdout + result.stderr)[-4000:]
            hits = message_hits(output)
            results.append({"file": rel.as_posix(), "passed": result.returncode != 0 and bool(hits),
                            "exit_code": result.returncode,
                            "contract_messages_reached": hits, "output_tail": output[-1200:]})
    unique = {item["file"] for item in results if item.get("passed")}
    required = len(rows)
    return {"passed": required > 0 and len(results) == required and len(unique) == required
            and all(item.get("passed") for item in results),
            "required": required, "results": results}


def _functional_diff(env: Path, gold: Path) -> tuple[int, int]:
    return functional_go_diff_dirs(env, gold)


def preflight_project(project: Path, root: Path, *, calibration_runs: int = 20,
                      timeout: int = 1800, force: bool = False) -> dict:
    base_snapshot = project / ".base_snapshot"
    fingerprint = input_fingerprint(
        project,
        root,
        env_source=base_snapshot if base_snapshot.is_dir() else project / "env",
    )
    evidence_path = project / "_evidence" / "preflight.json"
    if evidence_path.exists() and not force:
        cached = load_json(evidence_path)
        if (cached.get("gate_version") == PREFLIGHT_GATE_VERSION
                and cached.get("fingerprint") == fingerprint and cached.get("result") == "passed"):
            update_status(project, stage="preflight_passed", fingerprint=fingerprint, detail="cached")
            return cached
    issues = []
    try:
        read_project_summary(project)
    except RuntimeError as exc:
        issues.append(str(exc))
    collection = load_json(project / "collection.json")
    task_type = str(collection.get("task_type") or "")
    prompt = (project / "prompt.txt").read_text(encoding="utf-8").strip() if (project / "prompt.txt").exists() else ""
    if prompt != str(collection.get("user_query") or "").strip():
        issues.append("prompt.txt and collection.user_query differ")
    issues.extend(user_query_go_version_issues(prompt))
    verify_cmd = str(collection.get("verify_cmds") or "")
    require_race = collection.get("bug_category") == CONCURRENCY_CATEGORY
    issues.extend(validate_verify_cmds(verify_cmd, require_race=require_race))
    issues.extend(validate_concurrency_metadata(collection))
    issues.extend(private_test_issues(project / "env", project / "evaluator", verify_cmd))
    ok, detail = validate_review(project, task_type)
    if not ok:
        issues.extend(f"difficulty: {item}" for item in detail)
    ok, detail = validate_manifest(project)
    if not ok:
        issues.extend(f"coverage: {item}" for item in detail)
    env_module = find_module(project / "env")
    gold = root / "_gold" / project.name
    gold_module = find_module(gold)
    if not env_module or not gold_module:
        issues.append("env/gold must each contain exactly one discoverable go.mod")
    if task_type not in {"bugfix", "diagnosis"}:
        issues.append("task_type must be bugfix or diagnosis")
    if task_type == "bugfix":
        files, lines = _functional_diff(project / "env", gold)
        if not meets_minimum_functional_change(files, lines):
            issues.append(
                f"bugfix gold must change at least one functional Go file and "
                f"{MIN_FUNCTIONAL_CHANGED_LINES} functional lines"
            )
    if issues:
        payload = {"schema": 1, "gate_version": PREFLIGHT_GATE_VERSION,
                   "fingerprint": fingerprint, "result": "failed", "issues": issues,
                   "completed_at": now()}
        atomic_json(evidence_path, payload)
        update_status(project, stage="prepared", result="failed", detail="; ".join(issues[:3]),
                      fingerprint=fingerprint)
        return payload
    env = go_environment(env_module)
    checks = {}
    for label, module in (("env_build", env_module), ("gold_build", gold_module), ("gold_regression", gold_module)):
        command = ["go", "test", "./..."] if label == "gold_regression" else ["go", "build", "./..."]
        result = run(command, module, env=env, timeout=timeout)
        checks[label] = {"passed": result.returncode == 0, "exit_code": result.returncode,
                         "output_tail": (result.stdout + result.stderr)[-3000:]}
    checks["isolated_evaluator_compile"] = _isolated_evaluator_compile(
        project / "env", project / "evaluator", env, timeout
    )
    if task_type == "diagnosis":
        checks["diagnosis_acceptance_precheck"] = _diagnosis_acceptance_precheck(project)
    red_source = base_snapshot if task_type == "bugfix" and base_snapshot.is_dir() else project / "env"
    checks["red_calibration"] = _run_calibration(
        red_source, project / "evaluator", verify_cmd, "red", env, calibration_runs, timeout
    )
    checks["green_calibration"] = _run_calibration(
        gold, project / "evaluator", verify_cmd, "green", env, calibration_runs, timeout
    )
    checks["target_assertion_reached"] = _assertion_reached(checks["red_calibration"], project)
    if task_type == "bugfix" and (load_json(project / "difficulty_review.json").get("repair_ablation_checks") or []):
        checks["repair_ablation"] = _run_ablation(
            project, gold, project / "evaluator", verify_cmd, env, timeout
        )
    passed = all(item.get("passed") is True for item in checks.values())
    payload = {"schema": 1, "gate_version": PREFLIGHT_GATE_VERSION,
               "fingerprint": fingerprint, "result": "passed" if passed else "failed",
               "calibration_runs": calibration_runs, "checks": checks, "completed_at": now()}
    atomic_json(evidence_path, payload)
    update_status(project, stage="preflight_passed" if passed else "prepared",
                  result="passed" if passed else "failed", fingerprint=fingerprint,
                  detail="all local gates passed" if passed else "local gate failed")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch preflight and retained red/green calibration")
    parser.add_argument("--root", default=".")
    parser.add_argument("--date")
    parser.add_argument("--project", action="append", dest="projects")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--calibration-runs", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        projects = [p for p in iter_projects(root, args.date, set(args.projects or []))
                    if load_json(p / "status.json").get("state") not in {"done", "rejected"}]
        versions = {version for project in projects if (version := declared_version(project))}
        uuid_versions = {version for project in projects
                         if (version := declared_version(project)) and lc_uuid_required(project)}
        toolchain_canary(root, versions=versions, require_lc_uuid=uuid_versions, force=args.force)
    except Exception as exc:
        print(f"toolchain preflight failed: {exc}", file=sys.stderr)
        return 1
    failures = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers), thread_name_prefix="batch-preflight") as pool:
        futures = {pool.submit(preflight_project, p, root, calibration_runs=args.calibration_runs,
                               timeout=args.timeout, force=args.force): p for p in projects}
        for future in as_completed(futures):
            project = futures[future]
            try:
                result = future.result()
                print(f"{'PASS' if result['result'] == 'passed' else 'FAIL'} {project.name}")
                failures += result["result"] != "passed"
            except Exception as exc:
                failures += 1
                print(f"FAIL {project.name}: {exc}", file=sys.stderr)
    print(f"preflight: {len(projects) - failures}/{len(projects)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
