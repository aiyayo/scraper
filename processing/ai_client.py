# processing/ai_client.py
"""阿里雲百煉（DashScope）文檔分析：只用智能體 Application（image_list 傳 base64 圖片），輸出 標題、簡介、關鍵詞。

相對 v4 的強化：
- 送智能體前把代表頁縮圖（預設最長邊 1280）並轉 JPEG、base64 data URI，降低 token 與超限風險。
- 重試次數可設定，採退避等待；API Key 可由環境變數 DASHSCOPE_API_KEY 提供。
- JSON 解析更寬容（程式碼框、前後多餘文字、結尾多逗號、單引號），關鍵詞去重並限制數量。
"""
import os
import re
import json
import time

from core.config import DEFAULT_PROMPT

# 圖片副檔名 → MIME（縮圖失敗直接送原圖時用）
_IMAGE_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
               "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp"}


def parse_ai_json(ai_text, orig_name=""):
    """把模型回覆解析成 {title, summary, keywords, raw_response}；解析不了就拋出 ValueError。"""
    text = (ai_text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("模型回覆中找不到 JSON 物件")
    body = m.group(0)
    parsed = None
    for candidate in (body, re.sub(r",\s*([}\]])", r"\1", body)):  # 第二輪：去除結尾多餘逗號
        try:
            parsed = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    if parsed is None:
        raise ValueError("模型回覆的 JSON 格式無法解析")
    if not isinstance(parsed, dict):
        raise ValueError("模型回覆不是 JSON 物件")

    title = str(parsed.get("title") or orig_name or "").strip()
    summary = str(parsed.get("summary") or parsed.get("intro") or "").strip()
    kws = parsed.get("keywords") or []
    if isinstance(kws, str):
        kws = re.split(r"[,，、;；\s]+", kws)
    kws = list(dict.fromkeys(str(k).strip() for k in kws if str(k).strip()))[:12]
    return {"title": title, "summary": summary, "keywords": kws, "raw_response": ai_text}


def fallback_metadata(pages_data, orig_name, keyword=""):
    """AI 不可用時的本地備援：檔名當標題、前 180 字當簡介、搜尋關鍵字當關鍵詞。"""
    title = os.path.splitext(orig_name)[0] if orig_name else "未命名文檔"
    full = re.sub(r"\s+", " ", " ".join(p.get("content", "") for p in pages_data if p.get("content"))).strip()
    summary = full[:180] + ("…" if len(full) > 180 else "")
    return {"title": title, "summary": summary, "keywords": [keyword] if keyword else []}


class AliyunAnalyzer:
    def __init__(self, cfg, log=None):
        self.cfg = cfg or {}
        self.log = log or (lambda m: print(m))

    # ------------------------------------------------------------------ 提示詞與圖片
    def build_prompt(self, orig_name, total_pages, pages_data):
        template = (self.cfg.get("aliyun_prompt") or DEFAULT_PROMPT).strip()
        limit = int(self.cfg.get("ai_preview_chars", 2000) or 2000)
        preview = "\n\n".join(
            f"--- 第 {p['page']} 頁 ---\n{p['content']}" for p in pages_data[:4] if p.get("content"))[:limit]
        if not preview:
            preview = "（此文檔為純圖片或掃描件，請直接根據圖片視覺內容進行分析）"
        # 用 replace 而非 format：提示詞內含 JSON 大括號
        return (template.replace("{orig_name}", str(orig_name or ""))
                .replace("{total_pages}", str(total_pages))
                .replace("{text_preview}", preview))

    def _sample_images(self, sample_pages):
        """縮圖成 JPEG 並轉 base64 data URI；百煉智能體 image_list 只吃可存取的 URL / data URI，
        本機 file:// 路徑會讓雲端取不到圖而回 InternalError。"""
        import io
        import base64
        from PIL import Image
        max_side = int(self.cfg.get("ai_image_max_side", 1280) or 1280)
        out = []
        for p in sample_pages:
            src = p["webp_local_path"]
            try:
                with Image.open(src) as im:
                    im = im.convert("RGB")
                    im.thumbnail((max_side, max_side))
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=85)
                    mime, raw = "image/jpeg", buf.getvalue()
            except Exception:  # noqa: BLE001  縮圖失敗就直接送原圖
                with open(src, "rb") as fh:
                    raw = fh.read()
                ext = os.path.splitext(src)[1].lower().lstrip(".")
                mime = _IMAGE_MIME.get(ext, "image/jpeg")
            out.append(f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"))
        return out

    # ------------------------------------------------------------------ 呼叫
    def analyze(self, pages_data, total_pages, orig_name=""):
        api_key = (self.cfg.get("aliyun_api_key") or "").strip()
        if not api_key:
            raise ValueError("阿里雲 API Key 尚未填寫，請在『🤖 2. 阿里雲智能體配置』分頁中設定（或設環境變數 DASHSCOPE_API_KEY）！")
        app_id = (self.cfg.get("aliyun_app_id") or "").strip()
        if not app_id:
            raise ValueError("百煉智能體 App ID 尚未填寫，請在『🤖 2. 阿里雲智能體配置』分頁中設定！")
        max_sample = max(1, min(int(self.cfg.get("aliyun_max_sample_pages", 2) or 2), 5))
        attempts = 1 + max(0, int(self.cfg.get("aliyun_retry", 2)))

        import dashscope
        from dashscope import Application
        dashscope.api_key = api_key

        prompt = self.build_prompt(orig_name, total_pages, pages_data)
        images = self._sample_images(pages_data[:max_sample])
        last_err = None
        for attempt in range(attempts):
            try:
                resp = Application.call(app_id=app_id, prompt=prompt, image_list=images)
                if getattr(resp, "status_code", 200) != 200:
                    raise RuntimeError(f"百煉智能體錯誤 ({getattr(resp, 'code', '')}): {getattr(resp, 'message', '')}")
                return parse_ai_json(resp.output.text, orig_name)
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < attempts - 1:
                    time.sleep(1.5 * (attempt + 1))
        raise last_err
