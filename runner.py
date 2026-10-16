# runner.py
"""任務調度（與 GUI 無關，GUI 與 CLI 共用）：搜尋 → 下載 → 後處理，以及掃描 / 匯出 / 校驗等維護功能。"""
import os
import time
import random
import shutil

from core.constants import ALL_EXTS, MIN_FILE_BYTES
from core.db import DownloadDB
from core.utils import md5_file, norm_proxy, write_csv
from crawler.search import YahooSearcher, is_excluded
from crawler.downloader import Downloader
from processing.proc_manager import ProcManager

MODES = ("pipeline", "search_only", "db_only", "process_only")

DEFAULT_CRAWL_OPTS = {
    "file_ext": "pdf", "max_pages": 5, "pg_min": 2, "pg_max": 5, "stale_limit": 0,
    "workers": 8, "proc_workers": 2, "max_retries": 3, "retry_interval": 30 * 60,
    "dom_min": 180, "dom_max": 300, "proxy": "", "exclude": [],
    "headless": False, "verify": False, "retry_failed": False, "force": False, "wayback": False,
}


def make_opts(cloud_cfg, **override):
    """由『雲端/處理設定』+ 採集選項組成完整 opt；步驟開關取自 cloud_cfg。"""
    opt = dict(DEFAULT_CRAWL_OPTS)
    opt.update(override)
    opt["proxy"] = norm_proxy(opt.get("proxy"))
    opt["dom_max"] = max(opt["dom_min"], opt["dom_max"])
    opt["pg_max"] = max(opt["pg_min"], opt["pg_max"])
    opt.update({
        "cloud_cfg": cloud_cfg,
        "step1_dl": bool(cloud_cfg.get("step1_download", True)),
        "step2_extract": bool(cloud_cfg.get("step2_extract", True)),
        "step3_ai": bool(cloud_cfg.get("step3_aliyun_ai", True)),
        "step4_r2": bool(cloud_cfg.get("step4_upload_r2", True)),
        "step5_publish": bool(cloud_cfg.get("step5_publish_cms", True)),
        "shard_storage": bool(cloud_cfg.get("shard_storage", True)),
    })
    return opt


class TaskRunner:
    def __init__(self, log, stop_event, on_progress=None):
        self.log = log
        self.stop = stop_event
        self.on_progress = on_progress or (lambda done, total: None)
        self.db = None
        self.searcher = None

    # ------------------------------------------------------------------ 資料庫
    def open_db(self, storage_root):
        """採集資料庫固定放在 <storage_root>/db/downloads.db，獨立於 down/json/pic。

        舊版本把庫放在 <storage_root>/down/downloads.db；首次切換時自動複製遷移（只做一次，
        不刪舊檔），避免用戶誤以為數據丟失。路徑一律由 storage_root 派生，不隨 CWD 漂移。
        """
        if not storage_root:
            raise ValueError("storage_root 空白，無法決定資料庫位置")
        db_dir = os.path.join(storage_root, "db")
        os.makedirs(db_dir, exist_ok=True)
        path = os.path.join(db_dir, "downloads.db")
        if self.db and os.path.normcase(os.path.abspath(self.db.path)) == os.path.normcase(os.path.abspath(path)):
            return self.db
        if self.db:
            self.db.close()
        if not os.path.exists(path):
            legacy = os.path.join(storage_root, "down", "downloads.db")
            if os.path.exists(legacy):
                try:
                    shutil.copy2(legacy, path)
                    self.log(f"🗄️ 偵測到舊位置資料庫，已遷移：{legacy} → {path}（舊檔保留）")
                except OSError as e:  # noqa: BLE001
                    self.log(f"⚠️ 舊資料庫遷移失敗（改用全新空庫）: {e}")
        self.db = DownloadDB(path)
        return self.db

    def close(self):
        if self.searcher:
            self.searcher.quit_driver()
        if self.db:
            self.db.close()
            self.db = None

    def _sleep(self, sec):
        end = time.time() + sec
        while time.time() < end and not self.stop.is_set():
            time.sleep(0.3)

    # ------------------------------------------------------------------ 主任務
    def run(self, mode, keywords, down_dir, opt):
        if mode not in MODES:
            raise ValueError(f"未知模式: {mode}")
        cloud = opt["cloud_cfg"]
        proc = None
        try:
            for d in (cloud.get("storage_root"), down_dir, cloud.get("dir_pic"),
                      cloud.get("dir_json"), cloud.get("dir_db")):
                if d:
                    os.makedirs(d, exist_ok=True)
            db = self.open_db(cloud.get("storage_root") or os.path.dirname(down_dir))
            self.log(f"📁 路徑：下載={down_dir} | 圖片={cloud.get('dir_pic')} | "
                     f"JSON={cloud.get('dir_json')} | 資料庫={db.path}")

            enable_proc = (opt["step2_extract"] and mode in ("pipeline", "db_only")) or mode == "process_only"
            if enable_proc:
                proc = ProcManager(db, self.log, self.stop, cloud,
                                   enable_ai=opt["step3_ai"], enable_r2=opt["step4_r2"],
                                   enable_publish=opt["step5_publish"])
                proc.begin(opt["proc_workers"])

            # ---- 搜尋 ----
            if mode == "search_only" or (mode == "pipeline" and opt["step1_dl"]):
                self._search_stage(db, keywords, opt)

            # ---- 下載 ----
            if (mode == "db_only" or (mode == "pipeline" and opt["step1_dl"])) and not self.stop.is_set():
                self._download_stage(db, down_dir, opt, proc)

            # ---- 後處理：補送尚未處理的既有文檔 ----
            if proc and not self.stop.is_set():
                include_failed = mode == "process_only" or (mode == "pipeline" and not opt["step1_dl"])
                urls = db.pending_process(include_failed=include_failed)
                if urls:
                    self.log(f"🧾 待執行後處理的已下載文檔共 {len(urls)} 個")
                for u in urls:
                    proc.submit(u)
        except Exception as e:  # noqa: BLE001
            self.log(f"❌ 任務發生錯誤: {type(e).__name__}: {e}")
        finally:
            if proc:
                proc.end()
            if self.searcher:
                self.searcher.quit_driver()

    def _search_stage(self, db, keywords, opt):
        todo = db.add_keywords(keywords, opt["file_ext"], force=opt["force"])
        if not todo:
            self.log("ℹ️ 所有關鍵字皆已完成搜尋（勾選『強制重搜已完成關鍵字』可重跑）")
        self.searcher = YahooSearcher(db, self.log, self.stop)
        for idx, kw in enumerate(todo, 1):
            if self.stop.is_set():
                break
            self.log(f"\n🔑 [{idx}/{len(todo)}] 搜尋關鍵字：{kw}")
            if self.searcher.collect_links(kw, opt):
                self.log("🛑 搜尋被驗證頁攔截，終止後續關鍵字。")
                break
            if idx < len(todo):
                self._sleep(random.uniform(opt["pg_min"] + 1, opt["pg_max"] + 3))
        self.searcher.quit_driver()

    def _download_stage(self, db, down_dir, opt, proc):
        tasks = []
        for t in db.get_links_to_download(opt["retry_failed"]):
            if is_excluded(t["url"], opt["exclude"]):
                continue
            t["file_ext"] = (t.get("file_ext") or opt["file_ext"]).lower()
            tasks.append(t)
        if not tasks:
            self.log("✅ 目前沒有需要下載的新連結。")
            return
        on_success = proc.submit if (proc and opt["step2_extract"]) else None
        Downloader(db, self.log, self.stop, self.on_progress, on_success).download_all(tasks, down_dir, opt)

    # ------------------------------------------------------------------ 維護功能
    def scan_folder(self, save_dir):
        """掃描本地資料夾（含子資料夾）內的文檔並入庫等待後處理，回傳 (總數, 新增數)。

        資料庫位置與掃描目錄無關，一律使用目前已開啟的 <storage_root>/db 連線。
        """
        # 掃描目錄即 down 目錄（<root>/down），storage root 是它的上一層
        db = self.db or self.open_db(os.path.dirname(save_dir.rstrip("\\/")))
        self.log(f"🗂 掃描 {save_dir} 本地文檔並加入後處理佇列（資料庫：{db.path}）...")
        new = total = 0
        for root_, _, files in os.walk(save_dir):
            for fn in files:
                if self.stop.is_set():
                    return total, new
                ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
                if ext not in ALL_EXTS or fn.startswith(".tmp_"):
                    continue
                p = os.path.join(root_, fn)
                total += 1
                if db.has_path(p):
                    continue
                try:
                    size = os.path.getsize(p)
                    if size < MIN_FILE_BYTES:
                        continue
                    if db.add_local(p, md5_file(p), size, ext=ext, orig_name=fn):
                        new += 1
                except OSError:
                    continue
        self.log(f"✅ 掃描完成：共 {total} 個檔案，新增入庫 {new} 筆")
        return total, new

    def export_reports(self, save_dir):
        """匯出 4 份 CSV：成功清單 / 失敗清單 / 關鍵字統計 / 網站統計；回傳輸出資料夾。"""
        out = os.path.join(save_dir, f"報表_{time.strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(out, exist_ok=True)
        db = self.db
        rows = db.all_rows()
        write_csv(os.path.join(out, "成功清單.csv"),
                  ["網址", "網站", "檔案", "大小", "總頁數", "AI標題", "關鍵字", "後處理狀態", "時間", "JSON路徑"],
                  [(r["url"], r["domain"], r["file_path"] or "", r["size"] or "", r.get("total_pages") or "",
                    r.get("ai_title") or "", r["keyword"] or "", r.get("proc_status") or "",
                    r["updated_at"], r.get("json_path") or "")
                   for r in rows if r["status"] in ("ok", "dup")])
        write_csv(os.path.join(out, "失敗清單.csv"),
                  ["網址", "網站", "錯誤類型", "錯誤訊息", "失敗次數", "下次重試", "關鍵字"],
                  [(r["url"], r["domain"], r["err_type"] or "", r["err_msg"] or "", r["fail_count"] or 0,
                    r["next_retry_at"] or "停止重試", r["keyword"] or "")
                   for r in rows if r["status"] == "fail"])
        write_csv(os.path.join(out, "關鍵字統計.csv"),
                  ["關鍵字", "格式", "狀態", "開始", "結束", "耗時(秒)", "頁數", "發現連結", "新增", "成功", "重複", "失敗", "待處理", "略過"],
                  [(k["keyword"], k["file_ext"] or "", k["status"], k["started_at"] or "", k["finished_at"] or "",
                    int(k["duration_sec"] or 0), k["pages_crawled"] or 0, k["links_found"] or 0, k["new_links"] or 0,
                    k["ok"], k["dup"], k["fail"], k["pending"], k["skip"]) for k in db.keyword_stats()])
        write_csv(os.path.join(out, "網站統計.csv"),
                  ["網站", "成功", "重複", "失敗", "略過", "待處理", "總計", "容量(bytes)", "最近更新"],
                  [(d["domain"] or "未知", d["ok"], d["dup"], d["fail"], d["skip"], d["pending"], d["total"],
                    d["bytes"], d["last"] or "") for d in db.domain_stats()])
        self.log(f"📊 報表已匯出：{out}")
        return out

    def verify_files(self):
        res = self.db.verify_files()
        self.log(f"🩺 校驗完成：檔案遺失 {res['missing']} 筆、檔案過小/空檔 {res['small']} 筆（已改回待下載）")
        return res
