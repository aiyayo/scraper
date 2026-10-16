# gui/tab_ai.py
"""選項卡 2：阿里雲百煉智能體 / 視覺模型參數與自定義提示詞，含測試按鈕。"""
import os
import json
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext

from core.config import DEFAULT_PROMPT
from gui.widgets import to_int, set_text
from processing.ai_client import AliyunAnalyzer

class AITab:
    def __init__(self, app, parent):
        self.app = app
        saved = app.saved_cfg

        f = ttk.LabelFrame(parent, text="🤖 阿里雲百煉智能體 (Agent) 參數設定", padding=10)
        f.pack(fill=tk.X, pady=4)
        f.columnconfigure(1, weight=1)
        f.columnconfigure(3, weight=1)

        ttk.Label(f, text="DashScope API Key:").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.key_var = tk.StringVar(value=saved.get("aliyun_api_key", ""))
        ttk.Entry(f, textvariable=self.key_var, show="*", width=38).grid(row=0, column=1, sticky="ew", padx=6, pady=4)

        ttk.Label(f, text="百煉智能體 App ID:").grid(row=0, column=2, sticky=tk.W, padx=(12, 2), pady=4)
        self.appid_var = tk.StringVar(value=saved.get("aliyun_app_id", ""))
        ttk.Entry(f, textvariable=self.appid_var, width=38).grid(row=0, column=3, sticky="ew", padx=6, pady=4)

        opts = ttk.Frame(f)
        opts.grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        self.pages_var = tk.StringVar(value=str(saved.get("aliyun_max_sample_pages", 2)))
        self.retry_var = tk.StringVar(value=str(saved.get("aliyun_retry", 2)))
        self.side_var = tk.StringVar(value=str(saved.get("ai_image_max_side", 1280)))
        for text, var, lo, hi, w in (("代表頁圖片數:", self.pages_var, 1, 5, 4),
                                     ("失敗重試次數:", self.retry_var, 0, 6, 4),
                                     ("圖片最長邊(px):", self.side_var, 320, 4096, 6)):
            ttk.Label(opts, text=text).pack(side=tk.LEFT, padx=(8, 2))
            ttk.Spinbox(opts, from_=lo, to=hi, textvariable=var, width=w).pack(side=tk.LEFT)

        btns = ttk.Frame(f)
        btns.grid(row=1, column=2, columnspan=2, sticky="e", pady=4)
        ttk.Button(btns, text="🔄 恢復預設提示語", command=self._reset_prompt).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="⚡ 測試阿里雲回應", command=self._test).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="💾 保存配置", command=app.save_ui_settings).pack(side=tk.LEFT, padx=4)

        fp = ttk.LabelFrame(
            parent, padding=8,
            text="✍️ 自定義提示語 (Prompt) —— 變數：{orig_name} 原始檔名、{total_pages} 文檔總頁數、{text_preview} 提取文字預覽")
        fp.pack(fill=tk.BOTH, expand=True, pady=6)
        self.prompt_text = scrolledtext.ScrolledText(fp, wrap=tk.WORD, font=("Microsoft JhengHei", 10), height=14)
        self.prompt_text.pack(fill=tk.BOTH, expand=True)
        self.prompt_text.insert("1.0", saved.get("aliyun_prompt", DEFAULT_PROMPT))

        fo = ttk.LabelFrame(parent, text="🧪 測試輸出結果預覽", padding=6)
        fo.pack(fill=tk.X, pady=4)
        self.out = scrolledtext.ScrolledText(fo, wrap=tk.WORD, height=6, font=("Consolas", 9),
                                             bg="#0f172a", fg="#38bdf8")
        self.out.pack(fill=tk.X)

    # ------------------------------------------------------------------
    def collect(self):
        return {
            "aliyun_api_key": self.key_var.get().strip(),
            "aliyun_app_id": self.appid_var.get().strip(),
            "aliyun_max_sample_pages": to_int(self.pages_var, 2, 1, 5),
            "aliyun_retry": to_int(self.retry_var, 2, 0, 6),
            "ai_image_max_side": to_int(self.side_var, 1280, 320, 4096),
            "aliyun_prompt": self.prompt_text.get("1.0", tk.END).strip() or DEFAULT_PROMPT,
        }

    def _reset_prompt(self):
        set_text(self.prompt_text, DEFAULT_PROMPT)

    def _test(self):
        set_text(self.out, "⏳ 正在呼叫阿里雲進行測試，請稍候...\n")
        cfg = self.app.get_cloud_cfg()  # 在主執行緒先讀好所有 Tk 變數，背景執行緒不碰 GUI

        def work():
            try:
                from PIL import Image
                with tempfile.TemporaryDirectory(prefix="ai_test_") as td:
                    img = os.path.join(td, "_test.webp")
                    Image.new("RGB", (400, 300), (240, 248, 255)).save(img, "WEBP")
                    pages = [{"page": 1, "webp_local_path": img,
                              "content": "這是一份四年級下學期自然科學與生活科技領域的教學計畫表，包含昆蟲的一生、認識導電與通路等單元。"}]
                    res = AliyunAnalyzer(cfg).analyze(pages, total_pages=5, orig_name="四年級自然教學計畫.pdf")
                msg = "✅ 測試成功！解析結果：\n" + json.dumps(res, ensure_ascii=False, indent=2)
            except Exception as e:  # noqa: BLE001
                msg = f"❌ 測試失敗：\n{e}"
            self.app.ui(lambda: set_text(self.out, msg))

        threading.Thread(target=work, daemon=True).start()
