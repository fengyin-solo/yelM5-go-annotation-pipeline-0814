#!/usr/bin/env python3
"""Resumable batch orchestrator with bounded target-model concurrency."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from batch_preflight import declared_version, lc_uuid_required, preflight_project, toolchain_canary  # noqa: E402
from batch_state import atomic_json, input_fingerprint, iter_projects, load_json, update_status  # noqa: E402
from resource_lock import lock_name, resource_lock  # noqa: E402


HERE = Path(__file__).resolve().parent


def command(name: str, *args: str) -> list[str]:
    return [sys.executable, str(HERE / name), *map(str, args)]


def run_checked(cmd: list[str], *, cwd: Path, timeout: int = 7200) -> str:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}\n{output[-5000:]}")
    return output


def collection(project: Path) -> dict:
    return load_json(project / "collection.json")


def stage_passed(project: Path, stage: str) -> bool:
    pipeline = load_json(project / "status.json").get("pipeline", {})
    return pipeline.get("stages", {}).get(stage, {}).get("result") == "passed"


def mark(project: Path, stage: str, detail: str = "") -> None:
    update_status(project, stage=stage, detail=detail)
    print(f"PASS {project.name}: {stage}")


def require_unchanged_inputs(project: Path, root: Path) -> None:
    expected = load_json(project / "status.json").get("pipeline", {}).get("input_fingerprint")
    actual = input_fingerprint(project, root)
    if not expected or expected != actual:
        raise RuntimeError(
            "preflight inputs changed; rerun batch_pipeline.py preflight before consuming target-model time"
        )


def reconcile(project: Path) -> None:
    """Recover stage markers from durable artifacts after a process interruption."""
    data = collection(project)
    evidence = project / "_evidence"
    delivery = load_json(evidence / "repository_delivery.json")
    if delivery.get("state") in {"g1_published", "finalized"} and (project / "_delivery" / "g1_snapshot.json").exists():
        if not stage_passed(project, "g1_published"):
            mark(project, "g1_published", "reconciled from repository_delivery.json")
    red = load_json(evidence / "red_result.json")
    if red.get("session_id") and (evidence / "verify_red.jsonl").exists() and not stage_passed(project, "red_passed"):
        mark(project, "red_passed", "reconciled from local red evidence")
    sid = str(data.get("session_id") or "")
    acceptance = load_json(evidence / "trajectory_acceptance.json")
    guard = load_json(evidence / "trajectory_guard.json")
    if (sid and acceptance.get("result") == "passed" and acceptance.get("session_id") == sid
            and guard.get("session_id") == sid and (project / f"{sid}.jsonl").exists()
            and not stage_passed(project, "main_accepted")):
        mark(project, "main_accepted", "reconciled from accepted trajectory artifacts")
    task_type = data.get("task_type")
    if stage_passed(project, "main_accepted"):
        if task_type == "diagnosis":
            if not stage_passed(project, "green_passed"):
                mark(project, "green_passed", "not applicable for diagnosis")
        elif ((evidence / "verify_green.jsonl").exists()
              and load_json(evidence / "green_regression.json").get("result") == "passed"
              and not stage_passed(project, "green_passed")):
            mark(project, "green_passed", "reconciled from local green evidence")
    if ((task_type == "diagnosis" and stage_passed(project, "main_accepted"))
            or delivery.get("state") == "finalized"):
        if not stage_passed(project, "finalized"):
            mark(project, "finalized", "reconciled from delivery metadata")
    verify = data.get("verify_result")
    if isinstance(verify, str):
        try:
            verify = json.loads(verify)
        except json.JSONDecodeError:
            verify = {}
    required_evidence = [((verify or {}).get("pre_fix") or {}).get("trajectory_url")]
    if task_type == "bugfix":
        required_evidence.append(((verify or {}).get("post_fix") or {}).get("trajectory_url"))
    if data.get("trajectory") and all(required_evidence) and not stage_passed(project, "uploaded"):
        mark(project, "uploaded", "reconciled from collection URLs")
    if load_json(project / "status.json").get("state") == "done" and not stage_passed(project, "done"):
        mark(project, "done", "reconciled from workspace state")


def failure_metadata(exc: Exception) -> dict:
    """Classify failures so deterministic acceptance errors do not consume blind retries."""
    text = str(exc)
    if "轨迹守卫命中" in text:
        return {"failure_class": "trajectory_guard", "failure_signature": "trajectory_guard", "retryable": False}
    if "undefined:" in text or "[build failed]" in text:
        match = re.search(r"undefined:\s*([A-Za-z_][A-Za-z0-9_]*)", text)
        symbol = match.group(1) if match else "build_failed"
        return {"failure_class": "evaluator_compile", "failure_signature": f"evaluator_compile:{symbol}",
                "retryable": False}
    if "diagnosis_root_cause" in text:
        return {"failure_class": "diagnosis_root_cause", "failure_signature": "diagnosis_root_cause",
                "retryable": True, "max_same_failures": 2}
    if "private_verify" in text:
        match = re.search(r"--- FAIL:\s*(Test[A-Za-z0-9_]+)", text)
        test_name = match.group(1) if match else "unknown"
        return {"failure_class": "private_verify", "failure_signature": f"private_verify:{test_name}",
                "retryable": True, "max_same_failures": 2}
    if re.search(r"(?:full_regression|green_regression|\bregression:)\s*", text, re.IGNORECASE):
        return {"failure_class": "regression", "failure_signature": "regression", "retryable": False}
    return {"failure_class": "transient_or_unclassified", "failure_signature": "", "retryable": True}


def fail(project: Path, stage: str, exc: Exception, attempt: int | None = None,
         metadata: dict | None = None, fingerprint: str = "") -> int:
    metadata = metadata or failure_metadata(exc)
    attempt_data = {"stage": stage, "attempt": attempt, "result": "failed", "reason": str(exc)[-2000:],
                    **{key: value for key, value in metadata.items() if key != "retryable"}}
    if fingerprint:
        attempt_data["input_fingerprint"] = fingerprint
    update_status(project, stage=stage, result="failed", detail=str(exc)[-2000:],
                  attempt=attempt_data)
    signature = metadata.get("failure_signature")
    if not signature:
        return 1
    history = load_json(project / "status.json").get("pipeline", {}).get("attempt_history", [])
    return sum(1 for item in history if item.get("result") == "failed"
               and item.get("failure_signature") == signature
               and item.get("input_fingerprint") == fingerprint)


def run_docker(project: Path, root: Path, timeout: int) -> None:
    target = project / "_evidence" / "docker_verification.json"
    fingerprint = load_json(project / "_evidence" / "preflight.json").get("fingerprint")
    if target.exists():
        cached = load_json(target)
        if cached.get("fingerprint") == fingerprint and cached.get("result") == "passed":
            return
    try:
        output = run_checked(command("build_docker.py", "verify", "--root", str(root),
                                     "--project", project.name, "--date", project.parent.name),
                             cwd=root, timeout=timeout)
    except Exception as exc:
        atomic_json(target, {"schema": 1, "fingerprint": fingerprint, "result": "failed", "error": str(exc)})
        raise
    atomic_json(target, {"schema": 1, "fingerprint": fingerprint, "result": "passed",
                         "output_tail": output[-5000:]})


def do_preflight(projects: list[Path], root: Path, args) -> None:
    versions = {version for project in projects if (version := declared_version(project))}
    uuid_versions = {version for project in projects
                     if (version := declared_version(project)) and lc_uuid_required(project)}
    toolchain_canary(root, versions=versions, require_lc_uuid=uuid_versions, force=args.force)
    failures = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers), thread_name_prefix="batch-local") as pool:
        futures = {pool.submit(preflight_project, p, root, calibration_runs=args.calibration_runs,
                               timeout=args.timeout, force=args.force): p for p in projects}
        for future in as_completed(futures):
            project = futures[future]
            try:
                result = future.result()
                if result.get("result") != "passed":
                    raise RuntimeError("; ".join(result.get("issues") or ["preflight gate failed"]))
                print(f"PASS {project.name}: local preflight")
            except Exception as exc:
                failures.append((project, exc))
                print(f"FAIL {project.name}: {exc}", file=sys.stderr)
    if failures:
        raise RuntimeError(f"local preflight failed for {len(failures)} record(s)")
    if not args.skip_docker:
        # Docker builds compete heavily for disk/cache; keep this pool separately bounded.
        with ThreadPoolExecutor(max_workers=max(1, args.docker_workers), thread_name_prefix="batch-docker") as pool:
            futures = {pool.submit(run_docker, p, root, args.timeout): p for p in projects}
            for future in as_completed(futures):
                project = futures[future]
                future.result()
                print(f"PASS {project.name}: docker")


def publish(project: Path, root: Path, args) -> None:
    if stage_passed(project, "g1_published"):
        return
    require_unchanged_inputs(project, root)
    status = load_json(project / "status.json")
    repo = str(status.get("repo") or project.name.rsplit("__", 1)[0])
    run_checked(command("github_project.py", "publish", "--root", str(root), "--repo-name", repo,
                        "--project", project.name, "--date", project.parent.name), cwd=root, timeout=args.timeout)
    delivery = load_json(project / "_evidence" / "repository_delivery.json")
    data = collection(project)
    data["repo_url"] = delivery.get("repo_url", data.get("repo_url", ""))
    atomic_json(project / "collection.json", data)
    mark(project, "g1_published")


def red(project: Path, root: Path, args) -> None:
    if stage_passed(project, "red_passed"):
        return
    require_unchanged_inputs(project, root)
    run_checked(command("run_evidence_trajectories.py", "generate", "--root", str(root),
                        "--project", project.name, "--date", project.parent.name, "--phase", "red",
                        "--skip-upload", "--timeout", str(args.model_timeout),
                        "--model-slots", str(args.model_workers)), cwd=root,
                timeout=args.model_timeout + 600)
    mark(project, "red_passed")


def main_trajectory(project: Path, root: Path, args) -> None:
    if stage_passed(project, "main_accepted"):
        return
    require_unchanged_inputs(project, root)
    data = collection(project)
    fingerprint = load_json(project / "status.json").get("pipeline", {}).get("input_fingerprint", "")
    max_attempts = args.main_attempts
    for attempt in range(1, max_attempts + 1):
        update_status(project, stage="main_running", result="running",
                      attempt={"stage": "main_running", "attempt": attempt, "result": "started",
                               "rerun_reason": args.rerun_reason or "automatic acceptance retry"})
        cmd = command(
            "run_trajectory.py", "run", "--env", str(project / "env"),
            "--prompt", str(project / "prompt.txt"), "--output", str(project / "trajectory.jsonl"),
            "--snapshot", str(project / ".base_snapshot"), "--max-attempts", "1",
            "--timeout", str(args.model_timeout), "--task-type", str(data["task_type"]),
            "--verify-cmds", str(data["verify_cmds"]), "--evaluator", str(project / "evaluator"),
            "--g1-manifest", str(project / "_delivery" / "g1_snapshot.json"),
            "--model-slots", str(args.model_workers),
        )
        try:
            run_checked(cmd, cwd=root, timeout=args.model_timeout + 900)
            acceptance = load_json(project / "_evidence" / "trajectory_acceptance.json")
            if acceptance.get("result") != "passed":
                raise RuntimeError("trajectory acceptance did not pass")
            update_status(project, attempt={"stage": "main_running", "attempt": attempt, "result": "passed"})
            mark(project, "main_accepted")
            return
        except Exception as exc:
            metadata = failure_metadata(exc)
            same_failures = fail(project, "main_running", exc, attempt, metadata, fingerprint)
            if not metadata.get("retryable", True):
                raise RuntimeError(
                    f"确定性失败不可自动重试（{metadata['failure_signature']}），请修正输入并重新 preflight"
                ) from exc
            if same_failures >= int(metadata.get("max_same_failures") or 10**9):
                raise RuntimeError(
                    f"重复确定性失败已熔断（{metadata['failure_signature']}，连续输入指纹下已出现 {same_failures} 次）"
                ) from exc
            if attempt == max_attempts:
                raise


def green(project: Path, root: Path, args) -> None:
    data = collection(project)
    if data.get("task_type") == "diagnosis":
        mark(project, "green_passed", "not applicable for diagnosis")
        mark(project, "finalized", "diagnosis delivers G1 only")
        return
    if not stage_passed(project, "green_passed"):
        run_checked(command("run_evidence_trajectories.py", "generate", "--root", str(root),
                            "--project", project.name, "--date", project.parent.name, "--phase", "green",
                            "--skip-upload", "--timeout", str(args.model_timeout),
                            "--model-slots", str(args.model_workers)), cwd=root,
                    timeout=args.model_timeout + 900)
        mark(project, "green_passed")


def finalize(project: Path, root: Path, args) -> None:
    data = collection(project)
    if data.get("task_type") == "diagnosis":
        return
    if not stage_passed(project, "finalized"):
        status = load_json(project / "status.json")
        repo = str(status.get("repo") or project.name.rsplit("__", 1)[0])
        run_checked(command("github_project.py", "finalize", "--root", str(root), "--repo-name", repo,
                            "--project", project.name, "--date", project.parent.name), cwd=root,
                    timeout=args.timeout)
        mark(project, "finalized")


def model_phases(project: Path, root: Path, args) -> None:
    """Run per-record model phases in order; each model call also takes a global slot."""
    red(project, root, args)
    main_trajectory(project, root, args)
    green(project, root, args)


def record_pipeline(project: Path, root: Path, args) -> None:
    """Advance one record independently; shared Git writes lock inside github_project.py."""
    lock = root / "_locks" / "records" / lock_name(str(project.resolve()))
    with resource_lock(lock, label=f"记录 {project.name}"):
        reconcile(project)
        publish(project, root, args)
        model_phases(project, root, args)
        finalize(project, root, args)


def run_record_pipelines(projects: list[Path], root: Path, args) -> None:
    failures = []
    workers = max(1, min(args.workers, len(projects)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="batch-record") as pool:
        futures = {pool.submit(record_pipeline, project, root, args): project for project in projects}
        for future in as_completed(futures):
            project = futures[future]
            try:
                future.result()
                print(f"PASS {project.name}: record pipeline")
            except Exception as exc:
                failures.append((project, exc))
                print(f"FAIL {project.name}: {exc}", file=sys.stderr)
    if failures:
        names = ", ".join(project.name for project, _ in failures)
        raise RuntimeError(f"record pipelines failed for {len(failures)} record(s): {names}")


def run_model_phases(projects: list[Path], root: Path, args) -> None:
    failures = []
    with ThreadPoolExecutor(max_workers=max(1, args.model_workers), thread_name_prefix="batch-model") as pool:
        futures = {pool.submit(model_phases, project, root, args): project for project in projects}
        for future in as_completed(futures):
            project = futures[future]
            try:
                future.result()
                print(f"PASS {project.name}: model phases")
            except Exception as exc:
                failures.append((project, exc))
                print(f"FAIL {project.name}: {exc}", file=sys.stderr)
    if failures:
        names = ", ".join(project.name for project, _ in failures)
        raise RuntimeError(f"model phases failed for {len(failures)} record(s): {names}")


def upload_one(project: Path, root: Path, args) -> None:
    if stage_passed(project, "uploaded"):
        return
    run_checked(command("run_evidence_trajectories.py", "upload", "--root", str(root),
                        "--project", project.name, "--date", project.parent.name), cwd=root, timeout=args.timeout)
    run_checked(command("upload_trajectory.py", "upload", "--root", str(root),
                        "--project", project.name, "--date", project.parent.name), cwd=root, timeout=args.timeout)
    mark(project, "uploaded")


def finish(projects: list[Path], root: Path, args) -> None:
    with ThreadPoolExecutor(max_workers=max(1, args.upload_workers), thread_name_prefix="batch-upload") as pool:
        futures = {pool.submit(upload_one, p, root, args): p for p in projects}
        for future in as_completed(futures):
            future.result()
    # Expensive spreadsheet and registry mirrors are each rebuilt once per batch.
    run_checked(command("collection_table.py", "sync", "--root", str(root)), cwd=root, timeout=args.timeout)
    if args.projects:
        for project in projects:
            run_checked(command("post_qc.py", "--root", str(root), "--project", project.name,
                                "--date", project.parent.name, "--workers", "1"),
                        cwd=root, timeout=max(args.timeout, 7200))
    else:
        qc_cmd = command("post_qc.py", "--root", str(root), "--workers", str(args.workers))
        if args.date:
            qc_cmd.extend(["--date", args.date])
        run_checked(qc_cmd, cwd=root, timeout=max(args.timeout, 7200))
    for project in projects:
        data = collection(project)
        status = load_json(project / "status.json")
        repo_url = str(data.get("repo_url") or "")
        run_checked(command("repo_registry.py", "register", repo_url or str(status.get("repo") or project.name),
                            "--source", "auto", "--github-url", repo_url,
                            "--project", project.name, "--date", project.parent.name,
                            "--note", f"bug {data.get('bug_id', '')}"), cwd=root, timeout=args.timeout)
        run_checked(command("workspace.py", "set", "--root", str(root), "--project", project.name,
                            "--date", project.parent.name, "--state", "done"), cwd=root, timeout=args.timeout)
        mark(project, "done")
    run_checked(command("repo_registry.py", "sync", "--root", str(root)), cwd=root, timeout=args.timeout)


def status_report(projects: list[Path]) -> None:
    for project in projects:
        status = load_json(project / "status.json")
        pipeline = status.get("pipeline", {})
        stage = pipeline.get("stage") or f"legacy:{status.get('state', 'unknown')}"
        print(f"{project.name}\t{stage}\tattempts={len(pipeline.get('attempt_history', []))}")


def parse_args():
    parser = argparse.ArgumentParser(description="Resumable Go annotation batch pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "run", "resume", "status"):
        command_parser = sub.add_parser(name)
        command_parser.add_argument("--root", default=".")
        command_parser.add_argument("--date")
        command_parser.add_argument("--project", action="append", dest="projects")
        command_parser.add_argument("--workers", type=int, default=3)
        command_parser.add_argument("--docker-workers", type=int, default=1)
        command_parser.add_argument("--upload-workers", type=int, default=3)
        command_parser.add_argument("--model-workers", type=int, choices=(1, 2), default=2,
                                    help="目标模型并发数；默认 2，设为 1 可恢复串行")
        command_parser.add_argument("--calibration-runs", type=int, default=20)
        command_parser.add_argument("--timeout", type=int, default=3600)
        command_parser.add_argument("--model-timeout", type=int, default=1800)
        command_parser.add_argument("--main-attempts", type=int, default=3)
        command_parser.add_argument("--rerun-reason")
        command_parser.add_argument("--skip-docker", action="store_true")
        command_parser.add_argument("--skip-upload", action="store_true")
        command_parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    projects = iter_projects(root, args.date, set(args.projects or []))
    if args.command == "status":
        status_report(projects)
        return 0
    projects = [p for p in projects if load_json(p / "status.json").get("state") not in {"done", "rejected"}]
    if not projects:
        print("no records found", file=sys.stderr)
        return 1
    try:
        if args.command == "preflight":
            do_preflight(projects, root, args)
            return 0
        if args.command == "run":
            do_preflight(projects, root, args)
        elif not all(stage_passed(p, "preflight_passed") for p in projects):
            raise RuntimeError("resume requires every selected record to have passed preflight")
        # Records advance independently. Git writes are serialized per staging repo,
        # while model work and records backed by different repos can overlap.
        run_record_pipelines(projects, root, args)
        if not args.skip_upload:
            finish(projects, root, args)
        return 0
    except Exception as exc:
        print(f"batch stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
