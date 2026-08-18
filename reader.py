#!/usr/bin/env python3
"""
本地漫画阅读器服务器 (零依赖)

用法:
    python3 reader.py [--port 8080] [--dir downloads]

目录结构:
    downloads/{comic_id}/{NN}_{第N话}/pages/{0001.jpg,...}
    downloads/{comic_id}/meta.json : {"name": 标题, "chapters": {章号: 页数}}

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
import json, os, re, shutil, sys, urllib.parse
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


def comic_title(cid):
    """漫画标题: 读本漫画 meta.json 的 name, 取不到用 id"""
    p = os.path.join(DOWNLOADS, cid, "meta.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                name = json.load(f).get("name")
            if name:
                return name
        except Exception:
            pass
    return cid


def scan_comics():
    """扫描 downloads 下所有漫画"""
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
            "title": comic_title(cid),
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
    return {
        "id": cid,
        "title": comic_title(cid),
        "chapters": chapters,
        "chapterCount": len(chapters),
    }


class Handler(SimpleHTTPRequestHandler):
    def do_DELETE(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        m = re.match(r"^/api/comic/([^/]+)$", path)
        if not m:
            self._json({"ok": False, "error": "bad request"}, 400)
            return
        cid = urllib.parse.unquote(m.group(1))
        # id 只能是 downloads 下的单层目录名, 防止路径穿越
        if not cid or cid in (".", "..") or os.path.basename(cid) != cid:
            self._json({"ok": False, "error": "invalid id"}, 400)
            return
        d = os.path.join(DOWNLOADS, cid)
        try:
            if os.path.isdir(d):
                shutil.rmtree(d)
            if os.path.exists(d):
                raise OSError("comic directory still exists after deletion")
        except OSError as e:
            self._json({"ok": False, "error": f"delete failed: {e}"}, 500)
            return

        # 文件确实删除后再记录 id, 保证重新下载时跳过
        deleted = load_deleted()
        deleted.add(cid)
        save_deleted(deleted)
        self._json({"ok": True, "id": cid})

    def do_GET(self):
        path = self.path.rstrip("/") or "/"

        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/web/index.html")
            self.end_headers()
            return

        if path == "/api/comics":
            self._json({"ok": True, "comics": scan_comics()})
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
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args()
    os.chdir(HERE)
    print(f"[reader] http://{args.bind}:{args.port}/")
    HTTPServer((args.bind, args.port), Handler).serve_forever()
