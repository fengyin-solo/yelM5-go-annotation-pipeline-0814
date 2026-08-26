#!/usr/bin/env python3
"""Validate and submit qualified collection rows to go.jzxhnh.com.

The submission ledger is the local exactly-once guard. Every request is marked
in_flight before it is sent, then atomically changed to submitted, failed, or
uncertain. Automatic retries never resend in_flight/uncertain records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path

from resource_lock import resource_lock


DEFAULT_BASE_URL = "https://go.jzxhnh.com"
LOCAL_CONFIG = Path.home() / ".codex" / "go-annotation-pipeline" / "config.json"
LEGACY_CONFIG = Path.home() / ".codex" / "push_go_label" / "config.json"

HEADER_MAP = {"session id": "session_id"}
ONLINE_FIELDS = [
    "bug_id", "session_id", "task_type", "bug_category", "repo_url",
    "go_version", "repro_determinism", "user_query", "verify_cmds",
    "gold_root_cause", "success_criteria", "verify_result", "harness",
    "generator_model", "trajectory",
]
REQUIRED_FIELDS = [
    "bug_id", "session_id", "task_type", "bug_category", "repo_url",
    "go_version", "repro_determinism", "user_query", "verify_cmds",
    "success_criteria", "verify_result", "harness", "generator_model",
    "trajectory",
]
TASK_TYPES = {"bugfix", "diagnosis"}
BUG_CATEGORIES = {
    "concurrency并发问题", "nil相关问题", "slice相关问题",
    "error异常错误", "context相关问题", "defer相关问题", "其他问题",
}
REPRO_DETERMINISMS = {"deterministic", "flaky"}
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class PlatformSubmitError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlatformSubmitError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PlatformSubmitError(f"JSON root must be an object: {path}")
    return value


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def read_xlsx(path: Path) -> list[dict]:
    try:
        import openpyxl
    except ImportError as exc:
        raise PlatformSubmitError("missing dependency: openpyxl") from exc
    if not path.is_file():
        raise PlatformSubmitError(f"collection workbook does not exist: {path}")
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        header = None
        rows = []
        for index, values in enumerate(sheet.iter_rows(values_only=True), start=1):
            if index == 1:
                header = [_text(cell) for cell in values]
                continue
            if all(not _text(cell) for cell in values):
                continue
            raw = {name: value for name, value in zip(header or [], values) if name}
            normalized = {HEADER_MAP.get(name, name): _text(value) for name, value in raw.items()}
            rows.append({"row": index, "data": normalized})
        return rows
    finally:
        workbook.close()


def identity(data: dict) -> tuple[str, str]:
    return _text(data.get("bug_id")), _text(data.get("session_id"))


def identity_key(value: tuple[str, str]) -> str:
    return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))


def select_rows(rows: list[dict], records: list[list[str]] | None) -> list[dict]:
    if not records:
        return rows
    requested = {(str(item[0]), str(item[1])) for item in records}
    selected = [row for row in rows if identity(row["data"]) in requested]
    found = {identity(row["data"]) for row in selected}
    missing = requested - found
    if missing:
        rendered = ", ".join(f"{bug_id}/{session_id}" for bug_id, session_id in sorted(missing))
        raise PlatformSubmitError(f"requested records are missing from workbook: {rendered}")
    return selected


def validate(data: dict) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    for field in REQUIRED_FIELDS:
        if not data.get(field):
            errors.append(f"required field is empty: {field}")
    task_type = data.get("task_type")
    if task_type and task_type not in TASK_TYPES:
        errors.append(f"invalid task_type: {task_type!r}")
    category = data.get("bug_category")
    if category and category not in BUG_CATEGORIES:
        errors.append(f"invalid bug_category: {category!r}")
    determinism = data.get("repro_determinism")
    if determinism and determinism not in REPRO_DETERMINISMS:
        errors.append(f"invalid repro_determinism: {determinism!r}")
    session_id = data.get("session_id")
    if session_id and not UUID_RE.fullmatch(session_id):
        errors.append(f"session_id is not a UUID: {session_id!r}")
    repo_url = data.get("repo_url")
    if repo_url and not (URL_RE.match(repo_url) or repo_url.startswith("git@")):
        errors.append(f"invalid repo_url: {repo_url!r}")
    trajectory = data.get("trajectory")
    if trajectory and not URL_RE.match(trajectory) and not Path(trajectory).is_file():
        errors.append(f"trajectory is neither a URL nor a local file: {trajectory[:80]!r}")
    if task_type == "diagnosis" and not data.get("gold_root_cause"):
        errors.append("diagnosis requires gold_root_cause")
    verify_result = data.get("verify_result")
    if verify_result:
        try:
            parsed = json.loads(verify_result)
            if not isinstance(parsed, dict):
                raise ValueError("root must be an object")
            if task_type == "bugfix":
                if "pre_fix" not in parsed:
                    errors.append("bugfix verify_result is missing pre_fix")
                if "post_fix" not in parsed:
                    errors.append("bugfix verify_result is missing post_fix")
            elif task_type == "diagnosis":
                if "pre_fix" not in parsed:
                    errors.append("diagnosis verify_result is missing pre_fix")
                if "post_fix" in parsed:
                    warnings.append("diagnosis verify_result should not contain post_fix")
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"verify_result is not valid JSON: {exc}")
    if determinism == "flaky" and task_type == "bugfix":
        warnings.append("flaky records are normally diagnosis-only")
    return errors, warnings


def validate_all(rows: list[dict]) -> None:
    if not rows:
        raise PlatformSubmitError("no selected data rows")
    all_errors = []
    seen: dict[tuple[str, str], int] = {}
    for row in rows:
        row_identity = identity(row["data"])
        if row_identity in seen:
            all_errors.append(
                f"row {row['row']}: duplicate bug_id/session_id (first seen at row {seen[row_identity]})"
            )
        else:
            seen[row_identity] = row["row"]
        errors, warnings = validate(row["data"])
        for warning in warnings:
            print(f"WARN row {row['row']}: {warning}")
        all_errors.extend(f"row {row['row']}: {error}" for error in errors)
    if all_errors:
        raise PlatformSubmitError("workbook validation failed:\n- " + "\n- ".join(all_errors))


def build_payload(data: dict) -> dict:
    return {field: data[field] for field in ONLINE_FIELDS if field != "trajectory" and data.get(field)}


def payload_fingerprint(data: dict) -> str:
    material = {"data": build_payload(data), "trajectory": data.get("trajectory", "")}
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def workbook_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_credentials(args) -> tuple[str, str, str]:
    local = load_json(LOCAL_CONFIG)
    legacy = load_json(LEGACY_CONFIG)
    username = (
        getattr(args, "username", None) or os.environ.get("GOQA_USERNAME")
        or local.get("platform_username") or legacy.get("username") or ""
    )
    password = (
        getattr(args, "password", None) or os.environ.get("GOQA_PASSWORD")
        or local.get("platform_password") or legacy.get("password") or ""
    )
    base_url = (
        getattr(args, "base_url", None) or os.environ.get("GOQA_BASE_URL")
        or local.get("platform_base_url") or legacy.get("base_url") or DEFAULT_BASE_URL
    )
    return str(username), str(password), str(base_url).rstrip("/")


class TLS12Adapter:
    """Factory kept separate so importing the module does not require requests."""

    @staticmethod
    def build():
        from requests.adapters import HTTPAdapter

        class Adapter(HTTPAdapter):
            def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
                context = ssl.create_default_context()
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.maximum_version = ssl.TLSVersion.TLSv1_2
                kwargs["ssl_context"] = context
                return super().init_poolmanager(connections, maxsize, block=block, **kwargs)

        return Adapter()


class PlatformClient:
    def __init__(self, base_url: str, username: str, password: str):
        try:
            import requests
        except ImportError as exc:
            raise PlatformSubmitError("missing dependency: requests") from exc
        self.base_url = base_url
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.mount("https://", TLS12Adapter.build())

    def login(self) -> None:
        try:
            response = self.session.post(
                self.base_url + "/api/v1/auth/login",
                json={"username": self.username, "password": self.password},
                timeout=30,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            raise PlatformSubmitError(f"platform login failed: {exc}") from exc
        if body.get("code") != 0:
            raise PlatformSubmitError(f"platform login rejected: {body}")
        if not self.session.cookies.get("go_qa_csrf"):
            raise PlatformSubmitError("platform login succeeded without go_qa_csrf cookie")

    def submit(self, data: dict) -> dict:
        form = {"data": json.dumps(build_payload(data), ensure_ascii=False)}
        files = None
        trajectory = data.get("trajectory", "")
        handle = None
        if URL_RE.match(trajectory):
            form["trajectory_url"] = trajectory
        else:
            handle = open(trajectory, "rb")
            files = {"trajectory": (Path(trajectory).name, handle, "application/octet-stream")}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.base_url,
            "Referer": self.base_url + "/u/submit",
            "User-Agent": "go-annotation-pipeline-platform-submit/1.0",
            "X-CSRF-Token": self.session.cookies.get("go_qa_csrf", ""),
        }
        try:
            try:
                response = self.session.post(
                    self.base_url + "/api/v1/submissions",
                    data=form,
                    files=files,
                    headers=headers,
                    timeout=300,
                )
            except Exception as exc:
                raise PlatformSubmitError(f"submission request failed: {exc}", uncertain=True) from exc
            try:
                body = response.json()
            except Exception as exc:
                raise PlatformSubmitError(
                    f"submission returned non-JSON HTTP {response.status_code}: {response.text[:300]}",
                    uncertain=response.status_code < 400 or response.status_code >= 500,
                ) from exc
            if response.status_code >= 500:
                raise PlatformSubmitError(
                    f"submission returned HTTP {response.status_code}: {body}", uncertain=True
                )
            if response.status_code >= 400 or body.get("code") != 0:
                raise PlatformSubmitError(f"submission rejected: HTTP {response.status_code}: {body}")
            result = body.get("data") if isinstance(body.get("data"), dict) else {}
            if not result.get("id"):
                raise PlatformSubmitError("submission response is missing submission_id", uncertain=True)
            return result
        finally:
            if handle is not None:
                handle.close()


def initial_ledger(base_url: str) -> dict:
    return {"schema": 1, "base_url": base_url, "submissions": {}}


def format_submission_summary(report: dict) -> str:
    """Render the fixed user-facing summary for a completed submission run."""
    identifiers = []
    for record in report.get("records") or []:
        if record.get("state") not in {"submitted", "skipped"}:
            continue
        submission_id = str(record.get("submission_id") or "").strip()
        if not submission_id:
            raise PlatformSubmitError(
                f"completed record is missing submission_id: {record.get('bug_id') or '(unknown)'}"
            )
        identifiers.append(f"- {record.get('bug_id') or '(unknown)'}：{submission_id}")
    lines = [
        "平台上传摘要：",
        f"上传成功：{int(report.get('submitted') or 0)} 条",
        f"跳过：{int(report.get('skipped') or 0)} 条",
        "提交 ID：",
    ]
    lines.extend(identifiers or ["- 无"])
    return "\n".join(lines)


def submission_plan(rows: list[dict], ledger: dict, retry_uncertain: bool) -> tuple[list[dict], list[dict]]:
    pending, skipped = [], []
    entries = ledger.setdefault("submissions", {})
    for row in rows:
        row_identity = identity(row["data"])
        key = identity_key(row_identity)
        fingerprint = payload_fingerprint(row["data"])
        existing = entries.get(key) or {}
        state = existing.get("state")
        if state in {"in_flight", "uncertain"} and existing.get("payload_sha256") != fingerprint:
            raise PlatformSubmitError(
                f"ambiguous record changed since its prior request: {row_identity[0]}/{row_identity[1]}"
            )
        if state == "submitted":
            if existing.get("payload_sha256") != fingerprint:
                raise PlatformSubmitError(
                    f"already-submitted record changed: {row_identity[0]}/{row_identity[1]}"
                )
            skipped.append({
                "bug_id": row_identity[0], "session_id": row_identity[1], "state": "skipped",
                "submission_id": existing.get("submission_id"), "status": existing.get("status"),
            })
            continue
        if state in {"in_flight", "uncertain"} and not retry_uncertain:
            raise PlatformSubmitError(
                f"record has ambiguous prior state {state}; reconcile it before retrying: "
                f"{row_identity[0]}/{row_identity[1]}"
            )
        pending.append({**row, "key": key, "payload_sha256": fingerprint})
    return pending, skipped


def submit_selected(args) -> dict:
    workbook = Path(args.xlsx).resolve()
    rows = select_rows(read_xlsx(workbook), args.record)
    validate_all(rows)
    print(f"PASS workbook validation: {len(rows)}/{len(rows)} selected rows")
    if args.dry_run:
        return {
            "schema": 1, "dry_run": True, "workbook": str(workbook),
            "workbook_sha256": workbook_fingerprint(workbook), "total": len(rows),
            "records": [
                {"bug_id": identity(row["data"])[0], "session_id": identity(row["data"])[1], "state": "validated"}
                for row in rows
            ],
        }

    username, password, base_url = load_credentials(args)
    if not username or not password:
        raise PlatformSubmitError(
            "platform credentials are missing; configure platform_username/platform_password "
            "or GOQA_USERNAME/GOQA_PASSWORD"
        )
    ledger_path = Path(args.ledger).resolve() if args.ledger else workbook.parent / "platform-submissions.json"
    result_path = Path(args.result).resolve() if args.result else workbook.parent / "platform-submit-result.json"
    lock_path = ledger_path.with_name(ledger_path.name + ".lock")
    with resource_lock(lock_path, label="platform submission ledger"):
        ledger = load_json(ledger_path) if ledger_path.exists() else initial_ledger(base_url)
        if ledger.get("base_url") and ledger.get("base_url") != base_url:
            raise PlatformSubmitError(
                f"ledger base_url mismatch: {ledger.get('base_url')} != {base_url}"
            )
        pending, results = submission_plan(rows, ledger, args.retry_uncertain)
        if pending:
            client = PlatformClient(base_url, username, password)
            client.login()
            print(f"PASS platform login: {username}")
        for row in pending:
            data = row["data"]
            bug_id, session_id = identity(data)
            entry = {
                "bug_id": bug_id, "session_id": session_id,
                "payload_sha256": row["payload_sha256"], "state": "in_flight", "updated_at": now(),
            }
            ledger["submissions"][row["key"]] = entry
            ledger["updated_at"] = now()
            atomic_json(ledger_path, ledger)
            try:
                response = client.submit(data)
            except PlatformSubmitError as exc:
                entry.update({
                    "state": "uncertain" if exc.uncertain else "failed",
                    "error": str(exc), "updated_at": now(),
                })
                ledger["updated_at"] = now()
                atomic_json(ledger_path, ledger)
                results.append({
                    "bug_id": bug_id, "session_id": session_id,
                    "state": entry["state"], "error": str(exc),
                })
                if exc.uncertain:
                    break
                continue
            entry.update({
                "state": "submitted", "submission_id": response.get("id"),
                "status": response.get("status"), "updated_at": now(),
            })
            ledger["updated_at"] = now()
            atomic_json(ledger_path, ledger)
            results.append({
                "bug_id": bug_id, "session_id": session_id, "state": "submitted",
                "submission_id": response.get("id"), "status": response.get("status"),
            })
            print(f"PASS submitted {bug_id}: submission_id={response.get('id')} status={response.get('status')}")

        report = {
            "schema": 1, "dry_run": False, "base_url": base_url, "workbook": str(workbook),
            "workbook_sha256": workbook_fingerprint(workbook), "updated_at": now(),
            "total": len(rows),
            "submitted": sum(item["state"] == "submitted" for item in results),
            "skipped": sum(item["state"] == "skipped" for item in results),
            "records": results,
        }
        atomic_json(result_path, report)
        incomplete = [item for item in results if item["state"] not in {"submitted", "skipped"}]
        if len(results) != len(rows) or incomplete:
            raise PlatformSubmitError(
                f"platform submission incomplete; inspect {ledger_path} and {result_path} before retrying"
            )
        print(format_submission_summary(report))
        return report


def cmd_check(args) -> None:
    rows = select_rows(read_xlsx(Path(args.xlsx).resolve()), args.record)
    validate_all(rows)
    print(f"PASS workbook validation: {len(rows)}/{len(rows)} selected rows")


def cmd_login_test(args) -> None:
    username, password, base_url = load_credentials(args)
    if not username or not password:
        raise PlatformSubmitError("platform credentials are missing")
    client = PlatformClient(base_url, username, password)
    client.login()
    print(f"PASS platform login: {username} at {base_url}")


def add_credentials(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--base-url")


def add_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--xlsx", required=True)
    parser.add_argument(
        "--record", action="append", nargs=2, metavar=("BUG_ID", "SESSION_ID"),
        help="only process this bug_id/session_id pair; repeat for multiple records",
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Submit qualified Go annotation rows to the labeling platform")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("check", help="validate the workbook without network access")
    add_selection(command)
    command.set_defaults(func=cmd_check)
    command = sub.add_parser("login-test", help="verify credentials without submitting data")
    add_credentials(command)
    command.set_defaults(func=cmd_login_test)
    command = sub.add_parser("submit", help="validate, resume safely, and submit selected rows")
    add_selection(command)
    add_credentials(command)
    command.add_argument("--ledger")
    command.add_argument("--result")
    command.add_argument("--dry-run", action="store_true")
    command.add_argument(
        "--retry-uncertain", action="store_true",
        help="explicitly retry in_flight/uncertain rows after external reconciliation",
    )
    command.set_defaults(func=submit_selected)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        args.func(args)
        return 0
    except PlatformSubmitError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
