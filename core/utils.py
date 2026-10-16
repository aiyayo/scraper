# core/utils.py
"""與 GUI / 網路無關的通用小工具。"""
import os
import re
import csv
import sys
import time
import hashlib
import logging
import mimetypes
import subprocess
import urllib.parse
from logging.handlers import RotatingFileHandler


def domain_of(url):
    if url.startswith("local://"):
        return "(本地檔案)"
    return urllib.parse.urlsplit(url).netloc.lower()


def fmt_dur(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s" if m else f"{s}s"


def fmt_size(b):
    b = b or 0
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024 or unit == "GB":
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024


def safe_name(s, limit=80):
    s = re.sub(r'[\\/*?:"<>|\r\n\t]', "_", s).strip(" .")
    return s[:limit] or "unnamed"


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def md5_file(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def pic_hash(md5):
    """二次 MD5（對原 MD5 hex 字串再做一次 MD5）。
    pic 的路徑與檔名都用它，拿到圖片也無法反推原檔 MD5，也就猜不到 down/json 位址。"""
    return hashlib.md5(md5.encode("utf-8")).hexdigest()


def shard_dir(base, key, shard=True):
    """分層目錄：<base>/<key前2碼>/<key第3碼>/，例如 key=023.. → base/02/3/。"""
    folder = os.path.join(base, key[:2], key[2:3]) if shard else base
    os.makedirs(folder, exist_ok=True)
    return folder


def shard_path(base_dir, md5, ext, shard=True):
    """回傳檔案最終存放路徑並建立父目錄。
    shard=True : <base_dir>/<md5[0:2]>/<md5[2]>/<md5>.<ext>   （避免單一資料夾檔案過多）
    shard=False: <base_dir>/<md5>.<ext>
    """
    folder = shard_dir(base_dir, md5, shard)
    return os.path.join(folder, f"{md5}.{ext}")


def pic_shard_dir(pic_dir, md5, shard=True):
    """圖片用二次 MD5 分層：<pic_dir>/<二次MD5前2碼>/<第3碼>/。"""
    return shard_dir(pic_dir, pic_hash(md5), shard)


def open_path(path):
    """以系統預設程式開啟檔案/資料夾；失敗回傳錯誤訊息字串，成功回傳 None（不依賴 GUI）。"""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return None
    except Exception as e:  # pragma: no cover
        return str(e)


_CT_MAP = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "doc": "application/msword",
    "ppt": "application/vnd.ms-powerpoint",
    "xls": "application/vnd.ms-excel",
    "webp": "image/webp",
    "json": "application/json",
}


def guess_content_type(path):
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return _CT_MAP.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


def norm_proxy(s):
    s = (s or "").strip()
    if s and not re.match(r"^(https?|socks4|socks5)://", s, re.I):
        s = "http://" + s
    return s


def parse_keywords(text):
    """多行文字 → 去空白、去重（保持順序）的關鍵字清單。"""
    return list(dict.fromkeys(k.strip() for k in (text or "").splitlines() if k.strip()))


def retry_call(fn, attempts=3, base_delay=1.0, stop=None):
    """簡單的指數退避重試；stop 為 threading.Event 時可被中斷。"""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i == attempts - 1 or (stop is not None and stop.is_set()):
                break
            time.sleep(base_delay * (2 ** i))
    raise last


def setup_file_logger(log_dir, name="scraper"):
    """每日一檔、滾動保留的檔案日誌；回傳 logger（失敗時回傳 None，不影響主流程）。"""
    try:
        os.makedirs(log_dir, exist_ok=True)
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        path = os.path.join(log_dir, f"run_{time.strftime('%Y%m%d')}.log")
        for h in logger.handlers:
            if getattr(h, "baseFilename", None) == os.path.abspath(path):
                return logger
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()
        handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
        logger.propagate = False
        return logger
    except OSError:
        return None
