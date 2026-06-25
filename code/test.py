# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import Optional, Any
from hashlib import sha256
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from pymongo import MongoClient, UpdateOne, ReturnDocument
from firecrawl import FirecrawlApp

# ========== 基本設定 ==========
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "fake_news_demo"          # 你可改回原本的 DB 名稱
COL_ARTICLES = "articles"
COL_CONTENTS = "article_contents"

BASE_URL = "https://www.bbc.com/zhongwen"  # 起始網址（可換）

# 追蹤參數黑名單（hashtable）
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "igshid", "mc_cid", "mc_eid"
}

# 內容過濾門檻
MIN_CONTENT_LEN = 300
BULK_SIZE = 100

# ========== 黑白名單（Hashtable 版，可人工調整） ==========
ALLOWED_SCHEMES: set[str] = {"http", "https"}
_base_host = urlparse(BASE_URL).netloc.lower()
ALLOW_HOSTS: set[str] = {_base_host}
ALLOW_PREFIXES: set[str] = set()  # 非空時：必須命中前綴才放行
DENY_URLS: set[str] = set()
DENY_PREFIXES: set[str] = set()
DENY_PATH_CONTAINS: set[str] = {"ad/", "ads/", "sponsored/", "promo/"}

# ========== DOM 正文抽取與雜訊過濾 ==========
from lxml import html as lxml_html
from lxml.html.clean import Cleaner
from bs4 import BeautifulSoup

try:
    import trafilatura
    _HAS_TRAFILATURA = True
except Exception:
    _HAS_TRAFILATURA = False

try:
    from readability import Document as ReadabilityDocument
    _HAS_READABILITY = True
except Exception:
    _HAS_READABILITY = False

_BLACKLIST_PATTERNS = re.compile(
    r"(cookie|consent|gdpr|banner|popup|modal|subscribe|newsletter|"
    r"share|social|follow|breadcrumb|nav|header|footer|sidebar|"
    r"ad-|ads|advert|advertise|promo|promoted|sponsor|sponsored|"
    r"outbrain|taboola|widget|recommend|related|trending|"
    r"comment|reply|login|register|paywall)",
    re.I
)
_CONTENT_NOISE_PATTERNS = re.compile(
    r"(我們使用cookie|使用 cookie|隱私|版權所有|訂閱電子報|追蹤我們|"
    r"條款|隱私權政策|cookie policy|privacy|terms of service|"
    r"enable cookies|accept cookies|consent|manage preferences|"
    r"贊助|廣告|sponsored|advertisement|"
    r"留言|評論區|熱門文章|延伸閱讀|你可能還喜歡)",
    re.I
)
_SENT_END = re.compile(r"[。．.!?？！]+")

def _drop_blacklisted_nodes(doc: lxml_html.HtmlElement) -> None:
    for node in list(doc.iter()):
        attrs = []
        for k, v in node.attrib.items():
            if k in ("class", "id", "role", "aria-label") or k.startswith("data-"):
                attrs.append(str(v))
        if attrs and _BLACKLIST_PATTERNS.search(" ".join(attrs)):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)

def _link_char_ratio(el: lxml_html.HtmlElement) -> float:
    total = (el.text_content() or "").strip()
    if not total:
        return 1.0
    link_text = " ".join(a.text_content().strip() for a in el.xpath(".//a"))
    return min(1.0, (len(link_text) + 1) / (len(total) + 1))

def _keep_paragraph(p: str) -> bool:
    s = p.strip()
    if len(s) < 25:
        return False
    if _CONTENT_NOISE_PATTERNS.search(s):
        return False
    marks = len(_SENT_END.findall(s))
    if len(s) < 80 and marks == 0:
        return False
    return True

def _dom_to_clean_text(root: lxml_html.HtmlElement) -> str:
    paras: list[str] = []
    for el in root.xpath("//article//p|//main//p|//p"):
        if _link_char_ratio(el) > 0.6:
            continue
        t = el.text_content().strip()
        if _keep_paragraph(t):
            paras.append(t)
    if not paras:
        for line in (root.text_content() or "").splitlines():
            s = line.strip()
            if _keep_paragraph(s):
                paras.append(s)
    return "\n".join(paras).strip()

def extract_main_text(html: Optional[str], url: Optional[str] = None) -> str:
    if not html or not html.strip():
        return ""
    # 1) trafilatura
    if _HAS_TRAFILATURA:
        try:
            txt = trafilatura.extract(
                html, url=url, include_comments=False, include_tables=False,
                favor_recall=True, with_metadata=False, no_fallback=False
            )
            if txt and len(txt.strip()) >= 200:
                lines = [ln.strip() for ln in txt.splitlines() if _keep_paragraph(ln)]
                return "\n".join(lines).strip()
        except Exception:
            pass
    # 2) readability
    main_html = None
    if _HAS_READABILITY:
        try:
            doc = ReadabilityDocument(html)
            main_html = doc.summary(html_partial=True)
        except Exception:
            main_html = None
    source = main_html if main_html else html
    # 3) 準備 DOM
    try:
        root = lxml_html.fromstring(source)
    except Exception:
        soup = BeautifulSoup(source, "html.parser")
        for t in soup(["script", "style", "noscript"]): t.decompose()
        raw = soup.get_text(" ", strip=True)
        lines = [ln.strip() for ln in raw.splitlines() if _keep_paragraph(ln)]
        return "\n".join(lines).strip()
    cleaner = Cleaner(
        scripts=True, javascript=True, style=True, embedded=True, frames=True,
        forms=True, annoying_tags=True, comments=True, links=False, meta=True
    )
    root = cleaner.clean_html(root)
    _drop_blacklisted_nodes(root)
    text = _dom_to_clean_text(root)
    lines = [ln.strip() for ln in text.splitlines() if _keep_paragraph(ln)]
    return "\n".join(lines).strip()

# ========== 一般工具 ==========
def same_site_only(url: str, base: str) -> bool:
    try:
        return urlparse(url).netloc.lower() == urlparse(base).netloc.lower()
    except Exception:
        return False

def normalize_url(url: str) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if k.lower() not in TRACKING_PARAMS]
    norm = p._replace(netloc=p.netloc.lower(), query=urlencode(q, doseq=True), fragment="")
    return urlunparse(norm)

def pick_content(md: Optional[str], html: Optional[str]) -> tuple[Optional[str], Optional[str], int]:
    text = (md or "").strip() if (md and md.strip()) else (html or "")
    return (md, html, len(text))

def allowed_by_hashlists(url_norm: str) -> tuple[bool, str]:
    p = urlparse(url_norm)
    scheme = (p.scheme or "").lower()
    host = (p.netloc or "").lower()
    path_lower = (p.path or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return (False, "filtered: scheme")
    if host not in ALLOW_HOSTS:
        return (False, "offsite")
    if url_norm in DENY_URLS:
        return (False, "denied: exact")
    for pref in DENY_PREFIXES:
        if url_norm.startswith(pref):
            return (False, "denied: prefix")
    for frag in DENY_PATH_CONTAINS:
        if frag in path_lower:
            return (False, "denied: path-fragment")
    if ALLOW_PREFIXES:
        for pref in ALLOW_PREFIXES:
            if url_norm.startswith(pref):
                return (True, "ok")
        return (False, "filtered: not-in-allow-prefixes")
    return (True, "ok")

# ========== DB 連線與索引 ==========
mc = MongoClient(MONGO_URI)
db = mc[DB_NAME]
articles = db[COL_ARTICLES]
contents = db[COL_CONTENTS]
articles.create_index("url", unique=True)
articles.create_index("content_id")
contents.create_index("hash", unique=True)

# ========== Firecrawl ==========
app = FirecrawlApp(api_key="dummyKey", api_url="http://localhost:3002/")
crawl = app.scrape(
    BASE_URL,
    limit=200,
    
    formats=["markdown", "html"],
    onlyMainContent=True,
    blockAds=True,
    
)
payload = crawl.data
print(f"Crawled pages: {len(payload)}")

# ========== 主流程 ==========
ops: list[UpdateOne] = []
stats = {"total": len(payload), "denied": 0, "filtered": 0, "offsite": 0,
         "no_url": 0, "no_content": 0, "saved_ops": 0, "bulk_commits": 0, "errors": 0}
base_host = urlparse(BASE_URL).netloc.lower()
seen_hashes: dict[str, Any] = {}

for doc in payload:
    try:
        d = doc.model_dump(exclude_none=True)
        raw_url = d.get("url") or (d.get("metadata") or {}).get("sourceURL")
        if not raw_url:
            stats["no_url"] += 1
            continue
        url_norm = normalize_url(raw_url)

        if not same_site_only(url_norm, BASE_URL):
            stats["offsite"] += 1
            continue

        ok, reason = allowed_by_hashlists(url_norm)
        if not ok:
            if reason == "offsite": stats["offsite"] += 1
            elif reason.startswith("denied"): stats["denied"] += 1
            else: stats["filtered"] += 1
            continue

        md = d.get("markdown")
        html = d.get("html") or d.get("rawHtml")
        md, html, _ = pick_content(md, html)

        # ★ 正文抽取（重點）：先 trafilatura / readability，再 DOM 黑名單 + 啟發式
        text_clean = extract_main_text(html or "", url_norm) if (html and len((md or "")) < 200) else (
            "\n".join([ln.strip() for ln in (md or "").splitlines() if ln.strip()]) if md else extract_main_text(html or "", url_norm)
        )
        # 避免 md 本身是目錄/清單，仍做一次噪音過濾
        if text_clean and _CONTENT_NOISE_PATTERNS.search(text_clean[:400]):
            text_clean = extract_main_text(html or "", url_norm)

        if not text_clean or len(text_clean) < MIN_CONTENT_LEN:
            stats["no_content"] += 1
            continue

        title = (d.get("metadata") or {}).get("title")
        blob = (md if (md and md.strip()) else (html or "")).encode("utf-8", "ignore")
        h = sha256(blob).hexdigest()
        text_hash = sha256(text_clean.encode("utf-8", "ignore")).hexdigest()

        # 內容表：避免衝突 → **只用 $set** + upsert=True（不使用 $setOnInsert）
        content_doc = {
            "hash": h,
            "text_hash": text_hash,
            "markdown": md,
            "html": html,
            "text": text_clean,
            "text_len": len(text_clean),
            "source_url": url_norm,
            "updated_at": datetime.now(timezone.utc),
        }
        content = contents.find_one_and_update(
            {"hash": h},
            {"$set": content_doc},
            upsert=True,
            return_document=ReturnDocument.AFTER,
            projection={"_id": 1}
        )
        content_id = content["_id"]
        seen_hashes[h] = content_id

        # URL→內容 關聯表
        ops.append(UpdateOne(
            {"url": url_norm},
            {"$set": {
                "url": url_norm,
                "title": title,
                "content_id": content_id,
                "fetched_at": datetime.now(timezone.utc),
                "site": f"https://{base_host}",
            }},
            upsert=True
        ))
        if len(ops) >= BULK_SIZE:
            articles.bulk_write(ops, ordered=False)
            stats["saved_ops"] += len(ops); stats["bulk_commits"] += 1
            ops.clear()

    except Exception as e:
        stats["errors"] += 1
        # 你也可以把 e 印出或寫入 log
        continue

if ops:
    articles.bulk_write(ops, ordered=False)
    stats["saved_ops"] += len(ops); stats["bulk_commits"] += 1

print(
    "Done. total={total} denied={denied} filtered={filtered} offsite={offsite} "
    "no_url={no_url} no_content={no_content} saved_ops={saved_ops} "
    "bulk_commits={bulk_commits} errors={errors}".format(**stats)
)
