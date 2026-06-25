# -*- coding: utf-8 -*-
"""
Firecrawl 混合範例：
1) crawl() 先抓取多頁 URL（控制 maxDepth / limit / includePaths / excludePaths）
2) scrape() 再對每頁做主內容擷取與格式輸出（formats / onlyMainContent / excludeTags）

支援模式：
- 本地 self-hosted server: 需指定 base_url（api_url），api_key 可用 "local" / "dummy"
- 官方雲端 API: 需提供 FIRECRAWL_API_KEY
"""
import os
import sys
import time
import argparse
from typing import Any, Dict, List, Iterable, Optional

from firecrawl import FirecrawlApp


# --------- 工具函式 ---------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def to_list(maybe_list) -> List:
    if maybe_list is None:
        return []
    if isinstance(maybe_list, list):
        return maybe_list
    return [maybe_list]


def extract_pages(crawl_result: Any) -> List[Dict[str, Any]]:
    """
    兼容不同 SDK 版本的回傳：
    - 有些版本 crawl() 直接回傳 List[Page]
    - 有些版本回傳 { "data": [Page, ...], ... }
    """
    if isinstance(crawl_result, list):
        return crawl_result
    if isinstance(crawl_result, dict) and "data" in crawl_result and isinstance(crawl_result["data"], list):
        return crawl_result["data"]
    # 盡力一搏：若看起來像單頁也包成 list
    return to_list(crawl_result)


def extract_url(page: Dict[str, Any]) -> Optional[str]:
    # 常見欄位兼容
    return (
        page.get("url")
        or page.get("metadata", {}).get("url")
        or page.get("source", {}).get("url")
    )


def sleep_backoff(try_idx: int, base: float = 1.0, cap: float = 8.0):
    delay = min(cap, base * (2 ** (try_idx - 1)))
    time.sleep(delay)


# --------- 主要流程 ---------
def make_client(local_mode: bool, base_url: str, api_key_env: str) -> FirecrawlApp:
    """
    本地模式：
      - 你需自行啟動 Firecrawl server（例如：docker run -p 3002:3002 mendable/firecrawl）
      - SDK 仍然會檢查 api_key 不是 None/空字串，所以給個 'local' / 'dummy' 即可
    雲端模式：
      - 需提供 FIRECRAWL_API_KEY
    """
    if local_mode:
        api_key = os.getenv(api_key_env) or "local"
        print(f"[Info] Local mode → api_url={base_url}, api_key='{api_key}' (dummy)")
        return FirecrawlApp(api_key=api_key, api_url=base_url)
    else:
        api_key = os.getenv(api_key_env)
        if not api_key:
            print(f"[Error] 雲端模式需要環境變數 {api_key_env}，請先設定。", file=sys.stderr)
            sys.exit(1)
        print("[Info] Cloud mode → 使用官方 API")
        return FirecrawlApp(api_key=api_key)


def do_crawl(app: FirecrawlApp,
             start_url: str,
             max_depth: int = 1,
             limit: int = 10,
             include_paths: Optional[List[str]] = None,
             exclude_paths: Optional[List[str]] = None) -> List[str]:
    print(f"[Crawl] start_url={start_url} maxDepth={max_depth} limit={limit}")
    res = app.crawl(
        start_url,
        maxDepth=max_depth,
        limit=limit,
        includePaths=include_paths or [],
        excludePaths=exclude_paths or []
    )
    pages = extract_pages(res)
    urls: List[str] = []
    for idx, p in enumerate(pages, 1):
        u = extract_url(p)
        if u:
            urls.append(u)
            print(f"  - [{idx}] {u}")
    print(f"[Crawl] 取得 {len(urls)} 筆 URL")
    return urls


def do_scrape(app: FirecrawlApp,
              url: str,
              only_main: bool = True,
              exclude_tags: Optional[List[str]] = None,
              formats: Optional[List[str]] = None,
              wait_for_ms: Optional[int] = None,
              max_retries: int = 3) -> Dict[str, Any]:
    """
    針對單一 URL 擷取主內容。formats 常用：["markdown", "html"]
    exclude_tags 範例：["nav", "footer", "header", ".ads", ".advertisement", "#cookie-banner"]
    """
    exclude_tags = exclude_tags or []
    formats = formats or ["markdown", "html"]

    for attempt in range(1, max_retries + 1):
        try:
            res = app.scrape(
                url,
                formats=formats,
                onlyMainContent=only_main,
                excludeTags=exclude_tags,
                waitFor=wait_for_ms
            )
            return res
        except Exception as e:
            print(f"[Scrape][{attempt}/{max_retries}] {url} 失敗：{e}", file=sys.stderr)
            if attempt >= max_retries:
                raise
            sleep_backoff(attempt)
    return {}


def save_output(output_dir: str, url: str, doc: Dict[str, Any]) -> None:
    """
    以 URL 轉檔名，將 markdown / html 各存一份（若有）
    """
    os.makedirs(output_dir, exist_ok=True)
    safe_name = (
        url.replace("://", "_")
           .replace("/", "_")
           .replace("?", "_")
           .replace("&", "_")
           .replace("#", "_")
    )
    md = doc.get("markdown")
    html = doc.get("html")
    if md:
        path = os.path.join(output_dir, f"{safe_name}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"  → 保存 markdown: {path}")
    if html:
        path = os.path.join(output_dir, f"{safe_name}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  → 保存 html: {path}")


def main():
    parser = argparse.ArgumentParser(description="Firecrawl 混合範例：crawl 多頁 + scrape 主內容")
    parser.add_argument("--url", required=True, help="起始網址（例如：https://www.bbc.com/zhongwen）")
    parser.add_argument("--local", action="store_true", help="使用本地 self-hosted server（預設雲端）")
    parser.add_argument("--api-url", default="http://localhost:3002", help="本地 server 位址（local 模式時使用）")
    parser.add_argument("--api-key-env", default="FIRECRAWL_API_KEY", help="API Key 的環境變數名稱")
    parser.add_argument("--depth", type=int, default=1, help="crawl 深度（maxDepth）")
    parser.add_argument("--limit", type=int, default=10, help="crawl 頁數上限（limit）")
    parser.add_argument("--include", nargs="*", default=None, help="只包含這些 path（可多個），例：--include /news /blog")
    parser.add_argument("--exclude", nargs="*", default=None, help="排除這些 path（可多個），例：--exclude /login /privacy")
    parser.add_argument("--wait-for", type=int, default=None, help="scrape 前等待渲染毫秒數（例如 3000）")
    parser.add_argument("--out", default="./outputs", help="輸出資料夾")
    parser.add_argument("--no-main", action="store_true", help="不強制 onlyMainContent（預設為 True）")
    parser.add_argument("--more-exclude", action="store_true", help="啟用更激進的排除 selector（ads/cookie 等）")

    args = parser.parse_args()

    app = make_client(local_mode=args.local, base_url=args.api_url, api_key_env=args.api_key_env)

    include_paths = args.include or []
    exclude_paths = args.exclude or []

    urls = do_crawl(
        app,
        start_url=args.url,
        max_depth=args.depth,
        limit=args.limit,
        include_paths=include_paths,
        exclude_paths=exclude_paths
    )

    # 基礎排除選擇器
    exclude_tags = ["nav", "footer", "header"]
    if args.more_exclude:
        # 常見廣告 / 側欄 / cookie 同意 / 推薦列表等
        exclude_tags += [
            ".ads", ".ad", ".advertisement", ".sponsored",
            ".sidebar", ".related", ".recommend", ".outbrain",
            "#cookie-banner", ".cookie", ".consent", "#consent",
            "script", "style"
        ]

    only_main = not args.no_main

    print(f"[Scrape] 開始擷取（onlyMainContent={only_main}, excludeTags={exclude_tags}）")
    for i, u in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {u}")
        try:
            doc = do_scrape(
                app,
                url=u,
                only_main=only_main,
                exclude_tags=exclude_tags,
                formats=["markdown", "html"],
                wait_for_ms=args.wait_for
            )
            save_output(args.out, u, doc)
        except Exception as e:
            print(f"  × 擷取失敗：{e}", file=sys.stderr)

    print("[Done] 全部完成。")


if __name__ == "__main__":
    main()
