#!/usr/bin/env python3
"""上传轨迹 jsonl 到 COS（upload.jzxhnh.com）并回填 collection.json 的 trajectory 字段。

依赖：系统已安装 curl；需提供 upload.jzxhnh.com 的登录 cookie
（响应里的 cos_uploader_sid）。cookie 会过期；若本地配置或环境变量里有
COS_USERNAME/COS_PASSWORD，上传失败后会自动登录刷新一次。

用法:
  # 上传某个项目轨迹并回填
  upload_trajectory.py upload --root . --project <owner>__<repo> [--cookie <sid>] [--date YYYY-MM-DD]

  # 上传本期全部已填 collection.json 的项目轨迹并回填，再重建 xlsx
  upload_trajectory.py upload-all --root . [--cookie <sid>] [--date YYYY-MM-DD] [--sync]

  # 只看会做什么（dry-run）
  upload_trajectory.py upload-all --root . --dry-run

  # 只把已回填的 trajectory 链接重新生成 xlsx（不上传）
  upload_trajectory.py sync --root .

cookie 也可用环境变量 COS_UPLOADER_SID 提供。
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date as _date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from workspace import date_dir  # noqa: E402
from collection_table import collect_rows  # noqa: E402

LOCAL_CONFIG = Path.home() / ".codex" / "go-annotation-pipeline" / "config.json"
DEFAULT_COS_BASE = "https://upload.jzxhnh.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"


def load_local_config() -> dict:
    if LOCAL_CONFIG.exists():
        try:
            import json as _json
            return _json.loads(LOCAL_CONFIG.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_local_config(data: dict):
    LOCAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    LOCAL_CONFIG.chmod(0o600)


def cos_base_url() -> str:
    return (load_local_config().get("cos_base_url") or DEFAULT_COS_BASE).rstrip("/")


def upload_url() -> str:
    return cos_base_url() + "/api/upload"

class CookieExpiredError(RuntimeError):
    pass


GUIDANCE = """cookie 已过期，且无法自动刷新。请重新配置：
1. 推荐：python3 <skill>/scripts/configure.py setup --cos-username <账号> --cos-password <密码>
2. 或手动指定：export COS_UPLOADER_SID='<新cos_uploader_sid>'
3. 重新执行：python3 <skill>/scripts/upload_trajectory.py upload-all --root . --sync
（也可用 --cookie '<新cos_uploader_sid>' 临时传入）"""


def get_cookie(args):
    cookie = args.cookie or os.environ.get("COS_UPLOADER_SID") or ""
    if not cookie:
        cookie = (load_local_config().get("cos_uploader_sid") or "").strip()
    return cookie


def cos_credentials() -> tuple[str, str]:
    cfg = load_local_config()
    username = os.environ.get("COS_USERNAME") or (cfg.get("cos_username") or "")
    password = os.environ.get("COS_PASSWORD") or (cfg.get("cos_password") or "")
    return username.strip(), password


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
        print(f"❌ COS 登录请求失败: {exc}", file=sys.stderr)
        return None

    for c in cookies:
        for part in c.split(";"):
            if part.strip().startswith("cos_uploader_sid="):
                return part.strip().split("=", 1)[1]
    print(f"❌ COS 登录失败，账号密码可能不正确：{body[:200]}", file=sys.stderr)
    return None


def refresh_cookie() -> str | None:
    username, password = cos_credentials()
    if not username or not password:
        return None
    print("COS cookie 可能已过期，正在自动登录刷新 …")
    sid = cos_login(cos_base_url(), username, password)
    if not sid:
        return None
    cfg = load_local_config()
    cfg["cos_base_url"] = cos_base_url()
    cfg["cos_uploader_sid"] = sid
    cfg["cos_username"] = username
    cfg["cos_password"] = password
    save_local_config(cfg)
    print("✅ COS cookie 已刷新并写回本地配置。")
    return sid


def _upload_file_once(path: Path, filename: str, cookie: str) -> dict:
    if not cookie:
        raise RuntimeError("缺少 cookie：传 --cookie <sid> 或设置 COS_UPLOADER_SID")
    base = cos_base_url()
    r = subprocess.run(
        [
            "curl", "-sS", "--url", upload_url(),
            "-H", "accept: */*",
            "-H", f"origin: {base}",
            "-H", f"referer: {base}/",
            "-H", f"user-agent: {UA}",
            "-b", f"cos_uploader_sid={cookie}",
            "-F", f"files=@{path};filename={filename}",
            "-F", "expire=forever",
        ],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(f"curl 失败: {r.stderr[:300]}")
    body = r.stdout.strip()
    low = body.lower()
    if any(k in low for k in ("login", "unauthorized", "expired", "session", "sid")) and "files" not in low:
        raise CookieExpiredError(GUIDANCE)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise CookieExpiredError(GUIDANCE) if "login" in low or "expired" in low else RuntimeError(f"响应不是 JSON: {body[:300]}")
    files = data.get("files") or []
    if not data.get("ok") or not files:
        if "login" in low or "expired" in low or "unauthorized" in low:
            raise CookieExpiredError(GUIDANCE)
        raise RuntimeError(f"上传返回异常: {body[:300]}")
    return files[0]


def upload_file(path: Path, filename: str, cookie: str, auto_refresh: bool = True) -> dict:
    try:
        return _upload_file_once(path, filename, cookie)
    except CookieExpiredError:
        if not auto_refresh:
            raise
        new_cookie = refresh_cookie()
        if not new_cookie:
            raise CookieExpiredError(GUIDANCE)
        return _upload_file_once(path, filename, new_cookie)


def project_dirs(root: Path, date: str | None, project: str | None):
    if project:
        name = project.replace("/", "__").lower()
        d = date_dir(root, date)
        return [d / name]
    out = []
    for d in sorted(root.glob("*/")):
        if d.name.startswith("_"):
            continue
        for child in sorted(d.iterdir()):
            if child.is_dir() and (child / "collection.json").exists():
                out.append(child)
    return out


def upload_project(p: Path, cookie: str, dry_run: bool) -> str | None:
    coll = p / "collection.json"
    if not coll.exists():
        print(f"⏭️  跳过（无 collection.json）: {p.name}")
        return None
    data = json.loads(coll.read_text(encoding="utf-8"))
    sid = (data.get("session_id") or "").strip()
    # 优先按 session_id 命名，兜底 trajectory.jsonl
    traj = p / f"{sid}.jsonl" if sid else None
    if not traj or not traj.exists():
        traj = p / "trajectory.jsonl"
    if not traj.exists():
        print(f"⏭️  跳过（无轨迹 jsonl）: {p.name}")
        return None
    filename = f"{sid}.jsonl" if sid else traj.name
    if dry_run:
        print(f"[dry-run] {p.name}: 上传 {traj.name} 为 {filename}")
        return None
    res = upload_file(traj, filename, cookie)
    url = res.get("url") or res.get("directUrl")
    data["trajectory"] = url
    coll.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✅ {p.name}: {url}")
    return url


def cmd_upload(args):
    root = Path(args.root)
    cookie = get_cookie(args)
    p = project_dirs(root, args.date, args.project)
    if not p:
        print("（无项目）")
        return
    for proj in p:
        try:
            upload_project(proj, cookie, args.dry_run)
        except Exception as e:
            print(f"❌ {proj.name}: {e}")


def cmd_upload_all(args):
    root = Path(args.root)
    cookie = get_cookie(args)
    for proj in project_dirs(root, args.date, None):
        try:
            upload_project(proj, cookie, args.dry_run)
        except CookieExpiredError as e:
            print(f"❌ {proj.name}: {e}\n")
            print(GUIDANCE)
            sys.exit(2)
        except Exception as e:
            print(f"❌ {proj.name}: {e}")
    if args.sync and not args.dry_run:
        subprocess.run([sys.executable, str(Path(__file__).with_name("collection_table.py")), "sync", "--root", str(root)])


def cmd_sync(args):
    subprocess.run([sys.executable, str(Path(__file__).with_name("collection_table.py")), "sync", "--root", str(args.root)])


def main():
    p = argparse.ArgumentParser(description="上传轨迹 jsonl 到 COS 并回填 trajectory 字段")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("upload")
    c.add_argument("--root", default=".")
    c.add_argument("--project", required=True)
    c.add_argument("--cookie")
    c.add_argument("--date")
    c.add_argument("--dry-run", action="store_true")
    c.set_defaults(func=cmd_upload)

    c = sub.add_parser("upload-all")
    c.add_argument("--root", default=".")
    c.add_argument("--cookie")
    c.add_argument("--date")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--sync", action="store_true")
    c.set_defaults(func=cmd_upload_all)

    c = sub.add_parser("sync")
    c.add_argument("--root", default=".")
    c.set_defaults(func=cmd_sync)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
