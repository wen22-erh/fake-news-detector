# -*- coding: utf-8 -*-
"""
crawler.py -- 中文新聞 Frontier 爬蟲
================================================
架構來自 crawler_life.py，擴展為多站點 + 完整反爬蟲措施。

Frontier 設計（與 crawler_life.py 一致）：
  - deque + appendleft：推薦/熱門連結插隊優先
  - queued / visited / saved_urls 三組 set
  - Bulk write（UpdateOne × BULK_SIZE=100）
  - SIGINT 優雅停止

反爬蟲措施（來自 fccna_worker.py）：
  - UA 輪換池（12 種 Chrome/Edge/Firefox）
  - Proxy 輪換器（封鎖時自動切換）
  - 封鎖特徵偵測（Cloudflare/403/429 等）
  - 封鎖後等待 5–12 秒，最多重試 MAX_PROXY_RETRIES 次
  - 每筆請求間隔 CRAWL_DELAY_MIN~MAX 秒
  - 各網站獨立 wait_ms / referer

目標：僅限台灣中文新聞網站
存入：new.articles + new.contents（欄位對齊 fccna_worker）
"""

from __future__ import annotations

import os, re, json, signal, time, random, threading
from collections import deque
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import (
    urlparse, urlunparse, urljoin,
    parse_qsl, urlencode, unquote, quote,
)

import requests
from requests import Session
from bs4 import BeautifulSoup
from pymongo import MongoClient, UpdateOne, ReturnDocument

try:
    import trafilatura; _HAS_TRAFILATURA = True
except ImportError:
    _HAS_TRAFILATURA = False

try:
    from readability import Document as ReadabilityDocument; _HAS_READABILITY = True
except ImportError:
    _HAS_READABILITY = False

try:
    from lxml import html as lxml_html
    from lxml.html.clean import Cleaner
    _HAS_LXML = True
except ImportError:
    _HAS_LXML = False

# ============================================================
# SIGINT 優雅停止
# ============================================================
STOP_REQUESTED = False

def _handle_stop(signum, frame):
    global STOP_REQUESTED
    if not STOP_REQUESTED:
        print("\n[STOP] 收到 Ctrl+C，準備安全停止並寫入剩餘資料...")
    STOP_REQUESTED = True

signal.signal(signal.SIGINT, _handle_stop)

# ============================================================
# Config
# ============================================================
MONGO_URI     = os.getenv("CRAWLER_MONGO_URI", "mongodb://localhost:27017/")
DB_NAME       = os.getenv("CRAWLER_DB_NAME",   "new")
COL_ARTICLES  = os.getenv("COL_ARTICLES",      "articles")
COL_CONTENTS  = os.getenv("COL_CONTENTS",      "contents")

FIRECRAWL_BASE    = os.getenv("FIRECRAWL_BASE",    "http://localhost:3002").rstrip("/")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")

TARGET_ARTICLES              = int(os.getenv("TARGET_ARTICLES",        "500"))
MAX_FRONTIER_SIZE            = int(os.getenv("MAX_FRONTIER_SIZE",      "50000"))
MAX_VISITED_URLS             = int(os.getenv("MAX_VISITED_URLS",       "200000"))
SEED_CATEGORY_PAGES          = int(os.getenv("SEED_CATEGORY_PAGES",    "3"))
SEED_LIMIT_PER_CATEGORY_PAGE = int(os.getenv("SEED_LIMIT_PER_PAGE",   "40"))
BULK_SIZE                    = int(os.getenv("BULK_SIZE",              "100"))
MIN_CONTENT_LEN              = int(os.getenv("MIN_CONTENT_LEN",        "150"))
MAX_PROXY_RETRIES            = int(os.getenv("MAX_PROXY_RETRIES",      "2"))
CRAWL_DELAY_MIN              = float(os.getenv("CRAWL_DELAY_MIN",      "1.5"))
CRAWL_DELAY_MAX              = float(os.getenv("CRAWL_DELAY_MAX",      "3.0"))

# 每次執行，單一 domain 最多存幾篇（防止某網站吃掉所有配額）
# 12 個網站 × 5 篇 = 60 篇上限，TARGET_ARTICLES=50 時實際會先到 50 停止
MAX_ARTICLES_PER_DOMAIN      = int(os.getenv("MAX_ARTICLES_PER_DOMAIN", "5"))

# 同一 domain 連續請求之間的額外冷卻（秒），疊加在 CRAWL_DELAY 上
SAME_DOMAIN_EXTRA_DELAY_MIN  = float(os.getenv("SAME_DOMAIN_EXTRA_DELAY_MIN", "2.0"))
SAME_DOMAIN_EXTRA_DELAY_MAX  = float(os.getenv("SAME_DOMAIN_EXTRA_DELAY_MAX", "4.0"))

_proxy_env = os.getenv("PROXY_POOL", "")
PROXY_POOL: List[str] = [p.strip() for p in _proxy_env.split(",") if p.strip()]

# ============================================================
# 目標網站（僅限台灣中文新聞）
# ============================================================
TARGET_SITES: Dict[str, Dict[str, Any]] = {
    "www.cna.com.tw": {
        "seeds": [
            "https://www.cna.com.tw/list/aall.aspx",
            "https://www.cna.com.tw/list/aopl.aspx",
            "https://www.cna.com.tw/list/ahel.aspx",
            "https://www.cna.com.tw/list/afe.aspx",
            "https://www.cna.com.tw/list/asoc.aspx",
        ],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "www.chinatimes.com": {
        "seeds": [
            "https://www.chinatimes.com/realtimenews/?chdtv",
        ],
        "wait_ms": 8000,
        "referer": "https://www.google.com.tw/",
    },
    "www.ettoday.net": {
        "seeds": ["https://www.ettoday.net/news/news-list.htm"],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "news.tvbs.com.tw": {
        "seeds": [
            "https://news.tvbs.com.tw/news",
            "https://news.tvbs.com.tw/world",
        ],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "news.ltn.com.tw": {
        "seeds": [
            "https://news.ltn.com.tw/list/breakingnews",
            "https://news.ltn.com.tw/list/politics",
            "https://news.ltn.com.tw/list/society",
        ],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "ec.ltn.com.tw": {
        "seeds": ["https://ec.ltn.com.tw/list/breakingnews"],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "www.setn.com": {
        "seeds": [
            "https://www.setn.com/ViewAll.aspx",
            "https://www.setn.com/ViewAll.aspx?PageGroupID=0",
        ],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "news.ebc.net.tw": {
        "seeds": ["https://news.ebc.net.tw/"],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "ctinews.com": {
        "seeds": ["https://ctinews.com/"],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "news.cts.com.tw": {
        "seeds": ["https://news.cts.com.tw/"],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "news.ttv.com.tw": {
        "seeds": ["https://news.ttv.com.tw/"],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "tw.news.yahoo.com": {
        "seeds": ["https://tw.news.yahoo.com/"],
        "wait_ms": 3000,
        "referer": "https://tw.news.yahoo.com/",
        "only_main_content": True,
    },
    "www.teepr.com": {
        "seeds": [
            "https://www.teepr.com/",
            "https://www.teepr.com/trending/",
        ],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
        "only_main_content": True,
    },
    "kknews.cc": {
        "seeds": [
            "https://kknews.cc/",
            "https://kknews.cc/world/",
            "https://kknews.cc/society/",
        ],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
        "only_main_content": True,
    },
    "www.mission-tw.com": {
        "seeds": [
            # 只用主頁與全站列表頁當文章來源；不放 /main_category、/category 等分類頁
            "https://www.mission-tw.com/",
            "https://www.mission-tw.com/articlelist/latest",
            "https://www.mission-tw.com/articlelist/hot",
        ],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
        "only_main_content": True,
    },
    "www.zanliv.com": {
        "seeds": [
            "https://www.zanliv.com/",
        ],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
        "only_main_content": True,
    },
    # ── 新增內容農場 / 另類媒體 ──────────────────────────────
    "toments.com": {                 # 觸電網（中文內容農場）
        "seeds": [
            "https://toments.com/",
            "https://toments.com/category/New/",
        ],
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
        "only_main_content": True,
    },
    "www.upworthy.com": {            # Upworthy（英文病毒/正能量內容）
        "seeds": [
            "https://www.upworthy.com/",
            "https://www.upworthy.com/category/culture/",
            "https://www.upworthy.com/category/family/",
            "https://www.upworthy.com/category/nature/",
            "https://www.upworthy.com/category/science/",
        ],
        "wait_ms": 2000,
        "referer": "https://www.google.com/",
        "only_main_content": True,
    },
    "www.distractify.com": {         # Distractify（英文娛樂/八卦）
        "seeds": [
            "https://www.distractify.com/",
            "https://www.distractify.com/news",
            "https://www.distractify.com/trending",
        ],
        "wait_ms": 2000,
        "referer": "https://www.google.com/",
        "only_main_content": True,
    },
    "www.thethings.com": {           # TheThings（英文娛樂，Valnet）
        "seeds": [
            "https://www.thethings.com/",
            "https://www.thethings.com/category/reality-tv/",
        ],
        "wait_ms": 2000,
        "referer": "https://www.google.com/",
        "only_main_content": True,
    },
    "www.naturalnews.com": {         # NaturalNews（英文偽科學/陰謀）
        "seeds": [
            "https://www.naturalnews.com/",
            "https://www.naturalnews.com/index.html",
        ],
        "wait_ms": 3000,
        "referer": "https://www.google.com/",
        "only_main_content": True,
    },
    "beforeitsnews.com": {           # Before It's News（英文陰謀論聚合）
        "seeds": [
            "https://beforeitsnews.com/v3/recent/",
            "https://beforeitsnews.com/v3/top50/",
            "https://beforeitsnews.com/",
        ],
        "wait_ms": 3000,
        "referer": "https://www.google.com/",
        "only_main_content": True,
    },
}

_LTN_SUFFIX = ".ltn.com.tw"

# 環境變數 CRAWL_DOMAINS 可限定只爬指定 domain（逗號分隔），空白 = 全部
# 例如：CRAWL_DOMAINS=www.cna.com.tw
_crawl_domains_env = os.getenv("CRAWL_DOMAINS", "")
if _crawl_domains_env:
    _allowed = {d.strip() for d in _crawl_domains_env.split(",") if d.strip()}
    TARGET_SITES = {k: v for k, v in TARGET_SITES.items() if k in _allowed}
    print(f"[CONFIG] CRAWL_DOMAINS 限定：{list(TARGET_SITES.keys())}")

_ALL_ALLOWED_DOMAINS: set = set(TARGET_SITES.keys())

TRACKING_PARAMS = {
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "gclid","fbclid","igshid","mc_cid","mc_eid","topic",
}
DENY_PATH_KEYWORDS = {
    "/about","/privacy","/terms","/contact","/member",
    "/login","/register","/author","/tag","/search",
    "/reels","/video","/podcast","/topic","/columnist",
}
PRIORITY_SECTION_KEYWORDS = [
    "推薦文章","熱門文章","同分類文章","相關文章",
    "查看更多文章","延伸閱讀","相關新聞","更多新聞",
]

# ============================================================
# User-Agent 池
# ============================================================
UA_POOL: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:138.0) Gecko/20100101 Firefox/138.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:138.0) Gecko/20100101 Firefox/138.0",
]
_SEC_CH_UA = {
    "148":'"Chromium";v="148", "Google Chrome";v="148", "Not-A.Brand";v="24"',
    "147":'"Chromium";v="147", "Google Chrome";v="147", "Not-A.Brand";v="24"',
    "148e":'"Chromium";v="148", "Microsoft Edge";v="148", "Not-A.Brand";v="24"',
}
_PLAT = {"Windows":'"Windows"',"Macintosh":'"macOS"',"Linux":'"Linux"'}

def pick_random_ua() -> Tuple[str, str, str]:
    ua = random.choice(UA_POOL)
    if "Firefox" in ua:
        plat = "Macintosh" if "Macintosh" in ua else ("Linux" if "Linux" in ua else "Windows")
        return ua, "", _PLAT[plat]
    if "Edg/" in ua:
        m = re.search(r"Chrome/(\d+)", ua)
        ver = (m.group(1) if m else "148") + "e"
        return ua, _SEC_CH_UA.get(ver, _SEC_CH_UA["148e"]), _PLAT["Windows"]
    m = re.search(r"Chrome/(\d+)", ua)
    ver = m.group(1) if m else "148"
    plat = "Macintosh" if "Macintosh" in ua else ("Linux" if "Linux" in ua else "Windows")
    return ua, _SEC_CH_UA.get(ver, _SEC_CH_UA["148"]), _PLAT[plat]

# ============================================================
# Proxy Rotator
# ============================================================
class ProxyRotator:
    def __init__(self, proxies):
        self._proxies = proxies if proxies else [None]
        self._lock = threading.Lock()
        self._index = 0

    def current(self):
        with self._lock:
            return self._proxies[self._index % len(self._proxies)]

    def rotate(self):
        with self._lock:
            self._index += 1
            return self._proxies[self._index % len(self._proxies)]

    def mark_blocked(self, proxy):
        print(f"[Proxy] 封鎖，切換（{proxy or '直連'}）")
        self.rotate()

    def all_blocked(self): return False

PROXY_ROTATOR = ProxyRotator(PROXY_POOL)

def set_proxy_env(proxy):
    for var in ["HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy"]:
        if proxy: os.environ[var] = proxy
        else: os.environ.pop(var, None)

# ============================================================
# 封鎖偵測
# ============================================================
BLOCK_SIGNATURES = [
    "Just a moment","Checking your browser","cf-browser-verification",
    "cf_chl_opt","Enable JavaScript and cookies to continue",
    "cloudflare-nginx","__cf_bm","cf_chl_prog",
    "Attention Required! | Cloudflare","Sorry, you have been blocked",
    "This process is automatic","DDoS protection by Cloudflare",
    "Performance &amp; security by Cloudflare",
    "403 Forbidden","Access Denied","Access denied",
    "You have been blocked","您的存取已被封鎖","您的IP已被封鎖","存取受限","異常存取",
    "chinatimes.com/blocked","請確認您不是機器人",
    "Too Many Requests","Rate limit exceeded","請稍後再試",
    "g-recaptcha","hcaptcha",
]
BLOCK_STATUS = {403,429,503,407}

def detect_block(html=None, status_code=None, error_msg=None):
    if status_code and status_code in BLOCK_STATUS:
        return f"HTTP {status_code}"
    text = (html or "") + (error_msg or "")
    for sig in BLOCK_SIGNATURES:
        if sig.lower() in text.lower(): return sig
    return None

# ============================================================
# URL 工具
# ============================================================
def normalize_url(url: str) -> str:
    try:
        p = urlparse(url.strip())
        path = quote(unquote(p.path or ""), safe="/-._~!$&'()*+,;=:@")
        q = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
        netloc = (p.netloc or "").lower().replace(":443","")
        return urlunparse(p._replace(netloc=netloc, path=path,
                                     query=urlencode(q, doseq=True), fragment=""))
    except Exception: return url

def extract_domain(url):
    try: return (urlparse(url).netloc or "").lower()
    except: return ""

def extract_site(url):
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""
    except: return ""

def is_allowed_domain(url):
    d = extract_domain(url)
    if d in _ALL_ALLOWED_DOMAINS: return True
    if d.endswith(_LTN_SUFFIX) or d == _LTN_SUFFIX.lstrip("."): return True
    return False

# ============================================================
# 文章 URL 驗證
# ============================================================
def _valid_cna(p):
    path = p.path or ""
    if not (path.startswith("/news/") and path.endswith(".aspx")): return False
    fn = path.rstrip("/").rsplit("/",1)[-1].replace(".aspx","")
    return bool(re.search(r"\d{6,}", fn))

def _valid_setn(p):
    if (p.path or "").lower() != "/news.aspx": return False
    params = dict(parse_qsl(p.query))
    nid = params.get("NewsID") or params.get("newsid")
    return bool(nid and re.match(r"^\d+$", str(nid)))

def _valid_ltn(p): return bool(re.search(r"/\d{4,}$", p.path or ""))

def _valid_chinatimes(p):
    path = p.path or ""; pl = path.lower()
    BLOCKED = {"/album/", "/search", "/tag/", "/topic/", "/author/", "/columnist/",
               "/album/memberarticles"}
    if any(pl.startswith(b) for b in BLOCKED) or path in {"/", ""}: return False
    # 排除純數字頁碼導覽頁（路徑結尾是 /數字/ 且數字 < 7 位）
    segments = [s for s in path.rstrip("/").split("/") if s]
    last = segments[-1] if segments else ""
    if re.match(r"^\d{1,6}$", last): return False
    return bool(re.search(r"\d{8,}", path))

def _valid_yahoo(p):
    path = p.path or ""
    NON = {"/topic/","/search","/weather","/politics","/entertainment","/sports","/finance","/lifestyle"}
    if path in {"/",""} or any(path.startswith(x) for x in NON): return False
    return path.endswith(".html")

def is_article_url(url: str) -> bool:
    try:
        norm = normalize_url(url); p = urlparse(norm); domain = (p.netloc or "").lower()
        path = p.path or ""
        if any(kw in path.lower() for kw in DENY_PATH_KEYWORDS): return False
        if path in {"/",""}: return False
        if domain in {"www.cna.com.tw","cna.com.tw"}: return _valid_cna(p)
        if domain == "www.chinatimes.com": return _valid_chinatimes(p)
        if domain == "www.setn.com": return _valid_setn(p)
        if domain.endswith(_LTN_SUFFIX) or domain == "ltn.com.tw": return _valid_ltn(p)
        if domain in {"tw.news.yahoo.com","tw.stock.yahoo.com"}: return _valid_yahoo(p)

        # Teepr：路徑含數字 ID，至少兩段
        if domain == "www.teepr.com":
            parts = [s for s in path.split("/") if s]
            if len(parts) < 2: return False
            return bool(re.search(r"\d{4,}", path))

        # 每日頭條：/n/短碼.html
        if domain == "kknews.cc":
            return bool(re.match(r"^/n/[a-z0-9]+\.html$", path))

        # 密訊：/article/分類/數字（域名會輪換，目前為 mission-tw.com）
        if domain == "www.mission-tw.com":
            return bool(re.match(r"^/article/[^/]+/\d+", path))

        # 贊新聞：/slug-數字/
        if domain == "www.zanliv.com":
            parts = [s for s in path.split("/") if s]
            if not parts: return False
            return bool(re.search(r"-\d+$", parts[-1]))

        # 觸電網：/數字/
        if domain == "toments.com":
            return bool(re.match(r"^/\d+/?$", path))

        # Distractify：/p/slug
        if domain == "www.distractify.com":
            return bool(re.match(r"^/p/[a-z0-9-]+", path))

        # NaturalNews：/YYYY-MM-DD-slug.html
        if domain in {"www.naturalnews.com", "naturalnews.com"}:
            return bool(re.match(r"^/\d{4}-\d{2}-\d{2}-[a-z0-9-]+\.html$", path))

        # Before It's News：/分類/YYYY/MM/slug-數字ID.html
        if domain == "beforeitsnews.com":
            return bool(re.match(r"^/[a-z0-9_-]+/\d{4}/\d{2}/.+-\d+\.html$", path))

        # Upworthy / TheThings：單層 slug 且含連字號（/category/... 多層自動排除）
        if domain in {"www.upworthy.com", "www.thethings.com"}:
            parts = [s for s in path.split("/") if s]
            if len(parts) != 1: return False
            slug = parts[0]
            if slug in {"about-us", "contact-us", "newsletter", "partnerships",
                        "privacy-policy", "terms-of-use", "write-for-us"}:
                return False
            return "-" in slug

        return len([s for s in path.split("/") if s]) >= 1
    except: return True

# ============================================================
# Firecrawl 客戶端（每次請求換 UA）
# ============================================================
class LocalFirecrawl:
    SCRAPE_PATHS = ["/v1/scrape","/api/v1/scrape","/v2/scrape","/scrape"]
    PROBE_EPS    = ["/v2/health","/health","/test"]

    def __init__(self, base, api_key=None):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.sess = requests.Session()
        self.scrape_path = "/v1/scrape"  # 寫死初始值，與 fccna_worker 一致

    def _detect(self):
        for ep in self.PROBE_EPS:
            try:
                if self.sess.get(self.base+ep, timeout=3).status_code == 200: break
            except: pass
        for path in self.SCRAPE_PATHS:
            try:
                r = self.sess.post(self.base+path, json={"url":"https://example.com"}, timeout=5)
                if r.status_code != 404:
                    print(f"[Probe] scrape path: {path} (status={r.status_code})")
                    return path
            except: pass
        return "/v1/scrape"

    def _make_headers(self):
        ua, sec_ch_ua, plat = pick_random_ua()
        h = {
            "User-Agent": ua,
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
        }
        if self.api_key: h["Authorization"] = f"Bearer {self.api_key}"
        return h, ua, sec_ch_ua, plat

    def scrape(self, url, wait_ms=5000, referer="", only_main_content=False):
        headers, ua, sec_ch_ua, plat = self._make_headers()
        payload = {
            "url": url,
            "formats": ["markdown","html","rawHtml"],
            "onlyMainContent": only_main_content,
            "waitFor": wait_ms,
            "headers": {
                "User-Agent": ua,
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br, zstd",
                **({
                    "sec-ch-ua": sec_ch_ua,
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": plat,
                } if sec_ch_ua else {}),
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                **( {"Referer": referer} if referer else {} ),
            },
        }
        start = self.SCRAPE_PATHS.index(self.scrape_path) if self.scrape_path in self.SCRAPE_PATHS else 0
        for i in range(len(self.SCRAPE_PATHS)):
            path = self.SCRAPE_PATHS[(start+i) % len(self.SCRAPE_PATHS)]
            r = self.sess.post(self.base+path, headers=headers, json=payload, timeout=90)
            if r.status_code == 404: continue
            if r.status_code == 200:
                res = r.json()
                if isinstance(res, dict) and res.get("success") is False:
                    raise RuntimeError(f"Firecrawl success=false: {json.dumps(res)[:300]}")
                return res
            raise RuntimeError(f"Firecrawl {path}: {r.status_code} {r.text[:200]}")
        raise RuntimeError("All Firecrawl paths returned 404")

LOCAL = LocalFirecrawl(FIRECRAWL_BASE, FIRECRAWL_API_KEY)

# 輕量 session（seed 頁面發現連結用）
PLAIN = requests.Session()
_ua0, _, _ = pick_random_ua()
PLAIN.headers.update({"User-Agent": _ua0, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})

# ============================================================
# MongoDB
# ============================================================
_mc      = MongoClient(MONGO_URI)
_db      = _mc[DB_NAME]
articles = _db[COL_ARTICLES]
contents = _db[COL_CONTENTS]

articles.create_index("url",        unique=True)
articles.create_index("content_id")
articles.create_index("domain")
contents.create_index("hash",       unique=True)
contents.create_index("text_hash")
contents.create_index("source_url")

# ============================================================
# 內容抽取（fccna_worker 邏輯 + crawler_life noise filter）
# ============================================================
_BLACKLIST_PAT = re.compile(
    r"(cookie|consent|gdpr|banner|popup|modal|subscribe|newsletter|"
    r"share|social|follow|breadcrumb|nav|header|footer|sidebar|"
    r"ad-|ads|advert|advertise|promo|promoted|sponsor|sponsored|"
    r"outbrain|taboola|widget|recommend|related|trending|most-read|"
    r"comment|reply|login|register|paywall|sign in|watch|listen|video)", re.I)
_NOISE_PAT = re.compile(
    r"(我們使用cookie|使用 cookie|隱私|版權所有|訂閱電子報|追蹤我們|"
    r"條款|隱私權政策|cookie policy|privacy|terms of service|"
    r"推薦文章|熱門文章|同分類文章|相關文章|查看更多文章|載入更多|"
    r"贊助|廣告|sponsored|advertisement|留言|評論區|延伸閱讀|"
    r"related|more on this story|read more|top stories|most read|"
    r"sign up|newsletter)", re.I)
_SENT_END = re.compile(r"[。．.!?？！]+")
_URL_RE   = re.compile(r"https?://[^\s)\]]+|www\.[^\s)\]]+", re.I)
_YAHOO_CUT_WORDS = [
    "延伸閱讀","相關新聞","熱門新聞","更多新聞",
    "你可能也想看","推薦閱讀","更多報導","(編輯:","（編輯:",
]

def _drop_blacklisted(root):
    if not _HAS_LXML: return
    for node in list(root.iter()):
        attrs = []
        for k, v in node.attrib.items():
            if k in ("class","id","role","aria-label") or k.startswith("data-"):
                attrs.append(str(v))
        if attrs and _BLACKLIST_PAT.search(" ".join(attrs)):
            parent = node.getparent()
            if parent is not None: parent.remove(node)

def _link_char_ratio(el):
    total = (el.text_content() or "").strip()
    if not total: return 1.0
    link_text = " ".join(a.text_content().strip() for a in el.xpath(".//a"))
    return min(1.0, (len(link_text)+1) / (len(total)+1))

def _keep_paragraph(p: str) -> bool:
    s = p.strip()
    if len(s) < 30: return False
    if _NOISE_PAT.search(s): return False
    if len(s) < 80 and not _SENT_END.findall(s): return False
    return True

def _dom_to_clean_text(root) -> str:
    if not _HAS_LXML: return ""
    paras = []
    for el in root.xpath("//article//p|//main//p|//p"):
        if _link_char_ratio(el) > 0.6: continue
        t = el.text_content().strip()
        if _keep_paragraph(t): paras.append(t)
    if not paras:
        for line in (root.text_content() or "").splitlines():
            if _keep_paragraph(line.strip()): paras.append(line.strip())
    return "\n".join(paras).strip()

def _yahoo_cut(text):
    if not text: return text
    cut = len(text)
    for w in _YAHOO_CUT_WORDS:
        idx = text.find(w)
        if 0 < idx < cut: cut = idx
    return text[:cut].strip()

def _clean_cna(text):
    if not text: return text
    text = re.sub(r'[（(](?:核稿)?編輯[：:][^）)]{1,20}[）)]\s*\d{0,7}\s*$',
                  '', text, flags=re.MULTILINE).strip()
    return re.sub(r'^[（(]中央社[^）)]{0,60}(?:專電|日電|電)[）)]\s*', '', text).strip()

def _clean_teepr_html(html: str) -> str:
    """截斷 Teepr 延伸閱讀、投票、廣告區塊，移除 Twitter 嵌入貼文"""
    if not html: return html

    # 移除 Twitter/X 嵌入貼文
    html = re.sub(
        r'<blockquote[^>]*class=["\']twitter-tweet["\'][^>]*>.*?</blockquote>',
        '', html, flags=re.S | re.I
    )

    CUT_MARKERS = [
        '<div class="teepr-poll-system"',
        "<!-- TEEPR Modern: Related Articles -->",
        "<!-- Related Articles -->",
        "<!--.single_post-->",
        '<div class="tm-related"',
        '<div class="teepr-related"',
        '<div id="related"',
        '<div class="fortune-widget-wrapper"',
        "閱讀更多",
    ]
    cut = len(html)
    for marker in CUT_MARKERS:
        idx = html.lower().find(marker.lower())
        if 0 < idx < cut:
            cut = idx
    return html[:cut]

def _coalesce(*vals):
    for v in vals:
        if v: return v
    return None

def extract_main_text(html, url=None):
    if not html or not html.strip(): return ""
    html_src = html
    if url and "yahoo.com" in url and _HAS_LXML:
        try:
            root = lxml_html.fromstring(html)
            cleaner = Cleaner(scripts=True,javascript=True,style=True,embedded=True,
                              frames=True,forms=True,annoying_tags=True,comments=True,links=False,meta=True)
            root = cleaner.clean_html(root); _drop_blacklisted(root)
            _YAHOO_KW = {"延伸閱讀","相關新聞","熱門新聞","更多新聞","推薦閱讀"}
            for hd in root.xpath(".//*[self::h2 or self::h3]"):
                if any(kw in (hd.text_content() or "") for kw in _YAHOO_KW):
                    parent = hd.getparent()
                    if parent:
                        rm = False
                        for child in list(parent):
                            if child is hd: rm = True
                            if rm: parent.remove(child)
                    break
            from lxml import etree
            html_src = etree.tostring(root, encoding="unicode", method="html")
        except: pass
    if _HAS_TRAFILATURA:
        try:
            txt = trafilatura.extract(html_src, url=url, favor_recall=True,
                                      with_metadata=False, no_fallback=False)
            if txt and len(txt.strip()) >= 200:
                lines = [ln.strip() for ln in txt.splitlines() if _keep_paragraph(ln)]
                result = "\n".join(lines).strip()
                if url and "yahoo.com" in url: result = _yahoo_cut(result)
                return result
        except: pass
    main_html = None
    if _HAS_READABILITY:
        try: main_html = ReadabilityDocument(html).summary(html_partial=True)
        except: pass
    source = main_html or html
    if _HAS_LXML:
        try:
            root = lxml_html.fromstring(source)
            cleaner = Cleaner(scripts=True,javascript=True,style=True,embedded=True,
                              frames=True,forms=True,annoying_tags=True,comments=True,links=False,meta=True)
            root = cleaner.clean_html(root); _drop_blacklisted(root)
            return _dom_to_clean_text(root)
        except: pass
    soup = BeautifulSoup(source, "html.parser")
    for t in soup(["script","style","noscript"]): t.decompose()
    lines = [ln.strip() for ln in soup.get_text(" ",strip=True).splitlines() if _keep_paragraph(ln)]
    return "\n".join(lines).strip()

def standardize_page(api_res, fallback_url):
    def _as_dict(v): return v if isinstance(v, dict) else {}
    data = _coalesce(api_res.get("data"), api_res.get("content"), api_res)
    data = _as_dict(data)
    top_meta  = _as_dict(api_res.get("metadata"))
    data_meta = _as_dict(data.get("metadata"))
    md        = _coalesce(api_res.get("markdown"), data.get("markdown"))
    html_main = _coalesce(api_res.get("html"), data.get("html"))
    raw_html  = _coalesce(api_res.get("rawHtml"), api_res.get("raw_html"),
                          data.get("rawHtml"), data.get("raw_html"))
    def _empty(h):
        if not h or not h.strip(): return True
        s = re.sub(r"\s+","",h.strip().lower())
        return s in {"<html><body></body></html>","<body></body>","<html></html>"}
    html  = raw_html if _empty(html_main) and raw_html else (html_main or raw_html)
    url   = _coalesce(api_res.get("url"),
                      top_meta.get("url"), top_meta.get("sourceURL"),
                      data_meta.get("url"), data_meta.get("sourceURL"), fallback_url)
    title = _coalesce(top_meta.get("title"), data_meta.get("title"))
    return normalize_url(url), md, html, title

def html_or_markdown_to_clean_text(md, html, url=None):
    # Teepr：截斷延伸閱讀區塊
    if url and "teepr.com" in url and html:
        html = _clean_teepr_html(html)
    base = ""
    if html and html.strip(): base = extract_main_text(html, url)
    if not base and md: base = md
    if not base: return ""
    s = re.sub(r"[ \t]*\n+[ \t]*", "\n", base)
    raw_lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    clean = []
    for ln in raw_lines:
        if _NOISE_PAT.search(ln): continue
        if len(ln) < 20: continue
        if _URL_RE.search(ln) and len(_URL_RE.findall(ln)) >= 2: continue
        ln = re.sub(r"^[\-\*\+•·▪◦•\d]+\s*", "", ln).strip()
        if ln: clean.append(ln)
    result = _URL_RE.sub(" ", " ".join(clean))
    result = re.sub(r"\s+", " ", result).strip()
    if url and "yahoo.com" in url: result = _yahoo_cut(result)
    if url and "cna.com.tw" in url: result = _clean_cna(result)
    return result

# ============================================================
# 工具
# ============================================================
def dedupe_keep_order(urls):
    out=[]; seen=set()
    for u in urls:
        if not u or u in seen: continue
        seen.add(u); out.append(u)
    return out

def utc_now(): return datetime.now(timezone.utc)
def utc_now_iso(): return utc_now().isoformat()

def _anchor_text(a): return re.sub(r"\s+"," ",a.get_text(" ",strip=True)).strip()

def _find_heading_text(node, max_hops=4):
    cur, hops = node, 0
    while cur is not None and hops < max_hops:
        text = re.sub(r"\s+"," ",cur.get_text(" ",strip=True)).strip()
        if any(kw.lower() in text.lower() for kw in PRIORITY_SECTION_KEYWORDS):
            return text
        cur = cur.parent; hops += 1
    return ""

def _with_page_param(url, page):
    p = urlparse(url); q = dict(parse_qsl(p.query, keep_blank_values=True))
    if page > 1: q["page"] = str(page)
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))

# ============================================================
# Frontier 佇列操作（與 crawler_life.py 相同）
# ============================================================
def enqueue_back(frontier, queued, url, stats):
    if len(frontier) >= MAX_FRONTIER_SIZE: stats["frontier_overflow"]+=1; return False
    if url in queued: return False
    queued.add(url); frontier.append(url); return True

def enqueue_front(frontier, queued, url, stats):
    """priority link → 插到最前面"""
    if len(frontier) >= MAX_FRONTIER_SIZE: stats["frontier_overflow"]+=1; return False
    if url in queued: return False
    queued.add(url); frontier.appendleft(url); return True

# ============================================================
# 發現連結
# ============================================================
def fetch_seed_links_from_section_page(section_url, referer=""):
    """固定抓 SEED_CATEGORY_PAGES 頁，邏輯與 crawler_life.py 相同"""
    links = []
    for page in range(1, SEED_CATEGORY_PAGES + 1):
        page_url = _with_page_param(section_url, page)
        ua, _, _ = pick_random_ua()
        PLAIN.headers.update({"User-Agent": ua})
        if referer: PLAIN.headers["Referer"] = referer
        proxy = PROXY_ROTATOR.current()
        proxies = {"http": proxy, "https": proxy} if proxy else None
        page_links = []
        try:
            r = PLAIN.get(page_url, proxies=proxies, timeout=25, allow_redirects=True)
            block = detect_block(html=r.text, status_code=r.status_code)
            if block:
                print(f"[SEED BLOCKED] {page_url} -> {block}")
                PROXY_ROTATOR.mark_blocked(proxy)
                time.sleep(random.uniform(3.0, 6.0))
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a.get("href","").strip()
                if not href: continue
                abs_url = normalize_url(urljoin(page_url, href))
                if is_allowed_domain(abs_url) and is_article_url(abs_url):
                    page_links.append(abs_url)
                if len(dedupe_keep_order(page_links)) >= SEED_LIMIT_PER_CATEGORY_PAGE:
                    break
            page_links = dedupe_keep_order(page_links)[:SEED_LIMIT_PER_CATEGORY_PAGE]
            links.extend(page_links)
            print(f"[SEED] page={page}/{SEED_CATEGORY_PAGES} {page_url} +{len(page_links)}")
        except Exception as e:
            print(f"[SEED ERR] {page_url}: {e}")
        time.sleep(random.uniform(0.5, 1.5))
    return dedupe_keep_order(links)


def extract_related_article_links(html, base_url):
    """priority/fallback 區分（與 crawler_life.py 相同）"""
    soup = BeautifulSoup(html or "", "html.parser")
    priority, fallback = [], []
    for a in soup.find_all("a", href=True):
        href = a.get("href","").strip()
        if not href: continue
        try: abs_url = normalize_url(urljoin(base_url, href))
        except: continue
        if not is_allowed_domain(abs_url) or not is_article_url(abs_url): continue
        txt = _anchor_text(a); ctx = _find_heading_text(a)
        nearby = " ".join([txt, ctx, a.parent.get_text(" ",strip=True)[:120] if a.parent else ""])
        if any(kw.lower() in nearby.lower() for kw in PRIORITY_SECTION_KEYWORDS):
            priority.append(abs_url)
        else:
            fallback.append(abs_url)
    # 有推薦區塊時補抓所有站內連結
    if any(k in (soup.get_text("\n",strip=True)) for k in PRIORITY_SECTION_KEYWORDS):
        for a in soup.find_all("a", href=True):
            href = a.get("href","").strip()
            if not href: continue
            try: u = normalize_url(urljoin(base_url, href))
            except: continue
            if is_allowed_domain(u) and is_article_url(u):
                fallback.append(u)
    base_norm = normalize_url(base_url)
    priority  = dedupe_keep_order([u for u in priority if u != base_norm])
    fallback  = dedupe_keep_order([u for u in fallback if u != base_norm and u not in set(priority)])
    return priority, fallback

# ============================================================
# DB 存檔 + bulk flush（與 crawler_life.py 相同）
# ============================================================
def save_article_to_db(url_norm, title, md, html, fetched_at, stats, ops):
    try:
        text_clean = html_or_markdown_to_clean_text(md, html, url_norm)
        if not text_clean or len(text_clean) < MIN_CONTENT_LEN:
            stats["no_content"] += 1; return False
        blob         = (md if (md and md.strip()) else (html or "")).encode("utf-8","ignore")
        content_hash = sha256(blob).hexdigest()
        text_hash    = sha256(text_clean.encode("utf-8","ignore")).hexdigest()
        content = contents.find_one_and_update(
            {"hash": content_hash},
            {"$set": {"hash":content_hash,"text_hash":text_hash,"markdown":md,"html":html,
                      "text":text_clean,"text_len":len(text_clean),"source_url":url_norm,
                      "updated_at":utc_now()}},
            upsert=True, return_document=ReturnDocument.AFTER, projection={"_id":1},
        )
        ops.append(UpdateOne(
            {"url": url_norm},
            {"$set": {"url":url_norm,"normalized_url":url_norm,"title":title or "",
                      "content_id":content["_id"],"fetched_at":fetched_at,
                      "domain":extract_domain(url_norm),"site":extract_site(url_norm),
                      "text_len":len(text_clean),"crawler":"frontier_crawler"}},
            upsert=True,
        ))
        stats["saved_ready"] += 1; return True
    except Exception as e:
        stats["db_errors"] += 1; print(f"[DB ERR] {url_norm}: {e}"); return False

def flush_bulk(ops, stats):
    if not ops: return
    try:
        articles.bulk_write(ops, ordered=False)
        stats["saved_ops"] += len(ops); stats["bulk_commits"] += 1
    except Exception as e:
        print(f"[BULK ERR] {e}"); stats["bulk_errors"] += 1
    finally: ops.clear()

# ============================================================
# 爬取單篇（含 proxy retry + 封鎖偵測）
# ============================================================
def scrape_with_anti_bot(url, cfg):
    wait_ms   = cfg.get("wait_ms", 5000)
    referer   = cfg.get("referer", "https://www.google.com.tw/")
    only_main = cfg.get("only_main_content", False)
    fetched_at = utc_now_iso()
    for attempt in range(MAX_PROXY_RETRIES + 1):
        proxy = PROXY_ROTATOR.current(); set_proxy_env(proxy)
        try:
            api_res = LOCAL.scrape(url, wait_ms=wait_ms, referer=referer,
                                   only_main_content=only_main)
            norm_url, md, html, title = standardize_page(api_res, url)
            block = detect_block(html=html)
            if block:
                print(f"[BLOCKED] attempt={attempt+1} proxy={proxy or '直連'} {url} -> {block}")
                PROXY_ROTATOR.mark_blocked(proxy); PROXY_ROTATOR.rotate()
                time.sleep(random.uniform(5.0, 12.0)); continue
            return norm_url, md, html, title, fetched_at
        except Exception as e:
            err = str(e)
            if detect_block(error_msg=err):
                print(f"[BLOCKED-EX] attempt={attempt+1} {url}: {err[:60]}")
                PROXY_ROTATOR.mark_blocked(proxy); PROXY_ROTATOR.rotate()
                time.sleep(random.uniform(5.0, 12.0)); continue
            raise
    raise RuntimeError(f"max retries ({MAX_PROXY_RETRIES+1}) exceeded")

# ============================================================
# 主 Frontier 爬蟲（架構與 crawler_life.crawl_life_frontier 完全一致）
# ============================================================
def crawl_frontier() -> Dict[str, int]:
    frontier:   deque           = deque()
    queued:     set             = set()
    visited:    set             = set()
    saved_urls: set             = set()
    ops:        List[UpdateOne] = []

    stats: Dict[str, int] = {
        "seed_count":0,"visited":0,"scrape_errors":0,"blocked":0,
        "non_article_skip":0,"duplicate_skip":0,"domain_cap_skip":0,
        "priority_added":0,"fallback_added":0,"no_content":0,
        "saved_ready":0,"saved_articles":0,
        "saved_ops":0,"bulk_commits":0,"bulk_errors":0,"db_errors":0,"frontier_overflow":0,
    }
    # 每個 domain 本次已存篇數（per-domain cap 用）
    domain_counts: Dict[str, int] = {}

    # 1) 初始 seed
    print("[FRONTIER] 初始化 seed 頁面...")
    for domain, cfg in TARGET_SITES.items():
        if STOP_REQUESTED: break
        referer = cfg.get("referer","https://www.google.com.tw/")
        for seed_url in cfg.get("seeds",[]):
            if STOP_REQUESTED: break
            seed_links = fetch_seed_links_from_section_page(seed_url, referer)
            added = 0
            for u in seed_links:
                if enqueue_back(frontier, queued, u, stats):
                    stats["seed_count"] += 1; added += 1
            print(f"[SEED DONE] {seed_url} -> +{added}")

    print(f"\n[FRONTIER] 初始佇列：{len(frontier)} 筆 | 目標：{TARGET_ARTICLES} 篇")

    # 2) BFS 主迴圈
    while (frontier and stats["saved_articles"] < TARGET_ARTICLES
           and len(visited) < MAX_VISITED_URLS and not STOP_REQUESTED):

        url = frontier.popleft()
        if url in visited: stats["duplicate_skip"]+=1; continue
        visited.add(url); stats["visited"]+=1

        if not is_article_url(url): stats["non_article_skip"]+=1; continue

        domain = extract_domain(url)
        cfg    = TARGET_SITES.get(domain,{})
        if not cfg and domain.endswith(_LTN_SUFFIX):
            cfg = TARGET_SITES.get("news.ltn.com.tw",{})
        if not cfg: cfg = {"wait_ms":3000,"referer":"https://www.google.com.tw/"}

        # ── Per-domain cap：這個 domain 本次已達上限 → 跳過 ──
        if domain_counts.get(domain, 0) >= MAX_ARTICLES_PER_DOMAIN:
            stats["domain_cap_skip"] += 1
            continue

        try:
            norm_url, md, html, title, fetched_at = scrape_with_anti_bot(url, cfg)
        except Exception as e:
            stats["scrape_errors"] += 1
            if "blocked" in str(e).lower(): stats["blocked"]+=1
            print(f"[SCRAPE ERR] {url}: {str(e)[:80]}"); continue

        if not is_article_url(norm_url): stats["non_article_skip"]+=1; continue

        # related links
        priority_links, fallback_links = [], []
        if html:
            try: priority_links, fallback_links = extract_related_article_links(html, norm_url)
            except Exception as e: print(f"[LINK ERR] {norm_url}: {e}")

        added_p = sum(1 for nxt in priority_links
                      if nxt not in visited and enqueue_front(frontier,queued,nxt,stats))
        stats["priority_added"] += added_p

        added_f = sum(1 for nxt in fallback_links
                      if nxt not in visited and enqueue_back(frontier,queued,nxt,stats))
        stats["fallback_added"] += added_f

        saved = save_article_to_db(norm_url, title, md, html, fetched_at, stats, ops)
        if saved and norm_url not in saved_urls:
            saved_urls.add(norm_url)
            stats["saved_articles"] += 1
            domain_counts[domain] = domain_counts.get(domain, 0) + 1

        if len(ops) >= BULK_SIZE: flush_bulk(ops, stats)

        print(f"[ARTICLE] saved={stats['saved_articles']}/{TARGET_ARTICLES} "
              f"visited={stats['visited']} frontier={len(frontier)} "
              f"domain={domain}({domain_counts.get(domain,0)}/{MAX_ARTICLES_PER_DOMAIN}) "
              f"p+={added_p} f+={added_f} -> {norm_url}")

        if not STOP_REQUESTED:
            # 基本間隔
            delay = random.uniform(CRAWL_DELAY_MIN, CRAWL_DELAY_MAX)
            # 若下一篇也是同 domain，加上額外冷卻
            next_url = frontier[0] if frontier else None
            if next_url and extract_domain(next_url) == domain:
                delay += random.uniform(SAME_DOMAIN_EXTRA_DELAY_MIN, SAME_DOMAIN_EXTRA_DELAY_MAX)
            time.sleep(delay)

    # 3) 收尾
    if STOP_REQUESTED: print("[STOP] 優雅停止，寫入剩餘資料...")
    flush_bulk(ops, stats)
    print(f"\n[DONE] {stats}")
    return stats

# ============================================================
# CLI 入口
# ============================================================
def main():
    import argparse
    global TARGET_ARTICLES
    parser = argparse.ArgumentParser(description="中文新聞 Frontier 爬蟲")
    parser.add_argument("--target",   type=int, default=TARGET_ARTICLES,
                        help=f"目標篇數（預設 {TARGET_ARTICLES}）")
    args = parser.parse_args()
    TARGET_ARTICLES = args.target
    stats = crawl_frontier()
    print("Done.", stats)

if __name__ == "__main__":
    main()