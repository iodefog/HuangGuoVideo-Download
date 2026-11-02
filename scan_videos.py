#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扫描 https://huangguo.me 上所有类型（分类/榜单/列表/标签/专题/系列）下的视频下载地址。

发现方式（默认全部启用，自动去重）：
  1) sitemap: /sitemap.xml -> sitemap-playletdetail(剧集) / sitemap-videos(系列视频)
              / sitemap-pages(分类·列表·标签·专题·榜单等入口页)
  2) browse : 首页 + 上述入口页全部抓取，并从页面里再提取 /detail/、/series/watch/
  3) series : /series/ -> /series/all/{分类}/{页}/ 自动翻页补齐

解析方式：
  A. 剧集 /detail/{slug}/ -> /play/{slug}/ 取全部分集 chapterId
     -> /api/playlet/chapter/detail?chapterId=&playletSlug= 取 videoUrl
  B. 系列 /series/watch/{id}/ 页面内嵌 JSON 的 video.url（相对路径）
  C. 兜底：所有抓到的 HTML 正则扫直链（<video>/<source>/JSON 字段/相对 m3u8 路径）
  相对路径统一按前端规则拼成 https://huangguo.me/h5/m3u8/{path}

输出：JSON + CSV（默认 video_links.json / video_links.csv），仅依赖标准库。
"""

import argparse
import csv
import gzip
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

VIDEO_EXT = r"(?:m3u8|mp4|m4v|webm|mov|ogv|flv|ts|m3u|mkv|avi)"
URL_CHARS = r"[^" r"\"'\s<>\\()\[\]{}`|]+"

RE_DIRECT_URL = re.compile(r"(https?:)?//" + URL_CHARS + r"\." + VIDEO_EXT + r"(?:\?" + URL_CHARS + r")?", re.I)
RE_RELATIVE_URL = re.compile(r"[\"'](/[A-Za-z0-9_\-./]*?\." + VIDEO_EXT + r"(?:\?" + URL_CHARS + r")?)[\"']", re.I)
# 内嵌 JSON： "url": "v3/sp/xxx.m3u8?id=..&t=..&ticket=.." （可能不带前导 /）
RE_JSON_VIDEO = re.compile(r"[\"'](?:url|videoUrl|playUrl|hlsUrl|src|file)[\"']\s*:\s*[\"']([^\"']{10,2000})[\"']", re.I)
RE_DETAIL = re.compile(r"/detail/([A-Za-z0-9_\-]+)/?")
RE_SERIES = re.compile(r"/series/watch/(\d+)/?")
RE_SERIES_ALL = re.compile(r"/series/all/(\d+)/(\d+)/?")
RE_CHAPTER_ID = re.compile(r"data-chapter-id=[\"'](\d+)[\"']")
RE_PLAYLET_ID = re.compile(r"data-(?:playlet|drama)-id=[\"'](\d+)[\"']")
RE_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
RE_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
RE_OG_TITLE = re.compile(r'property="og:title"\s+content="([^"]*)"')
RE_LOC = re.compile(r"<loc>([^<]+)</loc>")
RE_HREFS = re.compile(r'href="(/(?:category|list|tag|topic|rank|series|comics)/[^"]*)"')

TYPE_PAGE_PREFIX = ("/category/", "/list/", "/tag/", "/topic/", "/rank/", "/series/", "/comics/")

FIELDS = ["kind", "type", "url", "title", "drama", "episode", "chapterId", "seriesId",
          "slug", "section", "page", "status", "verified", "note", "source"]


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def guess_ext(url):
    m = re.search(r"\.(" + VIDEO_EXT + r")(?:\?|#|$)", url, re.I)
    return m.group(1).lower() if m else ("m3u8" if "m3u8" in url.lower() else "")


class Fetcher:
    def __init__(self, base, cookie=None, delay=0.0, timeout=20, retries=2):
        self.base = base.rstrip("/")
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.cookie = cookie
        self._lock = threading.Lock()
        self._last = 0.0
        self.count = 0

    def _throttle(self):
        if self.delay <= 0:
            return
        with self._lock:
            wait = self.delay - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def get(self, url, referer=None, headers=None):
        if url.startswith("/"):
            url = self.base + url
        self._throttle()
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "*/*")
        req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
        req.add_header("Accept-Encoding", "gzip, deflate")
        req.add_header("Referer", referer or (self.base + "/"))
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        for k, v in (headers or {}).items():
            req.add_header(k, v)

        last_err = None
        for attempt in range(self.retries + 1):
            try:
                self.count += 1
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    enc = (resp.headers.get("Content-Encoding") or "").lower()
                    if "gzip" in enc:
                        raw = gzip.decompress(raw)
                    elif "deflate" in enc:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                    return {"ok": True, "status": resp.status, "url": resp.geturl(),
                            "type": resp.headers.get("Content-Type", ""),
                            "text": raw.decode("utf-8", "ignore")}
            except urllib.error.HTTPError as e:
                last_err = "HTTP %s" % e.code
                if e.code in (400, 401, 403, 404, 410):
                    break
                time.sleep(0.6 * (attempt + 1))
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                time.sleep(0.6 * (attempt + 1))
        return {"ok": False, "status": 0, "url": url, "type": "", "text": "", "error": last_err}


def scan_html_for_links(html, base):
    """返回 (直链列表, detail slug 列表, series id 列表, 类型页 URL 列表)"""
    direct = []
    for m in RE_DIRECT_URL.finditer(html):
        u = m.group(0)
        direct.append(("https:" + u) if u.startswith("//") else u)
    for m in RE_RELATIVE_URL.finditer(html):
        direct.append(base + m.group(1))
    unescaped = html.replace("\\u0026", "&")
    for m in RE_JSON_VIDEO.finditer(unescaped):
        v = m.group(1).strip()
        if not re.search(r"\." + VIDEO_EXT + r"(?:\?|$)", v, re.I):
            continue
        if re.match(r"^https?://", v, re.I):
            direct.append(v)
        elif v.startswith("//"):
            direct.append("https:" + v)
        else:  # 相对路径（可能无前导 /），走 /h5/m3u8/ 代理
            direct.append(base + "/h5/m3u8/" + v.lstrip("/"))
    pages = [base + p for p in RE_HREFS.findall(html)]
    return direct, RE_DETAIL.findall(html), RE_SERIES.findall(html), pages


class Scanner:
    def __init__(self, args):
        self.args = args
        self.base = args.base.rstrip("/")
        self.fetcher = Fetcher(self.base, args.cookie, args.delay, args.timeout)
        self.results = OrderedDict()
        self.lock = threading.Lock()
        self.stats = {"dramas": 0, "chapters": 0, "series": 0, "pages": 0,
                      "with_url": 0, "locked": 0}
        self.sections = {}   # slug/seriesId -> 所属入口（分类/标签/专题/榜单/系列）

    # ---------------- 1. sitemap ----------------
    def from_sitemap(self):
        dramas, series, pages = [], [], []
        idx = self.fetcher.get(self.base + "/sitemap.xml")
        if not idx["ok"]:
            print("      [warn] sitemap.xml 不可访问，改用页面爬取")
            return dramas, series, pages
        subs = RE_LOC.findall(idx["text"])
        if not subs:
            subs = [self.base + "/sitemap.xml"]

        def fetch_sub(u):
            r = self.fetcher.get(u)
            return r["text"] if r["ok"] else ""

        with ThreadPoolExecutor(max_workers=min(8, self.args.workers)) as ex:
            texts = list(ex.map(fetch_sub, subs))
        for text in texts:
            for loc in RE_LOC.findall(text):
                path = loc.replace(self.base, "")
                m = RE_DETAIL.search(path)
                if m:
                    dramas.append(m.group(1))
                    continue
                m = RE_SERIES.search(path)
                if m:
                    series.append(m.group(1))
                    continue
                if any(path.startswith(p) for p in TYPE_PAGE_PREFIX):
                    pages.append(loc)
        return list(OrderedDict.fromkeys(dramas)), list(OrderedDict.fromkeys(series)), list(OrderedDict.fromkeys(pages))

    # ---------------- 2. 浏览所有类型入口 ----------------
    @staticmethod
    def section_of(url):
        m = re.search(r"/(category|list|tag|topic|comics)/([^/]+)/?", url)
        if m:
            return "%s:%s" % (m.group(1), m.group(2))
        m = re.search(r"/series/all/(\d+)/", url)
        if m:
            return "series-cat:%s" % m.group(1)
        return "rank" if "/rank" in url else "home"

    def mark_section(self, key, section):
        if not section:
            return
        with self.lock:
            self.sections.setdefault(key, set()).add(section)

    def browse(self, seed_pages):
        dramas, series, more = [], [], []
        todo = list(OrderedDict.fromkeys([self.base + "/"] + seed_pages))
        if self.args.section:  # 指定类型时优先抓该类型的入口页
            frag = "/" + self.args.section.replace(":", "/") + "/"
            todo.sort(key=lambda u: 0 if frag in u else 1)
        seen = set()
        depth = 0
        while todo and depth < 2:
            depth += 1
            batch = [u for u in todo if u not in seen]
            if self.args.max_type_pages:
                room = self.args.max_type_pages - len(seen)
                if room <= 0:
                    break
                batch = batch[:room]
            for u in batch:
                seen.add(u)
            if not batch:
                break
            with ThreadPoolExecutor(max_workers=self.args.workers) as ex:
                futs = {ex.submit(self.fetcher.get, u): u for u in batch}
                for f in as_completed(futs):
                    url = futs[f]
                    r = f.result()
                    self.stats["pages"] += 1
                    if not r["ok"]:
                        continue
                    _, ds, ss, ps = scan_html_for_links(r["text"], self.base)
                    sec = self.section_of(r.get("url") or url)
                    for s in ds:
                        self.mark_section(s, sec)
                    for sid in ss:
                        self.mark_section("s" + sid, sec)
                    dramas += ds
                    series += ss
                    more += [p for p in ps if p not in seen]
            todo = more
            more = []
        return list(OrderedDict.fromkeys(dramas)), list(OrderedDict.fromkeys(series))

    # ---------------- 3. 系列分页补齐 ----------------
    def series_from_lists(self):
        ids = []
        r = self.fetcher.get(self.base + "/series/")
        if not r["ok"]:
            return ids
        cats = OrderedDict()
        for cid, page in RE_SERIES_ALL.findall(r["text"]):
            cats.setdefault(cid, int(page))
        ids += RE_SERIES.findall(r["text"])
        for cid in cats:
            for p in range(1, self.args.series_pages + 1):
                rp = self.fetcher.get("%s/series/all/%s/%d/" % (self.base, cid, p))
                if not rp["ok"]:
                    break
                found = RE_SERIES.findall(rp["text"])
                if not found:
                    break
                before = len(ids)
                ids = list(OrderedDict.fromkeys(ids + found))
                if len(ids) == before and p > 1:
                    break
        return ids

    # ---------------- 4. 剧集：分集 -> API ----------------
    def chapters_of(self, slug):
        r = self.fetcher.get("%s/play/%s/" % (self.base, slug))
        if not r["ok"]:
            return [], {}
        html = r["text"]
        ids = list(OrderedDict.fromkeys(RE_CHAPTER_ID.findall(html)))
        m = RE_PLAYLET_ID.search(html)
        title = ""
        mt = RE_H1.search(html) or RE_OG_TITLE.search(html) or RE_TITLE.search(html)
        if mt:
            title = strip_tags(mt.group(1))
        if self.args.max_chapters and len(ids) > self.args.max_chapters:
            ids = ids[: self.args.max_chapters]
        return ids, {"playletId": m.group(1) if m else "", "title": title}

    def resolve_chapter(self, slug, chapter_id, meta):
        api = "%s/api/playlet/chapter/detail?chapterId=%s&playletSlug=%s" % (self.base, chapter_id, slug)
        r = self.fetcher.get(api, referer="%s/play/%s/%s/" % (self.base, slug, chapter_id))
        rec = {"kind": "drama", "type": "", "url": "", "title": meta.get("title", ""),
               "drama": meta.get("title", ""), "episode": "", "chapterId": chapter_id,
               "seriesId": "", "slug": slug,
               "section": ",".join(sorted(self.sections.get(slug, []))[:5]) or "sitemap",
               "page": "%s/play/%s/%s/" % (self.base, slug, chapter_id),
               "status": "no_url", "verified": "", "note": "", "source": "api:chapter/detail"}
        if not r["ok"]:
            rec["status"] = "api_error"
            rec["note"] = r.get("error", "")
            return rec
        try:
            data = json.loads(r["text"])
        except Exception:
            rec["status"] = "bad_json"
            return rec
        if data.get("code") != 200 or not data.get("data"):
            rec["status"] = "api_fail"
            rec["note"] = data.get("tip") or data.get("msg") or ""
            return rec
        d = data["data"]
        rec["episode"] = d.get("currentEpisode", "")
        rec["title"] = d.get("title") or rec["title"]
        if d.get("needUnlock") and not d.get("canWatch"):
            rec["note"] = "locked(payType=%s,coins=%s)" % (d.get("payType"), d.get("singleCoins"))
        raw = (d.get("videoUrl") or "").strip()
        for key in ("h265Url", "videoUrlH265", "hevcUrl"):
            if d.get(key):
                u2 = self.normalize(d[key])
                self.add_result(dict(rec, url=u2, type=guess_ext(u2) or "m3u8", status="ok",
                                     source="api:%s" % key, note=rec["note"]))
        if not raw:
            return rec
        url = self.normalize(raw)
        rec.update({"url": url, "type": guess_ext(url) or "m3u8", "status": "ok"})
        return rec

    # ---------------- 5. 系列视频页 ----------------
    def resolve_series(self, sid):
        page = "%s/series/watch/%s/" % (self.base, sid)
        r = self.fetcher.get(page)
        rec = {"kind": "series", "type": "", "url": "", "title": "", "drama": "",
               "episode": "", "chapterId": "", "seriesId": sid, "slug": "",
               "section": ",".join(sorted(self.sections.get("s" + sid, []))) or "series",
               "page": page, "status": "no_url", "verified": "", "note": "",
               "source": "html:series/watch"}
        if not r["ok"]:
            rec["status"] = "fetch_error"
            rec["note"] = r.get("error", "")
            return rec
        html = r["text"].replace("\\u0026", "&")
        mt = RE_H1.search(html) or RE_OG_TITLE.search(html) or RE_TITLE.search(html)
        if mt:
            rec["title"] = strip_tags(mt.group(1))
        urls = []
        for m in RE_JSON_VIDEO.finditer(html):
            v = m.group(1).strip()
            if re.search(r"\." + VIDEO_EXT + r"(?:\?|$)", v, re.I):
                urls.append(v)
        if not urls:
            rec["status"] = "locked" if re.search(r"needUnlock|isLocked", html) else "no_url"
            if rec["status"] == "locked":
                rec["note"] = "locked"
            return rec
        url = self.normalize(urls[0])
        rec.update({"url": url, "type": guess_ext(url) or "m3u8", "status": "ok"})
        extra = [dict(rec, url=self.normalize(u), type=guess_ext(self.normalize(u)) or "m3u8",
                      source="html:series-extra") for u in urls[1:]]
        for e in extra:
            self.add_result(e)
        return rec

    def normalize(self, raw):
        raw = raw.strip()
        if re.match(r"^https?://", raw, re.I):
            return raw
        if raw.startswith("//"):
            return "https:" + raw
        path = raw.lstrip("/")
        if path.startswith("h5/m3u8/"):
            return self.base + "/" + path
        return self.base + "/h5/m3u8/" + path

    def add_result(self, rec):
        if not rec.get("url"):
            return
        rec["type"] = rec.get("type") or guess_ext(rec["url"]) or "m3u8"
        with self.lock:
            if rec["url"] not in self.results:
                self.results[rec["url"]] = rec

    def verify(self, items):
        def one(rec):
            r = self.fetcher.get(rec["url"], referer=rec.get("page") or self.base + "/")
            rec["verified"] = "%s %s" % (r.get("status"), (r.get("type") or "").split(";")[0])
            if not r["ok"]:
                rec["status"] = "dead"
            return rec
        with ThreadPoolExecutor(max_workers=self.args.workers) as ex:
            list(ex.map(one, items))

    # ---------------- 主流程 ----------------
    def run(self):
        print("[1/4] 发现入口（sitemap + 全类型页）...")
        s_dramas, s_series, s_pages = ([], [], [])
        if self.args.use_sitemap:
            s_dramas, s_series, s_pages = self.from_sitemap()
            print("      sitemap: 剧集 %d / 系列 %d / 入口页 %d"
                  % (len(s_dramas), len(s_series), len(s_pages)))
        b_dramas, b_series = ([], [])
        if self.args.use_browse:
            b_dramas, b_series = self.browse(s_pages)
            print("      浏览类型页: 剧集 %d / 系列 %d" % (len(b_dramas), len(b_series)))
        extra_series = self.series_from_lists() if self.args.use_series_list else []
        if extra_series:
            print("      系列列表翻页: %d" % len(extra_series))

        dramas = list(OrderedDict.fromkeys(s_dramas + b_dramas))
        series = list(OrderedDict.fromkeys(s_series + b_series + extra_series))
        if self.args.max_dramas:
            dramas = dramas[: self.args.max_dramas]
        if self.args.max_series:
            series = series[: self.args.max_series]
        if self.args.kinds and "drama" not in self.args.kinds:
            dramas = []
        if self.args.kinds and "series" not in self.args.kinds:
            series = []
        if self.args.section:
            key = self.args.section.lower()
            dramas = [s for s in dramas
                      if any(key in x for x in (self.sections.get(s) or {"sitemap"}))]
            series = [i for i in series
                      if any(key in x for x in (self.sections.get("s" + i) or {"sitemap"}))]
            print("      按类型过滤(%s): 剧集 %d / 系列 %d" % (self.args.section, len(dramas), len(series)))
        print("      合计: 剧集 %d 部 / 系列视频 %d 个" % (len(dramas), len(series)))
        self.stats["dramas"], self.stats["series"] = len(dramas), len(series)

        print("[2/4] 剧集分集 ...")
        drama_tasks = []
        if dramas:
            with ThreadPoolExecutor(max_workers=self.args.workers) as ex:
                futs = {ex.submit(self.chapters_of, s): s for s in dramas}
                for f in as_completed(futs):
                    slug = futs[f]
                    ids, meta = f.result()
                    for cid in ids:
                        drama_tasks.append((slug, cid, meta))
        self.stats["chapters"] = len(drama_tasks)
        print("      共 %d 集" % len(drama_tasks))

        print("[3/4] 解析地址（剧集 API + 系列页面）...")
        jobs = []
        with ThreadPoolExecutor(max_workers=self.args.workers) as ex:
            for slug, cid, meta in drama_tasks:
                jobs.append(ex.submit(self.resolve_chapter, slug, cid, meta))
            for sid in series:
                jobs.append(ex.submit(self.resolve_series, sid))
            done = 0
            for f in as_completed(jobs):
                rec = f.result()
                done += 1
                if not rec.get("url"):
                    self.stats["locked"] += 1
                self.add_result(rec)
                if done % 100 == 0:
                    print("      %d/%d，已获得 %d 条" % (done, len(jobs), len(self.results)))

        print("[4/4] 兜底直链扫描 ...")
        for u in [self.base + "/", self.base + "/rank/", self.base + "/series/"] + s_pages[:10]:
            r = self.fetcher.get(u)
            if not r["ok"]:
                continue
            direct, _, _, _ = scan_html_for_links(r["text"], self.base)
            for url in direct:
                self.add_result({"kind": "direct", "type": guess_ext(url), "url": url,
                                 "title": "", "drama": "", "episode": "", "chapterId": "",
                                 "seriesId": "", "slug": "", "section": "html-scan",
                                 "page": u, "status": "ok", "verified": "", "note": "",
                                 "source": "html:regex"})

        items = list(self.results.values())
        if self.args.kinds:
            items = [r for r in items if r["kind"] in self.args.kinds]
        if self.args.verify:
            print("      校验可达性 ...")
            self.verify(items)
        return items


def main():
    ap = argparse.ArgumentParser(description="扫描 huangguo.me 全类型视频下载地址")
    ap.add_argument("--base", default="https://huangguo.me")
    ap.add_argument("--kinds", default="", help="只扫描指定类型，逗号分隔：drama,series,direct")
    ap.add_argument("--section", default="", help="只扫某个类型入口，如 tag:dushi / category:rebang / series-cat:15648")
    ap.add_argument("--max-dramas", type=int, default=0, help="最多扫多少部剧(0=全部)")
    ap.add_argument("--max-series", type=int, default=0, help="最多扫多少个系列视频(0=全部)")
    ap.add_argument("--max-chapters", type=int, default=0, help="每部剧最多解析多少集(0=全部)")
    ap.add_argument("--series-pages", type=int, default=5, help="/series/all/{分类}/ 翻页上限")
    ap.add_argument("--max-type-pages", type=int, default=300, help="最多抓取多少个分类/标签/专题入口页(0=全部)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--delay", type=float, default=0.0, help="每请求间隔秒")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--cookie", default="", help="浏览器 Cookie（解锁付费内容）")
    ap.add_argument("--no-sitemap", action="store_true", help="不使用 sitemap")
    ap.add_argument("--no-browse", action="store_true", help="不爬取分类/标签/专题页")
    ap.add_argument("--no-series-list", action="store_true", help="不翻 /series/all/ 列表")
    ap.add_argument("--verify", action="store_true", help="校验地址是否可下载")
    ap.add_argument("--out", default="video_links.json")
    ap.add_argument("--csv", default="video_links.csv")
    args = ap.parse_args()
    args.use_sitemap = not args.no_sitemap
    args.use_browse = not args.no_browse
    args.use_series_list = not args.no_series_list
    args.kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]

    t0 = time.time()
    scanner = Scanner(args)
    items = scanner.run()

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    with open(args.csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in items:
            w.writerow({k: r.get(k, "") for k in FIELDS})

    ok = [r for r in items if r["url"] and r["status"] == "ok"]
    by_kind, by_type = {}, {}
    for r in ok:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    print("\n==== 完成（%.1fs）====" % (time.time() - t0))
    print("剧集 %d 部 / 分集 %d / 系列 %d / 入口页 %d"
          % (scanner.stats["dramas"], scanner.stats["chapters"],
             scanner.stats["series"], scanner.stats["pages"]))
    print("视频地址 %d（按来源 %s，按格式 %s）" % (len(ok), by_kind, by_type))
    print("未解锁/失败 %d，HTTP 请求 %d" % (scanner.stats["locked"], scanner.fetcher.count))
    print("输出: %s , %s" % (args.out, args.csv))
    for r in ok[:8]:
        print("  [%-6s][%-4s] %s | %s" % (r["kind"], r["type"], r["url"][:95], r["title"][:30]))


if __name__ == "__main__":
    sys.exit(main())
