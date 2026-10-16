# gui/app.py
"""主視窗：組裝 6 個選項卡（含獨立的帝國CMS 發布接口頁）、日誌佇列輪詢、任務執行緒與維護動作。"""
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from core.config import load_settings, save_settings
from core.utils import setup_file_logger, open_path
from gui.tab_ai import AITab
from gui.tab_doc import DocTab
from gui.tab_monitor import MonitorTab
from gui.tab_pipeline import PipelineTab
from gui.tab_publish import PublishTab
from gui.tab_r2 import R2Tab
from gui.widgets import FONT_UI_BOLD
from runner import TaskRunner, make_opts

TITLE = "統一文檔採集與智能處理系統 v5.0 (多選項卡 · 五步流水線 · 阿里雲百煉 · Cloudflare R2 · 帝國CMS發布)"


class App:
    def __init__(self, root):
        self.root = root
        root.title(TITLE)
        root.geometry("1300x920")
        root.configure(padx=8, pady=6)

        self.saved_cfg = load_settings()
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.runner = TaskRunner(self.log, self.stop_event, on_progress=lambda d, t: self.q.put(("progress", d, t)))
        self.file_logger = None
        self.running = False
        self._last_ui_update = 0.0

        self._build()
        self.switch_down_dir(self.r2.dir_down_var.get().strip(), quiet=True)
        root.after(150, self.poll_queue)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ 組裝
    def _build(self):
        top = ttk.Frame(self.root, padding=(4, 2))
        top.pack(side=tk.TOP, fill=tk.X)
        self.status_var = tk.StringVar(value="就緒")
        ttk.Label(top, textvariable=self.status_var, font=FONT_UI_BOLD, foreground="#0f172a").pack(side=tk.LEFT)
        ttk.Button(top, text="💾 保存全部配置", command=self.save_ui_settings).pack(side=tk.RIGHT, padx=4)

        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(side=tk.TOP, fill=tk.X, pady=3)

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill=tk.BOTH, expand=True)
        frames = {}
        for key, text, pad in (("pipe", "🚀 1. 採集與五步流水線", 8), ("ai", "🤖 2. 阿里雲智能體配置", 10),
                               ("r2", "☁️ 3. Cloudflare R2 與目錄", 10), ("doc", "📄 4. 統一文檔處理與預覽", 6),
                               ("mon", "📊 5. 數據與任務監控中心", 6),
                               ("pub", "📤 6. 發布接口（帝國CMS·步驟⑤）", 6)):
            frames[key] = ttk.Frame(self.nb, padding=pad)
            self.nb.add(frames[key], text=text)
        self.doc_frame = frames["doc"]

        self.pipeline = PipelineTab(self, frames["pipe"])
        self.ai = AITab(self, frames["ai"])
        self.r2 = R2Tab(self, frames["r2"])
        self.doc = DocTab(self, frames["doc"])
        self.monitor = MonitorTab(self, frames["mon"])
        self.publish = PublishTab(self, frames["pub"])

    # ------------------------------------------------------------------ 對外介面（供各選項卡使用）
    @property
    def db(self):
        return self.runner.db

    def down_dir(self):
        return self.r2.dir_down_var.get().strip()

    def log(self, message):
        """執行緒安全：寫入 GUI 日誌佇列，同時寫入檔案日誌。"""
        self.q.put(("log", message))
        if self.file_logger:
            self.file_logger.info(message.replace("\n", " ⏎ "))

    def ui(self, fn):
        """讓背景執行緒把函式排到主執行緒執行（避免在背景執行緒碰 Tk）。"""
        self.q.put(("call", fn))

    def get_cloud_cfg(self):
        cfg = {}
        cfg.update(self.ai.collect())
        cfg.update(self.r2.collect())
        cfg.update(self.pipeline.steps())
        cfg.update(self.publish.collect())
        return cfg

    def refresh_ui(self, force=False):
        self.monitor.refresh(force)

    def open_in_workbench(self, path):
        if path and os.path.exists(path):
            self.nb.select(self.doc_frame)
            self.doc.import_file(path)

    def switch_down_dir(self, d, quiet=False):
        """切換下載目錄：開啟 <存儲根>/db 裡的採集資料庫，並在 down/logs 啟用檔案日誌。"""
        if not d:
            return
        try:
            root = self.r2.root_var.get().strip() or os.path.dirname(d)
            self.runner.open_db(root)
            self.file_logger = setup_file_logger(os.path.join(d, "logs"))
            self.refresh_ui(force=True)
        except Exception as e:  # noqa: BLE001
            if not quiet:
                messagebox.showerror("開啟資料庫失敗", str(e))

    # ------------------------------------------------------------------ 設定
    def save_ui_settings(self, quiet=False):
        cfg = self.get_cloud_cfg()
        cfg["crawl_opts"] = self.pipeline.persist()
        for k in ("storage_root", "dir_down", "dir_pic", "dir_json", "dir_db"):
            if cfg.get(k):
                os.makedirs(cfg[k], exist_ok=True)
        ok = save_settings(cfg)
        if ok and not quiet:
            self.switch_down_dir(cfg["dir_down"])
            messagebox.showinfo("保存成功", "所有配置（提示詞、R2、目錄、採集選項）已保存！")
        return ok

    # ------------------------------------------------------------------ 任務
    def set_busy(self, busy):
        self.running = busy
        self.pipeline.set_busy(busy)

    def stop_task(self):
        self.stop_event.set()
        self.log("⏹ 收到停止指令，正在結束目前工作...")

    def start_task(self, mode):
        down_dir = self.down_dir()
        if not down_dir:
            messagebox.showwarning("警告", "請先設定下載目錄！")
            return
        keywords = self.pipeline.keywords()
        steps = self.pipeline.steps()
        if mode in ("pipeline", "search_only") and (mode == "search_only" or steps["step1_download"]) and not keywords:
            messagebox.showwarning("警告", "請輸入至少一個搜尋關鍵字！")
            return
        self.save_ui_settings(quiet=True)
        opt = make_opts(self.get_cloud_cfg(), **self.pipeline.crawl_opts())

        self.stop_event.clear()
        self.set_busy(True)
        self.pipeline.clear_log()
        self.progress["value"] = 0
        self.file_logger = setup_file_logger(os.path.join(down_dir, "logs"))

        def work():
            try:
                self.runner.run(mode, keywords, down_dir, opt)
            finally:
                self.q.put(("done",))
        threading.Thread(target=work, daemon=True).start()

    def _run_bg(self, fn, busy=True):
        def work():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.log(f"❌ {type(e).__name__}: {e}")
            finally:
                self.q.put(("done",))
        if busy:
            self.stop_event.clear()
            self.set_busy(True)
        threading.Thread(target=work, daemon=True).start()

    def scan_ui(self):
        d = self.down_dir()
        if d:
            self._run_bg(lambda: self.runner.scan_folder(d))

    def reset_failed_ui(self):
        if self.db and messagebox.askyesno("確認", "將所有失敗項重設為待下載？"):
            self.log(f"♻️ 已重置 {self.db.reset_failed()} 筆失敗項")
            self.refresh_ui(force=True)

    def verify_ui(self):
        if self.db:
            self._run_bg(self.runner.verify_files)

    def export_ui(self):
        d = self.down_dir()
        if not (d and self.db):
            return

        def work():
            out = self.runner.export_reports(d)
            self.ui(lambda: open_path(out))
        self._run_bg(work, busy=False)

    def reprocess_all(self):
        if self.db and messagebox.askyesno("確認", "將所有已下載文檔重設為待處理，並依目前勾選的步驟重新執行？"):
            self.db.reset_proc(None)
            self.start_task("process_only")

    # ------------------------------------------------------------------ 事件迴圈
    def poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.pipeline.append_log(item[1])
                elif kind == "progress":
                    self.progress["maximum"] = max(item[2], 1)
                    self.progress["value"] = item[1]
                elif kind == "call":
                    try:
                        item[1]()
                    except Exception as e:  # noqa: BLE001
                        self.pipeline.append_log(f"❌ UI 回呼錯誤: {e}")
                elif kind == "done":
                    self.set_busy(False)
                    self.refresh_ui(force=True)
        except queue.Empty:
            pass
        now = time.time()
        if self.db and now - self._last_ui_update > 2:
            self._last_ui_update = now
            self.refresh_ui()
        self.root.after(150, self.poll_queue)

    def on_close(self):
        self.stop_event.set()
        try:
            self.save_ui_settings(quiet=True)
        finally:
            self.runner.close()
            self.root.destroy()
