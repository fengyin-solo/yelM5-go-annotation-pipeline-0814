#!/usr/bin/env python3
"""题面/收集表文案雷同自检。

扫描 <root> 下任意层级的 YYYY-MM-DD/<record>/prompt.txt 与 collection.json：
1. 检查 user_query / success_criteria / verify_cmds 三条
   是否包含已被判定雷同的模板句（硬红）。
2. 分别对三条做跨题比对：任意两题的同一字段存在 >= --min-dup 个字符的
   完全相同的连续片段即硬红（verify_cmds 只比较精确测试标识，忽略必需命令骨架）。
3. 只读，不修改任何文件。
"""
import argparse
import json
import os
import re
import shlex
import sys
from collections import defaultdict

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
    ap.add_argument("--min-dup", type=int, default=12,
                    help="连续重复片段的最小字符数（默认 12）")
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

    if not banned and not dups and not forbidden:
        print("去重自检通过：三条文案均未发现被禁模板句、生僻/序号字符，也未发现 >= {} 字的跨题连续重复片段。".format(args.min_dup))
        return 0

    if banned:
        print("\n[硬红] 命中已判雷同的模板句，必须改写：")
        for item in banned:
            print("  - {id}/{field} ({source}): {phrase!r}\n    {path}".format(**item))

    if forbidden:
        print("\n[硬红] 出现生僻/序号字符（①②③… 等），必须改成普通写法（如 1. 2. 3. 或 ；分隔）：")
        for item in forbidden:
            print("  - {id}/{field} ({source}): {chars!r}\n    {path}".format(**item))

    if dups:
        print("\n[硬红] 任意两题的同一字段存在 >= {} 字连续重复片段:".format(args.min_dup))
        shown = 0
        for item in dups:
            print("  - [{field}] {sub!r} 出现在 {ids}".format(field=item["field"], sub=item["sub"], ids=", ".join(item["ids"])))
            shown += 1
            if shown >= 80:
                print("  ... 还有更多，已截断")
                break

    print("\n请逐条改写，使现象/废话/环境/验收三类文案句式错开；改完重跑本命令直到全绿。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
