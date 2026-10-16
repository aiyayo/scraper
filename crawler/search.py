# crawler/search.py
"""Yahoo TW 搜尋採集（Selenium）。

相對 v4 的修正 / 強化：
- 修正 v4 搜尋網址被寫成 Markdown 連結 "[https://...](https://...)" 導致無法開啟的問題。
- 翻頁：優先使用頁面上「下一頁」連結的真實 href（帶有 Yahoo 的分頁 token），
  取不到再退回 b= 位移；v4 定義了 NEXT_SELECTORS 卻從未使用。
- 位移依「實際取得的結果數」推進，而不是固定 +10。
- 連結批次入庫（單次 commit）。
"""
import re
import time
import random
import urllib.parse

from core.constants import RESULT_SELECTOR, NEXT_SELECTORS, UA_LIST, YAHOO_SEARCH_URL
from core.utils import domain_of, norm_proxy


def is_excluded(url, exclude):
    host = domain_of(url)
    return any(host == d or host.endswith("." + d) for d in exclude)


def extract_real_url(href):
    """Yahoo 的結果連結常為 .../RU=<真實網址>/RK=... 的跳轉格式，這裡還原成真實網址。"""
    m = re.search(r"/RU=(.+?)/R[KS]=", href) or re.search(r"[/?&]RU=([^/&]+)", href)
    return urllib.parse.unquote(m.group(1)) if m else href


class YahooSearcher:
    def __init__(self, db, log, stop_event):
        self.db = db
        self.log = log
        self.stop = stop_event
        self.driver = None

    # ------------------------------------------------------------------ 瀏覽器
    def quit_driver(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def _sleep(self, sec):
        end = time.time() + sec
        while time.time() < end and not self.stop.is_set():
            time.sleep(0.3)

    def ensure_driver(self, opt):
        """相容新版 Chrome：不帶會觸發 InvalidArgumentException 的舊參數與空代理參數。"""
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        if self.driver:
            try:
                _ = self.driver.current_url
                return self.driver
            except Exception:
                self.quit_driver()

        def make_opts(with_exp=True):
            o = Options()
            for arg in ("--window-size=1280,900", "--ignore-certificate-errors", "--lang=zh-TW",
                        "--disable-blink-features=AutomationControlled", "--disable-gpu",
                        "--no-sandbox", "--disable-dev-shm-usage", f"--user-agent={UA_LIST[0]}"):
                o.add_argument(arg)
            proxy = norm_proxy(opt.get("proxy"))
            if proxy:
                o.add_argument(f"--proxy-server={proxy}")
            if opt.get("headless"):
                o.add_argument("--headless=new")
            if with_exp:
                o.add_experimental_option("excludeSwitches", ["enable-automation"])
            return o

        try:
            self.driver = webdriver.Chrome(options=make_opts(True))
        except Exception:
            self.driver = webdriver.Chrome(options=make_opts(False))
        self.driver.set_page_load_timeout(40)
        return self.driver

    # ------------------------------------------------------------------ 頁面解析
    def _wait_results(self, driver, opt):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        try:
            WebDriverWait(driver, 12).until(EC.presence_of_element_located((By.CSS_SELECTOR, RESULT_SELECTOR)))
            return "ok"
        except Exception:
            pass
        if not driver.find_elements(By.CSS_SELECTOR, "#web, #results, #main"):
            if opt.get("headless"):
                return "blocked"
            self.log("   ⚠️ 請在瀏覽器中手動完成驗證 (最多等待 120 秒)...")
            end = time.time() + 120
            while time.time() < end and not self.stop.is_set():
                if driver.find_elements(By.CSS_SELECTOR, RESULT_SELECTOR):
                    return "ok"
                time.sleep(2)
            return "blocked"
        return "empty"

    def _page_links(self, driver):
        try:
            hrefs = driver.execute_script(
                "return Array.from(document.querySelectorAll(arguments[0])).map(a => a.href).filter(Boolean);",
                RESULT_SELECTOR) or []
        except Exception:
            return []
        out = []
        for h in hrefs:
            if not str(h).startswith("http"):
                continue
            real = extract_real_url(h).split("#")[0]
            host = urllib.parse.urlsplit(real).netloc.lower()
            if not host or re.search(r"(^|\.)yahoo\.", host) or "yimg." in host:
                continue
            out.append(real)
        return list(dict.fromkeys(out))

    def _next_href(self, driver):
        """取得『下一頁』連結的 href；找不到回傳 None。"""
        from selenium.webdriver.common.by import By
        for sel in NEXT_SELECTORS:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    href = el.get_attribute("href")
                    if href and href.startswith("http"):
                        return href
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------ 主流程
    def collect_links(self, keyword, opt):
        """採集單一關鍵字的多頁結果；回傳 True 表示被驗證頁攔截（呼叫端應停止後續關鍵字）。"""
        ext = str(opt.get("file_ext") or "pdf").strip().lstrip(".")
        max_pages = int(opt.get("max_pages") or 5)
        query = urllib.parse.urlencode({"p": f"{keyword} filetype:{ext}", "pz": "10"})
        base_url = f"{YAHOO_SEARCH_URL}?{query}"

        self.db.start_keyword(keyword, ext)
        t0 = time.time()
        pages = found = new = stale = 0
        status, err, blocked = "done", None, False
        try:
            driver = self.ensure_driver(opt)
            prev_sig = None
            next_url = base_url
            offset = 1
            for page in range(1, max_pages + 1):
                if self.stop.is_set():
                    status, err = "pending", "已停止"
                    break
                self.log(f"   📄 第 {page}/{max_pages} 頁")
                driver.get(next_url)
                res = self._wait_results(driver, opt)
                if res == "blocked":
                    blocked, status, err = True, "pending", "被驗證頁攔截"
                    break
                if res == "empty":
                    break
                links = self._page_links(driver)
                sig = tuple(links[:5])
                if not links or sig == prev_sig:  # 空頁或與上一頁相同 → 已到盡頭 / 翻頁失效
                    if links:
                        self.log("   ⚠️ 此頁與上一頁結果相同，停止翻頁")
                    break
                prev_sig = sig

                allowed = [u for u in links if not is_excluded(u, opt.get("exclude") or [])]
                n_new = self.db.add_pending_links(allowed, keyword, ext)
                pages += 1
                found += len(allowed)
                new += n_new
                self.log(f"   ✔️ 擷取 {len(allowed)} 個連結，新增 {n_new} 個")

                stale = stale + 1 if n_new == 0 else 0
                if opt.get("stale_limit") and stale >= opt["stale_limit"]:
                    self.log(f"   ⏭ 連續 {stale} 頁無新連結，停止翻頁")
                    break
                if page >= max_pages:
                    break

                # 準備下一頁：優先真實「下一頁」連結，其次 b= 位移（依實際結果數推進）
                offset += max(len(links), 10)
                nxt = self._next_href(driver)
                next_url = nxt or f"{base_url}&b={offset}"
                self._sleep(random.uniform(opt.get("pg_min", 2), opt.get("pg_max", 5)))
        except Exception as e:
            status, err = "pending", f"{type(e).__name__}: {str(e)[:120]}"
            self.log(f"   ❌ 瀏覽器錯誤: {err}")
            self.quit_driver()
        self.db.finish_keyword(keyword, status, time.time() - t0, pages, found, new, err)
        return blocked
