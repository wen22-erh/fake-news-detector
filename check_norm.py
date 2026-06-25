from urllib.parse import urlparse, parse_qsl, urlencode

def normalize_url(url: str) -> str:
    raw = str(url).strip()
    try:
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "http").lower()
        netloc = (parsed.netloc or "").lower()
        path = parsed.path or ""
        if path != "/":
            path = path.rstrip("/")
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        sorted_query = urlencode(sorted(query_pairs), doseq=True)
        normalized = f"{scheme}://{netloc}{path}"
        if sorted_query:
            normalized += f"?{sorted_query}"
        return normalized
    except Exception:
        return raw

print(normalize_url("https://www.cna.com.tw/news/ait/202605130089.aspx"))
