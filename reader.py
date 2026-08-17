#!/usr/bin/env python3
"""
本地漫画阅读器服务器 (零依赖)

用法:
    python3 reader.py [--port 8080] [--dir downloads]

目录结构:
    downloads/{comic_id}/{NN}_{第N话}/pages/{0001.jpg,...}
    catalog.csv : id,title

API:
    GET /             -> 跳转到 /web/index.html
    GET /api/comics   -> 漫画列表
    GET /api/comic/id -> 漫画详情
    DELETE /api/comic/id -> 删除漫画并记录 id(重新下载时跳过)

静态文件:
    /web/...         -> 前端页面
    /downloads/...   -> 漫画图片

已删除漫画 id 记录在 deleted.json, unlock.py 下载时会跳过

打开 http://127.0.0.1:8080/ 即可阅读
"""
import json, os, re, shutil, sys
from http.server import HTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS = os.path.join(HERE, "downloads")
DELETED_FILE = os.path.join(HERE, "deleted.json")


def load_deleted():
    """deleted.json → set of 已删除的漫画 id"""
    if not os.path.exists(DELETED_FILE):
        return set()
    try:
        with open(DELETED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(str(x) for x in (data or []))
    except Exception:
        return set()


def save_deleted(ids):
    """把已删除漫画 id 集合写回 deleted.json"""
    with open(DELETED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=1)


def load_catalog():
    """catalog.csv → {id: title}"""
    path = os.path.join(HERE, "catalog.csv")
    if not os.path.exists(path):
        return {}
    catalog = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        next(f, None)
        for line in f:
            line = line.rstrip("\n")
            if not line or "," not in line:
                continue
            i = line.find(",")
            catalog[line[:i]] = line[i + 1:]
    return catalog


def scan_comics():
    """扫描 downloads 下所有漫画"""
    catalog = load_catalog()
    comics = []
    if not os.path.isdir(DOWNLOADS):
        return comics
    for cid in sorted(os.listdir(DOWNLOADS)):
        d = os.path.join(DOWNLOADS, cid)
        if not os.path.isdir(d):
            continue
        chapters = []
        for item in sorted(os.listdir(d)):
            pages = os.path.join(d, item, "pages")
            if os.path.isdir(pages):
                chapters.append(item)
        if not chapters:
            continue
        cover = None
        first = os.path.join(d, chapters[0], "pages")
        if os.path.isdir(first):
            files = sorted(os.listdir(first))
            if files:
                cover = f"/downloads/{cid}/{chapters[0]}/pages/{files[0]}"
        total = 0
        for ch_name in chapters:
            pages_dir = os.path.join(d, ch_name, "pages")
            if os.path.isdir(pages_dir):
                total += len([f for f in os.listdir(pages_dir)
                             if f.lower().endswith((".jpg",".jpeg",".png",".webp",".gif"))])
        comics.append({
            "id": cid,
            "title": catalog.get(cid, cid),
            "cover": cover,
            "chapterCount": len(chapters),
            "totalPages": total,
        })
    comics.sort(key=lambda c: c["title"])
    return comics


def comic_detail(cid):
    """返回单部漫画详情"""
    d = os.path.join(DOWNLOADS, cid)
    if not os.path.isdir(d):
        return None
    chapters = []
    for item in sorted(os.listdir(d)):
        pages = os.path.join(d, item, "pages")
        if not os.path.isdir(pages):
            continue
        files = sorted(f for f in os.listdir(pages)
                       if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")))
        if not files:
            continue
        title = re.sub(r"^\d+_", "", item) or item
        chapters.append({
            "dir": item, "title": title,
            "pages": len(files), "files": files,
        })
    catalog = load_catalog()
    return {
        "id": cid,
        "title": catalog.get(cid, cid),
        "chapters": chapters,
        "chapterCount": len(chapters),
    }


class Handler(SimpleHTTPRequestHandler):
    _COMICS = scan_comics()

    def refresh(self):
        """重新扫描漫画列表"""
        self._COMICS = scan_comics()

    def do_DELETE(self):
        path = self.path.rstrip("/")
        m = re.match(r"^/api/comic/([^/]+)$", path)
        if not m:
            self._json({"ok": False, "error": "bad request"}, 400)
            return
        cid = urllib.parse.unquote(m.group(1))
        d = os.path.join(DOWNLOADS, cid)
        # 记录 id(即使目录不存在也记录, 保证重下时跳过)
        deleted = load_deleted()
        deleted.add(cid)
        save_deleted(deleted)
        # 删除本地目录(含整个漫画)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        self.refresh()
        self._json({"ok": True, "id": cid, "deleted": sorted(deleted)})

    def do_GET(self):
        path = self.path.rstrip("/") or "/"

        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/web/index.html")
            self.end_headers()
            return

        if path == "/api/comics":
            self._json({"ok": True, "comics": self._COMICS})
            return

        m = re.match(r"^/api/comic/([^/]+)$", path)
        if m:
            detail = comic_detail(urllib.parse.unquote(m.group(1)))
            if detail:
                self._json({"ok": True, "comic": detail})
            else:
                self._json({"ok": False, "error": "not found"}, 404)
            return

        super().do_GET()

    def _json(self, obj, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())

    def log_message(self, fmt, *args):
        sys.stderr.write("[reader] %s\n" % (fmt % args))


if __name__ == "__main__":
    import argparse, urllib
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args()
    os.chdir(HERE)
    print(f"[reader] http://{args.bind}:{args.port}/")
    HTTPServer((args.bind, args.port), Handler).serve_forever()
