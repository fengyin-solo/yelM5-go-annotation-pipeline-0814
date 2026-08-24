#!/usr/bin/env python3
"""题面/收集表文案雷同自检。

扫描 <root> 下任意层级的 YYYY-MM-DD/<record>/prompt.txt 与 collection.json：
1. 检查 user_query / success_criteria / verify_cmds 三条
   是否包含已被判定雷同的模板句（硬红）。
2. 分别对三条做跨题比对并报告相似片段；默认只作为人工复核提示。
3. 检查 user_query / success_criteria 是否泄露内部缺陷构造过程。
4. 检查 user_query / success_criteria 是否命中禁止项目或功能点。
5. 检查 user_query 是否写入 Go 版本号或工具链版本。
6. 只读，不修改任何文件。
"""
import argparse
import json
import os
import re
import shlex
import sys
from collections import defaultdict

from user_query_rules import user_query_go_version_issues

BANNED_PHRASES = [
    "之前是好的，估计是最近哪次改动搞出来的",
    "之前是好的，估计是最近哪次改动搞出来的。",
    "仓库就是当前目录",
    "工具链我都装好了",
    "工具链我已装好",
    "工具链我装好了",
    "工具链我装好了，",
    "先别改代码，帮我看看是哪里的问题",
    "工具链我已装好，go test ./... 直接能跑",
    "仓库就是当前目录，go.mod 是 go",
    # 旧版 SKILL 示例句池（曾作为「任选或改写」范例流传，一律禁止原样使用）
    "之前都是好的，最近突然不行了",
    "我印象里前阵子还正常，今天一跑就挂了",
    "以前没这毛病，估计是哪次改动带出来的",
    "刚更新完就出问题了，我没动过测试",
    "代码就在这个目录，go.mod 里写的是",
    "项目就是当前文件夹，Go 是",
    "不用管环境，就这个目录",
    "环境我都配好了",
    "依赖都齐",
    "直接 go test ./... 就能跑",
]

# verify_result 现为机器生成的 pre_fix/post_fix JSON，不参与文案雷同/去 AI 检查
FIELDS = ["user_query", "success_criteria", "verify_cmds"]

# 描述性字段里禁用的「生僻/序号」字符：①②③…、⑴⑵⑶…、ⅠⅡⅢ…、㈠㈡㈢…、ⒶⒷ… 等
FORBIDDEN_CHARS = set(
    "".join(chr(c) for c in range(0x2460, 0x2500))   # ①-⑳ ⑴-⒇ Ⓐ-ⓩ
    + "".join(chr(c) for c in range(0x2160, 0x2180)) # Ⅰ-Ⅻ ⅰ-ⅹ
    + "".join(chr(c) for c in range(0x3220, 0x3230)) # ㈠-㈩
    + "".join(chr(c) for c in range(0x3280, 0x3290)) # ㊀-㊉
)

# 这些短小且必须出现的命令/词，允许重复。
_ALLOWLIST_RAW = {
    "go build ./...",
    "go vet ./...",
}


def _norm_for_allow(s):
    return re.sub(r"\s+", "", s)


ALLOWLIST = {_norm_for_allow(x) for x in _ALLOWLIST_RAW}


def find_record_dirs(root):
    """扫描 root 下任意层级的 YYYY-MM-DD 日期目录，取其直接子目录作为记录目录。"""
    dirs = []
    for dirpath, dirnames, _ in os.walk(root):
        for d in sorted(dirnames):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                date_dir = os.path.join(dirpath, d)
                try:
                    children = os.listdir(date_dir)
                except OSError:
                    continue
                for rec in sorted(children):
                    rp = os.path.join(date_dir, rec)
                    if os.path.isdir(rp):
                        dirs.append(rp)
    seen = set()
    out = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def read_texts(rec_dir):
    """返回 [(field, text, source, path), ...]，三条任一存在即可。"""
    out = []
    cj = os.path.join(rec_dir, "collection.json")
    if os.path.isfile(cj):
        try:
            with open(cj, "r", encoding="utf-8") as f:
                data = json.load(f)
            for field in FIELDS:
                text = data.get(field) or ""
                if text and str(text).strip():
                    out.append((field, str(text).strip(), "collection.json", cj))
        except Exception as e:
            print(f"  [warn] 读 collection.json 失败: {cj}: {e}", file=sys.stderr)

    pt = os.path.join(rec_dir, "prompt.txt")
    if os.path.isfile(pt):
        with open(pt, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if text and not any(field == "user_query" for field, _, _, _ in out):
            out.append(("user_query", text, "prompt.txt", pt))
    return out


def norm(s):
    return re.sub(r"\s+", "", s)


def norm_for_duplicate(field, text):
    """保留描述字段全文；verify_cmds 只比较精确测试标识。

    同一仓库的定向命令天然共享 ``go test``、目标包和固定参数。把这些
    必需骨架按普通文案比较会误报，并诱导用 shell 引号/转义制造假差异。
    ``-run`` 的精确测试标识才是跨题去重时有意义的部分。
    """
    if field != "verify_cmds":
        return norm(text)
    try:
        tokens = shlex.split(text)
    except ValueError:
        return norm(text)
    for index, token in enumerate(tokens):
        if token.startswith("-run="):
            return token.partition("=")[2]
        if token == "-run" and index + 1 < len(tokens):
            return tokens[index + 1]
    return norm(text)


def collect(root):
    records = []
    for rec_dir in find_record_dirs(root):
        texts = read_texts(rec_dir)
        if texts:
            records.append({
                "id": os.path.basename(rec_dir),
                "dir": rec_dir,
                "texts": [{"field": f, "text": t, "source": s, "path": p} for f, t, s, p in texts],
            })
    return records


def check_banned(records):
    bad = []
    for r in records:
        for t in r["texts"]:
            matched = [ph for ph in BANNED_PHRASES if ph in t["text"] or ph in norm(t["text"])]
            if not matched:
                continue
            kept = []
            for ph in sorted(matched, key=len, reverse=True):
                if any(ph in k for k in kept):
                    continue
                kept.append(ph)
            for ph in kept:
                bad.append({"id": r["id"], "field": t["field"], "phrase": ph,
                            "source": t["source"], "path": t["path"]})
    return bad


def check_forbidden_chars(records):
    """检查三条描述性文案里是否出现 ①②③… 这类生僻/序号字符。"""
    bad = []
    for r in records:
        for t in r["texts"]:
            found = sorted({ch for ch in t["text"] if ch in FORBIDDEN_CHARS})
            if found:
                bad.append({"id": r["id"], "field": t["field"], "chars": "".join(found),
                            "source": t["source"], "path": t["path"]})
    return bad


def check_internal_construction(records):
    """检查交付文案是否把程序故障写成了人为构造的缺陷。"""
    from verify_cmds import validate_delivery_field_wording

    bad = []
    for record in records:
        for item in record["texts"]:
            issues = validate_delivery_field_wording({item["field"]: item["text"]})
            for issue in issues:
                bad.append({
                    "id": record["id"],
                    "field": item["field"],
                    "issue": issue,
                    "source": item["source"],
                    "path": item["path"],
                })
    return bad


def check_forbidden_domains(records):
    """检查题面与验收描述是否落入禁止业务类型。"""
    from domain_guard import validate_forbidden_domain

    bad = []
    for record in records:
        for item in record["texts"]:
            if item["field"] not in {"user_query", "success_criteria"}:
                continue
            for issue in validate_forbidden_domain(item["text"]):
                bad.append({
                    "id": record["id"],
                    "field": item["field"],
                    "category": issue["category"],
                    "match": issue["match"],
                    "source": item["source"],
                    "path": item["path"],
                })
    return bad


def check_user_query_go_versions(records):
    """检查 user_query 中的 Go 版本/工具链环境描述。"""
    bad = []
    for record in records:
        for item in record["texts"]:
            if item["field"] != "user_query":
                continue
            for issue in user_query_go_version_issues(item["text"]):
                bad.append({
                    "id": record["id"],
                    "field": item["field"],
                    "issue": issue,
                    "source": item["source"],
                    "path": item["path"],
                })
    return bad


def find_duplicates(records, min_dup):
    """对四个字段分别做两两最长公共块比对。"""
    from difflib import SequenceMatcher
    by_sub = defaultdict(set)
    for field in FIELDS:
        items = []
        for r in records:
            for t in r["texts"]:
                if t["field"] != field:
                    continue
                normalized = norm_for_duplicate(field, t["text"])
                if len(normalized) >= min_dup:
                    items.append((r["id"], normalized))
        for i in range(len(items)):
            a_id, a_text = items[i]
            for j in range(i + 1, len(items)):
                b_id, b_text = items[j]
                sm = SequenceMatcher(None, a_text, b_text)
                for block in sm.get_matching_blocks():
                    size = block.size
                    if size < min_dup:
                        continue
                    sub = a_text[block.a:block.a + size]
                    if sub in ALLOWLIST:
                        continue
                    by_sub[(field, sub)].update([a_id, b_id])

    dup = []
    for (field, sub), ids in by_sub.items():
        if len(ids) > 1:
            dup.append({"field": field, "sub": sub, "ids": sorted(ids)})
    dup.sort(key=lambda d: (-len(d["sub"]), d["field"], d["sub"]))
    return dup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--min-dup", type=int, default=24,
                    help="连续重复片段的报告阈值（默认 24）")
    ap.add_argument("--strict-duplicates", action="store_true",
                    help="把跨题连续重复片段升级为失败；默认仅报告")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    records = collect(root)
    if not records:
        print("未找到 prompt.txt 或 collection.json。请在流水线根目录执行，或确认已完成题面写作。")
        return 1

    print(f"扫描根目录: {root}")
    print(f"记录数: {len(records)}")

    banned = check_banned(records)
    dups = find_duplicates(records, args.min_dup)
    forbidden = check_forbidden_chars(records)
    internal = check_internal_construction(records)
    domains = check_forbidden_domains(records)
    go_versions = check_user_query_go_versions(records)

    hard_dups = dups if args.strict_duplicates else []
    if not banned and not hard_dups and not forbidden and not internal and not domains and not go_versions:
        print("文案自检通过：未发现模板泄漏、内部构造措辞、禁止业务类型、Go 版本环境描述或生僻序号字符。")
        if dups:
            print("相似度提示（不阻断）：发现 {} 处 >= {} 字连续重复片段，请人工确认是否模板化。".format(len(dups), args.min_dup))
        return 0

    if banned:
        print("\n[硬红] 命中已判雷同的模板句，必须改写：")
        for item in banned:
            print("  - {id}/{field} ({source}): {phrase!r}\n    {path}".format(**item))

    if forbidden:
        print("\n[硬红] 出现生僻/序号字符（①②③… 等），必须改成普通写法（如 1. 2. 3. 或 ；分隔）：")
        for item in forbidden:
            print("  - {id}/{field} ({source}): {chars!r}\n    {path}".format(**item))

    if internal:
        print("\n[硬红] 交付文案泄露内部缺陷构造过程，必须改成程序本身存在问题的叙事：")
        for item in internal:
            print("  - {id}/{field} ({source}): {issue}\n    {path}".format(**item))

    if domains:
        print("\n[硬红] 题面或验收描述命中禁止业务类型，必须更换功能点，禁止只改写措辞：")
        for item in domains:
            print("  - {id}/{field} ({source}): {category} 命中 {match!r}\n    {path}".format(**item))

    if go_versions:
        print("\n[硬红] user_query 不得写 Go 版本号或工具链版本，请改用‘当前项目’的自然表达：")
        for item in go_versions:
            print("  - {id}/{field} ({source}): {issue}\n    {path}".format(**item))

    if dups:
        level = "硬红" if args.strict_duplicates else "提示"
        print("\n[{}] 任意两题的同一字段存在 >= {} 字连续重复片段:".format(level, args.min_dup))
        shown = 0
        for item in dups:
            print("  - [{field}] {sub!r} 出现在 {ids}".format(field=item["field"], sub=item["sub"], ids=", ".join(item["ids"])))
            shown += 1
            if shown >= 80:
                print("  ... 还有更多，已截断")
                break

    if banned or forbidden or internal or domains or go_versions or hard_dups:
        print("\n请修复硬性问题后重跑。相似度提示由人工结合真实场景判断，不要求机械改写。")
        return 1
    print("\n仅发现相似度提示，不阻断流程。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
