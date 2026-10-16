# processing/proc_manager.py
"""後處理佇列：下載一完成就丟進佇列，由多個工作執行緒執行 ②轉圖 ③AI ④R2，狀態回寫資料庫。
（取代 v4 的 ProcMixin；不再依賴 GUI 變數，可被 GUI 與 CLI 共用。）
"""
import os
import queue
import threading
import time
from collections import Counter

from core.utils import fmt_dur
from processing.extractor import UnsupportedFormat
from processing.pipeline import DocPipeline


class ProcManager:
    def __init__(self, db, log, stop_event, cfg, enable_ai=True, enable_r2=True,
                 enable_publish=False):
        self.db = db
        self.log = log
        self.stop = stop_event
        self.cfg = cfg
        self.enable_ai = enable_ai
        self.enable_r2 = enable_r2
        self.enable_publish = enable_publish
        self.q = queue.Queue()
        self.threads = []
        self.counter = Counter()
        self._clock = threading.Lock()
        self._submitted = set()

    # ------------------------------------------------------------------ 生命週期
    def begin(self, workers):
        for _ in range(max(1, workers)):
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()
            self.threads.append(t)
        self.log(f"🧾 後處理管線已啟動 ({workers} 執行緒) | 步驟2(WebP+文字): 開啟 | "
                 f"步驟3(阿里雲): {'開啟' if self.enable_ai else '關閉'} | "
                 f"步驟4(Cloudflare R2): {'開啟' if self.enable_r2 else '關閉'} | "
                 f"步驟5(帝國CMS發布): {'開啟' if self.enable_publish else '關閉'}")

    @property
    def active(self):
        return bool(self.threads)

    def submit(self, url):
        if not self.threads:
            return
        with self._clock:
            if url in self._submitted:
                return
            self._submitted.add(url)
        self.q.put(url)

    def end(self):
        if not self.threads:
            return
        left = self.q.qsize()
        if left and not self.stop.is_set():
            self.log(f"⏳ 等待後處理佇列完成 (剩餘 {left} 個文檔)...")
        for _ in self.threads:
            self.q.put(None)
        for t in self.threads:
            t.join()
        self.threads = []
        c = self.counter
        self.log(f"🧾 後處理結束：成功 {c['done']} | 不支援 {c['unsupported']} | 失敗 {c['fail']} | "
                 f"總計處理 {c['pages']} 頁 (WebP)")

    def _bump(self, key, n=1):
        with self._clock:
            self.counter[key] += n

    # ------------------------------------------------------------------ 工作
    def _worker(self):
        while True:
            url = self.q.get()
            if url is None:
                break
            if self.stop.is_set():
                continue
            self._process_url(url)

    def _process_url(self, url):
        row = self.db.lookup(url)
        if not row or row["status"] != "ok" or not row["file_path"] or not os.path.exists(row["file_path"]):
            self.db.set_proc(url, "fail", "本地檔案不存在")
            self._bump("fail")
            return
        t0 = time.time()
        name = row.get("orig_name") or os.path.basename(row["file_path"])
        try:
            res = self.process_file(row)
            self.db.set_proc(url, res["status"], res.get("msg", ""), res.get("json_path", ""),
                             res.get("image_count", 0), total_pages=res.get("total_pages", 0),
                             ai_title=res.get("title", ""))
            # ④R2 / ⑤發布 狀態獨立回寫（即使生成 done，發布失敗也只標 pub_status=fail）
            if res.get("r2_done"):
                self.db.set_r2(url, "done", res.get("r2_msg", ""))
            pub = res.get("publish") or {}
            if pub.get("status") in ("done", "fail", "skipped"):
                self.db.set_publish(url, pub["status"], pub.get("msg", ""))
            self._bump(res["status"])
            self._bump("pages", res.get("total_pages", 0))
            self.log(f"🧾 {'✅' if res['status'] == 'done' else '⚠️'} {name[:48]} | "
                     f"總頁數 {res.get('total_pages', 0)} | {fmt_dur(time.time() - t0)} | {res.get('msg', '')}"
                     + (f"\n      → JSON: {res['json_path']}" if res.get("json_path") else ""))
        except Exception as e:  # noqa: BLE001
            self.db.set_proc(url, "fail", f"{type(e).__name__}: {str(e)[:150]}")
            self._bump("fail")
            self.log(f"🧾 ❌ 處理失敗 {name[:40]}: {type(e).__name__}: {str(e)[:100]}")

    def process_file(self, row):
        path = row["file_path"]
        md5 = row.get("md5") or os.path.splitext(os.path.basename(path))[0]
        try:
            out = DocPipeline(self.cfg, self.log).run(
                file_path=path, md5=md5, file_size=os.path.getsize(path),
                orig_name=row.get("orig_name") or os.path.basename(path),
                keyword=row.get("keyword") or "", enable_ai=self.enable_ai, enable_r2=self.enable_r2,
                enable_publish=self.enable_publish, source_url=row.get("url") or "")
            return {"status": "done", "msg": f"✅ {out['summary_msg']}", "json_path": out["json_path"],
                    "image_count": out["image_count"], "total_pages": out["total_pages"],
                    "title": out.get("title", ""), "r2_done": out.get("r2_done", False),
                    "r2_msg": out.get("r2_msg", ""), "publish": out.get("publish") or {"status": "off"}}
        except UnsupportedFormat as e:
            return {"status": "unsupported", "msg": str(e)[:150], "json_path": "", "image_count": 0,
                    "total_pages": 0}
        except Exception as e:  # noqa: BLE001
            return {"status": "fail", "msg": f"{type(e).__name__}: {str(e)[:150]}", "json_path": "",
                    "image_count": 0, "total_pages": 0}
