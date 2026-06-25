# -*- coding: utf-8 -*-
from __future__ import annotations
 
import os
import re
import csv
import json
import argparse
from pathlib import Path
from hashlib import sha256
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, unquote, quote
 
import threading
import queue
import time
import random

import requests
from requests import Session
from pymongo import MongoClient, ReturnDocument
from lxml import html as lxml_html
from lxml.html.clean import Cleaner
from bs4 import BeautifulSoup
 
# =========================
# Config
# =========================
FAKE_NEWS_MONGO_URI = os.getenv("FAKE_NEWS_MONGO_URI", "mongodb://localhost:27017/")
FAKE_NEWS_DB_NAME = os.getenv("FAKE_NEWS_DB_NAME", "fake_news_detector")
UNKNOWN_URLS_COLLECTION_NAME = os.getenv("UNKNOWN_URLS_COLLECTION_NAME", "unknown_urls")
 
# Crawler settings
FIRECRAWL_BASE = os.getenv("FIRECRAWL_BASE", "http://localhost:3002").rstrip("/")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
FIRECRAWL_WAIT_MS = int(os.getenv("FIRECRAWL_WAIT_MS", "6000"))
 
# 建議預設 False，避免 Firecrawl 的 main content extractor 把內容抽成空 body。
# 如果你確認特定網站可正常抽主文，再設 FIRECRAWL_ONLY_MAIN_CONTENT=true。
FIRECRAWL_ONLY_MAIN_CONTENT = os.getenv("FIRECRAWL_ONLY_MAIN_CONTENT", "false").lower() in {
    "1", "true", "yes", "y"
}
 
MIN_CONTENT_LEN = int(os.getenv("MIN_CONTENT_LEN", "200"))
DEFAULT_EXPORT_DIR = os.getenv("CRAWLER_EXPORT_DIR", "./exports")
 
# Debug 用：設 FIRECRAWL_DEBUG=1 可以印出 Firecrawl 回傳欄位長度。
FIRECRAWL_DEBUG = os.getenv("FIRECRAWL_DEBUG", "false").lower() in {
    "1", "true", "yes", "y"
}
 
TRACKING_PARAMS = {
    # 行銷追蹤參數
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "igshid", "mc_cid", "mc_eid",
    # CNA 分類/列表參數：?topic=XXXX 會讓頁面變成列表頁而非文章，必須去除
    "topic",
}

# =========================
# User-Agent 輪換池
# =========================
# 使用真實主流版本，避免版本號過舊被 Cloudflare 識別為 bot
USER_AGENT_POOL: list[str] = [
    # Chrome 148 Windows（最新版）
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    # Chrome 147 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    # Chrome 148 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    # Chrome 147 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    # Chrome 148 Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    # Chrome 147 Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    # Edge 148 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0",
    # Edge 147 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0",
    # Firefox 138 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0",
    # Firefox 137 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
    # Firefox 138 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:138.0) Gecko/20100101 Firefox/138.0",
    # Firefox 138 Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:138.0) Gecko/20100101 Firefox/138.0",
]

# sec-ch-ua 對應表（版本號必須和 UA 一致；Firefox 不送 sec-ch-ua）
_SEC_CH_UA_MAP: dict[str, str] = {
    "148": '"Chromium";v="148", "Google Chrome";v="148", "Not-A.Brand";v="24"',
    "147": '"Chromium";v="147", "Google Chrome";v="147", "Not-A.Brand";v="24"',
    "148e": '"Chromium";v="148", "Microsoft Edge";v="148", "Not-A.Brand";v="24"',
    "147e": '"Chromium";v="147", "Microsoft Edge";v="147", "Not-A.Brand";v="24"',
    "firefox": "",  # Firefox 不使用 sec-ch-ua
}

# sec-ch-ua-platform 對應表
_SEC_CH_UA_PLATFORM_MAP: dict[str, str] = {
    "Windows": '"Windows"',
    "Macintosh": '"macOS"',
    "Linux": '"Linux"',
}


def pick_random_ua() -> tuple[str, str, str]:
    """
    隨機選一個 User-Agent，回傳 (ua, sec_ch_ua, sec_ch_ua_platform)。
    Firefox UA 不帶 sec-ch-ua（回傳空字串）。
    Edge UA 使用 Edge 專屬的 sec-ch-ua brand。
    """
    ua = random.choice(USER_AGENT_POOL)

    # Firefox
    if "Firefox" in ua:
        if "Macintosh" in ua:
            platform = "Macintosh"
        elif "Linux" in ua:
            platform = "Linux"
        else:
            platform = "Windows"
        return ua, "", _SEC_CH_UA_PLATFORM_MAP[platform]

    # Edge
    if "Edg/" in ua:
        m = re.search(r"Chrome/(\d+)", ua)
        ver = m.group(1) if m else "148"
        sec_ch_ua = _SEC_CH_UA_MAP.get(f"{ver}e", _SEC_CH_UA_MAP["148e"])
        return ua, sec_ch_ua, _SEC_CH_UA_PLATFORM_MAP["Windows"]

    # Chrome
    m = re.search(r"Chrome/(\d+)", ua)
    ver = m.group(1) if m else "148"
    sec_ch_ua = _SEC_CH_UA_MAP.get(ver, _SEC_CH_UA_MAP["148"])
    if "Macintosh" in ua:
        platform = "Macintosh"
    elif "Linux" in ua:
        platform = "Linux"
    else:
        platform = "Windows"
    return ua, sec_ch_ua, _SEC_CH_UA_PLATFORM_MAP[platform]

# =========================
# Proxy Pool（IP 輪換）
# =========================
# 從環境變數設定，逗號分隔
# 例如：PROXY_POOL=http://user:pass@ip1:port,http://user:pass@ip2:port
# 若未設定，使用直連
_proxy_env = os.getenv("PROXY_POOL", "")
PROXY_POOL: list[str] = [p.strip() for p in _proxy_env.split(",") if p.strip()]

# 請求間隔（秒）
CRAWL_DELAY_MIN = float(os.getenv("CRAWL_DELAY_MIN", "1.5"))
CRAWL_DELAY_MAX = float(os.getenv("CRAWL_DELAY_MAX", "3.0"))


# =========================
# Block Detection 特徵
# =========================
BLOCK_SIGNATURES = [
    # Cloudflare challenge 頁面特徵（精確匹配，避免誤判正常頁面）
    "Just a moment", "Checking your browser", "cf-browser-verification",
    "cf_chl_opt", "Enable JavaScript and cookies to continue",
    "cloudflare-nginx", "__cf_bm", "cf_chl_prog",
    "Attention Required! | Cloudflare", "Sorry, you have been blocked",
    "This process is automatic", "DDoS protection by Cloudflare",
    "Performance &amp; security by Cloudflare",
    # 通用封鎖
    "403 Forbidden", "Access Denied", "Access denied",
    "You have been blocked", "您的存取已被封鎖",
    # 台灣媒體封鎖
    "您的IP已被封鎖", "存取受限", "異常存取",
    # 中時特定封鎖頁面特徵
    "chinatimes.com/blocked", "請確認您不是機器人",
    # Rate limit
    "Too Many Requests", "Rate limit exceeded", "請稍後再試",
    # Captcha（精確匹配，避免 robots.txt / robot 相關正常內容誤判）
    "g-recaptcha", "hcaptcha",
]

BLOCK_STATUS_CODES = {403, 429, 503, 407}

 
# =========================
# 白名單（公信力高，不需爬取）
# =========================
WHITELIST_DOMAINS: set[str] = {
    # 台灣
    "news.pts.org.tw",          # 公視新聞
    "udn.com",                  # 聯合新聞網
    # 國際
    "www.bbc.com",              # BBC
    "www.reuters.com",          # Reuters
    "www.cnn.com",              # CNN
    "www.snopes.com",           # Snopes（事實查核）
}
 
# 從環境變數動態擴充，逗號分隔，例如：WHITELIST_EXTRA_DOMAINS=example.com,example2.com
_extra_white = os.getenv("WHITELIST_EXTRA_DOMAINS", "")
if _extra_white:
    WHITELIST_DOMAINS.update(d.strip() for d in _extra_white.split(",") if d.strip())
 
# =========================
# 黑名單（非新聞類，不需爬取）
# =========================
BLACKLIST_DOMAINS: set[str] = {
    # 社群媒體
    "www.facebook.com",
    "www.instagram.com",
    "www.youtube.com",
    "twitter.com",
    "x.com",
    "www.threads.net",
    "www.tiktok.com",
    "line.me",
    # 電商購物
    "shopee.tw",
    "www.pchome.com.tw",
    "www.momo.com.tw",
    "www.books.com.tw",
    # 政府網站
    "www.gov.tw",
    "www.president.gov.tw",
    "www.ey.gov.tw",
    "www.ly.gov.tw",
    # 付費牆（幾乎全文章需訂閱，爬到的只有摘要，分析結果不可靠）
    "www.mirrormedia.mg",
    "www.storm.mg",             # 付費牆（VIP/VVIP 訂閱制）
    # UGC 平台（正文比例低，大量留言/sidebar 雜訊，分析結果不可靠）
    "beforeitsnews.com",
}
 
# 從環境變數動態擴充，逗號分隔，例如：BLACKLIST_EXTRA_DOMAINS=example.com,example2.com
_extra_black = os.getenv("BLACKLIST_EXTRA_DOMAINS", "")
if _extra_black:
    BLACKLIST_DOMAINS.update(d.strip() for d in _extra_black.split(",") if d.strip())
 
 
def is_whitelisted(url: str) -> bool:
    return extract_domain(url) in WHITELIST_DOMAINS
 
 
def is_blacklisted(url: str) -> bool:
    return extract_domain(url) in BLACKLIST_DOMAINS
 

def _is_valid_cna(parsed) -> bool:
    """CNA 文章頁驗證：/news/{category}/{articleId}.aspx"""
    path = parsed.path or ""
    if not (path.startswith("/news/") and path.endswith(".aspx")):
        return False
    filename = path.rstrip("/").rsplit("/", 1)[-1].replace(".aspx", "")
    return bool(re.search(r"\d{6,}", filename))


def _is_valid_setn(parsed) -> bool:
    """三立文章頁驗證：/News.aspx?NewsID=數字"""
    path = (parsed.path or "").lower()
    if path != "/news.aspx":
        return False
    params = dict(parse_qsl(parsed.query))
    news_id = params.get("NewsID") or params.get("newsid") or params.get("newsID")
    return bool(news_id and re.match(r"^\d+$", str(news_id)))


def _is_valid_ltn(parsed) -> bool:
    """
    自由時報文章頁驗證，支援所有 subdomain：
    - news.ltn.com.tw: /news/breakingnews/數字、/news/politics/paper/數字
    - ec.ltn.com.tw:   /article/paper/數字、/article/breakingnews/數字
    - ent.ltn.com.tw:  /news/breakingnews/數字
    - sports.ltn.com.tw: /news/breakingnews/數字
    共同特徵：路徑結尾是純數字 ID
    """
    path = parsed.path or ""
    # 路徑結尾必須是數字（文章 ID）
    return bool(re.search(r"/\d{4,}$", path))


# =========================
# 付費牆 / 贊助內容偵測
# =========================
# 各網站已知的付費或贊助內容路徑前綴，格式：domain -> [path_prefix, ...]
_PAYWALLED_PATHS: Dict[str, list] = {
    "www.chinatimes.com": ["/album/memberarticles"],   # 中時會員付費專區
    "www.zerohedge.com":  ["/sponsored-post"],         # ZeroHedge 贊助廣告文
}

def is_paywalled_url(url: str) -> Optional[str]:
    """
    檢查 URL 是否為已知的付費牆或贊助內容。
    回傳原因字串（給前端顯示）；若非付費內容則回傳 None。
    """
    try:
        normalized = normalize_url(url)
        parsed = urlparse(normalized)
        domain = (parsed.netloc or "").lower()
        path_lower = (parsed.path or "").lower()

        prefixes = _PAYWALLED_PATHS.get(domain, [])
        for prefix in prefixes:
            if path_lower.startswith(prefix):
                if domain == "www.chinatimes.com":
                    return "此為中時新聞網會員付費專區文章，無法爬取內容。"
                if domain == "www.zerohedge.com":
                    return "此為 ZeroHedge 贊助廣告內容，非新聞文章，不進行分析。"
                return "此為付費或贊助內容，無法爬取。"
    except Exception:
        pass
    return None


def _is_valid_zerohedge(parsed) -> bool:
    """ZeroHedge 文章頁驗證：拒絕贊助內容和非文章頁"""
    path = parsed.path or ""
    path_lower = path.lower()

    BLOCKED_PREFIXES = {
        "/sponsored-post",  # 贊助廣告文
        "/signup",          # 訂閱頁
        "/premium",         # 付費頁
        "/advertising",     # 廣告頁
        "/why-premium",     # 說明頁
    }
    if any(path_lower.startswith(p) for p in BLOCKED_PREFIXES):
        return False
    if path in {"/", ""}:
        return False
    return True


def _is_valid_chinatimes(parsed) -> bool:
    """
    中時文章頁驗證：
    - 允許：/realtimenews/{id}、/newspapers/{id}、/{category}/{id} 等新聞路徑
    - 拒絕：/album/MemberArticles/（會員付費專區）
    - 拒絕：/search、/tag、/topic 等非文章頁
    """
    path = parsed.path or ""
    path_lower = path.lower()

    # 明確拒絕的路徑前綴
    BLOCKED_PREFIXES = {
        "/album/memberarticles",  # 會員付費專區
        "/search",                # 搜尋頁
        "/tag/",                  # 標籤頁
        "/topic/",                # 主題頁
        "/author/",               # 作者頁
        "/columnist/",            # 專欄作者頁
    }
    if any(path_lower.startswith(p) for p in BLOCKED_PREFIXES):
        return False

    # 拒絕首頁和過短路徑
    if path in {"/", ""}:
        return False

    # 文章 URL 特徵：路徑結尾含數字 ID
    return bool(re.search(r"\d{6,}", path))


def _is_valid_yahoo(parsed) -> bool:
    """Yahoo 新聞文章頁驗證：/{title}.html，拒絕列表/分類頁"""
    path = parsed.path or ""
    NON_ARTICLE_PREFIXES = {"/topic/", "/search", "/weather", "/politics",
                            "/entertainment", "/sports", "/finance", "/lifestyle"}
    if path in {"/", ""}:
        return False
    if any(path.startswith(p) for p in NON_ARTICLE_PREFIXES):
        return False
    return path.endswith(".html")


def is_valid_article_url(url: str) -> bool:
    """
    驗證 URL 是否為可爬取的文章頁，而非列表/分類頁。
    各網站的驗證邏輯獨立在各自的 _is_valid_xxx() 函數中。
    未定義驗證的網站一律放行，由爬蟲自行處理。
    """
    try:
        normalized = normalize_url(url)
        parsed = urlparse(normalized)
        domain = (parsed.netloc or "").lower()

        if domain in {"www.cna.com.tw", "cna.com.tw"}:
            return _is_valid_cna(parsed)

        if domain == "www.chinatimes.com":
            return _is_valid_chinatimes(parsed)

        if domain == "www.setn.com":
            return _is_valid_setn(parsed)

        # 自由時報：所有 *.ltn.com.tw subdomain
        if domain.endswith(".ltn.com.tw") or domain == "ltn.com.tw":
            return _is_valid_ltn(parsed)

        if domain == "tw.news.yahoo.com" or domain == "tw.stock.yahoo.com":
            return _is_valid_yahoo(parsed)

        if domain == "www.zerohedge.com":
            return _is_valid_zerohedge(parsed)

        # 每日頭條：/n/短碼.html
        if domain == "kknews.cc":
            return bool(re.match(r"^/n/[a-z0-9]+\.html$", parsed.path or ""))

        # 密訊：/article/分類/數字（域名會輪換，目前為 mission-tw.com）
        if domain == "www.mission-tw.com":
            return bool(re.match(r"^/article/[^/]+/\d+", parsed.path or ""))

        # 贊新聞：/slug-數字/
        if domain == "www.zanliv.com":
            parts = [s for s in (parsed.path or "").split("/") if s]
            return bool(parts and re.search(r"-\d+$", parts[-1]))

        # 觸電網：/數字/
        if domain == "toments.com":
            return bool(re.match(r"^/\d+/?$", parsed.path or ""))

        # Distractify：/p/slug
        if domain == "www.distractify.com":
            return bool(re.match(r"^/p/[a-z0-9-]+", parsed.path or ""))

        # NaturalNews：/YYYY-MM-DD-slug.html
        if domain in {"www.naturalnews.com", "naturalnews.com"}:
            return bool(re.match(r"^/\d{4}-\d{2}-\d{2}-[a-z0-9-]+\.html$", parsed.path or ""))

        # Before It's News：/分類/YYYY/MM/slug-數字ID.html
        if domain == "beforeitsnews.com":
            return bool(re.match(r"^/[a-z0-9_-]+/\d{4}/\d{2}/.+-\d+\.html$", parsed.path or ""))

        # Upworthy / TheThings：單層 slug 且含連字號（/category/... 多層自動排除）
        if domain in {"www.upworthy.com", "www.thethings.com"}:
            parts = [s for s in (parsed.path or "").split("/") if s]
            if len(parts) != 1:
                return False
            slug = parts[0]
            if slug in {"about-us", "contact-us", "newsletter", "partnerships",
                        "privacy-policy", "terms-of-use", "write-for-us"}:
                return False
            return "-" in slug

        return True  # 其他網站不做限制，放行
    except Exception:
        return True



# Per-domain 爬取設定
# =========================
# wait_ms：等待頁面載入的上限時間（ms），優先用 waitForSelector，載入完就立刻返回不等滿
# actions：模擬真人行為，避免被 Cloudflare 行為分析擋住
# referer：偽裝從 Google 搜尋點進來，提高通過率
DOMAIN_PROFILES: Dict[str, Dict[str, Any]] = {
    # ── 台灣媒體 ──────────────────────────────────────────
    "www.chinatimes.com": {          # 🔴 高防護（Cloudflare）
        # wait_ms 拉高到 8000，讓 Firecrawl 的 waitFor 等 CF challenge 結束
        "wait_ms": 8000,
        "referer": "https://www.google.com.tw/",
    },
    "www.cna.com.tw": {              # 🟡 中防護
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "www.ettoday.net": {             # 🟡 中防護
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "news.ltn.com.tw": {             # 🟡 中防護（自由時報，代表設定）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    # 自由時報其他 subdomain 沿用相同設定（由 get_domain_profile suffix 匹配）
    "ec.ltn.com.tw": {               # 🟡 中防護（自由時報財經）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "ent.ltn.com.tw": {              # 🟡 中防護（自由時報娛樂）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "sports.ltn.com.tw": {           # 🟡 中防護（自由時報運動）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "talk.ltn.com.tw": {             # 🟡 中防護（自由時報論壇）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "news.tvbs.com.tw": {            # 🟡 中防護
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "ctinews.com": {                 # 🟡 中防護（中天）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "news.ebc.net.tw": {             # 🟡 中防護（東森）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "www.mirrormedia.mg": {          # 🟡 中防護（鏡週刊）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "www.setn.com": {                # 🟢 低防護（三立）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "news.cts.com.tw": {             # 🟢 低防護（華視）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    "news.ttv.com.tw": {             # 🟢 低防護（台視）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
    },
    # ── Yahoo 新聞 ─────────────────────────────────────────
    "tw.news.yahoo.com": {           # 🟡 中防護（台灣雅虎新聞）
        "wait_ms": 3000,
        "referer": "https://tw.news.yahoo.com/",
        "only_main_content": True,   # Yahoo 頁面有大量延伸閱讀/熱門新聞，必須只抓主文
    },
    "tw.stock.yahoo.com": {          # 🟡 中防護（台灣雅虎股市新聞）
        "wait_ms": 3000,
        "referer": "https://tw.stock.yahoo.com/",
        "only_main_content": True,
    },
    # ── 國外英語媒體（右翼/另類）────────────────────────────
    "www.breitbart.com": {           # 🟢 低防護（無 Cloudflare，Google Cloud）
        "wait_ms": 3000,
        "referer": "https://www.google.com/",
    },
    "www.zerohedge.com": {           # 🟢 低防護（無 Cloudflare，Google Cloud）
        "wait_ms": 3000,
        "referer": "https://www.google.com/",
    },
    "nypost.com": {                  # 🟢 低防護（無 Cloudflare，WordPress VIP）
        "wait_ms": 3000,
        "referer": "https://www.google.com/",
    },
    "www.newsmax.com": {             # 🟢 低防護（無 Cloudflare，Akamai）
        "wait_ms": 3000,
        "referer": "https://www.google.com/",
    },
    # ── 台灣內容農場 / 疑似假新聞 ────────────────────────────
    "www.teepr.com": {               # 🟢 低防護（內容農場）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
        "only_main_content": True,
    },
    "kknews.cc": {                   # 🟢 低防護（每日頭條，內容農場）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
        "only_main_content": True,
    },
    "www.mission-tw.com": {          # 🟢 低防護（密訊，域名會輪換）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
        "only_main_content": True,
    },
    "www.zanliv.com": {              # 🟢 低防護（贊新聞）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
        "only_main_content": True,
    },
    "toments.com": {                 # 🟢 低防護（觸電網，中文內容農場）
        "wait_ms": 2000,
        "referer": "https://www.google.com.tw/",
        "only_main_content": True,
    },
    "www.upworthy.com": {            # 🟢 低防護（Upworthy，英文病毒內容）
        "wait_ms": 2000,
        "referer": "https://www.google.com/",
        "only_main_content": True,
    },
    "www.distractify.com": {         # 🟢 低防護（Distractify，英文娛樂）
        "wait_ms": 2000,
        "referer": "https://www.google.com/",
        "only_main_content": True,
    },
    "www.thethings.com": {           # 🟢 低防護（TheThings，英文娛樂）
        "wait_ms": 2000,
        "referer": "https://www.google.com/",
        "only_main_content": True,
    },
    "www.naturalnews.com": {         # 🟡 偽科學/陰謀（可能有 Cloudflare）
        "wait_ms": 3000,
        "referer": "https://www.google.com/",
        "only_main_content": True,
    },
    "beforeitsnews.com": {           # 🟡 陰謀論聚合（有廣告牆）
        "wait_ms": 3000,
        "referer": "https://www.google.com/",
        "only_main_content": True,
    },
}
 
 
def get_domain_profile(url: str) -> Dict[str, Any]:
    """取得對應 domain 的爬取設定，找不到就用預設值。
    自由時報使用 suffix 匹配，未知 subdomain 沿用 news.ltn.com.tw 設定。
    """
    domain = extract_domain(url)
    if domain in DOMAIN_PROFILES:
        return DOMAIN_PROFILES[domain]
    # 自由時報 suffix 匹配：未定義的 subdomain 沿用 news.ltn.com.tw
    if domain.endswith(".ltn.com.tw") or domain == "ltn.com.tw":
        return DOMAIN_PROFILES.get("news.ltn.com.tw", {})
    return {
        "wait_ms": FIRECRAWL_WAIT_MS,
        "referer": "https://www.google.com/",
    }


# =========================
# ProxyRotator（Thread-safe IP 輪換器）
# =========================
class ProxyRotator:
    """
    Thread-safe proxy 選擇器（round-robin，無冷卻）。
    """
    def __init__(self, proxies: list[str]):
        self._proxies: list[str | None] = proxies if proxies else [None]
        self._lock = threading.Lock()
        self._index = 0

    def current(self) -> str | None:
        with self._lock:
            return self._proxies[self._index % len(self._proxies)]

    def rotate(self) -> str | None:
        with self._lock:
            self._index += 1
            return self._proxies[self._index % len(self._proxies)]

    def mark_blocked(self, proxy: str | None) -> None:
        """冷卻已移除，直接切換下一個 proxy。"""
        print(f"[ProxyRotator] blocked 偵測，切換下一個 proxy（{proxy or '直連'}）")
        self.rotate()

    def all_blocked(self) -> bool:
        return False

    def status(self) -> dict:
        with self._lock:
            return {
                str(p or "直連"): {"active": i == self._index % len(self._proxies)}
                for i, p in enumerate(self._proxies)
            }


PROXY_ROTATOR = ProxyRotator(PROXY_POOL)

 
# =========================
# DB
# =========================
fn_mongo = MongoClient(FAKE_NEWS_MONGO_URI)
fn_db = fn_mongo[FAKE_NEWS_DB_NAME]
unknown_urls_collection = fn_db[UNKNOWN_URLS_COLLECTION_NAME]
 
contents = fn_db["contents"]
articles = fn_db["articles"]
 
# =========================
# Firecrawl 本地 HTTP
# =========================
class LocalFirecrawl:
    CANDIDATE_SCRAPE_PATHS = ["/v1/scrape", "/api/v1/scrape"]
    PROBE_ENDPOINTS = ["/v2/health", "/health", "/test"]
 
    def __init__(self, base: str, api_key: Optional[str] = None):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.sess: Session = requests.Session()
        _ua, _sec_ch_ua, _sec_ch_ua_platform = pick_random_ua()
        self.common_headers = {
            "User-Agent": _ua,
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            # 暫存供 scrape() 參考
            "_sec_ch_ua": _sec_ch_ua,
            "_sec_ch_ua_platform": _sec_ch_ua_platform,
        }
        if self.api_key:
            self.common_headers["Authorization"] = f"Bearer {self.api_key}"
        self.scrape_path = "/v1/scrape"  # 寫死路徑，避免 Probe 偵測失敗 fallback 到錯誤路徑
 
    def _detect_scrape_path(self) -> str:
        for ep in self.PROBE_ENDPOINTS:
            try:
                r = self.sess.get(self.base + ep, timeout=3)
                if r.status_code == 200:
                    break
            except Exception:
                pass
 
        payload = {
            "url": "https://example.com",
            "formats": ["markdown", "html", "rawHtml"],
        }
 
        for path in self.CANDIDATE_SCRAPE_PATHS:
            try:
                r = self.sess.post(
                    self.base + path,
                    headers=self.common_headers,
                    json=payload,
                    timeout=5,
                )
                if r.status_code != 404:
                    print(f"[Probe] Using scrape path: {path} (status={r.status_code})")
                    return path
            except Exception:
                pass
 
        print("[Probe] No scrape path confirmed; default to /v2/scrape (will fallback on 404).")
        return "/v2/scrape"
 
    def scrape(self, url: str, wait_ms: int = 7000,
               referer: str = "", actions: list | None = None,
               proxy: str | None = None,
               only_main_content: bool | None = None) -> dict:
        # ⚠️  Self-hosted Firecrawl 不支援在 payload 裡傳 proxy（那是雲端版功能）。
        # 正確做法：在爬取前透過 set_proxy_env(proxy) 將 proxy 注入環境變數，
        # Firecrawl 底層的 Playwright/fetch 會自動讀取 HTTP_PROXY / HTTPS_PROXY。
        # waitFor 由 playwright-service 執行，需確認 PLAYWRIGHT_STEALTH=true 已設定。
        # actions 不傳：新版 Firecrawl 已將 actions 移至 /interact，self-hosted 不支援。
        payload = {
            "url": url,
            "formats": ["markdown", "html", "rawHtml"],
            "onlyMainContent": only_main_content if only_main_content is not None else FIRECRAWL_ONLY_MAIN_CONTENT,
            "waitFor": wait_ms,
            "headers": {
                "User-Agent": self.common_headers["User-Agent"],
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br, zstd",
                **({"sec-ch-ua": self.common_headers["_sec_ch_ua"],
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": self.common_headers["_sec_ch_ua_platform"]}
                   if self.common_headers.get("_sec_ch_ua") else {}),
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                **({"Referer": referer} if referer else {}),
            },
        }
 
        start_idx = (
            self.CANDIDATE_SCRAPE_PATHS.index(self.scrape_path)
            if self.scrape_path in self.CANDIDATE_SCRAPE_PATHS
            else 0
        )
 
        for i in range(len(self.CANDIDATE_SCRAPE_PATHS)):
            path = self.CANDIDATE_SCRAPE_PATHS[(start_idx + i) % len(self.CANDIDATE_SCRAPE_PATHS)]
            # proxy 已透過 set_proxy_env() 注入環境變數，這裡不需要額外傳 proxies 參數
            r = self.sess.post(
                self.base + path,
                headers=self.common_headers,
                json=payload,
                timeout=90,
            )
 
            if r.status_code == 404:
                continue
 
            if r.status_code == 200:
                res = r.json()
 
                # Firecrawl 有些版本會回 success=false 但 HTTP 仍是 200。
                if isinstance(res, dict) and res.get("success") is False:
                    raise RuntimeError(f"scrape returned success=false: {json.dumps(res, ensure_ascii=False)[:500]}")
 
                return res
 
            raise RuntimeError(f"scrape failed at {path}: {r.status_code} {r.text[:500]}")
 
        raise RuntimeError("All scrape paths returned 404. Check API base/port.")
 
 
LOCAL = LocalFirecrawl(FIRECRAWL_BASE, FIRECRAWL_API_KEY)


# =========================
# Proxy 環境變數注入
# =========================
def set_proxy_env(proxy: str | None) -> None:
    """
    Self-hosted Firecrawl 不支援 payload 裡的 proxy 欄位。
    正確做法是透過環境變數讓底層 Playwright / Node.js fetch 自動吃到 proxy。

    呼叫時機：每次 crawl_one_url 開始前，根據當前 ProxyRotator 選到的 proxy 設定。
    切換 proxy 時重新呼叫即可。
    清除時傳 None。
    """
    proxy_vars = ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]
    if proxy:
        for var in proxy_vars:
            os.environ[var] = proxy
        print(f"[ProxyEnv] 已設定 proxy 環境變數：{proxy}")
    else:
        for var in proxy_vars:
            os.environ.pop(var, None)
        print("[ProxyEnv] 已清除 proxy 環境變數（直連）")


# =========================
# URL 處理
# =========================
def normalize_url(url: str) -> str:
    p = urlparse(url)
    # decode 再重新 encode，確保不同編碼格式的同一 URL 對應到同一個 normalized URL
    path = quote(unquote(p.path or ""), safe="/-._~!$&'()*+,;=:@")
    q = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    norm = p._replace(
        netloc=p.netloc.lower(),
        path=path,
        query=urlencode(q, doseq=True),
        fragment="",
    )
    return urlunparse(norm)
 
 
def extract_domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""
 
 
def extract_site(url: str) -> str:
    try:
        p = urlparse(url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
        return ""
    except Exception:
        return ""
 
 
def coalesce(*vals):
    for v in vals:
        if v:
            return v
    return None
 
 
def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}
 
 
def _visible_text_len_from_html(value: Optional[str]) -> int:
    if not value or not value.strip():
        return 0
 
    try:
        soup = BeautifulSoup(value, "html.parser")
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        return len(soup.get_text(" ", strip=True))
    except Exception:
        text = re.sub(r"<[^>]+>", " ", value)
        text = re.sub(r"\s+", " ", text).strip()
        return len(text)
 
 
def is_effectively_empty_html(value: Optional[str], min_visible_chars: int = 20) -> bool:
    """
    Firecrawl onlyMainContent 抽空時，常見結果是：
    <html><body></body></html>
    長度剛好 26。
    """
    if not value or not value.strip():
        return True
 
    s = re.sub(r"\s+", "", value.strip().lower())
 
    empty_shells = {
        "<html><body></body></html>",
        "<body></body>",
        "<html></html>",
    }
 
    if s in empty_shells:
        return True
 
    return _visible_text_len_from_html(value) < min_visible_chars
 
 
def inspect_firecrawl_response(api_res: dict) -> None:
    if not FIRECRAWL_DEBUG:
        return
 
    data = api_res.get("data") if isinstance(api_res.get("data"), dict) else api_res
    data = _as_dict(data)
 
    report = {
        "success": api_res.get("success"),
        "top_keys": list(api_res.keys()),
        "data_keys": list(data.keys()),
        "error": api_res.get("error") or data.get("error"),
        "warning": data.get("warning"),
        "metadata": data.get("metadata"),
        "markdown_len": len(data.get("markdown") or ""),
        "html_len": len(data.get("html") or ""),
        "rawHtml_len": len(data.get("rawHtml") or ""),
        "html_preview": (data.get("html") or "")[:200],
        "rawHtml_preview": (data.get("rawHtml") or "")[:200],
    }
 
    print("[Firecrawl Debug]")
    print(json.dumps(report, ensure_ascii=False, indent=2))
 
 
def standardize_page(api_res: dict, fallback_url: str) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """
    統一 Firecrawl 不同版本的 response shape。
 
    關鍵修正：
    - 若 html 是 26-byte 空 shell，優先改用 rawHtml。
    - metadata.url / metadata.sourceURL 都支援。
    """
    api_dict = _as_dict(api_res)
    data = coalesce(api_dict.get("data"), api_dict.get("content"), api_dict)
    data = _as_dict(data)
 
    top_metadata = _as_dict(api_dict.get("metadata"))
    data_metadata = _as_dict(data.get("metadata"))
 
    md = coalesce(
        api_dict.get("markdown"),
        data.get("markdown"),
    )
 
    html_main = coalesce(
        api_dict.get("html"),
        data.get("html"),
    )
 
    raw_html = coalesce(
        api_dict.get("rawHtml"),
        api_dict.get("raw_html"),
        data.get("rawHtml"),
        data.get("raw_html"),
    )
 
    # 若 html 是 <html><body></body></html> 這類空殼，改用 rawHtml。
    if is_effectively_empty_html(html_main) and raw_html:
        html = raw_html
    else:
        html = html_main or raw_html
 
    url = coalesce(
        api_dict.get("url"),
        top_metadata.get("url"),
        top_metadata.get("sourceURL"),
        data_metadata.get("url"),
        data_metadata.get("sourceURL"),
        fallback_url,
    )
 
    title = coalesce(
        top_metadata.get("title"),
        data_metadata.get("title"),
    )
 
    return normalize_url(url), md, html, title
 
 
# =========================
# 內容抽取與清洗
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
    r"comment|reply|login|register|paywall|"
    r"caas-readmore|caas-related|caas-carousel|caas-more-from|"
    r"related-articles|related-news|more-stories|"
    r"recommendation-contents|comments-wrapper|share-button-group|"
    r"read-more-vendor|recommended-article-stream|module-coview|"
    r"StretchedBox|D\(f\)|Pos\(r\))",
    re.I,
)
 
_CONTENT_NOISE_PATTERNS = re.compile(
    r"(我們使用cookie|使用 cookie|隱私|版權所有|訂閱電子報|追蹤我們|"
    r"條款|隱私權政策|cookie policy|privacy|terms of service|"
    r"enable cookies|accept cookies|consent|manage preferences|"
    r"贊助|廣告|sponsored|advertisement|留言|評論區|熱門文章|延伸閱讀|你可能還喜歡)",
    re.I,
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
 
 
_YAHOO_READMORE_KEYWORDS = {
    "延伸閱讀", "相關新聞", "熱門新聞", "更多新聞",
    "你可能也想看", "推薦閱讀", "更多報導",
}


def _drop_yahoo_readmore(root: lxml_html.HtmlElement) -> None:
    """
    Yahoo 頁面：找到內文包含截斷關鍵字的 <h2>/<h3>，
    把它和它之後所有的兄弟節點一起移除。
    在 DOM 層截斷，比文字層截斷更根本。
    """
    for heading in root.xpath(".//*[self::h2 or self::h3]"):
        text = (heading.text_content() or "").strip()
        if any(kw in text for kw in _YAHOO_READMORE_KEYWORDS):
            parent = heading.getparent()
            if parent is None:
                continue
            removing = False
            for child in list(parent):
                if child is heading:
                    removing = True
                if removing:
                    parent.remove(child)
            break  # 只處理第一個截斷點


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
 
 
_YAHOO_CUT_PATTERNS = [
    "延伸閱讀", "相關新聞", "熱門新聞", "更多新聞",
    "你可能也想看", "推薦閱讀", "更多報導",
    "Yahoo即時新聞", "新頭殼", "三立新聞網",
    "聯合新聞網", "自由時報", "ETtoday", "nownews",
    "即時中心", "娛樂中心", "生活中心", "財經中心",
    # 各媒體「更多報導」區塊標題
    "更多udn報導", "更多三立新聞網報導", "更多ETtoday報導",
    "更多中時報導", "更多TVBS報導", "更多聯合報導",
    # 文章結尾標記
    "【看原文連結】", "看原文連結",
    "原文出處：", "原文出處:",
    # CNA 編輯署名（文章到此結束）
    "(編輯:", "（編輯:",
]


def _yahoo_text_cut(text: str) -> str:
    """
    Yahoo 文字層雙重截斷，供 trafilatura 路徑和 html_or_markdown_to_clean_text 共用。
    第一道：關鍵字截斷（正文結尾標記、媒體來源標記）
    第二道：連續短句截斷（相關新聞卡片的短標題特徵）
    """
    if not text:
        return text

    # 第一道：關鍵字截斷
    cut_pos = len(text)
    for pattern in _YAHOO_CUT_PATTERNS:
        idx = text.find(pattern)
        if idx != -1 and idx < cut_pos:
            cut_pos = idx
    text = text[:cut_pos].strip()

    # 第二道：連續短句截斷
    sentences = text.split(" ")
    result_sentences = []
    short_count = 0
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) < 30 and not any(sent.endswith(p) for p in ["。", "！", "？", ".", "!", "?"]):
            short_count += 1
        else:
            short_count = 0
        result_sentences.append(sent)
        if short_count >= 3:
            result_sentences = result_sentences[:-3]
            break

    return " ".join(result_sentences).strip()


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

    # Yahoo 頁面：先做 DOM 清理，再送給 trafilatura。
    # trafilatura 有自己的 HTML parser，不走我們的 lxml 路徑，
    # 所以必須在呼叫前先把雜訊節點從 HTML 移除。
    html_for_extract = html
    if url and "yahoo.com" in url:
        try:
            root = lxml_html.fromstring(html)
            cleaner = Cleaner(
                scripts=True, javascript=True, style=True,
                embedded=True, frames=True, forms=True,
                annoying_tags=True, comments=True, links=False, meta=True,
            )
            root = cleaner.clean_html(root)
            _drop_blacklisted_nodes(root)
            _drop_yahoo_readmore(root)
            from lxml import etree
            html_for_extract = etree.tostring(root, encoding="unicode", method="html")
        except Exception:
            html_for_extract = html  # 清理失敗就用原始 HTML

    if _HAS_TRAFILATURA:
        try:
            txt = trafilatura.extract(
                html_for_extract,
                url=url,
                favor_recall=True,
                with_metadata=False,
                no_fallback=False,
            )
            if txt and len(txt.strip()) >= 200:
                lines = [ln.strip() for ln in txt.splitlines() if _keep_paragraph(ln)]
                result = "\n".join(lines).strip()
                # Yahoo 頁面：文字層再補一道截斷（處理 trafilatura 仍可能帶入的雜訊）
                if url and "yahoo.com" in url:
                    result = _yahoo_text_cut(result)
                return result
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
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
 
        raw = soup.get_text(" ", strip=True)
        lines = [ln.strip() for ln in raw.splitlines() if _keep_paragraph(ln)]
        return "\n".join(lines).strip()
 
    cleaner = Cleaner(
        scripts=True,
        javascript=True,
        style=True,
        embedded=True,
        frames=True,
        forms=True,
        annoying_tags=True,
        comments=True,
        links=False,
        meta=True,
    )
 
    root = cleaner.clean_html(root)
    _drop_blacklisted_nodes(root)

    # Yahoo 頁面：在 DOM 層截掉延伸閱讀區塊（h2/h3 截斷法）
    if url and "yahoo.com" in url:
        _drop_yahoo_readmore(root)

    text = _dom_to_clean_text(root)
    lines = [ln.strip() for ln in text.splitlines() if _keep_paragraph(ln)]
 
    return "\n".join(lines).strip()
 
 
_URL_RE = re.compile(r"https?://[^\s)\]]+|www\.[^\s)\]]+", re.I)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^\)]*\)")
_MD_CODEBLOCK_RE = re.compile(r"```.*?```", re.S)
_MD_INLINE_CODE_RE = re.compile(r"`[^`]+`")
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+.*$")
_MD_QUOTE_RE = re.compile(r"^\s{0,3}>\s+.*$")
_MD_LIST_RE = re.compile(r"^\s{0,3}([*\-\+•·▪◦]|[0-9]+[.)])\s+")
_MD_RULE_RE = re.compile(r"^\s{0,3}([-*_])\1{2,}\s*$")
_MD_BOLD_ITALIC_RE = re.compile(r"(\*\*|\*|__|_)")
_MD_HTML_TAG_RE = re.compile(r"<[^>]+>")
 
_NOISE_LINE_RE = re.compile(
    r"(cookie|cookies|隱私|privacy|GDPR|條款|terms|"
    r"版權|copyright|使用條款|服務條款|"
    r"訂閱|newsletter|追蹤我們|追蹤|關注|"
    r"分享|share|social|"
    r"延伸閱讀|相關閱讀|你可能還喜歡|"
    r"熱門|趨勢|trending|"
    r"留言|評論|comment|reply|"
    r"廣告|贊助|sponsored|advertisement|promo|outbrain|taboola|推薦|"
    r"返回|回到|上一頁|下一頁|閱讀全文|read more)",
    re.I,
)
 
 
def _too_many_urls(line: str, max_ratio: float = 0.15) -> bool:
    urls = list(_URL_RE.finditer(line))
    if not urls:
        return False
 
    url_chars = sum((m.end() - m.start()) for m in urls)
    return url_chars / max(1, len(line)) >= max_ratio
 
 
def strip_markdown_to_text(md: str) -> str:
    if not md:
        return ""
 
    s = md
    s = _MD_CODEBLOCK_RE.sub(" ", s)
    s = _MD_IMG_RE.sub(" ", s)
    s = _MD_LINK_RE.sub(r"\1", s)
    s = _MD_INLINE_CODE_RE.sub(" ", s)
 
    lines = []
    for raw in s.splitlines():
        line = raw.strip()
 
        if not line:
            continue
        if _MD_HEADING_RE.match(line):
            # heading 包含截斷關鍵字（如「延伸閱讀」）→ 正文到此結束
            if any(kw in line for kw in _YAHOO_READMORE_KEYWORDS):
                break
            continue
        if _MD_QUOTE_RE.match(line):
            continue
        if _MD_LIST_RE.match(line):
            continue
        if _MD_RULE_RE.match(line):
            continue
 
        lines.append(line)
 
    s = "\n".join(lines)
    s = _MD_BOLD_ITALIC_RE.sub("", s)
    s = _MD_HTML_TAG_RE.sub(" ", s)
    s = _URL_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*([。．.!?？！；;,:：，、])\s*", r"\1", s)
    s = re.sub(r"(?:[。．!?？！]){2,}", lambda m: m.group(0)[0], s)
 
    return s.strip()
 
 
def strict_body_filter(text: str) -> str:
    if not text:
        return ""
 
    s = re.sub(r"[ \t]*\n+[ \t]*", "\n", text)
    raw_lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
 
    clean_lines: List[str] = []
 
    for ln in raw_lines:
        if _NOISE_LINE_RE.search(ln):
            continue
        if _too_many_urls(ln):
            continue
        if len(ln) < 15:
            continue
 
        ln = re.sub(r"^[\-\*\+•·▪◦•\d]+\s*", "", ln)
        ln = re.sub(r"(?:閱讀全文|Read more|更多)$", "", ln, flags=re.I)
 
        if not ln.strip():
            continue
 
        clean_lines.append(ln)
 
    if not clean_lines:
        return ""
 
    blob = " ".join(clean_lines)
    blob = _URL_RE.sub(" ", blob)
 
    sentences = re.split(r"(?<=[。．.!?？！])\s+|(?<=[;:;])\s+", blob)
 
    out = []
    for sent in sentences:
        st = sent.strip()
 
        if not st:
            continue
        if len(st) < 8:
            continue
        if _NOISE_LINE_RE.search(st):
            continue
        if _too_many_urls(st):
            continue
 
        out.append(st)
 
    final = " ".join(out)
    final = re.sub(r"\s+", " ", final).strip()
 
    return final
 
 
def _clean_cna_text(text: str) -> str:
    """
    清除中央社文章的頭尾固定格式雜訊：
    - 開頭：（中央社記者XXX地點日期專電）或（中央社XXXX日電）等
    - 結尾：（編輯：XXX）YYYMMDD 的署名與日期碼
    """
    if not text:
        return text

    # 結尾清除：（編輯：XXX）或（核稿編輯：XXX）後面可能跟著 7 位日期數字
    text = re.sub(
        r'[（(](?:核稿)?編輯[：:][^）)]{1,20}[）)]\s*\d{0,7}\s*$',
        '', text, flags=re.MULTILINE
    ).strip()

    # 開頭清除：（中央社...專電）或（中央社...日電）
    text = re.sub(
        r'^[（(]中央社[^）)]{0,60}(?:專電|日電|電)[）)]\s*',
        '', text
    ).strip()

    return text


def html_or_markdown_to_clean_text(
    md: Optional[str],
    html: Optional[str],
    url: Optional[str] = None,
) -> str:
    base = ""
 
    if html and html.strip():
        base = extract_main_text(html, url)
 
    if not base and md:
        base = strip_markdown_to_text(md)
 
    cleaned = strict_body_filter(base)

    # Yahoo 新聞頁面即使用 onlyMainContent 仍常混入延伸閱讀和熱門新聞。
    # 偵測到分隔關鍵字就截斷，比固定字數更精準。
    if url and "yahoo.com" in url:
        cleaned = _yahoo_text_cut(cleaned)

    # CNA 文章頭尾固定格式雜訊清除
    if url and ("cna.com.tw" in url):
        cleaned = _clean_cna_text(cleaned)

    return cleaned
 
 
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
 
 
# =========================
# Backend Pipeline Integration
# =========================
def load_job(job_path: str) -> Dict[str, Any]:
    path = Path(job_path)
 
    if not path.exists():
        raise FileNotFoundError(f"job file not found: {job_path}")
 
    with path.open("r", encoding="utf-8") as f:
        job = json.load(f)
 
    if not isinstance(job, dict):
        raise ValueError("job payload must be a JSON object")
 
    urls = job.get("urls", [])
 
    if not isinstance(urls, list):
        raise ValueError("job.urls must be a list")
 
    job.setdefault("job_id", path.stem)
    job.setdefault("source", "backend_unknown_urls")
    job.setdefault("created_at", utc_now_iso())
 
    return job
 
 
def prepare_urls(urls: List[str]) -> List[str]:
    prepared: List[str] = []
    seen: set[str] = set()
 
    for raw in urls:
        raw = str(raw).strip()
 
        if not raw:
            continue
 
        normalized = normalize_url(raw)
 
        if not normalized or normalized in seen:
            continue
 
        # 白名單或黑名單的 domain 直接略過
        if is_whitelisted(normalized) or is_blacklisted(normalized):
            continue

        # 非文章頁（如 CNA 列表/分類頁）直接略過，避免爬到無關內容
        if not is_valid_article_url(normalized):
            print(f"[SKIP] 非文章頁，略過爬取：{normalized}")
            continue
 
        seen.add(normalized)
        prepared.append(normalized)
 
    return prepared
 
 
def ensure_export_dir(export_dir: str) -> Path:
    p = Path(export_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p
 
 
def mark_urls_status(
    urls: List[str],
    status: str,
    job_id: str,
    extra: Dict[str, Any] | None = None,
) -> None:
    if not urls:
        return
 
    now = utc_now_iso()
    payload = {
        "crawl_status": status,
        "last_job_id": job_id,
        "last_status_updated_at": now,
    }
 
    if extra:
        payload.update(extra)
 
    normalized_urls = [normalize_url(u) for u in urls if str(u).strip()]
 
    if not normalized_urls:
        return
 
    unknown_urls_collection.update_many(
        {"normalized_url": {"$in": normalized_urls}},
        {"$set": payload},
    )
 
 
def mark_single_url_status(
    url: str,
    status: str,
    job_id: str,
    extra: Dict[str, Any] | None = None,
) -> None:
    normalized = normalize_url(url)
 
    if not normalized:
        return
 
    payload = {
        "crawl_status": status,
        "last_job_id": job_id,
        "last_status_updated_at": utc_now_iso(),
    }
 
    if extra:
        payload.update(extra)
 
    unknown_urls_collection.update_one(
        {"normalized_url": normalized},
        {"$set": payload},
    )
 
 
def write_jsonl(rows: List[Dict[str, Any]], out_path: Path) -> None:
    with out_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
 
 
def write_csv(rows: List[Dict[str, Any]], out_path: Path) -> None:
    fieldnames = [
        "job_id",
        "url",
        "normalized_url",
        "domain",
        "site",
        "title",
        "text",
        "text_len",
        "fetched_at",
        "status",
        "error_message",
    ]
 
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
 
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
 
 
def save_article_content_generic(
    url_norm: str,
    title: str | None,
    md: str | None,
    html: str | None,
    text_clean: str,
) -> str:
    blob = (md if (md and md.strip()) else (html or "")).encode("utf-8", "ignore")
    content_hash = sha256(blob).hexdigest()
    text_hash = sha256(text_clean.encode("utf-8", "ignore")).hexdigest()
 
    content_doc = {
        "hash": content_hash,
        "text_hash": text_hash,
        "markdown": md,
        "html": html,
        "text": text_clean,
        "text_len": len(text_clean),
        "source_url": url_norm,
        "updated_at": datetime.now(timezone.utc),
    }
 
    content = contents.find_one_and_update(
        {"hash": content_hash},
        {"$set": content_doc},
        upsert=True,
        return_document=ReturnDocument.AFTER,
        projection={"_id": 1},
    )
 
    content_id = str(content["_id"])
 
    articles.update_one(
        {"url": url_norm},
        {
            "$set": {
                "url": url_norm,
                "title": title or "",
                "content_id": content["_id"],
                "fetched_at": datetime.now(timezone.utc),
                "site": extract_site(url_norm),
                "domain": extract_domain(url_norm),
                "crawler": "fccna_worker",
            }
        },
        upsert=True,
    )
 
    return content_id
 
 

# =========================
# Block Detection
# =========================
def detect_block_reason(
    html: Optional[str] = None,
    md: Optional[str] = None,
    error_message: str = "",
    url: str = "",
) -> Optional[str]:
    """
    偵測是否被封鎖，回傳原因字串；無封鎖跡象則回傳 None。
    檢查優先順序：error_message → 頁面特徵 → 內容過短。
    """
    err = error_message.lower()

    # 1. 從 error message 判斷
    if "403" in err:
        return "http_403"
    if "429" in err:
        return "http_429_rate_limit"
    if "407" in err:
        return "http_407_proxy_auth"
    if "503" in err:
        return "http_503"
    if "timeout" in err or "timed out" in err:
        return "connection_timeout"
    if "connection" in err and "refused" in err:
        return "connection_refused"

    # 2. 從頁面內容判斷
    content = (html or "") + (md or "")
    if not content.strip():
        return None  # 空頁面另有原因，不算封鎖

    for sig in BLOCK_SIGNATURES:
        if sig in content:
            return f"block_signature:{sig[:40]}"

    # 3. 頁面有回應但文字極少（軟封鎖）
    visible_len = _visible_text_len_from_html(html) if html else 0

    # Cloudflare 高防護網站（如 chinatimes）軟封鎖門檻拉高：
    # CF challenge 通過失敗時頁面仍有一定文字量但不含正文
    domain = extract_domain(url) if url else ""
    soft_block_threshold = 300 if domain in {"www.chinatimes.com"} else 50

    if 0 < visible_len < soft_block_threshold and html and len(html) > 500:
        return "suspected_soft_block"

    return None


MAX_PROXY_RETRIES = int(os.getenv("MAX_PROXY_RETRIES", "3"))  # 被封後最多換幾次 proxy 重試

def crawl_one_url(job_id: str, url: str) -> Dict[str, Any]:
    """
    爬取單一 URL。
    - 自動偵測 IP 封鎖特徵
    - 封鎖時切換 proxy 重試（最多 MAX_PROXY_RETRIES 次）
    - 回傳 row dict，status: content_saved / failed / blocked
    """
    row: Dict[str, Any] = {
        "job_id": job_id,
        "url": url,
        "normalized_url": normalize_url(url),
        "domain": extract_domain(normalize_url(url)),
        "site": extract_site(normalize_url(url)),
        "title": "",
        "text": "",
        "text_len": 0,
        "fetched_at": utc_now_iso(),
        "status": "failed",
        "error_message": "",
        "is_blocked": False,
        "block_reason": None,
        "proxy_used": None,
        "attempts": 0,
    }

    for attempt in range(MAX_PROXY_RETRIES + 1):
        row["attempts"] = attempt + 1

        if PROXY_ROTATOR.all_blocked():
            row["status"] = "blocked"
            row["is_blocked"] = True
            row["block_reason"] = "all_proxies_cooling"
            row["error_message"] = "所有 proxy 都在冷卻中，無法繼續爬取"
            print(f"[CRAWL] {url} → 所有 proxy 冷卻中，放棄")
            return row

        proxy = PROXY_ROTATOR.current()
        row["proxy_used"] = proxy or "直連"

        # ✅ 正確做法：透過環境變數注入 proxy，讓 Firecrawl 底層 Playwright 自動讀取
        set_proxy_env(proxy)

        # 每次請求換一組 UA，降低特徵固定被識別的風險
        _ua, _sec_ch_ua, _sec_ch_ua_platform = pick_random_ua()
        LOCAL.common_headers["User-Agent"] = _ua
        LOCAL.common_headers["_sec_ch_ua"] = _sec_ch_ua
        LOCAL.common_headers["_sec_ch_ua_platform"] = _sec_ch_ua_platform

        try:
            profile = get_domain_profile(url)
            # 部分網站（如 Yahoo）需要 onlyMainContent，其他用全域設定
            only_main = profile.get("only_main_content", FIRECRAWL_ONLY_MAIN_CONTENT)
            api_res = LOCAL.scrape(
                url,
                wait_ms=profile["wait_ms"],
                referer=profile.get("referer", ""),
                only_main_content=only_main,
            )
            inspect_firecrawl_response(api_res)

            norm_url, md, html, title = standardize_page(api_res, url)
            text_clean = html_or_markdown_to_clean_text(md, html, norm_url)

            # ── 封鎖偵測 ──────────────────────────────────────────
            block_reason = detect_block_reason(html=html, md=md, url=url)
            if block_reason:
                print(f"[BLOCKED] attempt={attempt + 1} proxy={proxy or '直連'} "
                      f"url={url} reason={block_reason}")
                PROXY_ROTATOR.mark_blocked(proxy)
                PROXY_ROTATOR.rotate()
                wait = random.uniform(5.0, 12.0)
                print(f"[BLOCKED] 等待 {wait:.1f} 秒後重試...")
                time.sleep(wait)
                continue
            # ─────────────────────────────────────────────────────

            row["normalized_url"] = norm_url
            row["domain"] = extract_domain(norm_url)
            row["site"] = extract_site(norm_url)
            row["title"] = title or ""
            row["text"] = text_clean or ""
            row["text_len"] = len(text_clean or "")

            if not text_clean:
                row["status"] = "failed"
                row["error_message"] = (
                    "empty extracted content; "
                    f"markdown_len={len(md or '')}, "
                    f"html_len={len(html or '')}"
                )
                return row

            if len(text_clean) < MIN_CONTENT_LEN:
                row["status"] = "failed"
                row["error_message"] = f"content too short ({len(text_clean)} < {MIN_CONTENT_LEN})"
                return row

            save_article_content_generic(norm_url, title, md, html, text_clean)

            row["status"] = "content_saved"
            row["error_message"] = ""
            return row

        except Exception as error:
            err_str = str(error)
            block_reason = detect_block_reason(error_message=err_str, url=url)
            if block_reason:
                print(f"[BLOCKED] attempt={attempt + 1} proxy={proxy or '直連'} "
                      f"exception → {block_reason}")
                PROXY_ROTATOR.mark_blocked(proxy)
                PROXY_ROTATOR.rotate()
                wait = random.uniform(5.0, 12.0)
                time.sleep(wait)
                continue
            # 非封鎖錯誤，不換 proxy，直接回傳失敗
            row["status"] = "failed"
            row["error_message"] = err_str
            return row

    # 重試次數用完，確認為封鎖
    row["status"] = "blocked"
    row["is_blocked"] = True
    row["block_reason"] = row.get("block_reason") or "max_retries_exceeded"
    row["error_message"] = f"已嘗試 {MAX_PROXY_RETRIES + 1} 次，所有 proxy 均被封鎖"
    return row

def run_job(job: Dict[str, Any], export_dir: str) -> Dict[str, Any]:
    job_id = str(job["job_id"])
    raw_urls = job.get("urls", [])
    urls = prepare_urls(raw_urls)

    export_dir_path = ensure_export_dir(export_dir)
    jsonl_path = export_dir_path / f"{job_id}.jsonl"
    csv_path = export_dir_path / f"{job_id}.csv"

    result: Dict[str, Any] = {
        "job_id": job_id,
        "source": job.get("source", "backend_unknown_urls"),
        "created_at": job.get("created_at"),
        "started_at": utc_now_iso(),
        "input_count": len(raw_urls),
        "prepared_count": len(urls),
        "success_count": 0,
        "failed_count": 0,
        "blocked_count": 0,
        "jsonl_path": str(jsonl_path),
        "csv_path": str(csv_path),
    }

    if not urls:
        write_jsonl([], jsonl_path)
        write_csv([], csv_path)
        result["finished_at"] = utc_now_iso()
        return result

    mark_urls_status(
        urls,
        status="crawling",
        job_id=job_id,
        extra={"crawl_started_at": utc_now_iso()},
    )

    rows: List[Dict[str, Any]] = []
    success_urls: List[str] = []
    failed_urls: List[str] = []
    blocked_urls: List[str] = []

    for i, url in enumerate(urls):
        t_start = time.perf_counter()
        row = crawl_one_url(job_id, url)
        t_elapsed = time.perf_counter() - t_start
        print(f"爬取單一網頁耗時{t_elapsed:.2f} 秒")
        rows.append(row)

        status = row["status"]
        norm_url = row["normalized_url"]

        if status == "content_saved":
            # ── 成功 ──────────────────────────────────────────────
            success_urls.append(norm_url)
            mark_single_url_status(
                norm_url,
                status="content_saved",
                job_id=job_id,
                extra={"crawl_finished_at": utc_now_iso()},
            )
            # 爬完一篇立刻輸出單篇 CSV，讓後端可以更早送 mBERT
            url_slug = re.sub(r"[^\w]", "_", norm_url)[-40:]
            single_csv_path = export_dir_path / f"{job_id}_{url_slug}.csv"
            write_csv([row], single_csv_path)
            print(f"[CRAWL] ({i+1}/{len(urls)}) ✓ {url} (耗時 {t_elapsed:.2f} 秒)")

        elif status == "blocked":
            # ── IP 封鎖 ───────────────────────────────────────────
            blocked_urls.append(norm_url)
            failed_urls.append(norm_url)
            mark_single_url_status(
                norm_url,
                status="blocked",
                job_id=job_id,
                extra={
                    "crawl_finished_at": utc_now_iso(),
                    "block_reason": row.get("block_reason"),
                    "last_error_message": row["error_message"],
                },
            )
            print(f"[CRAWL] ({i+1}/{len(urls)}) ✗ BLOCKED {url} → {row.get('block_reason')} (耗時 {t_elapsed:.2f} 秒)")

        else:
            # ── 一般失敗（非封鎖）────────────────────────────────
            failed_urls.append(norm_url)
            mark_single_url_status(
                norm_url,
                status="failed",
                job_id=job_id,
                extra={
                    "crawl_finished_at": utc_now_iso(),
                    "last_error_message": row["error_message"],
                },
            )
            print(f"[CRAWL] ({i+1}/{len(urls)}) ✗ {url} → {row['error_message'][:80]} (耗時 {t_elapsed:.2f} 秒)")

        # ── 請求間隔（最後一筆 or 被封後不等）───────────────────
        if i < len(urls) - 1 and status != "blocked":
            delay = random.uniform(CRAWL_DELAY_MIN, CRAWL_DELAY_MAX)
            print(f"[CRAWL] 等待 {delay:.1f} 秒...")
            time.sleep(delay)

    # 全部跑完，輸出完整批次 CSV/JSONL（備份用）
    write_jsonl(rows, jsonl_path)
    write_csv(rows, csv_path)

    final_status = (
        "content_saved" if not failed_urls
        else "blocked" if blocked_urls and not success_urls
        else "partial_done"
    )

    mark_urls_status(
        urls,
        status=final_status,
        job_id=job_id,
        extra={
            "export_jsonl_path": str(jsonl_path),
            "export_csv_path": str(csv_path),
            "job_finished_at": utc_now_iso(),
            "blocked_count": len(blocked_urls),
        },
    )

    result["success_count"] = len(success_urls)
    result["failed_count"] = len(failed_urls)
    result["blocked_count"] = len(blocked_urls)
    result["finished_at"] = utc_now_iso()

    print(f"[JOB] {job_id} 完成 → 成功:{len(success_urls)} "
          f"失敗:{len(failed_urls) - len(blocked_urls)} 封鎖:{len(blocked_urls)}")
    return result

class CrawlScheduler:
    """
    管理批次爬取的執行順序，支援 hover URL 插隊優先處理。
 
    - batch_urls:  當前批次所有 URL（用來判斷 hover 的 URL 是否在批次裡）
    - done_urls:   已爬完的 URL（避免重複爬取）
    - priority_queue: hover 插隊用，優先於批次佇列
    - batch_queue:    正常批次順序
    """
 
    def __init__(self, job: Dict[str, Any], export_dir: str):
        self.job_id = str(job["job_id"])
        self.export_dir = export_dir
        self.export_dir_path = ensure_export_dir(export_dir)
 
        # 準備 URL 清單
        urls = prepare_urls(job.get("urls", []))
 
        # 共享狀態（thread-safe）
        self._lock = threading.Lock()
        self.batch_urls: set[str] = set(urls)   # 當前批次所有 URL
        self.done_urls: set[str] = set()         # 已爬完的 URL
        self.priority_queue: queue.Queue = queue.Queue()
        self.batch_queue: queue.Queue = queue.Queue()
 
        # 把批次 URL 放進批次佇列
        for url in urls:
            self.batch_queue.put(url)
 
        # 結果收集
        self.rows: List[Dict[str, Any]] = []
        self.success_urls: List[str] = []
        self.failed_urls: List[str] = []
 
        # 標記批次開始
        if urls:
            mark_urls_status(
                urls,
                status="crawling",
                job_id=self.job_id,
                extra={"crawl_started_at": utc_now_iso()},
            )
 
    def hover(self, url: str) -> str:
        """
        使用者 hover 某個 URL 時呼叫。
 
        回傳值：
        - "queued"：URL 在批次裡且尚未爬取，已插入優先佇列
        - "already_done"：URL 已爬完，直接查 DB 即可
        - "not_in_batch"：URL 不在批次裡，不處理
        """
        normalized = normalize_url(url)
 
        with self._lock:
            if normalized not in self.batch_urls:
                return "not_in_batch"
 
            if normalized in self.done_urls:
                return "already_done"
 
            # 插入優先佇列
            self.priority_queue.put(normalized)
            return "queued"
 
    def _next_url(self) -> Optional[str]:
        """
        取下一個要爬的 URL。
        優先佇列有東西就先取優先佇列，否則取批次佇列。
        """
        # 優先佇列優先
        try:
            return self.priority_queue.get_nowait()
        except queue.Empty:
            pass
 
        # 批次佇列
        try:
            return self.batch_queue.get_nowait()
        except queue.Empty:
            return None
 
    def _process_row(self, row: Dict[str, Any]) -> None:
        """處理爬取結果，寫 CSV、更新狀態。"""
        with self._lock:
            self.rows.append(row)
 
        if row["status"] == "content_saved":
            with self._lock:
                self.success_urls.append(row["normalized_url"])
                self.done_urls.add(row["normalized_url"])
 
            mark_single_url_status(
                row["normalized_url"],
                status="content_saved",
                job_id=self.job_id,
                extra={"crawl_finished_at": utc_now_iso()},
            )
 
            # 爬完一篇立刻輸出單篇 CSV
            url_slug = re.sub(r"[^\w]", "_", row["normalized_url"])[-40:]
            single_csv_path = self.export_dir_path / f"{self.job_id}_{url_slug}.csv"
            write_csv([row], single_csv_path)
 
        elif row["status"] == "blocked":
            # ── IP 封鎖，與一般失敗分開記錄 ──────────────────────
            with self._lock:
                self.failed_urls.append(row["normalized_url"])
                self.done_urls.add(row["normalized_url"])

            mark_single_url_status(
                row["normalized_url"],
                status="blocked",
                job_id=self.job_id,
                extra={
                    "crawl_finished_at": utc_now_iso(),
                    "block_reason": row.get("block_reason"),
                    "last_error_message": row["error_message"],
                },
            )

        else:
            # ── 一般失敗（非封鎖）────────────────────────────────
            with self._lock:
                self.failed_urls.append(row["normalized_url"])
                self.done_urls.add(row["normalized_url"])

            mark_single_url_status(
                row["normalized_url"],
                status="failed",
                job_id=self.job_id,
                extra={
                    "crawl_finished_at": utc_now_iso(),
                    "last_error_message": row["error_message"],
                },
            )

    def run(self) -> Dict[str, Any]:
        """
        執行批次爬取，支援 hover 插隊。
        每次取 URL 前先檢查優先佇列，有的話優先處理。
        已爬完的 URL 自動跳過不重複爬。
        """
        jsonl_path = self.export_dir_path / f"{self.job_id}.jsonl"
        csv_path = self.export_dir_path / f"{self.job_id}.csv"
 
        result: Dict[str, Any] = {
            "job_id": self.job_id,
            "started_at": utc_now_iso(),
            "success_count": 0,
            "failed_count": 0,
            "jsonl_path": str(jsonl_path),
            "csv_path": str(csv_path),
        }
 
        total = len(self.batch_urls)
        processed = 0

        while processed < total:
            url = self._next_url()

            if url is None:
                # 佇列暫時空了（可能 hover 插隊還沒進來），稍等
                time.sleep(0.1)
                continue

            # 已爬完的跳過（避免 hover 插隊重複爬）
            with self._lock:
                if url in self.done_urls:
                    processed += 1
                    continue

            t_start = time.perf_counter()
            row = crawl_one_url(self.job_id, url)
            t_elapsed = time.perf_counter() - t_start
            row["crawl_duration"] = t_elapsed
            self._process_row(row)
            processed += 1

            # ── 請求間隔（最後一筆 or 封鎖後不等）─────────────
            if processed < total and row["status"] != "blocked":
                delay = random.uniform(CRAWL_DELAY_MIN, CRAWL_DELAY_MAX)
                time.sleep(delay)

        # 全部跑完，輸出完整批次檔案
        write_jsonl(self.rows, jsonl_path)
        write_csv(self.rows, csv_path)

        final_status = (
            "content_saved" if not self.failed_urls
            else "partial_done"
        )

        mark_urls_status(
            list(self.batch_urls),
            status=final_status,
            job_id=self.job_id,
            extra={
                "export_jsonl_path": str(jsonl_path),
                "export_csv_path": str(csv_path),
                "job_finished_at": utc_now_iso(),
            },
        )

        result["success_count"] = len(self.success_urls)
        result["failed_count"] = len(self.failed_urls)
        result["finished_at"] = utc_now_iso()

        return result

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backend-triggered crawler worker")
    parser.add_argument("--job", required=True, help="Path to job json file")
    parser.add_argument("--export-dir", default=DEFAULT_EXPORT_DIR, help="Directory to write jsonl/csv")
    return parser.parse_args()
 
 
def main() -> None:
    args = parse_args()
    job = load_job(args.job)
    result = run_job(job, args.export_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
 
 
if __name__ == "__main__":
    main()