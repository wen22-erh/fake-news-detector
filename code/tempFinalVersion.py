# -*- coding: utf-8 -*-
from __future__ import annotations

import os, re, json
from typing import Optional, Tuple, List, Dict
from hashlib import sha256
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, urljoin
from collections import deque

import requests
from requests import Session
from pymongo import MongoClient, UpdateOne, ReturnDocument
from lxml import html as lxml_html
from lxml.html.clean import Cleaner
from bs4 import BeautifulSoup

# =========================
# 基本設定
# =========================
MONGO_URI = "mongodb://localhost:27017/"
DB_NAME = "politifact_fake"
COL_ARTICLES = "articles"
COL_CONTENTS = "contents"

BASE_URL = "https://www.politifact.com/factchecks/list/?ruling=false"
FIRECRAWL_BASE = "http://localhost:3002"             # 不要加尾斜線
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")    # 若容器需要金鑰，設這個環境變數

MAX_DEPTH = 5
MAX_PAGES = 100
MIN_CONTENT_LEN = 100
BULK_SIZE = 100

TRACKING_PARAMS = {
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "gclid","fbclid","igshid","mc_cid","mc_eid"
}

# =========================
# 黑白名單
# =========================
ALLOWED_SCHEMES: set[str] = {"http", "https"}
_base_host = urlparse(BASE_URL).netloc.lower()
ALLOW_HOSTS: set[str] = {_base_host}
ALLOW_PREFIXES: set[str] = set()
DENY_URLS: set[str] = set()
DENY_PREFIXES: set[str] = set()
DENY_PATH_CONTAINS: set[str] = {"ad/", "ads/", "sponsored/", "promo/"}

# =========================
# 內容抽取與清洗 - 基礎
# =========================
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
    r"comment|reply|login|register|paywall)", re.I
)
_CONTENT_NOISE_PATTERNS = re.compile(
    r"(我們使用cookie|使用 cookie|隱私|版權所有|訂閱電子報|追蹤我們|"
    r"條款|隱私權政策|cookie policy|privacy|terms of service|"
    r"enable cookies|accept cookies|consent|manage preferences|"
    r"贊助|廣告|sponsored|advertisement|留言|評論區|熱門文章|延伸閱讀|你可能還喜歡)", re.I
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
    if not total: return 1.0
    link_text = " ".join(a.text_content().strip() for a in el.xpath(".//a"))
    return min(1.0, (len(link_text) + 1) / (len(total) + 1))

def _keep_paragraph(p: str) -> bool:
    s = p.strip()
    if len(s) < 25: return False
    if _CONTENT_NOISE_PATTERNS.search(s): return False
    marks = len(_SENT_END.findall(s))
    if len(s) < 80 and marks == 0: return False
    return True

def _dom_to_clean_text(root: lxml_html.HtmlElement) -> str:
    paras: list[str] = []
    for el in root.xpath("//article//p|//main//p|//p"):
        if _link_char_ratio(el) > 0.6: continue
        t = el.text_content().strip()
        if _keep_paragraph(t): paras.append(t)
    if not paras:
        for line in (root.text_content() or "").splitlines():
            s = line.strip()
            if _keep_paragraph(s): paras.append(s)
    return "\n".join(paras).strip()

def extract_main_text(html: Optional[str], url: Optional[str] = None) -> str:
    if not html or not html.strip(): return ""
    if _HAS_TRAFILATURA:
        try:
            txt = trafilatura.extract(html, url=url, favor_recall=True, with_metadata=False, no_fallback=False)
            if txt and len(txt.strip()) >= 200:
                lines = [ln.strip() for ln in txt.splitlines() if _keep_paragraph(ln)]
                return "\n".join(lines).strip()
        except Exception:
            pass
    main_html = None
    if _HAS_READABILITY:
        try:
            doc = ReadabilityDocument(html)
            main_html = doc.summary(html_partial=True)
        except Exception:
            main_html = None
    source = main_html if main_html else html
    try:
        root = lxml_html.fromstring(source)
    except Exception:
        soup = BeautifulSoup(source, "html.parser")
        for t in soup(["script","style","noscript"]): t.decompose()
        raw = soup.get_text(" ", strip=True)
        lines = [ln.strip() for ln in raw.splitlines() if _keep_paragraph(ln)]
        return "\n".join(lines).strip()
    cleaner = Cleaner(scripts=True, javascript=True, style=True, embedded=True, frames=True,
                      forms=True, annoying_tags=True, comments=True, links=False, meta=True)
    root = cleaner.clean_html(root)
    _drop_blacklisted_nodes(root)
    text = _dom_to_clean_text(root)
    lines = [ln.strip() for ln in text.splitlines() if _keep_paragraph(ln)]
    return "\n".join(lines).strip()

# =========================
# 嚴格正文過濾（只留文章內文）
# =========================
_URL_RE = re.compile(r'https?://[^\s)\]]+|www\.[^\s)\]]+', re.I)
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^\)]+)\)')
_MD_IMG_RE = re.compile(r'!\[[^\]]*\]\([^\)]*\)')
_MD_CODEBLOCK_RE = re.compile(r'```.*?```', re.S)
_MD_INLINE_CODE_RE = re.compile(r'`[^`]+`')
_MD_HEADING_RE = re.compile(r'^\s{0,3}#{1,6}\s+.*$')
_MD_QUOTE_RE = re.compile(r'^\s{0,3}>\s+.*$')
_MD_LIST_RE = re.compile(r'^\s{0,3}([*\-\+•·▪◦]|[0-9]+[.)])\s+')
_MD_RULE_RE = re.compile(r'^\s{0,3}([-*_])\1{2,}\s*$')
_MD_BOLD_ITALIC_RE = re.compile(r'(\*\*|\*|__|_)')
_MD_HTML_TAG_RE = re.compile(r'<[^>]+>')

_NOISE_LINE_RE = re.compile(
    r'(cookie|cookies|隱私|privacy|GDPR|條款|terms|'
    r'版權|copyright|使用條款|服務條款|'
    r'訂閱|newsletter|追蹤我們|追蹤|關注|'
    r'分享|share|social|'
    r'延伸閱讀|相關閱讀|你可能還喜歡|更多|看更多|'
    r'熱門|趨勢|trending|'
    r'留言|評論|comment|reply|'
    r'廣告|贊助|sponsored|advertisement|promo|outbrain|taboola|推薦|'
    r'返回|回到|上一頁|下一頁|閱讀全文|read more)',
    re.I
)

def _too_many_urls(line: str, max_ratio: float = 0.15) -> bool:
    urls = list(_URL_RE.finditer(line))
    if not urls: return False
    url_chars = sum((m.end() - m.start()) for m in urls)
    return url_chars / max(1, len(line)) >= max_ratio

def strip_markdown_to_text(md: str) -> str:
    if not md: return ""
    s = md
    s = _MD_CODEBLOCK_RE.sub(" ", s)
    s = _MD_IMG_RE.sub(" ", s)
    s = _MD_LINK_RE.sub(r'\1', s)     # [text](url) -> text
    s = _MD_INLINE_CODE_RE.sub(" ", s)
    lines = []
    for raw in s.splitlines():
        line = raw.strip()
        if not line: continue
        if _MD_HEADING_RE.match(line): continue
        if _MD_QUOTE_RE.match(line): continue
        if _MD_LIST_RE.match(line): continue
        if _MD_RULE_RE.match(line): continue
        lines.append(line)
    s = "\n".join(lines)
    s = _MD_BOLD_ITALIC_RE.sub("", s)
    s = _MD_HTML_TAG_RE.sub(" ", s)
    s = _URL_RE.sub(" ", s)
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'\s*([。．.!?？！；;,:：，、])\s*', r'\1', s)
    s = re.sub(r'(?:[。．!?？！]){2,}', lambda m: m.group(0)[0], s)
    return s.strip()

def strict_body_filter(text: str) -> str:
    if not text: return ""
    s = re.sub(r'[ \t]*\n+[ \t]*', '\n', text)
    raw_lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    clean_lines: List[str] = []
    for ln in raw_lines:
        if _NOISE_LINE_RE.search(ln): continue
        if _too_many_urls(ln): continue
        if len(ln) < 15: continue
        ln = re.sub(r'^[\-\*\+•·▪◦•\d]+\s*', '', ln)
        ln = re.sub(r'(?:閱讀全文|Read more|更多)$', '', ln, flags=re.I)
        if not ln.strip(): continue
        clean_lines.append(ln)
    if not clean_lines: return ""
    blob = " ".join(clean_lines)
    blob = _URL_RE.sub(" ", blob)
    sentences = re.split(r'(?<=[。．.!?？！])\s+|(?<=[;:;])\s+', blob)
    out = []
    for sent in sentences:
        st = sent.strip()
        if not st: continue
        if len(st) < 8: continue
        if _NOISE_LINE_RE.search(st): continue
        if _too_many_urls(st): continue
        out.append(st)
    final = " ".join(out)
    final = re.sub(r'\s+', ' ', final).strip()
    return final

def html_or_markdown_to_clean_text(md: Optional[str], html: Optional[str], url: Optional[str] = None) -> str:
    base = ""
    if html and html.strip():
        base = extract_main_text(html, url)
    if not base and md:
        base = strip_markdown_to_text(md)
    return strict_body_filter(base)

# =========================
# 一般工具
# =========================
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
    if scheme not in ALLOWED_SCHEMES: return (False, "filtered: scheme")
    if host not in ALLOW_HOSTS: return (False, "offsite")
    if url_norm in DENY_URLS: return (False, "denied: exact")
    for frag in DENY_PATH_CONTAINS:
        if frag in path_lower: return (False, "denied: path-fragment")
    if ALLOW_PREFIXES:
        for pref in ALLOW_PREFIXES:
            if url_norm.startswith(pref): return (True, "ok")
        return (False, "filtered: not-in-allow-prefixes")
    return (True, "ok")

# =========================
# DB
# =========================
mc = MongoClient(MONGO_URI)
db = mc[DB_NAME]
articles = db[COL_ARTICLES]
contents = db[COL_CONTENTS]
articles.create_index("url", unique=True)
articles.create_index("content_id")
contents.create_index("hash", unique=True)

# =========================
# Firecrawl 本地 HTTP（自動尋徑）
# =========================
class LocalFirecrawl:
    CANDIDATE_SCRAPE_PATHS = ["/v2/scrape", "/scrape", "/v1/scrape", "/api/v1/scrape"]
    PROBE_ENDPOINTS = ["/v2/health", "/health", "/test"]

    def __init__(self, base: str, api_key: Optional[str] = None):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.sess: Session = requests.Session()
        self.common_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
        }
        if self.api_key:
            self.common_headers["Authorization"] = f"Bearer {self.api_key}"
        self.scrape_path = self._detect_scrape_path()

    def _detect_scrape_path(self) -> str:
        for ep in self.PROBE_ENDPOINTS:
            try:
                r = self.sess.get(self.base + ep, timeout=3)
                if r.status_code == 200: break
            except Exception:
                pass
        payload = {"url": "https://example.com"}
        for path in self.CANDIDATE_SCRAPE_PATHS:
            try:
                r = self.sess.post(self.base + path, headers=self.common_headers,
                                   data=json.dumps(payload), timeout=5)
                if r.status_code != 404:
                    print(f"[Probe] Using scrape path: {path} (status={r.status_code})")
                    return path
            except Exception:
                pass
        print("[Probe] No scrape path confirmed; default to /v2/scrape (will fallback on 404).")
        return "/v2/scrape"

    def scrape(self, url: str, wait_ms: int = 7000) -> dict:
        payload = {
            "url": url,
            "formats": ["markdown", "html"],
            "onlyMainContent": True,
            "waitFor": wait_ms,
            "headers": {
                "User-Agent": self.common_headers["User-Agent"],
                "Accept-Language": self.common_headers["Accept-Language"],
            }
        }
        start_idx = self.CANDIDATE_SCRAPE_PATHS.index(self.scrape_path) \
                    if self.scrape_path in self.CANDIDATE_SCRAPE_PATHS else 0
        for i in range(len(self.CANDIDATE_SCRAPE_PATHS)):
            path = self.CANDIDATE_SCRAPE_PATHS[(start_idx + i) % len(self.CANDIDATE_SCRAPE_PATHS)]
            r = self.sess.post(self.base + path, headers=self.common_headers,
                               data=json.dumps(payload), timeout=90)
            if r.status_code == 404:
                continue
            if r.status_code == 200:
                return r.json()
            raise RuntimeError(f"scrape failed at {path}: {r.status_code} {r.text[:200]}")
        raise RuntimeError("All scrape paths returned 404. Check API base/port.")

LOCAL = LocalFirecrawl(FIRECRAWL_BASE, FIRECRAWL_API_KEY)

# =========================
# 連結解析與標準化
# =========================
def coalesce(*vals):
    for v in vals:
        if v:
            return v
    return None

def extract_links_from_html(html: str, base_url: str) -> List[str]:
    links: List[str] = []
    soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.find_all("a", href=True):
        abs_url = urljoin(base_url, a.get("href"))
        try:
            norm = normalize_url(abs_url)
        except Exception:
            continue
        links.append(norm)
    return links

def dedupe_and_filter_links(links: List[str], base_url: str) -> List[str]:
    out, seen = [], set()
    for u in links:
        if u in seen: continue
        seen.add(u)
        ok, _ = allowed_by_hashlists(u)
        if not ok: continue
        if not same_site_only(u, base_url): continue
        out.append(u)
    return out

def standardize_page(api_res: dict, fallback_url: str) -> Tuple[str, Optional[str], Optional[str], List[str], Optional[str]]:
    data = coalesce(api_res.get("data"), api_res.get("content"), api_res)
    md = coalesce(api_res.get("markdown"), data.get("markdown") if isinstance(data, dict) else None)
    html = coalesce(api_res.get("html"), api_res.get("rawHtml"),
                    data.get("html") if isinstance(data, dict) else None,
                    data.get("rawHtml") if isinstance(data, dict) else None)
    url = coalesce(api_res.get("url"),
                   (api_res.get("metadata") or {}).get("url") if isinstance(api_res.get("metadata"), dict) else None,
                   (data.get("metadata") or {}).get("url") if isinstance(data, dict) and isinstance(data.get("metadata"), dict) else None,
                   fallback_url)
    title = coalesce(
        (api_res.get("metadata") or {}).get("title") if isinstance(api_res.get("metadata"), dict) else None,
        (data.get("metadata") or {}).get("title") if isinstance(data, dict) and isinstance(data.get("metadata"), dict) else None
    )
    links = []
    if html: links += extract_links_from_html(html, url)
    links = dedupe_and_filter_links(links, url)
    return url, md, html, links, title

# =========================
# BFS 爬蟲（只用本地 HTTP scrape）
# =========================
def bfs_crawl(start_url: str, max_depth: int, max_pages: int) -> List[dict]:
    queue = deque([(normalize_url(start_url), 0)])
    visited = set()
    payload = []
    print(f"[BFS] start={start_url} depth={max_depth} limit={max_pages}")
    while queue and len(payload) < max_pages:
        url, depth = queue.popleft()
        if url in visited: continue
        visited.add(url)
        try:
            res = LOCAL.scrape(url, wait_ms=7000)
            norm_url, md, html, links, title = standardize_page(res, url)
            print(f"[BFS] depth={depth} url={norm_url} html={bool(html)} md={bool(md)} links={len(links)}")
        except Exception as e:
            print(f"[BFS][ERR] {url} -> {e}")
            continue
        payload.append({"url": norm_url, "markdown": md, "html": html, "metadata": {"title": title} if title else {}})
        if depth < max_depth:
            for nxt in links:
                if nxt not in visited:
                    queue.append((nxt, depth + 1))
    return payload

payload = bfs_crawl(BASE_URL, MAX_DEPTH, MAX_PAGES)
print(f"[BFS] pages collected: {len(payload)}")

# =========================
# 寫入 MongoDB（使用嚴格正文過濾）
# =========================
ops: list[UpdateOne] = []
stats = {"total": len(payload), "denied": 0, "filtered": 0, "offsite": 0,
         "no_url": 0, "no_content": 0, "saved_ops": 0, "bulk_commits": 0, "errors": 0}
base_host = urlparse(BASE_URL).netloc.lower()

for d in payload:
    try:
        raw_url = d.get("url")
        if not raw_url: stats["no_url"] += 1; continue
        url_norm = normalize_url(raw_url)
        if not same_site_only(url_norm, BASE_URL): stats["offsite"] += 1; continue
        ok, _ = allowed_by_hashlists(url_norm)
        if not ok: stats["filtered"] += 1; continue

        md, html, _ = pick_content(d.get("markdown"), d.get("html"))

        # ★ 超嚴格正文：只留下純內文（無 markdown、無 URL、無延伸閱讀等）
        text_clean = html_or_markdown_to_clean_text(md, html, url_norm)

        if not text_clean or len(text_clean) < MIN_CONTENT_LEN:
            stats["no_content"] += 1
            continue

        title = (d.get("metadata") or {}).get("title")
        blob = (md if (md and md.strip()) else (html or "")).encode("utf-8", "ignore")
        h = sha256(blob).hexdigest()
        text_hash = sha256(text_clean.encode("utf-8", "ignore")).hexdigest()

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
        print("[PIPELINE][ERR]", e)
        continue

if ops:
    articles.bulk_write(ops, ordered=False)
    stats["saved_ops"] += len(ops); stats["bulk_commits"] += 1

print("Done.", stats)
