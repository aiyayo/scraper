# gui/tab_doc.py
"""選項卡 4：統一文檔處理與預覽工作台（單檔：轉 WebP → AI → 編輯 → 存 JSON → 上傳 R2）。"""
import os
import json
import threading
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox

from PIL import Image, ImageTk

from core.utils import fmt_size, md5_file
from gui.widgets import FONT_UI_BOLD, OPT_LABELS, label_of, set_text
from processing.ai_client import AliyunAnalyzer, fallback_metadata
from processing.extractor import PageExtractor, UnsupportedFormat
from processing.ocr import OcrUnavailable
from processing.structure import build_markdown
from processing.pipeline import DocPipeline, build_json_payload, save_json


class DocTab:
    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self.path = None
        self.md5 = None
        self.size = 0
        self.pages = []
        self.total = 0
        self.tk_img = None
        self._preview_job = None
        self.stream, self.questions, self.figures = [], [], []
        self._build()

    # ------------------------------------------------------------------ UI
    def _build(self):
        p = self.parent
        bar = ttk.Frame(p, padding=(6, 6))
        bar.pack(side=tk.TOP, fill=tk.X)
        for text, cmd in (
            ("📂 導入本地文檔 (PDF/Office)", self.import_file),
            ("🖼️ 1. 重新轉 WebP & 提取文字", lambda: self.run_extract(force=True)),
            ("🔍 OCR 當前頁", self.run_ocr_page),
            ("🤖 2. 阿里雲分析 標題/簡介/關鍵詞", self.run_ai),
            ("💾 3. 保存 JSON", self.save_json_local),
            ("☁️ 4. 同步此文檔至 R2", self.run_upload),
            ("📤 5. 發布到帝國CMS", self.run_publish),
        ):
            ttk.Button(bar, text=text, command=cmd).pack(side=tk.LEFT, padx=3)
        self.status_var = tk.StringVar(value="就緒：請導入文檔以提取每頁 WebP 圖片與文字")
        ttk.Label(bar, textvariable=self.status_var, foreground="#1d4ed8", font=FONT_UI_BOLD).pack(side=tk.RIGHT, padx=8)

        paned = ttk.PanedWindow(p, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        # 左：頁面列表 + 預覽
        left = ttk.Frame(paned, padding=4)
        paned.add(left, weight=3)
        lf = ttk.LabelFrame(left, text="📑 頁面列表 (點擊切換)", padding=4)
        lf.pack(fill=tk.X, pady=(0, 4))
        self.listbox = tk.Listbox(lf, height=7, exportselection=False)
        sb = ttk.Scrollbar(lf, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.bind("<<ListboxSelect>>", self.on_page_select)

        nav = ttk.Frame(left)
        nav.pack(fill=tk.X)
        ttk.Button(nav, text="◀ 上一頁", command=lambda: self._step(-1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(nav, text="下一頁 ▶", command=lambda: self._step(1)).pack(side=tk.LEFT, padx=2)

        lp = ttk.LabelFrame(left, text="🖼️ 當前頁 WebP 預覽", padding=4)
        lp.pack(fill=tk.BOTH, expand=True)
        self.preview = ttk.Label(lp, text="暫無圖片預覽", anchor=tk.CENTER)
        self.preview.pack(fill=tk.BOTH, expand=True)

        # 中：頁文字 + JSON 預覽
        mid = ttk.Frame(paned, padding=4)
        paned.add(mid, weight=4)
        mt = ttk.LabelFrame(mid, text="📝 當前頁提取文字（可編輯，即時同步至 JSON）", padding=4)
        mt.pack(fill=tk.BOTH, expand=True, pady=(0, 4))
        self.page_text = scrolledtext.ScrolledText(mt, wrap=tk.WORD, font=("Microsoft JhengHei", 10), height=12)
        self.page_text.pack(fill=tk.BOTH, expand=True)
        self.page_text.bind("<KeyRelease>", self._on_text_edited)
        mb = ttk.LabelFrame(mid, text="🧾 輸出預覽（JSON / 結構化 Markdown）", padding=4)
        mb.pack(fill=tk.BOTH, expand=True)
        self.out_nb = ttk.Notebook(mb)
        self.out_nb.pack(fill=tk.BOTH, expand=True)
        fj = ttk.Frame(self.out_nb)
        self.out_nb.add(fj, text="🧾 JSON")
        self.json_area = scrolledtext.ScrolledText(fj, wrap=tk.WORD, font=("Consolas", 9), height=12,
                                                   bg="#0f172a", fg="#e2e8f0")
        self.json_area.pack(fill=tk.BOTH, expand=True)
        fm = ttk.Frame(self.out_nb)
        self.out_nb.add(fm, text="📝 Markdown（可編輯）")
        tb = ttk.Frame(fm)
        tb.pack(fill=tk.X, pady=2)
        ttk.Label(tb, text="選項排版:").pack(side=tk.LEFT, padx=(4, 2))
        self.optstyle_var = tk.StringVar(value=label_of(OPT_LABELS, self.app.saved_cfg.get("option_style", "list")))
        ttk.Combobox(tb, textvariable=self.optstyle_var, values=list(OPT_LABELS), width=26,
                     state="readonly").pack(side=tk.LEFT)
        ttk.Button(tb, text="🔄 依此排版重新生成 Markdown", command=self.regen_markdown).pack(side=tk.LEFT, padx=6)
        self.md_area = scrolledtext.ScrolledText(fm, wrap=tk.WORD, font=("Microsoft JhengHei", 10), height=12)
        self.md_area.pack(fill=tk.BOTH, expand=True)
        self.md_area.bind("<KeyRelease>", lambda e: self._schedule_preview())

        # 右：元數據
        right = ttk.LabelFrame(paned, text="🤖 文檔總覽與 AI 元數據", padding=8)
        paned.add(right, weight=3)
        right.columnconfigure(1, weight=1)

        def info(row, label, var, **kw):
            ttk.Label(right, text=label).grid(row=row, column=0, sticky=tk.W, pady=3)
            ttk.Label(right, textvariable=var, foreground=kw.get("fg", "#334155"),
                      font=kw.get("font")).grid(row=row, column=1, sticky=tk.W, pady=3)

        self.name_var, self.size_var = tk.StringVar(value="-"), tk.StringVar(value="-")
        self.total_var, self.md5_var = tk.StringVar(value="0 頁"), tk.StringVar(value="-")
        info(0, "原始檔名:", self.name_var)
        info(1, "檔案大小 / 類型:", self.size_var)
        info(2, "文檔總頁數:", self.total_var, fg="#dc2626", font=("Microsoft JhengHei", 10, "bold"))
        ttk.Label(right, text="檔案 MD5:").grid(row=3, column=0, sticky=tk.W, pady=3)
        ttk.Entry(right, textvariable=self.md5_var, state="readonly").grid(row=3, column=1, sticky="ew", pady=3)
        ttk.Separator(right, orient=tk.HORIZONTAL).grid(row=4, column=0, columnspan=2, sticky="ew", pady=8)

        ttk.Label(right, text="📌 文檔標題 (Title):").grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=2)
        self.title_var = tk.StringVar()
        e1 = ttk.Entry(right, textvariable=self.title_var)
        e1.grid(row=6, column=0, columnspan=2, sticky="ew", pady=2)
        e1.bind("<KeyRelease>", lambda e: self._schedule_preview())
        ttk.Label(right, text="🏷️ 關鍵詞 (逗號分隔):").grid(row=7, column=0, columnspan=2, sticky=tk.W, pady=2)
        self.kw_var = tk.StringVar()
        e2 = ttk.Entry(right, textvariable=self.kw_var)
        e2.grid(row=8, column=0, columnspan=2, sticky="ew", pady=2)
        e2.bind("<KeyRelease>", lambda e: self._schedule_preview())
        ttk.Label(right, text="📖 內容簡介 (Summary):").grid(row=9, column=0, columnspan=2, sticky=tk.W, pady=2)
        self.summary = scrolledtext.ScrolledText(right, wrap=tk.WORD, height=8, font=("Microsoft JhengHei", 9))
        self.summary.grid(row=10, column=0, columnspan=2, sticky="nsew", pady=2)
        self.summary.bind("<KeyRelease>", lambda e: self._schedule_preview())
        right.rowconfigure(10, weight=1)

    # ------------------------------------------------------------------ 背景執行輔助
    def _bg(self, work, done, busy_text):
        self.status_var.set(busy_text)

        def run():
            try:
                res, err = work(), None
            except Exception as e:  # noqa: BLE001
                res, err = None, e
            self.app.ui(lambda: done(res, err))
        threading.Thread(target=run, daemon=True).start()

    def _need_file(self, need_pages=False):
        if not self.path:
            messagebox.showinfo("提示", "請先導入一份文檔！")
            return False
        if need_pages and not self.pages:
            messagebox.showinfo("提示", "請先完成『轉 WebP & 提取文字』！")
            return False
        return True

    # ------------------------------------------------------------------ 導入 / 提取
    def import_file(self, file_path=None):
        if not file_path:
            file_path = filedialog.askopenfilename(
                title="選擇要處理的文檔",
                filetypes=[("支援的文檔", "*.pdf *.docx *.pptx *.xlsx *.doc *.ppt *.xls"),
                           ("PDF 文檔", "*.pdf"), ("所有檔案", "*.*")])
        if not file_path or not os.path.exists(file_path):
            return
        self.path = file_path
        self.size = os.path.getsize(file_path)
        self.md5 = md5_file(file_path)
        name = os.path.basename(file_path)
        ext = os.path.splitext(name)[1].lstrip(".").lower()
        self.name_var.set(name)
        self.size_var.set(f"{fmt_size(self.size)}  |  類型: .{ext}")
        self.md5_var.set(self.md5)
        self.title_var.set(os.path.splitext(name)[0])
        self.kw_var.set("")
        self.summary.delete("1.0", tk.END)
        self.run_extract(force=False)

    def run_extract(self, force=False):
        if not self._need_file():
            return
        cfg, path, md5 = self.app.get_cloud_cfg(), self.path, self.md5
        pic_dir = cfg.get("dir_pic") or "/d/file/pic"

        def work():
            res = PageExtractor(cfg, self.app.log).extract_document(path, pic_dir, md5, force=force)
            return res, fallback_metadata(res.pages, os.path.basename(path))

        def done(res, err):
            if err:
                self.status_var.set("❌ 提取失敗")
                (messagebox.showwarning if isinstance(err, UnsupportedFormat) else messagebox.showerror)(
                    "提取失敗", str(err))
                return
            doc, fb = res
            self.pages, self.total = doc.pages, doc.total
            self.stream, self.questions, self.figures = doc.stream, doc.questions, doc.figures
            set_text(self.md_area, doc.markdown)
            self.total_var.set(f"{self.total} 頁 (WebP {len(self.pages)} 張 / 插圖 {len(self.figures)} 張 / 題目 {len(self.questions)} 題)")
            if not self.summary.get("1.0", tk.END).strip():
                self.summary.insert("1.0", fb.get("summary", ""))
            self.listbox.delete(0, tk.END)
            for p in self.pages:
                self.listbox.insert(tk.END, f"第 {p['page']:02d} 頁 - {p['webp_name']} ({len(p.get('content', ''))} 字)")
            if self.pages:
                self.listbox.selection_set(0)
                self.on_page_select(None)
            self.refresh_preview()
            self.status_var.set(f"✅ 提取完成：共 {self.total} 頁，WebP 已保存至 {cfg.get('dir_pic')}")

        self._bg(work, done, "⏳ 正在將文檔逐頁轉為 WebP 並提取文字...")

    # ------------------------------------------------------------------ 頁面瀏覽
    def _step(self, delta):
        sel = self.listbox.curselection()
        if not self.pages:
            return
        i = max(0, min(len(self.pages) - 1, (sel[0] if sel else 0) + delta))
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(i)
        self.listbox.see(i)
        self.on_page_select(None)

    def on_page_select(self, _event):
        sel = self.listbox.curselection()
        if not sel or sel[0] >= len(self.pages):
            return
        p = self.pages[sel[0]]
        self.page_text.delete("1.0", tk.END)
        self.page_text.insert("1.0", p.get("content", ""))
        img_path = p.get("webp_local_path")
        if img_path and os.path.exists(img_path):
            try:
                with Image.open(img_path) as im:
                    im = im.copy()
                w = max(self.preview.winfo_width(), 320)
                h = max(self.preview.winfo_height(), 380)
                im.thumbnail((w - 16, h - 16), Image.Resampling.LANCZOS)
                self.tk_img = ImageTk.PhotoImage(im)
                self.preview.config(image=self.tk_img, text="")
            except Exception as e:  # noqa: BLE001
                self.preview.config(image="", text=f"預覽失敗: {e}")

    def _on_text_edited(self, _event):
        sel = self.listbox.curselection()
        if sel and sel[0] < len(self.pages):
            self.pages[sel[0]]["content"] = self.page_text.get("1.0", tk.END).strip()
            self._schedule_preview()

    # ------------------------------------------------------------------ AI
    def run_ai(self):
        if not self._need_file(need_pages=True):
            return
        cfg, pages, total, name = self.app.get_cloud_cfg(), self.pages, self.total, self.name_var.get()

        def done(res, err):
            if err:
                self.status_var.set("❌ 阿里雲分析失敗")
                messagebox.showerror("阿里雲智能體錯誤", str(err))
                return
            if res.get("title"):
                self.title_var.set(res["title"])
            if res.get("keywords"):
                self.kw_var.set(", ".join(res["keywords"]))
            if res.get("summary"):
                self.summary.delete("1.0", tk.END)
                self.summary.insert("1.0", res["summary"])
            self.refresh_preview()
            self.status_var.set("✅ 分析完成，已自動填入標題、簡介與關鍵詞！")

        self._bg(lambda: AliyunAnalyzer(cfg, self.app.log).analyze(pages, total, name), done,
                 "🤖 正在呼叫阿里雲分析標題、簡介與關鍵詞...")

    # ------------------------------------------------------------------ JSON
    def build_json(self):
        if not self.path:
            return {}
        cfg = self.app.get_cloud_cfg()
        ext = os.path.splitext(self.path)[1].lstrip(".").lower() or "pdf"
        kws = [k.strip() for k in self.kw_var.get().replace("，", ",").split(",") if k.strip()]
        return build_json_payload(
            title=self.title_var.get().strip() or self.name_var.get(),
            summary=self.summary.get("1.0", tk.END).strip(), keywords=kws, file_size=self.size, ext=ext,
            total_pages=self.total, md5=self.md5, orig_name=self.name_var.get(), local_file_path=self.path,
            pages_data=self.pages, public_domain=cfg.get("r2_public_domain", ""),
            markdown=self.md_area.get("1.0", tk.END).strip(), figures=self.figures)

    def _schedule_preview(self):
        """輸入時延遲 300ms 才重繪 JSON，長文檔不會每按一鍵就卡頓。"""
        if self._preview_job:
            self.app.root.after_cancel(self._preview_job)
        self._preview_job = self.app.root.after(300, self.refresh_preview)

    def refresh_preview(self):
        self._preview_job = None
        data = self.build_json()
        self.json_area.delete("1.0", tk.END)
        if data:
            self.json_area.insert("1.0", json.dumps(data, ensure_ascii=False, indent=2))

    def save_json_local(self):
        if not self._need_file(need_pages=True):
            return None
        cfg = self.app.get_cloud_cfg()
        path = save_json(self.build_json(), cfg.get("dir_json") or "/d/file/json", self.md5,
                         shard=bool(cfg.get("shard_storage", True)))
        self.status_var.set(f"💾 JSON 已保存至: {path}")
        return path

    # ------------------------------------------------------------------ R2
    def run_upload(self):
        json_path = self.save_json_local()
        if not json_path:
            return
        cfg, path, md5, pages = self.app.get_cloud_cfg(), self.path, self.md5, list(self.pages)
        figures = list(self.figures)
        ext = os.path.splitext(path)[1].lstrip(".").lower() or "pdf"

        def done(stats, err):
            if err:
                self.status_var.set("❌ R2 上傳失敗")
                messagebox.showerror("R2 上傳失敗", str(err))
            else:
                self.status_var.set("☁️ 已成功同步至 Cloudflare R2")
                messagebox.showinfo("上傳完成", f"原檔 + {len(pages)} 張 WebP + JSON 已同步至 R2\n"
                                              f"(新傳 {stats['uploaded']} / 跳過已存在 {stats['skipped']})")

        self._bg(lambda: DocPipeline(cfg, self.app.log).upload_document(path, md5, ext, pages, json_path, figures),
                 done, "☁️ 正在上傳原檔、WebP 圖片與 JSON 至 Cloudflare R2...")

    # ------------------------------------------------------------ 發布帝國 CMS（步驟⑤）
    def run_publish(self):
        if not self._need_file(need_pages=True):
            return
        cfg = self.app.get_cloud_cfg()
        if not (cfg.get("publish_api_url") or "").strip():
            messagebox.showinfo("提示", "請先在『📤 發布接口（帝國CMS）』頁設定接口地址。")
            return
        payload = self.build_json()

        def done(res, err):
            if err:
                self.status_var.set("❌ 帝國CMS 發布失敗")
                messagebox.showerror("發布失敗", f"{type(err).__name__}: {err}")
            elif res.get("skipped"):
                self.status_var.set("📤 發布跳過")
                messagebox.showinfo("發布跳過", res.get("reason", "未設定接口地址"))
            elif res.get("success"):
                self.status_var.set(f"📤 發布成功 (HTTP {res['status_code']})")
                extra = f"\n縮略圖: {res['thumb_rel']}" if res.get("thumb_rel") else ""
                messagebox.showinfo("發布成功",
                                    f"已發布至帝國CMS\nHTTP {res['status_code']}（命中成功標記）{extra}")
            else:
                self.status_var.set(f"⚠️ 發布未成功 (HTTP {res.get('status_code')})")
                messagebox.showwarning("發布未成功",
                                       f"HTTP {res.get('status_code')}，回應未命中任何成功標記：\n"
                                       f"{res.get('text', '')[:300]}")

        self._bg(lambda: DocPipeline(cfg, self.app.log).publish_document(payload, self.pages, self.figures),
                 done, "📤 正在發布至帝國 CMS...")

    # ------------------------------------------------------------------ Markdown 重新生成 / OCR
    def regen_markdown(self):
        """用已解析的條目串流，依選擇的選項排版（逐行並排 / 單行並排 / 智能）重新生成 Markdown，不必重跑 OCR。"""
        if not self.stream:
            messagebox.showinfo("提示", "目前沒有可用的版面條目（Office 純文字備援模式不支援），請先導入 PDF 並提取。")
            return
        style = OPT_LABELS.get(self.optstyle_var.get(), "list")
        md, qs = build_markdown(self.stream, style)
        self.questions = qs
        set_text(self.md_area, md)
        self.refresh_preview()
        self.status_var.set(f"✅ 已重新生成 Markdown（{self.optstyle_var.get()}，{len(qs)} 題）")

    def run_ocr_page(self):
        if not self._need_file(need_pages=True):
            return
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showinfo("提示", "請先在頁面列表選擇要 OCR 的頁面！")
            return
        idx = sel[0]
        page = self.pages[idx]
        cfg = self.app.get_cloud_cfg()

        def done(text, err):
            if isinstance(err, OcrUnavailable):
                self.status_var.set("❌ OCR 不可用")
                messagebox.showwarning("OCR 不可用", f"{err}\n\n安裝：pip install paddlepaddle paddleocr")
            elif err:
                self.status_var.set("❌ OCR 失敗")
                messagebox.showerror("OCR 失敗", str(err))
            else:
                page["content"] = text
                self.listbox.selection_clear(0, tk.END)
                self.listbox.selection_set(idx)
                self.on_page_select(None)
                self.refresh_preview()
                self.status_var.set(f"✅ 第 {page['page']} 頁 OCR 完成（{len(text)} 字）")

        self._bg(lambda: PageExtractor(cfg, self.app.log).ocr_page_text(
                     page["webp_local_path"], page["page"], page.get("render_zoom")),
                 done, f"🔍 正在用 {cfg.get('ocr_engine', 'paddle')} 識別第 {page['page']} 頁...")
