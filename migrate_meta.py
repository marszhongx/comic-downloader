#!/usr/bin/env python3
"""
一次性迁移脚本: 为 downloads/ 下每部已下载的漫画生成 meta.json

    downloads/{comic_id}/meta.json
        {"name": 标题, "chapters": {章号: 页数}}

数据来源:
    name     <- catalog.csv (取不到则用 comic_id)
    chapters <- 旧的全局 downloads/meta.json (key 为 "{comic_id}/{章号}")

迁移完成后删除旧的全局 downloads/meta.json 和 catalog.csv。

用法:
    python3 migrate_meta.py          # 执行迁移
    python3 migrate_meta.py --dry    # 只预览, 不写文件
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS = os.path.join(HERE, "downloads")
CATALOG_CSV = os.path.join(HERE, "catalog.csv")
LEGACY_META = os.path.join(DOWNLOADS, "meta.json")


def load_catalog():
    catalog = {}
    if not os.path.exists(CATALOG_CSV):
        return catalog
    with open(CATALOG_CSV, "r", encoding="utf-8-sig") as f:
        next(f, None)
        for line in f:
            line = line.rstrip("\n")
            if not line or "," not in line:
                continue
            i = line.find(",")
            catalog[line[:i]] = line[i + 1:]
    return catalog


def main():
    dry = "--dry" in sys.argv
    catalog = load_catalog()

    legacy = {}
    if os.path.exists(LEGACY_META):
        legacy = json.load(open(LEGACY_META))

    written = skipped = 0
    for cid in sorted(os.listdir(DOWNLOADS)):
        d = os.path.join(DOWNLOADS, cid)
        if not os.path.isdir(d):
            continue
        meta_path = os.path.join(d, "meta.json")
        if os.path.exists(meta_path):
            skipped += 1
            continue
        prefix = f"{cid}/"
        chapters = {k[len(prefix):]: v for k, v in legacy.items()
                    if isinstance(k, str) and k.startswith(prefix)}
        data = {"name": catalog.get(cid, cid), "chapters": chapters}
        if dry:
            print(f"[dry] {cid} -> name={data['name'][:30]!r} chapters={len(chapters)}")
        else:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        written += 1

    print(f"[+] 迁移完成: 写入 {written} 部, 已有 meta.json 跳过 {skipped} 部")
    if not dry:
        if os.path.exists(LEGACY_META):
            os.remove(LEGACY_META)
            print(f"[+] 已删除旧的全局 {LEGACY_META}")
        if os.path.exists(CATALOG_CSV):
            os.remove(CATALOG_CSV)
            print(f"[+] 已删除 {CATALOG_CSV}")


if __name__ == "__main__":
    main()
