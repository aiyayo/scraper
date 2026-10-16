# gui/tab_publish.py
"""獨立選項卡：帝國 CMS 發布接口、動態欄位映射與文件自動歸檔備份。

這是流水線唯一的資料回傳出口（步驟⑤，R2 上傳後執行）：面向帝國 CMS ecmsinfo.php，
支援 form/json、utf-8/gbk、成功標記判斷、縮略圖規格與發布成功後歸檔。
"""
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from core.config import save_settings
from gui.widgets import spin, to_int
from processing.publish_client import (PublishClient, TAG_HELP, build_context, build_params)


class PublishTab:
    def __init__(self, app, parent):
        self.app = app
        saved = app.saved_cfg

        frame = ttk.LabelFrame(parent, text=" 帝國 CMS 發布接口、字段映射與文件自動歸檔備份 ", padding=15)
        frame.pack(fill="both", expand=True, padx=15, pady=10)

        top_conn_frame = ttk.Frame(frame)
        top_conn_frame.pack(fill="x", pady=5)

        ttk.Label(top_conn_frame, text="發布接口地址 (URL):").grid(row=0, column=0, sticky="w", pady=4)
        self.post_url_entry = ttk.Entry(top_conn_frame, width=72)
        self.post_url_entry.insert(0, saved.get("publish_api_url",
                                                "https://www.imomomenu.com/e/admin/ecmsinfo.php"))
        self.post_url_entry.grid(row=0, column=1, columnspan=3, sticky="w", pady=4, padx=5)

        ttk.Label(top_conn_frame, text="數據提交格式:").grid(row=1, column=0, sticky="w", pady=4)
        self.post_type_combo = ttk.Combobox(
            top_conn_frame, width=20, values=["form (表單 x-www-form-urlencoded)", "json"], state="readonly")
        cur_fmt = "form (表單 x-www-form-urlencoded)" if saved.get("publish_post_type", "form") == "form" else "json"
        self.post_type_combo.set(cur_fmt)
        self.post_type_combo.grid(row=1, column=1, sticky="w", pady=4, padx=5)

        ttk.Label(top_conn_frame, text="頁面編碼:").grid(row=1, column=2, sticky="w", pady=4, padx=(10, 2))
        self.encoding_combo = ttk.Combobox(top_conn_frame, width=10, values=["utf-8", "gbk"], state="readonly")
        self.encoding_combo.set(saved.get("publish_encoding", "utf-8"))
        self.encoding_combo.grid(row=1, column=3, sticky="w", pady=4)

        ttk.Label(top_conn_frame, text="縮略圖尺寸規格:").grid(row=2, column=0, sticky="w", pady=4)
        thumb_sub = ttk.Frame(top_conn_frame)
        thumb_sub.grid(row=2, column=1, sticky="w", pady=4, padx=5)
        self.thumb_w_entry = ttk.Entry(thumb_sub, width=6)
        self.thumb_w_entry.insert(0, str(saved.get("publish_thumb_width", 312)))
        self.thumb_w_entry.pack(side="left")
        ttk.Label(thumb_sub, text=" x ").pack(side="left")
        self.thumb_h_entry = ttk.Entry(thumb_sub, width=6)
        self.thumb_h_entry.insert(0, str(saved.get("publish_thumb_height", 208)))
        self.thumb_h_entry.pack(side="left")
        ttk.Label(thumb_sub, text=" 像素 (相對路徑: /d/file/pic/月日/文件名)").pack(side="left", padx=5)

        self.timeout_var = tk.StringVar(value=str(saved.get("publish_timeout", 20)))
        to_f = ttk.Frame(top_conn_frame)
        to_f.grid(row=2, column=2, sticky="w", pady=4)
        spin(to_f, "逾時(秒):", self.timeout_var, 5, 300, 5)
        ttk.Button(top_conn_frame, text="🔌 用範例資料測試發布",
                   command=self.test_publish).grid(row=2, column=3, sticky="e", padx=6)

        ttk.Label(top_conn_frame, text="成功返回標記:").grid(row=3, column=0, sticky="w", pady=4)
        self.success_kw_entry = ttk.Entry(top_conn_frame, width=72)
        kw_list = saved.get("publish_success_keywords", ["发送成功", "编辑成功", "增加信息成功", "SUCCESS"])
        self.success_kw_entry.insert(0, ", ".join(kw_list if isinstance(kw_list, list) else []))
        self.success_kw_entry.grid(row=3, column=1, columnspan=3, sticky="w", pady=4, padx=5)

        # 文件自動備份歸檔目錄與刪除選項
        ttk.Label(top_conn_frame, text="發布成功歸檔目標根目錄:").grid(row=4, column=0, sticky="w", pady=4)
        archive_sub = ttk.Frame(top_conn_frame)
        archive_sub.grid(row=4, column=1, columnspan=3, sticky="w", pady=4, padx=5)
        self.archive_dir_entry = ttk.Entry(archive_sub, width=52)
        self.archive_dir_entry.insert(0, saved.get("publish_archive_dir", ""))
        self.archive_dir_entry.pack(side="left")
        ttk.Button(archive_sub, text="📁 瀏覽選擇", command=self.choose_archive_dir).pack(side="left", padx=5)
        self.del_source_var = tk.BooleanVar(value=saved.get("publish_delete_source", False))
        ttk.Checkbutton(archive_sub, text="歸檔後刪除本地原始文件",
                        variable=self.del_source_var).pack(side="left", padx=5)

        list_box = ttk.LabelFrame(frame, text=" 動態發布字段映射表 (選中即可修改或刪除) ", padding=8)
        list_box.pack(fill="both", expand=True, pady=8)

        cols = ("param_name", "param_val")
        self.param_tree = ttk.Treeview(list_box, columns=cols, show="headings", height=7)
        self.param_tree.heading("param_name", text="POST 字段名稱 (例如 title, classid, smalltext, titlepic, newstext)")
        self.param_tree.heading("param_val", text="對應標籤或固定值 (如 [标题], [内容], [缩略图路径], 1)")
        self.param_tree.column("param_name", width=280)
        self.param_tree.column("param_val", width=460)
        self.param_tree.pack(side="left", fill="both", expand=True)

        pt_scroll = ttk.Scrollbar(list_box, orient="vertical", command=self.param_tree.yview)
        self.param_tree.configure(yscrollcommand=pt_scroll.set)
        pt_scroll.pack(side="right", fill="y")
        self.param_tree.bind("<<TreeviewSelect>>", self.on_select_param_row)

        edit_bar = ttk.Frame(frame)
        edit_bar.pack(fill="x", pady=4)
        ttk.Label(edit_bar, text="字段名:").pack(side="left", padx=2)
        self.edit_key_entry = ttk.Entry(edit_bar, width=18)
        self.edit_key_entry.pack(side="left", padx=4)
        ttk.Label(edit_bar, text="綁定標籤/值:").pack(side="left", padx=2)
        self.edit_val_entry = ttk.Entry(edit_bar, width=34)
        self.edit_val_entry.pack(side="left", padx=4)
        ttk.Button(edit_bar, text="➕ 添加/更新此字段", command=self.add_or_update_param_field).pack(side="left", padx=4)
        ttk.Button(edit_bar, text="❌ 刪除選中字段", command=self.delete_selected_param_field).pack(side="left", padx=4)

        ttk.Label(frame, text=TAG_HELP, foreground="#555555", justify="left").pack(anchor="w", pady=4)

        save_btn = ttk.Button(frame, text="💾 保存全部發布配置與字段映射", command=self.save_post_settings)
        save_btn.pack(anchor="e", pady=5)

        self.load_params_to_tree(dict(saved.get("publish_params", {})))

    # ------------------------------------------------------------------ 映射表
    def load_params_to_tree(self, params=None):
        for item in self.param_tree.get_children():
            self.param_tree.delete(item)
        for k, v in (params or {}).items():
            self.param_tree.insert("", "end", iid=str(k), values=(k, v))

    def _params_dict(self):
        """依 Treeview 順序收集映射（保住用戶的欄位先後）。"""
        out = {}
        for iid in self.param_tree.get_children():
            vals = self.param_tree.item(iid, "values")
            out[str(vals[0])] = str(vals[1])
        return out

    def on_select_param_row(self, event=None):
        sel = self.param_tree.selection()
        if not sel:
            return
        vals = self.param_tree.item(sel[0], "values")
        self.edit_key_entry.delete(0, tk.END)
        self.edit_key_entry.insert(0, vals[0])
        self.edit_val_entry.delete(0, tk.END)
        self.edit_val_entry.insert(0, vals[1])

    def add_or_update_param_field(self):
        k = self.edit_key_entry.get().strip()
        v = self.edit_val_entry.get().strip()
        if not k:
            messagebox.showwarning("提示", "字段名稱不能為空！")
            return
        params = self._params_dict()
        params[k] = v
        self.load_params_to_tree(params)
        self.edit_key_entry.delete(0, tk.END)
        self.edit_val_entry.delete(0, tk.END)

    def delete_selected_param_field(self):
        sel = self.param_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "請在列表中選中需要刪除的字段！")
            return
        params = self._params_dict()
        params.pop(str(sel[0]), None)
        self.load_params_to_tree(params)
        self.edit_key_entry.delete(0, tk.END)
        self.edit_val_entry.delete(0, tk.END)

    # ------------------------------------------------------------------ 收集/保存
    def collect(self):
        kw_list = [k.strip() for k in self.success_kw_entry.get().strip().split(",") if k.strip()]
        return {
            "publish_api_url": self.post_url_entry.get().strip(),
            "publish_post_type": "json" if self.post_type_combo.get().startswith("json") else "form",
            "publish_encoding": self.encoding_combo.get().strip() or "utf-8",
            "publish_timeout": to_int(self.timeout_var, 20, 1, 300),
            "publish_thumb_width": to_int(self.thumb_w_entry, 312, 1, 4000),
            "publish_thumb_height": to_int(self.thumb_h_entry, 208, 1, 4000),
            "publish_success_keywords": kw_list,
            "publish_archive_dir": self.archive_dir_entry.get().strip(),
            "publish_delete_source": self.del_source_var.get(),
            "publish_params": self._params_dict(),
        }

    def save_post_settings(self):
        cfg = self.collect()
        if not cfg["publish_params"]:
            messagebox.showwarning("提示", "字段映射表不可全空，至少保留一個字段！")
            return
        if save_settings(cfg):
            self.app.saved_cfg.update(cfg)
            messagebox.showinfo("成功", "帝國 CMS 發布配置、歸檔設置與字段映射已成功保存！")

    def choose_archive_dir(self):
        d = filedialog.askdirectory(title="選擇發布成功後的歸檔根目錄（會在其下建立 d/file/pic/月日/）")
        if d:
            self.archive_dir_entry.delete(0, tk.END)
            self.archive_dir_entry.insert(0, d)

    # ------------------------------------------------------------------ 測試發布
    def test_publish(self):
        cfg = self.collect()
        if not cfg["publish_api_url"]:
            messagebox.showinfo("提示", "請先填寫發布接口 URL。")
            return
        sample = {
            "title": "範例文件標題", "summary": "這是一筆用來測試帝國 CMS 發布接口的範例摘要。",
            "keywords": ["範例", "發布測試"], "file_size": 204800, "file_type": "pdf",
            "total_pages": 3, "page_count": 3, "md5": "0123456789abcdef0123456789abcdef",
            "file_path": "/d/file/down/sample.pdf",
            "markdown": "# 範例文件標題\n這是 **第一段** 正文，下圖帶寬高屬性。\n"
                        "![第1頁插圖](https://example.com/d/flie/pic/sample_p1.webp)\n"
                        "- 選項一\n- 選項二",
            "pages": [{"page": 1, "image": "https://example.com/d/flie/pic/sample_p1.webp",
                       "width": 1000, "height": 1400, "content": "", "figures": []}],
        }
        extra = {"account": cfg.get("publish_account", ""),
                 "file_url": "https://example.com/d/file/down/sample.pdf",
                 "source_url": "https://example.com/orig/sample.pdf", "thumb_rel": ""}

        def work():
            ctx = build_context(sample, extra)
            params = build_params(cfg["publish_params"], ctx)
            return PublishClient(cfg).send(params)

        def done(res, err):
            if err:
                messagebox.showerror("發布失敗", f"{type(err).__name__}: {err}")
            elif res.get("skipped"):
                messagebox.showinfo("發布跳過", res.get("reason", "未設定 URL"))
            else:
                verdict = "✅ 命中成功標記" if res["success"] else "⚠️ 未命中任何成功標記"
                messagebox.showinfo("發布結果",
                                    f"HTTP {res['status_code']}｜{verdict}\n\n回應內容:\n{res['text'][:300]}")

        self._bg(work, done)

    def _bg(self, work, done):
        def runner():
            try:
                res = work()
                err = None
            except Exception as e:  # noqa: BLE001
                res, err = None, e
            self.app.ui(lambda: done(res, err))
        threading.Thread(target=runner, daemon=True).start()
