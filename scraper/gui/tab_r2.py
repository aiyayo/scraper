# gui/tab_r2.py
"""選項卡 3：Cloudflare R2 金鑰 / 目錄 / 轉圖參數，以及雲端物件瀏覽器（所有網路操作都在背景執行緒）。"""
import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from core.constants import R2_DIR_DOWN, R2_DIR_PIC, R2_DIR_JSON
from core.config import derive_dirs
from core.utils import fmt_size
from gui.widgets import (spin, to_int, to_float, make_tree, fill_tree, add_context_menu,
                         OCR_LABELS, OPT_LABELS, label_of)
from processing.r2_client import R2Client


class R2Tab:
    def __init__(self, app, parent):
        self.app = app
        saved = app.saved_cfg

        f = ttk.LabelFrame(parent, text="☁️ Cloudflare R2 帳戶金鑰與本地/雲端映射目錄設定", padding=10)
        f.pack(fill=tk.X, pady=4)
        f.columnconfigure(1, weight=1)
        f.columnconfigure(3, weight=1)

        def entry(row, col, label, key, show=None, default=""):
            ttk.Label(f, text=label).grid(row=row, column=col, sticky=tk.W,
                                          padx=(12, 2) if col else 0, pady=4)
            var = tk.StringVar(value=saved.get(key, default))
            ttk.Entry(f, textvariable=var, show=show).grid(row=row, column=col + 1, sticky="ew", padx=6, pady=4)
            return var

        self.endpoint_var = entry(0, 0, "R2 Endpoint URL:", "r2_endpoint_url")
        self.bucket_var = entry(0, 2, "R2 Bucket 名稱:", "r2_bucket_name")
        self.ak_var = entry(1, 0, "R2 Access Key:", "r2_access_key")
        self.sk_var = entry(1, 2, "R2 Secret Key:", "r2_secret_key", show="*")
        self.domain_var = entry(2, 0, "自訂公開域名 (可選):", "r2_public_domain")

        # --- 轉圖參數 ---
        q = ttk.Frame(f)
        q.grid(row=2, column=2, columnspan=2, sticky="w", padx=(12, 2), pady=4)
        self.zoom_var = tk.StringVar(value=str(saved.get("webp_zoom", 2.0)))
        self.quality_var = tk.StringVar(value=str(saved.get("webp_quality", 85)))
        self.maxw_var = tk.StringVar(value=str(saved.get("webp_max_width", 1600)))
        spin(q, "WebP 渲染倍率:", self.zoom_var, 1, 4, 4)
        spin(q, "畫質(1-100):", self.quality_var, 50, 100, 4)
        spin(q, "最大寬度(px,0不限):", self.maxw_var, 0, 8000, 6)

        # --- 目錄：只選一個根目錄，自動建立 down/json/pic ---
        ttk.Label(f, text="存儲根目錄（自動建立 down/json/pic）:").grid(
            row=3, column=0, sticky=tk.W, pady=4)
        rbox = ttk.Frame(f)
        rbox.grid(row=3, column=1, sticky="ew", padx=6, pady=4)
        self.root_var = tk.StringVar(value=saved.get("storage_root", ""))
        ttk.Entry(rbox, textvariable=self.root_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(rbox, text="瀏覽", width=5, command=self._pick_root).pack(side=tk.RIGHT, padx=2)

        derived = derive_dirs(self.root_var.get())
        self.dir_down_var = tk.StringVar(value=derived["dir_down"])
        self.dir_pic_var = tk.StringVar(value=derived["dir_pic"])
        self.dir_json_var = tk.StringVar(value=derived["dir_json"])
        self.dirs_lbl = ttk.Label(f, foreground="#475569",
                                  text=f"📂 {self.dir_down_var.get()} ｜ 🖼️ {self.dir_pic_var.get()} ｜ "
                                       f"🧾 {self.dir_json_var.get()}（依 MD5 前2碼/第3碼分層，圖片用二次MD5）")
        self.dirs_lbl.grid(row=4, column=0, columnspan=4, sticky=tk.W, padx=4, pady=(0, 4))
        self.root_var.trace_add("write", lambda *_: self._sync_dirs())

        # --- 進階處理選項 ---
        adv = ttk.Frame(f)
        adv.grid(row=5, column=0, columnspan=4, sticky="w", pady=4)
        self.workers_var = tk.StringVar(value=str(saved.get("r2_upload_workers", 4)))
        self.maxpages_var = tk.StringVar(value=str(saved.get("max_render_pages", 0)))
        self.skip_var = tk.BooleanVar(value=saved.get("r2_skip_existing", True))
        self.reuse_var = tk.BooleanVar(value=saved.get("reuse_existing_webp", True))
        self.lo_var = tk.BooleanVar(value=saved.get("use_office_com", True))
        self.shard_var = tk.BooleanVar(value=saved.get("shard_storage", True))
        spin(adv, "R2 並行上傳數:", self.workers_var, 1, 16, 4)
        spin(adv, "最多渲染頁數(0=不限):", self.maxpages_var, 0, 2000, 5)
        for text, var in (("R2 已存在同大小則跳過", self.skip_var), ("重用已生成的WebP", self.reuse_var),
                          ("Office/WPS 轉圖(Windows)", self.lo_var), ("檔案依MD5分層存放", self.shard_var)):
            ttk.Checkbutton(adv, text=text, variable=var).pack(side=tk.LEFT, padx=6)

        adv2 = ttk.Frame(f)
        adv2.grid(row=6, column=0, columnspan=4, sticky="w", pady=2)
        self.layout_var = tk.BooleanVar(value=saved.get("text_layout", "smart") == "smart")
        self.tables_var = tk.BooleanVar(value=saved.get("layout_tables", True))
        self.join_var = tk.BooleanVar(value=saved.get("layout_join_lines", True))
        self.norm_var = tk.BooleanVar(value=saved.get("text_normalize", True))
        for text, var in (("智能版面還原文字(多欄/去頁眉頁腳)", self.layout_var), ("表格轉Markdown", self.tables_var),
                          ("折行合併成段落", self.join_var), ("全形轉半形/清洗空格", self.norm_var)):
            ttk.Checkbutton(adv2, text=text, variable=var).pack(side=tk.LEFT, padx=6)

        adv3 = ttk.Frame(f)
        adv3.grid(row=7, column=0, columnspan=4, sticky="w", pady=2)
        self.ocr_var = tk.StringVar(value=label_of(OCR_LABELS, saved.get("ocr_engine") or "paddle"))
        self.lang_var = tk.StringVar(value=saved.get("paddle_lang", "ch"))
        self.optstyle_var = tk.StringVar(value=label_of(OPT_LABELS, saved.get("option_style", "list")))
        self.figures_var = tk.BooleanVar(value=saved.get("extract_figures", True))
        self.mdstruct_var = tk.BooleanVar(value=saved.get("md_structure", True))
        ttk.Label(adv3, text="掃描頁 OCR:").pack(side=tk.LEFT, padx=(6, 2))
        ttk.Combobox(adv3, textvariable=self.ocr_var, values=list(OCR_LABELS), width=14,
                     state="readonly").pack(side=tk.LEFT)
        ttk.Label(adv3, text="語言:").pack(side=tk.LEFT, padx=(8, 2))
        ttk.Combobox(adv3, textvariable=self.lang_var, values=["ch", "chinese_cht", "en"], width=11).pack(side=tk.LEFT)
        ttk.Label(adv3, text="選項排版:").pack(side=tk.LEFT, padx=(8, 2))
        ttk.Combobox(adv3, textvariable=self.optstyle_var, values=list(OPT_LABELS), width=26,
                     state="readonly").pack(side=tk.LEFT)
        ttk.Checkbutton(adv3, text="插圖裁切並綁定題目", variable=self.figures_var).pack(side=tk.LEFT, padx=8)
        ttk.Checkbutton(adv3, text="Markdown 結構還原(大題/題號/選項)", variable=self.mdstruct_var).pack(side=tk.LEFT, padx=4)

        btns = ttk.Frame(f)
        btns.grid(row=3, column=2, columnspan=2, sticky="e", pady=4)
        ttk.Button(btns, text="🔌 測試 R2 連線", command=self._test).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="💾 保存配置", command=app.save_ui_settings).pack(side=tk.LEFT, padx=4)

        # 註：R2 上傳後的資料回傳統一走「📤 發布接口（帝國CMS）」頁（流水線步驟⑤），本頁不再內建 POST 設定。

        # --- 雲端瀏覽器 ---
        fb = ttk.LabelFrame(parent, text="🗂️ Cloudflare R2 雲端物件瀏覽器與下載工具", padding=8)
        fb.pack(fill=tk.BOTH, expand=True, pady=6)
        top = ttk.Frame(fb)
        top.pack(fill=tk.X, pady=4)
        ttk.Label(top, text="目錄前綴篩選:").pack(side=tk.LEFT)
        self.prefix_var = tk.StringVar(value="d/")
        ttk.Combobox(top, textvariable=self.prefix_var, width=22,
                     values=["d/", f"{R2_DIR_DOWN}/", f"{R2_DIR_PIC}/", f"{R2_DIR_JSON}/"]).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="🔄 刷新雲端列表", command=self._refresh).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="⬇️ 下載選中到本地", command=self._download).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="📤 手動上傳本地檔案", command=self._upload).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="🗑️ 刪除選中物件", command=self._delete).pack(side=tk.RIGHT, padx=4)

        self.tree = make_tree(fb, [
            ("key", "R2 Object Key (雲端完整路徑)", 560, tk.W),
            ("size", "檔案大小", 110, tk.CENTER),
            ("time", "最後更新時間", 180, tk.CENTER),
        ])
        add_context_menu(self.tree, [("📋 複製公開網址", self._copy_url), ("📋 複製 Key", self._copy_key)])

    # ------------------------------------------------------------------ 設定
    def collect(self):
        return {
            "r2_endpoint_url": self.endpoint_var.get().strip(),
            "r2_access_key": self.ak_var.get().strip(),
            "r2_secret_key": self.sk_var.get().strip(),
            "r2_bucket_name": self.bucket_var.get().strip(),
            "r2_public_domain": self.domain_var.get().strip(),
            "storage_root": self.root_var.get().strip(),
            "dir_down": self.dir_down_var.get().strip(),
            "dir_pic": self.dir_pic_var.get().strip(),
            "dir_json": self.dir_json_var.get().strip(),
            "webp_zoom": to_float(self.zoom_var, 2.0, 1.0, 4.0),
            "webp_quality": to_int(self.quality_var, 85, 1, 100),
            "webp_max_width": to_int(self.maxw_var, 1600, 0, 8000),
            "r2_upload_workers": to_int(self.workers_var, 4, 1, 16),
            "max_render_pages": to_int(self.maxpages_var, 0, 0, 2000),
            "r2_skip_existing": self.skip_var.get(),
            "reuse_existing_webp": self.reuse_var.get(),
            "use_office_com": self.lo_var.get(),
            "shard_storage": self.shard_var.get(),
            "text_layout": "smart" if self.layout_var.get() else "plain",
            "layout_tables": self.tables_var.get(),
            "layout_join_lines": self.join_var.get(),
            "text_normalize": self.norm_var.get(),
            "ocr_engine": OCR_LABELS.get(self.ocr_var.get(), "paddle"),
            "paddle_lang": self.lang_var.get().strip() or "ch",
            "option_style": OPT_LABELS.get(self.optstyle_var.get(), "list"),
            "extract_figures": self.figures_var.get(),
            "md_structure": self.mdstruct_var.get(),
        }

    def _sync_dirs(self):
        """根目錄改動時即時派生 down/json/pic，並切換下載庫；先不建立資料夾（保存時才建）。"""
        d = derive_dirs(self.root_var.get())
        self.dir_down_var.set(d["dir_down"])
        self.dir_pic_var.set(d["dir_pic"])
        self.dir_json_var.set(d["dir_json"])
        self.dirs_lbl.config(text=f"📂 {d['dir_down']} ｜ 🖼️ {d['dir_pic']} ｜ 🧾 {d['dir_json']}"
                                  f"（依 MD5 前2碼/第3碼分層，圖片用二次MD5）")

    def _pick_root(self):
        d = filedialog.askdirectory(title="選擇存儲根目錄（會在其下自動建立 down/json/pic）")
        if d:
            self.root_var.set(os.path.normpath(d))
            self._sync_dirs()
            down = self.dir_down_var.get().strip()
            if down:
                self.app.switch_down_dir(down)

    # ------------------------------------------------------------------ 背景執行輔助
    def _bg(self, work, done):
        """work 在背景執行緒；done(result, error) 回到 UI 執行緒。"""
        def run():
            try:
                res, err = work(), None
            except Exception as e:  # noqa: BLE001
                res, err = None, e
            self.app.ui(lambda: done(res, err))
        threading.Thread(target=run, daemon=True).start()

    def _client(self):
        return R2Client(self.app.get_cloud_cfg())

    def _keys(self):
        return [self.tree.item(i, "values")[0] for i in self.tree.selection()]

    # ------------------------------------------------------------------ 動作
    def _test(self):
        cfg = self.app.get_cloud_cfg()

        def done(_, err):
            if err:
                messagebox.showerror("連線失敗", f"❌ R2 連線失敗:\n{err}")
            else:
                messagebox.showinfo("連線成功", f"✅ 成功連接至 Cloudflare R2 Bucket: {cfg['r2_bucket_name']}")
        self._bg(lambda: R2Client(cfg).test_connection(), done)

    def _refresh(self):
        client, prefix = self._client(), self.prefix_var.get().strip()

        def done(objs, err):
            if err:
                messagebox.showerror("讀取 R2 失敗", str(err))
                return
            fill_tree(self.tree, [(o["Key"], fmt_size(o["Size"]), str(o["LastModified"])[:19]) for o in objs])
        self._bg(lambda: client.list_objects(prefix=prefix, max_keys=1000), done)

    def _download(self):
        keys = self._keys()
        if not keys:
            messagebox.showinfo("提示", "請先在列表中選取要下載的 R2 檔案！")
            return
        dest = filedialog.askdirectory(title="選擇下載保存的本地資料夾")
        if not dest:
            return
        client = self._client()

        def work():
            ok = 0
            for k in keys:
                try:
                    client.download_file(k, os.path.join(dest, os.path.basename(k)))
                    ok += 1
                    self.app.log(f"☁️⬇️ 已從 R2 下載: {k}")
                except Exception as e:  # noqa: BLE001
                    self.app.log(f"☁️❌ 下載失敗 {k}: {e}")
            return ok

        self._bg(work, lambda ok, err: messagebox.showinfo(
            "下載完成", f"成功下載 {ok}/{len(keys)} 個檔案至:\n{dest}") if not err else messagebox.showerror("下載失敗", str(err)))

    def _upload(self):
        fp = filedialog.askopenfilename(title="選擇要上傳至 R2 的本地檔案")
        if not fp:
            return
        ext = os.path.splitext(fp)[1].lower()
        folder = R2_DIR_PIC if ext == ".webp" else (R2_DIR_JSON if ext == ".json" else R2_DIR_DOWN)
        key = f"{folder}/{os.path.basename(fp)}"
        client = self._client()

        def done(_, err):
            if err:
                messagebox.showerror("上傳失敗", str(err))
            else:
                messagebox.showinfo("上傳成功", f"已上傳至 R2:\n/{key}")
                self._refresh()
        self._bg(lambda: client.upload_file(fp, key), done)

    def _delete(self):
        keys = self._keys()
        if not keys or not messagebox.askyesno("確認刪除", f"確定要從 Cloudflare R2 刪除選中的 {len(keys)} 個物件？此操作無法復原。"):
            return
        client = self._client()

        def done(_, err):
            if err:
                messagebox.showerror("刪除失敗", str(err))
            self._refresh()
        self._bg(lambda: client.delete_objects(keys), done)

    def _copy_key(self):
        keys = self._keys()
        if keys:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append("\n".join(keys))

    def _copy_url(self):
        pub = self.domain_var.get().strip().rstrip("/")
        keys = self._keys()
        if not pub:
            messagebox.showinfo("提示", "請先填寫『自訂公開域名』。")
        elif keys:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append("\n".join(f"{pub}/{k}" for k in keys))
