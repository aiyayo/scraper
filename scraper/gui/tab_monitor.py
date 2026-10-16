# gui/tab_monitor.py
"""選項卡 5：數據與任務監控中心（總覽 / 關鍵字 / 網站 / 成功 / 失敗 / 後處理），含右鍵管理動作。"""
import os
import webbrowser
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

from core.utils import fmt_size, fmt_dur, open_path
from gui.widgets import (make_tree, fill_tree, add_context_menu, selected_meta, set_text)

TAB_OVERVIEW, TAB_KW, TAB_DOMAIN, TAB_OK, TAB_FAIL, TAB_PROC = range(6)
_PROC_LABEL = {"done": "完成", "fail": "失敗", "unsupported": "不支援", "pending": "待處理", None: "待處理"}
_R2_LABEL = {"done": "✅ 已上傳", "skipped": "跳過", "fail": "❌ 失敗", None: "-"}
_PUB_LABEL = {"done": "✅ 發布成功", "skipped": "跳過", "fail": "❌ 未成功", None: "-"}


class MonitorTab:
    def __init__(self, app, parent):
        self.app = app
        self._last_sig = None

        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=2)
        ttk.Label(bar, text="🔎 即時篩選 (網址 / 標題 / 網站 / 狀態):").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.filter_var, width=32).pack(side=tk.LEFT, padx=6)
        self.filter_var.trace_add("write", lambda *a: self.invalidate())
        ttk.Button(bar, text="🔄 立即刷新", command=lambda: self.refresh(force=True)).pack(side=tk.RIGHT, padx=4)

        self.nb = ttk.Notebook(parent)
        self.nb.pack(fill=tk.BOTH, expand=True, pady=4)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.refresh(force=True))

        f_ov = ttk.Frame(self.nb)
        self.nb.add(f_ov, text="📌 總覽統計")
        self.overview = scrolledtext.ScrolledText(f_ov, state="disabled", font=("Consolas", 10))
        self.overview.pack(fill=tk.BOTH, expand=True)

        self.kw_tree = self._tab("🔑 關鍵字", [
            ("kw", "關鍵字", 200, tk.W), ("ext", "格式", 50, tk.CENTER), ("st", "狀態", 70, tk.CENTER),
            ("start", "開始時間", 130, tk.CENTER), ("end", "結束時間", 130, tk.CENTER), ("dur", "耗時", 70, tk.CENTER),
            ("pages", "頁數", 50, tk.CENTER), ("found", "發現連結", 70, tk.CENTER), ("new", "新增", 55, tk.CENTER),
            ("ok", "成功", 55, tk.CENTER), ("dup", "重複", 55, tk.CENTER), ("fail", "失敗", 55, tk.CENTER),
            ("pend", "待處理", 60, tk.CENTER), ("skip", "略過", 55, tk.CENTER), ("runs", "次數", 45, tk.CENTER),
            ("err", "備註", 160, tk.W)])
        self.domain_tree = self._tab("🌐 網站", [
            ("domain", "網站 (Domain)", 260, tk.W), ("ok", "成功", 60, tk.CENTER), ("dup", "重複", 60, tk.CENTER),
            ("fail", "失敗", 60, tk.CENTER), ("skip", "略過", 60, tk.CENTER), ("pend", "待處理", 65, tk.CENTER),
            ("total", "總計", 60, tk.CENTER), ("rate", "成功率", 70, tk.CENTER), ("size", "容量", 80, tk.CENTER),
            ("last", "最近更新", 130, tk.CENTER)])
        self.ok_tree = self._tab("✅ 成功下載", [
            ("url", "來源網址", 350, tk.W), ("domain", "網站", 140, tk.W), ("file", "本地檔案", 230, tk.W),
            ("size", "大小", 70, tk.CENTER), ("kw", "關鍵字", 120, tk.W), ("time", "時間", 130, tk.CENTER)])
        self.fail_tree = self._tab("❌ 失敗", [
            ("url", "目標網址", 350, tk.W), ("domain", "網站", 140, tk.W), ("type", "類型", 80, tk.CENTER),
            ("err", "錯誤訊息", 260, tk.W), ("cnt", "失敗次數", 70, tk.CENTER), ("next", "下次重試", 130, tk.CENTER),
            ("kw", "關鍵字", 110, tk.W)])
        f_proc = ttk.Frame(self.nb)
        self.nb.add(f_proc, text="🧾 後處理結果")
        self.proc_tree = self._build_proc(f_proc)

        add_context_menu(self.domain_tree, [
            ("🔎 只看此網站", self._filter_domain),
            ("♻️ 重置此網站所有失敗項為待下載", self._reset_domain),
        ])
        add_context_menu(self.ok_tree, [
            ("📄 在文檔工作台打開", self._ok_open_workbench),
            ("📂 開啟本地檔案", self._ok_open_file),
            ("🌐 開啟來源網址", lambda: self._open_urls(self.ok_tree)),
            None,
            ("🔁 重新下載（標為待下載）", lambda: self._reset(self.ok_tree)),
            ("🗑 刪除記錄", lambda: self._delete(self.ok_tree)),
        ], on_double=self._ok_open_workbench)
        add_context_menu(self.fail_tree, [
            ("🌐 開啟網址", lambda: self._open_urls(self.fail_tree)),
            ("📋 複製網址", lambda: self._copy_urls(self.fail_tree)),
            None,
            ("♻️ 重置為待下載", lambda: self._reset(self.fail_tree)),
            ("🗑 刪除記錄", lambda: self._delete(self.fail_tree)),
        ], on_double=lambda: self._open_urls(self.fail_tree))

    # ------------------------------------------------------------------ 建構輔助
    def _tab(self, title, cols):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=title)
        return make_tree(f, cols)

    def _build_proc(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=2)
        ttk.Label(bar, text="💡 雙擊列可在『📄 4. 統一文檔處理』中打開；右鍵可開啟 JSON / 標為待處理").pack(side=tk.LEFT)
        ttk.Button(bar, text="♻️ 全部重新執行後處理", command=self.app.reprocess_all).pack(side=tk.RIGHT, padx=4)
        tree = make_tree(parent, [
            ("file", "原始檔名", 180, tk.W), ("title", "AI 提取標題", 200, tk.W), ("ext", "格式", 45, tk.CENTER),
            ("size", "大小", 65, tk.CENTER), ("pages", "總頁數", 80, tk.CENTER), ("st", "生成狀態", 60, tk.CENTER),
            ("r2", "Cloudflare", 90, tk.CENTER), ("pub", "帝國CMS發布", 100, tk.CENTER),
            ("msg", "步驟執行結果", 190, tk.W), ("time", "處理時間", 130, tk.CENTER),
            ("json", "JSON 路徑", 220, tk.W), ("path", "檔案路徑", 220, tk.W), ("url", "來源網址", 180, tk.W)])
        add_context_menu(tree, [
            ("📄 在文檔工作台打開", self._proc_open_workbench),
            ("🧾 開啟 JSON 檔案", self._proc_open_json),
            None,
            ("🔁 標記為待處理", self._proc_reprocess),
        ], on_double=self._proc_open_workbench)
        return tree

    # ------------------------------------------------------------------ 右鍵動作
    def _urls(self, tree):
        return [m["url"] for m in selected_meta(tree)]

    def _open_urls(self, tree):
        for u in self._urls(tree)[:5]:
            if not u.startswith("local://"):
                webbrowser.open(u)

    def _copy_urls(self, tree):
        urls = self._urls(tree)
        if urls:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append("\n".join(urls))

    def _reset(self, tree):
        urls = self._urls(tree)
        if urls and self.app.db:
            self.app.db.reset_urls(urls)
            self.refresh(force=True)

    def _delete(self, tree):
        urls = self._urls(tree)
        if urls and self.app.db and messagebox.askyesno(
                "確認", f"從資料庫刪除 {len(urls)} 筆記錄？（不會刪除本地檔案）"):
            self.app.db.delete_urls(urls)
            self.refresh(force=True)

    def _ok_open_file(self):
        for m in selected_meta(self.ok_tree)[:3]:
            if m.get("file_path") and os.path.exists(m["file_path"]):
                err = open_path(m["file_path"])
                if err:
                    messagebox.showwarning("開啟失敗", err)

    def _ok_open_workbench(self):
        for m in selected_meta(self.ok_tree)[:1]:
            self.app.open_in_workbench(m.get("file_path"))

    def _filter_domain(self):
        metas = selected_meta(self.domain_tree)
        if metas:
            self.filter_var.set(metas[0]["domain"] or "")

    def _reset_domain(self):
        metas = selected_meta(self.domain_tree)
        if metas and self.app.db:
            n = self.app.db.reset_failed(metas[0]["domain"])
            self.app.log(f"♻️ 已重置 {metas[0]['domain']} 的 {n} 筆失敗項")
            self.refresh(force=True)

    def _proc_open_workbench(self):
        for m in selected_meta(self.proc_tree)[:1]:
            self.app.open_in_workbench(m.get("file_path"))

    def _proc_open_json(self):
        for m in selected_meta(self.proc_tree)[:5]:
            if m.get("json_path") and os.path.exists(m["json_path"]):
                open_path(m["json_path"])

    def _proc_reprocess(self):
        urls = self._urls(self.proc_tree)
        if urls and self.app.db:
            self.app.db.reset_proc(urls)
            self.refresh(force=True)

    # ------------------------------------------------------------------ 重繪
    def invalidate(self):
        self._last_sig = None

    def _rel(self, fp):
        root = self.app.down_dir()
        try:
            rel = os.path.relpath(fp, root)
            return fp if rel.startswith("..") else rel.replace(os.sep, "/")
        except ValueError:
            return fp

    def refresh(self, force=False):
        db = self.app.db
        if not db:
            return
        try:
            st = db.stats()
            ok, dup, fail, pend, skip = (st.get(k, 0) for k in ("ok", "dup", "fail", "pending", "skip"))
            ps = db.proc_stats()
            self.app.status_var.set(
                f"📥 下載成功 {ok} | 重複 {dup} | 失敗 {fail} | 待下載 {pend} | "
                f"🧾 後處理完成 {ps.get('done', 0)}/{ok} (累計總頁數 {ps.get('_pages', 0)} 頁) | 容量 {fmt_size(db.total_bytes())}")
            nb = self.nb
            nb.tab(TAB_KW, text=f"🔑 關鍵字 ({db.count_keywords()})")
            nb.tab(TAB_DOMAIN, text=f"🌐 網站 ({db.count_domains()})")
            nb.tab(TAB_OK, text=f"✅ 成功下載 ({ok + dup})")
            nb.tab(TAB_FAIL, text=f"❌ 失敗 ({fail})")
            nb.tab(TAB_PROC, text=f"🧾 後處理結果 ({ps.get('done', 0)}/{ok})")

            tab = nb.index("current")
            flt = self.filter_var.get().strip()
            sig = (db.version, tab, flt)
            if not force and sig == self._last_sig:
                return
            self._last_sig = sig
            {TAB_OVERVIEW: self._draw_overview, TAB_KW: self._draw_kw, TAB_DOMAIN: self._draw_domain,
             TAB_OK: self._draw_ok, TAB_FAIL: self._draw_fail, TAB_PROC: self._draw_proc}[tab](db, flt)
        except tk.TclError:
            pass

    def _draw_overview(self, db, flt):
        st = db.stats()
        ok, dup, fail, pend, skip = (st.get(k, 0) for k in ("ok", "dup", "fail", "pending", "skip"))
        total, done = ok + dup + fail + pend + skip, ok + dup + fail
        rate = f"{(ok + dup) * 100 / done:.1f}%" if done else "-"
        kws = db.keyword_stats()
        ps = db.proc_stats()
        lines = [
            "📌 資料庫總覽", "=" * 65,
            f"連結總數 {total} | 成功 {ok} | 重複 {dup} | 失敗 {fail} | 待處理 {pend} | 略過 {skip}",
            f"下載成功率 {rate} | 已儲存容量 {fmt_size(db.total_bytes())}",
            f"關鍵字共 {len(kws)} 個 | 累計發現連結 {sum((k['links_found'] or 0) for k in kws)}",
            "",
            f"🧾 後處理：已完成 {ps.get('done', 0)} | 待處理 {ps.get('pending', 0)} | "
            f"不支援 {ps.get('unsupported', 0)} | 失敗 {ps.get('fail', 0)} | 累計總頁數 {ps.get('_pages', 0)} 頁",
        ]
        errs = db.error_counts()
        if errs:
            lines += ["", "❌ 失敗原因分佈", "-" * 65]
            lines += [f"  {kind:<14} {n}" for kind, n in errs]
        set_text(self.overview, "\n".join(lines), readonly=True)

    def _draw_kw(self, db, flt):
        rows = [(k["keyword"], k["file_ext"] or "", k["status"], k["started_at"] or "-", k["finished_at"] or "-",
                 fmt_dur(k["duration_sec"]), k["pages_crawled"] or 0, k["links_found"] or 0, k["new_links"] or 0,
                 k["ok"], k["dup"], k["fail"], k["pending"], k["skip"], k["run_count"] or 0, k["last_error"] or "")
                for k in db.keyword_stats(flt)]
        fill_tree(self.kw_tree, rows)

    def _draw_domain(self, db, flt):
        rows, metas = [], []
        for d in db.domain_stats(flt):
            done = (d["ok"] or 0) + (d["dup"] or 0) + (d["fail"] or 0)
            rate = f"{((d['ok'] or 0) + (d['dup'] or 0)) * 100 / done:.0f}%" if done else "-"
            rows.append((d["domain"] or "未知", d["ok"], d["dup"], d["fail"], d["skip"], d["pending"],
                         d["total"], rate, fmt_size(d["bytes"]), d["last"] or ""))
            metas.append(d)
        fill_tree(self.domain_tree, rows, metas)

    def _draw_ok(self, db, flt):
        data = db.rows_by_status(("ok", "dup"), flt)
        rows = [(r["url"], r["domain"], self._rel(r["file_path"] or ""), fmt_size(r["size"]),
                 r["keyword"] or "", r["updated_at"]) for r in data]
        fill_tree(self.ok_tree, rows, data)

    def _draw_fail(self, db, flt):
        data = db.rows_by_status(("fail",), flt)
        rows = [(r["url"], r["domain"], r["err_type"] or "", r["err_msg"] or "", r["fail_count"],
                 r["next_retry_at"] or "停止重試", r["keyword"] or "") for r in data]
        fill_tree(self.fail_tree, rows, data)

    def _draw_proc(self, db, flt):
        data = db.proc_rows(flt)
        rows = [(r.get("orig_name") or os.path.basename(r["file_path"] or ""), r.get("ai_title") or "",
                 r["file_ext"] or "", fmt_size(r["size"]), r.get("total_pages") or r.get("image_count") or "",
                 _PROC_LABEL.get(r.get("proc_status"), r.get("proc_status")),
                 _R2_LABEL.get(r.get("r2_status"), r.get("r2_status") or "-"),
                 _PUB_LABEL.get(r.get("pub_status"), r.get("pub_status") or "-"),
                 r.get("proc_msg") or "", r.get("proc_at") or "",
                 r.get("json_path") or "", r["file_path"] or "", r["url"]) for r in data]
        fill_tree(self.proc_tree, rows, data)
