# processing/publish_client.py
"""帝國 CMS 發布接口客戶端（ecmsinfo.php 風格）。

契約：
- POST 到 api_url，提交格式 form(application/x-www-form-urlencoded) 或 json，編碼 utf-8 / gbk。
- 送出的欄位由 publish_params 映射表決定：值裡可寫動態標籤（[标题]/[内容]…），不含標籤即為固定值（如 classid=1）。
- 回應本文命中任一 success_keywords 視為發布成功。
- 成功後可把縮略圖/配圖歸檔到 publish_archive_dir/d/file/pic/月日/，並可選刪除本地原始檔。

本模組即流水線步驟⑤的唯一 POST 出口，服務「發布接口（帝國CMS）」選項卡。
"""
import os
import re
import html
import time
import shutil
from datetime import datetime
from urllib.parse import urlencode, unquote

import requests

# 可用動態標籤（名稱沿用帝國 CMS 發布工具既有標記，簡體中文，勿隨意改名）
TAGS = ["[标题]", "[内容]", "[摘要]", "[关键词]", "[账号名]", "[文章ID]", "[发布时间]", "[发布时间戳]",
        "[缩略图路径]", "[嵌入代码]", "[嵌入iframe]", "[iframe地址]", "[原文网址]",
        "[md5]", "[orig_name]", "[file_path]", "[file_url]", "[json_url]",
        "[file_size]", "[file_type]", "[total_pages]", "[page_count]", "[images]"]
TAG_HELP = (
    "【可用动态标签】：[标题]、[内容] (正文 Markdown 深度渲染为带宽高属性的 HTML 图文源码)、[摘要]、[关键词]、"
    "[账号名]、[文章ID]、[发布时间]、[发布时间戳] (帝国CMS newstime)、[缩略图路径] (/d/file/pic/月日/文件名)、"
    "[嵌入代码] (标准永久链接)、[嵌入iframe] (完整iframe代码)、[iframe地址]、[原文网址]\n"
    "【JSON 欄位标签】：[md5]、[orig_name]、[file_path] (R2键)、[file_url] (原檔完整網址)、[json_url]、"
    "[file_size]、[file_type]、[total_pages]、[page_count]、[images] (所有頁面圖網址，逗號分隔)\n"
    "* 值里不含标签时按固定值发送（例如 classid 填 1）。"
    "* 归档特性：配置了归档根目录后，发布成功会自动把缩略图与配图备份到该目录下的 /d/file/pic/月日/ 相同结构中。"
)

_TAG_RE = re.compile(r"\[([^\[\]]+)\]")
_IMG_RE = re.compile(r"!\[(.*?)\]\((.*?)\)")
_LINK_RE = re.compile(r"(?!!)\[(.*?)\]\((.*?)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_OLIST_RE = re.compile(r"^\s*\d+[.、)]\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


# ------------------------------------------------------------------ 標籤替換
def render_value(text, ctx):
    """把值中的 [标签] 換成 ctx 對應內容；未知標籤原樣保留；非字串直接轉字串。"""
    def _sub(m):
        name = m.group(1)
        return str(ctx.get(name, m.group(0)))
    return _TAG_RE.sub(_sub, str(text))


def build_params(post_params, ctx):
    """依映射表（有序 dict）組出實際要提交的參數；值為固定值或含標籤的範本。"""
    return {k: render_value(v, ctx) for k, v in (post_params or {}).items()}


def markdown_to_html(markDown_text, image_dims=None):
    """輕量 Markdown → HTML：標題/段落/粗體/列表/表格/圖片(帶寬高)/連結。

    image_dims: {圖片 URL: (width, height)}，命中時輸出 width/height 屬性（帝國CMS 後台慣例）。
    """
    image_dims = image_dims or {}
    lines = str(markDown_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def inline(s):
        s = html.escape(s)
        def _img(m):
            alt, src = m.group(1), m.group(2)
            wh = ""
            dim = image_dims.get(src)
            if dim and dim[0] and dim[1]:
                wh = f' width="{int(dim[0])}" height="{int(dim[1])}"'
            return f'<img src="{src}" alt="{alt}"{wh} style="max-width:100%;"/>'
        s = _IMG_RE.sub(_img, s)
        s = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
        s = _BOLD_RE.sub(r"<strong>\1</strong>", s)
        return s

    out, i, n = [], 0, len(lines)
    while i < n:
        raw = lines[i]
        line = raw.strip()
        if not line:
            i += 1
            continue
        # 表格：| a | b | 隨後一列分隔線
        if line.startswith("|") and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1] or ""):
            def cells(ln):
                return [c.strip() for c in ln.strip().strip("|").split("|")]
            head = cells(line)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(cells(lines[i].strip()))
                i += 1
            t = ['<table border="1" cellspacing="0" cellpadding="4">', "<thead><tr>"]
            t += [f"<th>{inline(c)}</th>" for c in head]
            t.append("</tr></thead><tbody>")
            for r in rows:
                t.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t))
            continue
        m = _HEAD_RE.match(line)
        if m:
            lv = len(m.group(1))
            out.append(f"<h{lv}>{inline(m.group(2))}</h{lv}>")
            i += 1
            continue
        # 無序/有序列表（各自合併連續列）
        if _LIST_RE.match(line) or _OLIST_RE.match(line):
            ordered = bool(_OLIST_RE.match(line))
            tag, rx = ("ol", _OLIST_RE) if ordered else ("ul", _LIST_RE)
            items = []
            while i < n:
                mm = rx.match(lines[i].strip())
                if not mm:
                    break
                items.append(f"<li>{inline(mm.group(1))}</li>")
                i += 1
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue
        out.append(f"<p>{inline(line)}</p>")
        i += 1
    return "\n".join(out)


def build_context(payload, extra=None, now_ts=None):
    """從統一 JSON payload + extra 建立標籤上下文。

    extra: account(账号名)、article_id(文章ID)、source_url(原文網址)、file_url、
           thumb_rel(缩略图路径，通常由 prepare_thumbnail 產生)、now_ts。
    """
    extra = extra or {}
    ts = extra.get("now_ts", now_ts) or time.time()
    dt = datetime.fromtimestamp(ts)
    dims = {p.get("image"): (p.get("width"), p.get("height"))
            for p in payload.get("pages", []) if p.get("image")}
    # Markdown 正文以題目文字為主；帝國CMS 文章需可見逐頁掃描圖，故補上未在正文出現的頁面圖
    content_html = markdown_to_html(payload.get("markdown", ""), dims)
    page_imgs = []
    for p in payload.get("pages", []):
        src = p.get("image")
        if not src or f'src="{src}"' in content_html:
            continue
        dim = dims.get(src)
        wh = f' width="{int(dim[0])}" height="{int(dim[1])}"' if dim and dim[0] and dim[1] else ""
        page = p.get("page", "")
        page_imgs.append(f'<p><img src="{src}"{wh} alt="第{page}頁" style="max-width:100%;"/></p>')
    if page_imgs:
        content_html = (content_html + "\n" if content_html.strip() else "") + "\n".join(page_imgs)
    file_url = (extra.get("file_url") or payload.get("file_url")
                or payload.get("file_path") or extra.get("source_url") or "")
    source_url = extra.get("source_url") or file_url
    kws = payload.get("keywords") or []
    images = [p.get("image") for p in payload.get("pages", []) if p.get("image")]
    return {
        "标题": payload.get("title", ""),
        "内容": content_html,
        "摘要": payload.get("summary", ""),
        "关键词": ", ".join(kws) if isinstance(kws, (list, tuple)) else str(kws),
        "账号名": extra.get("account", ""),
        "文章ID": str(extra.get("article_id") or payload.get("md5", "")),
        "发布时间": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "发布时间戳": str(int(ts)),
        # ---- 統一 JSON 欄位（與舊 POST 白名單對應，可直接映射到帝國接口參數）----
        "md5": payload.get("md5", ""),
        "orig_name": payload.get("orig_name", ""),
        "file_path": payload.get("file_path", ""),
        "file_url": file_url,
        "json_url": payload.get("json_url", ""),
        "file_size": payload.get("file_size", ""),
        "file_type": payload.get("file_type", ""),
        "total_pages": payload.get("total_pages", ""),
        "page_count": payload.get("page_count", ""),
        "images": ",".join(images),
        "缩略图路径": extra.get("thumb_rel", ""),
        "嵌入代码": f'<a href="{file_url}">{html.escape(str(payload.get("title", "")))}</a>',
        "嵌入iframe": f'<iframe src="{file_url}" width="100%" height="600" frameborder="0"></iframe>',
        "iframe地址": file_url,
        "原文网址": source_url,
    }


# ------------------------------------------------------------------ 縮略圖 / 歸檔
def month_day(ts=None):
    return datetime.fromtimestamp(ts or time.time()).strftime("%m%d")


def prepare_thumbnail(src_image, archive_root, md5, width, height, ts=None):
    """把首頁圖裁切為固定尺寸 JPG，存到 <archive_root>/d/file/pic/月日/{md5}_thumb.jpg。

    回傳 (網頁相對路徑, 絕對路徑)；src 不存在或未裝 PIL 時回傳 ("", None)。
    """
    if not src_image or not os.path.exists(src_image) or not archive_root:
        return "", None
    from PIL import Image, ImageOps
    rel_dir = f"/d/file/pic/{month_day(ts)}"
    rel = f"{rel_dir}/{md5}_thumb.jpg"
    dst = os.path.join(archive_root, rel.lstrip("/").replace("/", os.sep))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with Image.open(src_image) as im:
        im = im.convert("RGB")
        im = ImageOps.fit(im, (int(width), int(height)), Image.LANCZOS)
        im.save(dst, "JPEG", quality=90)
    return rel, dst


def archive_files(archive_root, local_files, ts=None, delete_source=False):
    """把本地圖片備份到 <archive_root>/d/file/pic/月日/原檔名，回傳 [(相對路徑, 絕對路徑)]。

    delete_source=True 時複製成功後刪除來源檔。
    """
    if not archive_root:
        return []
    done = []
    rel_dir = f"/d/file/pic/{month_day(ts)}"
    dst_dir = os.path.join(archive_root, rel_dir.lstrip("/").replace("/", os.sep))
    os.makedirs(dst_dir, exist_ok=True)
    for src in local_files or []:
        if not src or not os.path.exists(src):
            continue
        dst = os.path.join(dst_dir, os.path.basename(src))
        shutil.copy2(src, dst)
        done.append((f"{rel_dir}/{os.path.basename(src)}", dst))
        if delete_source and os.path.abspath(src) != os.path.abspath(dst):
            try:
                os.remove(src)
            except OSError:
                pass
    return done


# ------------------------------------------------------------------ HTTP 客戶端
class PublishClient:
    def __init__(self, cfg):
        g = lambda *keys: next((cfg.get(k) for k in keys if cfg.get(k) not in (None, "")), "")
        self.url = g("publish_api_url", "api_url")
        self.post_type = (g("publish_post_type", "post_type") or "form").lower()
        self.encoding = (g("publish_encoding", "encoding") or "utf-8").lower()
        self.timeout = int(cfg.get("publish_timeout", cfg.get("timeout", 20)))
        kws = cfg.get("publish_success_keywords", cfg.get("success_keywords"))
        self.success_keywords = kws if isinstance(kws, (list, tuple)) else [str(kws)] if kws else []

    def _encode(self, params):
        enc = "gbk" if self.encoding == "gbk" else "utf-8"
        if self.post_type == "json":
            import json
            body = json.dumps(params, ensure_ascii=False).encode(enc, errors="xmlcharrefreplace")
            ctype = f"application/json; charset={enc}"
        else:
            body = urlencode(params, encoding=enc, errors="xmlcharrefreplace").encode(enc)
            ctype = f"application/x-www-form-urlencoded; charset={enc}"
        return body, ctype, enc

    def send(self, params):
        """提交參數；回應本文命中成功標記時 success=True。"""
        if not self.url:
            return {"skipped": True, "reason": "publish_api_url 空白"}
        body, ctype, enc = self._encode(params)
        resp = requests.post(self.url, data=body, headers={"Content-Type": ctype}, timeout=self.timeout)
        resp.encoding = enc
        text = resp.text
        return {"status_code": resp.status_code, "text": text,
                "success": response_success(text, self.success_keywords)}


def response_success(text, keywords):
    return any(str(k) and str(k) in (text or "") for k in (keywords or []))


def parse_form_body(raw_bytes, content_type):
    """測試/除錯輔助：依 charset 解析收到的 form body 成 dict。

    不能用 parse_qs(..., encoding=...)：Python 3.8 的 bytes 路徑會再以 ascii 回編導致报错；
    也不可先整段 decode 再 parse_qs（其內部 unquote 預設 utf-8，會把 %XX 高位元組解成 U+FFFD）。
    """
    enc = "gbk" if "gbk" in (content_type or "").lower() else "utf-8"
    out = {}
    for pair in raw_bytes.decode("ascii", errors="replace").split("&"):
        if not pair:
            continue
        k, _, v = pair.partition("=")
        k = unquote(k.replace("+", " "), encoding=enc, errors="replace")
        v = unquote(v.replace("+", " "), encoding=enc, errors="replace")
        out[k] = v
    return out
