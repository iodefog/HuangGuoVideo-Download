#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量下载 huangguo.me 的视频（HLS / m3u8，支持 AES-128 解密 + ffmpeg 封装成 mp4）。

输入：scan_videos.py 产出的 video_links.json（也可 --url 直接给一个 m3u8）
流程：m3u8（支持 master 选最高码率） -> 取密钥 -> 并发下载分片 -> AES-128-CBC 解密
      -> 顺序合并 -> ffmpeg -c copy 封装成 mp4

依赖：仅标准库 + 系统 openssl（解密）+ 可选 ffmpeg（封装；无 ffmpeg 时保留 .ts）
"""

import argparse
import csv
import json
import os
import random
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# 部分 CDN/代理在关闭 TLS 连接时不发 close_notify，导致 Python 报
# "SSL: UNEXPECTED_EOF_WHILE_READING"。开启该选项可容忍，避免误判失败。
_TLS_CTX = ssl.create_default_context()
if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
    _TLS_CTX.options |= ssl.OP_IGNORE_UNEXPECTED_EOF

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

PRINT_LOCK = threading.Lock()
BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def log(msg):
    with PRINT_LOCK:
        print(msg, flush=True)


def sanitize(name, maxlen=80):
    s = BAD_CHARS.sub("_", (name or "").strip()) or "untitled"
    s = re.sub(r"_+", "_", s).strip(" ._")
    return s[:maxlen]


def which(cmd):
    return shutil.which(cmd)


# ---------------- HTTP ----------------
PROXY = None


def http_get(url, referer=None, cookie=None, timeout=25, retries=6):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            req.add_header("Accept", "*/*")
            req.add_header("Referer", referer or "https://huangguo.me/")
            if cookie:
                req.add_header("Cookie", cookie)
            handlers = [urllib.request.HTTPSHandler(context=_TLS_CTX)]
            if PROXY:
                handlers.append(urllib.request.ProxyHandler(
                    {"http": PROXY, "https": PROXY}))
            opener = urllib.request.build_opener(*handlers)
            with opener.open(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (400, 401, 403, 404, 410):
                return None
            time.sleep(1.0 * (i + 1))
        except Exception as e:  # noqa: BLE001
            # 代理/VPN 对 huangguo.me 的 TLS 握手偶发重置（connection closed EOF），
            # 退避后重试通常能跨过抖动窗口
            last = str(e)
            if i < retries:
                time.sleep(min(2 ** i, 8) + random.uniform(0, 1.5))
    log("    [warn] %s -> %s" % (url[:80], last))
    return None


# ---------------- m3u8 解析 ----------------
class Playlist:
    def __init__(self):
        self.is_master = False
        self.variants = []        # [(bandwidth, url)]
        self.segments = []        # [url]
        self.key_method = None
        self.key_uri = None
        self.key_iv = None        # hex 或 None
        self.media_sequence = 0


def parse_m3u8(text, base_url):
    pl = Playlist()
    lines = [l.strip() for l in text.splitlines()]
    pending_key = False
    for i, line in enumerate(lines):
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            pl.is_master = True
            bw = 0
            m = re.search(r"BANDWIDTH=(\d+)", line)
            if m:
                bw = int(m.group(1))
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt and not nxt.startswith("#"):
                pl.variants.append((bw, urllib.parse.urljoin(base_url, nxt)))
        elif line.startswith("#EXT-X-KEY"):
            m = re.search(r'METHOD=([^,]+)', line)
            if m and m.group(1).strip() != "NONE":
                pl.key_method = m.group(1).strip()
                mu = re.search(r'URI="([^"]+)"', line)
                if mu:
                    pl.key_uri = urllib.parse.urljoin(base_url, mu.group(1))
                iv = re.search(r'IV=0x([0-9A-Fa-f]+)', line)
                pl.key_iv = iv.group(1) if iv else None
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE"):
            m = re.search(r"(\d+)", line)
            if m:
                pl.media_sequence = int(m.group(1))
        elif not line.startswith("#"):
            pl.segments.append(urllib.parse.urljoin(base_url, line))
    return pl


def load_playlist(url, referer, cookie, timeout, retries):
    raw = http_get(url, referer, cookie, timeout, retries)
    if raw is None:
        return None, None, None
    text = raw.decode("utf-8", "ignore")
    pl = parse_m3u8(text, url)
    if pl.is_master and pl.variants:
        pl.variants.sort(key=lambda x: x[0], reverse=True)
        best = pl.variants[0][1]
        raw2 = http_get(best, referer, cookie, timeout, retries)
        if raw2 is None:
            return None, None, None
        text = raw2.decode("utf-8", "ignore")
        return parse_m3u8(text, best), best, text
    return pl, url, text


def is_stale_playlist(pl, text):
    """ticket 过期后，/h5/m3u8/ 会返回一份占位 m3u8（无 id/ticket 的 /enkey、分片极少）"""
    if pl.key_uri and "id=" not in pl.key_uri:
        return True
    if text and len(text) < 1024 and len(pl.segments) <= 3 and "#EXT-X-ENDLIST" in text:
        return True
    return False


# ---------------- 单个视频下载 ----------------
def download_hls(m3u8_url, dest, referer, cookie, workers, retries, timeout,
                 remux=True, engine="builtin"):
    """返回 dict(ok, bytes, segments, failed, note)"""
    if engine == "ffmpeg":
        return _download_by_ffmpeg(m3u8_url, dest, referer, cookie, timeout)

    pl, final_url, text = load_playlist(m3u8_url, referer, cookie, timeout, retries)
    if pl is None or not pl.segments:
        return {"ok": False, "bytes": 0, "segments": 0, "failed": 0, "reason": "no_playlist",
                "note": "m3u8 无法获取或为空（链接已失效，请 --refresh 重新生成）"}
    if is_stale_playlist(pl, text or ""):
        return {"ok": False, "bytes": 0, "segments": len(pl.segments), "failed": 0,
                "reason": "stale_m3u8",
                "note": "m3u8 已过期（服务端返回占位内容），需刷新链接"}

    note_cdn = "分片被 CDN 拒绝（403 防盗链；与链接新旧无关，多为 IP/地域或登录态限制）"

    key = None
    if pl.key_method == "AES-128" and pl.key_uri:
        key = http_get(pl.key_uri, referer, cookie, timeout, retries)
        if key is None or len(key) < 16:
            return {"ok": False, "bytes": 0, "segments": len(pl.segments), "failed": 0,
                    "reason": "no_key", "note": "密钥获取失败（多半也是链接失效）"}
        key = key[:16]
    elif pl.key_method:
        return {"ok": False, "bytes": 0, "segments": len(pl.segments), "failed": 0,
                "reason": "bad_crypto", "note": "不支持的加密方式: %s" % pl.key_method}

    tmpdir = dest + ".parts"
    os.makedirs(tmpdir, exist_ok=True)

    def fetch_seg(idx_url):
        idx, url = idx_url
        path = os.path.join(tmpdir, "s_%06d.bin" % idx)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return idx, True
        data = http_get(url, referer, cookie, timeout, retries)
        if not data:
            return idx, False
        if key:
            iv = bytes.fromhex(pl.key_iv) if pl.key_iv else \
                (pl.media_sequence + idx).to_bytes(16, "big")
            enc = path + ".enc"
            with open(enc, "wb") as f:
                f.write(data)
            rc = subprocess.call(["openssl", "enc", "-d", "-aes-128-cbc",
                                  "-K", key.hex(), "-iv", iv.hex(),
                                  "-in", enc, "-out", path])
            os.remove(enc)
            if rc != 0:
                return idx, False
        else:
            with open(path, "wb") as f:
                f.write(data)
        return idx, True

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(fetch_seg, list(enumerate(pl.segments))))

    ok_idx = [i for i, ok in results if ok]
    failed = len(pl.segments) - len(ok_idx)
    if not ok_idx:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return {"ok": False, "bytes": 0, "segments": len(pl.segments), "failed": failed,
                "reason": "cdn_blocked", "note": note_cdn}

    merged = os.path.join(tmpdir, "merged.ts")
    with open(merged, "wb") as out:
        for idx in sorted(ok_idx):
            with open(os.path.join(tmpdir, "s_%06d.bin" % idx), "rb") as f:
                shutil.copyfileobj(f, out)
    total = os.path.getsize(merged)

    final = dest if not remux else os.path.splitext(dest)[0] + ".mp4"
    if remux and which("ffmpeg"):
        tmpout = final + ".tmp.mp4"
        rc = subprocess.call(["ffmpeg", "-y", "-loglevel", "error", "-i", merged,
                              "-c", "copy", "-bsf:a", "aac_adtstoasc", tmpout])
        if rc != 0 or not os.path.exists(tmpout):
            rc = subprocess.call(["ffmpeg", "-y", "-loglevel", "error", "-i", merged,
                                  "-c", "copy", tmpout])
        if rc == 0 and os.path.exists(tmpout):
            os.replace(tmpout, final)
        else:
            if os.path.exists(tmpout):
                os.remove(tmpout)
            shutil.move(merged, os.path.splitext(dest)[0] + ".ts")
            final = os.path.splitext(dest)[0] + ".ts"
    else:
        shutil.move(merged, final if final.endswith(".ts") else os.path.splitext(dest)[0] + ".ts")
        final = final if final.endswith(".ts") else os.path.splitext(dest)[0] + ".ts"

    shutil.rmtree(tmpdir, ignore_errors=True)
    return {"ok": True, "bytes": os.path.getsize(final) if os.path.exists(final) else total,
            "segments": len(ok_idx), "failed": failed, "path": final,
            "note": ("缺 %d 个分片" % failed) if failed else ""}


def _download_by_ffmpeg(m3u8_url, dest, referer, cookie, timeout):
    """直接交给 ffmpeg（它自己处理 key/分片；对防盗链更敏感）"""
    out = os.path.splitext(dest)[0] + ".mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-user_agent", UA]
    headers = "Referer: %s\r\n" % (referer or "https://huangguo.me/")
    if cookie:
        headers += "Cookie: %s\r\n" % cookie
    cmd += ["-headers", headers, "-i", m3u8_url, "-c", "copy",
            "-bsf:a", "aac_adtstoasc", out]
    rc = subprocess.call(cmd, timeout=timeout * 60)
    if rc != 0 or not os.path.exists(out):
        return {"ok": False, "bytes": 0, "segments": 0, "failed": 0,
                "note": "ffmpeg 直接下载失败（可换内置引擎 --engine builtin）"}
    return {"ok": True, "bytes": os.path.getsize(out), "segments": -1, "failed": 0,
            "path": out, "note": ""}


# ---------------- 链接刷新 ----------------
def refresh_url(rec, base, cookie):
    """m3u8 带时效性，必要时重新取一个新鲜链接"""
    if rec.get("kind") == "drama" and rec.get("chapterId") and rec.get("slug"):
        api = "%s/api/playlet/chapter/detail?chapterId=%s&playletSlug=%s" % (
            base, rec["chapterId"], rec["slug"])
        data = http_get(api, rec.get("page") or base + "/", cookie)
        if data:
            try:
                d = json.loads(data.decode("utf-8", "ignore")).get("data") or {}
                vu = (d.get("videoUrl") or "").strip()
                if vu:
                    return vu if vu.startswith("http") else base + "/h5/m3u8/" + vu.lstrip("/")
            except Exception:
                pass
    if rec.get("seriesId"):
        html = http_get("%s/series/watch/%s/" % (base, rec["seriesId"]),
                         base + "/series/watch/%s/" % rec["seriesId"], cookie)
        if html:
            txt = html.decode("utf-8", "ignore").replace("\\u0026", "&")
            m = re.search(r'"url":"([^"]+\.m3u8[^"]*)"', txt)
            if m:
                v = m.group(1)
                return v if v.startswith("http") else base + "/h5/m3u8/" + v.lstrip("/")
    return None


# ---------------- 任务 ----------------
def build_dest(rec, out_dir):
    title = sanitize(rec.get("drama") or rec.get("title") or "video")
    if rec.get("kind") == "drama":
        sub = os.path.join(out_dir, "drama", title)
        ep = rec.get("episode")
        name = ("EP%s" % ep) if ep else ("id%s" % rec.get("chapterId", "x"))
    else:
        sub = os.path.join(out_dir, "series")
        name = "%s_%s" % (title, rec.get("seriesId") or rec.get("chapterId") or "")
    os.makedirs(sub, exist_ok=True)
    return os.path.join(sub, sanitize(name, 60) + ".ts")


def out_paths(rec, root):
    """返回某条目在 root 下的成品可能路径（不创建目录，仅拼路径）"""
    title = sanitize(rec.get("drama") or rec.get("title") or "video")
    if rec.get("kind") == "drama":
        sub = os.path.join(root, "drama", title)
        ep = rec.get("episode")
        name = ("EP%s" % ep) if ep else ("id%s" % rec.get("chapterId", "x"))
    else:
        sub = os.path.join(root, "series")
        name = "%s_%s" % (title, rec.get("seriesId") or rec.get("chapterId") or "")
    base = os.path.join(sub, sanitize(name, 60))
    return [base + ".mp4", base + ".ts"]


def rec_key(rec):
    """稳定标识，用于进度记录/续跑（跨次运行一致）"""
    if rec.get("kind") == "series":
        return "s:%s" % (rec.get("seriesId") or rec.get("chapterId") or rec.get("url"))
    return "d:%s#%s" % (rec.get("drama") or rec.get("title"), rec.get("episode", ""))


def is_done(rec, roots):
    """成品是否已存在于 out_dir 或任意 done_dir（用于跳过已下载）"""
    for root in roots:
        for p in out_paths(rec, root):
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return True
    return False


def load_progress(out_dir):
    p = os.path.join(out_dir, "_progress.json")
    if os.path.exists(p):
        try:
            return set(json.load(open(p, encoding="utf-8")).get("done", []))
        except Exception:
            return set()
    return set()


def save_progress(out_dir, done_keys):
    p = os.path.join(out_dir, "_progress.json")
    # 合并已有进度，避免（如 --limit 验证步骤）覆盖掉之前记录的已完成项
    existing = set()
    if os.path.exists(p):
        try:
            existing = set(json.load(open(p, encoding="utf-8")).get("done", []))
        except Exception:
            pass
    merged = existing | set(done_keys)
    json.dump({"updated": time.strftime("%Y-%m-%d %H:%M:%S"),
               "done": sorted(merged)}, open(p, "w", encoding="utf-8"),
              ensure_ascii=False, indent=0)
    return len(merged)


def run_task(rec, args):
    base = args.base.rstrip("/")
    url = rec.get("url") or ""
    if args.refresh:
        fresh = refresh_url(rec, base, args.cookie)
        if fresh:
            url = fresh
    referer = args.referer or rec.get("page") or (base + "/")
    dest = build_dest(rec, args.out_dir)
    mp4 = os.path.splitext(dest)[0] + (".mp4" if args.remux else ".ts")

    roots = [args.out_dir] + list(args.done_dir)
    if is_done(rec, roots) and not args.force:
        for p in out_paths(rec, args.out_dir):
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return dict(rec, ok=True, skipped=True, bytes=os.path.getsize(p),
                            segments=0, failed=0, path=p, note="已存在")
        # 命中 done_dir（外部磁盘）中的已完成文件
        for root in args.done_dir:
            for p in out_paths(rec, root):
                if os.path.exists(p) and os.path.getsize(p) > 0:
                    return dict(rec, ok=True, skipped=True, bytes=os.path.getsize(p),
                                segments=0, failed=0, path=p, note="已存在(外部磁盘)")

    t0 = time.time()
    res = download_hls(url, dest, referer, args.cookie, args.workers,
                       args.retries, args.timeout, remux=args.remux, engine=args.engine)
    # 链接时效很短：检测到占位/失效 m3u8 时自动换一个新鲜链接重试一次
    if (not res.get("ok") and args.auto_refresh
            and res.get("reason") in ("stale_m3u8", "no_playlist", "no_key")):
        fresh = refresh_url(rec, base, args.cookie)
        if fresh and fresh != url:
            url = fresh
            res = download_hls(url, dest, referer, args.cookie, args.workers,
                               args.retries, args.timeout, remux=args.remux,
                               engine=args.engine)
            res["refreshed"] = True
    res.setdefault("refreshed", False)
    if res.get("refreshed"):
        res["note"] = "[已刷新链接] " + (res.get("note") or "")
    res["seconds"] = round(time.time() - t0, 1)
    res["skipped"] = False
    out = dict(rec)
    out.update(res)
    tag = "OK  " if res["ok"] else "FAIL"
    log("  [%s] %-28s %5.1fs %6.1fMB %s%s" % (
        tag, (rec.get("title") or rec.get("drama") or "")[:28], res["seconds"],
        (res.get("bytes", 0) or 0) / 1048576.0, res.get("note", ""),
        "" if res["ok"] else " <- " + url[:60]))
    return out


def main():
    ap = argparse.ArgumentParser(description="批量下载 huangguo.me 视频（HLS/AES-128）")
    ap.add_argument("--input", default="video_links.json", help="scan_videos.py 产出的 JSON")
    ap.add_argument("--out-dir", default="downloads")
    ap.add_argument("--base", default="https://huangguo.me")
    ap.add_argument("--url", default="", help="直接下载单个 m3u8（配合 --name）")
    ap.add_argument("--name", default="single")
    ap.add_argument("--kinds", default="", help="只下某类：drama,series,direct")
    ap.add_argument("--section", default="", help="只下某个类型入口，如 tag:dushi")
    ap.add_argument("--title-match", default="", help="标题正则过滤")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=2, help="同时下载几个视频")
    ap.add_argument("--workers", type=int, default=8, help="单个视频的分片并发数")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--cookie", default="")
    ap.add_argument("--proxy", default="", help="HTTP(S) 代理，如 http://127.0.0.1:7890（分片被 CDN 封 IP 时用它换出口）")
    ap.add_argument("--referer", default="", help="覆盖分片请求的 Referer（防盗链排查用）")
    ap.add_argument("--engine", default="builtin", choices=["builtin", "ffmpeg"])
    ap.add_argument("--no-remux", action="store_true", help="不转 mp4，保留 .ts")
    ap.add_argument("--refresh", action="store_true", help="下载前总是重新取新鲜 m3u8（签名有时效）")
    ap.add_argument("--no-auto-refresh", action="store_true",
                    help="关闭「m3u8 失效时自动换新链接重试」（默认开启）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在文件")
    ap.add_argument("--done-dir", action="append", default=[],
                    help="已下载存档目录（可多次指定，如移动硬盘上的 downloads）。其中的成品视为已完成并跳过")
    ap.add_argument("--resume", action="store_true",
                    help="根据 downloads/_progress.json 续跑：跳过此前已完成的条目（不从头开始）")
    ap.add_argument("--snapshot", action="store_true",
                    help="仅扫描并写入当前进度快照（downloads/_progress.json），不下载")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    args.remux = not args.no_remux
    args.auto_refresh = not args.no_auto_refresh
    if args.proxy:
        global PROXY
        PROXY = args.proxy
        log("使用代理: %s" % PROXY)

    roots = [args.out_dir] + list(args.done_dir)

    if args.url:
        items = [{"kind": "direct", "url": args.url, "title": args.name,
                   "drama": args.name, "episode": "", "chapterId": "", "seriesId": "",
                   "slug": "", "section": "", "page": args.base}]
    else:
        if not os.path.exists(args.input):
            sys.exit("找不到输入文件 %s（先运行 scan_videos.py）" % args.input)
        with open(args.input, encoding="utf-8") as f:
            items = json.load(f)

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    if kinds:
        items = [r for r in items if r.get("kind") in kinds]
    if args.section:
        items = [r for r in items if args.section.lower() in (r.get("section") or "").lower()]
    if args.title_match:
        rx = re.compile(args.title_match)
        items = [r for r in items if rx.search((r.get("title") or "") + (r.get("drama") or ""))]
    items = [r for r in items if r.get("url")]
    if args.limit:
        items = items[: args.limit]

    if not items:
        sys.exit("没有符合条件的记录")

    done_keys = load_progress(args.out_dir) if args.resume else set()

    # 仅生成进度快照（扫描已完成条目并写入 _progress.json），不下载
    if args.snapshot:
        snap = set()
        for r in items:
            if is_done(r, roots):
                snap.add(rec_key(r))
        os.makedirs(args.out_dir, exist_ok=True)
        save_progress(args.out_dir, snap)
        done_files, done_bytes = 0, 0
        for root in roots:
            for rp, _, fs in os.walk(root):
                for fn in fs:
                    if fn.endswith(".mp4"):
                        done_files += 1
                        done_bytes += os.path.getsize(os.path.join(rp, fn))
        print("进度快照已写入: %s" % os.path.join(args.out_dir, "_progress.json"))
        print("映射到清单的已完成条目: %d / %d (%.1f%%)" %
              (len(snap), len(items), 100 * len(snap) / max(1, len(items))))
        print("磁盘成品 mp4: %d 个, 共 %.1f MB" % (done_files, done_bytes / 1048576.0))
        return

    # 续跑：跳过此前已完成条目（避免从头开始）
    if args.resume and done_keys:
        before = len(items)
        items = [r for r in items if rec_key(r) not in done_keys]
        log("续跑模式：已跳过 %d 个已完成条目，剩余 %d 个待处理" %
            (before - len(items), len(items)))

    log("待下载 %d 个，输出目录 %s，引擎 %s" % (len(items), args.out_dir, args.engine))
    if args.dry_run:
        for r in items[:20]:
            print("  %-8s %-30s %s" % (r.get("kind"), (r.get("title") or "")[:30], r["url"][:80]))
        return

    os.makedirs(args.out_dir, exist_ok=True)
    if not which("openssl"):
        log("[warn] 未找到 openssl，遇到 AES-128 加密的分片将无法解密")

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = [ex.submit(run_task, r, args) for r in items]
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:  # noqa: BLE001
                log("  [FAIL] 任务异常: %s" % e)

    ok = [r for r in results if r.get("ok")]
    skipped = [r for r in ok if r.get("skipped")]
    failed = [r for r in results if not r.get("ok")]
    report = os.path.join(args.out_dir, "_report.csv")
    with open(report, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["status", "reason", "kind", "title", "drama", "episode", "section",
                    "bytes", "segments", "failed_segments", "seconds", "path", "note", "url"])
        for r in results:
            w.writerow(["ok" if r.get("ok") else "fail", r.get("reason", ""), r.get("kind"),
                        r.get("title"), r.get("drama"), r.get("episode"), r.get("section"),
                        r.get("bytes"), r.get("segments"), r.get("failed"),
                        r.get("seconds"), r.get("path", ""), r.get("note", ""), r.get("url")])

    total_mb = sum((r.get("bytes") or 0) for r in ok) / 1048576.0
    # 记录进度（供下次 --resume 续跑，无需从头开始）
    all_done = done_keys | {rec_key(r) for r in ok}
    saved = save_progress(args.out_dir, all_done)
    log("\n==== 完成（%.1fs）====" % (time.time() - t0))
    log("成功 %d（跳过已存在 %d）/ 失败 %d，共 %.1fMB" % (len(ok), len(skipped), len(failed), total_mb))
    log("进度已记录: %s（累计已完成 %d 条，下次可用 --resume 续跑）" %
        (os.path.join(args.out_dir, "_progress.json"), saved))
    log("报告: %s" % report)
    if failed:
        reasons = {}
        for r in failed:
            reasons[r.get("reason", "unknown")] = reasons.get(r.get("reason", "unknown"), 0) + 1
        log("失败原因分布: %s" % reasons)
        for r in failed[:5]:
            log("  %s | %s" % ((r.get("title") or "")[:30], r.get("note", "")))
        if reasons.get("cdn_blocked"):
            log("提示: cdn_blocked 与链接新旧无关——m3u8/密钥都拿到了，是分片域名 webats.rlkfus.cn "
                "返回 403 Invalid Request（防盗链）。可尝试换网络出口、带登录 Cookie，或 --referer 调整来源。")


if __name__ == "__main__":
    main()
