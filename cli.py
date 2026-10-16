# cli.py —— 無介面模式（適合伺服器 / 排程）
# 範例：python main.py --cli --mode pipeline -k "四年級數學教案" -k "五年級國語" --pages 3
#       python main.py --cli --mode process_only --no-r2
import argparse
import threading

from core.config import load_settings
from runner import TaskRunner, make_opts, MODES


def main():
    ap = argparse.ArgumentParser(description="統一文檔採集與智能處理（無介面）")
    ap.add_argument("--mode", choices=MODES, default="pipeline")
    ap.add_argument("-k", "--keyword", action="append", default=[], help="搜尋關鍵字，可重複")
    ap.add_argument("--keywords-file", help="每行一個關鍵字的文字檔")
    ap.add_argument("--ext", default=None, help="目標格式 pdf/docx/...")
    ap.add_argument("--pages", type=int, default=None, help="每詞最大搜尋頁數")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wayback", action="store_true")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--no-ai", action="store_true", help="關閉步驟③")
    ap.add_argument("--no-r2", action="store_true", help="關閉步驟④")
    ap.add_argument("--no-publish", action="store_true", help="關閉步驟⑤(發布至帝國CMS)")
    ap.add_argument("--no-process", action="store_true", help="關閉步驟②~⑤")
    a = ap.parse_args()

    cfg = load_settings()
    if a.no_ai:
        cfg["step3_aliyun_ai"] = False
    if a.no_r2:
        cfg["step4_upload_r2"] = False
    if a.no_publish:
        cfg["step5_publish_cms"] = False
    if a.no_process:
        cfg["step2_extract"] = False

    saved = cfg.get("crawl_opts") or {}
    over = {"headless": a.headless or bool(saved.get("headless")), "wayback": a.wayback,
            "retry_failed": a.retry_failed}
    if a.ext:
        over["file_ext"] = a.ext
    elif saved.get("file_ext"):
        over["file_ext"] = saved["file_ext"]
    if a.pages:
        over["max_pages"] = a.pages
    if a.workers:
        over["workers"] = a.workers
    for k in ("dom_min", "dom_max", "pg_min", "pg_max", "proc_workers", "max_retries"):
        if k in saved and str(saved[k]).isdigit():
            over[k] = int(saved[k])

    keywords = list(a.keyword)
    if a.keywords_file:
        with open(a.keywords_file, encoding="utf-8") as f:
            keywords += [ln.strip() for ln in f if ln.strip()]

    stop = threading.Event()
    runner = TaskRunner(print, stop)
    try:
        runner.run(a.mode, keywords, cfg["dir_down"], make_opts(cfg, **over))
    except KeyboardInterrupt:
        stop.set()
        print("已中斷")
    finally:
        runner.close()
    return 0
