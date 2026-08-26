#!/usr/bin/env python3
"""Go 标注数据生产流水线 —— 首次配置向导与依赖自检。

解决“技能分享给别人后第一次跑不通”的问题：把 GitHub 凭据、git 作者、
COS 上传 cookie、Claude Code 可执行文件路径一次性收集好，落到用户家目录，
后续 SKILL 流程里的脚本会自动读取，无需每次手动 export。

触发方式（路径指向本脚本即可）：
  python3 <skill>/scripts/configure.py            # 交互式配置（有 TTY 时）
  python3 <skill>/scripts/configure.py check      # 只检查依赖与配置，不修改
  python3 <skill>/scripts/configure.py show       # 查看当前配置（token/cookie 脱敏）
  python3 <skill>/scripts/configure.py setup ...  # 非交互配置（Codex 引导用户填写后调用）
  python3 <skill>/scripts/configure.py reset-registry  # 清空全局已用仓库清单（存于 ~/.codex/go-annotation-pipeline/，仅自己想清零时用；分享技能无需此步）

配置落盘位置：
  GitHub 凭据 / git 作者 -> ~/.codex/pg-code/github-context.json
       （与 pg-code 技能共用；github_project.py 已经从这个文件读取）
  本技能私有配置        -> ~/.codex/go-annotation-pipeline/config.json
       （cos_uploader_sid、claude_bin、cos_base_url 等）

setup 可用参数（都可省略，省略时回退到同名环境变量，再回退到现有配置）：
  --github-username  GitHub 用户名
  --github-token     GitHub Personal Access Token（repo + delete_repo 权限）
  --git-name         git 提交作者名（不得为 “PINRU Local”）
  --git-email        git 提交作者邮箱
  --cos-cookie       COS 上传 cookie（cos_uploader_sid），跳过登录
  --cos-username     COS 上传站登录账号（与 --cos-password 一起自动登录拿 cookie，并保存供 cookie 过期时刷新）
  --cos-password     COS 上传站登录密码
  --platform-username  go.jzxhnh.com 标注平台账号
  --platform-password  go.jzxhnh.com 标注平台密码
  --claude           claude 可执行文件路径（默认 claude）
  --cos-base-url     COS 上传站地址（默认 https://upload.jzxhnh.com）
  --skip-verify      跳过 GitHub token / COS cookie 的联网校验
  --force            覆盖已有配置（无此项时默认与现有配置合并）
"""
from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
GITHUB_CONTEXT = Path.home() / ".codex" / "pg-code" / "github-context.json"
LOCAL_CONFIG_DIR = Path.home() / ".codex" / "go-annotation-pipeline"
LOCAL_CONFIG = LOCAL_CONFIG_DIR / "config.json"
LEGACY_PLATFORM_CONFIG = Path.home() / ".codex" / "push_go_label" / "config.json"
REGISTRY_JSON = LOCAL_CONFIG_DIR / "used-repositories.json"
REGISTRY_MD = LOCAL_CONFIG_DIR / "used-repositories.md"
# 旧版存放位置（技能目录内），已弃用；reset 时一并备份清理，防止旧数据日后被重新迁移回来。
LEGACY_REGISTRY_JSON = SKILL_DIR / "references" / "used-repositories.json"
LEGACY_REGISTRY_MD = SKILL_DIR / "references" / "used-repositories.md"

DEFAULT_COS_BASE = "https://upload.jzxhnh.com"
DEFAULT_PLATFORM_BASE = "https://go.jzxhnh.com"
UA = "go-annotation-pipeline-configure/1.0"

REQUIRED_DEPS = {
    "git": "git（仓库/分支管理）",
    "curl": "curl（COS 上传）",
    "go": "Go 工具链（go build / go test）",
    "rsync": "rsync（轨迹失败回滚快照）",
    "claude": "Claude Code CLI（跑轨迹，可用 --claude 或 CLAUDE_BIN 覆盖）",
    "python-openpyxl": "python3 的 openpyxl 包（生成收集表 xlsx）",
    "python-requests": "python3 的 requests 包（提交标注平台）",
}
OPTIONAL_DEPS = {
    "docker": "docker（本机容器验证，可选）",
}

INSTALL_HINTS = {
    "git": "xcode-select --install  或  brew install git",
    "curl": "brew install curl（macOS 一般自带）",
    "go": "brew install go  或  https://go.dev/dl/",
    "rsync": "brew install rsync",
    "claude": "npm install -g @anthropic-ai/claude-code",
    "python-openpyxl": "python3 -m pip install openpyxl",
    "python-requests": "python3 -m pip install requests",
    "docker": "安装 Docker Desktop：https://www.docker.com/products/docker-desktop/",
}


def warn(msg: str):
    print(f"⚠️  {msg}", file=sys.stderr)


def ok(msg: str):
    print(f"✅ {msg}")


def fail(msg: str):
    print(f"❌ {msg}", file=sys.stderr)


def _input(prompt: str, default: str | None = None, secret: bool = False) -> str:
    if secret:
        return getpass.getpass(f"{prompt}: ") or (default or "")
    if default:
        value = input(f"{prompt} [{default}]: ").strip()
        return value or default
    return input(f"{prompt}: ").strip()


# --------------------------------------------------------------------------- #
# 配置读写
# --------------------------------------------------------------------------- #

def load_github_context() -> dict:
    if GITHUB_CONTEXT.exists():
        try:
            return json.loads(GITHUB_CONTEXT.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            warn(f"读取 GitHub 配置失败，将重建: {exc}")
    return {}


def save_github_context(data: dict):
    GITHUB_CONTEXT.parent.mkdir(parents=True, exist_ok=True)
    GITHUB_CONTEXT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    GITHUB_CONTEXT.chmod(0o600)
    ok(f"GitHub 配置已写入 {GITHUB_CONTEXT}")


def load_local_config() -> dict:
    if LOCAL_CONFIG.exists():
        try:
            return json.loads(LOCAL_CONFIG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            warn(f"读取本地配置失败，将重建: {exc}")
    return {}


def load_legacy_platform_config() -> dict:
    if LEGACY_PLATFORM_CONFIG.exists():
        try:
            return json.loads(LEGACY_PLATFORM_CONFIG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_local_config(data: dict):
    LOCAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    LOCAL_CONFIG.chmod(0o600)
    ok(f"本地配置已写入 {LOCAL_CONFIG}")


# --------------------------------------------------------------------------- #
# 依赖与配置检查
# --------------------------------------------------------------------------- #

def check_dependencies() -> tuple[dict[str, bool], dict[str, bool]]:
    required, optional = {}, {}
    for name in REQUIRED_DEPS:
        if name.startswith("python-"):
            required[name] = importlib.util.find_spec(name.removeprefix("python-")) is not None
        else:
            required[name] = shutil.which(name) is not None
    for name in OPTIONAL_DEPS:
        optional[name] = shutil.which(name) is not None
    return required, optional


def print_dependency_status(required: dict[str, bool], optional: dict[str, bool]):
    print("依赖自检：")
    for name, found in required.items():
        label = REQUIRED_DEPS[name]
        print(f"  {'✅' if found else '❌'} {label}")
    for name, found in optional.items():
        label = OPTIONAL_DEPS[name]
        print(f"  {'✅' if found else '🔸'} {label}")
    missing = [k for k, v in required.items() if not v]
    if missing:
        print("\n缺失依赖，请先安装：")
        for name in missing:
            print(f"  - {REQUIRED_DEPS[name]}：{INSTALL_HINTS.get(name, '')}")


def github_context_status() -> tuple[bool, str]:
    data = load_github_context()
    github = data.get("github") or {}
    author = data.get("gitAuthor") or {}
    if not github.get("username") or not github.get("token"):
        return False, "缺少 github.username 或 github.token"
    if not author.get("name") or not author.get("email"):
        return False, "缺少 gitAuthor.name 或 gitAuthor.email"
    if author.get("name") == "PINRU Local":
        return False, "gitAuthor.name 不能为 PINRU Local"
    return True, f"username={github.get('username')}, author={author.get('name')} <{author.get('email')}>"


def cos_cookie_status() -> tuple[bool, str]:
    cfg = load_local_config()
    sid = (cfg.get("cos_uploader_sid") or "").strip()
    user = (cfg.get("cos_username") or "").strip()
    if sid and user:
        return True, f"已配置，过期后可用账号 {user} 自动刷新"
    return bool(sid), ("已配置" if sid else "未配置（上传轨迹前需配置，或手动 export COS_UPLOADER_SID）")


def platform_credentials_status() -> tuple[bool, str]:
    local = load_local_config()
    legacy = load_legacy_platform_config()
    username = (
        os.environ.get("GOQA_USERNAME") or local.get("platform_username")
        or legacy.get("username") or ""
    )
    password = (
        os.environ.get("GOQA_PASSWORD") or local.get("platform_password")
        or legacy.get("password") or ""
    )
    source = "主流水线配置" if local.get("platform_username") else (
        "push_go_label 兼容配置" if legacy.get("username") else "环境变量"
    )
    if username and password:
        return True, f"username={username}（{source}）"
    return False, "未配置（默认批次收尾会提交平台）"


def print_config_status():
    gh_ok, gh_msg = github_context_status()
    cos_ok, cos_msg = cos_cookie_status()
    platform_ok, platform_msg = platform_credentials_status()
    print("\n配置自检：")
    print(f"  {'✅' if gh_ok else '❌'} GitHub 凭据：{gh_msg}")
    print(f"  {'✅' if cos_ok else '⚠️ '} COS 上传 cookie：{cos_msg}")
    print(f"  {'✅' if platform_ok else '❌'} 标注平台凭据：{platform_msg}")
    return gh_ok and platform_ok


# --------------------------------------------------------------------------- #
# GitHub token 校验
# --------------------------------------------------------------------------- #

def verify_github_token(username: str, token: str) -> bool:
    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "User-Agent": UA},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        return (data.get("login") or "").lower() == username.lower()
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# COS 登录拿 cookie
# --------------------------------------------------------------------------- #

def cos_login(base_url: str, username: str, password: str) -> str | None:
    login_url = f"{base_url.rstrip('/')}/api/login"
    origin = base_url.rstrip("/")
    data = f"username={urllib.parse.quote(username)}&password={urllib.parse.quote(password)}"
    req = urllib.request.Request(
        login_url,
        data=data.encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
            "Referer": f"{origin}/login",
            "User-Agent": UA,
        },
        method="POST",
    )
    cookies, body = [], ""
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            cookies = resp.headers.get_all("Set-Cookie") or []
            body = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        cookies = exc.headers.get_all("Set-Cookie") or []
        body = exc.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        fail(f"COS 登录请求失败: {exc}")
        return None

    sid = None
    for c in cookies:
        for part in c.split(";"):
            if part.strip().startswith("cos_uploader_sid="):
                sid = part.strip().split("=", 1)[1]
                break
        if sid:
            break
    if not sid:
        fail(f"COS 登录失败，账号密码可能不正确：{body[:200]}")
        return None
    return sid


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #

def cmd_check(_args):
    required, optional = check_dependencies()
    print_dependency_status(required, optional)
    config_ok = print_config_status()

    missing_required = any(not v for v in required.values())
    print()
    if missing_required or not config_ok:
        print("运行 `python3 <skill>/scripts/configure.py setup ...` 补齐缺失项。")
        sys.exit(1)
    ok("依赖与必备配置齐全，可以开始生产数据。")


def cmd_show(_args):
    print(f"GitHub 配置: {GITHUB_CONTEXT}")
    data = load_github_context()
    github = data.get("github") or {}
    author = data.get("gitAuthor") or {}
    token = github.get("token") or ""
    masked = token[:4] + "***" + token[-4:] if len(token) > 8 else "***"
    print(f"  username : {github.get('username') or '(未配置)'}")
    print(f"  token    : {masked if token else '(未配置)'}")
    print(f"  author   : {author.get('name') or '(未配置)'} <{author.get('email') or '(未配置)'}>")

    print(f"\n本地配置: {LOCAL_CONFIG}")
    cfg = load_local_config()
    sid = cfg.get("cos_uploader_sid") or ""
    masked_sid = sid[:4] + "***" + sid[-4:] if len(sid) > 8 else ("***" if sid else "(未配置)")
    cos_username = cfg.get("cos_username") or ""
    cos_password = cfg.get("cos_password") or ""
    masked_cos_password = "***" if cos_password else "(未配置)"
    print(f"  cos_uploader_sid : {masked_sid}")
    print(f"  cos_username     : {cos_username or '(未配置)'}")
    print(f"  cos_password     : {masked_cos_password}")
    print(f"  cos_base_url     : {cfg.get('cos_base_url') or DEFAULT_COS_BASE}")
    print(f"  claude_bin       : {cfg.get('claude_bin') or 'claude'}")
    legacy = load_legacy_platform_config()
    platform_username = cfg.get("platform_username") or legacy.get("username") or ""
    platform_password = cfg.get("platform_password") or legacy.get("password") or ""
    platform_source = "主流水线配置" if cfg.get("platform_username") else "push_go_label 兼容配置"
    print(f"  platform_username : {platform_username or '(未配置)'}")
    print(f"  platform_password : {'***' if platform_password else '(未配置)'}")
    print(f"  platform_base_url : {cfg.get('platform_base_url') or legacy.get('base_url') or DEFAULT_PLATFORM_BASE}")
    if platform_username:
        print(f"  platform_source   : {platform_source}")


def cmd_setup(args):
    existing_gh = load_github_context()
    existing_local = load_local_config()
    legacy_platform = load_legacy_platform_config()

    gh_username = args.github_username or os.environ.get("GITHUB_USERNAME") or (existing_gh.get("github") or {}).get("username") or ""
    gh_token = args.github_token or os.environ.get("GITHUB_TOKEN") or (existing_gh.get("github") or {}).get("token") or ""
    git_name = args.git_name or os.environ.get("GIT_AUTHOR_NAME") or (existing_gh.get("gitAuthor") or {}).get("name") or ""
    git_email = args.git_email or os.environ.get("GIT_AUTHOR_EMAIL") or (existing_gh.get("gitAuthor") or {}).get("email") or ""
    claude_bin = args.claude or os.environ.get("CLAUDE_BIN") or existing_local.get("claude_bin") or "claude"
    cos_base_url = args.cos_base_url or existing_local.get("cos_base_url") or DEFAULT_COS_BASE
    cos_cookie = args.cos_cookie or os.environ.get("COS_UPLOADER_SID") or existing_local.get("cos_uploader_sid") or ""
    cos_username = args.cos_username or os.environ.get("COS_USERNAME") or existing_local.get("cos_username") or ""
    cos_password = args.cos_password or os.environ.get("COS_PASSWORD") or existing_local.get("cos_password") or ""
    platform_username = (
        args.platform_username or os.environ.get("GOQA_USERNAME")
        or existing_local.get("platform_username") or legacy_platform.get("username") or ""
    )
    platform_password = (
        args.platform_password or os.environ.get("GOQA_PASSWORD")
        or existing_local.get("platform_password") or legacy_platform.get("password") or ""
    )
    platform_base_url = (
        args.platform_base_url or os.environ.get("GOQA_BASE_URL")
        or existing_local.get("platform_base_url") or legacy_platform.get("base_url") or DEFAULT_PLATFORM_BASE
    )

    interactive = sys.stdin.isatty() and not (
        args.github_username or args.github_token or args.git_name or args.cos_cookie
        or args.platform_username or args.platform_password
    )

    if interactive:
        print("首次配置向导（直接回车可保留已有值）\n")
        gh_username = _input("GitHub 用户名", gh_username or None)
        gh_token = _input("GitHub Token", gh_token or None, secret=True)
        git_name = _input("git 提交作者名", git_name or None)
        git_email = _input("git 提交作者邮箱", git_email or None)
        cos_cookie = _input("COS 上传 cookie（cos_uploader_sid，可稍后配置）", cos_cookie or None)
        if not cos_cookie:
            use_login = _input("是否用账号密码自动登录 COS 获取 cookie？(y/N)", "N")
            if use_login.lower().startswith("y"):
                cos_username = _input("COS 登录账号", cos_username or None)
                cos_password = _input("COS 登录密码", cos_password or None, secret=True)
                cos_cookie = cos_login(cos_base_url, cos_username, cos_password) or ""
        platform_username = _input("标注平台账号", platform_username or None)
        platform_password = _input("标注平台密码", platform_password or None, secret=True)

    # 显式提供账号密码时立即登录刷新 cookie；没有现成 cookie 时也自动登录补齐。
    cos_login_requested = bool(args.cos_username or args.cos_password or (not cos_cookie and cos_username and cos_password))
    if cos_login_requested and cos_username and cos_password:
        cos_cookie = cos_login(cos_base_url, cos_username, cos_password) or ""

    missing = []
    if not gh_username:
        missing.append("--github-username")
    if not gh_token:
        missing.append("--github-token")
    if not git_name:
        missing.append("--git-name")
    if not git_email:
        missing.append("--git-email")
    if not platform_username:
        missing.append("--platform-username")
    if not platform_password:
        missing.append("--platform-password")
    if missing:
        fail("缺少必填项：" + "、".join(missing))
        print("\n非交互配置示例：")
        print("  python3 <skill>/scripts/configure.py setup \\")
        print("    --github-username <你的GitHub用户名> --github-token <ghp_xxx> \\")
        print("    --git-name <作者名> --git-email <作者邮箱> \\")
        print("    [--cos-cookie <cos_uploader_sid> | --cos-username <u> --cos-password <p>] \\")
        print("    --platform-username <u> --platform-password <p>")
        print("\n也支持环境变量：GITHUB_USERNAME / GITHUB_TOKEN / GIT_AUTHOR_NAME / GIT_AUTHOR_EMAIL / COS_UPLOADER_SID / GOQA_USERNAME / GOQA_PASSWORD / CLAUDE_BIN")
        sys.exit(1)

    if git_name == "PINRU Local":
        fail("git 作者名不能为 PINRU Local，请填写真实作者名。")
        sys.exit(1)

    if gh_token and not args.skip_verify:
        print("校验 GitHub token …")
        if verify_github_token(gh_username, gh_token):
            ok(f"GitHub token 校验通过（登录名 {gh_username}）。")
        else:
            warn("GitHub token 校验未通过（网络受限或 token 无效）。仍将写入配置；若后续 ensure 报 401 请重新配置。")

    # 写 GitHub 配置
    gh_data = dict(existing_gh)
    gh_data.setdefault("defaultBranch", "main")
    gh_data["exportedAt"] = datetime.now(timezone.utc).isoformat()
    gh_data["gitAuthor"] = {"name": git_name, "email": git_email}
    gh_data["github"] = {
        "username": gh_username,
        "token": gh_token,
        "accountId": (existing_gh.get("github") or {}).get("accountId") or "",
        "accountName": gh_username,
    }
    gh_data["source"] = "go-annotation-pipeline-configure"
    gh_data["version"] = 1
    save_github_context(gh_data)

    # 写本地配置
    local = dict(existing_local)
    local["cos_base_url"] = cos_base_url
    if cos_cookie:
        local["cos_uploader_sid"] = cos_cookie
    if cos_username:
        local["cos_username"] = cos_username
    if cos_password:
        local["cos_password"] = cos_password
    local["platform_username"] = platform_username
    local["platform_password"] = platform_password
    local["platform_base_url"] = platform_base_url
    local["claude_bin"] = claude_bin
    save_local_config(local)

    print()
    if not cos_cookie:
        warn("未配置 COS cookie。跑完轨迹上传前，重新运行 setup 提供 --cos-cookie，或手动 export COS_UPLOADER_SID。")
    ok("配置完成。运行 `python3 <skill>/scripts/configure.py check` 可再次自检。")


def cmd_reset_registry(_args):
    if not _args.yes and sys.stdin.isatty():
        answer = input(f"确认清空全局已用仓库清单（{REGISTRY_JSON}）？(y/N): ").strip().lower()
        if not answer.startswith("y"):
            fail("未确认，已取消。需要清零时请用 --yes 强制执行。")
            sys.exit(1)
    data = {"version": 2, "repositories": []}
    REGISTRY_JSON.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REGISTRY_MD.write_text(render_empty_md(), encoding="utf-8")
    # 旧版技能目录内的清单一并备份重命名，避免其中的旧数据日后被自动迁移回来。
    for old in (LEGACY_REGISTRY_JSON, LEGACY_REGISTRY_MD):
        if old.exists():
            backup = old.with_name(old.name + ".migrated")
            if backup.exists():
                backup.unlink()
            old.rename(backup)
    ok(f"已清空：{REGISTRY_JSON}（注册表在个人目录，不在技能包内，分享技能无需此步）")


def render_empty_md() -> str:
    return "\n".join([
        "# 已用仓库/项目（全局去重注册表）",
        "",
        "本文件由 `scripts/repo_registry.py sync` 自动生成，请勿手改；改 `used-repositories.json`。",
        "",
        "| 来源 | owner/repo | 地址 | commit | 使用日期 | 项目 | 使用次数 | 备注 |",
        "|---|---|---|---|---|---|---|---|",
        "| （空） | （暂无记录） |  |  |  |  | 0/5 |  |",
        "",
    ])


def main():
    p = argparse.ArgumentParser(description="Go 标注数据生产流水线 —— 配置向导")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="检查依赖与配置")
    c.set_defaults(func=cmd_check)

    c = sub.add_parser("show", help="查看当前配置（脱敏）")
    c.set_defaults(func=cmd_show)

    c = sub.add_parser("setup", help="写入配置（非交互参数 / 交互式）")
    c.add_argument("--github-username")
    c.add_argument("--github-token")
    c.add_argument("--git-name")
    c.add_argument("--git-email")
    c.add_argument("--cos-cookie")
    c.add_argument("--cos-username")
    c.add_argument("--cos-password")
    c.add_argument("--cos-base-url")
    c.add_argument("--platform-username")
    c.add_argument("--platform-password")
    c.add_argument("--platform-base-url")
    c.add_argument("--claude")
    c.add_argument("--skip-verify", action="store_true")
    c.add_argument("--force", action="store_true")
    c.set_defaults(func=cmd_setup)

    c = sub.add_parser("reset-registry", help="清空全局已用仓库清单")
    c.add_argument("--yes", action="store_true")
    c.set_defaults(func=cmd_reset_registry)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        fail(f"执行失败: {exc}")
        sys.exit(1)
