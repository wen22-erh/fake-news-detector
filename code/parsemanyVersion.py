import json
from datetime import datetime
from hashlib import sha256
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from firecrawl import FirecrawlApp, ScrapeOptions
from pymongo import MongoClient, UpdateOne, ReturnDocument
import argparse
import sys

# ========================
# 共用設定（可用 CLI 覆寫）
# ========================
DEFAULT_MONGO_URI = "mongodb://localhost:27017/"
DEFAULT_DB_NAME = "firecrawl_demo"
COL_ARTICLES = "articles"
COL_CONTENTS = "article_contents"

# 追蹤參數黑名單
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "igshid", "mc_cid", "mc_eid",
}

# 預設格式/門檻/批次
DEFAULT_FORMATS = ["markdown", "html"]
DEFAULT_ONLY_MAIN = True
DEFAULT_BLOCK_ADS = True
DEFAULT_LIMIT = 200
DEFAULT_MIN_CONTENT_LEN = 300
DEFAULT_BULK_SIZE = 100
DEFAULT_ALLOWED_SCHEMES = {"http", "https"}

# ========================
# 工具函式
# ========================
def normalize_url(url: str) -> str:
    """移除追蹤 query、統一 host 大小寫、去 fragment。"""
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if k.lower() not in TRACKING_PARAMS]
    norm = p._replace(
        netloc=p.netloc.lower(),
        query=urlencode(q, doseq=True),
        fragment=""
    )
    return urlunparse(norm)

def pick_content(md: str | None, html: str | None) -> tuple[str | None, str | None, int]:
    """優先用 markdown，沒 markdown 才用 html；回傳內容與長度。"""
    text = (md or "") if (md and md.strip()) else (html or "")
    length = len(text)
    return (md, html, length)

def allowed_by_hashlists(
    url_norm: str,
    *,
    allowed_schemes: set[str],
    allow_hosts: set[str],
    allow_prefixes: set[str],
    deny_urls: set[str],
    deny_prefixes: set[str],
    deny_path_contains: set[str],
) -> tuple[bool, str]:
    """
    回傳 (是否允許, 理由)
    判斷順序：scheme → host → DENY（URL/前綴/路徑片段）→ ALLOW_PREFIXES（若非空）
    """
    p = urlparse(url_norm)
    scheme = (p.scheme or "").lower()
    host = (p.netloc or "").lower()
    path_lower = (p.path or "").lower()

    if scheme not in allowed_schemes:
        return (False, "filtered: scheme not allowed")

    if host not in allow_hosts:
        return (False, "offsite")

    if url_norm in deny_urls:
        return (False, "denied: exact URL")
    for pref in deny_prefixes:
        if url_norm.startswith(pref):
            return (False, "denied: prefix")
    for frag in deny_path_contains:
        if frag in path_lower:
            return (False, "denied: path fragment")

    if allow_prefixes:
        for pref in allow_prefixes:
            if url_norm.startswith(pref):
                return (True, "ok")
        return (False, "filtered: not in allow-prefixes")

    return (True, "ok")

# ========================
# 單站主流程
# ========================
def crawl_once(cfg: dict[str, Any], mc: MongoClient) -> dict[str, int]:
    """
    cfg 支援欄位（皆選填，最少要有 base_url）：
      base_url: str
      allow_hosts: [str]  # 預設會自動加入 base_url 的 host
      allow_prefixes: [str]
      deny_urls: [str]
      deny_prefixes: [str]
      deny_path_contains: [str]
      allowed_schemes: [str]          # 預設 {"http","https"}
      formats: ["markdown","html"]    # 預設兩者
      onlyMainContent: bool           # 預設 True
      blockAds: bool                  # 預設 True
      limit: int                      # 預設 200
      min_content_len: int            # 預設 300
      bulk_size: int                  # 預設 100
      api_url: str                    # 預設 http://localhost:3002/
      api_key: str | null             # 預設 None
    """
    base_url = cfg["base_url"]
    base_host = urlparse(base_url).netloc.lower()

    # 參數整備（list→set）
    allow_hosts = set(map(str.lower, cfg.get("allow_hosts", [])))
    if not allow_hosts:
        allow_hosts = {base_host}
    else:
        allow_hosts.add(base_host)

    allow_prefixes = set(cfg.get("allow_prefixes", []))
    deny_urls = set(cfg.get("deny_urls", []))
    deny_prefixes = set(cfg.get("deny_prefixes", []))
    deny_path_contains = set(map(str.lower, cfg.get("deny_path_contains", {"ad/", "ads/", "sponsored/", "promo/"})))
    allowed_schemes = set(map(str.lower, cfg.get("allowed_schemes", list(DEFAULT_ALLOWED_SCHEMES))))

    # 其他選項
    formats = cfg.get("formats", DEFAULT_FORMATS)
    only_main = cfg.get("onlyMainContent", DEFAULT_ONLY_MAIN)
    block_ads = cfg.get("blockAds", DEFAULT_BLOCK_ADS)
    limit = int(cfg.get("limit", DEFAULT_LIMIT))
    min_content_len = int(cfg.get("min_content_len", DEFAULT_MIN_CONTENT_LEN))
    bulk_size = int(cfg.get("bulk_size", DEFAULT_BULK_SIZE))
    api_url = cfg.get("api_url", "http://localhost:3002/")
    api_key = cfg.get("api_key", None)

    # DB
    db = mc[cfg.get("db_name", DEFAULT_DB_NAME)]
    articles = db[COL_ARTICLES]
    contents = db[COL_CONTENTS]

    # 索引（存在則跳過）
    articles.create_index("url", unique=True)
    articles.create_index("content_id")
    contents.create_index("hash", unique=True)

    # Firecrawl
    app = FirecrawlApp(api_key=api_key, api_url=api_url)
    crawl = app.crawl_url(
        base_url,
        limit=limit,
        scrape_options=ScrapeOptions(
            formats=formats,
            onlyMainContent=only_main,
            blockAds=block_ads,
            # 可視需要增加：timeout=90000, waitFor=3000
        ),
    )
    payload = crawl.data  # List[FirecrawlDocument]
    print(f"[{base_host}] crawled pages: {len(payload)}")

    # 統計
    stats = {
        "total": len(payload),
        "denied": 0,
        "filtered": 0,
        "offsite": 0,
        "no_url": 0,
        "no_content": 0,
        "saved_ops": 0,
        "bulk_commits": 0,
        "errors": 0,
    }

    ops: list[UpdateOne] = []
    seen_hashes: dict[str, Any] = {}

    for doc in payload:
        try:
            d = doc.model_dump(exclude_none=True)

            raw_url = d.get("url") or (d.get("metadata") or {}).get("sourceURL")
            if not raw_url:
                stats["no_url"] += 1
                continue

            url_norm = normalize_url(raw_url)

            ok, reason = allowed_by_hashlists(
                url_norm,
                allowed_schemes=allowed_schemes,
                allow_hosts=allow_hosts,
                allow_prefixes=allow_prefixes,
                deny_urls=deny_urls,
                deny_prefixes=deny_prefixes,
                deny_path_contains=deny_path_contains,
            )
            if not ok:
                if reason == "offsite":
                    stats["offsite"] += 1
                elif reason.startswith("denied"):
                    stats["denied"] += 1
                else:
                    stats["filtered"] += 1
                continue

            md = d.get("markdown")
            html = d.get("html") or d.get("rawHtml")
            md, html, content_len = pick_content(md, html)
            if content_len < min_content_len:
                stats["no_content"] += 1
                continue

            title = (d.get("metadata") or {}).get("title")

            blob = (md if (md and md.strip()) else html).encode("utf-8", "ignore")
            h = sha256(blob).hexdigest()

            content_id = seen_hashes.get(h)
            if not content_id:
                doc_content = {
                    "hash": h,
                    "markdown": md,
                    "html": html,
                    "updated_at": datetime.utcnow(),
                    "source_url": url_norm,
                }
                content = contents.find_one_and_update(
                    {"hash": h},
                    {"$setOnInsert": doc_content},
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                    projection={"_id": 1}
                )
                content_id = content["_id"]
                seen_hashes[h] = content_id

            ops.append(UpdateOne(
                {"url": url_norm},
                {"$set": {
                    "url": url_norm,
                    "title": title,
                    "content_id": content_id,
                    "fetched_at": datetime.utcnow(),
                    "site": f"https://{base_host}",
                }},
                upsert=True
            ))

            if len(ops) >= bulk_size:
                articles.bulk_write(ops, ordered=False)
                stats["saved_ops"] += len(ops)
                stats["bulk_commits"] += 1
                ops.clear()

        except Exception:
            # 單頁失敗不影響整批
            stats["errors"] += 1
            continue

    if ops:
        articles.bulk_write(ops, ordered=False)
        stats["saved_ops"] += len(ops)
        stats["bulk_commits"] += 1

    print(f"[{base_host}] done. "
          f"total={stats['total']} denied={stats['denied']} filtered={stats['filtered']} "
          f"offsite={stats['offsite']} no_url={stats['no_url']} "
          f"no_content(<{min_content_len})={stats['no_content']} saved_ops={stats['saved_ops']} "
          f"bulk_commits={stats['bulk_commits']} errors={stats['errors']}")
    return stats

# ========================
# 批量入口
# ========================
def main():
    ap = argparse.ArgumentParser(description="Batch crawl sites from JSON seeds using Firecrawl + MongoDB.")
    ap.add_argument("--seeds", required=True, help="JSON 檔路徑（站點清單）")
    ap.add_argument("--mongo-uri", default=DEFAULT_MONGO_URI, help=f"Mongo URI（預設 {DEFAULT_MONGO_URI}）")
    ap.add_argument("--db-name", default=DEFAULT_DB_NAME, help=f"Mongo DB 名稱（預設 {DEFAULT_DB_NAME}）")
    ap.add_argument("--max-workers", type=int, default=1, help="併發執行緒數（1 表示逐站序列）")
    args = ap.parse_args()

    # 讀 seeds
    try:
        with open(args.seeds, "r", encoding="utf-8") as f:
            seeds = json.load(f)
        assert isinstance(seeds, list) and seeds, "seeds 檔應為非空陣列"
    except Exception as e:
        print(f"讀取 seeds 檔失敗：{e}", file=sys.stderr)
        sys.exit(2)

    # 注入全域 db_name（可被每個 cfg 覆寫）
    for s in seeds:
        s.setdefault("db_name", args.db_name)

    mc = MongoClient(args.mongo_uri)

    # 併發或序列
    total = {"total": 0, "denied": 0, "filtered": 0, "offsite": 0,
             "no_url": 0, "no_content": 0, "saved_ops": 0,
             "bulk_commits": 0, "errors": 0}

    if args.max_workers == 1:
        for cfg in seeds:
            st = crawl_once(cfg, mc)
            for k in total:
                total[k] += st.get(k, 0)
    else:
        with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
            futures = [ex.submit(crawl_once, cfg, mc) for cfg in seeds]
            for fut in as_completed(futures):
                st = fut.result()
                for k in total:
                    total[k] += st.get(k, 0)

    print("\n=== BATCH SUMMARY ===")
    print("total={total} denied={denied} filtered={filtered} offsite={offsite} "
          "no_url={no_url} no_content={no_content} saved_ops={saved_ops} "
          "bulk_commits={bulk_commits} errors={errors}".format(**total))

if __name__ == "__main__":
    main()
