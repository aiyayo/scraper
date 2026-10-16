# processing/extractor.py
"""文檔逐頁轉 WebP 並提取每頁文字。

支援：
- PDF：PyMuPDF 渲染；加密 PDF 先嘗試空密碼；掃描頁 / 文字層亂碼的頁用 PaddleOCR 識別（processing/ocr.py）。
- docx / doc / xlsx / xls：經 Windows Office（優先）／WPS（備援）COM 轉暫存 PDF 再渲染（processing/office_convert.py，
  邏輯取自 word.py）；失敗時 docx/pptx/xlsx 退回「純文字 + 文字示意圖」，doc/ppt/xls 則回報不支援。
- pptx / ppt：PowerPoint／WPS 簡報 Slide.Export 直出 PNG（免 PDF 中轉），pptx 文字由 XML 提取、.ppt 由 COM 提取。
圖片統一存入「二次 MD5」分層目錄並用二次 MD5 命名；可設定最大圖片寬度（webp_max_width）。
相對 v4：Office 示意圖改用 CJK 字型（v4 預設字型畫不出中文，全是方塊）、已存在的 WebP 可重用、可限制渲染頁數。
每頁文字預設走「智能版面還原」（processing/layout.py，參考 word.py）：多欄閱讀順序、表格轉 Markdown、
去頁眉頁腳頁碼、折行還原段落；失敗或結果異常時自動退回 PyMuPDF 純文字。docx COM 轉檔失敗時走結構化讀取
（processing/docx_reader.py）。
extract_document() 另外回傳：整份文件的結構化 Markdown（大題 / 題號 / 選項 / 表格 / 插圖，見 processing/structure.py）、
題庫清單、插圖檔案清單（插圖會裁切存成 WebP 並綁定到對應題目）。
"""
import os
import re
import html
import shutil
import tempfile
import threading
import zipfile
from dataclasses import dataclass, field

from core.constants import R2_DIR_PIC, LEGACY_PROC_EXTS, OFFICE_EXTS
from core.utils import pic_hash, pic_shard_dir
from processing.docx_reader import read_docx_pages
from processing.layout import PdfLayout
from processing.ocr import OcrEngine, OcrUnavailable, ocr_to_items
from processing.structure import build_markdown
from processing.textutil import clean_doc_text  # noqa: F401  (re-export，保持舊匯入路徑可用)

# PyMuPDF 並非執行緒安全，統一以全域鎖序列化「渲染」階段（AI / R2 階段仍可並行）
_FITZ_LOCK = threading.Lock()

_CJK_FONTS = [
    "C:/Windows/Fonts/msjh.ttc", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/mingliu.ttc",
    "/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
]


class UnsupportedFormat(Exception):
    """此格式目前無法處理（例如沒裝 Windows Office / WPS 的 .doc）。"""


def _load_font(size=22):
    from PIL import ImageFont
    for p in _CJK_FONTS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    """逐字量測換行（中文沒有空白，不能用 textwrap）。"""
    lines = []
    for para in text.split("\n"):
        cur = ""
        for ch in para:
            if draw.textlength(cur + ch, font=font) > max_w and cur:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        lines.append(cur)
    return lines


@dataclass
class ExtractResult:
    pages: list
    total: int
    markdown: str = ""
    questions: list = field(default_factory=list)
    figures: list = field(default_factory=list)
    stream: list = field(default_factory=list)   # 已排序條目串流，可用不同選項排版重新渲染 Markdown

    def as_tuple(self):
        return self.pages, self.total


def _garbled(text):
    """文字層亂碼（缺字型映射常見的 U+FFFD / 私有區字元佔多數）→ 應改用 OCR。"""
    if len(text) < 20:
        return False
    bad = sum(1 for ch in text if ch == "\ufffd" or "\ue000" <= ch <= "\uf8ff")
    return bad / len(text) > 0.3


def _plain_stream(text, page_no):
    """版面引擎失敗的頁：把純文字每行當成獨立段落，讓 Markdown 仍能涵蓋該頁。"""
    return [{"type": "line", "text": ln, "bbox": [0, i * 14, 500, i * 14 + 12], "size": 10.5, "bold": False,
             "page": page_no, "para": True} for i, ln in enumerate(text.split("\n")) if ln.strip()]


class PageExtractor:
    def __init__(self, cfg, log=None):
        self.cfg = cfg or {}
        self.log = log or (lambda m: print(m))
        c = self.cfg
        self.zoom = float(c.get("webp_zoom", 2.0) or 2.0)
        self.quality = int(c.get("webp_quality", 85) or 85)
        self.reuse = bool(c.get("reuse_existing_webp", True))
        self.max_pages = int(c.get("max_render_pages", 0) or 0)
        self.use_com = bool(c.get("use_office_com", True))
        self.max_width = int(c.get("webp_max_width", 0) or 0)   # 圖片最大寬度 px，0=不限
        self.shard = bool(c.get("shard_storage", True))
        self.smart_text = str(c.get("text_layout", "smart")) == "smart"
        self.normalize = bool(c.get("text_normalize", True))
        self.figures_on = bool(c.get("extract_figures", True))
        self.fig_zoom = float(c.get("figure_zoom", 3.0) or 3.0)
        self.md_on = bool(c.get("md_structure", True))
        self.option_style = c.get("option_style", "list")
        self.layout = PdfLayout(use_tables=bool(c.get("layout_tables", True)), normalize=self.normalize,
                                join_lines=bool(c.get("layout_join_lines", True)), figures=self.figures_on)
        engine = c.get("ocr_engine") or ("paddle" if c.get("ocr_fallback") else "off")
        self.ocr = OcrEngine(engine, c.get("paddle_lang", "ch"), float(c.get("ocr_min_conf", 0.5) or 0.5), self.log)
        self._ocr_warned = False

    # ------------------------------------------------------------------ 入口
    def extract(self, file_path, pic_dir, md5, force=False):
        """相容舊介面：回傳 (pages_data, total_pages)。"""
        return self.extract_document(file_path, pic_dir, md5, force).as_tuple()

    def extract_document(self, file_path, pic_dir, md5, force=False):
        """回傳 ExtractResult。force=True 時忽略既有 WebP 一律重繪。
        pic_dir 為圖片「根目錄」，實際輸出在 <pic_dir>/<二次MD5前2碼>/<第3碼>/ 下。"""
        ext = os.path.splitext(file_path)[1].lstrip(".").lower()
        reuse = self.reuse and not force
        if ext == "pdf":
            return self._extract_pdf(file_path, pic_dir, md5, reuse)
        if ext in OFFICE_EXTS:
            if self.use_com and os.name == "nt":
                try:
                    return self._via_office_com(file_path, ext, pic_dir, md5, reuse)
                except UnsupportedFormat:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.log(f"   ⚠️ Office/WPS 轉檔失敗，改用純文字模式: {str(e)[:80]}")
            elif self.use_com:
                self.log("   ⚠️ Office/WPS 轉圖僅支援 Windows，改用純文字模式")
            if ext in LEGACY_PROC_EXTS:
                raise UnsupportedFormat(f".{ext} 需要安裝 Windows Office 或 WPS 才能轉圖（或先另存為 .{ext}x）")
            return self._extract_office_text(file_path, ext, pic_dir, md5)
        raise UnsupportedFormat(f"暫不支援 .{ext} 格式")

    # ------------------------------------------------------------------ 共用
    def _page_dict(self, page_no, webp_name, webp_path, width, height, text, figures=None):
        return {
            "page": page_no, "webp_name": webp_name, "webp_local_path": webp_path,
            "r2_pic_key": f"/{R2_DIR_PIC}/{webp_name}", "width": width, "height": height, "content": text,
            "figures": figures or [],
        }

    # ------------------------------------------------------------------ OCR（PaddleOCR）
    def _ocr_collect(self, webp_path, page_no, scale=None):
        """對單頁 WebP 做 OCR，回傳 layout.collect_page 相同結構（座標已除以渲染倍率還原成 PDF 點座標）。
        scale 為該頁實際渲染倍率（受最大圖片寬度影響時會小於 webp_zoom）；純圖片直出頁用 1.0。"""
        scale = float(scale or self.zoom)
        from PIL import Image
        with Image.open(webp_path) as im:
            im = im.convert("RGB")
            raw = self.ocr.recognize(im)
            height = im.height / scale
        return {"items": ocr_to_items(raw, page_no, scale=scale), "height": height, "scanned": False, "ocr": True}

    def _try_ocr(self, webp_path, page_no, scale=None):
        if not self.ocr.enabled:
            return None
        try:
            self.log(f"   🔍 {self.ocr.engine} OCR 識別第 {page_no} 頁...")
            return self._ocr_collect(webp_path, page_no, scale)
        except OcrUnavailable as e:
            if not self._ocr_warned:
                self._ocr_warned = True
                self.log(f"   ⚠️ OCR 不可用，掃描頁將沒有文字: {e}")
            self.ocr.engine = "off"
        except Exception as e:  # noqa: BLE001
            self.log(f"   ⚠️ 第 {page_no} 頁 OCR 失敗: {str(e)[:80]}")
        return None

    def ocr_page_text(self, webp_path, page_no=1, scale=None):
        """給工作台『OCR 當前頁』用：回傳該頁還原後的文字（OCR 未啟用 / 不可用時拋出 OcrUnavailable）。"""
        if not self.ocr.enabled:
            self.ocr.engine = "paddle"
        c = self._ocr_collect(webp_path, page_no, scale)
        return self.layout.process([c])[0][0] or ""

    # ------------------------------------------------------------------ 插圖裁切
    def _save_figures(self, page, coll, page_no, md5, pic_dir, reuse, fitz, Image, out):
        keep = []
        figs = [it for it in coll["items"] if it["type"] == "figure"]
        for k, it in enumerate(figs, 1):
            name = f"{md5}_p{page_no}_f{k}.webp"
            path = os.path.join(pic_dir, name)
            try:
                if not (reuse and os.path.exists(path) and os.path.getsize(path) > 0):
                    pix = page.get_pixmap(matrix=fitz.Matrix(self.fig_zoom, self.fig_zoom),
                                          clip=fitz.Rect(*it["bbox"]), alpha=False)
                    Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save(path, "WEBP", quality=self.quality)
                it["fig"].update(name=name, key=f"/{R2_DIR_PIC}/{name}", local_path=path)
                keep.append(it)
                out.append(dict(it["fig"]))
            except Exception as e:  # noqa: BLE001
                self.log(f"   ⚠️ 第 {page_no} 頁插圖 {k} 裁切失敗: {str(e)[:60]}")
        coll["items"] = [i for i in coll["items"] if i["type"] != "figure"] + keep

    # ------------------------------------------------------------------ PDF
    def _page_zoom(self, page_w_pt):
        """實際渲染倍率：webp_zoom 為基準，若超過最大寬度則等比縮小。"""
        z = self.zoom
        if self.max_width and page_w_pt > 0:
            z = min(z, self.max_width / page_w_pt)
        return z

    def _extract_pdf(self, pdf_path, pic_base, md5, reuse):
        import fitz  # PyMuPDF
        from PIL import Image

        pid = pic_hash(md5)  # 圖片名稱／路徑一律用二次 MD5
        out_dir = pic_shard_dir(pic_base, md5, self.shard)
        geo, plain, collected, all_figs = [], [], [], []
        with _FITZ_LOCK:
            doc = fitz.open(pdf_path)
            try:
                if getattr(doc, "needs_pass", False) and not doc.authenticate(""):
                    raise RuntimeError("PDF 已加密，無法讀取")
                total = len(doc)
                n = min(total, self.max_pages) if self.max_pages else total
                if n < total:
                    self.log(f"   ✂️ 頁數 {total} 超過上限，僅渲染前 {n} 頁（total_pages 仍記錄 {total}）")
                for idx in range(n):
                    page, no = doc[idx], idx + 1
                    z = self._page_zoom(page.rect.width)
                    webp_name = f"{pid}_p{no}.webp"
                    webp_path = os.path.join(out_dir, webp_name)
                    if reuse and os.path.exists(webp_path) and os.path.getsize(webp_path) > 0:
                        with Image.open(webp_path) as im:
                            w, h = im.size
                    else:
                        pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
                        Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save(
                            webp_path, "WEBP", quality=self.quality)
                        w, h = pix.width, pix.height
                    geo.append((no, webp_name, webp_path, w, h, z))
                    plain.append(clean_doc_text(page.get_text("text")))
                    coll = self.layout.collect_page(page, no) if self.smart_text else None
                    if coll and self.figures_on:
                        self._save_figures(page, coll, no, pid, out_dir, reuse, fitz, Image, all_figs)
                    collected.append(coll)
            finally:
                doc.close()

        # 以下是純 Python / OCR 運算，不需要持有 PyMuPDF 鎖
        for i, (no, _name, webp_path, _w, _h, z) in enumerate(geo):
            c = collected[i]
            need_ocr = (c is None and not plain[i]) or bool(c and c.get("scanned")) or _garbled(plain[i])
            if need_ocr:
                oc = self._try_ocr(webp_path, no, z)
                if oc and oc["items"]:
                    collected[i] = oc
                    plain[i] = "" if _garbled(plain[i]) else plain[i]

        texts, streams = self.layout.process(collected)
        pages, stream = [], []
        figs_by_page = {}
        for f in all_figs:
            figs_by_page.setdefault(f["page"], []).append(f)
        for i, (no, webp_name, webp_path, w, h, z) in enumerate(geo):
            text = texts[i]
            # 保險：智能結果為空，或比純文字短太多（疑似誤刪）→ 退回純文字
            if not text or (len(plain[i]) > 50 and len(text) < 0.3 * len(plain[i])):
                text = plain[i] or text or ""
            d = self._page_dict(no, webp_name, webp_path, w, h, text, figs_by_page.get(no))
            d["render_zoom"] = z
            pages.append(d)
            stream.extend(streams[i] if streams[i] is not None else _plain_stream(plain[i], no))

        md, questions = "", []
        if self.md_on:
            md, questions = build_markdown(stream, self.option_style)
        return ExtractResult(pages, total, md, questions, all_figs, stream)

    # ------------------------------------------------------------------ Office / WPS（COM）
    def _via_office_com(self, src, ext, pic_base, md5, reuse):
        """doc/docx/xls/xlsx：Office/WPS → 暫存 PDF → 走 PDF 管線；ppt/pptx：Slide.Export 直出 PNG。"""
        from processing import office_convert

        tmp = tempfile.mkdtemp(prefix="oc_ext_")
        try:
            if ext in ("docx", "doc", "xlsx", "xls"):
                pdf = os.path.join(tmp, "in.pdf")
                office_convert.office_to_pdf(src, ext, pdf)
                return self._extract_pdf(pdf, pic_base, md5, reuse)

            img_dir = os.path.join(tmp, "slides")
            count, com_texts = office_convert.ppt_export_slides(
                src, ext, img_dir, zoom=self.zoom, width_cap=self.max_width, max_pages=self.max_pages)
            xml_texts = self._pptx_texts(src) if ext == "pptx" else {}
            return self._assemble_slides(img_dir, count, xml_texts or {i: t for i, t in enumerate(com_texts, 1)},
                                         pic_base, md5, reuse)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _assemble_slides(self, img_dir, total, texts_by_page, pic_base, md5, reuse):
        """PPT 直出 PNG → 逐頁存 WebP（二次 MD5 命名分層）；無文字頁補 OCR，再組 Markdown。"""
        from PIL import Image

        pid = pic_hash(md5)
        out_dir = pic_shard_dir(pic_base, md5, self.shard)
        pages, stream, all_figs = [], [], []
        for i in range(1, total + 1):
            png = os.path.join(img_dir, f"slide_{i:03d}.png")
            name = f"{pid}_p{i}.webp"
            path = os.path.join(out_dir, name)
            text = clean_doc_text(texts_by_page.get(i, ""))
            coll = None
            if not (reuse and os.path.exists(path) and os.path.getsize(path) > 0):
                with Image.open(png) as im:
                    im = im.convert("RGB")
                    if self.max_width and im.width > self.max_width:  # 雙保險：超出上限再縮一次
                        h = round(im.height * self.max_width / im.width)
                        im = im.resize((self.max_width, h), Image.Resampling.LANCZOS)
                    im.save(path, "WEBP", quality=self.quality)
                    w, h = im.size
            else:
                with Image.open(path) as im:
                    w, h = im.size
            if not text:  # 簡報頁無提取到文字 → 走 OCR（座標以像素 1:1 計）
                coll = self._try_ocr(path, i, 1.0)
            if coll:
                texts, streams = self.layout.process([coll])
                text = texts[0] or ""
                stream.extend(streams[0] or _plain_stream(text, i))
            else:
                stream.extend(_plain_stream(text, i))
            d = self._page_dict(i, name, path, w, h, text)
            d["render_zoom"] = 1.0   # PPT 直出圖為像素 1:1，OCR 座標不做倍率換算
            pages.append(d)

        md, questions = build_markdown(stream, self.option_style) if self.md_on else ("", [])
        return ExtractResult(pages, total, md, questions, all_figs, stream)

    @staticmethod
    def _pptx_texts(src):
        """從 pptx XML 直接取每頁投影片文字（page → text）；失敗回傳空 dict。"""
        import zipfile
        try:
            with zipfile.ZipFile(src) as z:
                names = z.namelist()
                slides = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                                key=lambda s: int(re.search(r"(\d+)\.xml$", s).group(1)))
                out = {}
                for i, n in enumerate(slides, 1):
                    xml = z.read(n).decode("utf-8", "ignore")
                    out[i] = clean_doc_text(
                        " ".join(html.unescape(t) for t in re.findall(r"<a:t[^>]*>(.*?)</a:t>", xml, re.S)))
                return out
        except Exception:  # noqa: BLE001
            return {}

    # ------------------------------------------------------------------ Office 純文字備援
    def _extract_office_text(self, file_path, ext, pic_base, md5):
        texts = []
        if ext == "docx":
            texts = read_docx_pages(file_path, normalize=self.normalize)
        elif ext == "pptx":
            slide_text = self._pptx_texts(file_path)
            texts = [slide_text.get(i, "") for i in range(1, len(slide_text) + 1)]
        else:
            with zipfile.ZipFile(file_path) as z:
                names = z.namelist()
                if "xl/sharedStrings.xml" in names:
                    xml = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
                    full = clean_doc_text(" ".join(
                        html.unescape(t) for t in re.findall(r"<t[^>]*>(.*?)</t>", xml, re.S)))
                    texts = [full[i:i + 1500] for i in range(0, len(full), 1500)][:50]
        if not texts:
            texts = [""]
        md = "\n\n".join(t for t in texts if t) if self.md_on else ""
        return ExtractResult(self._placeholder_pages(texts, pic_base, md5), len(texts), md)

    def _placeholder_pages(self, texts, pic_base, md5):
        from PIL import Image, ImageDraw
        pid = pic_hash(md5)
        out_dir = pic_shard_dir(pic_base, md5, self.shard)
        total, font = len(texts), _load_font(22)
        W, H, margin = 900, 1200, 40
        pages = []
        for i, txt in enumerate(texts, 1):
            img = Image.new("RGB", (W, H), (255, 255, 255))
            draw = ImageDraw.Draw(img)
            draw.text((margin, margin), f"Page {i}/{total}", fill=(120, 120, 120), font=font)
            y = margin + 44
            for line in _wrap(draw, txt[:1800], font, W - 2 * margin)[:36]:
                draw.text((margin, y), line, fill=(30, 30, 30), font=font)
                y += 28
            if self.max_width and W > self.max_width:  # 示意圖也遵守最大寬度
                H2 = round(H * self.max_width / W)
                img = img.resize((self.max_width, H2), Image.Resampling.LANCZOS)
                W2, H = self.max_width, H2
            else:
                W2, H = W, H
            name = f"{pid}_p{i}.webp"
            path = os.path.join(out_dir, name)
            img.save(path, "WEBP", quality=self.quality)
            pages.append(self._page_dict(i, name, path, W2, H, txt))
        return pages
