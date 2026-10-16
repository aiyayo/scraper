# core/constants.py
"""全局常量：搜尋選擇器、UA、副檔名、R2 路徑前綴等（集中管理，避免散落各處）。"""

# ---------------- Yahoo 搜尋 ----------------
YAHOO_SEARCH_URL = "https://tw.search.yahoo.com/search"
YAHOO_REFERER = "https://tw.yahoo.com/"
RESULT_SELECTOR = "#web h3 a, #results h3 a, div.compTitle a, div.algo a"
NEXT_SELECTORS = [
    ".compPagination a.next",
    "div.pages a.next",
    "a.next",
    "a[title='下一頁']",
    "a.next-page",
]
UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

# ---------------- 下載 ----------------
MIN_FILE_BYTES = 1024
ALL_EXTS = ["pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx"]
NET_KINDS = ("dns", "conn", "timeout", "ssl")
# ua: UA_LIST 索引 / referer: origin|yahoo|None / close: 是否關閉長連線 / legacy: 舊式 TLS
STRATEGIES = [
    {"ua": 0, "referer": "origin", "close": False, "legacy": False},
    {"ua": 1, "referer": None, "close": True, "legacy": False},
    {"ua": 0, "referer": "yahoo", "close": True, "legacy": True},
]
WAYBACK_PREFIX = "https://web.archive.org/web/2id_/"

# ---------------- 後處理 ----------------
NATIVE_PROC_EXTS = ("pdf", "docx", "pptx", "xlsx")
LEGACY_PROC_EXTS = ("doc", "ppt", "xls")  # 舊格式，需 Windows Office / WPS 轉檔
OFFICE_EXTS = ("docx", "pptx", "xlsx", "doc", "ppt", "xls")

# ---------------- Cloudflare R2 鍵前綴（沿用原規格，含 flie 拼寫） ----------------
R2_DIR_DOWN = "d/file/down"
R2_DIR_PIC = "d/flie/pic"
R2_DIR_JSON = "d/flie/json"
