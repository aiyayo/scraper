# processing/ocr.py
"""OCR 引擎：PaddleOCR（主）/ Tesseract（備援）。

- 同時相容 PaddleOCR 2.x（ocr.ocr(img, cls=True)）與 3.x（ocr.predict(img)）。
- 模型只初始化一次（依語言快取），推論以鎖序列化（Paddle predictor 非執行緒安全）。
- 相容新版 NumPy（np.int / np.float 等別名已移除，舊版 PaddleOCR 仍會引用）。
- 沒安裝 paddleocr 時拋 OcrUnavailable，由呼叫端記錄一次警告後略過 OCR，不影響其他流程。
輸出統一為 [(x0, y0, x1, y1, text)]（像素座標）；ocr_to_items 再轉成版面引擎可用的條目。
"""
import threading

_INIT_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()
_PADDLE_CACHE = {}

ENGINES = ("off", "paddle", "tesseract")


class OcrUnavailable(RuntimeError):
    """OCR 套件未安裝或初始化失敗。"""


def _numpy_compat():
    import numpy as np
    for name, val in (("int", int), ("float", float), ("bool", bool), ("object", object)):
        if name not in vars(np):
            setattr(np, name, val)


def _box_to_rect(box):
    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def _get(res, key):
    try:
        return res[key]
    except (KeyError, TypeError, IndexError):
        pass
    try:
        return res.get("res", {}).get(key)
    except AttributeError:
        return None


def parse_paddle_v2(result, min_conf=0.0):
    """遞迴解析 2.x 的巢狀結果：[[box, (text, score)], ...]（可能多包一層頁面列表）。"""
    items = []

    def walk(node):
        if not isinstance(node, (list, tuple)) or len(node) == 0:
            return
        if (len(node) == 2 and isinstance(node[1], (list, tuple)) and len(node[1]) >= 1
                and isinstance(node[1][0], str)):
            box, text = node[0], node[1][0].strip()
            score = float(node[1][1]) if len(node[1]) > 1 else 1.0
            if text and score >= min_conf:
                items.append((*_box_to_rect(box), text))
        else:
            for sub in node:
                walk(sub)

    walk(result)
    return items


def parse_paddle_v3(results, min_conf=0.0):
    items = []
    for r in results or []:
        texts = _get(r, "rec_texts") or []
        scores = _get(r, "rec_scores")
        polys = _get(r, "rec_polys")
        if polys is None or len(polys) == 0:
            polys = _get(r, "dt_polys")
        if polys is None:
            continue
        for i, text in enumerate(texts):
            text = (text or "").strip()
            score = float(scores[i]) if scores is not None and i < len(scores) else 1.0
            if text and i < len(polys) and score >= min_conf:
                items.append((*_box_to_rect(polys[i]), text))
    return items


class OcrEngine:
    def __init__(self, engine="paddle", lang="ch", min_conf=0.5, log=None):
        self.engine = engine if engine in ENGINES else "off"
        self.lang = lang or "ch"
        self.min_conf = float(min_conf)
        self.log = log or (lambda m: None)

    @property
    def enabled(self):
        return self.engine != "off"

    # ------------------------------------------------------------------ PaddleOCR
    def _paddle(self):
        with _INIT_LOCK:
            if self.lang in _PADDLE_CACHE:
                return _PADDLE_CACHE[self.lang]
            _numpy_compat()
            try:
                import paddleocr
                from paddleocr import PaddleOCR
            except Exception as e:  # noqa: BLE001
                raise OcrUnavailable(f"未安裝 PaddleOCR（pip install paddlepaddle paddleocr）: {e}") from e
            self.log("🔍 正在初始化 PaddleOCR 模型（首次會下載模型，請稍候）...")
            v3 = str(getattr(paddleocr, "__version__", "2")).split(".")[0] >= "3"
            try:
                if v3:
                    eng = PaddleOCR(lang=self.lang, use_doc_orientation_classify=False,
                                    use_doc_unwarping=False, use_textline_orientation=True)
                else:
                    try:
                        eng = PaddleOCR(use_angle_cls=True, lang=self.lang, show_log=False)
                    except (TypeError, ValueError):
                        eng = PaddleOCR(use_angle_cls=True, lang=self.lang)
            except Exception as e:  # noqa: BLE001
                raise OcrUnavailable(f"PaddleOCR 初始化失敗: {e}") from e
            _PADDLE_CACHE[self.lang] = (eng, v3)
            return _PADDLE_CACHE[self.lang]

    def _run_paddle(self, pil_img):
        import numpy as np
        eng, v3 = self._paddle()
        arr = np.array(pil_img.convert("RGB"))
        with _RUN_LOCK:
            if v3:
                return parse_paddle_v3(eng.predict(arr), self.min_conf)
            return parse_paddle_v2(eng.ocr(arr, cls=True), self.min_conf)

    # ------------------------------------------------------------------ Tesseract
    def _run_tesseract(self, pil_img):
        try:
            import pytesseract
        except ImportError as e:
            raise OcrUnavailable("未安裝 pytesseract") from e
        from processing.textutil import join_text
        data = pytesseract.image_to_data(pil_img.convert("RGB"), lang="chi_tra+chi_sim+eng",
                                         output_type=pytesseract.Output.DICT)
        groups = {}
        for i, t in enumerate(data["text"]):
            t = t.strip()
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                conf = 0
            if not t or conf < max(self.min_conf * 100 * 0.6, 0):
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            g = groups.setdefault(key, {"x0": x, "y0": y, "x1": x + w, "y1": y + h, "t": ""})
            g["x0"], g["y0"] = min(g["x0"], x), min(g["y0"], y)
            g["x1"], g["y1"] = max(g["x1"], x + w), max(g["y1"], y + h)
            g["t"] = join_text(g["t"], t)
        return [(g["x0"], g["y0"], g["x1"], g["y1"], g["t"]) for g in groups.values()]

    # ------------------------------------------------------------------ 入口
    def recognize(self, pil_img):
        if self.engine == "paddle":
            return self._run_paddle(pil_img)
        if self.engine == "tesseract":
            return self._run_tesseract(pil_img)
        return []


def ocr_to_items(ocr_items, page_no=1, scale=1.0):
    """OCR 結果 → 版面引擎條目（座標除以 scale 還原成 PDF 點座標；字號由行高估計）。"""
    items = []
    for x0, y0, x1, y1, text in ocr_items:
        t = (text or "").strip()
        if not t:
            continue
        bbox = [x0 / scale, y0 / scale, x1 / scale, y1 / scale]
        w, h = max(bbox[2] - bbox[0], 1), max(bbox[3] - bbox[1], 1)
        items.append({"type": "line", "bbox": bbox, "text": t, "size": max(h * 0.82, 6.0), "bold": False,
                      "page": page_no, "vertical": (h > w * 1.8 and len(t) >= 3)})
    return items
