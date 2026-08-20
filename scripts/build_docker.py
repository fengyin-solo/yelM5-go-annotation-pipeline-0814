#!/usr/bin/env python3
"""Docker 验证脚本（不再打 zip、不再截图）。

口径：
- 交付物是 GitHub `repo_url`，本地 `_gold` 只用于校准，不创建远程分支。
- Dockerfile 使用官方 golang 多架构基础镜像，默认具备 arm64/amd64 支持能力；
  如需交叉构建，可用：
      docker buildx build --platform linux/arm64,linux/amd64 -f <Dockerfile> -t <image> .
- 本脚本实际只验证当前机器平台：
  * bug 环境：必须能 go build ./...；go test ./... 仅记录结果，不强判全绿。
  * gold 环境：go build ./... 与 go test ./... 必须全绿。

用法:
  build_docker.py verify --root <本期根目录> --project <name>__<record> \
      [--date YYYY-MM-DD] [--go-version 1.22] [--image-prefix <prefix>]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from workspace import date_dir  # noqa: E402


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def detect_go_version(mod_path: Path) -> str:
    txt = mod_path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"^go\s+([0-9]+\.[0-9]+)", txt, re.M)
    return m.group(1) if m else "1.22"


def find_go_mod(d: Path, module_path: str | None) -> Path | None:
    """定位 go.mod：优先 --module-path，其次根目录，再次一层子目录（如 backend/）。"""
    if module_path:
        cand = d / module_path / "go.mod"
        return cand if cand.exists() else None
    root = d / "go.mod"
    if root.exists():
        return root
    cands = sorted(d.glob("*/go.mod"))
    return cands[0] if cands else None


def _module_layout(module_path: str | None) -> tuple[str, str]:
    """返回 Docker 构建上下文内的模块路径和容器工作目录。"""
    raw = (module_path or ".").strip()
    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"module_path 必须是仓库内的相对路径: {module_path}")
    normalized = rel.as_posix()
    if normalized in ("", "."):
        return "", "/app"
    return normalized.rstrip("/") + "/", f"/app/{normalized.rstrip('/')}"


def make_dockerfile(go_version: str, go_sum_exists: bool, module_path: str | None = None) -> str:
    """生成根目录交付 Dockerfile；嵌套 Go 模块也使用仓库根目录作为构建上下文。"""
    module_prefix, workdir = _module_layout(module_path)
    module_dest = module_prefix or "./"
    lines = [
        "# 评测用镜像：交付 Dockerfile 固定在仓库根目录，保留完整 Go 工具链。",
        f"FROM golang:{go_version}",
        "WORKDIR /app",
    ]
    if go_sum_exists:
        lines += [f"COPY {module_prefix}go.mod {module_prefix}go.sum {module_dest}"]
    else:
        lines += [f"COPY {module_prefix}go.mod {module_dest}"]
    lines += [
        f"WORKDIR {workdir}",
        "RUN go mod download",
        "WORKDIR /app",
        "COPY . .",
        f"WORKDIR {workdir}",
        "RUN go build ./...",
        'CMD ["bash"]',
        "",
        "# 多架构交叉构建示例（请在仓库根目录执行）：",
        "# docker buildx build --platform linux/arm64,linux/amd64 -f benzhi.Dockerfile -t <image> .",
    ]
    return "\n".join(lines) + "\n"


def make_context(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    r = run([
        "rsync", "-a", "--delete",
        "--exclude=.git", "--exclude=*.log", "--exclude=*.jsonl",
        "--exclude=node_modules", "--exclude=dist", "--exclude=build",
        str(src).rstrip("/") + "/", str(dst).rstrip("/") + "/",
    ])
    if r.returncode != 0:
        raise RuntimeError(f"导出目录失败: {r.stderr[:300]}")


def build_and_run(ctx: Path, image: str, checks: list[str], go_version: str, fail_on_test: bool) -> dict:
    go_sum_exists = (ctx / "go.sum").exists()
    dockerfile = ctx / "benzhi.Dockerfile"
    dockerfile.write_text(make_dockerfile(go_version, go_sum_exists), encoding="utf-8")
    (ctx / ".dockerignore").write_text(".git\n*.log\n*.jsonl\nnode_modules\ndist\nbuild\n", encoding="utf-8")

    r = run(["docker", "build", "--progress=plain", "-f", str(dockerfile), "-t", image, str(ctx)])
    build_ok = r.returncode == 0
    results = {"build": {"ok": build_ok, "output_tail": (r.stdout + r.stderr)[-1200:]}}
    if not build_ok:
        return results

    for cmd in checks:
        cr = run(["docker", "run", "--rm", image, "sh", "-c", cmd])
        results[cmd] = {
            "ok": cr.returncode == 0,
            "exit": cr.returncode,
            "output_tail": (cr.stdout + cr.stderr)[-1200:],
        }
        if fail_on_test and cr.returncode != 0:
            results["passed"] = False
            return results
    results["passed"] = True
    return results


def cmd_verify(args):
    root = Path(args.root)
    proj_name = args.project.replace("/", "__").lower()
    proj = date_dir(root, args.date) / proj_name
    env = proj / "env"
    gold = root / "_gold" / proj_name

    env_mod = find_go_mod(env, args.module_path)
    gold_mod = find_go_mod(gold, args.module_path)
    if not env_mod:
        print(f"❌ 找不到 bug 环境的 go.mod（env 根目录或一层子目录）: {env}")
        sys.exit(1)
    if not gold_mod:
        print(f"❌ 找不到 gold 环境的 go.mod（_gold 根目录或一层子目录）: {gold}")
        sys.exit(1)

    go_version = args.go_version or detect_go_version(env_mod)
    image_prefix = args.image_prefix or f"go-{proj_name}"
    day = (args.date or date.today().strftime("%Y-%m-%d")).replace("-", "")

    bug_ctx = proj / ".docker-bug"
    gold_ctx = proj / ".docker-gold"
    # 以 go.mod 所在目录作为 Docker 构建上下文，保证 Dockerfile 里 COPY go.mod 能命中。
    make_context(env_mod.parent, bug_ctx)
    make_context(gold_mod.parent, gold_ctx)

    print(f"=== 验证 bug 环境（当前平台，应能 build；test 结果仅记录） ===")
    bug_res = build_and_run(
        bug_ctx,
        f"{image_prefix}-bug:{day}",
        ["go version", "go build ./...", "go test ./..."],
        go_version,
        fail_on_test=False,
    )
    print(f"=== 验证 gold 环境（当前平台，build/test 必须全绿） ===")
    gold_res = build_and_run(
        gold_ctx,
        f"{image_prefix}-gold:{day}",
        ["go version", "go build ./...", "go test ./..."],
        go_version,
        fail_on_test=True,
    )

    gold_passed = gold_res.get("passed") is True
    bug_build_ok = bug_res.get("build", {}).get("ok") is True
    print(json.dumps({"project": proj_name, "goVersion": go_version, "bug": bug_res, "gold": gold_res, "passed": bug_build_ok and gold_passed}, ensure_ascii=False, indent=2))
    if not (bug_build_ok and gold_passed):
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(description="Docker 验证（不打包、不截图）")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("verify")
    c.add_argument("--root", default=".")
    c.add_argument("--project", required=True)
    c.add_argument("--date")
    c.add_argument("--go-version")
    c.add_argument("--image-prefix")
    c.add_argument("--module-path", help="Go module 相对 env/_gold 的子目录（如 backend），缺省自动探测")
    c.set_defaults(func=cmd_verify)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
