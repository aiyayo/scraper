# processing/r2_client.py
"""Cloudflare R2（S3 相容）客戶端：上傳 / 下載 / 列舉 / 刪除 / 連線測試。

相對 v4 的強化：
- 單一 client 重用（boto3 client 執行緒安全）；上傳帶退避重試。
- 可「已存在且大小相同則跳過」，整條流水線可重複執行而不重傳；
- 多張 WebP 並行上傳（upload_many）；列舉支援分頁；刪除用批次 API。
- Content-Type 依副檔名判斷（v4 對所有原檔都標成 application/pdf）。
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from core.utils import guess_content_type, retry_call


class R2Client:
    def __init__(self, cfg):
        self.cfg = cfg or {}
        self._s3 = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 連線
    @property
    def bucket(self):
        b = (self.cfg.get("r2_bucket_name") or "").strip()
        if not b:
            raise ValueError("請填寫 R2 Bucket 名稱！")
        return b

    def _client(self):
        with self._lock:
            if self._s3 is None:
                import boto3
                from botocore.config import Config
                endpoint = (self.cfg.get("r2_endpoint_url") or "").strip()
                ak = (self.cfg.get("r2_access_key") or "").strip()
                sk = (self.cfg.get("r2_secret_key") or "").strip()
                if not endpoint or not ak or not sk or "<ACCOUNT_ID>" in endpoint:
                    raise ValueError("Cloudflare R2 參數尚未填寫完整，請在『☁️ 3. Cloudflare R2 選項』分頁設定！")
                workers = int(self.cfg.get("r2_upload_workers", 4) or 4)
                self._s3 = boto3.client(
                    "s3", endpoint_url=endpoint, aws_access_key_id=ak, aws_secret_access_key=sk,
                    region_name="auto",
                    config=Config(signature_version="s3v4", retries={"max_attempts": 3},
                                  max_pool_connections=max(10, workers * 2)))
            return self._s3

    def test_connection(self):
        self._client().head_bucket(Bucket=self.bucket)
        return True

    # ------------------------------------------------------------------ 上傳
    def _same_size_exists(self, key, size):
        from botocore.exceptions import ClientError
        try:
            head = self._client().head_object(Bucket=self.bucket, Key=key)
            return int(head.get("ContentLength", -1)) == size
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def upload_file(self, local_path, key, content_type=None, skip_existing=False):
        """回傳 'uploaded' 或 'skipped'。"""
        key = key.lstrip("/")
        if skip_existing and self._same_size_exists(key, os.path.getsize(local_path)):
            return "skipped"
        extra = {"ContentType": content_type or guess_content_type(local_path)}
        retry_call(lambda: self._client().upload_file(local_path, self.bucket, key, ExtraArgs=extra),
                   attempts=3, base_delay=1.0)
        return "uploaded"

    def upload_many(self, items, workers=4, skip_existing=True):
        """items: [(local_path, key)]；並行上傳，回傳 {'uploaded': n, 'skipped': n}。任一失敗會在全部結束後拋出第一個錯誤。"""
        stats = {"uploaded": 0, "skipped": 0}
        if not items:
            return stats
        first_err = None
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = [ex.submit(self.upload_file, lp, k, None, skip_existing) for lp, k in items]
            for f in futs:
                try:
                    stats[f.result()] += 1
                except Exception as e:  # noqa: BLE001
                    first_err = first_err or e
        if first_err:
            raise first_err
        return stats

    # ------------------------------------------------------------------ 下載 / 列舉 / 刪除
    def download_file(self, key, local_path):
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        self._client().download_file(self.bucket, key.lstrip("/"), local_path)
        return local_path

    def list_objects(self, prefix="", max_keys=300):
        out, token = [], None
        while len(out) < max_keys:
            kw = {"Bucket": self.bucket, "Prefix": prefix.lstrip("/"), "MaxKeys": min(1000, max_keys - len(out))}
            if token:
                kw["ContinuationToken"] = token
            resp = self._client().list_objects_v2(**kw)
            out.extend(resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return out

    def delete_objects(self, keys):
        """批次刪除（每批最多 1000 個），回傳刪除數量。"""
        keys = [k.lstrip("/") for k in keys]
        for i in range(0, len(keys), 1000):
            batch = [{"Key": k} for k in keys[i:i + 1000]]
            self._client().delete_objects(Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True})
        return len(keys)
