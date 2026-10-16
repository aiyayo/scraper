# core/db.py
"""SQLite 資料庫：關鍵字、下載記錄、後處理狀態（total_pages / AI 標題 / JSON 路徑）。"""
import os
import time
import sqlite3
import threading

from core.constants import MIN_FILE_BYTES
from core.utils import domain_of


class DownloadDB:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.version = 0  # 每次寫入 +1，GUI 用來判斷是否需要重繪
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            c = self.conn
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("CREATE TABLE IF NOT EXISTS keywords(keyword TEXT PRIMARY KEY, status TEXT, updated_at TEXT)")
            c.execute("""CREATE TABLE IF NOT EXISTS downloads(
                url TEXT PRIMARY KEY, status TEXT, md5 TEXT, file_path TEXT, size INTEGER,
                err_type TEXT, err_msg TEXT, fail_count INTEGER DEFAULT 0, keyword TEXT,
                first_seen TEXT, updated_at TEXT)""")
            self._ensure_cols("keywords", {
                "file_ext": "TEXT", "started_at": "TEXT", "finished_at": "TEXT", "duration_sec": "REAL",
                "pages_crawled": "INTEGER DEFAULT 0", "links_found": "INTEGER DEFAULT 0",
                "new_links": "INTEGER DEFAULT 0", "run_count": "INTEGER DEFAULT 0", "last_error": "TEXT"})
            self._ensure_cols("downloads", {
                "domain": "TEXT", "next_retry_at": "TEXT", "file_ext": "TEXT", "elapsed": "REAL",
                "orig_name": "TEXT", "proc_status": "TEXT", "proc_msg": "TEXT", "json_path": "TEXT",
                "image_count": "INTEGER", "total_pages": "INTEGER", "ai_title": "TEXT", "proc_at": "TEXT",
                # 流水線末端狀態：④ Cloudflare 上傳、⑤ 帝國CMS 發布（NULL=尚未執行）
                "r2_status": "TEXT", "r2_at": "TEXT",
                "pub_status": "TEXT", "pub_at": "TEXT", "pub_msg": "TEXT"})
            for r in c.execute("SELECT url FROM downloads WHERE domain IS NULL OR domain=''").fetchall():
                c.execute("UPDATE downloads SET domain=? WHERE url=?", (domain_of(r["url"]), r["url"]))
            c.execute("CREATE INDEX IF NOT EXISTS idx_md5 ON downloads(md5)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_status ON downloads(status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_domain ON downloads(domain)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_kw ON downloads(keyword)")
            c.commit()

    # ------------------------------------------------------------------ 基礎
    def _ensure_cols(self, table, cols):
        have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @staticmethod
    def _now():
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _commit(self):
        self.conn.commit()
        self.version += 1

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ 關鍵字
    def add_keywords(self, keywords, ext, force=False):
        with self.lock:
            now, todo = self._now(), []
            for kw in dict.fromkeys(k.strip() for k in keywords if k.strip()):
                row = self.conn.execute("SELECT status FROM keywords WHERE keyword=?", (kw,)).fetchone()
                if not row:
                    self.conn.execute(
                        "INSERT INTO keywords(keyword,status,updated_at,file_ext) VALUES(?, 'pending', ?, ?)",
                        (kw, now, ext))
                    todo.append(kw)
                elif row["status"] != "done" or force:
                    self.conn.execute(
                        "UPDATE keywords SET status='pending', file_ext=?, updated_at=? WHERE keyword=?",
                        (ext, now, kw))
                    todo.append(kw)
            self._commit()
            return todo

    def start_keyword(self, kw, ext):
        with self.lock:
            now = self._now()
            self.conn.execute("""UPDATE keywords SET status='running', file_ext=?, started_at=?, finished_at=NULL,
                duration_sec=NULL, pages_crawled=0, links_found=0, new_links=0,
                run_count=COALESCE(run_count,0)+1, last_error=NULL, updated_at=? WHERE keyword=?""",
                              (ext, now, now, kw))
            self._commit()

    def finish_keyword(self, kw, status, duration, pages, found, new, err=None):
        with self.lock:
            now = self._now()
            self.conn.execute("""UPDATE keywords SET status=?, finished_at=?, duration_sec=?, pages_crawled=?,
                links_found=?, new_links=?, last_error=?, updated_at=? WHERE keyword=?""",
                              (status, now, duration, pages, found, new, err, now, kw))
            self._commit()

    def keyword_stats(self, flt=""):
        with self.lock:
            sql = """SELECT k.*, COALESCE(d.ok,0) ok, COALESCE(d.dup,0) dup, COALESCE(d.fail,0) fail,
                        COALESCE(d.pending,0) pending, COALESCE(d.skip,0) skip, COALESCE(d.total,0) total
                     FROM keywords k LEFT JOIN (
                        SELECT keyword, SUM(status='ok') ok, SUM(status='dup') dup, SUM(status='fail') fail,
                               SUM(status='pending') pending, SUM(status='skip') skip, COUNT(*) total
                        FROM downloads GROUP BY keyword) d ON d.keyword=k.keyword"""
            args = []
            if flt:
                sql += " WHERE k.keyword LIKE ?"
                args.append(f"%{flt}%")
            sql += " ORDER BY COALESCE(k.started_at, k.updated_at) DESC"
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def count_keywords(self):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0]

    # ------------------------------------------------------------------ 連結 / 下載
    def add_pending_link(self, url, keyword, ext):
        return self.add_pending_links([url], keyword, ext) == 1

    def add_pending_links(self, urls, keyword, ext):
        """批次新增待下載連結（單次 commit），回傳真正新增的數量。"""
        with self.lock:
            now, new = self._now(), 0
            for url in urls:
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO downloads(url,domain,status,keyword,file_ext,first_seen,updated_at,fail_count)
                       VALUES(?,?, 'pending', ?, ?, ?, ?, 0)""",
                    (url, domain_of(url), keyword, ext, now, now))
                new += cur.rowcount
            self._commit()
            return new

    def lookup(self, url):
        with self.lock:
            row = self.conn.execute("SELECT * FROM downloads WHERE url=?", (url,)).fetchone()
            return dict(row) if row else None

    def _upsert(self, url, status, md5=None, path=None, size=None, err_type=None, err_msg=None,
                fail_count=0, keyword=None, next_retry_at=None, file_ext=None, elapsed=None, orig_name=None):
        now = self._now()
        self.conn.execute("""
            INSERT INTO downloads(url,domain,status,md5,file_path,size,err_type,err_msg,fail_count,keyword,
                                  first_seen,updated_at,next_retry_at,file_ext,elapsed,orig_name)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
                status=excluded.status, md5=excluded.md5, file_path=excluded.file_path, size=excluded.size,
                err_type=excluded.err_type, err_msg=excluded.err_msg, fail_count=excluded.fail_count,
                keyword=COALESCE(excluded.keyword, keyword), updated_at=excluded.updated_at,
                next_retry_at=excluded.next_retry_at, file_ext=COALESCE(excluded.file_ext, file_ext),
                elapsed=excluded.elapsed, orig_name=COALESCE(excluded.orig_name, orig_name)
        """, (url, domain_of(url), status, md5, path, size, err_type, err_msg, fail_count, keyword,
              now, now, next_retry_at, file_ext, elapsed, orig_name))
        self._commit()

    def claim_ok(self, url, md5, path, size, keyword, ext, elapsed, orig_name=None):
        """登記下載成功。若相同 MD5 的檔案已存在，登記為 dup 並回傳既有路徑，否則回傳 None。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT file_path FROM downloads WHERE md5=? AND status='ok' AND url<>?", (md5, url)).fetchall()
            for r in rows:
                p = r["file_path"]
                if p and os.path.exists(p):
                    self._upsert(url, "dup", md5, p, size, keyword=keyword, file_ext=ext,
                                 elapsed=elapsed, orig_name=orig_name)
                    return p
            self._upsert(url, "ok", md5, path, size, keyword=keyword, file_ext=ext,
                         elapsed=elapsed, orig_name=orig_name)
            self.conn.execute("UPDATE downloads SET proc_status='pending', proc_msg=NULL WHERE url=?", (url,))
            self._commit()
            return None

    def record_fail(self, url, err_type, err_msg, max_retries, retry_interval):
        with self.lock:
            row = self.conn.execute(
                "SELECT fail_count, keyword, file_ext FROM downloads WHERE url=?", (url,)).fetchone()
            cnt = (row["fail_count"] if row and row["fail_count"] else 0) + 1
            nxt = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + retry_interval))
                   if cnt < max_retries else None)  # None = 已達重試上限，停止自動重試
            self._upsert(url, "fail", err_type=err_type, err_msg=(err_msg or "")[:300], fail_count=cnt,
                         keyword=row["keyword"] if row else None, next_retry_at=nxt,
                         file_ext=row["file_ext"] if row else None)

    def record_skip(self, url, reason):
        with self.lock:
            self._upsert(url, "skip", err_type="invalid_format", err_msg=(reason or "")[:300])

    def get_links_to_download(self, retry_failed):
        """待下載清單。
        修正：原版 retry_failed 會把「已達重試上限(next_retry_at 為空)」的連結也撈出來無限重試，
        現在只撈「已到冷卻時間」的失敗項；要強制重來請用『重置失敗項』。
        """
        with self.lock:
            sql = "SELECT url, keyword, file_ext FROM downloads WHERE url NOT LIKE 'local://%' AND ("
            args = []
            if retry_failed:
                sql += "status='pending' OR (status='fail' AND next_retry_at IS NOT NULL AND next_retry_at <= ?)"
                args.append(self._now())
            else:
                sql += "status='pending'"
            sql += ") ORDER BY first_seen"
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def reset_failed(self, domain=None):
        with self.lock:
            sql = "UPDATE downloads SET status='pending', fail_count=0, next_retry_at=NULL WHERE status='fail'"
            args = []
            if domain:
                sql += " AND domain=?"
                args.append(domain)
            n = self.conn.execute(sql, args).rowcount
            self._commit()
            return n

    def reset_urls(self, urls):
        with self.lock:
            for u in urls:
                self.conn.execute(
                    "UPDATE downloads SET status='pending', fail_count=0, next_retry_at=NULL WHERE url=?", (u,))
            self._commit()

    def delete_urls(self, urls):
        with self.lock:
            for u in urls:
                self.conn.execute("DELETE FROM downloads WHERE url=?", (u,))
            self._commit()

    def verify_files(self, min_bytes=MIN_FILE_BYTES):
        """自我修復：本地檔案遺失或過小的『成功』記錄 → 改回待下載，並回報數量。
        （對應『下載有時是空檔案』的問題：下次下載會重新抓取）"""
        missing = small = 0
        with self.lock:
            rows = self.conn.execute(
                "SELECT url, file_path FROM downloads WHERE status IN ('ok','dup') AND url NOT LIKE 'local://%'"
            ).fetchall()
            for r in rows:
                p = r["file_path"]
                bad = None
                if not p or not os.path.exists(p):
                    bad = "missing"
                else:
                    try:
                        if os.path.getsize(p) < min_bytes:
                            bad = "small"
                    except OSError:
                        bad = "missing"
                if bad:
                    if bad == "missing":
                        missing += 1
                    else:
                        small += 1
                    self.conn.execute(
                        "UPDATE downloads SET status='pending', fail_count=0, next_retry_at=NULL, "
                        "proc_status=NULL, proc_msg=NULL WHERE url=?", (r["url"],))
            self._commit()
        return {"missing": missing, "small": small}

    # ------------------------------------------------------------------ 後處理
    def pending_process(self, include_failed=True):
        with self.lock:
            cond = "proc_status IS NULL OR proc_status='pending'" + (" OR proc_status='fail'" if include_failed else "")
            return [r["url"] for r in self.conn.execute(
                f"SELECT url FROM downloads WHERE status='ok' AND ({cond}) ORDER BY updated_at").fetchall()]

    def set_proc(self, url, status, msg="", json_path=None, image_count=None, total_pages=None, ai_title=None):
        with self.lock:
            self.conn.execute("""UPDATE downloads SET proc_status=?, proc_msg=?, json_path=?,
                                 image_count=?, total_pages=?, ai_title=?, proc_at=? WHERE url=?""",
                              (status, (msg or "")[:300], json_path, image_count, total_pages or image_count,
                               (ai_title or "")[:200], self._now(), url))
            self._commit()

    def set_r2(self, url, status, msg=""):
        """登記步驟④ Cloudflare 上傳結果：done=已上傳 / skipped=跳過 / fail=失敗。"""
        with self.lock:
            self.conn.execute("UPDATE downloads SET r2_status=?, r2_at=? WHERE url=?",
                              (status, self._now(), url))
            self._commit()

    def set_publish(self, url, status, msg=""):
        """登記步驟⑤帝國CMS 發布結果：done=成功 / skipped=未設定接口 / fail=失敗(含未命中成功標記)。"""
        with self.lock:
            self.conn.execute("UPDATE downloads SET pub_status=?, pub_msg=?, pub_at=? WHERE url=?",
                              (status, (msg or "")[:300], self._now(), url))
            self._commit()

    def proc_stats(self):
        with self.lock:
            out = {r["s"]: r["c"] for r in self.conn.execute(
                "SELECT COALESCE(proc_status,'pending') s, COUNT(*) c FROM downloads WHERE status='ok' GROUP BY s")}
            out["_images"] = self.conn.execute(
                "SELECT COALESCE(SUM(image_count),0) FROM downloads WHERE status='ok'").fetchone()[0]
            out["_pages"] = self.conn.execute(
                "SELECT COALESCE(SUM(total_pages),0) FROM downloads WHERE status='ok'").fetchone()[0]
            return out

    def proc_rows(self, flt="", limit=3000):
        with self.lock:
            sql = "SELECT * FROM downloads WHERE status='ok'"
            args = []
            if flt:
                sql += " AND (orig_name LIKE ? OR ai_title LIKE ? OR url LIKE ? OR keyword LIKE ? OR proc_msg LIKE ?)"
                args += [f"%{flt}%"] * 5
            sql += " ORDER BY COALESCE(proc_at, updated_at) DESC LIMIT ?"
            args.append(limit)
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def reset_proc(self, urls=None):
        with self.lock:
            # 重新執行後處理時一併清掉 ④R2 / ⑤發布 的舊結果，避免監控頁顯示過期狀態
            if urls is None:
                self.conn.execute(
                    "UPDATE downloads SET proc_status='pending', r2_status=NULL, r2_at=NULL, "
                    "pub_status=NULL, pub_at=NULL, pub_msg=NULL WHERE status='ok'")
            else:
                for u in urls:
                    self.conn.execute(
                        "UPDATE downloads SET proc_status='pending', r2_status=NULL, r2_at=NULL, "
                        "pub_status=NULL, pub_at=NULL, pub_msg=NULL WHERE url=?", (u,))
            self._commit()

    # ------------------------------------------------------------------ 統計
    def stats(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) c FROM downloads WHERE url NOT LIKE 'local://%' GROUP BY status").fetchall()
            return {r["status"]: r["c"] for r in rows}

    def total_bytes(self):
        with self.lock:
            return self.conn.execute("SELECT COALESCE(SUM(size),0) FROM downloads WHERE status='ok'").fetchone()[0]

    def error_counts(self):
        with self.lock:
            return [(r[0] or "unknown", r[1]) for r in self.conn.execute(
                "SELECT err_type, COUNT(*) c FROM downloads WHERE status='fail' GROUP BY err_type ORDER BY c DESC")]

    def domain_stats(self, flt=""):
        with self.lock:
            sql = """SELECT domain, SUM(status='ok') ok, SUM(status='dup') dup, SUM(status='fail') fail,
                        SUM(status='skip') skip, SUM(status='pending') pending, COUNT(*) total,
                        COALESCE(SUM(CASE WHEN status='ok' THEN size END),0) bytes, MAX(updated_at) last
                     FROM downloads WHERE url NOT LIKE 'local://%'"""
            args = []
            if flt:
                sql += " AND domain LIKE ?"
                args.append(f"%{flt}%")
            sql += " GROUP BY domain ORDER BY total DESC"
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def count_domains(self):
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(DISTINCT domain) FROM downloads WHERE url NOT LIKE 'local://%'").fetchone()[0]

    def rows_by_status(self, statuses, flt="", limit=3000):
        with self.lock:
            q = ",".join("?" * len(statuses))
            sql = f"SELECT * FROM downloads WHERE status IN ({q})"
            args = list(statuses)
            if flt:
                sql += " AND (url LIKE ? OR domain LIKE ? OR keyword LIKE ? OR err_msg LIKE ?)"
                args += [f"%{flt}%"] * 4
            sql += " ORDER BY updated_at DESC LIMIT ?"
            args.append(limit)
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def domain_rows(self, domain):
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM downloads WHERE domain=? ORDER BY status, updated_at DESC", (domain,)).fetchall()]

    def all_rows(self):
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM downloads ORDER BY domain, status").fetchall()]

    # ------------------------------------------------------------------ 本地掃描
    def has_path(self, path):
        with self.lock:
            return self.conn.execute(
                "SELECT 1 FROM downloads WHERE file_path=? LIMIT 1", (path,)).fetchone() is not None

    def add_local(self, path, md5, size, ext="pdf", orig_name=None):
        with self.lock:
            if self.conn.execute("SELECT 1 FROM downloads WHERE md5=? AND status='ok' LIMIT 1", (md5,)).fetchone():
                return False
            url = "local://" + path
            self._upsert(url, "ok", md5, path, size, file_ext=ext, orig_name=orig_name or os.path.basename(path))
            self.conn.execute("UPDATE downloads SET proc_status='pending' WHERE url=?", (url,))
            self._commit()
            return True
