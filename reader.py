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
import hashlib
import json, os, re, shutil, sys, unicodedata, urllib.parse
from http.server import HTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS = os.path.join(HERE, "downloads")
DELETED_FILE = os.path.join(HERE, "deleted.json")
SOURCE_COMIC_URL = "https://bz2vraf.live/comic?id={comic_id}"  # 原站点(unlock.py 的下载源)详情页


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


def chapter_sort_key(name):
    """按章节号排序，而不是按目录名字典序。

    下载器目录通常是 ``02_第2话``，但旧目录、手工导入的目录可能是
    ``第10话``、``10_...`` 或只包含普通标题。优先使用目录前缀，
    再从标题中识别“第 N 话/章/回”，最后使用自然排序。
    """
    text = str(name)
    m = re.match(r"^\s*(\d+)(?:_|-|\s|$)", text)
    if not m:
        m = re.search(r"第\s*(\d+(?:\.\d+)?)\s*(?:话|話|章|回)", text, re.I)
    if m:
        try:
            return (0, float(m.group(1)), text.casefold())
        except ValueError:
            pass
    # 没有章节号的标题也采用自然排序，避免“番外10”排在“番外2”前。
    parts = re.split(r"(\d+(?:\.\d+)?)", text.casefold())
    natural = tuple(float(x) if re.fullmatch(r"\d+(?:\.\d+)?", x) else x for x in parts)
    return (1, natural)


def chapter_number(name):
    """从章节目录名中提取可供前端显示/跳转的章节号。"""
    text = str(name)
    m = re.match(r"^\s*(\d+(?:\.\d+)?)(?:_|-|\s|$)", text)
    if not m:
        m = re.search(r"第\s*(\d+(?:\.\d+)?)\s*(?:话|話|章|回)", text, re.I)
    return m.group(1) if m else None


def chapter_title(name):
    """去掉下载器使用的数字前缀，同时保留原始章节标题。"""
    return re.sub(r"^\s*\d+(?:\.\d+)?[_-]", "", name).strip() or name


def chapter_dirs(comic_dir):
    """返回按章节号排列的章节目录，避免 1、10、2 的错误顺序。"""
    result = []
    for item in os.listdir(comic_dir):
        if os.path.isdir(os.path.join(comic_dir, item, "pages")):
            result.append(item)
    return sorted(result, key=chapter_sort_key)


def series_title(name):
    """从标题提取系列名，用于合并分卷。"""
    s = unicodedata.normalize("NFKC", name).strip()
    # 先去掉“前编/中编”等后缀，再去掉卷号；这样“3 前編”也能识别。
    s = re.sub(r"\s*(?:前|中|后|後)編\s*$", "", s, flags=re.I)
    s = re.sub(r"\s*(?:前|中|后|後)篇\s*$", "", s, flags=re.I)
    s = re.sub(r"\s*(?:[#＃]\s*\d+|第\s*\d+\s*[话話章回]|(?:vol\.?|volume|part)\s*\d+|[。．.]?\s*\d+)\s*$", "", s, flags=re.I)
    return s.strip(" 。．.") or name


def series_id(title):
    return "series-" + hashlib.sha1(title.encode("utf-8")).hexdigest()[:16]


def series_key(name):
    """按“第 N 话”截断标题，截断前一致才视为同一系列。"""
    s = unicodedata.normalize("NFKC", name).strip()
    m = re.search(r"第\s*\d+(?:\.\d+)?\s*[话話章回]", s, re.I)
    if m:
        s = s[:m.start()]
    else:
        s = series_title(s)
    s = s.replace(",", "、").replace("，", "、")
    s = re.sub(r"\s+", "", s)
    return s.casefold().strip(" 。．.")


def local_comics():
    result = []
    if not os.path.isdir(DOWNLOADS):
        return result
    for cid in sorted(os.listdir(DOWNLOADS)):
        d = os.path.join(DOWNLOADS, cid)
        if os.path.isdir(d) and chapter_dirs(d):
            result.append({"id": cid, "title": comic_title(cid), "dir": d})
    return result


def series_groups():
    """按规范化后的系列名分组，同时保留一个可读的显示标题。"""
    groups = {}
    for c in local_comics():
        key = series_key(c["title"])
        if key not in groups:
            groups[key] = [series_title(c["title"]), []]
        groups[key][1].append(c)
        # 优先使用较短的标题作为显示名，避免把卷号带到系列标题里。
        groups[key][0] = min(groups[key][0], series_title(c["title"]), key=len)
    return {key: (value[0], value[1]) for key, value in groups.items()}


def find_series(sid):
    for key, (title, items) in series_groups().items():
        if series_id(title) == sid:
            return title, items
    return None, None


def book_order(name):
    """提取分本顺序：数字卷号优先，其次前/中/后篇。"""
    s = unicodedata.normalize("NFKC", name)
    m = re.search(r"(?:[#＃]|第|vol\.?|volume|part)?\s*(\d+(?:\.\d+)?)\s*(?:[话話章回])?(?:\s*(?:前|中|后|後)[編篇])?\s*$", s, re.I)
    if m:
        return (0, float(m.group(1)), s.casefold())
    for number, marker in enumerate(("前編", "前篇", "中編", "中篇", "後編", "后編", "後篇", "后篇"), 1):
        if s.endswith(marker):
            return (1, number, s.casefold())
    return (2, 0, s.casefold())


def scan_comics():
    """扫描 downloads 下所有本子；分本保持独立显示。"""
    comics = []
    for item in local_comics():
        chapters = chapter_dirs(item["dir"])
        cover = None
        if chapters:
            files = sorted(os.listdir(os.path.join(item["dir"], chapters[0], "pages")))
            if files:
                cover = f"/downloads/{item['id']}/{chapters[0]}/pages/{files[0]}"
        total = 0
        for ch in chapters:
            pd = os.path.join(item["dir"], ch, "pages")
            total += len([f for f in os.listdir(pd) if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))])
        comics.append({"id": item["id"], "title": item["title"], "cover": cover,
                       "chapterCount": len(chapters), "totalPages": total})
    return sorted(comics, key=lambda c: int(c["id"]) if str(c["id"]).isdigit() else str(c["id"]), reverse=True)


def one_comic_detail(cid):
    d = os.path.join(DOWNLOADS, cid)
    if not os.path.isdir(d): return None
    chapters = []
    for item in chapter_dirs(d):
        pages = os.path.join(d, item, "pages")
        files = sorted(f for f in os.listdir(pages) if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")))
        if files:
            chapters.append({"dir": item, "sourceId": cid, "number": chapter_number(item), "title": chapter_title(item), "pages": len(files), "files": files})
    return {"id": cid, "title": comic_title(cid), "chapters": chapters, "chapterCount": len(chapters), "volumeCount": 1,
            "sourceUrl": SOURCE_COMIC_URL.format(comic_id=urllib.parse.quote(str(cid)))}


def comic_detail(cid):
    """返回单本详情，并附上同系列前一本/后一本。"""
    detail = one_comic_detail(cid)
    if not detail:
        return None
    siblings = [c for c in local_comics() if series_key(c["title"]) == series_key(detail["title"])]
    siblings.sort(key=lambda c: book_order(c["title"]))
    pos = next((i for i, c in enumerate(siblings) if c["id"] == cid), -1)
    if pos > 0:
        detail["previousBook"] = {"id": siblings[pos - 1]["id"], "title": siblings[pos - 1]["title"]}
    if 0 <= pos < len(siblings) - 1:
        detail["nextBook"] = {"id": siblings[pos + 1]["id"], "title": siblings[pos + 1]["title"]}
    detail["seriesCount"] = len(siblings)
    return detail


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
