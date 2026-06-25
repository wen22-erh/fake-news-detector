from firecrawl import FirecrawlApp, ScrapeOptions
from pymongo import MongoClient, UpdateOne, ReturnDocument
from datetime import datetime
from hashlib import sha256
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


# ---------- 基本設定 ----------
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "firecrawl_demo"
COL_ARTICLES = "articles"
COL_CONTENTS = "article_contents"

BASE_URL = "https://www.bbc.com/zhongwen/articles/cj4wp0rek74o/trad"  # 起始網站

# [CHG] 追蹤參數黑名單維持 set（已是 hashtable 結構，保留）
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "igshid", "mc_cid", "mc_eid"
}

MIN_CONTENT_LEN = 300  # 內容長度門檻（避免把列表/導航頁存進來）
BULK_SIZE = 100        # 批次寫入大小

# ---------- 黑白名單（Hashtable 版，可人工維護） ----------
# [CHG] 允許的協定
ALLOWED_SCHEMES: set[str] = {"http", "https"}

# [CHG] 允許的主機：預設加入 BASE_URL 的 host；你也可以手動加其他 host
_base_host = urlparse(BASE_URL).netloc.lower()
ALLOW_HOSTS: set[str] = {_base_host}
# 例：ALLOW_HOSTS.update({"example.com", "news.example.com"})

# [CHG] 白名單前綴（可選）：若非空，URL 必須以其中任一前綴開頭才通過
ALLOW_PREFIXES: set[str] = set()
# 例：ALLOW_PREFIXES.update({
#     f"https://{_base_host}/zhongwen/", 
#     f"https://{_base_host}/news/"
# })

# [CHG] 黑名單（優先於白名單）
DENY_URLS: set[str] = set()  # 直接封鎖的完整 URL
DENY_PREFIXES: set[str] = set()  # 以「前綴」封鎖（含子路徑）
# 例：DENY_PREFIXES.update({
#     f"https://{_base_host}/ads/",
#     f"https://{_base_host}/promo/",
# })

# [CHG] 路徑若包含以下任一片段則封鎖（以子字串判斷）
DENY_PATH_CONTAINS: set[str] = {"ad/", "ads/", "sponsored/", "promo/"}

# ---------- 輔助工具 ----------
def same_site_only(url: str, base: str) -> bool:
    try:
        return urlparse(url).netloc.lower() == urlparse(base).netloc.lower()
    except Exception:
        return False

def normalize_url(url: str) -> str:
    """移除追蹤 query、統一 host 大小寫、去 fragment。"""
    p = urlparse(url)
    # 濾掉追蹤參數
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
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

# [CHG] URL 黑白名單判斷（Hashtable 版）
def allowed_by_hashlists(url_norm: str) -> tuple[bool, str]:
    """
    回傳 (是否允許, 理由)
    判斷順序：scheme → host → DENY（URL / 前綴 / 路徑片段）→ ALLOW_PREFIXES（若非空）
    """
    p = urlparse(url_norm)
    scheme = (p.scheme or "").lower()
    host = (p.netloc or "").lower()
    path_lower = (p.path or "").lower()

    # 1) 協定白名單
    if scheme not in ALLOWED_SCHEMES:
        return (False, "filtered: scheme not allowed")

    # 2) 主機白名單（同站限制仍會在外層檢查；這裡再保險一次）
    if host not in ALLOW_HOSTS:
        return (False, "filtered: host not allowed")

    # 3) 黑名單（優先於白名單）
    if url_norm in DENY_URLS:
        return (False, "denied: exact URL in denylist")

    for pref in DENY_PREFIXES:
        if url_norm.startswith(pref):
            return (False, "denied: URL matches deny-prefix")

    for frag in DENY_PATH_CONTAINS:
        if frag in path_lower:
            return (False, "denied: path contains blocked fragment")

    # 4) 白名單前綴（若有設定，需命中至少一個）
    if ALLOW_PREFIXES:
        for pref in ALLOW_PREFIXES:
            if url_norm.startswith(pref):
                return (True, "ok")
        return (False, "filtered: not in allow-prefixes")

    return (True, "ok")

# ---------- 連線 ----------
mc = MongoClient(MONGO_URI)
db = mc[DB_NAME]
articles = db[COL_ARTICLES]
contents = db[COL_CONTENTS]

articles.create_index("url", unique=True)
articles.create_index("content_id")       # 之後查同一內容的所有 URL 很方便
contents.create_index("hash", unique=True)

# ---------- Firecrawl ----------
app = FirecrawlApp(api_key=None, api_url="http://localhost:3002/")

crawl = app.crawl_url(
    BASE_URL,
    limit=200,
    scrape_options=ScrapeOptions(
        formats=["markdown", "html"],
        onlyMainContent=True,
        blockAds=True,
        # 視需要可加 timeout/waitFor：
        # timeout=90000, waitFor=3000
    ),
)

payload = crawl.data  # List[FirecrawlDocument]
print(f"Total pages crawled: {len(payload)}")

# ---------- 入庫（過濾與處理） ----------
ops: list[UpdateOne] = []
stats = {
    "total": len(payload),
    "denied": 0,
    "filtered": 0,
    "offsite": 0,
    "no_url": 0,
    "no_content": 0,
    "saved_ops": 0,
    "bulk_commits": 0,
}

base_host = urlparse(BASE_URL).netloc.lower()
seen_hashes: dict[str, object] = {}  # 在同一批內重用已拿到的 content_id，少打 DB

for doc in payload:
    d = doc.model_dump(exclude_none=True)

    # URL 取得與正規化
    raw_url = d.get("url") or (d.get("metadata") or {}).get("sourceURL")
    if not raw_url:
        stats["no_url"] += 1
        continue

    url_norm = normalize_url(raw_url)

    # 同站限制（預設只收 BASE_URL 網域）
    if not same_site_only(url_norm, BASE_URL):
        stats["offsite"] += 1
        continue

    # [CHG] 用 hashtable 黑白名單檢查（取代原本的 regex）
    ok, reason = allowed_by_hashlists(url_norm)
    if not ok:
        if reason.startswith("denied"):
            stats["denied"] += 1
        else:
            stats["filtered"] += 1
        continue

    # 內容選取與檢核
    md = d.get("markdown")
    html = d.get("html") or d.get("rawHtml")
    md, html, content_len = pick_content(md, html)

    if content_len < MIN_CONTENT_LEN:
        stats["no_content"] += 1
        continue

    title = (d.get("metadata") or {}).get("title")

    # 內容去重（hash）
    blob = (md if (md and md.strip()) else html).encode("utf-8", "ignore")
    h = sha256(blob).hexdigest()

    # 先用批內快取避免重複 DB 操作
    content_id = seen_hashes.get(h)
    if not content_id:
        # 單次取得 _id：find_one_and_update + upsert
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

    # 準備 upsert articles（每個 URL 一筆）
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

    # 夠一批就送一批，避免一次性過大
    if len(ops) >= BULK_SIZE:
        articles.bulk_write(ops, ordered=False)
        stats["saved_ops"] += len(ops)
        stats["bulk_commits"] += 1
        ops.clear()

# 送最後一批
if ops:
    articles.bulk_write(ops, ordered=False)
    stats["saved_ops"] += len(ops)
    stats["bulk_commits"] += 1

# ---------- 總結 ----------
print(
    "Done. "
    f"total={stats['total']} "
    f"denied={stats['denied']} "
    f"filtered={stats['filtered']} "
    f"offsite={stats['offsite']} "
    f"no_url={stats['no_url']} "
    f"no_content(<{MIN_CONTENT_LEN})={stats['no_content']} "
    f"saved_ops={stats['saved_ops']} "
    f"bulk_commits={stats['bulk_commits']}"
)
