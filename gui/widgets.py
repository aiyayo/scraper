# gui/widgets.py
"""各選項卡共用的小控件與輔助函式。"""
import re
import tkinter as tk
from tkinter import ttk

FONT_UI = ("Microsoft JhengHei", 9)
FONT_UI_BOLD = ("Microsoft JhengHei", 9, "bold")


def spin(parent, label, var, lo, hi, width=5):
    ttk.Label(parent, text=label).pack(side=tk.LEFT, padx=(6, 2))
    ttk.Spinbox(parent, from_=lo, to=hi, textvariable=var, width=width).pack(side=tk.LEFT)


def to_int(var, default, lo, hi):
    """安全讀取 Spinbox / Entry 的整數值並限制範圍。"""
    try:
        return min(hi, max(lo, int(float(var.get()))))
    except (ValueError, tk.TclError):
        return default


def to_float(var, default, lo, hi):
    try:
        return min(hi, max(lo, float(var.get())))
    except (ValueError, tk.TclError):
        return default


def set_text(widget, text, readonly=False):
    widget.config(state="normal")
    widget.delete("1.0", tk.END)
    widget.insert("1.0", text)
    if readonly:
        widget.config(state="disabled")


# ---------------------------------------------------------------------- Treeview
def make_tree(parent, cols, sortable=True):
    """cols: [(id, 標題, 寬度, anchor)]；點標題可排序（數字欄位以數值排序）。"""
    frame = ttk.Frame(parent)
    frame.pack(fill=tk.BOTH, expand=True)
    tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings", selectmode="extended")
    for cid, title, width, anchor in cols:
        tree.heading(cid, text=title,
                     command=(lambda c=cid, t=tree: sort_tree(t, c)) if sortable else None)
        tree.column(cid, width=width, anchor=anchor, stretch=True)
    vs = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
    hs = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=tree.xview)
    tree.configure(yscroll=vs.set, xscroll=hs.set)
    vs.pack(side=tk.RIGHT, fill=tk.Y)
    hs.pack(side=tk.BOTTOM, fill=tk.X)
    tree.pack(fill=tk.BOTH, expand=True)
    tree._sort = None
    tree._meta = {}
    return tree


def sort_tree(tree, col):
    rev = bool(tree._sort and tree._sort[0] == col and not tree._sort[1])
    tree._sort = (col, rev)
    apply_sort(tree)


def apply_sort(tree):
    if not tree._sort:
        return
    col, rev = tree._sort

    def key(iid):
        v = str(tree.set(iid, col))
        num = re.sub(r"[^\d.\-]", "", v)
        try:
            return (0, float(num), v) if num not in ("", "-", ".") else (1, 0, v)
        except ValueError:
            return (1, 0, v)

    for i, iid in enumerate(sorted(tree.get_children(""), key=key, reverse=rev)):
        tree.move(iid, "", i)


def fill_tree(tree, rows, metas=None):
    """重繪列表並保留捲動位置與排序；metas 為每列附帶的原始資料（供右鍵選單取用）。"""
    y = tree.yview()[0]
    tree.delete(*tree.get_children())
    tree._meta = {}
    for i, r in enumerate(rows):
        iid = tree.insert("", tk.END, values=r)
        if metas is not None:
            tree._meta[iid] = metas[i]
    apply_sort(tree)
    tree.yview_moveto(y)


def selected_meta(tree):
    return [tree._meta[i] for i in tree.selection() if i in tree._meta]


def add_context_menu(tree, items, on_double=None):
    """items: [(標題, callback)]，None 代表分隔線。右鍵（含 macOS 的 Button-2）彈出。"""
    menu = tk.Menu(tree, tearoff=0)
    for it in items:
        if it is None:
            menu.add_separator()
        else:
            menu.add_command(label=it[0], command=it[1])

    def popup(ev):
        row = tree.identify_row(ev.y)
        if row and row not in tree.selection():
            tree.selection_set(row)
        if tree.selection():
            menu.tk_popup(ev.x_root, ev.y_root)

    tree.bind("<Button-3>", popup)
    tree.bind("<Button-2>", popup)
    if on_double:
        tree.bind("<Double-1>", lambda e: on_double())
    return menu


# ---------------------------------------------------------------------- 設定值 ↔ 顯示文字
OCR_LABELS = {"PaddleOCR (推薦)": "paddle", "Tesseract": "tesseract", "關閉": "off"}
OPT_LABELS = {"逐行並排 (每個選項一行)": "list", "單行並排 (選項同一行)": "inline",
              "智能 (短選項單行/長選項逐行)": "auto"}


def label_of(mapping, value):
    return next((k for k, v in mapping.items() if v == value), next(iter(mapping)))
