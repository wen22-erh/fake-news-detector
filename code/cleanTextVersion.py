from firecrawl import FirecrawlApp, ScrapeOptions
from pymongo import MongoClient, UpdateOne, ReturnDocument
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import re
try:
    from bs4 import BeautifulSoup  # pip install beautifulsoup4
except Exception:
    BeautifulSoup = None

# ---------- 基本設定 ----------
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "firecrawl_demo"
COL_ARTICLES = "articles"
COL_CONTENTS = "article_contents"

BASE_URL = "https://www.bbc.com/zhongwen/articles/c3rvjd3r9wpo/trad"  # 起始網站

# 追蹤參數黑名單維持 set（已是 hashtable 結構，保留）
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "igshid", "mc_cid", "mc_eid"
}

MIN_CONTENT_LEN = 300  # 內容長度門檻（避免把列表/導航頁存進來）
BULK_SIZE = 100        # 批次寫入大小

# ---------- 黑白名單（Hashtable 版，可人工維護） ----------
ALLOWED_SCHEMES: set[str] = {"http", "https"}
_base_host = urlparse(BASE_URL).netloc.lower()
ALLOW_HOSTS: set[str] = {_base_host}

ALLOW_PREFIXES: set[str] = set()
DENY_URLS: set[str] = set()  # 直接封鎖的完整 URL
DENY_PREFIXES: set[str] = set()  # 以「前綴」封鎖（含子路徑）
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

# ---------- 文字清理：Markdown/HTML → 純文字 --------
MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
MD_IMAGE = re.compile(r"!\[([^\]]*)\]\((?:[^)]+)\)")
MD_CODEBLOCK = re.compile(r"```.*?```", re.S)
MD_INLINE_CODE = re.compile(r"`([^`]+)`")
MD_HEADER = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
MD_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
MD_LIST_BULLET = re.compile(r"^\s*[-*+]\s+", re.M)
MD_LIST_NUM = re.compile(r"^\s*\d+\.\s+", re.M)
HTML_TAG = re.compile(r"<[^>]+>")

def markdown_to_text(md: str) -> str:
    if not md:
        return ""
    s = MD_CODEBLOCK.sub("", md)                              # 移除三引號程式區塊
    s = MD_IMAGE.sub(lambda m: m.group(1) or "", s)           # 圖片：保留 alt
    s = MD_LINK.sub(lambda m: m.group(1), s)                  # 連結：保留可見文字
    s = MD_INLINE_CODE.sub(lambda m: m.group(1), s)           # 行內程式碼去反引號
    s = MD_HEADER.sub("", s)                                  # 標題去 #
    s = MD_BLOCKQUOTE.sub("", s)                              # 區塊引用去 >
    s = MD_LIST_BULLET.sub("", s)                             # 無序清單符號
    s = MD_LIST_NUM.sub("", s)                                # 有序清單序號
    s = re.sub(r"\s+", " ", s).strip()
    return s

def html_to_text(html: str) -> str:
    if not html:
        return ""
    if BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        text = soup.get_text(separator=" ", strip=True)
        return re.sub(r"\s+", " ", text).strip()
    text = HTML_TAG.sub(" ", html)
    return re.sub(r"\s+", " ", text).strip()

def extract_plain_text(md: str | None, html: str | None) -> str:
    if md and md.strip():
        return markdown_to_text(md)
    if html and html.strip():
        return html_to_text(html)
    return ""

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
seen_hashes: dict[str, object] = {}

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

    # 用 hashtable 黑白名單檢查（取代原本的 regex）
    ok, reason = allowed_by_hashlists(url_norm)
    if not ok:
        if reason.startswith("denied"):
            stats["denied"] += 1
        else:
            stats["filtered"] += 1
        continue

    # 內容選取
    md = d.get("markdown")
    html = d.get("html") or d.get("rawHtml")
    md, html, _ = pick_content(md, html)

    # 轉成純文字（供模型用）
    text_clean = extract_plain_text(md, html)

    # 以「純文字長度」做薄內容過濾更準確
    if len(text_clean) < MIN_CONTENT_LEN:
        stats["no_content"] += 1
        continue

    title = (d.get("metadata") or {}).get("title")

    # 內容去重（hash）：仍沿用原本策略（對 Markdown/HTML 的雜湊）
    blob = (md if (md and md.strip()) else (html or "")).encode("utf-8", "ignore")
    h = sha256(blob).hexdigest()

    # 也可選擇補存「純文字 hash」
    text_hash = sha256(text_clean.encode("utf-8", "ignore")).hexdigest()

    # 先用批內快取避免重複 DB 操作
    content_id = seen_hashes.get(h)
    if not content_id:
        # 單次取得 _id：find_one_and_update + upsert
        doc_content = {
            "hash": h,
            "text_hash": text_hash,
            "markdown": md,
            "html": html,
            "text": text_clean,
            "text_len": len(text_clean),
            "updated_at": datetime.now(),  # 使用 UTC
            "source_url": url_norm,
        }
        content = contents.find_one_and_update(
            {"hash": h},
            {"$set": {"updated_at": datetime.now()}},  # 更新 updated_at
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
            "fetched_at": datetime.now(),
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
