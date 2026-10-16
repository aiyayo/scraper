# gui/tab_pipeline.py
"""選項卡 1：採集與五步流水線（關鍵字、下載並發、防封鎖、任務按鈕、即時日誌）。"""
import re
import tkinter as tk
from tkinter import ttk, scrolledtext

from core.constants import ALL_EXTS
from core.utils import parse_keywords
from gui.widgets import spin, to_int


class PipelineTab:
    def __init__(self, app, parent):
        self.app = app
        saved = app.saved_cfg
        self._opts = saved.get("crawl_opts") or {}
        self.btns = []
        self._build(parent, saved)

    # ------------------------------------------------------------------ 變數
    def _sv(self, key, default):
        return tk.StringVar(value=str(self._opts.get(key, default)))

    def _bv(self, key, default):
        return tk.BooleanVar(value=bool(self._opts.get(key, default)))

    # ------------------------------------------------------------------ UI
    def _build(self, parent, saved):
        # --- 五步驟開關 ---
        f_steps = ttk.LabelFrame(parent, text="🎯 五步流水線執行開關（可自由勾選，預設全選自動串聯執行）", padding=10)
        f_steps.pack(fill=tk.X, pady=4)
        self.step_vars = {
            "step1_download": tk.BooleanVar(value=saved.get("step1_download", True)),
            "step2_extract": tk.BooleanVar(value=saved.get("step2_extract", True)),
            "step3_aliyun_ai": tk.BooleanVar(value=saved.get("step3_aliyun_ai", True)),
            "step4_upload_r2": tk.BooleanVar(value=saved.get("step4_upload_r2", True)),
            "step5_publish_cms": tk.BooleanVar(value=saved.get("step5_publish_cms", True)),
        }
        labels = [
            ("step1_download", "步驟 ①：Yahoo 搜尋與下載檔案 (down/)"),
            ("step2_extract", "步驟 ②：逐頁轉 WebP (pic/) + 每頁文字與總頁數寫入 JSON (json/)"),
            ("step3_aliyun_ai", "步驟 ③：阿里雲智能體 (標題/簡介/關鍵詞)"),
            ("step4_upload_r2", "步驟 ④：上傳至 Cloudflare R2 (原檔/圖片/JSON)"),
            ("step5_publish_cms", "步驟 ⑤：POST 發布至帝國 CMS（見「發布接口」頁）"),
        ]
        for i, (key, text) in enumerate(labels):
            if i:
                ttk.Label(f_steps, text="➔", foreground="#2563eb").pack(side=tk.LEFT)
            ttk.Checkbutton(f_steps, text=text, variable=self.step_vars[key]).pack(side=tk.LEFT, padx=8)

        # --- 關鍵字 / 搜尋 ---
        f_kw = ttk.LabelFrame(parent, text="🔍 批量搜尋關鍵字與 Yahoo 採集設定", padding=10)
        f_kw.pack(fill=tk.X, pady=4)
        f_kw.columnconfigure(1, weight=1)
        ttk.Label(f_kw, text="批量關鍵字\n(每行一個):").grid(row=0, column=0, sticky=tk.NW, pady=2)
        self.keyword_text = scrolledtext.ScrolledText(f_kw, height=5, width=60)
        self.keyword_text.grid(row=0, column=1, sticky="ew", padx=6, pady=2)
        self.keyword_text.insert(tk.END, self._opts.get("keywords", "四年級數學教案"))

        r1 = ttk.Frame(f_kw)
        r1.grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(r1, text="目標檔案格式:").pack(side=tk.LEFT)
        self.file_type_var = self._sv("file_ext", "pdf")
        ttk.Combobox(r1, textvariable=self.file_type_var, values=ALL_EXTS, width=7,
                     state="readonly").pack(side=tk.LEFT, padx=4)
        self.pages_var = self._sv("max_pages", 5)
        spin(r1, "每詞最大搜尋頁數:", self.pages_var, 1, 100, 5)
        self.pg_min_var, self.pg_max_var = self._sv("pg_min", 2), self._sv("pg_max", 5)
        spin(r1, "翻頁隨機間隔(秒):", self.pg_min_var, 0, 60, 4)
        spin(r1, "~", self.pg_max_var, 0, 120, 4)
        self.stale_var = self._sv("stale_limit", 0)
        spin(r1, "連續N頁無新連結停翻(0=關):", self.stale_var, 0, 20, 4)

        # --- 並發 / 防封鎖 ---
        f_dl = ttk.LabelFrame(parent, text="📥 下載並發、後處理設定與防封鎖策略", padding=10)
        f_dl.pack(fill=tk.X, pady=4)
        r2 = ttk.Frame(f_dl)
        r2.pack(fill=tk.X, pady=2)
        self.workers_var = self._sv("workers", 8)
        spin(r2, "下載並發執行緒:", self.workers_var, 1, 32, 4)
        self.proc_workers_var = self._sv("proc_workers", 2)
        spin(r2, "後處理執行緒(WebP/AI/R2):", self.proc_workers_var, 1, 8, 4)
        self.max_retries_var = self._sv("max_retries", 3)
        spin(r2, "失敗最大重試:", self.max_retries_var, 1, 20, 4)
        self.retry_min_var = self._sv("retry_min", 30)
        spin(r2, "重試冷卻(分):", self.retry_min_var, 1, 1440, 5)
        self.dom_min_var, self.dom_max_var = self._sv("dom_min", 180), self._sv("dom_max", 300)
        spin(r2, "同網站下載間隔(秒):", self.dom_min_var, 0, 3600, 5)
        spin(r2, "~", self.dom_max_var, 0, 7200, 5)

        r3 = ttk.Frame(f_dl)
        r3.pack(fill=tk.X, pady=4)
        ttk.Label(r3, text="HTTP 代理(可空):").pack(side=tk.LEFT)
        self.proxy_var = self._sv("proxy", "")
        ttk.Entry(r3, textvariable=self.proxy_var, width=22).pack(side=tk.LEFT, padx=4)

        # 排除域名：多行輸入，一行一個（相容舊的逗號分隔貼入）
        ex = ttk.Frame(f_dl)
        ex.pack(fill=tk.X, pady=2)
        ttk.Label(ex, text="排除域名(每行1個，自動含子網域):").pack(side=tk.LEFT, anchor=tk.N)
        self.exclude_text = scrolledtext.ScrolledText(ex, height=3, width=46, wrap=tk.NONE,
                                                      font=("Microsoft JhengHei", 9))
        self.exclude_text.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        old_ex = str(self._opts.get("exclude", ""))
        if isinstance(self._opts.get("exclude"), list):  # 舊版以清單保存時還原成多行
            old_ex = "\n".join(self._opts["exclude"])
        self.exclude_text.insert("1.0", old_ex)

        self.flag_vars = {
            "headless": self._bv("headless", False), "verify": self._bv("verify", False),
            "retry_failed": self._bv("retry_failed", False), "force": tk.BooleanVar(value=False),
            "wayback": self._bv("wayback", False),
        }
        for key, text in (("headless", "無頭模式(Headless)"), ("verify", "嚴格校驗SSL"),
                          ("retry_failed", "包含歷史失敗任務(到期重試)"), ("force", "強制重搜已完成關鍵字"),
                          ("wayback", "Wayback歷史存檔備援")):
            ttk.Checkbutton(r3, text=text, variable=self.flag_vars[key]).pack(side=tk.LEFT, padx=5)

        # --- 任務按鈕 ---
        bf = ttk.LabelFrame(parent, text="⚡ 任務啟動與控制", padding=8)
        bf.pack(fill=tk.X, pady=4)
        for text, cmd in (
            ("🚀 依勾選步驟執行完整流水線", lambda: self.app.start_task("pipeline")),
            ("🔎 僅採集 Yahoo 連結", lambda: self.app.start_task("search_only")),
            ("📥 僅下載未完成項 (+勾選的後處理)", lambda: self.app.start_task("db_only")),
            ("🧾 僅執行後處理 (步驟②~⑤)", lambda: self.app.start_task("process_only")),
            ("🗂 掃描本地資料夾入庫", self.app.scan_ui),
            ("♻️ 重置失敗項", self.app.reset_failed_ui),
            ("🩺 校驗本地檔案", self.app.verify_ui),
            ("📊 匯出報表", self.app.export_ui),
        ):
            b = ttk.Button(bf, text=text, command=cmd)
            b.pack(side=tk.LEFT, padx=3)
            self.btns.append(b)
        self.stop_btn = ttk.Button(bf, text="⏹ 停止任務", command=self.app.stop_task, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.RIGHT, padx=6)

        # --- 日誌 ---
        f_log = ttk.LabelFrame(parent, text="📝 即時執行日誌（同時寫入 下載目錄/logs/）", padding=4)
        f_log.pack(fill=tk.BOTH, expand=True, pady=4)
        self.log_area = scrolledtext.ScrolledText(f_log, state="disabled", bg="#1e1e1e", fg="#00e676",
                                                  font=("Consolas", 10))
        self.log_area.pack(fill=tk.BOTH, expand=True)


    # ------------------------------------------------------------------ 對外介面
    def _exclude_list(self):
        """多行排除域名 → 清單；每行一個，也相容逗號／頓號分隔，去掉開頭 www.。"""
        raw = self.exclude_text.get("1.0", tk.END)
        out = []
        for d in re.split(r"[\n,，、;；]+", raw):
            d = d.strip().lower().lstrip(".")
            if d.startswith("www."):
                d = d[4:]
            if d and d not in out:
                out.append(d)
        return out

    def keywords(self):
        return parse_keywords(self.keyword_text.get("1.0", tk.END))

    def steps(self):
        return {k: v.get() for k, v in self.step_vars.items()}

    def crawl_opts(self):
        """回傳給 runner.make_opts 的採集選項。"""
        i = to_int
        dmin = i(self.dom_min_var, 180, 0, 7200)
        pmin = i(self.pg_min_var, 2, 0, 120)
        return {
            "file_ext": self.file_type_var.get(),
            "max_pages": i(self.pages_var, 5, 1, 100),
            "pg_min": pmin, "pg_max": max(pmin, i(self.pg_max_var, 5, 0, 120)),
            "stale_limit": i(self.stale_var, 0, 0, 20),
            "workers": i(self.workers_var, 8, 1, 32),
            "proc_workers": i(self.proc_workers_var, 2, 1, 8),
            "max_retries": i(self.max_retries_var, 3, 1, 20),
            "retry_interval": i(self.retry_min_var, 30, 1, 1440) * 60,
            "dom_min": dmin, "dom_max": max(dmin, i(self.dom_max_var, 300, 0, 7200)),
            "proxy": self.proxy_var.get().strip(),
            "exclude": self._exclude_list(),
            "headless": self.flag_vars["headless"].get(), "verify": self.flag_vars["verify"].get(),
            "retry_failed": self.flag_vars["retry_failed"].get(), "force": self.flag_vars["force"].get(),
            "wayback": self.flag_vars["wayback"].get(),
        }

    def persist(self):
        """寫入設定檔的內容（下次啟動還原；『強制重搜』不保存，避免誤觸重跑）。"""
        data = {
            "keywords": self.keyword_text.get("1.0", tk.END).strip(),
            "file_ext": self.file_type_var.get(), "max_pages": self.pages_var.get(),
            "pg_min": self.pg_min_var.get(), "pg_max": self.pg_max_var.get(), "stale_limit": self.stale_var.get(),
            "workers": self.workers_var.get(), "proc_workers": self.proc_workers_var.get(),
            "max_retries": self.max_retries_var.get(), "retry_min": self.retry_min_var.get(),
            "dom_min": self.dom_min_var.get(), "dom_max": self.dom_max_var.get(),
            "proxy": self.proxy_var.get(), "exclude": self.exclude_text.get("1.0", tk.END).strip(),
        }
        for k in ("headless", "verify", "retry_failed", "wayback"):
            data[k] = self.flag_vars[k].get()
        return data

    def set_busy(self, busy):
        for b in self.btns:
            b.config(state=tk.DISABLED if busy else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if busy else tk.DISABLED)

    def clear_log(self):
        self.log_area.config(state="normal")
        self.log_area.delete("1.0", tk.END)
        self.log_area.config(state="disabled")

    def append_log(self, text):
        self.log_area.config(state="normal")
        self.log_area.insert(tk.END, text + "\n")
        if int(self.log_area.index("end-1c").split(".")[0]) > 6000:
            self.log_area.delete("1.0", "1500.0")
        self.log_area.see(tk.END)
        self.log_area.config(state="disabled")
