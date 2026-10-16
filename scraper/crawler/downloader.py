# crawler/downloader.py
"""多執行緒檔案下載器（同網域冷卻、多策略重試、MD5 去重、完整性校驗）。

相對 v4 的修正 / 強化：
- 修正 YAHOO_REFERER 被寫成 Markdown 連結導致 Referer 標頭錯誤。
- 完整性：Content-Length 與實際寫入不符 → 判定為截斷並重試（對應「下載有時是空檔案」）。
- 本地依 md5 前 2 位 / 第 3-4 位分層存放（可於設定關閉）。
- Wayback 歷史存檔備援真正生效（v4 有勾選項但未實作）。
- 429 / 403 視為被限流：該網站冷卻拉長；Content-Disposition 檔名解析。
- MD5 去重登記與 os.replace 放在同一把鎖內，避免並行時重複入庫。
"""
import os
import re
import ssl
import time
import uuid
import random
import hashlib
import threading
import urllib.parse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.constants import (UA_LIST, YAHOO_REFERER, MIN_FILE_BYTES, NET_KINDS, STRATEGIES, WAYBACK_PREFIX)
from core.utils import domain_of, safe_name, shard_path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_BLOCK_KINDS = ("http429", "http403")


class DLError(Exception):
    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind = kind
        self.msg = msg


class LegacyTLSAdapter(HTTPAdapter):
    """相容老舊伺服器：關閉憑證驗證、降低 SECLEVEL、允許 legacy renegotiation。"""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        except ssl.SSLError:
            pass
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


class HostState:
    def __init__(self):
        self.fail = 0
        self.dead = False


class Downloader:
    def __init__(self, db, log, stop_event, on_progress=None, on_success=None):
        self.db = db
        self.log = log
        self.stop = stop_event
        self.on_progress = on_progress or (lambda done, total: None)
        self.on_success = on_success or (lambda url: None)
        self.hosts = {}
        self._lock = threading.Lock()
        self._md5_lock = threading.Lock()

    def reset(self):
        with self._lock:
            self.hosts.clear()

    # ------------------------------------------------------------------ 網站健康度
    def _host(self, host):
        with self._lock:
            if host not in self.hosts:
                self.hosts[host] = HostState()
            return self.hosts[host]

    def _host_failed(self, hs):
        with self._lock:
            hs.fail += 1
            if hs.fail >= 3:
                hs.dead = True

    # ------------------------------------------------------------------ 輔助
    @staticmethod
    def url_variants(link):
        """正規化 path/query 的百分比編碼，並同時嘗試 https / http。"""
        p = urllib.parse.urlsplit(link)
        path = urllib.parse.quote(p.path, safe="/%:@!$&'()*+,;=-._~")
        query = urllib.parse.quote(p.query, safe="=&%+:/?@!$'()*,;-._~")
        alt = "http" if p.scheme == "https" else "https"
        build = lambda s: urllib.parse.urlunsplit((s, p.netloc, path, query, ""))  # noqa: E731
        return list(dict.fromkeys([build(p.scheme), build(alt)]))

    @staticmethod
    def verify_magic(chunk, ext):
        """用檔頭判斷是否真的是目標格式（擋掉伺服器回傳 HTML 錯誤頁的情況）。"""
        if not chunk:
            return False
        ext = ext.lower()
        if ext == "pdf":
            return b"%PDF" in chunk[:1024]
        if ext in ("docx", "pptx", "xlsx"):
            return chunk.startswith(b"PK\x03\x04") or chunk.startswith(b"PK\x05\x06")
        if ext in ("doc", "ppt", "xls"):
            return chunk.startswith(b"\xd0\xcf\x11\xe0")
        head = chunk[:256].lower()
        return not (b"<!doctype html" in head or b"<html" in head)

    @staticmethod
    def _filename_from_cd(cd):
        if not cd:
            return None
        m = re.search(r"filename\*\s*=\s*[\w-]+''([^;]+)", cd, re.I)
        if m:
            return urllib.parse.unquote(m.group(1)).strip().strip('"')
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
        if m:
            name = m.group(1).strip()
            try:  # 常見的 latin-1 誤解碼 UTF-8
                name = name.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            return name
        return None

    def pick_filename(self, resp, ext):
        name = self._filename_from_cd(resp.headers.get("Content-Disposition"))
        if not name:
            name = urllib.parse.unquote(os.path.basename(urllib.parse.urlsplit(resp.url).path))
        name = safe_name(name, 120) if name else f"file_{uuid.uuid4().hex[:8]}"
        if not re.search(r"\.(pdf|docx?|pptx?|xlsx?)$", name, re.I):
            name += "." + ext
        return name

    @staticmethod
    def _new_session(strat, opt):
        s = requests.Session()
        proxy = opt.get("proxy")
        s.proxies = {"http": proxy, "https": proxy} if proxy else {"http": None, "https": None}
        s.trust_env = False
        s.headers.update({"Accept": "*/*", "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
        if strat["close"]:
            s.headers["Connection"] = "close"
        retry = Retry(total=1, connect=1, read=0, backoff_factor=1,
                      status_forcelist=(502, 503, 504), raise_on_status=False)
        adapter = (LegacyTLSAdapter if strat["legacy"] else HTTPAdapter)(max_retries=retry)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        return s

    @staticmethod
    def classify(e):
        if isinstance(e, DLError):
            return e.kind, e.msg
        if isinstance(e, InterruptedError):
            return "stopped", "已停止"
        if isinstance(e, requests.exceptions.SSLError):
            return "ssl", str(e)[:100]
        if isinstance(e, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
            return "timeout", "逾時"
        if isinstance(e, requests.exceptions.ChunkedEncodingError):
            return "truncated", "傳輸中斷 (內容不完整)"
        if isinstance(e, requests.exceptions.HTTPError):
            code = e.response.status_code if e.response is not None else 0
            return ("gone" if code in (404, 410) else f"http{code}"), f"HTTP {code}"
        return "conn", str(e)[:80]

    # ------------------------------------------------------------------ 單次嘗試
    def _try_once(self, url, key, strat, keyword, ext, save_dir, opt, read_to):
        p = urllib.parse.urlsplit(url)
        headers = {"User-Agent": UA_LIST[strat["ua"]]}
        if strat["referer"] == "origin":
            headers["Referer"] = f"{p.scheme}://{p.netloc}/"
        elif strat["referer"] == "yahoo":
            headers["Referer"] = YAHOO_REFERER
        verify = bool(opt.get("verify")) and not strat["legacy"]

        session = self._new_session(strat, opt)
        tmp_path = None
        t0 = time.time()
        try:
            with session.get(url, headers=headers, stream=True, timeout=(10, read_to),
                             verify=verify, allow_redirects=True) as r:
                r.raise_for_status()
                it = r.iter_content(chunk_size=8192)
                first = next(it, b"")
                if not first:
                    raise DLError("empty", "空內容")
                if not self.verify_magic(first, ext):
                    raise DLError("invalid_format", f"非 {ext.upper()} 格式")

                os.makedirs(save_dir, exist_ok=True)
                tmp_path = os.path.join(save_dir, f".tmp_{uuid.uuid4().hex}.part")
                md5, written = hashlib.md5(), 0
                with open(tmp_path, "wb") as f:
                    f.write(first)
                    md5.update(first)
                    written += len(first)
                    for chunk in it:
                        if self.stop.is_set():
                            raise InterruptedError()
                        if chunk:
                            f.write(chunk)
                            md5.update(chunk)
                            written += len(chunk)

                if written < MIN_FILE_BYTES:
                    raise DLError("empty", f"過小 ({written} B)")

                # 完整性：未壓縮傳輸時，實際大小必須等於 Content-Length
                cl = r.headers.get("Content-Length")
                if cl and cl.isdigit() and not r.headers.get("Content-Encoding") and int(cl) != written:
                    raise DLError("truncated", f"大小不符 (預期 {cl} B，實得 {written} B)")

                digest = md5.hexdigest()
                final_path = shard_path(save_dir, digest, ext, shard=opt.get("shard_storage", True))
                orig_name = self.pick_filename(r, ext)
                with self._md5_lock:  # 登記 + 搬檔不可被其他執行緒穿插，否則同 MD5 會重複入庫
                    existing = self.db.claim_ok(key, digest, final_path, written, keyword, ext,
                                                time.time() - t0, orig_name)
                    if existing:
                        return "dup", f"MD5 重複 → {os.path.basename(existing)}"
                    os.replace(tmp_path, final_path)
                return "ok", f"已存入 {final_path} [{written / 1024:.0f} KB]"
        finally:
            session.close()
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ------------------------------------------------------------------ 單一連結（多網址變體 × 多策略）
    def download_one(self, task, save_dir, opt):
        link, keyword = task["url"], task.get("keyword") or ""
        ext = (task.get("file_ext") or opt["file_ext"]).lower()
        host = domain_of(link)
        hs = self._host(host)

        status = "fail"
        best_kind, best_detail = "unknown", "無嘗試"   # 保留「最有資訊量」的錯誤，而不是最後一個
        last_kind, detail = "", ""

        def keep_best(kind, msg):
            nonlocal best_kind, best_detail
            # HTTP 層級的明確錯誤（404/403/截斷/空內容…）優先於網路層錯誤（連線/DNS/逾時/SSL），
            # 否則 https→http 備援產生的連線錯誤會蓋掉真正原因，並讓網站被誤判為「死站」。
            if best_kind == "unknown" or (best_kind in NET_KINDS and kind not in NET_KINDS):
                best_kind, best_detail = kind, msg

        for url in self.url_variants(link):
            for strat in STRATEGIES:
                if self.stop.is_set():
                    return "fail", "已停止", "stopped"
                try:
                    status, detail = self._try_once(url, link, strat, keyword, ext, save_dir, opt, 45)
                    last_kind = ""
                    break
                except Exception as e:  # noqa: BLE001
                    last_kind, detail = self.classify(e)
                    if last_kind == "stopped":
                        return "fail", detail, "stopped"
                    if last_kind == "invalid_format":
                        status = "skip"
                        break
                    keep_best(last_kind, detail)
                    if last_kind in _BLOCK_KINDS:  # 已被限流/拒絕：換策略前稍等，避免更激進
                        time.sleep(random.uniform(1, 3))
            if status in ("ok", "dup", "skip"):
                break
        if status == "fail":
            last_kind, detail = best_kind, best_detail

        # 備援：Internet Archive 歷史存檔（僅在其他方式都失敗時）
        if status == "fail" and opt.get("wayback") and not self.stop.is_set():
            try:
                status, detail = self._try_once(WAYBACK_PREFIX + link, link, STRATEGIES[0],
                                                keyword, ext, save_dir, opt, 60)
                detail = "(Wayback) " + detail
                last_kind = ""
            except Exception as e:  # noqa: BLE001
                k, d = self.classify(e)
                if k == "stopped":
                    return "fail", d, "stopped"
                detail = f"{detail} | Wayback: {d}"

        if status in ("ok", "dup"):
            with self._lock:
                hs.fail = 0
        elif status == "fail" and last_kind in NET_KINDS:
            self._host_failed(hs)

        if status == "fail":
            self.db.record_fail(link, last_kind, detail, opt["max_retries"], opt["retry_interval"])
        elif status == "skip":
            self.db.record_skip(link, detail)
        if status == "ok":
            self.on_success(link)
        return status, detail, last_kind

    # ------------------------------------------------------------------ 批次排程
    @staticmethod
    def _cooldown(kind, opt):
        if kind in _BLOCK_KINDS:
            return random.uniform(30, 90)          # 被限流：該網站拉長冷卻
        if kind in NET_KINDS:
            return random.uniform(2, 6)
        return random.uniform(opt["dom_min"], opt["dom_max"]) if opt["dom_max"] > 0 else 0

    def download_all(self, tasks, save_dir, opt):
        """同網域同時只下載一個，並依網域冷卻；不同網域並行。回傳 Counter 統計。"""
        total = len(tasks)
        by_dom = {}
        for t in tasks:
            by_dom.setdefault(domain_of(t["url"]), deque()).append(t)
        workers = opt["workers"]
        self.log(f"\n🚀 開始下載 {total} 個檔案至 {save_dir}")
        self.on_progress(0, total)

        stats, done = Counter(), 0
        next_ok, busy, inflight = {}, set(), {}
        icons = {"ok": "📥", "skip": "⏭️", "dup": "♻️", "fail": "❌"}
        ex = ThreadPoolExecutor(max_workers=workers)
        try:
            while (by_dom or inflight) and not self.stop.is_set():
                for fut in [f for f in inflight if f.done()]:
                    task, dom = inflight.pop(fut)
                    busy.discard(dom)
                    try:
                        status, detail, kind = fut.result()
                    except Exception as e:  # noqa: BLE001
                        status, detail, kind = "fail", str(e)[:80], "error"
                    if kind == "stopped":
                        continue
                    stats[status] += 1
                    done += 1
                    next_ok[dom] = time.time() + self._cooldown(kind, opt)
                    self.log(f"{icons.get(status, '❓')} [{done}/{total}] {task['url'][:80]}\n      {detail}")
                    self.on_progress(done, total)

                now = time.time()
                for dom in list(by_dom.keys()):
                    if len(inflight) >= workers:
                        break
                    if self._host(dom).dead:
                        n = len(by_dom.pop(dom))
                        done += n
                        stats["skipped_dead_host"] += n
                        self.log(f"⛔ {dom} 連續連線失敗，本輪略過其餘 {n} 個連結（保持待下載，下次再試）")
                        self.on_progress(done, total)
                        continue
                    if dom in busy or next_ok.get(dom, 0) > now:
                        continue
                    task = by_dom[dom].popleft()
                    if not by_dom[dom]:
                        del by_dom[dom]
                    inflight[ex.submit(self.download_one, task, save_dir, opt)] = (task, dom)
                    busy.add(dom)
                time.sleep(0.3)
        finally:
            ex.shutdown(wait=True)
        self.log(f"📊 下載結束：成功 {stats['ok']} | 重複 {stats['dup']} | 略過 {stats['skip']} | 失敗 {stats['fail']}")
        return stats
