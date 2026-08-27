#!/usr/bin/env python3
"""0-1 自建项目的 GitHub 仓库与分支管理。

GitHub 凭据/作者一律读取 pg-code 的 `~/.codex/pg-code/github-context.json`，不读取全局 git
配置或 shell 环境。安全规则：日志和输出可以出现账号/作者，但禁止输出 token。

分支模型（一个 repo 最多 30 条记录）：
    bugfix:    bug<record>_green  G1 bug 单提交 -> G2 模型修复+测试
               bug<record>_red    R1 bug 代码+同一测试（orphan 单提交）
    diagnosis: bug<record>_red    轨迹后发布 bug 代码+测试（orphan 单提交），不创建 green

本地 `_gold/` 只用于红绿校准、难度检查和回归验证，不创建远程分支。
每个 bug 的 green/red 都独立生根，不得从 main 或其他 bug 分支派生。

用法:
  github_project.py ensure --root <dir> --repo-name <name> --local-path <clean_project_dir>
  github_project.py publish --root <dir> --repo-name <name> --project <name>__<record> \
      [--date YYYY-MM-DD] [--bug-id <id>]
  github_project.py finalize --root <dir> --repo-name <name> --project <name>__<record> \
      [--date YYYY-MM-DD] [--bug-id <id>]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trajectory_guard import (  # noqa: E402
    copy_evaluator_to_repo,
    evaluator_files,
    is_test_artifact,
    private_test_issues,
    source_manifest,
    write_source_manifest,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_docker import make_dockerfile, detect_go_version, find_go_mod  # noqa: E402
from change_scope import (  # noqa: E402
    MIN_FUNCTIONAL_CHANGED_LINES,
    functional_go_diff_from_numstat,
    meets_minimum_functional_change,
)
from domain_guard import validate_project_domain  # noqa: E402
from project_summary import read_project_summary, validate_project_summary  # noqa: E402
from resource_lock import lock_name, resource_lock  # noqa: E402

DEFAULT_CONTEXT = Path.home() / ".codex" / "pg-code" / "github-context.json"
DEFAULT_REMOTE = "origin"


def repo_write_lock(root: Path, repo_name: str):
    base = slugify(repo_name)
    resolved_name = resolve_repo_name(root, base) or base
    key = str((root / "_repos" / resolved_name).resolve())
    return resource_lock(
        root / "_locks" / "repos" / lock_name(key),
        label=f"staging Git 仓库 {resolved_name}",
    )


def run_git(repo: Path, *args: str, env: dict[str, str] | None = None, check: bool = True):
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result


def load_context(path: Path | None = None) -> dict:
    p = Path(path or DEFAULT_CONTEXT).expanduser()
    if not p.exists():
        raise RuntimeError(f"pg-code GitHub context not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    github = data.get("github") or {}
    if not github.get("username") or not github.get("token"):
        raise RuntimeError("pg-code GitHub context is missing username or token")
    author = data.get("gitAuthor") or {}
    if not author.get("name") or not author.get("email"):
        raise RuntimeError("pg-code GitHub context is missing gitAuthor.name or gitAuthor.email")
    if author.get("name") == "PINRU Local":
        raise RuntimeError("pg-code GitHub context gitAuthor.name must not be PINRU Local")
    return data


def git_auth_env(remote_url: str, username: str, token: str) -> dict[str, str]:
    parsed = urlparse(remote_url.strip())
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if parsed.scheme and parsed.netloc:
        base_url = f"{parsed.scheme}://{parsed.netloc}/"
        auth = base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = f"http.{base_url}.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {auth}"
    return env


def git_identity_env(base_env: dict[str, str], author_name: str, author_email: str) -> dict[str, str]:
    env = base_env.copy()
    env["GIT_AUTHOR_NAME"] = author_name
    env["GIT_AUTHOR_EMAIL"] = author_email
    env["GIT_COMMITTER_NAME"] = author_name
    env["GIT_COMMITTER_EMAIL"] = author_email
    return env


def api_request(method: str, url: str, token: str, data: dict | None = None) -> dict:
    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "go-annotation-pipeline",
    }
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"status": resp.status, "data": json.loads(resp.read().decode("utf-8") or "{}")}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        try:
            detail = json.loads(raw)
        except Exception:
            detail = {"message": raw}
        return {"status": exc.code, "data": detail}


def slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9_.-]+", "-", s)
    return s.strip("._-") or "task"


def ensure_github_repo(username: str, token: str, repo_name: str) -> str:
    repo_name = slugify(repo_name)
    resp = api_request("POST", "https://api.github.com/user/repos", token, {
        "name": repo_name,
        "private": False,
        "auto_init": False,
        "description": "0-1 Go annotation project",
    })
    if resp["status"] not in (200, 201):
        msg = resp["data"].get("message") or resp["data"]
        if "name already exists" in str(msg).lower() or resp["status"] == 422:
            return f"https://github.com/{username}/{repo_name}.git"
        raise RuntimeError(f"create GitHub repo failed: {resp['status']} {msg}")
    return f"https://github.com/{username}/{repo_name}.git"


def sync_bug_source(src: Path, dst: Path) -> None:
    """Sync model-visible source while removing all pre-existing test assets."""
    dst.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([
        "rsync", "-a", "--checksum", "--delete",
        "--exclude=.git", "--exclude=*_test.go", "--exclude=evaluator/",
        "--exclude=test/", "--exclude=tests/", "--exclude=testdata/",
        "--exclude=test_*", "--exclude=*_test.*", "--exclude=*.test.*", "--exclude=*.spec.*",
        "--exclude=node_modules", "--exclude=dist", "--exclude=build",
        "--exclude=*.log", "--exclude=*.jsonl", "--exclude=.env", "--exclude=.env.*",
        "--exclude=.DS_Store", "--exclude=SOURCE.txt", "--exclude=*.source.txt",
        "--exclude=BUG_REPRO.md", "--exclude=project_summary.txt",
        str(src).rstrip("/") + "/", str(dst).rstrip("/") + "/",
    ], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"rsync failed: {r.stderr[:300]}")


def clear_worktree(repo: Path) -> None:
    """Remove only the staging repository worktree, preserving .git."""
    resolved = repo.resolve()
    if not (resolved / ".git").is_dir() or resolved.name in {"", ".", ".."}:
        raise RuntimeError(f"拒绝清理非 Git staging repo: {resolved}")
    for child in resolved.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def delivery_branches(record: str) -> tuple[str, str]:
    return f"bug{record}_green", f"bug{record}_red"


def _branch_exists(repo: Path, branch: str, *, remote: bool = True) -> bool:
    if run_git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        return True
    if remote:
        result = run_git(repo, "ls-remote", "--exit-code", "--heads", DEFAULT_REMOTE, branch, check=False)
        return result.returncode == 0 and bool(result.stdout.strip())
    return False


def _remote_branches(repo: Path) -> list[str]:
    result = run_git(repo, "ls-remote", "--heads", DEFAULT_REMOTE)
    return sorted(line.split("refs/heads/", 1)[1] for line in result.stdout.splitlines() if "refs/heads/" in line)


def _assert_no_tests(repo: Path, revision: str) -> None:
    names = run_git(repo, "ls-tree", "-r", "--name-only", revision).stdout.splitlines()
    tests = [name for name in names if is_test_artifact(name)]
    if tests:
        raise RuntimeError(f"G1 必须不含任何测试文件/夹: {', '.join(tests[:8])}")


def _assert_no_symlinks(repo: Path) -> None:
    links = [str(path.relative_to(repo)) for path in repo.rglob("*") if ".git" not in path.parts and path.is_symlink()]
    if links:
        raise RuntimeError("G1 模型快照不允许符号链接，防止越界读取: " + ", ".join(links[:8]))


def _tree_entries(repo: Path, revision: str, *, tests: bool) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in run_git(repo, "ls-tree", "-r", revision).stdout.splitlines():
        meta, path = line.split("\t", 1)
        sha = meta.split()[2]
        if is_test_artifact(path) == tests:
            result[path] = sha
    return result


def _evaluator_entries(repo: Path, evaluator: Path) -> dict[str, str]:
    return {
        str(path.relative_to(evaluator)): run_git(repo, "hash-object", str(path)).stdout.strip()
        for path in evaluator_files(evaluator)
    }


def _write_delivery_metadata(proj: Path, data: dict) -> None:
    target = proj / "_evidence" / "repository_delivery.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


DELIVERY_ROOT_FILES = (
    "benzhi.Dockerfile",
    "build_benzhi_docker.sh",
    "BENZHI_README.md",
    ".dockerignore",
)


def make_build_script() -> str:
    return """#!/bin/bash
# 请在仓库根目录运行；第二个参数为目标平台（arm64 / amd64）。
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"
IMAGE_NAME=${1:-my-go-task}
PLATFORM=${2:-linux/amd64}

docker buildx build --platform "$PLATFORM" -f benzhi.Dockerfile -t "$IMAGE_NAME" .

echo ""
echo "✅ Docker image '$IMAGE_NAME' built successfully!"
echo "📋 进入容器: docker run -it $IMAGE_NAME bash"
"""


def make_readme(project: str, project_summary: str, go_version: str, module_rel: Path) -> str:
    module_dir = "." if module_rel == Path(".") else module_rel.as_posix()
    container_workdir = "/app" if module_dir == "." else f"/app/{module_dir}"
    return f"""{project_summary}

# {project}

## 构建镜像

请从**仓库根目录**执行；`benzhi.Dockerfile`、`build_benzhi_docker.sh`、`BENZHI_README.md` 均固定在该目录：

```bash
./build_benzhi_docker.sh <image-name> [linux/amd64|linux/arm64]
```

## 标准命令

```bash
go build ./...     # 编译
go run ./cmd/app   # 启动（如项目可运行）
go test ./...      # 测试（如有）
```

## 环境

- 基础镜像: golang:{go_version}
- Go 模块目录: `{module_dir}`
- 依赖已在镜像构建阶段预下载，容器内离线可用。
- 容器内工作目录: `{container_workdir}`
"""


def _remove_stale_nested_delivery_files(repo: Path) -> None:
    """删除旧版本写到模块子目录的交付文件，避免发布时出现错误副本。"""
    for name in DELIVERY_ROOT_FILES:
        for candidate in repo.rglob(name):
            if candidate.parent == repo or ".git" in candidate.parts:
                continue
            if candidate.is_file() or candidate.is_symlink():
                candidate.unlink()
            else:
                raise RuntimeError(f"交付文件路径不是普通文件，无法安全清理: {candidate}")


def _assert_root_delivery_files(repo: Path) -> None:
    """交付三件套只能位于 GitHub 仓库根目录，防止嵌套模块时漏检。"""
    missing = [name for name in DELIVERY_ROOT_FILES if not (repo / name).is_file()]
    nested = [
        str(candidate.relative_to(repo))
        for name in DELIVERY_ROOT_FILES
        for candidate in repo.rglob(name)
        if candidate.parent != repo and ".git" not in candidate.parts
    ]
    forbidden_repro = [
        str(path.relative_to(repo)) for path in repo.rglob("*")
        if ".git" not in path.parts and path.name.lower() == "bug_repro.md"
    ]
    if missing or nested or forbidden_repro:
        parts = []
        if missing:
            parts.append("根目录缺少: " + ", ".join(missing))
        if nested:
            parts.append("子目录存在违规副本: " + ", ".join(sorted(nested)))
        if forbidden_repro:
            parts.append("禁止 BUG_REPRO.md: " + ", ".join(sorted(forbidden_repro)))
        raise RuntimeError("交付文件位置校验失败；benzhi.Dockerfile、build_benzhi_docker.sh、BENZHI_README.md 必须位于仓库根目录（/）: " + "；".join(parts))


def ensure_delivery_files(repo: Path, project: str, project_summary: str, module_path: str | None = None) -> None:
    """生成交付文件并强制其位于 GitHub 仓库根目录。

    即使 go.mod 位于子目录（如 backend/），Docker 构建上下文仍固定为仓库根目录；
    Dockerfile 会切换到模块目录执行 Go 命令。module_path 缺省时自动探测一层子目录。
    """
    summary_issues = validate_project_summary(project_summary)
    if summary_issues:
        raise RuntimeError("项目类型简介不合格：" + "；".join(summary_issues))
    mod = find_go_mod(repo, module_path)
    if not mod:
        print(f"⚠️  未找到 go.mod（repo 根目录或一层子目录），跳过交付文件生成: {repo}")
        return
    module_rel = mod.parent.relative_to(repo)
    go_version = detect_go_version(mod)
    _remove_stale_nested_delivery_files(repo)
    # BUG_REPRO.md is no longer part of the project or delivery branches.
    for stale_repro in repo.rglob("*"):
        if (".git" not in stale_repro.parts and stale_repro.name.lower() == "bug_repro.md"
                and (stale_repro.exists() or stale_repro.is_symlink())):
            stale_repro.unlink()
    (repo / "benzhi.Dockerfile").write_text(
        make_dockerfile(go_version, (mod.parent / "go.sum").exists(), module_rel.as_posix()),
        encoding="utf-8",
    )
    build_sh = repo / "build_benzhi_docker.sh"
    build_sh.write_text(make_build_script(), encoding="utf-8")
    os.chmod(build_sh, 0o755)
    (repo / "BENZHI_README.md").write_text(
        make_readme(project, project_summary, go_version, module_rel), encoding="utf-8"
    )
    (repo / ".dockerignore").write_text(".git\n*.log\n*.jsonl\nnode_modules\ndist\nbuild\n.env\n.env.*\n", encoding="utf-8")
    _assert_root_delivery_files(repo)


def central_repo_dir(root: Path, repo_name: str) -> Path:
    return root / "_repos" / slugify(repo_name)


_NAME_MAP = "_name_map.json"


def _name_map_path(root: Path) -> Path:
    return root / "_repos" / _NAME_MAP


def _load_name_map(root: Path) -> dict:
    p = _name_map_path(root)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_name_map(root: Path, m: dict) -> None:
    p = _name_map_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_repo_name(root: Path, base: str) -> str | None:
    """把用户传入的 base 名解析为实际 GitHub repo 名（带随机码）。"""
    m = _load_name_map(root)
    if base in m:
        return m[base]
    # 兼容旧仓库：没有映射但本地已有同名 central repo
    if (root / "_repos" / base / ".git").exists():
        return base
    return None


def assign_repo_name(root: Path, base: str) -> str:
    """返回实际 GitHub repo 名：默认直接用 base（真实项目名，无 go- 前缀、无随机码）；已有映射时复用旧名。"""
    return resolve_repo_name(root, base) or base


def _cmd_ensure_unlocked(args):
    root = Path(args.root)
    if not Path(args.local_path).is_dir():
        raise RuntimeError(f"local path not found: {args.local_path}")
    base = slugify(args.repo_name)
    domain_issues = validate_project_domain(Path(args.local_path), base)
    if domain_issues:
        raise RuntimeError(
            "候选项目命中禁止类型，已在创建 GitHub 仓库前停止："
            + "；".join(domain_issues)
            + "。必须更换项目，禁止只改仓库名或 README 措辞。"
        )
    repo_name = assign_repo_name(root, base)
    repo = central_repo_dir(root, repo_name)
    repo.parent.mkdir(parents=True, exist_ok=True)

    ctx = load_context()
    github = ctx["github"]
    author = ctx["gitAuthor"]
    username = github["username"]
    token = github["token"]
    author_name = author["name"]
    author_email = author["email"]

    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        run_git(repo, "init")
        run_git(repo, "config", "user.name", author_name)
        run_git(repo, "config", "user.email", author_email)

    remote_url = ensure_github_repo(username, token, repo_name)
    existing = run_git(repo, "remote", "get-url", DEFAULT_REMOTE, check=False)
    if existing.returncode != 0:
        run_git(repo, "remote", "add", DEFAULT_REMOTE, remote_url)
    else:
        run_git(repo, "remote", "set-url", DEFAULT_REMOTE, remote_url)

    html = remote_url[:-4] if remote_url.endswith(".git") else remote_url
    print(json.dumps({
        "ok": True, "repoName": repo_name, "repoUrl": html, "localRepo": str(repo),
        "publishedBaseline": False,
    }, ensure_ascii=False))


def cmd_ensure(args):
    root = Path(args.root).resolve()
    with repo_write_lock(root, args.repo_name):
        _cmd_ensure_unlocked(args)


def _cmd_publish_unlocked(args):
    root = Path(args.root)
    base = slugify(args.repo_name)
    repo_name = resolve_repo_name(root, base) or base
    proj_name = args.project.replace("/", "__").lower()
    proj = root / (args.date or __import__("datetime").date.today().isoformat()) / proj_name

    if not (proj / "env").exists():
        raise RuntimeError(f"bug env not found: {proj / 'env'}")

    collection_data = {}
    coll = proj / "collection.json"
    if coll.exists():
        collection_data = json.loads(coll.read_text(encoding="utf-8"))
    private_issues = private_test_issues(
        proj / "env", proj / "evaluator", collection_data.get("verify_cmds") or ""
    )
    if private_issues:
        raise RuntimeError("私有测试门禁失败：" + "；".join(private_issues))

    bug_id = args.bug_id
    if not bug_id:
        bug_id = collection_data.get("bug_id") or ""
    if not bug_id:
        raise RuntimeError("--bug-id 缺失，且 collection.json 中没有 bug_id")

    repo = central_repo_dir(root, repo_name)
    if not (repo / ".git").exists():
        raise RuntimeError(f"central repo not found: {repo}（请先运行 github_project.py ensure）")

    ctx = load_context()
    github = ctx["github"]
    author = ctx["gitAuthor"]
    username = github["username"]
    token = github["token"]
    author_name = author["name"]
    author_email = author["email"]

    remote_url = run_git(repo, "remote", "get-url", DEFAULT_REMOTE).stdout.strip()
    html = remote_url[:-4] if remote_url.endswith(".git") else remote_url
    auth_env = git_auth_env(remote_url, username, token)
    identity_env = git_identity_env(auth_env, author_name, author_email)

    task_type = (collection_data.get("task_type") or "bugfix").strip().lower()
    if task_type not in {"bugfix", "diagnosis"}:
        raise RuntimeError("collection.json.task_type 必须是 bugfix 或 diagnosis")
    record = proj_name.rsplit("__", 1)[-1] if "__" in proj_name else "001"
    green_branch, red_branch = delivery_branches(record)
    forbidden = [name for name in _remote_branches(repo) if not re.fullmatch(r"bug\d{3}_(?:green|red)", name)]
    if forbidden:
        raise RuntimeError("远程存在可用于反推答案的非交付分支: " + ", ".join(forbidden))
    if _branch_exists(repo, green_branch) or _branch_exists(repo, red_branch):
        raise RuntimeError(
            f"交付分支已存在: {green_branch}/{red_branch}；拒绝覆盖可审计历史"
        )

    delivery_branch = red_branch if task_type == "diagnosis" else green_branch
    run_git(repo, "checkout", "--orphan", delivery_branch)
    clear_worktree(repo)
    sync_bug_source(proj / "env", repo)
    _assert_no_symlinks(repo)
    ensure_delivery_files(repo, proj_name, read_project_summary(proj), getattr(args, "module_path", None))
    _assert_root_delivery_files(repo)
    run_git(repo, "add", "-A", "--", ".")
    run_git(repo, "commit", "-m", f"bug: {bug_id}", env=identity_env)
    g1_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    if run_git(repo, "rev-list", "--count", delivery_branch).stdout.strip() != "1":
        raise RuntimeError(f"{delivery_branch} 必须为 orphan 单提交")
    _assert_no_tests(repo, g1_sha)
    manifest_path = proj / "_delivery" / "g1_snapshot.json"
    write_source_manifest(repo, manifest_path, commit=g1_sha, branch=delivery_branch)
    pending_repo_url = f"{html}/tree/{delivery_branch}"
    if task_type == "diagnosis":
        state = "g1_prepared"
        repo_url = ""
    else:
        run_git(repo, "push", "-u", DEFAULT_REMOTE, delivery_branch, env=identity_env)
        state = "g1_published"
        repo_url = pending_repo_url

    _write_delivery_metadata(proj, {
        "schema": 1,
        "state": state,
        "task_type": task_type,
        "repo_url": repo_url,
        "pending_repo_url": pending_repo_url if task_type == "diagnosis" else "",
        "green_branch": green_branch if task_type == "bugfix" else "",
        "red_branch": red_branch,
        "g1_commit": g1_sha,
        "g1_manifest": str(manifest_path.relative_to(proj)),
    })
    print(json.dumps({
        "ok": True,
        "repoUrl": repo_url,
        "pendingRepoUrl": pending_repo_url if task_type == "diagnosis" else "",
        "deliveryBranch": delivery_branch,
        "greenBranch": green_branch if task_type == "bugfix" else "",
        "redBranch": red_branch,
        "g1Commit": g1_sha,
        "g1Manifest": str(manifest_path),
    }, ensure_ascii=False))


def cmd_publish(args):
    root = Path(args.root).resolve()
    with repo_write_lock(root, args.repo_name):
        _cmd_publish_unlocked(args)


def _assert_formal_acceptance(proj: Path, data: dict, task_type: str) -> str:
    ev = proj / "_evidence"
    guard_path = ev / "trajectory_guard.json"
    acceptance_path = ev / "trajectory_acceptance.json"
    missing = [path.name for path in (guard_path, acceptance_path) if not path.exists()]
    if missing:
        raise RuntimeError("正式轨迹验收未完成（缺少 _evidence/" + "、_evidence/".join(missing) + "）")

    expected_sid = (data.get("session_id") or "").strip()
    if not expected_sid:
        raise RuntimeError("collection.json.session_id 为空，拒绝 finalize")
    if not (proj / f"{expected_sid}.jsonl").is_file():
        raise RuntimeError(f"缺少正式轨迹文件 {expected_sid}.jsonl，拒绝 finalize")

    guard = json.loads(guard_path.read_text(encoding="utf-8"))
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    guard_ok = guard.get("result") == "passed" and guard.get("classification", "clean") == "clean"
    if guard.get("classification") == "suspect":
        review_path = ev / "trajectory_review.json"
        if review_path.exists():
            review = json.loads(review_path.read_text(encoding="utf-8"))
            guard_ok = (
                review.get("decision") == "approved"
                and review.get("session_id") == guard.get("session_id")
                and len((review.get("reason") or "").strip()) >= 20
            )
    if not guard_ok or guard.get("tests_visible") is not False:
        raise RuntimeError("正式轨迹未获 clean 结论，或 suspect 未有效人工复核，拒绝 finalize")
    if guard.get("session_id") != expected_sid:
        raise RuntimeError("正式轨迹守卫 session_id 与 collection.json 不一致")

    checks = acceptance.get("checks") if isinstance(acceptance.get("checks"), dict) else {}
    required = {"trajectory_analysis", "regression", "task_semantics"}
    required.add("private_verify" if task_type == "bugfix" else "diagnosis_root_cause")
    if (
        acceptance.get("result") != "passed"
        or acceptance.get("session_id") != expected_sid
        or not required.issubset(checks)
        or any(checks[name].get("passed") is not True for name in required)
    ):
        raise RuntimeError("自动轨迹验收未通过、session 不一致，或缺少 task_type 对应验收证据，拒绝 finalize")
    return expected_sid


def _cmd_finalize_unlocked(args):
    """After acceptance, create G2 and an independent orphan R1."""
    root = Path(args.root)
    base = slugify(args.repo_name)
    repo_name = resolve_repo_name(root, base) or base
    proj_name = args.project.replace("/", "__").lower()
    proj = root / (args.date or __import__("datetime").date.today().isoformat()) / proj_name

    env_dir = proj / "env"
    if not env_dir.exists():
        raise RuntimeError(f"bug env not found: {env_dir}")

    coll = proj / "collection.json"
    bug_id = args.bug_id
    task_type = ""
    data = {}
    if coll.exists():
        data = json.loads(coll.read_text(encoding="utf-8"))
        bug_id = bug_id or data.get("bug_id") or ""
        task_type = (data.get("task_type") or "").strip().lower()
    if not bug_id:
        raise RuntimeError("--bug-id 缺失，且 collection.json 中没有 bug_id")
    if task_type not in {"bugfix", "diagnosis"}:
        raise RuntimeError("collection.json.task_type 必须是 bugfix 或 diagnosis")
    evaluator = proj / "evaluator"
    private_issues = private_test_issues(env_dir, evaluator, data.get("verify_cmds") or "")
    if private_issues:
        raise RuntimeError("私有测试门禁失败：" + "；".join(private_issues))

    if task_type == "diagnosis":
        expected_sid = _assert_formal_acceptance(proj, data, task_type)
        repo = central_repo_dir(root, repo_name)
        if not (repo / ".git").exists():
            raise RuntimeError(f"central repo not found: {repo}（请先运行 github_project.py ensure/publish）")

        ctx = load_context()
        github = ctx["github"]
        author = ctx["gitAuthor"]
        remote_url = run_git(repo, "remote", "get-url", DEFAULT_REMOTE).stdout.strip()
        html = remote_url[:-4] if remote_url.endswith(".git") else remote_url
        auth_env = git_auth_env(remote_url, github["username"], github["token"])
        identity_env = git_identity_env(auth_env, author["name"], author["email"])

        record = proj_name.rsplit("__", 1)[-1] if "__" in proj_name else "001"
        green_branch, red_branch = delivery_branches(record)
        if _branch_exists(repo, green_branch):
            raise RuntimeError(f"diagnosis 不应存在 green 分支 {green_branch}")
        if run_git(repo, "rev-parse", "--verify", red_branch, check=False).returncode != 0:
            raise RuntimeError(f"本地预备分支 {red_branch} 不存在（请先运行 github_project.py publish）")
        remote_red = run_git(
            repo, "ls-remote", "--exit-code", "--heads", DEFAULT_REMOTE, red_branch, check=False
        )
        remote_red_sha = remote_red.stdout.split()[0] if remote_red.returncode == 0 and remote_red.stdout.strip() else ""

        run_git(repo, "checkout", red_branch)
        if run_git(repo, "rev-list", "--count", red_branch).stdout.strip() != "1":
            raise RuntimeError("diagnosis 预备 red 必须为 orphan 单提交")
        current_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()
        manifest_path = proj / "_delivery" / "g1_snapshot.json"
        meta_path = proj / "_evidence" / "repository_delivery.json"
        if not manifest_path.is_file() or not meta_path.is_file():
            raise RuntimeError("缺少 diagnosis 预备快照或交付元数据")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        if metadata.get("state") != "g1_prepared":
            raise RuntimeError("diagnosis 预备 red 状态与 repository_delivery.json 不一致")
        if manifest.get("files") != source_manifest(repo):
            raise RuntimeError("diagnosis 预备 red 与 g1_snapshot.json 不一致")

        actual_tests = _tree_entries(repo, current_sha, tests=True)
        expected_tests = _evaluator_entries(repo, evaluator)
        if actual_tests:
            if actual_tests != expected_tests:
                raise RuntimeError("diagnosis 本地 red 已有测试，但与 evaluator 路径或内容不一致")
            copied_tests = sorted(expected_tests)
            red_sha = current_sha
            prepared_sha = metadata.get("g1_commit") or ""
        else:
            if metadata.get("g1_commit") != current_sha or manifest.get("commit") != current_sha:
                raise RuntimeError("diagnosis 预备 red commit 与快照元数据不一致")
            prepared_sha = current_sha
            _assert_no_tests(repo, prepared_sha)
            copied_tests = copy_evaluator_to_repo(evaluator, repo)
            run_git(repo, "add", "-A", "--", ".")
            run_git(repo, "commit", "--amend", "-m", f"red-test: {bug_id}", env=identity_env)
            red_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()
        if run_git(repo, "rev-list", "--count", red_branch).stdout.strip() != "1":
            raise RuntimeError("diagnosis 最终 red 必须为 orphan 单提交")
        if not _tree_entries(repo, red_sha, tests=True):
            raise RuntimeError("diagnosis 最终 red 缺少验收测试")
        if remote_red_sha and remote_red_sha != red_sha:
            raise RuntimeError(f"远程 diagnosis red {red_branch} 与本地最终提交不一致，拒绝 force-push")

        write_source_manifest(repo, manifest_path, commit=red_sha, branch=red_branch)
        run_git(repo, "push", "-u", DEFAULT_REMOTE, red_branch, env=identity_env)
        repo_url = f"{html}/tree/{red_branch}"
        _write_delivery_metadata(proj, {
            "schema": 2,
            "state": "finalized",
            "task_type": "diagnosis",
            "repo_url": repo_url,
            "green_branch": "",
            "red_branch": red_branch,
            "prepared_commit": prepared_sha,
            "g1_commit": red_sha,
            "r1_commit": red_sha,
            "g1_manifest": str(manifest_path.relative_to(proj)),
            "session_id": expected_sid,
            "test_files": copied_tests,
            "finalized_at": datetime.now(timezone.utc).isoformat(),
        })
        print(json.dumps({
            "ok": True,
            "repoUrl": repo_url,
            "r1Commit": f"{html}/commit/{red_sha}",
            "testFiles": copied_tests,
            "greenBranch": "",
            "redBranch": red_branch,
        }, ensure_ascii=False))
        return

    # 绿灯门禁：只有绿灯确认过的修复才推上 GitHub，避免无效修复 commit 进交付分支。
    ev = proj / "_evidence"
    required = (
        ev / "trajectory_guard.json",
        ev / "trajectory_acceptance.json",
        ev / "verify_green.jsonl",
        ev / "verify_result.json",
        ev / "green_regression.json",
    )
    missing = [p.name for p in required if not p.exists()]
    if missing:
        raise RuntimeError(
            "绿灯验收未完成（缺少 _evidence/" + "、_evidence/".join(missing) + "）。"
            "请先运行 run_evidence_trajectories.py generate --phase green 通过绿灯和全量回归再推送。"
        )
    regression = json.loads((ev / "green_regression.json").read_text(encoding="utf-8"))
    expected_sid = _assert_formal_acceptance(proj, data, task_type)
    if regression.get("result") != "passed":
        raise RuntimeError("绿灯后全量回归未通过，拒绝推送")

    repo = central_repo_dir(root, repo_name)
    if not (repo / ".git").exists():
        raise RuntimeError(f"central repo not found: {repo}（请先运行 github_project.py ensure/publish）")

    ctx = load_context()
    github = ctx["github"]
    author = ctx["gitAuthor"]
    remote_url = run_git(repo, "remote", "get-url", DEFAULT_REMOTE).stdout.strip()
    html = remote_url[:-4] if remote_url.endswith(".git") else remote_url
    auth_env = git_auth_env(remote_url, github["username"], github["token"])
    identity_env = git_identity_env(auth_env, author["name"], author["email"])

    record = proj_name.rsplit("__", 1)[-1] if "__" in proj_name else "001"
    green_branch, red_branch = delivery_branches(record)
    if run_git(repo, "rev-parse", "--verify", green_branch, check=False).returncode != 0:
        raise RuntimeError(f"分支 {green_branch} 不存在（请先运行 github_project.py publish）")
    if _branch_exists(repo, red_branch):
        raise RuntimeError(f"红测分支 {red_branch} 已存在，拒绝覆盖")

    run_git(repo, "checkout", green_branch)
    if run_git(repo, "rev-list", "--count", green_branch).stdout.strip() != "1":
        raise RuntimeError(f"{green_branch} 在 finalize 前必须只有 G1 一个提交")
    g1_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    _assert_no_tests(repo, g1_sha)
    sync_bug_source(env_dir, repo)
    ensure_delivery_files(repo, proj_name, read_project_summary(proj), getattr(args, "module_path", None))
    _assert_root_delivery_files(repo)
    # Make newly created files visible to diff without staging their contents yet.
    run_git(repo, "add", "-N", "--", ".")
    model_files, model_lines = functional_go_diff_from_numstat(
        run_git(repo, "-c", "core.quotePath=false", "diff", "--numstat", g1_sha, "--", ".").stdout
    )
    if not meets_minimum_functional_change(model_files, model_lines):
        run_git(repo, "reset", "--mixed", g1_sha)
        raise RuntimeError(
            f"模型最终补丁只有 {model_files} 个功能 Go 文件、{model_lines} 行增删；"
            f"至少需要 1 个文件、{MIN_FUNCTIONAL_CHANGED_LINES} 行，拒绝 finalize"
        )
    copied_tests = copy_evaluator_to_repo(evaluator, repo)
    run_git(repo, "add", "-A", "--", ".")
    if not run_git(repo, "diff", "--cached", "--name-only").stdout.strip():
        raise RuntimeError(f"env 与 {green_branch} 无差异，bugfix 题应有模型修复和目标测试")
    run_git(repo, "commit", "-m", f"fix+test: {bug_id}", env=identity_env)
    g2_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()

    run_git(repo, "checkout", "--orphan", red_branch)
    clear_worktree(repo)
    run_git(repo, "checkout", g1_sha, "--", ".")
    copy_evaluator_to_repo(evaluator, repo)
    run_git(repo, "add", "-A", "--", ".")
    run_git(repo, "commit", "-m", f"red-test: {bug_id}", env=identity_env)
    r1_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()

    if run_git(repo, "rev-list", "--count", red_branch).stdout.strip() != "1":
        raise RuntimeError("R1 red 分支必须为 orphan 单提交")
    if run_git(repo, "merge-base", green_branch, red_branch, check=False).returncode == 0:
        raise RuntimeError("green/red 分支存在共同祖先，拒绝交付")
    g1_business = _tree_entries(repo, g1_sha, tests=False)
    r1_business = _tree_entries(repo, r1_sha, tests=False)
    if g1_business != r1_business:
        raise RuntimeError("R1 与 G1 的非测试文件不完全一致")
    if _tree_entries(repo, g2_sha, tests=True) != _tree_entries(repo, r1_sha, tests=True):
        raise RuntimeError("G2 与 R1 的验收测试不完全一致")
    for rel in copied_tests:
        g2_blob = run_git(repo, "rev-parse", f"{g2_sha}:{rel}").stdout.strip()
        r1_blob = run_git(repo, "rev-parse", f"{r1_sha}:{rel}").stdout.strip()
        if g2_blob != r1_blob:
            raise RuntimeError(f"G2/R1 验收文件不一致: {rel}")

    run_git(repo, "push", DEFAULT_REMOTE, green_branch, red_branch, env=identity_env)
    run_git(repo, "checkout", green_branch)
    repo_url = f"{html}/tree/{green_branch}"
    _write_delivery_metadata(proj, {
        "schema": 1,
        "state": "finalized",
        "repo_url": repo_url,
        "green_branch": green_branch,
        "red_branch": red_branch,
        "g1_commit": g1_sha,
        "g2_commit": g2_sha,
        "r1_commit": r1_sha,
        "session_id": data.get("session_id") or "",
        "test_files": copied_tests,
        "model_functional_files": model_files,
        "model_functional_lines": model_lines,
        "finalized_at": datetime.now(timezone.utc).isoformat(),
    })
    print(json.dumps({
        "ok": True,
        "repoUrl": repo_url,
        "g1Commit": f"{html}/commit/{g1_sha}",
        "g2Commit": f"{html}/commit/{g2_sha}",
        "r1Commit": f"{html}/commit/{r1_sha}",
        "testFiles": copied_tests,
        "greenBranch": green_branch,
        "redBranch": red_branch,
    }, ensure_ascii=False))


def cmd_finalize(args):
    root = Path(args.root).resolve()
    with repo_write_lock(root, args.repo_name):
        _cmd_finalize_unlocked(args)


def cmd_push_fix(args):
    print("⚠️  push-fix 已弃用，按 finalize 的 G1/G2/R1 流程执行", file=sys.stderr)
    cmd_finalize(args)


def main():
    p = argparse.ArgumentParser(description="0-1 自建项目 GitHub 仓库与分支管理")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("ensure")
    c.add_argument("--root", default=".")
    c.add_argument("--repo-name", required=True)
    c.add_argument("--local-path", required=True)
    c.add_argument("--module-path", help="Go module 相对仓库根目录的子目录（如 backend），缺省自动探测")
    c.set_defaults(func=cmd_ensure)

    c = sub.add_parser("publish")
    c.add_argument("--root", default=".")
    c.add_argument("--repo-name", required=True)
    c.add_argument("--project", required=True)
    c.add_argument("--date")
    c.add_argument("--bug-id")
    c.add_argument("--module-path", help="Go module 相对仓库根目录的子目录（如 backend），缺省自动探测")
    c.set_defaults(func=cmd_publish)

    c = sub.add_parser("finalize")
    c.add_argument("--root", default=".")
    c.add_argument("--repo-name", required=True)
    c.add_argument("--project", required=True)
    c.add_argument("--date")
    c.add_argument("--bug-id")
    c.add_argument("--module-path", help="Go module 相对仓库根目录的子目录（如 backend），缺省自动探测")
    c.set_defaults(func=cmd_finalize)

    c = sub.add_parser("push-fix", help="兼容旧命令；实际执行 finalize")
    c.add_argument("--root", default=".")
    c.add_argument("--repo-name", required=True)
    c.add_argument("--project", required=True)
    c.add_argument("--date")
    c.add_argument("--bug-id")
    c.add_argument("--module-path", help="Go module 相对仓库根目录的子目录（如 backend），缺省自动探测")
    c.set_defaults(func=cmd_push_fix)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
