# core/config.py
"""設定檔：預設值、讀寫（原子寫入）、環境變數覆蓋、v4 設定自動遷移。"""
import os
import sys
import json

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_FILE = os.path.join(BASE_DIR, "user_settings_v5.json")
_LEGACY_FILES = [
    os.path.join(BASE_DIR, "user_settings_v4.json"),
    os.path.join(os.path.dirname(BASE_DIR), "scraper_v4", "user_settings_v4.json"),
]

DEFAULT_PROMPT = """你是一個專業的文檔分析智能體。請根據提供的文檔資訊與頁面內容，提取並總結出準確的「標題」、「簡介」與「關鍵詞」。

【文檔基本資訊】
- 原始檔名：{orig_name}
- 文檔總頁數：{total_pages} 頁
- 提取文字預覽：
{text_preview}

【輸出要求】
請嚴格以純 JSON 格式回傳以下欄位（不要加上 ```json 標記或任何額外說明）：
{
  "title": "準確且精煉的文檔標題",
  "summary": "文檔核心內容簡介（約 100~200 字，清楚說明文檔用途與重點）",
  "keywords": ["關鍵詞1", "關鍵詞2", "關鍵詞3", "關鍵詞4", "關鍵詞5"]
}"""

_WIN = sys.platform.startswith("win")

DEFAULT_SETTINGS = {
    # ---- 阿里雲百煉 / DashScope（只用智能體 Application）----
    "aliyun_api_key": "",
    "aliyun_app_id": "",
    "aliyun_max_sample_pages": 2,
    "aliyun_prompt": DEFAULT_PROMPT,
    "aliyun_retry": 2,                 # 失敗後額外重試次數
    "ai_preview_chars": 2000,          # 送給模型的文字預覽上限
    "ai_image_max_side": 1280,         # 送給模型的圖片最長邊（縮圖省 token / 避免超限）

    # ---- Cloudflare R2 ----
    "r2_endpoint_url": "https://<ACCOUNT_ID>.r2.cloudflarestorage.com",
    "r2_access_key": "",
    "r2_secret_key": "",
    "r2_bucket_name": "",
    "r2_public_domain": "",
    "r2_upload_workers": 4,            # 單文檔內多張圖並行上傳
    "r2_skip_existing": True,          # R2 已有同大小物件則跳過（可重複執行）

    # ---- 本地目錄（預設在軟體運行目錄下的 data/，自動建立 down/json/pic；DB 獨立於 db/）----
    "storage_root": os.path.join(BASE_DIR, "data"),
    "dir_down": os.path.join(BASE_DIR, "data", "down"),
    "dir_pic": os.path.join(BASE_DIR, "data", "pic"),
    "dir_json": os.path.join(BASE_DIR, "data", "json"),
    "dir_db": os.path.join(BASE_DIR, "data", "db"),
    "shard_storage": True,             # 下載/JSON 按 md5前2位/第3位/ 分層；pic 按二次MD5同規則分層

    # ---- 轉圖 / 提取 ----
    "webp_zoom": 2.0,
    "webp_quality": 85,
    "webp_max_width": 1600,            # 圖片最大寬度 px（0=不限，依 webp_zoom 渲染）
    "reuse_existing_webp": True,       # 已有 WebP 則不重繪
    "max_render_pages": 0,             # 0 = 不限制；超大 PDF 可設 200
    "use_office_com": True,            # Office 檔經 Windows Office（優先）/WPS（備援）轉圖，僅 Windows
    "text_layout": "smart",            # smart=智能版面還原(多欄/表格/去頁眉頁腳) | plain=PyMuPDF 純文字
    "layout_tables": True,             # 表格轉 Markdown（find_tables 較耗時，可關）
    "layout_join_lines": True,         # 折行合併成段落
    "text_normalize": True,            # 全形英數轉半形、去除中文間多餘空格、規範填空括號/底線
    "ocr_engine": "paddle",            # off | paddle | tesseract：只處理「掃描頁 / 文字層亂碼」的頁
    "paddle_lang": "ch",               # ch(簡繁中英) | chinese_cht(繁體) | en
    "ocr_min_conf": 0.5,               # OCR 信心度低於此值的文字丟棄
    "extract_figures": True,           # 偵測並裁切插圖（WebP），綁定到題目 / 所在位置
    "figure_zoom": 3.0,                # 插圖裁切渲染倍率
    "md_structure": True,              # 整份文件還原成結構化 Markdown（大題/題號/選項/表格/插圖）+ 題庫清單
    "option_style": "list",            # list=逐行並排(每選項一行) | inline=單行並排 | auto=短選項單行/長選項逐行

    # ---- 發布接口（帝國 CMS ecmsinfo.php，獨立選項卡；動態標籤欄位映射 + 成功後歸檔）----
    "publish_api_url": "https://www.imomomenu.com/e/admin/ecmsinfo.php",
    "publish_post_type": "form",       # form(application/x-www-form-urlencoded) | json
    "publish_encoding": "utf-8",       # utf-8 | gbk
    "publish_timeout": 20,
    "publish_thumb_width": 312,        # 縮略圖寬 px（titlepic，輸出 /d/file/pic/月日/ 檔名）
    "publish_thumb_height": 208,       # 縮略圖高 px
    "publish_success_keywords": ["发送成功", "编辑成功", "增加信息成功", "SUCCESS"],
    "publish_archive_dir": "",         # 發布成功後歸檔目標根目錄（其下複刻 d/file/pic/月日/）
    "publish_delete_source": False,    # 歸檔後刪除本地原始檔
    "publish_account": "",             # [账号名] 標籤取值
    "publish_params": {                # 動態發布欄位映射：POST 欄位名 → 標籤或固定值
        "title": "[标题]",
        "classid": "1",
        "smalltext": "[摘要]",
        "newstext": "[内容]",
        "titlepic": "[缩略图路径]",
        "newstime": "[发布时间戳]",
    },

    # ---- 步驟⑤：發布至帝國 CMS（Cloudflare R2 上傳後執行；設定見「發布接口」選項卡）----
    "step5_publish_cms": True,

    # ---- 步驟①~④開關 ----
    "step1_download": True,
    "step2_extract": True,
    "step3_aliyun_ai": True,
    "step4_upload_r2": True,

    # ---- 採集頁籤選項（下次啟動自動還原） ----
    "crawl_opts": {},
}

_ENV_OVERRIDES = {
    "aliyun_api_key": ("DASHSCOPE_API_KEY",),
    "r2_access_key": ("R2_ACCESS_KEY_ID", "R2_ACCESS_KEY"),
    "r2_secret_key": ("R2_SECRET_ACCESS_KEY", "R2_SECRET_KEY"),
}


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def load_settings():
    data = dict(DEFAULT_SETTINGS)
    src = SETTINGS_FILE if os.path.exists(SETTINGS_FILE) else next(
        (p for p in _LEGACY_FILES if os.path.exists(p)), None)
    raw = {}
    if src:
        try:
            raw = _read_json(src)
            data.update(raw)
        except Exception as e:
            print(f"讀取設定檔失敗，使用預設值: {e}")
    # 單一根目錄：down/json/pic 一律由 storage_root 派生（自動建立相關目錄）
    # 相對路徑（例如預設的 data）以軟體運行目錄（BASE_DIR）為基準，不隨啟動時 CWD 漂移
    root = str(data.get("storage_root") or "").strip().rstrip("\\/")
    if root:
        if not os.path.isabs(root):
            root = os.path.abspath(os.path.join(BASE_DIR, root))
        data["storage_root"] = root
        data["dir_down"] = os.path.join(root, "down")
        data["dir_json"] = os.path.join(root, "json")
        data["dir_pic"] = os.path.join(root, "pic")
        data["dir_db"] = os.path.join(root, "db")  # 採集資料庫獨立目錄，不與 down/json/pic 混在一起
    # 舊版步驟⑤（純 JSON POST）已移除，統一由帝國 CMS 發布接口負責 POST；清理舊設定
    for _old in ("step5_post_data", "post_url", "post_method", "post_headers",
                 "post_timeout", "post_fields"):
        data.pop(_old, None)
    # 舊版帝國發布開關叫 step6_publish_cms（當時是步驟⑥），自動沿用為步驟⑤（以檔案原值為準，而非預設值）
    if "step5_publish_cms" not in raw and "step6_publish_cms" in raw:
        data["step5_publish_cms"] = raw["step6_publish_cms"]
    data.pop("step6_publish_cms", None)
    if not isinstance(data.get("publish_params"), dict):
        data["publish_params"] = dict(DEFAULT_SETTINGS["publish_params"])
    if not isinstance(data.get("publish_success_keywords"), list):
        data["publish_success_keywords"] = list(DEFAULT_SETTINGS["publish_success_keywords"])
    data.pop("use_libreoffice", None)  # 舊版 LibreOffice 開關已由 use_office_com 取代
    for key, envs in _ENV_OVERRIDES.items():
        if not str(data.get(key) or "").strip():
            for env in envs:
                if os.environ.get(env):
                    data[key] = os.environ[env]
                    break
    return data


def derive_dirs(storage_root):
    """給 GUI 即時預覽／收集用：root → down/json/pic。"""
    root = str(storage_root or "").strip().rstrip("\\/")
    return {
        "storage_root": root,
        "dir_down": os.path.join(root, "down") if root else "",
        "dir_json": os.path.join(root, "json") if root else "",
        "dir_pic": os.path.join(root, "pic") if root else "",
        "dir_db": os.path.join(root, "db") if root else "",
    }


def save_settings(new_settings):
    """原子寫入，避免寫到一半當機造成設定檔損毀。"""
    data = load_settings()
    data.update(new_settings)
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
        try:
            os.chmod(SETTINGS_FILE, 0o600)  # 內含金鑰，限制僅本人可讀（Windows 會忽略）
        except OSError:
            pass
        return True
    except Exception as e:
        print(f"儲存設定檔失敗: {e}")
        return False
