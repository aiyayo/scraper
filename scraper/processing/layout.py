# processing/layout.py
"""PDF 版面還原：把一頁 PDF 轉成「閱讀順序正確的乾淨文字」。

移植並精簡自 word.py 的 TaiwanExamLayoutEngine（去掉題目 / 選項的考卷專用結構解析，保留通用部分）：
  1. 行級抽取：保留字號 / 粗體 / 上下標；直排抬頭合併；斜向浮水印丟棄；偽粗體重影行去重
  2. 原生表格 → Markdown 表格（過濾誤檢的單行 / 單列「表格」）
  3. 插圖偵測（嵌入圖 + 向量圖叢集，相鄰合併）：輸出 figure 條目供擷取階段裁切存檔；插圖內的標籤文字（座標軸、角標等短字）不混入正文
  4. 跨頁重複的頁眉 / 頁腳 / 頁碼自動清除
  5. 閱讀順序：通欄元素分帶 + 帶內雙欄 / 多欄偵測；獨立成塊的題號 / 括號吸附到正文
  6. 折行還原成段落（可關閉）
幾何運算改用純元組 (x0, y0, x1, y1)，不依賴 fitz.Rect，方便在沒有 PyMuPDF 的環境做單元測試。
"""
import re
from collections import Counter

from processing.textutil import (SUP_O, SUP_C, SUB_O, SUB_C, clean_inline, join_text, ends_sentence,
                                 restore_marks, rows_to_markdown_table)


# ---------------------------------------------------------------------- 幾何（純元組）
def _tup(r):
    return (float(r[0]), float(r[1]), float(r[2]), float(r[3])) if not hasattr(r, "x0") \
        else (float(r.x0), float(r.y0), float(r.x1), float(r.y1))


def _area(r):
    return max(r[2] - r[0], 0.0) * max(r[3] - r[1], 0.0)


def _inter_area(a, b):
    return max(min(a[2], b[2]) - max(a[0], b[0]), 0.0) * max(min(a[3], b[3]) - max(a[1], b[1]), 0.0)


def _intersects(a, b):
    return _inter_area(a, b) > 0


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _mostly_inside(rect, rects, frac):
    area = max(_area(rect), 1.0)
    return any(_inter_area(rect, r) >= frac * area for r in rects)


_MARKER_START = re.compile(
    r"^([（(]\s*\d{1,3}\s*[)）]|\d{1,3}\s*[.、．)）]|[（(]\s*[A-Ea-e]?\s*[)）]|[①-⑩]|"
    r"[一二三四五六七八九十]{1,3}\s*[、．.]|第\s*\d{1,3}\s*[題题]|[•·\-－▪●○■□])")


class PdfLayout:
    PAGE_NO_RE = re.compile(
        r"^[\s\-—–~〜（(]*(第\s*)?\d{1,3}\s*(頁|页|/\s*\d{1,3})?[\s\-—–~〜）)]*$"
        r"|^第\s*\d+\s*頁(\s*[，,]?\s*共\s*\d+\s*頁)?$|^共\s*\d+\s*頁$")
    NOISE_RE = re.compile(r"背面尚有|請翻頁|翻頁繼續|請接續|背面還有|未完待續")
    MARKER_RE = re.compile(r"^[（(]?\s*[A-Ea-e√×○]?\s*[)）]?\s*\d{0,3}\s*[.、．)）]?$")

    def __init__(self, use_tables=True, normalize=True, join_lines=True, figures=True):
        self.use_tables = use_tables
        self.figures = figures
        self.normalize = normalize
        self.join_lines = join_lines

    # ------------------------------------------------------------------ 表格
    def _extract_tables(self, page, page_no):
        items, rects = [], []
        if not self.use_tables:
            return items, rects
        try:
            page_area = _area(_tup(page.rect))
            for tab in page.find_tables().tables:
                rect = _tup(tab.bbox)
                if _area(rect) > 0.9 * page_area:
                    continue
                rows = tab.extract()
                ncols = max((len(r) for r in rows), default=0)
                nonempty = sum(1 for r in rows for c in r if c and str(c).strip())
                if len(rows) < 2 or ncols < 2 or nonempty < 3:
                    continue          # 單行 / 單列多半是答案框、邊框，不當表格
                md = rows_to_markdown_table(rows, clean_inline if self.normalize else None)
                if md:
                    rects.append(rect)
                    items.append({"type": "table", "bbox": list(rect), "md": md, "page": page_no})
        except Exception:
            pass                      # 舊版 PyMuPDF 沒有 find_tables → 當作沒有表格
        return items, rects

    # ------------------------------------------------------------------ 文字行
    @staticmethod
    def _line_from_dict(line, page_no):
        spans = line.get("spans", [])
        lb = line.get("bbox")
        sizes = [s.get("size", 10) for s in spans if s.get("text", "").strip()]
        if not sizes or not lb:
            return None
        max_size = max(sizes)
        base = next((s for s in spans if s.get("text", "").strip() and s.get("size", 0) >= max_size - 0.1), spans[0])
        base_y = base.get("origin", (0, 0))[1]
        parts, bold_chars, total = [], 0, 0
        for sp in spans:
            t = sp.get("text", "")
            if not t.strip():
                if t and parts:
                    parts.append(t)
                continue
            flags = sp.get("flags", 0)
            is_bold = bool(flags & 16) or "bold" in sp.get("font", "").lower()
            total += len(t)
            bold_chars += len(t) if is_bold else 0
            tt = t
            small = sp.get("size", max_size) < 0.78 * max_size
            if (flags & 1 or small) and parts and re.fullmatch(r"[0-9+\-−nxyabcmk]{1,3}", t.strip()):
                oy = sp.get("origin", (0, base_y))[1]
                if oy < base_y - 0.2 * max_size:
                    tt = SUP_O + t.strip() + SUP_C
                elif oy > base_y + 0.15 * max_size:
                    tt = SUB_O + t.strip() + SUB_C
            parts.append(tt)
        text = "".join(parts).strip()
        if not text:
            return None
        return {"type": "line", "bbox": list(map(float, lb)), "text": text, "size": max_size,
                "bold": total > 0 and bold_chars >= 0.6 * total, "page": page_no}

    def _extract_lines(self, page, page_no, exclude_rects):
        d = page.get_text("dict")
        items, vertical = [], []
        for b in d.get("blocks", []):
            if b.get("type") != 0:
                continue
            bbox = b.get("bbox", [0, 0, 0, 0])
            if _mostly_inside(_tup(bbox), exclude_rects, 0.55):
                continue
            lines = b.get("lines", [])
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            vcount = sum(1 for ln in lines if abs(ln.get("dir", (1, 0))[0]) < 0.3)
            raw = []
            for ln in lines:
                txt = "".join(sp.get("text", "") for sp in ln.get("spans", [])).strip()
                if txt:
                    raw.append(txt)
            if not raw:
                continue
            avg = sum(len(x) for x in raw) / len(raw)
            is_vert = (vcount > len(lines) / 2) or (h > w * 2.2 and avg <= 2.2 and len(raw) >= 3)
            if is_vert:
                size = max((sp.get("size", 10) for ln in lines for sp in ln.get("spans", [])), default=10)
                vertical.append({"type": "line", "bbox": list(map(float, bbox)), "text": "".join(raw),
                                 "size": size, "bold": False, "page": page_no, "vertical": True})
                continue
            for ln in lines:
                dr = ln.get("dir", (1, 0))
                if abs(dr[0]) >= 0.3 and abs(dr[1]) >= 0.3:
                    continue                      # 斜向浮水印
                if _mostly_inside(_tup(ln.get("bbox", bbox)), exclude_rects, 0.6):
                    continue
                it = self._line_from_dict(ln, page_no)
                if it:
                    items.append(it)
        # 偽粗體 / 重複疊印造成的重影行：同文本、位置幾乎重合 → 去重
        seen, uniq = {}, []
        for it in items:
            dup = any(abs(b[0] - it["bbox"][0]) < 3 and abs(b[1] - it["bbox"][1]) < 3 for b in seen.get(it["text"], []))
            if not dup:
                seen.setdefault(it["text"], []).append(it["bbox"])
                uniq.append(it)
        return uniq, vertical

    # ------------------------------------------------------------------ 插圖偵測 + 插圖內標籤剔除
    def _figures(self, page, page_no, table_rects, lines):
        """回傳 figure 條目（bbox 已含周邊標籤字）；同時把插圖內的標籤文字從 lines 剔除。"""
        pr = _tup(page.rect)
        cands = []
        try:
            for info in page.get_image_info():
                r = _tup(info["bbox"])
                if r[2] - r[0] >= 24 and r[3] - r[1] >= 24:
                    cands.append([r, "img"])
        except Exception:
            pass
        try:
            for r in page.cluster_drawings():
                r = _tup(r)
                if r[2] - r[0] >= 36 and r[3] - r[1] >= 30:
                    cands.append([r, "vec"])
        except Exception:
            pass
        good = []
        for r, kind in cands:
            r = (max(r[0], pr[0]), max(r[1], pr[1]), min(r[2], pr[2]), min(r[3], pr[3]))
            if _area(r) <= 0 or _area(r) >= 0.8 * _area(pr):
                continue
            if any(_inter_area(r, t) >= 0.5 * _area(r) for t in table_rects):
                continue
            good.append([r, kind])
        changed = True                           # 相鄰 / 重疊的圖塊合併
        while changed:
            changed = False
            for i in range(len(good)):
                for j in range(i + 1, len(good)):
                    a = (good[i][0][0] - 4, good[i][0][1] - 4, good[i][0][2] + 4, good[i][0][3] + 4)
                    if _intersects(a, good[j][0]):
                        good[i][0] = _union(good[i][0], good[j][0])
                        good[i][1] = "img" if "img" in (good[i][1], good[j][1]) else "vec"
                        del good[j]
                        changed = True
                        break
                if changed:
                    break
        figs, drop = [], set()
        label_re = re.compile(r"^[A-Za-zΑ-ωθπ′'°0-9 ]{1,3}$")
        for rect, kind in good:
            expanded = (rect[0] - 2, rect[1] - 2, rect[2] + 2, rect[3] + 2)
            near = (rect[0] - 12, rect[1] - 12, rect[2] + 12, rect[3] + 12)
            inside = [ln for ln in lines if id(ln) not in drop and _mostly_inside(tuple(ln["bbox"]), [expanded], 0.7)]
            long_chars = sum(len(ln["text"]) for ln in inside if len(ln["text"]) > 6)
            if kind == "vec" and long_chars >= 40:
                continue                         # 帶邊框的文字框，不是插圖
            inside_ids = {id(ln) for ln in inside}
            crop = rect
            for ln in lines:
                if id(ln) in drop:
                    continue
                t, lr = ln["text"].strip(), tuple(ln["bbox"])
                if len(t) <= 6 and id(ln) in inside_ids:
                    drop.add(id(ln))
                    crop = _union(crop, lr)
                elif label_re.match(t) and _intersects(lr, near):
                    drop.add(id(ln))
                    crop = _union(crop, lr)
            crop = (max(crop[0] - 3, pr[0]), max(crop[1] - 3, pr[1]), min(crop[2] + 3, pr[2]), min(crop[3] + 3, pr[3]))
            if self.figures:
                figs.append({"type": "figure", "bbox": list(crop), "page": page_no,
                             "fig": {"name": None, "key": None, "page": page_no,
                                     "w_px": round((crop[2] - crop[0]) * 96 / 72),
                                     "h_px": round((crop[3] - crop[1]) * 96 / 72)}})
        lines[:] = [ln for ln in lines if id(ln) not in drop]
        return figs

    # ------------------------------------------------------------------ 單頁收集
    def collect_page(self, page, page_no):
        """回傳 {'items': [...], 'height': h, 'scanned': bool}；失敗回傳 None（呼叫端退回純文字）。"""
        try:
            tables, trects = self._extract_tables(page, page_no)
            lines, vertical = self._extract_lines(page, page_no, trects)
            figs = self._figures(page, page_no, trects, lines)
            scanned = False
            if not lines and not tables and not vertical:
                try:
                    scanned = bool(page.get_images())
                except Exception:
                    scanned = False
            return {"items": lines + vertical + tables + figs, "height": float(_tup(page.rect)[3]), "scanned": scanned}
        except Exception:
            return None

    # ------------------------------------------------------------------ 頁眉頁腳
    @staticmethod
    def _in_margin(it, h):
        return it["bbox"][3] < 0.075 * h or it["bbox"][1] > 0.92 * h

    def strip_margins(self, pages_items, heights):
        n = len(pages_items)
        keycount = Counter()
        for items, h in zip(pages_items, heights):
            seen = set()
            for it in items:
                if it["type"] == "line" and not it.get("vertical") and self._in_margin(it, h):
                    seen.add(re.sub(r"\d+", "#", it["text"].strip()))
            keycount.update(seen)
        first_kept, out = set(), []
        for items, h in zip(pages_items, heights):
            kept = []
            for it in items:
                if it["type"] == "line" and not it.get("vertical") and self._in_margin(it, h):
                    txt = it["text"].strip()
                    key = re.sub(r"\d+", "#", txt)
                    if self.PAGE_NO_RE.match(txt) or self.NOISE_RE.search(txt):
                        continue
                    if n >= 2 and keycount[key] >= max(2, 0.5 * n):   # 多數頁都出現 → 頁眉/頁腳，只保留第一次
                        if key in first_kept:
                            continue
                        first_kept.add(key)
                kept.append(it)
            out.append(kept)
        return out

    # ------------------------------------------------------------------ 閱讀順序
    @staticmethod
    def _mark_flags(items, left, right):
        for it in items:
            if it["type"] != "line":
                continue
            size = it.get("size") or 10.0
            it["indent"] = (it["bbox"][0] - left) > 1.0 * size
            it["short"] = (right - it["bbox"][2]) > 2.0 * size

    @staticmethod
    def _merge_row(row):
        if len(row) == 1:
            return row[0]
        row = sorted(row, key=lambda i: i["bbox"][0])
        size = max(i.get("size") or 10 for i in row)
        parts, prev = [], None
        for it in row:
            if prev is not None:
                parts.append("  " if it["bbox"][0] - prev["bbox"][2] > 0.6 * size else "")
            parts.append(it["text"])
            prev = it
        merged = dict(row[0])
        merged["text"] = "".join(parts)
        merged["bbox"] = [min(i["bbox"][0] for i in row), min(i["bbox"][1] for i in row),
                          max(i["bbox"][2] for i in row), max(i["bbox"][3] for i in row)]
        merged["size"] = size
        merged["bold"] = any(i.get("bold") for i in row)
        return merged

    def _order_column(self, items, left, right):
        items = sorted(items, key=lambda i: (round(i["bbox"][1], 1), i["bbox"][0]))
        rows = []
        for it in items:
            if it["type"] == "line" and rows and rows[-1]["items"][0]["type"] == "line":
                row = rows[-1]
                ov = min(it["bbox"][3], row["y1"]) - max(it["bbox"][1], row["y0"])
                h = min(it["bbox"][3] - it["bbox"][1], row["y1"] - row["y0"])
                if h > 0 and ov >= 0.55 * h:          # 同一視覺行的多個片段合併
                    row["items"].append(it)
                    row["y0"] = min(row["y0"], it["bbox"][1])
                    row["y1"] = max(row["y1"], it["bbox"][3])
                    continue
            rows.append({"items": [it], "y0": it["bbox"][1], "y1": it["bbox"][3]})
        out = [self._merge_row(r["items"]) for r in rows]
        self._mark_flags(out, left, right)
        return out

    @staticmethod
    def _split_columns(band):
        if len(band) < 10:
            return [band]
        iv = sorted((i["bbox"][0], i["bbox"][2]) for i in band)
        sizes = sorted((i.get("size") or 10) for i in band)
        gap_thr = max(12.0, sizes[len(sizes) // 2])
        groups, (cs, ce) = [], iv[0]
        for s, e in iv[1:]:
            if s - ce >= gap_thr:
                groups.append((cs, ce))
                cs, ce = s, e
            else:
                ce = max(ce, e)
        groups.append((cs, ce))
        if len(groups) < 2:
            return [band]
        cols = [[] for _ in groups]
        for it in band:
            cx = (it["bbox"][0] + it["bbox"][2]) / 2
            for gi, (gs, ge) in enumerate(groups):
                if gs <= cx <= ge:
                    cols[gi].append(it)
                    break
        total_h = max(max(i["bbox"][3] for i in band) - min(i["bbox"][1] for i in band), 1.0)
        for col in cols:
            if len(col) < 5:
                return [band]
            if max(i["bbox"][3] for i in col) - min(i["bbox"][1] for i in col) < 0.55 * total_h:
                return [band]
        return cols

    def _order_band(self, band, pl, pr):
        if not band:
            return []
        top = bottom = None
        cols = self._split_columns(band)
        if len(cols) == 1 and len(band) >= 12:
            # 容錯：最上 / 最下各一條橫跨欄間距的游離行不參與分欄判斷
            srt = sorted(band, key=lambda i: (i["bbox"][1], i["bbox"][0]))
            top, bottom = srt[0], srt[-1]
            inner = [i for i in band if i is not top and i is not bottom]
            cols2 = self._split_columns(inner)
            if len(cols2) > 1:
                cols = cols2
            else:
                top = bottom = None
        if len(cols) > 1:
            out = []
            if top is not None:
                out.extend(self._order_column([top], pl, pr))
            for col in cols:
                lines = [i for i in col if i["type"] == "line"]
                lft = min((i["bbox"][0] for i in lines), default=pl)
                rgt = max((i["bbox"][2] for i in lines), default=pr)
                out.extend(self._order_column(col, lft, rgt))
            if bottom is not None:
                out.extend(self._order_column([bottom], pl, pr))
            return out
        return self._order_column(band, pl, pr)

    def _glue_markers(self, items):
        """把獨立成塊的題號 / 括號 / 項目標記（「( )」「1.」「(A)」）吸附到同一行右側的正文上。"""
        lines = [i for i in items if i["type"] == "line" and not i.get("vertical")]
        removed = set()
        markers = [m for m in lines if m["text"].strip() and len(m["text"].strip()) <= 8
                   and self.MARKER_RE.match(m["text"].strip()) and re.search(r"\d|[)）]", m["text"])]
        for m in sorted(markers, key=lambda i: -i["bbox"][0]):
            mh = m["bbox"][3] - m["bbox"][1]
            best, best_gap = None, 999.0
            for n in lines:
                if n is m or id(n) in removed:
                    continue
                gap = n["bbox"][0] - m["bbox"][2]
                if gap < -2 or gap > 40 or n["bbox"][0] <= m["bbox"][0]:
                    continue
                ov = min(m["bbox"][3], n["bbox"][3]) - max(m["bbox"][1], n["bbox"][1])
                if ov >= 0.5 * min(mh, n["bbox"][3] - n["bbox"][1]) and gap < best_gap:
                    best, best_gap = n, gap
            if best is not None:
                best["text"] = m["text"].strip() + " " + best["text"]
                best["bbox"] = [min(best["bbox"][0], m["bbox"][0]), min(best["bbox"][1], m["bbox"][1]),
                                max(best["bbox"][2], m["bbox"][2]), max(best["bbox"][3], m["bbox"][3])]
                removed.add(id(m))
        return [i for i in items if id(i) not in removed]

    def order_items(self, items):
        """單頁條目 → 閱讀順序：直書抬頭在前；通欄元素把頁面切成帶；帶內再偵測多欄。"""
        verts = [i for i in items if i.get("vertical")]
        items = [i for i in items if not i.get("vertical")]
        items = self._glue_markers(items)
        out = []
        if verts:
            verts.sort(key=lambda v: -v["bbox"][0])          # 直書：右 → 左
            merged = dict(verts[0])
            merged["text"] = "　".join(v["text"] for v in verts)
            merged["bbox"] = [min(v["bbox"][0] for v in verts), min(v["bbox"][1] for v in verts),
                              max(v["bbox"][2] for v in verts), max(v["bbox"][3] for v in verts)]
            out.append(merged)
        if not items:
            return out
        x0 = min(i["bbox"][0] for i in items)
        x1 = max(i["bbox"][2] for i in items)
        cw = max(x1 - x0, 1.0)
        spanning = sorted([i for i in items if i["bbox"][2] - i["bbox"][0] >= 0.58 * cw],
                          key=lambda i: (i["bbox"][1], i["bbox"][0]))
        sid = {id(s) for s in spanning}
        centers = [(s["bbox"][1] + s["bbox"][3]) / 2 for s in spanning]
        bands = [[] for _ in range(len(spanning) + 1)]
        for it in items:
            if id(it) in sid:
                continue
            cy = it["bbox"][1] + 2 if it["type"] != "line" else (it["bbox"][1] + it["bbox"][3]) / 2
            bands[sum(1 for c in centers if c < cy)].append(it)
        tl = [i for i in spanning if i["type"] == "line"] or [i for i in items if i["type"] == "line"]
        pl = min((i["bbox"][0] for i in tl), default=x0)
        pr = max((i["bbox"][2] for i in tl), default=x1)
        for k, band in enumerate(bands):
            out.extend(self._order_band(band, pl, pr))
            if k < len(spanning):
                self._mark_flags([spanning[k]], pl, pr)
                out.append(spanning[k])
        return out

    # ------------------------------------------------------------------ 輸出文字
    def items_to_text(self, ordered):
        """已排序條目 → 文字：折行還原成段落、表格輸出 Markdown、上下標還原為 <sup>/<sub>。"""
        blocks, cur, prev, fig_n = [], None, None, 0

        def flush():
            nonlocal cur, prev
            if cur is not None:
                blocks.append(("p", cur))
            cur = prev = None

        for it in ordered:
            if it["type"] == "table":
                flush()
                blocks.append(("t", it["md"].strip()))
                continue
            if it["type"] == "figure":
                flush()
                fig = it.get("fig") or {}
                if fig.get("key"):
                    fig_n += 1
                    blocks.append(("f", f"![插圖{fig_n}]({fig['key']})"))
                continue
            if it["type"] != "line":
                continue
            text = clean_inline(it["text"]) if self.normalize else it["text"].strip()
            if not text:
                continue
            if cur is None:
                cur, prev = text, it
                continue
            same_size = abs((prev.get("size") or 10) - (it.get("size") or 10)) < 1.0
            merge = (self.join_lines and same_size and not prev.get("short") and not it.get("indent")
                     and not prev.get("bold") and not it.get("bold") and not it.get("vertical")
                     and not prev.get("vertical") and not ends_sentence(cur) and not _MARKER_START.match(text))
            if merge:
                cur, prev = join_text(cur, text), it
            else:
                flush()
                cur, prev = text, it
        flush()

        out = []
        for i, (kind, val) in enumerate(blocks):
            if kind == "t" and out:
                out.append("")
            out.append(restore_marks(val))
            if kind == "t" and i + 1 < len(blocks):
                out.append("")
        return "\n".join(out).strip()

    # ------------------------------------------------------------------ 整份文件
    def process(self, collected):
        """collected: [collect_page 回傳 dict 或 None] → (每頁文字, 每頁已排序條目串流)；
        失敗頁（None）兩者皆為 None，由呼叫端退回純文字。"""
        ok = [(i, c) for i, c in enumerate(collected) if c]
        stripped = self.strip_margins([c["items"] for _, c in ok], [c["height"] for _, c in ok]) if ok else []
        texts, streams = [None] * len(collected), [None] * len(collected)
        for (i, _), items in zip(ok, stripped):
            ordered = self.order_items(items)
            streams[i] = ordered
            texts[i] = self.items_to_text(ordered)
        return texts, streams

    def pages_to_texts(self, collected):
        return self.process(collected)[0]
