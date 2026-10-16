# processing/pipeline.py
"""單一文檔的處理流水線：② 轉 WebP + 提取文字 → ③ 阿里雲 AI → 寫 JSON → ④ 上傳 R2 → ⑤ 發布帝國 CMS。

JSON 結構建構集中在 build_json_payload，批次流水線與『統一文檔處理』工作台共用同一份，
避免 v4 兩處各寫一份造成欄位不一致。
"""
import os
import json
import time

from core.constants import R2_DIR_DOWN, R2_DIR_PIC, R2_DIR_JSON
from processing.extractor import PageExtractor
from processing.ai_client import AliyunAnalyzer, fallback_metadata
from processing.r2_client import R2Client
from processing.publish_client import (PublishClient, build_context, build_params,
                                       prepare_thumbnail, archive_files)


def _rebase_markdown(md, pub):
    """Markdown 內的圖片路徑是 R2 鍵（/d/flie/pic/xxx.webp）；有公開域名時加上域名前綴。"""
    if not pub or not md:
        return md
    return md.replace(f"](/{R2_DIR_PIC}/", f"]({pub}/{R2_DIR_PIC}/")


def build_json_payload(*, title, summary, keywords, file_size, ext, total_pages, md5, orig_name,
                       local_file_path, pages_data, public_domain="", with_time=False,
                       markdown="", figures=None):
    """精簡 JSON：只放雲端資料（不含 local_image 本機路徑、不含 questions 題庫）。"""
    pub = (public_domain or "").strip().rstrip("/")
    pages_list = []
    for p in pages_data:
        pages_list.append({
            "page": p["page"],
            "image": f"{pub}{p['r2_pic_key']}" if pub else p["r2_pic_key"],
            "width": p["width"],
            "height": p["height"],
            "content": p["content"],
            "figures": [pub + f["key"] for f in p.get("figures", []) if f.get("key")],
        })
    figures_list = [{"page": f["page"], "image": pub + f["key"],
                     "width": f.get("w_px"), "height": f.get("h_px")}
                    for f in (figures or []) if f.get("key")]
    file_key = f"/{R2_DIR_DOWN}/{md5}.{ext}"
    json_key = f"/{R2_DIR_JSON}/{md5}.json"
    payload = {
        "title": title,
        "summary": summary,
        "keywords": keywords,
        "file_size": file_size,
        "file_type": ext,
        "total_pages": total_pages,
        "page_count": total_pages,
        "md5": md5,
        "orig_name": orig_name,
        # R2 鍵 + 可直接取用的完整網址（未設公開域名時與鍵相同）
        "file_path": file_key,
        "file_url": f"{pub}{file_key}" if pub else file_key,
        "json_url": f"{pub}{json_key}" if pub else json_key,
        "pages": pages_list,
        "images": pages_list,
        "markdown": _rebase_markdown(markdown, pub),
        "figures": figures_list,
    }
    if with_time:
        payload["processed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return payload


def save_json(payload, json_dir, md5, shard=True):
    """JSON 同樣按 md5前2碼/第3碼分層（與下載檔一致），原子寫入。"""
    from core.utils import shard_path
    path = shard_path(json_dir, md5, "json", shard=shard)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 原子替換，避免上傳到寫一半的 JSON
    return path


class DocPipeline:
    def __init__(self, cfg, log=None):
        self.cfg = cfg or {}
        self.log = log or (lambda m: print(m))
        self.extractor = PageExtractor(self.cfg, self.log)
        self.analyzer = AliyunAnalyzer(self.cfg, self.log)
        self.r2 = R2Client(self.cfg)

    # ------------------------------------------------------------------ 步驟 4：上傳
    def upload_document(self, file_path, md5, ext, pages_data, json_path, figures=None):
        """先傳原檔、全部頁面 WebP 與插圖，最後才傳 JSON（JSON 不會指向尚未存在的圖片）。"""
        workers = int(self.cfg.get("r2_upload_workers", 4) or 4)
        skip = bool(self.cfg.get("r2_skip_existing", True))
        items = [(file_path, f"{R2_DIR_DOWN}/{md5}.{ext}")]
        items += [(p["webp_local_path"], f"{R2_DIR_PIC}/{p['webp_name']}") for p in pages_data]
        items += [(f["local_path"], f"{R2_DIR_PIC}/{f['name']}") for f in (figures or []) if f.get("name")]
        stats = self.r2.upload_many(items, workers=workers, skip_existing=skip)
        self.r2.upload_file(json_path, f"{R2_DIR_JSON}/{md5}.json", "application/json", skip_existing=False)
        stats["uploaded"] += 1
        return stats

    # ------------------------------------------------------------------ 步驟 5：發布帝國 CMS
    def publish_document(self, payload, pages_data=None, figures=None, source_url=""):
        """把文檔依「發布接口」頁的標籤映射發布到帝國 CMS；成功後縮略圖/配圖歸檔。

        回傳 PublishClient.send() 結果（skipped / success / status_code / text），網路或映射錯誤拋例外。
        """
        pages_data = pages_data or []
        figures = figures or []
        if not str(self.cfg.get("publish_api_url") or "").strip():
            return {"skipped": True, "reason": "publish_api_url 空白"}

        archive_root = str(self.cfg.get("publish_archive_dir") or "").strip()
        thumb_rel = ""
        if archive_root and pages_data:
            first_local = pages_data[0].get("webp_local_path")
            if first_local and os.path.exists(first_local):
                thumb_rel, _ = prepare_thumbnail(
                    first_local, archive_root, payload["md5"],
                    int(self.cfg.get("publish_thumb_width", 312) or 312),
                    int(self.cfg.get("publish_thumb_height", 208) or 208))

        src = source_url or ""
        if src.startswith("local://"):  # 本地掃描入庫的文檔沒有原始網址
            src = src[len("local://"):]
        extra = {
            "account": str(self.cfg.get("publish_account") or ""),
            "source_url": src,
            "file_url": payload.get("file_url") or payload.get("file_path") or src,
            "thumb_rel": thumb_rel,
        }
        params = build_params(self.cfg.get("publish_params", {}), build_context(payload, extra))
        self.log(f"   📤 [步驟5] 發布至帝國 CMS：{self.cfg.get('publish_api_url')}（{len(params)} 個字段）...")
        res = PublishClient(self.cfg).send(params)
        if res.get("success"):
            self.log(f"   ✅ [步驟5] 發布成功（HTTP {res.get('status_code')}，命中成功標記）")
            local_imgs = [p.get("webp_local_path") for p in pages_data if p.get("webp_local_path")]
            local_imgs += [f.get("local_path") for f in figures if f.get("local_path")]
            if archive_root and local_imgs:
                arc = archive_files(archive_root, local_imgs,
                                    delete_source=bool(self.cfg.get("publish_delete_source", False)))
                self.log(f"   🗄️ [步驟5] 已歸檔備份 {len(arc)} 張配圖至 {archive_root}/d/file/pic/月日/")
        else:
            self.log(f"   ⚠️ [步驟5] 發布未成功：HTTP {res.get('status_code')}，回應未命中任何成功標記")
        res["thumb_rel"] = thumb_rel
        return res

    # ------------------------------------------------------------------ 完整流程
    def run(self, file_path, md5, file_size, orig_name, keyword="", enable_ai=True, enable_r2=True,
            enable_publish=False, source_url=""):
        pic_dir = self.cfg.get("dir_pic") or "/d/file/pic"
        json_dir = self.cfg.get("dir_json") or "/d/file/json"
        ext = os.path.splitext(file_path)[1].lstrip(".").lower() or "pdf"

        self.log(f"   🖼️ [步驟2] 逐頁轉 WebP 並提取文字: {orig_name}")
        doc = self.extractor.extract_document(file_path, pic_dir, md5)
        pages, total = doc.pages, doc.total
        if doc.questions or doc.figures:
            self.log(f"   📝 結構還原：{len(doc.questions)} 題、{len(doc.figures)} 張插圖已綁定")

        meta = fallback_metadata(pages, orig_name, keyword)
        ai_status = "跳過AI(本地提取)"
        if enable_ai:
            try:
                self.log(f"   🤖 [步驟3] 發送代表頁與文字至阿里雲分析 (共 {total} 頁)...")
                res = self.analyzer.analyze(pages, total_pages=total, orig_name=orig_name)
                for k in ("title", "summary", "keywords"):
                    if res.get(k):
                        meta[k] = res[k]
                ai_status = "阿里雲智能體成功"
            except Exception as e:  # noqa: BLE001
                ai_status = f"AI失敗已備援({str(e)[:35]})"
                self.log(f"   ⚠️ [步驟3] 阿里雲失敗，改用本地提取備援: {e}")

        payload = build_json_payload(
            title=meta["title"], summary=meta["summary"], keywords=meta["keywords"], file_size=file_size,
            ext=ext, total_pages=total, md5=md5, orig_name=orig_name, local_file_path=file_path,
            pages_data=pages, public_domain=self.cfg.get("r2_public_domain", ""), with_time=True,
            markdown=doc.markdown, figures=doc.figures)
        json_path = save_json(payload, json_dir, md5, shard=bool(self.cfg.get("shard_storage", True)))

        r2_status = "未勾選R2上傳"
        if enable_r2:
            self.log(f"   ☁️ [步驟4] 上傳原檔、{len(pages)} 張頁面圖、{len(doc.figures)} 張插圖與 JSON 至 Cloudflare R2...")
            st = self.upload_document(file_path, md5, ext, pages, json_path, doc.figures)
            r2_status = f"已上傳R2(新傳{st['uploaded']}/跳過{st['skipped']})"

        publish_info = {"status": "off", "msg": "未勾選發布"}
        if enable_publish:
            try:
                ures = self.publish_document(payload, pages, doc.figures, source_url=source_url)
                if ures.get("skipped"):
                    publish_info = {"status": "skipped", "msg": f"發布跳過({ures.get('reason', '未設URL')})"}
                elif ures.get("success"):
                    publish_info = {"status": "done", "msg": f"帝國CMS發布成功(HTTP {ures['status_code']})"}
                else:
                    publish_info = {"status": "fail",
                                    "msg": f"發布失敗(HTTP {ures.get('status_code')}未命中標記)"}
            except Exception as e:  # noqa: BLE001  發布失敗不影響文檔生成結果，只記狀態
                publish_info = {"status": "fail", "msg": f"發布失敗({str(e)[:40]})"}
                self.log(f"   ⚠️ [步驟5] 帝國CMS 發布失敗: {e}")

        return {
            "json_path": json_path,
            "image_count": len(pages),
            "total_pages": total,
            "title": meta["title"],
            "r2_done": bool(enable_r2),
            "r2_msg": r2_status,
            "publish": publish_info,
            "summary_msg": f"{ai_status} | {r2_status} | {publish_info['msg']}",
            "json_payload": payload,
        }
